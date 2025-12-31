"""
Email service using Resend API.
100 free emails/day on free tier.
"""
import os

# Try to import resend, but make it optional for development
try:
    import resend
    RESEND_AVAILABLE = True
except ImportError:
    RESEND_AVAILABLE = False
    print("[Email] resend package not installed - emails will be logged to console")

RESEND_API_KEY = os.environ.get('RESEND_API_KEY')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'TAPIERE <onboarding@resend.dev>')  # Use resend.dev for testing
BASE_URL = os.environ.get('BASE_URL', 'http://localhost:8000').rstrip('/')


def send_magic_link(email: str, token: str, link_type: str = 'login'):
    """Send magic link email."""
    link = f"{BASE_URL}/auth/verify?token={token}"

    if not RESEND_API_KEY or not RESEND_AVAILABLE:
        print(f"\n{'='*50}")
        print(f"[Email] Magic link ({link_type}) for {email}:")
        print(f"  {link}")
        print(f"{'='*50}\n")
        return

    resend.api_key = RESEND_API_KEY

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

    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": email,
            "subject": subject,
            "html": html
        })
        print(f"[Email] Sent {link_type} email to {email}")
    except Exception as e:
        print(f"[Email] Error sending to {email}: {e}")
        # Fallback to console
        print(f"[Email] Magic link: {link}")


def send_invite_confirmation(email: str):
    """Send confirmation that invite request was received."""
    if not RESEND_API_KEY or not RESEND_AVAILABLE:
        print(f"[Email] Invite confirmation for {email}: You're on the waitlist!")
        return

    resend.api_key = RESEND_API_KEY

    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": email,
            "subject": "TAPIERE - You're on the waitlist!",
            "html": """
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
        })
        print(f"[Email] Sent invite confirmation to {email}")
    except Exception as e:
        print(f"[Email] Error sending to {email}: {e}")
