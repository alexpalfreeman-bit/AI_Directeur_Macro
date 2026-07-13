"""
Harnais S12 (pas de snapshot le week-end) + S13 (kill-switch de drawdown).
Auto-contenu. Aucun réseau, aucune clé, aucun Redis.
"""
import sys, types
from datetime import datetime, timezone

fake_cfg = types.ModuleType("config.settings")
class _S:
    starting_capital = 100_000.0
    max_position_pct = 15.0
    max_sector_pct = 100.0
    cost_bps_per_side = 10.0
    cost_bps_per_side_smallcap = 30.0
    smallcap_cap_threshold = 2_000_000_000.0
    risk_sizing_actif = False
    max_position_risk_pct = 2.0
    atr_stop_multiple = 1.0
    min_ticket_usd = 100.0
    correlation_active = False        # on isole S13
    max_correlation_moyenne = 0.65
    correlation_jours = 90
    correlation_min_obs = 40
    killswitch_actif = True
    max_drawdown_pct = 15.0
    killswitch_reprise_pct = 10.0
fake_cfg.settings = _S()
sys.modules["config.settings"] = fake_cfg

prix_actuel = {"p": 100.0}
def _fake_get_fundamentals(t, utiliser_cache=True):
    return {"price": prix_actuel["p"], "market_cap": 3_000_000_000_000,
            "sector": "Tech", "ticker": t.upper()}
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = _fake_get_fundamentals
fake_mc.get_atr = lambda t, periode=14: 2.0
fake_mc.get_correlations = lambda c, d, jours=90, min_obs=40: {"ok": False}
fake_mc.get_seance_ohlc = lambda t: {"ok": False}
fake_mc.get_open_apres = lambda t, i: {"pret": False}
sys.modules["src.ingestion.market_client"] = fake_mc

import src.portfolio.paper_portfolio as pp
import src.analytics.performance as perf

VERT, ROUGE, RESET = "\033[92m", "\033[91m", "\033[0m"
_ok = _ko = 0
def check(nom, cond, detail=""):
    global _ok, _ko
    if cond: _ok += 1; print(f"  {VERT}\u2713{RESET} {nom}")
    else: _ko += 1; print(f"  {ROUGE}\u2717 \u00c9CHEC{RESET} {nom}  {detail}")

print("=== S12 — pas de snapshot le week-end ===")

# On force la date à un SAMEDI via un faux datetime dans le module performance.
vrai_dt = perf.datetime
class FauxDT(datetime):
    _jour = None
    @classmethod
    def now(cls, tz=None):
        return cls._jour
perf.datetime = FauxDT

FauxDT._jour = vrai_dt(2026, 7, 11, 21, 0, tzinfo=timezone.utc)   # samedi
res = perf.snapshot_quotidien()
check("SAMEDI → aucun snapshot enregistré", "Week-end" in res, res)

FauxDT._jour = vrai_dt(2026, 7, 12, 21, 0, tzinfo=timezone.utc)   # dimanche
res = perf.snapshot_quotidien()
check("DIMANCHE → aucun snapshot enregistré", "Week-end" in res, res)

FauxDT._jour = vrai_dt(2026, 7, 10, 21, 0, tzinfo=timezone.utc)   # vendredi
res = perf.snapshot_quotidien()
check("VENDREDI → snapshot pris normalement (pas de blocage)", "Week-end" not in res, res[:70])
perf.datetime = vrai_dt   # on restaure

print("\n=== S13 — kill-switch de drawdown ===")

def porte(cash, shares, prix):
    """Portefeuille : cash + 1 position. Équity = cash + shares*prix."""
    prix_actuel["p"] = prix
    p = pp.Portfolio(starting_capital=100_000.0, cash=cash)
    p.positions.append(pp.Position(
        ticker="X", shares=shares, entry_price=100.0, stop_loss=50.0,
        profit_target=200.0, conviction=0.6, sector="Tech", horizon_days=90))
    return p

# 1) Le pic se met à jour, aucun gel tant que ça monte
p = porte(cash=50_000, shares=500, prix=100.0)     # équity = 100 000
pp.maj_killswitch(p)
check("pic initialisé à l'équity courante (100 000$)", abs(p.equity_peak - 100_000) < 1, f"{p.equity_peak}")
check("aucun gel quand le portefeuille est au pic", p.killswitch_gele is False)

prix_actuel["p"] = 120.0                            # équity = 50k + 60k = 110 000
pp.maj_killswitch(p)
check("le pic MONTE avec l'équity (110 000$)", abs(p.equity_peak - 110_000) < 1, f"{p.equity_peak}")

# 2) Drawdown -10% → PAS encore de gel (seuil à -15%)
prix_actuel["p"] = 98.0                             # équity = 50k + 49k = 99 000 → -10% du pic
j = pp.maj_killswitch(p)
check("drawdown -10% → PAS de gel (seuil -15%)", p.killswitch_gele is False, str(j))

# 3) Drawdown -16% → GEL déclenché
prix_actuel["p"] = 84.0                             # équity = 50k + 42k = 92 400 → -16% du pic
j = pp.maj_killswitch(p)
check("drawdown -16% → KILL-SWITCH ACTIVÉ", p.killswitch_gele is True, str(j))
check("journal alerte clairement", any("KILL-SWITCH ACTIVÉ" in l for l in j), str(j))
check("le pic ne DESCEND jamais (reste 110 000$)", abs(p.equity_peak - 110_000) < 1, f"{p.equity_peak}")

# 4) Gelé → toute nouvelle entrée est REFUSÉE
log = pp.buy(p, "NEW", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("kill-switch actif → nouvelle entrée REFUSÉE",
      len(p.positions) == 1 and "kill-switch" in log.lower(), log)

# 5) MAIS les positions existantes restent gérées (on ne liquide rien)
check("les positions existantes ne sont PAS liquidées", len(p.positions) == 1)

# 6) HYSTÉRÉSIS : remonter à -12% ne suffit PAS à dégeler (reprise à -10%)
prix_actuel["p"] = 89.0                             # équity = 50k + 44.5k = 94 500 → -14.1%
j = pp.maj_killswitch(p)
check("remontée à -14% → TOUJOURS gelé (hystérésis)", p.killswitch_gele is True, str(j))

# 7) Remontée au-dessus de -10% → dégel
prix_actuel["p"] = 100.0                            # équity = 100 000 → -9.1% du pic 110k
j = pp.maj_killswitch(p)
check("remontée à -9% → KILL-SWITCH LEVÉ", p.killswitch_gele is False, str(j))
check("journal annonce la reprise", any("LEVÉ" in l for l in j), str(j))
log = pp.buy(p, "NEW", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("après dégel → les entrées reprennent", len(p.positions) == 2, log)

# 8) Interrupteur
_S.killswitch_actif = False
p = porte(cash=10_000, shares=100, prix=50.0)
p.equity_peak = 100_000.0                            # drawdown massif
p.killswitch_gele = True
log = pp.buy(p, "NEW", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
check("killswitch_actif=False → aucune garde", len(p.positions) == 2, log)
_S.killswitch_actif = True

# 9) Le snapshot affiche l'alerte
p = porte(cash=50_000, shares=500, prix=100.0)
p.killswitch_gele = True
p.equity_peak = 130_000.0
txt = pp.snapshot_text(p)
check("snapshot affiche l'alerte kill-switch", "KILL-SWITCH" in txt)

print(f"\n{'='*56}\n  RÉSULTAT S12+S13 : {VERT}{_ok} réussis{RESET}, "
      f"{ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*56}")
exit(1 if _ko else 0)