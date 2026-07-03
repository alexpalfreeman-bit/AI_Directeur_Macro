# src/agents/quant_agent.py
"""
Agent 2 : Le Quant.
Reçoit une thèse, récupère les CHIFFRES RÉELS via market_client,
et rend un verdict structuré. Le LLM juge ; il n'invente aucun nombre.
"""
import anthropic
from config.settings import settings
from src.schemas.thesis import MacroThesis, TickerHealth, QuantValidation
from src.ingestion.market_client import get_fundamentals
from src.agents.tool_helper import appel_avec_retry
from config.settings import settings

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

SYSTEM_PROMPT = """Tu es un analyste quantitatif rigoureux et sceptique.

On te remet une thèse macro et les CHIFFRES RÉELS du marché pour chaque action.
Ton rôle :
- Vérifier si les fondamentaux soutiennent la thèse (valorisation, santé financière).
- Repérer les actions déjà SURÉVALUÉES : le marché a peut-être déjà tout intégré.
- Décider lesquelles survivent et lesquelles sont rejetées, et pourquoi.

RÈGLE ABSOLUE : tu raisonnes UNIQUEMENT à partir des chiffres réels fournis.
Tu n'inventes aucune donnée. Si un chiffre est absent (null), signale-le, ne devine pas. 
ATTENTION AUX CYCLIQUES : sur un producteur de matières premières, le PE instantané
est trompeur. Un PE bas peut signaler un PIC de bénéfices (sommet de cycle = cher),
un PE élevé un CREUX (potentiel plancher). Privilégie l'EV/EBITDA et le Price-to-Book
pour juger la valorisation de ces titres, et croise toujours avec le PE.

FILTRE VALUE-TRAP (piège de valeur) : une action bon marché n'est pas forcément une aubaine.
Avant de laisser survivre un ticker sur un argument de faible valorisation (PE, P/B ou
EV/EBITDA bas), demande-toi si ce bas prix est MÉRITÉ : déclin structurel du secteur, érosion
durable des marges, endettement qui s'aggrave, désavantage concurrentiel permanent ou
obsolescence. Distingue « injustement puni » (edge réel) de « bon marché pour une bonne
raison » (piège). Un ratio bas COUPLÉ à des fondamentaux qui se dégradent (dette/capitaux
élevée, rentabilité en berne) est un signal de PIÈGE, pas d'opportunité : dans le doute,
rejette le ticker et explique-le dans `quant_notes`.
"""


def fetch_real_data(tickers: list[str]) -> list[TickerHealth]:
    """Récupère les vrais chiffres pour chaque ticker (zéro LLM ici)."""
    out = []
    for ticker in tickers:
        raw = get_fundamentals(ticker)
        out.append(TickerHealth(
            ticker=raw["ticker"], name=raw.get("name"), price=raw.get("price"),
            pe_ratio=raw.get("pe_ratio"), debt_to_equity=raw.get("debt_to_equity"),
            market_cap=raw.get("market_cap"),
            volatility_30d_pct=raw.get("volatility_30d_pct"),
            ev_to_ebitda=raw.get("ev_to_ebitda"),       # ← AJOUTE
            price_to_book=raw.get("price_to_book"),      # ← AJOUTE
            avg_volume=raw.get("avg_volume"),
        ))
    return out

def filtrer_qualite(real_data: list) -> tuple[list, list]:
    """
    Écarte les tickers trop petits ou trop peu liquides, en utilisant les
    données DÉJÀ récupérées (aucun nouvel appel API).
    """
    retenus, rejets = [], []
    for t in real_data:
        cap = t.market_cap
        vol = t.avg_volume

        if cap is None or vol is None:
            # Donnée manquante : on NE rejette PAS (on laisse le LLM juger),
            # car yfinance est parfois capricieux sur un champ isolé.
            retenus.append(t)
            continue
        if cap < settings.min_market_cap:
            rejets.append(f"{t.ticker} : capitalisation trop faible ({cap/1e6:.0f}M$)")
            continue
        if vol < settings.min_avg_volume:
            rejets.append(f"{t.ticker} : volume trop faible ({vol:,})")
            continue
        retenus.append(t)
    return retenus, rejets

def validate_thesis(thesis: MacroThesis) -> tuple[list[TickerHealth], QuantValidation]:
    # 1) Les VRAIS chiffres (API, pas LLM)
    real_data = fetch_real_data(thesis.candidate_tickers)
    # Filtre de qualité/liquidité AVANT le jugement du LLM
    real_data, rejets_qualite = filtrer_qualite(real_data)
    for motif in rejets_qualite:
        print(f"  🚫 Écarté (qualité) : {motif}")

    # Si plus aucun ticker ne passe le filtre, inutile d'appeler le LLM
    if not real_data:
        return [], QuantValidation(
            thesis_id=thesis.thesis_id,
            verdict="rejected",
            surviving_tickers=[],
            market_already_pricing_in=False,
            quant_notes="Tous les tickers écartés par le filtre de qualité/liquidité.",
        )
    
    data_text = "\n".join(
        f"- {t.ticker} ({t.name}) : prix={t.price}, PE={t.pe_ratio}, "
        f"EV/EBITDA={t.ev_to_ebitda}, P/B={t.price_to_book}, "
        f"dette/capitaux={t.debt_to_equity}, capitalisation={t.market_cap}, "
        f"volatilité 30j={t.volatility_30d_pct}%"
        for t in real_data
    )

    # 2) Le LLM juge, via sortie structurée forcée
    tool = {
        "name": "rendre_verdict",
        "description": "Rend un verdict quantitatif structuré sur la thèse.",
        "input_schema": QuantValidation.model_json_schema(),
    }
    user_content = (
        f"THÈSE À VALIDER :\n{thesis.model_dump_json(indent=2)}\n\n"
        f"CHIFFRES RÉELS (source yfinance — n'utilise QUE ceux-ci) :\n{data_text}\n\n"
        "Rends ton verdict via l'outil 'rendre_verdict'. Remplis TOUS les champs."
    )

    verdict = appel_avec_retry(
        client=client,
        model=settings.llm_model,
        system=SYSTEM_PROMPT,
        user_content=user_content,
        tool_name="rendre_verdict",
        schema=QuantValidation,
        max_tokens=1200,
        forcer_id={"thesis_id": thesis.thesis_id},   # on impose le bon id
    )

    return real_data, verdict


if __name__ == "__main__":
    from src.agents.macro_agent import generate_thesis

    scenario = (
        "La Banque du Japon a surpris en relevant ses taux ; le yen s'apprécie. "
        "Des tensions dans le détroit d'Ormuz perturbent le transport de produits "
        "chimiques et d'engrais vers l'Amérique du Nord."
    )

    print("\n🧠 Agent Macro : génération de la thèse...")
    thesis = generate_thesis(scenario)
    print(f"   Thème : {thesis.theme[:75]}...")
    print(f"   Tickers candidats : {thesis.candidate_tickers}")
    print(f"   Confiance : {thesis.confidence}\n")

    print("📊 Agent Quant : récupération des chiffres réels + validation...\n")
    real_data, verdict = validate_thesis(thesis)

    print("=== CHIFFRES RÉELS (source : yfinance) ===")
    for t in real_data:
        print(f"  {t.ticker:6} prix={t.price}  PE={t.pe_ratio}  capi={t.market_cap}")

    print("\n=== VERDICT DU QUANT ===")
    print(verdict.model_dump_json(indent=2))