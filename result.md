# Result: Personal Morning Intelligence Agent — bước 2-9 (full pipeline)

## What was implemented

Toàn bộ §12 bước 2-9 của `morning-intel-agent-brief.md`, dựa trên nền bước 1
đã có (`llm_client.py`, `arxiv.py`, `hackernews.py`, `pipeline.py` Stage 1).

**Nguồn dữ liệu mới** (`src/sources/`):
- `funding.py` — gộp RSS TechCrunch + e27 + DealStreetAsia, mỗi feed fail độc lập.
- `github_trending.py` — RSS không chính thức (mshibanami/GitHubTrendingRSS), type="product".
- `producthunt.py` — GraphQL API, fail-graceful nếu thiếu `PRODUCTHUNT_TOKEN`.
- `deep_tech.py` — IEEE Spectrum RSS + arXiv (eess.SY, eess.SP, physics.app-ph, cond-mat.mtrl-sci), type="deep_tech".
- `source_registry.py` — `SOURCE_TIERS` đúng bảng §6b, `get_tier()` default tier 3, hỗ trợ khớp subdomain.

**Verify tự động** (`src/verify.py`):
- `verify_item()`/`verify_stage()` — Stage 1.5. Tính `confidence` (CONFIRMED/REASONED/SINGLE_SOURCE) bằng code if/else thuần theo tier + cross_confirmed + numeric claim, đúng quy tắc cứng §6b. Cross-reference bằng so khớp token tiêu đề giữa các item Stage 0 cùng batch (không gọi LLM/web-search để quyết định confidence).
- Bất biến cứng: tier 3 không cross-confirm → luôn 🔴, double-enforced (cả trong `compute_confidence` và override cuối `verify_item`).

**Prompts** (`src/prompts.py`): `ANALYSIS_SYSTEM_PROMPT` và `WEEKLY_SYSTEM_PROMPT` copy nguyên văn từ §7 brief.

**pipeline.py mở rộng**:
- `tag_outside_candidates()` — retag item match `outside_lane_domains` (và không match thesis chính) thành `type="outside"` (không có nguồn nào tự emit type này).
- `filter_stage1()` — sửa lại cắt top-N theo TỪNG type, không cắt toàn cục (xem mục Deviations).
- `llm_rank_and_select()` — Stage 1 LLM batch ranking, fallback heuristic score thuần nếu thiếu key/LLM lỗi/parse fail.
- `analyze_stage2()` — gọi LLM thật với ANALYSIS_SYSTEM_PROMPT, parse digest + JSON cuối (strip ```), fallback message rõ nếu thiếu key, log digest vẫn gửi nếu JSON parse fail.
- `build_kb_summary()` — tóm tắt top themes/companies/investors/tech/deep_tech từ kb.json.

**memory.py** (Stage 3): `load_kb`/`save_kb`/`merge_kb_update` đúng schema §6 (count/first_seen/last_seen, investors tích từ company rounds), `save_archive`, `update_seen_ids` (dedupe + trim 5000 id gần nhất).

**weekly.py**: đọc 7 archive gần nhất + kb.json, gọi LLM với WEEKLY_SYSTEM_PROMPT, lưu `archive/weekly-YYYY-WW.md`, gửi qua deliver.py.

**deliver.py** (Stage 4): header ngày+thứ tiếng Việt, split message theo 4096 ký tự (cắt theo ranh giới dòng), Telegram Bot API raw HTTP, tôn trọng `DRY_RUN`.

**main.py**: nối full pipeline Stage 0→4, mọi nguồn fail độc lập, mọi bước cần LLM check key trước và degrade gracefully.

**GitHub Actions**: `daily.yml` (cron `0 23 * * *` UTC, `permissions: contents: write`, commit+push state/archive/knowledge), `weekly.yml` (cron Chủ nhật).

**Tests** (`tests/`): 22 unit test (test_verify.py 8, test_source_registry.py 6, test_memory.py 8) — tất cả PASS, không cần pytest cài sẵn (mỗi file tự chạy được).

**README.md**: đầy đủ cách lấy 5 secret, cách sửa thesis.yaml, cách thêm domain vào source_registry, cảnh báo data privacy Gemini.

## Tests: 22/22 pass

```
tests/test_verify.py:           8/8 passed
tests/test_source_registry.py:  6/6 passed
tests/test_memory.py:           8/8 passed
```

## Deviations from spec (với lý do)

1. **Stage 1 filter cắt top-N theo TỪNG type, không cắt toàn cục.** Brief §5 nói "Cắt còn ~20 item... Sau đó một lần gọi LLM rẻ (batch) để xếp hạng theo độ liên quan với thesis, chọn ra phân bổ". Khi build thật, phát hiện cắt top-20 toàn cục trước khi phân bổ làm GitHub Trending (luôn +5 điểm "freshness" hệ thống vì feed không có ngày publish theo từng repo) áp đảo hoàn toàn, loại sạch funding/deep_tech khỏi top-20 trước khi LLM ranking kịp chọn theo cơ cấu. Sửa: chia `keep_top_n` đều cho số type đang có (tối thiểu 4/type), giữ tinh thần "top N" nhưng đảm bảo mọi nhánh sống sót để allocation chọn đúng 2/1/1/1/1.
2. **`type="outside"` không có fetcher riêng.** Brief không định nghĩa nguồn riêng cho outside-lane, chỉ định nghĩa `outside_lane_domains` trong thesis.yaml. Giải quyết bằng `tag_outside_candidates()`: retag item match outside domains (và KHÔNG match thesis chính) thành outside, copy item gốc (không sửa instance gốc để không phá phân loại khác). Nếu hôm đó không có item nào match outside domains thật, mục outside sẽ trống — đúng nguyên tắc "thà thiếu còn hơn ép tin không liên quan", brief cũng cho phép "nếu không có ứng viên outside, chủ động lấy 1 essay/quan điểm từ nguồn ngoài lane" nhưng việc tự generate essay nằm ngoài scope nguồn dữ liệu hiện có — chưa implement nhánh "chủ động tìm essay riêng".
3. **funding.py chỉ giữ đúng 3 feed theo yêu cầu task này** (TechCrunch, e27, DealStreetAsia) thay vì toàn bộ danh sách §4 (Sifted, Tech in Asia, KrASIA, Crunchbase News...). Các domain đó đã có sẵn trong `SOURCE_TIERS` để dễ bổ sung URL feed sau, không cần sửa registry.
4. **Cross-reference trong verify.py** dùng so khớp token tiêu đề trong batch Stage 0 đã fetch, KHÔNG gọi LLM web-search-style như brief gợi ý nhánh 2 ("1 lần gọi LLM dùng web-search-style query nếu pipeline có quyền truy cập search"). Pipeline này không có search tool thật, nên dùng nhánh đầu brief cho phép ("tự thử tìm thêm... trong các item đã fetch ở Stage 0") — rẻ hơn, xác định được, không phụ thuộc LLM quyết định confidence.
5. **`requirements.txt` thêm `feedparser`** (không có trong brief) để parse RSS funding/deep_tech/github_trending — quyết định vì RSS thật thường lệch chuẩn, feedparser xử lý tốt hơn tự viết XML parser cho 5+ feed khác nhau, vẫn nhẹ.
6. **`update_seen_ids` trim còn 5000 id gần nhất** — brief không nói rõ chiến lược giới hạn kích thước seen.json theo thời gian; chọn giới hạn để file không phình vô hạn.

## Risks / concerns

- Chưa test được với API key thật (Gemini/DeepSeek/Telegram/ProductHunt) — xem phần (d) bên dưới.
- `funding.py`/`deep_tech.py` phụ thuộc cấu trúc RSS feed bên thứ 3 (TechCrunch, e27, DealStreetAsia, IEEE Spectrum, GitHub Trending RSS không chính thức) — nếu họ đổi domain/markup, feed có thể fail (đã fail-graceful, nhưng sẽ giảm chất lượng item type đó).
- `tag_outside_candidates` dùng so khớp keyword đơn giản (không NLP) nên có thể tag nhầm/tag thiếu outside candidate — chấp nhận được ở mức MVP theo "đơn giản, không over-engineer".
- Stage 2 prompt gửi `raw_text[:1500]` mỗi item — nếu RSS summary rất dài, có thể cắt mất phần claim quan trọng. Chưa optimize.
- DealStreetAsia trả 503 trong lúc test (server-side, không phải lỗi code) — đã handle đúng (log warning, bỏ qua, pipeline tiếp tục).

## Next steps (gợi ý, không phải bắt buộc)

- Operator add `GEMINI_API_KEY` để chạy thử Stage 1 LLM ranking + Stage 2 analyze thật, kiểm tra chất lượng digest 6 mục.
- Add `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_IDS` để test gửi thật (set `DRY_RUN=false` cục bộ 1 lần để xác nhận).
- Nếu muốn thêm Sifted/Tech in Asia/KrASIA vào funding.py, chỉ cần thêm dòng vào `FEEDS` dict trong `funding.py` (domain đã có tier sẵn trong `source_registry.py`).
- Sau khi có >=7 ngày archive thật, chạy `weekly.py` với key thật để xác nhận chất lượng tổng hợp tuần.
