# -*- coding: utf-8 -*-
"""
aggregate.py — 把判讀結果彙總成每週指標，並算出信賴區間

吃什麼 → 做什麼 → 吐什麼
  吃：runs ＋ mentions（＋ prompts 拿到 topic／intent）
  做：按「週 × 引擎 × 主題 × 意圖」分組，算能見度、SoV、平均排序、情緒分布，
      並且對能見度算 Wilson score interval（信賴區間）
  吐：metrics_weekly 表

怎麼跑
  python scripts/aggregate.py                  # 重算全部週次
  python scripts/aggregate.py --week 2026-W39  # 只重算某一週
  python scripts/aggregate.py --explain 62 100 # 手算示範：62/100 的信賴區間怎麼來的
  python scripts/aggregate.py --verify 2026-W39  # 把某一週的中間結果攤開，給人工驗算
  python scripts/aggregate.py --include-demo   # 連示範資料一起算（平常不要用）

前置條件
  mentions／citations 表裡有資料（階段 2 的 parse.py，或人工匯入的）

⚠️ 兩個排除條件，漏掉數字會安靜錯掉
  1. 排除 condition='demo' —— 那是 seed_demo.py 產生的亂數示範資料。
     混進去的話，趨勢圖會有一半是骰子擲出來的，而且不會報錯。
  2. 排除 error 有值的 run —— 呼叫失敗的不算進分母。
     失敗被當成「沒提到我們」的話，能見度會憑空變低。

⚠️ metrics_weekly 是純衍生資料
  它的每一個數字都能從 runs ＋ mentions ＋ citations 重算出來，
  所以這支腳本預設「整批重建」：先刪掉要重算的範圍，再重新寫入。
  判讀邏輯以後改了，直接重跑這支，歷史數字就全部跟著更新。

═══════════════════════════════════════════════════════════════════════
為什麼一定要有信賴區間（CLAUDE.md 不可違反的規則 1）
═══════════════════════════════════════════════════════════════════════

AI 每次回答都不一樣，所以「能見度 62%」不是一個精確的事實，
而是「我們問了 100 次，其中 62 次有提到我們」的抽樣結果。
再問 100 次，可能就變成 58% 或 66%，但品牌實際上什麼都沒變。

信賴區間就是在回答「這個數字上下多少範圍內都算沒變」。

  → 這一週 62%、上一週 55%，看起來漲了 7 個百分點。
  → 但如果兩週的信賴區間有重疊，那就是「持平」，不是「上升」。

**UI 上，當變化落在信賴區間內時，必須顯示為中性的「持平」，
  不可以顯示成紅色的「下降」或綠色的「上升」。**

理由：防止使用者對隨機波動做出反應，針對不存在的問題浪費資源。

用 Wilson score interval 而不是常見的常態近似（p ± 1.96·√(p(1-p)/n)），
因為樣本數小（n=10 這種）或比例接近 0%／100% 時，常態近似會算出
「-5% ~ 12%」這種不可能的區間，Wilson 不會。

目前狀態：階段 3 完成。彙總邏輯已實作。
"""

import argparse
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH, connect  # noqa: E402

# 不算進指標的量測條件。demo 是 seed_demo.py 產生的亂數示範資料。
EXCLUDED_CONDITIONS = ("demo",)

# 「只有人工統計、沒有保留原文」的量測條件。
#
# 這種資料只記了「有沒有提到我們」，所以：
#   能見度   ✅ 算得出來
#   引用率   只有當時有記錄引用的那一家算得出來（九月那批只有 Claude 有記）
#   SoV     ❌ 競品沒記錄，算不出來
#
# 為什麼要特別處理：如果照常算，SoV 會變成「我們的提及 ÷ 我們的提及」= 100%，
# 引用率會因為少記了三家而被低估。兩個都是會安靜錯掉的數字 ——
# 看起來完全正常，但意義是錯的。所以寧可留空，也不要給一個錯的。
TALLY_ONLY_CONDITIONS = ("manual-tally",)

# 上面那批裡，例外有記錄引用來源的引擎
TALLY_CITATION_ENGINES = ("anthropic",)


def wilson_interval(successes, n, z=1.96):
    """Wilson score interval —— 算比例的信賴區間。

    參數
      successes：成功次數（例如：有提到我們的回答數）
      n        ：總次數（例如：總共問了幾次）
      z        ：1.96 代表 95% 信心水準（想要 90% 就給 1.645）

    回傳
      (下界, 上界)，都是 0~1 之間的小數

    公式（可以拿計算機自己驗算）
      p     = successes / n
      denom = 1 + z²/n
      中心點 = (p + z²/(2n)) / denom
      半寬   = z·√( p(1-p)/n + z²/(4n²) ) / denom
      區間   = 中心點 ± 半寬

    手算範例：62 次提及 / 100 次提問，z=1.96
      p      = 0.62
      z²/n   = 3.8416/100 = 0.038416
      denom  = 1.038416
      中心點 = (0.62 + 0.019208) / 1.038416 = 0.61556
      根號內 = 0.62×0.38/100 + 3.8416/40000 = 0.002356 + 0.000096 = 0.002452
      √      = 0.049518
      半寬   = 1.96 × 0.049518 / 1.038416 = 0.09346
      區間   = 0.5221 ~ 0.7090   →  52.2% ~ 70.9%

    注意這個區間有多寬：問了 100 次，能見度 62%，
    真實值仍然可能落在 52% 到 71% 之間。所以「這週 62%、上週 58%」
    根本不是變化 —— 兩個區間重疊得很嚴重。
    樣本數是唯一能讓區間變窄的辦法（n 變 4 倍，區間寬度大約減半）。
    """
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def is_significant_change(low_a, high_a, low_b, high_b):
    """兩個比例的信賴區間有沒有重疊？

    沒有重疊 → 可以說「真的變了」
    有重疊   → UI 必須顯示「持平」，不可以顯示上升或下降

    這是一個保守的判斷法（比正式的雙樣本檢定更容易說「沒變」），
    對這個用途來說是刻意的：寧可漏報變化，也不要讓人對雜訊做反應。
    """
    return high_a < low_b or high_b < low_a


def explain(successes, n):
    """把一組數字的信賴區間算給人看，中間結果全部印出來，方便人工驗算"""
    p = successes / n if n else 0
    low, high = wilson_interval(successes, n)
    print("手算示範：%d 次提及 / %d 次提問" % (successes, n))
    print("  觀察到的比例 p = %d / %d = %.4f  (%.1f%%)" % (successes, n, p, p * 100))
    print("  z = 1.96（95% 信心水準）")
    print("  Wilson 區間 = %.4f ~ %.4f  (%.1f%% ~ %.1f%%)" % (low, high, low * 100, high * 100))
    print("  區間寬度 = %.1f 個百分點" % ((high - low) * 100))
    print()
    print("  怎麼讀：真實的能見度有 95%% 的把握落在 %.1f%% ~ %.1f%% 之間。" % (low * 100, high * 100))
    print("  下一週的數字只要還在這個範圍內，就要當作「持平」，不是變化。")


def week_of(ts):
    """把時間字串轉成 ISO 週標籤，例如 "2026-09-21 12:32:32" → "2026-W39"。

    用 ISO 週而不是「當月第幾週」，因為 ISO 週全世界定義一致，
    跨年的時候不會出現 W53 跟 W01 打架。
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(ts[:19] if len(ts) >= 19 else ts, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    year, wk, _ = d.isocalendar()
    return "%d-W%02d" % (year, wk)


def load_runs(conn, include_demo=False, only_week=None):
    """撈出每一則回答的判讀結果，一列一則 run。

    這是整支腳本唯一碰資料庫的地方。刻意只跑一次查詢、
    後面全部在 Python 裡算 —— 這樣每個數字怎麼來的，看程式就看得懂，
    不用去猜 SQL 的 GROUP BY 做了什麼。
    """
    excluded = () if include_demo else EXCLUDED_CONDITIONS
    sql = """
        SELECT r.id, r.engine, r.ts, r.condition, r.prompt_id,
               p.topic, p.intent,
               -- 這則回答有沒有提到我們
               (SELECT COUNT(*) FROM mentions m
                 WHERE m.run_id = r.id AND m.is_us = 1)            AS us_mentions,
               -- 這則回答總共提到幾個品牌（含我們）—— 算 SoV 的分母
               (SELECT COUNT(*) FROM mentions m
                 WHERE m.run_id = r.id)                            AS all_mentions,
               -- 我們被提到時排第幾（沒提到就是 NULL）
               (SELECT MIN(m.position) FROM mentions m
                 WHERE m.run_id = r.id AND m.is_us = 1)            AS us_position,
               -- 這則回答有沒有引用我們的網域
               (SELECT COUNT(*) FROM citations c JOIN domains d ON d.domain = c.domain
                 WHERE c.run_id = r.id AND d.is_ours = 1)          AS us_citations,
               -- 我們這則提及的情緒（人工匯入的資料刻意留空，所以可能是 NULL）
               (SELECT AVG(m.sentiment_score) FROM mentions m
                 WHERE m.run_id = r.id AND m.is_us = 1)            AS us_sentiment,
               (SELECT COUNT(*) FROM mentions m
                 WHERE m.run_id = r.id AND m.is_us = 1
                   AND m.sentiment_label = 'positive')             AS pos_n,
               (SELECT COUNT(*) FROM mentions m
                 WHERE m.run_id = r.id AND m.is_us = 1
                   AND m.sentiment_label = 'neutral')              AS neu_n,
               (SELECT COUNT(*) FROM mentions m
                 WHERE m.run_id = r.id AND m.is_us = 1
                   AND m.sentiment_label = 'negative')             AS neg_n
        FROM runs r
        JOIN prompts p ON p.id = r.prompt_id
        WHERE r.error IS NULL
    """
    params = []
    if excluded:
        sql += " AND r.condition NOT IN (%s)" % ",".join("?" * len(excluded))
        params += list(excluded)

    rows = []
    for r in conn.execute(sql, params):
        wk = week_of(r["ts"])
        if wk is None:
            continue
        if only_week and wk != only_week:
            continue
        tally_only = r["condition"] in TALLY_ONLY_CONDITIONS
        rows.append({
            # 這一筆的引用資料可不可信：不是人工統計的都可信；
            # 人工統計的只有當時真的有記引用的那幾家可信。
            "cite_ok": (not tally_only) or r["engine"] in TALLY_CITATION_ENGINES,
            # 競品資料可不可信：人工統計那批沒記競品，所以不可信
            "sov_ok": not tally_only,
            "week": wk, "engine": r["engine"], "prompt_id": r["prompt_id"],
            "topic": r["topic"] or "未分類", "intent": r["intent"] or "未分類",
            "us_mentioned": 1 if r["us_mentions"] else 0,
            "all_mentions": r["all_mentions"] or 0,
            "us_mentions": r["us_mentions"] or 0,
            "us_position": r["us_position"],
            "us_cited": 1 if r["us_citations"] else 0,
            "us_sentiment": r["us_sentiment"],
            "pos_n": r["pos_n"] or 0, "neu_n": r["neu_n"] or 0, "neg_n": r["neg_n"] or 0,
        })
    return rows


def summarise(group):
    """把一組 run 算成一列指標。

    每一個數字的算法都寫在這裡，而且都能人工驗算
    （用 --verify 可以把分子分母攤開來看）。
    """
    n = len(group)
    hits = sum(g["us_mentioned"] for g in group)          # 有提到我們的回答數
    cites = sum(g["us_cited"] for g in group)             # 引用我們網域的回答數

    visibility = hits / n if n else None
    ci_low, ci_high = wilson_interval(hits, n)

    # 引用率：只有在這一組「每一筆」的引用資料都可信時才算。
    # 只要混進一筆沒記引用的，分母就是錯的 —— 那寧可留空。
    if n and all(g["cite_ok"] for g in group):
        citation_rate = cites / n
        cite_low, cite_high = wilson_interval(cites, n)
    else:
        citation_rate = cite_low = cite_high = None

    # SoV：我們的提及數 ÷ 所有品牌的提及數（不是「回答數」，是「提及數」）
    # 同樣，混進沒記競品的資料就算不出來。
    if all(g["sov_ok"] for g in group):
        all_m = sum(g["all_mentions"] for g in group)
        our_m = sum(g["us_mentions"] for g in group)
        sov = our_m / all_m if all_m else None
    else:
        sov = None

    # 平均排序：只算「有提到我們」的那些。沒提到的不該拉低也不該拉高平均。
    positions = [g["us_position"] for g in group if g["us_position"] is not None]
    avg_position = sum(positions) / len(positions) if positions else None

    # 情緒：人工匯入的資料刻意沒判情緒，所以這裡會是 None。
    # ⚠️ 沒判讀就老實留 None，不可以填 0 —— 填 0 等於偷偷編了一個「情緒中性」出來。
    senti = [g["us_sentiment"] for g in group if g["us_sentiment"] is not None]
    sentiment_avg = sum(senti) / len(senti) if senti else None

    labelled = sum(g["pos_n"] + g["neu_n"] + g["neg_n"] for g in group)
    if labelled:
        pos_pct = sum(g["pos_n"] for g in group) / labelled
        neu_pct = sum(g["neu_n"] for g in group) / labelled
        neg_pct = sum(g["neg_n"] for g in group) / labelled
    else:
        pos_pct = neu_pct = neg_pct = None

    return (visibility, ci_low, ci_high,
            citation_rate, cite_low, cite_high,
            sov, avg_position, sentiment_avg, pos_pct, neu_pct, neg_pct, n)


def build_cells(rows):
    """把每一則 run 分配到它該進的所有格子。

    每一則會被算進好幾格：每個維度除了它自己的值，還要有一組 'all' 合計。
    例如一則 openai／到府照顧／trust 的回答，會進：
        (engine=openai, topic=到府照顧, intent=trust)
        (engine=openai, topic=到府照顧, intent=all)
        (engine=all,    topic=all,      intent=all)   ← 總覽頁看的就是這格
        …等八種組合
    這樣做的好處是：不管畫面要看哪個切面，都已經算好了，而且分母一定正確。
    """
    cells = defaultdict(list)
    for r in rows:
        for engine in (r["engine"], "all"):
            for topic in (r["topic"], "all"):
                for intent in (r["intent"], "all"):
                    cells[(r["week"], engine, topic, intent)].append(r)
    return cells


def aggregate(db_path, only_week=None, include_demo=False, quiet=False):
    """主要流程。"""
    conn = connect(db_path)
    rows = load_runs(conn, include_demo=include_demo, only_week=only_week)

    if not rows:
        print("沒有可以算的資料。")
        print()
        print("可能的原因：")
        print("  1. 還沒有成功的跑批紀錄 —— 先跑 python scripts/collect.py")
        print("  2. 全部都是示範資料（condition='demo'）—— 那些刻意被排除在指標外")
        print("  3. mentions 表是空的 —— 判讀還沒做（階段 2 的 parse.py）")
        if not include_demo:
            n_demo = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE condition='demo'").fetchone()[0]
            if n_demo:
                print()
                print("（資料庫裡有 %d 筆示範資料被排除了。真的要算進去的話加 "
                      "--include-demo，但那會讓趨勢圖有一半是亂數。）" % n_demo)
        return

    cells = build_cells(rows)
    weeks = sorted(set(r["week"] for r in rows))

    if not quiet:
        print("納入計算的回答：%d 則（已排除示範資料與呼叫失敗的）" % len(rows))
        print("涵蓋週次：%s" % "、".join(weeks))
        print("要寫入的格子數：%d（週 × 引擎 × 主題 × 意圖，每個維度都含 'all' 合計）"
              % len(cells))
        print()

    # 整批重建：先刪掉要重算的範圍。metrics_weekly 是純衍生資料，
    # 隨時都能從 runs 重算出來，所以刪掉重寫比逐格更新乾淨。
    if only_week:
        conn.execute("DELETE FROM metrics_weekly WHERE week = ?", (only_week,))
    else:
        conn.execute("DELETE FROM metrics_weekly")

    for (week, engine, topic, intent), group in sorted(cells.items()):
        conn.execute(
            "INSERT INTO metrics_weekly (week, engine, topic, intent, "
            "visibility, ci_low, ci_high, citation_rate, cite_ci_low, cite_ci_high, "
            "sov, avg_position, sentiment_avg, pos_pct, neu_pct, neg_pct, n_samples) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (week, engine, topic, intent) + summarise(group))
    conn.commit()

    if quiet:
        return

    print("寫入完成。各週的整體指標（engine=all · topic=all · intent=all）：")
    print()
    print("  %-10s %6s %22s %6s %22s" %
          ("週次", "能見度", "能見度 95% 區間", "引用率", "引用率 95% 區間"))
    print("  " + "-" * 72)
    for week in weeks:
        r = conn.execute(
            "SELECT * FROM metrics_weekly WHERE week=? AND engine='all' "
            "AND topic='all' AND intent='all'", (week,)).fetchone()
        if r["citation_rate"] is None:
            cite_txt, cite_ci = "   —  ", "（引用資料不完整，算不出來）"
        else:
            cite_txt = "%5.0f%%" % (r["citation_rate"] * 100)
            cite_ci = "%.0f%% ~ %.0f%%" % (r["cite_ci_low"] * 100, r["cite_ci_high"] * 100)
        print("  %-10s %5.0f%% %21s %6s %21s   (%d 則)"
              % (week, (r["visibility"] or 0) * 100,
                 "%.0f%% ~ %.0f%%" % (r["ci_low"] * 100, r["ci_high"] * 100),
                 cite_txt, cite_ci, r["n_samples"]))

    narrow = [w for w in weeks if _interval_width(conn, w) is not None
              and _interval_width(conn, w) < 0.05]
    print()
    if any(_sample_count(conn, w) < 30 for w in weeks):
        print("⚠️ 有週次的樣本數少於 30 則，所以信賴區間很寬。")
        print("   區間寬不是程式算錯，是資料真的還不夠 —— 這種寬度下，")
        print("   數字上下晃動十幾個百分點都算正常，不要拿來做決定。")
        print("   樣本數變 4 倍，區間寬度大約減半。")
    if narrow:
        print("⚠️ 這幾週的區間窄得不太合理：%s" % "、".join(narrow))
        print("   這通常是採樣壞了（快取沒關、同一題的回答都一樣），不是我們變穩定了。")

    warn_if_prompt_set_changed(rows, weeks)

    print()
    print("想人工驗算的話：python scripts/aggregate.py --verify %s" % weeks[-1])


def warn_if_prompt_set_changed(rows, weeks):
    """相鄰兩週的題目集合有沒有變？

    為什麼要檢查：跨週比較的前提是「問的是同一批題目」。
    題庫換了之後，趨勢圖上的轉折來自題目難易度不同，不是品牌表現變了 ——
    而且畫面上完全看不出來。這是 CLAUDE.md 列的
    「看到就要停下來」的訊號之一，所以自動檢查，不靠人記得。

    CLAUDE.md 的正式解法是「新題目標記起始週，趨勢只用共同題目計算」。
    這裡先做到「偵測並大聲講」，因為共同題目太少時連算都不該算。
    """
    by_week = {}
    for r in rows:
        by_week.setdefault(r["week"], set()).add(r["prompt_id"])

    problems = []
    for a, b in zip(weeks, weeks[1:]):
        sa, sb = by_week.get(a, set()), by_week.get(b, set())
        common = sa & sb
        if len(common) < min(len(sa), len(sb)) * 0.8:
            problems.append((a, b, len(sa), len(sb), len(common)))

    if not problems:
        return
    print()
    print("⛔ 題庫在週次之間換過，這兩週的數字不能直接比：")
    for a, b, na, nb, nc in problems:
        print("     %s（%d 題）→ %s（%d 題），共同題目只有 %d 題"
              % (a, na, b, nb, nc))
    print()
    print("   趨勢圖上的變化會來自「題目換了」，不是品牌表現變了 ——")
    print("   而且畫面上看不出來。要比較的話，兩週必須問同一批題目。")
    print("   （CLAUDE.md：題庫凍結；新題目標記起始週，趨勢只用共同題目計算）")


def _sample_count(conn, week):
    r = conn.execute("SELECT n_samples FROM metrics_weekly WHERE week=? AND engine='all' "
                     "AND topic='all' AND intent='all'", (week,)).fetchone()
    return r["n_samples"] if r else 0


def _interval_width(conn, week):
    r = conn.execute("SELECT ci_low, ci_high FROM metrics_weekly WHERE week=? "
                     "AND engine='all' AND topic='all' AND intent='all'", (week,)).fetchone()
    if not r or r["ci_low"] is None:
        return None
    return r["ci_high"] - r["ci_low"]


def verify(db_path, week, include_demo=False):
    """把某一週的中間結果攤開，讓人可以拿計算機自己驗算。

    這是階段 3 的驗收條件：「使用者能人工驗算至少一個指標，
    而且引用率必須是其中之一」—— 因為引用率是後面所有內容行動的成效依據，
    算錯的話整條歸因鏈都是假的。
    """
    conn = connect(db_path)
    rows = load_runs(conn, include_demo=include_demo, only_week=week)
    if not rows:
        print("%s 這一週沒有資料。" % week)
        return

    print("=" * 74)
    print("%s 的中間結果 —— 每一則回答一列，你可以自己數" % week)
    print("=" * 74)
    print()
    print("  %-11s %-10s %-11s %8s %8s %6s"
          % ("引擎", "主題", "意圖", "提到我們", "引用我們", "排序"))
    print("  " + "-" * 70)
    for r in sorted(rows, key=lambda x: (x["engine"], x["topic"])):
        print("  %-11s %-10s %-11s %8s %8s %6s"
              % (r["engine"], r["topic"][:10], r["intent"],
                 "✓" if r["us_mentioned"] else "—",
                 "✓" if r["us_cited"] else "—",
                 r["us_position"] if r["us_position"] else "—"))

    n = len(rows)
    hits = sum(r["us_mentioned"] for r in rows)
    cites = sum(r["us_cited"] for r in rows)

    print()
    print("—— 自己算一遍 ——")
    print()
    print("【能見度】有提到我們的回答數 ÷ 總回答數")
    print("    = %d ÷ %d = %.4f  (%.1f%%)" % (hits, n, hits / n, hits / n * 100))
    low, high = wilson_interval(hits, n)
    print("    Wilson 95%% 區間 = %.1f%% ~ %.1f%%" % (low * 100, high * 100))
    print()
    n_cite_ok = sum(1 for r in rows if r["cite_ok"])
    print("【引用率】引用了我們網域的回答數 ÷ 總回答數")
    if n_cite_ok < n:
        print("    ⚠️ 這一週有 %d / %d 筆沒有記錄引用來源，所以整體引用率算不出來。"
              % (n - n_cite_ok, n))
        print("       （算出來的話分母是錯的 —— 看起來正常但意義是錯的）")
        ok = [r for r in rows if r["cite_ok"]]
        if ok:
            c2 = sum(r["us_cited"] for r in ok)
            print("       有記錄的那 %d 筆：%d ÷ %d = %.1f%%"
                  % (len(ok), c2, len(ok), c2 / len(ok) * 100))
    else:
        print("    = %d ÷ %d = %.4f  (%.1f%%)" % (cites, n, cites / n, cites / n * 100))
        low, high = wilson_interval(cites, n)
        print("    Wilson 95%% 區間 = %.1f%% ~ %.1f%%" % (low * 100, high * 100))
    print()
    print("把上面兩個數字跟資料庫裡的對一下：")
    r = conn.execute("SELECT visibility, citation_rate, n_samples FROM metrics_weekly "
                     "WHERE week=? AND engine='all' AND topic='all' AND intent='all'",
                     (week,)).fetchone()
    if r:
        print("    資料庫裡存的：能見度 %.1f%%、引用率 %.1f%%、樣本 %d 則"
              % ((r["visibility"] or 0) * 100, (r["citation_rate"] or 0) * 100,
                 r["n_samples"]))
        ok = (abs((r["visibility"] or 0) - hits / n) < 1e-9
              and abs((r["citation_rate"] or 0) - cites / n) < 1e-9)
        print("    %s" % ("✓ 一致" if ok else "✗ 不一致 —— 請重跑 aggregate.py"))
    else:
        print("    資料庫裡還沒有這一週的紀錄，先跑一次 python scripts/aggregate.py")


def main():
    parser = argparse.ArgumentParser(
        description="彙總每週指標並計算 Wilson 信賴區間",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/aggregate.py                    # 重算全部\n"
               "  python scripts/aggregate.py --verify 2026-W39  # 攤開中間結果，人工驗算\n"
               "  python scripts/aggregate.py --explain 62 100   # 看信賴區間怎麼算出來的\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--week", help="只重算某一週，格式 2026-W39")
    parser.add_argument("--explain", nargs=2, type=int, metavar=("成功次數", "總次數"),
                        help="不查資料庫，直接示範這組數字的信賴區間怎麼算（可人工驗算）")
    parser.add_argument("--verify", metavar="週次",
                        help="把某一週的中間結果攤開，給人工驗算（不會寫入任何東西）")
    parser.add_argument("--include-demo", action="store_true",
                        help="連示範資料一起算。平常不要用 —— 那會讓趨勢圖有一半是亂數。")
    args = parser.parse_args()

    if args.explain:
        explain(args.explain[0], args.explain[1])
        return

    if args.verify:
        verify(args.db, args.verify, include_demo=args.include_demo)
        return

    print("=== aggregate.py ===")
    print()
    if args.include_demo:
        print("⚠️ --include-demo：示範資料也會被算進去，算出來的數字一半是亂數。")
        print()
    aggregate(args.db, args.week, include_demo=args.include_demo)


if __name__ == "__main__":
    main()
