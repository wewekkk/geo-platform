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
import sys
from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (ACTION_CHANNELS, DEFAULT_DB_PATH, GAP_STATUSES, INTENTS,  # noqa: E402
                    INTENT_WEIGHTS, OUR_BRAND, ROOT_DIR, SOURCE_TYPES, connect,
                    now_iso)
from aggregate import wilson_interval  # noqa: E402

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
      display: flex; align-items: center; gap: 14px;
      border-radius: 5px; padding: 18px 16px; min-height: 104px;
      box-shadow: rgba(68, 102, 242, 0.05) 1px 5px 24px 0px;
  }
  .kpi-ico {
      flex: 0 0 auto; width: 46px; height: 46px; border-radius: 50%;
      background: rgba(255,255,255,.28); display: flex;
      align-items: center; justify-content: center; font-size: 21px;
  }
  .kpi-label { font-size: 13px; color: rgba(255,255,255,.92); font-weight: 600; }
  .kpi-value { font-size: 26px; color: #ffffff; font-weight: 700; line-height: 1.25; }
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
KPI_TINT = {"teal": "#13c9ca", "amber": "#ffbc58",
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
    cond = q("SELECT condition, locale FROM runs ORDER BY ts DESC LIMIT 1")
    return {
        "n": int(row.at[0, "n_samples"]),
        "condition": cond.at[0, "condition"],
        "locale": cond.at[0, "locale"],
        "last_ts": row.at[0, "last_ts"],
        "engines": int(row.at[0, "n_engines"]),
        "prompts": int(row.at[0, "n_prompts"]),
        "runs": int(row.at[0, "n_runs"]),
    }


def page_header(title, subtitle=None):
    """每一頁都從這裡開始：標題 ＋ 量測條件列 ＋ 右上角「示範資料」標記

    版面照 Fluv 後台：標題與副標在左，麵包屑在右。
    """
    import html as _html
    st.markdown('<div class="demo-badge">示範資料</div>', unsafe_allow_html=True)
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
    """每頁底部再標一次資料來源，避免有人把假數字當真"""
    st.divider()
    st.caption("⚠️ 本頁所有數字皆為 **示範資料**，由 `scripts/seed_demo.py` 產生，"
               "目的是展示功能與版面，不代表真實的品牌表現。"
               "接上真實 API 之後（階段 1）會換成實際資料。")


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
        WHERE 1=1 """ + where_extra + """
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

    t = overall_trend()
    if t.empty:
        st.info("還沒有指標資料。請先執行 python scripts/seed_demo.py")
        demo_footer()
        return

    weeks = t["week"].tolist()
    cur, prev = t.iloc[-1], t.iloc[-2] if len(t) > 1 else t.iloc[-1]

    # ---- 四個 KPI --------------------------------------------------------
    st.subheader("本週關鍵指標")
    st.caption("和上週相比。**只有當兩週的信賴區間完全沒有重疊時，才會顯示上升或下降；"
               "有重疊就是「持平」** —— 因為那個差距和隨機波動分不開。")

    n_cur, n_prev = int(cur["n_samples"]), int(prev["n_samples"])
    hits_cur, hits_prev = round(cur["visibility"] * n_cur), round(prev["visibility"] * n_prev)

    c1, c2, c3, c4 = st.columns(4)

    d, diff, why = compare_proportions(hits_cur, n_cur, hits_prev, n_prev)
    kpi(c1, "能見度", "%.1f%%" % (cur["visibility"] * 100), d,
        "%+.1f pp" % (diff * 100),
        "有提到我們的回答佔比。%d / %d 則。\n\n95%% 信賴區間 %.1f%% ~ %.1f%%\n\n%s"
        % (hits_cur, n_cur, cur["ci_low"] * 100, cur["ci_high"] * 100, why),
        tint="teal", icon="👁")

    # 好感度：在「有提到我們」的那些回答裡，口氣是正面的佔多少
    pos_cur, pos_prev = round(cur["pos_pct"] * hits_cur), round(prev["pos_pct"] * hits_prev)
    d2, diff2, why2 = compare_proportions(pos_cur, hits_cur, pos_prev, hits_prev)
    kpi(c2, "好感度", "%.1f%%" % (cur["pos_pct"] * 100), d2,
        "%+.1f pp" % (diff2 * 100),
        "被提到時口氣是正面的比例。%d / %d 則提及。\n\n%s" % (pos_cur, hits_cur, why2),
        tint="amber", icon="💬")

    # 風險提及：負面而且會直接影響信任的那些
    risk_n = int(q("""SELECT COUNT(*) AS n FROM mentions m JOIN runs r ON r.id=m.run_id
                      WHERE m.is_us=1 AND m.risk_flag=1""").at[0, "n"])
    neg_cur, neg_prev = round(cur["neg_pct"] * hits_cur), round(prev["neg_pct"] * hits_prev)
    d3, diff3, why3 = compare_proportions(neg_cur, hits_cur, neg_prev, hits_prev)
    # 風險是「越少越好」，所以方向要反過來標示
    d3_disp = {"上升": "增加", "下降": "減少", "持平": "持平"}[d3]
    kpi(c3, "風險提及", "%d 則" % risk_n, "持平" if d3 == "持平" else d3_disp,
        "%+.1f pp" % (diff3 * 100),
        "負面且屬於信任／急件類題目的提及（會直接影響成交）。\n\n"
        "負面提及比例：%.1f%%\n\n%s" % (cur["neg_pct"] * 100, why3),
        tint="coral", icon="⚠️")

    # 平均排序：不是比例，沒有 Wilson 區間。
    # 用一個保守的門檻：差距小於 0.2 名就當作持平（樣本雜訊大約就是這個量級）。
    pos_now = cur["avg_position"] or 0
    pos_was = prev["avg_position"] or 0
    dp = pos_now - pos_was
    d4 = "持平" if abs(dp) < 0.2 else ("上升" if dp < 0 else "下降")  # 排序變小 = 變前面 = 好
    kpi(c4, "平均排序", "第 %.2f 名" % pos_now, d4, "%+.2f 名" % dp,
        "我們被提到時平均排在第幾個（越小越前面）。\n\n"
        "這不是比例，沒有 Wilson 區間；改用保守門檻：差距小於 0.2 名就當作持平。",
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

    # ---- 能見度趨勢圖（含信賴區間陰影 ＋ 行動標記線）---------------------
    st.subheader("能見度趨勢")
    st.caption("淺色陰影是 95% 信賴區間 —— 線在陰影範圍內上下跳動不代表有變化。"
               "琥珀色虛線是我們採取行動的日期，用來看「線是在哪次行動之後才開始往上」。")

    x = list(range(len(weeks)))
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x + x[::-1],
                             y=(t["ci_high"] * 100).tolist() + (t["ci_low"] * 100).tolist()[::-1],
                             fill="toself", fillcolor=C_BAND, line=dict(width=0),
                             hoverinfo="skip", name="95% 信賴區間"))
    fig.add_trace(go.Scatter(x=x, y=(t["visibility"] * 100).round(1),
                             mode="lines+markers", line=dict(color=C_US, width=3),
                             name="能見度",
                             customdata=t["n_samples"],
                             hovertemplate="能見度 %{y:.1f}%<br>樣本數 %{customdata} 則<extra></extra>"))

    acts = q("SELECT * FROM actions WHERE published_at IS NOT NULL ORDER BY published_at")
    for _, a in acts.iterrows():
        xi = week_index_of(a["published_at"], weeks)
        if xi is None:
            continue
        fig.add_vline(x=xi, line_dash="dash", line_color=C_ACTION, line_width=2,
                      annotation_text="▲ " + a["title"][:12],
                      annotation_position="top",
                      annotation_font=dict(size=10, color="#8a5d00"))
    fig.update_layout(height=380, margin=dict(t=40, b=10, l=10, r=10),
                      xaxis=dict(tickmode="array", tickvals=x, ticktext=weeks),
                      yaxis=dict(title="能見度 (%)", rangemode="tozero"),
                      showlegend=False, hovermode="x unified")
    st.plotly_chart(fig)

    if not acts.empty:
        st.caption("行動標記：" + " · ".join(
            "%s（%s）" % (a["title"], a["published_at"]) for _, a in acts.iterrows()))

    st.divider()

    # ---- 情緒比例堆疊趨勢 ------------------------------------------------
    cA, cB = st.columns([3, 2])
    with cA:
        st.subheader("口氣的正／中／負比例")
        st.caption("在有提到我們的回答裡，口氣的分布怎麼變。")
        f2 = go.Figure()
        for name, col, color in [("正面", "pos_pct", C_POS), ("中性", "neu_pct", C_NEU),
                                 ("負面", "neg_pct", C_NEG)]:
            # marker_line 是刻意的：堆疊區段之間要有 2px 的底色留白，
            # 不然三個色塊直接相黏，邊界會糊掉。
            f2.add_trace(go.Bar(x=weeks, y=(t[col] * 100).round(1), name=name,
                                marker_color=color,
                                marker_line=dict(color="#ffffff", width=2),
                                hovertemplate=name + " %{y:.1f}%<extra></extra>"))
        f2.update_layout(barmode="stack", height=330, margin=dict(t=20, b=10, l=10, r=10),
                         bargap=0.28,
                         yaxis=dict(title="佔比 (%)"), legend=dict(orientation="h", y=1.12))
        st.plotly_chart(f2)

    with cB:
        st.subheader("各引擎表現")
        st.caption("同一批題目，不同 AI 的答案差很多。誤差線是 95% 信賴區間。")
        lw = latest_week()
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
    insights = build_insights(t, weeks)
    for tone, text in insights:
        {"good": st.success, "bad": st.error, "flat": st.info}[tone](text)

    demo_footer()


def build_insights(t, weeks):
    """從資料自動整理出幾句人看得懂的結論"""
    out = []
    cur, prev = t.iloc[-1], t.iloc[-2] if len(t) > 1 else t.iloc[-1]
    n_cur, n_prev = int(cur["n_samples"]), int(prev["n_samples"])
    d, diff, _ = compare_proportions(round(cur["visibility"] * n_cur), n_cur,
                                     round(prev["visibility"] * n_prev), n_prev)
    if d == "持平":
        out.append(("flat", "**整體能見度和上週持平**（%.1f%% → %.1f%%，"
                            "差距落在信賴區間內，屬於隨機波動，不需要處理）。"
                    % (prev["visibility"] * 100, cur["visibility"] * 100)))
    else:
        out.append(("good" if diff > 0 else "bad",
                    "**整體能見度%s**：%.1f%% → %.1f%%，兩週的信賴區間沒有重疊，"
                    "這是真的變化。" % (d, prev["visibility"] * 100, cur["visibility"] * 100)))

    # 八週的頭尾比較（樣本累積起來，比單週比較有力）
    first = t.iloc[0]
    n_f = int(first["n_samples"])
    d8, diff8, _ = compare_proportions(round(cur["visibility"] * n_cur), n_cur,
                                       round(first["visibility"] * n_f), n_f)
    if d8 != "持平":
        out.append(("good" if diff8 > 0 else "bad",
                    "**八週來看趨勢明確**：%s → %s，能見度從 %.1f%% 變成 %.1f%%"
                    "（%+.1f 個百分點），這個幅度已經超出誤差範圍。"
                    % (weeks[0], weeks[-1], first["visibility"] * 100,
                       cur["visibility"] * 100, diff8 * 100)))

    # 最弱的意圖
    lw = latest_week()
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
        GROUP BY r.prompt_id
    """)
    df = prompts.merge(stats, left_on="id", right_on="prompt_id", how="left").fillna(
        {"runs": 0, "hits": 0, "neg": 0})

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
        st.caption("點一列，右邊會顯示那一題的完整原始回答。")
        # 欄位刻意只留最必要的四欄 —— 區間、平均排序等細節放在右邊的詳情區，
        # 表格塞太多欄會把「能見度」擠到看不見。
        show = pd.DataFrame({
            "id": view["id"],
            "題目": view["text"],
            "主題": view["topic"],
            # ⚠️ ×100：Streamlit 的 format 是直接套在原始值上，不會自動換算成百分比
            "能見度": (view["能見度"] * 100).round(1),
            "樣本": view["runs"].astype(int),
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
                           r.raw_path, r.cost,
                           (SELECT COUNT(*) FROM mentions m
                            WHERE m.run_id=r.id AND m.is_us=1) AS has_us
                    FROM runs r WHERE r.prompt_id=? ORDER BY r.engine, r.sample_index""",
                 (int(pid),))
        if runs.empty:
            st.info("這一題還沒有跑過。")
            demo_footer()
            return

        eng_list = sorted(runs["engine"].unique().tolist())
        e1, e2 = st.columns([1, 1])
        engine = e1.selectbox("引擎", eng_list, key="pm_engine")
        sub = runs[runs["engine"] == engine]
        labels = ["第 %d 次%s" % (int(r["sample_index"]) + 1,
                                "　✅ 有提到我們" if r["has_us"] else "　—")
                  for _, r in sub.iterrows()]
        idx = e2.selectbox("第幾次提問", range(len(labels)),
                           format_func=lambda i: labels[i], key="pm_sample")
        run = sub.iloc[idx]

        payload, err = read_raw(run["raw_path"])
        if err:
            st.error(err)
        else:
            mentions = q("SELECT * FROM mentions WHERE run_id=? ORDER BY position",
                         (int(run["id"]),))
            st.markdown(
                '<div class="answer-box">%s</div>'
                % highlight(payload.get("answer", ""), mentions["snippet"].tolist()),
                unsafe_allow_html=True)
            st.caption("模型 %s · 提問時間 %s · 花費 $%.4f · 檔案 `%s`"
                       % (run["model_version"], run["ts"], run["cost"], run["raw_path"]))

            st.markdown("**判讀結果**（黃色標記就是判定依據的那一句）")
            if mentions.empty:
                st.info("這則回答沒有提到任何品牌。")
            else:
                m = mentions.copy()
                m["品牌"] = m.apply(
                    lambda r: ("🔵 " if r["is_us"] else "⚪ ") + r["entity"], axis=1)
                m["口氣"] = m.apply(
                    lambda r: {"positive": "🙂 正面", "neutral": "😐 中性",
                               "negative": "🙁 負面"}[r["sentiment_label"]]
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
                WHERE p2.topic = p.topic) AS total
        FROM mentions m
        JOIN runs r    ON r.id = m.run_id
        JOIN prompts p ON p.id = r.prompt_id
        GROUP BY p.topic, m.entity
    """)
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
        "平均排序": b["avg_position"].round(2),
        "情緒均分": b["sentiment_avg"].round(3),
        "正面比例": (b["正面比例"] * 100).round(1),
        "負面比例": (b["負面比例"] * 100).round(1),
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
        WHERE c.run_id IN (SELECT run_id FROM mentions WHERE entity = ?)
        GROUP BY c.domain, c.source_type
        ORDER BY 次數 DESC
    """, (ent,))
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

    cits = q("SELECT * FROM citations")
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
        FROM citations c GROUP BY c.domain ORDER BY 引用次數 DESC
    """)
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
        if st.button("✍️ 產生草稿", key="mk_draft", type="primary"):
            st.info("草稿產生器（`draft.py`）排在階段 5。\n\n"
                    "屆時這個按鈕會執行：\n\n"
                    "```\npython scripts/draft.py --gap-id %d\n```\n\n"
                    "產出一律是 `status=draft`，等人審核，**永遠不自動發佈**。" % gid)

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


def page_settings():
    page_header("設定", "題庫、題目建議、競品名單、採樣設定。這一頁是唯一會寫入資料的地方。")

    t1, t2, t3, t4 = st.tabs(["📋 題庫管理", "💡 題目建議佇列", "🏢 競品名單", "⚙️ 採樣設定"])

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
                    FROM mentions GROUP BY entity, is_us ORDER BY 提及次數 DESC""")
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


# ═══════════════════════════════════════════════════════════════════════
# 主程式：側邊欄七頁
# ═══════════════════════════════════════════════════════════════════════

PAGES = {
    "📊 總覽": page_overview,
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
    st.sidebar.warning("**目前顯示的是示範資料**\n\n"
                       "由 `scripts/seed_demo.py` 產生，用來展示功能與版面，"
                       "不是真實的品牌表現。")
    st.sidebar.caption("階段 0：骨架\n\n"
                       "十張表已建好、七支腳本有可執行的空殼、七頁畫面已排版。\n\n"
                       "階段 1 會接上 OpenAI／Anthropic／Perplexity 的官方 API，"
                       "屆時這裡的數字就會換成真的。")

    PAGES[choice]()


main()
