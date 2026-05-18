import logging

from app.socket import sio, socket_app
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models
from app.database import engine
from app.config import ALLOWED_ORIGINS
from app.routers import bots, chat, templates, products, documents

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)-9s │ %(message)s",
    datefmt="%H:%M:%S",
)
# httpx logs every request at INFO — redundant with the llm.client call logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

models.Base.metadata.create_all(bind=engine)


app = FastAPI()
app.mount("/socket.io", socket_app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bots.router)
app.include_router(chat.router)
app.include_router(templates.router)
app.include_router(products.router)
app.include_router(documents.router)

@app.get("/")
def root():
    return {"message": "Hello from minibots-api!"}

