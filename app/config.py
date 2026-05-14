import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
CORS_ORIGINS: list[str] = ["http://localhost:5173"]
