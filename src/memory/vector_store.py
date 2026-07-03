# src/memory/vector_store.py
"""
Couche C : Mémoire augmentée (RAG), désormais PERSISTANTE dans le cloud.

Chaque décision du Directeur est résumée puis stockée. Lors d'une nouvelle thèse,
on retrouve les décisions passées les plus SIMILAIRES (par le SENS, pas par mots-clés)
pour donner au Directeur le recul de l'expérience ("comment ça s'est passé la
dernière fois qu'une thèse comme ça est passée ?").

⚠️ Problème résolu ici : sur Render, chaque cron tourne dans un conteneur NEUF au
disque ÉPHÉMÈRE. Chroma écrivait dans data/memory, effacé à chaque run → la mémoire
RAG était vidée en permanence dans le cloud, et recall_similar renvoyait presque
toujours vide (le "Apprends du passé" du Directeur était mort en production).

Solution : la SOURCE DE VÉRITÉ devient Upstash Redis (persistant), clé `rag_decisions`.
Chroma n'est plus qu'un index sémantique ÉPHÉMÈRE, RECONSTRUIT à chaque run à partir
d'Upstash. En local (sans Upstash), on retombe sur un fichier data/rag_decisions.json,
exactement comme le portefeuille et la mémoire du monde.

Les signatures publiques (remember_decision / recall_similar) sont INCHANGÉES :
aucun autre fichier n'a besoin d'être modifié.
"""
import os
import json
from pathlib import Path

import chromadb
from src.schemas.thesis import MacroThesis
from src.schemas.decision import PortfolioDecision

RAG_KEY = "rag_decisions"
RAG_FILE = Path("data/rag_decisions.json")
MAX_ENREGISTREMENTS = 500          # on borne l'historique pour rester léger

# ── Stockage persistant : Upstash en cloud, fichier local sinon (comme le portefeuille) ──
_redis = None
_url = os.getenv("UPSTASH_REDIS_REST_URL")
_token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
if _url and _token:
    from upstash_redis import Redis
    _redis = Redis(url=_url, token=_token)

# ── Chroma ÉPHÉMÈRE (en mémoire) : reconstruit depuis le stockage persistant à chaque
#    run. C'est le cœur du correctif — plus aucune dépendance au disque de Render. ──
_client = chromadb.EphemeralClient()   # (si ta version de chromadb est très ancienne : chromadb.Client())
collection = _client.get_or_create_collection("decisions")
_hydrate = False    # a-t-on déjà rechargé l'index depuis le stockage persistant ce run ?


# ─── Stockage persistant (source de vérité) ───
def _charger_enregistrements() -> list:
    """Charge tous les enregistrements de décision (Upstash en cloud, fichier en local)."""
    if _redis is not None:
        try:
            brut = _redis.get(RAG_KEY)
            return json.loads(brut) if brut else []
        except Exception as e:
            print(f"[vector_store] Lecture Upstash echouee ({e}).")
            return []
    if RAG_FILE.exists():
        try:
            return json.loads(RAG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[vector_store] Lecture fichier local echouee ({e}).")
    return []


def _sauver_enregistrements(records: list) -> None:
    """Sauvegarde tous les enregistrements (bornés aux plus récents)."""
    records = records[-MAX_ENREGISTREMENTS:]
    charge_utile = json.dumps(records, ensure_ascii=False)
    if _redis is not None:
        try:
            _redis.set(RAG_KEY, charge_utile)
        except Exception as e:
            print(f"[vector_store] Ecriture Upstash echouee ({e}).")
        return
    RAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    RAG_FILE.write_text(charge_utile, encoding="utf-8")


def _hydrater() -> None:
    """Reconstruit l'index Chroma depuis le stockage persistant — UNE fois par run."""
    global _hydrate
    if _hydrate:
        return
    records = _charger_enregistrements()
    if records:
        collection.add(
            ids=[r["id"] for r in records],
            documents=[r["document"] for r in records],
            metadatas=[r["metadata"] for r in records],
        )
    _hydrate = True


# ─── API publique (signatures inchangées) ───
def remember_decision(thesis: MacroThesis, decision: PortfolioDecision,
                      regime_tag: str = "indetermine") -> None:
    """Stocke une décision : dans l'index Chroma de la session ET dans le stockage persistant."""
    _hydrater()
    document = (
        f"Catalyseur: {thesis.catalyst.type.value}. "
        f"Thème: {thesis.theme}. "
        f"Secteur: {thesis.sector}. "
        f"Tickers: {', '.join(thesis.candidate_tickers)}. "
        f"Décision: {decision.action.value}. "
        f"Confiance: {decision.confidence}. "
        f"Raisonnement: {decision.portfolio_rationale[:500]}"
    )
    metadata = {
        "action": decision.action.value,
        "sector": thesis.sector,
        "catalyst": thesis.catalyst.type.value,
        "regime": regime_tag,
        "confidence": decision.confidence,
        "decided_at": decision.decided_at.isoformat(),
    }
    # 1) index sémantique de la session courante
    collection.add(ids=[decision.decision_id], documents=[document], metadatas=[metadata])
    # 2) persistance (survit aux redéploiements ET aux runs de cron)
    records = _charger_enregistrements()
    records.append({"id": decision.decision_id, "document": document, "metadata": metadata})
    _sauver_enregistrements(records)


def recall_similar(thesis: MacroThesis, k: int = 3) -> list:
    """Retrouve les k décisions passées les plus similaires à la thèse actuelle."""
    _hydrater()
    if collection.count() == 0:
        return []
    query = f"{thesis.catalyst.type.value} {thesis.theme} {thesis.sector}"
    res = collection.query(query_texts=[query], n_results=min(k, collection.count()))
    out = []
    for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
        out.append({"summary": doc, "meta": meta})
    return out


if __name__ == "__main__":
    _hydrater()
    stockage = "Upstash (cloud)" if _redis is not None else "fichier local (dev)"
    print(f"\n🧠 Mémoire RAG [{stockage}] : {collection.count()} décision(s) chargée(s).")
    print("   (Normal si c'est 0 au tout premier lancement.)")