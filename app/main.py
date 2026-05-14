from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models
from app.database import engine
from app.config import CORS_ORIGINS
from app.routers import bots, chat, templates, products

models.Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bots.router)
app.include_router(chat.router)
app.include_router(templates.router)
app.include_router(products.router)


@app.get("/")
def root():
    return {"message": "Hello from minibots-api!"}
