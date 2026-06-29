# src/schemas/decision.py
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field


class FinalAction(str, Enum):
    EXECUTE     = "execute"       # on prend la position
    WATCHLIST   = "watchlist"     # idée valable mais on attend un meilleur point d'entrée/timing
    REJECT      = "reject"        # on jette


class PositionPlan(BaseModel):
    """Le plan d'action concret pour UN ticker."""
    ticker: str
    conviction: float = Field(..., ge=0.0, le=1.0)
    position_size_pct: float = Field(..., ge=0.0, le=100.0)   # % du capital alloué
    entry_price: float | None = None
    profit_target: float | None = None
    stop_loss: float | None = None
    rationale: str = ""


class PortfolioDecision(BaseModel):
    """La décision finale et froide du Directeur — le livrable du Comité."""
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    thesis_id: str
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    action: FinalAction
    positions: list[PositionPlan] = Field(default_factory=list)
    macro_stop_loss: str = ""           # la condition macro qui invalide la thèse
    portfolio_rationale: str = ""        # pourquoi cette décision, en synthèse du débat
    confidence: float = Field(..., ge=0.0, le=1.0)