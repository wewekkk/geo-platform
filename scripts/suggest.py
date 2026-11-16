# -*- coding: utf-8 -*-
"""
suggest.py — 從真實資料裡撈出「題庫還沒涵蓋」的題目，提議給人決定

吃什麼 → 做什麼 → 吐什麼
  吃：① 已存下來的 AI 回答原文　② Google Search Console 查詢字（未接）
      ③ 站內搜尋紀錄（未接）　④ 現有題庫（用來去重）
  做：找出反覆出現、但題庫沒涵蓋的概念，估一個重要度
  吐：prompt_suggestions（status='pending'，等人決定加入或忽略）

怎麼跑
  python scripts/suggest.py --preview        # 看會提議什麼，不寫進資料庫
  python scripts/suggest.py                  # 寫進 prompt_suggestions
  python scripts/suggest.py --limit 30
  python scripts/suggest.py --gsc <CSV 檔>    # 吃 GSC 匯出的 CSV（手動下載的那種）

⚠️ 絕對不自己寫進 prompts 表
  這支只寫 prompt_suggestions，狀態一律 pending。
  加不加由人在「設定 → 題目建議佇列」決定。

═══════════════════════════════════════════════════════════════════════
這支存在的理由：題庫是內部人寫的，就會量錯東西
═══════════════════════════════════════════════════════════════════════

CLAUDE.md 把這列為會「安靜錯掉」的風險之一：

  題庫是內部人寫的 → 每個數字都很精準，但量錯了東西

在辦公室想出來的題目句子都很工整，但真實客戶打的字不是那樣。
量錯題目的話，後面所有數字都很漂亮、卻回答了沒人問的問題。

三個來源裡，目前只有第三個不需要外部授權：

  ① GSC 查詢字      需要 Google Search Console 授權（階段 5 才接）
                    → 現在支援手動匯出 CSV 餵進來（--gsc）
  ② 站內搜尋紀錄    需要 Fluv 後台提供（還沒有）
  ③ AI 回答裡的新概念  ✅ 現在就能做 —— 掃 data/raw/ 的原文

第三個的邏輯：AI 在回答我們的題目時，常常會順帶講到我們沒問過的東西
（例如反覆提到「寵物計程車」「術後照護」）。那些是客戶真的在關心、
而我們題庫沒涵蓋的面向。
"""

import argparse
import csv
import gzip
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH, ROOT_DIR, connect, now_iso  # noqa: E402

# 一個概念至少要在幾則不同的回答裡出現，才值得提議
MIN_DOC_FREQ = 3

# 候選詞的長度（中文字）。超過 6 個字的多半是整串斷句，不是一個概念。
MIN_TERM_LEN, MAX_TERM_LEN = 3, 6

# 這些詞出現得多但沒有資訊量，不要提議
STOPWORDS = set("""
寵物 保姆 保母 毛孩 飼主 服務 平台 可以 需要 建議 選擇 提供 使用 進行 相關
如果 因為 所以 這些 那些 比較 通常 一般 常見 注意 事項 問題 方式 方法 情況
時候 之後 之前 可能 必須 以及 或是 等等 例如 包括 其他 各種 不同 主要 台灣
我們 他們 您的 你的 一個 一些 真的 非常 目前 現在 實際 建立 確認 了解 知道
推薦 評價 資訊 內容 詳細 完整 專業 安心 貼心 優質
告訴 幫你 哪一 哪些 這樣 那樣 一定 不一 直接 期間 範圍 方面 部分 以上 以下
如下 下列 上述 如何 什麼 怎麼 為何 是否 以及 另外 此外 其中 由於 因此 總之
""".split())

# 這些字出現在候選詞裡，代表切點抓錯了（抓到斷句碎片而不是一個概念）
FRAGMENT_MARKERS = set("的了是在或與和要想會可能但而就都也還更最及等由被將把從對於"
                       "有無需含按上下前後個筆位些該此其並則若即約共每")

# 候選詞的長度（中文字）
MIN_TERM_LEN, MAX_TERM_LEN = 3, 6


def _ngrams(text):
    """把一段中文切成所有 3~6 字的連續片段。

    刻意不裝中文斷詞套件（jieba 之類）——
    這裡要的訊號是「跨文件重複」，不是精確斷詞。
    一個真的概念（「寵物計程車」）會在好幾則不同的回答裡以完全一樣的字串出現；
    一段斷句碎片（「母需透過實名」）不會。重複本身就是過濾器。
    """
    out = set()
    # 先用標點切成句子，n-gram 不要跨句
    for seg in re.split(r"[^\u4e00-\u9fff]+", text):
        for n in range(MIN_TERM_LEN, MAX_TERM_LEN + 1):
            for i in range(len(seg) - n + 1):
                term = seg[i:i + n]
                if term in STOPWORDS:
                    continue
                if any(ch in FRAGMENT_MARKERS for ch in term):
                    continue
                # 含有任何停用詞（不只開頭結尾）→ 切點抓錯了
                if any(w in term for w in STOPWORDS):
                    continue
                out.add(term)
    return out


def _drop_subsumed(doc_freq):
    """短詞如果總是被長詞包住、而且出現次數一樣，就只留長的。

    例如「寵物計程」和「寵物計程車」都出現 5 次 —— 留後者就好。
    """
    terms = sorted(doc_freq, key=len, reverse=True)
    keep = {}
    for t in terms:
        # 長詞的次數達到短詞的 70% 就留長的 ——
        # 「到府照」11 則 vs「到府照顧」10 則，該留的是後者。
        #
        # 試過放寬到 40%，結果更糟：會連帶砍掉「保險公司」「國泰產險」
        # 這類真概念，碎片反而留著。0.7 是實測下來比較平衡的值。
        covered = any(t != k and t in k and doc_freq[k] >= doc_freq[t] * 0.7
                      for k in keep)
        if not covered:
            keep[t] = doc_freq[t]
    return Counter(keep)


def iter_answers(conn):
    """逐則吐出有原文的回答（排除示範資料與失敗的）。"""
    rows = conn.execute(
        "SELECT id, raw_path FROM runs "
        "WHERE error IS NULL AND raw_path IS NOT NULL "
        "AND condition NOT IN ('demo')")
    for r in rows:
        full = os.path.join(ROOT_DIR, r["raw_path"])
        if not os.path.exists(full):
            continue
        try:
            with gzip.open(full, "rt", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if "_demo" in d:
            continue
        text = d.get("answer_text") or d.get("answer") or ""
        if text.strip():
            yield r["id"], text


def existing_terms(conn):
    """現有題庫的字詞，拿來去重。"""
    terms = set()
    for r in conn.execute("SELECT text FROM prompts"):
        t = r["text"] or ""
        for n in (2, 3, 4, 5):
            for i in range(len(t) - n + 1):
                terms.add(t[i:i + n])
    for r in conn.execute("SELECT text FROM prompt_suggestions"):
        terms.add((r["text"] or "").strip())
    return terms


def concepts_from_answers(conn):
    """從 AI 回答原文裡抽出反覆出現的概念。

    用「出現在幾則不同的回答裡」（document frequency）而不是總次數 ——
    一則回答把同一個詞講十次，不代表那是普遍關心的事。

    ⚠️ 這個來源需要量才有用，而且本來就會夾雜雜訊。

       中文沒有詞界，不裝斷詞套件的話一定會抽到一些被切斷的片段
       （「透過平」其實是「透過平台預訂」）。實測在 16 則樣本下，
       大約六成是真概念（自負額、國泰產險、實名認證、不予理賠…），
       四成是碎片。樣本越多，真概念重複得越明顯，比例會越好。

       所以提議出來的句子一律標成「［待改寫］」—— 真正的訊號是
       「這個概念反覆出現但題庫沒涵蓋」，不是那個機械組出來的句子。
       系統提議，人決定。
    """
    doc_freq = Counter()
    examples = {}
    n_docs = 0
    for run_id, text in iter_answers(conn):
        n_docs += 1
        terms = _ngrams(text)
        for term in terms:
            doc_freq[term] += 1
            if term not in examples:
                i = text.find(term)
                examples[term] = text[max(0, i - 40):i + 60].replace("\n", " ").strip()
    return _drop_subsumed(doc_freq), examples, n_docs


def load_gsc(path):
    """吃 Google Search Console 匯出的 CSV。

    GSC 的介面可以手動匯出「查詢」報表。欄位通常是
    Query / Clicks / Impressions / CTR / Position（中文介面是 查詢/點擊/曝光/...）。
    接 API 是階段 5 的事，在那之前手動匯出就能用。
    """
    if not os.path.exists(path):
        print("找不到 GSC 匯出檔：%s" % path)
        return []
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            low = {(k or "").strip().lower(): v for k, v in row.items()}
            q = (low.get("query") or low.get("查詢") or
                 low.get("top queries") or low.get("熱門查詢") or "").strip()
            if not q:
                continue
            imp = _num(low.get("impressions") or low.get("曝光") or low.get("曝光總數"))
            clk = _num(low.get("clicks") or low.get("點擊") or low.get("點擊次數"))
            rows.append((q, imp, clk))
    return rows


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def suggest(db_path, limit, preview, gsc_path):
    conn = connect(db_path)
    known = existing_terms(conn)
    out = []

    # ---- 來源 ③：AI 回答裡的新概念（現在就能做）----
    doc_freq, examples, n_docs = concepts_from_answers(conn)
    if n_docs == 0:
        print("⚠️ 沒有任何有原文的回答可以掃。")
        print("   九月那批是人工統計，只記了「有沒有被提到」，沒有原文。")
        print("   等 collect.py 透過 API 跑起來，這個來源才會有東西。")
    else:
        print("掃了 %d 則有原文的回答，抽出 %d 個候選概念。" % (n_docs, len(doc_freq)))

    for term, freq in doc_freq.most_common():
        if freq < MIN_DOC_FREQ:
            continue
        if term in known:
            continue                       # 題庫已經涵蓋了
        share = freq / n_docs
        out.append({
            # ⚠️ 這句是機械組出來的，一定要人改寫。
            #    真正的訊號是「這個概念反覆出現但題庫沒涵蓋」，不是這個句子。
            "text": "［待改寫］關於「%s」，客戶會怎麼問？" % term,
            "source": "ai-answers",
            "evidence": "「%s」出現在 %d / %d 則回答裡（%.0f%%），但題庫沒有涵蓋。\n原文片段：…%s…"
                        % (term, freq, n_docs, share * 100, examples.get(term, "")),
            "score": share * 100,
        })

    # ---- 來源 ①：GSC（手動匯出的 CSV）----
    if gsc_path:
        gsc = load_gsc(gsc_path)
        print("GSC 匯出檔讀到 %d 列查詢。" % len(gsc))
        for q, imp, clk in gsc:
            if q in known or any(q in k for k in known):
                continue
            # 曝光多但點擊少 = 搜尋結果裡看得到我們，但沒人點 ——
            # 那通常代表「我們的頁面跟這個問題對不上」，正是該補的題目
            ctr = (clk / imp) if imp else 0
            if imp < 10:
                continue
            out.append({
                "text": q if q.endswith(("?", "？")) else q + "？",
                "source": "gsc",
                "evidence": "GSC：曝光 %.0f 次、點擊 %.0f 次（CTR %.1f%%）。"
                            "曝光多但點擊少，代表現有頁面跟這個問題對不上。"
                            % (imp, clk, ctr * 100),
                "score": imp / 100.0 * (1 - ctr),
            })
    else:
        print()
        print("（沒給 --gsc，略過 Google Search Console 這個來源。）")
        print("  GSC 是最有價值的來源 —— 那是真人實際打進去的字，")
        print("  比在辦公室想出來的題目可靠。")
        print("  在 Search Console 的「成效 → 查詢」匯出 CSV，再用 --gsc <檔案> 餵進來。")

    # ---- 來源 ②：站內搜尋（還沒有資料來源）----
    # TODO（階段 5）：接 Fluv 後台的站內搜尋紀錄。
    #   找「被搜很多次但站上沒有對應內容」的問題 —— 那是最直接的缺口訊號。
    #   需要後台提供匯出，目前拿不到。

    out.sort(key=lambda x: -x["score"])
    out = out[:limit]

    if not out:
        print()
        print("沒有可以提議的新題目。")
        return

    print()
    print("=== 提議新增 %d 題 ===" % len(out))
    print()
    for i, s in enumerate(out, 1):
        print("  %d. %s" % (i, s["text"]))
        print("     來源 %s　重要度 %.1f" % (s["source"], s["score"]))
        for line in s["evidence"].split("\n"):
            print("     %s" % line)
        print()

    print("⚠️ 這些只是提議。題目句子是機械組出來的，一定要人改寫成客戶真的會打的字。")
    print("   而且開始量測之後題庫就不能隨便改 —— 加題目前先想清楚。")

    if preview:
        print()
        print("（--preview：沒有寫進資料庫）")
        return

    added = 0
    for s in out:
        dup = conn.execute("SELECT 1 FROM prompt_suggestions WHERE text=?",
                           (s["text"],)).fetchone()
        if dup:
            continue
        conn.execute(
            "INSERT INTO prompt_suggestions (text, source, evidence, est_importance, "
            "status, created_at) VALUES (?,?,?,?,?,?)",
            (s["text"], s["source"], s["evidence"], s["score"], "pending", now_iso()))
        added += 1
    conn.commit()
    print()
    print("✅ 寫入 prompt_suggestions %d 筆（status='pending'）。" % added)
    print("   到「設定 → 題目建議佇列」決定要加入還是忽略。")
    print("   ⚠️ 這支絕對不會自己寫進 prompts 表。")


def main():
    ap = argparse.ArgumentParser(
        description="從真實資料裡撈出題庫沒涵蓋的題目，提議給人決定",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/suggest.py --preview\n"
               "  python scripts/suggest.py --gsc ~/Downloads/queries.csv\n")
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--limit", type=int, default=20, help="最多提議幾題（預設 20）")
    ap.add_argument("--preview", action="store_true", help="只印出來，不寫進資料庫")
    ap.add_argument("--gsc", metavar="CSV", help="Google Search Console 匯出的查詢 CSV")
    args = ap.parse_args()

    print("=== suggest.py ===")
    print()
    suggest(args.db, args.limit, args.preview, args.gsc)


if __name__ == "__main__":
    main()
