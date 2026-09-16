"""
Harnais V2 — régime agressif (décision de l'investisseur, 2026-09-16) :
  V2.1  Plancher de taille sous la cible de déploiement (audit : 40 % investi, gagnants à 4 %)
  V2.2  Étiquette de régime sur chaque position (attribution avant/après)
  V2.3  Nettoyage des miettes (5 positions < 100 $ issues du charcutage)
  V2.4  Prompt du Directeur : grille de conviction, WATCHLIST = exception, déploiement injecté
  V2.5  Flux RSS diversifiés + découpage Telegram
Auto-contenu : aucun réseau, aucune clé, aucun Redis.
"""
import sys, types
from datetime import datetime, timezone, timedelta

fake_cfg = types.ModuleType("config.settings")
class _S:
    starting_capital = 10_000.0; max_position_pct = 15.0; max_sector_pct = 100.0
    cost_bps_per_side = 10.0; cost_bps_per_side_smallcap = 30.0; smallcap_cap_threshold = 2e9
    risk_sizing_actif = True; max_position_risk_pct = 3.0; atr_stop_multiple = 1.0; min_ticket_usd = 100.0
    correlation_active = False; killswitch_actif = False; max_drawdown_pct = 15.0; killswitch_reprise_pct = 10.0
    stop_elargissement_actif = True; stop_atr_multiple_min = 2.0
    gerant_delai_min_jours = 10; gerant_min_allegement_usd = 300.0; gerant_max_allegements = 2
    comite_pre_ouverture_seulement = True; dedup_fenetre_h = 48; dedup_similarite_min = 0.45
    regime_version = "v2-2026-09-16"; deploiement_cible_pct = 65.0; taille_min_position_pct = 6.0
    poussiere_seuil_usd = 50.0
    anthropic_api_key = "sk-test"; telegram_bot_token = "0:x"; telegram_chat_id = "0"
    llm_model = "m"; cheap_model = "m"; director_model = "m"; risk_profile = "agressif"; news_feeds = []
fake_cfg.settings = _S()
sys.modules["config.settings"] = fake_cfg

prix = {}
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = lambda t, utiliser_cache=True: {"price": prix.get(t, 100.0), "market_cap": 3e12,
                                                           "sector": "Technology", "ticker": t.upper()}
fake_mc.get_atr = lambda t, periode=14: 3.0
fake_mc.get_correlations = lambda c, d, jours=90, min_obs=40: {"ok": False}
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

def position(ticker, shares, sector="Technology"):
    return pp.Position(ticker=ticker, shares=shares, entry_price=100.0, stop_loss=90.0, profit_target=120.0,
                       conviction=0.5, sector=sector, horizon_days=90,
                       opened_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())

print("=== V2.1 — Plancher de taille sous la cible (audit : le Directeur misait 3-6 %) ===")
pp._cache_atr.clear()
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)          # 0 % déployé
log = pp.buy(p, "A", 100.0, 3.0, stop_loss=90.0, profit_target=120.0)   # le Directeur demande 3 %
check("0 % déployé + demande 3 % → taille relevée à 6 % (600$)",
      abs(p.positions[0].shares * 100.0 - 600.0) < 1.0, f"{p.positions[0].shares*100:.0f}$")
check("le journal explique la relève", "relevée" in log and "cible" in log, log)
check("étiquette de régime posée", p.positions[0].regime == "v2-2026-09-16", p.positions[0].regime)

pp._cache_atr.clear()
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
pp.buy(p, "A", 100.0, 10.0, stop_loss=90.0, profit_target=120.0)   # demande 10 % > plancher
check("demande 10 % → inchangée (le plancher ne réduit jamais)", abs(p.positions[0].shares*100.0 - 1000.0) < 1.0)

# Portefeuille DÉJÀ au-dessus de la cible → pas de plancher
pp._cache_atr.clear()
p = pp.Portfolio(starting_capital=10_000.0, cash=2_000.0)
p.positions.append(position("BIG", 80.0))                            # 8 000$ investis = 80 %
check("déploiement calculé = 80 %", abs(pp.deploiement_pct(p) - 80.0) < 0.5, f"{pp.deploiement_pct(p):.0f}")
log = pp.buy(p, "B", 100.0, 3.0, stop_loss=90.0, profit_target=120.0)
check("au-dessus de la cible → demande 3 % respectée (300$), pas de plancher",
      len(p.positions) == 2 and abs(p.positions[1].shares*100.0 - 300.0) < 1.0, log)

# Le plafond dur et le budget de risque restent SOUVERAINS malgré le plancher
pp._cache_atr.clear()
p = pp.Portfolio(starting_capital=10_000.0, cash=10_000.0)
pp.buy(p, "C", 100.0, 3.0, stop_loss=99.0, profit_target=120.0)      # stop ultra-serré
pos = p.positions[0]
check("plafond 15 % jamais dépassé", pos.shares * 100.0 <= 1_500.0 + 1)
check("perte au stop ≤ 3 % de l'équity (300$)", pos.shares * (100.0 - pos.stop_loss) <= 300.0 + 1,
      f"{pos.shares*(100.0-pos.stop_loss):.0f}$")

print("\n=== V2.2 — Le régime suit la position jusqu'à la clôture ===")
pp.close_position(p, pos, 110.0, "profit_target")
check("ClosedPosition porte le régime", p.closed[-1].regime == "v2-2026-09-16", p.closed[-1].regime)
legacy = position("OLD", 10.0)                                        # ouverte avant V2 : régime vide
p.positions.append(legacy); pp.close_position(p, legacy, 105.0, "profit_target")
check("position héritée (avant V2) → régime vide, pas d'invention", p.closed[-1].regime == "")

print("\n=== V2.3 — Nettoyage des MIETTES (audit : JPM 25$, BAC 25$, ECPG 16$) ===")
p = pp.Portfolio(starting_capital=10_000.0, cash=5_000.0)
prix.update({"JPM": 350.0, "BAC": 60.0, "SU": 70.0, "SANSPRIX": None})
p.positions += [position("JPM", 0.07), position("BAC", 0.42), position("SU", 7.75), position("SANSPRIX", 0.1)]
cash_avant = p.cash
j = pp.nettoyer_poussiere(p)
tickers = [x.ticker for x in p.positions]
check("JPM (25$) liquidé", "JPM" not in tickers)
check("BAC (25$) liquidé", "BAC" not in tickers)
check("SU (543$) CONSERVÉ", "SU" in tickers)
check("position sans prix CONSERVÉE (on ne vend pas à l'aveugle)", "SANSPRIX" in tickers)
check("motif de sortie = poussiere (exclu des stats)", all(c.exit_reason == "poussiere" for c in p.closed))
check("cash crédité", p.cash > cash_avant)
check("journal explicite", len(j) == 2 and all("miette" in l for l in j), str(j))

print("\n=== V2.4 — Prompt du Directeur ===")
src = open("src/agents/portfolio_manager_agent.py", encoding="utf-8").read()
check("WATCHLIST déclaré EXCEPTION avec conditions nommées", "WATCHLIST est une EXCEPTION" in src and "catalyseur est DATÉ" in src)
check("grille de conviction couvre 0,30 → 0,90", "0,30-0,45" in src and "0,80-0,90" in src)
check("interdit d'exécuter à 0,4 (contradiction)", "Tu N'EXÉCUTES PAS à ce niveau" in src)
check("déploiement injecté dans le message", "DÉPLOIEMENT :" in src and "SOUS LA CIBLE" in src)
check("stop 2×ATR expliqué au Directeur", "2×ATR" in src)
check("concentration thématique (crédit US) nommée", "UN seul pari" in src)

print("\n=== V2.5 — Flux RSS + Telegram ===")
import src.ingestion.news_client as nc
check(f"{len(nc.RSS_FEEDS)} flux (était 3)", len(nc.RSS_FEEDS) >= 10)
check("énergie/métaux couverts", any("oilprice" in u or "mining" in u for u in nc.RSS_FEEDS))
check("chaînes d'approvisionnement couvertes", any("freightwaves" in u or "supply" in u for u in nc.RSS_FEEDS))
check("Asie couverte", any("China" in u or "Japan" in u for u in nc.RSS_FEEDS))
check("investing.com (403 fréquents) retiré", not any("investing.com" in u for u in nc.RSS_FEEDS))
import src.communication.telegram_bot as tb
long = "\n".join(f"ligne {i} " + "x"*80 for i in range(120))          # ~10 000 caractères
morceaux = tb._decouper(long)
check("message de 10 000 car. découpé en plusieurs envois", len(morceaux) >= 3, f"{len(morceaux)}")
check("chaque morceau ≤ 4000", all(len(m) <= 4000 for m in morceaux))
check("aucune ligne perdue", "".join(morceaux).replace("\n","") == long.replace("\n",""))
check("message court → un seul envoi", tb._decouper("court") == ["court"])

print(f"\n{'='*60}\n  RÉSULTAT V2 : {VERT}{_ok} réussis{RESET}, {ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*60}")
exit(1 if _ko else 0)