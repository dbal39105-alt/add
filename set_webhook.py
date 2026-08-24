"""
One-time Webhook Setup Script
==============================
Run this after deploying to Vercel to register
the webhook URL with Telegram.

Usage:
    python set_webhook.py <your-vercel-url>

Example:
    python set_webhook.py https://your-app.vercel.app
"""

import sys
import requests

import os
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8777443945:AAGM_f-WtydhbzRDZ1bgMt1X6ATu_oushHs")

def set_webhook(base_url):
    webhook_url = f"{base_url}/api/webhook"
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"

    print(f"Setting webhook to: {webhook_url}")

    response = requests.post(api_url, json={
        'url': webhook_url,
        'allowed_updates': ['message', 'callback_query'],
        'drop_pending_updates': True
    })

    result = response.json()
    if result.get('ok'):
        print(f"[SUCCESS] Webhook set successfully!")
        print(f"   URL: {webhook_url}")
    else:
        print(f"[FAILED]: {result}")

    # Verify
    info = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo").json()
    print(f"\nWebhook Info:")
    print(f"  URL: {info['result'].get('url', 'Not set')}")
    print(f"  Pending updates: {info['result'].get('pending_update_count', 0)}")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python set_webhook.py <vercel-url>")
        print("Example: python set_webhook.py https://your-app.vercel.app")
        sys.exit(1)
    set_webhook(sys.argv[1].rstrip('/'))
