# run_report.py — point d'entrée du rapport de performance (cron hebdo optionnel).
# Prend le snapshot du jour (idempotent), imprime le rapport complet et l'envoie
# sur Telegram. Aucun chiffre ne vient d'un LLM : registre + snapshots + SPY ajusté.
import asyncio

from src.analytics.performance import snapshot_quotidien, rapport
from src.analytics.calibration import rapport_calibration
from src.portfolio.paper_portfolio import load_portfolio
from src.communication.telegram_bot import send_text

if __name__ == "__main__":
    print(snapshot_quotidien())
    texte = rapport()
    # S10 — la calibration des convictions, en annexe du rapport de performance.
    try:
        p = load_portfolio()
        texte += "\n\n" + rapport_calibration([c.model_dump() for c in p.closed])
    except Exception as e:
        print(f"[calibration] indisponible : {e}")
    print("\n" + texte)
    asyncio.run(send_text(texte))