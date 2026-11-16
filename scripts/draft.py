# -*- coding: utf-8 -*-
"""
draft.py — 把一個缺口變成一篇「為了被 AI 引用」的文章草稿

吃什麼 → 做什麼 → 吐什麼
  吃：一個 gap（＋ 它受影響的題目、＋ 已經存下來的 AI 回答原文）
  做：產生標題、大綱、骨架內文，撈出可以用的事實，並用四條寫作規則自我檢查
  吐：drafts 表（status='draft'，永遠不自動發佈）

怎麼跑
  python scripts/draft.py --list              # 有哪些缺口可以寫
  python scripts/draft.py --gap 7 --preview   # 看會產出什麼，不寫進資料庫
  python scripts/draft.py --gap 7             # 寫進 drafts 表
  python scripts/draft.py --check 我的文章.md   # 拿四條規則檢查一篇現成的文章

⚠️ 這支腳本不會幫你寫出成稿
  它產出的是「骨架 ＋ 要填哪些事實 ＋ 規則檢查」，不是可以直接發佈的散文。
  原因：四條規則裡最硬的一條是「事實密度高、有具體數字」，
  而那些數字（價格區間、理賠上限、驗證流程）在 Fluv 手上，不在這支程式手上。
  自動生成一篇沒有數字的漂亮文章，正好違反它要遵守的規則。

  它會做的是：把骨架排好、把「這裡需要哪一種事實」標清楚，
  並且從已經存下來的 AI 回答裡撈出「AI 已經知道的 Fluv 事實」當素材。

⚠️ 永遠不自動發佈
  產出一律 status='draft'，等人審核。CLAUDE.md 的規定。

寫作規則寫在 writing_rules.py，和階段 6 的 audit.py 共用同一份 ——
不是各寫一次，因為兩份規則遲早會不一致。
"""

import argparse
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH, OUR_BRAND, ROOT_DIR, connect, now_iso  # noqa: E402
from writing_rules import check_all, format_report  # noqa: E402

# 撈事實時，一句話至少要這麼長才有資訊量
MIN_FACT_CHARS = 14
MAX_FACTS = 10

# 數字後面要跟著這些單位，才算「具體事實」。
# 不然「### 1. 毛小愛」這種清單編號也會被當成數字。
FACT_UNITS = ["元", "％", "%", "天", "小時", "分鐘", "次", "萬", "位", "家",
              "項", "日", "個月", "年", "歲", "公斤", "倍", "成"]


def _has_concrete_number(text):
    """這句話裡有沒有「具體的數字」。

    三種都算：
      1. 數字後面跟著單位      → 「3 天內」「400–800 元」
      2. 幣別符號開頭          → 「NT$3,000」「$15,000」
      3. 三位數以上的數字      → 「15000」「10 萬」
    只有一位數又沒有單位的不算 —— 那多半是清單編號或章節號，沒有資訊。
    """
    units = "|".join(re.escape(u) for u in FACT_UNITS)
    if re.search(r"\d[\d,.]*\s*(%s)" % units, text):
        return True
    if re.search(r"(NT\$|NTD|US\$|\$)\s*\d", text, re.I):
        return True
    # 三位數以上才算 —— 但不能緊貼在字母後面，
    # 不然 PRO360、maokiddo2 這種品牌名會被當成數字。
    if re.search(r"(?<![A-Za-z0-9])\d{3,}", text.replace(",", "")):
        return True
    return False


def known_facts(conn):
    """從已經存下來的 AI 回答原文裡，撈出可以用的 Fluv 事實。

    為什麼有用：那些是 AI 目前已經知道、而且願意講出來的事實
    （例如自負額 3,000 元、醫療補助上限 15,000 元、通報期限 3 天）。
    寫草稿時直接拿來用，比憑印象寫可靠 —— 而且這些就是已經被引用過的內容。

    撈的條件（三層，逐層放寬但不放進雜訊）：
      1. 這則回答有提到我們（整篇在講我們，不是在講別人）
      2. 這一句有具體數字
      3. 這一句**沒有**提到任何競品 —— 不然會把 PetBacker 的理賠條件
         誤抄成我們的。這一條很重要，抄錯數字比沒有數字更糟。

    句子裡有直接寫出品牌名的排在前面，那些最確定是在講我們。

    只掃有保留原文的那些 run。人工統計的那批沒有原文，掃不到。
    """
    competitors = [r["entity"] for r in conn.execute(
        "SELECT DISTINCT entity FROM mentions WHERE is_us=0")]
    named, unnamed, seen = [], [], set()
    rows = conn.execute(
        "SELECT raw_path FROM runs WHERE raw_path IS NOT NULL "
        "AND error IS NULL AND condition NOT IN ('demo')")
    for r in rows:
        full = os.path.join(ROOT_DIR, r["raw_path"])
        if not os.path.exists(full):
            continue
        try:
            with gzip.open(full, "rt", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            continue
        if "_demo" in payload:
            continue
        text = payload.get("answer_text") or payload.get("answer") or ""
        if not text:
            continue
        # 整篇要有提到我們，才值得從裡面撈事實
        if not any(k in text for k in ("毛小愛", "Fluv", "fluv")):
            continue
        for sent in re.split(r"[。\n！？]", text):
            s = sent.strip(" 　-·*|#>")
            if len(s) < MIN_FACT_CHARS:
                continue
            # 標題、網址、引用標記都不是事實
            if s.startswith("#") or "http" in s or re.match(r"^\[\d+\]", s):
                continue
            if re.match(r"^#{1,4}\s", sent) or re.match(r"^\d+\.\s", s):
                continue
            if not _has_concrete_number(s):
                continue
            # ⚠️ 提到競品的句子一律跳過 —— 不然會把 PetBacker 的理賠條件
            #    誤抄成我們的。抄錯數字比沒有數字更糟。
            if any(cp in s for cp in competitors):
                continue
            k = re.sub(r"\s", "", s)[:40]
            if k in seen:
                continue
            seen.add(k)
            if any(b in s for b in ("毛小愛", "Fluv", "fluv")):
                named.append(s)
            else:
                unnamed.append(s)
    # 有寫出品牌名的排前面，那些最確定是在講我們
    return named[:MAX_FACTS], unnamed[:MAX_FACTS]


def gap_evidence(conn, gap):
    """這個缺口的證據：受影響的題目，以及每一家的表現。"""
    ids = [x.strip() for x in (gap["affected_prompt_ids"] or "").split(",") if x.strip()]
    if not ids:
        return [], []
    marks = ",".join("?" * len(ids))
    prompts = list(conn.execute(
        "SELECT id, text, intent, topic FROM prompts WHERE id IN (%s) ORDER BY id" % marks,
        ids))
    per_engine = list(conn.execute(
        "SELECT r.engine, COUNT(*) n, "
        " SUM(CASE WHEN EXISTS (SELECT 1 FROM mentions m "
        "       WHERE m.run_id=r.id AND m.is_us=1) THEN 1 ELSE 0 END) hit "
        "FROM runs r WHERE r.error IS NULL AND r.condition != 'demo' "
        "AND r.prompt_id IN (%s) GROUP BY r.engine ORDER BY hit" % marks, ids))
    return prompts, per_engine


def build_draft(conn, gap):
    """產出 (標題, 大綱, 骨架內文)。

    標題直接用受影響題目裡的第一題 —— 因為規則第一條就是
    「一個標題 = 一個明確問題」，而那些題目本來就是客戶真的會打的問句。
    """
    prompts, per_engine = gap_evidence(conn, gap)
    if not prompts:
        return None, None, None

    title = prompts[0]["text"].strip()

    # H1 用第一題，所以 H2 從第二題開始 —— 不然第一個小標會跟標題一字不差
    rest = prompts[1:]
    outline = ["# %s" % title]
    if rest:
        outline.append("（第一段直接回答標題這個問題）")
    for p in rest:
        outline.append("## %s" % p["text"].strip())
    outline.append("## 常見誤解")
    outline = "\n".join(outline)

    named_facts, other_facts = known_facts(conn)

    body = []
    body.append("<!-- 這是骨架，不是成稿。每個【答案】都要換成具體事實才算完成。 -->")
    body.append("")
    # 開頭那一段回答標題的問題，不另外開小標
    body.append("【答案】第一句就把「%s」這個問題答完，而且要能單獨看懂。"
                "需要填入的事實：%s" % (title, _needed_facts(prompts[0]["intent"])))
    body.append("")
    for p in rest:
        body.append("## %s" % p["text"].strip())
        body.append("")
        body.append("【答案】這一段的第一句就要把答案講完，而且要能單獨看懂。"
                    "需要填入的事實：%s" % _needed_facts(p["intent"]))
        body.append("")
    body.append("## 常見誤解")
    body.append("")
    body.append("【答案】列出 2–3 個客戶常誤會的點，每一點都用具體數字或規則澄清。")
    body.append("")

    if named_facts or other_facts:
        body.append("<!-- 從已存下來的 AI 回答裡撈到的事實。")
        body.append("     ⚠️ 發佈前每一個數字都要跟官網條款核對一次 ——")
        body.append("        這些是 AI 講的，AI 也會講錯。")
        if named_facts:
            body.append("")
            body.append("     【句子裡直接寫了品牌名，最確定在講我們】")
            for f in named_facts:
                body.append("     · %s" % f)
        if other_facts:
            body.append("")
            body.append("     【在提到我們的回答裡出現、但該句沒寫品牌名】")
            body.append("     （已排除提到競品的句子，但還是要自己確認是在講我們）")
            for f in other_facts:
                body.append("     · %s" % f)
        body.append("-->")

    return title, outline, "\n".join(body)


def _needed_facts(intent):
    return {
        "logistics": "步驟數、每一步要準備的東西、花多少時間",
        "trust": "驗證機制的具體項目、保險公司名稱、自負額與理賠上限金額、通報期限",
        "price": "價格區間（下限–上限）、計價單位、加價條件",
        "comparison": "兩種選項各自的價格、適用情境、風險差異，用數字對比",
        "urgent": "最短可預約時間、可用時段、臨時需求的處理流程",
        "discovery": "服務涵蓋地區、保姆數量、各地區的可預約密度",
    }.get(intent, "具體數字與可驗證的事實")


def cmd_list(conn):
    rows = list(conn.execute(
        "SELECT g.id, g.title, g.status, g.opportunity_score, g.affected_prompt_ids, "
        "(SELECT COUNT(*) FROM drafts d WHERE d.gap_id=g.id) nd "
        "FROM gaps g ORDER BY g.opportunity_score DESC"))
    if not rows:
        print("gaps 表是空的。請先跑：python scripts/gaps.py")
        return
    print("  %4s %6s %-34s %-12s %s" % ("缺口", "分數", "標題", "狀態", "已有草稿"))
    print("  " + "-" * 76)
    for r in rows:
        print("  #%-3s %6.2f %-34s %-12s %s 篇"
              % (r["id"], r["opportunity_score"] or 0, r["title"][:34],
                 r["status"], r["nd"]))
    print()
    print("  寫其中一個：python scripts/draft.py --gap <缺口編號> --preview")


def cmd_check(path):
    """拿四條規則檢查一篇現成的文章（Markdown 或純文字）。"""
    if not os.path.exists(path):
        print("找不到檔案：%s" % path)
        return 1
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    # 第一個 # 標題當 title，其餘當 body
    m = re.search(r"^\s{0,3}#\s+(.*)$", raw, re.M)
    title = m.group(1).strip() if m else os.path.basename(path)
    body = raw[m.end():] if m else raw
    print("檔案：%s" % path)
    print("標題：%s" % title)
    print()
    print(format_report(check_all(title, body)))
    return 0


def cmd_draft(conn, gap_id, preview):
    gap = conn.execute("SELECT * FROM gaps WHERE id=?", (gap_id,)).fetchone()
    if not gap:
        print("找不到缺口 #%s。用 --list 看有哪些。" % gap_id)
        return 1

    title, outline, body = build_draft(conn, gap)
    if title is None:
        print("缺口 #%s 沒有關聯到任何題目，產不出草稿。" % gap_id)
        return 1

    prompts, per_engine = gap_evidence(conn, gap)

    print("=" * 76)
    print("缺口 #%s　%s　（機會分數 %.2f）" % (gap["id"], gap["title"],
                                        gap["opportunity_score"] or 0))
    print("=" * 76)
    print()
    print("【各家目前的表現】")
    for e in per_engine:
        print("  %-12s %d / %d 則提到我們" % (e["engine"], e["hit"], e["n"]))
    print()
    print("【建議標題】%s" % title)
    print("  （直接用客戶真的會打的那句問句 —— 規則第一條：一個標題 = 一個明確問題）")
    print()
    print("【大綱】")
    for ln in outline.split("\n"):
        print("  %s" % ln)
    print()
    print("【骨架內文】")
    for ln in body.split("\n"):
        print("  %s" % ln)
    print()
    print("【把骨架拿去跑四條規則】")
    print("  骨架本來就會卡在「事實密度」—— 那正是它在告訴你還要填什麼。")
    print()
    print(format_report(check_all(title, body)))

    if preview:
        print("（--preview：沒有寫進資料庫）")
        return 0

    conn.execute(
        "INSERT INTO drafts (gap_id, title, outline, body, status, action_id, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (gap["id"], title, outline, body, "draft", None, now_iso()))
    conn.commit()
    print("✅ 已寫入 drafts（status='draft'）。")
    print("   草稿永遠不會自動發佈 —— 到「機會建議」頁審核、補上事實之後再發。")
    print()
    print("   補完內容之後，可以用這個再檢查一次：")
    print("     python scripts/draft.py --check <你的檔案>")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="把缺口變成「為了被 AI 引用」的文章草稿",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/draft.py --list\n"
               "  python scripts/draft.py --gap 7 --preview\n"
               "  python scripts/draft.py --check drafts/我的文章.md\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--list", action="store_true", help="列出有哪些缺口可以寫")
    parser.add_argument("--gap", type=int, help="要寫哪一個缺口")
    parser.add_argument("--preview", action="store_true", help="只印出來，不寫進資料庫")
    parser.add_argument("--check", metavar="檔案",
                        help="拿四條寫作規則檢查一篇現成的文章")
    args = parser.parse_args()

    if args.check:
        sys.exit(cmd_check(args.check))

    conn = connect(args.db)
    if args.list or args.gap is None:
        cmd_list(conn)
        return
    sys.exit(cmd_draft(conn, args.gap, args.preview))


if __name__ == "__main__":
    main()
