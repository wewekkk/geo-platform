# -*- coding: utf-8 -*-
"""
init_db.py — 建立資料庫與十張表

吃什麼 → 做什麼 → 吐什麼
  吃：什麼都不用吃（頂多給一個 --db 指定資料庫要放哪）
  做：建立 data/geo.db，照 CLAUDE.md 的資料模型建好十張表與索引
  吐：一個空的 data/geo.db（沒有資料，只有結構）

怎麼跑
  python scripts/init_db.py            # 建立資料庫（已存在的表不會動）
  python scripts/init_db.py --force    # 全部砍掉重建（資料會不見！）
  python scripts/init_db.py --show     # 只列出目前有哪些表、各幾筆

前置條件
  無。這是整條流程的第一步。

安全性
  預設用 CREATE TABLE IF NOT EXISTS，重複執行不會弄壞既有資料。
  只有加上 --force 才會刪表，而且會先要你打字確認。
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH, RAW_DIR  # noqa: E402


# ---------------------------------------------------------------------------
# 十張表的定義
# 順序有意義：被別人參照的表要先建（例如 prompts 要在 runs 之前）
# ---------------------------------------------------------------------------

SCHEMA = [
    # 1. 題庫 —— 由人維護（設定頁可上傳 CSV，也可直接在畫面上編輯）
    ("prompts", """
    CREATE TABLE IF NOT EXISTS prompts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        text          TEXT    NOT NULL,                  -- 題目全文
        topic         TEXT    NOT NULL,                  -- 主題分類，例如「寵物住宿」
        intent        TEXT    NOT NULL,                  -- discovery|comparison|price|trust|urgent|logistics
        intent_weight REAL    NOT NULL DEFAULT 1.0,      -- 意圖權重，算機會分數時會乘上去
        locale        TEXT    NOT NULL DEFAULT 'tw',     -- 語系／地區
        active        INTEGER NOT NULL DEFAULT 1,        -- 1=納入每週跑批，0=停用
        source        TEXT,                              -- 這題哪來的：manual|csv|suggestion
        created_at    TEXT    NOT NULL
    )"""),

    # 2. 題目建議佇列 —— 系統提議，等人決定加入或忽略
    ("prompt_suggestions", """
    CREATE TABLE IF NOT EXISTS prompt_suggestions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        text           TEXT    NOT NULL,                 -- 建議的題目
        source         TEXT,                             -- 哪來的：gsc|site_search|answer_concept
        evidence       TEXT,                             -- 為什麼建議這題（給人看的證據）
        est_importance REAL,                             -- 預估重要性，用來排序
        status         TEXT    NOT NULL DEFAULT 'pending', -- pending|accepted|ignored
        created_at     TEXT    NOT NULL
    )"""),

    # 3. 每次提問的紀錄 —— 自動產生
    #    注意：這裡只存原始回答的「檔案路徑」，不存回答全文（CLAUDE.md 規則 2）
    ("runs", """
    CREATE TABLE IF NOT EXISTS runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        prompt_id     INTEGER NOT NULL REFERENCES prompts(id),
        engine        TEXT    NOT NULL,                  -- openai|anthropic|perplexity|gemini|google_aio
        model_version TEXT,                              -- 實際回答的模型版本，之後對照數字變化用
        locale        TEXT    NOT NULL DEFAULT 'tw',
        condition     TEXT    NOT NULL DEFAULT 'api',    -- 量測條件，會顯示在每頁頂端
        sample_index  INTEGER NOT NULL DEFAULT 0,        -- 同一題跑 N 次，這是第幾次（0 起算）
        ts            TEXT    NOT NULL,                  -- 提問時間
        raw_path      TEXT,                              -- 原始回答的 gzip 檔路徑（相對專案根目錄）
        cost          REAL    DEFAULT 0,                 -- 這一次呼叫花了多少錢（美金）
        error         TEXT                               -- 有值代表這次失敗了，內容是人看得懂的原因
    )"""),

    # 4. 判讀結果：提及、排名、情緒 —— 自動產生
    ("mentions", """
    CREATE TABLE IF NOT EXISTS mentions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          INTEGER NOT NULL REFERENCES runs(id),
        entity          TEXT    NOT NULL,                -- 被提到的品牌名稱
        is_us           INTEGER NOT NULL DEFAULT 0,      -- 1=這是我們，0=競品
        position        INTEGER,                         -- 在回答中第幾個被提到（1 起算），沒排序則 NULL
        sentiment_score REAL,                            -- -1.0 ~ 1.0
        sentiment_label TEXT,                            -- positive|neutral|negative
        risk_flag       INTEGER NOT NULL DEFAULT 0,      -- 1=風險提及（負面且會影響信任，要優先處理）
        snippet         TEXT                             -- 判定依據：原文中的那一句
    )"""),

    # 5. 引用來源 —— 自動產生
    ("citations", """
    CREATE TABLE IF NOT EXISTS citations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      INTEGER NOT NULL REFERENCES runs(id),
        url         TEXT    NOT NULL,
        domain      TEXT    NOT NULL,
        source_type TEXT,                                -- 社群|媒體|品牌內容|論壇
        rank        INTEGER                              -- 在該則回答的引用清單中排第幾
    )"""),

    # 6. 網域清單與分類 —— 自動產生（第一次看到就記一筆）
    ("domains", """
    CREATE TABLE IF NOT EXISTS domains (
        domain      TEXT PRIMARY KEY,
        source_type TEXT,                                -- 社群|媒體|品牌內容|論壇
        is_ours     INTEGER NOT NULL DEFAULT 0,          -- 1=我們自己的網域
        first_seen  TEXT,                                -- 第一次被引用的日期
        notes       TEXT
    )"""),

    # 7. 每週彙總指標＋信賴區間 —— 自動產生
    #    ci_low / ci_high 是 visibility 的 Wilson score interval 上下界。
    #    UI 上判斷「有沒有真的變化」一律看這兩個欄位，不看 visibility 本身的差值。
    ("metrics_weekly", """
    CREATE TABLE IF NOT EXISTS metrics_weekly (
        week          TEXT NOT NULL,                     -- ISO 週，格式 2026-W34
        engine        TEXT NOT NULL,                     -- 引擎名稱，或 'all' 代表全部合計
        topic         TEXT NOT NULL,                     -- 主題，或 'all'
        intent        TEXT NOT NULL,                     -- 意圖，或 'all'
        visibility    REAL,                              -- 能見度：有提到我們的回答佔比 0~1
        ci_low        REAL,                              -- 信賴區間下界（Wilson）
        ci_high       REAL,                              -- 信賴區間上界（Wilson）
        sov           REAL,                              -- 聲量佔比 Share of Voice 0~1
        avg_position  REAL,                              -- 平均排序（越小越前面）
        sentiment_avg REAL,                              -- 平均情緒分數 -1~1
        pos_pct       REAL,                              -- 正面比例 0~1
        neu_pct       REAL,                              -- 中性比例 0~1
        neg_pct       REAL,                              -- 負面比例 0~1
        n_samples     INTEGER,                           -- 這格是用幾則回答算出來的（決定區間寬窄）
        PRIMARY KEY (week, engine, topic, intent)
    )"""),

    # 8. 內容缺口＋機會分數 —— 系統提議，人決定
    ("gaps", """
    CREATE TABLE IF NOT EXISTS gaps (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        title               TEXT    NOT NULL,
        topic               TEXT,
        opportunity_score   REAL,                        -- (受影響題數 × 意圖權重 × 缺席程度) ÷ 補齊難度
        status              TEXT    NOT NULL DEFAULT 'open', -- open|in_progress|done|holdout
        opened_at           TEXT,
        closed_at           TEXT,
        affected_prompt_ids TEXT,                        -- 受影響的 prompt id，逗號分隔
        why                 TEXT                         -- 為什麼這是缺口（給人看的說明）
    )"""),

    # 9. 採取過的行動 —— 人登記
    #    published_at 會被畫成趨勢圖上的垂直虛線（行動標記）
    ("actions", """
    CREATE TABLE IF NOT EXISTS actions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        gap_id       INTEGER REFERENCES gaps(id),
        channel      TEXT NOT NULL,                      -- owned|social|pr|ugc
        title        TEXT NOT NULL,
        url          TEXT,
        published_at TEXT,                               -- 發佈日期，趨勢圖標記線用這欄
        notes        TEXT
    )"""),

    # 10. 文章草稿 —— 系統產生，人審。永遠不自動發佈。
    ("drafts", """
    CREATE TABLE IF NOT EXISTS drafts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        gap_id     INTEGER REFERENCES gaps(id),
        title      TEXT NOT NULL,
        outline    TEXT,                                 -- 大綱
        body       TEXT,                                 -- 內文
        status     TEXT NOT NULL DEFAULT 'draft',        -- draft|reviewing|approved|published
        action_id  INTEGER REFERENCES actions(id),       -- 這篇草稿最後變成哪一筆行動
        created_at TEXT NOT NULL
    )"""),
]

# 索引：只建查詢真的會用到的，不多建
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_runs_prompt   ON runs(prompt_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_ts       ON runs(ts)",
    "CREATE INDEX IF NOT EXISTS idx_runs_engine   ON runs(engine)",
    "CREATE INDEX IF NOT EXISTS idx_mentions_run  ON mentions(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_mentions_ent  ON mentions(entity)",
    "CREATE INDEX IF NOT EXISTS idx_citations_run ON citations(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_citations_dom ON citations(domain)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_week  ON metrics_weekly(week)",
    "CREATE INDEX IF NOT EXISTS idx_gaps_status   ON gaps(status)",
    "CREATE INDEX IF NOT EXISTS idx_actions_gap   ON actions(gap_id)",
]

TABLE_NAMES = [name for name, _ in SCHEMA]


def show_tables(db_path):
    """列出資料庫裡有哪些表、各有幾筆資料"""
    if not os.path.exists(db_path):
        print("資料庫還不存在：%s" % db_path)
        print("請先執行：python scripts/init_db.py")
        return
    conn = sqlite3.connect(db_path)
    print("資料庫：%s" % db_path)
    print("-" * 46)
    for name in TABLE_NAMES:
        try:
            n = conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
            print("  %-20s %8d 筆" % (name, n))
        except sqlite3.OperationalError:
            print("  %-20s %8s" % (name, "（表不存在）"))
    print("-" * 46)
    conn.close()


def init_db(db_path, force=False):
    """建立資料庫與十張表"""
    # data/ 與 data/raw/ 資料夾先確保存在
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    os.makedirs(RAW_DIR, exist_ok=True)

    conn = sqlite3.connect(db_path)

    if force:
        print("--force：正在刪除既有的表 ...")
        # 反著刪，先刪參照別人的表，避免外鍵擋住
        for name in reversed(TABLE_NAMES):
            conn.execute("DROP TABLE IF EXISTS %s" % name)
        conn.commit()

    for name, ddl in SCHEMA:
        conn.execute(ddl)
        print("  ✓ %s" % name)

    for idx in INDEXES:
        conn.execute(idx)

    conn.commit()
    conn.close()

    print()
    print("完成。十張表都建好了：%s" % db_path)
    print("下一步：python scripts/seed_demo.py   （塞入示範資料）")


def main():
    parser = argparse.ArgumentParser(
        description="建立 GEO 監測平台的資料庫與十張表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/init_db.py\n"
               "  python scripts/init_db.py --show\n"
               "  python scripts/init_db.py --force\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help="資料庫檔案路徑（預設 data/geo.db）")
    parser.add_argument("--force", action="store_true",
                        help="砍掉重建所有表。既有資料會全部消失，會先要求打字確認。")
    parser.add_argument("--show", action="store_true",
                        help="只顯示目前有哪些表、各幾筆，不做任何修改")
    args = parser.parse_args()

    if args.show:
        show_tables(args.db)
        return

    if args.force and os.path.exists(args.db):
        print("警告：--force 會刪掉 %s 裡面全部十張表的資料，且無法復原。" % args.db)
        answer = input("確定要繼續嗎？請輸入 yes 再按 Enter：").strip()
        if answer != "yes":
            print("已取消，沒有動任何資料。")
            return

    print("正在建立資料表 ...")
    init_db(args.db, force=args.force)


if __name__ == "__main__":
    main()
