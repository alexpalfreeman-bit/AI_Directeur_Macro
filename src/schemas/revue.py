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

class RevuePortefeuille(BaseModel):
    """G1 — Les verdicts du Gérant sur TOUTES les positions, en UNE réponse.

    Avant : un appel LLM par position (13 positions = 13 appels Sonnet/jour, ~0,33 $).
    Maintenant : un seul appel qui voit le portefeuille ENTIER — ce qui est aussi
    meilleur sur le fond : le Gérant peut arbitrer entre positions (deux lignes sur le
    même thème, concentration) au lieu de juger chacune isolément."""
    verdicts: list[RevuePosition] = Field(default_factory=list)