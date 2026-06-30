# src/portfolio/paper_portfolio.py
"""
Couche Portefeuille — Paper trading (SIMULATION, zéro argent réel).
Tient un portefeuille persistant qui se met à jour à chaque EXECUTE et à
chaque stop/objectif touché. Le fichier data/portfolio.json survit entre
les exécutions : c'est la mémoire de tes positions.
"""
from __future__ import annotations
import os                                    
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field
from upstash_redis import Redis              

from config.settings import settings
from src.ingestion.market_client import get_fundamentals
from src.schemas.thesis import MacroThesis
from src.schemas.decision import PortfolioDecision

PORTFOLIO_FILE = Path("data/portfolio.json")

# Connexion Redis (cloud) si les variables d'environnement existent.
# En local sans ces variables, on retombe automatiquement sur le fichier JSON.
_redis = None
_url = os.getenv("UPSTASH_REDIS_REST_URL")
_token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
if _url and _token:
    _redis = Redis(url=_url, token=_token)
if _redis is not None:
    print("✅ Redis (Upstash) ACTIVÉ — le portefeuille sera persistant.")
else:
    print("⚠️ Redis NON configuré (variables absentes) — repli sur fichier local.")
PORTFOLIO_KEY = "portfolio"

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Position(BaseModel):
    ticker: str
    shares: float
    entry_price: float
    stop_loss: float | None = None
    profit_target: float | None = None
    thesis_id: str = ""
    opened_at: str = Field(default_factory=_now)


class ClosedPosition(BaseModel):
    ticker: str
    shares: float
    entry_price: float
    exit_price: float
    realized_pnl: float
    exit_reason: str
    opened_at: str
    closed_at: str = Field(default_factory=_now)


class Portfolio(BaseModel):
    starting_capital: float = Field(default_factory=lambda: settings.starting_capital)
    cash: float = Field(default_factory=lambda: settings.starting_capital)
    positions: list[Position] = Field(default_factory=list)
    closed: list[ClosedPosition] = Field(default_factory=list)


def load_portfolio() -> Portfolio:
    # Cloud : lecture depuis Redis (persiste entre les exécutions)
    if _redis is not None:
        data = _redis.get(PORTFOLIO_KEY)
        if data:
            return Portfolio.model_validate_json(data)
        p = Portfolio()
        save_portfolio(p)
        return p
    # Local : lecture depuis le fichier JSON
    if PORTFOLIO_FILE.exists():
        return Portfolio.model_validate_json(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    p = Portfolio()
    save_portfolio(p)
    return p


def save_portfolio(p: Portfolio) -> None:
    if _redis is not None:
        _redis.set(PORTFOLIO_KEY, p.model_dump_json())
        return
    PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_FILE.write_text(p.model_dump_json(indent=2), encoding="utf-8")

# ─── Acheter (paper) ───
def buy(p: Portfolio, ticker: str, price: float, size_pct: float,
        stop_loss: float | None, profit_target: float | None, thesis_id: str = "") -> str:
    if any(pos.ticker == ticker for pos in p.positions):
        return f"  ↪ {ticker} déjà en portefeuille — on n'ajoute pas."
    if not price or price <= 0:
        return f"  ↪ Prix indisponible pour {ticker} — achat annulé."
    dollars = (size_pct / 100.0) * p.starting_capital
    if dollars > p.cash:
        return f"  ↪ Cash insuffisant pour {ticker} ({dollars:.0f}$ demandés, {p.cash:.0f}$ dispo)."
    shares = round(dollars / price, 4)
    p.cash -= dollars
    p.positions.append(Position(
        ticker=ticker, shares=shares, entry_price=price,
        stop_loss=stop_loss, profit_target=profit_target, thesis_id=thesis_id,
    ))
    return f"  ✅ ACHAT {ticker} : {shares} actions @ {price}$ ({dollars:.0f}$ = {size_pct}%)"


# ─── Clôturer une position ───
def close_position(p: Portfolio, pos: Position, exit_price: float, reason: str) -> str:
    pnl = round((exit_price - pos.entry_price) * pos.shares, 2)
    p.cash += pos.shares * exit_price
    p.closed.append(ClosedPosition(
        ticker=pos.ticker, shares=pos.shares, entry_price=pos.entry_price,
        exit_price=exit_price, realized_pnl=pnl, exit_reason=reason, opened_at=pos.opened_at,
    ))
    p.positions.remove(pos)
    signe = "+" if pnl >= 0 else ""
    return f"  💰 VENTE {pos.ticker} @ {exit_price}$ ({reason}) → P&L {signe}{pnl}$"


# ─── Vérifier les sorties (stop-loss / objectif touchés) ───
def check_exits(p: Portfolio) -> list[str]:
    alerts = []
    for pos in list(p.positions):   # copie : on modifie la liste pendant l'itération
        price = get_fundamentals(pos.ticker).get("price")
        if not price:
            continue
        if pos.stop_loss and price <= pos.stop_loss:
            alerts.append("🛑 STOP touché ! " + close_position(p, pos, price, "stop_loss"))
        elif pos.profit_target and price >= pos.profit_target:
            alerts.append("🎯 OBJECTIF atteint ! " + close_position(p, pos, price, "profit_target"))
    return alerts


# ─── Photo du portefeuille (valeur + performance) ───
def snapshot_text(p: Portfolio) -> str:
    lignes = ["📂 PORTEFEUILLE (paper trading)", ""]
    valeur_positions = 0.0
    if not p.positions:
        lignes.append("  (Aucune position ouverte)")
    for pos in p.positions:
        price = get_fundamentals(pos.ticker).get("price")
        if price:
            pnl = (price - pos.entry_price) * pos.shares
            pnl_pct = (price / pos.entry_price - 1) * 100
            valeur_positions += pos.shares * price
            s = "+" if pnl >= 0 else ""
            lignes.append(f"  • {pos.ticker} : entrée {pos.entry_price}$ → {price}$ "
                          f"| {s}{pnl:.0f}$ ({s}{pnl_pct:.1f}%)")
        else:
            valeur_positions += pos.shares * pos.entry_price
            lignes.append(f"  • {pos.ticker} : entrée {pos.entry_price}$ | prix indispo")

    valeur_totale = p.cash + valeur_positions
    perf = (valeur_totale / p.starting_capital - 1) * 100
    st = "+" if perf >= 0 else ""
    lignes += [
        "",
        f"  💵 Liquidités : {p.cash:.0f}$",
        f"  📈 Valeur positions : {valeur_positions:.0f}$",
        f"  💰 Valeur totale : {valeur_totale:.0f}$ (départ {p.starting_capital:.0f}$)",
        f"  🏁 Performance : {st}{perf:.1f}%",
    ]
    if p.closed:
        pnl_r = sum(c.realized_pnl for c in p.closed)
        s = "+" if pnl_r >= 0 else ""
        lignes.append(f"  📜 Trades clôturés : {len(p.closed)} | P&L réalisé {s}{pnl_r:.0f}$")
    return "\n".join(lignes)


# ─── Enregistrer une décision du Directeur ───
def record_decision(thesis: MacroThesis, decision: PortfolioDecision) -> list[str]:
    """Vérifie les sorties, puis achète si la décision est EXECUTE. Renvoie un journal."""
    p = load_portfolio()
    log = check_exits(p)            # 1) on gère d'abord les sorties existantes

    if decision.action.value == "execute":   # 2) puis les nouveaux achats
        for pos in decision.positions:
            if pos.position_size_pct and pos.position_size_pct > 0 and pos.entry_price:
                log.append(buy(p, pos.ticker, pos.entry_price, pos.position_size_pct,
                               pos.stop_loss, pos.profit_target, thesis.thesis_id))
    else:
        log.append(f"  ↪ Décision {decision.action.value.upper()} : aucune position ouverte.")

    save_portfolio(p)
    return log


if __name__ == "__main__":
    print()
    print(snapshot_text(load_portfolio()))