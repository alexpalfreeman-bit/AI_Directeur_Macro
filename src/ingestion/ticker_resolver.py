# src/ingestion/ticker_resolver.py
"""
Résolution et validation des symboles boursiers.
Corrige les alias connus (ex: LSB -> LXU) et signale les tickers
qui ne renvoient aucune donnée exploitable.
"""

# Table d'alias : ancien/erroné -> symbole réel coté
TICKER_ALIASES = {
    "LSB": "LXU",   # LSB Industries cote sous LXU
    "FB":  "META",  # exemples courants, à enrichir au fil du temps
    "GOOG_OLD": "GOOGL",
    "NUBANK": "NU",      # Nu Holdings
    "NU BANK": "NU",
}


def resolve_ticker(ticker: str) -> str:
    """Renvoie le vrai symbole coté pour un ticker donné."""
    return TICKER_ALIASES.get(ticker.upper(), ticker.upper())


def is_valid_data(fundamentals: dict) -> bool:
    """Un ticker est exploitable s'il a au moins un prix ET une capitalisation."""
    return (
        fundamentals.get("price") is not None
        and fundamentals.get("market_cap") is not None
    )