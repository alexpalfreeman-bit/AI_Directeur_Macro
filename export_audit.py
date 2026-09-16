from dotenv import load_dotenv; load_dotenv()

import json
from src.portfolio.paper_portfolio import load_portfolio

sortie = {}
p = load_portfolio()
sortie["portefeuille"] = p.model_dump(mode="json")

# Série d'équity (snapshots) — pour vérifier Sharpe/alpha
try:
    from src.analytics.performance import _charger_historique
    sortie["equity"] = _charger_historique()
except Exception as e:
    sortie["equity_erreur"] = str(e)

# Mémoire RAG — contient les RAISONNEMENTS du Directeur
try:
    from src.memory.vector_store import _charger_enregistrements
    sortie["decisions"] = _charger_enregistrements()
except Exception as e:
    sortie["decisions_erreur"] = str(e)

# Journal des thèmes macro
try:
    from src.memory.world_memory import _charger_evenements
    sortie["themes"] = _charger_evenements()
except Exception as e:
    sortie["themes_erreur"] = str(e)

with open("export.json", "w", encoding="utf-8") as f:
    json.dump(sortie, f, indent=2, ensure_ascii=False)

print(f"positions ouvertes : {len(p.positions)}")
print(f"trades cloturés    : {len(p.closed)}")
print(f"points d'equity    : {len(sortie.get('equity', []))}")
print(f"decisions RAG      : {len(sortie.get('decisions', []))}")
print("-> export.json ecrit")
