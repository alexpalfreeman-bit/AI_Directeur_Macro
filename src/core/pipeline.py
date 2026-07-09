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
from src.portfolio.paper_portfolio import (
    record_decision, load_portfolio, snapshot_text, verifier_sorties,
    verrou_portefeuille, VerrouIndisponible,
)
from src.communication.telegram_bot import send_decision_et_portefeuille, send_text
from src.ingestion.news_client import fetch_headlines, is_macro_relevant, corroborer_actualites
from datetime import datetime, timezone
from src.memory.world_memory import enregistrer_evenement
from src.analytics.performance import snapshot_quotidien
import traceback

def executer_en_securite(nom_etape: str, fonction, *args, **kwargs):
    """
    Exécute une étape du pipeline en ISOLANT ses erreurs : si elle plante, on
    journalise la trace complète (pour débugger), on alerte sur Telegram, et on
    renvoie None pour laisser le RESTE du cycle continuer.
    """
    try:
        return fonction(*args, **kwargs)
    except Exception as e:
        print(f"❌ Étape « {nom_etape} » a échoué : {e}")
        print(traceback.format_exc())
        try:
            asyncio.run(send_text(f"⚠️ ERREUR pipeline — étape « {nom_etape} » :\n{e}"))
        except Exception as e_tel:
            print(f"   (Alerte Telegram impossible : {e_tel})")
        return None

def verifier_et_alerter_sorties() -> None:
    """Vérifie les sorties mécaniques et, s'il y en a, prévient sur Telegram."""
    sorties = verifier_sorties()
    if not sorties:
        return
    for ligne in sorties:
        print(ligne)
    texte = "🛡️ SORTIES AUTOMATIQUES (stop / invalidation / objectif) :\n\n" + "\n".join(sorties)
    texte += "\n\n" + snapshot_text(load_portfolio())
    asyncio.run(send_text(texte))

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
    # 🌍 Mémoire du monde : on journalise le thème du cycle (news OU screener).
    #    Ici (et pas dans le Macro) pour que l'agent Macro — qui a déjà tourné
    #    AVANT cet appel — ne voie QUE les thèmes des cycles PRÉCÉDENTS.
    enregistrer_evenement(theme=thesis.theme, tickers=thesis.candidate_tickers)
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
    journal_maj = record_decision(thesis, decision)
    for ligne in journal_maj:
        print(ligne)

    # 🔄 Si un arbitrage de capital a eu lieu, alerte Telegram dédiée
    rotations = [l for l in journal_maj if "🔄" in l or "réalloué" in l]
    if rotations:
        asyncio.run(send_text("🔄 ARBITRAGE DE CAPITAL\n\n" + "\n".join(rotations)))

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
    # 🔒 C3 — tout le cycle sous verrou distribué : deux crons concurrents ne s'écrasent plus.
    try:
        with verrou_portefeuille():
            _run_once_corps()
    except VerrouIndisponible as e:
        print(f"⏭️  Cycle run_news sauté — {e}")


def _run_once_corps() -> None:
    # 🛡️ Protection mécanique À CHAQUE cycle, en premier — avant toute autre chose.
    executer_en_securite("Vérification des sorties (stops)", verifier_et_alerter_sorties)
    # 📸 Photo quotidienne de l'équity (idempotent par date : le dernier cron du jour gagne).
    executer_en_securite("Snapshot performance", lambda: print(snapshot_quotidien()))
    """Cycle complet. Le soir (17h), on révise d'abord les positions ; puis on cherche des idées."""
    contexte = executer_en_securite("Lecture des actualités", construire_contexte_actu) or ""

    # 📋 Revue du portefeuille UNE fois par jour, sur le cycle du soir (21h UTC).
    #    Isolée : même si la lecture des actualités a échoué, le Gérant révise quand même.
    if datetime.now(timezone.utc).hour >= 20:
        executer_en_securite("Revue du Gérant", revue_gerant, contexte)

    if not contexte:
        print("   Aucune actualité macro significative — pas de nouvelle idée aujourd'hui.")
        return
    executer_en_securite("Comité (nouvelle idée)", lancer_comite, contexte)

def run_screener() -> None:
    # 🔒 C3 — même verrou distribué pour le cycle screener.
    try:
        with verrou_portefeuille():
            _run_screener_corps()
    except VerrouIndisponible as e:
        print(f"⏭️  Cycle screener sauté — {e}")


def _run_screener_corps() -> None:
    # 🛡️ Protection mécanique À CHAQUE cycle screener aussi.
    executer_en_securite("Vérification des sorties (stops)", verifier_et_alerter_sorties)
    # 📸 Même photo quotidienne ici (idempotent : capte le jour même sans news).
    executer_en_securite("Snapshot performance", lambda: print(snapshot_quotidien()))
    """Cycle BOTTOM-UP : screener → thèse → comité → portefeuille → Telegram."""
    from src.screener.screener_thesis import generer_these_screener

    print("\n🔍 === CYCLE SCREENER (bottom-up) ===")
    thesis = executer_en_securite("Génération de la thèse screener", generer_these_screener, top_n=5)
    if thesis is None:
        print("   Screener : aucune thèse exploitable ce cycle.")
        return
    print(f"     Thème : {thesis.theme[:70]}...")
    print(f"     Tickers : {thesis.candidate_tickers} | Confiance : {thesis.confidence}")
    executer_en_securite("Comité sur thèse screener", lancer_comite_sur_these, thesis)

if __name__ == "__main__":
    run_once()
    #run_screener()