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
    """Generate a responsive, minimalist, modern HTML template for Spac2 security emails."""
    formatted_code = " ".join(list(str(otp_code).strip()))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    body, table, td, p, a, li, blockquote {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
    table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
    img {{ -ms-interpolation-mode: bicubic; }}
    body {{
      margin: 0;
      padding: 0;
      background-color: #060709;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: #f1f5f9;
    }}
    .wrapper {{
      width: 100%;
      table-layout: fixed;
      background-color: #060709;
      padding: 40px 16px;
    }}
    .card {{
      max-width: 480px;
      margin: 0 auto;
      background: #0f1117;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 20px;
      overflow: hidden;
      box-shadow: 0 24px 48px rgba(0, 0, 0, 0.6);
    }}
    .inner {{
      padding: 38px 36px 32px;
      text-align: center;
    }}
    .brand {{
      font-size: 18px;
      font-weight: 800;
      letter-spacing: 3px;
      color: #38bdf8;
      text-transform: uppercase;
      margin-bottom: 28px;
      display: inline-block;
    }}
    .brand-dot {{
      color: #38bdf8;
      margin-right: 2px;
    }}
    h1 {{
      font-size: 22px;
      font-weight: 700;
      color: #ffffff;
      margin: 0 0 10px 0;
      letter-spacing: -0.3px;
    }}
    .subtitle {{
      font-size: 14px;
      line-height: 1.5;
      color: #94a3b8;
      margin: 0 0 28px 0;
    }}
    .otp-capsule {{
      background: #08090d;
      border: 1.5px solid rgba(56, 189, 248, 0.35);
      border-radius: 14px;
      padding: 22px 16px;
      margin: 0 auto 20px auto;
      box-shadow: 0 0 30px rgba(56, 189, 248, 0.08);
    }}
    .otp-number {{
      font-size: 36px;
      font-weight: 800;
      letter-spacing: 8px;
      color: #ffffff;
      font-family: 'JetBrains Mono', 'SF Mono', Consolas, 'Liberation Mono', Menlo, monospace;
      margin: 0;
      padding-left: 8px;
      text-shadow: 0 0 16px rgba(56, 189, 248, 0.35);
    }}
    .meta-tag {{
      font-size: 12px;
      color: #64748b;
      margin: 0 0 24px 0;
      letter-spacing: 0.3px;
    }}
    .divider {{
      height: 1px;
      background: rgba(255, 255, 255, 0.06);
      margin: 24px 0;
    }}
    .footer-note {{
      font-size: 12px;
      line-height: 1.6;
      color: #475569;
      margin: 0;
    }}
    .copyright {{
      font-size: 11px;
      color: #334155;
      margin-top: 20px;
      letter-spacing: 0.5px;
    }}
  </style>
</head>
<body>
  <div style="display:none;font-size:1px;color:#060709;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">
    {preheader}
  </div>
  <table class="wrapper" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td align="center">
        <div class="card">
          <div class="inner">
            <div class="brand"><span class="brand-dot">●</span> SPAC2</div>
            <h1>{greeting}</h1>
            <p class="subtitle">{description}</p>
            
            <div class="otp-capsule">
              <div class="otp-number">{formatted_code}</div>
            </div>
            
            <p class="meta-tag">Expires in <strong>{expires_in_minutes} minutes</strong> · One-time use</p>
            
            <div class="divider"></div>
            
            <p class="footer-note">{action_note}</p>
            <p class="copyright">&copy; 2026 Spac2 · Automated Security Dispatch</p>
          </div>
        </div>
      </td>
    </tr>
  </table>
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
                    if not RESEND_API_KEY:
                        return {"success": False, "id": None, "error": f"Cloudflare Worker error ({res.status_code}): {res.text}"}
        except Exception as ex:
            print("[Email Service] Cloudflare Worker dispatch exception:", ex)
            if not RESEND_API_KEY:
                return {"success": False, "id": None, "error": f"Failed to reach Cloudflare Worker: {str(ex)}"}

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
                    if not RESEND_API_KEY:
                        return {"success": False, "id": None, "error": f"Cloudflare Worker error ({res.status_code}): {res.text}"}
        except Exception as ex:
            print("[Email Service] Cloudflare Worker dispatch exception:", ex)
            if not RESEND_API_KEY:
                return {"success": False, "id": None, "error": f"Failed to reach Cloudflare Worker: {str(ex)}"}

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

