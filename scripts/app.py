# -*- coding: utf-8 -*-
"""
app.py — Streamlit 七頁儀表板

吃什麼 → 做什麼 → 吐什麼
  吃：data/geo.db 裡的十張表（＋ data/raw/ 底下的原始回答檔）
  做：把資料整理成七頁畫面
  吐：瀏覽器上的儀表板（不寫任何指標資料，只有「設定」頁會寫題庫）

怎麼跑
  streamlit run scripts/app.py

前置條件
  1. python scripts/init_db.py
  2. python scripts/seed_demo.py   （階段 0 用示範資料）

═══════════════════════════════════════════════════════════════════════
兩條在這支程式裡一定要守住的規則
═══════════════════════════════════════════════════════════════════════

規則 A：量測條件必須顯示在畫面上
  每一頁頂端固定一條「N=10 · condition=api · locale=tw · 上次跑批 ...」。
  理由：任何人看到「能見度 43%」都會問「這數字怎麼來的」，有這行就不用每次解釋。

規則 B：變化落在信賴區間內時，必須顯示成中性的「持平」
  絕對不可以顯示成紅色的「下降」或綠色的「上升」。
  理由：AI 每次回答都不同，數字本身就有誤差。
        如果把隨機波動畫成紅色，使用者會針對不存在的問題浪費資源。
  實作：所有比例類的比較都走 compare_proportions()，它會先比信賴區間有沒有重疊。
"""

import gzip
import html
import io
import json
import os
import subprocess
import sys
from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (ACTION_CHANNELS, DEFAULT_DB_PATH, GAP_STATUSES, INTENTS,  # noqa: E402
                    INTENT_WEIGHTS, OUR_BRAND, ROOT_DIR, SOURCE_TYPES, connect,
                    now_iso)
from aggregate import week_of, wilson_interval  # noqa: E402

st.set_page_config(page_title="Fluv GEO 監測平台", page_icon="🐾", layout="wide")

# ═══ 配色 ═══════════════════════════════════════════════════════════════
# 對齊 Fluv 後台的設計語言（logo 薄荷藍 + 珊瑚粉），但色碼不是直接照搬 ——
# logo 原色 #80c0d0 彩度只有 0.069，畫在圖表上會被當成灰色，所以加深過。
#
# 這組全部跑過可計算的檢查（OKLab ΔE 色盲模擬、亮度帶、彩度下限、WCAG 對比）：
#   類別色 4 色  #0B8CA8 #C08416 #6A4FA3 #E8595E  色盲 ΔE 17.9（門檻 8）
#   發散色階     正 #3E7D1E ← 中性灰 #9AA5AD → 負 #DC5257  色盲 ΔE 12.1
#   單色階       #9fbcc6 → #037586  單一色相、相鄰 ΔL >= 0.06、最淺端 2.0:1
# 要改色請重跑驗證，不要憑眼睛判斷。

C_US = "#0B8CA8"                       # 我們（品牌薄荷藍加深版）對白底 3.94:1
C_OTHER = "#B9C3C9"                    # 競品／其他（中性灰）
C_BAND = "rgba(11, 140, 168, 0.15)"    # 信賴區間陰影
C_POS = "#3E7D1E"                      # 正面 5.05:1
C_NEU = "#9AA5AD"                      # 中性（發散色階的中點，本來就該是灰的）
C_NEG = "#DC5257"                      # 負面 3.90:1
C_ACTION = "#C08416"                   # 行動標記線 3.20:1
C_CITE = "#6A4FA3"                     # 引用率（和 C_US 並排時色盲 ΔE 10.9）
C_CITE_BAND = "rgba(106, 79, 163, 0.15)"
C_ALERT = "#E8595E"                    # 高機會來源／風險
C_INK = "#313131"                      # 主文字（同後台）
C_MUTED = "#898989"                    # 次要文字（同後台）

# 類別色：固定順序，不可循環使用。第 5 類以後請歸入「其他」，不要自動生新色。
C_CAT = ["#0B8CA8", "#C08416", "#6A4FA3", "#E8595E"]
# 單色階：熱區圖用（由淺到深，單一色相）
C_SCALE = ["#9fbcc6", "#74abc3", "#5498b6", "#3785a4", "#037586"]

st.markdown("""
<style>
  /* 右上角固定的「示範資料」標記，七頁都看得到 */
  .demo-badge {
      position: fixed; top: 3.2rem; right: 1.2rem; z-index: 1000;
      background: #fff6e5; color: #8a5d00; border: 1px solid #ffd88a;
      border-radius: 999px; padding: 2px 12px; font-size: 12px; font-weight: 600;
  }
  /* 量測條件列 */
  .cond-bar {
      background: #f8f8f9; border-left: 4px solid #0B8CA8; border-radius: 5px;
      padding: 8px 14px; margin-bottom: 14px; font-size: 13px;
      font-family: ui-monospace, "SF Mono", Menlo, monospace; color: #313131;
  }
  .card {
      border: 1px solid #ececee; border-radius: 5px; padding: 12px 14px;
      height: 100%; background: #ffffff;
      box-shadow: rgba(68, 102, 242, 0.05) 1px 5px 24px 0px;
  }
  .card h4 { margin: 0 0 6px 0; font-size: 14px; }
  .card p  { margin: 0; font-size: 13px; color: #898989; }
  mark.hit { background: #ffe9b0; padding: 1px 2px; border-radius: 2px; }
  .answer-box {
      background: #ffffff; border: 1px solid #ececee; border-radius: 5px;
      padding: 12px 14px; font-size: 14px; line-height: 1.9;
      max-height: 460px; overflow-y: auto; white-space: pre-wrap;
  }

  /* ───── 側邊欄導覽：做成後台那種「圖示＋文字」的清單 ─────
     Streamlit 的 st.radio 預設會畫圓圈點選鈕，後台沒有那個東西。
     把圓圈藏起來，選中的用珊瑚色文字表示（同後台 #ff8084）。 */
  section[data-testid="stSidebar"] div[role="radiogroup"] {
      gap: 0 !important;
  }
  section[data-testid="stSidebar"] div[role="radiogroup"] > label {
      padding: 11px 10px !important;
      margin: 0 !important;
      border-radius: 5px;
      cursor: pointer;
      transition: background .12s ease;
  }
  /* 圓圈本體 */
  section[data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-child {
      display: none !important;
  }
  section[data-testid="stSidebar"] div[role="radiogroup"] > label p {
      font-size: 14px !important;
      font-weight: 600 !important;
      color: #313131 !important;
      margin: 0 !important;
  }
  section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
      background: #f2f2f3;
  }
  /* 選中：珊瑚色文字 ＋ 左側色條（同後台） */
  section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) {
      background: #fff4f4;
      box-shadow: inset 3px 0 0 0 #ff8084;
  }
  section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) p {
      color: #ff8084 !important;
  }

  /* ───── 頁首：標題＋副標在左，麵包屑在右（同後台） ───── */
  .page-head {
      display: flex; align-items: baseline; justify-content: space-between;
      flex-wrap: wrap; gap: 8px; margin: 0 0 18px 0;
  }
  .page-head .ph-title { font-size: 26px; font-weight: 700; color: #313131; line-height: 1.3; }
  .page-head .ph-sub   { font-size: 13px; color: #898989; margin-top: 2px; }
  .page-head .ph-crumb { font-size: 13px; color: #898989; }
  .page-head .ph-crumb b { color: #313131; font-weight: 600; }

  /* ───── KPI 色塊：後台那種飽和色塊 ───── */
  .kpi {
      display: flex; align-items: center; gap: 12px;
      border-radius: 5px; padding: 18px 14px; min-height: 104px; height: 100%;
      box-shadow: rgba(68, 102, 242, 0.05) 1px 5px 24px 0px;
  }
  .kpi-ico {
      flex: 0 0 auto; width: 46px; height: 46px; border-radius: 50%;
      background: rgba(255,255,255,.28); display: flex;
      align-items: center; justify-content: center; font-size: 21px;
  }
  .kpi-label { font-size: 13px; color: rgba(255,255,255,.92); font-weight: 600; }
  /* nowrap 是必要的：五欄並排時「第 1.77 名」會折行，
     那一塊就比其他四塊高，整列對不齊。 */
  .kpi-value {
      font-size: 25px; color: #ffffff; font-weight: 700; line-height: 1.25;
      white-space: nowrap;
  }
  /* ⚠️ delta 一律用半透明白底，顏色不帶方向意義 ——
     方向由「持平／上升／下降」這幾個字承載，不是顏色。
     這樣才不會把隨機波動畫成紅色（見 CLAUDE.md 規則一）。 */
  .kpi-delta {
      display: inline-block; margin-top: 4px; padding: 1px 8px;
      background: rgba(255,255,255,.25); border-radius: 999px;
      font-size: 12px; color: #ffffff; font-weight: 600;
  }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════
# 讀資料（統一走這裡，方便加快取）
# ═══════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=30)
def q(sql, params=()):
    """跑一句 SELECT，回傳 DataFrame。快取 30 秒，避免每次互動都重查。"""
    conn = connect(DEFAULT_DB_PATH)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def write(sql, params=()):
    """跑一句會改資料的 SQL（只有設定頁會用到），改完把快取清掉。"""
    conn = connect(DEFAULT_DB_PATH)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
    q.clear()


# ═══════════════════════════════════════════════════════════════════════
# 規則 B：信賴區間 → 持平判斷
# ═══════════════════════════════════════════════════════════════════════

def nz(v, default=0.0):
    """把 None／NaN 換成預設值。

    ⚠️ 不可以用 `v or 0` 代替 —— NaN 在 Python 裡是 truthy，
       `float("nan") or 0` 會回傳 NaN，接著 round(NaN) 直接炸掉。
       這個坑在「本週有判讀、上週沒判讀」的時候一定會踩到，
       而那正是剛開始判讀時的常態。
    """
    try:
        return default if v is None or pd.isna(v) else v
    except (TypeError, ValueError):
        return default


def compare_proportions(succ_now, n_now, succ_prev, n_prev):
    """比較兩個比例，回傳 (方向, 變化幅度, 說明)

    方向只有三種："上升" / "下降" / "持平"

    判斷方式：先各自算 Wilson 95% 信賴區間，
      兩個區間有重疊 → 「持平」（這個變化跟隨機波動分不開）
      完全沒有重疊   → 才說「上升」或「下降」

    這是刻意保守的做法：寧可漏報真的變化，
    也不要讓人對雜訊做反應。
    """
    if not n_now or not n_prev:
        return ("持平", 0.0, "樣本不足，無法判斷")
    p_now, p_prev = succ_now / n_now, succ_prev / n_prev
    lo_a, hi_a = wilson_interval(succ_now, n_now)
    lo_b, hi_b = wilson_interval(succ_prev, n_prev)
    diff = p_now - p_prev
    overlap = not (hi_a < lo_b or hi_b < lo_a)
    if overlap:
        why = ("本週區間 %.1f%%~%.1f%%，上週 %.1f%%~%.1f%%，兩者重疊 → 視為持平"
               % (lo_a * 100, hi_a * 100, lo_b * 100, hi_b * 100))
        return ("持平", diff, why)
    why = ("本週區間 %.1f%%~%.1f%%，上週 %.1f%%~%.1f%%，沒有重疊 → 是真的變化"
           % (lo_a * 100, hi_a * 100, lo_b * 100, hi_b * 100))
    return ("上升" if diff > 0 else "下降", diff, why)


# KPI 色塊的底色，取自後台儀表板（純裝飾用，不帶好壞的意思）
KPI_TINT = {"teal": "#13c9ca", "amber": "#ffbc58", "purple": "#8b6fd4",
            "coral": "#ff8084", "blue": "#00a8ff", "gray": "#a5a5a5"}
_ARROW = {"持平": "→", "上升": "↑", "下降": "↓", "增加": "↑", "減少": "↓"}


def kpi(col, label, value_text, direction, diff_text,
        help_text=None, tint="teal", icon="📊"):
    """畫一個 KPI 色塊（版面照後台儀表板）。

    ⚠️ 顏色絕對不可以拿來表示方向。
      色塊底色是固定的裝飾色，delta 一律是半透明白底，
      「持平／上升／下降」由文字和箭頭承載。
      沒有任何情況會把持平畫成紅色或綠色 —— 這是 CLAUDE.md 的規則一。
    """
    import html as _html
    # ⚠️ title 屬性裡不能有真的換行 —— Streamlit 會把整段丟給 markdown 處理，
    #   換行會把屬性切斷，HTML 就爛掉。換成 &#10;（tooltip 照樣會斷行）。
    tip = _html.escape(help_text or "", quote=True).replace("\n", "&#10;")
    col.markdown(
        '<div class="kpi" style="background:%s" title="%s">'
        '<div class="kpi-ico">%s</div><div>'
        '<div class="kpi-label">%s</div>'
        '<div class="kpi-value">%s</div>'
        '<div class="kpi-delta">%s %s %s</div>'
        '</div></div>' % (
            KPI_TINT.get(tint, KPI_TINT["teal"]), tip,
            icon, _html.escape(label), _html.escape(value_text),
            _ARROW.get(direction, "→"), _html.escape(direction),
            _html.escape(diff_text)),
        unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════
# 每頁頂端固定的量測條件列（規則 A）
# ═══════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=30)
def measurement_condition():
    """從 runs 表推出目前的量測條件"""
    row = q("""
        SELECT COUNT(*) AS n_runs, MAX(ts) AS last_ts,
               COUNT(DISTINCT sample_index) AS n_samples,
               COUNT(DISTINCT engine) AS n_engines,
               COUNT(DISTINCT prompt_id) AS n_prompts
        FROM runs
    """)
    if row.empty or not row.at[0, "n_runs"]:
        return None
    cond = q("SELECT condition, locale FROM runs r WHERE 1=1 %s "
             "ORDER BY ts DESC LIMIT 1" % source_sql("r"))
    return {
        "n": int(row.at[0, "n_samples"]),
        "condition": cond.at[0, "condition"],
        "locale": cond.at[0, "locale"],
        "last_ts": row.at[0, "last_ts"],
        "engines": int(row.at[0, "n_engines"]),
        "prompts": int(row.at[0, "n_prompts"]),
        "runs": int(row.at[0, "n_runs"]),
    }


# ═══════════════════════════════════════════════════════════════════════
# 資料來源篩選
#
# 資料庫裡同時有三種來源，混在一起看會得出錯的結論：
#   demo   —— seed_demo.py 產生的亂數示範資料
#   manual —— 人工在各家 AI 網頁介面問到的真實回答
#   api    —— collect.py 自動抓的真實回答
# ═══════════════════════════════════════════════════════════════════════

# 刻意用「排除法」而不是「允許名單」：
# 以後新增量測條件（例如不同的蒐集方式）會自動被當成真實資料，
# 不會因為忘記加進名單而被無聲地濾掉。
DEMO_CONDITIONS = ("demo",)

SOURCE_CHOICES = {
    "只看真實資料": ("EXCLUDE", DEMO_CONDITIONS),
    "只看示範資料": ("ONLY", DEMO_CONDITIONS),
    "全部（會混在一起）": None,
}


def source_conditions():
    """目前選的資料來源。回傳 None 代表不篩選。"""
    return SOURCE_CHOICES.get(st.session_state.get("data_source", "只看真實資料"))


def source_sql(alias="r"):
    """給 SQL 用的篩選片段，例如 " AND r.condition NOT IN ('demo')"。"""
    choice = source_conditions()
    if not choice:
        return ""
    mode, conds = choice
    op = "NOT IN" if mode == "EXCLUDE" else "IN"
    return " AND %s.condition %s (%s)" % (
        alias, op, ",".join("'%s'" % c for c in conds))


def source_label():
    return st.session_state.get("data_source", "只看真實資料")


def metrics_notice():
    """給「數字來自 metrics_weekly」的頁面用的提醒。

    兩件必須講清楚的事：
      1. metrics_weekly 沒有 condition 欄位，它是 aggregate.py 的產出，
         而 aggregate.py 一律排除示範資料。所以這一頁**永遠是真實資料**，
         側邊欄的來源篩選對它無效 —— 不講的話使用者會以為篩選有生效。
      2. 樣本數太少的時候，信賴區間會很寬。區間寬不是算錯，
         是資料真的還不夠。這種時候不該拿數字做決定。
    """
    r = q("SELECT MAX(week) AS w, COUNT(DISTINCT week) AS weeks FROM metrics_weekly")
    if r.empty or r.at[0, "w"] is None:
        st.info("還沒有指標資料。資料進來之後執行 `python scripts/aggregate.py` 就會算出來。")
        return

    weeks = int(r.at[0, "weeks"])
    # 側邊欄選了「示範資料」或「全部」的時候，這一頁的數字還是真實資料 ——
    # 這件事不講清楚，使用者會以為篩選壞了（或更糟：以為看到的是示範資料）。
    if (source_conditions() or (None,))[0] != "EXCLUDE":
        st.info("ℹ️ **側邊欄的「資料來源」對這一頁無效，這一頁永遠是真實資料。**　"
                "本頁數字讀的是 `metrics_weekly`，那是 `aggregate.py` 的產出，"
                "而 `aggregate.py` 一律排除示範資料。\n\n"
                "要看示範資料的話，去 **Prompt 監測**、**競品比較**、**來源分析** "
                "那幾頁 —— 它們直接讀原始紀錄，篩選對它們有效。")
    st.caption("本頁數字由 `aggregate.py` 從原始回答算出，**已排除示範資料**。"
               "資料庫裡有 %d 個週次，最新是 %s。" % (weeks, r.at[0, "w"]))
    if weeks < 2:
        st.warning("⚠️ **目前只有一週的資料，所以沒有比較基準。**　"
                   "畫面上所有「持平」都是跟自己比出來的，不是真的沒變化。"
                   "至少要累積兩週才看得出趨勢。")
    else:
        # 跨週比較的前提是「問的是同一批題目」。題庫換過的話，
        # 趨勢圖上的轉折來自題目難易度不同，不是品牌表現變了 ——
        # 而且畫面上完全看不出來。所以一定要講出來。
        # ⚠️ 週次一定要用 aggregate.py 的 week_of()（ISO 週）來算。
        #    SQLite 的 strftime('%%W') 是另一種週定義，算出來會跟
        #    metrics_weekly 裡的週次差一號 —— 那會讓警告指錯週次。
        bad = prompt_set_breaks()
        if bad:
            lines = "；".join(
                "%s（%d 題）→ %s（%d 題），共同題目 %d 題"
                % (a, na, b, nb, nc) for a, b, na, nb, nc in bad)
            st.error("⛔ **題庫在週次之間換過，下面的趨勢圖不能當成變化來讀。**　"
                     "%s。\n\n"
                     "趨勢線的轉折來自「題目換了」而不是品牌表現變了。"
                     "要比較的話，兩週必須問同一批題目。" % lines)


def prompt_set_breaks():
    """找出「題庫換過」的相鄰週次對。

    回傳 [(前一週, 後一週, 前一週題數, 後一週題數, 共同題數), ...]

    跨週比較的前提是「問的是同一批題目」。題庫換過的話，趨勢圖上的轉折
    來自題目難易度不同，不是品牌表現變了 —— 而且畫面上完全看不出來。

    ⚠️ 週次一定要用 aggregate.py 的 week_of()（ISO 週）來算。
       SQLite 的 strftime('%%W') 是另一種週定義，算出來會跟
       metrics_weekly 裡的週次差一號 —— 那會讓警告指錯週次。
    """
    sets = q("""SELECT r.ts, r.prompt_id FROM runs r
                WHERE r.error IS NULL %s
                GROUP BY r.ts, r.prompt_id""" % source_sql("r"))
    if sets.empty:
        return []
    sets = sets.copy()
    sets["wk"] = sets["ts"].map(week_of)
    by = sets.dropna(subset=["wk"]).groupby("wk")["prompt_id"].apply(set).sort_index()
    out = []
    for a, b in zip(by.index, by.index[1:]):
        sa, sb = by[a], by[b]
        common = len(sa & sb)
        if common < min(len(sa), len(sb)) * 0.8:
            out.append((a, b, len(sa), len(sb), common))
    return out


def page_header(title, subtitle=None):
    """每一頁都從這裡開始：標題 ＋ 量測條件列 ＋ 右上角「示範資料」標記

    版面照 Fluv 後台：標題與副標在左，麵包屑在右。
    """
    import html as _html
    badge = {"只看真實資料": "真實資料", "只看示範資料": "示範資料"}.get(
        source_label(), "真假混合")
    st.markdown('<div class="demo-badge">%s</div>' % badge, unsafe_allow_html=True)
    st.markdown(
        '<div class="page-head"><div>'
        '<div class="ph-title">%s</div>%s'
        '</div><div class="ph-crumb">GEO 監測平台 / <b>%s</b></div></div>' % (
            _html.escape(title),
            ('<div class="ph-sub">%s</div>' % _html.escape(subtitle)) if subtitle else '',
            _html.escape(title)),
        unsafe_allow_html=True)

    c = measurement_condition()
    if c is None:
        st.warning("資料庫裡還沒有任何跑批紀錄。請先執行：python scripts/seed_demo.py")
        return
    st.markdown(
        '<div class="cond-bar">N=%d · condition=%s · locale=%s · 上次跑批 %s'
        ' &nbsp;|&nbsp; %d 個引擎 × %d 個題目 × %d 次 = %d 則回答</div>'
        % (c["n"], c["condition"], c["locale"], c["last_ts"],
           c["engines"], c["prompts"], c["n"], c["runs"]),
        unsafe_allow_html=True)


def demo_footer():
    """每頁底部再標一次資料來源。

    ⚠️ 不可以寫死「本頁數字都是示範資料」—— 階段 0 那樣寫是對的，
    但現在資料庫裡同時有真實資料與示範資料，寫死會變成一句錯的話。
    依側邊欄選的來源 ＋ 資料庫實際內容決定要講什麼。
    """
    st.divider()
    counts = q("SELECT condition, COUNT(*) n FROM runs GROUP BY condition")
    if counts.empty:
        st.caption("資料庫裡還沒有任何跑批紀錄。")
        return
    n_demo = int(counts[counts["condition"].isin(DEMO_CONDITIONS)]["n"].sum())
    n_real = int(counts["n"].sum()) - n_demo
    mode = (source_conditions() or (None,))[0]

    if mode == "EXCLUDE":
        st.caption("本頁顯示的是 **真實資料**（%d 則），已排除 %d 則示範資料。"
                   "側邊欄可以切換資料來源。" % (n_real, n_demo))
    elif mode == "ONLY":
        st.caption("⚠️ 本頁顯示的是 **示範資料**（%d 則），由 `scripts/seed_demo.py` "
                   "用亂數產生，不代表真實的品牌表現。" % n_demo)
    else:
        st.caption("⚠️ 本頁把 **真實資料（%d 則）與示範資料（%d 則）混在一起**。"
                   "示範資料是亂數，拿來做決定會出錯 —— "
                   "側邊欄切成「只看真實資料」比較安全。" % (n_real, n_demo))


# ═══════════════════════════════════════════════════════════════════════
# 共用查詢
# ═══════════════════════════════════════════════════════════════════════

def overall_trend():
    """整體（不分引擎／主題／意圖）的每週指標，由舊到新"""
    return q("""
        SELECT * FROM metrics_weekly
        WHERE engine='all' AND topic='all' AND intent='all'
        ORDER BY week
    """)


def latest_week():
    r = q("SELECT MAX(week) AS w FROM metrics_weekly")
    return None if r.empty else r.at[0, "w"]


def brand_stats(where_extra="", params=()):
    """最新一週各品牌的提及狀況（我們 ＋ 競品）"""
    return q("""
        SELECT m.entity, m.is_us,
               COUNT(*)                    AS mentions,
               COUNT(DISTINCT m.run_id)    AS runs_hit,
               AVG(m.position)             AS avg_position,
               AVG(m.sentiment_score)      AS sentiment_avg,
               SUM(m.sentiment_label='positive') AS pos,
               SUM(m.sentiment_label='neutral')  AS neu,
               SUM(m.sentiment_label='negative') AS neg,
               SUM(m.risk_flag)            AS risks
        FROM mentions m
        JOIN runs r    ON r.id = m.run_id
        JOIN prompts p ON p.id = r.prompt_id
        WHERE 1=1 """ + where_extra + source_sql("r") + """
        GROUP BY m.entity, m.is_us
        ORDER BY mentions DESC
    """, params)


def total_runs():
    return int(q("SELECT COUNT(*) AS n FROM runs").at[0, "n"])


def week_index_of(day_str, weeks):
    """把一個日期字串換算成趨勢圖 x 軸上的位置（含週內的小數）"""
    try:
        d = datetime.strptime(str(day_str)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    y, w, wd = d.isocalendar()
    label = "%04d-W%02d" % (y, w)
    if label not in weeks:
        return None
    return weeks.index(label) + (wd - 1) / 7.0


# ═══════════════════════════════════════════════════════════════════════
# 第 1 頁：總覽
# ═══════════════════════════════════════════════════════════════════════

def page_overview():
    page_header("總覽", "這一頁回答一個問題：這週我們在 AI 回答裡的表現，跟上週比有沒有真的變化？")
    metrics_notice()

    t = overall_trend()
    if t.empty:
        st.info("還沒有指標資料。請先執行 python scripts/seed_demo.py")
        demo_footer()
        return

    weeks = t["week"].tolist()

    # ---- 要看哪一週 ------------------------------------------------------
    #
    # 為什麼需要選：預設看「最新一週」，但最新那一週不一定是完整跑批 ——
    # 可能是臨時只問了幾題，也可能是跑批失敗少跑了一半。
    # 兩種情況下那一週的數字都不能代表現況，而畫面上看不出來。
    n_by_week = {w: int(x) for w, x in zip(weeks, t["n_samples"])}
    sel = st.selectbox(
        "看哪一週", weeks[::-1], key="ov_week",
        format_func=lambda w: "%s（%d 則樣本）" % (w, n_by_week[w])
                              if w in n_by_week else str(w))
    i = weeks.index(sel)
    cur = t.iloc[i]
    prev = t.iloc[i - 1] if i > 0 else cur
    lw = sel

    # CLAUDE.md 的三個「看到就要停下來」訊號之一：
    # 某一週的樣本數比平常少很多 → 多半是有呼叫失敗但沒人發現，
    # 或者那一週只是臨時抽查、不是完整跑批。
    n_now = int(cur["n_samples"])
    others = [int(x) for w, x in zip(weeks, t["n_samples"]) if w != sel]
    if n_now < 30:
        st.warning("⚠️ **%s 的樣本只有 %d 則，信賴區間會很寬。**　"
                   "這種寬度下，數字上下晃動十幾個百分點都算正常 —— "
                   "還不能拿來做決定。樣本數變 4 倍，區間寬度大約減半。" % (sel, n_now))
    if others and n_now < max(others) * 0.5:
        st.warning("⚠️ **%s 只有 %d 則樣本，其他週最多有 %d 則。**　"
                   "這一週多半不是完整跑批（可能是臨時抽查，也可能是跑批失敗少跑了）。"
                   "這種週次的數字不能代表現況 —— 上面的選單可以切到樣本多的那一週。"
                   % (sel, n_now, max(others)))

    # ---- 四個 KPI --------------------------------------------------------
    st.subheader("本週關鍵指標")
    st.caption("和上週相比。**只有當兩週的信賴區間完全沒有重疊時，才會顯示上升或下降；"
               "有重疊就是「持平」** —— 因為那個差距和隨機波動分不開。")

    n_cur, n_prev = int(cur["n_samples"]), int(prev["n_samples"])
    hits_cur, hits_prev = round(cur["visibility"] * n_cur), round(prev["visibility"] * n_prev)

    c1, c2, c3, c4, c5 = st.columns(5)

    d, diff, why = compare_proportions(hits_cur, n_cur, hits_prev, n_prev)
    kpi(c1, "能見度", "%.1f%%" % (cur["visibility"] * 100), d,
        "%+.1f pp" % (diff * 100),
        "有提到我們的回答佔比。%d / %d 則。\n\n95%% 信賴區間 %.1f%% ~ %.1f%%\n\n%s"
        % (hits_cur, n_cur, cur["ci_low"] * 100, cur["ci_high"] * 100, why),
        tint="teal", icon="👁")

    # 引用率：AI 把我們的網域當成資料來源的回答佔比。
    # ⚠️ 這和能見度是兩件事 ——「被提到」是講出品牌名，「被引用」是附上我們的連結。
    #   內容行動（draft.py → actions）直接影響的是這個，所以評估成效要看它。
    # ⚠️ 引用率可能是空的 —— 當那一週的資料沒有完整記錄引用來源時，
    #    aggregate.py 會刻意留空而不是給一個分母錯誤的數字。
    #    這裡要跟著留空，不可以當成 0%。
    if pd.isna(cur["citation_rate"]):
        kpi(c2, "引用率", "未計算", "持平", "—",
            "這一週的引用來源沒有完整記錄，所以引用率算不出來。\n\n"
            "硬算的話分母是錯的 —— 數字會看起來正常但意義是錯的。"
            "等 `collect.py` 透過 API 跑起來，回應裡就會有完整的引用網址。",
            tint="purple", icon="🔗")
    else:
        cite_cur = round(cur["citation_rate"] * n_cur)
        if pd.isna(prev["citation_rate"]):
            # 上一週算不出來 → 沒有比較基準，只能顯示本週的值
            kpi(c2, "引用率", "%.1f%%" % (cur["citation_rate"] * 100), "持平", "—",
                "AI 把我們的網域當成資料來源的回答佔比（不只是提到名字）。%d / %d 則。"
                "\n\n95%% 信賴區間 %.1f%% ~ %.1f%%\n\n"
                "上一週的引用來源沒有完整記錄，所以沒有比較基準。"
                % (cite_cur, n_cur, cur["cite_ci_low"] * 100, cur["cite_ci_high"] * 100),
                tint="purple", icon="🔗")
        else:
            cite_prev = round(prev["citation_rate"] * n_prev)
            dc, diffc, whyc = compare_proportions(cite_cur, n_cur, cite_prev, n_prev)
            kpi(c2, "引用率", "%.1f%%" % (cur["citation_rate"] * 100), dc,
                "%+.1f pp" % (diffc * 100),
                "AI 把我們的網域當成資料來源的回答佔比（不只是提到名字）。%d / %d 則。"
                "\n\n95%% 信賴區間 %.1f%% ~ %.1f%%\n\n%s"
                % (cite_cur, n_cur, cur["cite_ci_low"] * 100,
                   cur["cite_ci_high"] * 100, whyc),
                tint="purple", icon="🔗")

    # 好感度：在「有提到我們」的那些回答裡，口氣是正面的佔多少
    #
    # ⚠️ 情緒可能完全沒判讀（人工匯入的資料刻意留空，因為 CLAUDE.md 規定
    #    判讀器必須盲測）。沒判讀就老實顯示「未判讀」—— 絕對不可以當成 0%，
    #    那會變成「好感度 0%」這種憑空編出來、而且看起來很嚴重的假數字。
    if pd.isna(cur["pos_pct"]):
        kpi(c3, "好感度", "未判讀", "持平", "—",
            "情緒還沒判讀。判讀器（parse.py）已經寫好，但還沒跑過 —— "
            "它需要 ANTHROPIC_API_KEY。\n\n"
            "跑完之後還要用 eval/check_judge.py 跟 100 則人工盲測標記比對，"
            "一致率 ≥ 90% 才算過關。在那之前判讀出來的情緒不能當正式指標。",
            tint="amber", icon="💬")
    else:
        pos_cur = round(cur["pos_pct"] * hits_cur)
        pos_prev = round(nz(prev["pos_pct"]) * hits_prev)
        d2, diff2, why2 = compare_proportions(pos_cur, hits_cur, pos_prev, hits_prev)
        kpi(c3, "好感度", "%.1f%%" % (cur["pos_pct"] * 100), d2,
            "%+.1f pp" % (diff2 * 100),
            "被提到時口氣是正面的比例。%d / %d 則提及。\n\n%s" % (pos_cur, hits_cur, why2),
            tint="amber", icon="💬")

    # 風險提及：負面而且會直接影響信任的那些
    risk_n = int(q("""SELECT COUNT(*) AS n FROM mentions m JOIN runs r ON r.id=m.run_id
                      WHERE m.is_us=1 AND m.risk_flag=1 %s""" % source_sql("r")
                   ).at[0, "n"])
    if pd.isna(cur["neg_pct"]):
        kpi(c4, "風險提及", "%d 則" % risk_n, "持平", "—",
            "負面且屬於信任／急件類題目的提及（會直接影響成交）。\n\n"
            "情緒還沒判讀，所以比例算不出來。這裡顯示的是 "
            "`mentions.risk_flag` 已經被標記的筆數。",
            tint="coral", icon="⚠️")
    else:
        neg_cur = round(cur["neg_pct"] * hits_cur)
        neg_prev = round(nz(prev["neg_pct"]) * hits_prev)
        d3, diff3, why3 = compare_proportions(neg_cur, hits_cur, neg_prev, hits_prev)
        # 風險是「越少越好」，所以方向要反過來標示
        d3_disp = {"上升": "增加", "下降": "減少", "持平": "持平"}[d3]
        kpi(c4, "風險提及", "%d 則" % risk_n, "持平" if d3 == "持平" else d3_disp,
            "%+.1f pp" % (diff3 * 100),
            "負面且屬於信任／急件類題目的提及（會直接影響成交）。\n\n"
            "負面提及比例：%.1f%%\n\n%s" % (cur["neg_pct"] * 100, why3),
            tint="coral", icon="⚠️")

    # 平均排序：不是比例，沒有 Wilson 區間。
    # 用一個保守的門檻：差距小於 0.2 名就當作持平（樣本雜訊大約就是這個量級）。
    # ⚠️ 不可以寫 `cur["avg_position"] or 0` —— NaN 在 Python 裡是 truthy，
    #    or 0 擋不住，畫面就會出現「第 nan 名」。
    #    而且平均排序算不出來的時候要老實說，不能顯示成「第 0 名」（那是最前面）。
    pos_now = cur["avg_position"]
    pos_was = prev["avg_position"]
    if pd.isna(pos_now):
        kpi(c5, "平均排序", "未記錄", "持平", "—",
            "我們被提到時平均排在第幾個（越小越前面）。\n\n"
            "這一週的資料沒有記錄排序 —— 九月那批是人工統計，"
            "只記了「有沒有被提到」，沒有記第幾個。"
            "等 `collect.py` 透過 API 跑起來，判讀器就會把排序記下來。",
            tint="blue", icon="🏅")
    else:
        if pd.isna(pos_was):
            d4, dp_txt = "持平", "—"
        else:
            dp = pos_now - pos_was
            # 排序變小 = 變前面 = 好
            d4 = "持平" if abs(dp) < 0.2 else ("上升" if dp < 0 else "下降")
            dp_txt = "%+.2f 名" % dp
        kpi(c5, "平均排序", "第 %.2f 名" % pos_now, d4, dp_txt,
            "我們被提到時平均排在第幾個（越小越前面）。\n\n"
            "這不是比例，沒有 Wilson 區間；改用保守門檻：差距小於 0.2 名就當作持平。"
            + ("\n\n上一週沒有記錄排序，所以沒有比較基準。" if pd.isna(pos_was) else ""),
            tint="blue", icon="🏅")

    with st.expander("這些數字怎麼算出來的？（可以自己拿計算機驗算）"):
        st.markdown("""
**能見度 = 有提到我們的回答數 ÷ 總回答數**

- 本週：%d ÷ %d = **%.4f**（%.1f%%）
- 95%% Wilson 信賴區間：**%.1f%% ~ %.1f%%**
- 上週：%d ÷ %d = %.4f（%.1f%%），區間 %.1f%% ~ %.1f%%
- 判定：%s

**為什麼區間這麼寬？** 因為 AI 每次回答都不一樣。要讓區間縮到一半，
樣本數要變成四倍（例如 N 從 10 提高到 40）。

想看完整推導：`python scripts/aggregate.py --explain %d %d`
        """ % (hits_cur, n_cur, cur["visibility"], cur["visibility"] * 100,
               cur["ci_low"] * 100, cur["ci_high"] * 100,
               hits_prev, n_prev, prev["visibility"], prev["visibility"] * 100,
               wilson_interval(hits_prev, n_prev)[0] * 100,
               wilson_interval(hits_prev, n_prev)[1] * 100,
               why, hits_cur, n_cur))

    st.divider()

    # ---- 兩張趨勢圖：能見度 ＋ 引用率 -------------------------------------
    #
    # ⚠️ 兩者不可以共用同一個 y 軸。引用率的絕對值比能見度小一個量級，
    #   疊在同一張圖上會被壓成一條貼著底的平線，而且雙 y 軸會誤導比例。
    #   分成兩張並排（小倍數圖），各自的 y 軸範圍獨立。
    st.subheader("趨勢")
    st.caption("淺色陰影是 95% 信賴區間 —— 線在陰影範圍內上下跳動不代表有變化。"
               "琥珀色虛線是我們採取行動的日期，用來看「線是在哪次行動之後才開始往上」。"
               "**評估內容行動有沒有效，看右邊的引用率** —— 那是我們寫的文章直接影響的東西。")

    x = list(range(len(weeks)))
    acts = q("SELECT * FROM actions WHERE published_at IS NOT NULL ORDER BY published_at")

    def trend_fig(val_col, lo_col, hi_col, name, color, band, y_title):
        """畫一條趨勢線 ＋ 信賴區間陰影 ＋ 行動標記線"""
        f = go.Figure()
        f.add_trace(go.Scatter(
            x=x + x[::-1],
            y=(t[hi_col] * 100).tolist() + (t[lo_col] * 100).tolist()[::-1],
            fill="toself", fillcolor=band, line=dict(width=0),
            hoverinfo="skip", name="95% 信賴區間"))
        f.add_trace(go.Scatter(
            x=x, y=(t[val_col] * 100).round(1),
            mode="lines+markers", line=dict(color=color, width=3),
            name=name, customdata=t["n_samples"],
            hovertemplate=name + " %{y:.1f}%<br>樣本數 %{customdata} 則<extra></extra>"))
        for _, a in acts.iterrows():
            xi = week_index_of(a["published_at"], weeks)
            if xi is None:
                continue
            f.add_vline(x=xi, line_dash="dash", line_color=C_ACTION, line_width=2,
                        annotation_text="▲", annotation_position="top",
                        annotation_font=dict(size=11, color="#8a5d00"))
        f.update_layout(height=360, margin=dict(t=40, b=10, l=10, r=10),
                        xaxis=dict(tickmode="array", tickvals=x, ticktext=weeks),
                        yaxis=dict(title=y_title, rangemode="tozero"),
                        showlegend=False, hovermode="x unified")
        return f

    tA, tB = st.columns(2)
    with tA:
        st.markdown("**能見度** —— AI 有沒有講出我們的名字")
        st.plotly_chart(trend_fig("visibility", "ci_low", "ci_high",
                                  "能見度", C_US, C_BAND, "能見度 (%)"))
    with tB:
        st.markdown("**引用率** —— AI 有沒有把我們的網站當成資料來源")
        st.plotly_chart(trend_fig("citation_rate", "cite_ci_low", "cite_ci_high",
                                  "引用率", C_CITE, C_CITE_BAND, "引用率 (%)"))

    if not acts.empty:
        st.caption("行動標記：" + " · ".join(
            "%s（%s）" % (a["title"], a["published_at"]) for _, a in acts.iterrows()))

    st.divider()

    # ---- 情緒比例堆疊趨勢 ------------------------------------------------
    cA, cB = st.columns([3, 2])
    with cA:
        st.subheader("口氣的正／中／負比例")
        st.caption("在有提到我們的回答裡，口氣的分布怎麼變。")
        if t[["pos_pct", "neu_pct", "neg_pct"]].isna().all().all():
            # 情緒全部是空值 —— 畫出來會是一張空圖，看起來像「沒有負評」，
            # 那是假的安心感。老實說明原因比畫一張空圖好。
            st.info("情緒還沒判讀，所以畫不出比例趨勢。\n\n"
                    "判讀器（`parse.py`）已經寫好，但還沒跑過 —— 它需要 "
                    "`ANTHROPIC_API_KEY`。\n\n"
                    "跑完之後還要依照 CLAUDE.md 用 100 則人工盲測標記驗收，"
                    "一致率 ≥ 90% 才准當正式指標 —— "
                    "那是整個系統唯一真正的品質關卡。")
        else:
            f2 = go.Figure()
            for name, col, color in [("正面", "pos_pct", C_POS), ("中性", "neu_pct", C_NEU),
                                     ("負面", "neg_pct", C_NEG)]:
                # marker_line 是刻意的：堆疊區段之間要有 2px 的底色留白，
                # 不然三個色塊直接相黏，邊界會糊掉。
                f2.add_trace(go.Bar(
                    x=weeks, y=pd.to_numeric(t[col], errors="coerce").mul(100).round(1),
                    name=name, marker_color=color,
                    marker_line=dict(color="#ffffff", width=2),
                    hovertemplate=name + " %{y:.1f}%<extra></extra>"))
            f2.update_layout(barmode="stack", height=330, margin=dict(t=20, b=10, l=10, r=10),
                             bargap=0.28,
                             yaxis=dict(title="佔比 (%)"), legend=dict(orientation="h", y=1.12))
            st.plotly_chart(f2)

    with cB:
        st.subheader("各引擎表現")
        st.caption("同一批題目，不同 AI 的答案差很多。誤差線是 95% 信賴區間。")
        eng = q("""SELECT engine, visibility, ci_low, ci_high, n_samples
                   FROM metrics_weekly
                   WHERE week=? AND topic='all' AND intent='all' AND engine<>'all'
                   ORDER BY visibility DESC""", (lw,))
        f3 = go.Figure(go.Bar(
            x=(eng["visibility"] * 100).round(1), y=eng["engine"], orientation="h",
            marker_color=C_US,
            error_x=dict(type="data", symmetric=False,
                         array=((eng["ci_high"] - eng["visibility"]) * 100),
                         arrayminus=((eng["visibility"] - eng["ci_low"]) * 100),
                         color=C_MUTED),
            customdata=eng["n_samples"],
            hovertemplate="%{y}: %{x:.1f}%<br>樣本 %{customdata} 則<extra></extra>"))
        f3.update_layout(height=330, margin=dict(t=20, b=10, l=10, r=10),
                         xaxis=dict(title="能見度 (%)"))
        st.plotly_chart(f3)
        st.caption("注意每根誤差線都很長：單一引擎只有 %d 則樣本，"
                   "引擎之間的排名經常在誤差範圍內。"
                   % (int(eng["n_samples"].iloc[0]) if not eng.empty else 0))

    st.divider()

    # ---- 競品比較 --------------------------------------------------------
    st.subheader("競品比較（本週）")
    b = brand_stats()
    if not b.empty:
        n_all = total_runs()
        b["能見度"] = (b["runs_hit"] / n_all * 100).round(1)
        b["聲量佔比 SoV"] = (b["mentions"] / b["mentions"].sum() * 100).round(1)
        colors = [C_US if int(u) else C_OTHER for u in b["is_us"]]
        f4 = go.Figure(go.Bar(x=b["entity"], y=b["能見度"], marker_color=colors,
                              text=b["能見度"].map(lambda v: "%.1f%%" % v),
                              textposition="outside",
                              hovertemplate="%{x}: %{y:.1f}%<extra></extra>"))
        f4.update_layout(height=320, margin=dict(t=20, b=10, l=10, r=10),
                         yaxis=dict(title="出現在多少比例的回答中 (%)"))
        st.plotly_chart(f4)

    st.divider()

    # ---- 重點洞察 --------------------------------------------------------
    st.subheader("重點洞察")
    st.caption("以下每一句都是從上面的資料自動整理出來的，不是人寫死的。")
    insights = build_insights(t, weeks, lw, prompt_set_breaks())
    for tone, text in insights:
        {"good": st.success, "bad": st.error, "flat": st.info}[tone](text)

    demo_footer()


def build_insights(t, weeks, week, breaks=()):
    """從資料自動整理出幾句人看得懂的結論。

    week 是使用者在畫面上選的那一週，不是永遠用最新的 ——
    最新那一週可能只是臨時抽查（樣本很少），拿它下結論會錯。

    breaks 是 prompt_set_breaks() 的結果。題庫在這兩週之間換過的話，
    這組比較根本無效 —— 不可以說「持平」。
    「持平」的意思是「量到了，而且沒變」，跟「這兩個數字不能比」完全不同，
    但兩句都長得像「沒事」，並排顯示會自相矛盾（畫面上真的發生過：
    一邊說「持平，不需要處理」，一邊說「不能當成變化來讀」）。
    """
    out = []
    i = weeks.index(week)
    cur = t.iloc[i]
    prev = t.iloc[i - 1] if i > 0 else cur
    n_cur, n_prev = int(cur["n_samples"]), int(prev["n_samples"])

    # 題庫換過的話，所有「跨週比較」的句子一律不產出 ——
    # 只跳過比較，這一週自己的事實（最弱意圖、最弱引擎、待處理缺口）照樣要講。
    broke = next((b for b in breaks if b[1] == week), None)
    if broke:
        # 完整說明已經印在趨勢圖上方了，這裡只補「所以這個數字不能解讀」，
        # 不重複一遍題數 —— 同一頁講兩次同一件事會讓人以為是兩個問題。
        out.append(("bad",
                    "**%.1f%% → %.1f%% 這個變化不能解讀** —— 題庫在這兩週之間換過，"
                    "原因見趨勢圖上方的說明。"
                    % (prev["visibility"] * 100, cur["visibility"] * 100)))
    else:
        d, diff, _ = compare_proportions(round(cur["visibility"] * n_cur), n_cur,
                                         round(prev["visibility"] * n_prev), n_prev)
        if d == "持平":
            out.append(("flat", "**整體能見度和上週持平**（%.1f%% → %.1f%%，"
                                "差距落在信賴區間內，屬於隨機波動，不需要處理）。"
                        % (prev["visibility"] * 100, cur["visibility"] * 100)))
        else:
            out.append(("good" if diff > 0 else "bad",
                        "**整體能見度%s**：%.1f%% → %.1f%%，兩週的信賴區間沒有重疊，"
                        "這是真的變化。"
                        % (d, prev["visibility"] * 100, cur["visibility"] * 100)))

        # 頭尾比較（樣本累積起來，比單週比較有力）。
        # 中間只要有任何一次換過題庫，頭尾也不能比 —— 同樣跳過。
        if not breaks:
            first = t.iloc[0]
            n_f = int(first["n_samples"])
            d8, diff8, _ = compare_proportions(round(cur["visibility"] * n_cur), n_cur,
                                               round(first["visibility"] * n_f), n_f)
            if d8 != "持平":
                out.append(("good" if diff8 > 0 else "bad",
                            "**長期趨勢明確**：%s → %s，能見度從 %.1f%% 變成 %.1f%%"
                            "（%+.1f 個百分點），這個幅度已經超出誤差範圍。"
                            % (weeks[0], weeks[-1], first["visibility"] * 100,
                               cur["visibility"] * 100, diff8 * 100)))

    # 最弱的意圖（看使用者選的那一週）
    lw = week
    it = q("""SELECT intent, visibility, n_samples FROM metrics_weekly
              WHERE week=? AND engine='all' AND topic='all' AND intent<>'all'
              ORDER BY visibility ASC LIMIT 1""", (lw,))
    if not it.empty:
        r = it.iloc[0]
        w = INTENT_WEIGHTS.get(r["intent"], 1.0)
        note = "而且這是權重最高的意圖之一（%.1f），直接影響成交" % w if w >= 1.4 else "權重 %.1f" % w
        out.append(("bad", "**最弱的一塊是 `%s` 類題目**，能見度只有 %.1f%%（%d 則樣本）—— %s。"
                    % (r["intent"], r["visibility"] * 100, int(r["n_samples"]), note)))

    # 最弱的引擎
    eng = q("""SELECT engine, visibility FROM metrics_weekly
               WHERE week=? AND topic='all' AND intent='all' AND engine<>'all'
               ORDER BY visibility ASC LIMIT 1""", (lw,))
    if not eng.empty:
        out.append(("flat", "**`%s` 是表現最弱的引擎**（%.1f%%）。單一引擎的樣本數只有整體的五分之一，"
                            "排名容易在誤差內晃動，連續兩三週都墊底才值得處理。"
                    % (eng.at[0, "engine"], eng.at[0, "visibility"] * 100)))

    # 待處理的缺口
    g = q("SELECT COUNT(*) AS n, MAX(opportunity_score) AS top FROM gaps WHERE status='open'")
    if not g.empty and g.at[0, "n"]:
        top = q("SELECT title FROM gaps WHERE status='open' ORDER BY opportunity_score DESC LIMIT 1")
        out.append(("flat", "**目前有 %d 個未處理的缺口**，機會分數最高的是「%s」（%.2f 分）。"
                            "詳情在「機會建議」頁。"
                    % (int(g.at[0, "n"]), top.at[0, "title"], g.at[0, "top"])))
    return out


# ═══════════════════════════════════════════════════════════════════════
# 第 2 頁：Prompt 監測
# ═══════════════════════════════════════════════════════════════════════

def highlight(answer, snippets):
    """把判定依據那幾句在原文裡標記起來（黃色底）"""
    safe = html.escape(answer)
    for s in sorted({x for x in snippets if x}, key=len, reverse=True):
        es = html.escape(s)
        if es in safe:
            safe = safe.replace(es, '<mark class="hit">%s</mark>' % es)
    return safe


def read_raw(rel_path):
    """讀出一則原始回答。原始檔案是唯讀的資產，這裡只讀不寫。"""
    # 呼叫失敗的 run 沒有原始檔（raw_path 是空的）。
    # 這不是錯誤，是正常狀態 —— 失敗的原因存在 runs.error。
    if not rel_path:
        return None, None
    full = os.path.join(ROOT_DIR, rel_path)
    if not os.path.exists(full):
        return None, "找不到原始檔案：%s" % rel_path
    try:
        with gzip.open(full, "rt", encoding="utf-8") as f:
            return json.load(f), None
    except (OSError, ValueError) as e:
        return None, "原始檔案讀不開（%s）：%s" % (rel_path, e)


def page_prompts():
    page_header("Prompt 監測",
                "每一題我們表現如何，以及點進去看 AI 到底是怎麼回答的、判讀依據是哪一句。")

    prompts = q("SELECT * FROM prompts ORDER BY id")
    if prompts.empty:
        st.info("題庫是空的。請到「設定」頁新增題目，或執行 python scripts/seed_demo.py")
        demo_footer()
        return

    # 每題的能見度（本週）
    stats = q("""
        SELECT r.prompt_id,
               COUNT(DISTINCT r.id) AS runs,
               COUNT(DISTINCT CASE WHEN m.is_us=1 THEN r.id END) AS hits,
               AVG(CASE WHEN m.is_us=1 THEN m.position END) AS avg_position,
               SUM(CASE WHEN m.is_us=1 AND m.sentiment_label='negative' THEN 1 ELSE 0 END) AS neg
        FROM runs r LEFT JOIN mentions m ON m.run_id = r.id
        WHERE 1=1 %s
        GROUP BY r.prompt_id
    """ % source_sql("r"))

    # 有幾則「真的留下回答原文」。
    # 人工統計那批（manual-tally）只記了有沒有被提到，沒有原文可讀 ——
    # 不標出來的話，使用者點到那些題目會以為畫面壞了。
    full = q("""
        SELECT r.prompt_id, COUNT(*) AS n_full
        FROM runs r
        WHERE r.error IS NULL AND r.condition NOT IN ('manual-tally') %s
        GROUP BY r.prompt_id
    """ % source_sql("r"))
    df = prompts.merge(stats, left_on="id", right_on="prompt_id", how="left")
    df = df.merge(full[["prompt_id", "n_full"]], left_on="id", right_on="prompt_id",
                  how="left", suffixes=("", "_f"))
    df = df.fillna({"runs": 0, "hits": 0, "neg": 0, "n_full": 0})

    c1, c2, c3 = st.columns(3)
    topics = ["全部"] + sorted(prompts["topic"].dropna().unique().tolist())
    topic = c1.selectbox("主題", topics)
    intent = c2.selectbox("意圖", ["全部"] + INTENTS)
    only_active = c3.selectbox("狀態", ["只看啟用中", "全部"])

    view = df.copy()
    if topic != "全部":
        view = view[view["topic"] == topic]
    if intent != "全部":
        view = view[view["intent"] == intent]
    if only_active == "只看啟用中":
        view = view[view["active"] == 1]

    if view.empty:
        st.warning("這組篩選條件下沒有題目。")
        demo_footer()
        return

    # 能見度與信賴區間
    view["能見度"] = view.apply(
        lambda r: r["hits"] / r["runs"] if r["runs"] else 0, axis=1)
    view["區間"] = view.apply(
        lambda r: "%.0f%% ~ %.0f%%" % tuple(v * 100 for v in
                                            wilson_interval(int(r["hits"]), int(r["runs"]))),
        axis=1)

    left, right = st.columns([1.3, 1], gap="medium")

    with left:
        st.subheader("題目清單")
        st.caption("點一列，右邊會顯示那一題的完整原始回答。"
                   "**「原文」那一欄是 0 的題目讀不到內容** —— "
                   "那批是人工統計的，只記了有沒有被提到。")
        # 欄位刻意只留最必要的四欄 —— 區間、平均排序等細節放在右邊的詳情區，
        # 表格塞太多欄會把「能見度」擠到看不見。
        show = pd.DataFrame({
            "id": view["id"],
            "題目": view["text"],
            "主題": view["topic"],
            # ⚠️ ×100：Streamlit 的 format 是直接套在原始值上，不會自動換算成百分比
            "能見度": (view["能見度"] * 100).round(1),
            "樣本": view["runs"].astype(int),
            "有原文": view["n_full"].astype(int),
        })
        sel = st.dataframe(
            show, hide_index=True, width="stretch", height=420,
            on_select="rerun", selection_mode="single-row", key="prompt_table",
            column_config={
                # 欄寬要指定，不然「題目」會把後面的欄位擠出畫面外
                "id": st.column_config.NumberColumn("#", width="small"),
                "題目": st.column_config.TextColumn("題目", width="medium"),
                "主題": st.column_config.TextColumn("主題", width="small"),
                "樣本": st.column_config.NumberColumn("樣本", width="small"),
                "有原文": st.column_config.NumberColumn(
                    "原文", width="small",
                    help="有幾則留下了回答原文、可以讀。0 代表那一題只有人工統計的"
                         "「有沒有被提到」，沒有原文 —— 等 collect.py 透過 API "
                         "跑起來才會有。"),
                "能見度": st.column_config.ProgressColumn(
                    "能見度", format="%.0f%%", min_value=0, max_value=100,
                    width="small"),
            })
        rows = sel.selection["rows"] if hasattr(sel, "selection") else []
        pid = int(show.iloc[rows[0]]["id"]) if rows else int(show.iloc[0]["id"])

    with right:
        row = view[view["id"] == pid].iloc[0]
        st.subheader("原始回答")
        st.markdown("**#%d %s**" % (pid, row["text"]))
        st.caption("主題：%s · 意圖：%s（權重 %.1f） · 能見度 %.0f%%（95%% 區間 %s，"
                   "%d 則樣本） · 平均排序 %s · %s"
                   % (row["topic"], row["intent"], row["intent_weight"],
                      row["能見度"] * 100, row["區間"], int(row["runs"]),
                      ("第 %.2f 名" % row["avg_position"]) if pd.notna(row["avg_position"])
                      else "—",
                      "啟用中" if row["active"] else "已停用"))

        runs = q("""SELECT r.id, r.engine, r.model_version, r.sample_index, r.ts,
                           r.raw_path, r.cost, r.error,
                           (SELECT COUNT(*) FROM mentions m
                            WHERE m.run_id=r.id AND m.is_us=1) AS has_us
                    FROM runs r WHERE r.prompt_id=? %s
                    ORDER BY r.engine, r.sample_index""" % source_sql("r"),
                 (int(pid),))
        if runs.empty:
            st.info("這一題在「%s」這個範圍內還沒有資料。" % source_label())
            demo_footer()
            return

        eng_list = sorted(runs["engine"].unique().tolist())

        def engine_label(e):
            """引擎選單上直接標出模型版本。

            為什麼要標：模型一換版，數字就會跳。不標的話，
            看到能見度掉了會以為是自己的內容出問題，
            其實只是 OpenAI 或 Google 改版了。
            同一家如果跨版本跑過，這裡會提示有幾個版本。
            """
            versions = sorted(set(
                v for v in runs[runs["engine"] == e]["model_version"] if v))
            if not versions:
                return "%s（沒記模型版本）" % e
            if len(versions) == 1:
                return "%s · %s" % (e, versions[0])
            return "%s · %d 個模型版本（數字不可直接比）" % (e, len(versions))

        e1, e2 = st.columns([1, 1])
        engine = e1.selectbox("引擎與模型", eng_list, key="pm_engine",
                              format_func=engine_label)
        sub = runs[runs["engine"] == engine]
        labels = ["第 %d 次 · $%.4f%s"
                  % (int(r["sample_index"]) + 1,
                     r["cost"] if pd.notna(r["cost"]) else 0.0,
                     "　✅ 有提到我們" if r["has_us"] else "　—")
                  for _, r in sub.iterrows()]
        idx = e2.selectbox("第幾次提問與花費", range(len(labels)),
                           format_func=lambda i: labels[i], key="pm_sample")
        run = sub.iloc[idx]

        # 這一題到目前為止花了多少 —— 調 --samples 的時候需要這個數字才知道划不划算
        st.caption("這一題累計跑了 %d 次、總花費 $%.4f；"
                   "其中 %s 跑了 %d 次、$%.4f"
                   % (len(runs), runs["cost"].fillna(0).sum(),
                      engine, len(sub), sub["cost"].fillna(0).sum()))

        payload, err = read_raw(run["raw_path"])
        if run["error"]:
            # 失敗的 run 沒有原文。這不是程式出錯，是那次呼叫 API 失敗了。
            st.error("**這一次呼叫失敗了**\n\n%s" % run["error"])
            st.caption("算指標時失敗的 run 會被排除在分母外，"
                       "不會被當成「沒提到我們」而讓能見度憑空變低。")
        elif err or payload is None:
            st.error(err or "讀不到原始檔案。")
        else:
            mentions = q("SELECT * FROM mentions WHERE run_id=? ORDER BY position",
                         (int(run["id"]),))
            # 兩種檔案格式的欄位名不一樣：
            #   collect.py 寫的真實資料用 answer_text
            #   seed_demo.py 寫的示範資料用 answer
            # 只讀其中一種的話，另一種會顯示成空白 —— 而且不會報錯。
            answer = payload.get("answer_text") or payload.get("answer") or ""
            meta_early = payload.get("meta") or {}
            if not answer:
                # 畫一個空框會讓人以為程式壞了。老實說明為什麼沒有原文。
                tally = payload.get("tally") or {}
                st.info("**這一則沒有保留回答原文。**\n\n%s\n\n"
                        "人工標記的結果：%s"
                        % (meta_early.get("collection_method")
                           or "這批資料只記錄了判讀結果，沒有存下 AI 的回答全文。",
                           tally.get("毛小愛有無被提及", "（沒有紀錄）")))
                for c in (meta_early.get("caveats") or [])[:3]:
                    st.caption("· %s" % c)
                st.caption("要能讀原文，得讓 `collect.py` 透過 API 去問 —— "
                           "API 的回應會連完整回答與引用網址一起存進 `data/raw/`。")
            else:
                st.markdown(
                    '<div class="answer-box">%s</div>'
                    % highlight(answer, mentions["snippet"].tolist()),
                    unsafe_allow_html=True)
            if "_demo" in payload:
                st.caption("⚠️ 這是 `seed_demo.py` 產生的示範資料，不是真的 AI 回答。")
            meta = payload.get("meta") or {}
            if meta.get("collection_method"):
                st.caption("蒐集方式：%s" % meta["collection_method"])
            st.caption("模型 %s · 提問時間 %s · 花費 $%.4f · 檔案 `%s`"
                       % (run["model_version"], run["ts"], run["cost"] or 0,
                          run["raw_path"]))

            st.markdown("**判讀結果**（黃色標記就是判定依據的那一句）")
            if mentions.empty:
                st.info("這則回答沒有提到任何品牌。")
            else:
                m = mentions.copy()
                m["品牌"] = m.apply(
                    lambda r: ("🔵 " if r["is_us"] else "⚪ ") + r["entity"], axis=1)
                # 情緒可能是空的 —— 人工蒐集的資料刻意不判情緒，
                # 因為 CLAUDE.md 規定判讀器必須盲測，知道誰是客戶的人標了一定偏。
                # 空值要老實顯示「未判讀」，不可以當成中性，那等於偷偷編了一個數字。
                m["口氣"] = m.apply(
                    lambda r: {"positive": "🙂 正面", "neutral": "😐 中性",
                               "negative": "🙁 負面"}.get(r["sentiment_label"], "－ 未判讀")
                    + ("　⚠️ 風險" if r["risk_flag"] else ""), axis=1)
                st.dataframe(
                    m[["position", "品牌", "口氣", "sentiment_score"]].rename(
                        columns={"position": "排序", "sentiment_score": "情緒分數"}),
                    hide_index=True, width="stretch")

            cits = q("SELECT * FROM citations WHERE run_id=? ORDER BY rank", (int(run["id"]),))
            if not cits.empty:
                st.markdown("**這則回答引用了**")
                st.dataframe(cits[["rank", "domain", "source_type", "url"]].rename(
                    columns={"rank": "順位", "domain": "網域", "source_type": "類型",
                             "url": "網址"}), hide_index=True, width="stretch")
            else:
                st.caption("這則回答沒有附引用來源。")

    demo_footer()


# ═══════════════════════════════════════════════════════════════════════
# 第 3 頁：受眾主題
# ═══════════════════════════════════════════════════════════════════════

def page_topics():
    page_header("受眾主題",
                "同樣的品牌，在不同主題上的表現差很多。這一頁找出「我們在哪類問題上被蓋過去」。")

    matrix = q("""
        SELECT p.topic, m.entity, m.is_us,
               COUNT(DISTINCT m.run_id) AS hits,
               (SELECT COUNT(*) FROM runs r2 JOIN prompts p2 ON p2.id=r2.prompt_id
                WHERE p2.topic = p.topic %s) AS total
        FROM mentions m
        JOIN runs r    ON r.id = m.run_id
        JOIN prompts p ON p.id = r.prompt_id
        WHERE 1=1 %s
        GROUP BY p.topic, m.entity
    """ % (source_sql("r2"), source_sql("r")))
    if matrix.empty:
        st.info("還沒有判讀資料。")
        demo_footer()
        return

    matrix["能見度"] = matrix["hits"] / matrix["total"] * 100
    pivot = matrix.pivot_table(index="entity", columns="topic",
                               values="能見度", fill_value=0)
    # 我們排在最上面，競品依整體表現排序
    order = [OUR_BRAND] + [e for e in pivot.mean(axis=1).sort_values(ascending=False).index
                           if e != OUR_BRAND]
    pivot = pivot.reindex([e for e in order if e in pivot.index])

    st.subheader("主題 × 品牌 表現矩陣")
    st.caption("數字是「在這個主題的所有回答中，這個品牌出現在多少比例的回答裡」。"
               "顏色越深表示越常出現。第一列是我們。")

    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=pivot.columns.tolist(), y=pivot.index.tolist(),
        colorscale=[[i / (len(C_SCALE) - 1), c] for i, c in enumerate(C_SCALE)], zmin=0, zmax=max(60, float(pivot.values.max())),
        text=[["%.0f%%" % v for v in row] for row in pivot.values],
        texttemplate="%{text}", textfont=dict(size=13),
        hovertemplate="%{y}<br>%{x}：%{z:.1f}%<extra></extra>",
        colorbar=dict(title="能見度 %")))
    fig.update_layout(height=90 + 46 * len(pivot), margin=dict(t=20, b=10, l=10, r=10),
                      xaxis=dict(side="top"),
                      # plotly 預設把第一列畫在最下面，反轉過來我們才會在最上面
                      yaxis=dict(autorange="reversed"))
    st.plotly_chart(fig)

    st.divider()
    cA, cB = st.columns([1.3, 1])

    with cA:
        st.subheader("各主題的落差")
        st.caption("「落差」＝ 最強的競品能見度 − 我們的能見度。正值越大代表被蓋得越兇。")
        rows = []
        for topic in pivot.columns:
            ours = float(pivot.loc[OUR_BRAND, topic]) if OUR_BRAND in pivot.index else 0.0
            comp = pivot.drop(index=[OUR_BRAND], errors="ignore")[topic]
            best = comp.idxmax() if not comp.empty else "—"
            best_v = float(comp.max()) if not comp.empty else 0.0
            rows.append({"主題": topic, "我們": ours, "最強競品": best,
                         "競品能見度": best_v, "落差": best_v - ours})
        gapdf = pd.DataFrame(rows).sort_values("落差", ascending=False)
        st.dataframe(gapdf, hide_index=True, width="stretch",
                     column_config={
                         "我們": st.column_config.NumberColumn(format="%.1f%%"),
                         "競品能見度": st.column_config.NumberColumn(format="%.1f%%"),
                         "落差": st.column_config.NumberColumn(format="%+.1f pp"),
                     })

    with cB:
        st.subheader("各主題的意圖權重")
        st.caption("落差大、權重又高的主題，就是「機會建議」頁排最前面的那幾個。")
        tw = q("""SELECT topic AS 主題, intent AS 意圖, COUNT(*) AS 題數,
                         AVG(intent_weight) AS 權重
                  FROM prompts GROUP BY topic, intent ORDER BY 權重 DESC""")
        st.dataframe(tw, hide_index=True, width="stretch",
                     column_config={"權重": st.column_config.NumberColumn(format="%.1f")})
        st.caption("trust（信任查證）與 urgent（緊急需求）權重最高 —— "
                   "寵物托育是信任門檻極高的服務，這兩類問題直接影響成交。")

    demo_footer()


# ═══════════════════════════════════════════════════════════════════════
# 第 4 頁：競品比較
# ═══════════════════════════════════════════════════════════════════════

def page_competitors():
    page_header("競品比較", "同一批題目、同一批回答，我們和競品各拿到多少版面。")

    b = brand_stats()
    if b.empty:
        st.info("還沒有判讀資料。")
        demo_footer()
        return

    n_all = total_runs()
    b["能見度"] = b["runs_hit"] / n_all
    b["SoV"] = b["mentions"] / b["mentions"].sum()
    b["正面比例"] = b["pos"] / b["mentions"]
    b["負面比例"] = b["neg"] / b["mentions"]
    b["區間"] = b.apply(lambda r: "%.0f%% ~ %.0f%%" % tuple(
        v * 100 for v in wilson_interval(int(r["runs_hit"]), n_all)), axis=1)

    c1, c2 = st.columns([1, 1])
    with c1:
        st.subheader("聲量佔比 SoV")
        st.caption("所有品牌提及次數中，各家佔多少。")
        f = go.Figure(go.Pie(
            labels=b["entity"], values=b["mentions"], hole=0.45,
            marker=dict(colors=[C_US if int(u) else C_OTHER for u in b["is_us"]]),
            textinfo="label+percent",
            hovertemplate="%{label}：%{value} 次提及（%{percent}）<extra></extra>"))
        f.update_layout(height=340, margin=dict(t=10, b=10, l=10, r=10), showlegend=False)
        st.plotly_chart(f)

    with c2:
        st.subheader("平均排序")
        st.caption("被提到時平均排在第幾個。柱子越短越好（越前面）。")
        bs = b.sort_values("avg_position")
        f = go.Figure(go.Bar(
            x=bs["avg_position"].round(2), y=bs["entity"], orientation="h",
            marker_color=[C_US if int(u) else C_OTHER for u in bs["is_us"]],
            text=bs["avg_position"].map(lambda v: "第 %.2f 名" % v),
            textposition="outside",
            hovertemplate="%{y}：第 %{x:.2f} 名<extra></extra>"))
        f.update_layout(height=340, margin=dict(t=10, b=10, l=10, r=10),
                        xaxis=dict(title="平均排序（越小越前面）"))
        st.plotly_chart(f)

    st.divider()
    st.subheader("完整比較表")
    table = pd.DataFrame({
        "品牌": b.apply(lambda r: ("🔵 " + r["entity"] + "（我們）") if r["is_us"]
                       else r["entity"], axis=1),
        # ⚠️ 這幾欄都先 ×100 —— 表格的 format 不會自動把 0.43 換算成 43%
        "能見度": (b["能見度"] * 100).round(1),
        "95% 區間": b["區間"],
        "SoV": (b["SoV"] * 100).round(1),
        "提及次數": b["mentions"].astype(int),
        "平均排序": pd.to_numeric(b["avg_position"], errors="coerce").round(2),
        # 情緒相關的三欄在「還沒判讀情緒」時會整欄是空值。
        # to_numeric 把空值轉成 NaN（表格會顯示成空白），
        # 直接相乘的話 pandas 會因為欄位是 object 型別而報錯。
        "情緒均分": pd.to_numeric(b["sentiment_avg"], errors="coerce").round(3),
        "正面比例": pd.to_numeric(b["正面比例"], errors="coerce").mul(100).round(1),
        "負面比例": pd.to_numeric(b["負面比例"], errors="coerce").mul(100).round(1),
        "風險提及": b["risks"].astype(int),
    }).sort_values("SoV", ascending=False)
    st.dataframe(table, hide_index=True, width="stretch", column_config={
        "能見度": st.column_config.ProgressColumn(format="%.0f%%", min_value=0,
                                                 max_value=100),
        "SoV": st.column_config.NumberColumn(format="%.1f%%"),
        "正面比例": st.column_config.NumberColumn(format="%.0f%%"),
        "負面比例": st.column_config.NumberColumn(format="%.0f%%"),
    })
    st.caption("⚠️ 名次靠得很近時不要當真：每一格的 95%% 區間大約有 ±4 個百分點寬"
               "（%d 則樣本），差 2、3 個百分點的兩家其實分不出高下。" % n_all)

    st.divider()
    st.subheader("各品牌出現時，AI 引用了哪些來源")
    st.caption("在提到該品牌的那些回答裡，被引用最多次的網域。"
               "競品常被引用、我們沒有的來源，就是可以去經營的對象（詳見「來源分析」頁）。")
    ent = st.selectbox("選一個品牌", b["entity"].tolist(), key="cmp_entity")
    src = q("""
        SELECT c.domain, c.source_type, COUNT(*) AS 次數
        FROM citations c
        WHERE c.run_id IN (SELECT m.run_id FROM mentions m
                           JOIN runs r ON r.id = m.run_id
                           WHERE m.entity = ? %s)
        GROUP BY c.domain, c.source_type
        ORDER BY 次數 DESC
    """ % source_sql("r"), (ent,))
    if src.empty:
        st.info("提到「%s」的回答裡，目前還沒有任何引用來源被記錄下來。" % ent)
    else:
        st.dataframe(src.rename(columns={"domain": "網域", "source_type": "類型"}),
                     hide_index=True, width="stretch")

    demo_footer()


# ═══════════════════════════════════════════════════════════════════════
# 第 5 頁：來源分析
# ═══════════════════════════════════════════════════════════════════════

def page_sources():
    page_header("來源分析",
                "AI 的答案是從別人的內容拼出來的。這一頁看它到底引用了誰，"
                "以及哪些來源「引用了競品但沒引用我們」。")

    cits = q("SELECT c.* FROM citations c JOIN runs r ON r.id = c.run_id "
             "WHERE 1=1 %s" % source_sql("r"))
    if cits.empty:
        st.info("還沒有引用資料。")
        demo_footer()
        return

    # 每個網域：被引用幾次、平均順位、在提到我們／競品的回答中各出現幾次
    dom = q("""
        SELECT c.domain,
               COUNT(*) AS 引用次數,
               AVG(c.rank) AS 平均順位,
               COUNT(DISTINCT c.run_id) AS 出現回答數,
               SUM(CASE WHEN EXISTS (SELECT 1 FROM mentions m
                        WHERE m.run_id=c.run_id AND m.is_us=1) THEN 1 ELSE 0 END) AS 提到我們,
               SUM(CASE WHEN EXISTS (SELECT 1 FROM mentions m
                        WHERE m.run_id=c.run_id AND m.is_us=0) THEN 1 ELSE 0 END) AS 提到競品
        FROM citations c JOIN runs r ON r.id = c.run_id
        WHERE 1=1 %s
        GROUP BY c.domain ORDER BY 引用次數 DESC
    """ % source_sql("r"))
    meta = q("SELECT domain, source_type, is_ours, first_seen, notes FROM domains")
    dom = dom.merge(meta, on="domain", how="left")
    dom["source_type"] = dom["source_type"].fillna("未分類")
    dom["is_ours"] = dom["is_ours"].fillna(0).astype(int)

    def opportunity(r):
        """機會等級：引用了競品但沒引用我們 = 高機會（該去經營的來源）"""
        if r["is_ours"]:
            return "自有"
        if r["提到競品"] > 0 and r["提到我們"] == 0:
            return "🔴 高"
        if r["提到競品"] > r["提到我們"]:
            return "🟡 中"
        return "🟢 低"

    dom["機會等級"] = dom.apply(opportunity, axis=1)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("被引用的網域數", "%d 個" % len(dom))
    c2.metric("總引用次數", "%d 次" % int(dom["引用次數"].sum()))
    hi = int((dom["機會等級"] == "🔴 高").sum())
    c3.metric("高機會來源", "%d 個" % hi, help="引用了競品但完全沒引用我們的網域")
    ours_cnt = int(dom[dom["is_ours"] == 1]["引用次數"].sum())
    c4.metric("引用到我們自己的次數", "%d 次" % ours_cnt,
              help="AI 直接引用 fluv.com 的次數。這個數字是「內容有沒有被模型讀進去」最直接的證據。")

    st.divider()
    cA, cB = st.columns([1.5, 1])

    with cA:
        st.subheader("來源影響力排名")
        st.caption("被引用越多次、平均順位越前面的來源，影響力越大。")
        top = dom.head(12).sort_values("引用次數")
        colors = [C_US if o else (C_ALERT if lv == "🔴 高" else C_OTHER)
                  for o, lv in zip(top["is_ours"], top["機會等級"])]
        f = go.Figure(go.Bar(x=top["引用次數"], y=top["domain"], orientation="h",
                             marker_color=colors,
                             hovertemplate="%{y}：被引用 %{x} 次<extra></extra>"))
        f.update_layout(height=420, margin=dict(t=10, b=10, l=10, r=10),
                        xaxis=dict(title="被引用次數"))
        st.plotly_chart(f)
        st.caption("薄荷藍＝我們自己的網域，珊瑚紅＝高機會來源，灰色＝其他。")

    with cB:
        st.subheader("來源類型分布")
        st.caption("AI 比較信任哪一類內容。")
        by_type = dom.groupby("source_type")["引用次數"].sum().reset_index()
        f = go.Figure(go.Pie(labels=by_type["source_type"], values=by_type["引用次數"],
                             hole=0.45, textinfo="label+percent",
                             marker=dict(colors=C_CAT),
                             hovertemplate="%{label}：%{value} 次<extra></extra>"))
        f.update_layout(height=420, margin=dict(t=10, b=10, l=10, r=10), showlegend=False)
        st.plotly_chart(f)
        st.caption("固定分類：" + " / ".join(SOURCE_TYPES))

    st.divider()
    st.subheader("來源清單")
    st.caption("**機會等級**：🔴 高 ＝ 這個來源引用了競品、但一次都沒引用我們 → "
               "最值得去投稿、合作或經營的對象。")
    lv = st.multiselect("篩選機會等級", ["🔴 高", "🟡 中", "🟢 低", "自有"],
                        default=["🔴 高", "🟡 中", "🟢 低", "自有"])
    view = dom[dom["機會等級"].isin(lv)]
    st.dataframe(
        view[["domain", "source_type", "機會等級", "引用次數", "平均順位",
              "提到我們", "提到競品", "first_seen"]].rename(columns={
                  "domain": "網域", "source_type": "類型", "first_seen": "首次出現"}),
        hide_index=True, width="stretch",
        column_config={"平均順位": st.column_config.NumberColumn(format="%.1f")})
    st.caption("「提到我們／提到競品」是指：這個網域被引用的那些回答裡，"
               "有幾則同時提到了我們／競品。")

    demo_footer()


# ═══════════════════════════════════════════════════════════════════════
# 第 6 頁：機會建議
# ═══════════════════════════════════════════════════════════════════════

# 四個渠道的說明與建議做法
CHANNEL_CARDS = {
    "owned": ("🏠 官網內容",
              "自己網站上的頁面。最容易控制、最容易被模型抓到，"
              "但要寫成「一個標題 = 一個明確問題、答案放段落開頭」才引用得到。"),
    "social": ("💬 社群",
               "Threads／IG／FB。模型很常引用社群貼文，"
               "尤其是有具體經驗描述的那種，不是宣傳文案。"),
    "pr": ("📰 PR 媒體",
           "媒體報導。權重最高但最慢，適合用在「信任查證」這類需要第三方背書的題目。"),
    "ugc": ("👥 UGC",
            "Dcard／PTT／Mobile01 上真實使用者的討論。"
            "不能買、不能自導自演，只能透過把服務做好去換。"),
}


def page_gaps():
    page_header("機會建議",
                "把「我們缺席、競品在場」的題目整理成一個個缺口，依機會分數排序決定先做哪個。")

    gaps = q("SELECT * FROM gaps ORDER BY opportunity_score DESC")
    if gaps.empty:
        st.info("目前沒有缺口紀錄。執行 python scripts/gaps.py 會產生（階段 4）。")
        demo_footer()
        return

    st.subheader("缺口清單")
    st.caption("機會分數 = (受影響題數 × 意圖權重 × 缺席程度) ÷ 補齊難度。"
               "**這是排序用的，不是評分** —— 4.6 分不代表「品質 4.6」，"
               "只代表它應該排在 3.8 分那個前面。")

    stat_filter = st.multiselect("狀態", GAP_STATUSES, default=GAP_STATUSES)
    view = gaps[gaps["status"].isin(stat_filter)]
    if view.empty:
        st.warning("這組篩選下沒有缺口。")
        demo_footer()
        return

    badge = {"open": "🔵 待處理", "in_progress": "🟡 進行中",
             "done": "🟢 已完成", "holdout": "⚪ 對照組"}
    show = pd.DataFrame({
        "id": view["id"],
        "缺口": view["title"],
        "主題": view["topic"],
        "機會分數": view["opportunity_score"],
        "狀態": view["status"].map(badge),
        "受影響題目": view["affected_prompt_ids"],
        "開立日": view["opened_at"],
    })
    sel = st.dataframe(show, hide_index=True, width="stretch",
                       on_select="rerun", selection_mode="single-row", key="gap_table",
                       column_config={
                           "id": st.column_config.NumberColumn("#", width="small"),
                           "機會分數": st.column_config.NumberColumn(format="%.2f")})
    rows = sel.selection["rows"] if hasattr(sel, "selection") else []
    gid = int(show.iloc[rows[0]]["id"]) if rows else int(show.iloc[0]["id"])
    g = gaps[gaps["id"] == gid].iloc[0]

    st.info("**holdout（對照組）是正式狀態，不是備註。** "
            "它代表「刻意不處理」的缺口，用途是日後比較「有行動」與「刻意不行動」"
            "兩組的變化幅度 —— 藉此區分數字變好是我們的功勞，還是模型自己在漂移。")

    st.divider()
    st.subheader("缺口詳情：%s" % g["title"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("機會分數", "%.2f" % g["opportunity_score"])
    c2.metric("狀態", badge.get(g["status"], g["status"]))
    c3.metric("主題", g["topic"] or "—")
    c4.metric("開立於", g["opened_at"] or "—")

    st.markdown("**為什麼這是缺口**")
    st.write(g["why"])

    # 受影響的題目與它們現在的表現
    ids = [int(x) for x in str(g["affected_prompt_ids"] or "").split(",") if x.strip().isdigit()]
    affected = pd.DataFrame()
    if ids:
        ph = ",".join("?" * len(ids))
        affected = q("""
            SELECT p.id, p.text AS 題目, p.topic AS 主題, p.intent AS 意圖,
                   p.intent_weight AS 權重,
                   COUNT(DISTINCT r.id) AS 樣本,
                   COUNT(DISTINCT CASE WHEN m.is_us=1 THEN r.id END) AS 有提到我們
            FROM prompts p
            LEFT JOIN runs r     ON r.prompt_id = p.id
            LEFT JOIN mentions m ON m.run_id = r.id
            WHERE p.id IN (%s) GROUP BY p.id""" % ph, tuple(ids))
        # 同樣 ×100，表格才會顯示成百分比
        affected["能見度"] = affected.apply(
            lambda r: round(r["有提到我們"] / r["樣本"] * 100, 1) if r["樣本"] else 0, axis=1)
        st.markdown("**受影響的題目**")
        st.dataframe(affected, hide_index=True, width="stretch", column_config={
            "id": st.column_config.NumberColumn("#", width="small"),
            "能見度": st.column_config.ProgressColumn(format="%.0f%%",
                                                    min_value=0, max_value=100)})

    st.divider()
    st.subheader("四張渠道行動卡")
    st.caption("同一個缺口，四種補法。已經做過的行動會列在卡片裡，"
               "並且會在總覽頁的趨勢圖上畫成垂直虛線。")

    acts = q("SELECT * FROM actions WHERE gap_id=? ORDER BY published_at", (int(gid),))
    cols = st.columns(4)
    for col, ch in zip(cols, ACTION_CHANNELS):
        title, desc = CHANNEL_CARDS[ch]
        done = acts[acts["channel"] == ch]
        body = "".join(
            "<br>✅ <b>%s</b><br><span style='font-size:12px;color:#898989'>%s</span>"
            % (html.escape(str(a["title"])), a["published_at"]) for _, a in done.iterrows())
        if not body:
            body = "<br><span style='font-size:12px;color:#9AA5AD'>尚未有行動</span>"
        col.markdown(
            '<div class="card"><h4>%s</h4><p>%s</p>%s</div>' % (title, desc, body),
            unsafe_allow_html=True)

    st.write("")
    st.subheader("預期成效（粗估）")
    if not affected.empty and affected["樣本"].sum():
        now_v = affected["有提到我們"].sum() / affected["樣本"].sum()
        # 目標值：這些題目上，最強競品目前的水準
        ph = ",".join("?" * len(ids))
        best = q("""
            SELECT m.entity, COUNT(DISTINCT m.run_id) AS hits
            FROM mentions m JOIN runs r ON r.id=m.run_id
            WHERE m.is_us=0 AND r.prompt_id IN (%s)
            GROUP BY m.entity ORDER BY hits DESC LIMIT 1""" % ph, tuple(ids))
        n_aff = int(affected["樣本"].sum())
        target = (int(best.at[0, "hits"]) / n_aff) if not best.empty else now_v
        share = n_aff / max(1, total_runs())
        uplift = max(0.0, target - now_v) * share
        m1, m2, m3 = st.columns(3)
        m1.metric("這些題目目前的能見度", "%.1f%%" % (now_v * 100),
                  help="%d / %d 則" % (int(affected["有提到我們"].sum()), n_aff))
        m2.metric("補齊後的目標水準", "%.1f%%" % (target * 100),
                  help="用最強競品在這些題目上的表現當作可達到的水準"
                       + ("（%s）" % best.at[0, "entity"] if not best.empty else ""))
        m3.metric("整體能見度可望增加", "%+.1f pp" % (uplift * 100),
                  help="把上面的差距按這些題目佔全部樣本的比重（%.0f%%）折算" % (share * 100))
        st.caption("怎麼算的：(目標 %.3f − 目前 %.3f) × 這些題目佔全部樣本的比重 %.3f "
                   "= %.3f，也就是 %+.1f 個百分點。\n\n"
                   "⚠️ 這是粗估，不是預測。它假設「補齊內容之後能追上最強競品」，"
                   "而這件事不保證會發生。真正的驗證方式是行動後看趨勢圖，"
                   "而且要等到變化超出信賴區間才算數。"
                   % (target, now_v, share, uplift, uplift * 100))
    else:
        st.caption("這個缺口沒有對應到具體題目，無法估算。")

    st.divider()
    cL, cR = st.columns(2)

    with cL:
        st.subheader("文章草稿")
        drafts = q("SELECT * FROM drafts WHERE gap_id=? ORDER BY id DESC", (int(gid),))
        if drafts.empty:
            st.caption("這個缺口還沒有草稿。")
        else:
            for _, d in drafts.iterrows():
                with st.expander("📄 %s　（%s）" % (d["title"], d["status"])):
                    st.markdown("**大綱**")
                    st.text(d["outline"])
                    st.markdown("**內文**")
                    st.text(d["body"])
                    st.caption("草稿永遠不會自動發佈。人審過、實際發佈之後，"
                               "再到下面登記一筆行動。")
        st.caption("第 5 步。要不要寫、什麼時候寫，由你決定 —— "
                   "所以它不在「跑一輪」那條龍裡面。")
        d1, d2 = st.columns(2)

        if d1.button("✍️ 產生骨架", key="mk_draft", width="stretch",
                     help="不呼叫 AI、不花錢。產出大綱與段落骨架，"
                          "並附上從已存的 AI 回答裡撈到的可用事實。"):
            with st.spinner("產生中…"):
                code, _ = _run_and_stream(_script_cmd("draft.py", ["--gap", str(gid)]))
            q.clear()
            if code == 0:
                st.success("骨架產生完成，重新整理這一頁就會出現在上面的草稿清單裡。")
            else:
                st.error("產生失敗（結束代碼 %s）。上面的訊息會說明原因。" % code)

        if d2.button("🤖 讓 AI 寫成文章", key="mk_draft_ai", type="primary",
                     width="stretch",
                     help="會呼叫 AI，一篇約 NT$3~8。只能用骨架裡列出的事實，"
                          "寫完會逐一檢查有沒有編造數字。"):
            with st.spinner("AI 撰寫中…　一篇大約一分鐘。"):
                code, out = _run_and_stream(
                    _script_cmd("draft.py", ["--gap", str(gid), "--write"]))
            q.clear()
            if code == 0:
                st.success("寫完了，重新整理這一頁就會看到。")
            else:
                st.error("撰寫失敗（結束代碼 %s）。上面的訊息會說明原因。" % code)
            if "沒有出處" in out or "編造" in out:
                st.warning("⚠️ 檢查到文章裡有來源不明的數字。"
                           "那是 AI 自己編的，發佈前一定要逐一核對。")

        st.caption("⚠️ 產出一律是 `status=draft`，**永遠不自動發佈**。"
                   "每一個數字都要跟官網條款核對過才能上線。")

    with cR:
        st.subheader("登記一筆行動")
        st.caption("發完文、聯絡完媒體之後在這裡登記。登記的日期會變成趨勢圖上的垂直標記線。")
        with st.form("action_form"):
            st.text_input("標題", placeholder="例：上線〈寵物保姆身分驗證說明〉", disabled=True)
            st.selectbox("渠道", ACTION_CHANNELS, disabled=True)
            st.text_input("網址", placeholder="https://", disabled=True)
            st.date_input("發佈日期", value=date.today(), disabled=True)
            st.text_area("備註", disabled=True, height=80)
            st.form_submit_button("登記（階段 4 開放）", disabled=True)
        st.caption("階段 0 先把版面排好。寫入功能跟 `gaps.py` 一起在階段 4 開放，"
                   "現在畫面上看到的行動是示範資料。")

    demo_footer()


# ═══════════════════════════════════════════════════════════════════════
# 第 7 頁：設定
# ═══════════════════════════════════════════════════════════════════════

CSV_TEMPLATE = (
    "text,topic,intent,intent_weight,locale,active\n"
    "台灣有哪些寵物保姆媒合平台？,平台選擇,discovery,1.0,tw,1\n"
    "寵物保姆一天大概多少錢？,價格,price,1.1,tw,1\n"
    "怎麼確認寵物保姆是可信的？,信任查證,trust,1.5,tw,1\n"
)


def _clean(v, default=""):
    """把 pandas 讀進來的值轉成乾淨的字串。

    ⚠️ 這個函式存在的原因：pandas 的空值是 NaN，而 NaN 在 Python 裡是「真」的，
      所以 `str(row["text"] or "")` 遇到空欄位會得到字串 "nan"，
      結果就是「空白的那一列被當成一題叫 nan 的題目匯進去」。
      所有從 DataFrame 讀出來的欄位都要先過這裡。
    """
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s if s else default


def save_prompts(edited, original):
    """把畫面上編輯過的題庫寫回資料庫。

    三件事：新增（沒有 id 的列）、修改（欄位有變的列）、刪除（被整列移掉的）。
    ⚠️ 已經有跑批紀錄的題目不能刪 —— 刪了歷史數字會對不起來。
      這種情況請改成「停用」（把「啟用」的勾拿掉），它就不會再被跑批，
      但過去的資料還在。
    """
    conn = connect(DEFAULT_DB_PATH)
    added = updated = removed = 0
    errors = []
    try:
        cur = conn.cursor()
        edited_ids = {int(v) for v in edited["id"].dropna().tolist()}

        # --- 刪除 ---
        for pid in set(original["id"].tolist()) - edited_ids:
            n = cur.execute("SELECT COUNT(*) FROM runs WHERE prompt_id=?",
                            (int(pid),)).fetchone()[0]
            if n:
                errors.append("第 %d 題已經有 %d 筆跑批紀錄，不能刪除。"
                              "請改成「停用」（把「啟用」的勾拿掉），歷史資料才不會斷掉。"
                              % (pid, n))
                continue
            cur.execute("DELETE FROM prompts WHERE id=?", (int(pid),))
            removed += 1

        # --- 新增與修改 ---
        orig_by_id = {int(r["id"]): r for _, r in original.iterrows()}
        for i, row in edited.iterrows():
            text = _clean(row.get("text"))
            if not text:
                continue
            intent = _clean(row.get("intent"), "discovery")
            if intent not in INTENTS:
                errors.append("「%s」的意圖「%s」不是合法值，必須是：%s"
                              % (text[:16], intent, " / ".join(INTENTS)))
                continue
            topic = _clean(row.get("topic"), "未分類")
            locale = _clean(row.get("locale"), "tw")
            active = 1 if bool(row.get("active", True)) else 0
            w = row.get("intent_weight")
            weight = float(w) if pd.notna(w) and w else INTENT_WEIGHTS[intent]

            pid = row.get("id")
            if pd.isna(pid):
                cur.execute(
                    "INSERT INTO prompts (text, topic, intent, intent_weight, locale, "
                    "active, source, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (text, topic, intent, weight, locale, active, "manual", now_iso()))
                added += 1
            else:
                o = orig_by_id.get(int(pid))
                if o is None:
                    continue
                same = (o["text"] == text and o["topic"] == topic
                        and o["intent"] == intent and float(o["intent_weight"]) == weight
                        and o["locale"] == locale and int(o["active"]) == active)
                if same:
                    continue
                cur.execute(
                    "UPDATE prompts SET text=?, topic=?, intent=?, intent_weight=?, "
                    "locale=?, active=? WHERE id=?",
                    (text, topic, intent, weight, locale, active, int(pid)))
                updated += 1
        conn.commit()
    finally:
        conn.close()
    q.clear()
    return added, updated, removed, errors


def import_csv(df):
    """把上傳的 CSV 匯入題庫。回傳 (成功筆數, 錯誤訊息清單)"""
    conn = connect(DEFAULT_DB_PATH)
    ok, errors = 0, []
    try:
        cur = conn.cursor()
        existing = {r[0] for r in cur.execute("SELECT text FROM prompts")}
        for i, row in df.iterrows():
            line = i + 2  # +2 是因為第 1 行是標題列，而且人是從 1 開始數的
            text = _clean(row.get("text"))
            if not text:
                errors.append("第 %d 行：題目是空的，已跳過。" % line)
                continue
            if text in existing:
                errors.append("第 %d 行：「%s」題庫裡已經有了，已跳過。" % (line, text[:20]))
                continue
            intent = _clean(row.get("intent"), "discovery")
            if intent not in INTENTS:
                errors.append("第 %d 行：意圖「%s」不合法，必須是 %s，已跳過。"
                              % (line, intent, " / ".join(INTENTS)))
                continue
            topic = _clean(row.get("topic"), "未分類")
            locale = _clean(row.get("locale"), "tw")
            try:
                active = int(row.get("active", 1))
            except (TypeError, ValueError):
                active = 1
            try:
                weight = float(row["intent_weight"]) if pd.notna(
                    row.get("intent_weight")) else INTENT_WEIGHTS[intent]
            except (TypeError, ValueError, KeyError):
                weight = INTENT_WEIGHTS[intent]
            cur.execute(
                "INSERT INTO prompts (text, topic, intent, intent_weight, locale, "
                "active, source, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (text, topic, intent, weight, locale, active, "csv", now_iso()))
            existing.add(text)
            ok += 1
        conn.commit()
    finally:
        conn.close()
    q.clear()
    return ok, errors


# ═══════════════════════════════════════════════════════════════════════
# 跑批按鈕
#
# 為什麼要有這個：跑批本來只能在終端機下指令。做成按鈕之後，
# 不用開終端機、不用記參數，按一下就好。
#
# 三道安全措施（因為這顆按鈕會花錢）：
#   1. 按之前一定先看到預估花費
#   2. 要勾「我知道這會花錢」才會亮起來
#   3. 同一時間只准跑一個（用鎖檔擋，避免兩個分頁同時按下去花兩次錢）
# ═══════════════════════════════════════════════════════════════════════

# 鎖檔位置。data/ 不進版控，放這裡剛好。
LOCK_PATH = os.path.join(ROOT_DIR, "data", ".collect.lock")


def _collect_cmd(engines, extra=None):
    """組出要執行的 collect.py 指令。

    用 sys.executable 而不是寫死 "python"，
    確保用的是跑這個儀表板的同一個 Python（虛擬環境才不會跑錯）。
    -u 是不要緩衝輸出，這樣畫面上才看得到即時進度。
    """
    cmd = [sys.executable, "-u", os.path.join(ROOT_DIR, "scripts", "collect.py")]
    for e in engines:
        cmd += ["--engine", e]
    return cmd + (extra or [])


def _script_cmd(script, extra=None):
    """組出要執行的某一支腳本的指令。

    跟 _collect_cmd 一樣用 sys.executable，確保跑的是同一個虛擬環境。
    """
    cmd = [sys.executable, "-u", os.path.join(ROOT_DIR, "scripts", script)]
    return cmd + (extra or [])


def _run_and_stream(cmd, max_lines=400):
    """執行指令，把輸出即時印在畫面上。回傳 (結束代碼, 全部輸出)。"""
    lines = []
    box = st.empty()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, cwd=ROOT_DIR)
    for line in proc.stdout:
        line = line.rstrip()
        # 濾掉 Python 的版本警告，那些跟跑批無關，只會洗版
        if "Warning" in line or "warnings.warn" in line or line.startswith("  warnings"):
            continue
        lines.append(line)
        box.code("\n".join(lines[-max_lines:]), language=None)
    proc.wait()
    return proc.returncode, "\n".join(lines)


def run_batch_tab():
    """「跑一輪」的實際內容。"""
    import collect as collect_mod
    from common import ENGINE_KEY_NAMES, iso_year_week, load_env

    st.caption("跑一輪 = 每一題、每一個選到的引擎，各問一次。"
               "要累積幾次就跑幾次 —— 編號會自動接續，舊的原始回答永遠不會被蓋掉。")
    st.caption("⚠️ 建議分不同天跑。同一天連跑好幾次的話，那幾次共享同一個時間點的"
               "搜尋環境，信賴區間會假性變窄（看起來「很穩定」，其實是同一個瞬間量了很多遍）。")

    load_env()
    n_prompts = int(q("SELECT COUNT(*) AS n FROM prompts WHERE active=1 AND locale='tw'")
                    .at[0, "n"])
    if n_prompts == 0:
        st.warning("題庫裡沒有啟用中的題目，沒有東西可以問。請先到「題庫管理」新增。")
        return

    # ---- 哪幾家可以跑（有金鑰的才給選）----
    ready, missing = [], []
    for e in collect_mod.ENGINES:
        key = os.environ.get(ENGINE_KEY_NAMES.get(e, ""), "").strip()
        (ready if key else missing).append(e)

    if missing:
        st.info("還沒有金鑰、所以不能選的引擎：**%s**　"
                "（要啟用的話，把金鑰填進專案根目錄的 `.env`）" % "、".join(missing))
    if not ready:
        st.error("`.env` 裡一把金鑰都沒有，沒辦法跑。請先填入至少一家的 API 金鑰。")
        return

    default = [e for e in collect_mod.DEFAULT_ENGINES if e in ready] or ready[:1]
    engines = st.multiselect("要問哪幾家", ready, default=default, key="rb_engines",
                             format_func=lambda e: "%s（%s）" % (e, collect_mod.MODELS[e]))
    if not engines:
        st.warning("至少要選一家。")
        return

    st.caption("這一輪會呼叫 **%d 題 × %d 家 = %d 次** API。"
               % (n_prompts, len(engines), n_prompts * len(engines)))

    max_cost = st.number_input(
        "花費上限（美金）", min_value=0.5, max_value=500.0, value=30.0, step=5.0,
        key="rb_maxcost",
        help="兩道煞車：開跑前預估超過就完全不跑；跑到一半累計達到上限也會停。"
             "已經跑完的都會保留，再按一次可以接著跑。")

    # ---- 先看預估花費 ----
    st.divider()
    if st.button("① 先試算花費（不會呼叫 API、不花錢）", key="rb_dry"):
        code, out = _run_and_stream(
            _collect_cmd(engines, ["--dry-run", "--max-cost", str(max_cost)]))
        st.session_state["rb_saw_estimate"] = True

    # ---- 真的跑 ----
    st.divider()
    locked = os.path.exists(LOCK_PATH)
    if locked:
        st.warning("偵測到已經有一輪正在跑（或上次跑到一半沒有正常結束）。"
                   "確定沒有在跑的話，按下面的按鈕解除。")
        if st.button("解除鎖定", key="rb_unlock"):
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass
            st.rerun()
        return

    st.markdown("**按下去會依序跑完這四步：**")
    st.caption("1️⃣ 問 AI（collect）→ 2️⃣ 判讀每則回答（parse）→ "
               "3️⃣ 算指標（aggregate）→ 4️⃣ 找缺口（gaps）")
    st.caption("第 5 步「寫草稿」不在這裡 —— 那要由你看過缺口之後決定要不要寫，"
               "所以放在「機會建議」頁，一個缺口一個按鈕。")

    agree = st.checkbox("我知道按下去會真的呼叫 API、會產生費用"
                        "（問 AI 和判讀都會花錢，判讀便宜很多）", key="rb_agree")
    go = st.button("② 開始跑一輪", type="primary", disabled=not agree, key="rb_go")

    if go:
        # 上鎖，避免兩個分頁同時按下去花兩次錢
        try:
            with open(LOCK_PATH, "w", encoding="utf-8") as f:
                f.write(now_iso())
        except OSError as e:
            st.error("建立鎖檔失敗：%s" % e)
            return

        # 第 4 步要指定週次 —— gaps.py 預設挑「樣本最多的那一週」，
        # 不指定的話會一直盤點舊資料，剛收完的這一批反而用不到。
        year, wk = iso_year_week()
        this_week = "%d-W%02d" % (year, wk)

        steps = [
            ("1/4　問 AI",   _collect_cmd(engines, ["--max-cost", str(max_cost)]),
             "去各家 AI 問題庫裡的每一題，把原始回答存下來。"),
            ("2/4　判讀",     _script_cmd("parse.py", ["--week", this_week]),
             "逐則判讀提到哪些品牌、排第幾、口氣如何、引用了哪些來源。"),
            ("3/4　算指標",   _script_cmd("aggregate.py", ["--week", this_week]),
             "算這一週的能見度、引用率、聲量佔比與信賴區間。"),
            ("4/4　找缺口",   _script_cmd("gaps.py", ["--week", this_week]),
             "找出「我們缺席、競品在場」的題目，排出機會分數。"),
        ]

        code, out, failed_at = 0, "", None
        try:
            for name, cmd, desc in steps:
                st.markdown("**步驟 %s**" % name)
                st.caption(desc)
                with st.spinner("執行中…　過程中不要關掉這個分頁。"):
                    code, step_out = _run_and_stream(cmd)
                out += "\n" + step_out
                if code != 0:
                    failed_at = name
                    break
                st.success("步驟 %s 完成" % name)
        finally:
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass

        q.clear()   # 清掉查詢快取，畫面才會看到新資料
        if failed_at is None:
            st.success("四步都跑完了。接下來：到「總覽」看這一週的數字，"
                       "到「機會建議」看找出哪些缺口、決定要不要寫草稿。")
        else:
            st.error("跑到「步驟 %s」出錯（結束代碼 %s），後面的步驟沒有執行。"
                     "上面的訊息會說明原因。\n\n"
                     "**前面已經完成的步驟不會白跑** —— 原始回答和判讀結果都存好了，"
                     "修掉問題之後再按一次會接著跑，不會重複收費。"
                     % (failed_at, code))

        if "沒有做任何網路搜尋" in out or "疑似沒開" in out:
            st.warning("⚠️ 有回答完全沒有做網路搜尋。"
                       "那代表 AI 回的是它的記憶而不是查來的資料 —— "
                       "數字會看起來正常但沒有意義。請先確認該家的搜尋工具有開。")
        if "完全一模一樣" in out:
            st.warning("⚠️ 有題目的多次回答完全相同。"
                       "這會讓信賴區間假性變窄，統計失去意義。")


def page_settings():
    page_header("設定", "題庫、題目建議、競品名單、採樣設定。這一頁是唯一會寫入資料的地方。")

    t1, t2, t3, t4 = st.tabs(["📋 題庫管理", "💡 題目建議佇列", "🏢 競品名單",
                              "⚙️ 採樣設定"])

    # ---- 題庫管理 --------------------------------------------------------
    with t1:
        st.subheader("直接在畫面上編輯題庫")
        st.caption("可以改文字、改主題、改意圖，也可以在最下面那一列直接新增題目。"
                   "改完按「儲存變更」。")

        prompts = q("SELECT id, text, topic, intent, intent_weight, locale, active, "
                    "source, created_at FROM prompts ORDER BY id")
        editor_df = prompts[["id", "text", "topic", "intent", "intent_weight",
                             "locale", "active"]].copy()
        editor_df["active"] = editor_df["active"].astype(bool)

        edited = st.data_editor(
            editor_df, num_rows="dynamic", width="stretch", height=420,
            key="prompt_editor",
            column_config={
                "id": st.column_config.NumberColumn("#", disabled=True, width="small",
                                                    help="系統自動編號，新增時留空"),
                "text": st.column_config.TextColumn("題目", width="large", required=True),
                "topic": st.column_config.TextColumn("主題", width="medium"),
                "intent": st.column_config.SelectboxColumn("意圖", options=INTENTS,
                                                           width="medium"),
                "intent_weight": st.column_config.NumberColumn(
                    "意圖權重", min_value=0.1, max_value=3.0, step=0.1, format="%.1f",
                    help="留空會自動帶入該意圖的預設權重"),
                "locale": st.column_config.TextColumn("語系", width="small"),
                "active": st.column_config.CheckboxColumn(
                    "啟用", help="取消勾選＝停用。停用的題目不會再被跑批，但歷史資料保留。"),
            })

        c1, c2 = st.columns([1, 4])
        if c1.button("💾 儲存變更", type="primary"):
            added, updated, removed, errors = save_prompts(edited, prompts)
            if added or updated or removed:
                st.success("已儲存：新增 %d 題、修改 %d 題、刪除 %d 題。"
                           % (added, updated, removed))
            elif not errors:
                st.info("沒有偵測到任何變更。")
            for e in errors:
                st.warning(e)
            if added or updated or removed:
                # 清掉表格編輯器的暫存狀態。不清的話，剛剛新增的那一列還留在編輯器裡，
                # 下次再按一次「儲存變更」就會被重複寫進去一次。
                st.session_state.pop("prompt_editor", None)
                st.rerun()
        c2.caption("意圖權重的預設值：" +
                   "　".join("%s=%.1f" % (k, v) for k, v in INTENT_WEIGHTS.items()))

        st.divider()
        st.subheader("上傳 CSV 匯入題庫")
        st.caption("欄位：`text`（必填）, `topic`, `intent`, `intent_weight`, `locale`, `active`。"
                   "跟現有題目文字重複的會自動跳過。")
        st.download_button("⬇️ 下載 CSV 範本", CSV_TEMPLATE.encode("utf-8-sig"),
                           file_name="prompts_template.csv", mime="text/csv")

        up = st.file_uploader("選擇 CSV 檔", type=["csv"], key="csv_up")
        if up is not None:
            try:
                raw = up.getvalue()
                try:
                    df = pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig")
                except UnicodeDecodeError:
                    # Excel 存出來的中文 CSV 常常是 Big5
                    df = pd.read_csv(io.BytesIO(raw), encoding="big5")
            except Exception as e:  # noqa: BLE001 - 要給使用者看得懂的訊息
                st.error("這個 CSV 讀不開：%s\n\n"
                         "請確認它是逗號分隔、第一行是欄位名稱的純文字檔。" % e)
                df = None
            if df is not None:
                if "text" not in df.columns:
                    st.error("CSV 裡找不到 `text` 欄位。第一行必須是欄位名稱，"
                             "而且至少要有一欄叫 `text`。目前讀到的欄位：%s"
                             % ", ".join(map(str, df.columns)))
                else:
                    st.write("預覽（共 %d 列）：" % len(df))
                    st.dataframe(df.head(10), hide_index=True, width="stretch")
                    if st.button("📥 確認匯入", type="primary"):
                        ok, errors = import_csv(df)
                        st.success("成功匯入 %d 題。" % ok)
                        for e in errors[:20]:
                            st.warning(e)
                        if len(errors) > 20:
                            st.caption("還有 %d 筆訊息未顯示。" % (len(errors) - 20))
                        if ok:
                            st.rerun()

    # ---- 題目建議佇列 ----------------------------------------------------
    with t2:
        st.subheader("題目建議佇列")
        st.caption("`suggest.py` 會從 Google Search Console、站內搜尋、以及 AI 回答裡的新概念"
                   "提議新題目。**系統只提議，加不加由你決定。**")

        pend = q("SELECT * FROM prompt_suggestions WHERE status='pending' "
                 "ORDER BY est_importance DESC")
        if pend.empty:
            st.info("目前沒有待處理的建議。")
        for _, s in pend.iterrows():
            with st.container(border=True):
                cA, cB, cC = st.columns([6, 1, 1])
                cA.markdown("**%s**" % s["text"])
                cA.caption("來源：%s · 預估重要性 %.2f\n\n證據：%s"
                           % (s["source"], s["est_importance"] or 0, s["evidence"]))
                if cB.button("加入題庫", key="acc%d" % s["id"], type="primary"):
                    write("INSERT INTO prompts (text, topic, intent, intent_weight, "
                          "locale, active, source, created_at) VALUES (?,?,?,?,?,?,?,?)",
                          (s["text"], "未分類", "discovery",
                           INTENT_WEIGHTS["discovery"], "tw", 1, "suggestion", now_iso()))
                    write("UPDATE prompt_suggestions SET status='accepted' WHERE id=?",
                          (int(s["id"]),))
                    st.success("已加入題庫。請到「題庫管理」把主題和意圖補上。")
                    st.rerun()
                if cC.button("忽略", key="ign%d" % s["id"]):
                    write("UPDATE prompt_suggestions SET status='ignored' WHERE id=?",
                          (int(s["id"]),))
                    st.rerun()

        done = q("SELECT text, source, status, est_importance FROM prompt_suggestions "
                 "WHERE status<>'pending' ORDER BY id DESC")
        if not done.empty:
            with st.expander("已處理過的建議（%d 筆）" % len(done)):
                st.dataframe(done.rename(columns={
                    "text": "題目", "source": "來源", "status": "處理結果",
                    "est_importance": "預估重要性"}), hide_index=True, width="stretch")

    # ---- 競品名單 --------------------------------------------------------
    with t3:
        st.subheader("競品名單")
        st.caption("這份名單不是手動維護的，是從判讀結果自動長出來的 —— "
                   "只要 AI 在回答裡提到某個品牌，它就會出現在這裡。")
        comp = q("""SELECT entity AS 品牌, is_us,
                           COUNT(*) AS 提及次數,
                           COUNT(DISTINCT run_id) AS 出現回答數,
                           AVG(position) AS 平均排序
                    FROM mentions m JOIN runs r ON r.id = m.run_id
                    WHERE 1=1 %s
                    GROUP BY entity, is_us ORDER BY 提及次數 DESC""" % source_sql("r"))
        if comp.empty:
            st.info("還沒有判讀資料。")
        else:
            comp["身分"] = comp["is_us"].map({1: "🔵 我們", 0: "⚪ 競品"})
            st.dataframe(comp[["品牌", "身分", "提及次數", "出現回答數", "平均排序"]],
                         hide_index=True, width="stretch",
                         column_config={"平均排序": st.column_config.NumberColumn(
                             format="%.2f")})
        st.caption("我們自己的品牌名稱寫在 `scripts/common.py` 的 `OUR_BRAND`，"
                   "目前是「%s」。判讀器靠它來決定 `mentions.is_us`。" % OUR_BRAND)

    # ---- 採樣設定 --------------------------------------------------------
    with t4:
        st.subheader("採樣設定")
        c = measurement_condition()
        if c is None:
            st.info("還沒有跑批紀錄。")
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("每題每引擎跑幾次 (N)", c["n"],
                      help="次數越多信賴區間越窄。N 變 4 倍，區間寬度大約減半。")
            c2.metric("引擎數", c["engines"])
            c3.metric("啟用中的題目", int(q("SELECT COUNT(*) AS n FROM prompts "
                                            "WHERE active=1").at[0, "n"]))
            c4.metric("量測條件", c["condition"])
            st.caption("上次跑批：%s ｜ 語系：%s ｜ 這一批總共 %d 則回答"
                       % (c["last_ts"], c["locale"], c["runs"]))

        st.divider()
        st.markdown("""
**這些設定目前怎麼改**

採樣設定是跑批時決定的，在 `collect.py` 的參數裡：

```
python scripts/collect.py --samples 10 --locale tw --condition api
```

改 N 之前先知道它的代價與效果：

| N | 每週 API 呼叫數（10 題 × 5 引擎） | 95% 信賴區間大約寬度 |
|---|---|---|
| 5  | 250   | ±13 個百分點 |
| 10 | 500   | ±9 個百分點 |
| 20 | 1,000 | ±6.5 個百分點 |
| 40 | 2,000 | ±4.5 個百分點 |

（以能見度 50% 為例；越接近 0% 或 100% 區間會更窄。）

**呼叫 API 時務必開啟各家的 web search 工具。**
不開的話問到的是「裸模型的記憶」，不是會查資料的助理，數字沒有意義。

**絕對不爬 AI 產品的網頁介面** —— 只用官方 API 與 SerpApi。
        """)

    demo_footer()


def page_run_batch():
    """「跑一輪」自成一頁。

    它原本是「設定」底下的第五個分頁，但那裡埋得太深 ——
    跑一輪是整個流程的第 1 步、是這個平台最主要的動作，
    不該放在「設定」裡面（設定是調東西的地方，不是做事的地方）。
    """
    page_header("跑一輪", "去問各家 AI，把回答存下來。這是整個流程的第 1 步。")
    run_batch_tab()


# ═══════════════════════════════════════════════════════════════════════
# 主程式：側邊欄七頁
# ═══════════════════════════════════════════════════════════════════════

PAGES = {
    "📊 總覽": page_overview,
    "▶️ 跑一輪": page_run_batch,
    "🔍 Prompt 監測": page_prompts,
    "🎯 受眾主題": page_topics,
    "🏁 競品比較": page_competitors,
    "🔗 來源分析": page_sources,
    "💡 機會建議": page_gaps,
    "⚙️ 設定": page_settings,
}


def main():
    st.sidebar.title("🐾 Fluv GEO 監測平台")
    st.sidebar.caption("追蹤「Fluv 毛小愛」在各家 AI 回答中的能見度")

    if not os.path.exists(DEFAULT_DB_PATH):
        st.error("找不到資料庫 `data/geo.db`。請先在終端機依序執行：\n\n"
                 "```\npython scripts/init_db.py\npython scripts/seed_demo.py\n```")
        st.stop()

    choice = st.sidebar.radio("頁面", list(PAGES.keys()), label_visibility="collapsed")
    st.sidebar.divider()

    # 資料來源篩選。預設只看真實資料 —— 把假資料當預設太危險了。
    st.sidebar.radio("資料來源", list(SOURCE_CHOICES.keys()),
                     key="data_source", index=0)

    counts = q("SELECT condition, COUNT(*) n FROM runs GROUP BY condition")
    n_real = int(counts[counts["condition"].isin(["manual", "api"])]["n"].sum()) \
        if not counts.empty else 0
    n_demo = int(counts[counts["condition"] == "demo"]["n"].sum()) \
        if not counts.empty else 0
    st.sidebar.caption("資料庫裡有 **%d 則真實回答**、%d 則示範資料。" % (n_real, n_demo))

    if (source_conditions() or (None,))[0] == "EXCLUDE":
        st.sidebar.info("**真實資料模式**\n\n"
                        "這個篩選影響的是直接讀 `runs`／`mentions`／`citations` 的頁面。\n\n"
                        "**總覽**頁讀的是 `metrics_weekly`，那是 `aggregate.py` 的產出，"
                        "它一律排除示範資料，所以永遠是真的。\n\n"
                        "資料變動後記得重跑：`python scripts/aggregate.py`")
    else:
        st.sidebar.warning("**含示範資料**\n\n"
                           "由 `scripts/seed_demo.py` 產生的亂數，"
                           "用來展示功能與版面，不是真實的品牌表現。")

    PAGES[choice]()


main()
