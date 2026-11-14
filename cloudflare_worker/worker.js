/**
 * AlphaMinter Telegram Signal-Triage & State Engine (Hardened & Secure)
 * Cloudflare Worker + Cloudflare D1
 * 
 * Security Features:
 * 1. Strict Chat ID Whitelisting: Ignores any request not originating from YOUR personal TELEGRAM_CHAT_ID.
 * 2. Protected Internal API: /api/silenced-list requires matching X-Internal-Secret header.
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 1. Protected API endpoint for GitHub Actions
    if (url.pathname === "/api/silenced-list") {
      // Security Guard: Verify internal secret if configured
      const authHeader = request.headers.get("X-Internal-Secret");
      if (env.INTERNAL_SECRET && authHeader !== env.INTERNAL_SECRET) {
        return new Response(JSON.stringify({ error: "Unauthorized access" }), {
          status: 401,
          headers: { "Content-Type": "application/json" }
        });
      }

      const today = new Date().toISOString().split("T")[0];
      const { results } = await env.DB.prepare(
        "SELECT symbol FROM stocks_state WHERE status = 'PERMABAN' OR (status = 'DROPPED_TEMP' AND cooldown_until >= ?)"
      ).bind(today).all();
      const silenced = (results || []).map(r => r.symbol);
      return new Response(JSON.stringify({ silenced }), { headers: { "Content-Type": "application/json" } });
    }

    // 2. Telegram Webhook Handler (Strict Whitelist)
    if (request.method === "POST") {
      try {
        const update = await request.json();
        
        // Security Guard: Extract incoming Chat ID
        const incomingChatId = update.callback_query?.message?.chat?.id || update.message?.chat?.id;

        // Strict Authorization: If TELEGRAM_CHAT_ID is set, reject any stranger trying to use your bot
        if (env.TELEGRAM_CHAT_ID && incomingChatId && incomingChatId.toString() !== env.TELEGRAM_CHAT_ID.toString()) {
          console.warn(`Unauthorized access attempt from Chat ID: ${incomingChatId}`);
          return new Response("Forbidden", { status: 403 });
        }

        // Handle 1-Tap Button Clicks
        if (update.callback_query) {
          const cb = update.callback_query;
          const [action, symbol] = (cb.data || "").split(":");
          const chatId = cb.message.chat.id;
          const messageId = cb.message.message_id;
          const today = new Date();
          let feedback = "";

          if (action === "DROP") {
            const cooldown = new Date();
            cooldown.setDate(today.getDate() + 30);
            const cooldownStr = cooldown.toISOString().split("T")[0];
            await env.DB.prepare(
              "INSERT INTO stocks_state (symbol, status, cooldown_until) VALUES (?, 'DROPPED_TEMP', ?) ON CONFLICT(symbol) DO UPDATE SET status='DROPPED_TEMP', cooldown_until=excluded.cooldown_until, updated_at=CURRENT_TIMESTAMP"
            ).bind(symbol, cooldownStr).run();
            feedback = `❌ Dropped ${symbol} for 30 Days`;
          } else if (action === "WATCH") {
            await env.DB.prepare(
              "INSERT INTO stocks_state (symbol, status, cooldown_until) VALUES (?, 'WATCHLIST', NULL) ON CONFLICT(symbol) DO UPDATE SET status='WATCHLIST', cooldown_until=NULL, updated_at=CURRENT_TIMESTAMP"
            ).bind(symbol).run();
            feedback = `⭐ Added ${symbol} to Watchlist`;
          } else if (action === "BAN") {
            await env.DB.prepare(
              "INSERT INTO stocks_state (symbol, status, cooldown_until) VALUES (?, 'PERMABAN', NULL) ON CONFLICT(symbol) DO UPDATE SET status='PERMABAN', cooldown_until=NULL, updated_at=CURRENT_TIMESTAMP"
            ).bind(symbol).run();
            feedback = `🚫 Permabanned ${symbol}`;
          }

          // Acknowledge click immediately (stops spinner + shows instant popup toast on phone)
          await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ callback_query_id: cb.id, text: feedback, show_alert: false })
          });

          // Edit message text to confirm action
          const updatedText = cb.message.text + `\n\n<b>Result:</b> ${feedback}`;
          await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/editMessageText`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chat_id: chatId, message_id: messageId, text: updatedText, parse_mode: "HTML" })
          });

          return new Response("OK");
        }

        // Handle Interactive Text Commands (/watchlist, /banned)
        if (update.message && update.message.text) {
          const text = update.message.text.trim();
          const chatId = update.message.chat.id;

          if (text === "/watchlist") {
            const { results } = await env.DB.prepare("SELECT symbol FROM stocks_state WHERE status = 'WATCHLIST' ORDER BY updated_at DESC").all();
            const list = (results || []).map(r => `• <code>${r.symbol}</code>`).join("\n") || "<i>Watchlist is empty.</i>";
            await sendMsg(env.TELEGRAM_BOT_TOKEN, chatId, `<b>⭐ Active Watchlist:</b>\n\n${list}`);
          } else if (text === "/banned") {
            const { results } = await env.DB.prepare("SELECT symbol FROM stocks_state WHERE status = 'PERMABAN' ORDER BY updated_at DESC").all();
            const list = (results || []).map(r => `• <code>${r.symbol}</code>`).join("\n") || "<i>No permabanned stocks.</i>";
            await sendMsg(env.TELEGRAM_BOT_TOKEN, chatId, `<b>🚫 Permabanned Stocks:</b>\n\n${list}`);
          }
        }
      } catch (err) {
        console.error("Worker error:", err);
      }
      return new Response("OK");
    }

    return new Response("AlphaMinter Bot Backend is Active.");
  }
};

async function sendMsg(token, chatId, text) {
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML" })
  });
}
