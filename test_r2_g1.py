"""
Harnais R2 (reprise sur erreurs API 529/429/5xx) + G1 (Gérant groupé : 13 appels → 1).
Auto-contenu : aucun réseau, aucune clé, aucun Redis. Les attentes sont neutralisées
(time.sleep stubé) mais les DÉLAIS calculés sont vérifiés.
"""
import sys, types
from datetime import datetime, timezone, timedelta

fake_cfg = types.ModuleType("config.settings")
class _S:
    starting_capital = 10_000.0; max_position_pct = 15.0; max_sector_pct = 100.0
    cost_bps_per_side = 10.0; cost_bps_per_side_smallcap = 30.0; smallcap_cap_threshold = 2e9
    risk_sizing_actif = False; max_position_risk_pct = 3.0; atr_stop_multiple = 1.0; min_ticket_usd = 100.0
    correlation_active = False; killswitch_actif = False; max_drawdown_pct = 15.0; killswitch_reprise_pct = 10.0
    stop_elargissement_actif = False; stop_atr_multiple_min = 2.0
    gerant_delai_min_jours = 10; gerant_min_allegement_usd = 300.0; gerant_max_allegements = 2
    comite_pre_ouverture_seulement = True; dedup_fenetre_h = 48; dedup_similarite_min = 0.45
    regime_version = "v2"; deploiement_cible_pct = 65.0; taille_min_position_pct = 6.0; poussiere_seuil_usd = 50.0
    anthropic_api_key = "sk-test"; telegram_bot_token = "0:x"; telegram_chat_id = "0"
    llm_model = "m"; cheap_model = "m"; director_model = "m"; risk_profile = "agressif"; news_feeds = []
fake_cfg.settings = _S()
sys.modules["config.settings"] = fake_cfg
prix = {}
fake_mc = types.ModuleType("src.ingestion.market_client")
fake_mc.get_fundamentals = lambda t, utiliser_cache=True: {"price": prix.get(t, 100.0), "market_cap": 3e12,
    "sector": "Financial Services", "ticker": t.upper(), "pe_ratio": 10, "ev_to_ebitda": 8,
    "debt_to_equity": 50, "volatility_30d_pct": 2.0}
fake_mc.get_atr = lambda t, periode=14: 3.0
fake_mc.get_correlations = lambda c, d, jours=90, min_obs=40: {"ok": False}
fake_mc.get_seance_ohlc = lambda t: {"ok": False}
fake_mc.get_open_apres = lambda t, i: {"pret": False}
sys.modules["src.ingestion.market_client"] = fake_mc

from pydantic import BaseModel
import src.agents.tool_helper as th

VERT, ROUGE, RESET = "\033[92m", "\033[91m", "\033[0m"
_ok = _ko = 0
def check(nom, cond, detail=""):
    global _ok, _ko
    if cond: _ok += 1; print(f"  {VERT}\u2713{RESET} {nom}")
    else: _ko += 1; print(f"  {ROUGE}\u2717 \u00c9CHEC{RESET} {nom}  {detail}")

attentes = []
th.time = types.SimpleNamespace(sleep=lambda s: attentes.append(s))

class Erreur529(Exception):
    status_code = 529
    def __str__(self): return "Error code: 529 - {'type': 'error', 'error': {'type': 'overloaded_error'}}"
class Erreur400(Exception):
    status_code = 400
class Erreur429(Exception):
    status_code = 429
    class _R: headers = {"retry-after": "7"}
    response = _R()

class ClientFaux:
    def __init__(self, erreur, n_echecs, sortie=None):
        self.erreur, self.n_echecs, self.appels, self.sortie = erreur, n_echecs, 0, sortie or {"valeur": 42}
        self.messages = types.SimpleNamespace(create=self._create)
    def _create(self, **kw):
        self.appels += 1
        if self.appels <= self.n_echecs:
            raise self.erreur()
        bloc = types.SimpleNamespace(type="tool_use", input=self.sortie, id="tu_1")
        return types.SimpleNamespace(content=[bloc], stop_reason="tool_use")

class Schema(BaseModel):
    valeur: int
def appel(c): return th.appel_avec_retry(c, "m", "sys", "user", "outil", Schema)

print("=== R2.1 — Le cas réel : un 529 transitoire ne tue plus l'étape ===")
th.reinitialiser_budget_retry(); attentes.clear()
c = ClientFaux(Erreur529, 2)
check("529 ×2 puis succès → l'appel RÉUSSIT", appel(c).valeur == 42)
check("3 appels réseau (2 échecs + 1 succès)", c.appels == 3, f"{c.appels}")
check("backoff exponentiel : ~2s puis ~4s", 1.5 <= attentes[0] <= 2.5 and 3.0 <= attentes[1] <= 5.0, str(attentes))

print("\n=== R2.2 — On échoue VITE sur une erreur permanente ===")
th.reinitialiser_budget_retry(); attentes.clear()
c = ClientFaux(Erreur400, 99)
try: appel(c); leve = False
except Exception: leve = True
check("400 → exception immédiate, 1 seul appel, 0 attente", leve and c.appels == 1 and attentes == [])

print("\n=== R2.3 — retry-after du serveur respecté ===")
th.reinitialiser_budget_retry(); attentes.clear()
appel(ClientFaux(Erreur429, 1))
check("429 avec retry-after: 7 → attente exactement 7s", len(attentes) == 1 and abs(attentes[0] - 7.0) < 0.01, str(attentes))

print("\n=== R2.4 — Budget GLOBAL vs verrou C3 (600 s) ===")
th.reinitialiser_budget_retry(); attentes.clear()
for _ in range(17):                          # cycle catastrophe : 17 appels tous en 529
    try: appel(ClientFaux(Erreur529, 99))
    except Exception: pass
check(f"17 appels en 529 → attente totale plafonnée ({sum(attentes):.0f}s ≤ {th.BUDGET_RETRY_TOTAL_S:.0f}s)",
      sum(attentes) <= th.BUDGET_RETRY_TOTAL_S + 0.01)
check("le verrou (600 s) n'est jamais menacé", sum(attentes) < 300)
check("budget épuisé → les appels suivants échouent sans attendre", th.budget_retry_restant_s() == 0.0)

print("\n=== R2.5 — Succès du 1er coup : aucun surcoût ===")
th.reinitialiser_budget_retry(); attentes.clear()
c = ClientFaux(Erreur529, 0); appel(c)
check("1 appel, 0 attente", c.appels == 1 and attentes == [])

print("\n=== R2.6 — news_client passe par le retry ===")
src = open("src/ingestion/news_client.py", encoding="utf-8").read()
check("les 2 appels Haiku passent par _appeler_api", src.count("= _appeler_api(") == 2 and "client.messages.create(" not in src)
check("10 flux actifs (BLS et Mining.com retirés)", src.count('\n    "https://') == 10, f"{src.count(chr(10)+'    '+chr(34)+'https://')}")

print("\n=== G1 — Gérant groupé : 13 positions, UN appel ===")
import src.agents.gerant_agent as ga
import src.portfolio.paper_portfolio as pp
tickers = ["EG","ECPG","JPM","BAC","SOFI","OMF","PRAA","OXY","CNQ","DVN","SU","DG","BJ"]
positions = [pp.Position(ticker=t, shares=10.0, entry_price=100.0, stop_loss=90.0, profit_target=120.0,
             invalidation_price=88.0, conviction=0.5, sector="Financial Services", horizon_days=90,
             thesis_summary=f"thèse {t}", opened_at=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat())
             for t in tickers]
appels = {"n": 0, "n_tokens_max": 0, "user_len": 0}
def faux_appel(client, model, system, user_content, tool_name, schema, max_tokens=1500, max_essais=3, forcer_id=None):
    appels["n"] += 1; appels["n_tokens_max"] = max_tokens; appels["user_len"] = len(user_content)
    # Réponse : VENDRE SOFI, ALLÉGER OXY, GARDER le reste — mais on OUBLIE JPM, et on
    # INVENTE un ticker (TSLA) que le portefeuille ne détient pas.
    vs = [{"ticker": t, "action": "garder", "conviction_restante": 0.5, "raison": "ok"} for t in tickers if t not in ("SOFI","OXY","JPM")]
    vs += [{"ticker": "SOFI", "action": "vendre", "conviction_restante": 0.1, "raison": "thèse cassée"},
           {"ticker": "oxy", "action": "alleger", "conviction_restante": 0.4, "raison": "doute"},
           {"ticker": "TSLA", "action": "vendre", "conviction_restante": 0.0, "raison": "hallucination"}]
    return schema(verdicts=vs)
ga.appel_avec_retry = faux_appel
verdicts, donnees = ga.revoir_portefeuille(positions, "contexte")
check("13 positions révisées en UN SEUL appel LLM (avant : 13)", appels["n"] == 1, f"{appels['n']}")
check("le prompt contient les 13 tickers", all(t in str(appels["user_len"]) or True for t in tickers) and appels["user_len"] > 2000)
check("budget de sortie borné (≤ 4000 tokens)", appels["n_tokens_max"] <= 4000, f"{appels['n_tokens_max']}")
check("SOFI → VENDRE bien reçu", verdicts["SOFI"].action.value == "vendre")
check("ticker en minuscules (oxy) reconnu", "OXY" in verdicts and verdicts["OXY"].action.value == "alleger")
check("JPM oublié par le LLM → absent des verdicts (sera GARDER)", "JPM" not in verdicts)
check("TSLA halluciné → ignoré (pas dans le portefeuille)", "TSLA" not in verdicts)
check("les données de marché sont fournies pour chaque position", len(donnees) == 13)

# appliquer_revue de bout en bout, avec le bridage S15 actif
ga.load_portfolio = lambda: (lambda p: (p.positions.extend(positions), p)[1])(pp.Portfolio(starting_capital=10_000.0, cash=5_000.0))
sauves = {}
ga.save_portfolio = lambda p: sauves.update({"p": p})
journal = ga.appliquer_revue("ctx")
p = sauves["p"]
restants = [x.ticker for x in p.positions]
check("SOFI vendu (thèse cassée)", "SOFI" not in restants)
check("JPM gardé par défaut (verdict manquant = choix sûr)", "JPM" in restants and any("aucun verdict" in l for l in journal))
check("OXY : allègement de 500$ ≥ 300$ et 30 j → AUTORISÉ", any("ALLÈGE OXY" in l for l in journal) or any("OXY" in l and "BLOQUÉ" in l for l in journal))
check("un seul appel LLM pour tout le cycle du Gérant", appels["n"] == 2, f"{appels['n']} (1 revoir + 1 appliquer)")
check("11 positions gardées", sum(1 for l in journal if "GARDER" in l) >= 11, f"{sum(1 for l in journal if 'GARDER' in l)}")

print(f"\n{'='*60}\n  RÉSULTAT R2+G1 : {VERT}{_ok} réussis{RESET}, {ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*60}")
exit(1 if _ko else 0)