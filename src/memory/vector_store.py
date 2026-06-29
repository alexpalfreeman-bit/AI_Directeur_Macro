# src/memory/vector_store.py
"""
Couche C : Mémoire augmentée (RAG).
Chaque décision est stockée avec son régime de marché. Lors d'une nouvelle
thèse, on retrouve les décisions passées les plus SIMILAIRES pour donner au
Directeur le recul de l'expérience ("comment ça s'est passé la dernière fois ?").
"""
import chromadb
from src.schemas.thesis import MacroThesis
from src.schemas.decision import PortfolioDecision

client = chromadb.PersistentClient(path="data/memory")
collection = client.get_or_create_collection("decisions")


def remember_decision(thesis: MacroThesis, decision: PortfolioDecision,
                      regime_tag: str = "indetermine") -> None:
    """Stocke une décision dans la mémoire, indexée par son contenu sémantique."""
    document = (
        f"Catalyseur: {thesis.catalyst.type.value}. "
        f"Thème: {thesis.theme}. "
        f"Secteur: {thesis.sector}. "
        f"Tickers: {', '.join(thesis.candidate_tickers)}. "
        f"Décision: {decision.action.value}. "
        f"Confiance: {decision.confidence}. "
        f"Raisonnement: {decision.portfolio_rationale[:500]}"
    )
    collection.add(
        ids=[decision.decision_id],
        documents=[document],
        metadatas=[{
            "action": decision.action.value,
            "sector": thesis.sector,
            "catalyst": thesis.catalyst.type.value,
            "regime": regime_tag,
            "confidence": decision.confidence,
            "decided_at": decision.decided_at.isoformat(),
        }],
    )


def recall_similar(thesis: MacroThesis, k: int = 3) -> list[dict]:
    """Retrouve les k décisions passées les plus similaires à la thèse actuelle."""
    if collection.count() == 0:
        return []
    query = f"{thesis.catalyst.type.value} {thesis.theme} {thesis.sector}"
    res = collection.query(query_texts=[query], n_results=min(k, collection.count()))
    out = []
    for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
        out.append({"summary": doc, "meta": meta})
    return out


if __name__ == "__main__":
    print(f"\n🧠 Mémoire actuelle : {collection.count()} décision(s) stockée(s).")
    print("   (Normal si c'est 0 au premier lancement.)")