# src/agents/gerant_agent.py
"""
Agent Gérant : la gestion active des positions déjà ouvertes.
Pour chaque position, il relit la thèse d'origine, récupère les CHIFFRES
RÉELS actuels, rend un verdict GARDER / ALLÉGER / VENDRE, puis L'APPLIQUE
au portefeuille. Le LLM juge ; les nombres viennent de l'API.
"""
import anthropic
from config.settings import settings
from src.agents.tool_helper import appel_avec_retry
from src.ingestion.market_client import get_fundamentals
from src.portfolio.paper_portfolio import (
    allegement_autorise,
    load_portfolio, save_portfolio, close_position, trim_position,
    snapshot_text, Position,
)
from src.schemas.revue import RevuePosition, RevuePortefeuille

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

SYSTEM_PROMPT = """Tu es le Gérant de portefeuille. Ton rôle n'est PAS de trouver de
nouvelles idées : c'est de surveiller une position DÉJÀ OUVERTE et de décider quoi en
faire, froidement, à partir des chiffres réels.

Trois verdicts possibles :
- GARDER  : la thèse d'origine tient toujours. On laisse courir. Ne coupe pas un gagnant
            juste parce qu'il monte — tant que la thèse est valide, on garde.
- ALLÉGER : la thèse est encore valable mais le risque a monté ou la conviction a baissé
            (position devenue trop grosse, fondamentaux qui se dégradent, doute partiel).
            On réduit sans solder.
- VENDRE  : la thèse est CASSÉE ou INVALIDÉE (un fait nouveau la contredit, le niveau qui
            devait l'invalider est franchi, ou les fondamentaux se sont effondrés). On
            solde, sans espérer un retour.

Principes non négociables :
- Tu juges UNIQUEMENT à partir des chiffres réels et de la thèse fournis. Tu n'inventes
  aucun nombre. Si une donnée manque, dis-le, ne devine pas.
- Ne moyenne JAMAIS à la baisse. Une position qui perd ET dont la thèse est cassée se
  VEND, elle ne se renforce pas par espoir.
- Une perte latente seule ne justifie pas de vendre si la thèse tient encore ; un gain
  latent seul ne justifie pas de vendre si la thèse a encore du chemin.
- Sois décisif et concis : un verdict clair, une raison courte.

CONTRAINTES MÉCANIQUES (le système les applique après toi — n'y perds pas de verdicts) :
- ALLÉGER est REFUSÉ si la position a moins de 10 jours, si le lot vendu ferait moins de
  300 $, ou si elle a déjà été allégée 2 fois. Dans ces cas, ton choix réel est GARDER
  ou VENDRE. Un ALLÉGER qui sera refusé est un verdict perdu.
- Les stops et objectifs sont surveillés automatiquement chaque jour : tu n'as PAS à
  vendre « parce que le stop approche ». Tu vends parce que la THÈSE est cassée.

Tu vois le portefeuille ENTIER. Profites-en : si deux positions portent le même pari
(ex. plusieurs banques régionales), juge-les ensemble — c'est la concentration qu'on
gère, pas seulement chaque ligne. Rends un verdict pour CHAQUE ticker listé ; un ticker
absent de ta réponse sera traité comme GARDER.
"""

EMOJI = {"garder": "🟢", "alleger": "🟡", "vendre": "🔴"}


def revoir_position(pos: Position, contexte_actu: str = "") -> tuple[RevuePosition, dict]:
    """Relit UNE position à la lumière des chiffres réels actuels et rend un verdict."""
    data = get_fundamentals(pos.ticker)
    price = data.get("price")
    pnl_pct = round((price / pos.entry_price - 1) * 100, 1) if price else None

    resume = pos.thesis_summary or "(résumé de thèse non disponible pour cette position)"
    bloc_actu = f"\nACTUALITÉ RÉCENTE À PRENDRE EN COMPTE :\n{contexte_actu}\n" if contexte_actu else ""

    user_content = (
        f"POSITION À RÉVISER : {pos.ticker}\n"
        f"Thèse d'origine (pourquoi on l'a achetée) : {resume}\n"
        f"Prix d'entrée : {pos.entry_price}$ | Stop initial : {pos.stop_loss}$ | "
        f"Objectif : {pos.profit_target}$\n"
        f"PRIX D'INVALIDATION de la thèse : {pos.invalidation_price}$ "
        f"(sous ce niveau, la thèse est cassée → VENDRE)\n\n"
        f"CHIFFRES RÉELS ACTUELS (source yfinance — n'utilise QUE ceux-ci) :\n"
        f"- Prix actuel : {price}$  (P&L latent : {pnl_pct}%)\n"
        f"- PE : {data.get('pe_ratio')} | EV/EBITDA : {data.get('ev_to_ebitda')} | "
        f"P/B : {data.get('price_to_book')}\n"
        f"- Dette/capitaux : {data.get('debt_to_equity')} | "
        f"Volatilité 30j : {data.get('volatility_30d_pct')}%\n"
        f"{bloc_actu}\n"
        f"La thèse tient-elle toujours ? Rends ton verdict via 'rendre_revue' "
        f"(GARDER / ALLÉGER / VENDRE), avec une conviction restante (0 à 1) et une raison courte."
    )

    verdict = appel_avec_retry(
        client=client,
        model=settings.llm_model,
        system=SYSTEM_PROMPT,
        user_content=user_content,
        tool_name="rendre_revue",
        schema=RevuePosition,
        max_tokens=700,
        forcer_id={"ticker": pos.ticker},
    )
    return verdict, data


def revoir_portefeuille(positions: list[Position], contexte_actu: str = "") -> tuple[dict, dict]:
    """
    G1 — Révise TOUTES les positions en UN SEUL appel LLM.

    Renvoie (verdicts_par_ticker, donnees_par_ticker). Un ticker absent de la réponse
    reçoit GARDER (le choix sûr). Un ticker inconnu renvoyé par le LLM est ignoré.
    """
    donnees: dict[str, dict] = {}
    blocs: list[str] = []
    for i, pos in enumerate(positions, 1):
        data = get_fundamentals(pos.ticker) or {}
        donnees[pos.ticker.upper()] = data
        price = data.get("price")
        pnl_pct = round((price / pos.entry_price - 1) * 100, 1) if price else None
        resume = (pos.thesis_summary or "(résumé non disponible)")[:260]
        blocs.append(
            f"[{i}] {pos.ticker} — secteur {pos.sector or 'inconnu'} — détenue {_age_jours(pos)} j "
            f"— déjà allégée {getattr(pos, 'n_allegements', 0)}×\n"
            f"    Thèse : {resume}\n"
            f"    Entrée {pos.entry_price}$ | Stop {pos.stop_loss}$ | Objectif {pos.profit_target}$ | "
            f"Invalidation {pos.invalidation_price}$\n"
            f"    ACTUEL : {price}$ (P&L latent {pnl_pct}%) | PE {data.get('pe_ratio')} | "
            f"EV/EBITDA {data.get('ev_to_ebitda')} | Dette/cap {data.get('debt_to_equity')} | "
            f"Vol 30j {data.get('volatility_30d_pct')}%"
        )
    bloc_actu = f"\nACTUALITÉ RÉCENTE :\n{contexte_actu}\n" if contexte_actu else ""
    user_content = (
        f"PORTEFEUILLE À RÉVISER — {len(positions)} position(s). Chiffres réels (yfinance) ; "
        f"n'utilise QUE ceux-ci.\n\n" + "\n\n".join(blocs) + "\n" + bloc_actu +
        "\nRends un verdict (GARDER / ALLÉGER / VENDRE) pour CHAQUE ticker via "
        "'rendre_revue_portefeuille', avec une conviction restante (0 à 1) et une raison courte."
    )
    # Budget de sortie : ~150 tokens par verdict, borné (S6 double automatiquement si tronqué).
    revue = appel_avec_retry(
        client=client, model=settings.llm_model, system=SYSTEM_PROMPT,
        user_content=user_content, tool_name="rendre_revue_portefeuille",
        schema=RevuePortefeuille, max_tokens=min(600 + 160 * len(positions), 4000),
    )
    connus = {pos.ticker.upper() for pos in positions}
    verdicts: dict[str, RevuePosition] = {}
    for v in revue.verdicts:
        t = (v.ticker or "").upper().strip()
        if t in connus:
            verdicts[t] = v
        else:
            print(f"  ⚠️ Gérant : verdict sur un ticker inconnu ignoré ({v.ticker}).")
    return verdicts, donnees


def _age_jours(pos: Position) -> int:
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(pos.opened_at)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - t).days)
    except Exception:
        return 0


def appliquer_revue(contexte_actu: str = "") -> list[str]:
    """Révise CHAQUE position ouverte et APPLIQUE le verdict au portefeuille."""
    p = load_portfolio()
    journal = []
    if not p.positions:
        return journal
    # G1 — UN appel pour tout le portefeuille (au lieu d'un par position).
    verdicts, donnees = revoir_portefeuille(list(p.positions), contexte_actu)
    for pos in list(p.positions):        # copie : on modifie la liste pendant l'itération
        verdict = verdicts.get(pos.ticker.upper())
        if verdict is None:
            journal.append(f"🟢 {pos.ticker} → GARDER (aucun verdict rendu : choix sûr)")
            continue
        data = donnees.get(pos.ticker.upper(), {})
        price = data.get("price")
        action = verdict.action.value

        if action == "vendre":
            if price:
                journal.append(close_position(p, pos, price, "gerant_vendre"))
                journal.append(f"     ↳ {verdict.raison}")
            else:
                journal.append(f"  ⚠️ {pos.ticker} : VENDRE voulu mais prix indispo — on garde par prudence.")
        elif action == "alleger":
            if price:
                # 🛡️ S15 — Le verdict du LLM ne suffit plus : 134 des 153 sorties de l'audit
                #    étaient des allègements (lot médian 25 $). On vérifie mécaniquement que
                #    l'allègement a un SENS (délai, montant, répétition) avant de l'exécuter.
                autorise, motif = allegement_autorise(pos, price, 0.5)
                if autorise:
                    journal.append(trim_position(p, pos, price, 0.5, "gerant_alleger"))
                    journal.append(f"     ↳ {verdict.raison}")
                else:
                    journal.append(f"  ⏸️ {pos.ticker} : allègement BLOQUÉ — {motif}.")
            else:
                journal.append(f"  ⚠️ {pos.ticker} : ALLÉGER voulu mais prix indispo — on garde.")
        else:  # garder
            journal.append(f"🟢 {pos.ticker} → GARDER (conviction restante {verdict.conviction_restante})")

    save_portfolio(p)
    return journal


if __name__ == "__main__":
    p = load_portfolio()
    if not p.positions:
        print("\n📂 Aucune position ouverte à réviser. Le Gérant n'a rien à faire.")
    else:
        print(f"\n📋 Le Gérant révise et gère {len(p.positions)} position(s)...\n")
        for ligne in appliquer_revue():
            print(ligne)
        print("\n" + snapshot_text(load_portfolio()))