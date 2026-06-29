# test_telegram.py
import requests
from config.settings import settings

url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
payload = {
    "chat_id": settings.telegram_chat_id,
    "text": "🤖 Le Directeur Macro-Automatisé est connecté. Tout fonctionne !",
}

r = requests.post(url, json=payload)
print("✅ Message envoyé !" if r.ok else f"❌ Erreur : {r.text}")