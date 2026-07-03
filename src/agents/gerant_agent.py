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
    load_portfolio, save_portfolio, close_position, trim_position,
    snapshot_text, Position,
)
from src.schemas.revue import RevuePosition

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


def appliquer_revue(contexte_actu: str = "") -> list[str]:
    """Révise CHAQUE position ouverte et APPLIQUE le verdict au portefeuille."""
    p = load_portfolio()
    journal = []
    for pos in list(p.positions):        # copie : on modifie la liste pendant l'itération
        verdict, data = revoir_position(pos, contexte_actu)
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
                journal.append(trim_position(p, pos, price, 0.5, "gerant_alleger"))
                journal.append(f"     ↳ {verdict.raison}")
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