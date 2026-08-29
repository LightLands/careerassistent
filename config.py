import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    bot_token: str
    openrouter_api_key: Optional[str] = None
    openrouter_model: str = "google/gemini-2.0-flash-exp:free"
    proxy_url: Optional[str] = None
    log_level: str = "INFO"
    db_path: str = "career_bot.db"
    
    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN")
        if not token:
            raise ValueError("BOT_TOKEN не задан! Проверь .env файл.")
        
        return cls(
            bot_token=token,
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-exp:free"),
            proxy_url=os.getenv("PROXY_URL"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            db_path=os.getenv("DB_PATH", "career_bot.db")
        )

config = Config.from_env()
