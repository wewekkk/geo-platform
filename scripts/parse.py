# -*- coding: utf-8 -*-
"""
parse.py — 逐則判讀原始回答

吃什麼 → 做什麼 → 吐什麼
  吃：runs 表裡的紀錄 → 照 raw_path 讀出 data/raw/ 底下的原始回答
  做：用便宜的模型逐則判讀四件事
      ① 有沒有提到我們（Fluv 毛小愛）
      ② 提到的品牌各排第幾
      ③ 每個提及的口氣是正面／中性／負面，以及是不是風險提及
      ④ 這則回答引用了哪些來源
  吐：mentions／citations／domains 三張表

怎麼跑
  python scripts/parse.py                 # 判讀還沒判讀過的
  python scripts/parse.py --dry-run       # 只印第一則的提示詞與花費預估，不呼叫 API
  python scripts/parse.py --rerun-all     # 全部重跑（改了判讀邏輯之後用這個）
  python scripts/parse.py --week 2026-W47 # 只跑某一週

前置條件
  1. collect.py 已經跑過，data/raw/ 底下有檔案
  2. .env 裡有 ANTHROPIC_API_KEY（判讀用的）

═══════════════════════════════════════════════════════════════════════
⚠️ 判讀器不可以知道誰是客戶
═══════════════════════════════════════════════════════════════════════

送給模型的提示詞裡**完全沒有** Fluv、毛小愛、「我們」、「客戶」這些字。
模型拿到的是一則回答，任務是「把裡面提到的寵物照護平台全部列出來」，
它不知道誰付錢。

`is_us` 是在 Python 裡、拿到模型回覆之後才標上去的 —— 比對 common.OUR_BRAND。
這跟 eval/label.py 的盲測是同一條規則：還原身分是在寫入的那一刻才做。

理由：如果判讀器知道哪個是客戶，模糊的提及會被判成「有提到」，
中性的句子會被判成正面。那樣算出來的能見度是自己騙自己。

═══════════════════════════════════════════════════════════════════════
⚠️ 這是整個系統唯一真正的品質關卡，順序不可以顛倒（CLAUDE.md 階段 2）
═══════════════════════════════════════════════════════════════════════

  1. 先由人手工標記 100 則 → eval/labeled.csv（這是標準答案）
  2. 才寫這支的判讀邏輯
  3. 用 eval/check_judge.py 拿判讀結果跟標準答案比對，一致率要 ≥ 90%

  理由：判讀器不會壞、不會報錯，就是默默判錯一成多，
       而沒有人會去讀那幾千則原文。

  ⚠️ 本檔寫於標準答案還沒產出之前。程式結構是完整的，但
     **判讀品質尚未驗收過**。跑出來的數字在 check_judge.py 通過之前
     不可以當成正式指標。驗收不過的話，要改的是 JUDGE_PROMPT，
     然後 --rerun-all 整批重算。

⚠️ 重跑是設計的一部分
  原始檔案永不修改，所以判讀邏輯改了就整批重算，歷史數字全部跟著更新。
  --rerun-all 會先刪掉舊的 mentions／citations 再重建，不是疊加。
"""

import argparse
import gzip
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    DEFAULT_DB_PATH, OUR_BRAND, OUR_DOMAIN, ROOT_DIR, SENTIMENT_LABELS,
    SOURCE_TYPES, connect, get_api_key, iso_year_week,
)

# 判讀用的模型。
#
# 這裡跟 collect.py 的選法刻意相反 —— collect 要用消費者實際會碰到的旗艦模型，
# parse 只是在讀一段中文、照規則填表，用便宜的就夠，而且要跑幾千則。
# 一致率撐不到 90% 的話，先改提示詞；提示詞改到底還是不行，才換大模型。
JUDGE_MODEL = "claude-haiku-4-5-20251001"

# 每百萬 token 的價錢（美金）：(輸入, 輸出)
PRICING = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}

# --dry-run 估算用。一則回答約 1,000～1,500 字中文，加上提示詞本身。
EST_INPUT_TOKENS = 2500
EST_OUTPUT_TOKENS = 500

MAX_TOKENS = 2000
SLEEP_BETWEEN_CALLS = 0.3

# 跑到這個金額就停下來。判讀很便宜，所以這條線設得低 ——
# 真的撞到了，表示有東西不對（例如在無窮迴圈裡重跑同一則）。
DEFAULT_MAX_COST = 5.00

# ---------------------------------------------------------------------------
# 品牌名稱正規化
# ---------------------------------------------------------------------------
# 模型是自由作答的，同一家會寫成「毛小愛」「Fluv」「毛小愛 Fluv」「毛小愛（Fluv）」。
# 不合併的話，SoV 的分母會被灌水，平均排序也會算錯。
#
# ⚠️ 這張表只合併「已知品牌的不同寫法」，不是品牌白名單 ——
#    模型找到表上沒有的平台，照樣會以它自己的寫法存進去。
#    這點跟寫死品牌清單的做法不一樣，不會因為漏列而無聲地少算競品。
#    （這次就是靠這個機制才發現 maokiddo、託毛所 PetchQ。）
BRAND_ALIASES = {
    OUR_BRAND:        ["毛小愛", "fluv"],
    "PetBacker":      ["petbacker", "貝背客"],
    "PRO360":         ["pro360", "達人網"],
    "爪記 Pawjot":     ["pawjot", "爪記"],
    "maokiddo":       ["maokiddo"],
    "託毛所 PetchQ":   ["petchq", "託毛所"],
    "毛孩居服員":      ["毛孩居服員"],
    "Rover":          ["rover"],
    "Toby":           ["toby"],
}


def canonical_brand(name):
    """把模型寫的品牌名收斂成標準寫法。對不上的原樣保留。"""
    low = (name or "").strip().lower()
    if not low:
        return None
    for canon, aliases in BRAND_ALIASES.items():
        if any(a in low for a in aliases):
            return canon
    return name.strip()


def is_our_brand(canon):
    return canon == OUR_BRAND


# ---------------------------------------------------------------------------
# 判讀提示詞
# ---------------------------------------------------------------------------
# 刻意不提任何品牌名，也不說哪一家是客戶。見檔頭的盲測說明。
JUDGE_PROMPT = """你在替一份市場調查做資料整理。

以下是某個 AI 助理回答台灣飼主提問的內容。請你讀完，把兩件事整理成 JSON。

【一】這則回答裡提到了哪些「寵物照護服務平台或品牌」

平台／品牌指的是：寵物保母媒合平台、寵物照護 App、接案媒合網站、
連鎖寵物旅館品牌、個人工作室品牌。

不要列入：
- 一般名詞（「寵物保母」「到府照顧」「寵物旅館」這類服務類型）
- 社群平台本身（Facebook、Instagram、Dcard、LINE、Threads）
- 保險公司、動物醫院、政府機關
- 只在「資料來源」標註裡出現、本文沒有討論到的名稱

每一個平台要給：
- name：回答裡寫的名稱，原樣抄下來
- position：第幾個被提到（依第一次出現的先後，1 起算）
- sentiment：這則回答對它的口氣，只能填 positive / neutral / negative
    positive = 明確推薦、稱讚它的審核嚴格／保障完整／介面好用
    neutral  = 只是列出來、陳述事實、或優缺點並陳
    negative = 明確指出缺點、風險、品質不穩、理賠困難
- risk：true 或 false。只有當這則回答會讓讀者「不敢用這個平台」時才填 true
    （例如：出事不賠、保母素質堪慮、有糾紛案例）
- snippet：原文中最能支持你這個判斷的那一句，原樣抄，不要改寫，不超過 100 字

【二】這則回答引用了哪些資料來源

來源通常長得像「（來源：fluv.com）」「[來源：自由時報]」，或段落後面單獨一行的網站名。
每一個來源要給：
- source：來源名稱或網域，原樣抄
- url：如果原文有完整網址就填，沒有就填 null
- type：只能填 社群 / 媒體 / 品牌內容 / 論壇
    社群 = Facebook、Instagram、Threads、Medium、LINE
    媒體 = 新聞媒體、電視台
    論壇 = Dcard、PTT、Reddit
    品牌內容 = 公司官網、品牌部落格、業者自己寫的介紹文

只輸出 JSON，不要有其他文字，格式如下：
{{"brands": [{{"name": "...", "position": 1, "sentiment": "neutral", "risk": false, "snippet": "..."}}],
 "sources": [{{"source": "...", "url": null, "type": "品牌內容"}}]}}

如果一個平台都沒提到，brands 就給空陣列。

=== 回答內容開始 ===
{answer}
=== 回答內容結束 ===
"""


# ---------------------------------------------------------------------------
# 呼叫模型
# ---------------------------------------------------------------------------

def call_judge(answer_text, model, api_key):
    """丟一則回答給模型判讀。回傳 (結果 dict, 輸入 token, 輸出 token)。"""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user",
                   "content": JUDGE_PROMPT.format(answer=answer_text)}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return parse_json(text), resp.usage.input_tokens, resp.usage.output_tokens


def parse_json(text):
    """模型偶爾會在 JSON 前後多包一層 ```json，剝掉再解析。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        # 退一步：抓最外層的大括號。真的解不出來就讓呼叫端知道。
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


# ---------------------------------------------------------------------------
# 原始檔
# ---------------------------------------------------------------------------

def load_raw(raw_path):
    full = raw_path if os.path.isabs(raw_path) else os.path.join(ROOT_DIR, raw_path)
    with gzip.open(full, "rt", encoding="utf-8") as f:
        return json.load(f)


def structured_urls(payload):
    """從原始檔裡挖出「有結構的引用網址」。

    API 收回來的有真網址；瀏覽器手動收的只有來源名稱（「來源：Fluv」），
    那種挖不到，後面只能靠模型從文字裡讀。差別會記在統計裡。
    """
    resp = payload.get("response") or {}
    urls = []
    for c in resp.get("citations") or []:
        u = c.get("url") if isinstance(c, dict) else c
        if u:
            urls.append(u)
    urls += [u for u in (resp.get("urls_found") or []) if u]
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def domain_of(url_or_name):
    m = re.search(r"https?://([^/\s]+)", url_or_name or "")
    host = m.group(1) if m else (url_or_name or "")
    host = host.strip().lower().lstrip("www.")
    return host if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host) else ""


def is_our_domain(dom):
    return bool(dom) and (dom == OUR_DOMAIN or dom.endswith("." + OUR_DOMAIN))


# ---------------------------------------------------------------------------
# 寫入
# ---------------------------------------------------------------------------

def save_one(conn, run, result, struct_urls, ts):
    """把一則的判讀結果寫進三張表。回傳 (mentions 數, citations 數, 無網址數)。"""
    n_m = n_c = n_nourl = 0

    for b in result.get("brands") or []:
        canon = canonical_brand(b.get("name"))
        if not canon:
            continue
        label = (b.get("sentiment") or "neutral").strip()
        if label not in SENTIMENT_LABELS:
            label = "neutral"
        score = {"positive": 0.6, "neutral": 0.0, "negative": -0.6}[label]
        conn.execute(
            "INSERT INTO mentions (run_id,entity,is_us,position,sentiment_score,"
            "sentiment_label,risk_flag,snippet) VALUES (?,?,?,?,?,?,?,?)",
            (run["id"], canon, 1 if is_our_brand(canon) else 0,
             _int(b.get("position")), score, label,
             1 if b.get("risk") else 0, (b.get("snippet") or "")[:300]))
        n_m += 1

    # 先把原始檔裡有結構的網址依網域建索引 —— 模型讀到的來源名稱
    # 能對上網域的，就把真網址補回去。這樣 audit.py 才有頁面可以檢查。
    by_domain = {}
    for u in struct_urls:
        by_domain.setdefault(domain_of(u), u)

    rank = 0
    used = set()
    for s in result.get("sources") or []:
        raw_name = (s.get("source") or "").strip()
        url = (s.get("url") or "").strip()
        dom = domain_of(url) or domain_of(raw_name) or _slug(raw_name)
        if not dom or dom in used:
            continue
        if not url and dom in by_domain:
            url = by_domain[dom]
        used.add(dom)
        rank += 1
        stype = s.get("type") if s.get("type") in SOURCE_TYPES else None
        conn.execute(
            "INSERT INTO citations (run_id,url,domain,source_type,rank) VALUES (?,?,?,?,?)",
            (run["id"], url, dom, stype, rank))
        conn.execute(
            "INSERT OR IGNORE INTO domains (domain,source_type,is_ours,first_seen,notes)"
            " VALUES (?,?,?,?,?)",
            (dom, stype, 1 if is_our_domain(dom) else 0, (run["ts"] or ts)[:10], None))
        n_c += 1
        if not url:
            n_nourl += 1

    # 原始檔裡有、但模型沒讀到的網址，照樣算一筆引用 —— 它確實被引用了
    for dom, u in by_domain.items():
        if dom and dom not in used:
            rank += 1
            conn.execute(
                "INSERT INTO citations (run_id,url,domain,source_type,rank) VALUES (?,?,?,?,?)",
                (run["id"], u, dom, None, rank))
            conn.execute(
                "INSERT OR IGNORE INTO domains (domain,source_type,is_ours,first_seen,notes)"
                " VALUES (?,?,?,?,?)",
                (dom, None, 1 if is_our_domain(dom) else 0, (run["ts"] or ts)[:10],
                 "原始檔有網址，但判讀器沒從文字裡讀到"))
            n_c += 1

    return n_m, n_c, n_nourl


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _slug(name):
    """來源只有名稱、連網域都湊不出來時的退路。保留原名才查得回去。"""
    s = re.sub(r"\s+", "-", (name or "").strip())
    return ("name:" + s)[:80] if s else ""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def pick_runs(conn, rerun_all, week, limit):
    sql = ("SELECT r.id, r.prompt_id, r.engine, r.ts, r.raw_path, r.condition "
           "FROM runs r WHERE r.raw_path IS NOT NULL AND TRIM(r.raw_path) != '' "
           "AND (r.error IS NULL OR r.error = '')")
    if not rerun_all:
        sql += " AND NOT EXISTS (SELECT 1 FROM mentions m WHERE m.run_id = r.id)"
    rows = [r for r in conn.execute(sql + " ORDER BY r.ts, r.prompt_id, r.engine")]
    if week:
        rows = [r for r in rows if _week_of(r["ts"]) == week]
    return rows[:limit] if limit else rows


def _week_of(ts):
    """把時間戳換成 '2026-W47' 這種字串。

    用 ISO 週，不是 strftime('%W') —— 那個的週界跟 ISO 差一天，
    跨年的時候會把同一批資料拆到兩週去。
    """
    import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            y, w = iso_year_week(datetime.datetime.strptime((ts or "")[:19], fmt))
            return "%d-W%02d" % (y, w)
        except ValueError:
            continue
    return None


def clear_scope(conn, runs):
    ids = [r["id"] for r in runs]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    conn.execute("DELETE FROM mentions WHERE run_id IN (%s)" % marks, ids)
    conn.execute("DELETE FROM citations WHERE run_id IN (%s)" % marks, ids)
    # domains 刻意不刪 —— 它是跨週累積的網域字典，而且人可能改過分類。


def parse_runs(db_path, rerun_all, week, limit, model, dry_run, max_cost):
    conn = connect(db_path)
    runs = pick_runs(conn, rerun_all, week, limit)

    if not runs:
        print("沒有需要判讀的回答。")
        print()
        print("可能是：")
        print("  1. 全部都判讀過了 —— 要重跑的話加 --rerun-all")
        print("  2. runs 表裡沒有保留原文的紀錄（raw_path 是空的）")
        print("     人工統計匯入的那種只有數字、沒有原文，判讀不了。")
        return

    pin, pout = PRICING.get(model, (1.00, 5.00))
    est = len(runs) * (EST_INPUT_TOKENS / 1e6 * pin + EST_OUTPUT_TOKENS / 1e6 * pout)
    print("要判讀 %d 則　模型 %s　預估花費 約 US$%.2f" % (len(runs), model, est))
    if est > max_cost:
        print()
        print("⛔ 預估花費 US$%.2f 超過上限 US$%.2f。" % (est, max_cost))
        print("   確定要跑的話加 --max-cost，或用 --limit 先跑一小批。")
        return
    print()

    if dry_run:
        r = runs[0]
        payload = load_raw(r["raw_path"])
        print("=== --dry-run：第一則的提示詞（不呼叫 API）===")
        print(JUDGE_PROMPT.format(answer=payload["answer_text"])[:2000])
        print("...")
        print()
        print("（--dry-run：什麼都沒做）")
        return

    try:
        api_key = get_api_key("anthropic")
    except RuntimeError as e:
        print(e)
        return
    if rerun_all:
        clear_scope(conn, runs)
        print("--rerun-all：已清掉這批的舊 mentions／citations，準備重算")
        print()

    tot_m = tot_c = tot_nourl = 0
    spent = 0.0
    failed = []

    for i, r in enumerate(runs, 1):
        try:
            payload = load_raw(r["raw_path"])
        except Exception as e:
            failed.append((r["id"], "讀不到原始檔：%s" % e))
            continue

        try:
            result, ti, to = call_judge(payload["answer_text"], model, api_key)
        except Exception as e:
            failed.append((r["id"], "%s：%s" % (type(e).__name__, e)))
            continue

        spent += ti / 1e6 * pin + to / 1e6 * pout
        n_m, n_c, n_nourl = save_one(conn, r, result, structured_urls(payload), r["ts"])
        tot_m += n_m
        tot_c += n_c
        tot_nourl += n_nourl
        conn.commit()

        ours = "★" if any(is_our_brand(canonical_brand(b.get("name")))
                          for b in result.get("brands") or []) else " "
        print("  [%3d/%d] 題 %-3s %-11s %s 品牌 %d　來源 %d"
              % (i, len(runs), r["prompt_id"], r["engine"], ours, n_m, n_c))

        if spent > max_cost:
            print()
            print("⛔ 已經花了 US$%.2f，超過上限 US$%.2f，停在第 %d 則。"
                  % (spent, max_cost, i))
            break
        time.sleep(SLEEP_BETWEEN_CALLS)

    print()
    print("判讀完成：mentions %d 筆、citations %d 筆，實際花費 US$%.2f"
          % (tot_m, tot_c, spent))

    if tot_nourl:
        print()
        print("⚠️ 有 %d 筆引用只有來源名稱、沒有網址。" % tot_nourl)
        print("   原因是那則原始檔沒有結構化的引用資料（瀏覽器手動收的都是這樣），")
        print("   來源只寫「來源：某某」。這些算得進引用率，但 audit.py 沒有頁面可以檢查。")

    if failed:
        print()
        print("⚠️ %d 則判讀失敗：" % len(failed))
        for rid, msg in failed[:10]:
            print("   run %s　%s" % (rid, msg))
        if len(failed) > 10:
            print("   （還有 %d 則）" % (len(failed) - 10))

    print()
    print("下一步：python eval/check_judge.py")
    print("        拿這批結果跟 eval/labeled.csv 比對，一致率要 ≥ 90% 才算過關。")
    print("        沒過的話改 JUDGE_PROMPT，然後 --rerun-all 重算。")


def main():
    parser = argparse.ArgumentParser(
        description="逐則判讀原始回答，產出 mentions／citations／domains",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/parse.py --dry-run\n"
               "  python scripts/parse.py --week 2026-W47\n"
               "  python scripts/parse.py --rerun-all\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--rerun-all", action="store_true",
                        help="連判讀過的也重跑（改了 JUDGE_PROMPT 之後用這個）")
    parser.add_argument("--week", help="只判讀某一週，格式 2026-W47")
    parser.add_argument("--limit", type=int, help="最多判讀幾則（先跑一小批用）")
    parser.add_argument("--model", default=JUDGE_MODEL, help="判讀用的模型")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印出提示詞與花費預估，不呼叫 API、不寫資料庫")
    parser.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST,
                        help="花費上限（美金），預設 %.2f" % DEFAULT_MAX_COST)
    args = parser.parse_args()

    print("=== parse.py ===")
    print()
    parse_runs(args.db, args.rerun_all, args.week, args.limit,
               args.model, args.dry_run, args.max_cost)


if __name__ == "__main__":
    main()
