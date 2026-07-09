"""
Test S6 (routage) — generer_these_screener passe par appel_avec_retry :
- impose les tickers du screener (écrase ceux inventés par le LLM),
- génère un thesis_id FRAIS (ignore celui inventé par le LLM),
- ne crashe plus le cycle sur un champ manquant.
"""
import os
import sys
import types
import uuid as _uuid

# ── Faux anthropic : le client renvoie une MacroThesis complète (via tool_use) ──
class _FakeBlock:
    def __init__(self, inp): self.type = "tool_use"; self.input = inp; self.id = "b1"
class _FakeResp:
    def __init__(self, content, stop="end_turn"): self.content = content; self.stop_reason = stop
class _FakeMessages:
    def __init__(self, resp): self._resp = resp
    def create(self, **k): return self._resp
class _FakeClient:
    def __init__(self, resp): self.messages = _FakeMessages(resp)

# Sortie modèle : complète, MAIS avec un thesis_id pourri et de mauvais tickers à écraser
_INPUT = {
    "catalyst": {"type": "other", "description": "fondé sur le screen"},
    "causal_chain": ["momentum fort", "flux acheteurs persistants"],
    "sector": "Technology",
    "theme": "Leaders momentum",
    "direction": "long",
    "time_horizon_days": 30,
    "regions": ["US"],
    "rationale": "scores de momentum élevés, valo tendue",
    "confidence": 0.6,
    "thesis_id": "LLM-INVENTE-POURRI",     # doit être ignoré (forcer_id impose un uuid frais)
    "candidate_tickers": ["WRONG"],         # doit être écrasé par les tickers du screener
}
fake_anthropic = types.ModuleType("anthropic")
fake_anthropic.Anthropic = lambda *a, **k: _FakeClient(_FakeResp([_FakeBlock(dict(_INPUT))]))
sys.modules["anthropic"] = fake_anthropic

fake_config = types.ModuleType("config"); fake_cs = types.ModuleType("config.settings")
class _S: llm_model = "x"; anthropic_api_key = "x"
fake_cs.settings = _S(); fake_config.settings = fake_cs
sys.modules["config"] = fake_config; sys.modules["config.settings"] = fake_cs

# Stub scanner_univers (le screener bottom-up)
fake_scr = types.ModuleType("src.screener.screener")
fake_scr.scanner_univers = lambda top_n=5: [
    {"ticker": "AAPL", "name": "Apple", "score": 88,
     "detail": {"momentum": 30, "croissance": 20, "qualite": 20, "valorisation": 18}},
    {"ticker": "MSFT", "name": "Microsoft", "score": 85,
     "detail": {"momentum": 28, "croissance": 22, "qualite": 21, "valorisation": 14}},
]
sys.modules["src.screener.screener"] = fake_scr

from src.screener.screener_thesis import generer_these_screener

echecs = []
def check(nom, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {nom}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: echecs.append(nom)

print("\n=== S6 — routage screener : tickers imposés + id frais + pas de crash ===")
these = generer_these_screener(top_n=2)
check("MacroThesis renvoyée", these is not None and type(these).__name__ == "MacroThesis")
check("tickers imposés par le screener (AAPL, MSFT), pas 'WRONG'",
      these.candidate_tickers == ["AAPL", "MSFT"], detail=str(these.candidate_tickers))
check("thesis_id frais (pas la valeur inventée par le LLM)",
      these.thesis_id != "LLM-INVENTE-POURRI", detail=these.thesis_id)
try:
    _uuid.UUID(these.thesis_id); ok_uuid = True
except Exception:
    ok_uuid = False
check("thesis_id est un UUID valide", ok_uuid, detail=these.thesis_id)
check("direction correctement typée (long)", these.direction.value == "long")

print("\n" + ("🎉 TOUS LES TESTS PASSENT" if not echecs else f"⚠️ ÉCHECS : {echecs}"))
sys.exit(1 if echecs else 0)