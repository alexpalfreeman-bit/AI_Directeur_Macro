"""
Test S6 — appel_avec_retry double le budget de tokens quand une réponse est TRONQUÉE
(stop_reason='max_tokens'), et ne le double PAS sur une simple erreur de validation.
Faux client Anthropic : aucun réseau.
"""
import os
import sys
import importlib.util
from pydantic import BaseModel

_ICI = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("th", os.path.join(_ICI, "src", "agents", "tool_helper.py"))
th = importlib.util.module_from_spec(spec)
sys.modules["th"] = th
spec.loader.exec_module(th)


class Mini(BaseModel):
    nom: str
    valeur: int


class FakeBlock:
    def __init__(self, inp): self.type = "tool_use"; self.input = inp; self.id = "b1"

class FakeResp:
    def __init__(self, stop_reason, content): self.stop_reason = stop_reason; self.content = content

class FakeMessages:
    def __init__(self, outer): self.outer = outer
    def create(self, model, max_tokens, system, tools, tool_choice, messages):
        self.outer.calls.append(max_tokens)
        return self.outer.scenario[len(self.outer.calls) - 1]

class FakeClient:
    def __init__(self, scenario):
        self.scenario = scenario; self.calls = []; self.messages = FakeMessages(self)


def appel(client, max_tokens=1500):
    return th.appel_avec_retry(
        client=client, model="m", system="sys", user_content="u",
        tool_name="soumettre", schema=Mini, max_tokens=max_tokens,
    )

echecs = []
def check(nom, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {nom}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: echecs.append(nom)

print("\n=== S6 — réponse tronquée puis succès : le budget de tokens DOUBLE ===")
c = FakeClient([
    FakeResp("max_tokens", []),                                   # tronquée : aucun tool_use
    FakeResp("tool_use", [FakeBlock({"nom": "x", "valeur": 1})]),  # OK au 2e essai
])
r = appel(c, max_tokens=1500)
check("résultat validé renvoyé", isinstance(r, Mini) and r.valeur == 1)
check("budgets = [1500, 3000] (doublé après troncature)", c.calls == [1500, 3000], detail=str(c.calls))

print("\n=== S6 — erreur de validation SANS troncature : budget INCHANGÉ ===")
c = FakeClient([
    FakeResp("tool_use", [FakeBlock({"nom": "x"})]),               # 'valeur' manquant → ValidationError
    FakeResp("tool_use", [FakeBlock({"nom": "x", "valeur": 2})]),  # OK au 2e essai
])
r = appel(c, max_tokens=1500)
check("résultat validé renvoyé", isinstance(r, Mini) and r.valeur == 2)
check("budgets = [1500, 1500] (pas de doublement)", c.calls == [1500, 1500], detail=str(c.calls))

print("\n=== S6 — cas nominal : un seul appel, pas de doublement ===")
c = FakeClient([FakeResp("tool_use", [FakeBlock({"nom": "y", "valeur": 9})])])
r = appel(c, max_tokens=1500)
check("résultat validé renvoyé", isinstance(r, Mini) and r.valeur == 9)
check("un seul appel à 1500", c.calls == [1500], detail=str(c.calls))

print("\n=== S6 — le doublement est plafonné à 8192 ===")
c = FakeClient([
    FakeResp("max_tokens", []),
    FakeResp("tool_use", [FakeBlock({"nom": "z", "valeur": 3})]),
])
r = appel(c, max_tokens=5000)               # 5000*2 = 10000 → plafonné à 8192
check("budgets = [5000, 8192] (plafond respecté)", c.calls == [5000, 8192], detail=str(c.calls))

print("\n" + ("🎉 TOUS LES TESTS PASSENT" if not echecs else f"⚠️ ÉCHECS : {echecs}"))
sys.exit(1 if echecs else 0)