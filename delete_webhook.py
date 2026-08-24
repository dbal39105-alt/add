import requests
import os

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8777443945:AAGM_f-WtydhbzRDZ1bgMt1X6ATu_oushHs")
url = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True"

r = requests.get(url).json()
print("Delete Webhook Result:", r)
