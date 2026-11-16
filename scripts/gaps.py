# -*- coding: utf-8 -*-
"""
gaps.py — 找出內容缺口，算機會分數決定先補哪個

吃什麼 → 做什麼 → 吐什麼
  吃：metrics_weekly ＋ prompts ＋ mentions
  做：找出「我們持續缺席、但競品有出現」的題目群，聚成一個個缺口，
      再算機會分數決定排序
  吐：gaps 表（status=open，等人決定要不要做）

怎麼跑
  python scripts/gaps.py --preview       # 先看會產出什麼，不寫進資料庫
  python scripts/gaps.py                 # 盤點並寫入 gaps 表
  python scripts/gaps.py --min-score 2.0 # 只留機會分數夠高的
  python scripts/gaps.py --week 2026-W38 # 指定用哪一週的指標（預設用樣本最多的那一週）
  python scripts/gaps.py --clear-demo    # 清掉 seed_demo.py 產生的假缺口

前置條件
  aggregate.py 已經跑過，metrics_weekly 有資料

═══════════════════════════════════════════════════════════════════════
機會分數公式
═══════════════════════════════════════════════════════════════════════

  opportunity_score = (受影響題數 × 意圖權重 × 缺席程度) ÷ 補齊難度

  受影響題數：這個缺口牽涉到幾個 prompt。牽涉越多題，補一次的效益越大。
  意圖權重  ：trust（信任查證）與 urgent（緊急需求）權重最高。
              寵物托育是信任門檻極高的服務，這兩類問題直接影響成交。
  缺席程度  ：1 - 能見度。完全沒被提到 = 1.0，已經常常被提到 = 接近 0。
  補齊難度  ：估計要花多少力氣才補得起來（1=寫篇文章就好，5=要做整套內容）。

這個分數**不是評分，是排序**。3.2 分不代表「品質 3.2」，
只代表它應該排在 2.8 分的那個缺口前面。不要拿它跨專案比較。

⚠️ 關於 holdout
  gaps.status 有一個 holdout（刻意不處理的對照組）。
  這支腳本產出的缺口一律是 open，holdout 只能由人在畫面上手動改。
  用途：日後比較「有行動」與「刻意不行動」兩組的變化幅度，
  藉此區分數字變好是我們的功勞、還是模型自己在漂移。

⚠️ 一個缺口要成立，必須「這題會點名品牌，只是沒點到我們」
  如果某一題 AI 根本不提任何品牌（整段只在講服務類型），那不是內容缺口 ——
  寫再多文章也不會被提到，因為 AI 回答那題不需要點名平台。
  所以這支腳本會去對照 mentions：**競品有出現、我們沒出現**才算缺口；
  我們跟競品都沒出現的，標記成「無法驗證」並在說明裡講清楚。

  目前資料的限制：九月那批（manual-tally）沒有記錄競品，
  所以大部分缺口會是「無法驗證」。那不是程式的問題，是資料缺那一欄。

目前狀態：階段 4 完成。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH, INTENT_WEIGHTS, connect, now_iso  # noqa: E402

# 意圖的中文說法，寫進缺口標題給人看
INTENT_LABEL = {
    "discovery": "探索尋找", "comparison": "比較選擇", "price": "價格",
    "trust": "信任查證", "urgent": "緊急需求", "logistics": "操作流程",
}

# 補齊難度（1=寫一篇文章就好，5=要做整套內容與外部曝光）
#
# 為什麼 discovery 最難：那類題目（「台北地區寵物旅館推薦」）AI 是去抓
# 第三方的推薦名單，要被列進去得靠 PR 與媒體合作，不是靠自有內容。
# 為什麼 logistics 最容易：那類題目（「到府餵貓要準備什麼」）就是一篇清單文，
# 純自有內容寫得出來，而且事實密度高、天生好引用。
DIFFICULTY_BY_INTENT = {
    "logistics": 1.0,
    "trust": 2.0,
    "comparison": 2.0,
    "price": 2.0,
    "urgent": 2.0,
    "discovery": 4.0,
}


def opportunity_score(affected_prompts, intent_weight, absence_rate, difficulty):
    """機會分數 = (受影響題數 × 意圖權重 × 缺席程度) ÷ 補齊難度

    難度不可以是 0（會除以零），所以最小值當作 1。
    """
    difficulty = max(1.0, float(difficulty))
    return (affected_prompts * intent_weight * absence_rate) / difficulty


def pick_week(conn, week=None):
    """要用哪一週的指標來盤點缺口。

    預設挑「樣本最多的那一週」而不是最新的一週 ——
    最新那一週可能只是臨時抽查幾題，拿它盤缺口會盤出一堆假缺口。
    """
    if week:
        return week
    r = conn.execute(
        "SELECT week, n_samples FROM metrics_weekly WHERE engine='all' "
        "AND topic='all' AND intent='all' ORDER BY n_samples DESC, week DESC "
        "LIMIT 1").fetchone()
    return r["week"] if r else None


def competitor_evidence(conn, prompt_ids, week):
    """這些題目裡，競品有沒有出現過？

    回傳 (競品出現的回答數, 有競品資料的回答數)。
    第二個數字是 0 的話，代表這批資料根本沒記錄競品 —— 無法驗證。

    為什麼這件事重要：如果 AI 回那題時連競品都不提，那題就不會點名任何平台，
    不是內容缺口。寫文章也不會被提到。
    """
    if not prompt_ids:
        return 0, 0
    marks = ",".join("?" * len(prompt_ids))
    # manual-tally 那批沒有記錄競品，要排除在「有資料」的分母外，
    # 不然會把「沒記錄」誤當成「競品沒出現」。
    rows = conn.execute(
        "SELECT r.id, "
        " (SELECT COUNT(*) FROM mentions m WHERE m.run_id=r.id AND m.is_us=0) AS comp "
        "FROM runs r WHERE r.error IS NULL AND r.condition NOT IN ('demo','manual-tally') "
        "AND r.prompt_id IN (%s)" % marks, list(prompt_ids)).fetchall()
    return sum(1 for r in rows if r["comp"]), len(rows)


def build_gap(conn, cell, week):
    """把一個 主題×意圖 的格子，變成一筆人看得懂的缺口。"""
    topic, intent = cell["topic"], cell["intent"]
    vis = cell["visibility"] or 0.0
    absence = 1.0 - vis
    weight = INTENT_WEIGHTS.get(intent, 1.0)
    difficulty = DIFFICULTY_BY_INTENT.get(intent, 2.0)

    prompts = conn.execute(
        "SELECT id, text FROM prompts WHERE active=1 AND topic=? AND intent=? ORDER BY id",
        (topic, intent)).fetchall()
    pids = [p["id"] for p in prompts]

    comp_hits, comp_total = competitor_evidence(conn, pids, week)
    if comp_total == 0:
        verdict = "unverified"
    elif comp_hits > 0:
        verdict = "confirmed"
    else:
        verdict = "no-brands"

    score = opportunity_score(len(pids) or 1, weight, absence, difficulty)
    label = INTENT_LABEL.get(intent, intent)
    sev = "完全缺席" if vis == 0 else "能見度偏低"
    title = "「%s」的%s類題目%s" % (topic, label, sev)

    lines = [
        "【現況】%s 的這一組題目，能見度 %.0f%%（95%% 區間 %.0f%% ~ %.0f%%，%d 則樣本）。"
        % (week, vis * 100, (cell["ci_low"] or 0) * 100,
           (cell["ci_high"] or 0) * 100, cell["n_samples"] or 0),
        "",
        "【受影響的題目】共 %d 題：" % len(pids),
    ]
    for p in prompts:
        lines.append("  · #%s %s" % (p["id"], p["text"]))
    lines += [
        "",
        "【機會分數怎麼算的】%.2f = (%d 題 × 意圖權重 %.1f × 缺席程度 %.2f) ÷ 難度 %.0f"
        % (score, len(pids) or 1, weight, absence, difficulty),
        "  意圖權重：%s 類的權重是 %.1f（trust 與 urgent 最高，直接影響成交）"
        % (intent, weight),
        "  補齊難度：%.0f —— %s" % (difficulty, _difficulty_reason(intent)),
        "",
    ]
    if verdict == "confirmed":
        lines.append("【這是真的缺口】這些題目裡，競品有出現但我們沒有（%d / %d 則有競品）。"
                     % (comp_hits, comp_total))
        lines.append("代表 AI 回這類題目時會點名平台，只是沒點到我們 —— 內容補得起來。")
    elif verdict == "no-brands":
        lines.append("【⚠️ 這可能不是內容缺口】有競品資料的 %d 則回答裡，"
                     "連競品都沒出現。" % comp_total)
        lines.append("那代表 AI 回這類題目時根本不點名任何平台（整段只講服務類型），"
                     "寫文章也不會被提到。先確認這件事再投入資源。")
    else:
        lines.append("【⚠️ 無法驗證】這批資料沒有記錄「AI 提到哪些競品」，"
                     "所以分不出是哪一種情況：")
        lines.append("  (a) AI 會點名平台，只是沒點到我們 → 內容補得起來")
        lines.append("  (b) AI 回這題根本不提任何品牌 → 寫文章也沒用")
        lines.append("第一步應該是把其中一題的原文調出來讀，確認是哪一種。"
                     "等 collect.py 透過 API 跑起來，這一欄就會自動有資料。")

    return {"title": title, "topic": topic, "intent": intent, "score": score,
            "pids": pids, "why": "\n".join(lines), "verdict": verdict,
            "visibility": vis, "n_samples": cell["n_samples"] or 0}


def _difficulty_reason(intent):
    return {
        "logistics": "一篇清單／教學文就能補，純自有內容，事實密度高天生好引用",
        "discovery": "AI 抓的是第三方推薦名單，要被列進去得靠 PR 與媒體，不是自有內容",
        "trust": "要寫出有具體數字的權威頁面（保障條款、驗證機制）",
        "comparison": "要寫一篇紮實的比較指南，帶具體數字與適用情境",
        "price": "要寫出具體的價格區間與計算方式，不能只寫「價格依服務而定」",
        "urgent": "要寫情境式內容（臨時出差、住院、颱風天該怎麼辦）",
    }.get(intent, "一般內容補齊")


def clear_demo_gaps(conn):
    """清掉 seed_demo.py 產生的假缺口（連帶它的假行動與假草稿）。

    判斷依據：缺口指向的題目全部是示範題（prompts.source='demo'）。
    """
    victims = []
    for g in conn.execute("SELECT id, title, affected_prompt_ids FROM gaps"):
        ids = [x.strip() for x in (g["affected_prompt_ids"] or "").split(",") if x.strip()]
        if not ids:
            continue
        marks = ",".join("?" * len(ids))
        n_demo = conn.execute(
            "SELECT COUNT(*) FROM prompts WHERE source='demo' AND id IN (%s)" % marks,
            ids).fetchone()[0]
        if n_demo == len(ids):
            victims.append((g["id"], g["title"]))
    if not victims:
        print("沒有找到示範缺口。")
        return
    print("要刪掉 %d 筆示範缺口（連帶它們的假行動與假草稿）：" % len(victims))
    for gid, title in victims:
        print("  #%s %s" % (gid, title))
    ids = [v[0] for v in victims]
    marks = ",".join("?" * len(ids))
    conn.execute("DELETE FROM drafts WHERE gap_id IN (%s)" % marks, ids)
    conn.execute("DELETE FROM actions WHERE gap_id IN (%s)" % marks, ids)
    conn.execute("DELETE FROM gaps WHERE id IN (%s)" % marks, ids)
    conn.commit()
    print("✅ 已刪除")


def find_gaps(db_path, min_score, preview, week=None, max_visibility=0.5,
              min_samples=4, clear_demo=False, detail=False):
    """主要流程。"""
    conn = connect(db_path)

    if clear_demo:
        clear_demo_gaps(conn)
        print()

    week = pick_week(conn, week)
    if not week:
        print("metrics_weekly 是空的。請先跑：python scripts/aggregate.py")
        return

    cells = list(conn.execute(
        "SELECT topic, intent, visibility, ci_low, ci_high, n_samples "
        "FROM metrics_weekly WHERE week=? AND engine='all' "
        "AND topic<>'all' AND intent<>'all' ORDER BY visibility", (week,)))

    print("用 %s 的指標盤點（那一週樣本最多）" % week)
    print("篩選條件：能見度 < %.0f%%、樣本 >= %d 則、機會分數 >= %.1f"
          % (max_visibility * 100, min_samples, min_score))
    print()

    gaps, skipped = [], []
    for cell in cells:
        vis = cell["visibility"] or 0.0
        if vis >= max_visibility:
            continue
        if (cell["n_samples"] or 0) < min_samples:
            skipped.append((cell, "樣本只有 %d 則" % (cell["n_samples"] or 0)))
            continue
        g = build_gap(conn, cell, week)
        if g["score"] < min_score:
            skipped.append((cell, "機會分數 %.2f 低於門檻" % g["score"]))
            continue
        gaps.append(g)

    gaps.sort(key=lambda g: -g["score"])

    if not gaps:
        print("沒有符合條件的缺口。")
        if skipped:
            print()
            print("被篩掉的：")
            for cell, why in skipped:
                print("  %s × %s —— %s" % (cell["topic"], cell["intent"], why))
        return

    print("找到 %d 個缺口，依機會分數排序：" % len(gaps))
    print()
    VERDICT = {"confirmed": "✅ 已確認", "no-brands": "⚠️ 可能不是缺口",
               "unverified": "❓ 無法驗證"}
    print("  %5s  %-32s %7s %5s %s" % ("分數", "缺口", "能見度", "題數", "競品對照"))
    print("  " + "-" * 76)
    for g in gaps:
        print("  %5.2f  %-32s %6.0f%% %4d 題 %s"
              % (g["score"], g["title"], g["visibility"] * 100, len(g["pids"]),
                 VERDICT[g["verdict"]]))

    if skipped:
        print()
        print("被篩掉的 %d 組：" % len(skipped))
        for cell, why in skipped:
            print("  %s × %s —— %s" % (cell["topic"], cell["intent"], why))

    unver = sum(1 for g in gaps if g["verdict"] == "unverified")
    if unver:
        print()
        print("⚠️ 有 %d 個缺口的「競品對照」無法驗證，因為九月那批資料沒有記錄競品。" % unver)
        print("   分不出是「AI 會點名平台但沒點到我們」還是「AI 回這題根本不提品牌」——")
        print("   前者補內容有用，後者沒用。投入資源前先讀一題原文確認。")

    if detail:
        for g in gaps:
            print()
            print("=" * 76)
            print("%.2f　%s" % (g["score"], g["title"]))
            print("=" * 76)
            print(g["why"])

    if preview:
        print()
        print("（--preview：什麼都沒寫進資料庫%s）"
              % ("" if detail else "。想看完整說明加 --detail"))
        return

    # ---- 寫入 ----
    # 已經存在的缺口只更新分數與說明，不動 status ——
    # 人可能已經把它改成 in_progress 或 holdout，那是人的決定，程式不該蓋掉。
    added = updated = 0
    for g in gaps:
        row = conn.execute("SELECT id, status FROM gaps WHERE title=?",
                           (g["title"],)).fetchone()
        pids = ",".join(str(i) for i in g["pids"])
        if row:
            conn.execute("UPDATE gaps SET opportunity_score=?, topic=?, "
                         "affected_prompt_ids=?, why=? WHERE id=?",
                         (g["score"], g["topic"], pids, g["why"], row["id"]))
            updated += 1
        else:
            conn.execute("INSERT INTO gaps (title, topic, opportunity_score, status, "
                         "opened_at, affected_prompt_ids, why) VALUES (?,?,?,?,?,?,?)",
                         (g["title"], g["topic"], g["score"], "open",
                          now_iso(), pids, g["why"]))
            added += 1
    conn.commit()
    print()
    print("✅ 寫入 gaps：新增 %d 筆、更新 %d 筆（既有的 status 沒有被動過）"
          % (added, updated))
    print()
    print("下一步：到儀表板的「機會建議」頁看完整說明與受影響的題目。")


def main():
    parser = argparse.ArgumentParser(
        description="找出內容缺口並依機會分數排序",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/gaps.py\n"
               "  python scripts/gaps.py --preview\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--min-score", type=float, default=1.0,
                        help="機會分數低於這個值的缺口就不記錄（預設 1.0）")
    parser.add_argument("--preview", action="store_true",
                        help="只印出結果，不寫進資料庫")
    parser.add_argument("--week", help="用哪一週的指標（預設用樣本最多的那一週）")
    parser.add_argument("--max-visibility", type=float, default=0.5,
                        help="能見度高於這個值就不算缺口（預設 0.5）")
    parser.add_argument("--min-samples", type=int, default=4,
                        help="樣本數少於這個值就不判缺口（預設 4）")
    parser.add_argument("--clear-demo", action="store_true",
                        help="清掉 seed_demo.py 產生的假缺口（連帶假行動與假草稿）")
    parser.add_argument("--detail", action="store_true",
                        help="把每個缺口的完整說明印出來")
    args = parser.parse_args()

    print("=== gaps.py ===")
    print()
    find_gaps(args.db, args.min_score, args.preview, args.week,
              args.max_visibility, args.min_samples, args.clear_demo,
              detail=args.detail)


if __name__ == "__main__":
    main()
