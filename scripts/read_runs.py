# -*- coding: utf-8 -*-
"""
read_runs.py — 把存下來的原始回答讀出來看

吃什麼 → 做什麼 → 吐什麼
  吃：runs 表 ＋ data/raw/ 底下的 gzip 檔
  做：列出跑過哪些、把指定的那幾則原文印在畫面上
  吐：純文字（給人看的，不寫任何資料）

這支腳本存在的理由
  階段 1 的驗收條件是「使用者能把原文讀出來」。
  更重要的是：整個系統之後的數字都是從這些原文算出來的，
  如果原文本身有問題（例如 web search 沒開、回答被截斷），
  越早發現越好 —— 而唯一的發現方法就是真的把它讀出來。

怎麼跑
  python scripts/read_runs.py                      # 總覽：跑過幾批、各家幾筆
  python scripts/read_runs.py --list               # 列出最近 30 筆
  python scripts/read_runs.py --latest 3           # 讀最近 3 則的完整原文
  python scripts/read_runs.py --run-id 42          # 讀第 42 筆的完整原文
  python scripts/read_runs.py --errors             # 只看失敗的那些
  python scripts/read_runs.py --engine openai --latest 1
  python scripts/read_runs.py --check              # 健檢：抽驗 web search 有沒有真的開

這支腳本只讀不寫，怎麼跑都不會弄壞資料。
"""

import argparse
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH, ROOT_DIR, connect  # noqa: E402

# 橫線寬度，純粹讓畫面好讀
WIDTH = 72


def read_raw(rel_path):
    """讀出一個 gzip 原始檔。讀不到就回 None（不要讓整支腳本死掉）。"""
    if not rel_path:
        return None
    full = os.path.join(ROOT_DIR, rel_path)
    if not os.path.exists(full):
        return None
    try:
        with gzip.open(full, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("  （這個檔案讀不出來：%s —— %s）" % (rel_path, e))
        return None


def answer_of(payload):
    """取出回答文字。

    兩種檔案格式都要讀得到：
      - collect.py 寫的真實資料用 answer_text
      - seed_demo.py 寫的示範資料用 answer
    """
    return payload.get("answer_text") or payload.get("answer") or ""


def is_demo(payload):
    """這則是不是示範資料（seed_demo.py 產生的假回答）。"""
    return "_demo" in payload


def find_links(payload):
    """從一則回答裡把引用連結抓出來。

    為什麼要抓：這是驗證「web search 有沒有真的開」最快的方法。
    答案裡完全沒有任何連結，多半代表問到的是模型的記憶，不是查來的資料 ——
    這種情況數字看起來完全正常，意義卻是錯的。
    """
    urls = []

    # 1. 回答文字裡直接出現的網址
    text = answer_of(payload)
    urls += re.findall(r"https?://[^\s\)\]\}，。、；？！\"']+", text)

    # 2. 各家把來源放在回應結構的不同位置，整包轉成字串一起撈，
    #    這樣三家不用各寫一套解析（正式的來源解析是階段 2 parse.py 的工作）
    blob = json.dumps(
        payload.get("response") or payload.get("citations") or {}, ensure_ascii=False)
    # 各家的欄位名不一樣：OpenAI／Perplexity 用 "url"，Gemini 的 grounding 用 "uri"
    urls += re.findall(r'"(?:url|uri)"\s*:\s*"(https?://[^"]+)"', blob)

    # 去重，保持原順序
    seen, out = set(), []
    for u in urls:
        u = u.rstrip(".,)]}")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def show_overview(conn):
    """總覽：跑過哪幾批、各家幾筆、花了多少。"""
    rows = list(conn.execute(
        "SELECT condition, engine, COUNT(*) n, "
        "       SUM(CASE WHEN error IS NULL THEN 1 ELSE 0 END) ok, "
        "       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) bad, "
        "       ROUND(SUM(cost), 2) cost, MIN(ts) first_ts, MAX(ts) last_ts "
        "FROM runs GROUP BY condition, engine ORDER BY condition, engine"
    ))
    if not rows:
        print("runs 表是空的 —— 還沒有跑過任何一批。")
        print("先跑：python scripts/collect.py --dry-run")
        return

    print("=== 跑批總覽 ===")
    print()
    print("  %-10s %-12s %6s %6s %6s %9s  %s"
          % ("條件", "引擎", "總數", "成功", "失敗", "花費", "最後一次"))
    print("  " + "-" * (WIDTH + 8))
    for r in rows:
        print("  %-10s %-12s %6d %6d %6d %9s  %s"
              % (r["condition"], r["engine"], r["n"], r["ok"], r["bad"],
                 "$%.2f" % (r["cost"] or 0), r["last_ts"]))
    print()

    # 原始檔在不在，是資料能不能重跑的根本，所以每次都報一下
    total, missing = 0, 0
    for r in conn.execute("SELECT raw_path FROM runs WHERE raw_path IS NOT NULL"):
        total += 1
        if not os.path.exists(os.path.join(ROOT_DIR, r["raw_path"])):
            missing += 1
    print("  原始檔案：資料庫記錄 %d 筆，實際存在 %d 筆" % (total, total - missing))
    if missing:
        print("  ⚠️  有 %d 筆的原始檔不見了。原始回答是這個專案唯一不可重建的資產，" % missing)
        print("      判讀邏輯之後要改的話，沒有原文就沒辦法重算歷史數字。")
    print()
    print("接下來可以：")
    print("  python scripts/read_runs.py --list       列出最近幾筆")
    print("  python scripts/read_runs.py --latest 1   把最新一則原文讀出來")
    print("  python scripts/read_runs.py --check      抽驗 web search 有沒有真的開")


def show_list(conn, where, params, limit):
    """列出符合條件的 runs（表格）。"""
    rows = list(conn.execute(
        "SELECT r.id, r.prompt_id, r.engine, r.model_version, r.condition, "
        "       r.sample_index, r.ts, r.cost, r.error, p.text "
        "FROM runs r LEFT JOIN prompts p ON p.id = r.prompt_id "
        "%s ORDER BY r.id DESC LIMIT %d" % (where, limit), params))
    if not rows:
        print("沒有符合條件的紀錄。")
        return
    print("  %-6s %-12s %-6s %-5s %-20s %s" % ("run", "引擎", "題號", "第幾次", "時間", "題目"))
    print("  " + "-" * (WIDTH + 20))
    for r in rows:
        mark = "✗" if r["error"] else " "
        text = (r["text"] or "（題目已刪除）")
        if len(text) > 28:
            text = text[:28] + "…"
        print("  %s%-5s %-12s %-6s %-5s %-20s %s"
              % (mark, r["id"], r["engine"], r["prompt_id"],
                 r["sample_index"], r["ts"], text))
    print()
    print("  用 python scripts/read_runs.py --run-id <run> 讀完整原文")


def show_one(conn, run_id):
    """把一筆 run 的完整原文印出來。"""
    r = conn.execute(
        "SELECT r.*, p.text AS prompt_text, p.topic, p.intent "
        "FROM runs r LEFT JOIN prompts p ON p.id = r.prompt_id WHERE r.id = ?",
        (run_id,)).fetchone()
    if not r:
        print("找不到 run id = %s" % run_id)
        return

    print("=" * WIDTH)
    print("run #%s　%s　%s　第 %s 次"
          % (r["id"], r["engine"], r["model_version"] or "（沒記版本）", r["sample_index"]))
    print("時間 %s　條件 condition=%s · locale=%s　花費 $%.4f"
          % (r["ts"], r["condition"], r["locale"], r["cost"] or 0))
    print("=" * WIDTH)
    print()
    print("【題目】%s" % (r["prompt_text"] or "（題目已刪除）"))
    if r["topic"]:
        print("　　　　主題：%s　意圖：%s" % (r["topic"], r["intent"]))
    print()

    if r["error"]:
        print("【這次失敗了】")
        print(r["error"])
        print()
        print("（失敗的 run 沒有原文。算指標時會被排除在分母外，")
        print("  不會被當成「沒提到我們」而讓能見度憑空變低。）")
        return

    payload = read_raw(r["raw_path"])
    if payload is None:
        print("【讀不到原始檔】%s" % r["raw_path"])
        print("檔案不存在。原始回答是不可重建的資產，這筆已經救不回來了。")
        return

    if is_demo(payload):
        print("⚠️  這是示範資料，不是真的 AI 回答（seed_demo.py 產生的）")
        print()

    print("【AI 的完整回答】")
    print("-" * WIDTH)
    print(answer_of(payload) or "（回答是空的）")
    print("-" * WIDTH)
    print()

    links = find_links(payload)
    meta = payload.get("meta") or {}
    searches = meta.get("web_searches")
    if searches is None and is_demo(payload):
        searches = "（示範資料不適用）"
    print("【這次做了幾次網路搜尋】%s" % (searches if searches is not None else "（沒記錄）"))
    print("【回答裡出現的來源連結】%d 個" % len(links))
    for u in links[:15]:
        print("    %s" % u)
    if len(links) > 15:
        print("    …另外還有 %d 個" % (len(links) - 15))

    if not links and not searches and not is_demo(payload):
        print()
        print("  ⚠️  這則回答沒有任何搜尋、也沒有任何來源連結。")
        print("      這通常代表 web search 沒有真的被開啟 ——")
        print("      問到的是模型的記憶，不是會查資料的助理。數字會看起來正常但沒有意義。")

    print()
    print("（原始檔：%s）" % r["raw_path"])


def show_check(conn, sample_size):
    """健檢：抽驗最近的幾則，看 web search 是不是真的有開。"""
    rows = list(conn.execute(
        "SELECT id, engine, raw_path FROM runs "
        "WHERE error IS NULL AND raw_path IS NOT NULL "
        "ORDER BY id DESC LIMIT ?", (sample_size,)))
    if not rows:
        print("還沒有成功的紀錄可以檢查。")
        return

    print("=== web search 健檢：抽驗最近 %d 則 ===" % len(rows))
    print()
    print("為什麼要查這個：web search 沒開的話，AI 回答的是它的記憶，不是查來的資料。")
    print("這種情況畫面上的數字會完全正常，但意義是錯的 —— 只能靠讀原文發現。")
    print()
    print("  %-6s %-12s %8s %8s  %s" % ("run", "引擎", "搜尋次數", "來源連結", "判斷"))
    print("  " + "-" * WIDTH)

    bad = []
    checked = 0
    for r in rows:
        payload = read_raw(r["raw_path"])
        if payload is None:
            print("  %-6s %-12s %8s %8s  讀不到原始檔" % (r["id"], r["engine"], "-", "-"))
            continue
        if is_demo(payload):
            print("  %-6s %-12s %8s %8s  （示範資料，跳過）"
                  % (r["id"], r["engine"], "-", "-"))
            continue
        checked += 1
        searches = (payload.get("meta") or {}).get("web_searches")
        links = find_links(payload)
        ok = bool(links) or bool(searches)
        if not ok:
            bad.append(r["id"])
        print("  %-6s %-12s %8s %8d  %s"
              % (r["id"], r["engine"], searches if searches is not None else "?",
                 len(links), "✓ 有查資料" if ok else "✗ 疑似沒開 web search"))

    print()
    if checked == 0:
        print("（抽到的全部都是示範資料，等於沒有檢查到任何真實回答。）")
        print("先跑一次真的蒐集，再回來做這個健檢：")
        print("    python scripts/collect.py --limit 1 --samples 1")
    elif bad:
        print("⛔ 檢查了 %d 則真實回答，其中 %d 則沒有任何搜尋痕跡：run %s"
              % (checked, len(bad), ", ".join(map(str, bad))))
        print("   請先確認那一家的 web search 工具有正確開啟，再繼續往下做。")
    else:
        print("✓ 檢查了 %d 則真實回答，每一則都有查資料的痕跡，web search 是開著的。" % checked)


def main():
    parser = argparse.ArgumentParser(
        description="把存下來的原始回答讀出來看（只讀不寫）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/read_runs.py --latest 3\n"
               "  python scripts/read_runs.py --run-id 42\n"
               "  python scripts/read_runs.py --check\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--list", action="store_true", help="列出最近的紀錄（表格）")
    parser.add_argument("--latest", type=int, metavar="N", help="讀最近 N 則的完整原文")
    parser.add_argument("--run-id", type=int, action="append", help="讀指定 run 的完整原文，可重複給")
    parser.add_argument("--errors", action="store_true", help="只看失敗的那些")
    parser.add_argument("--engine", help="只看某一家")
    parser.add_argument("--prompt-id", type=int, help="只看某一題")
    parser.add_argument("--condition", help="只看某個量測條件（例如 api 或 demo）")
    parser.add_argument("--check", action="store_true", help="健檢：抽驗 web search 有沒有真的開")
    parser.add_argument("--limit", type=int, default=30, help="--list 最多列幾筆（預設 30）")
    args = parser.parse_args()

    conn = connect(args.db)

    # 組出共用的篩選條件
    clauses, params = [], []
    if args.errors:
        clauses.append("r.error IS NOT NULL")
    if args.engine:
        clauses.append("r.engine = ?")
        params.append(args.engine)
    if args.prompt_id:
        clauses.append("r.prompt_id = ?")
        params.append(args.prompt_id)
    if args.condition:
        clauses.append("r.condition = ?")
        params.append(args.condition)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    if args.check:
        show_check(conn, args.limit)
        return

    if args.run_id:
        for rid in args.run_id:
            show_one(conn, rid)
            print()
        return

    if args.latest:
        rows = list(conn.execute(
            "SELECT r.id FROM runs r %s ORDER BY r.id DESC LIMIT %d"
            % (where, args.latest), params))
        if not rows:
            print("沒有符合條件的紀錄。")
            return
        for r in rows:
            show_one(conn, r["id"])
            print()
        return

    if args.list or clauses:
        show_list(conn, where, params, args.limit)
        return

    show_overview(conn)


if __name__ == "__main__":
    main()
