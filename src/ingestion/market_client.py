# src/ingestion/market_client.py
"""
Couche d'ingestion — Données quantitatives (marché).
C'est la SOURCE DE VÉRITÉ des chiffres. Le LLM ne doit jamais
les inventer : ils viennent toujours d'ici.
"""
import yfinance as yf
import requests
from src.ingestion.ticker_resolver import resolve_ticker


# 🗄️ S3 — Cache par PROCESSUS. Dans un même cycle (un run de cron), on ne réinterroge
# pas yfinance pour un ticker déjà récupéré avec succès. Cela réduit fortement le nombre
# d'appels `t.info` (SPY et chaque position sont sinon lus plusieurs fois par cycle) →
# moins de throttling Yahoo → moins de `price=None` qui désactivent silencieusement les stops.
# Le cache vit le temps du processus : chaque cron repart d'un cache vide (aucune péremption
# à gérer, et donc aucun risque de prix périmé d'un cycle à l'autre).
_CACHE_FONDAMENTAUX: dict[str, dict] = {}


def vider_cache_fondamentaux() -> None:
    """Vide le cache de fondamentaux (utile en test, ou pour forcer un rafraîchissement)."""
    _CACHE_FONDAMENTAUX.clear()


def get_fundamentals(ticker: str, utiliser_cache: bool = True) -> dict:
    """
    Récupère les fondamentaux RÉELS d'une action via Yahoo Finance.

    S3 — Un SUCCÈS est mémorisé pour le reste du processus. Un ÉCHEC n'est PAS mis en
    cache : le throttling est transitoire, on veut pouvoir réessayer plus tard dans le cycle.
    """
    cle = resolve_ticker(ticker)          # LSB devient LXU, etc.

    if utiliser_cache and cle in _CACHE_FONDAMENTAUX:
        return _CACHE_FONDAMENTAUX[cle]

    session = requests.Session()
    session.headers["User-agent"] = "Mozilla/5.0"
    t = yf.Ticker(cle, session=session)

    # 🛡️ Filet de sécurité : un ticker invalide/exotique ne doit JAMAIS tout faire planter
    try:
        try:
            info = t.info
        except Exception:
            import time
            time.sleep(1.5)          # petite pause, puis on retente une fois
            info = t.info
        hist = t.history(period="1mo")
    except Exception as e:
        print(f"  ⚠️  Données indisponibles pour {cle} ({e}) — ticker ignoré.")
        # ⚠️ Échec NON mis en cache : on pourra réessayer ce ticker plus tard dans le cycle.
        return {
            "ticker": cle.upper(), "name": None, "price": None,
            "pe_ratio": None, "debt_to_equity": None, "revenue_growth_yoy": None,
            "market_cap": None, "sector": None, "volatility_30d_pct": None,
            "ev_to_ebitda": None, "price_to_book": None, "avg_volume": None,
            "data_source": "indisponible",
        }

    if not hist.empty:
        daily_returns = hist["Close"].pct_change().dropna()
        volatility_30d = round(daily_returns.std() * 100, 2)  # en %
    else:
        volatility_30d = None

    # Prix robuste : actions → currentPrice ; ETF/indices → regularMarketPrice ou
    # previousClose ; en dernier recours, le dernier cours de clôture de l'historique.
    price = (info.get("currentPrice")
             or info.get("regularMarketPrice")
             or info.get("previousClose"))
    if price is None and not hist.empty:
        price = round(float(hist["Close"].iloc[-1]), 2)

    resultat = {
        "ticker": cle.upper(),
        "name": info.get("shortName"),
        "price": price,
        "pe_ratio": info.get("trailingPE"),
        "debt_to_equity": info.get("debtToEquity"),
        "revenue_growth_yoy": info.get("revenueGrowth"),
        "market_cap": info.get("marketCap"),
        "sector": info.get("sector"),
        "volatility_30d_pct": volatility_30d,
        "ev_to_ebitda": info.get("enterpriseToEbitda"),   # meilleur que le PE sur cycliques
        "price_to_book": info.get("priceToBook"),         # lecture de cycle plus honnête
        # 🎯 S1 — le volume moyen est ENFIN renseigné : le filtre de liquidité devient VIVANT.
        # (plusieurs clés yfinance possibles selon le titre ; on prend la première disponible)
        "avg_volume": (info.get("averageVolume")
                       or info.get("averageDailyVolume3Month")
                       or info.get("averageVolume10days")),
        "data_source": "yfinance",                        # traçabilité : d'où vient le chiffre
    }
    _CACHE_FONDAMENTAUX[cle] = resultat      # S3 — succès mémorisé pour le reste du processus
    return resultat


if __name__ == "__main__":
    # Test rapide : on récupère les vraies données d'Apple
    data = get_fundamentals("AAPL")
    print("\n=== Données RÉELLES récupérées (zéro hallucination) ===")
    for key, value in data.items():
        print(f"  {key:.<22} {value}")

# ─── R1b — Prix d'OUVERTURE de la prochaine séance (réalisme d'exécution) ───
def get_open_apres(ticker: str, date_ordre_iso: str) -> dict:
    """
    R1b — Renvoie le prix d'OUVERTURE de la première séance qui s'est ouverte
    STRICTEMENT APRÈS `date_ordre_iso` (l'instant où l'ordre a été placé).

    Pourquoi : un comité qui tourne à 17h Montréal décide marché FERMÉ. En réel,
    l'ordre partirait à l'ouverture du lendemain, à un prix qui peut gapper. Remplir
    au close du jour (inexécutable) flatte systématiquement le paper trading.

    Renvoie :
      {"pret": True,  "open": 123.45, "date": "2026-07-10"}  -> une séance a ouvert : on remplit
      {"pret": False, "raison": "..."}                        -> pas encore de séance : on attend

    Le prix vient de yfinance (jamais du LLM) — la règle d'or tient.
    """
    from datetime import datetime, timezone
    try:
        t_ordre = datetime.fromisoformat(date_ordre_iso)
        if t_ordre.tzinfo is None:
            t_ordre = t_ordre.replace(tzinfo=timezone.utc)
    except Exception as e:
        return {"pret": False, "raison": f"date d'ordre illisible ({e})"}

    symbole = resolve_ticker(ticker) or ticker
    try:
        hist = yf.Ticker(symbole).history(period="10d", interval="1d")
    except Exception as e:
        return {"pret": False, "raison": f"yfinance indisponible ({e})"}

    if hist is None or hist.empty or "Open" not in hist.columns:
        return {"pret": False, "raison": "aucun historique de séance disponible"}

    from datetime import timedelta

    for horodatage, ligne in hist.iterrows():
        ts = horodatage.to_pydatetime()

        # 🔑 S14 — On compare à l'instant RÉEL D'OUVERTURE de la séance (9h30 heure du
        #    marché), PAS à l'horodatage de la barre. yfinance date la barre journalière à
        #    MINUIT heure du marché (~04h00 UTC). Comparer cet horodatage brut faisait rater
        #    l'ouverture du jour même à un ordre placé le matin (cron 11h UTC = 7h ET, soit
        #    AVANT l'ouverture) : l'ordre sautait une séance entière sans raison.
        #    En ajoutant 9h30 à la barre, on obtient l'ouverture réelle, avec l'heure d'été
        #    gérée automatiquement par le fuseau porté par l'index.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
            ouverture_seance = ts + timedelta(hours=13, minutes=30)   # repli : 9h30 ET ≈ 13h30 UTC
        else:
            ouverture_seance = ts + timedelta(hours=9, minutes=30)    # fuseau du marché : DST OK

        # 1re séance dont l'OUVERTURE est postérieure au placement de l'ordre.
        if ouverture_seance > t_ordre:
            ouverture = ligne.get("Open")
            if ouverture is None or not (ouverture == ouverture) or ouverture <= 0:  # NaN-safe
                continue
            return {"pret": True, "open": round(float(ouverture), 4),
                    "date": ts.strftime("%Y-%m-%d")}

    return {"pret": False, "raison": "aucune séance ouverte depuis l'ordre (marché fermé/week-end)"}


# ─── R1c — OHLC de la dernière séance (stops testés en INTRADAY) ───
def get_seance_ohlc(ticker: str) -> dict:
    """
    R1c — Renvoie l'Open / High / Low / Close de la DERNIÈRE séance cotée.

    Pourquoi : un stop n'est pas testé sur le cours de clôture. En réel, il se déclenche
    dès que le prix TOUCHE le niveau EN SÉANCE. Un titre qui plonge à -8% intraday puis
    clôture à -1% déclenche le stop dans la vraie vie, mais pas dans un paper qui ne
    regarde que le close. Ignorer le Low/High flatte systématiquement les sorties.

    Renvoie {"ok": True, "open":.., "high":.., "low":.., "close":.., "date": "YYYY-MM-DD"}
    ou {"ok": False, "raison": "..."} si indisponible (l'appelant retombe alors sur le
    dernier prix connu — dégradation propre, jamais d'exception).

    Prix issus de yfinance uniquement — la règle d'or tient.
    """
    symbole = resolve_ticker(ticker) or ticker
    try:
        hist = yf.Ticker(symbole).history(period="5d", interval="1d")
    except Exception as e:
        return {"ok": False, "raison": f"yfinance indisponible ({e})"}

    if hist is None or hist.empty:
        return {"ok": False, "raison": "aucune séance disponible"}

    for col in ("Open", "High", "Low", "Close"):
        if col not in hist.columns:
            return {"ok": False, "raison": f"colonne {col} absente"}

    ligne = hist.iloc[-1]          # dernière séance cotée
    try:
        o, h, l, c = (float(ligne["Open"]), float(ligne["High"]),
                      float(ligne["Low"]), float(ligne["Close"]))
    except Exception as e:
        return {"ok": False, "raison": f"OHLC illisible ({e})"}

    # NaN-safe (yfinance peut renvoyer NaN sur une séance incomplète)
    if not all(v == v and v > 0 for v in (o, h, l, c)):
        return {"ok": False, "raison": "OHLC incomplet (NaN)"}

    horodatage = hist.index[-1]
    return {"ok": True, "open": round(o, 4), "high": round(h, 4),
            "low": round(l, 4), "close": round(c, 4),
            "date": horodatage.strftime("%Y-%m-%d")}


# ─── S9 — ATR (volatilité réelle du titre), pour plancher la distance au stop ───
def get_atr(ticker: str, periode: int = 14) -> float | None:
    """
    S9 — Average True Range sur `periode` séances : de combien ce titre bouge-t-il
    NORMALEMENT en une journée, en dollars.

    Pourquoi : le dimensionnement par le risque divise par la distance au stop. Si un
    LLM propose un stop à -0,5 % sur un titre qui bouge de 3 %/jour, la formule
    ferait exploser la taille de position ET le stop serait touché par le simple bruit.
    L'ATR sert de PLANCHER : on ne dimensionne jamais comme si un titre était plus
    calme qu'il ne l'est réellement.

    True Range = max(H-L, |H-C_prev|, |L-C_prev|) — capture les gaps, contrairement
    au simple H-L. Renvoie None si indisponible (l'appelant dégrade proprement).
    """
    symbole = resolve_ticker(ticker) or ticker
    try:
        hist = yf.Ticker(symbole).history(period="2mo", interval="1d")
    except Exception:
        return None

    if hist is None or hist.empty or len(hist) < periode + 1:
        return None
    for col in ("High", "Low", "Close"):
        if col not in hist.columns:
            return None

    trs = []
    closes = hist["Close"].tolist()
    highs = hist["High"].tolist()
    lows = hist["Low"].tolist()
    for i in range(1, len(hist)):
        h, l, c_prev = highs[i], lows[i], closes[i - 1]
        if not all(v == v for v in (h, l, c_prev)):     # NaN-safe
            continue
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))

    if len(trs) < periode:
        return None
    atr = sum(trs[-periode:]) / periode
    return round(float(atr), 4) if atr and atr > 0 else None


# ─── S11 — Corrélations réelles (diversification par les CHIFFRES, pas par les étiquettes) ───
_cache_correlations: dict[str, float] = {}     # clé "A|B" (triée) -> corrélation


def get_correlations(candidat: str, detenus: list[str],
                     jours: int = 90, min_obs: int = 40) -> dict:
    """
    S11 — Corrélation des rendements QUOTIDIENS entre un candidat et les titres détenus.

    Pourquoi : deux titres de secteurs DIFFÉRENTS (OXY=Energy, FCX=Materials, CAT=Industrials)
    peuvent être un SEUL pari macro (la demande cyclique). Les plafonds sectoriels ne voient
    rien. Seule la corrélation des rendements réels dit la vérité sur la diversification.

    Renvoie {"ok": True, "correlations": {"OXY": 0.72, ...}} ou {"ok": False, "raison": ...}.
    Les corrélations sont mises en cache par paire (le calcul est identique dans un cycle).

    Chiffres yfinance uniquement — la règle d'or tient.
    """
    if not detenus:
        return {"ok": True, "correlations": {}}

    # 1) Ce qui est déjà en cache (par paire) n'est pas rechargé.
    resultats: dict[str, float] = {}
    a_calculer: list[str] = []
    for t in detenus:
        cle = "|".join(sorted([candidat.upper(), t.upper()]))
        if cle in _cache_correlations:
            resultats[t] = _cache_correlations[cle]
        elif t.upper() != candidat.upper():
            a_calculer.append(t)

    if not a_calculer:
        return {"ok": True, "correlations": resultats}

    # 2) Téléchargement GROUPÉ (un seul appel réseau, pas un par titre).
    symboles = [resolve_ticker(candidat) or candidat] + \
               [resolve_ticker(t) or t for t in a_calculer]
    try:
        data = yf.download(symboles, period=f"{max(jours, 60)}d", interval="1d",
                           progress=False, auto_adjust=True, group_by="column")
    except Exception as e:
        return {"ok": False, "raison": f"téléchargement échoué ({e})"}

    if data is None or data.empty:
        return {"ok": False, "raison": "aucune donnée de prix"}

    try:
        closes = data["Close"] if "Close" in data.columns else data
    except Exception:
        return {"ok": False, "raison": "colonne Close introuvable"}

    # Un seul symbole → pandas renvoie une Series : on la remet en DataFrame.
    if hasattr(closes, "to_frame") and closes.ndim == 1:
        closes = closes.to_frame()

    rendements = closes.pct_change().dropna(how="all")
    sym_candidat = resolve_ticker(candidat) or candidat
    if sym_candidat not in rendements.columns:
        return {"ok": False, "raison": f"pas de série pour {candidat}"}

    serie_c = rendements[sym_candidat]

    for t in a_calculer:
        sym_t = resolve_ticker(t) or t
        if sym_t not in rendements.columns:
            continue
        paire = rendements[[sym_candidat, sym_t]].dropna()
        if len(paire) < min_obs:      # trop peu d'observations : on ne prétend rien
            continue
        try:
            c = float(paire[sym_candidat].corr(paire[sym_t]))
        except Exception:
            continue
        if c != c:                     # NaN-safe (titre à variance nulle)
            continue
        c = round(c, 3)
        resultats[t] = c
        _cache_correlations["|".join(sorted([candidat.upper(), t.upper()]))] = c

    if not resultats:
        return {"ok": False, "raison": "aucune corrélation calculable (historique insuffisant)"}
    return {"ok": True, "correlations": resultats}