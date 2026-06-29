# src/core/scheduler.py
"""
L'automatisation : fait tourner le système tout seul à intervalles réguliers.
- Surveillance : lit l'actualité + fait débattre le Comité, plusieurs fois par jour.
- Bilan quotidien : envoie l'état du portefeuille chaque matin (+ vérifie les stops).

⚠️ Le terminal doit rester OUVERT pour que le planificateur tourne.
"""
import asyncio
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler

from src.core.pipeline import run_once
from src.portfolio.paper_portfolio import load_portfolio, snapshot_text, check_exits, save_portfolio
from src.communication.telegram_bot import send_text


def job_surveillance():
    """Un cycle complet : actualités → Comité → portefeuille → Telegram."""
    print(f"\n{'='*55}\n⏰ {datetime.now():%Y-%m-%d %H:%M} — Surveillance des marchés\n{'='*55}")
    try:
        run_once()
    except Exception as e:
        print(f"  ⚠️  Erreur pendant la surveillance (on continue) : {e}")


def job_bilan_quotidien():
    """Vérifie les stops/objectifs et envoie l'état du portefeuille."""
    print(f"\n📊 {datetime.now():%Y-%m-%d %H:%M} — Bilan quotidien")
    try:
        p = load_portfolio()
        alertes = check_exits(p)          # vend si un stop ou un objectif est touché
        save_portfolio(p)
        texte = "📊 BILAN QUOTIDIEN\n\n" + snapshot_text(p)
        if alertes:
            texte += "\n\n⚡ Mouvements :\n" + "\n".join(alertes)
        asyncio.run(send_text(texte))
        print("   Bilan envoyé.")
    except Exception as e:
        print(f"  ⚠️  Erreur pendant le bilan (on continue) : {e}")


if __name__ == "__main__":
    scheduler = BlockingScheduler()   # utilise l'heure locale de ton ordinateur

    # 🔧 RÉGLAGE : surveillance 3 fois par jour (9h, 13h, 17h). Modifie les heures ici.
    scheduler.add_job(job_surveillance, "cron", hour="9,13,17", minute=0)

    # Bilan du portefeuille chaque matin à 8h
    scheduler.add_job(job_bilan_quotidien, "cron", hour=8, minute=0)

    print("🤖 Le Directeur Macro est EN SERVICE.")
    print("   Surveillance : 9h, 13h, 17h  |  Bilan : 8h")
    print("   (Laisse ce terminal ouvert. Ctrl+C pour arrêter.)\n")

    # Un cycle immédiat au démarrage, pour vérifier que tout marche tout de suite
    job_surveillance()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n👋 Directeur Macro arrêté.")