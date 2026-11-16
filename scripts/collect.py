# -*- coding: utf-8 -*-
"""
collect.py — 每週去問各家 AI，把原始回答存下來

吃什麼 → 做什麼 → 吐什麼
  吃：prompts 表裡 active=1 的題目
  做：每一題 × 每一個引擎，各問一次
  吐：① 原始回答壓成 gzip 存進 data/raw/{年}/W{週}/{引擎}/
      ② 每次呼叫的 metadata（時間、模型版本、花費、檔案路徑）寫進 runs 表

跑一次 = 一輪。編號自動接續，不會覆蓋
  同一題要累積多次回答，是因為 AI 每次講的都不一樣 ——
  問一次的話，提到就是 100%、沒提到就是 0%，那是擲硬幣不是量測。
  累積多次之後才算得出「10 次裡有 6 次提到我們」這種有意義的數字。

  這支腳本每跑一次，就替每題每家多問一次，編號自動接在上次後面
  （第 1 次、第 2 次、第 3 次…），舊的檔案永遠不會被蓋掉。
  所以要累積幾次，由你決定跑幾次，不需要改任何設定。

  ⚠️ 分散在不同天跑，比一次連問很多次好。
     一次連問的話，那幾次共享同一個時間點的搜尋環境
     （例如當天剛好有篇新聞洗版），信賴區間會假性變窄，
     看起來「數字很穩定」，其實只是同一個瞬間量了很多遍。

怎麼跑
  python scripts/collect.py --dry-run           # 先看會花多少錢，不呼叫 API（強烈建議先跑這個）
  python scripts/collect.py --limit 1           # 只試一題，確認接得通
  python scripts/collect.py                     # 跑一輪：全部 active 題目 × 每家各一次
  python scripts/collect.py --engine gemini     # 只跑一家
  python scripts/collect.py --samples 2         # 一次跑兩輪（少用，分開跑比較好）

  上次跑到一半中斷的話，直接再跑一次就好 —— 已經有檔案的會自動跳過、不會重複收費。

前置條件
  1. 已經跑過 init_db.py
  2. prompts 表裡有 active=1 的題目
  3. .env 裡有各家的 API 金鑰（cp .env.example .env 後填入）
  4. pip install -r requirements.txt

⚠️ 兩件絕對不能違反的事
  1. 只用官方 API + SerpApi。絕對不爬 AI 產品的網頁介面（ToS 風險）。
  2. 呼叫各家時務必開啟該家的 web search 工具。
     不開的話，問到的是「裸模型的記憶」，不是會查資料的助理，數字沒有意義。
     → 本檔三個引擎都已經把 web search 打開，見各 call_* 函式。

⚠️ 原始回答一旦寫入就永不修改、永不刪除。
  判讀邏輯之後改了，要能拿這些檔案整批重跑，把歷史數字全部重算。
  → 實作上：檔案已存在就跳過，不覆蓋；寫入用「先寫暫存檔再改名」確保不會留半個檔案。
"""

import argparse
import gzip
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    DEFAULT_DB_PATH, ENGINE_KEY_NAMES, ROOT_DIR, connect, get_api_key,
    iso_year_week, now_iso, raw_path,
)

# ---------------------------------------------------------------------------
# 引擎與模型設定
# ---------------------------------------------------------------------------

# 支援的引擎。Google AI Overviews（走 SerpApi）留到階段 3。
ENGINES = ["gemini", "openai", "anthropic", "perplexity"]

# 不指定 --engine 時預設會跑哪幾家。
#
# 2026-09-21 決定先只跑 Gemini —— 它的免費額度包含 Google 搜尋
# （Gemini 2.5 Flash，每天 500 次帶搜尋的請求），不用綁信用卡就能把整條路跑通。
# 其他三家的程式都寫好了，要跑的時候加 --engine 就會動，不用改程式：
#     python scripts/collect.py --engine gemini --engine anthropic
DEFAULT_ENGINES = ["gemini"]

# 每家用哪個模型問。
#
# 為什麼選各家的旗艦模型：我們要量的是「一般消費者問 AI 會得到什麼答案」，
# 所以要用消費者實際會碰到的那顆模型，不是便宜的小模型。
# 模型換版會讓數字跳動，所以每一筆都會把實際回答的版本記進 runs.model_version。
MODELS = {
    # 2026-09-21 實測：gemini-2.5-flash 已經退役（呼叫回 404），
    # 所以改用 3.8 Flash。刻意寫死版本而不用 gemini-flash-latest 這種別名 ——
    # 別名會在某天無聲無息換成新模型，數字跟著跳，而沒有人知道為什麼。
    #
    # ⚠️ 開了 Google 搜尋就一定要有付費帳戶（見 README）。
    #    免費額度只能做「不查資料」的問答，那種答案對這個專案沒有意義。
    "gemini": "gemini-3.8-flash",
    "openai": "gpt-6-astra",
    "anthropic": "claude-opus-5",
    "perplexity": "perplexity/sonar",
}

# 每百萬 token 的價錢（美金）：(輸入, 輸出)
# 這是用來估算與記帳的，官方調價的話這裡要跟著改。
PRICING = {
    # Gemini 3.8 Flash：2026 年底前的價格，2027-01-01 起漲成 $1.50 / $7.50。
    # 另外「Google 搜尋」每月前 5,000 次免費，超過才 $14／1,000 次。
    # 我們一輪只用約 216 次搜尋，一個月約 900 次，都在免費範圍內，
    # 所以下面只算 token 錢，沒有把搜尋費算進去。
    "gemini-3.8-flash": (0.75, 3.75),
    "gpt-6-astra": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "perplexity/sonar": (0.25, 2.50),
}

# 開了 web search 之後，每做一次搜尋另外算錢（美金／次）。
# 這是估計值，用來讓 --dry-run 的預估不要低估太多。
WEB_SEARCH_COST_PER_SEARCH = 0.01

# --dry-run 估算用的 token 數。開了 web search 之後，搜尋結果會被塞進輸入，
# 所以輸入 token 比題目本身多很多。這兩個數字是粗估，實際跑過一輪後可以回來調。
EST_INPUT_TOKENS = 8000
EST_OUTPUT_TOKENS = 1200
EST_SEARCHES_PER_CALL = 3

# 回答長度上限。消費者問句的答案不會太長，4000 綽綽有餘。
MAX_TOKENS = 4000

# 每次呼叫之間停一下，避免撞到各家的速率限制（秒）
SLEEP_BETWEEN_CALLS = 1.0


# ---------------------------------------------------------------------------
# 三家的實際呼叫
#
# 每個 call_* 函式的約定：
#   成功 → 回傳 dict：{answer_text, model_version, cost, searches, response}
#   失敗 → 直接把例外丟出去，由上層寫進 runs.error
#
# ⚠️ 三家都刻意「不設 temperature、不開 prompt caching」：
#    同一題要問 N 次才算得出信賴區間。如果每次回答都一樣，
#    區間會假性地收得很窄，整套統計就變成裝飾品。
#    （註：Claude Opus 5 / Sonnet 5 這一代已經把 temperature 參數移除了，
#      送過去會直接 400。回答的變異性由模型本身提供，
#      所以本腳本改用「跑完檢查 N 則回答是不是全都一樣」來把關，見 warn_if_identical()。）
# ---------------------------------------------------------------------------

def call_gemini(prompt_text, model, api_key):
    """Gemini：用 generate_content，開 Google 搜尋（grounding）。

    免費額度：Gemini 2.5 Flash 每天 500 次帶搜尋的請求，不用綁信用卡。
    超過的話 API 會回錯誤（不會偷偷變成付費），錯誤訊息會寫進 runs.error。
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=prompt_text,
        config=types.GenerateContentConfig(
            # 這一行就是「把 Google 搜尋打開」。拿掉的話問到的是模型的記憶。
            tools=[types.Tool(google_search=types.GoogleSearch())],
            # Gemini 這一代還支援 temperature。設 1.0（預設值）讓每次回答不一樣，
            # 同一題問 N 次才算得出信賴區間。千萬不要設成 0。
            temperature=1.0,
            max_output_tokens=MAX_TOKENS,
        ),
    )
    raw = resp.model_dump(mode="json")

    # 搜尋次數：grounding_metadata 裡記了它實際下了哪些搜尋關鍵字
    searches = 0
    for cand in raw.get("candidates") or []:
        meta = cand.get("grounding_metadata") or {}
        searches += len(meta.get("web_search_queries") or [])

    usage = raw.get("usage_metadata") or {}
    return {
        "answer_text": resp.text or "",
        "model_version": raw.get("model_version") or model,
        # 免費額度內實際是 $0，這個數字是「如果要付費會是多少」
        "cost": token_cost(model, usage.get("prompt_token_count"),
                           usage.get("candidates_token_count")),
        "searches": searches,
        "response": raw,
    }


def call_openai(prompt_text, model, api_key):
    """OpenAI：用 Responses API，開 web_search 工具。"""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    resp = client.responses.create(
        model=model,
        input=prompt_text,
        tools=[{
            "type": "web_search",
            # 固定在台灣，跨週的數字才可以比。地區變了，答案就會變。
            "user_location": {
                "type": "approximate",
                "country": "TW",
                "timezone": "Asia/Taipei",
            },
        }],
        max_output_tokens=MAX_TOKENS,
    )
    raw = resp.model_dump()
    usage = raw.get("usage") or {}
    searches = sum(
        1 for item in (raw.get("output") or [])
        if item.get("type") == "web_search_call"
    )
    return {
        "answer_text": resp.output_text or "",
        "model_version": raw.get("model") or model,
        "cost": token_cost(model, usage.get("input_tokens"), usage.get("output_tokens")) +
                searches * WEB_SEARCH_COST_PER_SEARCH,
        "searches": searches,
        "response": raw,
    }


def call_anthropic(prompt_text, model, api_key):
    """Anthropic：用 Messages API，開 web_search 工具。"""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        tools=[{
            "type": "web_search_20260209",
            "name": "web_search",
            "user_location": {
                "type": "approximate",
                "country": "TW",
                "timezone": "Asia/Taipei",
            },
        }],
        messages=[{"role": "user", "content": prompt_text}],
    )
    raw = resp.to_dict()

    # 回答文字可能被拆成多個 text 區塊（中間夾著搜尋結果），要全部接起來
    answer = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    )

    usage = raw.get("usage") or {}
    # 搜尋次數：SDK 有給就用，沒有就自己數回應裡的搜尋結果區塊
    searches = ((usage.get("server_tool_use") or {}).get("web_search_requests"))
    if searches is None:
        searches = sum(
            1 for b in (raw.get("content") or [])
            if b.get("type") == "web_search_tool_result"
        )

    return {
        "answer_text": answer,
        "model_version": raw.get("model") or model,
        "cost": token_cost(model, usage.get("input_tokens"), usage.get("output_tokens")) +
                searches * WEB_SEARCH_COST_PER_SEARCH,
        "searches": searches,
        "response": raw,
    }


def call_perplexity(prompt_text, model, api_key):
    """Perplexity：用 Agent API（POST /v1/agent），開 web_search 工具。

    ⚠️ 注意：舊的 Sonar chat completions 端點在 2026-09-27 停用，
       所以這裡直接用新的 Agent API，不用舊的 /chat/completions。
    """
    import requests

    resp = requests.post(
        "https://api.perplexity.ai/v1/agent",
        headers={
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": prompt_text,
            "tools": [{"type": "web_search"}],
        },
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError("Perplexity 回傳 HTTP %s：%s" % (resp.status_code, resp.text[:300]))
    raw = resp.json()

    # 回答文字在 output 陣列裡 type=message 的那一項
    answer = ""
    searches = 0
    for item in raw.get("output") or []:
        if item.get("type") == "message":
            for block in item.get("content") or []:
                answer += block.get("text") or ""
        elif item.get("type") == "search_results":
            searches += 1

    usage = raw.get("usage") or {}
    # Perplexity 直接回報這次花了多少錢，有的話就用它的，比自己算準
    cost = (usage.get("cost") or {}).get("total_cost")
    if cost is None:
        cost = token_cost(model, usage.get("input_tokens"), usage.get("output_tokens"))

    return {
        "answer_text": answer,
        "model_version": raw.get("model") or model,
        "cost": float(cost),
        "searches": searches,
        "response": raw,
    }


CALLERS = {
    "gemini": call_gemini,
    "openai": call_openai,
    "anthropic": call_anthropic,
    "perplexity": call_perplexity,
}


def token_cost(model, input_tokens, output_tokens):
    """依 PRICING 算這次呼叫的 token 花費（美金）。"""
    price_in, price_out = PRICING.get(model, (0.0, 0.0))
    return ((input_tokens or 0) / 1e6 * price_in) + ((output_tokens or 0) / 1e6 * price_out)


# ---------------------------------------------------------------------------
# 原始回答的寫入
# ---------------------------------------------------------------------------

def next_round(year, week, engine, locale):
    """這一家、這一週，下一次的編號應該是幾號。

    為什麼用「看檔案」而不是「看資料庫」：原始檔案是永不修改的資產，
    它存在就代表那一次真的問到了。資料庫可能有失敗的紀錄（沒有檔案），
    那些不該佔用編號。

    回傳 0 代表這一家這一週還沒問過。
    """
    folder = os.path.join(ROOT_DIR, "data", "raw", str(year), "W%02d" % int(week), engine)
    if not os.path.isdir(folder):
        return 0
    pattern = re.compile(r"^\d+_%s_(\d+)\.json\.gz$" % re.escape(locale))
    highest = -1
    for name in os.listdir(folder):
        m = pattern.match(name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def write_raw(rel_path, payload):
    """把一則原始回答壓成 gzip 寫到指定路徑。

    兩個保護：
      1. 檔案已經存在就不動它（原始回答永不修改、永不刪除）
      2. 先寫 .tmp 再改名 —— 中途當掉不會留下一個讀不出來的半截檔案
    """
    full = os.path.join(ROOT_DIR, rel_path)
    if os.path.exists(full):
        return False  # 已經有了，跳過
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.rename(tmp, full)
    return True


# ---------------------------------------------------------------------------
# 主要流程
# ---------------------------------------------------------------------------

def load_prompts(conn, locale, limit, prompt_ids):
    """讀出要跑的題目。"""
    sql = "SELECT id, text, topic, intent FROM prompts WHERE active = 1 AND locale = ?"
    params = [locale]
    if prompt_ids:
        sql += " AND id IN (%s)" % ",".join("?" * len(prompt_ids))
        params += list(prompt_ids)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT %d" % limit
    return list(conn.execute(sql, params))


def estimate_cost(engines, n_prompts, samples):
    """--dry-run 用：粗估這一輪會花多少錢。"""
    rows = []
    total = 0.0
    for engine in engines:
        model = MODELS[engine]
        calls = n_prompts * samples
        per_call = (
            token_cost(model, EST_INPUT_TOKENS, EST_OUTPUT_TOKENS)
            + EST_SEARCHES_PER_CALL * WEB_SEARCH_COST_PER_SEARCH
        )
        subtotal = per_call * calls
        rows.append((engine, model, calls, per_call, subtotal))
        total += subtotal
    return rows, total


def warn_if_identical(collected):
    """檢查同一題的 N 則回答是不是全都一樣。

    為什麼要檢查：同一題問 N 次才算得出信賴區間。如果每次回答都一模一樣，
    區間會假性地收得很窄，看起來「數字非常穩定」，其實是採樣壞了
    （快取沒關、或模型被設成完全不隨機）。這是 CLAUDE.md 列的
    「看到就要停下來」的訊號之一，所以每次跑完自動檢查，不靠人記得看。

    collected: {(prompt_id, engine): [回答文字, ...]}
    """
    problems = []
    for (prompt_id, engine), answers in sorted(collected.items()):
        if len(answers) < 2:
            continue
        if len(set(answers)) == 1:
            problems.append((prompt_id, engine, len(answers)))
    if problems:
        print()
        print("⚠️  採樣可能有問題：下列題目的多次回答『完全一模一樣』")
        print("    這代表信賴區間會假性變窄，統計會失去意義。")
        print("    可能原因：該家開了 prompt caching、或模型被設成不隨機。")
        for prompt_id, engine, n in problems:
            print("      - prompt %s @ %s：%d 次回答完全相同" % (prompt_id, engine, n))
    return problems


# 每個引擎需要哪個 Python 套件
REQUIRED_PACKAGES = {
    "gemini": "google.genai",
    "openai": "openai",
    "anthropic": "anthropic",
    "perplexity": "requests",
}


def preflight(engines):
    """真的開始跑之前，一次把「缺什麼」全部查出來講清楚。

    刻意一次列出全部而不是遇到第一個就停：
    少一把金鑰跟少一個套件是兩件事，分兩次才發現很煩。
    回傳 (金鑰字典, 問題清單)。問題清單不是空的就不要往下跑。
    """
    keys = {}
    problems = []

    for engine in engines:
        # 套件裝了沒
        package = REQUIRED_PACKAGES[engine]
        try:
            __import__(package)
        except ImportError:
            problems.append(
                "[%s] 少了 Python 套件 %s\n"
                "      解法：pip install -r requirements.txt" % (engine, package)
            )
        # 金鑰有沒有
        try:
            keys[engine] = get_api_key(engine)
        except RuntimeError as e:
            problems.append("[%s] %s" % (engine, str(e).split("\n")[0] +
                                         "\n      解法：編輯 .env，填入 " +
                                         ENGINE_KEY_NAMES[engine]))
    return keys, problems


def collect(db_path, engines, samples, locale, condition, dry_run,
            limit, prompt_ids, max_cost, resume):
    """主要流程。"""
    conn = connect(db_path)
    prompts = load_prompts(conn, locale, limit, prompt_ids)

    if not prompts:
        print("prompts 表裡沒有 active=1 而且 locale=%s 的題目，沒事可做。" % locale)
        print("請先到「設定」頁匯入題庫，或檢查 --locale 是不是打錯了。")
        return

    year, week = iso_year_week()

    print("本次範圍：%d 題 × %d 家 × %d 次 = %d 次呼叫"
          % (len(prompts), len(engines), samples, len(prompts) * len(engines) * samples))
    print("存放週次：%d 年 第 %d 週　　量測條件：condition=%s · locale=%s"
          % (year, week, condition, locale))
    print()

    # ---- 花費預估（不管跑不跑真的，都先印出來讓人心裡有數）----
    rows, total_est = estimate_cost(engines, len(prompts), samples)
    print("預估花費（粗估，實際以各家帳單為準）")
    print("  %-12s %-20s %8s %12s %10s" % ("引擎", "模型", "呼叫次數", "每次約", "小計"))
    for engine, model, calls, per_call, subtotal in rows:
        print("  %-12s %-20s %8d %12s %10s"
              % (engine, model, calls, "$%.4f" % per_call, "$%.2f" % subtotal))
    print("  %-12s %-20s %8s %12s %10s" % ("", "", "", "合計", "$%.2f" % total_est))
    print()

    if total_est > max_cost:
        print("⛔ 預估花費 $%.2f 超過上限 $%.2f，停手不跑。" % (total_est, max_cost))
        print("   確定要跑的話，加 --max-cost 提高上限，例如：--max-cost %.0f"
              % (total_est * 1.5))
        return

    if dry_run:
        print("（--dry-run：以上都不會真的執行，沒有呼叫任何 API、沒有寫任何資料）")
        return

    # ---- 先確認每一家的套件和金鑰都在，不要跑到一半才發現少一把 ----
    keys, problems = preflight(engines)
    if problems:
        print("⛔ 還不能開始跑，有 %d 件事要先處理：" % len(problems))
        print()
        for i, problem in enumerate(problems, 1):
            print("  %d. %s" % (i, problem))
        print()
        print("處理完再跑一次就好。要先只測一家的話：--engine %s" % engines[0])
        return

    # ---- 真的開始跑 ----
    spent = 0.0
    ok_count = 0
    fail_count = 0
    skip_count = 0
    collected = {}   # (prompt_id, engine) -> [回答文字, ...]，跑完檢查變異性用
    started = time.time()

    # 每一家各自算「這一輪從第幾次開始」。
    # 分開算的原因：你可能某天只跑 Gemini、隔天跑兩家，
    # 各家的次數不見得一樣，各自接續才不會亂掉。
    start_index = {}
    for engine in engines:
        start_index[engine] = next_round(year, week, engine, locale)
    print("這一輪的編號：")
    for engine in engines:
        base = start_index[engine]
        if samples == 1:
            print("  %-12s 第 %d 次（本週這家先前已問過 %d 次）"
                  % (engine, base + 1, base))
        else:
            print("  %-12s 第 %d ~ %d 次（本週這家先前已問過 %d 次）"
                  % (engine, base + 1, base + samples, base))
    print()

    for prompt in prompts:
        for engine in engines:
            model = MODELS[engine]
            for k in range(samples):
                i = start_index[engine] + k
                rel = raw_path(year, week, engine, prompt["id"], locale, i)
                full = os.path.join(ROOT_DIR, rel)

                # 已經有檔案了 → 這一格跑過了，跳過（--resume 的核心）
                if resume and os.path.exists(full):
                    skip_count += 1
                    continue

                # 花費上限：每一次呼叫前都檢查，寧可少跑也不要爆預算
                if spent >= max_cost:
                    print()
                    print("⛔ 已花費 $%.2f，達到上限 $%.2f，停止。" % (spent, max_cost))
                    print("   已經跑完的部分都存好了，之後用 --resume 可以接著跑。")
                    _finish(collected, ok_count, fail_count, skip_count, spent, started)
                    return

                label = "[%s] prompt %-3s 第 %d 次" % (engine, prompt["id"], i + 1)
                try:
                    result = CALLERS[engine](prompt["text"], model, keys[engine])
                except Exception as e:
                    # 單一題失敗不中斷整批，把人看得懂的原因寫進 runs.error
                    msg = "%s：%s" % (type(e).__name__, e)
                    conn.execute(
                        "INSERT INTO runs (prompt_id, engine, model_version, locale, "
                        "condition, sample_index, ts, raw_path, cost, error) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (prompt["id"], engine, model, locale, condition, i,
                         now_iso(), None, 0, msg[:500]),
                    )
                    conn.commit()
                    fail_count += 1
                    print("  ✗ %s 失敗：%s" % (label, msg[:120]))
                    time.sleep(SLEEP_BETWEEN_CALLS)
                    continue

                # 原始回答：連同 metadata 一起存，之後重跑判讀時看得出當時的條件
                payload = {
                    "meta": {
                        "prompt_id": prompt["id"],
                        "prompt_text": prompt["text"],
                        "topic": prompt["topic"],
                        "intent": prompt["intent"],
                        "engine": engine,
                        "model": model,
                        "model_version": result["model_version"],
                        "locale": locale,
                        "condition": condition,
                        "sample_index": i,
                        "ts": now_iso(),
                        "cost": result["cost"],
                        "web_searches": result["searches"],
                    },
                    "answer_text": result["answer_text"],
                    "response": result["response"],
                }
                write_raw(rel, payload)

                conn.execute(
                    "INSERT INTO runs (prompt_id, engine, model_version, locale, "
                    "condition, sample_index, ts, raw_path, cost, error) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (prompt["id"], engine, result["model_version"], locale, condition,
                     i, now_iso(), rel, result["cost"], None),
                )
                conn.commit()

                spent += result["cost"]
                ok_count += 1
                collected.setdefault((prompt["id"], engine), []).append(result["answer_text"])

                # 搜尋次數 0 = web search 沒有真的被使用，答案是模型的記憶，數字沒有意義
                flag = "" if result["searches"] else "　⚠️ 這次沒有做任何網路搜尋"
                print("  ✓ %s　$%.4f　搜尋 %s 次　%d 字%s"
                      % (label, result["cost"], result["searches"],
                         len(result["answer_text"]), flag))

                time.sleep(SLEEP_BETWEEN_CALLS)

    _finish(collected, ok_count, fail_count, skip_count, spent, started)


def _finish(collected, ok_count, fail_count, skip_count, spent, started):
    """跑完之後的總結與自動檢查。"""
    elapsed = time.time() - started
    print()
    print("=" * 60)
    print("跑完了。成功 %d 筆，失敗 %d 筆，跳過 %d 筆（已存在）"
          % (ok_count, fail_count, skip_count))
    print("實際花費：$%.2f　　耗時：%d 分 %d 秒"
          % (spent, elapsed // 60, elapsed % 60))

    if fail_count:
        print()
        print("⚠️  有 %d 筆失敗。失敗的原因存在 runs.error，用這個看：" % fail_count)
        print("      python scripts/read_runs.py --errors")
        print("    算指標時這些會被排除在分母外，不會被當成「沒提到我們」。")

    warn_if_identical(collected)

    print()
    print("下一步：抽幾則出來讀，確認答案裡有引用連結（代表 web search 真的有開）")
    print("      python scripts/read_runs.py --latest 3")


def main():
    parser = argparse.ArgumentParser(
        description="每週去問各家 AI，原文存 gzip、metadata 進 runs 表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="建議的第一次使用順序：\n"
               "  1. python scripts/collect.py --dry-run              先看會花多少錢\n"
               "  2. python scripts/collect.py --limit 1 --samples 1  只試一題，確認接得通\n"
               "  3. python scripts/read_runs.py --latest 1           把回答讀出來看\n"
               "  4. python scripts/collect.py                        確認沒問題才正式跑\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--engine", choices=ENGINES, action="append",
                        help="只跑指定引擎，可重複給。不給就是預設的 %s。"
                             "（perplexity 目前預設不跑，要跑請明確指定）"
                             % "＋".join(DEFAULT_ENGINES))
    parser.add_argument("--samples", type=int, default=1,
                        help="這一次要問幾輪（預設 1，也就是每題每家各問一次）。"
                             "編號會自動接續本週先前跑過的，不會覆蓋。")
    parser.add_argument("--locale", default="tw", help="語系／地區（預設 tw）")
    parser.add_argument("--condition", default="api",
                        help="量測條件標籤，會顯示在畫面頂端（預設 api）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印出打算做什麼與預估花費，不呼叫任何 API、不寫任何資料")
    parser.add_argument("--limit", type=int,
                        help="只跑前幾題（第一次測試用，例如 --limit 1）")
    parser.add_argument("--prompt-id", type=int, action="append",
                        help="只跑指定題號，可重複給")
    parser.add_argument("--max-cost", type=float, default=30.0,
                        help="這一輪的花費上限（美金，預設 30）。超過就停手。")
    parser.add_argument("--no-resume", dest="resume", action="store_false",
                        help="不要跳過已經有檔案的題目。"
                             "（預設會跳過 —— 這是為了上次跑到一半中斷時能接著跑完）")
    parser.set_defaults(resume=True)
    args = parser.parse_args()

    engines = args.engine or DEFAULT_ENGINES
    print("=== collect.py ===")
    print()
    collect(args.db, engines, args.samples, args.locale, args.condition,
            args.dry_run, args.limit, args.prompt_id, args.max_cost, args.resume)


if __name__ == "__main__":
    main()
