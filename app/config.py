import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")
API_TOKEN: str = os.getenv("API_TOKEN", "")
DEFAULT_TENANT_ID: str = os.getenv("DEFAULT_TENANT_ID", "fcbb503a-6e49-4e4c-ac58-fc232064513e")
ALLOWED_ORIGINS: list[str] = ["*"] if ENVIRONMENT == "development" else [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

GARAGE_ENDPOINT: str = os.getenv("GARAGE_ENDPOINT", "http://localhost:3900")
GARAGE_REGION: str = os.getenv("GARAGE_REGION", "garage")
GARAGE_ACCESS_KEY_ID: str = os.getenv("GARAGE_ACCESS_KEY_ID", "")
GARAGE_SECRET_ACCESS_KEY: str = os.getenv("GARAGE_SECRET_ACCESS_KEY", "")
GARAGE_BUCKET: str = os.getenv("GARAGE_BUCKET", "minibots")
