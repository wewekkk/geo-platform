# -*- coding: utf-8 -*-
"""
collect.py — 每週去問各家 AI，把原始回答存下來

吃什麼 → 做什麼 → 吐什麼
  吃：prompts 表裡 active=1 的題目
  做：每一題 × 每一個引擎，各問 N 次（同一題問多次是因為 AI 每次回答都不同）
  吐：① 原始回答壓成 gzip 存進 data/raw/{年}/W{週}/{引擎}/
      ② 每次呼叫的 metadata（時間、模型版本、花費、檔案路徑）寫進 runs 表

怎麼跑
  python scripts/collect.py                        # 全部 active 題目，全部引擎
  python scripts/collect.py --engine openai        # 只跑一家
  python scripts/collect.py --samples 10           # 每題跑幾次（預設 10）
  python scripts/collect.py --dry-run              # 只印出「會做什麼」，不真的呼叫 API

前置條件
  1. 已經跑過 init_db.py
  2. prompts 表裡有 active=1 的題目
  3. .env 裡有各家的 API 金鑰（階段 1 才需要）

⚠️ 兩件絕對不能違反的事
  1. 只用官方 API + SerpApi。絕對不爬 AI 產品的網頁介面（ToS 風險）。
  2. 呼叫 OpenAI / Anthropic / Gemini 時務必開啟各自的 web search 工具。
     不開的話，問到的是「裸模型的記憶」，不是會查資料的助理，數字沒有意義。

⚠️ 原始回答一旦寫入就永不修改、永不刪除。
  判讀邏輯之後改了，要能拿這些檔案整批重跑，把歷史數字全部重算。

目前狀態：階段 0 空殼。真正接 API 是階段 1 的工作。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH  # noqa: E402

# 支援的引擎。階段 1 先接前三家，Google AI Overviews（走 SerpApi）留到階段 3。
ENGINES = ["openai", "anthropic", "perplexity"]


def collect(db_path, engines, samples, locale, condition, dry_run):
    """主要流程（階段 1 再實作）"""
    print("TODO: 從 prompts 表讀出 active=1 的題目")
    print("TODO: 對每一題 × 每一個引擎 %s，各問 %d 次" % (engines, samples))
    print("TODO: 呼叫官方 API，並且務必開啟該家的 web search 工具")
    print("TODO: 回答原文壓成 gzip 存到 data/raw/{年}/W{週}/{引擎}/{prompt_id}_%s_{n}.json.gz" % locale)
    print("TODO: metadata 寫入 runs 表（engine, model_version, condition=%s, ts, raw_path, cost, error）" % condition)
    print("TODO: 單一題目失敗不要中斷整批，把人看得懂的原因寫進 runs.error 就好")
    if dry_run:
        print("（--dry-run：以上都不會真的執行）")


def main():
    parser = argparse.ArgumentParser(
        description="每週去問各家 AI，原文存 gzip、metadata 進 runs 表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/collect.py\n"
               "  python scripts/collect.py --engine openai --samples 10\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--engine", choices=ENGINES, action="append",
                        help="只跑指定引擎，可重複給。不給就是全部。")
    parser.add_argument("--samples", type=int, default=10,
                        help="每題每引擎要問幾次（預設 10。次數越多信賴區間越窄）")
    parser.add_argument("--locale", default="tw", help="語系／地區（預設 tw）")
    parser.add_argument("--condition", default="api",
                        help="量測條件標籤，會顯示在畫面頂端（預設 api）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印出打算做什麼，不呼叫任何 API、不寫任何資料")
    args = parser.parse_args()

    engines = args.engine or ENGINES
    print("=== collect.py（階段 0 空殼）===")
    collect(args.db, engines, args.samples, args.locale, args.condition, args.dry_run)


if __name__ == "__main__":
    main()
