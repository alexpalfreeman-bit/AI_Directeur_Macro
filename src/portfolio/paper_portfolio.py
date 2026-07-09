# src/portfolio/paper_portfolio.py
"""
Couche Portefeuille — Paper trading (SIMULATION, zéro argent réel).
Tient un portefeuille persistant qui se met à jour à chaque EXECUTE et à
chaque stop/objectif touché. Le fichier data/portfolio.json survit entre
les exécutions : c'est la mémoire de tes positions.
"""
from __future__ import annotations
import os                                    
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field
from upstash_redis import Redis              

from config.settings import settings
from src.ingestion.market_client import get_fundamentals
from src.schemas.thesis import MacroThesis, Direction
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
PORTFOLIO_INIT_KEY = "portfolio_initialized"   # C4 — témoin : un portefeuille a déjà existé
VERROU_KEY = "verrou:portfolio"                # C3 — clé du verrou distribué

# 🎯 C1 — tolérance de dérive entre le prix supposé par le plan (celui du LLM) et le
# prix RÉEL au moment du fill. Au-delà, les niveaux (stop/objectif/invalidation) ne sont
# plus calibrés sur le marché : on refuse d'entrer plutôt que d'ouvrir une position bancale.
TOLERANCE_ENTREE_PCT = 1.5

# 🔒 C3 — paramètres du verrou. TTL large (LLM lents) ; un cron concurrent attend au plus
# ATTENTE_MAX puis SAUTE son cycle (on préfère sauter que de risquer une écriture concurrente).
VERROU_TTL_S = 600
VERROU_ATTENTE_MAX_S = 60
VERROU_INTERVALLE_S = 3.0


class LectureStockageErreur(RuntimeError):
    """C4 — Une LECTURE de stockage a échoué (ou incohérence détectée). L'appelant NE DOIT
    PAS écrire : écrire par-dessus détruirait l'historique. On lève, on n'écrase jamais."""


class VerrouIndisponible(RuntimeError):
    """C3 — Le verrou portefeuille n'a pas pu être acquis à temps : un autre cycle tourne.
    On saute proprement plutôt que de risquer une écriture concurrente (lost update)."""


# Script Lua de libération sûre : ne supprime le verrou QUE s'il nous appartient encore.
_LUA_LIBERER = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


@contextmanager
def verrou_portefeuille(ttl: int = VERROU_TTL_S,
                        attente_max: int = VERROU_ATTENTE_MAX_S,
                        intervalle: float = VERROU_INTERVALLE_S):
    """
    C3 — Verrou distribué autour d'un cycle qui lit-modifie-écrit le portefeuille.
    En LOCAL (pas de Redis), un seul processus tourne : le verrou est inutile → no-op.
    En CLOUD, SET NX EX pose le verrou ; s'il est déjà pris, on réessaie jusqu'à
    `attente_max`, sinon on lève VerrouIndisponible. Libération sûre par comparaison de jeton.
    """
    if _redis is None:
        yield
        return

    jeton = uuid.uuid4().hex
    debut = time.monotonic()
    acquis = False
    while True:
        try:
            ok = _redis.set(VERROU_KEY, jeton, nx=True, ex=ttl)
        except Exception as e:
            raise LectureStockageErreur(f"Verrou Redis inaccessible : {e}") from e
        if ok:
            acquis = True
            break
        if time.monotonic() - debut >= attente_max:
            break
        time.sleep(intervalle)

    if not acquis:
        raise VerrouIndisponible(
            f"Verrou « {VERROU_KEY} » déjà détenu depuis > {attente_max}s — "
            f"cycle sauté pour éviter une écriture concurrente.")

    try:
        yield
    finally:
        # Libération best-effort : à défaut, le TTL nettoiera de toute façon.
        try:
            _redis.eval(_LUA_LIBERER, [VERROU_KEY], [jeton])
        except Exception:
            try:
                if _redis.get(VERROU_KEY) == jeton:
                    _redis.delete(VERROU_KEY)
            except Exception:
                pass

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
        # C4 — une lecture qui ÉCHOUE ne doit jamais mener à recréer/écraser.
        try:
            data = _redis.get(PORTFOLIO_KEY)
        except Exception as e:
            raise LectureStockageErreur(
                f"Lecture Redis du portefeuille échouée ({e}) — écriture bloquée "
                f"pour ne pas écraser l'historique.") from e

        if data:
            # Auto-cicatrisation : marque tout portefeuille préexistant comme initialisé,
            # pour que la garde ci-dessous protège aussi les portefeuilles créés avant C4.
            try:
                _redis.set(PORTFOLIO_INIT_KEY, "1")
            except Exception:
                pass
            return Portfolio.model_validate_json(data)

        # Clé « portfolio » vide : VRAI premier démarrage, ou clé disparue/tronquée ?
        try:
            deja_initialise = _redis.get(PORTFOLIO_INIT_KEY)
        except Exception as e:
            raise LectureStockageErreur(
                f"Lecture du témoin d'initialisation échouée ({e}) — écriture bloquée.") from e

        if deja_initialise:
            # Un portefeuille a DÉJÀ existé mais la clé est vide : anomalie de stockage.
            raise LectureStockageErreur(
                "Clé « portfolio » vide alors que le témoin d'initialisation existe : "
                "anomalie de stockage. Refus de recréer un portefeuille neuf "
                "(protection anti-écrasement). Vérifie Upstash avant de relancer.")

        # Authentique premier démarrage : aucun portefeuille, aucun témoin.
        p = _nouveau_portefeuille()
        save_portfolio(p)
        return p

    # Local : lecture depuis le fichier JSON (mono-processus, comportement inchangé)
    if PORTFOLIO_FILE.exists():
        return Portfolio.model_validate_json(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    p = _nouveau_portefeuille()
    save_portfolio(p)
    return p


def save_portfolio(p: Portfolio) -> None:
    if _redis is not None:
        _redis.set(PORTFOLIO_KEY, p.model_dump_json())
        _redis.set(PORTFOLIO_INIT_KEY, "1")   # C4 — mémorise qu'un portefeuille a existé
        return
    PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_FILE.write_text(p.model_dump_json(indent=2), encoding="utf-8")

# ─── Acheter (paper) ───
# ─── S2/S4 — Bases de décision : équity courante & exposition en valeur de marché ───
def _prix_courant(ticker: str, repli: float) -> float:
    """Prix marché courant (via le cache market_client), repli sur le coût d'entrée si indispo."""
    prix = get_fundamentals(ticker).get("price")
    return prix if (prix and prix > 0) else repli


def _valeur_marche(pos: "Position") -> float:
    """Valeur de marché d'une position = actions × prix courant (repli : coût d'entrée)."""
    return pos.shares * _prix_courant(pos.ticker, pos.entry_price)


def equity_courante(p: "Portfolio") -> float:
    """S2 — Équity courante = cash + valeur de marché des positions ouvertes. C'est la BASE
    de dimensionnement : le risque % reste stable au lieu de gonfler quand l'équity baisse."""
    return p.cash + sum(_valeur_marche(pos) for pos in p.positions)


def _expo_secteur(p: "Portfolio", secteur: str) -> float:
    """S4 — Exposition d'un secteur en VALEUR DE MARCHÉ (plus en coût d'entrée)."""
    return sum(_valeur_marche(pos) for pos in p.positions if pos.sector == secteur)


def _secteur_reel(ticker: str, repli_texte: str) -> str:
    """S4 — Secteur STANDARDISÉ via yfinance (Energy, Technology…) au lieu du texte libre du
    LLM. Repli : le texte de la thèse normalisé (title-case) si yfinance ne renvoie rien."""
    s = get_fundamentals(ticker).get("sector")
    if s:
        return s
    return (repli_texte or "").strip().title() or "Inconnu"


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
    base = equity_courante(p)                       # S2 — dimensionnement sur l'ÉQUITY courante
    dollars = (size_pct / 100.0) * base

    # 🛡️ Plafond par TITRE : aucun titre ne dépasse le plafond, quoi que dise le Directeur
    plafond_dollars = (settings.max_position_pct / 100.0) * base
    if dollars > plafond_dollars:
        dollars = plafond_dollars

    # 🛡️ Plafond par SECTEUR : on ne surconcentre pas un même thème macro
    if sector:
        plafond_secteur = (getattr(settings, "max_sector_pct", 40.0) / 100.0) * base
        expo_secteur = _expo_secteur(p, sector)     # S4 — exposition en VALEUR DE MARCHÉ
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
    cout_cible = (min(plan.position_size_pct, settings.max_position_pct) / 100.0) * equity_courante(p)

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
    plafond = (getattr(settings, "max_sector_pct", 40.0) / 100.0) * equity_courante(p)   # S2
    expo = _expo_secteur(p, secteur)                                                      # S4
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

    cout_cible = (min(plan.position_size_pct, settings.max_position_pct) / 100.0) * equity_courante(p)
    plafond_secteur = (getattr(settings, "max_sector_pct", 40.0) / 100.0) * equity_courante(p)

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
    cout_faible = faible.shares * prix_faible                 # S4 — valeur de marché = place réellement libérée
    expo_secteur = _expo_secteur(p, secteur)                  # S4 — exposition en valeur de marché
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
def _plan_incoherent(entry: float | None, stop: float | None,
                     target: float | None, invalidation: float | None) -> str | None:
    """
    C2 — Valide la COHÉRENCE des niveaux d'un plan LONG. Renvoie une raison (str) si le
    plan est incohérent, sinon None. Ne contrôle que les niveaux FOURNIS (None = absent =
    non vérifié). Pour un LONG : stop SOUS l'entrée, objectif AU-DESSUS, invalidation SOUS.
    """
    if not entry or entry <= 0:
        return "prix d'entrée absent ou nul"
    if stop is not None and stop >= entry:
        return f"stop {stop}$ ≥ entrée {entry}$ (un stop LONG doit être SOUS l'entrée)"
    if target is not None and target <= entry:
        return f"objectif {target}$ ≤ entrée {entry}$ (un objectif LONG doit être AU-DESSUS de l'entrée)"
    if invalidation is not None and invalidation >= entry:
        return f"invalidation {invalidation}$ ≥ entrée {entry}$ (doit être SOUS l'entrée pour un LONG)"
    return None


def _prix_reel(ticker: str) -> float | None:
    """C1 — Prix RÉEL du marché au moment du fill (jamais celui imaginé par le LLM)."""
    fond = get_fundamentals(ticker) or {}
    prix = fond.get("price")
    return prix if (prix and prix > 0) else None


def record_decision(thesis: MacroThesis, decision: PortfolioDecision) -> list[str]:
    """Vérifie les sorties, puis achète si la décision est EXECUTE. Renvoie un journal."""
    p = load_portfolio()
    log = check_exits(p)            # 1) on gère d'abord les sorties existantes

    # 🛡️ C2 — Le portefeuille est LONG-ONLY. Une thèse SHORT ne doit JAMAIS être exécutée
    #    comme un achat LONG (ce qui inverserait complètement le pari). On refuse en bloc.
    if thesis.direction == Direction.SHORT:
        log.append(f"  ⛔ Thèse SHORT « {thesis.theme[:60]} » refusée : portefeuille long-only — "
                   f"on n'exécute pas un short comme un achat (ce serait le pari inverse).")
        save_portfolio(p)
        return log

    if decision.action.value == "execute":   # 2) puis les nouveaux achats
        rotation_utilisee = False   # 🔄 au plus UNE rotation par cycle (sectorielle OU capital)
        for pos in decision.positions:
            if not (pos.position_size_pct and pos.position_size_pct > 0 and pos.entry_price):
                continue

            # 🛡️ C2 — Cohérence des niveaux du plan (référence = entry_price du plan LLM).
            raison = _plan_incoherent(pos.entry_price, pos.stop_loss,
                                      pos.profit_target, pos.invalidation_price)
            if raison:
                log.append(f"  ⛔ {pos.ticker} écarté — plan incohérent : {raison}.")
                continue

            # 🛡️ C1 — On lit le PRIX RÉEL du marché ; on ne remplit jamais au prix du LLM.
            prix_reel = _prix_reel(pos.ticker)
            if prix_reel is None:
                log.append(f"  ⛔ {pos.ticker} écarté — prix marché indisponible au moment du fill.")
                continue

            # 🛡️ C1 — Si le marché a dérivé du plan au-delà de la tolérance, la thèse
            #    (stop/objectif/invalidation) n'est plus calibrée : on refuse d'entrer.
            derive_pct = abs(prix_reel / pos.entry_price - 1) * 100
            if derive_pct > TOLERANCE_ENTREE_PCT:
                log.append(f"  ⛔ {pos.ticker} écarté — le marché a bougé de {derive_pct:.1f}% "
                           f"vs le plan ({pos.entry_price}$ → {prix_reel}$, tolérance "
                           f"{TOLERANCE_ENTREE_PCT}%). Thèse à recalibrer.")
                continue

            # S4 — secteur STANDARDISÉ (yfinance) au lieu du texte libre du LLM ; sert aux
            # plafonds ET est stocké sur la position pour une exposition cohérente.
            secteur_reel = _secteur_reel(pos.ticker, thesis.sector)

            cout = (min(pos.position_size_pct, settings.max_position_pct) / 100.0) * equity_courante(p)

            # 🔄 1) Le SECTEUR bloque-t-il ? -> rotation INTRA-secteur (vend la faible du secteur)
            if (not rotation_utilisee and secteur_reel
                    and _headroom_secteur(p, secteur_reel) < cout):
                faite, journal_arb = tenter_arbitrage_sectoriel(p, pos, secteur_reel)
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
            # 🔑 C1 — on passe PRIX_REEL à buy(), plus jamais pos.entry_price.
            log.append(buy(p, pos.ticker, prix_reel, pos.position_size_pct,
                           pos.stop_loss, pos.profit_target, thesis.thesis_id, resume,
                           invalidation_price=pos.invalidation_price,
                           conviction=pos.conviction,
                           sector=secteur_reel,
                           horizon_days=thesis.time_horizon_days))
    else:
        log.append(f"  ↪ Décision {decision.action.value.upper()} : aucune position ouverte.")

    save_portfolio(p)
    return log


if __name__ == "__main__":
    print()
    print(snapshot_text(load_portfolio()))