# -*- coding: utf-8 -*-
"""
gaps.py — 找出內容缺口，算機會分數決定先補哪個

吃什麼 → 做什麼 → 吐什麼
  吃：metrics_weekly ＋ prompts ＋ mentions
  做：找出「我們持續缺席、但競品有出現」的題目群，聚成一個個缺口，
      再算機會分數決定排序
  吐：gaps 表（status=open，等人決定要不要做）

怎麼跑
  python scripts/gaps.py                 # 重新盤點缺口
  python scripts/gaps.py --min-score 2.0 # 只留機會分數夠高的
  python scripts/gaps.py --preview       # 只印出來看，不寫進資料庫

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

目前狀態：階段 0 空殼。真正的缺口盤點是階段 4 的工作。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH, INTENT_WEIGHTS  # noqa: E402


def opportunity_score(affected_prompts, intent_weight, absence_rate, difficulty):
    """機會分數 = (受影響題數 × 意圖權重 × 缺席程度) ÷ 補齊難度

    難度不可以是 0（會除以零），所以最小值當作 1。
    """
    difficulty = max(1.0, float(difficulty))
    return (affected_prompts * intent_weight * absence_rate) / difficulty


def find_gaps(db_path, min_score, preview):
    """主要流程（階段 4 再實作）"""
    print("TODO: 從 metrics_weekly 找出能見度持續偏低的 主題 × 意圖 組合")
    print("TODO: 對照 mentions，確認那些題目「競品有出現但我們沒有」（我們跟競品都沒出現的不算缺口）")
    print("TODO: 把同性質的題目聚成一個缺口，寫出人看得懂的 title 與 why")
    print("TODO: 用 opportunity_score() 算分數  ← 公式已經寫好了")
    print("TODO: 意圖權重用 common.INTENT_WEIGHTS，目前是 %s" % INTENT_WEIGHTS)
    print("TODO: 只留分數 >= %s 的" % min_score)
    print("TODO: 寫入 gaps（status='open'，affected_prompt_ids 存逗號分隔的 id）")
    print("TODO: 已經存在的缺口只更新分數，不要重複開一筆（避免人已經改成 in_progress 又被蓋掉）")
    if preview:
        print("（--preview：以上結果只會印出來，不會寫進資料庫）")


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
    args = parser.parse_args()

    print("=== gaps.py（階段 0 空殼，但機會分數公式已可用）===")
    find_gaps(args.db, args.min_score, args.preview)


if __name__ == "__main__":
    main()
