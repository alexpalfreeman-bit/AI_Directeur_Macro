# src/core/pipeline.py
"""
L'orchestrateur : la boucle complète du Directeur Macro.
Actualités réelles → Comité (4 agents) → Portefeuille → Telegram.
C'est le point d'entrée de tout le système.
"""
import asyncio

from src.ingestion.news_client import fetch_headlines, is_macro_relevant
from src.agents.macro_agent import generate_thesis
from src.agents.quant_agent import validate_thesis
from src.agents.devils_advocate_agent import challenge_thesis
from src.agents.portfolio_manager_agent import make_decision
from src.portfolio.paper_portfolio import record_decision, load_portfolio, snapshot_text
from src.communication.telegram_bot import send_decision_et_portefeuille


def construire_contexte_actu(max_titres: int = 8) -> str:
    """Récupère les vraies actualités et garde celles qui sont macro-pertinentes."""
    print("📰 Lecture des actualités du jour...")
    titres = fetch_headlines()
    retenus = []
    for item in titres:
        if is_macro_relevant(item["title"]):
            retenus.append(item)
        if len(retenus) >= max_titres:
            break

    if not retenus:
        return ""  # rien de macro aujourd'hui

    print(f"   {len(retenus)} actualité(s) macro retenue(s).")
    return "\n".join(f"- {item['title']} (source: {item['source']})" for item in retenus)


def lancer_comite(contexte_actu: str) -> None:
    """Fait tourner le Comité complet sur un contexte d'actualités réelles."""
    print("\n🧠 [1/4] Agent Macro...")
    thesis = generate_thesis(contexte_actu)
    print(f"     Thème : {thesis.theme[:70]}...")
    print(f"     Tickers : {thesis.candidate_tickers} | Confiance : {thesis.confidence}")

    print("📊 [2/4] Agent Quant...")
    _, quant = validate_thesis(thesis)
    print(f"     Survivants : {quant.surviving_tickers}")

    print("😈 [3/4] Avocat du Diable...")
    risk = challenge_thesis(thesis, quant)
    print(f"     Sévérité : {risk.severity} | Survit : {risk.survives_scrutiny}")

    print("🏛️  [4/4] Directeur de Portefeuille...")
    decision = make_decision(thesis, quant, risk)
    print(f"     >>> ACTION : {decision.action.upper()} (confiance {decision.confidence})")

    # Mise à jour du portefeuille
    print("\n📂 Mise à jour du portefeuille...")
    for ligne in record_decision(thesis, decision):
        print(ligne)
    portefeuille = snapshot_text(load_portfolio())
    print("\n" + portefeuille)

    # Envoi sur Telegram (un seul envoi groupé)
    print("\n📲 Envoi sur Telegram...")
    asyncio.run(send_decision_et_portefeuille(thesis, decision, portefeuille))
    print("   Envoyé !")


def run_once() -> None:
    """Un cycle complet du système, sur les vraies actualités du moment."""
    contexte = construire_contexte_actu()
    if not contexte:
        print("   Aucune actualité macro significative pour l'instant. On attend.")
        return
    lancer_comite(contexte)


if __name__ == "__main__":
    run_once()