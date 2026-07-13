"""
Banc de test à STUBS pour le correctif C1 + C2 de record_decision.
On isole record_decision de Redis, yfinance et config en injectant de faux modules
AVANT de charger le fichier patché, puis on remplace load/save/check_exits/get_fundamentals.
Aucun réseau, aucune clé, aucune dépendance cloud.
"""
import os
import sys
import types
import importlib.util
from enum import Enum

# ─────────────────────────────────────────────────────────────────────────────
# 1) Faux modules injectés dans sys.modules AVANT de charger paper_portfolio_patched
# ─────────────────────────────────────────────────────────────────────────────

# upstash_redis.Redis  (jamais instancié : pas de variables d'env → _redis = None)
fake_redis = types.ModuleType("upstash_redis")
class _FakeRedis:  # pragma: no cover
    def __init__(self, *a, **k): pass
fake_redis.Redis = _FakeRedis
sys.modules["upstash_redis"] = fake_redis

# config.settings.settings
fake_config = types.ModuleType("config")
fake_config_settings = types.ModuleType("config.settings")
class _Settings:
    starting_capital = 100_000.0
    max_position_pct = 20.0
    max_sector_pct = 40.0
fake_config_settings.settings = _Settings()
fake_config.settings = fake_config_settings
sys.modules["config"] = fake_config
sys.modules["config.settings"] = fake_config_settings

# src.ingestion.market_client.get_fundamentals  (stub par défaut ; on le remplacera par test)
fake_src = types.ModuleType("src")
fake_ingestion = types.ModuleType("src.ingestion")
fake_market = types.ModuleType("src.ingestion.market_client")
def _default_get_fundamentals(ticker):  # remplacé dans chaque test
    return {"price": 100.0}
fake_market.get_fundamentals = _default_get_fundamentals
sys.modules["src"] = fake_src
sys.modules["src.ingestion"] = fake_ingestion
sys.modules["src.ingestion.market_client"] = fake_market

# src.schemas.thesis  (Direction RÉEL + MacroThesis factice, juste pour l'import)
fake_schemas = types.ModuleType("src.schemas")
fake_thesis_mod = types.ModuleType("src.schemas.thesis")
class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
class MacroThesis:  # placeholder : seulement utilisé comme annotation
    pass
fake_thesis_mod.Direction = Direction
fake_thesis_mod.MacroThesis = MacroThesis
fake_decision_mod = types.ModuleType("src.schemas.decision")
class PortfolioDecision:  # placeholder : annotation seulement
    pass
fake_decision_mod.PortfolioDecision = PortfolioDecision
sys.modules["src.schemas"] = fake_schemas
sys.modules["src.schemas.thesis"] = fake_thesis_mod
sys.modules["src.schemas.decision"] = fake_decision_mod

# ─────────────────────────────────────────────────────────────────────────────
# 2) Charger le fichier PATCHÉ comme module "pp"
# ─────────────────────────────────────────────────────────────────────────────
_ICI = os.path.dirname(os.path.abspath(__file__))
_CHEMIN_PP = os.path.join(_ICI, "src", "portfolio", "paper_portfolio.py")
spec = importlib.util.spec_from_file_location("pp", _CHEMIN_PP)
pp = importlib.util.module_from_spec(spec)
sys.modules["pp"] = pp
spec.loader.exec_module(pp)

# ─────────────────────────────────────────────────────────────────────────────
# 3) Neutraliser les E/S : load renvoie un portefeuille neuf, save capture, pas de sorties
# ─────────────────────────────────────────────────────────────────────────────
_SAVED = {"p": None}
def _fresh_portfolio():
    return pp.Portfolio(starting_capital=100_000.0, cash=100_000.0)
pp.load_portfolio = lambda: _CURRENT["p"]
pp.save_portfolio = lambda p: _SAVED.__setitem__("p", p)
pp.check_exits = lambda p: []

_CURRENT = {"p": None}

# ─────────────────────────────────────────────────────────────────────────────
# 4) Petits constructeurs d'objets (pas de pydantic : de simples porteurs d'attributs)
# ─────────────────────────────────────────────────────────────────────────────
class _Obj:
    def __init__(self, **kw): self.__dict__.update(kw)

def make_thesis(direction=Direction.LONG, sector="Énergie"):
    return _Obj(direction=direction, sector=sector, theme="Thème de test",
                thesis_id="TH-TEST", time_horizon_days=30)

def make_pos(ticker="XOM", entry=100.0, stop=95.0, target=115.0,
             inval=93.0, size=10.0):
    return _Obj(ticker=ticker, position_size_pct=size, entry_price=entry,
                stop_loss=stop, profit_target=target, invalidation_price=inval,
                conviction=0.7, rationale="raison de test")

def make_decision(positions, action="execute"):
    return _Obj(action=_Obj(value=action), positions=positions)

def set_price(price):
    pp.get_fundamentals = (lambda t: {"price": price})

def run(thesis, decision, price):
    """Prépare un portefeuille neuf, fixe le prix marché, exécute record_decision."""
    _CURRENT["p"] = _fresh_portfolio()
    _SAVED["p"] = None
    set_price(price)
    log = pp.record_decision(thesis, decision)
    return log, _SAVED["p"]

# ─────────────────────────────────────────────────────────────────────────────
# 5) Tests
# ─────────────────────────────────────────────────────────────────────────────
echecs = []
def check(nom, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {nom}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        echecs.append(nom)

print("\n=== C2 — thèse SHORT refusée (pas d'inversion en achat LONG) ===")
log, saved = run(make_thesis(direction=Direction.SHORT), make_decision([make_pos()]), price=100.5)
check("aucune position ouverte", len(saved.positions) == 0)
check("journal explique le refus SHORT", any("SHORT" in l and "refus" in l.lower() for l in log),
      detail=str(log))

print("\n=== C2 — plan incohérent : stop AU-DESSUS de l'entrée ===")
log, saved = run(make_thesis(), make_decision([make_pos(entry=100, stop=105, target=115, inval=93)]), price=100.0)
check("aucune position ouverte", len(saved.positions) == 0)
check("journal signale l'incohérence", any("incohérent" in l for l in log), detail=str(log))

print("\n=== C2 — plan incohérent : objectif SOUS l'entrée ===")
log, saved = run(make_thesis(), make_decision([make_pos(entry=100, stop=95, target=90, inval=93)]), price=100.0)
check("aucune position ouverte", len(saved.positions) == 0)

print("\n=== C2 — plan incohérent : invalidation AU-DESSUS de l'entrée ===")
log, saved = run(make_thesis(), make_decision([make_pos(entry=100, stop=95, target=115, inval=104)]), price=100.0)
check("aucune position ouverte", len(saved.positions) == 0)

print("\n=== C1 — dérive marché > tolérance (1.5%) : entrée refusée ===")
log, saved = run(make_thesis(), make_decision([make_pos(entry=100.0)]), price=103.0)  # +3%
check("aucune position ouverte", len(saved.positions) == 0)
check("journal chiffre la dérive", any("bougé" in l for l in log), detail=str(log))

print("\n=== C1 — prix marché indisponible : entrée refusée ===")
log, saved = run(make_thesis(), make_decision([make_pos(entry=100.0)]), price=None)
check("aucune position ouverte", len(saved.positions) == 0)
check("journal signale le prix indispo", any("indisponible" in l for l in log), detail=str(log))

print("\n=== C1 — cas nominal : fill au PRIX RÉEL, pas au prix du LLM ===")
# Plan LLM à 100$, marché réel à 100.5$ (dérive 0.5% < 1.5%) → on entre à 100.5$.
log, saved = run(make_thesis(), make_decision([make_pos(entry=100.0)]), price=100.5)
check("R1b — exactement 1 ORDRE placé (fill à l'ouverture suivante)",
      len(saved.pending) == 1 and len(saved.positions) == 0, detail=str(log))
if saved.pending:
    o = saved.pending[0]
    check("R1b — l'ordre porte le bon ticker et la bonne taille",
          o.ticker == "XOM" and abs(o.size_pct - 10.0) < 1e-9, detail=f"{o.ticker} {o.size_pct}")
    check("R1b — stop/objectif/invalidation du plan conservés dans l'ordre",
          o.stop_loss == 95.0 and o.profit_target == 115.0 and o.invalidation_price == 93.0)
    check("R1b — aucun fill immédiat : le cash n'a PAS bougé au placement",
          abs(saved.cash - 100_000.0) < 1e-9, detail=f"cash={saved.cash}")

print("\n=== C1 — dérive juste SOUS la tolérance (1.4%) : on entre quand même ===")
log, saved = run(make_thesis(), make_decision([make_pos(entry=100.0)]), price=101.4)  # +1.4%
check("R1b — ordre placé (dérive tolérée)", len(saved.pending) == 1, detail=str(log))

print("\n" + ("🎉 TOUS LES TESTS PASSENT" if not echecs else f"⚠️ ÉCHECS : {echecs}"))
sys.exit(1 if echecs else 0)