# test_performance.py — validation à sec de la couche de mesure (aucun réseau).
import math
import sys
import numpy as np
import pandas as pd

# ── Isolation : faux modules, pour tourner sans .env, sans Redis, sans réseau ──
import types as _t
_fcfg = _t.ModuleType("config"); _fcfg_s = _t.ModuleType("config.settings")
class _FakeSettings:
    starting_capital = 10_000.0; max_position_pct = 15.0; max_sector_pct = 40.0
    anthropic_api_key = "x"; telegram_bot_token = "x"; telegram_chat_id = "x"
_fcfg_s.settings = _FakeSettings(); _fcfg.settings = _fcfg_s
sys.modules["config"] = _fcfg; sys.modules["config.settings"] = _fcfg_s
_fur = _t.ModuleType("upstash_redis")
class _FakeRedisImport:
    def __init__(self, *a, **k): pass
_fur.Redis = _FakeRedisImport; sys.modules["upstash_redis"] = _fur
_fing = _t.ModuleType("src.ingestion"); _fing.__path__ = []
_fmc = _t.ModuleType("src.ingestion.market_client")
_fmc.get_fundamentals = lambda ticker: {"price": 100.0}
sys.modules["src.ingestion"] = _fing; sys.modules["src.ingestion.market_client"] = _fmc

from src.analytics import performance as perf

rng = np.random.default_rng(42)
ok = True


def check(nom, cond, detail=""):
    global ok
    print(f"  {'✅' if cond else '❌'} {nom} {detail}")
    if not cond:
        ok = False


# ── 1) Wilson CI ──
print("\n[1] Intervalle de Wilson")
bas, haut = perf._wilson_ci(6, 10)
check("6/10 → IC ~[31%, 83%]", 0.30 < bas < 0.33 and 0.81 < haut < 0.86, f"({bas:.3f}, {haut:.3f})")
check("0 trade → (0,1)", perf._wilson_ci(0, 0) == (0.0, 1.0))

# ── 2) stats_trades sur registre synthétique ──
print("\n[2] stats_trades")
closed = []
for i in range(12):
    entry, shares = 100.0, 10.0
    exit_p = entry * (1.06 if i % 3 else 0.95)   # 8 gagnants (+6%), 4 perdants (−5%)
    closed.append({
        "ticker": f"T{i}", "shares": shares, "entry_price": entry, "exit_price": exit_p,
        "realized_pnl": round((exit_p - entry) * shares, 2),
        "exit_reason": "profit_target" if exit_p > entry else "stop_loss",
        "opened_at": "2026-01-01T00:00:00+00:00", "closed_at": "2026-01-15T00:00:00+00:00",
    })
st = perf.stats_trades(closed, cout_bps_par_cote=10.0)
check("n=12", st["n"] == 12)
check("taux réussite 8/12", abs(st["taux_reussite"] - 8 / 12) < 1e-9)
# coût attendu par trade gagnant : (100+106)*10*10/10000 = 2.06$ → pnl net 57.94$
check("coût simulé appliqué (gain net ≈ 57.94$)", abs(st["gain_moyen"] - 57.94) < 0.01, f"{st['gain_moyen']:.2f}")
pnl_brut_attendu = 8 * 60.0 + 4 * (-50.0)
check("P&L brut = 280$", abs(st["pnl_total_brut"] - pnl_brut_attendu) < 0.01)
check("net < brut (coûts déduits)", st["pnl_total_net"] < st["pnl_total_brut"])
check("durée médiane = 14 j", abs(st["duree_mediane_jours"] - 14.0) < 1e-9)
check("non conclusif (n<30)", st["conclusif"] is False)
check("ventilation par motif (2 motifs)", len(st["par_motif_sortie"]) == 2)

# ── 3) stats_series : bêta/alpha retrouvés sur données construites ──
print("\n[3] stats_series (bêta≈0.8 construit, alpha≈+8%/an injecté)")
n = 250
dates = pd.bdate_range("2025-07-01", periods=n)
rf_j = (1 + 0.04) ** (1 / 252) - 1
r_m = rng.normal(0.0004, 0.010, n)                      # marché
alpha_j_vrai = 0.08 / 252
r_p = rf_j + alpha_j_vrai + 0.8 * (r_m - rf_j) + rng.normal(0, 0.004, n)
bench_close = pd.Series(500 * np.cumprod(1 + r_m), index=dates)
equity = pd.Series(10_000 * np.cumprod(1 + r_p), index=dates)
deployed = equity * 0.7                                  # 70% investi en permanence
ss = perf.stats_series(equity, deployed, bench_close)
check("n_obs = 249", ss["n_obs"] == n - 1)
check("bêta retrouvé ≈ 0.8", abs(ss["beta"] - 0.8) < 0.08, f"{ss['beta']:.3f}")
check("alpha ≈ +8%/an (±5 pts, bruit)", abs(ss["alpha_jensen_annualise"] - 0.08) < 0.05,
      f"{ss['alpha_jensen_annualise']*100:.1f}%")
check("déploiement moyen ≈ 70%", abs(ss["deploiement_moyen"] - 0.7) < 0.02, f"{ss['deploiement_moyen']:.2f}")
check("drawdown max négatif", ss["drawdown_max"] < 0)
check("Sharpe fini + SE fournie", math.isfinite(ss["sharpe"]) and ss["sharpe_se"] > 0,
      f"SR={ss['sharpe']:.2f}±{ss['sharpe_se']:.2f}")
check("t_alpha cohérent avec alpha>0", ss["t_alpha"] > 0)

# Cas dégénéré : 2 points → refus propre
ss2 = perf.stats_series(equity.iloc[:2], deployed.iloc[:2], bench_close)
check("2 snapshots → refus propre", "message" in ss2)

# Benchmark plus long que la série + trous de dates → alignement ffill
equity_trous = equity.iloc[::2]                          # un snapshot sur deux
ss3 = perf.stats_series(equity_trous, deployed.iloc[::2], bench_close)
check("dates à trous : bêta stable ≈0.8", abs(ss3["beta"] - 0.8) < 0.12, f"{ss3['beta']:.3f}")

# ── 4) Snapshot : idempotence par date + garde anti-écrasement (mode fichier) ──
print("\n[4] snapshot_quotidien (stubs, mode fichier local)")
import types
fake_pf = types.SimpleNamespace(
    cash=4000.0,
    positions=[types.SimpleNamespace(ticker="AAA", shares=10.0, entry_price=100.0),
               types.SimpleNamespace(ticker="BBB", shares=5.0, entry_price=200.0)],
    closed=[],
)
import src.portfolio.paper_portfolio as pp
import src.ingestion.market_client as mc
pp_load, mc_get = pp.load_portfolio, mc.get_fundamentals
pp.load_portfolio = lambda: fake_pf
mc.get_fundamentals = lambda t: {"price": 110.0 if t == "AAA" else None}  # BBB : prix manquant

perf.FICHIER_SNAPSHOTS.unlink(missing_ok=True)
msg1 = perf.snapshot_quotidien()
msg2 = perf.snapshot_quotidien()      # relance le MÊME jour → doit écraser, pas doubler
snaps = perf._charger_snapshots()
check("1 seule entrée après 2 appels (idempotent)", len(snaps) == 1, f"{len(snaps)} entrée(s)")
ligne = list(snaps.values())[0]
check("équity = 4000 + 10×110 + 5×200 = 6100", abs(ligne["equity"] - 6100.0) < 0.01, f"{ligne['equity']}")
check("prix manquant journalisé (BBB)", ligne["prix_manquants"] == ["BBB"])

# Garde anti-écrasement : lecture qui échoue → on n'écrit PAS
orig_charger = perf._charger_snapshots
perf._charger_snapshots = lambda: (_ for _ in ()).throw(perf.LectureStockageErreur("réseau"))
msg3 = perf.snapshot_quotidien()
perf._charger_snapshots = orig_charger
check("échec lecture → snapshot refusé (pas d'écrasement)", "NON enregistré" in msg3, msg3[:60])
check("l'historique existant est intact", len(perf._charger_snapshots()) == 1)

pp.load_portfolio, mc.get_fundamentals = pp_load, mc_get
perf.FICHIER_SNAPSHOTS.unlink(missing_ok=True)

print("\n" + ("🎉 TOUS LES TESTS PASSENT" if ok else "💥 ÉCHEC — voir ci-dessus"))
sys.exit(0 if ok else 1)