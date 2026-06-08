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

    # Wheel Bot — separate Alpaca account (completely isolated from Kova)
    alpaca_wheel_key: str = ""         # Alpaca API key for wheel account
    alpaca_wheel_secret: str = ""      # Alpaca secret for wheel account
    # Set this on Railway to switch paper ↔ live without any code change:
    #   paper: https://paper-api.alpaca.markets
    #   live:  https://api.alpaca.markets
    alpaca_wheel_base_url: str = "https://paper-api.alpaca.markets"

    class Config:
        env_file = ".env"


settings = Settings()
