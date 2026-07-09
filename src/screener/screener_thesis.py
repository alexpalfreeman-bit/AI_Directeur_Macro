# src/screener/screener_thesis.py
"""
Pont entre le screener (bottom-up) et le comité.
Transforme les meilleurs scores en une thèse que le Quant, l'Avocat du Diable
et le Directeur savent déjà évaluer. Le screener PROPOSE, le comité DISPOSE.
"""
import uuid
from datetime import datetime, timezone
import anthropic
from config.settings import settings
from src.schemas.thesis import MacroThesis
from src.screener.screener import scanner_univers
from src.agents.tool_helper import appel_avec_retry   # S6 — même robustesse que les autres agents

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

    user_content = (
        f"Voici le TOP {top_n} d'un screener par facteurs (données réelles) :\n\n"
        f"{classement}\n\n"
        "Formule une thèse bottom-up via l'outil. Le type de catalyseur est 'other'. "
        "Utilise EXACTEMENT ces tickers comme candidats. Sois honnête sur les "
        "valorisations si elles sont tendues."
    )

    # 🛡️ S6 — On passe par appel_avec_retry (redemande si un champ manque, au lieu de
    #    crasher le cycle sur un KeyError/StopIteration) et le wrapper élargit le budget
    #    de tokens si la réponse est tronquée. On IMPOSE nos champs via forcer_id :
    #    les tickers viennent du screener, l'id et la date sont générés frais côté serveur.
    these = appel_avec_retry(
        client=client,
        model=settings.llm_model,
        system=SYSTEM_PROMPT,
        user_content=user_content,
        tool_name="soumettre_these",
        schema=MacroThesis,
        max_tokens=3000,                       # une MacroThesis complète (chaîne causale) est verbeuse
        forcer_id={
            "candidate_tickers": tickers,      # on impose les tickers du screener
            "thesis_id": str(uuid.uuid4()),    # id frais (pas celui qu'inventerait le LLM)
            "created_at": datetime.now(timezone.utc),
        },
    )
    return these


if __name__ == "__main__":
    print("🔍 Génération d'une thèse à partir du screener...\n")
    these = generer_these_screener(top_n=5)
    print("=== THÈSE BOTTOM-UP (screener) ===")
    print(these.model_dump_json(indent=2))