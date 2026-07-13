"""
Harnais R1c — stops/objectifs testés contre le HIGH/LOW de la séance (intraday),
avec slippage de gap. Auto-contenu : stubs config.settings + market_client.
Aucun réseau, aucune clé, aucun Redis.
"""
import sys, types
from datetime import datetime, timezone, timedelta

# ── Stub config.settings ──
fake_cfg = types.ModuleType("config.settings")
class _Settings:
    starting_capital = 10_000.0
    max_position_pct = 100.0
    max_sector_pct = 100.0
    cost_bps_per_side = 10.0
    cost_bps_per_side_smallcap = 30.0
    smallcap_cap_threshold = 2_000_000_000.0
fake_cfg.settings = _Settings()
sys.modules["config.settings"] = fake_cfg

# ── Stub market_client (get_fundamentals + get_seance_ohlc + get_open_apres) ──
faux_prix = {}    # ticker -> dernier prix (repli)
faux_ohlc = {}    # ticker -> {"ok":True,"open":..,"high":..,"low":..,"close":..}
def _fake_get_fundamentals(ticker, utiliser_cache=True):
    return {"price": faux_prix.get(ticker), "market_cap": 3_000_000_000_000,
            "sector": "Technology", "ticker": ticker.upper()}
def _fake_get_seance_ohlc(ticker):
    return faux_ohlc.get(ticker, {"ok": False, "raison": "stub: pas d'OHLC"})
def _fake_get_open_apres(ticker, iso):
    return {"pret": False, "raison": "stub"}
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = _fake_get_fundamentals
fake_mc.get_seance_ohlc = _fake_get_seance_ohlc
fake_mc.get_open_apres = _fake_get_open_apres
sys.modules["src.ingestion.market_client"] = fake_mc

import src.portfolio.paper_portfolio as pp

VERT, ROUGE, RESET = "\033[92m", "\033[91m", "\033[0m"
_ok = _ko = 0
def check(nom, cond, detail=""):
    global _ok, _ko
    if cond: _ok += 1; print(f"  {VERT}\u2713{RESET} {nom}")
    else: _ko += 1; print(f"  {ROUGE}\u2717 \u00c9CHEC{RESET} {nom}  {detail}")

def porte(ticker="AAPL", entry=100.0, stop=95.0, cible=115.0, inval=93.0, horizon=30, age_j=1):
    """Portefeuille avec 1 position ouverte."""
    p = pp.Portfolio(starting_capital=10_000.0, cash=5_000.0)
    p.positions.append(pp.Position(
        ticker=ticker, shares=50.0, entry_price=entry, stop_loss=stop,
        profit_target=cible, invalidation_price=inval, conviction=0.6,
        sector="Technology", horizon_days=horizon, entry_cost=5.0,
        opened_at=(datetime.now(timezone.utc) - timedelta(days=age_j)).isoformat(),
    ))
    return p

# ═════ 1) LE BUG CORRIGÉ : stop touché en INTRADAY mais clôture au-dessus ═════
# Le titre plonge à 94 (sous le stop 95) puis rebondit et clôture à 99.
# ANCIEN comportement (close only) : rien ne se passe → paper flatté.
# R1c : le stop DÉCLENCHE, fill au niveau du stop (95).
p = porte()
faux_ohlc["AAPL"] = {"ok": True, "open": 99.5, "high": 100.0, "low": 94.0, "close": 99.0}
faux_prix["AAPL"] = 99.0
alerts = pp.check_exits(p)
check("stop touché en séance (low 94 < stop 95) → position FERMÉE malgré close 99",
      len(p.positions) == 0 and len(p.closed) == 1, str(alerts))
if p.closed:
    check("fill AU niveau du stop (95.0), pas au close (99.0)",
          abs(p.closed[-1].exit_price - 95.0) < 1e-9, f"{p.closed[-1].exit_price}")
    check("motif = stop_loss", p.closed[-1].exit_reason == "stop_loss")

# ═════ 2) GAP AU TRAVERS : ouverture déjà sous le stop → fill à l'OPEN (pire) ═════
# Mauvaise nouvelle overnight : ouvre à 90 (sous le stop 95). En réel on sort à 90, pas 95.
p = porte()
faux_ohlc["AAPL"] = {"ok": True, "open": 90.0, "high": 91.0, "low": 88.0, "close": 89.0}
alerts = pp.check_exits(p)
check("gap au travers → fill à l'OUVERTURE (90.0), PAS au stop (95.0)",
      p.closed and abs(p.closed[-1].exit_price - 90.0) < 1e-9,
      f"{p.closed[-1].exit_price if p.closed else 'aucune sortie'}")
check("journal signale le GAP", any("GAP" in a for a in alerts), str(alerts))

# ═════ 3) Stop NON touché → position conservée ═════
p = porte()
faux_ohlc["AAPL"] = {"ok": True, "open": 99.0, "high": 101.0, "low": 96.0, "close": 100.0}
alerts = pp.check_exits(p)
check("low 96 > stop 95 → position CONSERVÉE", len(p.positions) == 1 and not alerts)

# ═════ 4) OBJECTIF touché en séance puis reperdu → encaissé quand même ═════
p = porte()
faux_ohlc["AAPL"] = {"ok": True, "open": 110.0, "high": 116.0, "low": 109.0, "close": 111.0}
alerts = pp.check_exits(p)
check("high 116 ≥ cible 115 → OBJECTIF encaissé malgré close 111",
      p.closed and p.closed[-1].exit_reason == "profit_target", str(alerts))
check("fill AU niveau de l'objectif (115.0)",
      p.closed and abs(p.closed[-1].exit_price - 115.0) < 1e-9,
      f"{p.closed[-1].exit_price if p.closed else '-'}")

# ═════ 5) PRIORITÉ CONSERVATRICE : stop ET objectif touchés le même jour → STOP gagne ═════
# Séance folle : low 94 (stop) ET high 116 (cible). Sur une barre journalière on ignore
# l'ordre chronologique → on suppose le PIRE.
p = porte()
faux_ohlc["AAPL"] = {"ok": True, "open": 100.0, "high": 116.0, "low": 94.0, "close": 105.0}
alerts = pp.check_exits(p)
check("stop ET cible touchés → le STOP l'emporte (hypothèse conservatrice)",
      p.closed and p.closed[-1].exit_reason == "stop_loss", str(alerts))

# ═════ 6) INVALIDATION (sous le stop) : si le stop est absent, l'invalidation joue ═════
p = porte(stop=0.0)          # pas de stop, invalidation à 93
faux_ohlc["AAPL"] = {"ok": True, "open": 96.0, "high": 97.0, "low": 92.0, "close": 95.0}
alerts = pp.check_exits(p)
check("low 92 < invalidation 93 → thèse INVALIDÉE (fill à 93)",
      p.closed and p.closed[-1].exit_reason == "these_invalidee"
      and abs(p.closed[-1].exit_price - 93.0) < 1e-9, str(alerts))

# ═════ 7) HORIZON : évalué à la CLÔTURE (pas un ordre au marché) ═════
p = porte(horizon=10, age_j=20)     # échéance dépassée
faux_ohlc["AAPL"] = {"ok": True, "open": 98.0, "high": 99.0, "low": 96.0, "close": 97.0}
alerts = pp.check_exits(p)
check("horizon dépassé + close 97 < entrée 100 → sortie au CLOSE (97.0)",
      p.closed and p.closed[-1].exit_reason == "horizon_expire"
      and abs(p.closed[-1].exit_price - 97.0) < 1e-9, str(alerts))

# Position GAGNANTE à l'échéance → on la laisse courir
p = porte(horizon=10, age_j=20)
faux_ohlc["AAPL"] = {"ok": True, "open": 108.0, "high": 110.0, "low": 107.0, "close": 109.0}
alerts = pp.check_exits(p)
check("horizon dépassé mais GAGNANTE (close 109 > entrée 100) → on laisse courir",
      len(p.positions) == 1 and not alerts)

# ═════ 8) REPLI : OHLC indisponible → ancien test au dernier prix (dégradation propre) ═════
p = porte()
faux_ohlc.pop("AAPL", None)          # pas d'OHLC
faux_prix["AAPL"] = 94.0             # dernier prix sous le stop
alerts = pp.check_exits(p)
check("OHLC indisponible → repli sur dernier prix (stop 94 ≤ 95 → sortie)",
      p.closed and p.closed[-1].exit_reason == "stop_loss", str(alerts))

# OHLC indisponible ET prix indisponible → on ne fait rien, sans exception
p = porte()
faux_prix["AAPL"] = None
try:
    alerts = pp.check_exits(p)
    check("aucune donnée → position conservée, aucune exception", len(p.positions) == 1)
except Exception as e:
    check("aucune donnée → aucune exception", False, str(e))

# ═════ 9) Les frais R1a sont bien débités sur une sortie R1c ═════
p = porte()
faux_ohlc["AAPL"] = {"ok": True, "open": 99.0, "high": 100.0, "low": 94.0, "close": 98.0}
cash_avant = p.cash
pp.check_exits(p)
# 50 actions @ 95 = 4750 ; frais 10 bps = 4.75$ → cash += 4750 - 4.75
check("frais R1a débités à la sortie intraday (4.75$)",
      abs(p.cash - (cash_avant + 4750.0 - 4.75)) < 1e-9, f"{p.cash}")
check("exit_cost enregistré", p.closed and abs(p.closed[-1].exit_cost - 4.75) < 1e-9,
      f"{p.closed[-1].exit_cost if p.closed else '-'}")

print(f"\n{'='*54}\n  RÉSULTAT R1c : {VERT}{_ok} réussis{RESET}, "
      f"{ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*54}")
exit(1 if _ko else 0)