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
# 可以用環境變數 GEO_DB 指定別的資料庫，方便並存多個版本：
#   GEO_DB=data/geo-empty.db streamlit run scripts/app.py
# 每支腳本的 --db 參數也還在，優先權比環境變數高。
DEFAULT_DB_PATH = os.environ.get("GEO_DB") or os.path.join(ROOT_DIR, "data", "geo.db")
if not os.path.isabs(DEFAULT_DB_PATH):
    DEFAULT_DB_PATH = os.path.join(ROOT_DIR, DEFAULT_DB_PATH)

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


# ---------------------------------------------------------------------------
# API 金鑰（階段 1 開始需要）
# ---------------------------------------------------------------------------

# .env 的位置。這個檔案已被 .gitignore 排除，不會進版控。
ENV_PATH = os.path.join(ROOT_DIR, ".env")

# 每個引擎需要哪一把金鑰
ENGINE_KEY_NAMES = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google_aio": "SERPAPI_KEY",
}


def load_env():
    """把 .env 讀進環境變數。

    刻意不用 python-dotenv —— 需要的只是「一行一個 KEY=VALUE」，
    自己寫十行就好，少一個套件少一件要維護的事。
    已經存在的環境變數優先，不會被 .env 蓋掉。
    """
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 跳過空行與註解
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # 已經在環境變數裡的優先（方便臨時覆寫）
            if key and key not in os.environ:
                os.environ[key] = value


def get_api_key(engine):
    """取出某個引擎的 API 金鑰。沒有的話丟出人看得懂的錯誤。"""
    load_env()
    name = ENGINE_KEY_NAMES.get(engine)
    if not name:
        raise ValueError("不認識的引擎：%s" % engine)
    key = os.environ.get(name, "").strip()
    if not key:
        raise RuntimeError(
            "找不到 %s 的 API 金鑰。\n"
            "請這樣做：\n"
            "  1. cp .env.example .env\n"
            "  2. 編輯 .env，把 %s= 後面填上金鑰\n"
            "（.env 不會進版控，金鑰不會外流）" % (engine, name)
        )
    return key


def iso_year_week(dt=None):
    """回傳 (ISO 年, ISO 週)。原始檔案按年／週分資料夾用的。

    用 ISO 週而不是「第幾個七天」，是因為 ISO 週全世界定義一致，
    跨年的時候不會出現 W53 跟 W01 打架的狀況。
    """
    d = dt or datetime.now()
    year, week, _ = d.isocalendar()
    return year, week
