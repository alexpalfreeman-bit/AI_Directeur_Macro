"""
Harnais S11 — diversification par la CORRÉLATION réelle.
Auto-contenu : stubs config.settings + market_client. Aucun réseau/clé/Redis.
"""
import sys, types

fake_cfg = types.ModuleType("config.settings")
class _S:
    starting_capital = 100_000.0
    max_position_pct = 15.0
    max_sector_pct = 100.0
    cost_bps_per_side = 10.0
    cost_bps_per_side_smallcap = 30.0
    smallcap_cap_threshold = 2_000_000_000.0
    risk_sizing_actif = False          # on isole S11 (S9 testé ailleurs)
    max_position_risk_pct = 2.0
    atr_stop_multiple = 1.0
    min_ticket_usd = 100.0
    correlation_active = True
    max_correlation_moyenne = 0.65
    correlation_jours = 90
    correlation_min_obs = 40
fake_cfg.settings = _S()
sys.modules["config.settings"] = fake_cfg

faux_correl = {}     # (candidat, detenu) -> corrélation
correl_ok = {"ok": True}
def _fake_get_fundamentals(t, utiliser_cache=True):
    return {"price": 100.0, "market_cap": 3_000_000_000_000, "sector": "Technology",
            "ticker": t.upper()}
def _fake_get_correlations(candidat, detenus, jours=90, min_obs=40):
    if not correl_ok["ok"]:
        return {"ok": False, "raison": "panne yfinance simulée"}
    out = {t: faux_correl[(candidat, t)] for t in detenus if (candidat, t) in faux_correl}
    if not out:
        return {"ok": False, "raison": "aucune corrélation calculable"}
    return {"ok": True, "correlations": out}
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = _fake_get_fundamentals
fake_mc.get_correlations = _fake_get_correlations
fake_mc.get_atr = lambda t, periode=14: 2.0
fake_mc.get_seance_ohlc = lambda t: {"ok": False}
fake_mc.get_open_apres = lambda t, i: {"pret": False}
sys.modules["src.ingestion.market_client"] = fake_mc

import src.portfolio.paper_portfolio as pp

VERT, ROUGE, RESET = "\033[92m", "\033[91m", "\033[0m"
_ok = _ko = 0
def check(nom, cond, detail=""):
    global _ok, _ko
    if cond: _ok += 1; print(f"  {VERT}\u2713{RESET} {nom}")
    else: _ko += 1; print(f"  {ROUGE}\u2717 \u00c9CHEC{RESET} {nom}  {detail}")

def avec(positions):
    """positions : liste de (ticker, shares) — prix 100$ chacun."""
    p = pp.Portfolio(starting_capital=100_000.0, cash=60_000.0)
    for t, sh in positions:
        p.positions.append(pp.Position(
            ticker=t, shares=sh, entry_price=100.0, stop_loss=90.0, profit_target=120.0,
            conviction=0.6, sector="Various", horizon_days=30))
    return p

# ═══ 1) LE CAS RÉEL : 3 "secteurs" différents, UN SEUL pari cyclique ═══
# Portefeuille : OXY (Energy) + FCX (Materials). Candidat : CAT (Industrials).
# Les plafonds SECTORIELS laisseraient passer (3 secteurs différents !).
# Mais les corrélations sont ~0,80 : c'est le même pari macro.
faux_correl[("CAT", "OXY")] = 0.82
faux_correl[("CAT", "FCX")] = 0.78
p = avec([("OXY", 100), ("FCX", 100)])
log = pp.buy(p, "CAT", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("3 secteurs DIFFÉRENTS mais corrélation 0,80 → achat REFUSÉ",
      len(p.positions) == 2 and "corrélation" in log.lower(), log)
check("journal explique que c'est le même pari en double",
      "même pari" in log or "double" in log.lower(), log)

# ═══ 2) Vraie diversification (corrélation basse) → achat ACCEPTÉ ═══
faux_correl[("KO", "OXY")] = 0.15
faux_correl[("KO", "FCX")] = 0.10
p = avec([("OXY", 100), ("FCX", 100)])
log = pp.buy(p, "KO", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("corrélation faible (0,12) → achat ACCEPTÉ (vraie diversification)",
      len(p.positions) == 3, log)

# ═══ 3) PONDÉRATION : forte corrélation avec une GROSSE ligne pèse plus ═══
# Candidat corrélé 0,90 avec une position ÉNORME (900 actions) et 0,10 avec une petite (10).
# Moyenne SIMPLE = 0,50 (passerait). Moyenne PONDÉRÉE ≈ 0,89 (doit bloquer).
faux_correl[("NEW", "BIG")] = 0.90
faux_correl[("NEW", "SMALL")] = 0.10
p = avec([("BIG", 900), ("SMALL", 10)])
correl, detail = pp._correlation_portefeuille(p, "NEW")
check("corrélation PONDÉRÉE par le poids (≈0,89), pas la moyenne simple (0,50)",
      correl is not None and 0.85 < correl < 0.92, f"{correl}")
log = pp.buy(p, "NEW", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("achat bloqué : la grosse ligne corrélée domine à juste titre",
      len(p.positions) == 2, log)

# Inverse : forte corrélation avec une PETITE ligne seulement → passe
p = avec([("BIG", 900), ("SMALL", 10)])
faux_correl[("NEW2", "BIG")] = 0.10
faux_correl[("NEW2", "SMALL")] = 0.90
correl, _ = pp._correlation_portefeuille(p, "NEW2")
check("forte corrélation avec une PETITE ligne → moyenne pondérée reste basse",
      correl is not None and correl < 0.2, f"{correl}")
log = pp.buy(p, "NEW2", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("achat accepté (le risque réel est faible)", len(p.positions) == 3, log)

# ═══ 4) Portefeuille VIDE → aucune garde (rien à corréler) ═══
p = pp.Portfolio(starting_capital=100_000.0, cash=100_000.0)
log = pp.buy(p, "AAPL", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("portefeuille vide → 1re position toujours acceptée", len(p.positions) == 1, log)

# ═══ 5) PANNE yfinance → on LAISSE PASSER (une panne n'est pas une règle de gestion) ═══
correl_ok["ok"] = False
p = avec([("OXY", 100), ("FCX", 100)])
log = pp.buy(p, "CAT", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("corrélation indisponible → achat NON bloqué (dégradation permissive)",
      len(p.positions) == 3, log)
correl_ok["ok"] = True

# ═══ 6) Seuil respecté à la frontière ═══
faux_correl[("EDGE", "OXY")] = 0.64      # juste SOUS 0,65
faux_correl[("EDGE", "FCX")] = 0.64
p = avec([("OXY", 100), ("FCX", 100)])
log = pp.buy(p, "EDGE", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("corrélation 0,64 < seuil 0,65 → accepté", len(p.positions) == 3, log)

faux_correl[("EDGE2", "OXY")] = 0.70
faux_correl[("EDGE2", "FCX")] = 0.70
p = avec([("OXY", 100), ("FCX", 100)])
log = pp.buy(p, "EDGE2", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("corrélation 0,70 > seuil 0,65 → refusé", len(p.positions) == 2, log)

# ═══ 7) Interrupteur ═══
_S.correlation_active = False
p = avec([("OXY", 100), ("FCX", 100)])
log = pp.buy(p, "CAT", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("correlation_active=False → garde désactivée", len(p.positions) == 3, log)
_S.correlation_active = True

# ═══ 8) Corrélation NÉGATIVE (vraie couverture) → toujours acceptée ═══
faux_correl[("GLD", "OXY")] = -0.30
faux_correl[("GLD", "FCX")] = -0.25
p = avec([("OXY", 100), ("FCX", 100)])
log = pp.buy(p, "GLD", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("corrélation négative (couverture) → acceptée", len(p.positions) == 3, log)

print(f"\n{'='*56}\n  RÉSULTAT S11 : {VERT}{_ok} réussis{RESET}, "
      f"{ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*56}")
exit(1 if _ko else 0)