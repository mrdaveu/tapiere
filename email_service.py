"""
Email service using Resend API via httpx.
100 free emails/day on free tier.
"""
import os
import httpx

RESEND_API_KEY = os.environ.get('RESEND_API_KEY')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'TAPIERE <onboarding@resend.dev>')
BASE_URL = os.environ.get('BASE_URL', 'http://localhost:8000').rstrip('/')


def _send_email(to: str, subject: str, html: str):
    """Send email via Resend API using httpx."""
    if not RESEND_API_KEY:
        print(f"[Email] No API key - would send to {to}: {subject}")
        return False

    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": FROM_EMAIL,
                "to": [to],
                "subject": subject,
                "html": html,
            },
            timeout=10,
        )
        response.raise_for_status()
        print(f"[Email] Sent to {to}: {subject}")
        return True
    except Exception as e:
        print(f"[Email] Error sending to {to}: {e}")
        return False


def send_magic_link(email: str, token: str, link_type: str = 'login'):
    """Send magic link email."""
    link = f"{BASE_URL}/auth/verify?token={token}"

    if not RESEND_API_KEY:
        print(f"\n{'='*50}")
        print(f"[Email] Magic link ({link_type}) for {email}:")
        print(f"  {link}")
        print(f"{'='*50}\n")
        return

    if link_type == 'invite':
        subject = "You're in! Welcome to TAPIERE"
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 500px; margin: 0 auto; padding: 40px 20px; }}
        h1 {{ color: #1a1a1a; margin-bottom: 20px; }}
        .button {{ display: inline-block; background: #000; color: #fff; padding: 12px 24px; text-decoration: none; border-radius: 6px; margin: 20px 0; }}
        .footer {{ color: #666; font-size: 14px; margin-top: 40px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Welcome to TAPIERE!</h1>
        <p>Your invite request has been approved. Click the button below to get started:</p>
        <a href="{link}" class="button">Sign in to TAPIERE</a>
        <p class="footer">This link expires in 24 hours.<br>If you didn't request this, you can ignore this email.</p>
    </div>
</body>
</html>
        """
    else:
        subject = "Sign in to TAPIERE"
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 500px; margin: 0 auto; padding: 40px 20px; }}
        h1 {{ color: #1a1a1a; margin-bottom: 20px; }}
        .button {{ display: inline-block; background: #000; color: #fff; padding: 12px 24px; text-decoration: none; border-radius: 6px; margin: 20px 0; }}
        .footer {{ color: #666; font-size: 14px; margin-top: 40px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Sign in to TAPIERE</h1>
        <p>Click the button below to sign in:</p>
        <a href="{link}" class="button">Sign in</a>
        <p class="footer">This link expires in 24 hours.<br>If you didn't request this, you can ignore this email.</p>
    </div>
</body>
</html>
        """

    if not _send_email(email, subject, html):
        # Fallback to console
        print(f"[Email] Magic link: {link}")


def send_invite_confirmation(email: str):
    """Send confirmation that invite request was received."""
    if not RESEND_API_KEY:
        print(f"[Email] Invite confirmation for {email}: You're on the waitlist!")
        return

    _send_email(
        email,
        "TAPIERE - You're on the waitlist!",
        """
<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 500px; margin: 0 auto; padding: 40px 20px; }
        h1 { color: #1a1a1a; margin-bottom: 20px; }
        .footer { color: #666; font-size: 14px; margin-top: 40px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Thanks for your interest!</h1>
        <p>We've received your invite request for TAPIERE. We'll send you a link when you're approved.</p>
        <p class="footer">In the meantime, you can try out the demo on our landing page.</p>
    </div>
</body>
</html>
        """
    )
