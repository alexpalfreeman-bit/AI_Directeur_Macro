"""
Banc de test à STUBS pour S2 (dimensionnement sur l'équity courante) + S4 (plafond
secteur en valeur de marché + secteur yfinance). Faux modules, aucun réseau.
"""
import os
import sys
import types
import importlib.util
from enum import Enum

# ── Faux modules ─────────────────────────────────────────────────────────────
fake_redis = types.ModuleType("upstash_redis")
class _FakeRedis:
    def __init__(self, *a, **k): pass
fake_redis.Redis = _FakeRedis
sys.modules["upstash_redis"] = fake_redis

fake_config = types.ModuleType("config"); fake_config_settings = types.ModuleType("config.settings")
class _Settings:
    starting_capital = 8_000.0     # volontairement != équity des tests, pour distinguer S2
    max_position_pct = 15.0
    max_sector_pct = 40.0
fake_config_settings.settings = _Settings()
fake_config.settings = fake_config_settings
sys.modules["config"] = fake_config; sys.modules["config.settings"] = fake_config_settings

fake_src = types.ModuleType("src"); fake_src.__path__ = []
fake_ing = types.ModuleType("src.ingestion"); fake_ing.__path__ = []
fake_market = types.ModuleType("src.ingestion.market_client")
fake_market.get_fundamentals = lambda t, *a, **k: {"price": None, "sector": None}
sys.modules["src"] = fake_src
sys.modules["src.ingestion"] = fake_ing
sys.modules["src.ingestion.market_client"] = fake_market

fake_schemas = types.ModuleType("src.schemas")
fake_thesis_mod = types.ModuleType("src.schemas.thesis")
class Direction(str, Enum):
    LONG = "long"; SHORT = "short"
class MacroThesis: pass
fake_thesis_mod.Direction = Direction; fake_thesis_mod.MacroThesis = MacroThesis
fake_decision_mod = types.ModuleType("src.schemas.decision")
class PortfolioDecision: pass
fake_decision_mod.PortfolioDecision = PortfolioDecision
sys.modules["src.schemas"] = fake_schemas
sys.modules["src.schemas.thesis"] = fake_thesis_mod
sys.modules["src.schemas.decision"] = fake_decision_mod

# ── Charger le fichier patché ────────────────────────────────────────────────
_ICI = os.path.dirname(os.path.abspath(__file__))
_CHEMIN_PP = os.path.join(_ICI, "src", "portfolio", "paper_portfolio.py")
spec = importlib.util.spec_from_file_location("pp", _CHEMIN_PP)
pp = importlib.util.module_from_spec(spec)
sys.modules["pp"] = pp
spec.loader.exec_module(pp)

# ── Stub prix/secteur par ticker ─────────────────────────────────────────────
PRIX = {}
pp.get_fundamentals = lambda t, *a, **k: PRIX.get(t.upper(), {"price": None, "sector": None})

echecs = []
def check(nom, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {nom}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: echecs.append(nom)

def pos(ticker, shares, entry, sector):
    return pp.Position(ticker=ticker, shares=shares, entry_price=entry, sector=sector, conviction=0.6)

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== S2 — dimensionnement sur l'ÉQUITY courante, pas le capital de départ ===")
# Portefeuille : cash 5000 + XOM (50 actions @ coût 60 = 3000, mais prix courant 100 → marché 5000)
# → équity = 5000 + 5000 = 10000  (≠ starting_capital 8000)
PRIX.clear(); PRIX["XOM"] = {"price": 100.0, "sector": "Energy"}
p = pp.Portfolio(starting_capital=8_000.0, cash=5_000.0)
p.positions.append(pos("XOM", 50, 60.0, "Energy"))
check("equity_courante = 10000 (cash + valeur marché)", abs(pp.equity_courante(p) - 10_000.0) < 1e-6,
      detail=str(pp.equity_courante(p)))
msg = pp.buy(p, "MSFT", price=50.0, size_pct=10.0, stop_loss=45.0, profit_target=60.0, sector="Technology")
msft = next((x for x in p.positions if x.ticker == "MSFT"), None)
check("MSFT ouvert", msft is not None, detail=msg)
if msft:
    invest = msft.shares * msft.entry_price
    check("investi = 10% de l'ÉQUITY (1000), pas 10% du capital départ (800)",
          abs(invest - 1_000.0) < 1e-6, detail=f"investi={invest}")

print("\n=== S4 — plafond secteur en VALEUR DE MARCHÉ (pas coût d'entrée) ===")
# XOM : coût 3000, marché 5000. Équity = 5000 cash + 5000 = 10000. Plafond Energy = 40% = 4000.
# Expo marché (5000) > 4000 → tout nouvel achat Energy REFUSÉ. (Sur base coût 3000, il passerait.)
PRIX.clear(); PRIX["XOM"] = {"price": 100.0, "sector": "Energy"}
p = pp.Portfolio(starting_capital=8_000.0, cash=5_000.0)
p.positions.append(pos("XOM", 50, 60.0, "Energy"))
msg = pp.buy(p, "CVX", price=100.0, size_pct=10.0, stop_loss=90.0, profit_target=120.0, sector="Energy")
check("nouvel achat Energy refusé (plafond atteint en valeur de marché)", "Plafond secteur" in msg, detail=msg)
check("aucune position CVX ajoutée", all(x.ticker != "CVX" for x in p.positions))

print("\n=== S4 — contre-épreuve : sur base de COÛT, l'achat serait passé ===")
# Preuve que c'est bien la valeur de marché qui bloque : si XOM valait son coût (60), marché=3000<4000.
PRIX.clear(); PRIX["XOM"] = {"price": 60.0, "sector": "Energy"}   # marché == coût
p = pp.Portfolio(starting_capital=8_000.0, cash=5_000.0)
p.positions.append(pos("XOM", 50, 60.0, "Energy"))
# équity = 5000 + 3000 = 8000 ; plafond Energy = 3200 ; expo 3000 → headroom 200 > 0 → petit achat OK
msg = pp.buy(p, "CVX", price=100.0, size_pct=10.0, stop_loss=90.0, profit_target=120.0, sector="Energy")
check("achat Energy accepté quand marché == coût (headroom > 0)", any(x.ticker == "CVX" for x in p.positions),
      detail=msg)

# ── record_decision : résolution du VRAI secteur ─────────────────────────────
class _Obj:
    def __init__(self, **kw): self.__dict__.update(kw)

def make_thesis(sector, direction=Direction.LONG):
    return _Obj(direction=direction, sector=sector, theme="Thème test",
                thesis_id="TH", time_horizon_days=30)

def make_pos(ticker, entry=100.0, stop=95.0, target=115.0, inval=93.0, size=10.0):
    return _Obj(ticker=ticker, position_size_pct=size, entry_price=entry, stop_loss=stop,
                profit_target=target, invalidation_price=inval, conviction=0.7, rationale="r")

def make_decision(positions):
    return _Obj(action=_Obj(value="execute"), positions=positions)

_SAVED = {"p": None}
pp.save_portfolio = lambda p: _SAVED.__setitem__("p", p)
pp.check_exits = lambda p: []

def run_record(thesis, decision, prix_map):
    PRIX.clear(); PRIX.update({k.upper(): v for k, v in prix_map.items()})
    pp.load_portfolio = lambda: pp.Portfolio(starting_capital=100_000.0, cash=100_000.0)
    _SAVED["p"] = None
    pp.record_decision(thesis, decision)
    return _SAVED["p"]

print("\n=== S4 — record_decision stocke le secteur yfinance (Energy), pas le texte LLM ===")
saved = run_record(make_thesis("énergie"), make_decision([make_pos("XOM")]),
                   {"XOM": {"price": 100.0, "sector": "Energy"}})
check("1 position ouverte", len(saved.positions) == 1)
if saved.positions:
    check("secteur stocké = 'Energy' (yfinance), pas 'énergie' (LLM)",
          saved.positions[0].sector == "Energy", detail=saved.positions[0].sector)

print("\n=== S4 — repli : si yfinance n'a pas de secteur, on normalise le texte LLM ===")
saved = run_record(make_thesis("énergie"), make_decision([make_pos("ZZZ")]),
                   {"ZZZ": {"price": 100.0, "sector": None}})
check("secteur = 'Énergie' (texte LLM normalisé en title-case)",
      saved.positions and saved.positions[0].sector == "Énergie",
      detail=saved.positions[0].sector if saved.positions else "aucune position")

print("\n" + ("🎉 TOUS LES TESTS PASSENT" if not echecs else f"⚠️ ÉCHECS : {echecs}"))
sys.exit(1 if echecs else 0)