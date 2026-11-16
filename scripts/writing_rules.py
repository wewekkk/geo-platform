# -*- coding: utf-8 -*-
"""
writing_rules.py — 「為了被 AI 引用而寫」的四條規則，寫成可重複呼叫的檢查函式

這不是一支腳本，是一個共用模組。兩個地方會用到同一份規則：
  1. draft.py —— 產生新草稿時，當寫作準則，並檢查產出合不合格
  2. audit.py —— 階段 6 拿同一份規則去掃已經上線的頁面

CLAUDE.md 特別要求寫成一份共用：
  「同一組規則有兩個用途……寫成一份、兩邊共用，不要各寫一次 ——
    兩份規則遲早會不一致。」

═══════════════════════════════════════════════════════════════════════
四條規則，以及為什麼是這四條
═══════════════════════════════════════════════════════════════════════

目標不是 Google 排名，是「被 AI 當成資料來源引用」。這兩件事要的東西不一樣：

  Google 排名要的      AI 引用要的
  ──────────────      ──────────────
  關鍵字覆蓋            一個標題回答一個明確問題
  停留時間、內鏈         答案能被獨立擷取（不用讀上下文）
  篇幅夠長              事實密度高、有具體數字
  吸引點擊的文案         沒有行銷形容詞（模型不引用形容詞）

1. 一個標題 = 一個明確問題
   AI 要回答「到府餵貓要準備什麼」時，會去找標題就是這個問題的頁面。
   標題寫成「寵物保姆服務介紹」的話，它不知道這頁回答了什麼。

2. 答案放在段落開頭，可以被獨立擷取
   模型擷取的是「一小段能獨立成立的文字」。
   答案埋在第五段、或前面鋪陳三句「隨著國人飼養寵物的比例逐年上升……」，
   那一段就擷取不出完整答案。

3. 事實密度高、有具體數字
   「費用依服務型態而定」不會被引用，「到府餵貓一次 400–800 元」會。
   模型引用的是可驗證的事實，不是結論。

4. 避免行銷形容詞
   「專業貼心的優質保姆」裡沒有任何資訊。模型不引用形容詞，只引用事實。
   而且形容詞多的頁面會被判定成廣告，降低整頁的可信度。

═══════════════════════════════════════════════════════════════════════

每個檢查回傳一個 CheckResult，欄位刻意對齊 page_audits 表
（check_key / passed / detail / severity），這樣 audit.py 可以直接寫進資料庫。
"""

import re


def visible(text):
    """只留下讀者與 AI 真的看得到的文字。

    HTML 註解（<!-- ... -->）不會被渲染，所以不該算進事實密度，
    也不該被當成行銷用語。草稿骨架裡的提示註解尤其不能算 ——
    不然「要填什麼」的提示本身會讓檢查過關，那就失去意義了。
    """
    return re.sub(r"<!--.*?-->", " ", text or "", flags=re.S)


# 嚴重度。high = 不改就不會被引用；medium = 會降低被引用的機會
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"


class CheckResult(object):
    """一條規則的檢查結果。"""

    def __init__(self, key, name, passed, detail, severity, fix=None):
        self.check_key = key
        self.name = name
        self.passed = passed
        self.detail = detail          # 為什麼過／沒過，給人看的
        self.severity = severity
        self.fix = fix                # 怎麼改

    def __repr__(self):
        return "<%s %s>" % (self.check_key, "pass" if self.passed else "FAIL")


# ---------------------------------------------------------------------------
# 規則 1：一個標題 = 一個明確問題
# ---------------------------------------------------------------------------

# 標題裡出現這些字，代表它不是一個問題而是一個分類名稱
VAGUE_TITLE_WORDS = ["服務介紹", "介紹", "總覽", "大全", "懶人包", "整理",
                     "相關資訊", "須知", "指南", "說明"]

QUESTION_MARKS = "？?"


def check_title_is_one_question(title, body=""):
    key, name = "title_one_question", "一個標題 = 一個明確問題"
    t = (title or "").strip()
    if not t:
        return CheckResult(key, name, False, "沒有標題。", SEVERITY_HIGH,
                           "標題直接寫成客戶會問的那句話。")

    n_q = sum(t.count(m) for m in QUESTION_MARKS)

    if n_q == 0:
        vague = [w for w in VAGUE_TITLE_WORDS if w in t]
        if vague:
            return CheckResult(
                key, name, False,
                "標題不是一個問題，而且含有「%s」這種分類字眼。" % "、".join(vague),
                SEVERITY_HIGH,
                "改寫成客戶真的會打出來的那句問句，例如"
                "「到府餵貓要準備什麼？」而不是「到府餵貓服務介紹」。")
        return CheckResult(
            key, name, False, "標題不是問句（沒有問號）。", SEVERITY_HIGH,
            "AI 要回答某個問題時，會找標題就是那個問題的頁面。"
            "把標題改成那句問句。")

    if n_q > 1:
        return CheckResult(
            key, name, False, "標題裡有 %d 個問號，塞了多個問題。" % n_q,
            SEVERITY_HIGH,
            "一個標題只回答一個問題。多個問題就拆成多篇，"
            "或是把其他問題改成 H2 小標。")

    return CheckResult(key, name, True,
                       "標題是單一問句：「%s」" % t, SEVERITY_HIGH)


# ---------------------------------------------------------------------------
# 規則 2：答案放在段落開頭，可以被獨立擷取
# ---------------------------------------------------------------------------

# 段落開頭出現這些就是在鋪陳，不是在回答
FILLER_OPENERS = [
    "隨著", "近年來", "近年", "在現代", "現今", "如今", "眾所皆知", "眾所周知",
    "相信大家", "大家都知道", "我們都知道", "不少人", "許多人", "很多人",
    "說到", "提到", "談到", "在討論", "首先", "在開始之前", "其實",
    "你是否", "您是否", "有沒有想過",
]

# 一段答案要能獨立擷取，長度不該超過這個（中文字）
MAX_ANSWER_CHARS = 220


def _sections(body):
    """把內文切成 (小標, 那一段的內文) 的清單。

    認 Markdown 的 ## 與 ### 當小標。沒有小標的話，整篇算一段。
    """
    lines = (body or "").split("\n")
    out, cur_head, cur = [], None, []
    for ln in lines:
        m = re.match(r"^\s{0,3}(#{2,4})\s+(.*)$", ln)
        if m:
            if cur_head is not None or any(x.strip() for x in cur):
                out.append((cur_head, "\n".join(cur).strip()))
            cur_head, cur = m.group(2).strip(), []
        else:
            cur.append(ln)
    if cur_head is not None or any(x.strip() for x in cur):
        out.append((cur_head, "\n".join(cur).strip()))
    return out


def _first_paragraph(text):
    for block in re.split(r"\n\s*\n", text or ""):
        b = block.strip()
        # 跳過清單與引用，答案要是一段完整的話
        if b and not b.startswith(("-", "*", ">", "|")) and not re.match(r"^\d+\.", b):
            return b
    return ""


def check_answer_first(title, body=""):
    key, name = "answer_first", "答案放在段落開頭，可以被獨立擷取"
    secs = _sections(visible(body))
    if not secs:
        return CheckResult(key, name, False, "內文是空的。", SEVERITY_HIGH,
                           "每個小標底下第一句就把答案講完。")

    problems = []
    for head, text in secs:
        para = _first_paragraph(text)
        label = head or "（開頭）"
        if not para:
            problems.append("「%s」底下沒有可擷取的段落（只有清單或表格）。" % label)
            continue
        hit = [w for w in FILLER_OPENERS if para.startswith(w)]
        if hit:
            problems.append("「%s」第一段用「%s」開頭，那是鋪陳不是答案。"
                            % (label, hit[0]))
        elif len(para) > MAX_ANSWER_CHARS:
            problems.append("「%s」第一段 %d 字，太長了（超過 %d 字就很難被整段擷取）。"
                            % (label, len(para), MAX_ANSWER_CHARS))

    if problems:
        return CheckResult(
            key, name, False, "　".join(problems), SEVERITY_HIGH,
            "每個小標底下的第一段，就是那個問題的完整答案，"
            "而且要能單獨看懂、不用讀上下文。鋪陳、背景、故事往後放或刪掉。")

    return CheckResult(key, name, True,
                       "%d 個段落的第一段都是直接回答。" % len(secs), SEVERITY_HIGH)


# ---------------------------------------------------------------------------
# 規則 3：事實密度高、有具體數字
# ---------------------------------------------------------------------------

# 每一百字至少要有幾個數字
MIN_NUMBERS_PER_100 = 1.5

# 這些是「假的具體」—— 看起來在講數字，其實沒有
VAGUE_QUANTIFIERS = ["依服務而定", "依情況而定", "視情況", "因人而異", "不盡相同",
                     "各有不同", "依需求", "price varies", "依方案而定"]


def check_fact_density(title, body=""):
    key, name = "fact_density", "事實密度高、有具體數字"
    text = visible(body)
    chars = len(re.sub(r"\s", "", text))
    if chars == 0:
        return CheckResult(key, name, False, "內文是空的。", SEVERITY_HIGH,
                           "放具體數字：價格區間、天數、次數、比例。")

    numbers = re.findall(r"\d+(?:[.,]\d+)?", text)
    per100 = len(numbers) / (chars / 100.0)
    vague = [v for v in VAGUE_QUANTIFIERS if v in text]

    detail = "%d 個數字 / %d 字，約每百字 %.1f 個（門檻 %.1f）。" % (
        len(numbers), chars, per100, MIN_NUMBERS_PER_100)
    if vague:
        detail += "　另外出現了「%s」這種含糊說法。" % "、".join(vague)

    passed = per100 >= MIN_NUMBERS_PER_100 and not vague
    fix = ("把每一句結論換成可驗證的事實。"
           "「費用依服務型態而定」改成「到府餵貓一次 400–800 元，過夜 800–1500 元」。"
           "模型引用的是事實，不是結論。")
    return CheckResult(key, name, passed, detail, SEVERITY_HIGH,
                       None if passed else fix)


# ---------------------------------------------------------------------------
# 規則 4：避免行銷形容詞
# ---------------------------------------------------------------------------

MARKETING_WORDS = [
    "專業", "優質", "頂尖", "最佳", "首選", "第一品牌", "領先", "卓越",
    "貼心", "用心", "安心", "放心", "完善", "豐富", "嚴選", "精選",
    "超值", "物超所值", "極致", "全方位", "一流", "知名", "口碑",
    "值得信賴", "最好的", "最棒", "無與倫比", "業界", "獨家",
]

# 每百字最多幾個行銷形容詞
MAX_MARKETING_PER_100 = 0.8


def check_no_marketing_adjectives(title, body=""):
    key, name = "no_marketing", "避免行銷形容詞"
    text = (title or "") + "\n" + visible(body)
    chars = len(re.sub(r"\s", "", text))
    if chars == 0:
        return CheckResult(key, name, False, "沒有內容可以檢查。", SEVERITY_MEDIUM)

    found = {}
    for w in MARKETING_WORDS:
        c = text.count(w)
        if c:
            found[w] = c
    total = sum(found.values())
    per100 = total / (chars / 100.0)

    if not found:
        return CheckResult(key, name, True,
                           "沒有偵測到行銷形容詞。", SEVERITY_MEDIUM)

    top = "、".join("%s×%d" % (w, c) for w, c in
                    sorted(found.items(), key=lambda kv: -kv[1])[:6])
    detail = "%d 個行銷形容詞 / %d 字，約每百字 %.1f 個（門檻 %.1f）：%s" % (
        total, chars, per100, MAX_MARKETING_PER_100, top)
    passed = per100 <= MAX_MARKETING_PER_100
    fix = ("形容詞換成事實。「專業的保姆」→「保姆需通過身分證驗證與良民證審核」；"
           "「安心保障」→「國泰產險，自負額 3,000 元，醫療補助上限 15,000 元」。")
    return CheckResult(key, name, passed, detail, SEVERITY_MEDIUM,
                       None if passed else fix)


# ---------------------------------------------------------------------------
# 一次跑完四條
# ---------------------------------------------------------------------------

RULES = [
    check_title_is_one_question,
    check_answer_first,
    check_fact_density,
    check_no_marketing_adjectives,
]


def check_all(title, body=""):
    """跑完四條規則，回傳 CheckResult 清單（順序固定）。"""
    return [fn(title, body) for fn in RULES]


def format_report(results, indent="  "):
    """把檢查結果排成給人看的文字。"""
    lines = []
    n_pass = sum(1 for r in results if r.passed)
    lines.append("%s寫作規則檢查：%d / %d 條通過" % (indent, n_pass, len(results)))
    lines.append("")
    for r in results:
        mark = "✅" if r.passed else ("❌" if r.severity == SEVERITY_HIGH else "⚠️")
        lines.append("%s%s %s" % (indent, mark, r.name))
        lines.append("%s     %s" % (indent, r.detail))
        if not r.passed and r.fix:
            lines.append("%s     → 怎麼改：%s" % (indent, r.fix))
        lines.append("")
    return "\n".join(lines)


def all_passed(results):
    return all(r.passed for r in results)
