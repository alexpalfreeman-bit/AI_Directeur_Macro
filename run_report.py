# run_report.py — point d'entrée du rapport de performance (cron hebdo optionnel).
# Prend le snapshot du jour (idempotent), imprime le rapport complet et l'envoie
# sur Telegram. Aucun chiffre ne vient d'un LLM : registre + snapshots + SPY ajusté.
import asyncio

from src.analytics.performance import snapshot_quotidien, rapport
from src.communication.telegram_bot import send_text

if __name__ == "__main__":
    print(snapshot_quotidien())
    texte = rapport()
    print("\n" + texte)
    asyncio.run(send_text(texte))