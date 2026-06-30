# src/screener/screener.py
"""
Le screener : scanne l'univers, score chaque titre, renvoie les meilleurs.
C'est la source d'idées BOTTOM-UP (par les chiffres), complémentaire des
idées TOP-DOWN (par les news) de ton comité macro.
"""
from src.screener.universe import get_universe
from src.screener.factor_data import get_factor_data
from src.screener.scorer import score_titre
from config.settings import settings


def scanner_univers(top_n: int = 5) -> list[dict]:
    """Score tout l'univers et renvoie les `top_n` meilleurs titres."""
    univers = get_universe()
    print(f"🔍 Scan de {len(univers)} titres en cours...")
    resultats = []

    for i, ticker in enumerate(univers, 1):
        try:
            d = get_factor_data(ticker)
            # Filtre qualité de base (mêmes seuils que le Quant)
            cap, vol = d.get("market_cap"), d.get("avg_volume")
            if cap and cap < settings.min_market_cap:
                continue
            if vol and vol < settings.min_avg_volume:
                continue
            resultats.append(score_titre(d))
        except Exception as e:
            print(f"  ⚠️  {ticker} ignoré : {e}")
        if i % 20 == 0:
            print(f"   ...{i}/{len(univers)} scannés")

    # Classement du meilleur score au moins bon
    resultats.sort(key=lambda r: r["score"], reverse=True)
    return resultats[:top_n]


if __name__ == "__main__":
    top = scanner_univers(top_n=10)
    print("\n" + "=" * 60)
    print("       🏆 TOP 10 DU SCREENER PAR FACTEURS")
    print("=" * 60)
    for r in top:
        d = r["detail"]
        print(f"\n  {r['score']:5.1f}/100  {r['ticker']:6} {r['name']}")
        print(f"           momentum={d['momentum']} croissance={d['croissance']} "
              f"qualité={d['qualite']} valo={d['valorisation']} tendance={d['tendance']}")
        