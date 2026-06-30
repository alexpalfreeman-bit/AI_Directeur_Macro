# src/screener/universe.py
"""
L'univers d'investissement du screener : actions US liquides + grandes
sociétés canadiennes via leur listing US (en USD, achetables sur Wealthsimple).
Tous les tickers sont en USD pour garder la comptabilité cohérente.
"""

# Grandes capitalisations US liquides (toutes sur Wealthsimple)
US_LARGE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V",
    "WMT", "MA", "COST", "HD", "NFLX", "ORCL", "AMD", "CRM", "BAC", "KO",
    "PEP", "MCD", "CSCO", "ABBV", "GE", "CAT", "DIS", "INTC", "QCOM", "TXN",
]

# Secteurs cycliques / matières premières (le terrain de ta stratégie AEM/VNP/CEU)
US_CYCLICALS = [
    "CF", "MOS", "NTR", "LYB", "DOW", "FCX", "NUE", "CLF", "X", "AA",
    "OXY", "DVN", "HAL", "SLB", "MP", "ALB", "EMN", "CE", "OLN", "UAN",
]

# Grandes sociétés CANADIENNES via leur listing US en USD (achetables sur Wealthsimple)
CANADA_US_LISTED = [
    "AEM",   # Agnico Eagle (or)
    "SHOP",  # Shopify
    "NTR",   # Nutrien (déjà en US$)
    "BAM",   # Brookfield Asset Management
    "CNQ",   # Canadian Natural Resources
    "SU",    # Suncor Energy
    "TRI",   # Thomson Reuters
    "WCN",   # Waste Connections
    "GIB",   # CGI
    "TECK",  # Teck Resources
    "FNV",   # Franco-Nevada (or)
    "WPM",   # Wheaton Precious Metals (or)
]

# Quelques small/mid caps dynamiques (style spéculatif assumé, profil agressif)
US_GROWTH = [
    "SOFI", "PLTR", "COIN", "RBLX", "DKNG", "AFRM", "U", "RIVN", "ENPH", "FSLR",
]


def get_universe() -> list[str]:
    """Renvoie l'univers complet, sans doublons."""
    tous = US_LARGE + US_CYCLICALS + CANADA_US_LISTED + US_GROWTH
    return sorted(set(tous))   # set() retire les doublons (ex: NTR)


if __name__ == "__main__":
    univers = get_universe()
    print(f"\n🌐 Univers du screener : {len(univers)} titres")
    print(", ".join(univers))