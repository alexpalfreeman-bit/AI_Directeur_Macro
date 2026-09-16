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
                "Le système borne mécaniquement la perte à 3% de l'équity par position (stop "
                "élargi à 2×ATR, taille ajustée). Tailles attendues : 6-9% (conviction 0,50-0,60), "
                "9-12% (0,65-0,75), 12-15% (0,80+). Jamais plus de 15% sur un seul titre. "
                "Un rapport rendement/risque médiocre reste écarté même en agressif.",
}

SYSTEM_PROMPT = """Tu es le Directeur de Portefeuille d'un fonds à posture AGRESSIVE et
horizon long. Tu as lu trois contributions : la thèse (Macro), la validation chiffrée
(Quant), et la démolition (Avocat du Diable). Tu tranches maintenant.

RÈGLE ZÉRO — TU DÉCIDES, TU N'OBSERVES PAS.
Tu as deux issues normales : EXECUTE ou REJECT. WATCHLIST est une EXCEPTION, réservée à
deux cas précis : (a) une thèse juste dont le catalyseur est DATÉ dans le futur (résultats,
décision de banque centrale, vote) et qu'il faut attendre ; (b) une faille SÉRIEUSE mais
non fatale de l'Avocat, qui exige une confirmation identifiable. Si tu ne peux pas nommer
la date ou la confirmation que tu attends, WATCHLIST est interdit : c'est EXECUTE ou REJECT.
Un fonds qui observe deux fois plus qu'il n'agit ne gère pas le risque, il gère son
inconfort.

GRILLE DE CONVICTION — utilise TOUTE l'échelle, chaque cran a des critères :
  0,30-0,45  Chaîne causale longue (≥ 3 maillons), catalyseur non daté ou non vérifié,
             OU faille sérieuse de l'Avocat. → WATCHLIST justifié ou REJECT.
             Tu N'EXÉCUTES PAS à ce niveau : exécuter une thèse à laquelle tu ne crois
             pas est une contradiction, pas de la prudence.
  0,50-0,60  Chaîne courte (≤ 2 maillons), catalyseur daté et vérifié, ≥ 2 survivants
             Quant, Avocat au plus MODÉRÉ. → EXECUTE, taille 6-9 %.
  0,65-0,75  Ce qui précède + corroboration par ≥ 2 sources indépendantes + régime de
             marché favorable OU précédent gagnant comparable en mémoire. → EXECUTE, 9-12 %.
  0,80-0,90  Ce qui précède + rapport rendement/risque ≥ 2:1 CALCULÉ avec les prix fournis
             (distance à l'objectif / distance à l'invalidation). → EXECUTE, 12-15 %.
Une conviction ne descend pas parce que « le marché est incertain » : le marché est
toujours incertain. Elle descend parce qu'un critère ci-dessus manque. Nomme-le.

DÉPLOIEMENT. On te donne le pourcentage du capital investi et la cible. SOUS LA CIBLE,
une thèse qui survit à l'Avocat et compte des survivants Quant doit être EXÉCUTÉE — le
capital qui dort ne produit rien et c'est TON échec, pas une sécurité. Au-dessus de la
cible, tu peux être sélectif et exiger 0,65+.

Tes principes de discipline, inchangés :

1. L'AVOCAT DU DIABLE A UN POIDS FORT. Faille FATALE ou thèse qui ne survit pas → REJECT,
   sans hésiter. Faille SÉRIEUSE → conviction ≤ 0,45. Faille MINEURE → elle ne te freine pas.

2. NE GARDE QUE LES SURVIVANTS DU QUANT. Tu ne ressuscites pas un ticker rejeté.

3. STOP MACRO. Définis la CONDITION qui invaliderait la thèse (« désescalade à Ormuz »,
   « bascule en risk-off »), pas seulement un niveau de prix.

4. APPRENDS DU PASSÉ. On te fournit ta calibration chiffrée et tes décisions similaires.
   Si tes convictions hautes ont perdu, exige plus ; si tes 0,40 ont gagné +17 %, tu
   sous-estimais tes bonnes idées — corrige-le.

5. REGARDE TON PORTEFEUILLE. Ne prends pas une position qui CONTREDIT une thèse en cours.
   Ne surconcentre pas : si le portefeuille est déjà lourd sur un secteur ou un thème
   (ex. crédit américain via banques + fintech + collecteurs de dettes = UN seul pari),
   une nouvelle thèse sur ce thème exige 0,65+ ou REJECT.

6. RÉGIME DE MARCHÉ. En RISK-OFF, réduis d'un cran la conviction des cycliques. En
   RISK-ON, tu peux viser 0,65+ plus souvent.

7. STOPS. Ton stop sera automatiquement repoussé à au moins 2×ATR par le système : ne
   mets pas un stop serré en croyant réduire le risque, il serait touché par le bruit.
   Place-le là où la thèse est FAUSSE, et laisse la taille absorber le risque.

PRIX D'INVALIDATION. Pour CHAQUE position exécutée, `invalidation_price` = le niveau
sous lequel la thèse est prouvée fausse, calibré sur les PRIX ACTUELS fournis, sous le
prix d'entrée.

Sois concis et décisif. Tu donnes un ordre, pas un essai."""

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

    # V2 — état du déploiement, pour que le Directeur sache s'il doit pousser ou trier.
    try:
        from src.portfolio.paper_portfolio import deploiement_pct
        _dep = deploiement_pct(pf)
        _cible = getattr(settings, "deploiement_cible_pct", 0.0)
        if _cible:
            statut = "SOUS LA CIBLE → pousse le déploiement" if _dep < _cible else "au-dessus de la cible → sois sélectif"
            deploiement_txt = f"DÉPLOIEMENT : {_dep:.0f}% du capital investi, cible {_cible:.0f}% ({statut})."
        else:
            deploiement_txt = f"DÉPLOIEMENT : {_dep:.0f}% du capital investi."
    except Exception:
        deploiement_txt = ""

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
        f"Liquidités disponibles : {capital_dispo}\n"
        f"{deploiement_txt}\n\n"
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