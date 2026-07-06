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
    invalidation_price: float | None = None
    conviction: float | None = None          # ← conviction du Directeur (0-1), sert à l'arbitrage
    sector: str = ""                         # ← secteur de la thèse, pour le plafond de diversification
    horizon_days: int | None = None          # ← horizon de la thèse (jours), pour la sortie à l'échéance
    thesis_id: str = ""
    thesis_summary: str = ""
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
    spy_start_price: float | None = None      # ← AJOUTE : prix du S&P au démarrage
    started_at: str = Field(default_factory=_now)   # ← AJOUTE : date de démarrage

def _nouveau_portefeuille() -> Portfolio:
    """Crée un portefeuille neuf en figeant le prix de départ du S&P 500."""
    p = Portfolio()
    spy = get_fundamentals("SPY").get("price")
    p.spy_start_price = spy
    return p

def load_portfolio() -> Portfolio:
    # Cloud : lecture depuis Redis (persiste entre les exécutions)
    if _redis is not None:
        data = _redis.get(PORTFOLIO_KEY)
        if data:
            return Portfolio.model_validate_json(data)
        p = _nouveau_portefeuille()
        save_portfolio(p)
        return p
    # Local : lecture depuis le fichier JSON
    if PORTFOLIO_FILE.exists():
        return Portfolio.model_validate_json(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    p = _nouveau_portefeuille()
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
        stop_loss: float | None, profit_target: float | None,
        thesis_id: str = "", thesis_summary: str = "",
        invalidation_price: float | None = None,
        conviction: float | None = None, sector: str = "",
        horizon_days: int | None = None) -> str:
    if any(pos.ticker == ticker for pos in p.positions):
        return f"  ↪ {ticker} déjà en portefeuille — on n'ajoute pas."
    if not price or price <= 0:
        return f"  ↪ Prix indisponible pour {ticker} — achat annulé."
    dollars = (size_pct / 100.0) * p.starting_capital

    # 🛡️ Plafond par TITRE : aucun titre ne dépasse le plafond, quoi que dise le Directeur
    plafond_dollars = (settings.max_position_pct / 100.0) * p.starting_capital
    if dollars > plafond_dollars:
        dollars = plafond_dollars

    # 🛡️ Plafond par SECTEUR : on ne surconcentre pas un même thème macro
    if sector:
        plafond_secteur = (getattr(settings, "max_sector_pct", 40.0) / 100.0) * p.starting_capital
        expo_secteur = sum(pos.shares * pos.entry_price
                           for pos in p.positions if pos.sector == sector)
        headroom = plafond_secteur - expo_secteur
        if headroom <= 0:
            return (f"  ↪ Plafond secteur « {sector} » atteint "
                    f"({expo_secteur:.0f}$/{plafond_secteur:.0f}$) — {ticker} écarté.")
        if dollars > headroom:
            dollars = headroom   # on écrête à la place restante dans le secteur

    if dollars > p.cash:
        return f"  ↪ Cash insuffisant pour {ticker} ({dollars:.0f}$ demandés, {p.cash:.0f}$ dispo)."
    shares = round(dollars / price, 4)
    p.cash -= dollars
    p.positions.append(Position(
        ticker=ticker, shares=shares, entry_price=price,
        stop_loss=stop_loss, profit_target=profit_target,
        thesis_id=thesis_id, thesis_summary=thesis_summary,
        invalidation_price=invalidation_price,
        conviction=conviction, sector=sector, horizon_days=horizon_days,
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

def trim_position(p: Portfolio, pos: Position, exit_price: float,
                  fraction: float = 0.5, reason: str = "alleger") -> str:
    """Vend une FRACTION d'une position (moitié par défaut) et garde le reste."""
    shares_vendues = round(pos.shares * fraction, 4)
    if shares_vendues <= 0 or not exit_price:
        return f"  ↪ {pos.ticker} : rien à alléger."
    pnl = round((exit_price - pos.entry_price) * shares_vendues, 2)
    p.cash += shares_vendues * exit_price
    p.closed.append(ClosedPosition(
        ticker=pos.ticker, shares=shares_vendues, entry_price=pos.entry_price,
        exit_price=exit_price, realized_pnl=pnl, exit_reason=reason, opened_at=pos.opened_at,
    ))
    pos.shares = round(pos.shares - shares_vendues, 4)
    signe = "+" if pnl >= 0 else ""
    ligne = f"🔻 ALLÈGE {pos.ticker} : -{shares_vendues} actions @ {exit_price}$ → P&L {signe}{pnl}$"
    if pos.shares <= 0:              # sécurité : si tout est parti, on retire la position
        p.positions.remove(pos)
    return ligne

# ─── Vérifier les sorties (stop-loss / invalidation / objectif / échéance) ───
def check_exits(p: Portfolio) -> list[str]:
    alerts = []
    for pos in list(p.positions):   # copie : on modifie la liste pendant l'itération
        price = get_fundamentals(pos.ticker).get("price")
        if not price:
            continue
        if pos.stop_loss and price <= pos.stop_loss:
            alerts.append("🛑 STOP touché ! " + close_position(p, pos, price, "stop_loss"))
        elif pos.invalidation_price and price <= pos.invalidation_price:
            alerts.append("❌ THÈSE INVALIDÉE ! " + close_position(p, pos, price, "these_invalidee"))
        elif pos.profit_target and price >= pos.profit_target:
            alerts.append("🎯 OBJECTIF atteint ! " + close_position(p, pos, price, "profit_target"))
        elif (pos.horizon_days and _age_jours(pos) >= pos.horizon_days
              and price <= pos.entry_price):
            # ⏳ La fenêtre de la thèse est passée SANS qu'elle paie : on libère le capital.
            #    Une position GAGNANTE (price > entrée), elle, continue de courir.
            alerts.append("⏳ HORIZON ATTEINT (thèse non réalisée) ! "
                          + close_position(p, pos, price, "horizon_expire"))
    return alerts

def verifier_sorties() -> list[str]:
    """
    Protection mécanique indépendante : charge le portefeuille, vérifie stops /
    invalidation / objectifs / échéance sur TOUTES les positions ouvertes, applique
    les sorties déclenchées et sauvegarde. À appeler à CHAQUE cycle, sans attendre
    une décision. Renvoie le journal des sorties (vide si rien ne s'est déclenché).
    """
    p = load_portfolio()
    sorties = check_exits(p)
    if sorties:
        save_portfolio(p)
    return sorties

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

    # Comparaison avec le S&P 500 (avec rattrapage si le prix de départ manque)
    if not p.spy_start_price:
        spy_init = get_fundamentals("SPY").get("price")
        if spy_init:
            p.spy_start_price = spy_init
            save_portfolio(p)   # on fige enfin le point de départ

    if p.spy_start_price:
        spy_now = get_fundamentals("SPY").get("price")
        if spy_now:
            perf_spy = (spy_now / p.spy_start_price - 1) * 100
            alpha = perf - perf_spy
            s_spy = "+" if perf_spy >= 0 else ""
            s_alpha = "+" if alpha >= 0 else ""
            verdict = "🟢 tu BATS le marché" if alpha >= 0 else "🔴 le marché fait mieux"
            lignes += [
                "",
                f"  📊 S&P 500 (même période) : {s_spy}{perf_spy:.1f}%",
                f"  ⭐ Ton alpha : {s_alpha}{alpha:.1f}%  ({verdict})",
            ]
    return "\n".join(lignes)


# ─── Arbitrage de capital ───
# Quand le cash manque pour financer une nouvelle idée FORTE, on vend la position
# détenue la plus FAIBLE (conviction la plus basse) pour libérer le capital — mais
# seulement si l'écart de conviction est NET, si la position est assez ANCIENNE, et
# si la vente libère VRAIMENT assez de cash. Sinon, on ne touche à rien (pas de churn).
# Réglages surchargés par config/settings.py s'ils y sont définis (sinon, défauts sûrs).
_ARB_ACTIF = getattr(settings, "arbitrage_actif", True)
_ARB_MIN_EDGE = getattr(settings, "arbitrage_min_edge", 0.15)        # écart de conviction min (échelle 0-1)
_ARB_MIN_JOURS = getattr(settings, "arbitrage_min_holding_days", 3)  # âge min avant de pouvoir sacrifier

def _age_jours(pos: Position) -> float:
    """Nombre de jours depuis l'ouverture de la position."""
    try:
        ouverture = datetime.fromisoformat(pos.opened_at)
        if ouverture.tzinfo is None:
            ouverture = ouverture.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 1e9   # date illisible → traitée comme très ancienne (donc éligible)
    return (datetime.now(timezone.utc) - ouverture).total_seconds() / 86400.0

def _proximite_invalidation(pos: Position, prix: float | None) -> float:
    """
    Fragilité fondamentale : marge relative jusqu'au prix d'invalidation.
    Plus PETIT = plus proche de l'invalidation = plus fragile. Renvoie 1.0 si
    non défini. Sert uniquement de DÉPARTAGE entre deux convictions égales.
    """
    if not pos.invalidation_price or not prix or prix <= 0:
        return 1.0
    return max(0.0, min(1.0, (prix - pos.invalidation_price) / prix))

def tenter_arbitrage(p: Portfolio, plan) -> tuple[bool, list[str]]:
    """
    Tente UNE rotation de capital pour financer `plan` (un PositionPlan du Directeur) :
    vend la position détenue la plus faible si — et seulement si — toutes les
    conditions de sécurité sont réunies. Renvoie (rotation_effectuee, journal).
    """
    log: list[str] = []
    if not _ARB_ACTIF:
        return False, log

    conv_cible = getattr(plan, "conviction", None)
    if conv_cible is None:
        return False, log   # sans conviction sur la nouvelle idée, pas d'arbitrage

    # Coût (écrêté au plafond 15 %) de la position visée
    cout_cible = (min(plan.position_size_pct, settings.max_position_pct) / 100.0) * p.starting_capital

    # Candidats au sacrifice : conviction CONNUE, assez anciens, ticker différent de la cible
    candidats = [
        pos for pos in p.positions
        if pos.conviction is not None
        and pos.ticker != plan.ticker
        and _age_jours(pos) >= _ARB_MIN_JOURS
    ]
    if not candidats:
        log.append("  ↪ Arbitrage impossible : aucune position éligible "
                   "(conviction connue + assez ancienne).")
        return False, log

    # Prix courants (une seule fois) pour le départage et le calcul du produit de vente
    prix_courants = {pos.ticker: get_fundamentals(pos.ticker).get("price") for pos in candidats}

    # La plus FAIBLE = conviction la plus basse ; départage par proximité d'invalidation
    faible = min(candidats, key=lambda pos: (
        pos.conviction, _proximite_invalidation(pos, prix_courants.get(pos.ticker))
    ))

    # L'écart de conviction doit être NET (sinon on ne churne pas pour un gain marginal)
    ecart = conv_cible - faible.conviction
    if ecart < _ARB_MIN_EDGE:
        log.append(f"  ↪ Arbitrage écarté : idée {plan.ticker} ({conv_cible:.2f}) pas assez "
                   f"supérieure à {faible.ticker} ({faible.conviction:.2f}) — "
                   f"écart {ecart:.2f} < seuil {_ARB_MIN_EDGE:.2f}.")
        return False, log

    prix_faible = prix_courants.get(faible.ticker)
    if not prix_faible or prix_faible <= 0:
        log.append(f"  ↪ Arbitrage annulé : prix indisponible pour {faible.ticker}.")
        return False, log

    # La vente doit VRAIMENT libérer assez de cash, sinon on vendrait pour rien
    produit_vente = faible.shares * prix_faible
    if p.cash + produit_vente < cout_cible:
        log.append(f"  ↪ Arbitrage écarté : vendre {faible.ticker} ne libère pas assez "
                   f"({p.cash + produit_vente:.0f}$ dispo < {cout_cible:.0f}$ requis pour {plan.ticker}).")
        return False, log

    # ✅ Feu vert : on sacrifie la plus faible pour financer la cible
    log.append("  🔄 ARBITRAGE — " + close_position(p, faible, prix_faible, "arbitrage_capital"))
    log.append(f"     → capital réalloué vers {plan.ticker} "
               f"(conviction {conv_cible:.2f} vs {faible.conviction:.2f} sacrifiée).")
    return True, log


# ─── Arbitrage sectoriel ───
def _headroom_secteur(p: Portfolio, secteur: str) -> float:
    """Place restante (en $) dans un secteur avant d'atteindre le plafond."""
    plafond = (getattr(settings, "max_sector_pct", 40.0) / 100.0) * p.starting_capital
    expo = sum(pos.shares * pos.entry_price for pos in p.positions if pos.sector == secteur)
    return plafond - expo


def tenter_arbitrage_sectoriel(p: Portfolio, plan, secteur: str) -> tuple[bool, list[str]]:
    """
    Rotation INTRA-secteur : quand un secteur est plein, vend sa position la plus
    FAIBLE pour financer une nouvelle idée nettement plus forte DU MÊME secteur.
    Ne touche JAMAIS une position d'un autre secteur. Renvoie (rotation_effectuee, journal).
    """
    log: list[str] = []
    if not _ARB_ACTIF or not secteur:
        return False, log

    conv_cible = getattr(plan, "conviction", None)
    if conv_cible is None:
        return False, log

    cout_cible = (min(plan.position_size_pct, settings.max_position_pct) / 100.0) * p.starting_capital
    plafond_secteur = (getattr(settings, "max_sector_pct", 40.0) / 100.0) * p.starting_capital

    # Candidats : MÊME secteur, conviction connue, assez anciens, ticker différent
    candidats = [
        pos for pos in p.positions
        if pos.sector == secteur and pos.conviction is not None
        and pos.ticker != plan.ticker and _age_jours(pos) >= _ARB_MIN_JOURS
    ]
    if not candidats:
        log.append(f"  ↪ Arbitrage sectoriel « {secteur} » impossible : aucune position éligible dans ce secteur.")
        return False, log

    prix_courants = {pos.ticker: get_fundamentals(pos.ticker).get("price") for pos in candidats}
    faible = min(candidats, key=lambda pos: (
        pos.conviction, _proximite_invalidation(pos, prix_courants.get(pos.ticker))
    ))

    ecart = conv_cible - faible.conviction
    if ecart < _ARB_MIN_EDGE:
        log.append(f"  ↪ Arbitrage sectoriel écarté : {plan.ticker} ({conv_cible:.2f}) pas assez "
                   f"supérieure à {faible.ticker} ({faible.conviction:.2f}) — écart {ecart:.2f} < {_ARB_MIN_EDGE:.2f}.")
        return False, log

    prix_faible = prix_courants.get(faible.ticker)
    if not prix_faible or prix_faible <= 0:
        log.append(f"  ↪ Arbitrage sectoriel annulé : prix indisponible pour {faible.ticker}.")
        return False, log

    # La vente doit débloquer À LA FOIS assez de place SECTEUR et assez de CASH pour la cible
    cout_faible = faible.shares * faible.entry_price          # base de coût = place secteur libérée
    expo_secteur = sum(pos.shares * pos.entry_price for pos in p.positions if pos.sector == secteur)
    headroom_apres = plafond_secteur - (expo_secteur - cout_faible)
    cash_apres = p.cash + faible.shares * prix_faible
    if headroom_apres < cout_cible or cash_apres < cout_cible:
        log.append(f"  ↪ Arbitrage sectoriel écarté : vendre {faible.ticker} ne libère pas assez "
                   f"(secteur {headroom_apres:.0f}$ / cash {cash_apres:.0f}$ vs {cout_cible:.0f}$ requis).")
        return False, log

    log.append("  🔄 ARBITRAGE SECTORIEL — " + close_position(p, faible, prix_faible, "arbitrage_sectoriel"))
    log.append(f"     → place réallouée dans « {secteur} » vers {plan.ticker} "
               f"(conviction {conv_cible:.2f} vs {faible.conviction:.2f} sacrifiée).")
    return True, log


# ─── Enregistrer une décision du Directeur ───
def record_decision(thesis: MacroThesis, decision: PortfolioDecision) -> list[str]:
    """Vérifie les sorties, puis achète si la décision est EXECUTE. Renvoie un journal."""
    p = load_portfolio()
    log = check_exits(p)            # 1) on gère d'abord les sorties existantes

    if decision.action.value == "execute":   # 2) puis les nouveaux achats
        rotation_utilisee = False   # 🔄 au plus UNE rotation par cycle (sectorielle OU capital)
        for pos in decision.positions:
            if pos.position_size_pct and pos.position_size_pct > 0 and pos.entry_price:
                cout = (min(pos.position_size_pct, settings.max_position_pct) / 100.0) * p.starting_capital

                # 🔄 1) Le SECTEUR bloque-t-il ? -> rotation INTRA-secteur (vend la faible du secteur)
                if (not rotation_utilisee and thesis.sector
                        and _headroom_secteur(p, thesis.sector) < cout):
                    faite, journal_arb = tenter_arbitrage_sectoriel(p, pos, thesis.sector)
                    log.extend(journal_arb)
                    if faite:
                        rotation_utilisee = True

                # 🔄 2) Sinon le CASH bloque-t-il ? -> rotation portefeuille (vend la plus faible globale)
                if not rotation_utilisee and cout > p.cash:
                    faite, journal_arb = tenter_arbitrage(p, pos)
                    log.extend(journal_arb)
                    if faite:
                        rotation_utilisee = True

                resume = f"{thesis.theme} | {pos.rationale[:200]}"
                log.append(buy(p, pos.ticker, pos.entry_price, pos.position_size_pct,
                               pos.stop_loss, pos.profit_target, thesis.thesis_id, resume,
                               invalidation_price=pos.invalidation_price,
                               conviction=pos.conviction,
                               sector=thesis.sector,
                               horizon_days=thesis.time_horizon_days))
    else:
        log.append(f"  ↪ Décision {decision.action.value.upper()} : aucune position ouverte.")

    save_portfolio(p)
    return log


if __name__ == "__main__":
    print()
    print(snapshot_text(load_portfolio()))