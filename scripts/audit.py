# -*- coding: utf-8 -*-
"""
audit.py — 檢查自家頁面「AI 讀不讀得到」

吃什麼 → 做什麼 → 吐什麼
  吃：自家網址清單（參數給，或從 citations 表撈）
  做：抓頁面做規則比對，十項檢查
  吐：page_audits 表（一個網址一次檢查會有多列，一列一項）

怎麼跑
  python scripts/audit.py --url https://www.fluv.com/tw/zh-tw/fluv-protection
  python scripts/audit.py --cited          # 檢查「AI 已經引用過」的頁面（當對照組）
  python scripts/audit.py --url A --url B  # 一次檢查多個
  python scripts/audit.py --history <網址>  # 看某個網址歷次檢查的變化
  python scripts/audit.py --url A --preview  # 只印出來，不寫進資料庫

這支腳本補的是整個設計唯一的盲點
  前面五支都在回答「AI 有沒有提到我們」，這一支回答「**為什麼沒有**」。
  可能不是內容不夠，而是結構讓模型讀不到 ——
  沒有 schema、答案埋在第五段、robots.txt 直接擋掉 AI 爬蟲。

⚠️ 不呼叫任何 AI API、不花錢
  全部是抓自己的頁面做規則比對。唯一的網路行為是 GET 自家網址。

⚠️ 檢查項目控制在十項以內
  同類工具動輒五十幾項，但項目多到那個程度就沒人會照著改。
  每一項都要能明確回答「所以我該改什麼」，做不到就不要放進來。

四條寫作規則（標題／答案位置／事實密度／行銷形容詞）直接用 writing_rules.py
的同一份函式，不是在這裡重寫一次 —— 兩份規則遲早會不一致。
"""

import argparse
import json
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_DB_PATH, OUR_DOMAIN, connect, now_iso  # noqa: E402
from writing_rules import (  # noqa: E402
    SEVERITY_HIGH, SEVERITY_MEDIUM, CheckResult,
    check_answer_first, check_fact_density, check_no_marketing_adjectives,
    check_title_is_one_question,
)

# 假裝成一般瀏覽器。不是為了規避什麼 —— 有些 CDN 會對沒有 UA 的請求回 403，
# 那樣會檢查不到自己的頁面。
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 FluvGEOAudit/1.0")
TIMEOUT = 20

# 這四隻是各家 AI 的爬蟲。robots.txt 擋掉它們，內容就進不了 AI 的資料來源。
AI_CRAWLERS = ["GPTBot", "ClaudeBot", "anthropic-ai", "PerplexityBot",
               "Google-Extended", "CCBot", "Applebot-Extended"]

# 頁面正文至少要有這麼多字，否則多半是「內容要跑 JS 才看得到」
MIN_VISIBLE_CHARS = 300

# 信任訊號的關鍵字（E-E-A-T）
AUTHOR_HINTS = ["作者", "撰文", "editor", "author", "byline", "審稿", "獸醫師"]
ABOUT_HINTS = ["about", "關於我們", "關於", "about-us", "team", "公司"]


# ---------------------------------------------------------------------------
# HTML 解析
#
# 刻意用標準函式庫的 HTMLParser，不裝 BeautifulSoup ——
# 這裡要的東西很結構化（標題、JSON-LD、正文、連結），stdlib 夠用，
# 而且少一個套件少一件要維護的事（CLAUDE.md：不要引入額外學習成本的抽象層）。
# ---------------------------------------------------------------------------

# 這些標籤裡的文字不是正文，不該算進事實密度與形容詞比例
SKIP_TEXT_IN = {"script", "style", "nav", "header", "footer", "aside",
                "noscript", "svg", "button", "form", "select"}
BLOCK_TAGS = {"p", "div", "li", "br", "tr", "section", "article", "blockquote"}
HEADINGS = {"h1", "h2", "h3", "h4"}


class PageParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.stack = []
        self.headings = []        # [(層級, 文字)]
        self.jsonld = []          # JSON-LD 區塊的原始字串
        self.links = []           # [(href, 連結文字)]
        self.title = None
        self.meta = {}
        self._buf = []            # 正文（已標上小標）
        self._head_buf = None
        self._link_buf = None
        self._link_href = None
        self._in_title = False
        self._in_jsonld = False

    # —— 開標籤 ——
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.stack.append(tag)
        if tag == "title":
            self._in_title = True
        elif tag in HEADINGS:
            self._head_buf = []
        elif tag == "a":
            self._link_href = a.get("href")
            self._link_buf = []
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_jsonld = True
            self.jsonld.append([])
        elif tag == "meta":
            key = a.get("name") or a.get("property")
            if key:
                self.meta[key.lower()] = a.get("content", "")
        elif tag in BLOCK_TAGS:
            self._buf.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    # —— 關標籤 ——
    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in HEADINGS and self._head_buf is not None:
            text = re.sub(r"\s+", " ", "".join(self._head_buf)).strip()
            if text:
                level = int(tag[1])
                self.headings.append((level, text))
                # 轉成 markdown 的小標，writing_rules 才吃得到
                self._buf.append("\n\n%s %s\n\n" % ("#" * level, text))
            self._head_buf = None
        elif tag == "a" and self._link_buf is not None:
            text = re.sub(r"\s+", " ", "".join(self._link_buf)).strip()
            if self._link_href:
                self.links.append((self._link_href, text))
            self._link_buf = self._link_href = None
        elif tag == "script":
            self._in_jsonld = False
        elif tag in BLOCK_TAGS:
            self._buf.append("\n")
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:            # 未閉合的標籤，倒著清掉
            while self.stack and self.stack.pop() != tag:
                pass

    # —— 文字 ——
    def handle_data(self, data):
        if self._in_title:
            self.title = (self.title or "") + data
        if self._in_jsonld and self.jsonld:
            self.jsonld[-1].append(data)
            return
        if any(t in SKIP_TEXT_IN for t in self.stack):
            return
        if self._head_buf is not None:
            self._head_buf.append(data)
        if self._link_buf is not None:
            self._link_buf.append(data)
        self._buf.append(data)

    # —— 產出 ——
    def body_markdown(self):
        """正文，小標轉成 markdown 的 ## 格式，給 writing_rules 用。"""
        text = "".join(self._buf)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        return "\n".join(l.strip() for l in text.split("\n")).strip()

    def visible_text(self):
        return re.sub(r"\s+", "", self.body_markdown())

    def ld_objects(self):
        """把 JSON-LD 解析出來，攤平成一串物件（@graph 也攤開）。"""
        out = []
        for chunk in self.jsonld:
            raw = "".join(chunk).strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            queue = data if isinstance(data, list) else [data]
            while queue:
                item = queue.pop(0)
                if not isinstance(item, dict):
                    continue
                out.append(item)
                g = item.get("@graph")
                if isinstance(g, list):
                    queue.extend(g)
        return out


def fetch(url):
    """抓一個網址。回傳 (狀態碼, 內容文字, 錯誤訊息)。

    優先用 requests，沒裝就退回標準函式庫的 urllib ——
    這支不呼叫任何 AI API，不該因為少裝一個套件就整個跑不起來。
    """
    try:
        import requests
    except ImportError:
        requests = None

    if requests is not None:
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
            return r.status_code, r.text, None
        except Exception as e:
            return None, None, "%s：%s" % (type(e).__name__, e)

    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return r.status, raw.decode(charset, "replace"), None
    except urllib.error.HTTPError as e:
        # 404／403 也是結果，要讓檢查項自己判斷，不能當成抓取失敗
        raw = e.read() if hasattr(e, "read") else b""
        return e.code, raw.decode("utf-8", "replace"), None
    except Exception as e:
        return None, None, "%s：%s" % (type(e).__name__, e)


# ---------------------------------------------------------------------------
# 網站層級的兩項檢查（一個網域只要做一次）
# ---------------------------------------------------------------------------

def check_robots(domain):
    key, name = "robots_ai_crawlers", "robots.txt 沒有擋掉 AI 爬蟲"
    url = "https://%s/robots.txt" % domain
    code, text, err = fetch(url)
    if err or code != 200:
        return CheckResult(
            key, name, False,
            "讀不到 %s（%s）。" % (url, err or "HTTP %s" % code), SEVERITY_HIGH,
            "確認 robots.txt 存在且可公開讀取。AI 爬蟲讀不到它的時候，"
            "有些會保守地選擇不抓。")

    # 逐段解析：一段 User-agent 底下的 Disallow 只對那些 UA 生效
    blocked, cur = {}, []
    for line in text.split("\n"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            cur = [v]
        elif k == "disallow" and cur:
            for ua in cur:
                if v == "/":
                    blocked.setdefault(ua, []).append(v)

    hits = []
    for bot in AI_CRAWLERS:
        for ua, rules in blocked.items():
            if ua.lower() == bot.lower() and rules:
                hits.append(bot)
    all_blocked = any(ua == "*" and rules for ua, rules in blocked.items())

    if hits or all_blocked:
        who = "、".join(hits) if hits else "所有爬蟲（User-agent: *）"
        return CheckResult(
            key, name, False,
            "robots.txt 擋掉了 %s（Disallow: /）。" % who, SEVERITY_HIGH,
            "把這幾隻放行：%s。擋掉它們，內容就進不了 AI 的資料來源 ——"
            "寫再多文章也不會被引用。" % "、".join(AI_CRAWLERS))

    return CheckResult(key, name, True,
                       "沒有擋掉任何 AI 爬蟲（檢查了 %s）。" % "、".join(AI_CRAWLERS),
                       SEVERITY_HIGH)


def check_llms_txt(domain):
    key, name = "llms_txt", "有 llms.txt"
    url = "https://%s/llms.txt" % domain
    code, text, err = fetch(url)
    ok = (code == 200 and text and not text.lstrip().lower().startswith("<!doctype")
          and "<html" not in text[:400].lower())
    if ok:
        return CheckResult(key, name, True,
                           "%s 存在（%d 字）。" % (url, len(text)), SEVERITY_MEDIUM)
    why = "回 HTTP %s" % code if code else (err or "讀不到")
    if code == 200:
        why = "回的是 HTML 頁面，不是純文字的 llms.txt"
    return CheckResult(
        key, name, False, "%s %s。" % (url, why), SEVERITY_MEDIUM,
        "建立 /llms.txt，用純文字列出網站最重要的頁面與一句話說明。"
        "這是給 AI 看的網站地圖 —— 不是硬性標準，但成本很低。")


# ---------------------------------------------------------------------------
# 頁面層級的八項檢查
# ---------------------------------------------------------------------------

def check_fetchable(url, code, html, parser):
    key, name = "fetchable", "頁面抓得到，而且正文在 HTML 裡"
    if code != 200:
        return CheckResult(key, name, False, "HTTP %s。" % code, SEVERITY_HIGH,
                           "AI 爬蟲抓不到就什麼都不用談了。先確認這個網址是通的。")
    chars = len(parser.visible_text())
    if chars < MIN_VISIBLE_CHARS:
        return CheckResult(
            key, name, False,
            "HTML 裡只有 %d 個字的正文（門檻 %d）。" % (chars, MIN_VISIBLE_CHARS),
            SEVERITY_HIGH,
            "內容多半是靠 JavaScript 載入的。大部分 AI 爬蟲不執行 JS —— "
            "它們看到的就是這個空殼。要做伺服器端渲染（SSR）或預先產生靜態 HTML。")
    return CheckResult(key, name, True,
                       "HTTP 200，HTML 裡有 %d 個字的正文。" % chars, SEVERITY_HIGH)


def check_schema(url, code, html, parser):
    key, name = "schema_org", "有 schema.org 結構化標記"
    objs = parser.ld_objects()
    types = []
    for o in objs:
        t = o.get("@type")
        if isinstance(t, list):
            types.extend(str(x) for x in t)
        elif t:
            types.append(str(t))
    wanted = {"FAQPage", "Article", "BlogPosting", "NewsArticle",
              "Organization", "WebPage", "BreadcrumbList", "Product"}
    found = [t for t in types if t in wanted]
    if not objs:
        return CheckResult(
            key, name, False, "頁面裡沒有任何 JSON-LD 結構化標記。", SEVERITY_HIGH,
            "加上 schema.org 標記。問答型頁面用 FAQPage、文章用 Article、"
            "首頁或關於我們用 Organization。這是告訴 AI「這一段是問題、這一段是答案」"
            "最直接的方式。")
    if not found:
        return CheckResult(
            key, name, False,
            "有 JSON-LD，但型別是 %s，不是常用的那幾種。" % "、".join(sorted(set(types))[:5]),
            SEVERITY_MEDIUM,
            "改成 FAQPage / Article / Organization 其中之一。")
    return CheckResult(key, name, True,
                       "有 %d 個 JSON-LD 區塊，型別包含 %s。"
                       % (len(objs), "、".join(sorted(set(found)))), SEVERITY_HIGH)


def check_single_h1(url, code, html, parser):
    key, name = "single_h1", "一頁一個 h1"
    h1 = [t for lvl, t in parser.headings if lvl == 1]
    if len(h1) == 1:
        return CheckResult(key, name, True, "只有一個 h1：「%s」" % h1[0][:50],
                           SEVERITY_MEDIUM)
    if not h1:
        return CheckResult(key, name, False, "整頁沒有 h1。", SEVERITY_MEDIUM,
                           "加一個 h1，內容就是這一頁要回答的那個問題。")
    return CheckResult(
        key, name, False,
        "有 %d 個 h1：%s" % (len(h1), "、".join("「%s」" % x[:22] for x in h1[:4])),
        SEVERITY_MEDIUM,
        "只留一個。多個 h1 會讓模型分不出這一頁的主題是哪一個。")


def check_h2_questions(url, code, html, parser):
    """h2 是不是明確的問題 —— 用 writing_rules 的同一條規則逐個檢查。"""
    key, name = "h2_is_question", "小標是明確的問題"
    h2 = [t for lvl, t in parser.headings if lvl == 2]
    if not h2:
        return CheckResult(key, name, False, "整頁沒有 h2 小標。", SEVERITY_MEDIUM,
                           "用 h2 把內容切成幾個明確的問題，每個小標就是一句問句。")
    bad = [t for t in h2 if not check_title_is_one_question(t).passed]
    if not bad:
        return CheckResult(key, name, True,
                           "%d 個 h2 全部都是單一問句。" % len(h2), SEVERITY_MEDIUM)
    return CheckResult(
        key, name, len(bad) <= len(h2) // 2,
        "%d / %d 個 h2 不是明確的問句，例如：%s"
        % (len(bad), len(h2), "、".join("「%s」" % x[:20] for x in bad[:3])),
        SEVERITY_MEDIUM,
        "AI 要回答某個問題時會找小標就是那個問題的段落。"
        "把「服務特色」這種分類名稱改成「到府餵貓一次多少錢？」這種問句。")


def check_trust_signals(url, code, html, parser):
    """信任訊號（E-E-A-T）。

    這一項對這個專案特別重要 —— `trust`（信任查證）的意圖權重 1.5 是全部裡最高的。
    """
    key, name = "trust_signals", "信任訊號（作者、關於我們、外部來源）"
    body = parser.body_markdown()
    host = urlparse(url).netloc

    has_author = any(h in body[:4000].lower() or h in body[:4000]
                     for h in AUTHOR_HINTS) or "author" in parser.meta
    has_about = any(
        any(h in (href or "").lower() or h in (txt or "") for h in ABOUT_HINTS)
        for href, txt in parser.links)
    externals = set()
    for href, _ in parser.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        h = urlparse(urljoin(url, href)).netloc
        if h and h != host and OUR_DOMAIN not in h:
            externals.add(h)

    got = []
    missing = []
    (got if has_author else missing).append("作者署名")
    (got if has_about else missing).append("關於我們連結")
    (got if externals else missing).append("引用外部來源")

    detail = "有：%s。" % "、".join(got) if got else "三項都沒有。"
    if externals:
        detail += "　外部來源 %d 個網域。" % len(externals)
    if missing:
        detail += "　缺：%s。" % "、".join(missing)

    return CheckResult(
        key, name, len(got) >= 2, detail, SEVERITY_HIGH,
        None if len(got) >= 2 else
        "補上作者署名與資歷（最好有獸醫師或實務經驗）、連到關於我們、"
        "並引用至少一個外部來源。這個品類的信任門檻很高，"
        "而 trust 類題目的意圖權重 1.5 是全部裡最高的。")


# ---------------------------------------------------------------------------
# 跑完一個頁面
# ---------------------------------------------------------------------------

STRUCTURAL_CHECKS = [check_fetchable, check_schema, check_single_h1,
                     check_h2_questions, check_trust_signals]


def audit_page(url):
    """對一個網址跑八項頁面檢查。回傳 (CheckResult 清單, 錯誤訊息)。"""
    code, html, err = fetch(url)
    if err:
        return [CheckResult("fetchable", "頁面抓得到，而且正文在 HTML 裡",
                            False, err, SEVERITY_HIGH,
                            "確認網址正確、網站有在線上。")], err

    parser = PageParser()
    try:
        parser.feed(html)
    except Exception as e:
        return [CheckResult("fetchable", "頁面抓得到，而且正文在 HTML 裡",
                            False, "HTML 解析失敗：%s" % e, SEVERITY_HIGH,
                            None)], str(e)

    results = [fn(url, code, html, parser) for fn in STRUCTURAL_CHECKS]

    # 四條寫作規則：用 writing_rules.py 的同一份函式，不在這裡重寫
    h1 = [t for lvl, t in parser.headings if lvl == 1]
    title = (h1[0] if h1 else (parser.title or "")).strip()
    body = parser.body_markdown()
    results += [
        check_title_is_one_question(title, body),
        check_answer_first(title, body),
        check_fact_density(title, body),
        check_no_marketing_adjectives(title, body),
    ]
    return results, None


def audit_site(domain):
    return [check_robots(domain), check_llms_txt(domain)]


# ---------------------------------------------------------------------------
# 輸出與寫入
# ---------------------------------------------------------------------------

def print_results(label, results):
    n_pass = sum(1 for r in results if r.passed)
    print()
    print("=" * 76)
    print("%s" % label)
    print("通過 %d / %d 項" % (n_pass, len(results)))
    print("=" * 76)
    for r in results:
        mark = "✅" if r.passed else ("❌" if r.severity == SEVERITY_HIGH else "⚠️")
        print("  %s %s" % (mark, r.name))
        print("       %s" % r.detail)
        if not r.passed and r.fix:
            # 修法可能很長，折行印
            for i in range(0, len(r.fix), 62):
                print("       %s %s" % ("→ 怎麼改：" if i == 0 else "          ",
                                        r.fix[i:i + 62]))


def save(conn, url, results):
    ts = now_iso()
    for r in results:
        conn.execute(
            "INSERT INTO page_audits (url, checked_at, check_key, passed, detail, severity) "
            "VALUES (?,?,?,?,?,?)",
            (url, ts, r.check_key, 1 if r.passed else 0, r.detail, r.severity))
    conn.commit()
    return ts


def cited_pages(conn, limit):
    """AI 已經引用過的自家頁面。

    這些是對照組 —— 它們已經被引用了，所以「一個能被引用的頁面長什麼樣」
    可以從它們身上看出來。拿它們跟沒被引用的頁面比，差異就是該補的東西。
    """
    rows = conn.execute(
        "SELECT c.url, COUNT(*) n FROM citations c "
        "JOIN domains d ON d.domain = c.domain "
        "JOIN runs r ON r.id = c.run_id "
        # 空網址檢查不了，也不該佔掉一個名額 ——
        # 判讀器只認得來源名稱、抓不到實際連結時會留空（例如 AI 摘要的「來源：Fluv」）
        "WHERE d.is_ours = 1 AND r.condition != 'demo' "
        "AND c.url IS NOT NULL AND TRIM(c.url) != '' "
        "GROUP BY c.url ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
    return [(r["url"], r["n"]) for r in rows]


def show_history(conn, url):
    rows = conn.execute(
        "SELECT checked_at, check_key, passed, detail FROM page_audits "
        "WHERE url = ? ORDER BY checked_at, check_key", (url,)).fetchall()
    if not rows:
        print("這個網址還沒有檢查紀錄：%s" % url)
        return
    by_time = {}
    for r in rows:
        by_time.setdefault(r["checked_at"], []).append(r)
    print("=== %s 的歷次檢查 ===" % url)
    print()
    times = sorted(by_time)
    for t in times:
        rs = by_time[t]
        print("  %s　通過 %d / %d" % (t, sum(1 for x in rs if x["passed"]), len(rs)))
    if len(times) >= 2:
        first, last = by_time[times[0]], by_time[times[-1]]
        fd = {r["check_key"]: r["passed"] for r in first}
        ld = {r["check_key"]: r["passed"] for r in last}
        changed = [(k, fd.get(k), v) for k, v in ld.items() if fd.get(k) != v]
        print()
        if changed:
            print("  跟第一次比，這幾項變了：")
            for k, a, b in changed:
                arrow = "❌ → ✅ 修好了" if b else "✅ → ❌ 變差了"
                print("    %-22s %s" % (k, arrow))
        else:
            print("  跟第一次比沒有任何項目改變。")


def main():
    parser = argparse.ArgumentParser(
        description="檢查自家頁面「AI 讀不讀得到」（不呼叫 AI API、不花錢）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  python scripts/audit.py --cited\n"
               "  python scripts/audit.py --url https://www.fluv.com/tw/zh-tw\n"
               "  python scripts/audit.py --history <網址>\n")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="資料庫路徑")
    parser.add_argument("--url", action="append", help="要檢查的網址，可重複給")
    parser.add_argument("--cited", action="store_true",
                        help="檢查 AI 已經引用過的自家頁面（當對照組）")
    parser.add_argument("--limit", type=int, default=5,
                        help="--cited 最多檢查幾個頁面（預設 5）")
    parser.add_argument("--history", metavar="網址",
                        help="看某個網址歷次檢查的變化（不會重新檢查）")
    parser.add_argument("--preview", action="store_true",
                        help="只印出結果，不寫進資料庫")
    parser.add_argument("--skip-site", action="store_true",
                        help="跳過 robots.txt 與 llms.txt 這兩項網站層級檢查")
    args = parser.parse_args()

    conn = connect(args.db)

    if args.history:
        show_history(conn, args.history)
        return

    urls = list(args.url or [])
    if args.cited:
        pages = cited_pages(conn, args.limit)
        if not pages:
            print("citations 表裡沒有自家網域的頁面。")
            print("那代表目前的資料還沒記錄到 AI 引用我們 —— 直接用 --url 指定要檢查的頁面。")
            return
        print("AI 已經引用過的自家頁面（被引用次數多的排前面）：")
        for u, n in pages:
            print("  %2d 次  %s" % (n, u))
        urls += [u for u, _ in pages]

    if not urls:
        parser.print_help()
        return

    print("=== audit.py ===")
    print("不呼叫任何 AI API，只 GET 自家網址做規則比對。")

    # 網站層級的兩項，一個網域只做一次
    if not args.skip_site:
        for domain in sorted({urlparse(u).netloc for u in urls if urlparse(u).netloc}):
            rs = audit_site(domain)
            print_results("【網站層級】%s" % domain, rs)
            if not args.preview:
                save(conn, "https://%s/" % domain, rs)

    summary = []
    for u in urls:
        rs, err = audit_page(u)
        print_results("【頁面】%s" % u, rs)
        if not args.preview:
            save(conn, u, rs)
        summary.append((u, sum(1 for r in rs if r.passed), len(rs),
                        [r for r in rs if not r.passed and r.severity == SEVERITY_HIGH]))

    print()
    print("=" * 76)
    print("總結")
    print("=" * 76)
    for u, ok, total, highs in summary:
        print("  %d/%d  %s" % (ok, total, u))
        for r in highs:
            print("        ❌ %s" % r.name)
    if args.preview:
        print()
        print("（--preview：沒有寫進資料庫）")
    else:
        print()
        print("結果已寫入 page_audits。改完之後重跑同一個網址，再用這個看差異：")
        print("  python scripts/audit.py --history <網址>")


if __name__ == "__main__":
    main()
