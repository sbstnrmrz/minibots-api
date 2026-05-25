import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text

from app import models
from app.config import ALLOWED_ORIGINS, LOG_JSON
from app.database import engine
from app.db_pool import get_pool
from app.observability import RequestIDMiddleware, configure_logging
from app.rate_limit import limiter
from app.routers import agents, bots, chats, documents, products, templates, usage
from app.socket import sio, socket_app

configure_logging(json_logs=LOG_JSON)
logger = logging.getLogger("startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create ORM schema on demand. The dedicated psycopg pool
    # is opened lazily on first use; we touch it here so a cold start
    # eats the connect cost up front instead of on the first request.
    models.Base.metadata.create_all(bind=engine)
    pool = get_pool()
    logger.info("startup complete", extra={"pool_size": pool.get_stats().get("pool_size")})
    try:
        yield
    finally:
        logger.info("shutdown: closing pools")
        pool.close()
        engine.dispose()


app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/socket.io", socket_app)

app.include_router(bots.router)
app.include_router(templates.router)
app.include_router(products.router)
app.include_router(documents.router)
app.include_router(agents.router)
app.include_router(chats.router)
app.include_router(usage.router)


@app.get("/")
def root():
    return {"message": "Hello from minibots-api!"}


@app.get("/healthz")
def healthz():
    """Liveness probe — process is up. No external checks."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness probe — verifies a DB round-trip via the psycopg pool."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        logger.exception("readiness check failed")
        return {"status": "degraded", "error": str(e)}
