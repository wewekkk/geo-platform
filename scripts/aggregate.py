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
  python scripts/aggregate.py --week 2026-W34  # 只重算某一週
  python scripts/aggregate.py --explain 62 100 # 手算示範：62/100 的信賴區間怎麼來的

前置條件
  parse.py 已經跑過，mentions 表裡有資料

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

目前狀態：階段 0 空殼。彙總邏輯留到階段 3。
  但 wilson_interval() 是「已經實作好、可以用的真程式碼」，
  因為 seed_demo.py 的示範資料也用它來算，數字才前後一致。
"""

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH  # noqa: E402


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


def aggregate(db_path, week):
    """主要流程（階段 3 再實作）"""
    print("TODO: 從 runs join mentions join prompts 撈出原始判讀結果（week=%s）" % week)
    print("TODO: 按 週 × 引擎 × 主題 × 意圖 分組（每個維度都要另外算一組 'all' 合計）")
    print("TODO: visibility = 有提到我們的回答數 ÷ 該組總回答數")
    print("TODO: ci_low / ci_high = wilson_interval(有提到我們的回答數, 總回答數)  ← 已經寫好了")
    print("TODO: sov = 我們的提及數 ÷ 所有品牌的提及數")
    print("TODO: avg_position = 我們被提到時的平均排序（沒被提到的不計入）")
    print("TODO: sentiment_avg / pos_pct / neu_pct / neg_pct 從 mentions 的情緒欄位算")
    print("TODO: n_samples 一定要存，UI 要靠它說明「這個數字是幾則算出來的」")
    print("TODO: 寫入 metrics_weekly（同一格重算就覆蓋，用 INSERT OR REPLACE）")


def main():
    parser = argparse.ArgumentParser(
        description="彙總每週指標並計算 Wilson 信賴區間",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/aggregate.py\n"
               "  python scripts/aggregate.py --explain 62 100   # 看信賴區間怎麼算出來的\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--week", help="只重算某一週，格式 2026-W34")
    parser.add_argument("--explain", nargs=2, type=int, metavar=("成功次數", "總次數"),
                        help="不查資料庫，直接示範這組數字的信賴區間怎麼算（可人工驗算）")
    args = parser.parse_args()

    if args.explain:
        explain(args.explain[0], args.explain[1])
        return

    print("=== aggregate.py（階段 0 空殼，但 wilson_interval() 已可用）===")
    aggregate(args.db, args.week)


if __name__ == "__main__":
    main()
