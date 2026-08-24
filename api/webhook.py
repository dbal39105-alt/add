"""
Vercel Serverless Webhook Handler for Telegram Bot
===================================================
This replaces the polling-based main() loop.
Telegram sends updates via POST to /api/webhook.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

# Add parent directory to path so we can import bot module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ad import process_webhook_update, init_bot

# Initialize bot on cold start
init_bot()


class handler(BaseHTTPRequestHandler):
    """Vercel serverless function handler."""

    def do_POST(self):
        """Handle incoming Telegram webhook updates."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            update = json.loads(body.decode('utf-8'))

            # Process the update
            process_webhook_update(update)

            # Return 200 OK immediately
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True}).encode())

        except Exception as e:
            print(f"Webhook error: {e}")
            self.send_response(200)  # Still return 200 to prevent Telegram retries
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True, 'error': str(e)}).encode())

    def do_GET(self):
        """Health check + UIDAI connectivity test."""
        import requests, uuid, base64
        results = {'status': 'online', 'bot': 'RAHUL AADHAAR BOT'}

        # Test UIDAI captcha endpoint via bot's UIDAI session
        try:
            from ad import get_uidai_session
            s = get_uidai_session()
            txn = str(uuid.uuid4())
            hdrs = {
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'Origin': 'https://myaadhaar.uidai.gov.in',
                'Referer': 'https://myaadhaar.uidai.gov.in/',
                'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
                'appid': 'MYAADHAAR',
                'x-request-id': txn,
                'transactionId': txn,
            }
            r = s.post(
                'https://tathya.uidai.gov.in/audioCaptchaService/api/captcha/v3/generation',
                json={'captchaLength': '6', 'captchaType': '2', 'audioCaptchaRequired': True},
                headers=hdrs, timeout=12
            )
            j = r.json()
            results['uidai_status'] = r.status_code
            results['uidai_keys'] = list(j.keys())
            results['uidai_image_len'] = len(j.get('imageBase64', ''))
            results['uidai_txn'] = j.get('transactionId', '')
            results['uidai_ok'] = r.status_code == 200 and len(j.get('imageBase64', '')) > 100
        except Exception as e:
            results['uidai_error'] = str(e)
            results['uidai_error_type'] = type(e).__name__

        # Get Vercel server IP
        try:
            ip_r = requests.get('https://api.ipify.org?format=json', timeout=5)
            results['server_ip'] = ip_r.json().get('ip')
        except Exception as e:
            results['server_ip_error'] = str(e)

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(results, indent=2).encode())

    def log_message(self, format, *args):
        """Suppress default logging to avoid clutter in Vercel logs."""
        pass
