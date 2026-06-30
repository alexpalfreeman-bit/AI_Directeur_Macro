# src/screener/factor_data.py
"""
Récupère les données brutes nécessaires au scoring par facteurs d'un titre :
rendements passés (momentum), tendance, + les fondamentaux déjà connus.
"""
import yfinance as yf
import requests
from src.ingestion.market_client import get_fundamentals


def get_factor_data(ticker: str) -> dict:
    """Données brutes pour scorer un titre. Robuste : ne plante jamais."""
    base = get_fundamentals(ticker)   # prix, PE, EV/EBITDA, P/B, dette, cap, volume...

    # Historique de prix pour le momentum et la tendance
    mom_3m = mom_6m = trend = None
    try:
        session = requests.Session()
        session.headers["User-agent"] = "Mozilla/5.0"
        hist = yf.Ticker(ticker, session=session).history(period="1y")
        if not hist.empty and len(hist) > 130:
            closes = hist["Close"]
            prix = float(closes.iloc[-1])
            # Rendement sur ~3 mois (63 séances) et ~6 mois (126 séances)
            mom_3m = round((prix / float(closes.iloc[-63]) - 1) * 100, 1)
            mom_6m = round((prix / float(closes.iloc[-126]) - 1) * 100, 1)
            # Tendance : prix vs moyenne mobile 50 jours
            sma50 = float(closes.tail(50).mean())
            trend = "haussiere" if prix > sma50 else "baissiere"
    except Exception:
        pass

    return {
        "ticker": base.get("ticker"),
        "name": base.get("name"),
        "price": base.get("price"),
        "pe_ratio": base.get("pe_ratio"),
        "ev_to_ebitda": base.get("ev_to_ebitda"),
        "price_to_book": base.get("price_to_book"),
        "debt_to_equity": base.get("debt_to_equity"),
        "revenue_growth_yoy": base.get("revenue_growth_yoy"),
        "market_cap": base.get("market_cap"),
        "avg_volume": base.get("avg_volume"),
        "momentum_3m": mom_3m,
        "momentum_6m": mom_6m,
        "trend": trend,
    }