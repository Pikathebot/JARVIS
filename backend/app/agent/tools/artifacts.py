"""
Artifact tools for LLM agent interaction (Build Plan §15).
Allows the agent to create, update, and read structured artifacts in the database.
"""
import logging
from typing import Optional
from app.services.artifact_service import ArtifactService
from app.agent.tools.file_kinds import closest_region

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
    Save substantial, self-contained content the user will reuse, edit or keep -- a plan, report, spec, document, or longer code meant to be read and iterated on -- as a versioned artifact. Short answers, snippets and explanations stay in chat; anything that must live on disk to be used (a script to run, config, a change to project code) is a file (write_file).

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
    project_id: Optional[str] = None,
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
        err = _out_of_scope(service, clean_id, project_id)
        if err:
            return err
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


def _out_of_scope(service, artifact_id: str, project_id: Optional[str]) -> str:
    """An artifact from another workspace is invisible from this one (tool review, 2026-09-30)."""
    if not project_id:
        return ""
    art = service.get_artifact(artifact_id)
    if art and art.project_id and art.project_id != project_id:
        return f"Error: Artifact with ID '{artifact_id}' not found in this workspace."
    return ""


def patch_artifact(
    artifact_id: str,
    search_block: str,
    replacement_block: str,
    replace_all: bool = False,
    summary: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """
    Change part of an existing artifact by replacing an exact block of its text, saving a new version -- use this instead of update_artifact for edits, so the whole artifact isn't resent.

    Args:
        artifact_id: The unique ID of the artifact to change.
        search_block: The exact existing text to replace (unique unless replace_all).
        replacement_block: The new text.
        replace_all: Replace every occurrence instead of requiring exactly one.
        summary: Optional changelog note for the new version.
    """
    clean_id = str(artifact_id or "").strip()
    if not clean_id:
        return "Error: artifact_id cannot be empty."
    if not search_block:
        return "Error: search_block cannot be empty."
    try:
        service = ArtifactService()
        art = service.get_artifact(clean_id)
        if not art or (project_id and art.project_id and art.project_id != project_id):
            return f"Error: Artifact with ID '{clean_id}' not found in this workspace."
        current = (art.content or "").replace("\r\n", "\n")
        search = search_block.replace("\r\n", "\n")
        count = current.count(search)
        if count == 0:
            return (f"Error: That text isn't in '{art.name}'. The closest part of version {art.version} is:\n"
                    f"{closest_region(current, search)}\n"
                    "Use the exact current text (without the line numbers) as search_block.")
        if count > 1 and not replace_all:
            return (f"Error: That text appears {count} times in '{art.name}'. Include more surrounding "
                    "lines to make it unique, or pass replace_all=true.")
        new_content = current.replace(search, (replacement_block or "").replace("\r\n", "\n"), -1 if replace_all else 1)
        updated = service.update_artifact(
            artifact_id=clean_id, content=new_content, summary=summary or "Edited part of the artifact", created_by="agent"
        )
        logger.info("Agent patched artifact '%s' to v%d (%d replacement(s))", updated.id, updated.version, count if replace_all else 1)
        return (f"Patched '{updated.name}' (ID: {updated.id}): {count if replace_all else 1} replacement(s), "
                f"now version {updated.version}.")
    except KeyError:
        return f"Error: Artifact with ID '{clean_id}' not found."
    except Exception as e:
        logger.error("Error patching artifact '%s': %s", clean_id, e)
        return f"Error patching artifact '{clean_id}': {str(e)}"


def read_artifact(artifact_id: str, project_id: Optional[str] = None) -> str:
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
        if not art or (project_id and art.project_id and art.project_id != project_id):
            return f"Error: Artifact with ID '{clean_id}' not found in this workspace."
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
