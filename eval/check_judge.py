# -*- coding: utf-8 -*-
"""
check_judge.py — 拿判讀器的結果跟人工標準答案比對（階段 2 的品質關卡）

吃什麼 → 做什麼 → 吐什麼
  吃：eval/labeled.csv（人工盲測標記的標準答案）＋ mentions 表（parse.py 的產出）
  做：逐則比對，算一致率
  吐：一份報告。一致率 < 90% 就回傳非 0 的結束代碼（CI 可以擋）

怎麼跑
  python eval/check_judge.py              # 完整報告
  python eval/check_judge.py --errors     # 只看判錯的那些，附原文
  python eval/check_judge.py --threshold 0.9

═══════════════════════════════════════════════════════════════════════
為什麼需要這支
═══════════════════════════════════════════════════════════════════════

判讀器是整個系統唯一真正的品質關卡，也是最容易「安靜出錯」的地方 ——
它不會壞、不會報錯，就是默默判錯一成多，而不會有人發現，
因為沒有人會去讀那幾千則原文。

所以要有一把尺。這支就是那把尺。

**一致率 < 90% 不准往下走。** 不到就改判讀 prompt 再測，不要先做別的。

比對四件事，各自分開算 —— 因為它們會用不同的方式錯：
  1. 提及   這則有沒有提到某個品牌（最基本，錯了後面都不用談）
  2. 排序   提到的話排第幾
  3. 口氣   正面／中性／負面
  4. 風險   有沒有被標成風險提及

「提及」用的是 F1，不是單純的正確率 —— 因為漏掉一個品牌和多認一個品牌
是兩種不同的錯，正確率會把它們混在一起。
"""

import argparse
import csv
import gzip
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from common import DEFAULT_DB_PATH, connect  # noqa: E402

LABELED_CSV = os.path.join(ROOT, "eval", "labeled.csv")
MIN_LABELS = 100


def load_truth():
    """讀標準答案。回傳 {run_id: {品牌: {position, sentiment, risk}}}"""
    if not os.path.exists(LABELED_CSV):
        return None
    truth = defaultdict(dict)
    seen_runs = set()
    with open(LABELED_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = int(row["run_id"])
            seen_runs.add(rid)
            ent = (row.get("entity") or "").strip()
            if not ent:
                continue        # 「這則沒提到任何品牌」也是有效答案
            truth[rid][ent] = {
                "position": _int(row.get("position")),
                "sentiment": (row.get("sentiment_label") or "").strip(),
                "risk": row.get("risk_flag") in ("1", 1, True, "True"),
            }
    # 標過但沒提到品牌的 run 也要在字典裡（空的），否則會被當成沒標過
    for rid in seen_runs:
        truth.setdefault(rid, {})
    return dict(truth)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def load_predictions(conn, run_ids):
    """讀 parse.py 寫進 mentions 的判讀結果。"""
    if not run_ids:
        return {}
    marks = ",".join("?" * len(run_ids))
    rows = conn.execute(
        "SELECT run_id, entity, position, sentiment_label, risk_flag "
        "FROM mentions WHERE run_id IN (%s)" % marks, list(run_ids)).fetchall()
    pred = defaultdict(dict)
    for r in rows:
        pred[r["run_id"]][(r["entity"] or "").strip()] = {
            "position": r["position"],
            "sentiment": (r["sentiment_label"] or "").strip(),
            "risk": bool(r["risk_flag"]),
        }
    for rid in run_ids:
        pred.setdefault(rid, {})
    return dict(pred)


def compare(truth, pred):
    """逐則比對，算四個指標。"""
    tp = fp = fn = 0                      # 提及的對／多認／漏掉
    pos_ok = pos_n = 0                    # 排序
    sen_ok = sen_n = 0                    # 口氣
    rsk_ok = rsk_n = 0                    # 風險
    errors = []

    for rid, t in sorted(truth.items()):
        p = pred.get(rid, {})
        for ent in set(t) | set(p):
            in_t, in_p = ent in t, ent in p
            if in_t and in_p:
                tp += 1
                a, b = t[ent], p[ent]
                if a["position"] is not None and b["position"] is not None:
                    pos_n += 1
                    if a["position"] == b["position"]:
                        pos_ok += 1
                    else:
                        errors.append((rid, ent, "排序", a["position"], b["position"]))
                if a["sentiment"] and b["sentiment"]:
                    sen_n += 1
                    if a["sentiment"] == b["sentiment"]:
                        sen_ok += 1
                    else:
                        errors.append((rid, ent, "口氣", a["sentiment"], b["sentiment"]))
                rsk_n += 1
                if a["risk"] == b["risk"]:
                    rsk_ok += 1
                else:
                    errors.append((rid, ent, "風險", a["risk"], b["risk"]))
            elif in_p:
                fp += 1
                errors.append((rid, ent, "多認一個品牌", "沒提到", "判成有提到"))
            else:
                fn += 1
                errors.append((rid, ent, "漏掉一個品牌", "有提到", "判成沒提到"))

    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": prec, "recall": rec, "mention_f1": f1,
        "position": (pos_ok, pos_n, pos_ok / pos_n if pos_n else None),
        "sentiment": (sen_ok, sen_n, sen_ok / sen_n if sen_n else None),
        "risk": (rsk_ok, rsk_n, rsk_ok / rsk_n if rsk_n else None),
        "errors": errors,
    }


def pct(v):
    return "—" if v is None else "%.1f%%" % (v * 100)


def show_errors(conn, errors, limit):
    print()
    print("=== 判錯的項目（最多 %d 筆）===" % limit)
    print()
    cache = {}
    for rid, ent, kind, truth_v, pred_v in errors[:limit]:
        print("  run #%-5s %-14s %-16s 人工：%-10s 判讀器：%s"
              % (rid, ent[:14], kind, truth_v, pred_v))
        if rid not in cache:
            r = conn.execute("SELECT raw_path FROM runs WHERE id=?", (rid,)).fetchone()
            text = ""
            if r and r["raw_path"]:
                full = os.path.join(ROOT, r["raw_path"])
                if os.path.exists(full):
                    try:
                        with gzip.open(full, "rt", encoding="utf-8") as f:
                            d = json.load(f)
                        text = d.get("answer_text") or d.get("answer") or ""
                    except (OSError, ValueError):
                        pass
            cache[rid] = text
        snippet = cache[rid]
        if snippet and ent:
            i = snippet.find(ent)
            if i >= 0:
                lo, hi = max(0, i - 50), min(len(snippet), i + 70)
                print("        原文：…%s…" % snippet[lo:hi].replace("\n", " "))
        print()


def main():
    ap = argparse.ArgumentParser(
        description="拿判讀器的結果跟人工標準答案比對（一致率 < 90% 不准往下走）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--threshold", type=float, default=0.90, help="一致率門檻（預設 0.9）")
    ap.add_argument("--errors", action="store_true", help="列出判錯的項目與原文")
    ap.add_argument("--limit", type=int, default=25, help="--errors 最多列幾筆")
    args = ap.parse_args()

    truth = load_truth()
    if truth is None:
        print("找不到標準答案：%s" % LABELED_CSV)
        print()
        print("階段 2 的順序是：")
        print("  1. python eval/label.py          人工盲測標記 100 則")
        print("  2. 才寫 scripts/parse.py")
        print("  3. python eval/check_judge.py    拿這支驗")
        sys.exit(2)

    print("=== 判讀器驗收 ===")
    print()
    print("標準答案：%d 則（%s）" % (len(truth), LABELED_CSV))

    if len(truth) < MIN_LABELS:
        print()
        print("⚠️ 標準答案只有 %d 則，不到 %d 則。" % (len(truth), MIN_LABELS))
        print("   樣本太少的話，「一致率 92%%」本身的誤差就很大，驗不出什麼。")
        print("   繼續標：python eval/label.py")
        print()

    conn = connect(args.db)
    pred = load_predictions(conn, list(truth))
    if not any(pred.values()):
        print()
        print("mentions 表裡沒有這些 run 的判讀結果。")
        print("parse.py 還沒寫，或是還沒跑過。")
        print()
        print("（這是預期的 —— CLAUDE.md 訂的順序是先有標準答案，才寫 parse.py。）")
        sys.exit(2)

    m = compare(truth, pred)

    print()
    print("【提及】這則有沒有提到某個品牌")
    print("  對 %d 筆　多認 %d 筆　漏掉 %d 筆" % (m["tp"], m["fp"], m["fn"]))
    print("  精確率 %s（判成有提到的裡面，真的有提到的比例）" % pct(m["precision"]))
    print("  召回率 %s（真的有提到的裡面，被judge抓到的比例）" % pct(m["recall"]))
    print("  F1     %s  ← 這是主要指標" % pct(m["mention_f1"]))
    print()
    for label, key in [("排序", "position"), ("口氣", "sentiment"), ("風險", "risk")]:
        ok, n, rate = m[key]
        print("【%s】%s（%d / %d）" % (label, pct(rate), ok, n))

    if args.errors:
        show_errors(conn, m["errors"], args.limit)

    print()
    print("=" * 70)
    passed = m["mention_f1"] >= args.threshold
    sen_rate = m["sentiment"][2]
    sen_ok = sen_rate is None or sen_rate >= args.threshold
    if passed and sen_ok:
        print("✅ 通過（提及 F1 %s ≥ %s）" % (pct(m["mention_f1"]), pct(args.threshold)))
        print("   可以往階段 3 走了。")
        sys.exit(0)
    print("❌ 沒通過門檻 %s" % pct(args.threshold))
    if not passed:
        print("   提及 F1 只有 %s。" % pct(m["mention_f1"]))
        if m["fn"] > m["fp"]:
            print("   漏掉的比多認的多 —— 判讀 prompt 可能講得太嚴格，或品牌別名沒餵進去。")
        elif m["fp"] > m["fn"]:
            print("   多認的比漏掉的多 —— 判讀器把「順帶提到同類服務」也算成品牌提及了。")
    if not sen_ok:
        print("   口氣一致率只有 %s。" % pct(sen_rate))
        print("   ⚠️ 特別檢查：判讀器是不是對某一個品牌系統性地比較寬容？")
        print("      那正是盲測要防的事 —— 它不會報錯，只會讓數字一直偏好同一邊。")
    print()
    print("   改判讀 prompt 再測，不要先做別的。這是整個專案唯一真正的品質關卡。")
    print("   看判錯了什麼：python eval/check_judge.py --errors")
    sys.exit(1)


if __name__ == "__main__":
    main()
