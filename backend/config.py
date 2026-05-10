from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    anthropic_api_key: str
    gemini_api_key: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
