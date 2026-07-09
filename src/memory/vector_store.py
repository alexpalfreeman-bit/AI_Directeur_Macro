"""
Couche C : Mémoire augmentée (RAG) — PERSISTANTE (Upstash) et LÉGÈRE (sans modèle).

Chaque décision du Directeur est résumée puis stockée. Lors d'une nouvelle thèse,
on retrouve les décisions passées les plus SIMILAIRES pour donner au Directeur le
recul de l'expérience.

🪶 MÉMOIRE — pourquoi plus de ChromaDB :
Sur le plan Starter de Render (512 Mo), l'embedding par défaut de Chroma (modèle ONNX
MiniLM via onnxruntime) consommait 200–350 Mo à lui seul et faisait tomber les crons en
OOM (« Ran out of memory ») — le process était tué AVANT l'envoi Telegram. Pour une
mémoire de ≤500 courts résumés, un modèle sémantique est de la sur-ingénierie. On le
remplace par une similarité lexicale (cosinus TF pur-Python) : ~0 Mo de surcoût, aucun
téléchargement de modèle, aucune dépendance lourde. La source de vérité reste Upstash.

🛡️ C5 — ANTI-ÉCRASEMENT (même protection que C4 sur le portefeuille) :
La lecture LÈVE `LectureStockageErreur` en cas d'échec ; les écritures refusent alors
d'écraser l'historique, et les lectures pures dégradent proprement (index vide ce run).

Les signatures publiques (remember_decision / recall_similar) sont INCHANGÉES.
"""
import os
import re
import json
import math
from collections import Counter
from pathlib import Path

from src.schemas.thesis import MacroThesis
from src.schemas.decision import PortfolioDecision

RAG_KEY = "rag_decisions"
RAG_INIT_KEY = "rag_decisions_initialized"   # C5 — témoin : une mémoire RAG a déjà existé
RAG_FILE = Path("data/rag_decisions.json")
MAX_ENREGISTREMENTS = 500          # on borne l'historique pour rester léger
SEUIL_SIMILARITE = 0.02            # en dessous, on considère « aucun lien » (on n'affiche pas)

# ── Stockage persistant : Upstash en cloud, fichier local sinon (comme le portefeuille) ──
_redis = None
_url = os.getenv("UPSTASH_REDIS_REST_URL")
_token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
if _url and _token:
    from upstash_redis import Redis
    _redis = Redis(url=_url, token=_token)


class LectureStockageErreur(RuntimeError):
    """C5 — Une LECTURE du stockage a échoué (réseau) ou une incohérence a été détectée
    (clé vide alors qu'un témoin d'init existe). L'appelant NE DOIT PAS écrire :
    écrire par-dessus détruirait l'historique. On lève, on n'écrase jamais."""


# ─── Stockage persistant (source de vérité) ───
def _charger_enregistrements() -> list:
    """
    Charge tous les enregistrements de décision.

    C5 — Sémantique stricte (comme C4) :
      • Cloud : lecture Upstash en échec → LÈVE. Clé vide + témoin d'init présent →
        anomalie, on lève. JSON corrompu → on lève.
      • Local : fichier absent = premier démarrage → []. Fichier présent illisible → on lève.
    """
    if _redis is not None:
        try:
            brut = _redis.get(RAG_KEY)
        except Exception as e:
            raise LectureStockageErreur(
                f"Lecture Upstash de la mémoire RAG échouée ({e}) — écriture bloquée "
                f"pour ne pas écraser l'historique."
            ) from e

        if brut:
            try:
                _redis.set(RAG_INIT_KEY, "1")   # auto-cicatrisation du témoin
            except Exception:
                pass
            try:
                return json.loads(brut)
            except Exception as e:
                raise LectureStockageErreur(
                    f"Mémoire RAG Upstash illisible (JSON corrompu : {e}) — écriture bloquée."
                ) from e

        try:
            deja_init = _redis.get(RAG_INIT_KEY)
        except Exception as e:
            raise LectureStockageErreur(
                f"Lecture du témoin d'initialisation échouée ({e}) — écriture bloquée."
            ) from e
        if deja_init:
            raise LectureStockageErreur(
                "Clé « rag_decisions » vide alors que le témoin d'init existe : "
                "anomalie de stockage. Refus d'écrire par-dessus (anti-écrasement)."
            )
        return []  # authentique premier démarrage

    # --- Local ---
    if not RAG_FILE.exists():
        return []
    try:
        return json.loads(RAG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise LectureStockageErreur(
            f"Fichier local de la mémoire RAG illisible ({e}) — écriture bloquée."
        ) from e


def _sauver_enregistrements(records: list) -> None:
    """Sauvegarde tous les enregistrements (bornés aux plus récents). Pose le témoin
    d'init après une écriture Upstash réussie."""
    records = records[-MAX_ENREGISTREMENTS:]
    charge_utile = json.dumps(records, ensure_ascii=False)
    if _redis is not None:
        try:
            _redis.set(RAG_KEY, charge_utile)
        except Exception as e:
            print(f"[vector_store] ⚠️ Ecriture Upstash echouee ({e}) — historique préservé.")
            return
        try:
            _redis.set(RAG_INIT_KEY, "1")
        except Exception:
            pass
        return
    RAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    RAG_FILE.write_text(charge_utile, encoding="utf-8")


# ─── Similarité lexicale (pur-Python, sans modèle) ───
# Mots-outils FR/EN + termes financiers trop génériques pour discriminer une thèse.
_MOTS_VIDES = {
    "les", "des", "une", "aux", "avec", "pour", "dans", "sur", "par", "que", "qui",
    "the", "and", "for", "with", "this", "that", "from", "are", "was", "were",
    "catalyseur", "theme", "thème", "secteur", "sector", "tickers", "decision",
    "décision", "confiance", "confidence", "raisonnement", "action", "marche", "marché",
    "these", "thèse", "titre", "titres", "prix", "trade", "position",
}


def _tokeniser(texte: str) -> list[str]:
    """Mots significatifs en minuscules (>2 lettres, hors mots-outils)."""
    bruts = re.findall(r"[0-9a-zA-Zà-ÿ]+", (texte or "").lower())
    return [m for m in bruts if len(m) > 2 and m not in _MOTS_VIDES]


def _similarite(q: Counter, doc_tokens: list[str]) -> float:
    """Cosinus de fréquence de termes entre la requête (Counter) et un document."""
    if not q or not doc_tokens:
        return 0.0
    d = Counter(doc_tokens)
    communs = set(q) & set(d)
    if not communs:
        return 0.0
    num = sum(q[t] * d[t] for t in communs)
    nq = math.sqrt(sum(v * v for v in q.values()))
    nd = math.sqrt(sum(v * v for v in d.values()))
    return num / (nq * nd) if nq and nd else 0.0


# ─── API publique (signatures inchangées) ───
def remember_decision(thesis: MacroThesis, decision: PortfolioDecision,
                      regime_tag: str = "indetermine") -> None:
    """Persiste une décision (Upstash source de vérité).

    C5 — BEST-EFFORT : si la lecture préalable de l'historique échoue, on saute la
    persistance (sans écraser) au lieu de lever — la mémoire est un bonus, jamais un
    point de défaillance qui tuerait le comité après la décision d'Opus."""
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
    try:
        records = _charger_enregistrements()
    except LectureStockageErreur as e:
        print(f"[vector_store] ⚠️ Persistance RAG SAUTÉE (anti-écrasement) : {e}")
        return
    records.append({"id": decision.decision_id, "document": document, "metadata": metadata})
    _sauver_enregistrements(records)


def recall_similar(thesis: MacroThesis, k: int = 3) -> list:
    """Retrouve les k décisions passées les plus similaires (similarité lexicale).

    C5 — LECTURE pure : si le stockage est illisible, on renvoie [] (dégradation propre),
    sans jamais lever vers l'appelant ni écrire."""
    try:
        records = _charger_enregistrements()
    except LectureStockageErreur as e:
        print(f"[vector_store] ⚠️ Rappel RAG indisponible ce cycle ({e}) — aucun historique servi.")
        return []
    if not records:
        return []

    requete = f"{thesis.catalyst.type.value} {thesis.theme} {thesis.sector}"
    q = Counter(_tokeniser(requete))

    notes = []
    for r in records:
        score = _similarite(q, _tokeniser(r.get("document", "")))
        meta = r.get("metadata", {}) or {}
        # Petit bonus pour un secteur / catalyseur IDENTIQUE (champs contrôlés, très discriminants)
        if meta.get("sector") and thesis.sector and str(meta["sector"]).lower() == thesis.sector.lower():
            score += 0.15
        if meta.get("catalyst") and meta["catalyst"] == thesis.catalyst.type.value:
            score += 0.15
        notes.append((score, r))

    notes.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in notes[:k]:
        if score < SEUIL_SIMILARITE:
            continue   # aucun lien réel : mieux vaut ne rien servir qu'un exemple hors-sujet
        out.append({"summary": r["document"], "meta": r["metadata"]})
    return out


if __name__ == "__main__":
    stockage = "Upstash (cloud)" if _redis is not None else "fichier local (dev)"
    try:
        n = len(_charger_enregistrements())
    except LectureStockageErreur as e:
        n = f"illisible ({e})"
    print(f"\n🧠 Mémoire RAG [{stockage}] : {n} décision(s) enregistrée(s).")
    print("   (Normal si c'est 0 au tout premier lancement. Aucun modèle chargé : léger en RAM.)")