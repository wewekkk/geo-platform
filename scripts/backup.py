# -*- coding: utf-8 -*-
"""
backup.py — 把 data/ 打包成一個備份檔

吃什麼 → 做什麼 → 吐什麼
  吃：data/geo.db ＋ data/raw/
  做：壓成一個 .tar.gz，檔名帶日期
  吐：backups/geo-data-YYYYMMDD-HHMM.tar.gz

為什麼需要這支腳本
  data/ 不在版控裡（.gitignore 排除），而且它是整個專案唯一
  不可重建的資產 —— 程式壞了可以重寫，原始回答刪了就永遠沒有了。
  CLAUDE.md 的上線檢查清單有這一條。

怎麼跑
  python scripts/backup.py              # 備份到 backups/
  python scripts/backup.py --out ~/某處   # 備份到別的地方（建議放雲端同步資料夾）
  python scripts/backup.py --list        # 看目前有哪些備份
  python scripts/backup.py --verify <檔>  # 驗證某個備份檔讀得出來

⚠️ 備份在同一顆硬碟上，只能防「手殘刪掉」，不能防硬碟壞掉。
   真正安全要再複製一份到雲端或外接硬碟 —— 跑完會提醒。
"""

import argparse
import os
import sys
import tarfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT_DIR  # noqa: E402

DATA_DIR = os.path.join(ROOT_DIR, "data")
DEFAULT_OUT = os.path.join(ROOT_DIR, "backups")

# 要備份的東西。只挑不可重建的 ——
# .backup-* 那些是舊的資料庫快照，已經包含在現在的 geo.db 裡了，不用重複打包。
INCLUDE = ["geo.db", "raw"]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def do_backup(out_dir):
    if not os.path.isdir(DATA_DIR):
        print("找不到 data/ 資料夾，沒有東西可以備份。")
        return 1

    targets = [(name, os.path.join(DATA_DIR, name))
               for name in INCLUDE
               if os.path.exists(os.path.join(DATA_DIR, name))]
    if not targets:
        print("data/ 裡沒有 %s，沒有東西可以備份。" % "、".join(INCLUDE))
        return 1

    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = os.path.join(out_dir, "geo-data-%s.tar.gz" % stamp)

    print("要備份的內容：")
    n_files = 0
    for name, full in targets:
        if os.path.isdir(full):
            size = dir_size(full)
            cnt = sum(len(fs) for _, _, fs in os.walk(full))
            n_files += cnt
            print("  %-10s %8s  %d 個檔案" % (name, human(size), cnt))
        else:
            n_files += 1
            print("  %-10s %8s" % (name, human(os.path.getsize(full))))
    print()

    print("打包中…")
    # 先寫 .tmp 再改名 —— 中途當掉不會留下一個解不開的半截備份
    tmp = path + ".tmp"
    with tarfile.open(tmp, "w:gz") as tar:
        for name, full in targets:
            tar.add(full, arcname=name)
    os.rename(tmp, path)

    size = os.path.getsize(path)
    print("✅ 備份完成")
    print("   %s" % path)
    print("   %s（%d 個檔案）" % (human(size), n_files))
    print()
    print("⚠️ 這份備份在同一顆硬碟上，只能防「手殘刪掉」，不能防硬碟壞掉。")
    print("   請再複製一份到雲端或外接硬碟，例如：")
    print("     開 Finder → 把這個檔案拖進 Google Drive 或隨身碟")
    return 0


def do_list(out_dir):
    if not os.path.isdir(out_dir):
        print("還沒有任何備份。跑一次：python scripts/backup.py")
        return 0
    files = sorted(f for f in os.listdir(out_dir)
                   if f.startswith("geo-data-") and f.endswith(".tar.gz"))
    if not files:
        print("還沒有任何備份。跑一次：python scripts/backup.py")
        return 0
    print("目前的備份（放在 %s）：" % out_dir)
    for f in files:
        full = os.path.join(out_dir, f)
        print("  %-34s %9s  %s" % (
            f, human(os.path.getsize(full)),
            datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M")))
    return 0


def do_verify(path):
    """確認備份檔真的解得開、而且內容在裡面。

    備份最糟的失敗方式是「以為有備份，要用的時候才發現解不開」，
    所以做完一定要驗一次。
    """
    if not os.path.exists(path):
        print("找不到檔案：%s" % path)
        return 1
    print("驗證 %s" % path)
    try:
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
    except (tarfile.TarError, OSError) as e:
        print("❌ 這個備份檔讀不出來：%s" % e)
        return 1
    db = [n for n in names if n.endswith("geo.db")]
    raw = [n for n in names if n.startswith("raw/") and n.endswith(".json.gz")]
    print("  檔案總數：%d" % len(names))
    print("  geo.db   ：%s" % ("✅ 在裡面" if db else "❌ 不在裡面"))
    print("  原始回答 ：%d 個 .json.gz" % len(raw))
    ok = bool(db) and len(raw) > 0
    print()
    print("✅ 這份備份是完整的、解得開的。" if ok else "❌ 這份備份不完整。")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(
        description="把 data/ 打包成備份檔（它不在版控裡，是唯一不可重建的資產）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/backup.py\n"
               "  python scripts/backup.py --list\n")
    parser.add_argument("--out", default=DEFAULT_OUT, help="備份放哪裡")
    parser.add_argument("--list", action="store_true", help="列出現有備份")
    parser.add_argument("--verify", metavar="檔案", help="驗證某個備份檔")
    args = parser.parse_args()

    if args.verify:
        sys.exit(do_verify(args.verify))
    if args.list:
        sys.exit(do_list(os.path.expanduser(args.out)))
    sys.exit(do_backup(os.path.expanduser(args.out)))


if __name__ == "__main__":
    main()
