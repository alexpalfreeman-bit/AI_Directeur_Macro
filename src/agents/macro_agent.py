# src/agents/macro_agent.py
"""
Agent 1 : Le Macroéconomiste.
Lit un contexte d'actualités et produit une thèse d'investissement
STRUCTURÉE et VALIDÉE, centrée sur les effets de 2e/3e ordre.
"""
import anthropic
from config.settings import settings
from src.schemas.thesis import MacroThesis
from src.agents.tool_helper import appel_avec_retry

from datetime import datetime

MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]

def contexte_temporel() -> str:
    n = datetime.now()
    trimestre = (n.month - 1) // 3 + 1
    return (
        "CONTEXTE TEMPOREL — À RESPECTER ABSOLUMENT :\n"
        f"Nous sommes le {n.day} {MOIS_FR[n.month - 1]} {n.year} (trimestre Q{trimestre}).\n"
        "Ancre TOUTE ta réflexion dans cette date : tiens compte de la saison et du "
        "trimestre en cours. Une opportunité saisonnière déjà passée n'est PAS une "
        "opportunité. Vérifie toujours que ta fenêtre d'action est encore ouverte "
        "par rapport à AUJOURD'HUI."
    )

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

SYSTEM_PROMPT = """Tu es un macroéconomiste global macro de très haut niveau.

Ta philosophie :
- Tu IGNORES les effets de premier ordre, déjà arbitrés par le marché
  (ex: "un détroit ferme -> le pétrole monte" : inutile, c'est trop évident).
- Tu CHERCHES les effets de second et troisième ordre : les conséquences
  en cascade, indirectes, que le marché n'a pas encore intégrées
  (ex: "détroit fermé -> rupture d'appro d'un composant chimique -> pénurie
   aux USA -> avantage pour un producteur alternatif local").
- Ton horizon est de quelques jours à plusieurs mois. JAMAIS de day trading.

Tu identifies des actions (tickers) précises qui profiteraient de ta chaîne
causale. Tu n'inventes aucun chiffre financier : la validation chiffrée
viendra après, par un autre agent.
UNIVERS D'INVESTISSEMENT — RÈGLE STRICTE : propose UNIQUEMENT des tickers cotés aux
États-Unis (NYSE / NASDAQ), y compris les ADR de sociétés étrangères (qui se négocient
en USD). N'utilise JAMAIS de tickers en devise étrangère (ex: .HK, .OL, .PA, .L, .T) :
le portefeuille est en USD et un ticker en devise locale fausse la comptabilité. Pour
une exposition internationale, utilise l'ADR américain de l'entreprise s'il existe ;
sinon, choisis un acteur américain équivalent exposé au même thème.
PRÉCISION DES TICKERS : utilise le symbole boursier EXACT et officiel (ex: "NU" pour
Nu Holdings, pas "NUBANK" ; "GOOGL" pour Alphabet). Si tu n'es pas certain à 100% du
symbole exact d'une société, ne la propose pas — préfère une entreprise dont tu
connais le ticker avec certitude. Un mauvais symbole = un candidat perdu.

CORROBORATION DES SOURCES : on te fournit une synthèse indiquant combien de SOURCES
distinctes couvrent chaque thème. Règle de prudence : un catalyseur confirmé par
PLUSIEURS sources mérite une confiance plus élevée. Un catalyseur vu dans UNE SEULE
source est fragile (rumeur, info isolée, voire erreur) — traite-le avec méfiance et
abaisse fortement ta confiance, ou ne bâtis pas de thèse dessus. Ne construis jamais
un pari important sur un événement non corroboré.

"""


def generate_thesis(news_context: str) -> MacroThesis:
    """Produit une MacroThesis validée à partir d'un contexte d'actualités."""
    user_content = (
        "Voici les actualités macro du moment. Identifie l'opportunité "
        "de 2e/3e ordre la plus prometteuse et soumets ta thèse via l'outil "
        "'soumettre_these'. Remplis TOUS les champs requis (dont 'rationale' et "
        "'confidence').\n\n"
        f"--- ACTUALITÉS ---\n{news_context}"
    )

    # 🛡️ Sortie structurée + retry automatique si un champ manque (rationale, confidence…)
    thesis = appel_avec_retry(
        client=client,
        model=settings.llm_model,
        system=f"{contexte_temporel()}\n\n{SYSTEM_PROMPT}",
        user_content=user_content,
        tool_name="soumettre_these",
        schema=MacroThesis,
        max_tokens=1500,
    )
    return thesis


if __name__ == "__main__":
    # Scénario de test (tu le remplaceras par tes vraies actualités plus tard)
    scenario = (
        "La Banque du Japon a surpris les marchés en relevant ses taux directeurs ; "
        "le yen s'apprécie fortement. En parallèle, des tensions dans le détroit "
        "d'Ormuz perturbent le transport de produits chimiques et d'engrais vers "
        "l'Amérique du Nord."
    )

    print("\n🧠 L'Agent Macro réfléchit...\n")
    thesis = generate_thesis(scenario)
    print("=== THÈSE D'INVESTISSEMENT GÉNÉRÉE ===\n")
    print(thesis.model_dump_json(indent=2))