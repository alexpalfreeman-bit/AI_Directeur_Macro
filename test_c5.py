"""
Harnais de test C5 — anti-écrasement de world_memory et vector_store,
+ vérification que vector_store fonctionne SANS ChromaDB (similarité lexicale).

Aucun réseau réel : on injecte un faux stockage Upstash contrôlable (peut être
forcé à échouer en lecture) et on vérifie l'invariant clé :

    UNE LECTURE QUI ÉCHOUE NE DOIT JAMAIS DÉTRUIRE L'HISTORIQUE.
"""
import json
import types

VERT, ROUGE, RESET = "\033[92m", "\033[91m", "\033[0m"
_ok = _ko = 0
def check(cond, libelle):
    global _ok, _ko
    if cond:
        _ok += 1; print(f"  {VERT}\u2713{RESET} {libelle}")
    else:
        _ko += 1; print(f"  {ROUGE}\u2717 \u00c9CHEC{RESET} {libelle}")


# ══════════════════ Faux stockage Upstash REST (pour world_memory) ══════════════════
class FauxStoreREST:
    def __init__(self):
        self.data = {}
        self.lecture_echoue = False
    def post(self, url, headers=None, json=None, timeout=None):
        cmd, cle = json[0], json[1]
        store = self
        class Rep:
            def raise_for_status(self_):
                if store.lecture_echoue and cmd == "GET":
                    raise RuntimeError("panne réseau simulée (GET)")
            def json(self_):
                return {"result": store.data.get(cle)}
        if cmd == "SET":
            store.data[cle] = json[2]
        return Rep()


# ══════════════════ Faux client upstash_redis (pour vector_store) ══════════════════
class FauxRedisLib:
    def __init__(self):
        self.data = {}
        self.lecture_echoue = False
    def get(self, cle):
        if self.lecture_echoue:
            raise RuntimeError("panne réseau simulée (GET)")
        return self.data.get(cle)
    def set(self, cle, val):
        self.data[cle] = val


# ═══════════════════════════════ WORLD_MEMORY ═══════════════════════════════
print("\n=== world_memory.py ===")
import src.memory.world_memory as wm

faux = FauxStoreREST()
wm._URL, wm._TOKEN = "http://fake", "tok"
wm.requests = types.SimpleNamespace(post=faux.post)

faux.data.clear()
check("aucun theme macro" in wm.contexte_historique(), "1er démarrage → contexte neutre")

wm.enregistrer_evenement(theme="Engrais azotés", tickers=["CF"])
wm.enregistrer_evenement(theme="Terres rares", tickers=["MP"])
hist = json.loads(faux.data[wm.CLE_REDIS])
check(len(hist) == 2, f"2 enregistrements normaux → 2 événements (obtenu {len(hist)})")
check(faux.data.get(wm.CLE_INIT) == "1", "témoin d'initialisation posé après écriture")

avant = faux.data[wm.CLE_REDIS]
faux.lecture_echoue = True
wm.enregistrer_evenement(theme="Choc pétrolier", tickers=["OXY"])
faux.lecture_echoue = False
check(faux.data[wm.CLE_REDIS] == avant, "PANNE LECTURE → enregistrement abandonné, historique INTACT")
check(len(json.loads(faux.data[wm.CLE_REDIS])) == 2, "l'événement fantôme n'a PAS remplacé le journal")

faux.data[wm.CLE_REDIS] = None
wm.enregistrer_evenement(theme="Ne doit pas passer", tickers=["X"])
check(faux.data.get(wm.CLE_REDIS) is None, "clé vide + témoin présent → écriture refusée")

faux.data[wm.CLE_REDIS] = json.dumps(hist)
faux.lecture_echoue = True
try:
    check("indisponible" in wm.contexte_historique().lower(), "contexte_historique dégrade proprement")
except Exception as e:
    check(False, f"contexte_historique a levé : {e}")
faux.lecture_echoue = False


# ═══════════════════════════════ VECTOR_STORE (sans Chroma) ═══════════════════════════════
print("\n=== vector_store.py (sans ChromaDB) ===")
import sys
check("chromadb" not in sys.modules, "ChromaDB N'EST PAS chargé en mémoire (économie RAM → plus d'OOM)")

import src.memory.vector_store as vs
faux2 = FauxRedisLib()
vs._redis = faux2

from src.schemas.thesis import MacroThesis, Catalyst, CatalystType, Direction
from src.schemas.decision import PortfolioDecision, FinalAction

def fabriquer(theme, sector="Energy", catalyst=CatalystType.OTHER):
    th = MacroThesis(
        catalyst=Catalyst(type=catalyst, description="test"),
        causal_chain=["a", "b"], sector=sector, theme=theme, direction=Direction.LONG,
        time_horizon_days=30, candidate_tickers=["CF"], regions=["US"],
        rationale="r", confidence=0.6,
    )
    dec = PortfolioDecision(thesis_id=th.thesis_id, action=FinalAction.WATCHLIST,
                            confidence=0.6, portfolio_rationale=f"Décision sur {theme}")
    return th, dec

# 1) Accumulation normale
faux2.data.clear()
th1, d1 = fabriquer("Engrais azotés et gaz naturel", sector="Basic Materials")
th2, d2 = fabriquer("Terres rares et aimants", sector="Technology")
vs.remember_decision(th1, d1)
vs.remember_decision(th2, d2)
recs = json.loads(faux2.data[vs.RAG_KEY])
check(len(recs) == 2, f"2 décisions mémorisées → 2 enregistrements persistés (obtenu {len(recs)})")
check(faux2.data.get(vs.RAG_INIT_KEY) == "1", "témoin d'initialisation posé")

# 2) La recherche lexicale fonctionne SANS modèle : la thèse la plus proche remonte en tête
th_query, _ = fabriquer("Nouveau choc sur les engrais azotés", sector="Basic Materials")
res = vs.recall_similar(th_query, k=2)
check(isinstance(res, list) and len(res) >= 1, "recall_similar renvoie des résultats (similarité lexicale)")
check(res and "Engrais" in res[0]["summary"], "le plus similaire (engrais/Basic Materials) remonte en 1er")

# 3) TEST CRITIQUE : lecture échoue → remember saute la persistance SANS écraser NI lever
avant = faux2.data[vs.RAG_KEY]
faux2.lecture_echoue = True
th3, d3 = fabriquer("Choc pétrolier")
try:
    vs.remember_decision(th3, d3); leve = False
except Exception:
    leve = True
faux2.lecture_echoue = False
check(not leve, "remember_decision NE LÈVE PAS en cas de panne lecture (pipeline protégé)")
check(faux2.data[vs.RAG_KEY] == avant, "PANNE LECTURE → persistance sautée, historique INTACT")
check(len(json.loads(faux2.data[vs.RAG_KEY])) == 2, "la décision fantôme n'a PAS remplacé l'historique")

# 4) Anomalie clé vide + témoin → refus d'écrire
faux2.data[vs.RAG_KEY] = None
th4, d4 = fabriquer("Ne doit pas passer")
vs.remember_decision(th4, d4)
check(faux2.data.get(vs.RAG_KEY) is None, "clé vide + témoin présent → persistance refusée")

# 5) recall_similar dégrade en [] (sans lever) quand la lecture casse
faux2.data[vs.RAG_KEY] = json.dumps(recs)
faux2.lecture_echoue = True
try:
    out = vs.recall_similar(th1)
    check(out == [], "recall_similar renvoie [] proprement quand la lecture casse")
except Exception as e:
    check(False, f"recall_similar a levé : {e}")
faux2.lecture_echoue = False


# ═══════════════════════════════ BILAN ═══════════════════════════════
print(f"\n{'='*50}")
print(f"  RÉSULTAT : {VERT}{_ok} réussis{RESET}, "
      f"{ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}")
print(f"{'='*50}")
exit(1 if _ko else 0)