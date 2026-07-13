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

# R1b/S14 — au-delà de ce délai sans séance ouverte, un ordre en attente est annulé.
# 96h (4 jours) et non 72h : un ordre placé VENDREDI dont le lundi est FÉRIÉ (Labor Day,
# Thanksgiving…) n'a sa 1re séance que MARDI, soit >72h plus tard. À 72h, on annulait donc
# des ordres parfaitement valides qui n'avaient simplement jamais eu l'occasion de s'exécuter.
MAX_ATTENTE_ORDRE_H = 96

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
    entry_cost: float = 0.0                  # R1a — frais réels payés à l'achat (débités du cash)
    opened_at: str = Field(default_factory=_now)


class ClosedPosition(BaseModel):
    ticker: str
    shares: float
    entry_price: float
    exit_price: float
    realized_pnl: float                      # BRUT (différence de prix) — les coûts sont à part
    exit_reason: str
    entry_cost: float = 0.0                   # R1a — part des frais d'entrée imputée à ce lot
    exit_cost: float = 0.0                    # R1a — frais réels payés à la vente
    conviction: float | None = None           # S10 — la conviction qui a OUVERT ce trade
    sector: str = ""                          # S10 — secteur (calibration par secteur)
    thesis_id: str = ""                       # S10 — lien vers la thèse d'origine
    opened_at: str
    closed_at: str = Field(default_factory=_now)


class PendingOrder(BaseModel):
    """R1b — Ordre PLACÉ mais pas encore rempli. Il sera exécuté au prix d'OUVERTURE
    de la prochaine séance (réalisme : on ne peut pas acheter au close, marché fermé)."""
    ticker: str
    size_pct: float
    plan_price: float                        # prix de référence du plan (pour la garde de dérive C1)
    stop_loss: float
    profit_target: float
    invalidation_price: float | None = None
    conviction: float | None = None
    sector: str = ""
    horizon_days: int | None = None
    thesis_id: str = ""
    thesis_summary: str = ""
    placed_at: str = Field(default_factory=_now)   # instant du placement (référence pour l'open)


class Portfolio(BaseModel):
    starting_capital: float = Field(default_factory=lambda: settings.starting_capital)
    cash: float = Field(default_factory=lambda: settings.starting_capital)
    positions: list[Position] = Field(default_factory=list)
    closed: list[ClosedPosition] = Field(default_factory=list)
    pending: list[PendingOrder] = Field(default_factory=list)   # R1b — ordres en attente d'ouverture
    total_costs_paid: float = 0.0             # R1a — cumul des frais de transaction payés
    equity_peak: float = 0.0                  # S13 — plus haut historique de l'équity
    killswitch_gele: bool = False             # S13 — entrées gelées (drawdown excessif)
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


# ─── S11 — Diversification par la CORRÉLATION réelle ───
def _correlation_portefeuille(p: Portfolio, candidat: str) -> tuple[float | None, str]:
    """
    S11 — Corrélation MOYENNE PONDÉRÉE du candidat avec le portefeuille existant.

    Pondérée par la valeur de marché : une corrélation de 0,85 avec une position qui pèse
    12% du portefeuille est bien plus dangereuse que la même corrélation avec une ligne à 1%.
    Une moyenne simple masquerait ce fait.

    Renvoie (correlation, detail) ; (None, raison) si non calculable — auquel cas
    l'appelant LAISSE PASSER (on ne bloque jamais un achat sur une donnée manquante :
    ce serait transformer une panne yfinance en règle de gestion).
    """
    if not p.positions:
        return (None, "portefeuille vide")

    detenus = [pos.ticker for pos in p.positions if pos.ticker.upper() != candidat.upper()]
    if not detenus:
        return (None, "aucune autre position")

    try:
        from src.ingestion.market_client import get_correlations
        res = get_correlations(
            candidat, detenus,
            jours=getattr(settings, "correlation_jours", 90),
            min_obs=getattr(settings, "correlation_min_obs", 40),
        )
    except Exception as e:
        return (None, f"calcul indisponible ({e})")

    if not res.get("ok"):
        return (None, res.get("raison", "indisponible"))

    correls = res["correlations"]
    if not correls:
        return (None, "aucune corrélation calculable")

    # Pondération par la valeur de marché de chaque position détenue.
    total_poids = 0.0
    somme = 0.0
    pires: list[tuple[str, float]] = []
    for pos in p.positions:
        c = correls.get(pos.ticker)
        if c is None:
            continue
        prix = get_fundamentals(pos.ticker).get("price") or pos.entry_price
        poids = pos.shares * prix
        if poids <= 0:
            continue
        somme += c * poids
        total_poids += poids
        pires.append((pos.ticker, c))

    if total_poids <= 0:
        return (None, "poids nuls")

    moyenne = somme / total_poids
    pires.sort(key=lambda x: x[1], reverse=True)
    detail = ", ".join(f"{t} {c:+.2f}" for t, c in pires[:3])
    return (round(moyenne, 3), detail)



_cache_atr: dict[str, float | None] = {}

def _atr_ticker(ticker: str) -> float | None:
    """ATR(14) du titre, mis en cache pour ne pas rappeler yfinance à chaque fill.
    Import à l'appel : garde la compatibilité avec les bancs de test qui stubbent
    market_client sans exposer get_atr."""
    cle = ticker.upper()
    if cle in _cache_atr:
        return _cache_atr[cle]
    try:
        from src.ingestion.market_client import get_atr
        valeur = get_atr(ticker)
    except Exception:
        valeur = None
    _cache_atr[cle] = valeur
    return valeur


# ─── R1a — Coûts de transaction (débités du cash à CHAQUE fill) ───
def _frais_bps(ticker: str) -> float:
    """
    R1a — Coût par CÔTÉ en bps, tiéré par la liquidité (via la capitalisation yfinance,
    déjà en cache dans le cycle). Capitalisation inconnue ⇒ tarif small-cap (biais
    CONSERVATEUR : quand on doute, on paie plus cher — un paper trading doit se sous-flatter).
    """
    cap = get_fundamentals(ticker).get("market_cap")
    seuil = getattr(settings, "smallcap_cap_threshold", 2_000_000_000.0)
    bps_large = getattr(settings, "cost_bps_per_side", 10.0)
    bps_small = getattr(settings, "cost_bps_per_side_smallcap", 30.0)
    if cap is None:
        return bps_small
    return bps_large if cap >= seuil else bps_small


def _frais(ticker: str, notionnel: float) -> float:
    """Frais réels (en $) pour un côté, sur un notionnel = actions × prix."""
    if not notionnel or notionnel <= 0:
        return 0.0
    return round(notionnel * _frais_bps(ticker) / 10_000.0, 2)


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

    # ─── S13 — KILL-SWITCH : aucune nouvelle entrée pendant un drawdown sévère ───
    if getattr(settings, "killswitch_actif", False) and p.killswitch_gele:
        return (f"  🚨 {ticker} REFUSÉ — kill-switch actif (drawdown > "
                f"{getattr(settings, 'max_drawdown_pct', 15.0):.0f}% depuis le pic). "
                f"Aucune nouvelle entrée tant que le portefeuille n'est pas redressé.")

    # ─── S11 — GARDE DE CORRÉLATION ───
    # Les plafonds sectoriels croient diversifier ; la corrélation dit la vérité. Trois titres
    # dans trois "secteurs" peuvent être un SEUL pari macro (la demande cyclique). Si le
    # candidat est trop corrélé au livre existant, on ne diversifie pas : on DOUBLE la mise.
    if getattr(settings, "correlation_active", False) and p.positions:
        correl, detail = _correlation_portefeuille(p, ticker)
        seuil = getattr(settings, "max_correlation_moyenne", 0.65)
        if correl is not None and correl > seuil:
            return (f"  ⛔ {ticker} écarté — corrélation moyenne {correl:+.2f} avec le "
                    f"portefeuille (> {seuil:.2f}) : ce n'est pas une diversification, "
                    f"c'est le même pari en double. Plus corrélés : {detail}.")
        # correl is None → donnée indisponible : on LAISSE PASSER (une panne yfinance ne
        # doit pas devenir une règle de gestion), mais on le dit dans le journal.

    base = equity_courante(p)                       # S2 — dimensionnement sur l'ÉQUITY courante
    dollars = (size_pct / 100.0) * base
    note_risque = ""

    # ─── S9 — DIMENSIONNEMENT PAR LE RISQUE ───
    # On ne fixe plus la taille, on fixe la PERTE MAX. Une position dont le stop est loin
    # est plus PETITE, à budget de risque égal. Sans cela, deux positions de même taille
    # mais aux stops différents portent des risques radicalement différents.
    if getattr(settings, "risk_sizing_actif", False) and stop_loss and stop_loss < price:
        distance = price - stop_loss

        # 🛡️ PLANCHER ATR — le garde-fou essentiel. Un stop plus serré que la respiration
        #    normale du titre (a) serait touché par le simple bruit, (b) ferait EXPLOSER la
        #    taille (on divise par la distance). On ne dimensionne jamais comme si un titre
        #    était plus calme qu'il ne l'est. L'ATR vient de yfinance — jamais du LLM.
        atr = _atr_ticker(ticker)
        if atr:
            plancher = getattr(settings, "atr_stop_multiple", 1.0) * atr
            if distance < plancher:
                note_risque = f" | stop serré ({distance:.2f}$) planché à 1×ATR ({plancher:.2f}$)"
                distance = plancher

        budget_risque = (getattr(settings, "max_position_risk_pct", 2.0) / 100.0) * base
        actions_max = budget_risque / distance
        dollars_risque = actions_max * price

        # On prend le MINIMUM : le risque ne peut que RÉDUIRE la taille voulue par le
        # Directeur, jamais l'augmenter. Laisser un stop serré gonfler la position serait
        # confier le levier au LLM — exactement ce qu'on veut éviter.
        if dollars_risque < dollars:
            note_risque = (f" | taille réduite par le risque ({dollars:.0f}$ → {dollars_risque:.0f}$, "
                           f"perte max {budget_risque:.0f}$ = {getattr(settings,'max_position_risk_pct',2.0)}%)"
                           + note_risque)
            dollars = dollars_risque

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

    # S9 — plancher de ticket : une position de 3$ n'a aucun sens (frais > enjeu)
    ticket_min = getattr(settings, "min_ticket_usd", 100.0)
    if dollars < ticket_min:
        return (f"  ↪ {ticker} écarté — taille finale trop petite ({dollars:.0f}$ < "
                f"{ticket_min:.0f}$ minimum) après plafonds/risque.")

    # R1a — frais d'entrée réels, débités du cash EN PLUS du notionnel
    frais_entree = _frais(ticker, dollars)
    if dollars + frais_entree > p.cash:
        return (f"  ↪ Cash insuffisant pour {ticker} "
                f"({dollars:.0f}$ + {frais_entree:.2f}$ frais, {p.cash:.0f}$ dispo).")
    shares = round(dollars / price, 4)
    p.cash -= (dollars + frais_entree)                       # R1a — notionnel + frais
    p.total_costs_paid = round(p.total_costs_paid + frais_entree, 2)
    p.positions.append(Position(
        ticker=ticker, shares=shares, entry_price=price,
        stop_loss=stop_loss, profit_target=profit_target,
        thesis_id=thesis_id, thesis_summary=thesis_summary,
        invalidation_price=invalidation_price,
        conviction=conviction, sector=sector, horizon_days=horizon_days,
        entry_cost=frais_entree,
    ))
    pct_reel = (dollars / base * 100.0) if base else 0.0
    return (f"  ✅ ACHAT {ticker} : {shares} actions @ {price}$ "
            f"({dollars:.0f}$ = {pct_reel:.1f}% équity | frais {frais_entree:.2f}$){note_risque}")

# ─── Clôturer une position ───
def close_position(p: Portfolio, pos: Position, exit_price: float, reason: str) -> str:
    pnl = round((exit_price - pos.entry_price) * pos.shares, 2)   # BRUT (hors frais)
    frais_sortie = _frais(pos.ticker, pos.shares * exit_price)     # R1a
    p.cash += pos.shares * exit_price - frais_sortie              # R1a — produit NET de frais
    p.total_costs_paid = round(p.total_costs_paid + frais_sortie, 2)
    p.closed.append(ClosedPosition(
        ticker=pos.ticker, shares=pos.shares, entry_price=pos.entry_price,
        exit_price=exit_price, realized_pnl=pnl, exit_reason=reason, opened_at=pos.opened_at,
        entry_cost=pos.entry_cost, exit_cost=frais_sortie,
        conviction=pos.conviction, sector=pos.sector, thesis_id=pos.thesis_id,   # S10
    ))
    p.positions.remove(pos)
    pnl_net = round(pnl - pos.entry_cost - frais_sortie, 2)       # net des DEUX côtés
    signe = "+" if pnl_net >= 0 else ""
    return (f"  💰 VENTE {pos.ticker} @ {exit_price}$ ({reason}) → "
            f"P&L net {signe}{pnl_net}$ (brut {'+' if pnl>=0 else ''}{pnl}$, frais {round(pos.entry_cost+frais_sortie,2)}$)")

def trim_position(p: Portfolio, pos: Position, exit_price: float,
                  fraction: float = 0.5, reason: str = "alleger") -> str:
    """Vend une FRACTION d'une position (moitié par défaut) et garde le reste."""
    shares_vendues = round(pos.shares * fraction, 4)
    if shares_vendues <= 0 or not exit_price:
        return f"  ↪ {pos.ticker} : rien à alléger."
    pnl = round((exit_price - pos.entry_price) * shares_vendues, 2)   # BRUT
    frais_sortie = _frais(pos.ticker, shares_vendues * exit_price)     # R1a
    # R1a — part des frais d'entrée imputée au lot vendu (au prorata des actions)
    part = (shares_vendues / pos.shares) if pos.shares else 0.0
    entry_cost_lot = round(pos.entry_cost * part, 2)
    p.cash += shares_vendues * exit_price - frais_sortie              # R1a — produit NET
    p.total_costs_paid = round(p.total_costs_paid + frais_sortie, 2)
    p.closed.append(ClosedPosition(
        ticker=pos.ticker, shares=shares_vendues, entry_price=pos.entry_price,
        exit_price=exit_price, realized_pnl=pnl, exit_reason=reason, opened_at=pos.opened_at,
        entry_cost=entry_cost_lot, exit_cost=frais_sortie,
        conviction=pos.conviction, sector=pos.sector, thesis_id=pos.thesis_id,   # S10
    ))
    pos.entry_cost = round(pos.entry_cost - entry_cost_lot, 2)        # le reste garde sa part
    pos.shares = round(pos.shares - shares_vendues, 4)
    pnl_net = round(pnl - entry_cost_lot - frais_sortie, 2)
    signe = "+" if pnl_net >= 0 else ""
    ligne = (f"🔻 ALLÈGE {pos.ticker} : -{shares_vendues} actions @ {exit_price}$ → "
             f"P&L net {signe}{pnl_net}$ (frais {round(entry_cost_lot+frais_sortie,2)}$)")
    if pos.shares <= 0:              # sécurité : si tout est parti, on retire la position
        p.positions.remove(pos)
    return ligne

# ─── Vérifier les sorties (stop-loss / invalidation / objectif / échéance) ───
def _prix_sortie_baissiere(niveau: float, ohlc: dict) -> float | None:
    """
    R1c — Un niveau de sortie BAISSIER (stop, invalidation) est-il touché en séance ?

    Deux cas, et c'est toute la différence avec un test sur le close :
      • GAP AU TRAVERS : la séance OUVRE déjà sous le niveau → en réel, l'ordre stop
        devient un ordre au marché et s'exécute à l'OPEN, PAS au niveau du stop.
        C'est le « slippage de gap » : la raison pour laquelle un stop ne protège
        jamais autant qu'on le croit. On remplit donc à l'open (plus bas = plus honnête).
      • TOUCHÉ EN SÉANCE : le Low descend jusqu'au niveau → fill AU niveau.

    Renvoie le prix de sortie, ou None si le niveau n'a pas été touché.
    """
    if not niveau:
        return None
    if ohlc["open"] <= niveau:          # gap au travers : on subit l'open
        return ohlc["open"]
    if ohlc["low"] <= niveau:           # touché en séance : fill au niveau
        return niveau
    return None


def _prix_sortie_haussiere(niveau: float, ohlc: dict) -> float | None:
    """R1c — Symétrique pour un objectif de profit (gap au-dessus → fill à l'open)."""
    if not niveau:
        return None
    if ohlc["open"] >= niveau:
        return ohlc["open"]
    if ohlc["high"] >= niveau:
        return niveau
    return None


def check_exits(p: Portfolio) -> list[str]:
    """
    R1c — Les niveaux mécaniques sont testés contre le HIGH/LOW de la séance, plus
    seulement contre le dernier prix. Un stop touché en intraday DÉCLENCHE, même si
    le titre a rebondi avant la clôture — c'est ce qui se passerait en réel.

    Ordre de priorité conservateur quand plusieurs niveaux sont touchés dans la même
    séance : stop > invalidation > objectif. On ne peut pas savoir, sur une barre
    journalière, si le bas a précédé le haut ; on suppose donc le PIRE (le stop d'abord).
    Se supposer chanceux est la façon la plus courante de se mentir en backtest.

    Si l'OHLC est indisponible, on retombe proprement sur l'ancien test au dernier prix.
    """
    from src.ingestion.market_client import get_seance_ohlc   # import à l'appel (stubs de test)

    alerts = []
    for pos in list(p.positions):   # copie : on modifie la liste pendant l'itération
        ohlc = get_seance_ohlc(pos.ticker)

        if not ohlc.get("ok"):
            # Repli : OHLC indisponible → ancien comportement (dernier prix connu).
            price = get_fundamentals(pos.ticker).get("price")
            if not price:
                continue
            ohlc = {"open": price, "high": price, "low": price, "close": price}

        close = ohlc["close"]

        # 1) STOP — le plus prioritaire (hypothèse conservatrice)
        prix = _prix_sortie_baissiere(pos.stop_loss, ohlc)
        if prix is not None:
            gap = " (GAP au travers — fill à l'ouverture)" if ohlc["open"] <= pos.stop_loss else ""
            alerts.append(f"🛑 STOP touché en séance{gap} ! "
                          + close_position(p, pos, prix, "stop_loss"))
            continue

        # 2) INVALIDATION de la thèse
        prix = _prix_sortie_baissiere(pos.invalidation_price, ohlc)
        if prix is not None:
            gap = (" (GAP au travers — fill à l'ouverture)"
                   if pos.invalidation_price and ohlc["open"] <= pos.invalidation_price else "")
            alerts.append(f"❌ THÈSE INVALIDÉE en séance{gap} ! "
                          + close_position(p, pos, prix, "these_invalidee"))
            continue

        # 3) OBJECTIF de profit
        prix = _prix_sortie_haussiere(pos.profit_target, ohlc)
        if prix is not None:
            alerts.append("🎯 OBJECTIF atteint en séance ! "
                          + close_position(p, pos, prix, "profit_target"))
            continue

        # 4) ÉCHÉANCE — évaluée à la CLÔTURE (ce n'est pas un ordre au marché, mais une
        #    décision de fin de journée : on libère le capital d'une thèse qui n'a pas payé).
        if (pos.horizon_days and _age_jours(pos) >= pos.horizon_days
                and close <= pos.entry_price):
            alerts.append("⏳ HORIZON ATTEINT (thèse non réalisée) ! "
                          + close_position(p, pos, close, "horizon_expire"))
    return alerts

def maj_killswitch(p: Portfolio) -> list[str]:
    """
    S13 — KILL-SWITCH DE DRAWDOWN.

    Met à jour le pic d'équity et gèle les NOUVELLES entrées si le portefeuille est tombé
    de plus de `max_drawdown_pct` sous ce pic. Les positions existantes continuent d'être
    gérées normalement (stops, objectifs, échéances) — on ne liquide RIEN, on arrête
    seulement de creuser.

    Pourquoi : c'est la règle que tout gérant institutionnel a et qu'un système autonome
    n'a pas. Sans elle, un pipeline qui tourne 3×/jour peut enchaîner les entrées perdantes
    pendant des semaines, sans qu'aucun humain n'ait à valider. Le kill-switch est le
    disjoncteur.

    HYSTÉRÉSIS : on gèle à -15%, mais on ne reprend qu'à -10%. Sans cet écart, une équity
    qui oscille autour du seuil ferait clignoter le système (gel/dégel à chaque cycle).
    """
    if not getattr(settings, "killswitch_actif", False):
        return []

    equity = equity_courante(p)
    if equity <= 0:
        return []

    # Le pic ne descend jamais (c'est un plus-haut historique).
    if equity > p.equity_peak:
        p.equity_peak = round(equity, 2)
    if p.equity_peak <= 0:
        p.equity_peak = round(equity, 2)
        return []

    drawdown_pct = (equity / p.equity_peak - 1) * 100      # négatif en drawdown
    seuil_gel = -abs(getattr(settings, "max_drawdown_pct", 15.0))
    seuil_reprise = -abs(getattr(settings, "killswitch_reprise_pct", 10.0))

    journal = []
    if not p.killswitch_gele and drawdown_pct <= seuil_gel:
        p.killswitch_gele = True
        journal.append(
            f"🚨 KILL-SWITCH ACTIVÉ — drawdown {drawdown_pct:.1f}% depuis le pic "
            f"({p.equity_peak:.0f}$ → {equity:.0f}$). NOUVELLES ENTRÉES GELÉES. "
            f"Les positions existantes restent gérées (stops/objectifs actifs). "
            f"Reprise automatique au-dessus de {seuil_reprise:.0f}%.")
    elif p.killswitch_gele and drawdown_pct > seuil_reprise:
        p.killswitch_gele = False
        journal.append(
            f"✅ KILL-SWITCH LEVÉ — drawdown revenu à {drawdown_pct:.1f}% "
            f"(> {seuil_reprise:.0f}%). Les nouvelles entrées reprennent.")
    elif p.killswitch_gele:
        journal.append(
            f"🚨 Entrées toujours GELÉES — drawdown {drawdown_pct:.1f}% "
            f"(reprise au-dessus de {seuil_reprise:.0f}%).")
    return journal


def verifier_sorties() -> list[str]:
    """
    Protection mécanique indépendante : charge le portefeuille, exécute les ordres en
    attente (R1b), vérifie stops / invalidation / objectifs / échéance sur TOUTES les
    positions ouvertes, applique les sorties déclenchées et sauvegarde. À appeler à
    CHAQUE cycle, sans attendre une décision. Renvoie le journal (vide si rien).

    R1b — C'est ICI que les ordres placés sont remplis, et non dans record_decision
    seul : un jour sans thèse retenue ne doit pas laisser un ordre coincé en attente.
    """
    p = load_portfolio()
    fills = executer_ordres_en_attente(p)     # R1b — remplissage à l'OUVERTURE
    sorties = check_exits(p)
    alerte_ks = maj_killswitch(p)             # S13 — disjoncteur de drawdown
    if fills or sorties or alerte_ks:
        save_portfolio(p)
    return fills + sorties + alerte_ks

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
        pnl_r = sum(c.realized_pnl for c in p.closed)               # brut
        frais_r = sum(getattr(c, "entry_cost", 0.0) + getattr(c, "exit_cost", 0.0) for c in p.closed)
        pnl_net = pnl_r - frais_r
        s = "+" if pnl_r >= 0 else ""
        sn = "+" if pnl_net >= 0 else ""
        lignes.append(f"  📜 Trades clôturés : {len(p.closed)} | P&L brut {s}{pnl_r:.0f}$ "
                      f"| net {sn}{pnl_net:.0f}$ (frais −{frais_r:.0f}$)")
    if getattr(p, "killswitch_gele", False):
        lignes.append(f"  🚨 KILL-SWITCH ACTIF — nouvelles entrées gelées "
                      f"(drawdown > {getattr(settings, 'max_drawdown_pct', 15.0):.0f}% "
                      f"depuis le pic de {p.equity_peak:.0f}$)")
    if getattr(p, "pending", None):
        lignes.append(f"  📝 Ordres en attente d'ouverture : "
                      + ", ".join(f"{o.ticker} ({o.size_pct}%)" for o in p.pending))
    if getattr(p, "total_costs_paid", 0.0) > 0:
        lignes.append(f"  💸 Frais de transaction payés (cumul) : {p.total_costs_paid:.2f}$")

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


def executer_ordres_en_attente(p: Portfolio) -> list[str]:
    """
    R1b — Exécute les ordres placés lors des cycles précédents, au prix d'OUVERTURE
    de la première séance ouverte APRÈS le placement.

    Appelée en TÊTE de chaque cycle. Trois issues par ordre :
      • Pas encore de séance ouverte  → l'ordre RESTE en attente (rien ne se passe).
      • Séance ouverte + open valide  → on remplit à l'open réel (frais R1a débités).
      • Dérive > tolérance / ordre trop vieux → ANNULÉ (la thèse n'est plus calibrée).

    Le prix vient de yfinance. Le LLM ne fournit jamais un prix de fill.
    """
    if not p.pending:
        return []

    log: list[str] = []
    restants: list[PendingOrder] = []

    # Import à l'appel (et non au chargement du module) : garde la compatibilité avec
    # les bancs de test qui stubbent market_client sans exposer get_open_apres.
    from src.ingestion.market_client import get_open_apres

    for ordre in p.pending:
        res = get_open_apres(ordre.ticker, ordre.placed_at)

        # 1) Aucune séance depuis le placement → on patiente (cas normal : week-end, soirée)
        if not res.get("pret"):
            age_h = _age_heures(ordre.placed_at)
            if age_h > MAX_ATTENTE_ORDRE_H:
                log.append(f"  ⌛ Ordre {ordre.ticker} ANNULÉ — en attente depuis {age_h:.0f}h "
                           f"(> {MAX_ATTENTE_ORDRE_H}h) : thèse périmée.")
                continue          # on le laisse tomber (pas dans restants)
            restants.append(ordre)
            continue

        prix_open = res["open"]

        # 2) Garde de dérive (C1) — appliquée à l'OPEN réel, cette fois elle mord vraiment :
        #    si le titre a gappé au-delà de la tolérance, stop/objectif ne valent plus rien.
        derive_pct = abs(prix_open / ordre.plan_price - 1) * 100 if ordre.plan_price else 0.0
        if derive_pct > TOLERANCE_ENTREE_PCT:
            log.append(f"  ⛔ Ordre {ordre.ticker} ANNULÉ — gap à l'ouverture : "
                       f"{ordre.plan_price}$ → {prix_open}$ ({derive_pct:.1f}% > "
                       f"{TOLERANCE_ENTREE_PCT}%). Thèse à recalibrer.")
            continue

        # 3) Fill au prix d'OUVERTURE réel (frais R1a débités par buy())
        log.append(f"  🔔 Ouverture {res['date']} — exécution de l'ordre {ordre.ticker} :")
        log.append(buy(p, ordre.ticker, prix_open, ordre.size_pct,
                       ordre.stop_loss, ordre.profit_target,
                       ordre.thesis_id, ordre.thesis_summary,
                       invalidation_price=ordre.invalidation_price,
                       conviction=ordre.conviction,
                       sector=ordre.sector,
                       horizon_days=ordre.horizon_days))

    p.pending = restants
    return log


def _age_heures(iso: str) -> float:
    """Âge en heures d'un horodatage ISO (0 si illisible)."""
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except Exception:
        return 0.0


def record_decision(thesis: MacroThesis, decision: PortfolioDecision) -> list[str]:
    """Vérifie les sorties, puis achète si la décision est EXECUTE. Renvoie un journal."""
    p = load_portfolio()
    # R1b — 0) d'abord, on remplit les ordres placés aux cycles précédents (au prix d'OUVERTURE).
    log = executer_ordres_en_attente(p)
    log += check_exits(p)           # 1) puis on gère les sorties existantes

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

            # 🛡️ C1 — Le prix réel sert ici à VALIDER que le plan est encore calibré
            #    (garde de dérive), PAS à remplir : le fill se fera à l'OPEN de la
            #    prochaine séance (R1b), car décider marché fermé n'autorise aucun fill.
            prix_reel = _prix_reel(pos.ticker)
            if prix_reel is None:
                log.append(f"  ⛔ {pos.ticker} écarté — prix marché indisponible au moment du fill.")
                continue

            derive_pct = abs(prix_reel / pos.entry_price - 1) * 100
            if derive_pct > TOLERANCE_ENTREE_PCT:
                log.append(f"  ⛔ {pos.ticker} écarté — le marché a bougé de {derive_pct:.1f}% "
                           f"vs le plan ({pos.entry_price}$ → {prix_reel}$, tolérance "
                           f"{TOLERANCE_ENTREE_PCT}%). Thèse à recalibrer.")
                continue

            # R1b — anti-doublon : un ordre déjà en attente sur ce ticker ? on n'empile pas.
            if any(o.ticker.upper() == pos.ticker.upper() for o in p.pending):
                log.append(f"  ↪ {pos.ticker} : un ordre est déjà en attente d'ouverture.")
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
            # 🔑 R1b — On PLACE l'ordre ; il sera rempli au prix d'OUVERTURE de la
            #    prochaine séance (prix yfinance réel), pas au close inexécutable.
            p.pending.append(PendingOrder(
                ticker=pos.ticker, size_pct=pos.position_size_pct,
                plan_price=pos.entry_price,
                stop_loss=pos.stop_loss, profit_target=pos.profit_target,
                invalidation_price=pos.invalidation_price,
                conviction=pos.conviction, sector=secteur_reel,
                horizon_days=thesis.time_horizon_days,
                thesis_id=thesis.thesis_id, thesis_summary=resume,
            ))
            log.append(f"  📝 ORDRE PLACÉ {pos.ticker} ({pos.position_size_pct}%) — "
                       f"exécution à l'OUVERTURE de la prochaine séance.")
    else:
        log.append(f"  ↪ Décision {decision.action.value.upper()} : aucune position ouverte.")

    save_portfolio(p)
    return log


if __name__ == "__main__":
    print()
    print(snapshot_text(load_portfolio()))