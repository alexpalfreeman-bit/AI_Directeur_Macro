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

🛡️ C5 — ANTI-ÉCRASEMENT (même protection que C4 sur le portefeuille) :
Une LECTURE qui échoue ne renvoie plus `[]`. Elle LÈVE `LectureStockageErreur`.
Les fonctions d'ÉCRITURE (enregistrer_evenement) attrapent cette erreur et
N'ÉCRIVENT PAS — sinon un simple hoquet réseau Upstash effacerait 90 jours de
mémoire en la remplaçant par un seul événement. Les fonctions de LECTURE pure
(contexte_historique) dégradent proprement : pas d'historique ce cycle, sans
jamais rien détruire.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

# --- Configuration ---------------------------------------------------------

CLE_REDIS = "world_events"                 # clé Upstash pour le journal
CLE_INIT = "world_events_initialized"      # C5 — témoin : un journal a déjà existé
FICHIER_LOCAL = Path("data/world_events.json")

RETENTION_JOURS = 90                # au-delà, on purge les vieux événements
MAX_EVENEMENTS_CONTEXTE = 40        # plafond d'événements injectés dans le prompt
TIMEOUT_REQUETE = 10                # secondes, pour ne jamais bloquer un cron

_URL = os.getenv("UPSTASH_REDIS_REST_URL")
_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")


class LectureStockageErreur(RuntimeError):
    """C5 — Une LECTURE du stockage a échoué (réseau) ou une incohérence a été
    détectée (clé vide alors qu'un témoin d'init existe). L'appelant NE DOIT PAS
    écrire : écrire par-dessus détruirait l'historique. On lève, on n'écrase jamais."""


def _redis_configure() -> bool:
    """Vrai si les deux variables Upstash sont présentes (donc en cloud)."""
    return bool(_URL and _TOKEN)


# --- Accès bas niveau au stockage Redis (REST) -----------------------------

def _redis_get(cle: str) -> Optional[str]:
    """GET brut sur Upstash REST. Renvoie la valeur (str) ou None si la clé est
    absente. LÈVE en cas d'échec réseau/HTTP (jamais de repli silencieux)."""
    reponse = requests.post(
        _URL,
        headers={"Authorization": f"Bearer {_TOKEN}"},
        json=["GET", cle],
        timeout=TIMEOUT_REQUETE,
    )
    reponse.raise_for_status()
    return reponse.json().get("result")


def _redis_set(cle: str, valeur: str) -> None:
    """SET brut sur Upstash REST. LÈVE en cas d'échec (l'appelant décide du repli)."""
    reponse = requests.post(
        _URL,
        headers={"Authorization": f"Bearer {_TOKEN}"},
        json=["SET", cle, valeur],
        timeout=TIMEOUT_REQUETE,
    )
    reponse.raise_for_status()


# --- Chargement / sauvegarde de la liste d'événements ----------------------

def _charger_evenements() -> list:
    """
    Charge la liste complète des événements.

    C5 — Sémantique stricte :
      • Cloud : une lecture Redis qui ÉCHOUE lève LectureStockageErreur (au lieu
        de renvoyer [] puis de laisser l'écriture écraser l'historique). Une clé
        VIDE alors que le témoin d'init existe = anomalie → on lève aussi.
      • Local : fichier absent = premier démarrage → []. Fichier présent mais
        illisible/corrompu = on lève (on n'écrase pas un fichier qu'on n'a pas su lire).
    """
    if _redis_configure():
        try:
            brut = _redis_get(CLE_REDIS)
        except Exception as e:
            raise LectureStockageErreur(
                f"Lecture Redis du journal échouée ({e}) — écriture bloquée "
                f"pour ne pas écraser l'historique."
            ) from e

        if brut:
            # Auto-cicatrisation : on marque le journal comme initialisé, pour que
            # la garde ci-dessous protège aussi les journaux créés avant C5.
            try:
                _redis_set(CLE_INIT, "1")
            except Exception:
                pass  # best-effort : le témoin se reposera au prochain succès
            try:
                return json.loads(brut)
            except Exception as e:
                raise LectureStockageErreur(
                    f"Journal Redis illisible (JSON corrompu : {e}) — écriture bloquée."
                ) from e

        # Clé vide : VRAI premier démarrage, ou clé disparue/tronquée ?
        try:
            deja_init = _redis_get(CLE_INIT)
        except Exception as e:
            raise LectureStockageErreur(
                f"Lecture du témoin d'initialisation échouée ({e}) — écriture bloquée."
            ) from e
        if deja_init:
            raise LectureStockageErreur(
                "Clé « world_events » vide alors que le témoin d'init existe : "
                "anomalie de stockage. Refus d'écrire par-dessus (anti-écrasement)."
            )
        return []  # authentique premier démarrage

    # --- Local (mono-processus) ---
    if not FICHIER_LOCAL.exists():
        return []  # premier démarrage local
    try:
        return json.loads(FICHIER_LOCAL.read_text(encoding="utf-8"))
    except Exception as e:
        raise LectureStockageErreur(
            f"Fichier local du journal illisible ({e}) — écriture bloquée."
        ) from e


def _sauvegarder_evenements(evenements: list) -> None:
    """Sauvegarde la liste complète (Redis si dispo, sinon fichier). Pose le témoin
    d'init après une écriture Redis réussie."""
    charge_utile = json.dumps(evenements, ensure_ascii=False)

    if _redis_configure():
        _redis_set(CLE_REDIS, charge_utile)   # lève si échec → l'appelant journalise
        try:
            _redis_set(CLE_INIT, "1")          # C5 — mémorise qu'un journal a existé
        except Exception:
            pass
        return

    # Repli fichier local
    FICHIER_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    FICHIER_LOCAL.write_text(charge_utile, encoding="utf-8")


# --- API publique ----------------------------------------------------------

def enregistrer_evenement(
    theme: str,
    tickers: Optional[list] = None,
    regime: Optional[str] = None,
    resume: Optional[str] = None,
) -> None:
    """
    Enregistre un thème macro horodaté dans le journal du monde.

    C5 — Si la LECTURE préalable échoue, on N'ÉCRIT PAS (on préfère perdre CET
    événement plutôt que d'écraser tout l'historique). On journalise et on sort.
    """
    if not theme or not theme.strip():
        return  # rien à enregistrer

    try:
        evenements = _charger_evenements()
    except LectureStockageErreur as e:
        print(f"[world_memory] ⚠️ Enregistrement ABANDONNÉ (anti-écrasement) : {e}")
        return

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

    try:
        _sauvegarder_evenements(evenements)
    except Exception as e:
        # Écriture ratée : on n'a rien détruit (l'ancienne valeur Redis est intacte).
        print(f"[world_memory] ⚠️ Écriture du journal échouée ({e}) — historique préservé.")


def contexte_historique(jours: int = 30) -> str:
    """
    Retourne un résumé texte des thèmes macro des `jours` derniers jours,
    prêt à être injecté dans le prompt de l'agent Macro.

    C5 — LECTURE pure : si le stockage est illisible, on DÉGRADE proprement
    (message neutre « historique indisponible »), sans jamais écrire ni lever
    vers l'appelant : un cycle sans contexte historique reste un cycle valide.
    """
    try:
        evenements = _charger_evenements()
    except LectureStockageErreur as e:
        print(f"[world_memory] ⚠️ Contexte historique indisponible ce cycle ({e}).")
        return (
            f"CONTEXTE HISTORIQUE ({jours} derniers jours) : temporairement "
            "indisponible (lecture du stockage en échec ce cycle)."
        )

    limite = datetime.now(timezone.utc) - timedelta(days=jours)
    recents = [e for e in evenements if _date_evenement(e) >= limite]
    recents.sort(key=_date_evenement)  # du plus ancien au plus récent

    if not recents:
        return (
            f"CONTEXTE HISTORIQUE ({jours} derniers jours) : aucun theme macro "
            "enregistre (premiere execution ou historique vide)."
        )

    recents = recents[-MAX_EVENEMENTS_CONTEXTE:]  # on garde les plus récents

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
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


# --- Test isolé ------------------------------------------------------------

if __name__ == "__main__":
    print(">>> Test isole de la memoire du monde\n")
    print("Redis configure :", _redis_configure(), "(False en local = normal)\n")
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