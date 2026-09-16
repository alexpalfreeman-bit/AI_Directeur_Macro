"""
Harnais S15 + T1 — construits sur l'AUDIT RÉEL de 153 trades / 190 décisions :
  S15.1  Stops élargis à 2×ATR      (6 stops à -10 % vs 6 objectifs à +17 %)
  S15.2  Gérant bridé               (134 allègements/153, lot médian 25 $, CUBI allégée 17×)
  S15.3  Backfill des secteurs      (EG = 1 400 $ sans secteur ; libellés libres du LLM)
  T1.1   Comité pré-ouverture seul  (3,8 comités/jour, 66 % en WATCHLIST)
  T1.2   Déduplication des thèmes   (même thème proposé jusqu'à 9 fois)
Auto-contenu : aucun réseau, aucune clé, aucun Redis.
"""
import sys, types
from datetime import datetime, timezone, timedelta

fake_cfg = types.ModuleType("config.settings")
class _S:
    starting_capital = 100_000.0; max_position_pct = 15.0; max_sector_pct = 100.0
    cost_bps_per_side = 10.0; cost_bps_per_side_smallcap = 30.0; smallcap_cap_threshold = 2e9
    risk_sizing_actif = True; max_position_risk_pct = 2.0; atr_stop_multiple = 1.0; min_ticket_usd = 100.0
    correlation_active = False; killswitch_actif = False; max_drawdown_pct = 15.0; killswitch_reprise_pct = 10.0
    stop_elargissement_actif = True; stop_atr_multiple_min = 2.0
    gerant_delai_min_jours = 10; gerant_min_allegement_usd = 300.0; gerant_max_allegements = 2
    comite_pre_ouverture_seulement = True; dedup_fenetre_h = 48; dedup_similarite_min = 0.45
    anthropic_api_key = "sk-test"; telegram_bot_token = "0:x"; telegram_chat_id = "0"
    llm_model = "m"; cheap_model = "m"; director_model = "m"; risk_profile = "agressif"; news_feeds = []
fake_cfg.settings = _S()
sys.modules["config.settings"] = fake_cfg

faux_atr, faux_secteur = {"X": 3.0}, {}
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = lambda t, utiliser_cache=True: {"price": 100.0, "market_cap": 3e12,
                                                           "sector": faux_secteur.get(t), "ticker": t.upper()}
fake_mc.get_atr = lambda t, periode=14: faux_atr.get(t)
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

def position(ticker="X", age_j=30, shares=100.0, n_alleg=0, sector="Technology"):
    return pp.Position(ticker=ticker, shares=shares, entry_price=100.0, stop_loss=90.0, profit_target=120.0,
        conviction=0.6, sector=sector, horizon_days=90, n_allegements=n_alleg,
        opened_at=(datetime.now(timezone.utc) - timedelta(days=age_j)).isoformat())

print("=== S15.1 — le STOP est élargi (audit : stops touchés en 12 j, objectifs en 41 j) ===")
pp._cache_atr.clear()
p = pp.Portfolio(starting_capital=100_000.0, cash=100_000.0)
log = pp.buy(p, "X", 100.0, 5.0, stop_loss=98.0, profit_target=120.0, invalidation_price=97.0)
pos = p.positions[0]
check("stop 98$ (dans le bruit, ATR=3$) élargi à 2×ATR = 94$", abs(pos.stop_loss - 94.0) < 1e-9, f"{pos.stop_loss}")
check("invalidation alignée (97$ → 94$)", abs(pos.invalidation_price - 94.0) < 1e-9)
check("perte au stop ≤ budget de risque 2 000$", pos.shares * 6.0 <= 2001, f"{pos.shares*6:.0f}$")
pp._cache_atr.clear(); p = pp.Portfolio(starting_capital=100_000.0, cash=100_000.0)
pp.buy(p, "X", 100.0, 5.0, stop_loss=88.0, profit_target=120.0)
check("stop déjà large (88$) laissé intact", abs(p.positions[0].stop_loss - 88.0) < 1e-9)

print("\n=== S15.2 — REPRODUCTION DU CHARCUTAGE RÉEL (CUBI : 17 allègements → 0,003$) ===")
p = pp.Portfolio(starting_capital=10_000.0, cash=9_600.0)
cubi = position("CUBI", age_j=40, shares=4.0)          # 400$ comme dans l'audit
p.positions.append(cubi)
n_ok = 0
for i in range(17):                                     # le Gérant tente 17 fois, comme en vrai
    ok, motif = pp.allegement_autorise(cubi, 100.0, 0.5)
    if ok:
        pp.trim_position(p, cubi, 100.0, 0.5, "gerant_alleger"); n_ok += 1
check(f"17 tentatives → seulement {n_ok} allègement(s) autorisé(s) (avant : 17)", n_ok <= 2, f"{n_ok}")
check("la position SURVIT (≥ 100$ au lieu de 0,003$)", cubi.shares * 100.0 >= 100.0, f"{cubi.shares*100:.2f}$")
ok, motif = pp.allegement_autorise(position(age_j=3), 100.0, 0.5)
check("détenue 3 j → BLOQUÉ (délai 10 j)", not ok and "respirer" in motif, motif)
ok, motif = pp.allegement_autorise(position(age_j=30, shares=0.5), 100.0, 0.5)
check("lot de 25$ (le lot MÉDIAN de l'audit) → BLOQUÉ", not ok and "frais" in motif, motif)
ok, motif = pp.allegement_autorise(position(age_j=30, n_alleg=2), 100.0, 0.5)
check("déjà allégée 2× → BLOQUÉ (VENDRE ou GARDER)", not ok, motif)
ok, _ = pp.allegement_autorise(position(age_j=30, shares=100.0), 100.0, 0.5)
check("position mûre de 10 000$ → allègement AUTORISÉ (le Gérant garde son rôle)", ok)

print("\n=== S15.3 — Backfill des secteurs (EG = 1 400$ sans secteur dans l'audit) ===")
faux_secteur.update({"EG": "Financial Services", "ECPG": "Financial Services"})
p = pp.Portfolio(starting_capital=10_000.0, cash=5_000.0)
p.positions += [position("EG", sector=""), position("ECPG", sector="Financial Services — Distressed Credit & Debt Collection"),
                position("KO", sector="Consumer Defensive")]
j = pp.backfill_secteurs(p)
check("EG (vide) → Financial Services", p.positions[0].sector == "Financial Services")
check("ECPG (libellé libre du LLM) → normalisé", p.positions[1].sector == "Financial Services")
check("KO (déjà officiel) intact", p.positions[2].sector == "Consumer Defensive")

print("\n=== T1.1 — Fenêtre de délibération ===")
import src.core.pipeline as pipe
mtl = lambda h, m=0: datetime(2026, 9, 15, h, m, tzinfo=timezone(timedelta(hours=-4)))
check("7h Montréal → pré-ouverture (comité AUTORISÉ, fill à l'open du jour)", pipe._fenetre_marche(mtl(7)) == "pre_ouverture")
check("8h Montréal (screener) → pré-ouverture", pipe._fenetre_marche(mtl(8)) == "pre_ouverture")
check("9h29 → pré-ouverture", pipe._fenetre_marche(mtl(9, 29)) == "pre_ouverture")
check("12h Montréal → séance (mécanique seulement, aucun LLM)", pipe._fenetre_marche(mtl(12)) == "seance")
check("17h Montréal → post-clôture (Gérant oui, comité non)", pipe._fenetre_marche(mtl(17)) == "post_cloture")
src = open("src/core/pipeline.py", encoding="utf-8").read()
check("le gating 'seance' est APRÈS les protections mécaniques", src.index("Snapshot performance") < src.index('fenetre == "seance"'))
check("le Gérant tourne AVANT le gating post-clôture", src.index("Revue du Gérant") < src.index('fenetre == "post_cloture"'))

print("\n=== T1.2 — Déduplication (audit : même thème proposé 9×) ===")
import src.memory.vector_store as vs
maintenant = datetime.now(timezone.utc)
def rec(theme, action, h_ago):
    return {"id": "x", "document": f"Catalyseur: regulation. Thème: {theme}. Secteur: Financial Services. Tickers: KRE. Décision: {action}. Confiance: 0.4. Raisonnement: r",
            "metadata": {"action": action, "decided_at": (maintenant - timedelta(hours=h_ago)).isoformat()}}
class These: pass
t = These(); t.theme = "Répression réglementaire bancaire régionale : vide de crédit et re-rating des prêteurs alternatifs"
vs._charger_enregistrements = lambda: [rec("Répression réglementaire bancaire régionale → vide de crédit, prêteurs alternatifs", "watchlist", 5)]
motif = pipe._these_deja_jugee(t)
check("thème quasi identique jugé WATCHLIST il y a 5h → comité INTERROMPU", motif is not None and "WATCHLIST" in motif, str(motif))
vs._charger_enregistrements = lambda: [rec("Répression réglementaire bancaire régionale → vide de crédit", "watchlist", 80)]
check("même thème mais il y a 80h (> 48h) → on redélibère", pipe._these_deja_jugee(t) is None)
vs._charger_enregistrements = lambda: [rec("Répression réglementaire bancaire régionale → vide de crédit", "execute", 5)]
check("même thème mais EXECUTE → pas filtré ici (anti-doublons du portefeuille)", pipe._these_deja_jugee(t) is None)
vs._charger_enregistrements = lambda: [rec("Choc pétrolier moyen-orient et raffineurs", "watchlist", 5)]
check("thème DIFFÉRENT → on délibère", pipe._these_deja_jugee(t) is None)
vs._charger_enregistrements = lambda: (_ for _ in ()).throw(RuntimeError("panne"))
check("mémoire indisponible → on délibère (dégradation permissive)", pipe._these_deja_jugee(t) is None)

print(f"\n{'='*60}\n  RÉSULTAT S15+T1 : {VERT}{_ok} réussis{RESET}, {ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*60}")
exit(1 if _ko else 0)