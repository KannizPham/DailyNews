# Build brief: Personal Morning Intelligence Agent

Bạn là Claude Code. Hãy build một hệ thống agent chạy tự động, phục vụ **một người dùng duy nhất** (tên gọi nội bộ: "operator"), gửi một bản digest cố định mỗi sáng 6h (giờ VN, Asia/Ho_Chi_Minh).

Đọc toàn bộ brief trước khi viết code. Ưu tiên: **đơn giản, chi phí ~$0, dễ sửa**. Không over-engineer.

---

## 0. Mục tiêu & triết lý (đọc để ra quyết định đúng)

Operator đang trên lộ trình: nghiên cứu AI → quant researcher → capital allocator → founder ở Đông Nam Á. Mục tiêu của sản phẩm KHÔNG phải "gom tin", mà là:

1. **Hiểu tại sao** — tại sao người ta build cái đó, tại sao họ được rót vốn.
2. **Mở rộng góc nhìn** — chống filter bubble, không chỉ đọc thứ đã thích.
3. **Tích tụ theo thời gian** — kiến thức phải *compound*. Hệ thống phải nhớ, rút pattern, và định kỳ tự tổng hợp lại sự dịch chuyển. Đây là yêu cầu lõi, không phải tính năng phụ.

Hệ quả thiết kế: phần "phân tích" và phần "trí nhớ tích luỹ" quan trọng hơn phần "lấy tin". Đừng dồn công vào scraping; dồn công vào prompt phân tích và knowledge base.

4. **Deep tech / hard tech, góc nhìn kỹ thuật:** ngoài AI/startup/funding, hệ thống phải cover xe điện & tự lái, năng lượng & pin, vật liệu, robotics, semiconductor — và không chỉ tin tức, mà phải kéo ra được *nguyên lý kỹ thuật*: tại sao bài toán này khó, tại sao giải pháp này work về mặt vật lý/kỹ thuật, không chỉ "ai vừa ra mắt cái gì".

5. **Tự động hoá tối đa + kiểm chứng chất lượng:** pipeline phải tự lọc nguồn theo độ uy tín, tự cross-reference, tự gắn mức độ tin cậy cho từng claim — KHÔNG cần operator làm thao tác kiểm tra thủ công nào trong vòng lặp hàng ngày. Toàn bộ việc "verify" nằm trong code/prompt, chạy không người can thiệp.
   - Giới hạn thật của tự động hoá: pipeline giảm tối đa xác suất sai và làm mức độ tin cậy của mỗi claim **hiện rõ ra** (nhãn 🟢🟡🔴, xem §6b) để operator biết ngay câu nào dùng thẳng được, câu nào nên tự kiểm trước khi dùng để quyết định việc lớn. Đây không phải bước thủ công còn sót — nhãn tin cậy LÀ một phần của output tự động, không phải việc operator phải tự làm.

Mọi output bằng **tiếng Việt**, giữ thuật ngữ tiếng Anh khi tự nhiên (làm thành biến config `OUTPUT_LANG`).

---

## 1. Ràng buộc kỹ thuật

- **Ngôn ngữ:** Python 3.11+.
- **Hạ tầng:** KHÔNG VPS, KHÔNG database. Chạy bằng **GitHub Actions** (scheduled cron). State và archive lưu bằng file commit ngược lại vào repo.
- **LLM:** engine chính = **Google Gemini Flash-Lite** (free tier, đủ dùng cho summarize/analyze, gọi 1-2 lần/ngày nên nằm gọn trong quota free). Fallback = **DeepSeek V4 Flash** (`deepseek-v4-flash`, endpoint Anthropic-compatible `https://api.deepseek.com/anthropic`) khi Gemini lỗi/429. Viết một lớp `llm_client` trừu tượng để đổi engine bằng config, không hardcode.
  - LƯU Ý DỮ LIỆU: Gemini free tier có thể dùng prompt để train. Chỉ feed tin công khai. Không feed dữ liệu cá nhân/research riêng. Ghi cảnh báo này trong README.
- **Output:** Telegram bot (free). Gửi tới một chat_id cố định.
- **Chi phí mục tiêu:** $0 hạ tầng, < $1/tháng API (gần như luôn $0 nếu Gemini free tier đủ).
- **Mọi secret** qua biến môi trường / GitHub Secrets, không hardcode.

---

## 2. Cấu trúc thư mục

```
morning-intel/
├── README.md
├── requirements.txt
├── config/
│   └── thesis.yaml          # operator tự sửa: đang track gì, coi gì là "matter"
├── src/
│   ├── main.py              # entrypoint: chạy pipeline ngày
│   ├── weekly.py            # entrypoint: tổng hợp tuần
│   ├── sources/             # mỗi file = 1 nguồn, trả về list[Item] chuẩn hoá
│   │   ├── arxiv.py
│   │   ├── hackernews.py
│   │   ├── github_trending.py
│   │   ├── producthunt.py
│   │   ├── funding.py       # gộp các RSS funding
│   │   ├── deep_tech.py     # IEEE Spectrum, eng blogs, eess/physics arXiv (xem §4b)
│   │   └── source_registry.py  # bảng tier uy tín cho MỌI nguồn (xem §6b)
│   ├── llm_client.py        # lớp trừu tượng Gemini + DeepSeek fallback
│   ├── pipeline.py          # filter (stage 1) + analyze (stage 2)
│   ├── verify.py            # cross-reference + gắn nhãn tin cậy tự động (xem §6b)
│   ├── memory.py            # knowledge base: đọc/ghi/append, dedupe
│   ├── deliver.py           # format + gửi Telegram
│   └── prompts.py           # chứa 2 system prompt (analysis, weekly)
├── state/
│   └── seen.json            # hash các item đã gửi, để dedupe across days
├── archive/                 # bản digest mỗi ngày, dùng cho tổng hợp tuần
│   └── 2026-06-18.md
├── knowledge/
│   └── kb.json              # LỚP TÍCH LUỸ: themes, companies, investors, tech + đếm + ngày
└── .github/workflows/
    ├── daily.yml
    └── weekly.yml
```

Mọi item nội bộ chuẩn hoá thành dataclass:
`Item(id, type, title, url, source, source_tier, published_at, raw_text, score)`
với `type ∈ {research, funding, product, deep_tech, outside}` và `source_tier ∈ {1, 2, 3}` (xem bảng tier ở §6b).

---

## 3. config/thesis.yaml (operator tự sửa)

Đây là "lăng kính". Agent đọc file này làm context cho mọi phân tích. Tạo file mẫu:

```yaml
output_lang: vi
tracking:
  - diffusion models / generative modeling
  - optimal transport
  - uncertainty / OOD / long-tailed learning
  - quant trading & market microstructure
  - SEA fintech / payments / startup ecosystem
  - capital allocation / VC thesis / moats
deep_tech_tracking:            # góc nhìn KỸ THUẬT, không phải tin tức bề mặt
  - xe điện & tự lái (battery chemistry, sensor stack, autonomy stack)
  - năng lượng & pin (battery tech, grid, energy storage)
  - vật liệu mới (semiconductor, advanced materials)
  - robotics (actuators, control, manipulation)
what_counts_as_matters: >
  Ưu tiên cái thay đổi cách một thị trường/bài toán vận hành, hoặc lộ ra một
  bet về tương lai. Với deep tech: ưu tiên giải thích được NGUYÊN LÝ KỸ THUẬT
  tại sao nó khó / tại sao nó work, không chỉ tin ra mắt sản phẩm. Bỏ qua tin
  PR, version bump, drama.
outside_lane_domains:        # nguồn cho mục "mở rộng góc nhìn"
  - biotech / synthetic bio
  - energy / climate tech (nếu không trùng deep_tech_tracking)
  - geopolitics & trade
  - design / consumer behavior
keywords_boost: [moat, distribution, why now, unit economics, defensibility]
```

---

## 4. Nguồn dữ liệu (tất cả free)

Viết mỗi nguồn thành module riêng, fail độc lập (một nguồn chết thì pipeline vẫn chạy tiếp, log warning).

| type | nguồn | cách lấy |
|---|---|---|
| research | arXiv API | endpoint `http://export.arxiv.org/api/query`, filter categories cs.LG, cs.AI, stat.ML, q-fin; lấy 24h gần nhất |
| research | Hacker News | Algolia HN API `http://hn.algolia.com/api/v1/search_by_date` (free, không key); lọc `points > 50` |
| research | GitHub Trending | RSS không chính thức hoặc parse trang trending (daily) |
| product | Product Hunt | API GraphQL (cần token free) hoặc RSS |
| funding | TechCrunch RSS, Crunchbase News RSS | RSS |
| funding | **Sifted** (EU), **e27**, **Tech in Asia**, **DealStreetAsia**, **KrASIA** (SEA) | RSS — ƯU TIÊN, vì SEA là theater của operator |
| thesis/why | a16z, Sequoia, YC Requests for Startups, Stratechery | RSS/page, fetch định kỳ (đổi chậm, không cần mỗi ngày) |

### 4b. Nguồn deep tech / kỹ thuật (`deep_tech.py`)

Mục tiêu khác bảng trên: không lấy tin "ai ra mắt gì", mà lấy nội dung giải thích được *nguyên lý*. Ưu tiên nguồn tier 1-2 (xem §6b):

| domain | nguồn | cách lấy |
|---|---|---|
| xe điện/tự lái, robotics, vật liệu, năng lượng | **IEEE Spectrum** | RSS, tier 1 — bài có editorial kỹ thuật, không phải PR |
| mọi domain deep tech | **arXiv eess, physics.app-ph, cond-mat** | arXiv API, cùng cơ chế với `arxiv.py` nhưng category khác |
| pin/battery | bài research blog của CATL, Tesla Engineering Blog, Waymo Tech Blog (nếu có RSS) | RSS, tier 1 (nguồn chính chủ kỹ thuật) |
| semiconductor | Asianometry-style deep dive, IEEE Spectrum semiconductor tag | RSS |
| patent (tín hiệu sớm) | Google Patents Public Data / patent RSS theo từ khoá | optional, có thể bỏ ở bản đầu nếu tốn công |

Mỗi item từ nguồn này gắn `type="deep_tech"`. Stage 2 phải yêu cầu LLM trả lời được "tại sao đây khó về kỹ thuật" — nếu nguồn không đủ chi tiết để trả lời, KHÔNG được tự bịa cơ chế vật lý, phải nói "nguồn không đủ sâu để giải thích nguyên lý".

---

## 5. Pipeline ngày (`main.py`)

**Stage 0 — Fetch:** gọi mọi source, gộp, chuẩn hoá thành `list[Item]`.

**Stage 1 — Filter (rẻ, ưu tiên rule-based):**
- Bỏ item đã có trong `state/seen.json` (dedupe theo hash của url/title).
- Heuristic chấm điểm: khớp keyword trong `thesis.yaml`, độ mới, HN points, match category arXiv.
- Cắt còn ~20 item (tăng từ 15 vì giờ có thêm nhánh deep_tech). Sau đó **một lần gọi LLM rẻ** (batch) để xếp hạng theo độ liên quan với thesis, chọn ra phân bổ:
  - 2 `research`, 1-2 `funding`, 1 `product`, 1 `deep_tech`, 1 `outside`.
- Mục `outside` BẮT BUỘC chọn từ `outside_lane_domains` — cố ý lệch khỏi thesis. Nếu không có ứng viên outside, chủ động lấy 1 essay/quan điểm từ nguồn ngoài lane.
- Mục `deep_tech` ưu tiên item có nguồn tier 1-2 (xem §6b); nếu chỉ có tier 3, vẫn lấy nhưng đánh dấu để Stage 1.5 cross-check kỹ hơn.

**Stage 1.5 — Verify tự động (`verify.py`, KHÔNG có bước thủ công):**
Trước khi đưa ~6 item đã chọn vào Stage 2, chạy kiểm chứng tự động cho mọi claim quan trọng (đặc biệt funding số liệu và claim kỹ thuật deep_tech):
- Tra `source_registry.py` để lấy tier của nguồn gốc.
- Với claim có số liệu (số tiền, %, tên nhà đầu tư) hoặc claim kỹ thuật cụ thể: tự thử tìm thêm tối thiểu 1 nguồn độc lập khác nói cùng sự kiện (search lại trong các item đã fetch ở Stage 0, hoặc 1 lần gọi LLM dùng web-search-style query nếu pipeline có quyền truy cập search). Đánh dấu `cross_confirmed: true/false`.
- Tính `confidence` cho từng item/claim theo quy tắc cứng (không để LLM tự quyết, để đảm bảo nhất quán):
  - 🟢 `confirmed`: tier 1-2 VÀ (cross_confirmed=true HOẶC là số liệu trích trực tiếp có link).
  - 🟡 `reasoned`: là suy luận/phân tích (không phải fact trích xuất), dựa trên dữ kiện tier 1-2.
  - 🔴 `single_source`: chỉ tier 3, hoặc tier 1-2 nhưng không cross-confirm được, hoặc là suy luận dựa trên dữ liệu tier 3.
- Output Stage 1.5 là list item kèm `confidence` gắn sẵn cho từng claim con — Stage 2 PHẢI dùng nhãn này khi viết, không tự gắn lại.

**Stage 2 — Analyze (LLM, prompt trong `prompts.py`):**
- Một lần gọi với toàn bộ ~6 item đã chọn (kèm nhãn confidence từ Stage 1.5) + nội dung `thesis.yaml` + **tóm tắt knowledge base hiện tại** (top themes/investors/tech đang nổi, lấy từ `kb.json`).
- Trả về 2 thứ trong cùng response: (a) digest người đọc (markdown, 6 mục theo trình tự cố định, mỗi claim quan trọng có nhãn 🟢🟡🔴 đi kèm), (b) một block JSON cuối có cấu trúc để cập nhật KB (xem §6). Parse JSON an toàn (strip ```), nếu parse fail thì vẫn gửi digest, log lỗi.

**Stage 3 — Accumulate (`memory.py`):** append entities từ block JSON vào `kb.json` (đếm số lần xuất hiện, ngày đầu/cuối thấy). Lưu digest vào `archive/YYYY-MM-DD.md`. Cập nhật `state/seen.json`.

**Stage 4 — Deliver:** format Telegram, gửi. Commit `state/`, `archive/`, `knowledge/` ngược vào repo.

---

## 6. LỚP TÍCH LUỸ (quan trọng nhất — đừng cắt gọn)

### knowledge/kb.json
Cấu trúc tích luỹ theo thời gian:
```json
{
  "themes":    {"<tên theme>": {"count": 7, "first_seen": "2026-05-01", "last_seen": "2026-06-18", "notes": []}},
  "companies": {"<tên cty>":   {"count": 3, "rounds": [...], "first_seen": "...", "last_seen": "..."}},
  "investors": {"<tên quỹ>":   {"count": 5, "pattern_notes": [...]}},
  "tech":      {"<tên kỹ thuật>": {"count": 4, "first_seen": "...", "last_seen": "..."}},
  "deep_tech": {"<tên cơ chế/nguyên lý kỹ thuật>": {"count": 2, "domain": "battery/autonomy/...", "first_seen": "...", "last_seen": "..."}}
}
```
Mỗi ngày stage 2 trả block JSON dạng:
```json
{"themes": ["..."], "companies": [{"name":"...","round":"...","investors":["..."]}], "tech": ["..."], "deep_tech": [{"name":"...","domain":"..."}]}
```
`memory.py` merge vào kb.json (tăng count, cập nhật ngày).

### Tổng hợp tuần (`weekly.py`, chạy Chủ nhật)
Đây là chỗ kiến thức *compound* và "thay đổi" operator. Đọc 7 file `archive/` gần nhất + `kb.json`, gọi LLM với **weekly prompt** (§7) để sinh ra một bản tổng hợp:
- Pattern nào lặp lại / đang nổi lên (dựa trên count tăng trong kb.json).
- Cái gì đang dịch chuyển so với tuần trước.
- Một quỹ/founder/bet đáng đào sâu.
- "Góc nhìn của bạn nên cập nhật ở đâu" — 2-3 câu thẳng, có thể nghịch với giả định hiện tại của operator.
- Lưu vào `archive/weekly-YYYY-WW.md` và gửi Telegram.

---

## 6b. Source tier registry + cơ chế tin cậy tự động (`source_registry.py`, `verify.py`)

Đây là cơ chế trả lời yêu cầu "chỉ lấy nguồn xịn, tự kiểm chứng". Nguyên tắc: **mọi việc verify nằm trong code/prompt, chạy tự động, không có bước nào operator phải làm tay.** Phần duy nhất "lộ ra" cho operator là nhãn tin cậy trên output — đó là kết quả của tự động hoá, không phải thay cho nó.

### Bảng tier (hardcode trong `source_registry.py`, dễ sửa)
```python
SOURCE_TIERS = {
    # tier 1 — chính chủ, editorial mạnh, hoặc primary source
    "arxiv.org": 1, "ieee.org": 1, "techcrunch.com": 1, "crunchbase.com": 1,
    "sequoiacap.com": 1, "a16z.com": 1, "stratechery.com": 1,
    # tier 2 — uy tín trong ngành/khu vực, editorial có nhưng nhỏ hơn
    "sifted.eu": 2, "e27.co": 2, "techinasia.com": 2, "dealstreetasia.com": 2,
    "kr-asia.com": 2, "ycombinator.com": 2,
    # tier 3 — aggregator/forum, dùng được nhưng phải cross-check
    "news.ycombinator.com": 3, "producthunt.com": 3, "github.com": 3,
}
# default nếu domain lạ: tier 3 (an toàn — coi như chưa uy tín tới khi được thêm vào bảng)
```
Domain ngoài danh sách → tier 3 mặc định. Việc "thêm nguồn mới" = thêm 1 dòng vào dict này, để operator kiểm soát được nguồn nào được tin tưởng mức nào, không để LLM tự quyết.

### Quy tắc cứng cho confidence (không giao cho LLM tự đánh giá, để nhất quán & khó bị thao túng bởi prompt injection từ nội dung nguồn)
- `verify.py` tính `confidence` bằng code thường (if/else theo tier + cross_confirmed), KHÔNG hỏi LLM "claim này tin được không". LLM chỉ được dùng để *tìm* nguồn thứ hai (cross-reference), không được dùng để *quyết* mức tin cậy.
- Mọi item tier 3 không cross-confirm được → ép `🔴 single_source`, không có ngoại lệ, dù LLM "cảm thấy" nó đúng.
- Claim dạng số liệu cụ thể (tiền, %, ngày) bắt buộc phải trích được nguồn/link cụ thể trong raw_text; nếu không trích được → tự động `🔴`, dù tier nguồn là gì.

### Output ra digest
Mỗi claim quan trọng trong digest (đặc biệt mục 2 — funding, và mục deep_tech) phải có nhãn ngay sau nó, ví dụ: "Series B $40M dẫn dắt bởi Quỹ X 🟢" hoặc "có thể đây là bet vào pin thể rắn thay LFP 🟡". Operator đọc lướt vẫn thấy ngay câu nào chắc, câu nào cần tự kiểm trước khi dựa vào nó để quyết định việc lớn.

---

## 7. Prompts (đặt trong `src/prompts.py`)

### ANALYSIS_SYSTEM_PROMPT
```
Bạn là research analyst riêng của một người đang đi từ nghiên cứu AI → quant →
capital allocator → founder ở Đông Nam Á. Nhiệm vụ: giúp họ HIỂU TẠI SAO và MỞ
RỘNG GÓC NHÌN, không phải tóm tin.

Bạn nhận: (1) thesis của họ, (2) ~6 item đã chọn kèm loại VÀ nhãn confidence đã
được gắn sẵn từ bước verify tự động (🟢/🟡/🔴 — KHÔNG tự đổi nhãn này, chỉ
dùng lại), (3) tóm tắt knowledge base (pattern đang tích luỹ). Trả về digest
tiếng Việt theo ĐÚNG 6 mục, theo đúng thứ tự, ngắn gọn, không sáo rỗng. MỌI
claim cụ thể (số liệu, tên, cơ chế kỹ thuật) phải mang đúng nhãn confidence đã
được gắn cho nó — không tự nâng hay hạ nhãn:

1. TÍN HIỆU — cái gì thực sự dịch chuyển (2 mục). Mỗi mục: nó là gì + tại sao
   matter. Bỏ qua cái chỉ "hot".
2. LĂNG KÍNH TIỀN — tại sao họ được rót vốn (1-2 vòng). VỚI MỖI VÒNG, tách rõ
   hai phần có nhãn:
     • "Đã biết:" số tiền, nhà đầu tư, điều họ TUYÊN BỐ (chỉ ghi nếu có trong
       nguồn; KHÔNG bịa số liệu; thiếu thì ghi "không rõ"). Gắn nhãn confidence.
     • "Suy luận của tôi:" bet đằng sau, why now, moat, pattern của quỹ — luôn
       🟡 hoặc 🔴, không bao giờ 🟢 vì đây là suy đoán, không phải fact.
   TUYỆT ĐỐI không trộn hai phần. Nếu nguồn quá nghèo để suy luận, nói thẳng.
3. LĂNG KÍNH NGƯỜI XÂY — tại sao họ build cái này (1 sản phẩm): wedge là gì,
   tại sao cách này, phòng thủ ở đâu.
4. GÓC NHÌN KỸ THUẬT / DEEP TECH (1 mục): một item từ domain hard tech (xe điện,
   pin, vật liệu, robotics, semiconductor...). Phải trả lời được "tại sao đây
   khó về mặt kỹ thuật/vật lý" và "tại sao giải pháp này work" — không chỉ kể
   sản phẩm. Nếu nguồn không đủ sâu để giải thích nguyên lý, nói thẳng "nguồn
   không đủ chi tiết kỹ thuật" thay vì tự suy ra cơ chế vật lý không có căn cứ.
5. NGOÀI ĐƯỜNG RAY (1 mục): phải thực sự lệch khỏi thesis. Mục đích là phá bong
   bóng, nên đừng kéo về sở thích quen.
6. NỐI ĐIỂM (1 đoạn): nối các mục hôm nay thành một pattern, liên hệ với knowledge
   base ("khớp với pattern X đã thấy N lần") và với lộ trình quant/capital/SEA của
   họ. Kết bằng ĐÚNG 1 câu hỏi để họ nghĩ chủ động.

Nguyên tắc xuyên suốt: thà nói "không đủ thông tin" còn hơn bịa một lý do nghe
hợp lý. Phân biệt rạch ròi điều đã biết với điều bạn suy luận. Nhãn confidence
không phải trang trí — người đọc dựa vào nó để biết câu nào cần tự kiểm trước
khi dùng để quyết định việc lớn, nên gắn đúng, đừng gắn 🟢 cho thứ thực ra là 🟡.

Sau digest, in một dòng "---JSON---" rồi một block JSON đúng schema:
{"themes": [...], "companies": [{"name","round","investors"}], "tech": [...],
 "deep_tech": [{"name","domain"}]}
Không thêm chữ nào sau JSON.
```

### WEEKLY_SYSTEM_PROMPT
```
Bạn nhận 7 digest gần nhất + knowledge base tích luỹ (gồm cả deep_tech). Viết một
bản tổng hợp tuần tiếng Việt, mục tiêu là làm kiến thức compound và đẩy người đọc
thay đổi góc nhìn:
1. Pattern lặp lại / đang nổi (dựa count tăng trong KB, gồm cả nguyên lý kỹ thuật
   lặp lại trong deep_tech) — 2-3 cái.
2. Cái gì dịch chuyển so với mạch trước.
3. Một bet/quỹ/founder đáng đào sâu, kèm lý do.
4. "Góc nhìn của bạn nên cập nhật ở đâu" — 2-3 câu thẳng, được phép nghịch với
   giả định hiện tại của họ. Không nịnh.
Ngắn, đậm đặc, không liệt kê lại tin. Đây là phần phản tư, không phải bản tin.
```

---

## 8. Output Telegram (`deliver.py`)
- Markdown, dùng emoji tiết chế cho 6 tiêu đề mục để dễ quét. Giữ nhãn 🟢🟡🔴 nguyên trong text, không bỏ khi format.
- Header cố định: ngày + thứ.
- Nếu digest dài quá giới hạn Telegram (4096 ký tự), tách message.
- Có biến `DRY_RUN`: nếu true thì in ra console thay vì gửi (để test local).

---

## 9. GitHub Actions
- `daily.yml`: cron `0 23 * * *` UTC (= 6h sáng VN), chạy `python src/main.py`, rồi commit & push các thư mục state/archive/knowledge.
- `weekly.yml`: cron Chủ nhật, chạy `python src/weekly.py`.
- Dùng `permissions: contents: write` để commit ngược.

## 10. Secrets (GitHub Secrets / .env)
`GEMINI_API_KEY`, `GEMINI_MODELS`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`,
`PRODUCTHUNT_TOKEN` (nếu dùng). README hướng dẫn cách lấy từng cái.

---

## 11. Acceptance criteria
1. `DRY_RUN=true python src/main.py` chạy được local, in ra digest 6 mục đúng thứ tự, không cần Telegram.
2. Một nguồn lỗi (vd arXiv timeout) KHÔNG làm sập cả pipeline.
3. Item đã gửi hôm trước không xuất hiện lại (dedupe hoạt động).
4. Sau khi chạy, `kb.json` được cập nhật count (gồm cả `deep_tech`); `archive/<ngày>.md` được tạo.
5. Mục funding luôn tách rõ "Đã biết" vs "Suy luận của tôi", và mọi claim đều có nhãn 🟢🟡🔴 đúng với confidence được `verify.py` gắn — không phải LLM tự gắn.
6. Item nguồn tier 3 không cross-confirm được PHẢI hiện 🔴 trong output (test bằng cách giả một item tier 3 đơn lẻ).
7. Mục deep_tech: nếu nguồn không đủ chi tiết kỹ thuật, digest phải nói thẳng điều đó, không tự suy ra cơ chế vật lý không có căn cứ.
8. `weekly.py` đọc được archive và sinh bản tổng hợp tuần.
9. Chi phí thực tế ~$0 (log số token mỗi lần gọi để kiểm chứng).
10. Toàn bộ pipeline (fetch → filter → verify → analyze → deliver) chạy hết trong 1 lệnh, không có bước nào yêu cầu input từ operator giữa chừng.

## 12. Thứ tự build (làm tăng dần, đừng làm hết một lần)
1. `llm_client` + `arxiv` + `hackernews` + filter heuristic + `DRY_RUN` in ra mục 1. Chạy được rồi mới đi tiếp.
2. Thêm nguồn funding (TechCrunch + e27 + DealStreetAsia) + mục 2.
3. `source_registry.py` + `verify.py` (tier + cross-reference + quy tắc confidence cứng). Làm TRƯỚC khi viết prompt phân tích đầy đủ, vì Stage 2 phụ thuộc vào nhãn này.
4. Thêm nguồn `deep_tech.py` (IEEE Spectrum + arXiv eess/physics) + mục deep tech.
5. Viết `prompts.py` analysis đầy đủ 6 mục + parse JSON. (Tốn nhất, lặp nhiều.)
6. `memory.py` + kb.json (gồm field deep_tech) + archive.
7. `weekly.py` + tổng hợp tuần.
8. Telegram + 2 workflow GitHub Actions.
9. README đầy đủ (cách set secrets, cách sửa thesis.yaml, cách thêm domain vào source_registry, cảnh báo data privacy Gemini).

Khi xong từng bước, dừng lại cho operator chạy thử `DRY_RUN` trước khi sang bước sau.
