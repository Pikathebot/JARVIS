"""
Artifact tools for LLM agent interaction (Build Plan §15).
Allows the agent to create, update, and read structured artifacts in the database.
"""
import logging
from typing import Optional
from app.services.artifact_service import ArtifactService

logger = logging.getLogger("jarvis.agent.tools.artifacts")


def create_artifact(
    name: str,
    content: str,
    type: str = "code",
    language: Optional[str] = None,
    summary: Optional[str] = None,
    project_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """
    Create a new structured artifact (e.g. code file, markdown doc, diagram, report) saved to the project/session.
    Always use this tool when generating significant standalone documents, code deliverables, or reusable artifacts.

    Args:
        name: The title or filename of the artifact (e.g. 'main.py', 'Architecture Plan', 'design_system.md').
        type: The artifact type: 'code', 'markdown', 'html', 'json', 'csv', 'python', 'svg', 'document', or 'other'.
        content: The full content of the artifact.
        language: Programming or markup language (e.g. 'python', 'typescript', 'markdown').
        summary: A brief description of what this artifact contains.
    """
    clean_name = str(name or "").strip()
    if not clean_name:
        return "Error: Artifact name cannot be empty."

    clean_type = str(type or "code").strip().lower()

    try:
        service = ArtifactService()
        art = service.create_artifact(
            name=clean_name,
            type=clean_type,
            content=content or "",
            conversation_id=session_id,
            project_id=project_id,
            language=language,
            summary=summary,
            created_by="agent"
        )
        logger.info("Agent created artifact '%s' (ID: %s, v%d)", art.name, art.id, art.version)
        return f"Successfully created artifact '{art.name}' (ID: {art.id}, Version: {art.version}, Type: {art.type})."
    except Exception as e:
        logger.error("Error creating artifact '%s': %s", clean_name, e)
        return f"Error creating artifact '{clean_name}': {str(e)}"


def update_artifact(
    artifact_id: str,
    content: str,
    summary: Optional[str] = None,
) -> str:
    """
    Update the content of an existing artifact, automatically creating a new version snapshot.

    Args:
        artifact_id: The unique ID of the artifact to update.
        content: The updated content of the artifact.
        summary: Optional changelog note explaining what changed in this version.
    """
    clean_id = str(artifact_id or "").strip()
    if not clean_id:
        return "Error: artifact_id cannot be empty."

    try:
        service = ArtifactService()
        art = service.update_artifact(
            artifact_id=clean_id,
            content=content,
            summary=summary,
            created_by="agent"
        )
        logger.info("Agent updated artifact '%s' to v%d", art.id, art.version)
        return f"Successfully updated artifact '{art.name}' (ID: {art.id}) to Version {art.version}."
    except KeyError:
        return f"Error: Artifact with ID '{clean_id}' not found."
    except Exception as e:
        logger.error("Error updating artifact '%s': %s", clean_id, e)
        return f"Error updating artifact '{clean_id}': {str(e)}"


def read_artifact(artifact_id: str) -> str:
    """
    Retrieve and read the current content and metadata of an existing artifact by ID.

    Args:
        artifact_id: The unique ID of the artifact to read.
    """
    clean_id = str(artifact_id or "").strip()
    if not clean_id:
        return "Error: artifact_id cannot be empty."

    try:
        service = ArtifactService()
        art = service.get_artifact(clean_id)
        if not art:
            return f"Error: Artifact with ID '{clean_id}' not found."
        return (
            f"Artifact: {art.name} (ID: {art.id})\n"
            f"Type: {art.type} | Language: {art.language or 'text'} | Version: {art.version}\n"
            f"Updated: {art.updated_at.isoformat() if art.updated_at else 'unknown'}\n"
            f"---\n"
            f"{art.content}"
        )
    except Exception as e:
        logger.error("Error reading artifact '%s': %s", clean_id, e)
        return f"Error reading artifact '{clean_id}': {str(e)}"
