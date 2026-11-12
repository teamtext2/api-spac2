/**
 * Cloudflare Worker: Spac2 Edge Email & OTP Verification Engine
 * Domain: email-verify.spac2.com
 * 
 * Features:
 * - 100% Edge Processing: Generates, stores, and verifies OTPs at Cloudflare Edge
 * - Automatic KV Storage: Uses `env.OTP_KV` (or in-memory cache fallback) with 300s TTL
 * - Cryptographic Verified Tickets: Issues signed HMAC-SHA256 tickets upon valid OTP
 * - Direct Resend Dispatch: Sends emails via Resend with dark-minimalist Cyberpunk HTML
 * - CORS enabled for all Spac2 origins
 * 
 * Cloudflare Environment Variables / Secrets (Dashboard -> Settings -> Variables):
 * - RESEND_API_KEY: (Secret) Your Resend API Key (re_...)
 * - RESEND_FROM_EMAIL: "Spac2 Security <no-reply@spac2.com>"
 * - WORKER_SECRET: "spac2_super_secure_secret_2026" (Secret used to sign tickets & protect webhooks)
 * - OTP_KV: (KV Namespace Binding, optional but recommended)
 */

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Spac2-Secret",
  "Content-Type": "application/json"
};

// In-memory fallback if KV binding is not attached
const _memoryCache = new Map();

function getCache(env) {
  if (env.OTP_KV) {
    return {
      async get(key) {
        const val = await env.OTP_KV.get(key);
        return val ? JSON.parse(val) : null;
      },
      async set(key, value, ttlSeconds) {
        await env.OTP_KV.put(key, JSON.stringify(value), { expirationTtl: ttlSeconds });
      },
      async del(key) {
        await env.OTP_KV.delete(key);
      }
    };
  }

  return {
    async get(key) {
      const item = _memoryCache.get(key);
      if (!item) return null;
      if (Date.now() > item.exp) {
        _memoryCache.delete(key);
        return null;
      }
      return item.data;
    },
    async set(key, value, ttlSeconds) {
      _memoryCache.set(key, {
        data: value,
        exp: Date.now() + (ttlSeconds * 1000)
      });
    },
    async del(key) {
      _memoryCache.delete(key);
    }
  };
}

// --- HMAC-SHA256 Ticket Signing & Verification ---
async function createHmacSignature(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const signature = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return btoa(String.fromCharCode(...new Uint8Array(signature)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function generateSignedTicket(secret, payload) {
  const payloadStr = JSON.stringify(payload);
  const base64Payload = btoa(payloadStr)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
  const sig = await createHmacSignature(secret, base64Payload);
  return `${base64Payload}.${sig}`;
}

// --- Minimalist Spac2 Email Template ---
function buildOtpHtml(title, preheader, greeting, description, otpCode, actionNote, expiresInMins = 5) {
  const formattedCode = (otpCode || "").toString().trim().split("").join(" ");

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${title}</title>
  <style>
    body, table, td, p, a, li, blockquote { -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }
    table, td { mso-table-lspace: 0pt; mso-table-rspace: 0pt; }
    img { -ms-interpolation-mode: bicubic; }
    body {
      margin: 0;
      padding: 0;
      background-color: #060709;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: #f1f5f9;
    }
    .wrapper {
      width: 100%;
      table-layout: fixed;
      background-color: #060709;
      padding: 40px 16px;
    }
    .card {
      max-width: 480px;
      margin: 0 auto;
      background: #0f1117;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 20px;
      overflow: hidden;
      box-shadow: 0 24px 48px rgba(0, 0, 0, 0.6);
    }
    .inner {
      padding: 38px 36px 32px;
      text-align: center;
    }
    .brand {
      font-size: 18px;
      font-weight: 800;
      letter-spacing: 3px;
      color: #38bdf8;
      text-transform: uppercase;
      margin-bottom: 28px;
      display: inline-block;
    }
    .brand-dot {
      color: #38bdf8;
      margin-right: 2px;
    }
    h1 {
      font-size: 22px;
      font-weight: 700;
      color: #ffffff;
      margin: 0 0 10px 0;
      letter-spacing: -0.3px;
    }
    .subtitle {
      font-size: 14px;
      line-height: 1.5;
      color: #94a3b8;
      margin: 0 0 28px 0;
    }
    .otp-capsule {
      background: #08090d;
      border: 1.5px solid rgba(56, 189, 248, 0.35);
      border-radius: 14px;
      padding: 22px 16px;
      margin: 0 auto 20px auto;
      box-shadow: 0 0 30px rgba(56, 189, 248, 0.08);
    }
    .otp-number {
      font-size: 36px;
      font-weight: 800;
      letter-spacing: 8px;
      color: #ffffff;
      font-family: 'JetBrains Mono', 'SF Mono', Consolas, 'Liberation Mono', Menlo, monospace;
      margin: 0;
      padding-left: 8px;
      text-shadow: 0 0 16px rgba(56, 189, 248, 0.35);
    }
    .meta-tag {
      font-size: 12px;
      color: #64748b;
      margin: 0 0 24px 0;
      letter-spacing: 0.3px;
    }
    .divider {
      height: 1px;
      background: rgba(255, 255, 255, 0.06);
      margin: 24px 0;
    }
    .footer-note {
      font-size: 12px;
      line-height: 1.6;
      color: #475569;
      margin: 0;
    }
    .copyright {
      font-size: 11px;
      color: #334155;
      margin-top: 20px;
      letter-spacing: 0.5px;
    }
  </style>
</head>
<body>
  <div style="display:none;font-size:1px;color:#060709;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">
    ${preheader}
  </div>
  <table class="wrapper" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td align="center">
        <div class="card">
          <div class="inner">
            <div class="brand"><span class="brand-dot">●</span> SPAC2</div>
            <h1>${greeting}</h1>
            <p class="subtitle">${description}</p>
            
            <div class="otp-capsule">
              <div class="otp-number">${formattedCode}</div>
            </div>
            
            <p class="meta-tag">Expires in <strong>${expiresInMins} minutes</strong> · One-time use</p>
            
            <div class="divider"></div>
            
            <p class="footer-note">${actionNote}</p>
            <p class="copyright">&copy; 2026 Spac2 · Automated Security Dispatch</p>
          </div>
        </div>
      </td>
    </tr>
  </table>
</body>
</html>`;
}

async function callResend(apiKey, payload) {
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  const resText = await res.text();
  let resJson = null;
  try {
    resJson = JSON.parse(resText);
  } catch (_) {}

  if (!res.ok) {
    throw new Error(resJson?.message || resJson?.detail || `Resend HTTP ${res.status}: ${resText}`);
  }

  return resJson;
}

export default {
  async fetch(request, env, ctx) {
    // 1. CORS Preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const cache = getCache(env);
    const secretKey = env.WORKER_SECRET || "spac2_super_secure_secret_2026";
    const defaultFrom = env.RESEND_FROM_EMAIL || "Spac2 Security <no-reply@spac2.com>";
    const resendApiKey = env.RESEND_API_KEY;

    // 2. Health check
    if (request.method === "GET" && (path === "/" || path === "/health")) {
      return new Response(
        JSON.stringify({
          status: "ok",
          service: "Spac2 Edge Email & Verification Worker",
          timestamp: new Date().toISOString()
        }),
        { status: 200, headers: CORS_HEADERS }
      );
    }

    // 3. Route: POST /send-otp (Browser or VPS requests OTP dispatch)
    if (request.method === "POST" && (path === "/send-otp" || path === "/api/send-otp")) {
      try {
        if (!resendApiKey) {
          return new Response(
            JSON.stringify({ success: false, error: "Server Configuration Error: RESEND_API_KEY is not set." }),
            { status: 500, headers: CORS_HEADERS }
          );
        }

        const body = await request.json();
        const email = (body.email || "").trim().toLowerCase();
        const type = (body.type || "verify").toLowerCase(); // "verify" or "reset"
        const name = body.name || (type === "verify" ? "Spac2 Explorer" : "Spac2 User");

        if (!email || !email.includes("@")) {
          return new Response(
            JSON.stringify({ success: false, error: "Please provide a valid email address." }),
            { status: 400, headers: CORS_HEADERS }
          );
        }

        const cacheKey = `otp:${type}:${email}`;

        // Anti-spam rate limit: Check if code was requested in the last 45 seconds
        const existing = await cache.get(cacheKey);
        if (existing && existing.sent_at) {
          const timeSince = (Date.now() - existing.sent_at) / 1000;
          if (timeSince < 45) {
            const waitSec = Math.ceil(45 - timeSince);
            return new Response(
              JSON.stringify({
                success: false,
                error: `Please wait ${waitSec}s before requesting a new code.`
              }),
              { status: 429, headers: CORS_HEADERS }
            );
          }
        }

        // Generate 6-digit cryptographic numeric OTP
        const randomArray = new Uint32Array(1);
        crypto.getRandomValues(randomArray);
        const otpCode = String(100000 + (randomArray[0] % 900000));

        // Store OTP in KV / Cache with 300s TTL (5 minutes)
        await cache.set(
          cacheKey,
          {
            code: otpCode,
            attempts: 0,
            sent_at: Date.now(),
            email: email,
            type: type
          },
          300
        );

        // Build Email
        let subject, title, preheader, greeting, description, actionNote;

        if (type === "reset") {
          subject = `[${otpCode}] Spac2 Password Reset Code`;
          title = `${otpCode} is your Spac2 password reset code`;
          preheader = `Reset code: ${otpCode}. Valid for 5 minutes.`;
          greeting = "Password Reset Request";
          description = `Hello <strong>${name}</strong>, enter the code below to reset your Spac2 account password.`;
          actionNote = "If you didn't request a password reset, you can safely ignore this email.";
        } else {
          subject = `[${otpCode}] Spac2 Account Verification Code`;
          title = `${otpCode} is your Spac2 verification code`;
          preheader = `Verification code: ${otpCode}. Valid for 5 minutes.`;
          greeting = "Verify Your Email Address";
          description = `Hi <strong>${name}</strong>, use the 6-digit code below to confirm your Spac2 account email.`;
          actionNote = "Enter this code on the Spac2 verification screen. It expires in 5 minutes.";
        }

        const html = buildOtpHtml(title, preheader, greeting, description, otpCode, actionNote, 5);
        const plainText = `Your Spac2 code is: ${otpCode}\nValid for 5 minutes.\nDo not share this code with anyone.`;

        const resendPayload = {
          from: body.from || defaultFrom,
          to: [email],
          subject: subject,
          html: html,
          text: plainText
        };

        const result = await callResend(resendApiKey, resendPayload);

        return new Response(
          JSON.stringify({
            success: true,
            id: result?.id,
            email: email,
            expires_in: 300,
            message: `Verification code sent to ${email}`
          }),
          { status: 200, headers: CORS_HEADERS }
        );
      } catch (err) {
        return new Response(
          JSON.stringify({ success: false, error: err.message || "Failed to dispatch email." }),
          { status: 502, headers: CORS_HEADERS }
        );
      }
    }

    // 4. Route: POST /verify-otp (Browser validates 6-digit OTP at Edge)
    if (request.method === "POST" && (path === "/verify-otp" || path === "/api/verify-otp")) {
      try {
        const body = await request.json();
        const email = (body.email || "").trim().toLowerCase();
        const codeInput = (body.otp || "").toString().trim();
        const type = (body.type || "verify").toLowerCase();

        if (!email || !codeInput) {
          return new Response(
            JSON.stringify({ success: false, error: "Email and OTP code are required." }),
            { status: 400, headers: CORS_HEADERS }
          );
        }

        const cacheKey = `otp:${type}:${email}`;
        const record = await cache.get(cacheKey);

        if (!record) {
          return new Response(
            JSON.stringify({
              success: false,
              error: "Verification code has expired or was not requested. Please request a new code."
            }),
            { status: 400, headers: CORS_HEADERS }
          );
        }

        // Anti-brute force: increment attempts
        record.attempts = (record.attempts || 0) + 1;

        if (record.attempts > 5) {
          await cache.del(cacheKey);
          return new Response(
            JSON.stringify({
              success: false,
              error: "Too many incorrect attempts. Please request a new code."
            }),
            { status: 400, headers: CORS_HEADERS }
          );
        }

        if (record.code !== codeInput) {
          const remaining = Math.max(0, 5 - record.attempts);
          // Save updated attempts count
          await cache.set(cacheKey, record, 300);
          return new Response(
            JSON.stringify({
              success: false,
              error: `Incorrect code. ${remaining} attempt(s) remaining.`
            }),
            { status: 400, headers: CORS_HEADERS }
          );
        }

        // Valid OTP! Remove from cache immediately (anti-replay)
        await cache.del(cacheKey);

        // Issue a cryptographically signed ticket valid for 15 minutes
        const ticketPayload = {
          email: email,
          type: type,
          verified_at: Date.now(),
          exp: Date.now() + (15 * 60 * 1000)
        };
        const resetTicket = await generateSignedTicket(secretKey, ticketPayload);

        return new Response(
          JSON.stringify({
            success: true,
            verified: true,
            email: email,
            type: type,
            ticket: resetTicket,
            message: "Code verified successfully."
          }),
          { status: 200, headers: CORS_HEADERS }
        );
      } catch (err) {
        return new Response(
          JSON.stringify({ success: false, error: err.message || "Verification failed." }),
          { status: 500, headers: CORS_HEADERS }
        );
      }
    }

    // 5. Route: POST /send (Generic Email Dispatch for VPS backend)
    if (request.method === "POST" && path === "/send") {
      try {
        if (!resendApiKey) {
          return new Response(
            JSON.stringify({ success: false, error: "RESEND_API_KEY is not set." }),
            { status: 500, headers: CORS_HEADERS }
          );
        }

        const body = await request.json();
        const to = body.to;
        const subject = body.subject;
        const html = body.html;
        const text = body.text;

        if (!to || !subject || (!html && !text)) {
          return new Response(
            JSON.stringify({ error: "Missing required fields: to, subject, and html/text are required." }),
            { status: 400, headers: CORS_HEADERS }
          );
        }

        const recipients = Array.isArray(to) ? to : [to];
        const resendPayload = {
          from: body.from || defaultFrom,
          to: recipients,
          subject: subject,
          html: html,
          text: text
        };

        const result = await callResend(resendApiKey, resendPayload);

        return new Response(
          JSON.stringify({ success: true, id: result?.id }),
          { status: 200, headers: CORS_HEADERS }
        );
      } catch (err) {
        return new Response(
          JSON.stringify({ success: false, error: err.message }),
          { status: 502, headers: CORS_HEADERS }
        );
      }
    }

    // Default 404
    return new Response(
      JSON.stringify({
        error: "Not Found",
        available_endpoints: ["GET /", "POST /send-otp", "POST /verify-otp", "POST /send"]
      }),
      { status: 404, headers: CORS_HEADERS }
    );
  }
};
