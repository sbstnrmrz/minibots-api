import asyncio
import json
import logging
import tempfile
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app import models
from app.agents.business_analyzer_agent import BusinessAnalyzerAgent
from app.auth import require_api_key
from app.database import SessionLocal, get_db
from llm.usage import get_calls, start_tracking
from app.config import (
    ALLOWED_UPLOAD_SUFFIXES,
    GARAGE_BUCKET,
    MAX_UPLOAD_FILE_BYTES,
    MAX_UPLOAD_FILE_COUNT,
)
from app.services.storage import delete_file, get_client, get_presigned_url
from rag.store import clear_namespace_by_source_type, ingest, init_rag_table

logger = logging.getLogger("agents")

router = APIRouter(prefix="/agents", tags=["agents"])


async def _stream_to_tempfile(file: UploadFile, suffix: str) -> tuple[str, int]:
    """Read an UploadFile in chunks into a temp file, enforcing the size cap.

    Returns (tmp_path, total_bytes). Raises HTTPException(413) if the file
    exceeds MAX_UPLOAD_FILE_BYTES — without ever loading the whole thing
    into memory.
    """
    total = 0
    chunk_size = 1024 * 1024  # 1 MB
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_FILE_BYTES:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"file '{file.filename}' exceeds "
                        f"{MAX_UPLOAD_FILE_BYTES} bytes"
                    ),
                )
            tmp.write(chunk)
        return tmp.name, total


def _ingest_file_job(
    file_id: uuid.UUID,
    tmp_path: str,
    s3_key: str,
    namespace: str,
    content_type: str,
    source_name: str,
) -> None:
    """Background task: upload to Garage, ingest into RAG, update DB row.

    Runs in the same worker after the response is sent. Holds a fresh
    SQLAlchemy session because the request's session is long gone by
    the time this fires.
    """
    db = SessionLocal()
    try:
        try:
            with open(tmp_path, "rb") as f:
                body = f.read()
            get_client().put_object(
                Bucket=GARAGE_BUCKET,
                Key=s3_key,
                Body=body,
                ContentType=content_type,
            )
            ingest(tmp_path, namespace, source_name=source_name, source_type="file")
            row = db.query(models.TenantFile).filter(
                models.TenantFile.id == file_id
            ).first()
            if row:
                row.status = models.TenantFileStatus.ingested
                db.commit()
        except Exception as e:
            logger.exception(
                "background ingest failed for file_id=%s: %s", file_id, e
            )
            row = db.query(models.TenantFile).filter(
                models.TenantFile.id == file_id
            ).first()
            if row:
                row.status = models.TenantFileStatus.failed
                db.commit()
    finally:
        db.close()
        Path(tmp_path).unlink(missing_ok=True)


class Link(BaseModel):
    label: str
    url: str


class FAQ(BaseModel):
    question: str
    answer: str


class SocialMedia(BaseModel):
    instagram: str | None = None
    facebook: str | None = None
    linkedin: str | None = None
    tiktok: str | None = None
    website: str | None = None


class GeneralInfo(BaseModel):
    description: str | None = None
    services: str | None = None
    mission: str | None = None
    vision: str | None = None
    sales_pitch: str | None = None
    faq: list[FAQ] = []
    additional_info: str | None = None
    social_media: SocialMedia | None = None


class ContactInfo(BaseModel):
    name: str | None = None
    phone: str | None = None
    company_name: str | None = None


class SetupPayload(BaseModel):
    general: GeneralInfo | None = None
    contact: ContactInfo | None = None
    links: list[Link] = []
    gcal_calendar_id: str | None = None

    model_config = {"populate_by_name": True, "alias_generator": None}


def _normalize_payload(raw: dict) -> dict:
    """Normalize frontend camelCase / hyphenated keys to snake_case."""
    if isinstance(raw.get("general"), dict):
        g = raw["general"]
        if "sales-pitch" in g:
            g["sales_pitch"] = g.pop("sales-pitch")
        if "additional-info" in g:
            g["additional_info"] = g.pop("additional-info")
        if "socialMedia" in g:
            g["social_media"] = g.pop("socialMedia")
    if isinstance(raw.get("contact"), dict):
        c = raw["contact"]
        if "companyName" in c:
            c["company_name"] = c.pop("companyName")
    return raw


@router.post("/analyze")
async def analyze(
    payload: str = Form(...),
    current_tenant: models.Tenant = Depends(require_api_key),
):
    """Score the setup payload with BusinessAnalyzerAgent. No data is saved.

    Returns an NDJSON stream (application/x-ndjson). Each line is a JSON
    object. Chunk types in emission order:

      {"type": "scored",        "overall_score": int, "categories": {...}, "field_details": {...}}
      {"type": "section",       "key": "<cat>", "data": {...}}  — one per category (5 total)
      {"type": "content_flags", "flags": [...]}
      {"type": "complete",      "language": "...", "business_type": "...",
                                "overall_summary": "...", "chatbot_potential": "...",
                                "next_steps": [...], "faq_coverage": {...}}

    The "scored" chunk arrives in ~1 s (pure Python). Section chunks arrive
    as each parallel LLM call resolves (~10–20 s total instead of ~2 min).

    Frontend: consume with fetch + ReadableStream / getReader(), splitting on
    newlines and JSON.parse()-ing each line.
    """
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=str(e))

    normalized = _normalize_payload(raw)
    tenant_id = current_tenant.id

    async def stream():
        start_tracking()
        agent = BusinessAnalyzerAgent()
        db = SessionLocal()
        try:
            async for chunk in agent.analyze_sections_async(json.dumps(normalized)):
                yield json.dumps(chunk, ensure_ascii=False) + "\n"
        except Exception:
            # Log internally; never stream the raw exception text to the client.
            logger.exception("analyze stream failed for tenant_id=%s", tenant_id)
            yield json.dumps(
                {"type": "error", "message": "analysis failed"}, ensure_ascii=False
            ) + "\n"
        finally:
            # Persist all accumulated LLM usage after the stream completes.
            try:
                for c in get_calls():
                    db.add(models.LLMCall(
                        tenant_id=tenant_id,
                        agent_name=c.agent_name,
                        provider=c.provider,
                        model=c.model,
                        prompt_tokens=c.prompt_tokens,
                        completion_tokens=c.completion_tokens,
                        total_tokens=c.total_tokens,
                        cost_usd=c.cost_usd,
                    ))
                db.commit()
            except Exception:
                logger.exception("Failed to persist LLM usage for tenant_id=%s", tenant_id)
            finally:
                db.close()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@router.post("/setup", status_code=202)
async def setup(
    background_tasks: BackgroundTasks,
    payload: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    if len(files) > MAX_UPLOAD_FILE_COUNT:
        raise HTTPException(
            status_code=413,
            detail=f"too many files (max {MAX_UPLOAD_FILE_COUNT})",
        )
    for f in files:
        suffix = Path(f.filename or "").suffix.lower()
        if suffix and suffix not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"file '{f.filename}' has unsupported extension '{suffix}'. "
                    f"Allowed: {sorted(ALLOWED_UPLOAD_SUFFIXES)}"
                ),
            )
    try:
        raw = json.loads(payload)
        # normalize frontend camelCase / hyphenated keys to snake_case
        if isinstance(raw.get("general"), dict):
            g = raw["general"]
            if "sales-pitch" in g:
                g["sales_pitch"] = g.pop("sales-pitch")
            if "additional-info" in g:
                g["additional_info"] = g.pop("additional-info")
            if "socialMedia" in g:
                g["social_media"] = g.pop("socialMedia")
        if isinstance(raw.get("contact"), dict):
            c = raw["contact"]
            if "companyName" in c:
                c["company_name"] = c.pop("companyName")
        data = SetupPayload.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as e:
        raise HTTPException(status_code=422, detail=str(e))

    logger.info("agents/setup files=%s", [f.filename for f in files])
    logger.debug("agents/setup payload=%s", data.model_dump())

    tenant = current_tenant

    # contact
    if data.contact:
        if data.contact.name:
            tenant.contact_name = data.contact.name
        if data.contact.phone:
            tenant.contact_phone = data.contact.phone
        if data.contact.company_name:
            tenant.name = data.contact.company_name

    if data.gcal_calendar_id is not None:
        tenant.gcal_calendar_id = data.gcal_calendar_id

    # agent_config + links
    links_data = [link.model_dump() for link in data.links]
    if tenant.agent_config_id:
        agent_config = db.query(models.AgentConfig).filter(
            models.AgentConfig.id == tenant.agent_config_id
        ).first()
        agent_config.links = links_data
    else:
        agent_config = models.AgentConfig(
            name=tenant.name,
            agent_type="generic_info",
            links=links_data,
            config_scope="tenant_default",
        )
        db.add(agent_config)
        db.flush()
        tenant.agent_config_id = agent_config.id

    # general info
    if data.general:
        general = db.query(models.AgentGeneralInfo).filter(
            models.AgentGeneralInfo.agent_config_id == agent_config.id
        ).first()
        g = data.general
        info_data = dict(
            description=g.description,
            services=g.services,
            mission=g.mission,
            vision=g.vision,
            sales_pitch=g.sales_pitch,
            faq=[f.model_dump() for f in g.faq] if g.faq else None,
            social_media=g.social_media.model_dump() if g.social_media else None,
            additional_info=g.additional_info,
        )
        if general:
            for k, v in info_data.items():
                setattr(general, k, v)
        else:
            db.add(models.AgentGeneralInfo(agent_config_id=agent_config.id, **info_data))

    # Ensure RAG table + namespace exist; register in rag_sources for
    # consistent namespace resolution via get_namespace().
    namespace = f"agent_{agent_config.id}"
    init_rag_table(namespace)

    existing_source = db.query(models.RagSource).filter(
        models.RagSource.namespace == namespace
    ).first()
    if existing_source:
        existing_source.scope_type = "agent"
        existing_source.scope_id = agent_config.id
        existing_source.tenant_id = current_tenant.id
    else:
        db.add(models.RagSource(
            namespace=namespace,
            scope_type="agent",
            scope_id=agent_config.id,
            tenant_id=current_tenant.id,
        ))

    # Always clear + re-ingest links (fast; reflects current link list).
    clear_namespace_by_source_type(namespace, "link")
    for link in data.links:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".md", mode="w") as tmp:
            tmp.write(f"# {link.label}\n\n{link.url}\n")
            tmp_path = tmp.name
        try:
            ingest(tmp_path, namespace, source_name=link.label, source_type="link")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # Only delete + replace uploaded files when new files are provided.
    # Calling setup to update contact info or links alone no longer wipes
    # previously uploaded documents.
    if files:
        existing_files = db.query(models.TenantFile).filter(
            models.TenantFile.tenant_id == current_tenant.id
        ).all()
        for existing in existing_files:
            ext = Path(existing.filename).suffix if existing.filename else ""
            delete_file(f"{current_tenant.id}/agent_docs/{existing.id}{ext}")
            db.delete(existing)
        clear_namespace_by_source_type(namespace, "file")

    # Stream each upload to a temp file (size-capped), record a pending
    # TenantFile row, and hand the heavy work (S3 put + MarkItDown +
    # embed) to a BackgroundTask so the response returns promptly.
    pending = []
    for file in files:
        ext = Path(file.filename).suffix if file.filename else ""
        tmp_path, _bytes = await _stream_to_tempfile(file, suffix=ext)
        file_id = uuid.uuid4()
        s3_key = f"{current_tenant.id}/agent_docs/{file_id}{ext}"
        db.add(models.TenantFile(
            id=file_id,
            tenant_id=current_tenant.id,
            agent_config_id=agent_config.id,
            filename=file.filename,
            content_type=file.content_type or "application/octet-stream",
            status=models.TenantFileStatus.pending,
        ))
        pending.append({
            "file_id": file_id,
            "tmp_path": tmp_path,
            "s3_key": s3_key,
            "filename": file.filename,
            "content_type": file.content_type or "application/octet-stream",
        })

    db.commit()

    for p in pending:
        background_tasks.add_task(
            _ingest_file_job,
            file_id=p["file_id"],
            tmp_path=p["tmp_path"],
            s3_key=p["s3_key"],
            namespace=namespace,
            content_type=p["content_type"],
            source_name=p["filename"],
        )

    return {
        "links": links_data,
        "files": [
            {
                "file_id": str(p["file_id"]),
                "filename": p["filename"],
                "status": "pending",
            }
            for p in pending
        ],
    }


@router.get("/me")
def get_me(
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """Return the current tenant's saved setup: contact, agent config,
    general info, links and uploaded files (with ingestion status)."""
    tenant = current_tenant

    agent_config = None
    general = None
    links: list = []
    if tenant.agent_config_id:
        agent_config = (
            db.query(models.AgentConfig)
            .filter(models.AgentConfig.id == tenant.agent_config_id)
            .first()
        )
        if agent_config:
            links = agent_config.links or []
            general = (
                db.query(models.AgentGeneralInfo)
                .filter(models.AgentGeneralInfo.agent_config_id == agent_config.id)
                .first()
            )

    file_rows = (
        db.query(models.TenantFile)
        .filter(models.TenantFile.tenant_id == tenant.id)
        .all()
    )

    return {
        "tenant": {
            "id": str(tenant.id),
            "name": tenant.name,
            "contact_name": tenant.contact_name,
            "contact_phone": tenant.contact_phone,
        },
        "agent_config": (
            {
                "id": agent_config.id,
                "name": agent_config.name,
                "agent_type": agent_config.agent_type,
                "system_prompt": agent_config.system_prompt,
            } if agent_config else None
        ),
        "general": (
            {
                "description": general.description,
                "services": general.services,
                "mission": general.mission,
                "vision": general.vision,
                "sales_pitch": general.sales_pitch,
                "faq": general.faq or [],
                "social_media": general.social_media or {},
                "additional_info": general.additional_info,
            } if general else None
        ),
        "links": links,
        "files": [
            {
                "file_id": str(row.id),
                "filename": row.filename,
                "content_type": row.content_type,
                "status": row.status.value,
            }
            for row in file_rows
        ],
    }


@router.get("/files/{file_id}")
def get_file_status(
    file_id: uuid.UUID,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    row = (
        db.query(models.TenantFile)
        .filter(
            models.TenantFile.id == file_id,
            models.TenantFile.tenant_id == current_tenant.id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="file not found")
    presigned: str | None = None
    if row.status == models.TenantFileStatus.ingested:
        ext = Path(row.filename or "").suffix
        presigned = get_presigned_url(f"{row.tenant_id}/agent_docs/{row.id}{ext}")
    return {
        "file_id": str(row.id),
        "filename": row.filename,
        "status": row.status.value,
        "url": presigned,
    }
