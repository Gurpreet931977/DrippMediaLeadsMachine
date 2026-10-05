"""
Hostinger Email Connection Verifier
====================================
Tests authentication and connectivity with Hostinger SMTP without sending emails.

Usage:
  .venv/bin/python scripts/test_hostinger_connection.py
"""

import os
import sys
import smtplib
import ssl
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

def test_connection():
    host = os.getenv("SMTP_HOST", "smtp.hostinger.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    from_addr = os.getenv("EMAIL_FROM", user)

    print("=" * 60)
    print("HOSTINGER SMTP CREDENTIALS TEST")
    print("=" * 60)
    print(f"Host:      {host}")
    print(f"Port:      {port}")
    print(f"User:      {user}")
    print(f"From:      {from_addr}")
    print(f"Password:  {'*' * len(password) if password else '[NOT SET]'}")
    print("-" * 60)

    if not user or not password:
        print("❌ ERROR: SMTP_USER or SMTP_PASSWORD is not set in .env")
        print("Please update your .env with:")
        print("  SMTP_HOST=smtp.hostinger.com")
        print("  SMTP_PORT=465")
        print("  SMTP_USER=gurpreet@drippmedia.com")
        print("  SMTP_PASSWORD=<your_hostinger_mailbox_password>")
        print("  EMAIL_FROM=Gurpreet - Dripp Media <gurpreet@drippmedia.com>")
        return False

    context = ssl.create_default_context()
    try:
        if port == 465:
            print("Connecting via direct SSL (port 465)...")
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as server:
                server.login(user, password)
                print("✅ SUCCESS: Successfully authenticated with Hostinger SMTP server!")
                return True
        else:
            print(f"Connecting via STARTTLS (port {port})...")
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls(context=context)
                server.login(user, password)
                print("✅ SUCCESS: Successfully authenticated with Hostinger SMTP server!")
                return True
    except smtplib.SMTPAuthenticationError as e:
        print(f"❌ AUTHENTICATION FAILED: {e}")
        print("Please check that your password in .env matches your Hostinger email account password.")
        return False
    except Exception as e:
        print(f"❌ CONNECTION ERROR: {e}")
        return False

if __name__ == "__main__":
    success = test_connection()
    sys.exit(0 if success else 1)
