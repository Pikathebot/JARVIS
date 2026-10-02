import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from app.config import settings
from app.database import get_session
from app.database.models import Artifact, ArtifactVersion, Attachment, Project
from app.services.artifact_service import ArtifactService

logger = logging.getLogger("jarvis.routers.artifacts")

router = APIRouter(prefix="/api", tags=["artifacts", "attachments"])

ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml",
    ".cpp", ".h", ".hpp", ".cs", ".ini", ".csv", ".html", ".css", ".svg",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"
}


def get_artifact_service(db: Session = Depends(get_session)) -> ArtifactService:
    return ArtifactService(db_engine=db.get_bind())


# ==========================================
# Pydantic Schemas
# ==========================================

class AttachmentResponse(BaseModel):
    id: str
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    filename: str
    path: str
    size_bytes: int
    content_type: Optional[str] = None
    created_at: datetime


class ArtifactVersionResponse(BaseModel):
    id: str
    artifact_id: str
    version: int
    content: str
    summary: Optional[str] = None
    created_by: str = "agent"
    created_at: datetime


class ArtifactResponse(BaseModel):
    id: str
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    name: str
    type: str
    content: str
    version: int
    language: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class CreateArtifactRequest(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = Field(default="code", description="code/markdown/html/json/csv/python/svg/document/other")
    content: str
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    language: Optional[str] = None
    summary: Optional[str] = None
    created_by: str = "agent"


class CreateArtifactVersionRequest(BaseModel):
    content: str
    summary: Optional[str] = None
    created_by: str = "agent"


class UpdateArtifactRequest(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    content: Optional[str] = None
    language: Optional[str] = None
    summary: Optional[str] = None
    created_by: str = "agent"
    create_new_version: bool = Field(default=True, description="Whether to snapshot new content as an incremented version")


class ProjectFileItem(BaseModel):
    name: str
    path: str
    size_bytes: int
    is_dir: bool
    updated_at: Optional[float] = None


# ==========================================
# Helpers
# ==========================================

def sanitize_filename(filename: str) -> str:
    """Strip path traversal characters and dangerous special characters."""
    base = os.path.basename(filename).strip()
    sanitized = re.sub(r"[^\w\.\-]", "_", base)
    if not sanitized or sanitized.startswith("."):
        sanitized = f"upload_{uuid.uuid4().hex[:8]}{sanitized}"
    return sanitized


def get_target_files_dir(project_id: Optional[str], db: Session) -> Path:
    """Resolve and ensure the target upload directory exists."""
    workspace_base = Path(settings.workspace_path).resolve()

    if project_id:
        from app.workspaces import attachments_dir

        proj = db.get(Project, project_id)
        target_dir = attachments_dir(proj.workspace_path if proj else None, project_id)
    else:
        target_dir = workspace_base / "files"

    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


# ==========================================
# Attachments (User-Uploaded Files) Endpoints
# ==========================================

@router.post("/upload", response_model=AttachmentResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
    project_id: Optional[str] = Form(None),
    chat_mode: Optional[str] = Form(None),
    ephemeral: bool = Form(False),
    db: Session = Depends(get_session),
) -> AttachmentResponse:
    # Ephemeral: the file lives in a temp directory owned by the in-memory session and deleted
    # with it; no session or attachment row is written and nothing is indexed. Freeform: stored
    # normally, but not indexed into any project (the turn has none).
    if ephemeral and not session_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An ephemeral upload needs its session_id.")
    freeform = (chat_mode or "").upper() == "FREEFORM"
    if freeform:
        project_id = None
    raw_filename = file.filename or "uploaded_file.txt"
    ext = os.path.splitext(raw_filename)[1].lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File extension '{ext}' is not permitted. Allowed extensions: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    clean_name = sanitize_filename(raw_filename)
    if ephemeral:
        import tempfile
        from app.memory.ephemeral import ephemeral_registry

        try:
            target_dir = ephemeral_registry.upload_dir(session_id, Path(tempfile.gettempdir()) / "jarvis-ephemeral")
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        project_id = None
    else:
        target_dir = get_target_files_dir(project_id, db)
    dest_path = (target_dir / clean_name).resolve()

    try:
        dest_path.relative_to(target_dir)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file path detected.",
        )

    if dest_path.exists():
        stem, extension = os.path.splitext(clean_name)
        clean_name = f"{stem}_{uuid.uuid4().hex[:6]}{extension}"
        dest_path = target_dir / clean_name

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    content = await file.read()
    size_bytes = len(content)

    if size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size ({size_bytes / (1024*1024):.2f}MB) exceeds maximum limit of {settings.max_upload_size_mb}MB.",
        )

    with open(dest_path, "wb") as f:
        f.write(content)

    if ephemeral:
        logger.info("Uploaded ephemeral attachment '%s' (%d bytes); not persisted", clean_name, size_bytes)
        return AttachmentResponse(
            id=str(uuid.uuid4()),
            session_id=session_id,
            project_id=None,
            filename=clean_name,
            path=str(dest_path),
            size_bytes=size_bytes,
            content_type=file.content_type,
            created_at=datetime.utcnow(),
        )

    # A client uploads before it sends the first message of a new session, so the session row
    # the attachment points at may not exist yet; without this the insert fails its foreign key
    # and the image silently never reaches the turn.
    if session_id:
        from app.database.models import Session as ChatSession
        import time as _time

        if db.get(ChatSession, session_id) is None:
            now = _time.time()
            db.add(ChatSession(
                session_id=session_id, project_id=project_id, created_at=now, updated_at=now,
                chat_mode="FREEFORM" if freeform else "WORKSPACE",
            ))
            db.flush()

    attachment = Attachment(
        id=str(uuid.uuid4()),
        session_id=session_id,
        project_id=project_id,
        filename=clean_name,
        path=str(dest_path),
        size_bytes=size_bytes,
        content_type=file.content_type,
        created_at=datetime.utcnow(),
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)

    # Automatic RAG indexing for uploaded file
    try:
        from app.rag.indexer import WorkspaceIndexer
        target_pid = project_id
        if not target_pid and not freeform:
            from app.database.models import Project
            act = db.exec(select(Project).where(Project.is_active == True)).first()
            if act:
                target_pid = act.id
        if target_pid:
            WorkspaceIndexer().index_file(dest_path, project_id=target_pid)
    except Exception as idx_err:
        logger.debug("Automatic indexing after upload skipped/failed: %s", idx_err)

    logger.info("Uploaded attachment '%s' (%d bytes) to %s", clean_name, size_bytes, dest_path)
    return AttachmentResponse(
        id=attachment.id,
        session_id=attachment.session_id,
        project_id=attachment.project_id,
        filename=attachment.filename,
        path=attachment.path,
        size_bytes=attachment.size_bytes,
        content_type=attachment.content_type,
        created_at=attachment.created_at,
    )


@router.get("/attachments", response_model=list[AttachmentResponse])
def list_attachments(
    session_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_session),
) -> list[AttachmentResponse]:
    statement = select(Attachment)
    if session_id:
        statement = statement.where(Attachment.session_id == session_id)
    if project_id:
        statement = statement.where(Attachment.project_id == project_id)
    statement = statement.order_by(col(Attachment.created_at).desc())
    attachments = db.exec(statement).all()
    return [
        AttachmentResponse(
            id=a.id,
            session_id=a.session_id,
            project_id=a.project_id,
            filename=a.filename,
            path=a.path,
            size_bytes=a.size_bytes,
            content_type=a.content_type,
            created_at=a.created_at,
        )
        for a in attachments
    ]


@router.delete("/attachments/{attachment_id}")
def delete_attachment(
    attachment_id: str,
    delete_file: bool = Query(default=True),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    attachment = db.get(Attachment, attachment_id)
    if not attachment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Attachment '{attachment_id}' not found.",
        )

    file_path = attachment.path
    db.delete(attachment)
    db.commit()

    if delete_file and file_path:
        p = Path(file_path)
        if p.exists() and p.is_file():
            try:
                p.unlink()
            except Exception as e:
                logger.warning("Failed to delete attachment file %s: %s", file_path, e)

    return {"deleted": True, "attachment_id": attachment_id}


# ==========================================
# Artifacts (AI Generated Outputs) Endpoints (Build Plan §15)
# ==========================================

@router.get("/artifacts", response_model=list[ArtifactResponse])
def list_artifacts(
    conversation_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    service: ArtifactService = Depends(get_artifact_service),
) -> list[ArtifactResponse]:
    """List artifacts with optional filtering by conversation_id, project_id, or type."""
    artifacts = service.list_artifacts(
        conversation_id=conversation_id,
        session_id=session_id,
        project_id=project_id,
        type=type
    )
    return [
        ArtifactResponse(
            id=a.id,
            project_id=a.project_id,
            session_id=a.session_id,
            conversation_id=a.conversation_id or a.session_id,
            name=a.name,
            type=a.type,
            content=a.content,
            version=a.version,
            language=a.language,
            created_at=a.created_at,
            updated_at=a.updated_at,
        )
        for a in artifacts
    ]


@router.post("/artifacts", response_model=ArtifactResponse, status_code=status.HTTP_201_CREATED)
def create_artifact(
    req: CreateArtifactRequest,
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactResponse:
    """Create a new artifact and initialize Version 1 in ArtifactVersion."""
    conv_id = req.conversation_id or req.session_id
    artifact = service.create_artifact(
        name=req.name,
        type=req.type,
        content=req.content,
        conversation_id=conv_id,
        project_id=req.project_id,
        language=req.language,
        summary=req.summary,
        created_by=req.created_by,
    )
    return ArtifactResponse(
        id=artifact.id,
        project_id=artifact.project_id,
        session_id=artifact.session_id,
        conversation_id=artifact.conversation_id or artifact.session_id,
        name=artifact.name,
        type=artifact.type,
        content=artifact.content,
        version=artifact.version,
        language=artifact.language,
        created_at=artifact.created_at,
        updated_at=artifact.updated_at,
    )


@router.get("/artifacts/{artifact_id}", response_model=ArtifactResponse)
def get_artifact(
    artifact_id: str,
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactResponse:
    """Retrieve detailed artifact metadata and current active content."""
    artifact = service.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact '{artifact_id}' not found.",
        )
    return ArtifactResponse(
        id=artifact.id,
        project_id=artifact.project_id,
        session_id=artifact.session_id,
        conversation_id=artifact.conversation_id or artifact.session_id,
        name=artifact.name,
        type=artifact.type,
        content=artifact.content,
        version=artifact.version,
        language=artifact.language,
        created_at=artifact.created_at,
        updated_at=artifact.updated_at,
    )


@router.patch("/artifacts/{artifact_id}", response_model=ArtifactResponse)
def patch_artifact(
    artifact_id: str,
    req: UpdateArtifactRequest,
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactResponse:
    """Update artifact content, auto-incrementing version and snapshotting previous content."""
    try:
        artifact = service.update_artifact(
            artifact_id=artifact_id,
            content=req.content,
            name=req.name,
            type=req.type,
            language=req.language,
            summary=req.summary,
            created_by=req.created_by,
        )
        return ArtifactResponse(
            id=artifact.id,
            project_id=artifact.project_id,
            session_id=artifact.session_id,
            conversation_id=artifact.conversation_id or artifact.session_id,
            name=artifact.name,
            type=artifact.type,
            content=artifact.content,
            version=artifact.version,
            language=artifact.language,
            created_at=artifact.created_at,
            updated_at=artifact.updated_at,
        )
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact '{artifact_id}' not found.",
        )


@router.put("/artifacts/{artifact_id}", response_model=ArtifactResponse)
def update_artifact(
    artifact_id: str,
    req: UpdateArtifactRequest,
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactResponse:
    """Backward compatible PUT endpoint alias for artifact updates."""
    return patch_artifact(artifact_id, req, service)


@router.delete("/artifacts/{artifact_id}")
def delete_artifact(
    artifact_id: str,
    service: ArtifactService = Depends(get_artifact_service),
) -> dict[str, Any]:
    """Delete an artifact and cascade all associated version history."""
    deleted = service.delete_artifact(artifact_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact '{artifact_id}' not found.",
        )
    return {"deleted": True, "artifact_id": artifact_id}


@router.get("/artifacts/{artifact_id}/versions", response_model=list[ArtifactVersionResponse])
def list_artifact_versions(
    artifact_id: str,
    service: ArtifactService = Depends(get_artifact_service),
) -> list[ArtifactVersionResponse]:
    """Retrieve full version history for an artifact ordered by version number."""
    try:
        versions = service.list_versions(artifact_id)
        return [
            ArtifactVersionResponse(
                id=v.id,
                artifact_id=v.artifact_id,
                version=v.version,
                content=v.content,
                summary=v.summary,
                created_by=v.created_by,
                created_at=v.created_at,
            )
            for v in versions
        ]
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact '{artifact_id}' not found.",
        )


@router.get("/artifacts/{artifact_id}/versions/{version}", response_model=ArtifactVersionResponse)
def get_artifact_version(
    artifact_id: str,
    version: int,
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactVersionResponse:
    """Retrieve specific version snapshot content for an artifact."""
    ver = service.get_version(artifact_id, version)
    if not ver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {version} not found for artifact '{artifact_id}'.",
        )
    return ArtifactVersionResponse(
        id=ver.id,
        artifact_id=ver.artifact_id,
        version=ver.version,
        content=ver.content,
        summary=ver.summary,
        created_by=ver.created_by,
        created_at=ver.created_at,
    )


@router.post("/artifacts/{artifact_id}/restore/{version}", response_model=ArtifactResponse)
def restore_artifact_version(
    artifact_id: str,
    version: int,
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactResponse:
    """Restore artifact content from a historical version."""
    try:
        restored = service.restore_version(artifact_id, version, created_by="user")
        return ArtifactResponse(
            id=restored.id,
            project_id=restored.project_id,
            session_id=restored.session_id,
            conversation_id=restored.conversation_id or restored.session_id,
            name=restored.name,
            type=restored.type,
            content=restored.content,
            version=restored.version,
            language=restored.language,
            created_at=restored.created_at,
            updated_at=restored.updated_at,
        )
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Artifact '{artifact_id}' not found.")
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.post("/artifacts/{artifact_id}/versions", response_model=ArtifactVersionResponse, status_code=status.HTTP_201_CREATED)
def create_artifact_version(
    artifact_id: str,
    req: CreateArtifactVersionRequest,
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactVersionResponse:
    """Create a new version for an artifact, updating active content."""
    try:
        updated = service.update_artifact(
            artifact_id=artifact_id,
            content=req.content,
            summary=req.summary,
            created_by=req.created_by,
        )
        ver = service.get_version(artifact_id, updated.version)
        if not ver:
            raise HTTPException(status_code=500, detail="Failed retrieving created version.")
        return ArtifactVersionResponse(
            id=ver.id,
            artifact_id=ver.artifact_id,
            version=ver.version,
            content=ver.content,
            summary=ver.summary,
            created_by=ver.created_by,
            created_at=ver.created_at,
        )
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Artifact '{artifact_id}' not found.")


# ==========================================
# Project Files Explorer (Section 19 / Amendment 4)
# ==========================================

@router.get("/projects/{project_id}/files", response_model=list[ProjectFileItem])
def list_project_files(
    project_id: str,
    db: Session = Depends(get_session),
) -> list[ProjectFileItem]:
    target_dir = get_target_files_dir(project_id, db)
    items: list[ProjectFileItem] = []

    if not target_dir.exists() or not target_dir.is_dir():
        return items

    try:
        for entry in os.scandir(target_dir):
            stat = entry.stat()
            items.append(
                ProjectFileItem(
                    name=entry.name,
                    path=entry.path,
                    size_bytes=stat.st_size if entry.is_file() else 0,
                    is_dir=entry.is_dir(),
                    updated_at=stat.st_mtime,
                )
            )
    except Exception as e:
        logger.warning("Error scanning project files directory %s: %s", target_dir, e)

    items.sort(key=lambda x: (not x.is_dir, x.name.lower()))
    return items
