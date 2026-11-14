/**
 * AlphaMinter Telegram Signal-Triage & State Engine (Hardened & Secure)
 * Cloudflare Worker + Cloudflare D1
 * 
 * Features:
 * 1. Strict Chat ID Whitelisting (Zero stranger access)
 * 2. Protected Internal API (/api/silenced-list)
 * 3. 1-Tap Triage Buttons ([Watchlist], [Drop 30D], [Permaban])
 * 4. Interactive Commands: /watchlist, /banned, /unban <SYMBOL>
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 1. Protected API endpoint for GitHub Actions
    if (url.pathname === "/api/silenced-list") {
      const authHeader = request.headers.get("X-Internal-Secret");
      if (env.INTERNAL_SECRET && authHeader !== env.INTERNAL_SECRET) {
        return new Response(JSON.stringify({ error: "Unauthorized access" }), {
          status: 401,
          headers: { "Content-Type": "application/json" }
        });
      }

      const { results } = await env.DB.prepare(
        "SELECT symbol FROM stocks_state WHERE status = 'PERMABAN' OR (status = 'DROPPED_TEMP' AND cooldown_until > date('now'))"
      ).all();
      const silenced = (results || []).map(r => r.symbol);
      return new Response(JSON.stringify({ silenced }), { headers: { "Content-Type": "application/json" } });
    }

    // 2. Telegram Native Command Menu Registration (/api/setup-commands)
    if (url.pathname === "/api/setup-commands") {
      const commands = [
        { command: "watchlist", description: "View stocks on active watchlist" },
        { command: "banned", description: "View permabanned stocks" },
        { command: "unban", description: "Restore a stock: /unban SYMBOL" },
        { command: "help", description: "Show commands and scan schedule" }
      ];
      const resp = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/setMyCommands`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ commands })
      });
      const data = await resp.json();
      return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
    }

    // 2. Telegram Webhook Handler (Strict Whitelist)
    if (request.method === "POST") {
      try {
        const update = await request.json();
        
        // Security Guard: Extract incoming Chat ID
        const incomingChatId = update.callback_query?.message?.chat?.id || update.message?.chat?.id;

        // Strict Authorization: If TELEGRAM_CHAT_ID is set, reject strangers
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

        // Handle Interactive Text Commands (/watchlist, /banned, /unban <SYMBOL>, /help)
        if (update.message && update.message.text) {
          const text = update.message.text.trim();
          const lowerText = text.toLowerCase();
          const chatId = update.message.chat.id;

          const helpMessage = `<b>🤖 AlphaMinter Bot Commands & Guide</b>\n\n` +
            `<b>📋 Available Commands:</b>\n` +
            `• <code>/watchlist</code> — View stocks saved to your active watchlist\n` +
            `• <code>/banned</code> — View stocks currently permabanned from alerts\n` +
            `• <code>/unban SYMBOL</code> — Restore a stock (e.g. <code>/unban GOLDIETF</code>)\n` +
            `• <code>/help</code> — Show this help guide\n\n` +
            `<b>⚡ 1-Tap Alert Buttons:</b>\n` +
            `When an alert arrives on your phone, use the inline buttons:\n` +
            `• <b>⭐ Watchlist:</b> Add stock to your active watchlist\n` +
            `• <b>❌ Drop 30D:</b> Silence alerts for 30 days (cooldown)\n` +
            `• <b>🚫 Permaban:</b> Permanently exclude stock from all future scans\n\n` +
            `<b>🕒 Autonomous Scan Schedule (IST):</b>\n` +
            `• <b>18:45</b> — Delivery Accumulation (Bhavcopy)\n` +
            `• <b>19:00</b> — PEAD Earnings Drift\n` +
            `• <b>19:15</b> — Buyback Arbitrage Monitor\n` +
            `• <b>19:30</b> — 20%+ Forward Guidance (Gemini AI)\n\n` +
            `<i>Tip: Tap or copy any command above to run it instantly.</i>`;

          if (lowerText === "/help" || lowerText === "help" || lowerText === "/start" || lowerText === "start") {
            // Register Telegram native popup menu commands in background
            fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/setMyCommands`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                commands: [
                  { command: "watchlist", description: "View stocks on active watchlist" },
                  { command: "banned", description: "View permabanned stocks" },
                  { command: "unban", description: "Restore a stock: /unban SYMBOL" },
                  { command: "help", description: "Show commands and scan schedule" }
                ]
              })
            }).catch(() => {});
            await sendMsg(env.TELEGRAM_BOT_TOKEN, chatId, helpMessage);
          } else if (lowerText === "/watchlist" || lowerText === "watchlist") {
            const { results } = await env.DB.prepare("SELECT symbol FROM stocks_state WHERE status = 'WATCHLIST' ORDER BY updated_at DESC").all();
            const list = (results || []).map(r => `• <code>${r.symbol}</code>`).join("\n") || "<i>Watchlist is empty.</i>";
            await sendMsg(env.TELEGRAM_BOT_TOKEN, chatId, `<b>⭐ Active Watchlist:</b>\n\n${list}`);
          } else if (lowerText === "/banned" || lowerText === "banned") {
            const { results } = await env.DB.prepare("SELECT symbol FROM stocks_state WHERE status = 'PERMABAN' ORDER BY updated_at DESC").all();
            const list = (results || []).map(r => `• <code>${r.symbol}</code>`).join("\n") || "<i>No permabanned stocks.</i>";
            await sendMsg(env.TELEGRAM_BOT_TOKEN, chatId, `<b>🚫 Permabanned Stocks:</b>\n\n${list}\n\n<i>To unban a stock, send:</i>\n<code>/unban SYMBOL</code>`);
          } else if (lowerText.startsWith("/unban") || lowerText.startsWith("unban")) {
            const parts = text.split(/\s+/);
            if (parts.length < 2) {
              await sendMsg(env.TELEGRAM_BOT_TOKEN, chatId, "⚠️ <b>Usage:</b> <code>/unban SYMBOL</code>\n<i>Example: /unban GOLDIETF</i>");
            } else {
              const targetSymbol = parts[1].trim().toUpperCase();
              await env.DB.prepare("DELETE FROM stocks_state WHERE symbol = ?").bind(targetSymbol).run();
              await sendMsg(env.TELEGRAM_BOT_TOKEN, chatId, `✅ <b>Unbanned <code>${targetSymbol}</code></b>.\nIt is now active for future scans.`);
            }
          } else {
            // Default fallback for /help, /start, help, or ANY unrecognized text
            await sendMsg(env.TELEGRAM_BOT_TOKEN, chatId, helpMessage);
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
