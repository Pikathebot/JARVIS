"""
SQLModel table definitions for Jarvis memory and extended schema.
Defines Project, Artifact, Memory, Document, DocumentChunk, ToolCall, AgentRun,
and maps existing Session, Message, CompactionEvent, ToolCallAudit, and ReliabilityEvent models.
"""
from datetime import datetime
from typing import Optional
import uuid
from sqlmodel import Field, SQLModel


# ==========================================
# Existing Core & Audit Models
# ==========================================

class Session(SQLModel, table=True):
    __tablename__ = "sessions"

    session_id: str = Field(primary_key=True)
    project_id: Optional[str] = Field(default=None, foreign_key="projects.id", index=True)
    title: Optional[str] = None
    chat_mode: Optional[str] = Field(default="WORKSPACE")
    created_at: Optional[float] = None
    updated_at: Optional[float] = None



class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="sessions.session_id", index=True)
    role: str
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    name: Optional[str] = None
    tool_calls_json: Optional[str] = None
    token_estimate: int = Field(default=0)
    is_summary: int = Field(default=0)
    created_at: Optional[float] = None


class CompactionEvent(SQLModel, table=True):
    __tablename__ = "compaction_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="sessions.session_id", index=True)
    strategy: str
    tokens_before: int
    tokens_after: int
    details: Optional[str] = None
    created_at: Optional[float] = None


class ToolCallAudit(SQLModel, table=True):
    __tablename__ = "tool_call_audit"

    call_id: str = Field(primary_key=True)
    turn_id: str = Field(index=True)
    timestamp: float
    tool_name: str
    args_json: str
    model_tier: str
    validation_result: str
    permission_result: str
    executed: int = Field(default=0)
    error: Optional[str] = None
    repair_attempt: int = Field(default=0)


class ReliabilityEvent(SQLModel, table=True):
    __tablename__ = "reliability_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: float = Field(index=True)
    event_type: str
    backend_from: str
    backend_to: str
    reliability_rate: float
    window_size: int
    failed_calls_count: int
    failed_calls_json: str
    details: Optional[str] = None


# ==========================================
# Extended Schema Models
# ==========================================

class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = None
    instructions: Optional[str] = None
    workspace_path: Optional[str] = None
    local_folders_json: Optional[str] = None
    is_active: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)



class Artifact(SQLModel, table=True):
    __tablename__ = "artifacts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    project_id: Optional[str] = Field(default=None, foreign_key="projects.id", index=True)
    session_id: Optional[str] = Field(default=None, foreign_key="sessions.session_id", index=True)
    conversation_id: Optional[str] = Field(default=None, index=True)
    name: str = Field(index=True)
    type: str = Field(description="code/markdown/html/json/csv/python/svg/document/other")
    content: str
    version: int = Field(default=1)
    language: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ArtifactVersion(SQLModel, table=True):
    __tablename__ = "artifact_versions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    artifact_id: str = Field(foreign_key="artifacts.id", index=True)
    version: int = Field(index=True)
    content: str
    summary: Optional[str] = None
    created_by: str = Field(default="agent")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FileVersion(SQLModel, table=True):
    __tablename__ = "file_versions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    file_path: str = Field(index=True)
    session_id: str = Field(index=True)
    content: str
    version_number: int = Field(default=1, index=True)
    created_by: str = Field(default="agent")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Attachment(SQLModel, table=True):
    __tablename__ = "attachments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: Optional[str] = Field(default=None, foreign_key="sessions.session_id", index=True)
    project_id: Optional[str] = Field(default=None, foreign_key="projects.id", index=True)
    filename: str = Field(index=True)
    path: str
    size_bytes: int
    content_type: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)



class Memory(SQLModel, table=True):
    __tablename__ = "memories"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    project_id: Optional[str] = Field(default=None, foreign_key="projects.id", index=True)
    category: str = Field(index=True, description="preference/fact/decision/workflow/task_context")
    content: str
    source_session_id: Optional[str] = Field(default=None, foreign_key="sessions.session_id", index=True)
    confidence: float = Field(default=1.0)
    pinned: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = None


class Document(SQLModel, table=True):
    __tablename__ = "documents"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    project_id: str = Field(foreign_key="projects.id", index=True)
    file_path: str = Field(index=True)
    file_name: str
    language: Optional[str] = None
    file_hash: Optional[str] = None
    size_bytes: Optional[int] = None
    last_indexed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DocumentChunk(SQLModel, table=True):
    __tablename__ = "document_chunks"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    document_id: str = Field(foreign_key="documents.id", index=True)
    chunk_index: int
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    content: str
    embedding_vector_id: Optional[str] = None
    metadata_json: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ToolCall(SQLModel, table=True):
    __tablename__ = "tool_calls"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: str = Field(foreign_key="sessions.session_id", index=True)
    message_id: Optional[int] = Field(default=None, foreign_key="messages.id", index=True)
    call_id: str = Field(index=True)
    tool_name: str = Field(index=True)
    arguments_json: Optional[str] = None
    status: str = Field(default="pending", index=True)
    result: Optional[str] = None
    error_message: Optional[str] = None
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AgentRun(SQLModel, table=True):
    __tablename__ = "agent_runs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: str = Field(foreign_key="sessions.session_id", index=True)
    project_id: Optional[str] = Field(default=None, foreign_key="projects.id", index=True)
    status: str = Field(default="running", index=True)
    model: Optional[str] = None
    tokens_in: int = Field(default=0)
    tokens_out: int = Field(default=0)
    latency_ms: int = Field(default=0)
    tool_calls_count: int = Field(default=0)
    files_modified_json: Optional[str] = None
    errors_json: Optional[str] = None
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
