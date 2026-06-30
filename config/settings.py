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

settings = Settings()   # cet objet `settings` sera importé partout dans le projet