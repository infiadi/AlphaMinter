/**
 * AlphaMinter Telegram Signal-Triage & State Engine
 * Cloudflare Worker + Cloudflare D1 (Permanent Free Tier)
 * 
 * Features:
 * 1. Handles Telegram inline button callbacks in <100ms
 * 2. Manages stocks_state in Cloudflare D1 SQL database
 * 3. Answers text commands (/watchlist, /banned)
 * 4. Exposes HTTPS /api/silenced-list for GitHub Actions
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 1. API endpoint for GitHub Actions: Fetch list of all silenced stocks
    if (url.pathname === "/api/silenced-list") {
      const today = new Date().toISOString().split("T")[0];
      const { results } = await env.DB.prepare(
        "SELECT symbol FROM stocks_state WHERE status = 'PERMABAN' OR (status = 'DROPPED_TEMP' AND cooldown_until >= ?)"
      ).bind(today).all();
      const silenced = (results || []).map(r => r.symbol);
      return new Response(JSON.stringify({ silenced }), { headers: { "Content-Type": "application/json" } });
    }

    // 2. Telegram Webhook Handler (Instant <100ms response)
    if (request.method === "POST") {
      try {
        const update = await request.json();

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
