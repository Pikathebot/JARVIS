import json
import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from app.config import settings
from app.workspaces import jarvis_project_dir, validate_workspace_folder
from app.database import SessionLocal, get_session
from app.database.models import Project, Session as DBSession

logger = logging.getLogger("jarvis.routers.projects")

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ==========================================
# Pydantic Schemas
# ==========================================

class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Project name")
    description: Optional[str] = Field(default=None, description="Project overview or description")
    instructions: Optional[str] = Field(default=None, description="Custom system instructions for the project")
    workspace_path: Optional[str] = Field(default=None, description="Custom on-disk workspace directory path")
    local_folders: list[str] = Field(default_factory=list, description="List of local folder paths attached to project")


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None
    instructions: Optional[str] = None
    workspace_path: Optional[str] = None
    local_folders: Optional[list[str]] = None
    is_active: Optional[bool] = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    instructions: Optional[str] = None
    workspace_path: Optional[str] = None
    local_folders: list[str] = Field(default_factory=list)
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ==========================================
# Helpers
# ==========================================

def init_project_filesystem(project_id: str) -> Path:
    """
    Initializes Section 7 project directory hierarchy on local disk:
    workspace/
    └── projects/
        └── {project-id}/
            ├── files/
            ├── knowledge/
            ├── artifacts/
            ├── memory/
            └── indexes/
    """
    workspace_base = Path(settings.workspace_path)
    project_dir = workspace_base / "projects" / project_id
    subdirs = ["files", "knowledge", "artifacts", "memory", "indexes"]

    for sub in subdirs:
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    logger.info("Initialized project filesystem at %s", project_dir)
    return project_dir


def format_project_response(project: Project) -> ProjectResponse:
    folders: list[str] = []
    if project.local_folders_json:
        try:
            parsed = json.loads(project.local_folders_json)
            if isinstance(parsed, list):
                folders = [str(f) for f in parsed]
        except Exception:
            folders = []

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        instructions=project.instructions,
        workspace_path=project.workspace_path,
        local_folders=folders,
        is_active=bool(project.is_active),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


# ==========================================
# Endpoints
# ==========================================

@router.get("", response_model=list[ProjectResponse])
def list_projects(db: Session = Depends(get_session)) -> list[ProjectResponse]:
    """List all projects ordered by updated_at descending."""
    statement = select(Project).order_by(col(Project.updated_at).desc())
    projects = db.exec(statement).all()
    return [format_project_response(p) for p in projects]


@router.get("/active/current", response_model=Optional[ProjectResponse])
def get_current_active_project(db: Session = Depends(get_session)) -> Optional[ProjectResponse]:
    """Retrieve the currently active project if one is marked active."""
    statement = select(Project).where(Project.is_active == True).limit(1)  # noqa: E712
    project = db.exec(statement).first()
    if not project:
        return None
    return format_project_response(project)


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    req: CreateProjectRequest,
    db: Session = Depends(get_session),
) -> ProjectResponse:
    """
    Create a new Project entity, initialize its on-disk filesystem workspace,
    and persist metadata to the database.
    """
    # The workspace is the folder the user picked (an existing directory, used as is), else a
    # new folder of Jarvis's own. Either way Jarvis's bookkeeping lives in its project dir.
    final_workspace_path: Optional[str] = None
    if req.workspace_path and req.workspace_path.strip():
        try:
            final_workspace_path = str(validate_workspace_folder(req.workspace_path))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    project_id = str(uuid.uuid4())
    project_dir = init_project_filesystem(project_id)
    if final_workspace_path is None:
        final_workspace_path = str(project_dir)

    # Check if there are no existing projects; if none exist, default this to active
    existing_count = db.exec(select(Project)).all()
    make_active = len(existing_count) == 0

    now = datetime.utcnow()
    project = Project(
        id=project_id,
        name=req.name.strip(),
        description=req.description,
        instructions=req.instructions,
        workspace_path=final_workspace_path,
        local_folders_json=json.dumps(req.local_folders),
        is_active=make_active,
        created_at=now,
        updated_at=now,
    )

    db.add(project)
    db.commit()
    db.refresh(project)
    logger.info("Created project '%s' (%s)", project.name, project.id)
    return format_project_response(project)


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, db: Session = Depends(get_session)) -> ProjectResponse:
    """Retrieve detailed metadata for a single project."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_id}' not found.",
        )
    return format_project_response(project)


@router.put("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: str,
    req: UpdateProjectRequest,
    db: Session = Depends(get_session),
) -> ProjectResponse:
    """Update project metadata, attached folders, or active state."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_id}' not found.",
        )

    if req.name is not None:
        project.name = req.name.strip()
    if req.description is not None:
        project.description = req.description
    if req.instructions is not None:
        project.instructions = req.instructions
    # Local folders are read-only knowledge and never move the workspace (they used to: the first
    # one silently became the workspace on any update that didn't name a location).
    if req.workspace_path is not None:
        if req.workspace_path.strip():
            try:
                project.workspace_path = str(validate_workspace_folder(req.workspace_path))
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        else:
            # Back to Jarvis's own folder for this workspace.
            project.workspace_path = str(init_project_filesystem(project_id))
    if req.local_folders is not None:
        project.local_folders_json = json.dumps(req.local_folders)

    if req.is_active is True:
        # Deactivate all other projects
        all_projects = db.exec(select(Project)).all()
        for p in all_projects:
            if p.id != project_id and p.is_active:
                p.is_active = False
                p.updated_at = datetime.utcnow()
                db.add(p)
        project.is_active = True
    elif req.is_active is False:
        project.is_active = False

    project.updated_at = datetime.utcnow()
    db.add(project)
    db.commit()
    db.refresh(project)
    return format_project_response(project)


@router.delete("/{project_id}")
def delete_project(
    project_id: str,
    clean_files: bool = Query(default=True, description="Whether to remove on-disk project directory"),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """Delete a project and optionally clean up its workspace directory from disk."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_id}' not found.",
        )

    was_active = project.is_active
    # Only ever Jarvis's own directory: a workspace the user pointed at their own folder keeps
    # that folder (and its .jarvis/attachments) when the workspace is deleted.
    workspace_dir = str(jarvis_project_dir(project_id))

    # Delete project record from DB
    db.delete(project)
    db.commit()

    # Clean on-disk workspace directory if requested
    if clean_files and workspace_dir:
        try:
            ws_path = Path(workspace_dir)
            if ws_path.exists() and ws_path.is_dir():
                shutil.rmtree(ws_path, ignore_errors=True)
                logger.info("Cleaned project workspace directory: %s", ws_path)
        except Exception as e:
            logger.warning("Failed to remove project workspace directory %s: %s", workspace_dir, e)

    # If the deleted project was active, activate the next available project if one exists
    if was_active:
        next_project = db.exec(select(Project).order_by(col(Project.updated_at).desc())).first()
        if next_project:
            next_project.is_active = True
            next_project.updated_at = datetime.utcnow()
            db.add(next_project)
            db.commit()

    return {"deleted": True, "project_id": project_id}


@router.post("/{project_id}/activate", response_model=ProjectResponse)
def activate_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_session),
) -> ProjectResponse:
    """Set the specified project as the single active project in the workspace."""
    target_project = db.get(Project, project_id)
    if not target_project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_id}' not found.",
        )

    # Set all other projects to inactive
    all_projects = db.exec(select(Project)).all()
    now = datetime.utcnow()
    for p in all_projects:
        if p.id != project_id and p.is_active:
            p.is_active = False
            p.updated_at = now
            db.add(p)

    target_project.is_active = True
    target_project.updated_at = now
    db.add(target_project)
    db.commit()
    db.refresh(target_project)

    # Trigger background indexing for the activated project workspace
    def _bg_index(pid: str):
        try:
            from app.rag.indexer import WorkspaceIndexer
            WorkspaceIndexer().index_project(pid)
        except Exception as e:
            logger.warning("Background indexing on activation failed for %s: %s", pid, e)

    background_tasks.add_task(_bg_index, project_id)

    logger.info("Activated project '%s' (%s)", target_project.name, target_project.id)
    return format_project_response(target_project)


@router.post("/{project_id}/index")
def index_project_endpoint(
    project_id: str,
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """
    Manually trigger or re-index a project workspace, embedding all source and knowledge files.
    Returns indexing statistics.
    """
    target_project = db.get(Project, project_id)
    if not target_project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_id}' not found.",
        )

    from app.rag.indexer import WorkspaceIndexer
    indexer = WorkspaceIndexer()
    stats = indexer.index_project(project_id)
    return stats
