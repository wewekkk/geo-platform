# -*- coding: utf-8 -*-
"""
seed_demo.py — 塞入示範資料，讓七頁畫面打開就有東西看

吃什麼 → 做什麼 → 吐什麼
  吃：一個已經建好十張表的空資料庫
  做：生一整套假的（但長得合理的）資料 —— 8 週趨勢、5 個引擎、10 個題目、
      數百則回答、數十筆引用、幾個缺口與行動
  吐：① 十張表都有資料
      ② data/raw/ 底下有假的 gzip 回答檔（Prompt 監測頁要顯示完整原文）

怎麼跑
  python scripts/init_db.py     # 先建表
  python scripts/seed_demo.py   # 再塞資料
  python scripts/seed_demo.py --force   # 已經有資料時強制重塞

⚠️ 這些數字全部是假的，只用來展示「功能長什麼樣、資料會怎麼呈現」，
  不是真實的品牌表現。每一頁角落都會標「示範資料」。
  等階段 1 接上真的 API，就用 --force 清掉，換成真資料。

資料怎麼生的（重要，因為之後看到數字會想知道哪些是算的、哪些是編的）
  1. 最新一週的 runs／mentions／citations 是「編出來的原始資料」
  2. 最新一週的 metrics_weekly 是拿上面那批**真的算出來的**
     （包含 Wilson 信賴區間，用的就是 aggregate.py 裡那支函式）
  3. 前面 7 週的 metrics_weekly 是照一條上升趨勢反推的，
     讓趨勢圖有東西看，也讓「行動標記線之後才上升」這件事看得出來
"""

import argparse
import gzip
import json
import os
import random
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (DEFAULT_DB_PATH, OUR_BRAND, ROOT_DIR, connect,  # noqa: E402
                    raw_path)
from aggregate import wilson_interval  # noqa: E402

# 固定亂數種子 → 每次跑出來的示範資料都一樣，方便對照與除錯
random.seed(20260824)

TABLES = ["drafts", "actions", "gaps", "metrics_weekly", "domains",
          "citations", "mentions", "runs", "prompt_suggestions", "prompts"]

# 五個引擎。google_aio 走 SerpApi 抓 Google AI Overviews。
ENGINES = [
    ("openai", "gpt-5.1-2026-04"),
    ("anthropic", "claude-opus-4-6"),
    ("perplexity", "sonar-pro-2026-03"),
    ("gemini", "gemini-3.0-pro"),
    ("google_aio", "aio-serpapi"),
]

# 競品名單（示範用的假名字）
COMPETITORS = ["PetBacker", "寵物保姆網", "樂寵 LePet", "毛守護寵物托育", "旺來寵物旅館"]

# 十個題目：text, topic, intent, 難易（我們在這題的基礎能見度）
PROMPTS = [
    ("台灣有哪些寵物保姆媒合平台？", "平台選擇", "discovery", 0.72),
    ("找寵物保姆到府餵貓，推薦哪個平台？", "到府服務", "discovery", 0.66),
    ("寵物保姆平台比較，哪一個比較好用？", "平台選擇", "comparison", 0.58),
    ("寵物保姆一天大概多少錢？", "價格", "price", 0.44),
    ("寵物寄宿和到府保姆哪個比較划算？", "價格", "comparison", 0.36),
    ("怎麼確認寵物保姆是可信的？有沒有身分驗證？", "信任查證", "trust", 0.28),
    ("寵物保姆把我的狗弄丟或受傷，平台會賠嗎？", "信任查證", "trust", 0.18),
    ("臨時出差明天就要出門，可以馬上找到寵物保姆嗎？", "急件需求", "urgent", 0.22),
    ("過年期間還找得到寵物保姆嗎？要多早預約？", "急件需求", "urgent", 0.31),
    ("寵物保姆需要準備什麼？飼主要交代哪些事？", "服務流程", "logistics", 0.49),
]

# 各引擎的體質差異：有些引擎就是比較常提到我們
ENGINE_BIAS = {
    "openai": 0.05, "anthropic": 0.02, "perplexity": 0.10,
    "gemini": -0.06, "google_aio": -0.12,
}

# 引用來源池：(網域, 類型, 是不是我們的)
DOMAIN_POOL = [
    ("fluv.com", "品牌內容", 1),
    ("blog.fluv.com", "品牌內容", 1),
    ("petbacker.com", "品牌內容", 0),
    ("lepet.com.tw", "品牌內容", 0),
    ("dcard.tw", "論壇", 0),
    ("ptt.cc", "論壇", 0),
    ("mobile01.com", "論壇", 0),
    ("facebook.com", "社群", 0),
    ("instagram.com", "社群", 0),
    ("youtube.com", "社群", 0),
    ("threads.net", "社群", 0),
    ("ettoday.net", "媒體", 0),
    ("storm.mg", "媒體", 0),
    ("businesstoday.com.tw", "媒體", 0),
    ("gvm.com.tw", "媒體", 0),
    ("setn.com", "媒體", 0),
]

N_SAMPLES = 10          # 每題每引擎問幾次
LOCALE = "tw"
CONDITION = "api"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def week_label(d):
    """把日期換成 ISO 週標籤，例如 2026-W34"""
    y, w, _ = d.isocalendar()
    return "%04d-W%02d" % (y, w)


def last_n_mondays(n):
    """回傳最近 n 個週一（由舊到新，最後一個是本週一）"""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return [monday - timedelta(weeks=i) for i in range(n - 1, -1, -1)]


# ---------------------------------------------------------------------------
# 產生一則假的 AI 回答
# ---------------------------------------------------------------------------

def build_answer(prompt_text, ordered_entities, sentiments):
    """照「被提到的品牌順序」組出一段假的回答文字。

    重點是 snippet 要真的出現在這段文字裡，
    這樣 Prompt 監測頁顯示原文時，判定依據才標得出來。
    """
    lines = ["針對「%s」，以下整理幾個目前台灣常見的選擇：" % prompt_text, ""]
    snippets = {}
    for i, ent in enumerate(ordered_entities, start=1):
        label = sentiments.get(ent, "neutral")
        if label == "positive":
            tail = "使用者評價普遍不錯，媒合速度快、保姆資料也標示得清楚。"
        elif label == "negative":
            tail = "不過有部分使用者反映客服回覆慢，臨時取消的處理方式也讓人有疑慮。"
        else:
            tail = "服務範圍涵蓋雙北與中南部主要都會區，收費方式依服務型態而定。"
        sentence = "%d. %s —— %s" % (i, ent, tail)
        lines.append(sentence)
        snippets[ent] = sentence
    lines += ["", "建議實際比較保姆的驗證資料、保險條款與過往評價再做決定。"]
    return "\n".join(lines), snippets


def write_raw_file(d, engine, prompt_id, sample_index, payload):
    """把假的原始回答壓成 gzip 寫進 data/raw/。回傳相對路徑。"""
    y, w, _ = d.isocalendar()
    rel = raw_path(y, w, engine, prompt_id, LOCALE, sample_index)
    full = os.path.join(ROOT_DIR, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with gzip.open(full, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return rel


# ---------------------------------------------------------------------------
# 主要流程
# ---------------------------------------------------------------------------

def seed(conn, verbose=True):
    cur = conn.cursor()
    weeks = last_n_mondays(8)
    latest = weeks[-1]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # --- 1. 題庫 -----------------------------------------------------------
    from common import INTENT_WEIGHTS
    prompt_rows = []
    for i, (text, topic, intent, base) in enumerate(PROMPTS, start=1):
        cur.execute(
            "INSERT INTO prompts (id, text, topic, intent, intent_weight, locale, "
            "active, source, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (i, text, topic, intent, INTENT_WEIGHTS[intent], LOCALE, 1, "demo", now))
        prompt_rows.append((i, text, topic, intent, base))
    say(verbose, "  prompts             %d 筆" % len(prompt_rows))

    # --- 2. 題目建議佇列 ---------------------------------------------------
    suggestions = [
        ("寵物保姆有保險嗎？出事誰負責？", "gsc",
         "GSC 上個月曝光 1,840 次、點擊 23 次，站上沒有對應內容", 0.92, "pending"),
        ("寵物保姆和寵物旅館差在哪？", "gsc",
         "GSC 曝光 1,210 次，排名第 14 名，屬於容易補上的位置", 0.81, "pending"),
        ("到府餵貓一次多久？保姆會待多久？", "site_search",
         "站內搜尋近 30 天出現 87 次，站上沒有明確答案", 0.74, "pending"),
        ("寵物保姆可以帶狗出去散步嗎？", "answer_concept",
         "在 6 則 AI 回答中被提到，但題庫沒有涵蓋", 0.63, "pending"),
        ("寵物保姆要付訂金嗎？可以退嗎？", "gsc",
         "GSC 曝光 640 次，且屬於價格類意圖", 0.58, "pending"),
        ("寵物保姆會提供每日回報照片嗎？", "answer_concept",
         "競品回答中反覆出現的賣點，我們沒有對應題目", 0.55, "pending"),
        ("寵物保姆平台哪家手續費最低？", "site_search",
         "站內搜尋 31 次；已有相近題目，可能重複", 0.34, "ignored"),
        ("狗狗寄宿要打什麼疫苗？", "gsc",
         "GSC 曝光 2,100 次，但與媒合服務關聯較低", 0.41, "accepted"),
    ]
    for text, src, ev, imp, status in suggestions:
        cur.execute(
            "INSERT INTO prompt_suggestions (text, source, evidence, est_importance, "
            "status, created_at) VALUES (?,?,?,?,?,?)",
            (text, src, ev, imp, status, now))
    say(verbose, "  prompt_suggestions  %d 筆" % len(suggestions))

    # --- 3. 網域清單 -------------------------------------------------------
    first_seen = (latest - timedelta(weeks=6)).isoformat()
    for dom, stype, ours in DOMAIN_POOL:
        note = "我們自己的網域" if ours else None
        cur.execute(
            "INSERT INTO domains (domain, source_type, is_ours, first_seen, notes) "
            "VALUES (?,?,?,?,?)", (dom, stype, ours, first_seen, note))
    say(verbose, "  domains             %d 筆" % len(DOMAIN_POOL))

    # --- 4. 最新一週的 runs／mentions／citations ---------------------------
    n_runs = n_mentions = n_citations = 0
    for prompt_id, text, topic, intent, base in prompt_rows:
        for engine, model_version in ENGINES:
            p_us = min(0.95, max(0.02, base + ENGINE_BIAS[engine]))
            for s in range(N_SAMPLES):
                ts = (datetime.combine(latest, datetime.min.time())
                      + timedelta(hours=3, minutes=12 + s, seconds=prompt_id * 7))
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")

                # 這則回答提到了哪些品牌、順序是什麼
                mentioned = []
                if random.random() < p_us:
                    mentioned.append(OUR_BRAND)
                for c in COMPETITORS:
                    if random.random() < 0.45:
                        mentioned.append(c)
                random.shuffle(mentioned)
                # 我們有被提到時，順序偏前面一點（但不是每次都第一）
                if OUR_BRAND in mentioned and random.random() < 0.5:
                    mentioned.remove(OUR_BRAND)
                    mentioned.insert(random.randint(0, min(1, len(mentioned))), OUR_BRAND)

                # 情緒：多數中性偏正面，信任類題目偶爾出現負面（風險提及）
                sentiments = {}
                for ent in mentioned:
                    r = random.random()
                    neg_p = 0.14 if intent == "trust" else 0.06
                    if r < neg_p:
                        sentiments[ent] = "negative"
                    elif r < neg_p + 0.45:
                        sentiments[ent] = "neutral"
                    else:
                        sentiments[ent] = "positive"

                answer, snippets = build_answer(text, mentioned, sentiments)

                # 這則有沒有附引用來源（大約四分之一有，湊成數十筆）
                cites = []
                if random.random() < 0.055:
                    pool = random.sample(DOMAIN_POOL, random.randint(2, 4))
                    for rank, (dom, stype, ours) in enumerate(pool, start=1):
                        cites.append({
                            "url": "https://%s/article/%d" % (dom, random.randint(100, 999)),
                            "domain": dom, "source_type": stype, "rank": rank,
                        })

                payload = {
                    "prompt_id": prompt_id, "prompt_text": text,
                    "engine": engine, "model_version": model_version,
                    "locale": LOCALE, "condition": CONDITION,
                    "sample_index": s, "ts": ts_str,
                    "web_search_used": True,
                    "answer": answer,
                    "citations": cites,
                    "_demo": "這是示範資料，不是真的 AI 回答",
                }
                rel = write_raw_file(latest, engine, prompt_id, s, payload)

                cur.execute(
                    "INSERT INTO runs (prompt_id, engine, model_version, locale, "
                    "condition, sample_index, ts, raw_path, cost, error) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (prompt_id, engine, model_version, LOCALE, CONDITION, s,
                     ts_str, rel, round(random.uniform(0.004, 0.019), 4), None))
                run_id = cur.lastrowid
                n_runs += 1

                for pos, ent in enumerate(mentioned, start=1):
                    label = sentiments[ent]
                    score = {"positive": random.uniform(0.35, 0.85),
                             "neutral": random.uniform(-0.12, 0.15),
                             "negative": random.uniform(-0.85, -0.30)}[label]
                    is_us = 1 if ent == OUR_BRAND else 0
                    # 風險提及 = 負面 而且 是信任／急件類題目（會直接影響成交）
                    risk = 1 if (label == "negative" and intent in ("trust", "urgent")) else 0
                    cur.execute(
                        "INSERT INTO mentions (run_id, entity, is_us, position, "
                        "sentiment_score, sentiment_label, risk_flag, snippet) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, ent, is_us, pos, round(score, 3), label, risk,
                         snippets[ent]))
                    n_mentions += 1

                for c in cites:
                    cur.execute(
                        "INSERT INTO citations (run_id, url, domain, source_type, rank) "
                        "VALUES (?,?,?,?,?)",
                        (run_id, c["url"], c["domain"], c["source_type"], c["rank"]))
                    n_citations += 1

    say(verbose, "  runs                %d 筆（原始檔案寫進 data/raw/）" % n_runs)
    say(verbose, "  mentions            %d 筆" % n_mentions)
    say(verbose, "  citations           %d 筆" % n_citations)

    # --- 5. metrics_weekly -------------------------------------------------
    n_metrics = seed_metrics(conn, weeks)
    say(verbose, "  metrics_weekly      %d 筆（最新一週是真的從上面算出來的）" % n_metrics)

    # --- 6. 缺口 -----------------------------------------------------------
    d = lambda wk: weeks[wk].isoformat()
    gaps = [
        ("保姆身分驗證與保險保障說明", "信任查證", 4.62, "open", d(1), None, "6,7",
         "「怎麼確認保姆可信」「出事誰賠」這類題目，AI 幾乎只引用論壇討論串，"
         "沒有任何一家平台提供可被引用的制式說明。我們的驗證流程與保險條款"
         "目前沒有獨立頁面，模型抓不到。"),
        ("急件／連假時段的可預約性", "急件需求", 3.85, "open", d(2), None, "8,9",
         "臨時與連假需求的題目上，我們的能見度只有 22%，"
         "但這兩類是轉換率最高的意圖。回答中常見「建議提早兩週預約」，"
         "沒有人講「臨時也找得到」這件事。"),
        ("到府服務 vs 寄宿的費用比較", "價格", 3.21, "in_progress", d(2), None, "4,5",
         "價格類題目 AI 一律給區間，資料來源多半是三年前的部落格文章。"
         "提供一份有明確數字、有更新日期的比較表，很容易被取代掉。"),
        ("寵物保姆服務流程與飼主準備事項", "服務流程", 2.44, "done", d(4), d(6), "10",
         "流程類題目原本完全缺席。已於 7 月補上完整流程頁，"
         "目前該題能見度已從 31% 升到 62%。"),
        ("平台比較類題目的第三方背書", "平台選擇", 1.98, "holdout", d(3), None, "1,3",
         "刻意不處理的對照組。這個缺口的條件跟「價格比較」很像，"
         "我們刻意先不動它，之後用來對照：如果它也自己上升了，"
         "代表數字變好是模型漂移，不是我們的功勞。"),
    ]
    for g in gaps:
        cur.execute(
            "INSERT INTO gaps (title, topic, opportunity_score, status, opened_at, "
            "closed_at, affected_prompt_ids, why) VALUES (?,?,?,?,?,?,?,?)", g)
    say(verbose, "  gaps                %d 筆（含一筆 holdout 對照組）" % len(gaps))

    # --- 7. 行動（趨勢圖上的垂直標記線）-----------------------------------
    actions = [
        (4, "owned", "上線〈寵物保姆服務全流程〉說明頁", "https://fluv.com/guide/process",
         d(3), "把原本散在 FAQ 的流程整理成一頁，每段開頭直接給答案。"),
        (4, "social", "Threads 系列貼文：保姆到府的一天", "https://threads.net/@fluv/post/demo",
         d(4), "六則圖文，帶出流程頁連結。"),
        (3, "pr", "ETtoday 專訪：寵物托育的價格結構", "https://ettoday.net/news/demo",
         d(5), "媒體露出，提供了具體的費用區間數字。"),
        (3, "owned", "上線〈到府 vs 寄宿費用比較表〉", "https://fluv.com/guide/pricing-compare",
         d(6), "含明確金額與更新日期，方便被模型擷取。"),
    ]
    for a in actions:
        cur.execute(
            "INSERT INTO actions (gap_id, channel, title, url, published_at, notes) "
            "VALUES (?,?,?,?,?,?)", a)
    say(verbose, "  actions             %d 筆" % len(actions))

    # --- 8. 草稿 -----------------------------------------------------------
    drafts = [
        (1, "寵物保姆的身分驗證包含哪些項目？",
         "1. 驗證包含哪四項\n2. 每項怎麼驗、由誰驗\n3. 驗證失敗會怎樣\n4. 保險理賠範圍與上限",
         "Fluv 的保姆驗證包含四項：身分證件、居住地址、飼養經驗證明、面試記錄。\n\n"
         "身分證件由第三方服務比對，平均驗證時間 18 小時。\n\n"
         "每一筆委託都含平安險，單次事故理賠上限為新台幣 10 萬元。\n",
         "draft", None, now),
        (2, "臨時出差，最快多久可以找到寵物保姆？",
         "1. 最快媒合時間（給數字）\n2. 哪些時段比較難找\n3. 連假前建議提前多久",
         "在雙北地區，臨時委託的平均媒合時間為 3 小時 40 分，"
         "有 68% 的急件在 6 小時內完成媒合。\n\n"
         "農曆年前兩週是最難媒合的時段，建議提前 21 天預約。\n",
         "reviewing", None, now),
        (3, "到府保姆和寵物寄宿，一天各要多少錢？",
         "1. 兩種服務的單日費用區間\n2. 費用差在哪\n3. 什麼情況選哪一種",
         "到府保姆單日費用區間為 600～1,200 元，寵物寄宿為 500～900 元。\n\n"
         "到府服務較貴的原因是交通成本與單一委託制。\n",
         "approved", 4, now),
    ]
    for dr in drafts:
        cur.execute(
            "INSERT INTO drafts (gap_id, title, outline, body, status, action_id, "
            "created_at) VALUES (?,?,?,?,?,?,?)", dr)
    say(verbose, "  drafts              %d 筆（全部停在未發佈狀態）" % len(drafts))

    conn.commit()


def seed_metrics(conn, weeks):
    """產生 metrics_weekly。

    最新一週：從剛剛塞進去的 runs／mentions **真的算出來**（含 Wilson 區間）
    前面 7 週：照一條上升趨勢往回推，讓趨勢圖有東西看

    這樣做的原因：最新一週的數字要能跟 Prompt 監測頁的原始資料對得起來，
    否則使用者在兩頁看到不同的數字會不知道該信哪個。
    """
    cur = conn.cursor()
    latest_week = week_label(weeks[-1])

    # 三種切法：整體、分引擎、分主題、分意圖
    dims = [("all", "all", "all", "1=1", ())]
    for engine, _ in ENGINES:
        dims.append((engine, "all", "all", "r.engine = ?", (engine,)))
    for topic in sorted({p[1] for p in PROMPTS}):
        dims.append(("all", topic, "all", "p.topic = ?", (topic,)))
    for intent in sorted({p[2] for p in PROMPTS}):
        dims.append(("all", "all", intent, "p.intent = ?", (intent,)))

    n = 0
    for engine, topic, intent, where, params in dims:
        # 這一格總共有幾則回答
        total = cur.execute(
            "SELECT COUNT(*) FROM runs r JOIN prompts p ON p.id = r.prompt_id "
            "WHERE " + where, params).fetchone()[0]
        if not total:
            continue
        # 其中有幾則提到了我們（能見度的分子）
        hits = cur.execute(
            "SELECT COUNT(DISTINCT r.id) FROM runs r JOIN prompts p ON p.id = r.prompt_id "
            "JOIN mentions m ON m.run_id = r.id AND m.is_us = 1 WHERE " + where,
            params).fetchone()[0]
        # 我們的提及數 ÷ 所有品牌的提及數 = SoV
        us_m, all_m = cur.execute(
            "SELECT SUM(m.is_us), COUNT(*) FROM runs r JOIN prompts p ON p.id = r.prompt_id "
            "JOIN mentions m ON m.run_id = r.id WHERE " + where, params).fetchone()
        # 我們被提到時的平均排序
        avg_pos = cur.execute(
            "SELECT AVG(m.position) FROM runs r JOIN prompts p ON p.id = r.prompt_id "
            "JOIN mentions m ON m.run_id = r.id AND m.is_us = 1 WHERE " + where,
            params).fetchone()[0]
        # 我們的情緒分布
        sent = cur.execute(
            "SELECT AVG(m.sentiment_score), "
            "SUM(m.sentiment_label='positive'), SUM(m.sentiment_label='neutral'), "
            "SUM(m.sentiment_label='negative'), COUNT(*) "
            "FROM runs r JOIN prompts p ON p.id = r.prompt_id "
            "JOIN mentions m ON m.run_id = r.id AND m.is_us = 1 WHERE " + where,
            params).fetchone()

        vis = hits / total
        low, high = wilson_interval(hits, total)
        sov = (us_m or 0) / all_m if all_m else 0
        sc = sent[4] or 0
        rows_latest = dict(
            visibility=vis, ci_low=low, ci_high=high, sov=sov,
            avg_position=avg_pos, sentiment_avg=sent[0] or 0,
            pos_pct=(sent[1] or 0) / sc if sc else 0,
            neu_pct=(sent[2] or 0) / sc if sc else 0,
            neg_pct=(sent[3] or 0) / sc if sc else 0,
            n_samples=total,
        )
        _insert_metric(cur, latest_week, engine, topic, intent, rows_latest)
        n += 1

        # 往回推 7 週：能見度一路比現在低，中間有起伏（模擬隨機波動）
        deltas = [-0.19, -0.17, -0.16, -0.13, -0.13, -0.08, -0.04]
        wobble = [0.012, -0.021, 0.008, -0.014, 0.019, -0.007, 0.011]
        for i, wk in enumerate(weeks[:-1]):
            v = max(0.02, min(0.97, vis + deltas[i] + wobble[i]))
            h = int(round(v * total))
            lo, hi = wilson_interval(h, total)
            shift = deltas[i] * 0.5   # 早期的情緒也差一點
            pos = max(0.0, rows_latest["pos_pct"] + shift)
            neg = min(1.0, rows_latest["neg_pct"] - shift * 0.6)
            neu = max(0.0, 1.0 - pos - neg)
            _insert_metric(cur, week_label(wk), engine, topic, intent, dict(
                visibility=h / total, ci_low=lo, ci_high=hi,
                sov=max(0.02, rows_latest["sov"] + deltas[i] * 0.6),
                avg_position=(rows_latest["avg_position"] or 2.5) - deltas[i] * 2.2,
                sentiment_avg=rows_latest["sentiment_avg"] + shift,
                pos_pct=pos, neu_pct=neu, neg_pct=neg, n_samples=total))
            n += 1
    return n


def _insert_metric(cur, week, engine, topic, intent, v):
    cur.execute(
        "INSERT OR REPLACE INTO metrics_weekly (week, engine, topic, intent, "
        "visibility, ci_low, ci_high, sov, avg_position, sentiment_avg, "
        "pos_pct, neu_pct, neg_pct, n_samples) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (week, engine, topic, intent,
         round(v["visibility"], 4), round(v["ci_low"], 4), round(v["ci_high"], 4),
         round(v["sov"], 4),
         round(v["avg_position"], 2) if v["avg_position"] else None,
         round(v["sentiment_avg"], 3),
         round(v["pos_pct"], 4), round(v["neu_pct"], 4), round(v["neg_pct"], 4),
         v["n_samples"]))


def say(verbose, msg):
    if verbose:
        print(msg)


def clear(conn):
    cur = conn.cursor()
    for t in TABLES:
        cur.execute("DELETE FROM %s" % t)
        cur.execute("DELETE FROM sqlite_sequence WHERE name = ?", (t,))
    conn.commit()


def main():
    parser = argparse.ArgumentParser(
        description="塞入示範資料，讓七頁畫面打開就有東西看",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/seed_demo.py\n"
               "  python scripts/seed_demo.py --force   # 清掉舊資料重塞\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--force", action="store_true",
                        help="資料庫裡已經有資料時，清空後重塞")
    parser.add_argument("--quiet", action="store_true", help="不要印出過程")
    args = parser.parse_args()

    try:
        conn = connect(args.db)
    except FileNotFoundError as e:
        print(e)
        return 1

    existing = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
    if existing and not args.force:
        real = conn.execute(
            "SELECT COUNT(*) FROM prompts WHERE source IS NULL OR source <> 'demo'"
        ).fetchone()[0]
        print("資料庫裡已經有 %d 筆題目（其中 %d 筆不是示範資料）。" % (existing, real))
        if real:
            print("這些看起來是你自己加的題目。如果確定要全部清掉重塞示範資料，")
        print("請加上 --force：python scripts/seed_demo.py --force")
        conn.close()
        return 1

    if existing:
        say(not args.quiet, "正在清空舊資料 ...")
        clear(conn)

    say(not args.quiet, "正在產生示範資料（數字全部是假的，只用來展示畫面）...")
    seed(conn, verbose=not args.quiet)
    conn.close()

    print()
    print("完成。下一步：streamlit run scripts/app.py")
    print("（七頁都會有內容，每頁角落標示「示範資料」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
