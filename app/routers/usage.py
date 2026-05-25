"""Usage reporting endpoint.

Returns aggregated token and cost data for the current tenant,
optionally filtered by bot, chat, or date range.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.auth import require_api_key
from app.database import get_db

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("")
def get_usage(
    bot_id: int | None = Query(None),
    chat_id: str | None = Query(None),
    start: datetime | None = Query(None, description="ISO-8601 start datetime (inclusive)"),
    end: datetime | None = Query(None, description="ISO-8601 end datetime (inclusive)"),
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """Return token/cost totals for the current tenant.

    Scoped to the authenticated tenant — no cross-tenant leakage.
    All filters are optional and combinable.
    """
    q = db.query(models.LLMCall).filter(
        models.LLMCall.tenant_id == current_tenant.id
    )
    if bot_id is not None:
        q = q.filter(models.LLMCall.bot_id == bot_id)
    if chat_id is not None:
        q = q.filter(models.LLMCall.chat_id == chat_id)
    if start is not None:
        q = q.filter(models.LLMCall.created_at >= start)
    if end is not None:
        q = q.filter(models.LLMCall.created_at <= end)

    rows = q.all()

    total_calls       = len(rows)
    total_tokens      = sum(r.total_tokens for r in rows)
    total_prompt      = sum(r.prompt_tokens for r in rows)
    total_completion  = sum(r.completion_tokens for r in rows)
    total_cost        = sum(float(r.cost_usd or 0) for r in rows)

    # --- by_model breakdown ---
    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r.provider, r.model)
        if key not in by_model:
            by_model[key] = {"provider": r.provider, "model": r.model,
                             "calls": 0, "tokens": 0, "cost_usd": 0.0}
        by_model[key]["calls"]    += 1
        by_model[key]["tokens"]   += r.total_tokens
        by_model[key]["cost_usd"] += float(r.cost_usd or 0)

    # --- by_bot breakdown ---
    by_bot: dict[int | None, dict[str, Any]] = {}
    for r in rows:
        k = r.bot_id
        if k not in by_bot:
            by_bot[k] = {"bot_id": k, "calls": 0, "tokens": 0, "cost_usd": 0.0}
        by_bot[k]["calls"]    += 1
        by_bot[k]["tokens"]   += r.total_tokens
        by_bot[k]["cost_usd"] += float(r.cost_usd or 0)

    # --- by_chat breakdown (only when not filtering by a specific chat) ---
    by_chat: list[dict[str, Any]] = []
    if chat_id is None:
        by_chat_map: dict[str | None, dict[str, Any]] = {}
        for r in rows:
            k = r.chat_id
            if k not in by_chat_map:
                by_chat_map[k] = {"chat_id": k, "calls": 0, "tokens": 0, "cost_usd": 0.0}
            by_chat_map[k]["calls"]    += 1
            by_chat_map[k]["tokens"]   += r.total_tokens
            by_chat_map[k]["cost_usd"] += float(r.cost_usd or 0)
        by_chat = list(by_chat_map.values())

    return {
        "total_calls":       total_calls,
        "total_tokens":      total_tokens,
        "prompt_tokens":     total_prompt,
        "completion_tokens": total_completion,
        "total_cost_usd":    round(total_cost, 6),
        "by_model":          list(by_model.values()),
        "by_bot":            list(by_bot.values()),
        "by_chat":           by_chat,
    }
