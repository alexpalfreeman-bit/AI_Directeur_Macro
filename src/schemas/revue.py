# src/schemas/revue.py
"""
Schéma du verdict du Gérant : la revue d'UNE position déjà ouverte.
Le Gérant relit la thèse d'origine, regarde les chiffres réels actuels,
et tranche : GARDER / ALLÉGER / VENDRE. Le LLM juge, il n'invente aucun nombre.
"""
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field


class ActionGerant(str, Enum):
    GARDER  = "garder"    # la thèse tient : on ne touche à rien
    ALLEGER = "alleger"   # thèse encore valable mais on réduit (risque/conviction)
    VENDRE  = "vendre"    # thèse cassée ou invalidée : on solde


class RevuePosition(BaseModel):
    """Le verdict du Gérant sur une position ouverte."""
    ticker: str
    action: ActionGerant
    conviction_restante: float = Field(..., ge=0.0, le=1.0)
    raison: str