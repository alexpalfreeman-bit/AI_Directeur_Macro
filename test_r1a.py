"""
Harnais R1a — coûts de transaction intégrés à l'objet portefeuille.
Auto-contenu : stubs pour config.settings et market_client AVANT import.
Aucun réseau, aucune clé, aucun Redis.
"""
import sys, types

# ── 1) Stub config.settings (évite les clés API) ──
fake_cfg = types.ModuleType("config.settings")
class _Settings:
    starting_capital = 10_000.0
    max_position_pct = 100.0            # plafonds neutralisés pour tester les frais purs
    max_sector_pct = 100.0
    cost_bps_per_side = 10.0
    cost_bps_per_side_smallcap = 30.0
    smallcap_cap_threshold = 2_000_000_000.0
fake_cfg.settings = _Settings()
sys.modules["config.settings"] = fake_cfg

# ── 2) Stub market_client (contrôle prix + capitalisation, zéro yfinance) ──
faux_marche = {}
def _fake_get_fundamentals(ticker, utiliser_cache=True):
    d = faux_marche.get(ticker, {})
    return {"price": d.get("price"), "market_cap": d.get("market_cap"),
            "sector": d.get("sector"), "ticker": ticker.upper()}
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = _fake_get_fundamentals
sys.modules["src.ingestion.market_client"] = fake_mc

# ── 3) Import du VRAI paper_portfolio (avec les stubs en place) ──
import src.portfolio.paper_portfolio as pp
import src.analytics.performance as perf

VERT, ROUGE, RESET = "\033[92m", "\033[91m", "\033[0m"
_ok = _ko = 0
def check(nom, cond, detail=""):
    global _ok, _ko
    if cond: _ok += 1; print(f"  {VERT}\u2713{RESET} {nom}")
    else: _ko += 1; print(f"  {ROUGE}\u2717 \u00c9CHEC{RESET} {nom}  {detail}")

# ═══════════ 1) Achat large cap : frais 10 bps débités EN PLUS du notionnel ═══════════
faux_marche["AAPL"] = {"price": 100.0, "market_cap": 3_000_000_000_000, "sector": "Technology"}
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
pp.buy(p, "AAPL", 100.0, 20.0, stop_loss=90.0, profit_target=120.0)
pos = p.positions[0]
check("shares sur le notionnel (20), pas réduites par les frais", abs(pos.shares - 20.0) < 1e-9, f"{pos.shares}")
check("frais d'entrée = 2.00$ (10 bps large cap)", abs(pos.entry_cost - 2.00) < 1e-9, f"{pos.entry_cost}")
check("cash = 10000 - 2000 - 2.00 = 7998.00", abs(p.cash - 7998.00) < 1e-9, f"{p.cash}")
check("frais cumulés = 2.00$", abs(p.total_costs_paid - 2.00) < 1e-9, f"{p.total_costs_paid}")

# ═══════════ 2) Small cap : tarif 30 bps ═══════════
faux_marche["CF"] = {"price": 50.0, "market_cap": 1_000_000_000, "sector": "Basic Materials"}
p2 = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
pp.buy(p2, "CF", 50.0, 20.0, stop_loss=45.0, profit_target=60.0)
check("small cap facturée 30 bps (frais 6.00$)", abs(p2.positions[0].entry_cost - 6.00) < 1e-9,
      f"{p2.positions[0].entry_cost}")

# ═══════════ 3) Cap inconnue ⇒ tarif conservateur small (30 bps) ═══════════
faux_marche["XYZ"] = {"price": 10.0, "market_cap": None, "sector": ""}
check("cap inconnue ⇒ 30 bps (conservateur)", abs(pp._frais_bps("XYZ") - 30.0) < 1e-9)

# ═══════════ 4) Clôture : produit NET de frais, P&L brut inchangé ═══════════
cash_avant = p.cash
pp.close_position(p, p.positions[0], 110.0, "profit_target")
c = p.closed[-1]
check("P&L brut stocké = (110-100)*20 = 200", abs(c.realized_pnl - 200.0) < 1e-9, f"{c.realized_pnl}")
check("exit_cost = 2.20$", abs(c.exit_cost - 2.20) < 1e-9, f"{c.exit_cost}")
check("entry_cost reporté sur le trade clôturé (2.00$)", abs(c.entry_cost - 2.00) < 1e-9, f"{c.entry_cost}")
check("cash crédité NET : +2200 - 2.20", abs(p.cash - (cash_avant + 2200.0 - 2.20)) < 1e-9, f"{p.cash}")
check("frais cumulés = 4.20$", abs(p.total_costs_paid - 4.20) < 1e-9, f"{p.total_costs_paid}")

# ═══════════ 5) Allègement : frais + part d'entry_cost au prorata ═══════════
faux_marche["NU"] = {"price": 10.0, "market_cap": 5_000_000_000_000, "sector": "Financial"}
p3 = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
pp.buy(p3, "NU", 10.0, 40.0, stop_loss=9.0, profit_target=12.0)   # 4000$, frais 4.00$, 400 actions
pos3 = p3.positions[0]
check("entry_cost initial = 4.00$", abs(pos3.entry_cost - 4.00) < 1e-9, f"{pos3.entry_cost}")
pp.trim_position(p3, pos3, 11.0, fraction=0.5, reason="alleger")  # 200 actions @ 11 → notionnel 2200
lot = p3.closed[-1]
check("allègement : exit_cost = 2.20$", abs(lot.exit_cost - 2.20) < 1e-9, f"{lot.exit_cost}")
check("allègement : entry_cost du lot = 2.00$ (moitié)", abs(lot.entry_cost - 2.00) < 1e-9, f"{lot.entry_cost}")
check("reste garde l'autre moitié d'entry_cost (2.00$)", abs(pos3.entry_cost - 2.00) < 1e-9, f"{pos3.entry_cost}")
check("actions restantes = 200", abs(pos3.shares - 200.0) < 1e-9, f"{pos3.shares}")

# ═══════════ 6) Refus si notionnel + frais > cash ═══════════
p4 = pp.Portfolio(starting_capital=1000.0, cash=1000.0)
faux_marche["BIG"] = {"price": 100.0, "market_cap": 5_000_000_000_000, "sector": "Tech"}
log = pp.buy(p4, "BIG", 100.0, 100.0, stop_loss=90.0, profit_target=120.0)   # 1000$ + 1$ > 1000
check("achat refusé si notionnel+frais > cash", len(p4.positions) == 0 and "insuffisant" in log.lower(), log)

# ═══════════ 7) Rétrocompat stats_trades ═══════════
legacy = [{"ticker": "T", "shares": 10.0, "entry_price": 100.0, "exit_price": 106.0,
           "realized_pnl": 60.0, "exit_reason": "profit_target",
           "opened_at": "2026-01-01T00:00:00+00:00", "closed_at": "2026-01-15T00:00:00+00:00"}]
st = perf.stats_trades(legacy, cout_bps_par_cote=10.0)
check("legacy sans coûts → repli simulation bps (net 57.94$)", abs(st["gain_moyen"] - 57.94) < 0.01,
      f"{st['gain_moyen']:.2f}")
reel = [{"ticker": "T", "shares": 10.0, "entry_price": 100.0, "exit_price": 106.0,
         "realized_pnl": 60.0, "exit_reason": "profit_target", "entry_cost": 1.0, "exit_cost": 3.0,
         "opened_at": "2026-01-01T00:00:00+00:00", "closed_at": "2026-01-15T00:00:00+00:00"}]
st2 = perf.stats_trades(reel, cout_bps_par_cote=10.0)
check("coûts réels stockés (4$) utilisés → net 56.00$", abs(st2["gain_moyen"] - 56.00) < 0.01,
      f"{st2['gain_moyen']:.2f}")

print(f"\n{'='*50}\n  RÉSULTAT R1a : {VERT}{_ok} réussis{RESET}, "
      f"{ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*50}")
exit(1 if _ko else 0)