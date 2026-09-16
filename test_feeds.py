"""
Valide chaque flux RSS : est-il joignable, renvoie-t-il des titres, sont-ils récents ?
À lancer sur TA machine (réseau ouvert). Aucune clé API nécessaire, aucun appel LLM.
Un flux à 0 titre ou en erreur → retire-le de RSS_FEEDS (src/ingestion/news_client.py).
"""
import time
import feedparser

from src.ingestion.news_client import RSS_FEEDS

VERT, ROUGE, JAUNE, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
ok = ko = 0
print(f"\nValidation de {len(RSS_FEEDS)} flux RSS…\n")
for url in RSS_FEEDS:
    t0 = time.time()
    try:
        feed = feedparser.parse(url)
        n = len(feed.entries)
        titre = (feed.feed.get("title") or url)[:45]
        dt = time.time() - t0
        if n == 0:
            ko += 1
            print(f"  {ROUGE}✗ 0 titre{RESET}   {titre:<45} {url[:70]}")
        else:
            ok += 1
            exemple = (feed.entries[0].get("title") or "")[:70]
            couleur = VERT if dt < 5 else JAUNE
            print(f"  {couleur}✓ {n:>2} titres{RESET} ({dt:.1f}s)  {titre:<45} ex: « {exemple} »")
    except Exception as e:
        ko += 1
        print(f"  {ROUGE}✗ ERREUR{RESET}    {url[:60]}  {e}")

print(f"\n{ok} flux OK, {ko} à retirer." + ("" if ko == 0 else f"  {JAUNE}→ supprime les lignes ✗ de RSS_FEEDS.{RESET}"))