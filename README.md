# Bản Tin Kinh Tế & Thị Trường

Pipeline cá nhân, chạy bằng GitHub Actions, gửi digest mỗi sáng 6h (giờ VN)
qua Telegram. Không VPS, không database — state lưu bằng file commit ngược
vào repo.

> Trạng thái hiện tại: **đầy đủ §1-9 của build brief** (`morning-intel-agent-brief.md`).
> Toàn bộ pipeline (fetch → filter → verify → analyze → accumulate → deliver)
> chạy bằng 1 lệnh `DRY_RUN=true python src/main.py`, không cần input giữa
> chừng. Nếu thiếu `GEMINI_API_KEY`/`DEEPSEEK_API_KEY`, các bước cần LLM
> (Stage 1 batch ranking, Stage 2 analyze) tự fallback/degrade — pipeline vẫn
> chạy hết, không crash, chỉ in cảnh báo rõ ràng thiếu key gì.

---

## ⚠️ CẢNH BÁO DATA PRIVACY (đọc trước khi dùng)

**Gemini free tier có thể dùng nội dung prompt để train mô hình của Google.**
Quy tắc cứng cho hệ thống này:

- CHỈ feed tin công khai (arXiv, Hacker News, RSS công khai của TechCrunch/
  e27/DealStreetAsia/IEEE Spectrum...) vào prompt.
- KHÔNG BAO GIỜ feed dữ liệu cá nhân, research riêng, ghi chú nội bộ, hay bất
  kỳ thông tin nhạy cảm nào của operator vào `thesis.yaml` hoặc bất kỳ chỗ
  nào sẽ đi vào prompt LLM.
- `config/thesis.yaml` chỉ nên chứa **chủ đề** quan tâm (ví dụ "diffusion
  models", "SEA fintech"), không chứa thông tin định danh hay chiến lược kinh
  doanh cụ thể.
- Nếu cần riêng tư tuyệt đối, đổi `LLM_ENGINE=deepseek` và chỉ dùng DeepSeek
  (đọc kỹ chính sách dữ liệu của DeepSeek trước khi quyết định) — nhưng mặc
  định hệ thống vẫn ưu tiên Gemini trước vì free tier rộng hơn.

---

## Cấu trúc

```
newnews/
├── README.md
├── requirements.txt
├── morning-intel-agent-brief.md   # nguồn sự thật cho thiết kế/prompts/schema
├── config/
│   └── thesis.yaml                # "lăng kính" — operator tự sửa
├── src/
│   ├── main.py                    # entrypoint pipeline ngày (Stage 0-4)
│   ├── weekly.py                  # entrypoint tổng hợp tuần
│   ├── pipeline.py                # Item, ThesisConfig, Stage 1 filter,
│   │                               # Stage 1 LLM ranking, Stage 2 analyze
│   ├── verify.py                  # Stage 1.5 — tier + cross-reference +
│   │                               # quy tắc confidence cứng (🟢🟡🔴)
│   ├── memory.py                  # Stage 3 — kb.json, archive, seen.json
│   ├── deliver.py                 # Stage 4 — format + gửi Telegram
│   ├── llm_client.py              # Gemini (chính) + DeepSeek (fallback)
│   ├── prompts.py                 # ANALYSIS_SYSTEM_PROMPT, WEEKLY_SYSTEM_PROMPT
│   └── sources/
│       ├── arxiv.py               # arXiv API (cs.LG, cs.AI, stat.ML, q-fin)
│       ├── hackernews.py          # Algolia HN API (points>50)
│       ├── funding.py             # TechCrunch + e27 + DealStreetAsia RSS
│       ├── github_trending.py     # GitHub Trending (RSS không chính thức)
│       ├── producthunt.py         # Product Hunt GraphQL (cần token)
│       ├── deep_tech.py           # IEEE Spectrum + arXiv eess/physics/cond-mat
│       └── source_registry.py     # bảng tier uy tín SOURCE_TIERS
├── state/
│   └── seen.json                  # hash item đã gửi, dedupe qua các ngày
├── archive/                       # digest mỗi ngày + tổng hợp tuần
├── knowledge/
│   └── kb.json                    # LỚP TÍCH LUỸ: themes/companies/investors/
│                                   # tech/deep_tech + count + ngày
├── tests/                         # unit test cho verify.py, memory.py,
│                                   # source_registry.py
└── .github/workflows/
    ├── daily.yml                  # cron 0 23 * * * UTC = 6h sáng VN
    └── weekly.yml                 # cron Chủ nhật
```

---

## Cách chạy local (DRY_RUN)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd src
DRY_RUN=true python main.py      # pipeline ngày
DRY_RUN=true python weekly.py    # tổng hợp tuần (cần archive/*.md có sẵn)
```

Chạy unit test (không cần pytest, file tự chạy được trực tiếp; có pytest thì
dùng `pytest tests/`):

```bash
cd /path/to/newnews
source .venv/bin/activate
python tests/test_verify.py
python tests/test_source_registry.py
python tests/test_memory.py
```

**Không cần API key nào để DRY_RUN chạy hết pipeline** — nếu thiếu
`GEMINI_API_KEY`/`DEEPSEEK_API_KEY`, Stage 1 batch ranking fallback về
heuristic score thuần, Stage 2 analyze in ra thông báo rõ "thiếu key X, Y"
thay vì sinh digest 6 mục, nhưng Stage 0/1/1.5/3/4 vẫn chạy đầy đủ
(fetch/filter/verify/lưu archive+kb.json+seen.json/in console).

---

## Secrets cần thiết — cách lấy từng cái

Đặt vào `.env` (local, không commit — đã có trong `.gitignore`) hoặc
**Settings → Secrets and variables → Actions** trên GitHub repo (production).

| Secret | Bắt buộc? | Cách lấy |
|---|---|---|
| `GEMINI_API_KEY` | Khuyến nghị (engine chính) | Vào [Google AI Studio](https://aistudio.google.com/app/apikey) → "Create API key". Free tier đủ dùng cho 1-2 lần gọi/ngày của hệ thống này. |
| `DEEPSEEK_API_KEY` | Khuyến nghị (fallback khi Gemini lỗi/429) | Vào [platform.deepseek.com](https://platform.deepseek.com/api_keys) → tạo API key. Dùng endpoint Anthropic-compatible `https://api.deepseek.com/anthropic`. |
| `TELEGRAM_BOT_TOKEN` | Cần để gửi thật (không cần cho DRY_RUN) | Chat với [@BotFather](https://t.me/BotFather) trên Telegram → `/newbot` → đặt tên → BotFather trả về token dạng `123456:ABC-DEF...`. |
| `TELEGRAM_CHAT_ID` | Cần để gửi thật | Chat với bot vừa tạo (gửi bất kỳ tin gì), sau đó mở `https://api.telegram.org/bot<TOKEN>/getUpdates` trên browser, tìm field `"chat":{"id": ...}` trong JSON trả về — đó là chat_id của bạn. |
| `PRODUCTHUNT_TOKEN` | Tuỳ chọn | Vào [api.producthunt.com/v2/oauth/applications](https://api.producthunt.com/v2/oauth/applications) → tạo application → lấy "Developer Token". **Nếu không set, `producthunt.py` tự bỏ qua nguồn này (log warning), không block pipeline.** |
| `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_KV_NAMESPACE_ID` | Tuỳ chọn (chỉ cần cho tương tác real-time) | Xem mục "Thiết lập tương tác real-time (Cloudflare Workers)" phía dưới — cần làm cùng bước thiết lập Worker. **Nếu không set, `deliver.py::write_items_to_kv` tự bỏ qua (log warning), không block pipeline.** |

Không có key nào trong bảng trên là bắt buộc để pipeline **chạy được** (DRY_RUN
hoặc thật) — pipeline luôn degrade gracefully và log rõ thiếu gì. Nhưng để có
digest 6 mục đầy đủ (Stage 2 analyze) và gửi Telegram thật, cần ít nhất:
`GEMINI_API_KEY` hoặc `DEEPSEEK_API_KEY` (1 trong 2), và
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (cả 2).

### Set secrets trên GitHub Actions

Repo → **Settings → Secrets and variables → Actions → New repository secret**,
thêm từng secret theo tên đúng như bảng trên (case-sensitive). Workflow
`.github/workflows/daily.yml` và `weekly.yml` đã đọc đúng các tên này qua
`${{ secrets.TÊN_SECRET }}`.

---

## Thiết lập tương tác real-time (Cloudflare Workers)

Mặc định bot chỉ MỘT CHIỀU: cron gửi digest, operator không tương tác lại được.
Phần này thêm webhook serverless free (Cloudflare Workers) để bot nhận lệnh
Telegram **ngay lập tức** (không polling chậm):

- `/start` — xem hướng dẫn.
- `/refresh` — chạy lại pipeline ngay qua GitHub `repository_dispatch`.
- `/trends` — xem xu hướng tích luỹ (themes/companies/tech/deep_tech xuất
  hiện nhiều lần qua các ngày, đọc thẳng từ kb.json, không qua LLM) — "1 nơi"
  để nắm bắt xu hướng dài hạn, không chỉ digest hôm nay.
- `/forget` — xoá lịch sử hội thoại đã nhớ với bot (xem mục Q&A dưới), bắt
  đầu lại từ đầu.
- Nút **"🔍 Hỏi sâu thêm"** dưới mỗi mục digest — hỏi sâu riêng item đó (đọc
  context từ Cloudflare KV, gọi Gemini trực tiếp từ Worker).
- Gõ câu hỏi tự do bất kỳ — bot dùng digest gần nhất + tóm tắt xu hướng tích
  luỹ (`trends:latest`) làm nền để trả lời, **VÀ nhớ tối đa 6 lượt hỏi-đáp
  gần nhất** của riêng chat đó (key `conv:<chat_id>` trong KV, TTL 7 ngày)
  để trả lời nối tiếp được câu hỏi trước — không phải hỏi-đáp rời rạc từng
  lần như trước.

Code Worker nằm ở `worker/` (root repo), JS thuần, không cần build step.
**Phần dưới đây CẦN OPERATOR TỰ LÀM** (cần đăng nhập tài khoản Cloudflare/
GitHub cá nhân — agent không tự động hoá được). Làm đúng thứ tự:

### 1. Cài wrangler + login

```bash
npm install -g wrangler
wrangler login
```

(Hoặc dùng `npx wrangler <command>` thay cho `wrangler <command>` ở mọi bước
dưới đây nếu không muốn cài global.)

### 2. Tạo KV namespace

```bash
cd worker
wrangler kv:namespace create DIGEST_KV
```

Lệnh trả về một `id` dạng chuỗi hex — copy giá trị này, dán vào
`worker/wrangler.toml`, thay chỗ `REPLACE_WITH_NAMESPACE_ID_FROM_WRANGLER_KV_NAMESPACE_CREATE`.

### 3. Sửa `worker/wrangler.toml`

Mở `worker/wrangler.toml`, thay 2 dòng vars:

```toml
[vars]
GITHUB_OWNER = "<github-username-của-bạn>"
GITHUB_REPO = "<tên-repo-này>"
```

### 4. Set secrets cho Worker

Chạy trong thư mục `worker/` (mỗi lệnh sẽ hỏi nhập giá trị secret, không gõ
trực tiếp vào terminal history):

```bash
wrangler secret put GEMINI_API_KEY
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put GITHUB_PAT
```

- `GEMINI_API_KEY`: dùng lại đúng key đã lấy ở mục "Secrets cần thiết" phía
  trên (Google AI Studio).
- `TELEGRAM_BOT_TOKEN`: dùng lại đúng token bot đã tạo qua @BotFather.
- `GITHUB_PAT`: xem cách lấy ở mục 6 dưới đây.

### 5. Deploy Worker

```bash
cd worker
wrangler deploy
```

Lệnh trả về URL dạng `https://morning-intel-webhook.<subdomain>.workers.dev`
— copy lại URL này, dùng ở bước 6.

### 6. Set Telegram webhook

Lấy `<TELEGRAM_BOT_TOKEN>` và `<WORKER_URL>` (từ bước 5), gọi 1 dòng curl:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=<WORKER_URL>"
```

Kết quả mong đợi: `{"ok":true,"result":true,"description":"Webhook was set"}`.
Từ giờ mọi message/callback Telegram sẽ POST trực tiếp tới Worker, không cần
polling.

### 7. Lấy GitHub PAT (cho `/refresh`)

GitHub → **Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**:
- Repository access: chọn đúng repo này.
- Permissions cần tối thiểu: **Contents: Read and write** (để
  `repository_dispatch` hoạt động — GitHub yêu cầu quyền contents cho event
  dispatch) hoặc bật thêm quyền "Actions: Read and write" nếu fine-grained
  token của bạn tách riêng quyền dispatch.
- Copy token, dùng ở bước 4 (`wrangler secret put GITHUB_PAT`).

### 8. Lấy Cloudflare API Token + Account ID (cho main.py ghi KV)

- **API Token**: Cloudflare dashboard → **My Profile → API Tokens → Create
  Token** → chọn template "Edit Cloudflare Workers" hoặc tạo custom token với
  quyền **Workers KV Storage: Edit**.
- **Account ID**: Cloudflare dashboard → chọn account → nhìn sidebar phải,
  có field "Account ID" (chuỗi hex).
- **Namespace ID**: chính là `id` đã lấy ở bước 2.

### 9. Set 3 secret Cloudflare vào GitHub Secrets

Để `src/main.py` chạy trong GitHub Actions ghi được lên Cloudflare KV, vào
repo → **Settings → Secrets and variables → Actions → New repository
secret**, thêm:

| Secret | Giá trị |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Token từ bước 8 |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID từ bước 8 |
| `CLOUDFLARE_KV_NAMESPACE_ID` | Namespace ID từ bước 2 |

Thiếu 1 trong 3 secret này KHÔNG làm sập pipeline — `main.py` log warning và
bỏ qua bước ghi KV (nút "Hỏi sâu thêm"/"/refresh"/"/trends" sẽ không hoạt
động cho lần gửi đó, nhưng digest vẫn gửi bình thường qua Telegram).

### Test local trước khi deploy thật (`wrangler dev`)

Không cần deploy ngay để thử Worker — chạy local:

```bash
cd worker
wrangler dev
```

Wrangler in ra URL local (vd `http://localhost:8787`). Giả lập 1 update
Telegram bằng curl:

```bash
# Giả lập /start
curl -X POST http://localhost:8787/ \
  -H "Content-Type: application/json" \
  -d '{"message":{"chat":{"id":123456},"text":"/start"}}'

# Giả lập /refresh
curl -X POST http://localhost:8787/ \
  -H "Content-Type: application/json" \
  -d '{"message":{"chat":{"id":123456},"text":"/refresh"}}'

# Giả lập bấm nút "Hỏi sâu thêm" item số 1 (cần đã có key "item:1" trong KV —
# dùng `wrangler kv:key put --binding=DIGEST_KV "item:1" '{"title":"test","url":"https://x.com","raw_text":"nội dung test","type":"research","confidence":"🟢"}' --local`
# để seed dữ liệu test trước khi gọi callback dưới đây)
curl -X POST http://localhost:8787/ \
  -H "Content-Type: application/json" \
  -d '{"callback_query":{"id":"cb1","message":{"chat":{"id":123456}},"data":"qa:1"}}'

# Giả lập câu hỏi tự do
curl -X POST http://localhost:8787/ \
  -H "Content-Type: application/json" \
  -d '{"message":{"chat":{"id":123456},"text":"item nào đáng chú ý nhất hôm nay?"}}'
```

Vì `wrangler dev` không có secret thật trừ khi bạn set `.dev.vars` (file local,
KHÔNG commit) với nội dung tương tự `GEMINI_API_KEY=...`, `TELEGRAM_BOT_TOKEN=...`,
`GITHUB_PAT=...` — Worker sẽ log lỗi rõ "thiếu X" giống cơ chế graceful-degrade
của pipeline Python, không silent fail.

### ⚠️ Cảnh báo privacy (nhắc lại)

Nhánh "Hỏi sâu thêm"/free-text Q&A gọi **CÙNG GEMINI_API_KEY free tier** như
pipeline chính — áp dụng đúng cảnh báo data privacy ở đầu README: Gemini free
tier có thể dùng prompt để train, chỉ nội dung tin công khai (title/url/
raw_text rút gọn của các nguồn công khai) được gửi đi, không có dữ liệu cá
nhân nào trong context KV.

---

## Cách sửa `config/thesis.yaml`

File này là "lăng kính" — quyết định cái gì được coi là "matter" và cách
Stage 1/2 đánh giá độ liên quan. Các field:

- `tracking`: list chủ đề chính đang theo dõi (AI/quant/SEA). Mỗi cụm từ
  được tách token để so khớp keyword trong Stage 1 heuristic.
- `deep_tech_tracking`: chủ đề hard tech (xe điện, pin, vật liệu, robotics).
- `keywords_boost`: từ khoá ưu tiên cộng điểm thêm (vd "moat", "why now").
- `outside_lane_domains`: chủ đề CỐ Ý lệch khỏi thesis chính, dùng để chọn
  mục 5 "NGOÀI ĐƯỜNG RAY" trong digest — chống filter bubble.
- `what_counts_as_matters`: đoạn văn tự do mô tả tiêu chí lọc, dùng làm
  context bổ sung (hiện tại chưa được code Stage 1 parse trực tiếp, nhưng
  Stage 2 LLM đọc được toàn bộ file nếu cần mở rộng).

Sửa trực tiếp file YAML, không cần đổi code. Lưu ý: nội dung file này sẽ đi
vào prompt LLM — tuân theo cảnh báo data privacy ở đầu README.

---

## Cách thêm domain mới vào `source_registry.py`

Mở `src/sources/source_registry.py`, thêm 1 dòng vào dict `SOURCE_TIERS`:

```python
SOURCE_TIERS: dict[str, int] = {
    ...
    "domain-moi.com": 2,   # tự quyết định tier 1/2/3 dựa vào độ uy tín
}
```

- **Tier 1**: chính chủ, editorial mạnh, hoặc primary source (arXiv, IEEE,
  TechCrunch, quỹ VC lớn viết blog chính chủ).
- **Tier 2**: uy tín trong ngành/khu vực nhưng editorial nhỏ hơn (Sifted,
  e27, Tech in Asia, DealStreetAsia, KrASIA, Y Combinator).
- **Tier 3**: aggregator/forum, dùng được nhưng phải cross-check (Hacker
  News, Product Hunt, GitHub).

Domain KHÔNG có trong bảng tự động nhận **tier 3** (an toàn — coi như chưa uy
tín tới khi được thêm vào bảng). Đây là quyết định CỦA OPERATOR, không để LLM
tự quyết tier của 1 nguồn.

---

## Cách verify/confidence hoạt động (tóm tắt, đọc `src/verify.py` để chi tiết)

Mọi nhãn 🟢/🟡/🔴 trong digest được **code if/else tính ra**, KHÔNG hỏi LLM:

- 🟢 `confirmed`: nguồn tier 1-2 VÀ (có nguồn khác xác nhận cùng sự kiện
  HOẶC là số liệu trích trực tiếp có link từ tier 1-2).
- 🟡 `reasoned`: là suy luận/phân tích dựa trên dữ kiện tier 1-2 (không bao
  giờ là fact, không bao giờ 🟢).
- 🔴 `single_source`: tier 3 không cross-confirm (LUÔN, không ngoại lệ), hoặc
  tier 1-2 không cross-confirm được, hoặc claim số liệu thiếu nguồn cụ thể.

Cross-reference triển khai bằng so khớp token tiêu đề giữa các item đã fetch
trong cùng batch Stage 0 (rẻ, xác định được, không phụ thuộc LLM — xem
docstring đầu file `verify.py` để biết lý do chọn cách này thay vì gọi
web-search thật).

---

## GitHub Actions

- `daily.yml`: cron `0 23 * * *` UTC (= 6h sáng giờ VN), chạy `python
  src/main.py`, commit + push `state/`, `archive/`, `knowledge/` ngược vào
  repo. Cần `permissions: contents: write` (đã set trong workflow). Cũng nhận
  trigger `repository_dispatch` (event_type `refresh-digest`) — đây là cách
  Cloudflare Worker gọi lại pipeline khi operator gõ `/refresh` trên Telegram
  (xem mục "Thiết lập tương tác real-time" phía trên).
- `weekly.yml`: cron Chủ nhật `0 23 * * 0` UTC, chạy `python src/weekly.py`.

Cả 2 đều có `workflow_dispatch` để chạy thủ công từ tab Actions khi cần test.

---

## Chi phí

- Hạ tầng: $0 (GitHub Actions free tier dư cho 1-2 lần chạy/ngày).
- LLM: Gemini Flash-Lite free tier — log số token mỗi lần gọi (xem log
  "input_tokens=... output_tokens=..." trong output) để tự kiểm chứng chi
  phí thực tế ~$0/tháng. Fallback DeepSeek chỉ tốn phí khi Gemini lỗi/429.
