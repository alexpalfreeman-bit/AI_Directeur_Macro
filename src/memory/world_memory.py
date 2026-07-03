"""
src/memory/world_memory.py

Mémoire du monde — journal horodaté des thèmes macro étudiés par le système.

But : donner au Directeur Macro-Automatisé une "conscience du temps qui passe".
À chaque comité lancé sur une thèse, on enregistre le thème macro du moment.
Avant de raisonner, l'agent Macro reçoit un résumé des ~30 derniers jours de
thèmes déjà étudiés, ce qui lui évite de repartir de zéro à chaque cycle et
l'aide à repérer une tendance persistante ou un retournement.

Stockage :
  - Cloud (officiel) : Upstash Redis via API REST, clé `world_events`.
  - Local (repli)     : fichier data/world_events.json.
  Les deux sont SÉPARÉS, exactement comme pour le portefeuille. En local,
  "Redis non configuré" est NORMAL (les variables ne sont que dans Render).

Ce module est autonome (ne dépend d'aucun autre fichier du projet) et ne
lève jamais d'exception vers l'appelant : en cas de pépin réseau, il se
rabat silencieusement sur le fichier local et journalise un avertissement.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

# --- Configuration ---------------------------------------------------------

CLE_REDIS = "world_events"          # clé Upstash pour le journal
FICHIER_LOCAL = Path("data/world_events.json")

RETENTION_JOURS = 90                # au-delà, on purge les vieux événements
MAX_EVENEMENTS_CONTEXTE = 40        # plafond d'événements injectés dans le prompt
TIMEOUT_REQUETE = 10                # secondes, pour ne jamais bloquer un cron

_URL = os.getenv("UPSTASH_REDIS_REST_URL")
_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")


def _redis_configure() -> bool:
    """Vrai si les deux variables Upstash sont présentes (donc en cloud)."""
    return bool(_URL and _TOKEN)


# --- Accès bas niveau au stockage -----------------------------------------

def _charger_evenements() -> list:
    """Charge la liste complète des événements (Redis si dispo, sinon fichier)."""
    if _redis_configure():
        try:
            reponse = requests.post(
                _URL,
                headers={"Authorization": f"Bearer {_TOKEN}"},
                json=["GET", CLE_REDIS],
                timeout=TIMEOUT_REQUETE,
            )
            reponse.raise_for_status()
            brut = reponse.json().get("result")
            if not brut:
                return []
            return json.loads(brut)
        except Exception as e:
            print(f"[world_memory] Lecture Redis echouee ({e}) — repli fichier local.")
            # on tente quand même le fichier local en dernier recours

    # Repli fichier local
    try:
        if FICHIER_LOCAL.exists():
            return json.loads(FICHIER_LOCAL.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[world_memory] Lecture fichier local echouee ({e}).")
    return []


def _sauvegarder_evenements(evenements: list) -> None:
    """Sauvegarde la liste complète (Redis si dispo, sinon fichier)."""
    charge_utile = json.dumps(evenements, ensure_ascii=False)

    if _redis_configure():
        try:
            reponse = requests.post(
                _URL,
                headers={"Authorization": f"Bearer {_TOKEN}"},
                json=["SET", CLE_REDIS, charge_utile],
                timeout=TIMEOUT_REQUETE,
            )
            reponse.raise_for_status()
            return
        except Exception as e:
            print(f"[world_memory] Ecriture Redis echouee ({e}) — repli fichier local.")

    # Repli fichier local
    try:
        FICHIER_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        FICHIER_LOCAL.write_text(charge_utile, encoding="utf-8")
    except Exception as e:
        print(f"[world_memory] Ecriture fichier local echouee ({e}).")


# --- API publique ----------------------------------------------------------

def enregistrer_evenement(
    theme: str,
    tickers: Optional[list] = None,
    regime: Optional[str] = None,
    resume: Optional[str] = None,
) -> None:
    """
    Enregistre un thème macro horodaté dans le journal du monde.

    À appeler dans `lancer_comite_sur_these` (voir insert d'intégration).

    Args:
        theme   : le thème macro central étudié (ex. "Résilience des engrais azotés").
        tickers : titres concernés (ex. ["CF", "NTR"]).
        regime  : régime de marché du moment ("RISK-ON" / "NEUTRE" / "RISK-OFF").
        resume  : une phrase de contexte optionnelle.
    """
    if not theme or not theme.strip():
        return  # rien à enregistrer

    evenements = _charger_evenements()

    evenements.append({
        "date": datetime.now(timezone.utc).isoformat(),
        "theme": theme.strip(),
        "tickers": tickers or [],
        "regime": regime or "",
        "resume": resume or "",
    })

    # Purge des événements trop anciens pour garder le journal léger
    limite = datetime.now(timezone.utc) - timedelta(days=RETENTION_JOURS)
    evenements = [e for e in evenements if _date_evenement(e) >= limite]

    _sauvegarder_evenements(evenements)


def contexte_historique(jours: int = 30) -> str:
    """
    Retourne un résumé texte des thèmes macro des `jours` derniers jours,
    prêt à être injecté dans le prompt de l'agent Macro.

    Renvoie un message neutre si l'historique est vide (première exécution).
    """
    evenements = _charger_evenements()
    limite = datetime.now(timezone.utc) - timedelta(days=jours)

    recents = [e for e in evenements if _date_evenement(e) >= limite]
    recents.sort(key=_date_evenement)  # du plus ancien au plus récent

    if not recents:
        return (
            f"CONTEXTE HISTORIQUE ({jours} derniers jours) : aucun theme macro "
            "enregistre (premiere execution ou historique vide)."
        )

    # On garde les plus récents si la liste est très longue
    recents = recents[-MAX_EVENEMENTS_CONTEXTE:]

    lignes = [f"CONTEXTE HISTORIQUE ({jours} derniers jours) — themes macro deja etudies :"]
    for e in recents:
        date_courte = _date_evenement(e).strftime("%Y-%m-%d")
        details = []
        if e.get("tickers"):
            details.append(", ".join(e["tickers"]))
        if e.get("regime"):
            details.append(f"regime {e['regime']}")
        suffixe = f" ({' ; '.join(details)})" if details else ""
        ligne = f"- [{date_courte}] {e['theme']}{suffixe}"
        if e.get("resume"):
            ligne += f" — {e['resume']}"
        lignes.append(ligne)

    return "\n".join(lignes)


# --- Utilitaire interne ----------------------------------------------------

def _date_evenement(e: dict) -> datetime:
    """Parse la date ISO d'un événement ; renvoie une date très ancienne si illisible."""
    try:
        d = datetime.fromisoformat(e["date"])
        # sécurité : on force la présence d'un fuseau (UTC) pour comparer sans erreur
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


# --- Test isolé ------------------------------------------------------------

if __name__ == "__main__":
    print(">>> Test isole de la memoire du monde\n")

    print("Redis configure :", _redis_configure(),
          "(False en local = normal)\n")

    print("Contexte AVANT enregistrement :")
    print(contexte_historique(), "\n")

    print("Enregistrement d'un theme de test...")
    enregistrer_evenement(
        theme="Test — resilience du secteur des engrais azotes",
        tickers=["CF", "NTR"],
        regime="RISK-ON",
        resume="Prix du gaz naturel en baisse, marges en hausse.",
    )

    print("\nContexte APRES enregistrement :")
    print(contexte_historique())