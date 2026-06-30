# src/ingestion/sentiment_client.py
"""
Couche A (bis) : Sentiment & régime de marché.
Donne au Comité une lecture OBJECTIVE du climat :
- VIX (la peur)
- Courbe des taux 10 ans - 3 mois (signal de récession)
- Tendance du S&P 500 vs sa moyenne 200 jours (risk-on / risk-off)
On en déduit un régime qui module les décisions.
"""
import yfinance as yf
import requests


def _last_close(symbol: str) -> float | None:
    """Dernier cours de clôture d'un symbole (robuste pour les indices)."""
    try:
        session = requests.Session()
        session.headers["User-agent"] = "Mozilla/5.0"
        hist = yf.Ticker(symbol, session=session).history(period="1y")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def _spy_vs_200d() -> tuple[float | None, float | None]:
    """Prix actuel du SPY et sa moyenne mobile 200 jours."""
    try:
        session = requests.Session()
        session.headers["User-agent"] = "Mozilla/5.0"
        hist = yf.Ticker("SPY", session=session).history(period="1y")
        if hist.empty or len(hist) < 200:
            return None, None
        price = float(hist["Close"].iloc[-1])
        sma200 = float(hist["Close"].tail(200).mean())
        return price, sma200
    except Exception:
        return None, None


def get_market_regime() -> dict:
    """Calcule les signaux de régime et en déduit une étiquette globale."""
    vix = _last_close("^VIX")
    tnx = _last_close("^TNX")   # rendement 10 ans
    irx = _last_close("^IRX")   # rendement 3 mois
    spy_price, spy_sma200 = _spy_vs_200d()

    # Courbe des taux 10 ans - 3 mois (négatif = inversée = signal de récession)
    yield_curve = round(tnx - irx, 2) if (tnx is not None and irx is not None) else None

    # Tendance du marché
    spy_trend = None
    if spy_price is not None and spy_sma200 is not None:
        spy_trend = "haussière" if spy_price > spy_sma200 else "baissière"

    # --- Déduction du régime (score : positif = risk-on, négatif = risk-off) ---
    score = 0
    raisons = []

    if vix is not None:
        if vix < 18:
            score += 1; raisons.append(f"VIX bas ({vix:.1f}) → marché calme")
        elif vix > 28:
            score -= 2; raisons.append(f"VIX élevé ({vix:.1f}) → peur / stress")
        else:
            raisons.append(f"VIX modéré ({vix:.1f})")

    if spy_trend == "haussière":
        score += 1; raisons.append("S&P au-dessus de sa moyenne 200j → tendance haussière")
    elif spy_trend == "baissière":
        score -= 1; raisons.append("S&P sous sa moyenne 200j → tendance baissière")

    if yield_curve is not None:
        if yield_curve < 0:
            score -= 1; raisons.append(f"Courbe des taux inversée ({yield_curve}) → risque de récession")
        else:
            raisons.append(f"Courbe des taux normale ({yield_curve})")

    if score >= 2:
        regime = "RISK-ON"
    elif score <= -2:
        regime = "RISK-OFF"
    else:
        regime = "NEUTRE"

    return {"regime": regime, "vix": vix, "yield_curve_10y_3m": yield_curve,
            "spy_trend": spy_trend, "raisons": raisons}


def regime_text(regime: dict) -> str:
    """Met en forme le régime pour l'injecter dans les prompts des agents."""
    lignes = [f"RÉGIME DE MARCHÉ ACTUEL : {regime['regime']}"]
    lignes += [f"- {r}" for r in regime["raisons"]]
    lignes.append(
        "Implication : en RISK-OFF, sois beaucoup plus prudent sur les cycliques "
        "(matières premières, industriels, small caps) — un repli de marché peut les "
        "écraser malgré de bons fondamentaux. En RISK-ON, momentum et cycliques sont "
        "mieux soutenus."
    )
    return "\n".join(lignes)


if __name__ == "__main__":
    r = get_market_regime()
    print("\n=== RÉGIME DE MARCHÉ ===")
    print(regime_text(r))