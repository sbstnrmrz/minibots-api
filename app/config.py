import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")
API_TOKEN: str = os.getenv("API_TOKEN", "")
DEFAULT_TENANT_ID: str = os.getenv("DEFAULT_TENANT_ID", "fcbb503a-6e49-4e4c-ac58-fc232064513e")

# Seconds to wait after each incoming chat message before running the
# workflow. Additional messages arriving inside this window are
# concatenated and dispatched in a single turn, so a user typing in
# bursts gets one coherent reply.
CHAT_COALESCE_WINDOW_SECONDS: float = float(os.getenv("CHAT_COALESCE_WINDOW_SECONDS", "2"))

# Emit logs as JSON when running outside development. Easy to override
# locally with LOG_JSON=true if you want production-style output.
LOG_JSON: bool = os.getenv("LOG_JSON", "false" if ENVIRONMENT == "development" else "true").lower() == "true"

# Upload limits for /agents/setup. A single oversize file or a flood of
# them can exhaust memory and drag MarkItDown into long-running PDF parses.
MAX_UPLOAD_FILE_BYTES: int = int(os.getenv("MAX_UPLOAD_FILE_BYTES", str(20 * 1024 * 1024)))  # 20 MB
MAX_UPLOAD_FILE_COUNT: int = int(os.getenv("MAX_UPLOAD_FILE_COUNT", "10"))
# Suffix allow-list. Cheap and reliable; client-supplied content_type is not.
ALLOWED_UPLOAD_SUFFIXES: set[str] = {
    s.strip().lower() for s in os.getenv(
        "ALLOWED_UPLOAD_SUFFIXES",
        ".pdf,.docx,.doc,.txt,.md,.csv,.xlsx,.html,.htm,.json,.yaml,.yml",
    ).split(",") if s.strip()
}
ALLOWED_ORIGINS: list[str] = ["*"] if ENVIRONMENT == "development" else [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

GARAGE_ENDPOINT: str = os.getenv("GARAGE_ENDPOINT", "http://localhost:3900")
GARAGE_REGION: str = os.getenv("GARAGE_REGION", "garage")
GARAGE_ACCESS_KEY_ID: str = os.getenv("GARAGE_ACCESS_KEY_ID", "")
GARAGE_SECRET_ACCESS_KEY: str = os.getenv("GARAGE_SECRET_ACCESS_KEY", "")
GARAGE_BUCKET: str = os.getenv("GARAGE_BUCKET", "minibots")

# Scheduling agent
GCAL_SERVICE_ACCOUNT_JSON: str = os.getenv("GCAL_SERVICE_ACCOUNT_JSON", "")
GCAL_CALENDAR_ID: str = os.getenv("GCAL_CALENDAR_ID", "primary")
SCHEDULING_TIMEZONE: str = os.getenv("SCHEDULING_TIMEZONE", "UTC")
SCHEDULING_BUSINESS_START: str = os.getenv("SCHEDULING_BUSINESS_START", "09:00")
SCHEDULING_BUSINESS_END: str = os.getenv("SCHEDULING_BUSINESS_END", "18:00")
