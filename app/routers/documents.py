import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import models
from app.auth import require_api_key
from app.database import get_db
from rag.store import init_rag_table, ingest

router = APIRouter(tags=["documents"])


def _ingest_and_register(
    file: UploadFile,
    tmp_path: str,
    namespace: str,
    scope_type: str,
    scope_id: int,
    tenant_id,
    db: Session,
) -> int:
    init_rag_table(namespace)
    chunks = ingest(tmp_path, namespace, source_name=file.filename)

    existing = db.query(models.RagSource).filter(
        models.RagSource.namespace == namespace
    ).first()
    if existing:
        # Keep the owner in sync (backfills rows created before tenant_id existed).
        if existing.tenant_id is None:
            existing.tenant_id = tenant_id
            db.commit()
    else:
        db.add(models.RagSource(
            namespace=namespace,
            scope_type=scope_type,
            scope_id=scope_id,
            tenant_id=tenant_id,
        ))
        db.commit()

    return chunks


@router.post("/bots/{bot_id}/documents")
async def upload_bot_document(
    bot_id: int,
    file: UploadFile,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    bot = (
        db.query(models.Bot)
        .filter(models.Bot.id == bot_id, models.Bot.tenant_id == current_tenant.id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id} not found.")

    suffix = Path(file.filename).suffix if file.filename else ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        chunks = _ingest_and_register(
            file, tmp_path,
            namespace=f"bot_{bot_id}",
            scope_type="bot",
            scope_id=bot_id,
            tenant_id=current_tenant.id,
            db=db,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return {"scope": "bot", "scope_id": bot_id, "filename": file.filename, "chunks_ingested": chunks}


@router.post("/workflows/{workflow_id}/documents")
async def upload_workflow_document(
    workflow_id: int,
    file: UploadFile,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    workflow = (
        db.query(models.Workflow)
        .filter(
            models.Workflow.id == workflow_id,
            models.Workflow.tenant_id == current_tenant.id,
        )
        .first()
    )
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found.")

    suffix = Path(file.filename).suffix if file.filename else ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        chunks = _ingest_and_register(
            file, tmp_path,
            namespace=f"workflow_{workflow_id}",
            scope_type="workflow",
            scope_id=workflow_id,
            tenant_id=current_tenant.id,
            db=db,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return {"scope": "workflow", "scope_id": workflow_id, "filename": file.filename, "chunks_ingested": chunks}


@router.post("/agent-configs/{agent_config_id}/documents")
async def upload_agent_document(
    agent_config_id: int,
    file: UploadFile,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    agent_config = db.query(models.AgentConfig).filter(
        models.AgentConfig.id == agent_config_id
    ).first()
    if not agent_config:
        raise HTTPException(status_code=404, detail=f"AgentConfig {agent_config_id} not found.")

    # Ownership: an AgentConfig has no tenant_id column, so verify it is
    # reachable from the current tenant — either as the tenant's default
    # config, or as a step in one of the tenant's workflows. Reject (404,
    # not 403, to avoid confirming existence) anything else.
    owned = agent_config.id == current_tenant.agent_config_id
    if not owned:
        owned = (
            db.query(models.WorkflowAgent)
            .join(models.Workflow, models.WorkflowAgent.workflow_id == models.Workflow.id)
            .filter(
                models.WorkflowAgent.agent_config_id == agent_config_id,
                models.Workflow.tenant_id == current_tenant.id,
            )
            .first()
            is not None
        )
    if not owned:
        raise HTTPException(status_code=404, detail=f"AgentConfig {agent_config_id} not found.")

    suffix = Path(file.filename).suffix if file.filename else ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        chunks = _ingest_and_register(
            file, tmp_path,
            namespace=f"agent_{agent_config_id}",
            scope_type="agent",
            scope_id=agent_config_id,
            tenant_id=current_tenant.id,
            db=db,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return {"scope": "agent", "scope_id": agent_config_id, "filename": file.filename, "chunks_ingested": chunks}
