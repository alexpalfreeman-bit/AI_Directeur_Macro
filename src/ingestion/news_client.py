# src/ingestion/news_client.py
"""
Couche d'ingestion — Flux qualitatif (actualités).
On récupère des titres via RSS, puis on les filtre avec un mini-LLM
bon marché (Haiku) pour ne garder QUE le macro sérieux.
"""
import feedparser
import anthropic
from config.settings import settings

# Flux RSS macro/éco. Si l'un casse un jour, retire-le ou remplace-le.
RSS_FEEDS = [
    "https://www.federalreserve.gov/feeds/press_all.xml",    # Réserve fédérale US
    "https://www.investing.com/rss/news_25.rss",             # Actualités économiques
    "https://www.cnbc.com/id/20910258/device/rss/rss.html",  # CNBC Economy
]

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)


def fetch_headlines(max_per_feed: int = 5) -> list[dict]:
    """Récupère les derniers titres de tous les flux RSS."""
    headlines = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", url)
            for entry in feed.entries[:max_per_feed]:
                title = entry.get("title", "").strip()
                if title:
                    headlines.append({
                        "title": title,
                        "link": entry.get("link", ""),
                        "source": source,
                    })
        except Exception as e:
            print(f"  ⚠️  Flux ignoré ({url}) : {e}")
    return headlines


def is_macro_relevant(title: str) -> bool:
    """Demande à Haiku si un titre est du macro sérieux (OUI / NON)."""
    prompt = (
        "Tu es un filtre pour un fonds d'investissement global macro. "
        "Réponds UNIQUEMENT par OUI ou NON, rien d'autre.\n\n"
        "Ce titre concerne-t-il la macroéconomie, la géopolitique, la "
        "politique monétaire (banques centrales, taux d'intérêt), le commerce "
        "international ou les chaînes d'approvisionnement, avec un impact "
        "potentiel sur les marchés financiers ?\n\n"
        f"Titre : « {title} »"
    )
    try:
        msg = client.messages.create(
            model=settings.cheap_model,
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip().upper().startswith("OUI")
    except Exception as e:
        print(f"  ⚠️  Filtre en échec sur ce titre : {e}")
        return False


if __name__ == "__main__":
    print("\n📰 Récupération des actualités...")
    news = fetch_headlines()
    print(f"   {len(news)} titres récupérés. Filtrage macro en cours...\n")

    kept = []
    for item in news:
        relevant = is_macro_relevant(item["title"])
        flag = "✅ MACRO" if relevant else "❌ bruit"
        print(f"  {flag}  {item['title'][:80]}")
        if relevant:
            kept.append(item)

    print(f"\n=== {len(kept)} actualité(s) macro retenue(s) sur {len(news)} ===")
    for item in kept:
        print(f"  • {item['title']}\n    {item['link']}")