/**
 * Cloudflare Worker — webhook Telegram cho Bản Tin Kinh Tế & Thị Trường.
 *
 * Xử lý các loại update Telegram:
 *   1. command /start          -> welcome message + nút nhanh "🔄 Refresh".
 *   2. command /refresh HOẶC callback_query "refresh" (nút nhanh dưới /start)
 *      -> trigger lại pipeline qua GitHub repository_dispatch (event_type
 *      "refresh-digest"). Cả 2 đường dùng chung handleRefresh().
 *   3. command /trends         -> đọc rolling trend kinh tế/thị trường từ key
 *      "trends:latest" (ghi bởi src/deliver.py::write_trends_to_kv) và trả
 *      top 5 topic theo document frequency, KHÔNG gọi LLM.
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
  const displayName = getTelegramDisplayName(message);
  await sendMessage(env, chatId, buildWelcomeText(displayName), START_KEYBOARD);
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

function buildWelcomeText(displayName) {
  return `👋 Chào ${displayName}!

Đây là Bản Tin Kinh Tế & Thị Trường.
Bot tổng hợp và phân tích tin tức kinh tế, tài chính, ngân hàng, chứng khoán, doanh nghiệp, công nghệ và địa chính trị.

Lệnh có sẵn:
• /start — xem lại hướng dẫn này.
• /refresh — chạy lại pipeline ngay.
• /trends — xem xu hướng tích luỹ.
• /forget — xoá lịch sử hội thoại.
• Bấm nút "🔍 Hỏi sâu thêm" dưới mỗi mục digest để hỏi riêng về mục đó.
• Hoặc gõ thẳng câu hỏi tự do về digest gần nhất.

👇 Hoặc bấm nút nhanh dưới đây:`;
}

function getTelegramDisplayName(message) {
  const firstName = (message?.from?.first_name || "").trim();
  const lastName = (message?.from?.last_name || "").trim();
  const username = (message?.from?.username || "").trim();

  const fullName = [firstName, lastName].filter(Boolean).join(" ");

  if (fullName) return fullName;
  if (username) return `@${username}`;

  return "bạn";
}

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

  let trends = null;
  if (raw) {
    try {
      trends = JSON.parse(raw);
    } catch (err) {
      trends = null;
    }
  }

  if (!trends || !hasTrendData(trends)) {
    await sendMessage(
      env,
      chatId,
      "Chưa có dữ liệu xu hướng. Hãy chạy workflow ngày ít nhất một lần và kiểm tra cấu hình Cloudflare KV."
    );
    return;
  }

  await sendMessage(env, chatId, buildTrendsMessage(trends));
}

function hasTrendData(trends) {
  return trends?.schema_version === 2 && Array.isArray(trends?.trends) && trends.trends.length > 0;
}

function buildTrendsMessage(trends) {
  const windowDays = trends.window_days === 14 ? 14 : 7;
  const items = trends.trends.slice(0, 5).map((item, index) =>
    [
      `${index + 1}. ${item.label_vi}`,
      `   • Xuất hiện trong: ${item.count} bản tin`,
      `   • Gần nhất: ${item.last_seen}`,
      `   • Diễn biến: ${item.direction}`,
    ].join("\n")
  );
  return [`📊 Xu hướng ${windowDays} ngày gần nhất`, "", ...items].join("\n\n");
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
  const result = await callLLMWithFallback(env, prompt);
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
  const result = await callLLMWithFallback(env, prompt);
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
  const result = await callLLMWithFallback(env, prompt);
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

  const trendsBlock = hasTrendData(trends) ? formatTrendsForPrompt(trends) : "(chưa có dữ liệu xu hướng gần đây)";
  const historyBlock = formatConversationHistory(history);

  return `Bạn là research analyst riêng của operator, đã trao đổi với operator nhiều lượt
trước đó (xem lịch sử hội thoại bên dưới) — trả lời với tinh thần liên tục, có thể
nối tiếp ý đã trao đổi trước nếu liên quan, KHÔNG cần lặp lại y nguyên thông tin
operator đã biết rồi.

LỊCH SỬ HỘI THOẠI GẦN ĐÂY (cũ nhất trước, mới nhất sau):
${historyBlock}

DỮ LIỆU XU HƯỚNG KINH TẾ VÀ THỊ TRƯỜNG GẦN ĐÂY:
${trendsBlock}

DIGEST GẦN NHẤT:
${itemsBlock}

Trả lời ngắn gọn, tiếng Việt, CHỈ dựa trên nội dung trên (digest + xu hướng gần đây +
lịch sử hội thoại) — nếu không đủ thông tin để trả lời, nói thẳng "không đủ thông tin
để trả lời câu này" thay vì bịa.

Câu hỏi mới của operator: ${question}`;
}

function formatTrendsForPrompt(trends) {
  const items = Array.isArray(trends?.trends) ? trends.trends.slice(0, 5) : [];
  if (!items.length) return "(rỗng)";
  return items
    .map(
      (item) =>
        `- ${item.label_vi}: ${item.count} bản tin, gần nhất ${item.last_seen}, ${item.direction}`
    )
    .join("\n");
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
// LLM fallback (raw HTTP, không SDK)
// ---------------------------------------------------------------------

const GEMINI_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash"];
const LLM_RETRYABLE_STATUSES = new Set([429, 500, 502, 503, 504]);
const LLM_MAX_ATTEMPTS = 2;

async function callLLMWithFallback(env, prompt) {
  const attempted = [];
  let lastError = "không rõ nguyên nhân";

  if (env.GEMINI_API_KEY) {
    for (const model of GEMINI_MODELS) {
      const result = await callGeminiModel(env, prompt, model);
      attempted.push(model);
      if (result.ok) return result;
      lastError = result.error;
      if (!result.allowFallback) return result;
    }
  }

  if (env.OPENROUTER_API_KEY) {
    const result = await callOpenRouter(env, prompt);
    attempted.push("openrouter");
    if (result.ok) return result;
    lastError = result.error;
    if (!result.allowFallback) return result;
  }

  if (env.DEEPSEEK_API_KEY) {
    const result = await callDeepSeek(env, prompt);
    attempted.push("deepseek");
    if (result.ok) return result;
    lastError = result.error;
  }

  if (!attempted.length) {
    return {
      ok: false,
      error: "Worker thiếu GEMINI_API_KEY, OPENROUTER_API_KEY và DEEPSEEK_API_KEY — không gọi được LLM để trả lời.",
    };
  }

  return {
    ok: false,
    error: sanitizeLLMError(
      `⚠️ Tất cả LLM đều thất bại (đã thử: ${attempted.join(", ")}). Lỗi cuối: ${lastError}`,
      env
    ),
  };
}

async function callGeminiModel(env, prompt, model) {
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`;
  const request = await fetchLLMWithRetry(
    url,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-goog-api-key": env.GEMINI_API_KEY,
      },
      body: JSON.stringify({
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig: { temperature: 0.3 },
      }),
    },
    model,
    env
  );
  if (!request.response) {
    return { ok: false, allowFallback: true, error: request.error };
  }

  const body = await request.response.text();
  if (!request.response.ok) {
    const unavailable =
      request.response.status === 404 || isModelUnavailableError(body);
    const allowFallback =
      unavailable || LLM_RETRYABLE_STATUSES.has(request.response.status);
    return {
      ok: false,
      allowFallback,
      error: sanitizeLLMError(
        `⚠️ ${model} lỗi HTTP ${request.response.status}: ${providerErrorDetail(body)}`,
        env
      ),
    };
  }

  try {
    const data = JSON.parse(body);
    const candidates = Array.isArray(data?.candidates) ? data.candidates : [];
    const text = candidates
      .flatMap((candidate) =>
        Array.isArray(candidate?.content?.parts) ? candidate.content.parts : []
      )
      .map((part) => (typeof part?.text === "string" ? part.text : ""))
      .join("")
      .trim();
    if (!text) {
      return {
        ok: false,
        allowFallback: true,
        error: `⚠️ ${model} trả response không có nội dung.`,
      };
    }
    return { ok: true, text, engine: model };
  } catch (err) {
    return {
      ok: false,
      allowFallback: true,
      error: sanitizeLLMError(`⚠️ ${model} trả response JSON không hợp lệ: ${err}`, env),
    };
  }
}

async function callOpenRouter(env, prompt) {
  const request = await fetchLLMWithRetry(
    "https://openrouter.ai/api/v1/chat/completions",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.OPENROUTER_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "openrouter/free",
        messages: [{ role: "user", content: prompt }],
        temperature: 0.3,
      }),
    },
    "openrouter",
    env
  );
  if (!request.response) {
    return { ok: false, allowFallback: true, error: request.error };
  }

  const body = await request.response.text();
  if (!request.response.ok) {
    return {
      ok: false,
      allowFallback: ![400, 401, 403].includes(request.response.status),
      error: sanitizeLLMError(
        `⚠️ openrouter lỗi HTTP ${request.response.status}: ${providerErrorDetail(body)}`,
        env
      ),
    };
  }

  try {
    const data = JSON.parse(body);
    const content = data?.choices?.[0]?.message?.content;
    const text = Array.isArray(content)
      ? content
          .map((part) => (typeof part?.text === "string" ? part.text : ""))
          .join("")
          .trim()
      : typeof content === "string"
        ? content.trim()
        : "";
    if (!text) {
      return { ok: false, allowFallback: true, error: "⚠️ openrouter trả response không có nội dung." };
    }
    return { ok: true, text, engine: "openrouter" };
  } catch (err) {
    return {
      ok: false,
      allowFallback: true,
      error: sanitizeLLMError(`⚠️ openrouter trả response JSON không hợp lệ: ${err}`, env),
    };
  }
}

async function callDeepSeek(env, prompt) {
  const request = await fetchLLMWithRetry(
    "https://api.deepseek.com/anthropic/v1/messages",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": env.DEEPSEEK_API_KEY,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: env.DEEPSEEK_MODEL || "deepseek-v4-flash",
        max_tokens: 4096,
        temperature: 0.3,
        messages: [{ role: "user", content: prompt }],
      }),
    },
    "deepseek",
    env
  );
  if (!request.response) {
    return { ok: false, allowFallback: true, error: request.error };
  }

  const body = await request.response.text();
  if (!request.response.ok) {
    return {
      ok: false,
      allowFallback: ![400, 401, 403].includes(request.response.status),
      error: sanitizeLLMError(
        `⚠️ deepseek lỗi HTTP ${request.response.status}: ${providerErrorDetail(body)}`,
        env
      ),
    };
  }

  try {
    const data = JSON.parse(body);
    const text = Array.isArray(data?.content)
      ? data.content
          .map((part) => (part?.type === "text" && typeof part.text === "string" ? part.text : ""))
          .join("")
          .trim()
      : "";
    if (!text) {
      return { ok: false, allowFallback: true, error: "⚠️ deepseek trả response không có nội dung." };
    }
    return { ok: true, text, engine: "deepseek" };
  } catch (err) {
    return {
      ok: false,
      allowFallback: true,
      error: sanitizeLLMError(`⚠️ deepseek trả response JSON không hợp lệ: ${err}`, env),
    };
  }
}

async function fetchLLMWithRetry(url, options, engine, env) {
  for (let attempt = 1; attempt <= LLM_MAX_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(url, options);
      if (LLM_RETRYABLE_STATUSES.has(response.status) && attempt < LLM_MAX_ATTEMPTS) {
        continue;
      }
      return { response };
    } catch (err) {
      const safeError = sanitizeLLMError(err, env);
      if (attempt === LLM_MAX_ATTEMPTS) {
        return {
          response: null,
          error: `⚠️ ${engine} không kết nối được sau ${LLM_MAX_ATTEMPTS} lần thử: ${safeError}`,
        };
      }
    }
  }
  return { response: null, error: `⚠️ ${engine} request thất bại.` };
}

function isModelUnavailableError(body) {
  return /model[^.\n]*(not found|unavailable|not available)|(not found|unavailable|not available)[^.\n]*model/i.test(
    body
  );
}

function providerErrorDetail(body) {
  try {
    const parsed = JSON.parse(body);
    const message = parsed?.error?.message ?? parsed?.error;
    if (typeof message === "string" && message.trim()) return truncate(message.trim(), 300);
  } catch (err) {
    // Body không phải JSON: chỉ dùng đoạn text ngắn đã được sanitize ở caller.
  }
  return truncate(body || "không có chi tiết", 300);
}

function sanitizeLLMError(value, env) {
  let safe = String(value);
  const secrets = [
    env?.GEMINI_API_KEY,
    env?.OPENROUTER_API_KEY,
    env?.DEEPSEEK_API_KEY,
    env?.TELEGRAM_BOT_TOKEN,
    env?.GITHUB_PAT,
  ];
  for (const secret of secrets) {
    if (typeof secret === "string" && secret) {
      safe = safe.split(secret).join("[REDACTED]");
    }
  }
  safe = safe.replace(/AIza[A-Za-z0-9_-]{16,}/g, "[REDACTED]");
  safe = safe.replace(/sk-or-v1-[A-Za-z0-9_-]+/g, "[REDACTED]");
  safe = safe.replace(/(authorization|x-goog-api-key|x-api-key)\s*[:=]\s*[^\s,;]+/gi, "$1: [REDACTED]");
  return truncate(safe, 500);
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

function getTelegramDisplayName(message) {
  const firstName = (message?.from?.first_name || "").trim();
  const lastName = (message?.from?.last_name || "").trim();
  const username = (message?.from?.username || "").trim();

  const fullName = [firstName, lastName].filter(Boolean).join(" ");

  if (fullName) return fullName;
  if (username) return `@${username}`;

  return "bạn";
}
function sanitizeSensitiveText(value, env) {
  let text = String(value ?? "");

  const secrets = [
    env.GEMINI_API_KEY,
    env.OPENROUTER_API_KEY,
    env.DEEPSEEK_API_KEY,
    env.TELEGRAM_BOT_TOKEN,
    env.GITHUB_PAT,
  ].filter(Boolean);

  for (const secret of secrets) {
    text = text.split(secret).join("[REDACTED]");
  }

  // Che các chuỗi token phổ biến ngay cả khi env không có sẵn.
  text = text
    .replace(/sk-or-v1-[A-Za-z0-9_-]+/g, "[REDACTED_OPENROUTER_KEY]")
    .replace(/ghp_[A-Za-z0-9]+/g, "[REDACTED_GITHUB_TOKEN]")
    .replace(/github_pat_[A-Za-z0-9_]+/g, "[REDACTED_GITHUB_TOKEN]")
    .replace(/\b\d{8,12}:[A-Za-z0-9_-]{20,}\b/g, "[REDACTED_TELEGRAM_TOKEN]");

  return text;
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
