import logging

from app.socket import sio, socket_app
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app import models
from app.database import engine
from app.config import ALLOWED_ORIGINS
from app.rate_limit import limiter
from app.routers import bots, templates, products, documents, agents

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)-9s │ %(message)s",
    datefmt="%H:%M:%S",
)
# httpx logs every request at INFO — redundant with the llm.client call logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

models.Base.metadata.create_all(bind=engine)


app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.mount("/socket.io", socket_app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bots.router)
app.include_router(templates.router)
app.include_router(products.router)
app.include_router(documents.router)
app.include_router(agents.router)

@app.get("/")
def root():
    return {"message": "Hello from minibots-api!"}

