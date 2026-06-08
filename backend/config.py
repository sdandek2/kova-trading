from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    anthropic_api_key: str
    gemini_api_key: str = ""
    database_url: str = ""

    # Phase 7 external data APIs (optional — connectors degrade gracefully without keys)
    uw_api_key: str = ""           # Unusual Whales — options flow (legacy)
    fmp_api_key: str = ""          # Financial Modeling Prep — earnings surprise
    quiver_api_key: str = ""       # Quiver Quantitative — dark pool (legacy)
    finnhub_api_key: str = ""      # Finnhub — analyst recommendation trends

    # Wheel Bot — separate Alpaca paper account (optional — falls back to main keys if not set)
    alpaca_wheel_key: str = ""     # Alpaca API key for wheel paper account
    alpaca_wheel_secret: str = ""  # Alpaca secret for wheel paper account

    class Config:
        env_file = ".env"


settings = Settings()
