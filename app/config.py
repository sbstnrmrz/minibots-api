import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")
ALLOWED_ORIGINS: list[str] = ["*"] if ENVIRONMENT == "development" else [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
