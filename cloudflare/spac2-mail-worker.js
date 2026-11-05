/**
 * Cloudflare Worker: Spac2 Outbound Email & OTP Dispatcher (via Resend)
 * 
 * Features:
 * - CORS support for browser and server requests
 * - POST /send-otp: Dispatches branded 6-digit OTP emails (Verify / Reset Password)
 * - POST /send: Generic email sending
 * - GET /: Health check
 * 
 * Cloudflare Environment Variables / Secrets (Set in Cloudflare Dashboard -> Settings -> Variables):
 * - RESEND_API_KEY: "re_7tM6wB6J_QEGAQrVLhCbSr6QoVPJV3DYS" (Required, Encrypted Secret)
 * - RESEND_FROM_EMAIL: "Spac2 Security <no-reply@spac2.com>" (Optional)
 * - WORKER_SECRET: "spac2_super_secure_secret_2026" (Optional, if set requires X-Spac2-Secret header)
 */

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Spac2-Secret",
  "Content-Type": "application/json"
};

function buildOtpHtml(title, preheader, greeting, description, otpCode, actionNote, expiresInMins = 5) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${title}</title>
  <style>
    body, table, td, a { -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }
    body {
      margin: 0;
      padding: 0;
      background-color: #0b0f19;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: #e2e8f0;
    }
    .email-container {
      max-width: 560px;
      margin: 30px auto;
      background: #111827;
      border: 1px solid #1f2937;
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
    }
    .header {
      background: linear-gradient(135deg, #1e1b4b 0%, #0f172a 100%);
      padding: 28px 32px;
      border-bottom: 1px solid #1f2937;
      text-align: center;
    }
    .logo {
      font-size: 26px;
      font-weight: 800;
      letter-spacing: 2px;
      color: #38bdf8;
      text-decoration: none;
      display: inline-block;
    }
    .content {
      padding: 36px 32px;
    }
    h1 {
      font-size: 22px;
      font-weight: 700;
      color: #f8fafc;
      margin-top: 0;
      margin-bottom: 12px;
    }
    p {
      font-size: 15px;
      line-height: 1.6;
      color: #94a3b8;
      margin: 0 0 18px 0;
    }
    .otp-box {
      background: #0f172a;
      border: 1.5px dashed #38bdf8;
      border-radius: 12px;
      padding: 22px;
      text-align: center;
      margin: 28px 0;
    }
    .otp-label {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 1.5px;
      color: #64748b;
      margin-bottom: 8px;
    }
    .otp-code {
      font-size: 34px;
      font-weight: 800;
      font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
      color: #38bdf8;
      letter-spacing: 10px;
      margin: 0;
      padding-left: 10px;
    }
    .warning {
      font-size: 13px;
      color: #f59e0b;
      background: rgba(245, 158, 11, 0.08);
      border-left: 3px solid #f59e0b;
      padding: 12px 14px;
      border-radius: 4px;
      margin-top: 24px;
    }
    .footer {
      background: #090d16;
      padding: 24px 32px;
      text-align: center;
      border-top: 1px solid #1f2937;
      font-size: 12px;
      color: #64748b;
    }
  </style>
</head>
<body>
  <div style="display:none;font-size:1px;color:#333;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">
    ${preheader}
  </div>
  <div class="email-container">
    <div class="header">
      <span class="logo">SPAC2</span>
    </div>
    <div class="content">
      <h1>${greeting}</h1>
      <p>${description}</p>
      
      <div class="otp-box">
        <div class="otp-label">Verification Code (Expires in ${expiresInMins} mins)</div>
        <div class="otp-code">${otpCode}</div>
      </div>
      
      <p>${actionNote}</p>
      
      <div class="warning">
        <strong>Security Notice:</strong> Spac2 will never ask for your verification code. Never share this code with anyone.
      </div>
    </div>
    <div class="footer">
      <p style="margin: 0 0 8px 0;">This email was sent automatically by Spac2 Security System.</p>
      <p style="margin: 0;">&copy; 2026 Spac2 Ecosystem. All rights reserved.</p>
    </div>
  </div>
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
    // 1. Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    // 2. Health check route
    if (request.method === "GET" && (path === "/" || path === "/health")) {
      return new Response(
        JSON.stringify({
          status: "ok",
          service: "Spac2 Cloudflare Mail Worker",
          timestamp: new Date().toISOString()
        }),
        { status: 200, headers: CORS_HEADERS }
      );
    }

    // 3. Security check (if WORKER_SECRET is defined in env)
    if (env.WORKER_SECRET) {
      const incomingSecret = request.headers.get("X-Spac2-Secret") || "";
      if (incomingSecret !== env.WORKER_SECRET) {
        return new Response(
          JSON.stringify({ error: "Unauthorized: Invalid or missing X-Spac2-Secret header" }),
          { status: 401, headers: CORS_HEADERS }
        );
      }
    }

    const resendApiKey = env.RESEND_API_KEY;
    if (!resendApiKey) {
      return new Response(
        JSON.stringify({ error: "Server Configuration Error: RESEND_API_KEY secret is not set in Cloudflare Worker environment." }),
        { status: 500, headers: CORS_HEADERS }
      );
    }

    const defaultFrom = env.RESEND_FROM_EMAIL || "Spac2 Security <no-reply@spac2.com>";

    // 4. Route: POST /send-otp
    if (request.method === "POST" && path === "/send-otp") {
      try {
        const body = await request.json();
        const email = (body.email || "").trim().lower();
        const otp = (body.otp || "").toString().trim();
        const type = body.type || "verify"; // "verify" or "reset"
        const name = body.name || (type === "verify" ? "Spac2 Explorer" : "Spac2 User");

        if (!email || !otp) {
          return new Response(
            JSON.stringify({ error: "Missing required fields: email and otp are required." }),
            { status: 400, headers: CORS_HEADERS }
          );
        }

        let subject = "";
        let title = "";
        let preheader = "";
        let greeting = "";
        let description = "";
        let actionNote = "";

        if (type === "reset") {
          subject = `[${otp}] Spac2 Password Reset Code`;
          title = `${otp} is your Spac2 password reset code`;
          preheader = `Reset code: ${otp}. Valid for 5 minutes.`;
          greeting = "Password Reset Request";
          description = `Hello <strong>${name}</strong>, we received a request to reset the password for your Spac2 account.`;
          actionNote = "If you made this request, enter this 6-digit code to set a new password. If you didn't request this, you can safely ignore this email.";
        } else {
          subject = `[${otp}] Spac2 Account Verification Code`;
          title = `${otp} is your Spac2 verification code`;
          preheader = `Verification code: ${otp}. Valid for 5 minutes.`;
          greeting = "Verify Your Email Address";
          description = `Hi <strong>${name}</strong>, welcome to Spac2! Use the verification code below to confirm and activate your account email.`;
          actionNote = "Enter this 6-digit code on the Spac2 verification screen. This code will expire in 5 minutes.";
        }

        const html = buildOtpHtml(title, preheader, greeting, description, otp, actionNote, 5);
        const plainText = `Your Spac2 code is: ${otp}\nValid for 5 minutes.\nDo not share this code with anyone.`;

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
            message: `OTP sent successfully to ${email}`
          }),
          { status: 200, headers: CORS_HEADERS }
        );
      } catch (err) {
        return new Response(
          JSON.stringify({ success: false, error: err.message || "Failed to send OTP via Resend" }),
          { status: 502, headers: CORS_HEADERS }
        );
      }
    }

    // 5. Route: POST /send (Generic Email)
    if (request.method === "POST" && path === "/send") {
      try {
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
      JSON.stringify({ error: "Not Found", available_endpoints: ["GET /", "POST /send-otp", "POST /send"] }),
      { status: 404, headers: CORS_HEADERS }
    );
  }
};

