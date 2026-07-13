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

    for horodatage, ligne in hist.iterrows():
        # L'index yfinance est daté de la SÉANCE (avec fuseau du marché).
        ts = horodatage.to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        # On veut la 1re séance dont l'OUVERTURE est postérieure à l'ordre.
        # yfinance date la barre au début de séance : on compare directement.
        if ts > t_ordre:
            ouverture = ligne.get("Open")
            if ouverture is None or not (ouverture == ouverture) or ouverture <= 0:  # NaN-safe
                continue
            return {"pret": True, "open": round(float(ouverture), 4),
                    "date": ts.strftime("%Y-%m-%d")}

    return {"pret": False, "raison": "aucune séance ouverte depuis l'ordre (marché fermé/week-end)"}