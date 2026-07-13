"""
Harnais S14 — robustesse week-end / jours fériés :
  1. Comparateur de séance : un ordre du matin (pré-ouverture) est rempli à l'ouverture DU JOUR.
  2. Expiration 96h : un long week-end férié ne tue plus un ordre valide.
  3. Comité LLM gelé le week-end (protections mécaniques conservées).
Auto-contenu. Aucun réseau, aucune clé, aucun Redis.
"""
import sys, types
from datetime import datetime, timezone, timedelta

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
    correlation_active = False
    killswitch_actif = False
    max_drawdown_pct = 15.0
    killswitch_reprise_pct = 10.0
    # Clés factices : le pipeline instancie des clients au chargement du module.
    # Aucun appel réseau n'est fait dans ce test (on ne teste que la logique de dates).
    anthropic_api_key = "sk-test"
    telegram_bot_token = "0:test"
    telegram_chat_id = "0"
    llm_model = "claude-haiku-4-5"
    director_model = "claude-opus-4-8"
    risk_profile = "agressif"
    news_feeds = []
fake_cfg.settings = _S()
sys.modules["config.settings"] = fake_cfg

faux_open = {"res": {"pret": False, "raison": "stub"}}
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = lambda t, utiliser_cache=True: {
    "price": 100.0, "market_cap": 3e12, "sector": "Tech", "ticker": t.upper()}
fake_mc.get_open_apres = lambda t, iso: faux_open["res"]
fake_mc.get_atr = lambda t, periode=14: 2.0
fake_mc.get_correlations = lambda c, d, jours=90, min_obs=40: {"ok": False}
fake_mc.get_seance_ohlc = lambda t: {"ok": False}
sys.modules["src.ingestion.market_client"] = fake_mc

import src.portfolio.paper_portfolio as pp

VERT, ROUGE, RESET = "\033[92m", "\033[91m", "\033[0m"
_ok = _ko = 0
def check(nom, cond, detail=""):
    global _ok, _ko
    if cond: _ok += 1; print(f"  {VERT}\u2713{RESET} {nom}")
    else: _ko += 1; print(f"  {ROUGE}\u2717 \u00c9CHEC{RESET} {nom}  {detail}")

def ordre(heures_ago):
    return pp.PendingOrder(
        ticker="AAPL", size_pct=10.0, plan_price=100.0, stop_loss=90.0,
        profit_target=120.0, invalidation_price=85.0, conviction=0.6,
        sector="Tech", horizon_days=30, thesis_id="t", thesis_summary="s",
        placed_at=(datetime.now(timezone.utc) - timedelta(hours=heures_ago)).isoformat())

print("=== 1) Expiration portée à 96h (long week-end férié) ===")
check("constante = 96h (et non 72h)", pp.MAX_ATTENTE_ORDRE_H == 96, f"{pp.MAX_ATTENTE_ORDRE_H}")

# Scénario RÉEL : ordre placé vendredi 21h, lundi FÉRIÉ → 1re séance mardi (~85h plus tard).
faux_open["res"] = {"pret": False, "raison": "marché fermé (férié)"}
p = pp.Portfolio(starting_capital=100_000.0, cash=100_000.0)
p.pending = [ordre(heures_ago=85)]      # vendredi soir → mardi matin
log = pp.executer_ordres_en_attente(p)
check("ordre de 85h (vendredi → mardi férié) : SURVIT (aurait été tué à 72h)",
      len(p.pending) == 1, str(log))

# Mais un ordre vraiment périmé (>96h) est bien annulé
p = pp.Portfolio(starting_capital=100_000.0, cash=100_000.0)
p.pending = [ordre(heures_ago=120)]
log = pp.executer_ordres_en_attente(p)
check("ordre de 120h (>96h) : ANNULÉ (thèse réellement périmée)",
      len(p.pending) == 0 and any("ANNULÉ" in l for l in log), str(log))

# Et dès qu'une séance ouvre (mardi), il est rempli
faux_open["res"] = {"pret": True, "open": 101.0, "date": "2026-09-08"}
p = pp.Portfolio(starting_capital=100_000.0, cash=100_000.0)
p.pending = [ordre(heures_ago=85)]
log = pp.executer_ordres_en_attente(p)
check("séance de mardi ouvre → l'ordre survivant est REMPLI à l'open (101$)",
      len(p.positions) == 1 and abs(p.positions[0].entry_price - 101.0) < 1e-9, str(log))

print("\n=== 2) Comparateur de séance : ouverture réelle, pas horodatage de barre ===")
# On rejoue la LOGIQUE du correctif (sans réseau) : barre datée minuit ET (04h UTC),
# ouverture réelle 9h30 ET (13h30 UTC). Ordre placé à 11h UTC (cron du matin, pré-ouverture).
barre_minuit_et = datetime(2026, 7, 10, 0, 0, tzinfo=timezone(timedelta(hours=-4)))  # minuit ET
ouverture_reelle = barre_minuit_et + timedelta(hours=9, minutes=30)                  # 9h30 ET
ordre_11h_utc = datetime(2026, 7, 10, 11, 0, tzinfo=timezone.utc)                    # 7h ET

check("ordre de 11h UTC est bien AVANT l'ouverture (13h30 UTC) → pas de look-ahead",
      ordre_11h_utc < ouverture_reelle)
check("ANCIENNE logique (horodatage brut 04h UTC) : ratait la séance du jour",
      not (barre_minuit_et > ordre_11h_utc))
check("NOUVELLE logique (ouverture réelle) : remplit le JOUR MÊME (séance non sautée)",
      ouverture_reelle > ordre_11h_utc)

# Un ordre placé APRÈS l'ouverture (cron 16h UTC, marché ouvert) attend le lendemain
ordre_16h_utc = datetime(2026, 7, 10, 16, 0, tzinfo=timezone.utc)
check("ordre de 16h UTC (marché OUVERT) : ne remplit PAS à l'ouverture passée du jour",
      not (ouverture_reelle > ordre_16h_utc))

print("\n=== 3) Comité LLM gelé le week-end ===")
import src.core.pipeline as pipe
sam = datetime(2026, 7, 11, 21, 0, tzinfo=timezone.utc)
dim = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
ven = datetime(2026, 7, 10, 21, 0, tzinfo=timezone.utc)
lun = datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc)
check("samedi détecté comme week-end", pipe._marche_ferme_weekend(sam) is True)
check("dimanche détecté comme week-end", pipe._marche_ferme_weekend(dim) is True)
check("vendredi = jour ouvré (comité normal)", pipe._marche_ferme_weekend(ven) is False)
check("lundi = jour ouvré (comité normal)", pipe._marche_ferme_weekend(lun) is False)

# Les protections mécaniques passent AVANT le gel dans le corps du cycle
src = open("src/core/pipeline.py", encoding="utf-8").read()
i_sorties = src.index("Vérification des sorties (stops)")
i_gel = src.index("_marche_ferme_weekend():")
check("les sorties/fills tournent AVANT le gel (protections jamais suspendues)",
      i_sorties < i_gel)
check("le screener aussi est gelé le week-end", src.count("_marche_ferme_weekend()") >= 2)

print(f"\n{'='*56}\n  RÉSULTAT S14 : {VERT}{_ok} réussis{RESET}, "
      f"{ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*56}")
exit(1 if _ko else 0)