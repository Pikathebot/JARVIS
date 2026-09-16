from app.routers.projects import router as projects_router
from app.routers.artifacts import router as artifacts_router
from app.routers.memories import router as memories_router
from app.routers.persona import router as persona_router
from app.routers.voice import router as voice_router
from app.routers.awareness import router as awareness_router
from app.routers.routines import router as routines_router
from app.routers.models import router as models_router
from app.routers.confirmations import router as confirmations_router

__all__ = [
    "projects_router",
    "artifacts_router",
    "memories_router",
    "persona_router",
    "voice_router",
    "awareness_router",
    "routines_router",
    "models_router",
    "confirmations_router",
]
