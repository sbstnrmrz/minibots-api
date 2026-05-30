from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any

from app.auth import require_api_key
from app.database import get_db
from app import models
from app.schemas.bot import BotCreate, BotResponse
from app.schemas.chat import ChatMessageResponse

router = APIRouter(prefix="/bots", tags=["bots"])


@router.post("", response_model=BotResponse)
def create_bot(
    body: BotCreate,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    bot = models.Bot(
        tenant_id=current_tenant.id,
        name=body.name,
        bot_type=body.bot_type,
        spreadsheet_id=body.spreadsheet_id,
        workflow_id=body.workflow_id,
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot


@router.get("", response_model=list[BotResponse])
def get_bots(
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Bot)
        .filter(models.Bot.tenant_id == current_tenant.id)
        .all()
    )


@router.get("/{bot_id}", response_model=BotResponse)
def get_bot(
    bot_id: int,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    bot = (
        db.query(models.Bot)
        .filter(models.Bot.id == bot_id, models.Bot.tenant_id == current_tenant.id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


class SchedulerConfigPayload(BaseModel):
    nombre_negocio: str
    canal: str
    idioma: str = "Español"
    tono: dict[str, Any] = {}
    tipo_negocio: str = ""
    moneda_principal: str = "USD"
    contacto_pagos_alternativos: str = ""
    telefono_soporte_humano: str = ""
    recursos: list[dict[str, Any]] = []
    extras_disponibles: list[dict[str, Any]] = []
    porcentaje_inicial: int = 0
    politica_modificacion_dias: int = 15
    colchon_limpieza_minutos: int = 0
    calendario_reuniones_id: str = ""
    normas_de_uso_tool: str = ""


def _get_scheduler_agent_config(
    bot_id: int,
    tenant_id: Any,
    db: Session,
) -> models.AgentConfig:
    bot = (
        db.query(models.Bot)
        .filter(models.Bot.id == bot_id, models.Bot.tenant_id == tenant_id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if not bot.workflow_id:
        raise HTTPException(status_code=422, detail="Bot has no workflow")

    wa = (
        db.query(models.WorkflowAgent)
        .join(models.AgentConfig, models.WorkflowAgent.agent_config_id == models.AgentConfig.id)
        .filter(
            models.WorkflowAgent.workflow_id == bot.workflow_id,
            models.AgentConfig.agent_type == "scheduler",
        )
        .first()
    )
    if not wa:
        raise HTTPException(status_code=422, detail="No scheduler agent found in bot workflow")

    agent_config = (
        db.query(models.AgentConfig)
        .filter(models.AgentConfig.id == wa.agent_config_id)
        .first()
    )
    if not agent_config:
        raise HTTPException(status_code=404, detail="AgentConfig not found")
    return agent_config


@router.post("/{bot_id}/scheduler-config", status_code=200)
def set_scheduler_config(
    bot_id: int,
    body: SchedulerConfigPayload,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    agent_config = _get_scheduler_agent_config(bot_id, current_tenant.id, db)
    existing: dict = dict(agent_config.config_json or {})
    existing["business_config"] = body.model_dump()
    agent_config.config_json = existing
    db.commit()
    return {"status": "ok", "business_config": body.model_dump()}


@router.get("/{bot_id}/scheduler-config")
def get_scheduler_config(
    bot_id: int,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    agent_config = _get_scheduler_agent_config(bot_id, current_tenant.id, db)
    cfg: dict = agent_config.config_json or {}
    return {"business_config": cfg.get("business_config")}


@router.get("/{bot_id}/messages", response_model=list[ChatMessageResponse])
def get_messages(
    bot_id: int,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    bot = (
        db.query(models.Bot)
        .filter(models.Bot.id == bot_id, models.Bot.tenant_id == current_tenant.id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    return (
        db.query(models.ChatMessage)
        .filter(models.ChatMessage.bot_id == bot_id)
        .order_by(models.ChatMessage.created_at)
        .all()
    )
