# src/screener/screener_thesis.py
"""
Pont entre le screener (bottom-up) et le comité.
Transforme les meilleurs scores en une thèse que le Quant, l'Avocat du Diable
et le Directeur savent déjà évaluer. Le screener PROPOSE, le comité DISPOSE.
"""
from datetime import datetime
import anthropic
from config.settings import settings
from src.schemas.thesis import MacroThesis
from src.screener.screener import scanner_univers

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

SYSTEM_PROMPT = """Tu es un analyste qui transforme un classement quantitatif en
thèse d'investissement structurée.

On te donne les meilleurs titres d'un screener par facteurs (momentum, croissance,
qualité, valorisation). Ton rôle : formuler une thèse bottom-up cohérente à partir
de ces données — PAS inventer un catalyseur géopolitique.

Sois lucide : si les titres ont d'excellents scores de momentum mais des
valorisations très élevées (valo basse), DIS-LE et abaisse ta confiance. Le
screener identifie des candidats, il ne garantit rien. La validation chiffrée et la
critique viendront après, par d'autres agents."""


def generer_these_screener(top_n: int = 5) -> MacroThesis:
    """Scanne l'univers et formule une thèse bottom-up sur les meilleurs titres."""
    top = scanner_univers(top_n=top_n)

    classement = "\n".join(
        f"- {r['ticker']} ({r['name']}) : score {r['score']}/100 "
        f"[momentum={r['detail']['momentum']}, croissance={r['detail']['croissance']}, "
        f"qualité={r['detail']['qualite']}, valorisation={r['detail']['valorisation']}]"
        for r in top
    )
    tickers = [r["ticker"] for r in top]

    tool = {
        "name": "soumettre_these",
        "description": "Soumet une thèse d'investissement bottom-up structurée.",
        "input_schema": MacroThesis.model_json_schema(),
    }
    response = client.messages.create(
        model=settings.llm_model,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        tools=[tool],
        tool_choice={"type": "tool", "name": "soumettre_these"},
        messages=[{"role": "user", "content": (
            f"Voici le TOP {top_n} d'un screener par facteurs (données réelles) :\n\n"
            f"{classement}\n\n"
            "Formule une thèse bottom-up via l'outil. Le type de catalyseur est 'other'. "
            "Utilise EXACTEMENT ces tickers comme candidats. Sois honnête sur les "
            "valorisations si elles sont tendues."
        )}],
    )

    block = next(b for b in response.content if b.type == "tool_use")
    data = dict(block.input)
    data.pop("thesis_id", None)
    data.pop("created_at", None)
    data["candidate_tickers"] = tickers   # on impose les tickers du screener
    return MacroThesis(**data)


if __name__ == "__main__":
    print("🔍 Génération d'une thèse à partir du screener...\n")
    these = generer_these_screener(top_n=5)
    print("=== THÈSE BOTTOM-UP (screener) ===")
    print(these.model_dump_json(indent=2))