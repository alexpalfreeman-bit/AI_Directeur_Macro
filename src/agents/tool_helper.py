# src/agents/tool_helper.py
"""
Utilitaire partagé : appelle Claude avec une sortie structurée (tool use),
valide le résultat avec Pydantic, et REDEMANDE automatiquement si un champ
manque — au lieu d'accepter une réponse incomplète.

🔁 R2 — RÉSILIENCE AUX ERREURS API TRANSITOIRES (529 / 429 / 5xx)
Avant, `client.messages.create(...)` n'était protégé par AUCUN try/except : une erreur
529 « overloaded_error » remontait telle quelle et tuait l'étape entière.

  • 429 `rate_limit_error`  → TES limites de compte (monter de palier aiderait).
  • 529 `overloaded_error`  → l'infrastructure d'Anthropic est saturée, tous utilisateurs
    confondus. Ta requête était VALIDE. Payer plus n'y change RIEN. La seule réponse
    correcte : un retry borné avec backoff exponentiel.

Deux garde-fous :
  1. ON NE RETENTE QUE LE TRANSITOIRE. Un 400/401 échouera identiquement : on échoue VITE.
  2. BUDGET GLOBAL BORNÉ. Le cycle tient un verrou Redis (TTL 600 s). Si chaque appel
     pouvait retenter une minute, le cycle dépasserait le verrou et un cron concurrent
     entrerait en pleine écriture. On plafonne le temps TOTAL d'attente du processus.
"""
import random
import time

from pydantic import BaseModel, ValidationError

MAX_ESSAIS_API = 4            # 1 tentative + 3 reprises
BACKOFF_BASE_S = 2.0          # 2 s, 4 s, 8 s…
BACKOFF_MAX_S = 20.0
BUDGET_RETRY_TOTAL_S = 120.0  # ⏱️ pour TOUT le processus — très en deçà du verrou (600 s)
CODES_TRANSITOIRES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_budget_consomme_s = 0.0      # un cron Render = un processus neuf → repart à 0


def _statut_erreur(e) -> int | None:
    """Code HTTP d'une exception SDK, sans dépendre des noms de classes du SDK."""
    code = getattr(e, "status_code", None)
    if isinstance(code, int):
        return code
    code = getattr(getattr(e, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def _est_transitoire(e) -> tuple[bool, int | None]:
    """(cette erreur mérite-t-elle une reprise ?, code_http_ou_None)"""
    code = _statut_erreur(e)
    if code is not None:
        return (code in CODES_TRANSITOIRES, code)
    nom = type(e).__name__.lower()
    if any(m in nom for m in ("connection", "timeout")):
        return (True, None)
    texte = str(e).lower()
    if "overloaded" in texte or "529" in texte:
        return (True, 529)
    if "timeout" in texte or "temporarily unavailable" in texte:
        return (True, None)
    return (False, None)


def _delai_suggere(e) -> float | None:
    """Respecte `retry-after` quand le serveur le fournit (fréquent sur 429)."""
    entetes = getattr(getattr(e, "response", None), "headers", None)
    if not entetes:
        return None
    try:
        v = entetes.get("retry-after")
        return float(v) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _appeler_api(client, **kwargs):
    """messages.create avec backoff exponentiel + jitter sur erreurs transitoires,
    et budget d'attente GLOBAL (le verrou C3 n'est jamais menacé)."""
    global _budget_consomme_s
    for essai in range(1, MAX_ESSAIS_API + 1):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            transitoire, code = _est_transitoire(e)
            etiquette = f"HTTP {code}" if code else type(e).__name__
            if not transitoire:
                print(f"  ✖ Erreur API non transitoire ({etiquette}) — abandon immédiat.")
                raise
            if essai >= MAX_ESSAIS_API:
                print(f"  ✖ Erreur API {etiquette} persistante après {MAX_ESSAIS_API} essais.")
                raise
            attente = _delai_suggere(e)
            if attente is None:
                attente = min(BACKOFF_BASE_S * (2 ** (essai - 1)), BACKOFF_MAX_S)
                attente *= random.uniform(0.75, 1.25)      # jitter anti-troupeau
            restant = BUDGET_RETRY_TOTAL_S - _budget_consomme_s
            if restant <= 0:
                print(f"  ✖ Budget de reprise épuisé ({BUDGET_RETRY_TOTAL_S:.0f}s/cycle) — "
                      f"abandon pour ne pas dépasser le verrou.")
                raise
            attente = min(attente, restant)
            motif = "API Anthropic surchargée (529)" if code == 529 else f"erreur transitoire {etiquette}"
            print(f"  ⏳ {motif} — nouvelle tentative dans {attente:.1f}s (essai {essai}/{MAX_ESSAIS_API}).")
            time.sleep(attente)
            _budget_consomme_s += attente
    raise RuntimeError("Boucle de reprise API terminée sans résultat.")


def budget_retry_restant_s() -> float:
    return max(0.0, BUDGET_RETRY_TOTAL_S - _budget_consomme_s)


def reinitialiser_budget_retry() -> None:
    global _budget_consomme_s
    _budget_consomme_s = 0.0


def appel_avec_retry(client, model, system, user_content, tool_name,
                     schema: type[BaseModel], max_tokens=1500, max_essais=3,
                     forcer_id: dict | None = None):
    """
    Demande à Claude de remplir `schema` via l'outil `tool_name`.
    Si la sortie est invalide/incomplète, réessaie jusqu'à `max_essais` fois
    en signalant l'erreur au modèle. Renvoie une instance validée de `schema`.
    """
    tool = {
        "name": tool_name,
        "description": f"Renvoie un objet structuré complet et valide.",
        "input_schema": schema.model_json_schema(),
    }
    messages = [{"role": "user", "content": user_content}]
    forcer_id = forcer_id or {}

    derniere_erreur = None
    tokens = max_tokens
    for essai in range(1, max_essais + 1):
        # 🔁 R2 — appel réseau protégé (backoff 529/429/5xx, budget global borné)
        response = _appeler_api(
            client,
            model=model, max_tokens=tokens, system=system,
            tools=[tool], tool_choice={"type": "tool", "name": tool_name},
            messages=messages,
        )
        # 🛡️ S6 — réponse TRONQUÉE (budget de tokens atteint) : un tool_use coupé au milieu
        #    est invalide et ferait échouer tous les essais au même plafond. On double le
        #    budget pour le prochain essai (plafonné), au lieu de ré-échouer identiquement.
        if getattr(response, "stop_reason", None) == "max_tokens":
            tokens = min(tokens * 2, 8192)
            print(f"  ⚠️  Réponse tronquée (max_tokens) — budget porté à {tokens} au prochain essai.")
        block = next((b for b in response.content if b.type == "tool_use"), None)
        if block is None:
            derniere_erreur = "Aucun appel d'outil dans la réponse."
            continue

        data = dict(block.input)
        for cle in forcer_id:               # on retire les champs qu'on impose nous-mêmes
            data.pop(cle, None)

        try:
            return schema(**{**data, **forcer_id})   # ✅ validé et complet
        except ValidationError as e:
            derniere_erreur = str(e)
            # On renvoie l'erreur au modèle via un tool_result (OBLIGATOIRE après un tool_use)
            messages.append({"role": "assistant", "content": [block]})
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": block.id,
                "is_error": True,
                "content": (
                    f"Ta réponse était incomplète ou invalide :\n{e}\n\n"
                    f"Refais l'appel à '{tool_name}' en remplissant TOUS les champs requis."
                ),
            }]})
            print(f"  🔄 {tool_name} : essai {essai} incomplet, on redemande...")

    # Si après tous les essais c'est toujours invalide, on lève l'erreur clairement
    raise ValueError(f"Échec après {max_essais} essais. Dernière erreur : {derniere_erreur}")