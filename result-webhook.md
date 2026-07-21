# Result: Tương tác real-time qua Cloudflare Worker (webhook Telegram)

## What was implemented

1. **`worker/wrangler.toml`** — config Cloudflare Worker (`ban-tin-kinh-te-thi-truong`),
   binding KV `DIGEST_KV`, vars `GITHUB_OWNER`/`GITHUB_REPO`/`GEMINI_MODELS`.
   Operator phải điền `id` namespace KV và owner/repo thật trước khi deploy.

2. **`worker/src/index.js`** — JS thuần (không build step), xử lý:
   - `/start` → welcome message.
   - `/refresh` → `POST /repos/{owner}/{repo}/dispatches` (event_type
     `refresh-digest`) dùng `GITHUB_PAT`, reply xác nhận hoặc lỗi rõ.
   - `callback_query` `"qa:<idx>"` → `answerCallbackQuery` trước (tắt loading),
     đọc `item:<idx>` từ KV, gọi Gemini raw HTTP (`generateContent`), reply.
   - text tự do khác (không phải `/command`) → đọc `latest_items` từ KV, feed
     toàn bộ context vào Gemini cùng câu hỏi, reply tiếng Việt.
   - Mọi lỗi (KV rỗng/hỏng, Gemini lỗi, GitHub API lỗi, thiếu secret) đều
     `sendMessage` báo rõ cho operator — không có nhánh nào để Telegram
     timeout im lặng.

3. **`src/deliver.py`** — thêm:
   - `build_inline_keyboard(verified_items)` — tạo `reply_markup` với 1 nút
     "🔍 Hỏi sâu thêm: <tên ngắn>" / item, `callback_data="qa:<idx 1-based>"`.
   - `write_items_to_kv(verified_items)` — ghi `item:<idx>` (context rút gọn:
     title/url/raw_text ≤1500 ký tự/type/confidence) + `latest_items` (toàn
     bộ list) lên Cloudflare KV qua REST API, TTL 48h. Graceful-degrade: thiếu
     `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_KV_NAMESPACE_ID`
     → log warning, trả `False`, KHÔNG raise.
   - `deliver()` nhận thêm `reply_markup` optional, chỉ gắn vào chunk Telegram
     cuối cùng (tránh lặp nút ở message bị tách do quá 4096 ký tự).
   - `send_telegram_message()` nhận thêm `reply_markup` optional.

4. **`src/main.py`** — sau Stage 3 (accumulate), thêm Stage 3.5: gọi
   `write_items_to_kv(verified_items)` rồi `build_inline_keyboard(verified_items)`,
   truyền `reply_markup` vào `deliver()`. Toàn bộ optional, không block Stage 4
   nếu thiếu secret Cloudflare.

5. **`.github/workflows/daily.yml`** — thêm trigger
   `repository_dispatch: types: [refresh-digest]` (giữ nguyên `schedule` +
   `workflow_dispatch`), thêm 3 env Cloudflare vào step "Run daily pipeline".

6. **`.env.example`** — thêm 3 biến optional `CLOUDFLARE_API_TOKEN`,
   `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_KV_NAMESPACE_ID` kèm comment rõ
   "không làm sập pipeline nếu thiếu".

7. **`README.md`** — thêm mục "Thiết lập tương tác real-time (Cloudflare
   Workers)" đầy đủ 9 bước copy-paste (wrangler install/login → kv namespace
   create → sửa wrangler.toml → secret put × 3 → deploy → setWebhook curl →
   lấy GitHub PAT → lấy Cloudflare API Token/Account ID → set 3 GitHub
   Secrets), kèm hướng dẫn test bằng `wrangler dev` + curl giả webhook payload
   (`/start`, `/refresh`, callback `qa:1`, free-text), và cảnh báo privacy
   nhắc lại (Worker dùng cùng `GEMINI_API_KEY` free tier).

8. **`tests/test_deliver.py`** (mới) — 4 test: `build_inline_keyboard` trả
   `None` khi rỗng, callback_data khớp đúng index 1-based; `write_items_to_kv`
   graceful-degrade (trả `False`, không raise) khi thiếu secret hoặc list rỗng.

## Tests

`26 passed` (22 cũ + 4 mới), chạy bằng `python -m pytest tests/` từ `src/`.

`DRY_RUN=true python src/main.py` (không set 3 biến Cloudflare trong `.env`)
chạy hết Stage 0→4, log warning rõ ràng ở Stage 3.5
("thiếu CLOUDFLARE_API_TOKEN/... -> bỏ qua ghi KV"), digest vẫn in console +
inline keyboard vẫn được build và in ra (chunk cuối) đúng định dạng Telegram
`reply_markup`. Xác nhận: pipeline cũ KHÔNG bị phá.

Lưu ý: sau khi chạy test DRY_RUN, các file state (`archive/2026-06-19.md`,
`knowledge/kb.json`, `state/seen.json`) bị ghi đè bởi lần chạy thử — đã
`git checkout --` để khôi phục, không để lại side-effect ngoài ý muốn trong
repo.

## Deviations from spec (with reasoning)

- **Key cho item context**: spec gợi ý "index 1-6 hoặc hash item.id rút
  gọn" — chọn index 1-based đơn giản (`qa:1`...`qa:N`) vì: (a) Telegram
  `callback_data` giới hạn 64 byte, index ngắn nhất; (b) khớp 1-1 dễ dàng
  giữa Python (`write_items_to_kv` ghi `item:<idx>` theo đúng thứ tự
  `verified_items`) và Worker (đọc `item:<idx>` từ `callback_data`), không
  cần đồng bộ hash giữa 2 ngôn ngữ.
- **GitHub PAT permission**: README ghi "Contents: Read and write" là quyền
  tối thiểu cho fine-grained token vì GitHub không expose permission riêng
  tên "repository_dispatch" trong UI fine-grained — quyền `contents` là điều
  kiện cần cho `repository_dispatch` hoạt động trên token fine-grained theo
  tài liệu GitHub hiện tại. Đã ghi rõ trong README để operator tự kiểm khi
  tạo token, có thể cần bật thêm "Actions: Read and write" tuỳ chính sách
  GitHub thời điểm operator làm.
- **Không dùng SDK** cho cả 2 phía (Python `requests` thuần đã có sẵn từ
  trước; JS Worker dùng `fetch` thuần) — đúng yêu cầu giảm phức tạp deploy.

## Risks or concerns

- Worker code **chưa được test chạy thật** (không có Cloudflare account) —
  chỉ kiểm tra cú pháp JS hợp lệ (`node --check`) và đọc lại logic thủ công.
  Operator cần tự test bằng `wrangler dev` + curl theo hướng dẫn README trước
  khi tin tưởng hoàn toàn vào production.
- `write_items_to_kv` gọi Cloudflare REST API tuần tự (N item + 1 "latest_items"
  = N+1 HTTP request) trong main.py — với N=5-6 item/ngày, độ trễ thêm không
  đáng kể, nhưng nếu sau này tăng số item/ngày đáng kể nên xem xét batch
  hoặc gọi song song.
- GitHub PAT cần quyền `repository_dispatch` — nếu operator tạo fine-grained
  token thiếu đúng quyền, `/refresh` sẽ lỗi 403/404; Worker đã có nhánh báo
  lỗi rõ (`GitHub API trả lỗi khi trigger refresh (status ...)`), nhưng
  operator cần đọc log Worker (Cloudflare dashboard → Worker → Logs) để debug
  nếu gặp lỗi này.
- KV TTL 48h: nếu operator bấm "Hỏi sâu thêm" sau hơn 48h từ lúc digest gửi,
  Worker sẽ báo "không tìm thấy context" — đây là chủ ý (free tier KV có giới
  hạn dung lượng), không phải bug.

## Next steps (cho operator — phải tự làm, agent không tự động hoá được)

Theo đúng thứ tự bắt buộc:
1. `npm install -g wrangler` + `wrangler login`.
2. `cd worker && wrangler kv:namespace create DIGEST_KV` → copy `id` vào
   `worker/wrangler.toml`.
3. Sửa `worker/wrangler.toml`: điền `GITHUB_OWNER`/`GITHUB_REPO` thật.
4. `wrangler secret put GEMINI_API_KEY` / `TELEGRAM_BOT_TOKEN` / `GITHUB_PAT`
   (chạy trong `worker/`).
5. `wrangler deploy` (trong `worker/`) → copy URL `https://xxx.workers.dev`.
6. `curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WORKER_URL>"`.
7. Lấy GitHub PAT (Settings → Developer settings → Personal access tokens →
   Fine-grained → quyền Contents Read/write tối thiểu cho repo này).
8. Lấy Cloudflare API Token (My Profile → API Tokens → quyền Workers KV
   Storage: Edit) + Account ID (sidebar phải dashboard).
9. Set 3 GitHub Secrets (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`,
   `CLOUDFLARE_KV_NAMESPACE_ID`) vào repo → Settings → Secrets and variables
   → Actions.
10. (Tuỳ chọn, khuyến nghị) Test bằng `wrangler dev` + curl giả webhook
    payload theo README trước khi tin tưởng production.

Mọi bước trên đều ghi chi tiết, copy-paste được, trong README.md mục
"Thiết lập tương tác real-time (Cloudflare Workers)".
