# -*- coding: utf-8 -*-
"""
共用小工具（不是框架，只是把每支腳本都會重複寫的三五行集中起來）

吃什麼 → 做什麼 → 吐什麼
  吃：資料庫路徑（預設 data/geo.db）
  做：開啟 SQLite 連線、提供固定值清單、算原始檔案存放路徑
  吐：連線物件 / 字串 / 清單

刻意不做的事：
  - 不包 ORM，查詢一律直接寫 SQL（CLAUDE.md 的技術限制）
  - 不做連線池、不做快取，這是內部自用工具，簡單就好
"""

import os
import sqlite3
from datetime import datetime

# ---------------------------------------------------------------------------
# 路徑
# ---------------------------------------------------------------------------

# 專案根目錄 = 本檔案的上一層（scripts/ 的父目錄）
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 資料庫預設位置。data/ 已被 .gitignore 排除，不會進版控。
DEFAULT_DB_PATH = os.path.join(ROOT_DIR, "data", "geo.db")

# 原始回答存放的根目錄（永不修改、永不刪除）
RAW_DIR = os.path.join(ROOT_DIR, "data", "raw")


# ---------------------------------------------------------------------------
# 固定值（CLAUDE.md 規定的列舉值，寫死在這裡讓各腳本共用）
# ---------------------------------------------------------------------------

# 提問意圖
INTENTS = ["discovery", "comparison", "price", "trust", "urgent", "logistics"]

# 意圖權重：trust（信任查證）與 urgent（緊急需求）最高
# 理由 → 寵物托育是信任門檻極高的服務，這兩類問題直接影響成交
INTENT_WEIGHTS = {
    "trust": 1.5,
    "urgent": 1.4,
    "comparison": 1.2,
    "price": 1.1,
    "discovery": 1.0,
    "logistics": 0.9,
}

# 情緒標籤
SENTIMENT_LABELS = ["positive", "neutral", "negative"]

# 缺口狀態。holdout = 刻意不處理的對照組（正式狀態，不是備註）
GAP_STATUSES = ["open", "in_progress", "done", "holdout"]

# 行動渠道
ACTION_CHANNELS = ["owned", "social", "pr", "ugc"]

# 引用來源類型
SOURCE_TYPES = ["社群", "媒體", "品牌內容", "論壇"]

# 草稿狀態。永遠不自動發佈，產出後停在 draft 等人審。
DRAFT_STATUSES = ["draft", "reviewing", "approved", "published"]

# 我們自己（判讀時用來認出「有沒有提到我們」）
OUR_BRAND = "Fluv 毛小愛"
OUR_DOMAIN = "fluv.com"


# ---------------------------------------------------------------------------
# 資料庫
# ---------------------------------------------------------------------------

def connect(db_path=None):
    """開啟 SQLite 連線。

    回傳的連線設定成 row_factory=sqlite3.Row，
    這樣查詢結果可以用 row["欄位名"] 取值，比用數字索引好讀。
    """
    path = db_path or DEFAULT_DB_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            "找不到資料庫檔案：%s\n"
            "請先執行：python scripts/init_db.py" % path
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # 開啟外鍵約束（SQLite 預設是關的）
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def raw_path(year, week, engine, prompt_id, locale, sample_index):
    """算出一則原始回答該存到哪裡（相對於專案根目錄的路徑）。

    格式：data/raw/{年}/W{週}/{引擎}/{prompt_id}_{locale}_{sample_index}.json.gz
    按 年/週/引擎 分層，避免單一資料夾累積數十萬個檔案。
    """
    return os.path.join(
        "data", "raw", str(year), "W%02d" % int(week), engine,
        "%s_%s_%s.json.gz" % (prompt_id, locale, sample_index),
    )


def now_iso():
    """現在時間，格式 2026-08-24 03:12:00（整個專案統一用這個格式存時間）"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
