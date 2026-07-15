"""
Harnais C6 — reproduit la panne du 08:00 : yfinance renvoie None SANS lever d'exception
('NoneType' object has no attribute 'get') → check_exits ET snapshot tombaient.
Auto-contenu. Aucun réseau.
"""
import sys, types

fake_cfg = types.ModuleType("config.settings")
class _S:
    starting_capital = 10_000.0
    max_position_pct = 15.0
    max_sector_pct = 100.0
    cost_bps_per_side = 10.0
    cost_bps_per_side_smallcap = 30.0
    smallcap_cap_threshold = 2_000_000_000.0
    risk_sizing_actif = False
    max_position_risk_pct = 2.0
    atr_stop_multiple = 1.0
    min_ticket_usd = 100.0
    correlation_active = False
    killswitch_actif = False
    max_drawdown_pct = 15.0
    killswitch_reprise_pct = 10.0
fake_cfg.settings = _S()
sys.modules["config.settings"] = fake_cfg

VERT, ROUGE, RESET = "\033[92m", "\033[91m", "\033[0m"
_ok = _ko = 0
def check(nom, cond, detail=""):
    global _ok, _ko
    if cond: _ok += 1; print(f"  {VERT}\u2713{RESET} {nom}")
    else: _ko += 1; print(f"  {ROUGE}\u2717 \u00c9CHEC{RESET} {nom}  {detail}")

print("=== 1) LE VRAI TEST : get_fundamentals face à un yfinance qui renvoie None ===")
# On simule yfinance : .info renvoie None SANS lever (exactement le cas du throttling 08:00)
faux_yf = types.ModuleType("yfinance")
class _TickerNone:
    def __init__(self, *a, **k): pass
    @property
    def info(self): return None          # ← LE BUG : None, pas d'exception
    def history(self, **k):
        import pandas as pd
        return pd.DataFrame()
faux_yf.Ticker = _TickerNone
faux_yf.download = lambda *a, **k: None
sys.modules["yfinance"] = faux_yf

import src.ingestion.market_client as mc
try:
    res = mc.get_fundamentals("AAPL")
    leve = False
except AttributeError as e:
    leve = True
    res = None
check("get_fundamentals NE LÈVE PLUS sur .info = None (le bug du 08:00)", not leve,
      "AttributeError: NoneType has no attribute 'get'")
check("renvoie un dict 'indisponible' propre",
      isinstance(res, dict) and res.get("price") is None and res["data_source"] == "indisponible",
      str(res)[:80] if res else "None")

print("\n=== 2) Le pipeline SURVIT : check_exits et snapshot ne tombent plus ===")
# market_client stubé pour renvoyer None (pire cas : la fonction elle-même renvoie None)
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = lambda t, utiliser_cache=True: None      # ← pire cas absolu
fake_mc.get_seance_ohlc = lambda t: None                            # ← pire cas absolu
fake_mc.get_atr = lambda t, periode=14: None
fake_mc.get_correlations = lambda c, d, jours=90, min_obs=40: {"ok": False}
fake_mc.get_open_apres = lambda t, i: {"pret": False}
sys.modules["src.ingestion.market_client"] = fake_mc

import src.portfolio.paper_portfolio as pp
p = pp.Portfolio(starting_capital=10_000.0, cash=5_000.0)
p.positions.append(pp.Position(
    ticker="META", shares=10.0, entry_price=669.21, stop_loss=600.0,
    profit_target=750.0, conviction=0.6, sector="Tech", horizon_days=30))

try:
    alerts = pp.check_exits(p)
    leve = False
except Exception as e:
    leve = True; alerts = str(e)
check("check_exits SURVIT quand toutes les données sont None (plus de crash)",
      not leve, str(alerts))
check("aucune sortie déclenchée sur données absentes (on ne vend pas à l'aveugle)",
      not leve and alerts == [] and len(p.positions) == 1)

try:
    txt = pp.snapshot_text(p)
    leve2 = False
except Exception as e:
    leve2 = True; txt = str(e)
check("snapshot_text SURVIT (plus de crash)", not leve2, txt[:60])

try:
    eq = pp.equity_courante(p)
    leve3 = False
except Exception as e:
    leve3 = True; eq = str(e)
check("equity_courante SURVIT (repli sur le prix d'entrée)", not leve3, str(eq))

print("\n=== 3) Cache OHLC : un seul appel yfinance par ticker et par processus ===")
import importlib
sys.modules.pop("src.ingestion.market_client")
appels = {"n": 0}
class _TickerOK:
    def __init__(self, *a, **k): pass
    @property
    def info(self): return {"currentPrice": 100.0, "marketCap": 3e12, "sector": "Tech"}
    def history(self, **k):
        import pandas as pd
        appels["n"] += 1
        idx = pd.date_range("2026-07-08", periods=3, freq="D", tz="America/New_York")
        return pd.DataFrame({"Open": [99.0, 100.0, 101.0], "High": [102.0]*3,
                             "Low": [98.0]*3, "Close": [100.0]*3}, index=idx)
faux_yf.Ticker = _TickerOK
import src.ingestion.market_client as mc2
importlib.reload(mc2)
mc2.get_seance_ohlc("META"); mc2.get_seance_ohlc("META"); mc2.get_seance_ohlc("META")
check("3 appels get_seance_ohlc('META') → 1 SEUL appel yfinance (cache)",
      appels["n"] == 1, f"{appels['n']} appels")

print(f"\n{'='*56}\n  RÉSULTAT C6 : {VERT}{_ok} réussis{RESET}, "
      f"{ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*56}")
exit(1 if _ko else 0)