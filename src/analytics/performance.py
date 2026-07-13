# src/analytics/performance.py
"""
Couche de MESURE — la seule chose qui dira un jour si le système a un edge.

Deux briques indépendantes :

1) SNAPSHOT QUOTIDIEN (à appeler à chaque cron — idempotent par date) :
   photographie l'équity du jour (cash + valeur de marché des positions) et le
   taux de déploiement. Sans cette série quotidienne, Sharpe / bêta / alpha /
   drawdown sont IMPOSSIBLES à calculer. C'est la donnée brute qu'on ne peut
   pas reconstruire a posteriori : il faut la capter chaque jour.

2) RAPPORT DE PERFORMANCE (à la demande ou hebdomadaire) :
   - Statistiques de trades (registre `closed` du portefeuille) : taux de
     réussite avec intervalle de confiance, profit factor, gain/perte moyens,
     espérance par trade, durée de détention, ventilation par motif de sortie,
     BRUT et NET de coûts simulés.
   - Statistiques de série (snapshots quotidiens) : Sharpe, drawdown max,
     bêta et alpha (Jensen) vs SPY dividendes réinvestis, alpha de SÉLECTION
     corrigé du cash drag (benchmark mixte : w×SPY + (1−w)×cash), t-stats.
   - Honnêteté statistique intégrée : chaque chiffre est accompagné de son
     incertitude ; le rapport REFUSE de conclure sous les seuils de
     significativité au lieu de laisser croire à un signal.

Principes :
- Aucun nombre ne vient d'un LLM : équity, prix, benchmark = API/registre.
- Idempotent : re-exécuter le snapshot le même jour ÉCRASE la même clé date
  (un cron rejoué ne crée jamais de doublon).
- Anti perte de données : si la LECTURE du stockage échoue, on N'ÉCRIT PAS
  (on ne remplace jamais un historique par une liste vide à cause d'un
  incident réseau — contrairement au pattern actuel de vector_store /
  world_memory, qu'il faudra corriger de la même façon).

Stockage : Upstash Redis clé `perf_snapshots` (un dict JSON {date: ligne}),
repli fichier local data/perf_snapshots.json — même logique que le portefeuille.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# ── Réglages ────────────────────────────────────────────────────────────────
CLE_SNAPSHOTS = "perf_snapshots"
FICHIER_SNAPSHOTS = Path("data/perf_snapshots.json")

COUT_BPS_PAR_COTE = 10.0          # coût simulé par CÔTÉ (10 bps = 0,10 %) : spread + timing
TAUX_SANS_RISQUE_ANNUEL = 0.04    # proxy cash (T-bills) pour Sharpe et benchmark mixte
JOURS_BOURSE_AN = 252
MIN_OBS_SERIE = 20                # sous ce nombre de snapshots, on refuse de conclure
MIN_TRADES_CONCLUSION = 30        # sous ce nombre de trades, tout n'est que bruit

# ── Connexion stockage (même pattern que paper_portfolio) ───────────────────
_redis = None
_url = os.getenv("UPSTASH_REDIS_REST_URL")
_token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
if _url and _token:
    from upstash_redis import Redis
    _redis = Redis(url=_url, token=_token)


class LectureStockageErreur(RuntimeError):
    """Échec de LECTURE du stockage : on doit refuser d'écrire (anti-écrasement)."""


def _charger_snapshots() -> dict:
    """
    Charge le dict {date: ligne}. LÈVE LectureStockageErreur si la lecture
    Redis échoue (au lieu de renvoyer {} et de risquer d'écraser l'historique).
    """
    if _redis is not None:
        try:
            brut = _redis.get(CLE_SNAPSHOTS)
        except Exception as e:
            raise LectureStockageErreur(f"Lecture Redis impossible : {e}") from e
        return json.loads(brut) if brut else {}
    if FICHIER_SNAPSHOTS.exists():
        return json.loads(FICHIER_SNAPSHOTS.read_text(encoding="utf-8"))
    return {}


def _sauver_snapshots(snapshots: dict) -> None:
    charge = json.dumps(snapshots, ensure_ascii=False)
    if _redis is not None:
        _redis.set(CLE_SNAPSHOTS, charge)
        return
    FICHIER_SNAPSHOTS.parent.mkdir(parents=True, exist_ok=True)
    FICHIER_SNAPSHOTS.write_text(charge, encoding="utf-8")


# ═════════════════════════════ 1) SNAPSHOT QUOTIDIEN ════════════════════════
def snapshot_quotidien() -> str:
    """
    Photographie l'équity du jour et l'enregistre sous la clé du jour (UTC).
    Idempotent : appelé plusieurs fois le même jour, il écrase la même entrée
    (la DERNIÈRE photo du jour gagne — donc celle du cron du soir, après clôture).
    À appeler à la fin de CHAQUE cycle (news, screener) : coût quasi nul.
    """
    # Imports locaux : évite tout cycle et permet de tester ce module à sec.
    from src.portfolio.paper_portfolio import load_portfolio
    from src.ingestion.market_client import get_fundamentals

    p = load_portfolio()
    valeur_positions, tickers_prix_manquant = 0.0, []
    for pos in p.positions:
        prix = get_fundamentals(pos.ticker).get("price")
        if prix:
            valeur_positions += pos.shares * prix
        else:
            # Prix indisponible : on retient le coût d'entrée MAIS on le journalise,
            # pour que le rapport sache que ce point d'équity est approximatif.
            valeur_positions += pos.shares * pos.entry_price
            tickers_prix_manquant.append(pos.ticker)

    equity = p.cash + valeur_positions
    aujourdhui = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ligne = {
        "date": aujourdhui,
        "equity": round(equity, 2),
        "cash": round(p.cash, 2),
        "deployed": round(valeur_positions, 2),
        "n_positions": len(p.positions),
        "n_closed_total": len(p.closed),
        "prix_manquants": tickers_prix_manquant,
        "horodatage_utc": datetime.now(timezone.utc).isoformat(),
    }

    try:
        snapshots = _charger_snapshots()
    except LectureStockageErreur as e:
        # Lecture impossible → on N'ÉCRIT PAS (on ne détruit jamais l'historique).
        return f"⚠️ Snapshot NON enregistré (lecture stockage en échec : {e})"

    snapshots[aujourdhui] = ligne
    _sauver_snapshots(snapshots)
    suffixe = f" (prix manquants : {tickers_prix_manquant})" if tickers_prix_manquant else ""
    return f"📸 Snapshot {aujourdhui} : équity {equity:.0f}$ | déployé {valeur_positions:.0f}$ | cash {p.cash:.0f}${suffixe}"


# ═════════════════════ 2a) STATISTIQUES DE TRADES (registre) ════════════════
def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance de Wilson à 95 % pour une proportion k/n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    marge = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - marge), min(1.0, centre + marge))


def _duree_jours(ouverture: str, cloture: str) -> float | None:
    try:
        d0 = datetime.fromisoformat(ouverture)
        d1 = datetime.fromisoformat(cloture)
        if d0.tzinfo is None:
            d0 = d0.replace(tzinfo=timezone.utc)
        if d1.tzinfo is None:
            d1 = d1.replace(tzinfo=timezone.utc)
        return (d1 - d0).total_seconds() / 86400.0
    except (ValueError, TypeError, KeyError):
        return None


def stats_trades(closed: list[dict], cout_bps_par_cote: float = COUT_BPS_PAR_COTE) -> dict:
    """
    Statistiques du registre de trades clôturés (liste de ClosedPosition en dict).
    Calcule BRUT et NET d'un coût simulé appliqué aux deux côtés :
      coût = (entrée + sortie) × actions × bps/10 000.
    Fonction PURE (aucun accès réseau/stockage) → testable à sec.
    """
    n = len(closed)
    if n == 0:
        return {"n": 0, "message": "Aucun trade clôturé : rien à mesurer."}

    lignes = []
    for c in closed:
        pnl_brut = c["realized_pnl"]
        # R1a — si le portefeuille a stocké les frais RÉELS (entry_cost/exit_cost), on les
        # utilise. Sinon (trades hérités d'avant R1a), on retombe sur l'estimation bps.
        cout_reel = (c.get("entry_cost") or 0.0) + (c.get("exit_cost") or 0.0)
        if cout_reel > 0:
            cout = cout_reel
        else:
            notionnel = (c["entry_price"] + c["exit_price"]) * c["shares"]
            cout = notionnel * cout_bps_par_cote / 10_000.0
        lignes.append({
            "ticker": c["ticker"],
            "pnl_brut": pnl_brut,
            "cout": cout,
            "pnl_net": pnl_brut - cout,
            "motif": c.get("exit_reason", "?"),
            "duree_j": _duree_jours(c.get("opened_at", ""), c.get("closed_at", "")),
        })

    df = pd.DataFrame(lignes)
    gagnants = df[df["pnl_net"] > 0]
    perdants = df[df["pnl_net"] <= 0]
    k = len(gagnants)
    ci_bas, ci_haut = _wilson_ci(k, n)

    gains = gagnants["pnl_net"].sum()
    pertes = abs(perdants["pnl_net"].sum())
    profit_factor_net = (gains / pertes) if pertes > 0 else float("inf")

    g_brut = df[df["pnl_brut"] > 0]["pnl_brut"].sum()
    p_brut = abs(df[df["pnl_brut"] <= 0]["pnl_brut"].sum())
    profit_factor_brut = (g_brut / p_brut) if p_brut > 0 else float("inf")

    durees = df["duree_j"].dropna()
    par_motif = (
        df.groupby("motif")["pnl_net"]
        .agg(n="count", pnl_total="sum")
        .reset_index()
        .to_dict("records")
    )

    return {
        "n": n,
        "taux_reussite": k / n,
        "taux_reussite_ci95": (ci_bas, ci_haut),
        "profit_factor_brut": profit_factor_brut,
        "profit_factor_net": profit_factor_net,
        "gain_moyen": float(gagnants["pnl_net"].mean()) if k else 0.0,
        "perte_moyenne": float(perdants["pnl_net"].mean()) if len(perdants) else 0.0,
        "esperance_par_trade_net": float(df["pnl_net"].mean()),
        "pnl_total_brut": float(df["pnl_brut"].sum()),
        "couts_simules_total": float(df["cout"].sum()),
        "pnl_total_net": float(df["pnl_net"].sum()),
        "duree_mediane_jours": float(durees.median()) if len(durees) else None,
        "par_motif_sortie": par_motif,
        "conclusif": n >= MIN_TRADES_CONCLUSION,
    }


# ═══════════════ 2b) STATISTIQUES DE SÉRIE (snapshots quotidiens) ═══════════
def stats_series(
    equity: pd.Series,
    deployed: pd.Series,
    bench_close: pd.Series,
    rf_annuel: float = TAUX_SANS_RISQUE_ANNUEL,
) -> dict:
    """
    Métriques risque/rendement à partir des séries quotidiennes.
      equity      : équity totale par date (index = dates de snapshot)
      deployed    : valeur de marché investie par date (même index)
      bench_close : cours AJUSTÉ (dividendes réinvestis) du benchmark, série
                    quotidienne couvrant la période (SPY auto_adjust=True)
    Fonction PURE (le benchmark est injecté) → testable sans réseau.

    Renvoie : Sharpe, drawdown max, bêta, alpha de Jensen annualisé + t-stat,
    alpha de SÉLECTION (vs benchmark mixte w×SPY + (1−w)×cash) + t-stat,
    taux de déploiement moyen, coût du cash drag estimé.
    """
    equity = equity.sort_index().dropna()
    if len(equity) < 3:
        return {"n_obs": len(equity), "message": "Moins de 3 snapshots : aucune métrique de série possible."}

    # Benchmark aligné sur les dates de snapshot (ffill : dernier cours connu).
    # Ainsi, si un snapshot manque un jour, le rendement du portefeuille couvre
    # 2 jours ET celui du benchmark aussi — la comparaison reste à armes égales.
    bench = bench_close.sort_index().reindex(equity.index, method="ffill")

    r_p = equity.pct_change().dropna()
    r_m = bench.pct_change().dropna()
    aligne = pd.concat([r_p.rename("rp"), r_m.rename("rm")], axis=1).dropna()
    n = len(aligne)
    if n < 3:
        return {"n_obs": n, "message": "Séries trop courtes après alignement."}

    rf_j = (1 + rf_annuel) ** (1 / JOURS_BOURSE_AN) - 1
    ex_p = aligne["rp"] - rf_j          # excès de rendement du portefeuille
    ex_m = aligne["rm"] - rf_j          # excès de rendement du marché

    # ── Sharpe annualisé + erreur-type (Lo, 2002 ; hypothèse i.i.d.) ──
    vol_j = ex_p.std(ddof=1)
    sharpe = float(ex_p.mean() / vol_j * math.sqrt(JOURS_BOURSE_AN)) if vol_j > 0 else 0.0
    annees = n / JOURS_BOURSE_AN
    se_sharpe = math.sqrt((1 + 0.5 * sharpe ** 2) / annees) if annees > 0 else float("inf")

    # ── Drawdown maximal sur l'équity ──
    cummax = equity.cummax()
    drawdown_max = float(((equity - cummax) / cummax).min())

    # ── Bêta et alpha de Jensen (régression excès vs excès) ──
    var_m = ex_m.var(ddof=1)
    beta = float(ex_p.cov(ex_m) / var_m) if var_m > 0 else 0.0
    alpha_j = float(ex_p.mean() - beta * ex_m.mean())         # alpha quotidien
    residus = ex_p - (alpha_j + beta * ex_m)
    se_alpha_j = residus.std(ddof=2) / math.sqrt(n) if n > 2 else float("inf")
    t_alpha = alpha_j / se_alpha_j if se_alpha_j > 0 else 0.0
    alpha_ann = alpha_j * JOURS_BOURSE_AN

    # ── Correction du cash drag : benchmark MIXTE w×marché + (1−w)×cash ──
    # w = part investie la VEILLE (c'est l'allocation qui a produit le rendement du jour).
    w = (deployed.sort_index().reindex(equity.index).ffill() / equity).clip(0, 1.5).shift(1)
    w = w.reindex(aligne.index).fillna(0.0)
    r_mixte = w * aligne["rm"] + (1 - w) * rf_j
    ecart_selection = aligne["rp"] - r_mixte
    alpha_selection_ann = float(ecart_selection.mean() * JOURS_BOURSE_AN)
    se_sel = ecart_selection.std(ddof=1) / math.sqrt(n)
    t_selection = float(ecart_selection.mean() / se_sel) if se_sel > 0 else 0.0

    # Coût d'opportunité du cash (ce que le cash non investi a « raté » vs marché)
    cash_drag_ann = float(((1 - w) * (aligne["rm"] - rf_j)).mean() * JOURS_BOURSE_AN)

    perf_totale = float(equity.iloc[-1] / equity.iloc[0] - 1)
    perf_bench = float(bench.iloc[-1] / bench.iloc[0] - 1)

    return {
        "n_obs": n,
        "annees": annees,
        "perf_totale": perf_totale,
        "perf_bench_totale": perf_bench,
        "sharpe": sharpe,
        "sharpe_se": se_sharpe,
        "drawdown_max": drawdown_max,
        "beta": beta,
        "alpha_jensen_annualise": alpha_ann,
        "t_alpha": float(t_alpha),
        "alpha_selection_annualise": alpha_selection_ann,
        "t_alpha_selection": t_selection,
        "deploiement_moyen": float(w.mean()),
        "cash_drag_annualise": cash_drag_ann,
        "conclusif": n >= MIN_OBS_SERIE,
    }


# ═════════════════════ Benchmark (dividendes réinvestis) ════════════════════
def _telecharger_bench(dates: pd.DatetimeIndex, symbole: str = "SPY") -> pd.Series:
    """Cours AJUSTÉ du benchmark sur la période (auto_adjust=True ⇒ dividendes
    réinvestis — comparer ton portefeuille au SPY en PRIX seul t'offrirait
    ~1,3 %/an d'alpha fictif). Isolé pour rester injectable dans les tests."""
    import yfinance as yf
    debut = (dates.min() - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    fin = (dates.max() + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    hist = yf.download(symbole, start=debut, end=fin, auto_adjust=True, progress=False)
    serie = hist["Close"]
    if isinstance(serie, pd.DataFrame):          # yfinance récent : colonnes multi-index
        serie = serie.iloc[:, 0]
    serie.index = pd.to_datetime(serie.index).tz_localize(None)
    return serie


# ═══════════════════════════ Rapport assemblé ═══════════════════════════════
def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def rapport(cout_bps_par_cote: float = COUT_BPS_PAR_COTE) -> str:
    """Assemble le rapport complet (trades + série + honnêteté statistique)."""
    from src.portfolio.paper_portfolio import load_portfolio

    p = load_portfolio()
    lignes = ["📏 RAPPORT DE PERFORMANCE (paper trading)", ""]

    # ── 1) Trades clôturés ──
    st = stats_trades([c.model_dump() for c in p.closed], cout_bps_par_cote)
    if st["n"] == 0:
        lignes.append("Trades clôturés : aucun. Rien à conclure côté trades.")
    else:
        bas, haut = st["taux_reussite_ci95"]
        pf = st["profit_factor_net"]
        pf_txt = f"{pf:.2f}" if math.isfinite(pf) else "∞ (aucune perte)"
        lignes += [
            f"— TRADES (n = {st['n']}, coûts simulés {cout_bps_par_cote:.0f} bps/côté) —",
            f"Taux de réussite : {st['taux_reussite']*100:.0f}%  "
            f"[IC95 : {bas*100:.0f}%–{haut*100:.0f}%]",
            f"Profit factor net : {pf_txt} | Espérance/trade : {st['esperance_par_trade_net']:+.0f}$",
            f"Gain moyen : {st['gain_moyen']:+.0f}$ | Perte moyenne : {st['perte_moyenne']:+.0f}$",
            f"P&L brut : {st['pnl_total_brut']:+.0f}$ | Coûts simulés : −{st['couts_simules_total']:.0f}$ "
            f"| P&L net : {st['pnl_total_net']:+.0f}$",
        ]
        if st["duree_mediane_jours"] is not None:
            lignes.append(f"Détention médiane : {st['duree_mediane_jours']:.0f} j")
        lignes.append("Par motif de sortie : " + " | ".join(
            f"{m['motif']} n={m['n']} ({m['pnl_total']:+.0f}$)" for m in st["par_motif_sortie"]))
        if not st["conclusif"]:
            lignes.append(f"⚠️ n < {MIN_TRADES_CONCLUSION} : ces chiffres sont du BRUIT, "
                          "pas un signal. Ne change pas la stratégie sur cette base.")
        lignes.append("")

    # ── 2) Série quotidienne ──
    try:
        snaps = _charger_snapshots()
    except LectureStockageErreur as e:
        snaps = {}
        lignes.append(f"⚠️ Snapshots illisibles ({e}).")

    if len(snaps) < 3:
        lignes.append(f"Snapshots quotidiens : {len(snaps)} — il en faut ≥ 3 pour les métriques "
                      "de série (Sharpe, bêta, alpha). Laisse tourner.")
        return "\n".join(lignes)

    df = pd.DataFrame(sorted(snaps.values(), key=lambda r: r["date"]))
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    bench = _telecharger_bench(df.index)
    ss = stats_series(df["equity"], df["deployed"], bench)

    if "message" in ss:
        lignes.append(ss["message"])
        return "\n".join(lignes)

    lignes += [
        f"— SÉRIE ({ss['n_obs']} jours ≈ {ss['annees']:.2f} an) —",
        f"Portefeuille : {_fmt_pct(ss['perf_totale'])} | SPY (div. réinvestis) : {_fmt_pct(ss['perf_bench_totale'])}",
        f"Sharpe : {ss['sharpe']:.2f} (± {ss['sharpe_se']:.2f}) | Drawdown max : {_fmt_pct(ss['drawdown_max'])}",
        f"Bêta : {ss['beta']:.2f} | Alpha (Jensen) : {_fmt_pct(ss['alpha_jensen_annualise'])}/an "
        f"(t = {ss['t_alpha']:.2f})",
        f"Déploiement moyen : {ss['deploiement_moyen']*100:.0f}% | "
        f"Cash drag : {_fmt_pct(ss['cash_drag_annualise'])}/an",
        f"Alpha de SÉLECTION (corrigé du cash) : {_fmt_pct(ss['alpha_selection_annualise'])}/an "
        f"(t = {ss['t_alpha_selection']:.2f})",
    ]
    verdict_ok = ss["conclusif"] and abs(ss["t_alpha"]) >= 2 and st.get("n", 0) >= MIN_TRADES_CONCLUSION
    if verdict_ok:
        sens = "POSITIF" if ss["alpha_jensen_annualise"] > 0 else "NÉGATIF"
        lignes.append(f"✅ Signal statistiquement détectable : alpha {sens} (|t| ≥ 2).")
    else:
        lignes.append("⚖️ Verdict : INDÉTERMINÉ. |t| < 2 ou historique trop court — "
                      "aucune conclusion honnête possible sur l'edge à ce stade.")
    return "\n".join(lignes)


if __name__ == "__main__":
    print(snapshot_quotidien())
    print()
    print(rapport())