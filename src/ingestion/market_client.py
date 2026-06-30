# src/ingestion/market_client.py
"""
Couche d'ingestion — Données quantitatives (marché).
C'est la SOURCE DE VÉRITÉ des chiffres. Le LLM ne doit jamais
les inventer : ils viennent toujours d'ici.
"""
import yfinance as yf
import requests
from src.ingestion.ticker_resolver import resolve_ticker


def get_fundamentals(ticker: str) -> dict:
    """Récupère les fondamentaux RÉELS d'une action via Yahoo Finance."""
    ticker = resolve_ticker(ticker)          # LSB devient LXU, etc.

    session = requests.Session()
    session.headers["User-agent"] = "Mozilla/5.0"
    t = yf.Ticker(ticker, session=session)

    # 🛡️ Filet de sécurité : un ticker invalide/exotique ne doit JAMAIS tout faire planter
    try:
        info = t.info
        hist = t.history(period="1mo")
    except Exception as e:
        print(f"  ⚠️  Données indisponibles pour {ticker} ({e}) — ticker ignoré.")
        return {
            "ticker": ticker.upper(), "name": None, "price": None,
            "pe_ratio": None, "debt_to_equity": None, "revenue_growth_yoy": None,
            "market_cap": None, "sector": None, "volatility_30d_pct": None,
            "ev_to_ebitda": None, "price_to_book": None, "data_source": "yfinance",
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

    return {
        "ticker": ticker.upper(),
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
        "data_source": "yfinance",                        # traçabilité : d'où vient le chiffre
    }


if __name__ == "__main__":
    # Test rapide : on récupère les vraies données d'Apple
    data = get_fundamentals("AAPL")
    print("\n=== Données RÉELLES récupérées (zéro hallucination) ===")
    for key, value in data.items():
        print(f"  {key:.<22} {value}")