"""
Banc de test à STUBS pour S1 (filtre liquidité activé) + S3 (cache par processus).
Faux yfinance / requests / ticker_resolver : aucun réseau. On compte les CONSTRUCTIONS
de yf.Ticker pour prouver que le cache évite les réappels.
"""
import os
import sys
import types
import importlib.util
import pandas as pd

# ── 1) Faux yfinance ─────────────────────────────────────────────────────────
class FakeTicker:
    construits: list[str] = []          # journal des constructions = "appels réseau"
    reponses: dict[str, tuple] = {}     # ticker résolu -> ("ok", info) | ("fail", None)

    def __init__(self, ticker, session=None):
        FakeTicker.construits.append(ticker)
        self._ticker = ticker

    @property
    def info(self):
        mode, payload = FakeTicker.reponses.get(self._ticker, ("ok", {}))
        if mode == "fail":
            raise RuntimeError("throttled (simulé)")
        return payload

    def history(self, period="1mo"):
        mode, _ = FakeTicker.reponses.get(self._ticker, ("ok", {}))
        if mode == "fail":
            raise RuntimeError("throttled (simulé)")
        return pd.DataFrame({"Close": [100.0, 101.0, 102.0]})

fake_yf = types.ModuleType("yfinance")
fake_yf.Ticker = FakeTicker
sys.modules["yfinance"] = fake_yf

# ── 2) Faux requests ─────────────────────────────────────────────────────────
fake_requests = types.ModuleType("requests")
class _Session:
    def __init__(self): self.headers = {}
fake_requests.Session = _Session
sys.modules["requests"] = fake_requests

# ── 3) Faux ticker_resolver (LSB -> LXU pour tester l'aliasing du cache) ──────
fake_src = types.ModuleType("src"); fake_src.__path__ = []
fake_ing = types.ModuleType("src.ingestion"); fake_ing.__path__ = []
fake_res = types.ModuleType("src.ingestion.ticker_resolver")
fake_res.resolve_ticker = lambda t: {"LSB": "LXU"}.get(t.upper(), t.upper())
sys.modules["src"] = fake_src
sys.modules["src.ingestion"] = fake_ing
sys.modules["src.ingestion.ticker_resolver"] = fake_res

# ── 4) Neutraliser le sleep de 1.5s du retry (tests rapides) ─────────────────
import time as _time
_time.sleep = lambda *a, **k: None

# ── 5) Charger le fichier patché ─────────────────────────────────────────────
_ICI = os.path.dirname(os.path.abspath(__file__))
_CHEMIN_MC = os.path.join(_ICI, "src", "ingestion", "market_client.py")
spec = importlib.util.spec_from_file_location("mc", _CHEMIN_MC)
mc = importlib.util.module_from_spec(spec)
sys.modules["mc"] = mc
spec.loader.exec_module(mc)

echecs = []
def check(nom, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {nom}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: echecs.append(nom)

def reset(reponses):
    FakeTicker.construits = []
    FakeTicker.reponses = dict(reponses)
    mc.vider_cache_fondamentaux()

INFO_AAPL = {
    "currentPrice": 200.0, "shortName": "Apple", "marketCap": 3_000_000_000_000,
    "sector": "Technology", "averageVolume": 55_000_000, "trailingPE": 30.0,
}

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== S1 — avg_volume est ENFIN renseigné sur le chemin de succès ===")
reset({"AAPL": ("ok", INFO_AAPL)})
d = mc.get_fundamentals("AAPL")
check("avg_volume renseigné (55M)", d["avg_volume"] == 55_000_000, detail=str(d.get("avg_volume")))
check("price renseigné", d["price"] == 200.0)
check("sector renseigné", d["sector"] == "Technology")

print("\n=== S1 — repli sur averageDailyVolume3Month si averageVolume absent ===")
reset({"XYZ": ("ok", {"currentPrice": 10.0, "averageDailyVolume3Month": 1_200_000})})
d = mc.get_fundamentals("XYZ")
check("avg_volume pris sur le repli (1.2M)", d["avg_volume"] == 1_200_000, detail=str(d.get("avg_volume")))

print("\n=== S1 — échec : avg_volume reste None (inchangé) ===")
reset({"BAD": ("fail", None)})
d = mc.get_fundamentals("BAD")
check("data_source = indisponible", d["data_source"] == "indisponible")
check("avg_volume None sur échec", d["avg_volume"] is None)

print("\n=== S3 — 2e appel du même ticker : AUCUN réappel réseau (cache) ===")
reset({"AAPL": ("ok", INFO_AAPL)})
d1 = mc.get_fundamentals("AAPL")
d2 = mc.get_fundamentals("AAPL")
check("yf.Ticker construit une seule fois", FakeTicker.construits.count("AAPL") == 1,
      detail=f"{FakeTicker.construits}")
check("les deux résultats sont identiques", d1 == d2)

print("\n=== S3 — utiliser_cache=False force un réappel ===")
reset({"AAPL": ("ok", INFO_AAPL)})
mc.get_fundamentals("AAPL")
mc.get_fundamentals("AAPL", utiliser_cache=False)
check("2 constructions quand cache désactivé", FakeTicker.construits.count("AAPL") == 2,
      detail=f"{FakeTicker.construits}")

print("\n=== S3 — un ÉCHEC n'est PAS mis en cache (réessai possible) ===")
reset({"BAD": ("fail", None)})
mc.get_fundamentals("BAD")
mc.get_fundamentals("BAD")
check("échec réinterrogé (2 constructions)", FakeTicker.construits.count("BAD") == 2,
      detail=f"{FakeTicker.construits}")

print("\n=== S3 — aliasing : LSB et LXU partagent l'entrée de cache (résolus pareil) ===")
reset({"LXU": ("ok", {"currentPrice": 12.0, "averageVolume": 400_000})})
mc.get_fundamentals("LSB")     # résolu -> LXU, fetch
mc.get_fundamentals("LXU")     # déjà en cache sous LXU
check("une seule construction pour LSB+LXU", FakeTicker.construits.count("LXU") == 1,
      detail=f"{FakeTicker.construits}")

print("\n=== S3 — vider_cache_fondamentaux() force un rafraîchissement ===")
reset({"AAPL": ("ok", INFO_AAPL)})
mc.get_fundamentals("AAPL")
mc.vider_cache_fondamentaux()
mc.get_fundamentals("AAPL")
check("2 constructions après vidage", FakeTicker.construits.count("AAPL") == 2,
      detail=f"{FakeTicker.construits}")

print("\n" + ("🎉 TOUS LES TESTS PASSENT" if not echecs else f"⚠️ ÉCHECS : {echecs}"))
sys.exit(1 if echecs else 0)