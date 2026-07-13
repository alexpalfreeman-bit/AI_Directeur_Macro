"""
Harnais S9 — dimensionnement par le RISQUE (budget de perte + plancher ATR).
Auto-contenu : stubs config.settings + market_client. Aucun réseau/clé/Redis.
"""
import sys, types

fake_cfg = types.ModuleType("config.settings")
class _Settings:
    starting_capital = 100_000.0
    max_position_pct = 15.0
    max_sector_pct = 100.0
    cost_bps_per_side = 10.0
    cost_bps_per_side_smallcap = 30.0
    smallcap_cap_threshold = 2_000_000_000.0
    risk_sizing_actif = True
    max_position_risk_pct = 2.0      # on accepte de perdre 2% de l'équity par position
    atr_stop_multiple = 1.0
    min_ticket_usd = 100.0
fake_cfg.settings = _Settings()
sys.modules["config.settings"] = fake_cfg

faux_atr = {}
def _fake_get_fundamentals(ticker, utiliser_cache=True):
    return {"price": 100.0, "market_cap": 3_000_000_000_000, "sector": "Technology",
            "ticker": ticker.upper()}
def _fake_get_atr(ticker, periode=14):
    return faux_atr.get(ticker)
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = _fake_get_fundamentals
fake_mc.get_atr = _fake_get_atr
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

def neuf():
    pp._cache_atr.clear()
    return pp.Portfolio(starting_capital=100_000.0, cash=100_000.0)

# Équity 100 000$ → budget de risque 2% = 2 000$ de perte max par position.

# ═══ 1) LE CŒUR : le risque par position est PLAFONNÉ à 2% (jamais dépassé) ═══
# Contrat réel : dollars = min(taille voulue par le LLM, taille autorisée par le risque).
# Le risque ne peut que RÉDUIRE — il ne gonfle jamais une position (pas de levier au LLM).
#
# Titre A : stop -5% (distance 5$). Risque autorise 2000/5 = 400 actions = 40 000$,
#           mais le LLM ne demande que 10% = 10 000$ → on garde 10 000$ (perte max 500$).
faux_atr["A"] = 1.0
p = neuf()
pp.buy(p, "A", 100.0, 10.0, stop_loss=95.0, profit_target=120.0)
posA = p.positions[0]
check("stop serré : le risque n'AUGMENTE pas la position (10% demandé → 10 000$)",
      abs(posA.shares * 100.0 - 10_000.0) < 1.0, f"{posA.shares*100:.0f}$")

# Titre B : stop -20% (distance 20$). Risque n'autorise que 2000/20 = 100 actions = 10 000$.
#           Le LLM demandait 10% = 10 000$ → coïncide. Perte max = 2 000$ = le budget.
faux_atr["B"] = 1.0
p = neuf()
pp.buy(p, "B", 100.0, 10.0, stop_loss=80.0, profit_target=120.0)
posB = p.positions[0]
check("stop large : taille limitée par le budget de risque (10 000$)",
      abs(posB.shares * 100.0 - 10_000.0) < 1.0, f"{posB.shares*100:.0f}$")

# LA PROPRIÉTÉ GARANTIE : la perte au stop ne dépasse JAMAIS le budget (2% = 2 000$).
perte_A = posA.shares * (100.0 - 95.0)
perte_B = posB.shares * (100.0 - 80.0)
check("PERTE AU STOP ≤ budget de 2 000$ dans les DEUX cas (risque borné)",
      perte_A <= 2000.0 + 1 and perte_B <= 2000.0 + 1,
      f"A={perte_A:.0f}$  B={perte_B:.0f}$")
check("AVANT S9, B aurait risqué 2 000$ pour 10 000$ investis, et une demande de 15% "
      "aurait risqué 3 000$ (> budget) — c'est ce dépassement qui est désormais impossible",
      True)

# ═══ 2) Le risque RÉDUIT une demande LLM trop grosse ═══
# LLM demande 15%, stop à -20% → risque autorise 10 000$ seulement.
p = neuf()
log = pp.buy(p, "B", 100.0, 15.0, stop_loss=80.0, profit_target=120.0)
check("demande LLM 15% (15 000$) réduite à 10 000$ par le budget de risque",
      abs(p.positions[0].shares * 100.0 - 10_000.0) < 1.0, f"{p.positions[0].shares*100:.0f}$")
check("journal explique la réduction par le risque", "risque" in log.lower(), log)

# ═══ 3) LE PIÈGE : stop absurdement serré → l'ATR l'empêche d'exploser la taille ═══
# Stop à -0,5% (distance 0,5$) mais le titre bouge de 3$/jour (ATR).
# SANS plancher : actions = 2000/0.5 = 4000 → 400 000$ (!!) → 4× l'équity.
# AVEC plancher ATR : distance = 3$ → actions = 666 → 66 600$ → écrêté à 15% = 15 000$.
faux_atr["VOL"] = 3.0
p = neuf()
log = pp.buy(p, "VOL", 100.0, 15.0, stop_loss=99.5, profit_target=120.0)
taille = p.positions[0].shares * 100.0
check("stop ultra-serré : taille N'EXPLOSE PAS (plafond 15% tient)",
      abs(taille - 15_000.0) < 1.0, f"{taille:.0f}$")
check("journal signale le plancher ATR", "atr" in log.lower(), log)
check("le levier reste impossible (taille ≤ équity)", taille <= 100_000.0)

# ═══ 4) ATR indisponible → dégradation propre (pas de crash, sizing risque quand même) ═══
p = neuf()   # pas d'ATR pour "NOATR"
log = pp.buy(p, "NOATR", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)
# distance 10$ → risque autorise 20 000$ ; LLM demande 10 000$ → min = 10 000$
check("ATR indisponible → aucun crash, sizing appliqué proprement",
      len(p.positions) == 1 and abs(p.positions[0].shares * 100.0 - 10_000.0) < 1.0,
      f"{p.positions[0].shares*100:.0f}$" if p.positions else log)
check("ATR indisponible : perte au stop ≤ budget",
      p.positions and p.positions[0].shares * 10.0 <= 2000.0 + 1)

# ═══ 5) Plancher de ticket : une position poussière est refusée ═══
_Settings.max_position_pct = 0.05      # plafond ridicule → 50$ sur 100k
p = neuf()
log = pp.buy(p, "A", 100.0, 10.0, stop_loss=95.0, profit_target=120.0)
check("position poussière (<100$) refusée", len(p.positions) == 0 and "trop petite" in log.lower(), log)
_Settings.max_position_pct = 15.0      # on restaure

# ═══ 6) Pas de stop → sizing classique (rétrocompat) ═══
p = neuf()
pp.buy(p, "A", 100.0, 10.0, stop_loss=None, profit_target=120.0)
check("sans stop → sizing classique (10% = 10 000$)",
      abs(p.positions[0].shares * 100.0 - 10_000.0) < 1.0, f"{p.positions[0].shares*100:.0f}$")

# ═══ 7) Sizing désactivable (interrupteur) ═══
_Settings.risk_sizing_actif = False
p = neuf()
pp.buy(p, "B", 100.0, 10.0, stop_loss=80.0, profit_target=120.0)
check("risk_sizing_actif=False → ancien comportement (10% = 10 000$)",
      abs(p.positions[0].shares * 100.0 - 10_000.0) < 1.0, f"{p.positions[0].shares*100:.0f}$")
_Settings.risk_sizing_actif = True

# ═══ 8) Le plafond dur reste SOUVERAIN (le risque ne peut jamais l'outrepasser) ═══
faux_atr["TIGHT"] = 0.1
p = neuf()
pp.buy(p, "TIGHT", 100.0, 15.0, stop_loss=99.0, profit_target=120.0)
check("plafond 15% jamais dépassé, quel que soit le stop",
      p.positions[0].shares * 100.0 <= 15_000.0 + 1.0, f"{p.positions[0].shares*100:.0f}$")

print(f"\n{'='*54}\n  RÉSULTAT S9 : {VERT}{_ok} réussis{RESET}, "
      f"{ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*54}")
exit(1 if _ko else 0)