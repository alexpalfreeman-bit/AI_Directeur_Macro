# src/screener/scorer.py
"""
Le moteur de scoring par facteurs. Note chaque titre sur 100 à partir de
données RÉELLES : momentum, croissance, qualité, valorisation, tendance.
Aucune note ne vient d'un LLM — tout est calculé.
"""


def _score_momentum(d: dict) -> float:
    """0-25 pts : force du prix sur 3 et 6 mois."""
    pts = 0.0
    m3, m6 = d.get("momentum_3m"), d.get("momentum_6m")
    if m3 is not None:
        if m3 > 15: pts += 7
        elif m3 > 5: pts += 5
        elif m3 > 0: pts += 3
    if m6 is not None:
        if m6 > 25: pts += 10
        elif m6 > 10: pts += 7
        elif m6 > 0: pts += 4
    # Bonus tendance
    if d.get("trend") == "haussiere":
        pts += 8
    return min(pts, 25)


def _score_croissance(d: dict) -> float:
    """0-20 pts : croissance des revenus."""
    g = d.get("revenue_growth_yoy")
    if g is None:
        return 8  # neutre si donnée absente
    g_pct = g * 100 if abs(g) < 5 else g   # yfinance renvoie parfois 0.37 pour 37%
    if g_pct > 30: return 20
    if g_pct > 15: return 15
    if g_pct > 5:  return 10
    if g_pct > 0:  return 6
    return 2


def _score_qualite(d: dict) -> float:
    """0-20 pts : santé du bilan (dette)."""
    de = d.get("debt_to_equity")
    if de is None:
        return 10  # neutre
    if de < 30:  return 20
    if de < 70:  return 15
    if de < 120: return 10
    if de < 200: return 5
    return 0   # surendettée → écartée de fait


def _score_valorisation(d: dict) -> float:
    """0-15 pts : pas trop chère (EV/EBITDA en priorité, PE en repli)."""
    ev = d.get("ev_to_ebitda")
    pe = d.get("pe_ratio")
    if ev is not None and ev > 0:
        if ev < 8:  return 15
        if ev < 12: return 11
        if ev < 18: return 7
        if ev < 25: return 3
        return 1
    if pe is not None and pe > 0:
        if pe < 12: return 13
        if pe < 20: return 9
        if pe < 30: return 5
        return 2
    return 7  # neutre si aucune valorisation dispo


def score_titre(d: dict) -> dict:
    """Calcule le score total sur 100 d'un titre, avec le détail."""
    momentum = _score_momentum(d)
    croissance = _score_croissance(d)
    qualite = _score_qualite(d)
    valorisation = _score_valorisation(d)
    # Tendance déjà incluse dans le momentum ; on ajoute la part « confirmation »
    tendance = 20 if d.get("trend") == "haussiere" else (5 if d.get("trend") == "baissiere" else 12)

    total = round(momentum + croissance + qualite + valorisation + tendance, 1)
    return {
        "ticker": d.get("ticker"),
        "name": d.get("name"),
        "score": total,
        "detail": {
            "momentum": momentum, "croissance": croissance, "qualite": qualite,
            "valorisation": valorisation, "tendance": tendance,
        },
        "data": d,
    }
