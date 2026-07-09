"""
Test S8 — _doit_lancer_gerant : le Gérant tourne sur RUN_GERANT=1 (explicite, décidé
par le cron), sinon repli sur l'heure LOCALE (>=17h), au lieu d'un seuil UTC qui dérive
au changement d'heure. On stub tous les imports de pipeline pour l'isoler.
"""
import os
import sys
import types
import importlib.util
from datetime import datetime, timezone
from contextlib import contextmanager

def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

# Paquets parents + modules feuilles importés par pipeline.py
for pkg in ("src", "src.ingestion", "src.agents", "src.portfolio",
            "src.communication", "src.memory", "src.analytics"):
    _mod(pkg)

_mod("src.ingestion.news_client",
     fetch_headlines=lambda *a, **k: [], is_macro_relevant=lambda *a, **k: False,
     corroborer_actualites=lambda *a, **k: None)
_mod("src.agents.macro_agent", generate_thesis=lambda *a, **k: None)
_mod("src.agents.quant_agent", validate_thesis=lambda *a, **k: None)
_mod("src.agents.devils_advocate_agent", challenge_thesis=lambda *a, **k: None)
_mod("src.agents.portfolio_manager_agent", make_decision=lambda *a, **k: None)

class _VerrouIndisponible(Exception): pass
@contextmanager
def _verrou(*a, **k): yield
_mod("src.portfolio.paper_portfolio",
     record_decision=lambda *a, **k: [], load_portfolio=lambda *a, **k: None,
     snapshot_text=lambda *a, **k: "", verifier_sorties=lambda *a, **k: None,
     verrou_portefeuille=_verrou, VerrouIndisponible=_VerrouIndisponible)
_mod("src.communication.telegram_bot",
     send_decision_et_portefeuille=lambda *a, **k: None, send_text=lambda *a, **k: None)
_mod("src.memory.world_memory", enregistrer_evenement=lambda *a, **k: None)
_mod("src.analytics.performance", snapshot_quotidien=lambda *a, **k: "")

_ICI = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pl", os.path.join(_ICI, "src", "core", "pipeline.py"))
pl = importlib.util.module_from_spec(spec)
sys.modules["pl"] = pl
spec.loader.exec_module(pl)

echecs = []
def check(nom, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {nom}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: echecs.append(nom)

def avec_env(val, fn):
    old = os.environ.get("RUN_GERANT")
    if val is None: os.environ.pop("RUN_GERANT", None)
    else: os.environ["RUN_GERANT"] = val
    try:
        return fn()
    finally:
        if old is None: os.environ.pop("RUN_GERANT", None)
        else: os.environ["RUN_GERANT"] = old

print("\n=== S8 — contrôle EXPLICITE via RUN_GERANT ===")
check("RUN_GERANT=1 → True", avec_env("1", lambda: pl._doit_lancer_gerant()) is True)
check("RUN_GERANT=0 → False", avec_env("0", lambda: pl._doit_lancer_gerant()) is False)
check("RUN_GERANT=' 1 ' (espaces tolérés) → True", avec_env(" 1 ", lambda: pl._doit_lancer_gerant()) is True)

print("\n=== S8 — repli sur l'heure locale quand RUN_GERANT n'est pas défini (seuil 16h) ===")
t_soir_ete = datetime(2025, 7, 1, 17, 0, tzinfo=timezone.utc)    # .hour = 17 → été, soir
t_soir_hiver = datetime(2025, 1, 1, 16, 0, tzinfo=timezone.utc)  # .hour = 16 → hiver, soir (21h UTC)
t_apresmidi = datetime(2025, 7, 1, 15, 0, tzinfo=timezone.utc)   # .hour = 15 → trop tôt
t_matin = datetime(2025, 7, 1, 11, 0, tzinfo=timezone.utc)       # .hour = 11 → passage midi
check("17h → True (soir été)", avec_env(None, lambda: pl._doit_lancer_gerant(t_soir_ete)) is True)
check("16h → True (soir hiver, 21h UTC en EST)", avec_env(None, lambda: pl._doit_lancer_gerant(t_soir_hiver)) is True)
check("15h → False (juste sous le seuil)", avec_env(None, lambda: pl._doit_lancer_gerant(t_apresmidi)) is False)
check("11h → False (passage de midi, jamais de Gérant)", avec_env(None, lambda: pl._doit_lancer_gerant(t_matin)) is False)

print("\n" + ("🎉 TOUS LES TESTS PASSENT" if not echecs else f"⚠️ ÉCHECS : {echecs}"))
sys.exit(1 if echecs else 0)