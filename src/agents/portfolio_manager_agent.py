# src/agents/portfolio_manager_agent.py
"""
Agent 4 : Le Directeur de Portefeuille.
Lit le débat complet (thèse + chiffres + démolition) et tranche.
Produit une décision finale, froide et exploitable, dimensionnée selon
le profil de risque ET le portefeuille déjà détenu, puis met à jour le
portefeuille en paper trading.
"""
from datetime import datetime
import anthropic
import asyncio
from config.settings import settings
from src.schemas.thesis import MacroThesis, QuantValidation, RiskAssessment
from src.schemas.decision import PortfolioDecision
from src.ingestion.market_client import get_fundamentals
from src.memory.vector_store import recall_similar, remember_decision
from src.analytics.calibration import texte_pour_directeur
from src.communication.telegram_bot import send_decision_et_portefeuille
from src.portfolio.paper_portfolio import record_decision, load_portfolio, snapshot_text
from src.ingestion.sentiment_client import get_market_regime, regime_text
import uuid
from src.agents.tool_helper import appel_avec_retry

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

PROFILS_RISQUE = {
    "prudent":  "Profil PRUDENT : risque max ~1% du capital par position, taille max ~5%, "
                "capital déployé total max ~50%.",
    "modere":   "Profil MODÉRÉ : risque max ~2% par position, taille max ~8%, déployé max ~70%.",
    "agressif": "Profil AGRESSIF assumé (investisseur jeune, horizon long, tolérance élevée). "
                "Risque jusqu'à ~4% du capital par position, taille jusqu'à ~15% sur FORTE "
                "conviction ET bon rapport rendement/risque. Capital déployé total jusqu'à ~90%. "
                "MAIS jamais plus de 15% sur un seul titre, et un rapport rendement/risque "
                "médiocre reste écarté même en agressif.",
}

SYSTEM_PROMPT = """Tu es le Directeur de Portefeuille. Froid, rationnel, responsable
du capital. Tu as lu trois contributions : la thèse (Macro), la validation chiffrée
(Quant), et la démolition (Avocat du Diable). Tu tranches maintenant.

Tes principes :

1. L'AVOCAT DU DIABLE A UN POIDS FORT. S'il a trouvé une faille FATALE ou jugé que
   la thèse ne survit pas, tu ne forces pas le trade. Le capital se protège d'abord.

2. TIMING RÉALISTE. Une thèse juste mais hors-saison ou prématurée va en WATCHLIST,
   pas en EXECUTE. "Bonne idée, mauvais moment" est une décision valide et sage.

3. DIMENSIONNEMENT PAR LA VOLATILITÉ, LA CONVICTION ET LE PROFIL DE RISQUE. Plus un
   titre est volatil ou la conviction faible (chaîne longue, confiance basse, dette
   élevée), plus la position est petite. Respecte les plafonds du PROFIL DE RISQUE
   fourni dans le message. Un mauvais rapport rendement/risque reste écarté.

4. NE GARDE QUE LES SURVIVANTS DU QUANT. Tu ne ressuscites pas un ticker rejeté.

5. STOP-LOSS MACRO. Définis la CONDITION qui invaliderait la thèse (ex: "désescalade
   à Ormuz" ou "bascule en risk-off généralisé"), pas seulement un niveau de prix.

6. APPRENDS DU PASSÉ. On te fournit tes décisions passées sur des situations
   similaires. Si tu as déjà sur-estimé une thèse cyclique comparable, sois plus
   prudent. La cohérence dans le temps prime sur l'enthousiasme du moment.

7. REGARDE TON PORTEFEUILLE AVANT D'AGIR. On te montre tes positions ouvertes et tes
   liquidités. NE PRENDS PAS une position qui CONTREDIT une thèse déjà en cours (ex:
   parier sur la hausse d'un actif dont tu détiens déjà le pari inverse). Ne
   surconcentre pas. Si une nouvelle actualité INVALIDE une position ouverte, tu peux
   recommander de la VENDRE plutôt que d'en ouvrir une opposée. Le portefeuille est un
   tout cohérent, pas une collection de paris isolés.

8. TIENS COMPTE DU RÉGIME DE MARCHÉ. On te fournit le régime actuel (RISK-ON / NEUTRE
   / RISK-OFF), basé sur le VIX, la courbe des taux et la tendance du S&P 500. En
   RISK-OFF, réduis fortement la taille des positions cycliques (matières premières,
   industriels, small caps) ou passe en WATCHLIST : un repli général peut les écraser
   malgré de bons fondamentaux. En RISK-ON, tu peux être plus offensif. Le bon trade
   au mauvais moment du cycle reste un mauvais trade.
PRIX D'INVALIDATION (le chiffre qui casse la thèse). Pour CHAQUE position que tu
EXÉCUTES, remplis aussi `invalidation_price` : le niveau de prix précis EN DESSOUS
duquel la thèse est prouvée fausse — pas un simple stop de risque, mais le point où
le marché te donne tort. Il se situe sous le prix d'entrée. Sers-toi des PRIX ACTUELS
fournis pour le calibrer. Si ce niveau est franchi, on sort sans discuter.

Sois concis et décisif. Tu n'écris pas un essai : tu donnes un ordre clair."""

def _regime_tag(regime) -> str:
    """Extrait une étiquette de régime propre (str) quel que soit le type renvoyé
    par get_market_regime(), pour l'enregistrer dans la mémoire RAG."""
    if isinstance(regime, str):
        return regime
    for attr in ("value", "regime", "label", "name"):
        v = getattr(regime, attr, None)
        if isinstance(v, str):
            return v
    if isinstance(regime, dict):
        for cle in ("regime", "label", "name", "value"):
            if isinstance(regime.get(cle), str):
                return regime[cle]
    return str(regime)

def make_decision(thesis: MacroThesis, quant: QuantValidation,
                  risk: RiskAssessment) -> PortfolioDecision:
    prix_actuels = "\n".join(
        f"- {tk} : prix actuel = {(get_fundamentals(tk) or {}).get('price')} $"
        for tk in quant.surviving_tickers
    )

    # ── Lecture du portefeuille actuel (pour la cohérence des positions) ──
    pf = load_portfolio()
    if pf.positions:
        positions_actuelles = "\n".join(
            f"- {p.ticker} : {p.shares} actions, entrée {p.entry_price}$, "
            f"stop {p.stop_loss}$, thèse liée {p.thesis_id[:8]}"
            for p in pf.positions
        )
    else:
        positions_actuelles = "Aucune position ouverte (portefeuille 100% liquide)."
    capital_dispo = f"{pf.cash:.0f}$ de liquidités sur {pf.starting_capital:.0f}$"
    regime = get_market_regime()
    regime_txt = regime_text(regime)

    now = datetime.now()

    # S10 — retour d'expérience CHIFFRÉ sur la calibration des convictions passées.
    #    Best-effort : une erreur ici ne doit jamais empêcher une décision d'être prise.
    try:
        calibration_txt = texte_pour_directeur([c.model_dump() for c in pf.closed])
    except Exception as e:
        calibration_txt = "CALIBRATION : indisponible ce cycle."
        print(f"[calibration] ⚠️ indisponible ({e}) — le comité continue.")

    passe = recall_similar(thesis)
    memoire_text = "Aucune décision passée comparable." if not passe else "\n".join(
        f"- [{m['meta']['action'].upper()}, conf {m['meta']['confidence']}] {m['summary'][:200]}"
        for m in passe
    )

    user_content = (
        f"DATE DU JOUR : {now.day}/{now.month}/{now.year} (Q{(now.month-1)//3+1}).\n\n"
        f"PROFIL DE RISQUE : {PROFILS_RISQUE[settings.risk_profile]}\n\n"
        f"PLAFOND DUR : aucune position ne peut dépasser {settings.max_position_pct}% "
        f"du capital. Toute demande au-dessus sera automatiquement écrêtée.\n\n"
        f"⚠️ PORTEFEUILLE ACTUEL (tiens-en compte !) :\n{positions_actuelles}\n"
        f"Liquidités disponibles : {capital_dispo}\n\n"
        f"{regime_txt}\n\n"
        f"PRIX ACTUELS DES SURVIVANTS :\n{prix_actuels}\n\n"
        f"{calibration_txt}\n\n"
        f"MÉMOIRE — décisions passées similaires :\n{memoire_text}\n\n"
        f"--- THÈSE (Macro) ---\n{thesis.model_dump_json(indent=2)}\n\n"
        f"--- VALIDATION (Quant) ---\n{quant.model_dump_json(indent=2)}\n\n"
        f"--- DÉMOLITION (Avocat du Diable) ---\n{risk.model_dump_json(indent=2)}\n\n"
        "Tranche maintenant via l'outil 'rendre_decision'. Ne retiens que des tickers "
        "présents dans les survivants du Quant. Remplis TOUS les champs requis (dont "
        "`action`, `confidence`, et `invalidation_price` pour CHAQUE position exécutée)."
    )

    # 🛡️ Sortie structurée + retry auto (comme Macro/Quant/Gérant/Avocat).
    #    On force thesis_id ET un decision_id neuf (on ne fait pas confiance au LLM pour l'id).
    decision = appel_avec_retry(
        client=client,
        model=settings.director_model,   # Opus ; passe à settings.llm_model si trop cher
        system=SYSTEM_PROMPT,
        user_content=user_content,
        tool_name="rendre_decision",
        schema=PortfolioDecision,
        max_tokens=1500,
        forcer_id={"thesis_id": thesis.thesis_id, "decision_id": str(uuid.uuid4())},
    )

    # On mémorise la décision AVANT de la renvoyer
    remember_decision(thesis, decision, regime_tag=_regime_tag(regime))
    return decision

if __name__ == "__main__":
    from src.agents.macro_agent import generate_thesis
    from src.agents.quant_agent import validate_thesis
    from src.agents.devils_advocate_agent import challenge_thesis

    scenario = (
        "La Banque du Japon a surpris en relevant ses taux ; le yen s'apprécie. "
        "Des tensions dans le détroit d'Ormuz perturbent le transport de produits "
        "chimiques et d'engrais vers l'Amérique du Nord."
    )

    print("\n🧠 [1/4] Agent Macro...")
    thesis = generate_thesis(scenario)
    print(f"     Tickers : {thesis.candidate_tickers} | Confiance : {thesis.confidence}")

    print("📊 [2/4] Agent Quant...")
    _, quant = validate_thesis(thesis)
    print(f"     Survivants : {quant.surviving_tickers}")

    print("😈 [3/4] Avocat du Diable...")
    risk = challenge_thesis(thesis, quant)
    print(f"     Sévérité : {risk.severity} | Survit : {risk.survives_scrutiny}")

    print("🏛️  [4/4] Directeur de Portefeuille : décision finale...\n")
    decision = make_decision(thesis, quant, risk)

    print("=" * 55)
    print("           DÉCISION FINALE DU COMITÉ")
    print("=" * 55)
    print(decision.model_dump_json(indent=2))
    print("\n>>> ACTION :", decision.action.upper())
    for p in decision.positions:
        print(f"    {p.ticker} : {p.position_size_pct}% du capital | "
              f"entrée={p.entry_price} | objectif={p.profit_target} | stop={p.stop_loss}")

    # ── Mise à jour du portefeuille (paper trading) ──
    print("\n📂 Mise à jour du portefeuille (paper trading)...")
    journal = record_decision(thesis, decision)
    for ligne in journal:
        print(ligne)

    portefeuille = snapshot_text(load_portfolio())
    print("\n" + portefeuille)

    # ── Un SEUL envoi Telegram (décision + portefeuille) ──
    print("\n📲 Envoi sur Telegram...")
    asyncio.run(send_decision_et_portefeuille(thesis, decision, portefeuille))
    print("   Envoyé !")