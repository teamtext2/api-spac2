import os
import re
import httpx
from typing import Optional, Dict, Any
from config import RESEND_API_KEY, RESEND_FROM_EMAIL, CLOUDFLARE_MAIL_WORKER_URL, WORKER_SECRET_KEY


RESEND_API_ENDPOINT = "https://api.resend.com/emails"


def _build_otp_html(
    title: str,
    preheader: str,
    greeting: str,
    description: str,
    otp_code: str,
    action_note: str,
    expires_in_minutes: int = 5
) -> str:
    """Generate a responsive, modern HTML template for Spac2 security emails."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
    body {{
      margin: 0;
      padding: 0;
      background-color: #0b0f19;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: #e2e8f0;
    }}
    .email-container {{
      max-width: 560px;
      margin: 30px auto;
      background: #111827;
      border: 1px solid #1f2937;
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
    }}
    .header {{
      background: linear-gradient(135deg, #1e1b4b 0%, #0f172a 100%);
      padding: 28px 32px;
      border-bottom: 1px solid #1f2937;
      text-align: center;
    }}
    .logo {{
      font-size: 26px;
      font-weight: 800;
      letter-spacing: 2px;
      color: #38bdf8;
      text-decoration: none;
      display: inline-block;
    }}
    .content {{
      padding: 36px 32px;
    }}
    h1 {{
      font-size: 22px;
      font-weight: 700;
      color: #f8fafc;
      margin-top: 0;
      margin-bottom: 12px;
    }}
    p {{
      font-size: 15px;
      line-height: 1.6;
      color: #94a3b8;
      margin: 0 0 18px 0;
    }}
    .otp-box {{
      background: #0f172a;
      border: 1.5px dashed #38bdf8;
      border-radius: 12px;
      padding: 22px;
      text-align: center;
      margin: 28px 0;
    }}
    .otp-label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 1.5px;
      color: #64748b;
      margin-bottom: 8px;
    }}
    .otp-code {{
      font-size: 34px;
      font-weight: 800;
      font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
      color: #38bdf8;
      letter-spacing: 10px;
      margin: 0;
      padding-left: 10px;
    }}
    .warning {{
      font-size: 13px;
      color: #f59e0b;
      background: rgba(245, 158, 11, 0.08);
      border-left: 3px solid #f59e0b;
      padding: 12px 14px;
      border-radius: 4px;
      margin-top: 24px;
    }}
    .footer {{
      background: #090d16;
      padding: 24px 32px;
      text-align: center;
      border-top: 1px solid #1f2937;
      font-size: 12px;
      color: #64748b;
    }}
    .footer a {{
      color: #38bdf8;
      text-decoration: none;
    }}
  </style>
</head>
<body>
  <div style="display:none;font-size:1px;color:#333;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">
    {preheader}
  </div>
  <div class="email-container">
    <div class="header">
      <span class="logo">SPAC2</span>
    </div>
    <div class="content">
      <h1>{greeting}</h1>
      <p>{description}</p>
      
      <div class="otp-box">
        <div class="otp-label">Verification Code (Expires in {expires_in_minutes} mins)</div>
        <div class="otp-code">{otp_code}</div>
      </div>
      
      <p>{action_note}</p>
      
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
</html>"""


async def send_resend_email(
    to_email: str,
    subject: str,
    html_content: str,
    text_content: Optional[str] = None,
    from_email: Optional[str] = None
) -> Dict[str, Any]:
    """
    Send an email via Resend API using standard async httpx.
    Returns: {"success": bool, "id": str | None, "error": str | None}
    """
    clean_to = to_email.strip().lower()
    sender = from_email or RESEND_FROM_EMAIL

    if not RESEND_API_KEY:
        print("[Resend] Error: RESEND_API_KEY is not set.")
        return {"success": False, "error": "RESEND_API_KEY is missing."}

    payload: Dict[str, Any] = {
        "from": sender,
        "to": [clean_to],
        "subject": subject,
        "html": html_content
    }
    if text_content:
        payload["text"] = text_content

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(RESEND_API_ENDPOINT, json=payload, headers=headers)
            
            if response.status_code in (200, 201):
                data = response.json()
                email_id = data.get("id")
                print(f"[Resend] Successfully sent email to {clean_to} (ID: {email_id})")
                return {"success": True, "id": email_id, "error": None}
            else:
                err_body = response.text
                print(f"[Resend] Delivery failed (HTTP {response.status_code}): {err_body}")
                return {"success": False, "id": None, "error": f"HTTP {response.status_code}: {err_body}"}
    except Exception as ex:
        print(f"[Resend] Network exception while sending email: {ex}")
        return {"success": False, "id": None, "error": str(ex)}


async def send_verification_otp_email(to_email: str, otp_code: str, user_name: Optional[str] = None) -> Dict[str, Any]:
    """Send a 6-digit email verification OTP (via Cloudflare Worker or direct Resend API)."""
    name_display = user_name or "Spac2 Explorer"

    # 1. Try Cloudflare Worker relay if URL is configured
    if CLOUDFLARE_MAIL_WORKER_URL:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    f"{CLOUDFLARE_MAIL_WORKER_URL}/send-otp",
                    json={
                        "email": to_email,
                        "otp": otp_code,
                        "type": "verify",
                        "name": name_display
                    },
                    headers={
                        "Content-Type": "application/json",
                        "X-Spac2-Secret": WORKER_SECRET_KEY
                    }
                )
                if res.status_code == 200:
                    data = res.json()
                    print(f"[Email Service] Dispatched verify OTP via Cloudflare Worker to {to_email}")
                    return {"success": True, "id": data.get("id"), "error": None}
                else:
                    print(f"[Email Service] Cloudflare Worker returned {res.status_code}: {res.text}")
        except Exception as ex:
            print("[Email Service] Cloudflare Worker dispatch exception:", ex)

    # 2. Fallback to direct Resend API
    title = f"{otp_code} is your Spac2 verification code"
    preheader = f"Your verification code is {otp_code}. Valid for 5 minutes."
    greeting = "Verify Your Email Address"
    description = f"Hi <strong>{name_display}</strong>, welcome to Spac2! Use the verification code below to confirm and activate your account email."
    action_note = "Enter this 6-digit code on the Spac2 verification screen to complete the setup. This code will expire in 5 minutes."

    html = _build_otp_html(
        title=title,
        preheader=preheader,
        greeting=greeting,
        description=description,
        otp_code=otp_code,
        action_note=action_note,
        expires_in_minutes=5
    )
    plain_text = f"Your Spac2 verification code is: {otp_code}\nThis code will expire in 5 minutes.\nDo not share this code with anyone."

    return await send_resend_email(
        to_email=to_email,
        subject=f"[{otp_code}] Spac2 Account Verification Code",
        html_content=html,
        text_content=plain_text
    )


async def send_password_reset_otp_email(to_email: str, otp_code: str, user_name: Optional[str] = None) -> Dict[str, Any]:
    """Send a 6-digit password reset OTP (via Cloudflare Worker or direct Resend API)."""
    name_display = user_name or "Spac2 User"

    # 1. Try Cloudflare Worker relay if URL is configured
    if CLOUDFLARE_MAIL_WORKER_URL:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    f"{CLOUDFLARE_MAIL_WORKER_URL}/send-otp",
                    json={
                        "email": to_email,
                        "otp": otp_code,
                        "type": "reset",
                        "name": name_display
                    },
                    headers={
                        "Content-Type": "application/json",
                        "X-Spac2-Secret": WORKER_SECRET_KEY
                    }
                )
                if res.status_code == 200:
                    data = res.json()
                    print(f"[Email Service] Dispatched reset OTP via Cloudflare Worker to {to_email}")
                    return {"success": True, "id": data.get("id"), "error": None}
                else:
                    print(f"[Email Service] Cloudflare Worker returned {res.status_code}: {res.text}")
        except Exception as ex:
            print("[Email Service] Cloudflare Worker dispatch exception:", ex)

    # 2. Fallback to direct Resend API
    title = f"{otp_code} is your Spac2 password reset code"
    preheader = f"Reset code: {otp_code}. Valid for 5 minutes."
    greeting = "Password Reset Request"
    description = f"Hello <strong>{name_display}</strong>, we received a request to reset the password for your Spac2 account."
    action_note = "If you made this request, enter the 6-digit code below to set a new password. If you didn't request a password reset, you can safely ignore this email."

    html = _build_otp_html(
        title=title,
        preheader=preheader,
        greeting=greeting,
        description=description,
        otp_code=otp_code,
        action_note=action_note,
        expires_in_minutes=5
    )
    plain_text = f"Your Spac2 password reset code is: {otp_code}\nThis code will expire in 5 minutes.\nIf you did not request this, please ignore."

    return await send_resend_email(
        to_email=to_email,
        subject=f"[{otp_code}] Spac2 Password Reset Code",
        html_content=html,
        text_content=plain_text
    )

