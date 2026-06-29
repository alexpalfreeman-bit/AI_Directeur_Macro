# src/schemas/thesis.py
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CatalystType(str, Enum):
    GEOPOLITICAL    = "geopolitical"
    MONETARY_POLICY = "monetary_policy"
    REGULATION      = "regulation"
    IPO             = "ipo"
    SUPPLY_CHAIN    = "supply_chain"
    OTHER           = "other"


class Direction(str, Enum):
    LONG  = "long"
    SHORT = "short"


class Catalyst(BaseModel):
    type: CatalystType
    description: str


class MacroThesis(BaseModel):
    thesis_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=_now)

    catalyst: Catalyst
    # La chaîne causale, étape par étape (1er -> 2e -> 3e ordre) :
    causal_chain: list[str] = Field(..., min_length=2)
    sector: str
    theme: str
    direction: Direction
    time_horizon_days: int = Field(..., ge=2, le=365)   # >= 2 : interdit le day trading
    candidate_tickers: list[str] = Field(..., min_length=1)
    regions: list[str]
    rationale: str
    confidence: float = Field(..., ge=0.0, le=1.0)
class TickerHealth(BaseModel):
    """Les chiffres RÉELS d'une action (viennent de l'API, jamais du LLM)."""
    ticker: str
    name: str | None = None
    price: float | None = None
    pe_ratio: float | None = None
    debt_to_equity: float | None = None
    market_cap: float | None = None
    volatility_30d_pct: float | None = None
    ev_to_ebitda: float | None = None      # ← AJOUTE cette ligne
    price_to_book: float | None = None     # ← AJOUTE cette ligne
    data_source: str = "yfinance"


class QuantValidation(BaseModel):
    """Le JUGEMENT du Quant (produit par le LLM, à partir des chiffres réels)."""
    thesis_id: str                       # 🔗 relie ce verdict à la thèse d'origine
    verdict: str = "incomplet"
    surviving_tickers: list[str] = Field(default_factory=list)
    rejected_tickers: list[str] = Field(default_factory=list)
    market_already_pricing_in: bool = False
    quant_notes: str = ""


class RiskSeverity(str, Enum):
    FATAL      = "fatal"        # la thèse est morte
    SERIOUS    = "serious"      # faille majeure, à corriger avant tout achat
    MANAGEABLE = "manageable"   # risques réels mais gérables
    MINOR      = "minor"        # solide, objections mineures


class RiskAssessment(BaseModel):
    """Le verdict de l'Avocat du Diable : la thèse survit-elle à la démolition ?"""
    thesis_id: str
    kill_shot: str = ""                                       # la meilleure tentative de démolition
    macro_regime_risks: list[str] = Field(default_factory=list)      # risk-off, carry trade, cyclicité
    causal_chain_weaknesses: list[str] = Field(default_factory=list) # maillons faibles, sur-ingénierie
    timing_seasonality_risks: list[str] = Field(default_factory=list)# hors-saison vs date du jour
    valuation_traps: list[str] = Field(default_factory=list)         # pièges PE cyclique, etc.
    data_quality_concerns: list[str] = Field(default_factory=list)   # tickers douteux, données nulles
    severity: RiskSeverity
    survives_scrutiny: bool                                   # tient-elle après démolition ?
    verdict_notes: str = ""