"""
S10 — BOUCLE D'AUTO-AMÉLIORATION : la conviction du Directeur est-elle CALIBRÉE ?

Le Directeur produit un chiffre de `conviction` (0,45 / 0,6 / 0,8…) sur chaque décision.
Ce chiffre pilote le dimensionnement ET l'arbitrage de capital (on vend « la plus faible
conviction »). Or personne ne vérifie jamais s'il veut dire quelque chose.

Ce module ferme la boucle : on relie chaque position CLÔTURÉE à la conviction qui l'a
ouverte, et on répond à une question falsifiable :

    « Une conviction de 0,8 gagne-t-elle réellement plus souvent qu'une conviction de 0,5 ? »

Trois mesures :
  • TAUX DE RÉUSSITE par tranche de conviction (avec intervalle de confiance de Wilson).
  • SCORE DE BRIER : moyenne de (conviction − résultat)², résultat ∈ {0,1}. Plus c'est
    BAS, mieux c'est. On le compare au score qu'obtiendrait un modèle NAÏF qui prédirait
    toujours le taux de base. Si le Directeur ne bat pas le naïf, sa conviction est du bruit.
  • MONOTONIE : les tranches hautes gagnent-elles plus que les basses ? C'est le test le
    plus important, et le plus dur à passer.

DISCIPLINE STATISTIQUE (même règle que performance.py) : sous SEUIL_CONCLUSIF trades, on
REFUSE de conclure. Un taux de réussite sur 6 trades ne veut rien dire, et recalibrer un
prompt sur du bruit est pire que ne rien faire — on apprendrait le hasard.

RÈGLE D'OR : un trade est GAGNANT s'il est positif NET DE FRAIS. Un trade qui gagne 0,3 %
brut mais perd après coûts est une PERTE. C'est le seul verdict qui compte.
"""
from __future__ import annotations

import math

# Tranches de conviction. Bornes basses incluses, hautes exclues (sauf la dernière).
TRANCHES = [
    (0.00, 0.50, "faible   (< 0,50)"),
    (0.50, 0.65, "moyenne  (0,50–0,65)"),
    (0.65, 0.80, "forte    (0,65–0,80)"),
    (0.80, 1.01, "très forte (≥ 0,80)"),
]

SEUIL_CONCLUSIF = 30        # sous ce nombre de trades, on ne conclut RIEN
SEUIL_TRANCHE = 10          # sous ce nombre par tranche, la tranche est indicative seulement


def _wilson_ci(succes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance de Wilson (robuste aux petits échantillons, contrairement
    à l'intervalle normal qui donne des bornes absurdes)."""
    if n == 0:
        return (0.0, 1.0)
    p = succes / n
    d = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / d
    demi = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / d
    return (max(0.0, centre - demi), min(1.0, centre + demi))


def _pnl_net(c: dict) -> float:
    """P&L NET de frais (R1a). C'est le seul verdict qui compte."""
    return (c.get("realized_pnl") or 0.0) - (c.get("entry_cost") or 0.0) - (c.get("exit_cost") or 0.0)


def _rendement_net_pct(c: dict) -> float | None:
    """Rendement net en % du capital engagé (comparable entre positions de tailles ≠)."""
    engage = (c.get("entry_price") or 0.0) * (c.get("shares") or 0.0)
    if engage <= 0:
        return None
    return _pnl_net(c) / engage * 100.0


def stats_calibration(closed: list[dict]) -> dict:
    """
    Calcule la calibration de la conviction sur les trades clôturés.

    `closed` : liste de dicts (portefeuille.closed, via model_dump()).
    Les trades SANS conviction (positions héritées d'avant S10) sont ignorés — on ne
    peut pas calibrer ce qu'on n'a pas mesuré.
    """
    # On ne garde que les trades exploitables : conviction connue ET capital engagé connu.
    utiles = [c for c in closed
              if c.get("conviction") is not None and _rendement_net_pct(c) is not None]

    n = len(utiles)
    if n == 0:
        return {"n": 0, "conclusif": False, "tranches": [], "brier": None,
                "brier_naif": None, "monotone": None,
                "message": "Aucun trade clôturé avec conviction enregistrée."}

    # Résultat binaire : gagnant NET (frais déduits) ou non.
    for c in utiles:
        c["_gagnant"] = 1 if _pnl_net(c) > 0 else 0

    taux_base = sum(c["_gagnant"] for c in utiles) / n

    # ── Score de Brier : (conviction − résultat)². Plus bas = mieux calibré. ──
    brier = sum((c["conviction"] - c["_gagnant"]) ** 2 for c in utiles) / n
    # Modèle NAÏF : prédit toujours le taux de base. Si le Directeur ne le bat pas,
    # sa conviction n'apporte AUCUNE information.
    brier_naif = sum((taux_base - c["_gagnant"]) ** 2 for c in utiles) / n

    # ── Ventilation par tranche de conviction ──
    tranches = []
    for bas, haut, libelle in TRANCHES:
        lot = [c for c in utiles if bas <= c["conviction"] < haut]
        if not lot:
            continue
        k = len(lot)
        gagnants = sum(c["_gagnant"] for c in lot)
        ci_bas, ci_haut = _wilson_ci(gagnants, k)
        rendements = [_rendement_net_pct(c) for c in lot]
        tranches.append({
            "libelle": libelle,
            "borne_basse": bas,
            "n": k,
            "gagnants": gagnants,
            "taux_reussite": gagnants / k,
            "ci_bas": ci_bas,
            "ci_haut": ci_haut,
            "conviction_moyenne": sum(c["conviction"] for c in lot) / k,
            "rendement_net_moyen_pct": sum(rendements) / k,
            "fiable": k >= SEUIL_TRANCHE,
        })

    # ── MONOTONIE : les tranches hautes gagnent-elles plus que les basses ? ──
    # On ne teste que sur les tranches ayant assez de trades pour dire quoi que ce soit.
    fiables = [t for t in tranches if t["fiable"]]
    monotone = None
    if len(fiables) >= 2:
        taux = [t["taux_reussite"] for t in sorted(fiables, key=lambda x: x["borne_basse"])]
        monotone = all(taux[i] <= taux[i + 1] for i in range(len(taux) - 1))

    return {
        "n": n,
        "conclusif": n >= SEUIL_CONCLUSIF,
        "taux_base": taux_base,
        "brier": brier,
        "brier_naif": brier_naif,
        "informatif": brier < brier_naif,     # la conviction bat-elle le modèle naïf ?
        "tranches": tranches,
        "monotone": monotone,
        "message": "",
    }


def _ventilation(closed: list[dict], cle: str, minimum: int = 3) -> list[dict]:
    """Taux de réussite net par valeur d'une clé (secteur, motif de sortie…)."""
    groupes: dict[str, list[dict]] = {}
    for c in closed:
        v = (c.get(cle) or "").strip() or "Inconnu"
        groupes.setdefault(v, []).append(c)
    out = []
    for v, lot in groupes.items():
        if len(lot) < minimum:
            continue
        gagnants = sum(1 for c in lot if _pnl_net(c) > 0)
        rends = [r for r in (_rendement_net_pct(c) for c in lot) if r is not None]
        out.append({
            "valeur": v, "n": len(lot),
            "taux_reussite": gagnants / len(lot),
            "rendement_net_moyen_pct": (sum(rends) / len(rends)) if rends else 0.0,
        })
    return sorted(out, key=lambda x: x["rendement_net_moyen_pct"], reverse=True)


def texte_pour_directeur(closed: list[dict]) -> str:
    """
    Résumé COURT et CHIFFRÉ, injecté dans le prompt du Directeur.

    C'est ici que la boucle se ferme : le Directeur voit ses propres résultats passés,
    chiffrés, et peut corriger sa tendance à la sur- ou sous-confiance.

    Sous le seuil statistique, on le dit EXPLICITEMENT et on n'ordonne aucune correction.
    Laisser un LLM « recalibrer » sur 8 trades, c'est lui apprendre le bruit — et il
    obéira, avec aplomb, à un signal qui n'existe pas.
    """
    st = stats_calibration(closed)

    if st["n"] == 0:
        return ("CALIBRATION DE TA CONVICTION : aucun trade clôturé avec conviction "
                "enregistrée. Aucun retour d'expérience disponible — raisonne sur le fond.")

    lignes = [f"CALIBRATION DE TA CONVICTION (sur {st['n']} trade(s) clôturé(s), "
              f"résultats NETS DE FRAIS) :"]

    if not st["conclusif"]:
        lignes.append(
            f"⚠️ ÉCHANTILLON INSUFFISANT ({st['n']} < {SEUIL_CONCLUSIF} trades). Les chiffres "
            f"ci-dessous sont INDICATIFS et statistiquement muets. NE MODIFIE PAS ton "
            f"raisonnement sur cette base : à ce stade, un écart de taux de réussite est "
            f"indiscernable du hasard. Ils te sont montrés pour information seulement.")

    lignes.append(f"- Taux de réussite global : {st['taux_base']*100:.0f}%")

    for t in st["tranches"]:
        fiab = "" if t["fiable"] else "  [n trop faible — indicatif]"
        lignes.append(
            f"- Conviction {t['libelle']} : {t['gagnants']}/{t['n']} gagnants "
            f"({t['taux_reussite']*100:.0f}%, IC95 {t['ci_bas']*100:.0f}–{t['ci_haut']*100:.0f}%) "
            f"| rendement net moyen {t['rendement_net_moyen_pct']:+.1f}%{fiab}")

    # Verdict de calibration — uniquement si l'échantillon le permet.
    if st["conclusif"]:
        if st["informatif"]:
            lignes.append(
                f"→ Ta conviction est INFORMATIVE (Brier {st['brier']:.3f} < naïf "
                f"{st['brier_naif']:.3f}) : elle apporte de l'information réelle.")
        else:
            lignes.append(
                f"→ ⚠️ Ta conviction n'est PAS informative (Brier {st['brier']:.3f} ≥ naïf "
                f"{st['brier_naif']:.3f}) : elle ne prédit pas mieux qu'un chiffre constant. "
                f"Sois plus discriminant — réserve les convictions hautes aux thèses dont le "
                f"catalyseur est vérifiable et le mécanisme causal court.")

        if st["monotone"] is False:
            lignes.append(
                "→ ⚠️ NON MONOTONE : tes convictions élevées ne gagnent PAS plus souvent que "
                "les faibles. C'est le signe d'un excès de confiance sur les thèses séduisantes. "
                "Exige davantage de preuves avant d'attribuer une conviction élevée.")
        elif st["monotone"] is True:
            lignes.append("→ Monotone : tes convictions hautes gagnent effectivement plus souvent. "
                          "Continue.")

    return "\n".join(lignes)


def rapport_calibration(closed: list[dict]) -> str:
    """Rapport lisible (Telegram / console) — pour TOI, pas pour le prompt."""
    st = stats_calibration(closed)
    lignes = ["🎯 CALIBRATION DE LA CONVICTION", ""]

    if st["n"] == 0:
        lignes.append("Aucun trade clôturé avec conviction enregistrée.")
        lignes.append("(Les positions ouvertes avant S10 n'ont pas de conviction tracée.)")
        return "\n".join(lignes)

    lignes.append(f"Trades exploitables : {st['n']}  |  Réussite globale (nette) : "
                  f"{st['taux_base']*100:.0f}%")
    lignes.append("")

    for t in st["tranches"]:
        marque = "✅" if t["fiable"] else "·"
        lignes.append(
            f"{marque} {t['libelle']:<22} n={t['n']:<3} "
            f"réussite {t['taux_reussite']*100:>3.0f}% "
            f"(IC {t['ci_bas']*100:.0f}–{t['ci_haut']*100:.0f}%)  "
            f"rend. net moy. {t['rendement_net_moyen_pct']:+.1f}%")

    lignes.append("")
    if not st["conclusif"]:
        lignes.append(f"⚖️ INDÉTERMINÉ — {st['n']} trade(s) < {SEUIL_CONCLUSIF} requis.")
        lignes.append("   Aucune conclusion n'est tirée. Le prompt du Directeur n'est PAS")
        lignes.append("   recalibré sur ces chiffres (on n'apprend pas du bruit).")
    else:
        lignes.append(f"Score de Brier : {st['brier']:.3f}  vs  naïf : {st['brier_naif']:.3f}")
        if st["informatif"]:
            lignes.append("✅ La conviction APPORTE de l'information (bat le modèle naïf).")
        else:
            lignes.append("❌ La conviction N'APPORTE PAS d'information — c'est du bruit décoratif.")
            lignes.append("   ⚠️ Conséquence : l'arbitrage de capital (« vendre la plus faible")
            lignes.append("      conviction ») repose alors sur un chiffre sans valeur.")
        if st["monotone"] is False:
            lignes.append("❌ NON MONOTONE : les convictions hautes ne gagnent pas plus.")
        elif st["monotone"] is True:
            lignes.append("✅ MONOTONE : les convictions hautes gagnent effectivement plus.")

    # Ventilations secondaires (informatives, jamais conclusives)
    secteurs = _ventilation(closed, "sector")
    if secteurs:
        lignes.append("")
        lignes.append("Par secteur (n ≥ 3, indicatif) :")
        for s in secteurs[:6]:
            lignes.append(f"  · {s['valeur']:<22} n={s['n']:<3} réussite {s['taux_reussite']*100:>3.0f}%"
                          f"  rend. net moy. {s['rendement_net_moyen_pct']:+.1f}%")

    motifs = _ventilation(closed, "exit_reason")
    if motifs:
        lignes.append("")
        lignes.append("Par motif de sortie (n ≥ 3, indicatif) :")
        for m in motifs:
            lignes.append(f"  · {m['valeur']:<22} n={m['n']:<3} "
                          f"rend. net moy. {m['rendement_net_moyen_pct']:+.1f}%")

    return "\n".join(lignes)


if __name__ == "__main__":
    from src.portfolio.paper_portfolio import load_portfolio
    p = load_portfolio()
    print()
    print(rapport_calibration([c.model_dump() for c in p.closed]))