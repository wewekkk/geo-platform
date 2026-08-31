# -*- coding: utf-8 -*-
"""
suggest.py — 自動提議新題目，讓題庫會自己長大

吃什麼 → 做什麼 → 吐什麼
  吃：三個來源
      ① Google Search Console 的查詢字（真的有人在搜的問題）
      ② 站內搜尋紀錄（真的有人在站上找的東西）
      ③ AI 回答裡出現、但我們題庫沒有的新概念
  做：整理成候選題目，估一個重要性分數，去掉跟現有題庫重複的
  吐：prompt_suggestions 表（status=pending，等人在設定頁決定加入或忽略）

怎麼跑
  python scripts/suggest.py              # 三個來源全跑
  python scripts/suggest.py --source gsc # 只跑一個來源
  python scripts/suggest.py --limit 20   # 最多提議幾則

前置條件
  ① 需要 .env 裡的 GSC 憑證
  ③ 需要 parse.py 跑過（要從判讀結果裡挖新概念）

⚠️ 這支永遠只「提議」，不會自己把題目加進 prompts 表。
  加不加由人在設定頁按下去。題庫是人維護的資產，不讓系統自己亂長。

目前狀態：階段 0 空殼。實作在階段 5。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH  # noqa: E402

SOURCES = ["gsc", "site_search", "answer_concept"]


def suggest(db_path, sources, limit):
    """主要流程（階段 5 再實作）"""
    if "gsc" in sources:
        print("TODO: 接 Google Search Console API，抓曝光多但點擊少的查詢字")
    if "site_search" in sources:
        print("TODO: 讀站內搜尋紀錄，找出被搜很多次但站上沒有對應內容的問題")
    if "answer_concept" in sources:
        print("TODO: 掃 data/raw/ 的回答，找出反覆出現、但題庫沒涵蓋的概念")
    print("TODO: 去重（跟現有 prompts 語意相近的就不提議了）")
    print("TODO: 估 est_importance，並在 evidence 欄寫清楚「為什麼建議這題」給人判斷")
    print("TODO: 最多寫入 %d 筆到 prompt_suggestions（status='pending'）" % limit)
    print("TODO: 絕對不要自己寫進 prompts 表 —— 加不加由人決定")


def main():
    parser = argparse.ArgumentParser(
        description="從 GSC／站內搜尋／回答內容提議新題目",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/suggest.py\n"
               "  python scripts/suggest.py --source gsc --limit 20\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--source", choices=SOURCES, action="append",
                        help="只跑指定來源，可重複給。不給就是全部。")
    parser.add_argument("--limit", type=int, default=20, help="最多提議幾則（預設 20）")
    args = parser.parse_args()

    sources = args.source or SOURCES
    print("=== suggest.py（階段 0 空殼）===")
    suggest(args.db, sources, args.limit)


if __name__ == "__main__":
    main()
