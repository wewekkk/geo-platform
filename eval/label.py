# -*- coding: utf-8 -*-
"""
label.py — 人工盲測標記工具（階段 2 的第一步）

吃什麼 → 做什麼 → 吐什麼
  吃：runs 表裡有保留原文的紀錄
  做：一則一則顯示在畫面上，請人判斷提到哪些品牌、各排第幾、口氣如何
  吐：eval/labeled.csv（這就是標準答案）

怎麼跑
  python eval/label.py              # 從上次停的地方繼續標
  python eval/label.py --status     # 看標到第幾則了
  python eval/label.py --target 100 # 設定要標幾則（預設 100）
  python eval/label.py --review     # 重看已經標過的

═══════════════════════════════════════════════════════════════════════
⚠️ 為什麼要盲測 —— 這支工具存在的全部理由
═══════════════════════════════════════════════════════════════════════

標記的人**不可以知道哪個品牌是客戶**。

如果知道，標準答案本身就已經帶偏了 —— 模糊的提及會標成正面、
競品的好話會標成中性。而後面是拿這份答案去驗判讀器的，
等於用一把歪的尺去量，量出來的「一致率 92%」毫無意義。

所以這支工具做三件事：
  1. 把回答原文裡的**所有品牌名遮成代號**（品牌 A、品牌 B…）
  2. 代號每一則都重新洗牌 —— 同一個品牌在第 3 則是 A、第 7 則可能是 C
  3. 標記過程中**完全不顯示真實品牌名**

還原是在寫入 CSV 的那一刻才做的，標記的人看不到。

這同一條規則也適用於 parse.py：判讀器收到的是一份平等的品牌名單，
它從頭到尾不知道誰付錢。`is_us` 是寫進資料庫時才由程式標記的。

═══════════════════════════════════════════════════════════════════════

標完之後下一步
  python eval/check_judge.py --help   # 拿判讀結果跟這份標準答案比對
"""

import argparse
import csv
import gzip
import json
import os
import random
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from common import DEFAULT_DB_PATH, connect  # noqa: E402

LABELED_CSV = os.path.join(ROOT, "eval", "labeled.csv")

FIELDS = ["run_id", "prompt_id", "engine", "entity", "position",
          "sentiment_label", "risk_flag", "snippet", "labeled_at", "labeler_note"]

# 遮蔽用的代號。用「品牌 A」而不是「我們／競品 1」—— 後者本身就洩漏了身分。
MASKS = ["品牌 A", "品牌 B", "品牌 C", "品牌 D", "品牌 E",
         "品牌 F", "品牌 G", "品牌 H", "品牌 I", "品牌 J"]

SENTIMENTS = {"1": "positive", "2": "neutral", "3": "negative"}


def known_brands(conn):
    """目前已知的所有品牌名（含別名）。

    刻意從 mentions 表撈出全部 entity —— 不分我們和競品，一視同仁。
    這份清單只用來「遮蔽」，不帶任何身分資訊。
    """
    names = set()
    for r in conn.execute("SELECT DISTINCT entity FROM mentions WHERE entity IS NOT NULL"):
        names.add(r["entity"].strip())
    # 常見別名也要一起遮，不然遮了「Fluv 毛小愛」卻漏掉單獨出現的「毛小愛」
    extra = {"Fluv 毛小愛": ["毛小愛", "Fluv", "fluv"],
             "PetBacker": ["PetBacker", "Petbacker", "petbacker"],
             "PRO360 達人網": ["PRO360", "Pro360"],
             "maokiddo": ["maokiddo", "Maokiddo"]}
    out = {}
    for n in names:
        out[n] = sorted(set([n] + extra.get(n, [])), key=len, reverse=True)
    return out


def mask_text(text, brands, rng):
    """把原文裡所有品牌名換成隨機代號。

    回傳 (遮蔽後的文字, {代號: 真實品牌名})。
    只有在這則回答裡真的出現的品牌才會拿到代號 ——
    給一份包含沒出現品牌的清單，等於洩漏了「應該要找幾個」。
    """
    present = [n for n, aliases in brands.items()
               if any(a in text for a in aliases)]
    rng.shuffle(present)
    masks = MASKS[:len(present)]
    mapping = dict(zip(masks, present))

    masked = text
    # 長的別名先換，不然「Fluv」會先吃掉「Fluv 毛小愛」的一半
    pairs = []
    for mask, real in mapping.items():
        for alias in brands[real]:
            pairs.append((alias, mask))
    pairs.sort(key=lambda p: -len(p[0]))
    for alias, mask in pairs:
        masked = masked.replace(alias, mask)
    return masked, mapping


def load_done():
    if not os.path.exists(LABELED_CSV):
        return set()
    done = set()
    with open(LABELED_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            done.add(int(row["run_id"]))
    return done


def append_rows(rows):
    exists = os.path.exists(LABELED_CSV)
    os.makedirs(os.path.dirname(LABELED_CSV), exist_ok=True)
    with open(LABELED_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def candidates(conn, done):
    """可以標的 run：有原文、沒失敗、不是示範資料、還沒標過。"""
    rows = conn.execute(
        "SELECT r.id, r.prompt_id, r.engine, r.raw_path, p.text AS prompt_text "
        "FROM runs r JOIN prompts p ON p.id = r.prompt_id "
        "WHERE r.error IS NULL AND r.raw_path IS NOT NULL "
        "AND r.condition NOT IN ('demo', 'manual-tally') "
        "ORDER BY r.id").fetchall()
    out = []
    for r in rows:
        if r["id"] in done:
            continue
        full = os.path.join(ROOT, r["raw_path"])
        if not os.path.exists(full):
            continue
        try:
            with gzip.open(full, "rt", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            continue
        if "_demo" in payload:
            continue
        text = payload.get("answer_text") or payload.get("answer") or ""
        if len(text.strip()) < 50:
            continue
        out.append((r, text))
    return out


def ask(prompt_text, valid=None, allow_empty=True):
    while True:
        try:
            v = input(prompt_text).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not v and allow_empty:
            return ""
        if valid is None or v in valid:
            return v
        print("    請輸入 %s 其中之一。" % "／".join(valid))


def label_one(run, text, brands, rng, idx, target):
    masked, mapping = mask_text(text, brands, rng)
    if not mapping:
        return []    # 這則沒提到任何已知品牌，跳過

    print()
    print("=" * 76)
    print("第 %d / %d 則　（run #%s）" % (idx, target, run["id"]))
    print("=" * 76)
    print("【題目】%s" % run["prompt_text"])
    print()
    print("【AI 的回答】（品牌名已遮蔽）")
    print("-" * 76)
    print(masked)
    print("-" * 76)
    print()
    print("這則回答裡出現了這幾個品牌：%s" % "、".join(sorted(mapping)))
    print()
    print("請依照「在回答中第幾個被提到」的順序，逐一標記。")
    print("口氣：1=正面　2=中性　3=負面　　風險提及（負面且會影響信任）：y／留空")
    print("直接按 Enter 跳過某個品牌（代表它其實沒有被真的『提到』）。")
    print()

    rows = []
    for mask in sorted(mapping):
        print("  ── %s ──" % mask)
        pos = ask("     排第幾（數字，留空=跳過這個品牌）： ")
        if pos is None:
            return None            # 使用者中斷
        if not pos:
            continue
        if not pos.isdigit():
            print("     不是數字，跳過。")
            continue
        senti = ask("     口氣 1/2/3： ", valid=["1", "2", "3"], allow_empty=False)
        if senti is None:
            return None
        risk = ask("     風險提及？ y／留空： ")
        if risk is None:
            return None
        snippet = ask("     判定依據（貼上原文那一句，可留空）： ")
        if snippet is None:
            return None
        rows.append({
            "run_id": run["id"], "prompt_id": run["prompt_id"],
            "engine": run["engine"],
            # ⚠️ 還原真實品牌名是在這一刻才做的，標記的人看不到
            "entity": mapping[mask],
            "position": pos,
            "sentiment_label": SENTIMENTS[senti],
            "risk_flag": 1 if risk.lower() == "y" else 0,
            "snippet": snippet,
            "labeled_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "labeler_note": "",
        })
    # 整則都沒提到任何品牌也是一種有效答案，要留一筆空的，
    # 不然之後分不出「標過但沒提到」和「還沒標」
    if not rows:
        rows.append({
            "run_id": run["id"], "prompt_id": run["prompt_id"],
            "engine": run["engine"], "entity": "", "position": "",
            "sentiment_label": "", "risk_flag": 0, "snippet": "",
            "labeled_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "labeler_note": "這則沒有提到任何品牌",
        })
    return rows


def show_status(conn):
    done = load_done()
    pool = candidates(conn, set())
    print("標準答案：%s" % LABELED_CSV)
    print("  已標記  %d 則" % len(done))
    print("  可標記  %d 則（有原文、非示範資料、非失敗）" % len(pool))
    print()
    if len(done) >= 100:
        print("✅ 已經達到 100 則。可以開始寫 parse.py 了。")
        print("   寫完之後用這個驗：python eval/check_judge.py")
    else:
        print("⚠️ 還差 %d 則才到 100。" % (100 - len(done)))
        print("   CLAUDE.md 訂的順序：先有標準答案，才寫 parse.py。")
        if len(pool) < 100 - len(done):
            print()
            print("   ⚠️ 可標記的池子只有 %d 則，不夠標到 100。" % len(pool))
            print("      要先讓 collect.py 跑起來、累積更多有原文的回答。")


def main():
    ap = argparse.ArgumentParser(
        description="人工盲測標記工具 —— 產出 eval/labeled.csv（標準答案）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--target", type=int, default=100, help="要標幾則（預設 100）")
    ap.add_argument("--status", action="store_true", help="只看進度")
    ap.add_argument("--seed", type=int, help="固定洗牌亂數（只在重現問題時用）")
    args = ap.parse_args()

    conn = connect(args.db)
    if args.status:
        show_status(conn)
        return

    brands = known_brands(conn)
    if not brands:
        print("mentions 表裡沒有任何品牌，沒辦法做遮蔽。")
        print("先讓 collect.py 跑起來、並且至少有一批判讀結果。")
        return

    done = load_done()
    pool = candidates(conn, done)
    if not pool:
        print("沒有可以標的紀錄了。")
        show_status(conn)
        return

    rng = random.Random(args.seed)
    print("=== 人工盲測標記 ===")
    print()
    print("⚠️ 畫面上的品牌名全部都是代號，而且每一則重新洗牌。")
    print("   這是刻意的 —— 標記的人知道誰是客戶的話，標準答案本身就帶偏了。")
    print()
    print("已標 %d 則，目標 %d 則。隨時按 Ctrl+C 結束，進度會保留。" % (len(done), args.target))

    n = len(done)
    for run, text in pool:
        if n >= args.target:
            break
        rows = label_one(run, text, brands, rng, n + 1, args.target)
        if rows is None:
            print()
            print("已中斷。已標記的 %d 則都存好了。" % n)
            break
        if rows:
            append_rows(rows)
            n += 1
    else:
        print()
        print("可標記的紀錄全部標完了。")

    print()
    show_status(conn)


if __name__ == "__main__":
    main()
