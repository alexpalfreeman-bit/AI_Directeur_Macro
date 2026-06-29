# src/agents/devils_advocate_agent.py
"""
Agent 3 : L'Avocat du Diable.
Son unique mission : DÉTRUIRE la thèse. Il reçoit l'idée (Macro) ET les
chiffres (Quant), et cherche la faille fatale. Il encode les réflexes
critiques affûtés à la main : cyclicité en risk-off, chaînes sur-ingénierées,
saisonnalité, pièges de valorisation, données douteuses.
"""
from datetime import datetime
import anthropic
from config.settings import settings
from src.schemas.thesis import MacroThesis, QuantValidation, RiskAssessment

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

SYSTEM_PROMPT = """Tu es le Risk Manager le plus redouté du fonds : l'Avocat du Diable.

Ton UNIQUE mission est de DÉTRUIRE la thèse qu'on te présente. Tu ne cherches pas
l'équilibre : tu cherches la faille fatale. Sois impitoyable mais rigoureux.

Tes angles d'attaque prioritaires :

1. RÉGIME MACRO & CYCLICITÉ. Si la thèse repose sur un choc macro (hausse de taux,
   appréciation d'une devise, deleveraging d'un carry trade), demande-toi : un
   "risk-off" généralisé ne ferait-il pas s'effondrer ces titres MALGRÉ leurs
   fondamentaux ? Sur des valeurs cycliques (matières premières, chimie), le krach
   macro l'emporte souvent sur l'effet micro. Le bon scénario peut tuer le trade.

2. CHAÎNE CAUSALE SUR-INGÉNIERÉE. Attaque les chaînes longues. Chaque maillon
   supplémentaire (surtout les liens de 3e ordre monétaire → devise → matière
   première) multiplie la fragilité. Une chaîne de 5-6 étapes sur 60 jours est
   hautement spéculative. Identifie le maillon le plus faible et tire dessus.

3. TIMING & SAISONNALITÉ. Vérifie, par rapport à la DATE DU JOUR fournie, que la
   fenêtre d'opportunité est encore ouverte. Une saison déjà passée n'est PAS une
   opportunité. Une thèse hors-saison est disqualifiée.

4. PIÈGES DE VALORISATION CYCLIQUE. Sur un producteur de matières premières, le PE
   instantané MENT : un PE bas signale souvent un PIC de bénéfices (sommet de cycle,
   donc cher), un PE élevé signale souvent un CREUX (potentiel plancher). Conteste
   toute conclusion de valorisation qui ignore ce renversement.

5. CALIBRAGE DE LA CONFIANCE. Plus la chaîne causale a d'hypothèses indépendantes,
   plus la confiance devrait être BASSE. Dénonce toute confiance qui ne reflète pas
   le nombre de paris empilés.

6. QUALITÉ DES DONNÉES. Méfie-toi des données manquantes, des tickers non résolus,
   des analyses qui reposent sur des inputs douteux. Un jugement sur du vide est nul.

À la fin, tranche honnêtement : malgré ta démolition, la thèse SURVIT-elle ? Une
bonne thèse peut encaisser tes coups. Ne tue pas par principe — tue par raison."""


def challenge_thesis(thesis: MacroThesis, quant: QuantValidation) -> RiskAssessment:
    now = datetime.now()
    tool = {
        "name": "rendre_evaluation_risque",
        "description": "Rend une évaluation de risque structurée après démolition.",
        "input_schema": RiskAssessment.model_json_schema(),
    }
    response = client.messages.create(
        model=settings.llm_model,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        tools=[tool],
        tool_choice={"type": "tool", "name": "rendre_evaluation_risque"},
        messages=[{
            "role": "user",
            "content": (
                f"DATE DU JOUR : {now.day}/{now.month}/{now.year} (trimestre Q{(now.month-1)//3+1}).\n\n"
                f"THÈSE À DÉTRUIRE :\n{thesis.model_dump_json(indent=2)}\n\n"
                f"VALIDATION DU QUANT (chiffres réels + verdict) :\n{quant.model_dump_json(indent=2)}\n\n"
                "Démolis cette thèse via l'outil 'rendre_evaluation_risque'."
            ),
        }],
    )

    block = next(b for b in response.content if b.type == "tool_use")
    data = dict(block.input)
    data.pop("thesis_id", None)
    try:
        return RiskAssessment(thesis_id=thesis.thesis_id, **data)
    except Exception as e:
        print(f"  ⚠️  Sortie incomplète de l'Avocat du Diable, défauts appliqués : {e}")
        data.setdefault("severity", "serious")
        data.setdefault("survives_scrutiny", False)
        return RiskAssessment(thesis_id=thesis.thesis_id, **data)


if __name__ == "__main__":
    from src.agents.macro_agent import generate_thesis
    from src.agents.quant_agent import validate_thesis

    scenario = (
        "La Banque du Japon a surpris en relevant ses taux ; le yen s'apprécie. "
        "Des tensions dans le détroit d'Ormuz perturbent le transport de produits "
        "chimiques et d'engrais vers l'Amérique du Nord."
    )

    print("\n🧠 [1/3] Agent Macro...")
    thesis = generate_thesis(scenario)
    print(f"     Tickers : {thesis.candidate_tickers} | Confiance : {thesis.confidence}")

    print("📊 [2/3] Agent Quant...")
    _, quant = validate_thesis(thesis)
    print(f"     Survivants : {quant.surviving_tickers}")

    print("😈 [3/3] Avocat du Diable : démolition en cours...\n")
    risk = challenge_thesis(thesis, quant)

    print("=== ÉVALUATION DU RISQUE ===")
    print(risk.model_dump_json(indent=2))
    print(f"\n>>> Sévérité : {risk.severity.upper()} | "
          f"La thèse survit : {'OUI ✅' if risk.survives_scrutiny else 'NON ❌'}")