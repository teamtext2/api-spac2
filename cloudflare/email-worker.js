/**
 * Cloudflare Email Routing Worker: Spac2 Inbound Reverse Email Verifier
 * 
 * Flow:
 * 1. Cloudflare receives email sent to `verify@spac2.com`.
 * 2. This worker reads the stream and extracts sender email and the verification token.
 * 3. Worker sends a secure HTTPS POST webhook to Spac2 FastAPI backend.
 * 4. FastAPI verifies the token and marks the user account as verified.
 */

export default {
  async email(message, env, ctx) {
    const sender = (message.from || "").toLowerCase().trim();
    const recipient = (message.to || "").toLowerCase().trim();
    const subject = message.headers.get("subject") || "";

    console.log(`[Email Verifier] Inbound email from: ${sender} to: ${recipient}, subject: "${subject}"`);

    // 1. Read the raw message content (stream)
    let rawContent = "";
    try {
      const rawStream = message.raw;
      const reader = rawStream.getReader();
      const decoder = new TextDecoder("utf-8");
      
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        rawContent += decoder.decode(value, { stream: true });
        // Cap buffer at 64KB to avoid excessive memory usage
        if (rawContent.length > 65536) break;
      }
    } catch (err) {
      console.error("[Email Verifier] Error reading message stream:", err);
    }

    // 2. Extract Token from Subject and Raw Body
    const combinedText = `${subject}\n${rawContent}`;
    let token = null;

    // Standard Spac2 Token Pattern: SPAC2-[A-Z0-9]{6,12}
    const tokenRegex = /(SPAC2-[A-Z0-9]{6,12})/i;
    const match = combinedText.match(tokenRegex);

    if (match) {
      token = match[1].toUpperCase();
    } else {
      // Fallback Pattern: Token: XXXXXX or Token = XXXXXX
      const fallbackRegex = /token[:\s=]+([a-zA-Z0-9_-]{6,16})/i;
      const fallbackMatch = combinedText.match(fallbackRegex);
      if (fallbackMatch) {
        token = fallbackMatch[1].trim();
      }
    }

    if (!token) {
      console.warn(`[Email Verifier] No valid verification token found from sender: ${sender}`);
      return;
    }

    console.log(`[Email Verifier] Detected token ${token} for sender ${sender}`);

    // 3. Dispatch Webhook to Spac2 FastAPI Backend
    const webhookUrl = env.FASTAPI_WEBHOOK_URL || "https://api1.spac2.com/api/auth/email-webhook";
    const webhookSecret = env.WEBHOOK_SECRET_KEY || "spac2_super_secure_secret_2026";

    try {
      const response = await fetch(webhookUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Spac2-Secret": webhookSecret,
          "User-Agent": "Cloudflare-Email-Worker/Spac2-ReverseAuth"
        },
        body: JSON.stringify({
          sender: sender,
          recipient: recipient,
          token: token,
          subject: subject,
          received_at: new Date().toISOString()
        })
      });

      if (!response.ok) {
        const errText = await response.text();
        console.error(`[Email Verifier] Webhook failed (HTTP ${response.status}): ${errText}`);
      } else {
        const resData = await response.json();
        console.log(`[Email Verifier] Successfully verified user via webhook:`, resData);
      }
    } catch (e) {
      console.error("[Email Verifier] Network error sending webhook to FastAPI:", e);
    }
  }
};
