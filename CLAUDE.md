# Fluv GEO 監測平台

## 這個專案在做什麼

追蹤品牌「Fluv 毛小愛」（台灣寵物保姆媒合平台）在各家 AI 助理回答中的能見度，
把原本重複性的人工 GEO 工作自動化。

系統要能做四件事：

1. **自動蒐集** — 定期問各家 AI 一組問題，記錄完整回答
2. **自動分析** — 判讀每則回答：有沒有提到我們、排第幾、口氣如何、引用了誰
3. **找內容缺口** — 哪些題目我們缺席、依影響力排序該先補哪個
4. **效果衡量** — 補完之後數字有沒有真的動

## 誰在用這個專案

**專案擁有者不是工程師。** 因此：

- 所有程式碼要有**中文註解**
- 每支腳本開頭要用中文說明「吃什麼 → 做什麼 → 吐什麼」
- README 要寫清楚每一支怎麼跑、需要什麼前置條件
- 錯誤訊息要人看得懂，不要只丟 stack trace
- 不要引入需要額外學習成本的抽象層

---

## 技術限制（不要違反）

- **Python + SQLite + Streamlit**
- ❌ 不要 ORM 框架（直接用 sqlite3）
- ❌ 不要 vector database
- ❌ 不要 Docker
- ❌ 不要微服務、不要非同步框架
- ❌ **絕對不要爬 AI 產品的網頁介面**（ToS 風險）— 只用官方 API + SerpApi
- ✅ 呼叫 OpenAI / Anthropic / Gemini 時**務必開啟各自的 web search 工具**
  （不開的話問到的是「裸模型的記憶」，不是會查資料的助理）
- ✅ `.gitignore` 必須排除 `.env` 和 `data/`

保持簡單。這是內部自用工具，不是要對外賣的產品。

---

## 三條不可違反的規則

### 1. 統計顯著性必須顯示

AI 每次回答都不同，所以每個數字本身都有誤差。

- `aggregate.py` 必須計算信賴區間（Wilson score interval）
- **UI 上，當變化落在信賴區間內時，必須顯示為中性的「持平」，不可顯示為紅色的「下降」**

理由：防止使用者對隨機波動做出反應。沒有這條，使用者會針對不存在的問題浪費資源。

### 2. 原始回答不可變

- `data/raw/` 底下的檔案一旦寫入，**永不修改、永不刪除**
- 判讀邏輯改了，要能拿原始檔案整批重跑，歷史數字全部重算
- 資料庫裡只存檔案路徑，不存回答全文

理由：程式碼是耗材，原始資料是資產。

### 3. 量測條件必須顯示在畫面上

每一頁頂端固定一條「量測條件列」：

```
N=10 · condition=api · locale=tw · 上次跑批 2026-08-24 03:12
```

理由：任何人看到「能見度 62%」都會問「這數字怎麼來的」。有這行就不用每次解釋。

---

## 資料模型（十張表 ＋ 階段 6 的第十一張）

| 表 | 用途 | 誰維護 |
|---|---|---|
| `prompts` | 題庫 | 人 |
| `prompt_suggestions` | 題目建議佇列（待人決定加入或忽略） | 系統提議 |
| `runs` | 每次提問的紀錄 | 自動 |
| `mentions` | 判讀結果：提及、排名、情緒 | 自動 |
| `citations` | 引用來源 | 自動 |
| `domains` | 網域清單與分類 | 自動 |
| `metrics_weekly` | 每週彙總指標＋信賴區間 | 自動 |
| `gaps` | 內容缺口＋機會分數 | 系統提議，人決定 |
| `actions` | 採取過的行動 | 人 |
| `drafts` | 文章草稿 | 系統產生，人審 |
| `page_audits` | 自家頁面的 AI 可讀性檢查結果（**階段 6 才建**） | 自動 |

### 欄位

```
prompts             id, text, topic, intent, intent_weight, locale, active,
                    source, created_at
prompt_suggestions  id, text, source, evidence, est_importance, status, created_at
runs                id, prompt_id, engine, model_version, locale, condition,
                    sample_index, ts, raw_path, cost, error
mentions            id, run_id, entity, is_us, position, sentiment_score,
                    sentiment_label, risk_flag, snippet
citations           id, run_id, url, domain, source_type, rank
domains             domain, source_type, is_ours, first_seen, notes
metrics_weekly      week, engine, topic, intent, visibility, ci_low, ci_high,
                    sov, avg_position, sentiment_avg, pos_pct, neu_pct, neg_pct,
                    n_samples
gaps                id, title, topic, opportunity_score, status, opened_at,
                    closed_at, affected_prompt_ids, why
actions             id, gap_id, channel, title, url, published_at, notes
drafts              id, gap_id, title, outline, body, status, action_id, created_at
page_audits         id, url, checked_at, check_key, passed, detail, severity
                    （階段 6 才建。一個頁面一次檢查會有多列，一列一個檢查項目）
```

### 固定值

```
intent                = discovery | comparison | price | trust | urgent | logistics
sentiment_label       = positive | neutral | negative
gaps.status           = open | in_progress | done | holdout
actions.channel       = owned | social | pr | ugc
citations.source_type = 社群 | 媒體 | 品牌內容 | 論壇
```

**注意 `gaps.status` 的 `holdout`** — 這是「刻意不處理的對照組」，必須是正式狀態，
不可以只寫在備註欄。用途是日後比較「有行動」與「刻意不行動」兩組的變化幅度，
藉此區分數字變好是我們的功勞、還是模型自己在漂移。

### 原始檔案路徑格式

```
data/raw/{年}/W{週}/{引擎}/{prompt_id}_{locale}_{sample_index}.json.gz
```

按 年/週/引擎 分層，避免單一資料夾累積數十萬檔案。用 gzip 壓縮。

---

## 八支腳本的職責

| 腳本 | 頻率 | 吃 → 做 → 吐 |
|---|---|---|
| `collect.py` | 每週 | 題庫 → 每題×每引擎跑 N 次 → 原文存 gzip、metadata 進 `runs` |
| `parse.py` | 每週 | 原文 → 便宜模型逐則判讀 → `mentions`／`citations`／`domains` |
| `aggregate.py` | 每週 | 判讀結果 → 算指標**與信賴區間** → `metrics_weekly` |
| `gaps.py` | 每週 | 指標 → 找缺席題目群、算機會分數 → `gaps` |
| `suggest.py` | 每週 | GSC 查詢字＋站內搜尋＋回答裡的新概念 → `prompt_suggestions` |
| `draft.py` | 手動 | 一個 gap ＋ 它的證據 → 文章草稿 → `drafts` |
| `audit.py` | 手動 | 自家網址清單 → 抓頁面做規則檢查 → `page_audits`（階段 6） |
| `app.py` | 隨時 | 全部的表 → Streamlit 八頁畫面 |

### 機會分數公式

```
opportunity_score = (受影響題數 × 意圖權重 × 缺席程度) ÷ 補齊難度
```

意圖權重中，`trust`（信任查證）與 `urgent`（緊急需求）最高 —— 這是信任門檻極高的
服務品類，這兩類問題直接影響成交。

### draft.py 的寫作規則

產出的文章是**為了「被 AI 引用」而寫，不是為了 Google 排名**：

- 一個標題 = 一個明確問題
- 答案放在段落開頭，可以被獨立擷取
- 事實密度高、有具體數字
- 避免行銷形容詞（模型不引用形容詞，只引用事實）

**永遠不自動發佈。** 產出 `status=draft`，等人審核。

**上面這四條規則要寫成可重複呼叫的檢查函式，不要只寫死在產生流程裡。**
同一組規則有兩個用途：產生新草稿時當寫作準則，以及 `audit.py` 拿去掃已經上線的頁面。
寫成一份、兩邊共用，不要各寫一次 —— 兩份規則遲早會不一致。

---

## Dashboard 八頁

| 頁 | 內容 |
|---|---|
| **總覽** | 量測條件列、四個 KPI（能見度／好感度／風險提及／平均排序）、能見度趨勢圖（含信賴區間陰影 ＋ 行動日期的垂直標記線）、正中負比例堆疊趨勢、各引擎表現、競品比較、重點洞察 |
| **Prompt 監測** | 主題篩選、Prompt 清單表格、點選後右側顯示**完整原始回答**並標出判定依據 |
| **受眾主題** | 主題 × 競品的表現矩陣（色階） |
| **競品比較** | 各競品的 SoV、排名、被引用來源 |
| **來源分析** | Citation 次數、來源影響力排名、來源清單（含「機會等級」欄：引用競品但沒引用我們 = 高機會）、來源類型分布 |
| **機會建議** | 依機會分數排序的缺口表、選定缺口的詳情（原因／四張渠道行動卡：官網內容·社群·PR媒體·UGC／預期成效）、產生草稿的按鈕 |
| **網站健檢** | 自家頁面的 AI 可讀性檢查結果、每頁的問題清單與修法、與「哪些缺口對應到哪些頁面」的對照（階段 6 才做） |
| **設定** | 題庫管理（**支援 CSV 上傳匯入，也可直接在畫面上新增／編輯／停用題目**）、題目建議佇列（加入／忽略）、競品名單、採樣設定 |

---

## 三個名詞（如果程式碼裡看到請照這個意思實作）

**信賴區間** — 「這個數字上下多少範圍內都算沒變」。用 Wilson score interval。
採樣次數越多，區間越窄。

**機會分數** — 決定「這麼多缺口先做哪一個」的排序依據。不是評分，是排序。

**行動標記** — 在趨勢圖上，把 `actions.published_at` 畫成垂直虛線，
讓人一眼看出「線是在哪次行動之後開始上升的」。

---

## 資料從哪裡來

三個來源，只有第一項和第三項需要人操作：

1. **題庫** — 人提供。兩種方式都要支援：
   - 在「設定」頁**上傳 CSV 匯入**
   - 直接在畫面上新增／編輯／停用
   骨架階段先放範例題目，之後由使用者替換成真的題庫。

2. **AI 回應** — 系統自動抓。`collect.py` 依排程去問各家官方 API，
   **使用者不需要手動提供任何回應資料**。

3. **行動紀錄** — 人登記。發完文、聯絡完媒體之後，在「機會建議」頁登記一筆。

其餘所有資料（判讀結果、引用來源、每週指標、缺口清單）都是從上面三項算出來的，
不需要人碰。

---

## 建置順序

**不綁時間。** 每個階段做完、驗收通過，才進下一個。

### 階段 0 — 骨架

十張表建好、七支腳本有可執行的空殼、七頁畫面排好版、一支塞假資料的 seed 腳本。

（這裡的七支／七頁是階段 0 當時的範圍，已完成。`audit.py`、`page_audits` 表和第八頁
「網站健檢」是 2026-09-01 才追加進設計的，屬於階段 6，不在階段 0 的驗收範圍內。）

**驗收條件：**

- 依序執行這三行全部成功：
  `python scripts/init_db.py` → `python scripts/seed_demo.py` → `streamlit run scripts/app.py`
- **七頁全部都有內容可以看**（用假資料）

> ⚠️ **這個階段結束時，畫面絕對不可以是空白的。**
>
> 此時還沒有真題庫、還沒有 API 金鑰，這很正常。但使用者必須能從畫面上看出
> 「這個平台有哪些功能、每一頁長什麼樣、資料會怎麼呈現」。
>
> **假資料的用途是展示功能，不是展示真實數據。** 每頁角落標明「示範資料」即可。

### 階段 1 — 真的問得到 AI

`collect.py` 接上 OpenAI／Anthropic／Perplexity 三家（先不接 Google AI Overviews）。
加一支能列出並閱讀 `runs` 原始回答的腳本。

**驗收：** 跑一輪之後，`data/raw/` 底下有真實的回答檔案，而且使用者能把原文讀出來。

### 階段 2 — 判讀器（品質關卡）

⚠️ **順序不可以顛倒：**

1. **先由人手工標記 100 則** → 存成 `eval/labeled.csv`（這是標準答案）
2. **才寫** `parse.py`
3. 用 `eval/check_judge.py` 拿判讀結果跟標準答案比對

**驗收：** 一致率 ≥ 90%。不到就改判讀 prompt 再測，不要往下走。

### 階段 3 — 指標與信賴區間

`aggregate.py`（含 Wilson score interval）＋ 接 Google AI Overviews（SerpApi）
＋ Dashboard 前三頁換成真資料。

**驗收：** 使用者能人工驗算至少一個指標的中間結果。

### 階段 4 — 缺口與行動

`gaps.py` ＋ 機會分數 ＋ Dashboard 剩下四頁 ＋ `actions` 登記介面。

**驗收：** 系統能產出排序過的缺口清單，使用者能登記一筆行動並在趨勢圖上看到標記線。

### 階段 5 — 自動長大

`suggest.py`（接 Google Search Console）＋ `draft.py`。

### 階段 6 — 網站健檢（AI 可讀性）

`audit.py` ＋ Dashboard 第八頁。

**這一階段補的是整個設計唯一的盲點。** 前五階段都在回答「AI 有沒有提到我們」，
但如果 `fluv.com` 一直沒被引用，沒有任何一頁告訴你**為什麼**。
可能不是內容不夠，而是結構讓模型讀不到 —— 沒有 schema、答案埋在第五段、
`robots.txt` 直接擋掉 AI 爬蟲。

檢查項目（全部是抓自己的頁面做規則比對，**不呼叫任何 AI API、不花錢**）：

- `robots.txt` 有沒有擋掉 AI 爬蟲（GPTBot、ClaudeBot、PerplexityBot、Google-Extended）
- 有沒有 `llms.txt`
- schema.org 標記：`FAQPage` / `Article` / `Organization` 有沒有、對不對
- 標題結構：一頁一個 `h1`，`h2` 是不是一個明確問題
- 答案位置：答案在不在段落開頭（用 `draft.py` 抽出來的那組共用規則）
- 事實密度：有沒有具體數字，形容詞比例會不會太高

**依賴關係：** 只需要階段 4 做完（要先有缺口清單，才知道該檢查哪些頁面）。
和階段 5 沒有先後關係，階段 5 如果卡住可以先做這個。

**驗收：** 對至少一個已知沒被引用的自家頁面，系統能指出具體是哪一項不合格，
而且照著改完之後可以重跑驗證。

### 之後（等公司內部資料可用）

事實錯誤偵測（需要先建品牌權威事實表）· GA4 與預約資料歸因 · AI 爬蟲日誌分析 ·
Slack 週報 · 對照組效果比較

### 刻意不做的（看到同類工具有、但我們不加）

2026-09-01 比對過市面上的 GEO SaaS（Geoptie 等）之後決定：

- **不加 Copilot / Grok 引擎** —— 台灣寵物保姆的客群不會用這些找保姆。
  加引擎會讓每週 API 成本線性上升，換到的是幾乎沒人看的數字。
  引擎範圍就是 OpenAI／Anthropic／Perplexity／Google AI Overviews／Gemini，不再擴充。
- **不做關鍵字自我競食（cannibalization）檢查** —— 那是 Google 排名的問題，不是被引用的問題。
- **不建預存的大型 prompt 資料庫** —— 那類產品回答的是「AI 曾經對某個陌生人說過什麼」，
  我們要的是「AI 現在對我們的題目說什麼，問 N 次，附信賴區間」。是兩個不同的問題。

---

## 給 AI 協作者的提醒

**不要跳過第 2 週的手工標記步驟。** 判讀器是整個系統品質的所在，
也是最容易「安靜出錯」的地方 —— 它不會壞、不會報錯，就是默默判錯一成多，
而使用者不會發現，因為他不會去讀那幾千則原文。

先有標準答案，再有判讀器。這是這個專案唯一真正的品質關卡。

**另外：這個系統的產出是數字，而數字是唯一一種會安靜錯掉的東西。**
專案擁有者無法從程式碼層面驗證正確性，所以任何涉及統計計算的地方，
請主動說明你在算什麼、為什麼這樣算，並提供可以人工驗算的中間結果。
