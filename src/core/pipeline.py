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
from src.communication.telegram_bot import send_decision_et_portefeuille, send_text
from src.ingestion.news_client import fetch_headlines, is_macro_relevant, corroborer_actualites

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

    print(f"   {len(retenus)} actualité(s) macro retenue(s). Corroboration en cours...")
    contexte_corrobore = corroborer_actualites(retenus)

    # On donne au Macro à la fois la synthèse corroborée ET les titres détaillés
    titres_detail = "\n".join(f"- [{item['source']}] {item['title']}" for item in retenus)
    return (
        "SYNTHÈSE CORROBORÉE (nombre de sources par thème) :\n"
        f"{contexte_corrobore}\n\n"
        "TITRES DÉTAILLÉS :\n"
        f"{titres_detail}"
    )

def lancer_comite(contexte_actu: str) -> None:
    """Fait tourner le Comité complet sur un contexte d'actualités réelles."""
    print("\n🧠 [1/4] Agent Macro...")
    thesis = generate_thesis(contexte_actu)
    print(f"     Thème : {thesis.theme[:70]}...")
    print(f"     Tickers : {thesis.candidate_tickers} | Confiance : {thesis.confidence}")
    lancer_comite_sur_these(thesis)

def lancer_comite_sur_these(thesis) -> None:
    """Fait passer une thèse (news OU screener) par le comité complet."""
    print("📊 [2/4] Agent Quant...")
    _, quant = validate_thesis(thesis)
    print(f"     Survivants : {quant.surviving_tickers}")

    print("😈 [3/4] Avocat du Diable...")
    risk = challenge_thesis(thesis, quant)
    print(f"     Sévérité : {risk.severity} | Survit : {risk.survives_scrutiny}")

    print("🏛️  [4/4] Directeur de Portefeuille...")
    decision = make_decision(thesis, quant, risk)
    print(f"     >>> ACTION : {decision.action.upper()} (confiance {decision.confidence})")

    print("\n📂 Mise à jour du portefeuille...")
    for ligne in record_decision(thesis, decision):
        print(ligne)
    portefeuille = snapshot_text(load_portfolio())
    print("\n" + portefeuille)

    print("\n📲 Envoi sur Telegram...")
    asyncio.run(send_decision_et_portefeuille(thesis, decision, portefeuille))
    print("   Envoyé !")

def revue_gerant(contexte_actu: str = "") -> None:
    """
    Reconcile / Manage : le Gérant révise et gère les positions DÉJÀ ouvertes
    (garder / alléger / vendre) AVANT que le Comité cherche de nouvelles idées.
    Prévient sur Telegram s'il y a un mouvement (vente ou allègement).
    """
    from src.agents.gerant_agent import appliquer_revue   # import local (évite tout cycle)

    p = load_portfolio()
    if not p.positions:
        print("📋 Gérant : aucune position ouverte à gérer.")
        return

    print(f"\n📋 Gérant : revue de {len(p.positions)} position(s) ouverte(s)...")
    journal = appliquer_revue(contexte_actu)
    for ligne in journal:
        print(ligne)

    # Si le Gérant a VENDU ou ALLÉGÉ, on prévient sur Telegram (sinon on reste silencieux)
    mouvements = [l for l in journal if ("VENTE" in l or "ALLÈGE" in l)]
    if mouvements:
        texte = "📋 GÉRANT — mouvements sur le portefeuille :\n\n" + "\n".join(mouvements)
        texte += "\n\n" + snapshot_text(load_portfolio())
        asyncio.run(send_text(texte))
        print("   Mouvements du Gérant envoyés sur Telegram.")

def run_once() -> None:
    """Un cycle complet : gestion des positions ouvertes, puis recherche de nouvelles idées."""
    contexte = construire_contexte_actu()
    revue_gerant(contexte)          # ← 1) on gère d'abord ce qu'on détient (Reconcile/Manage)
    if not contexte:
        print("   Aucune actualité macro significative — pas de nouvelle idée aujourd'hui.")
        return
    lancer_comite(contexte)         # ← 2) puis on cherche de nouvelles idées

def run_screener() -> None:
    """Cycle BOTTOM-UP : screener → thèse → comité → portefeuille → Telegram."""
    from src.screener.screener_thesis import generer_these_screener

    print("\n🔍 === CYCLE SCREENER (bottom-up) ===")
    thesis = generer_these_screener(top_n=5)
    print(f"     Thème : {thesis.theme[:70]}...")
    print(f"     Tickers : {thesis.candidate_tickers} | Confiance : {thesis.confidence}")
    lancer_comite_sur_these(thesis)   # on réutilise le comité (voir étape 4)

if __name__ == "__main__":
    run_once()
    #run_screener()