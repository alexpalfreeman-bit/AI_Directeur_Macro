"""
Harnais R1b — fills au prix d'OUVERTURE de la prochaine séance.
Auto-contenu : stubs config.settings + market_client (get_fundamentals ET get_open_apres).
Aucun réseau, aucune clé, aucun Redis.
"""
import sys, types
from datetime import datetime, timezone, timedelta

# ── 1) Stub config.settings ──
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

# ── 2) Stub market_client ──
faux_marche = {}     # ticker -> {"price","market_cap","sector"}
faux_opens = {}      # ticker -> {"pret":bool, "open":float, "date":str, "raison":str}
def _fake_get_fundamentals(ticker, utiliser_cache=True):
    d = faux_marche.get(ticker, {})
    return {"price": d.get("price"), "market_cap": d.get("market_cap"),
            "sector": d.get("sector"), "ticker": ticker.upper()}
def _fake_get_open_apres(ticker, date_ordre_iso):
    return faux_opens.get(ticker, {"pret": False, "raison": "aucune séance (stub)"})
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = _fake_get_fundamentals
fake_mc.get_open_apres = _fake_get_open_apres
sys.modules["src.ingestion.market_client"] = fake_mc

import src.portfolio.paper_portfolio as pp

VERT, ROUGE, RESET = "\033[92m", "\033[91m", "\033[0m"
_ok = _ko = 0
def check(nom, cond, detail=""):
    global _ok, _ko
    if cond: _ok += 1; print(f"  {VERT}\u2713{RESET} {nom}")
    else: _ko += 1; print(f"  {ROUGE}\u2717 \u00c9CHEC{RESET} {nom}  {detail}")

def ordre(ticker, plan=100.0, size=20.0, heures_ago=1.0):
    return pp.PendingOrder(
        ticker=ticker, size_pct=size, plan_price=plan,
        stop_loss=plan*0.9, profit_target=plan*1.2, invalidation_price=plan*0.85,
        conviction=0.6, sector="Technology", horizon_days=30,
        thesis_id="t1", thesis_summary="test",
        placed_at=(datetime.now(timezone.utc) - timedelta(hours=heures_ago)).isoformat(),
    )

faux_marche["AAPL"] = {"price": 100.0, "market_cap": 3_000_000_000_000, "sector": "Technology"}

# ═══════ 1) Ordre placé, AUCUNE séance ouverte → reste en attente, rien n'est acheté ═══════
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
p.pending = [ordre("AAPL")]
faux_opens["AAPL"] = {"pret": False, "raison": "marché fermé"}
log = pp.executer_ordres_en_attente(p)
check("aucune séance → ordre RESTE en attente", len(p.pending) == 1)
check("aucune séance → aucune position ouverte", len(p.positions) == 0)
check("aucune séance → cash intact", abs(p.cash - 10_000.0) < 1e-9, f"{p.cash}")

# ═══════ 2) Séance ouverte → fill au prix d'OUVERTURE (pas au close/plan) ═══════
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
p.pending = [ordre("AAPL", plan=100.0, size=20.0)]
faux_opens["AAPL"] = {"pret": True, "open": 101.0, "date": "2026-07-10"}   # gap +1%
log = pp.executer_ordres_en_attente(p)
check("séance ouverte → ordre retiré de la file", len(p.pending) == 0)
check("séance ouverte → position ouverte", len(p.positions) == 1, str(log))
if p.positions:
    pos = p.positions[0]
    check("fill au prix d'OUVERTURE (101.0), PAS au plan (100.0)",
          abs(pos.entry_price - 101.0) < 1e-9, f"{pos.entry_price}")
    check("prix du plan (100.0) N'EST PAS utilisé comme fill", abs(pos.entry_price - 100.0) > 1e-6)
    # dollars = 20% * 10000 = 2000 → shares = 2000/101
    check("shares calculées sur l'open réel", abs(pos.shares - round(2000.0/101.0, 4)) < 1e-6,
          f"{pos.shares}")
    check("frais R1a débités au fill (10 bps → 2.00$)", abs(pos.entry_cost - 2.00) < 1e-9,
          f"{pos.entry_cost}")

# ═══════ 3) GAP au-delà de la tolérance → ordre ANNULÉ (thèse décalibrée) ═══════
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
p.pending = [ordre("AAPL", plan=100.0)]
faux_opens["AAPL"] = {"pret": True, "open": 130.0, "date": "2026-07-10"}   # gap +30%
log = pp.executer_ordres_en_attente(p)
check("gap > tolérance → ordre ANNULÉ", len(p.pending) == 0 and len(p.positions) == 0)
check("journal explique le gap", any("gap" in l.lower() or "ANNULÉ" in l for l in log), str(log))
check("cash intact (aucun achat)", abs(p.cash - 10_000.0) < 1e-9, f"{p.cash}")

# ═══════ 4) Ordre trop vieux (>72h) sans séance → ANNULÉ (thèse périmée) ═══════
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
p.pending = [ordre("AAPL", heures_ago=100.0)]
faux_opens["AAPL"] = {"pret": False, "raison": "marché fermé"}
log = pp.executer_ordres_en_attente(p)
check("ordre > 72h sans séance → ANNULÉ", len(p.pending) == 0 and len(p.positions) == 0)
check("journal signale la péremption", any("périmée" in l or "ANNULÉ" in l for l in log), str(log))

# ═══════ 5) Ordre récent (<72h) sans séance → PATIENTE (week-end) ═══════
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
p.pending = [ordre("AAPL", heures_ago=40.0)]
log = pp.executer_ordres_en_attente(p)
check("ordre < 72h (week-end) → patiente encore", len(p.pending) == 1)

# ═══════ 6) IDEMPOTENCE : rejouer executer_ordres_en_attente ne double-compte pas ═══════
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
p.pending = [ordre("AAPL", plan=100.0, size=20.0)]
faux_opens["AAPL"] = {"pret": True, "open": 101.0, "date": "2026-07-10"}
pp.executer_ordres_en_attente(p)
cash_apres_1er = p.cash
pp.executer_ordres_en_attente(p)   # rejoué (cron relancé)
pp.executer_ordres_en_attente(p)   # et encore
check("rejeu : 1 seule position (pas de double achat)", len(p.positions) == 1, f"{len(p.positions)}")
check("rejeu : cash inchangé après le 1er fill", abs(p.cash - cash_apres_1er) < 1e-9, f"{p.cash}")
check("rejeu : file vide", len(p.pending) == 0)

# ═══════ 7) Le snapshot affiche les ordres en attente ═══════
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
p.pending = [ordre("NVDA", size=12.0)]
txt = pp.snapshot_text(p)
check("snapshot affiche les ordres en attente", "attente" in txt.lower() and "NVDA" in txt)

print(f"\n{'='*52}\n  RÉSULTAT R1b : {VERT}{_ok} réussis{RESET}, "
      f"{ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*52}")
exit(1 if _ko else 0)