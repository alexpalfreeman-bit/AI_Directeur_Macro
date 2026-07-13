# config/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Clés API (chargées automatiquement depuis le .env) ──
    anthropic_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str

    # ── Choix des modèles ──
    llm_model: str = "claude-sonnet-4-6"             # raisonnement (les agents)
    cheap_model: str = "claude-haiku-4-5-20251001"   # bon marché (filtrage des news)
    director_model: str = "claude-opus-4-5"   # le jugement final mérite le meilleur modèle

    # ── Règles métier ──
    paper_trading: bool = True                        # TOUJOURS True au début
    max_position_risk_pct: float = 0.02               # max 2 % du capital par trade

    starting_capital: float = 10000.0   # capital virtuel de départ ($)
    risk_profile: str = "agressif"       # "prudent" | "modere" | "agressif"

    min_market_cap: float = 500_000_000      # 500 M$ : on évite les nano/micro-caps fragiles
    min_avg_volume: int = 300_000             # 300k actions/jour : liquidité minimale

    # ── R1a — Coûts de transaction (débités du cash à CHAQUE fill) ──
    # bps = points de base (1 bp = 0,01 %). Appliqués par CÔTÉ (achat ET vente) sur le
    # notionnel (actions × prix). Tiérés par liquidité : les large caps se négocient à
    # spread serré, les small/mid cycliques (CF, OLN, MP…) coûtent bien plus cher.
    cost_bps_per_side: float = 10.0            # large caps (spread + slippage + timing)
    cost_bps_per_side_smallcap: float = 30.0   # small/mid caps (moins liquides)
    smallcap_cap_threshold: float = 2_000_000_000.0   # < 2 Md$ ⇒ tarif small/mid

    # ── S9 — Dimensionnement par le RISQUE (et non par une taille arbitraire) ──
    # On ne fixe plus la taille, on fixe la PERTE MAXIMALE acceptée par position.
    # dollars = (risk_pct × équity) / distance_au_stop   → un stop large ⇒ position petite.
    risk_sizing_actif: bool = True
    max_position_risk_pct: float = 2.0    # % de l'équity risqué si le stop est touché
    atr_stop_multiple: float = 1.0        # la distance au stop ne peut être < 1 × ATR(14)
    min_ticket_usd: float = 100.0         # sous ce montant, la position est de la poussière

    # ── S11 — Diversification par la CORRÉLATION (et non par les étiquettes de secteur) ──
    # Deux titres de secteurs différents peuvent être un seul pari macro. Seule la
    # corrélation des rendements réels le révèle.
    correlation_active: bool = True
    max_correlation_moyenne: float = 0.65   # au-delà, le candidat double un pari existant
    correlation_jours: int = 90             # fenêtre de calcul (rendements quotidiens)
    correlation_min_obs: int = 40           # sous ce nb d'observations, on ne conclut pas

    # ── S13 — KILL-SWITCH de drawdown ──
    # Si l'équity chute de X% sous son PIC historique, on gèle les NOUVELLES entrées.
    # Les positions existantes continuent d'être gérées (stops, objectifs) normalement.
    killswitch_actif: bool = True
    max_drawdown_pct: float = 15.0      # gel des entrées au-delà de -15% depuis le pic
    killswitch_reprise_pct: float = 10.0  # reprise quand le drawdown remonte au-dessus de -10%

    max_position_pct: float = 15.0   # aucun titre ne dépasse 15% du capital (garde-fou dur)
    max_sector_pct: float = 40.0    # exposition max par secteur (% du capital de départ)
    arbitrage_actif: bool = True
    arbitrage_min_edge: float = 0.15          # écart de conviction min (0-1) idée vs plus faible détenue
    arbitrage_min_holding_days: int = 3       # jamais sacrifier une position ouverte depuis < N jours

settings = Settings()   # cet objet `settings` sera importé partout dans le projet