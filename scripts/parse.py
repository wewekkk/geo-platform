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
  python scripts/parse.py --rerun-all     # 全部重跑（改了判讀邏輯之後用這個）
  python scripts/parse.py --week 2026-W34 # 只跑某一週

前置條件
  1. collect.py 已經跑過，data/raw/ 底下有檔案
  2. .env 裡有判讀用模型的金鑰

⚠️ 這是整個系統唯一真正的品質關卡，順序不可以顛倒（CLAUDE.md 階段 2）
  1. 先由人手工標記 100 則 → eval/labeled.csv（這是標準答案）
  2. 才寫這支的判讀邏輯
  3. 用 eval/check_judge.py 拿判讀結果跟標準答案比對，一致率要 ≥ 90%
  理由：判讀器不會壞、不會報錯，就是默默判錯一成多，而沒有人會去讀那幾千則原文。

⚠️ 重跑是設計的一部分
  原始檔案永不修改，所以判讀邏輯改了就整批重算，歷史數字全部跟著更新。
  --rerun-all 會先刪掉舊的 mentions／citations 再重建，不是疊加。

目前狀態：階段 0 空殼。真正的判讀是階段 2 的工作，而且要先有標準答案。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH  # noqa: E402


def parse_runs(db_path, rerun_all, week, limit):
    """主要流程（階段 2 再實作，而且要先有 eval/labeled.csv）"""
    if rerun_all:
        print("TODO: --rerun-all 先清掉既有的 mentions／citations，準備整批重算")
    print("TODO: 從 runs 表選出要判讀的紀錄（week=%s, limit=%s）" % (week, limit))
    print("TODO: 照 raw_path 讀出 gzip 原始回答（唯讀，絕不修改原檔）")
    print("TODO: 丟給便宜模型判讀：提及哪些品牌、各排第幾、口氣如何、有沒有風險")
    print("TODO: 判讀結果寫入 mentions（entity, is_us, position, sentiment_*, risk_flag, snippet）")
    print("TODO: snippet 一定要存原文那一句，這是之後給人核對判定依據用的")
    print("TODO: 引用來源寫入 citations（url, domain, source_type, rank）")
    print("TODO: 新出現的網域補進 domains 表，並分類成 社群／媒體／品牌內容／論壇")


def main():
    parser = argparse.ArgumentParser(
        description="逐則判讀原始回答，產出 mentions／citations／domains",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/parse.py\n"
               "  python scripts/parse.py --rerun-all\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--rerun-all", action="store_true",
                        help="全部重跑。改過判讀邏輯之後用這個，歷史數字會一起重算。")
    parser.add_argument("--week", help="只判讀某一週，格式 2026-W34")
    parser.add_argument("--limit", type=int, help="最多判讀幾則（測試判讀 prompt 時很好用）")
    args = parser.parse_args()

    print("=== parse.py（階段 0 空殼）===")
    parse_runs(args.db, args.rerun_all, args.week, args.limit)


if __name__ == "__main__":
    main()
