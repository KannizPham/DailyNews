/**
 * Cloudflare Worker — webhook Telegram cho Bản Tin Kinh Tế & Thị Trường.
 *
 * Xử lý các loại update Telegram:
 *   1. command /start          -> welcome message + nút nhanh "🔄 Refresh".
 *   2. command /refresh HOẶC callback_query "refresh" (nút nhanh dưới /start)
 *      -> trigger lại pipeline qua GitHub repository_dispatch (event_type
 *      "refresh-digest"). Cả 2 đường dùng chung handleRefresh().
 *   3. command /trends         -> đọc tóm tắt kb.json (key "trends:latest",
 *      ghi bởi src/deliver.py::write_trends_to_kv sau Stage 3) -> trả top
 *      themes/companies/tech/deep_tech tích luỹ qua các ngày, KHÔNG cần gọi
 *      LLM (đọc thẳng số liệu, rẻ và xác định được).
 *   4. callback_query "qa:<key>" (bấm nút "🔍 Hỏi sâu thêm" dưới digest)
 *      -> đọc context item từ KV (key ghi bởi src/deliver.py qua
 *      write_items_to_kv), hỏi Gemini đào sâu thêm, reply.
 *   5. text tự do khác         -> coi là câu hỏi follow-up, dùng toàn bộ
 *      context "latest_items" + "trends:latest" trong KV làm nền, CỘNG lịch
 *      sử hội thoại gần đây của chính chat_id đó (key "conv:<chatId>") để
 *      trả lời có tính liên tục, không phải hỏi-đáp rời rạc từng lần.
 *
 * Nguyên tắc: KHÔNG để Telegram timeout im lặng — mọi nhánh lỗi (KV rỗng,
 * Gemini lỗi, GitHub API lỗi) đều phải sendMessage báo lỗi rõ cho operator.
 *
 * Không dùng SDK nào (Gemini gọi raw fetch HTTP) để giữ Worker thuần JS,
 * không cần build step — theo đúng yêu cầu "free tier, không framework".
 */

const TELEGRAM_API = (token, method) => `https://api.telegram.org/bot${token}/${method}`;

// Lịch sử hội thoại theo chat_id — giữ tối đa 6 lượt hỏi-đáp gần nhất (12
// entry user+model), đủ để trả lời "liên tục" mà không phình prompt vô hạn.
const CONV_MAX_ENTRIES = 12;
// TTL dài hơn item context (48h) vì đây là trí nhớ "của operator" nên muốn
// giữ qua nhiều ngày, nhưng không vĩnh viễn để tránh KV phình theo thời gian.
const CONV_TTL_SECONDS = 7 * 24 * 3600;

// Sau khi bấm "Hỏi sâu thêm", operator có 10 phút để gõ câu hỏi RIÊNG về
// đúng item đó (key "pending_qa:<chatId>" trong KV) — quá thời gian này,
// tin nhắn tự do tiếp theo coi như câu hỏi chung về cả digest như cũ.
const PENDING_QA_TTL_SECONDS = 10 * 60;

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("Bản Tin Kinh Tế & Thị Trường đang chạy. Dùng POST từ Telegram.", {
        status: 200,
      });
    }

    let update;
    try {
      update = await request.json();
    } catch (err) {
      // Body không phải JSON hợp lệ -> không phải update Telegram thật, trả 200
      // để Telegram không retry vô hạn, nhưng không làm gì thêm.
      return new Response("ignored: bad json", { status: 200 });
    }

    try {
      if (update.callback_query) {
        await handleCallbackQuery(update.callback_query, env);
      } else if (update.message) {
        await handleMessage(update.message, env);
      }
    } catch (err) {
      // Lỗi bất ngờ ở tầng cao nhất — vẫn cố báo cho operator nếu biết chat_id.
      console.error("Unhandled error:", err);
      const chatId = update?.message?.chat?.id ?? update?.callback_query?.message?.chat?.id;
      if (chatId && env.TELEGRAM_BOT_TOKEN) {
        await sendMessage(env, chatId, `Lỗi không xác định ở webhook: ${escapeForTelegram(String(err))}`);
      }
    }

    // Luôn trả 200 cho Telegram dù xử lý nội bộ lỗi gì (đã reply lỗi cho
    // operator riêng) — tránh Telegram coi webhook là fail và retry dồn dập.
    return new Response("ok", { status: 200 });
  },
};

// ---------------------------------------------------------------------
// Message (text command hoặc free-text follow-up)
// ---------------------------------------------------------------------

async function handleMessage(message, env) {
  const chatId = message.chat?.id;
  const text = (message.text || "").trim();

  if (!chatId) return;

  if (text === "/start" || text.startsWith("/start ")) {
    await sendMessage(env, chatId, WELCOME_TEXT, START_KEYBOARD);
    return;
  }

  if (text === "/refresh" || text.startsWith("/refresh ")) {
    await handleRefresh(chatId, env);
    return;
  }

  if (text === "/trends" || text.startsWith("/trends ")) {
    await handleTrends(chatId, env);
    return;
  }

  if (text === "/forget" || text.startsWith("/forget ")) {
    await handleForget(chatId, env);
    return;
  }

  if (text.startsWith("/")) {
    await sendMessage(
      env,
      chatId,
      "Lệnh không nhận diện được. Lệnh có sẵn: /start, /refresh, /trends, /forget. Hoặc gõ câu hỏi tự do về digest gần nhất."
    );
    return;
  }

  if (!text) {
    // Update message không có text (ảnh, sticker...) — bỏ qua êm, không spam lỗi.
    return;
  }

  // Nếu operator vừa bấm "Hỏi sâu thêm" trong 10 phút gần đây (xem
  // PENDING_QA_TTL_SECONDS), coi tin nhắn tự do tiếp theo là câu hỏi RIÊNG
  // về đúng item đó, không phải câu hỏi chung cả digest -> dùng đúng context
  // item đó cho prompt, trả lời tập trung hơn. Xoá pending ngay (dùng 1 lần)
  // để câu hỏi sau đó không bị gán nhầm vào item cũ.
  const pendingItemKey = await getPendingItemQuestion(env, chatId);
  if (pendingItemKey) {
    await clearPendingItemQuestion(env, chatId);
    await handleItemSpecificQuestion(chatId, pendingItemKey, text, env);
    return;
  }

  await handleFreeTextQuestion(chatId, text, env);
}

const WELCOME_TEXT = `👋 Chào operator!

Đây là Bản Tin Kinh Tế & Thị Trường.
Bot tổng hợp và phân tích tin tức kinh tế, tài chính, ngân hàng, chứng khoán, doanh nghiệp, công nghệ và địa chính trị.

Lệnh có sẵn:
• /start — xem lại hướng dẫn này.
• /refresh — chạy lại pipeline ngay (digest mới sẽ tới trong vài phút).
• /trends — xem xu hướng tích luỹ (themes/companies/tech xuất hiện nhiều lần qua các ngày), không chỉ digest hôm nay.
• /forget — xoá lịch sử hội thoại đã nhớ với bot, bắt đầu lại từ đầu.
• Bấm nút "🔍 Hỏi sâu thêm" dưới mỗi mục digest để nhận phân tích mặc định — SAU ĐÓ có 10 phút để gõ tiếp câu hỏi RIÊNG của bạn về đúng item đó (vd "so với X thì sao?").
• Hoặc gõ thẳng câu hỏi tự do (không vừa bấm nút) — bot dùng cả digest gần nhất làm nền, nhớ vài lượt hỏi-đáp gần nhất với bạn nên có thể hỏi tiếp/nối ý, không cần lặp lại context.

👇 Hoặc bấm nút nhanh dưới đây:`;

// Nút nhanh dưới /start — operator yêu cầu "easy to use giống các con bot
// khác", không phải gõ lệnh tay mỗi lần. callback_data "refresh" dùng lại
// CHÍNH logic handleRefresh() (xem handleCallbackQuery).
const START_KEYBOARD = {
  inline_keyboard: [[{ text: "🔄 Refresh digest ngay", callback_data: "refresh" }]],
};

// ---------------------------------------------------------------------
// /refresh -> GitHub repository_dispatch
// ---------------------------------------------------------------------

async function handleRefresh(chatId, env) {
  if (!env.GITHUB_PAT || !env.GITHUB_OWNER || !env.GITHUB_REPO) {
    await sendMessage(
      env,
      chatId,
      "Không thể /refresh: Worker thiếu GITHUB_PAT hoặc GITHUB_OWNER/GITHUB_REPO. Operator cần set secret/var này (xem README)."
    );
    return;
  }

  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/dispatches`;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_PAT}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "ban-tin-kinh-te-thi-truong-webhook",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ event_type: "refresh-digest" }),
    });

    if (resp.status === 204) {
      await sendMessage(env, chatId, "✅ Đang chạy lại pipeline. Digest mới sẽ tới trong ít phút.");
    } else {
      const body = await resp.text();
      await sendMessage(
        env,
        chatId,
        `⚠️ GitHub API trả lỗi khi trigger refresh (status ${resp.status}): ${truncate(body, 300)}`
      );
    }
  } catch (err) {
    await sendMessage(env, chatId, `⚠️ Lỗi gọi GitHub API để refresh: ${escapeForTelegram(String(err))}`);
  }
}

// ---------------------------------------------------------------------
// /trends -> đọc thẳng "trends:latest" trong KV (không gọi LLM)
// ---------------------------------------------------------------------

async function handleTrends(chatId, env) {
  if (!env.DIGEST_KV) {
    await sendMessage(env, chatId, "Worker thiếu binding KV (DIGEST_KV) — không đọc được dữ liệu xu hướng.");
    return;
  }

  let raw;
  try {
    raw = await env.DIGEST_KV.get("trends:latest");
  } catch (err) {
    await sendMessage(env, chatId, `Lỗi đọc KV: ${escapeForTelegram(String(err))}`);
    return;
  }

  if (!raw) {
    await sendMessage(
      env,
      chatId,
      "Chưa có dữ liệu xu hướng tích luỹ (cần ít nhất 1 lần pipeline chạy xong Stage 3 — xem src/deliver.py::write_trends_to_kv)."
    );
    return;
  }

  let trends;
  try {
    trends = JSON.parse(raw);
  } catch (err) {
    await sendMessage(env, chatId, "Dữ liệu xu hướng trong KV bị hỏng (không parse được JSON).");
    return;
  }

  await sendMessage(env, chatId, buildTrendsMessage(trends));
}

function buildTrendsMessage(trends) {
  const section = (emoji, label, list) => {
    if (!list || !list.length) return `${emoji} ${label}: (chưa có dữ liệu)`;
    const lines = list
      .slice(0, 8)
      .map((x, i) => `${i + 1}. ${x.name} — ${x.count} lần (gần nhất ${x.last_seen})`);
    return `${emoji} ${label}:\n${lines.join("\n")}`;
  };

  return [
    "📊 Xu hướng tích luỹ (toàn bộ lịch sử digest, không chỉ hôm nay):",
    "",
    section("🧠", "Themes", trends.themes),
    "",
    section("🏢", "Companies", trends.companies),
    "",
    section("🛠️", "Tech", trends.tech),
    "",
    section("🔬", "Deep tech", trends.deep_tech),
  ].join("\n");
}

// ---------------------------------------------------------------------
// /forget -> xoá lịch sử hội thoại của chat_id này
// ---------------------------------------------------------------------

async function handleForget(chatId, env) {
  if (!env.DIGEST_KV) {
    await sendMessage(env, chatId, "Worker thiếu binding KV (DIGEST_KV) — không có gì để xoá.");
    return;
  }
  try {
    await env.DIGEST_KV.delete(`conv:${chatId}`);
    await sendMessage(env, chatId, "🧹 Đã xoá lịch sử hội thoại. Bắt đầu lại từ đầu.");
  } catch (err) {
    await sendMessage(env, chatId, `Lỗi xoá lịch sử hội thoại: ${escapeForTelegram(String(err))}`);
  }
}

// ---------------------------------------------------------------------
// callback_query "qa:<key>" -> đào sâu 1 item cụ thể
// ---------------------------------------------------------------------

async function handleCallbackQuery(callbackQuery, env) {
  const chatId = callbackQuery.message?.chat?.id;
  const data = callbackQuery.data || "";

  // answerCallbackQuery TRƯỚC để Telegram tắt loading xoay vô hạn trên nút,
  // dù phần xử lý sau có chậm/lỗi.
  await answerCallbackQuery(env, callbackQuery.id);

  if (!chatId) return;

  if (data === "refresh") {
    await handleRefresh(chatId, env);
    return;
  }

  if (!data.startsWith("qa:")) {
    await sendMessage(env, chatId, "Không nhận diện được nút bấm này.");
    return;
  }

  const key = data.slice("qa:".length);

  if (!env.DIGEST_KV) {
    await sendMessage(env, chatId, "Worker thiếu binding KV (DIGEST_KV) — không đọc được context item.");
    return;
  }

  let raw;
  try {
    raw = await env.DIGEST_KV.get(`item:${key}`);
  } catch (err) {
    await sendMessage(env, chatId, `Lỗi đọc KV: ${escapeForTelegram(String(err))}`);
    return;
  }

  if (!raw) {
    await sendMessage(
      env,
      chatId,
      "Không tìm thấy context cho item này (có thể đã hết hạn sau 48h, hoặc digest này quá cũ). Đợi digest mới hoặc dùng /refresh."
    );
    return;
  }

  let itemCtx;
  try {
    itemCtx = JSON.parse(raw);
  } catch (err) {
    await sendMessage(env, chatId, "Context item lưu trong KV bị hỏng (không parse được JSON).");
    return;
  }

  const itemTitle = itemCtx.title || "item";

  // Bấm nút chỉ gửi callback_query ngầm cho Worker, KHÔNG tự hiện thành tin
  // nhắn nào trong chat Telegram của operator -> nếu chỉ gửi thẳng câu trả
  // lời, operator đọc lại sau sẽ không biết bot đang trả lời cho câu hỏi/mục
  // nào. Echo lại rõ "đang hỏi sâu về gì" thành 1 tin riêng TRƯỚC, đóng vai
  // tin "của operator" trong luồng hội thoại (Telegram không cho Worker gửi
  // thay mặt operator, nên đây là cách gần nhất để luồng đọc lại còn hiểu được).
  await sendMessage(env, chatId, `🔍 Hỏi sâu thêm: "${itemTitle}"\n⏳ Đang phân tích...`);

  const prompt = buildDeepDivePrompt(itemCtx);
  const result = await callGemini(env, prompt);
  if (!result.ok) {
    await sendMessage(env, chatId, result.error);
    return;
  }
  await sendMessage(
    env,
    chatId,
    `${result.text}\n\n💬 Gõ tiếp câu hỏi RIÊNG của bạn về mục này trong 10 phút (vd "so với X thì sao?"), hoặc bỏ qua để hỏi tự do bình thường.`
  );
  // Ghi vào lịch sử hội thoại để nếu operator hỏi tiếp tự do ngay sau đó
  // ("vậy nó khác gì X?"), bot vẫn nhớ vừa đào sâu item nào.
  await appendConversation(env, chatId, `[Hỏi sâu] ${itemTitle}`, result.text);
  // Mở "cửa sổ" 10 phút để tin nhắn tự do tiếp theo của CHÍNH chat này được
  // hiểu là câu hỏi riêng về đúng item này (xem handleMessage), không phải
  // câu hỏi chung cả digest.
  await setPendingItemQuestion(env, chatId, key);
}

function buildDeepDivePrompt(itemCtx) {
  return `Đào sâu thêm về item dưới đây. CHỈ dùng đúng raw_text đã cho, đừng bịa thêm
chi tiết không có trong nguồn. Nếu raw_text không đủ thông tin để trả lời sâu
hơn, nói thẳng "nguồn không đủ chi tiết" thay vì suy diễn. Trả lời tiếng Việt,
ngắn gọn (dưới 200 từ), có thể dùng nhãn 🟢/🟡/🔴 nếu phù hợp (đã biết vs suy luận).

Loại: ${itemCtx.type || "không rõ"}
Tiêu đề: ${itemCtx.title || "không rõ"}
URL: ${itemCtx.url || "không rõ"}
Confidence đã gắn sẵn: ${itemCtx.confidence || "không rõ"}
Nội dung gốc (raw_text, rút gọn):
"""
${itemCtx.raw_text || "(không có raw_text)"}
"""

Hãy giải thích sâu hơn: cơ chế/why-now/bối cảnh, dựa đúng nội dung trên.`;
}

// ---------------------------------------------------------------------
// "pending_qa:<chatId>" — cửa sổ 10 phút sau khi bấm "Hỏi sâu thêm" để
// operator gõ câu hỏi RIÊNG về đúng item đó (xem PENDING_QA_TTL_SECONDS).
// ---------------------------------------------------------------------

async function setPendingItemQuestion(env, chatId, itemKey) {
  if (!env.DIGEST_KV) return;
  try {
    await env.DIGEST_KV.put(`pending_qa:${chatId}`, itemKey, {
      expirationTtl: PENDING_QA_TTL_SECONDS,
    });
  } catch (err) {
    console.error("setPendingItemQuestion lỗi:", err);
  }
}

async function getPendingItemQuestion(env, chatId) {
  if (!env.DIGEST_KV) return null;
  try {
    return await env.DIGEST_KV.get(`pending_qa:${chatId}`);
  } catch (err) {
    console.error("getPendingItemQuestion lỗi:", err);
    return null;
  }
}

async function clearPendingItemQuestion(env, chatId) {
  if (!env.DIGEST_KV) return;
  try {
    await env.DIGEST_KV.delete(`pending_qa:${chatId}`);
  } catch (err) {
    console.error("clearPendingItemQuestion lỗi:", err);
  }
}

// ---------------------------------------------------------------------
// Câu hỏi RIÊNG về 1 item cụ thể (sau khi bấm "Hỏi sâu thêm") -> dùng đúng
// context item đó, KHÔNG dùng toàn bộ "latest_items" như free-text thường,
// để câu trả lời tập trung đúng vào item operator vừa quan tâm.
// ---------------------------------------------------------------------

async function handleItemSpecificQuestion(chatId, itemKey, question, env) {
  if (!env.DIGEST_KV) {
    await sendMessage(env, chatId, "Worker thiếu binding KV (DIGEST_KV) — không đọc được context item.");
    return;
  }

  let raw;
  try {
    raw = await env.DIGEST_KV.get(`item:${itemKey}`);
  } catch (err) {
    await sendMessage(env, chatId, `Lỗi đọc KV: ${escapeForTelegram(String(err))}`);
    return;
  }

  if (!raw) {
    // Context item đã hết hạn (48h) hoặc bị xoá -> fallback về hỏi chung cả
    // digest thay vì báo lỗi cứng, vẫn cố trả lời được câu hỏi của operator.
    await handleFreeTextQuestion(chatId, question, env);
    return;
  }

  let itemCtx;
  try {
    itemCtx = JSON.parse(raw);
  } catch (err) {
    await handleFreeTextQuestion(chatId, question, env);
    return;
  }

  const history = await getConversation(env, chatId);
  const prompt = buildItemQuestionPrompt(itemCtx, history, question);
  const result = await callGemini(env, prompt);
  if (!result.ok) {
    await sendMessage(env, chatId, result.error);
    return;
  }
  await sendMessage(env, chatId, result.text);
  await appendConversation(env, chatId, question, result.text);
}

function buildItemQuestionPrompt(itemCtx, history, question) {
  const historyBlock = formatConversationHistory(history);
  return `Operator đang hỏi tiếp 1 câu hỏi RIÊNG về item cụ thể dưới đây (không phải
toàn bộ digest). CHỈ dùng đúng raw_text đã cho, đừng bịa thêm chi tiết không có
trong nguồn. Nếu raw_text không đủ thông tin để trả lời, nói thẳng "nguồn không
đủ chi tiết" thay vì suy diễn. Trả lời tiếng Việt, ngắn gọn.

LỊCH SỬ HỘI THOẠI GẦN ĐÂY:
${historyBlock}

Item đang được hỏi:
Loại: ${itemCtx.type || "không rõ"}
Tiêu đề: ${itemCtx.title || "không rõ"}
URL: ${itemCtx.url || "không rõ"}
Confidence: ${itemCtx.confidence || "không rõ"}
Nội dung gốc (raw_text, rút gọn):
"""
${itemCtx.raw_text || "(không có raw_text)"}
"""

Câu hỏi của operator: ${question}`;
}

// ---------------------------------------------------------------------
// Free-text follow-up -> dùng "latest_items" + "trends:latest" + lịch sử
// hội thoại của chat_id này trong KV
// ---------------------------------------------------------------------

async function handleFreeTextQuestion(chatId, question, env) {
  if (!env.DIGEST_KV) {
    await sendMessage(env, chatId, "Worker thiếu binding KV (DIGEST_KV) — không đọc được digest gần nhất.");
    return;
  }

  const [rawItems, rawTrends, history] = await Promise.all([
    env.DIGEST_KV.get("latest_items"),
    env.DIGEST_KV.get("trends:latest"),
    getConversation(env, chatId),
  ]);

  if (!rawItems) {
    await sendMessage(env, chatId, "Chưa có digest nào để hỏi, đợi lần chạy đầu tiên.");
    return;
  }

  let items;
  try {
    items = JSON.parse(rawItems);
  } catch (err) {
    await sendMessage(env, chatId, "Context digest gần nhất trong KV bị hỏng (không parse được JSON).");
    return;
  }

  // trends:latest là optional (chỉ có nếu pipeline đã chạy ít nhất 1 lần với
  // secret Cloudflare đủ) -> thiếu thì vẫn trả lời được, chỉ thiếu phần RAG
  // xu hướng dài hạn, KHÔNG block câu hỏi.
  let trends = null;
  if (rawTrends) {
    try {
      trends = JSON.parse(rawTrends);
    } catch (err) {
      trends = null;
    }
  }

  const prompt = buildFreeTextPrompt(items, trends, history, question);
  const result = await callGemini(env, prompt);
  if (!result.ok) {
    await sendMessage(env, chatId, result.error);
    return;
  }
  await sendMessage(env, chatId, result.text);
  await appendConversation(env, chatId, question, result.text);
}

function buildFreeTextPrompt(items, trends, history, question) {
  const itemsBlock = (Array.isArray(items) ? items : [])
    .map((it, idx) => {
      return `[Item ${idx + 1}] (${it.type || "?"}, confidence ${it.confidence || "?"})
Tiêu đề: ${it.title || "?"}
URL: ${it.url || "?"}
Raw: ${truncate(it.raw_text || "", 1200)}`;
    })
    .join("\n\n");

  const trendsBlock = trends ? formatTrendsForPrompt(trends) : "(chưa có dữ liệu xu hướng tích luỹ)";
  const historyBlock = formatConversationHistory(history);

  return `Bạn là research analyst riêng của operator, đã trao đổi với operator nhiều lượt
trước đó (xem lịch sử hội thoại bên dưới) — trả lời với tinh thần liên tục, có thể
nối tiếp ý đã trao đổi trước nếu liên quan, KHÔNG cần lặp lại y nguyên thông tin
operator đã biết rồi.

LỊCH SỬ HỘI THOẠI GẦN ĐÂY (cũ nhất trước, mới nhất sau):
${historyBlock}

DỮ LIỆU XU HƯỚNG TÍCH LUỸ (themes/companies/tech xuất hiện nhiều lần qua các ngày,
KHÔNG chỉ digest hôm nay):
${trendsBlock}

DIGEST GẦN NHẤT:
${itemsBlock}

Trả lời ngắn gọn, tiếng Việt, CHỈ dựa trên nội dung trên (digest + xu hướng tích luỹ +
lịch sử hội thoại) — nếu không đủ thông tin để trả lời, nói thẳng "không đủ thông tin
để trả lời câu này" thay vì bịa.

Câu hỏi mới của operator: ${question}`;
}

function formatTrendsForPrompt(trends) {
  const section = (label, list) => {
    if (!list || !list.length) return "";
    const lines = list
      .slice(0, 10)
      .map((x) => `- ${x.name} (xuất hiện ${x.count} lần, gần nhất ${x.last_seen})`);
    return `${label}:\n${lines.join("\n")}`;
  };
  const blocks = [
    section("Themes", trends.themes),
    section("Companies", trends.companies),
    section("Tech", trends.tech),
    section("Deep tech", trends.deep_tech),
  ].filter(Boolean);
  return blocks.length ? blocks.join("\n\n") : "(rỗng)";
}

// ---------------------------------------------------------------------
// Lịch sử hội thoại (conv:<chatId>) — trí nhớ "native" của bot với từng
// operator, tách biệt theo chat_id, tự trim/expire để không phình KV.
// ---------------------------------------------------------------------

async function getConversation(env, chatId) {
  if (!env.DIGEST_KV) return [];
  try {
    const raw = await env.DIGEST_KV.get(`conv:${chatId}`);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (err) {
    console.error("getConversation lỗi:", err);
    return [];
  }
}

async function appendConversation(env, chatId, userText, modelText) {
  if (!env.DIGEST_KV) return;
  const history = await getConversation(env, chatId);
  history.push({ role: "user", text: truncate(userText, 500) });
  history.push({ role: "model", text: truncate(modelText, 1000) });
  const trimmed = history.slice(-CONV_MAX_ENTRIES);
  try {
    await env.DIGEST_KV.put(`conv:${chatId}`, JSON.stringify(trimmed), {
      expirationTtl: CONV_TTL_SECONDS,
    });
  } catch (err) {
    console.error("appendConversation lỗi:", err);
  }
}

function formatConversationHistory(turns) {
  if (!turns || !turns.length) return "(chưa có lịch sử hội thoại trước đó)";
  return turns.map((t) => `${t.role === "user" ? "Operator" : "Bot"}: ${t.text}`).join("\n");
}

// ---------------------------------------------------------------------
// Gemini call (raw HTTP, không SDK)
// ---------------------------------------------------------------------

async function callGemini(env, prompt) {
  if (!env.GEMINI_API_KEY) {
    return { ok: false, error: "Worker thiếu GEMINI_API_KEY — không gọi được LLM để trả lời." };
  }

  const model = env.GEMINI_MODEL || "gemini-3.5-flash";
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${env.GEMINI_API_KEY}`;

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig: { temperature: 0.3 },
      }),
    });

    if (!resp.ok) {
      const body = await resp.text();
      return { ok: false, error: `⚠️ Gemini lỗi (status ${resp.status}): ${truncate(body, 300)}` };
    }

    const data = await resp.json();
    const text = data?.candidates?.[0]?.content?.parts?.[0]?.text;

    if (!text) {
      return { ok: false, error: "⚠️ Gemini trả response không có nội dung." };
    }

    return { ok: true, text };
  } catch (err) {
    return { ok: false, error: `⚠️ Lỗi gọi Gemini: ${escapeForTelegram(String(err))}` };
  }
}

// ---------------------------------------------------------------------
// Telegram helpers
// ---------------------------------------------------------------------

async function sendMessage(env, chatId, text, replyMarkup) {
  if (!env.TELEGRAM_BOT_TOKEN) {
    console.error("Thiếu TELEGRAM_BOT_TOKEN, không gửi được:", text);
    return;
  }
  const url = TELEGRAM_API(env.TELEGRAM_BOT_TOKEN, "sendMessage");
  const payload = {
    chat_id: chatId,
    text: truncate(text, 4000),
  };
  if (replyMarkup) payload.reply_markup = replyMarkup;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    console.error("sendMessage lỗi:", err);
  }
}

async function answerCallbackQuery(env, callbackQueryId) {
  if (!env.TELEGRAM_BOT_TOKEN) return;
  const url = TELEGRAM_API(env.TELEGRAM_BOT_TOKEN, "answerCallbackQuery");
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ callback_query_id: callbackQueryId }),
    });
  } catch (err) {
    console.error("answerCallbackQuery lỗi:", err);
  }
}

function truncate(text, maxLen) {
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen) + "... (cắt bớt)";
}

function escapeForTelegram(text) {
  // Worker dùng plain text (không parse_mode) cho message lỗi nên không cần
  // escape HTML/Markdown — giữ hàm này để rõ ý định nếu sau cần đổi parse_mode.
  return text;
}
