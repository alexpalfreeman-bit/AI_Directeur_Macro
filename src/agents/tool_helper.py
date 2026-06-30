# src/agents/tool_helper.py
"""
Utilitaire partagé : appelle Claude avec une sortie structurée (tool use),
valide le résultat avec Pydantic, et REDEMANDE automatiquement si un champ
manque — au lieu d'accepter une réponse incomplète.
"""
from pydantic import BaseModel, ValidationError


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
    for essai in range(1, max_essais + 1):
        response = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            tools=[tool], tool_choice={"type": "tool", "name": tool_name},
            messages=messages,
        )
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
            # On renvoie l'erreur au modèle pour qu'il corrige au prochain essai
            messages.append({"role": "assistant", "content": [block]})
            messages.append({"role": "user", "content": (
                f"Ta réponse était incomplète ou invalide :\n{e}\n\n"
                f"Refais l'appel à '{tool_name}' en remplissant TOUS les champs requis."
            )})
            print(f"  🔄 Quant : essai {essai} incomplet, on redemande...")

    # Si après tous les essais c'est toujours invalide, on lève l'erreur clairement
    raise ValueError(f"Échec après {max_essais} essais. Dernière erreur : {derniere_erreur}")