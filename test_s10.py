"""
Harnais S10 — calibration de la conviction du Directeur.
On construit des jeux de trades dont on CONNAÎT la vérité terrain (Directeur calibré,
bruité, ou inversé) et on vérifie que le module dit la vérité — y compris quand elle
est désagréable. Aucun réseau, aucune clé, aucun Redis.
"""
import sys, types

fake_cfg = types.ModuleType("config.settings")
class _S: starting_capital = 10_000.0
fake_cfg.settings = _S()
sys.modules["config.settings"] = fake_cfg

import src.analytics.calibration as cal

VERT, ROUGE, RESET = "\033[92m", "\033[91m", "\033[0m"
_ok = _ko = 0
def check(nom, cond, detail=""):
    global _ok, _ko
    if cond: _ok += 1; print(f"  {VERT}\u2713{RESET} {nom}")
    else: _ko += 1; print(f"  {ROUGE}\u2717 \u00c9CHEC{RESET} {nom}  {detail}")

def trade(conviction, gagnant, secteur="Technology", frais=2.0, motif="profit_target"):
    """100 actions @ 100$ = 10 000$ engagés. Gain brut +600$ ou perte -500$."""
    brut = 600.0 if gagnant else -500.0
    return {"ticker": "X", "shares": 100.0, "entry_price": 100.0,
            "exit_price": 106.0 if gagnant else 95.0,
            "realized_pnl": brut, "exit_reason": motif if gagnant else "stop_loss",
            "entry_cost": frais, "exit_cost": frais,
            "conviction": conviction, "sector": secteur, "thesis_id": "t",
            "opened_at": "2026-01-01T00:00:00+00:00", "closed_at": "2026-01-15T00:00:00+00:00"}

# ═══ 1) RÈGLE D'OR : le verdict est NET DE FRAIS ═══
# Trade qui gagne 3$ brut mais paie 5$ de frais → c'est une PERTE.
marginal = {"ticker": "X", "shares": 100.0, "entry_price": 100.0, "exit_price": 100.03,
            "realized_pnl": 3.0, "exit_reason": "profit_target",
            "entry_cost": 5.0, "exit_cost": 5.0, "conviction": 0.8, "sector": "Tech",
            "opened_at": "2026-01-01T00:00:00+00:00", "closed_at": "2026-01-15T00:00:00+00:00"}
st = cal.stats_calibration([marginal])
check("trade +3$ brut mais -7$ NET → compté comme PERTE (frais déduits)",
      st["taux_base"] == 0.0, f"taux={st['taux_base']}")

# ═══ 2) DISCIPLINE : sous 30 trades → REFUS de conclure ═══
petit = [trade(0.8, True) for _ in range(5)] + [trade(0.5, False) for _ in range(3)]
st = cal.stats_calibration(petit)
check("8 trades → non conclusif", st["conclusif"] is False)
txt = cal.texte_pour_directeur(petit)
check("prompt AVERTIT explicitement que l'échantillon est muet",
      "INSUFFISANT" in txt and "NE MODIFIE PAS" in txt, txt[:120])
check("prompt ne donne AUCUN ordre de recalibration sous le seuil",
      "Sois plus discriminant" not in txt and "excès de confiance" not in txt)

# ═══ 3) DIRECTEUR CALIBRÉ : conviction 0.85 gagne ~85%, conviction 0.45 gagne ~45% ═══
calibre = []
for _ in range(17): calibre.append(trade(0.85, True))    # 17/20 = 85%
for _ in range(3):  calibre.append(trade(0.85, False))
for _ in range(9):  calibre.append(trade(0.45, True))    # 9/20 = 45%
for _ in range(11): calibre.append(trade(0.45, False))
st = cal.stats_calibration(calibre)
check("40 trades → conclusif", st["conclusif"] is True and st["n"] == 40)
check("Directeur calibré → conviction INFORMATIVE (Brier < naïf)",
      st["informatif"] is True, f"brier={st['brier']:.3f} naif={st['brier_naif']:.3f}")
check("Directeur calibré → MONOTONE (les convictions hautes gagnent plus)",
      st["monotone"] is True)
txt = cal.texte_pour_directeur(calibre)
check("prompt confirme la bonne calibration", "INFORMATIVE" in txt and "Continue" in txt)

# ═══ 4) DIRECTEUR BRUITÉ : toutes les convictions gagnent pareil (50%) ═══
# → la conviction n'apporte AUCUNE information. Le module doit le DIRE.
bruite = []
for _ in range(10): bruite.append(trade(0.85, True))
for _ in range(10): bruite.append(trade(0.85, False))    # 50%
for _ in range(10): bruite.append(trade(0.45, True))
for _ in range(10): bruite.append(trade(0.45, False))    # 50%
st = cal.stats_calibration(bruite)
check("Directeur bruité → conviction NON informative (Brier ≥ naïf)",
      st["informatif"] is False, f"brier={st['brier']:.3f} naif={st['brier_naif']:.3f}")
txt = cal.texte_pour_directeur(bruite)
check("prompt DIT au Directeur que sa conviction est du bruit",
      "PAS informative" in txt and "discriminant" in txt)

# ═══ 5) DIRECTEUR INVERSÉ : ses convictions HAUTES perdent le plus (excès de confiance) ═══
inverse = []
for _ in range(4):  inverse.append(trade(0.85, True))    # 4/20 = 20% (!)
for _ in range(16): inverse.append(trade(0.85, False))
for _ in range(16): inverse.append(trade(0.45, True))    # 16/20 = 80%
for _ in range(4):  inverse.append(trade(0.45, False))
st = cal.stats_calibration(inverse)
check("Directeur inversé → NON MONOTONE détecté", st["monotone"] is False)
txt = cal.texte_pour_directeur(inverse)
check("prompt dénonce l'excès de confiance",
      "NON MONOTONE" in txt and "excès de confiance" in txt)

# ═══ 6) Trades SANS conviction (hérités d'avant S10) → ignorés proprement ═══
legacy = [{"ticker": "OLD", "shares": 10.0, "entry_price": 100.0, "exit_price": 110.0,
           "realized_pnl": 100.0, "exit_reason": "profit_target",
           "opened_at": "2026-01-01T00:00:00+00:00", "closed_at": "2026-01-15T00:00:00+00:00"}]
st = cal.stats_calibration(legacy)
check("trades sans conviction → ignorés (on ne calibre pas ce qu'on n'a pas mesuré)",
      st["n"] == 0 and st["conclusif"] is False)
txt = cal.texte_pour_directeur(legacy)
check("prompt gère proprement l'absence de données", "Aucun" in txt or "aucun" in txt)

# ═══ 7) Intervalle de Wilson : petites tranches marquées non fiables ═══
mixte = [trade(0.85, True) for _ in range(12)] + [trade(0.85, False) for _ in range(8)] \
      + [trade(0.55, True) for _ in range(3)]     # tranche moyenne : n=3 seulement
st = cal.stats_calibration(mixte)
tr_moy = [t for t in st["tranches"] if t["n"] == 3]
check("tranche à n=3 marquée NON fiable", tr_moy and tr_moy[0]["fiable"] is False)
tr_forte = [t for t in st["tranches"] if t["n"] == 20]
check("tranche à n=20 marquée fiable", tr_forte and tr_forte[0]["fiable"] is True)
check("IC de Wilson borné dans [0,1]",
      all(0.0 <= t["ci_bas"] <= t["ci_haut"] <= 1.0 for t in st["tranches"]))

# ═══ 8) Rapport lisible : ne plante pas, dit la vérité ═══
rap = cal.rapport_calibration(bruite)
check("rapport signale que l'arbitrage de capital repose sur un chiffre sans valeur",
      "arbitrage de capital" in rap, rap[:200])
rap_vide = cal.rapport_calibration([])
check("rapport sur portefeuille vide → aucun crash", "Aucun trade" in rap_vide)

# ═══ 9) Ventilation par secteur ═══
sect = [trade(0.7, True, secteur="Energy") for _ in range(4)] + \
       [trade(0.7, False, secteur="Financial") for _ in range(4)]
rap = cal.rapport_calibration(sect)
check("ventilation par secteur présente", "Energy" in rap and "Financial" in rap)

print(f"\n{'='*56}\n  RÉSULTAT S10 : {VERT}{_ok} réussis{RESET}, "
      f"{ROUGE + str(_ko) + ' échoués' + RESET if _ko else '0 échoué'}\n{'='*56}")
exit(1 if _ko else 0)