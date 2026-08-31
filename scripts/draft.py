# -*- coding: utf-8 -*-
"""
draft.py — 針對一個缺口產出文章草稿

吃什麼 → 做什麼 → 吐什麼
  吃：一個 gap id ＋ 它的證據（受影響的題目、AI 實際怎麼回答、競品被引用了哪些來源）
  做：寫成一篇「為了被 AI 引用」而寫的文章草稿
  吐：drafts 表一筆，status=draft

怎麼跑
  python scripts/draft.py --gap-id 3            # 針對第 3 號缺口產草稿
  python scripts/draft.py --gap-id 3 --outline-only  # 只產大綱，先看方向對不對

前置條件
  gaps 表裡有那筆缺口

═══════════════════════════════════════════════════════════════════════
寫作規則：這是寫給 AI 讀的，不是寫給 Google 排名的
═══════════════════════════════════════════════════════════════════════

  1. 一個標題 = 一個明確問題
     ✗「寵物住宿全攻略」
     ✓「貓咪可以連續寄宿幾天？」

  2. 答案放在段落開頭，可以被獨立擷取
     模型是一段一段抓的，答案埋在第三段就等於不存在。

  3. 事實密度高、有具體數字
     ✓「95% 的保姆在 2 小時內回覆」
     ✗「回覆速度很快」

  4. 避免行銷形容詞
     模型不引用形容詞，只引用事實。「業界領先」「最優質」這種詞
     不會讓任何一個 AI 多提到我們一次，只會稀釋整段的事實密度。

⚠️ 永遠不自動發佈
  產出一律 status='draft'，等人審完、實際發佈之後，
  再由人到「機會建議」頁登記一筆 action，把 draft 跟 action 串起來。

目前狀態：階段 0 空殼。實作在階段 5。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH  # noqa: E402


def make_draft(db_path, gap_id, outline_only):
    """主要流程（階段 5 再實作）"""
    print("TODO: 讀出 gaps 第 %s 號：title、why、affected_prompt_ids" % gap_id)
    print("TODO: 撈證據 —— 那些題目 AI 實際上怎麼回答的（從 data/raw/ 讀原文）")
    print("TODO: 撈證據 —— 競品在這些題目上被引用了哪些來源（citations）")
    print("TODO: 產出大綱：一個標題 = 一個明確問題，答案放段落開頭")
    if outline_only:
        print("（--outline-only：到大綱為止，不寫內文）")
        return
    print("TODO: 產出內文：事實密度高、有具體數字、不要行銷形容詞")
    print("TODO: 寫入 drafts（gap_id, title, outline, body, status='draft'）")
    print("TODO: 絕對不自動發佈。人審過、真的發了，再由人登記一筆 action。")


def main():
    parser = argparse.ArgumentParser(
        description="針對一個內容缺口產出文章草稿（永遠不自動發佈）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/draft.py --gap-id 3\n"
               "  python scripts/draft.py --gap-id 3 --outline-only\n",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--gap-id", type=int, required=True, help="要處理的缺口編號")
    parser.add_argument("--outline-only", action="store_true",
                        help="只產大綱不寫內文，先確認方向")
    args = parser.parse_args()

    print("=== draft.py（階段 0 空殼）===")
    make_draft(args.db, args.gap_id, args.outline_only)


if __name__ == "__main__":
    main()
