from fastapi import APIRouter

from app.security.response_helpers import success_response, error_response
from app.database import get_db

router = APIRouter()


@router.get("/health")
async def health_check():
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
        return success_response({"status": "healthy", "version": "1.0.0", "database": "connected"})
    except Exception:
        return error_response("UNHEALTHY", "Health check failed", 503)