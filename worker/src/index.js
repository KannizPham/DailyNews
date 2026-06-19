/**
 * Cloudflare Worker — webhook tương tác Telegram cho Morning Intel Agent.
 *
 * Xử lý 3 loại update Telegram:
 *   1. command /start          -> welcome message.
 *   2. command /refresh        -> trigger lại pipeline qua GitHub
 *      repository_dispatch (event_type "refresh-digest").
 *   3. callback_query "qa:<key>" (bấm nút "🔍 Hỏi sâu thêm" dưới digest)
 *      -> đọc context item từ KV (key ghi bởi src/deliver.py qua
 *      write_items_to_kv), hỏi Gemini đào sâu thêm, reply.
 *   4. text tự do khác         -> coi là câu hỏi follow-up, dùng toàn bộ
 *      context "latest_items" trong KV làm nền, hỏi Gemini, trả lời.
 *
 * Nguyên tắc: KHÔNG để Telegram timeout im lặng — mọi nhánh lỗi (KV rỗng,
 * Gemini lỗi, GitHub API lỗi) đều phải sendMessage báo lỗi rõ cho operator.
 *
 * Không dùng SDK nào (Gemini gọi raw fetch HTTP) để giữ Worker thuần JS,
 * không cần build step — theo đúng yêu cầu "free tier, không framework".
 */

const TELEGRAM_API = (token, method) => `https://api.telegram.org/bot${token}/${method}`;

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("Morning Intel webhook đang chạy. Dùng POST từ Telegram.", {
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
    await sendMessage(env, chatId, WELCOME_TEXT);
    return;
  }

  if (text === "/refresh" || text.startsWith("/refresh ")) {
    await handleRefresh(chatId, env);
    return;
  }

  if (text.startsWith("/")) {
    await sendMessage(
      env,
      chatId,
      "Lệnh không nhận diện được. Lệnh có sẵn: /start, /refresh. Hoặc gõ câu hỏi tự do về digest gần nhất."
    );
    return;
  }

  if (!text) {
    // Update message không có text (ảnh, sticker...) — bỏ qua êm, không spam lỗi.
    return;
  }

  await handleFreeTextQuestion(chatId, text, env);
}

const WELCOME_TEXT = `👋 Chào operator!

Đây là Morning Intel Agent — bot gửi digest tình báo buổi sáng (6 mục: tín hiệu, lăng kính tiền, lăng kính người xây, deep tech, ngoài đường ray, nối điểm).

Lệnh có sẵn:
• /start — xem lại hướng dẫn này.
• /refresh — chạy lại pipeline ngay (digest mới sẽ tới trong vài phút).
• Bấm nút "🔍 Hỏi sâu thêm" dưới mỗi mục digest để hỏi sâu riêng item đó.
• Hoặc gõ thẳng câu hỏi tự do — bot sẽ dùng digest gần nhất làm nền để trả lời.`;

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
        "User-Agent": "morning-intel-webhook",
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
// callback_query "qa:<key>" -> đào sâu 1 item cụ thể
// ---------------------------------------------------------------------

async function handleCallbackQuery(callbackQuery, env) {
  const chatId = callbackQuery.message?.chat?.id;
  const data = callbackQuery.data || "";

  // answerCallbackQuery TRƯỚC để Telegram tắt loading xoay vô hạn trên nút,
  // dù phần xử lý sau có chậm/lỗi.
  await answerCallbackQuery(env, callbackQuery.id);

  if (!chatId) return;

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

  const prompt = buildDeepDivePrompt(itemCtx);
  await askGeminiAndReply(env, chatId, prompt, "deep-dive");
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
// Free-text follow-up -> dùng toàn bộ "latest_items" trong KV
// ---------------------------------------------------------------------

async function handleFreeTextQuestion(chatId, question, env) {
  if (!env.DIGEST_KV) {
    await sendMessage(env, chatId, "Worker thiếu binding KV (DIGEST_KV) — không đọc được digest gần nhất.");
    return;
  }

  let raw;
  try {
    raw = await env.DIGEST_KV.get("latest_items");
  } catch (err) {
    await sendMessage(env, chatId, `Lỗi đọc KV: ${escapeForTelegram(String(err))}`);
    return;
  }

  if (!raw) {
    await sendMessage(env, chatId, "Chưa có digest nào để hỏi, đợi lần chạy đầu tiên.");
    return;
  }

  let items;
  try {
    items = JSON.parse(raw);
  } catch (err) {
    await sendMessage(env, chatId, "Context digest gần nhất trong KV bị hỏng (không parse được JSON).");
    return;
  }

  const prompt = buildFreeTextPrompt(items, question);
  await askGeminiAndReply(env, chatId, prompt, "free-text-qa");
}

function buildFreeTextPrompt(items, question) {
  const itemsBlock = (Array.isArray(items) ? items : [])
    .map((it, idx) => {
      return `[Item ${idx + 1}] (${it.type || "?"}, confidence ${it.confidence || "?"})
Tiêu đề: ${it.title || "?"}
URL: ${it.url || "?"}
Raw: ${truncate(it.raw_text || "", 1200)}`;
    })
    .join("\n\n");

  return `Bạn là research analyst riêng của operator. Dưới đây là toàn bộ item trong
digest gần nhất (đã có nhãn confidence 🟢🟡🔴 gắn sẵn, không tự đổi nhãn).
Operator hỏi một câu follow-up tự do. Trả lời ngắn gọn, tiếng Việt, CHỈ dựa
trên nội dung dưới đây — nếu không đủ thông tin để trả lời, nói thẳng "không
đủ thông tin trong digest để trả lời câu này" thay vì bịa.

${itemsBlock}

Câu hỏi của operator: ${question}`;
}

// ---------------------------------------------------------------------
// Gemini call (raw HTTP, không SDK)
// ---------------------------------------------------------------------

async function askGeminiAndReply(env, chatId, prompt, contextLabel) {
  if (!env.GEMINI_API_KEY) {
    await sendMessage(env, chatId, "Worker thiếu GEMINI_API_KEY — không gọi được LLM để trả lời.");
    return;
  }

  const model = env.GEMINI_MODEL || "gemini-flash-lite-latest";
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
      await sendMessage(
        env,
        chatId,
        `⚠️ Gemini lỗi (${contextLabel}, status ${resp.status}): ${truncate(body, 300)}`
      );
      return;
    }

    const data = await resp.json();
    const text = data?.candidates?.[0]?.content?.parts?.[0]?.text;

    if (!text) {
      await sendMessage(env, chatId, `⚠️ Gemini trả response không có nội dung (${contextLabel}).`);
      return;
    }

    await sendMessage(env, chatId, text);
  } catch (err) {
    await sendMessage(env, chatId, `⚠️ Lỗi gọi Gemini (${contextLabel}): ${escapeForTelegram(String(err))}`);
  }
}

// ---------------------------------------------------------------------
// Telegram helpers
// ---------------------------------------------------------------------

async function sendMessage(env, chatId, text) {
  if (!env.TELEGRAM_BOT_TOKEN) {
    console.error("Thiếu TELEGRAM_BOT_TOKEN, không gửi được:", text);
    return;
  }
  const url = TELEGRAM_API(env.TELEGRAM_BOT_TOKEN, "sendMessage");
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: truncate(text, 4000),
      }),
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
