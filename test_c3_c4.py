"""
Banc de test à STUBS pour C3 (verrou distribué) + C4 (anti-écrasement).
Faux Redis en mémoire qui simule SET NX EX, GET, DELETE et EVAL (compare-and-delete).
Aucun réseau, aucune clé réelle.
"""
import os
import sys
import types
import importlib.util
from enum import Enum

# ── 1) Faux modules (identiques au banc C1/C2) ───────────────────────────────
fake_redis = types.ModuleType("upstash_redis")
class _FakeRedisImport:
    def __init__(self, *a, **k): pass
fake_redis.Redis = _FakeRedisImport
sys.modules["upstash_redis"] = fake_redis

fake_config = types.ModuleType("config")
fake_config_settings = types.ModuleType("config.settings")
class _Settings:
    starting_capital = 10_000.0
    max_position_pct = 15.0
    max_sector_pct = 40.0
fake_config_settings.settings = _Settings()
fake_config.settings = fake_config_settings
sys.modules["config"] = fake_config
sys.modules["config.settings"] = fake_config_settings

fake_src = types.ModuleType("src")
fake_ingestion = types.ModuleType("src.ingestion")
fake_market = types.ModuleType("src.ingestion.market_client")
fake_market.get_fundamentals = lambda t: {"price": 100.0}
sys.modules["src"] = fake_src
sys.modules["src.ingestion"] = fake_ingestion
sys.modules["src.ingestion.market_client"] = fake_market

fake_schemas = types.ModuleType("src.schemas")
fake_thesis_mod = types.ModuleType("src.schemas.thesis")
class Direction(str, Enum):
    LONG = "long"; SHORT = "short"
class MacroThesis: pass
fake_thesis_mod.Direction = Direction
fake_thesis_mod.MacroThesis = MacroThesis
fake_decision_mod = types.ModuleType("src.schemas.decision")
class PortfolioDecision: pass
fake_decision_mod.PortfolioDecision = PortfolioDecision
sys.modules["src.schemas"] = fake_schemas
sys.modules["src.schemas.thesis"] = fake_thesis_mod
sys.modules["src.schemas.decision"] = fake_decision_mod

# ── 2) Charger le fichier patché ─────────────────────────────────────────────
_ICI = os.path.dirname(os.path.abspath(__file__))
_CHEMIN_PP = os.path.join(_ICI, "src", "portfolio", "paper_portfolio.py")
spec = importlib.util.spec_from_file_location("pp", _CHEMIN_PP)
pp = importlib.util.module_from_spec(spec)
sys.modules["pp"] = pp
spec.loader.exec_module(pp)
pp.get_fundamentals = lambda t: {"price": 100.0}   # pour _nouveau_portefeuille

# ── 3) Faux Redis en mémoire ─────────────────────────────────────────────────
class FakeRedis:
    def __init__(self, fail_get=False):
        self.store = {}
        self.fail_get = fail_get
    def get(self, key):
        if self.fail_get:
            raise RuntimeError("réseau Redis HS (simulé)")
        return self.store.get(key)
    def set(self, key, value, nx=None, ex=None, **kw):
        if nx and key in self.store:
            return None            # NX : refuse si la clé existe déjà
        self.store[key] = value
        return "OK"
    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]; n += 1
        return n
    def eval(self, script, keys=None, args=None):   # simule le CAD de _LUA_LIBERER
        keys = keys or []; args = args or []
        if keys and self.store.get(keys[0]) == args[0]:
            del self.store[keys[0]]
            return 1
        return 0

echecs = []
def check(nom, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {nom}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: echecs.append(nom)

VK = pp.VERROU_KEY
PK = pp.PORTFOLIO_KEY
IK = pp.PORTFOLIO_INIT_KEY

# ─────────────────────────────────────────────────────────────────────────────
# C3 — VERROU
# ─────────────────────────────────────────────────────────────────────────────
print("\n=== C3 — acquisition sur verrou libre, puis libération ===")
pp._redis = FakeRedis()
with pp.verrou_portefeuille():
    check("verrou posé pendant le with", VK in pp._redis.store)
check("verrou libéré à la sortie", VK not in pp._redis.store)

print("\n=== C3 — contention : un 2e cycle ne peut PAS acquérir pendant que le 1er tient ===")
pp._redis = FakeRedis()
bloque = False
with pp.verrou_portefeuille():
    try:
        with pp.verrou_portefeuille(attente_max=0, intervalle=0):
            pass
    except pp.VerrouIndisponible:
        bloque = True
check("2e acquisition refusée (VerrouIndisponible)", bloque)
check("verrou libéré après le 1er cycle", VK not in pp._redis.store)

print("\n=== C3 — ré-acquisition possible après libération ===")
pp._redis = FakeRedis()
with pp.verrou_portefeuille():
    pass
reacquis = False
with pp.verrou_portefeuille(attente_max=0):
    reacquis = True
check("ré-acquisition réussie", reacquis)

print("\n=== C3 — LOCAL (pas de Redis) : verrou = no-op, aucune exception ===")
pp._redis = None
passe = False
with pp.verrou_portefeuille():
    passe = True
check("le with s'exécute normalement en local", passe)

# ─────────────────────────────────────────────────────────────────────────────
# C4 — ANTI-ÉCRASEMENT
# ─────────────────────────────────────────────────────────────────────────────
def portefeuille_json():
    return pp.Portfolio(starting_capital=10_000.0, cash=8_000.0).model_dump_json()

print("\n=== C4 — portefeuille existant : lu ET témoin d'init posé (auto-cicatrisation) ===")
pp._redis = FakeRedis()
pp._redis.store[PK] = portefeuille_json()      # portefeuille présent, PAS de témoin
p = pp.load_portfolio()
check("portefeuille chargé (cash=8000)", abs(p.cash - 8_000.0) < 1e-6)
check("témoin d'init posé rétroactivement", pp._redis.store.get(IK) == "1")

print("\n=== C4 — vrai premier run (rien en base) : création + sauvegarde ===")
pp._redis = FakeRedis()
p = pp.load_portfolio()
check("nouveau portefeuille créé", p is not None)
check("portefeuille sauvegardé", PK in pp._redis.store)
check("témoin d'init posé", pp._redis.store.get(IK) == "1")

print("\n=== C4 — clé vide MAIS témoin présent : anomalie → REFUS de recréer ===")
pp._redis = FakeRedis()
pp._redis.store[IK] = "1"                       # a déjà existé, mais 'portfolio' a disparu
leve = False
try:
    pp.load_portfolio()
except pp.LectureStockageErreur:
    leve = True
check("LectureStockageErreur levée", leve)
check("AUCUN portefeuille neuf écrit par-dessus", PK not in pp._redis.store)

print("\n=== C4 — lecture Redis qui ÉCHOUE : refus d'écrire (pas d'écrasement) ===")
pp._redis = FakeRedis(fail_get=True)
pp._redis.store[PK] = "PORTEFEUILLE_HISTORIQUE"  # doit rester intact
leve = False
try:
    pp.load_portfolio()
except pp.LectureStockageErreur:
    leve = True
check("LectureStockageErreur levée sur lecture ratée", leve)
check("historique NON écrasé", pp._redis.store.get(PK) == "PORTEFEUILLE_HISTORIQUE")

print("\n=== C4 — save_portfolio pose bien les DEUX clés ===")
pp._redis = FakeRedis()
pp.save_portfolio(pp.Portfolio(starting_capital=10_000.0, cash=10_000.0))
check("clé portfolio écrite", PK in pp._redis.store)
check("témoin d'init écrit", pp._redis.store.get(IK) == "1")

print("\n" + ("🎉 TOUS LES TESTS PASSENT" if not echecs else f"⚠️ ÉCHECS : {echecs}"))
sys.exit(1 if echecs else 0)