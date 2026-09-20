import json
import logging
import time
from typing import Any, Callable, Optional
from sqlmodel import Session, select, col, or_

from app.database.models import (
    Project,
    Artifact,
    Memory,
    Document,
    DocumentChunk,
    ToolCall,
    AgentRun,
    Session as DBSession,
    Message,
    CompactionEvent,
    ToolCallAudit,
    ReliabilityEvent,
)

# Seeded by the initial migration; the workspace a session belongs to when none was chosen.
DEFAULT_PROJECT_ID = "default-workspace"

logger = logging.getLogger("jarvis.memory.store")


class MemoryStore:
    """
    SQLModel-backed short-term conversation memory and compaction event store.
    Uses unified database session factory managed by Alembic migrations.
    """

    def __init__(
        self,
        session_factory: Optional[Callable[[], Session]] = None,
        db_path: Optional[str] = None
    ):
        if session_factory is not None:
            self._session_factory = session_factory
        elif db_path is not None:
            # Isolated engine creation for standalone tests specifying a temp db path
            from pathlib import Path
            from sqlalchemy import event
            from sqlalchemy.orm import sessionmaker
            from sqlmodel import SQLModel, create_engine
            clean_path = Path(db_path).resolve().as_posix()
            eng = create_engine(
                f"sqlite:///{clean_path}",
                connect_args={"check_same_thread": False},
                pool_pre_ping=True
            )

            @event.listens_for(eng, "connect")
            def _set_pragmas(dbapi_connection, connection_record):
                try:
                    c = dbapi_connection.cursor()
                    c.execute("PRAGMA journal_mode=WAL")
                    c.execute("PRAGMA busy_timeout=5000")
                    c.close()
                except Exception:
                    pass

            SQLModel.metadata.create_all(eng)
            self._session_factory = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=eng,
                class_=Session
            )
        else:
            from app.database.session import SessionLocal
            self._session_factory = SessionLocal

    def _get_session(self) -> Session:
        """Create and return a new session from the injected session factory."""
        return self._session_factory()

    def get_or_create_session(
        self,
        session_id: str,
        title: Optional[str] = None,
        chat_mode: Optional[str] = "WORKSPACE",
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = time.time()
        mode_val = (chat_mode or "WORKSPACE").upper()
        with self._get_session() as session:
            try:
                sess = session.get(DBSession, session_id)
                if sess:
                    modified = False
                    if chat_mode and sess.chat_mode != mode_val:
                        sess.chat_mode = mode_val
                        modified = True
                    if project_id is not None and sess.project_id != project_id:
                        sess.project_id = project_id
                        modified = True
                    if modified:
                        sess.updated_at = now
                        session.add(sess)
                        session.commit()
                    return {
                        "session_id": sess.session_id,
                        "project_id": sess.project_id,
                        "title": sess.title,
                        "chat_mode": sess.chat_mode,
                        "created_at": sess.created_at,
                        "updated_at": sess.updated_at
                    }

                default_title = title or f"Session {session_id[:8]}"
                sess = DBSession(
                    session_id=session_id,
                    project_id=project_id,
                    title=default_title,
                    chat_mode=mode_val,
                    created_at=now,
                    updated_at=now
                )
                session.add(sess)
                session.commit()
                return {
                    "session_id": session_id,
                    "project_id": project_id,
                    "title": default_title,
                    "chat_mode": mode_val,
                    "created_at": now,
                    "updated_at": now
                }
            except Exception:
                session.rollback()
                raise

    def set_session_chat_mode(self, session_id: str, chat_mode: str) -> None:
        mode_val = (chat_mode or "WORKSPACE").upper()
        now = time.time()
        with self._get_session() as session:
            try:
                sess = session.get(DBSession, session_id)
                if sess:
                    sess.chat_mode = mode_val
                    sess.updated_at = now
                    session.add(sess)
                    session.commit()
            except Exception:
                session.rollback()
                raise

    def list_sessions(self, project_id: Optional[str] = None, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """Most-recent-first. ``limit`` bounds the result: a sidebar showing "recent sessions"
        must never be handed the whole table (hundreds of rows once QA runs pile up)."""
        with self._get_session() as session:
            try:
                statement = select(DBSession)
                if project_id == DEFAULT_PROJECT_ID:
                    # Sessions created before a project was ever chosen have no project_id;
                    # they belong to the default workspace, not to nowhere.
                    statement = statement.where(
                        or_(DBSession.project_id == project_id, col(DBSession.project_id).is_(None))
                    )
                elif project_id is not None:
                    statement = statement.where(DBSession.project_id == project_id)
                statement = statement.order_by(col(DBSession.updated_at).desc())
                if limit is not None and limit > 0:
                    statement = statement.limit(limit)
                sessions = session.exec(statement).all()
                # The stored title is a placeholder ("Session session_") unless a caller named
                # the session, so the sidebar shows the first thing the user said instead.
                first_user_message: dict[str, str] = {}
                if sessions:
                    ids = [s.session_id for s in sessions]
                    msg_statement = (
                        select(Message.session_id, Message.content)
                        .where(col(Message.session_id).in_(ids))
                        .where(Message.role == "user")
                        .where(Message.is_summary == 0)
                        .order_by(col(Message.id).asc())
                    )
                    for session_id, content in session.exec(msg_statement).all():
                        if session_id not in first_user_message and content:
                            first_user_message[session_id] = content.strip().splitlines()[0][:80] if content.strip() else ""
                return [
                    {
                        "session_id": s.session_id,
                        "project_id": s.project_id,
                        "title": s.title,
                        "chat_mode": s.chat_mode,
                        "created_at": s.created_at,
                        "updated_at": s.updated_at,
                        "last_message": first_user_message.get(s.session_id),
                    }
                    for s in sessions
                ]
            except Exception:
                session.rollback()
                raise


    def delete_session(self, session_id: str) -> bool:
        with self._get_session() as session:
            try:
                # Delete associated messages
                messages = session.exec(
                    select(Message).where(Message.session_id == session_id)
                ).all()
                for msg in messages:
                    session.delete(msg)

                # Delete associated compaction events
                events = session.exec(
                    select(CompactionEvent).where(CompactionEvent.session_id == session_id)
                ).all()
                for ev in events:
                    session.delete(ev)

                # Delete session record
                sess = session.get(DBSession, session_id)
                if sess:
                    session.delete(sess)
                    session.commit()
                    return True
                session.commit()
                return False
            except Exception:
                session.rollback()
                raise

    def get_messages(self, session_id: str, limit: Optional[int] = None) -> list[dict[str, Any]]:
        with self._get_session() as session:
            try:
                statement = (
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(col(Message.id).asc())
                )
                if limit is not None and limit > 0:
                    statement = statement.limit(limit)
                rows = session.exec(statement).all()


                messages = []
                for row in rows:
                    msg: dict[str, Any] = {
                        "role": row.role,
                        "content": row.content or ""
                    }
                    if row.reasoning_content:
                        msg["reasoning_content"] = row.reasoning_content
                    if row.name:
                        msg["name"] = row.name
                    if row.tool_calls_json:
                        try:
                            msg["tool_calls"] = json.loads(row.tool_calls_json)
                        except Exception:
                            pass
                    if row.is_summary:
                        msg["is_summary"] = bool(row.is_summary)
                    messages.append(msg)
                return messages
            except Exception:
                session.rollback()
                raise

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        tool_calls: Optional[list] = None,
        is_summary: bool = False,
        reasoning_content: Optional[str] = None
    ) -> None:
        self.get_or_create_session(session_id)
        now = time.time()
        tool_calls_str = json.dumps(tool_calls) if tool_calls else None
        token_estimate = max(1, len(content or "") // 4)

        with self._get_session() as session:
            try:
                msg = Message(
                    session_id=session_id,
                    role=role,
                    content=content,
                    reasoning_content=reasoning_content,
                    name=name,
                    tool_calls_json=tool_calls_str,
                    token_estimate=token_estimate,
                    is_summary=1 if is_summary else 0,
                    created_at=now
                )
                session.add(msg)

                sess = session.get(DBSession, session_id)
                if sess:
                    sess.updated_at = now
                    session.add(sess)

                session.commit()
            except Exception:
                session.rollback()
                raise

    def replace_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """
        Replace all stored messages for a session (used after compaction).
        """
        now = time.time()
        with self._get_session() as session:
            try:
                old_messages = session.exec(
                    select(Message).where(Message.session_id == session_id)
                ).all()
                for m in old_messages:
                    session.delete(m)

                for msg in messages:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    name = msg.get("name")
                    tool_calls = msg.get("tool_calls")
                    tool_calls_str = json.dumps(tool_calls) if tool_calls else None
                    is_summary = 1 if msg.get("is_summary") else 0
                    token_est = max(1, len(content) // 4)

                    new_msg = Message(
                        session_id=session_id,
                        role=role,
                        content=content,
                        name=name,
                        tool_calls_json=tool_calls_str,
                        token_estimate=token_est,
                        is_summary=is_summary,
                        created_at=now
                    )
                    session.add(new_msg)

                sess = session.get(DBSession, session_id)
                if sess:
                    sess.updated_at = now
                    session.add(sess)

                session.commit()
            except Exception:
                session.rollback()
                raise

    def record_compaction(
        self,
        session_id: str,
        strategy: str,
        tokens_before: int,
        tokens_after: int,
        details: Optional[str] = None
    ) -> dict[str, Any]:
        now = time.time()
        with self._get_session() as session:
            try:
                ev = CompactionEvent(
                    session_id=session_id,
                    strategy=strategy,
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    details=details,
                    created_at=now
                )
                session.add(ev)
                session.commit()
                return {
                    "session_id": session_id,
                    "strategy": strategy,
                    "tokens_before": tokens_before,
                    "tokens_after": tokens_after,
                    "details": details,
                    "timestamp": now
                }
            except Exception:
                session.rollback()
                raise

    def get_compaction_events(self, session_id: str) -> list[dict[str, Any]]:
        with self._get_session() as session:
            try:
                statement = (
                    select(CompactionEvent)
                    .where(CompactionEvent.session_id == session_id)
                    .order_by(col(CompactionEvent.id).asc())
                )
                events = session.exec(statement).all()
                return [
                    {
                        "id": e.id,
                        "session_id": e.session_id,
                        "strategy": e.strategy,
                        "tokens_before": e.tokens_before,
                        "tokens_after": e.tokens_after,
                        "details": e.details,
                        "created_at": e.created_at
                    }
                    for e in events
                ]
            except Exception:
                session.rollback()
                raise

    def record_tool_call_audit(
        self,
        call_id: str,
        turn_id: str,
        tool_name: str,
        args: dict[str, Any],
        model_tier: str,
        validation_result: str,
        permission_result: str,
        executed: bool,
        error: Optional[str] = None,
        timestamp: Optional[float] = None,
        repair_attempt: int = 0,
    ) -> dict[str, Any]:
        now = timestamp if timestamp is not None else time.time()
        args_json = json.dumps(args, sort_keys=True) if isinstance(args, dict) else str(args)
        with self._get_session() as session:
            try:
                audit = session.get(ToolCallAudit, call_id)
                if not audit:
                    audit = ToolCallAudit(
                        call_id=call_id,
                        turn_id=turn_id,
                        timestamp=now,
                        tool_name=tool_name,
                        args_json=args_json,
                        model_tier=model_tier,
                        validation_result=validation_result,
                        permission_result=permission_result,
                        executed=1 if executed else 0,
                        error=error,
                        repair_attempt=repair_attempt
                    )
                else:
                    audit.turn_id = turn_id
                    audit.timestamp = now
                    audit.tool_name = tool_name
                    audit.args_json = args_json
                    audit.model_tier = model_tier
                    audit.validation_result = validation_result
                    audit.permission_result = permission_result
                    audit.executed = 1 if executed else 0
                    audit.error = error
                    audit.repair_attempt = repair_attempt

                session.add(audit)
                session.commit()
                return {
                    "call_id": call_id,
                    "turn_id": turn_id,
                    "timestamp": now,
                    "tool_name": tool_name,
                    "args_json": args_json,
                    "model_tier": model_tier,
                    "validation_result": validation_result,
                    "permission_result": permission_result,
                    "executed": executed,
                    "error": error,
                    "repair_attempt": repair_attempt,
                }
            except Exception:
                session.rollback()
                raise

    def get_tool_call_audits(
        self,
        turn_id: Optional[str] = None,
        limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._get_session() as session:
            try:
                statement = select(ToolCallAudit)
                if turn_id:
                    statement = (
                        statement.where(ToolCallAudit.turn_id == turn_id)
                        .order_by(col(ToolCallAudit.timestamp).asc())
                        .limit(limit)
                    )
                else:
                    statement = (
                        statement.order_by(col(ToolCallAudit.timestamp).desc())
                        .limit(limit)
                    )
                rows = session.exec(statement).all()
                return [
                    {
                        "call_id": r.call_id,
                        "turn_id": r.turn_id,
                        "timestamp": r.timestamp,
                        "tool_name": r.tool_name,
                        "args_json": r.args_json,
                        "model_tier": r.model_tier,
                        "validation_result": r.validation_result,
                        "permission_result": r.permission_result,
                        "executed": r.executed,
                        "error": r.error,
                        "repair_attempt": r.repair_attempt
                    }
                    for r in rows
                ]
            except Exception:
                session.rollback()
                raise

    def get_tool_call_stats(self, model_tier: Optional[str] = None) -> dict[str, Any]:
        """
        Compute empirical tool-calling reliability metrics:
        First-attempt valid rate, repaired valid rate, and escalation rate.
        """
        with self._get_session() as session:
            try:
                statement = select(ToolCallAudit)
                if model_tier:
                    statement = statement.where(ToolCallAudit.model_tier == model_tier)
                rows = session.exec(statement).all()
            except Exception:
                session.rollback()
                raise

        total = len(rows)
        if total == 0:
            return {
                "total_calls": 0,
                "first_attempt_valid_count": 0,
                "first_attempt_valid_rate": 1.0,
                "repaired_valid_count": 0,
                "repaired_valid_rate": 0.0,
                "escalated_count": 0,
                "escalated_rate": 0.0,
                "executed_count": 0,
            }

        first_attempt_valid = sum(
            1 for r in rows if r.validation_result == "valid" and (r.repair_attempt or 0) == 0
        )
        repaired_valid = sum(
            1 for r in rows if r.validation_result == "valid" and (r.repair_attempt or 0) > 0
        )
        escalated = sum(1 for r in rows if r.validation_result == "invalid_escalated")
        executed = sum(1 for r in rows if r.executed == 1)

        return {
            "total_calls": total,
            "first_attempt_valid_count": first_attempt_valid,
            "first_attempt_valid_rate": round(first_attempt_valid / total, 4),
            "repaired_valid_count": repaired_valid,
            "repaired_valid_rate": round(repaired_valid / total, 4),
            "escalated_count": escalated,
            "escalated_rate": round(escalated / total, 4),
            "executed_count": executed,
        }

    def calculate_rolling_reliability(
        self,
        window_size: int = 30,
        model_tier: Optional[str] = "tier2",
        floor: float = 0.75,
        since_timestamp: Optional[float] = None
    ) -> dict[str, Any]:
        """
        Calculates tool-call reliability over a rolling window of the most recent tool calls.
        Formula: (first-attempt clean calls + auto-repaired calls) / total tool calls.
        """
        with self._get_session() as session:
            try:
                statement = select(ToolCallAudit).where(
                    ToolCallAudit.validation_result != "invalid_repaired"
                )
                if model_tier:
                    statement = statement.where(ToolCallAudit.model_tier == model_tier)
                if since_timestamp is not None:
                    statement = statement.where(col(ToolCallAudit.timestamp) >= since_timestamp)
                statement = statement.order_by(col(ToolCallAudit.timestamp).desc()).limit(window_size)
                rows = session.exec(statement).all()
            except Exception:
                session.rollback()
                raise

        total_samples = len(rows)
        if total_samples == 0:
            return {
                "total_samples": 0,
                "window_size": window_size,
                "cold_start": True,
                "clean_success_count": 0,
                "repaired_success_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "reliability_rate": 1.0,
                "floor": floor,
                "floor_breached": False,
                "failed_calls": [],
                "recent_calls": []
            }

        clean_success = sum(
            1 for r in rows if r.validation_result == "valid" and (r.repair_attempt or 0) == 0
        )
        repaired_success = sum(
            1 for r in rows if r.validation_result == "valid" and (r.repair_attempt or 0) > 0
        )
        success_count = clean_success + repaired_success
        failed_calls = [
            {
                "call_id": r.call_id,
                "turn_id": r.turn_id,
                "timestamp": r.timestamp,
                "tool_name": r.tool_name,
                "args_json": r.args_json,
                "model_tier": r.model_tier,
                "validation_result": r.validation_result,
                "permission_result": r.permission_result,
                "executed": r.executed,
                "error": r.error,
                "repair_attempt": r.repair_attempt
            }
            for r in rows if r.validation_result != "valid"
        ]
        failure_count = len(failed_calls)
        reliability_rate = round(success_count / total_samples, 4)
        cold_start = total_samples < window_size
        floor_breached = (not cold_start) and (reliability_rate < floor)

        recent_calls = [
            {
                "call_id": r.call_id,
                "turn_id": r.turn_id,
                "timestamp": r.timestamp,
                "tool_name": r.tool_name,
                "args_json": r.args_json,
                "model_tier": r.model_tier,
                "validation_result": r.validation_result,
                "permission_result": r.permission_result,
                "executed": r.executed,
                "error": r.error,
                "repair_attempt": r.repair_attempt
            }
            for r in rows
        ]

        return {
            "total_samples": total_samples,
            "window_size": window_size,
            "cold_start": cold_start,
            "clean_success_count": clean_success,
            "repaired_success_count": repaired_success,
            "success_count": success_count,
            "failure_count": failure_count,
            "reliability_rate": reliability_rate,
            "floor": floor,
            "floor_breached": floor_breached,
            "failed_calls": failed_calls,
            "recent_calls": recent_calls
        }

    def record_reliability_event(
        self,
        event_type: str,
        backend_from: str,
        backend_to: str,
        reliability_rate: float,
        window_size: int,
        failed_calls: list[dict[str, Any]],
        details: Optional[str] = None,
        timestamp: Optional[float] = None
    ) -> dict[str, Any]:
        """Record an alert or rollback event in the reliability audit history."""
        now = timestamp if timestamp is not None else time.time()
        failed_json = json.dumps(failed_calls)
        with self._get_session() as session:
            try:
                ev = ReliabilityEvent(
                    timestamp=now,
                    event_type=event_type,
                    backend_from=backend_from,
                    backend_to=backend_to,
                    reliability_rate=reliability_rate,
                    window_size=window_size,
                    failed_calls_count=len(failed_calls),
                    failed_calls_json=failed_json,
                    details=details
                )
                session.add(ev)
                session.commit()
                session.refresh(ev)
                return {
                    "id": ev.id,
                    "timestamp": now,
                    "event_type": event_type,
                    "backend_from": backend_from,
                    "backend_to": backend_to,
                    "reliability_rate": reliability_rate,
                    "window_size": window_size,
                    "failed_calls_count": len(failed_calls),
                    "failed_calls": failed_calls,
                    "details": details
                }
            except Exception:
                session.rollback()
                raise

    def get_reliability_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve recent reliability and rollback events."""
        with self._get_session() as session:
            try:
                statement = (
                    select(ReliabilityEvent)
                    .order_by(col(ReliabilityEvent.timestamp).desc())
                    .limit(limit)
                )
                rows = session.exec(statement).all()
                events = []
                for r in rows:
                    ev = {
                        "id": r.id,
                        "timestamp": r.timestamp,
                        "event_type": r.event_type,
                        "backend_from": r.backend_from,
                        "backend_to": r.backend_to,
                        "reliability_rate": r.reliability_rate,
                        "window_size": r.window_size,
                        "failed_calls_count": r.failed_calls_count,
                        "details": r.details
                    }
                    if r.failed_calls_json:
                        try:
                            ev["failed_calls"] = json.loads(r.failed_calls_json)
                        except Exception:
                            ev["failed_calls"] = []
                    else:
                        ev["failed_calls"] = []
                    events.append(ev)
                return events
            except Exception:
                session.rollback()
                raise
