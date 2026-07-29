import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes_admin import router as admin_router
from app.api.routes_auth import router as auth_router
from app.api.routes_documents import router as documents_router
from app.api.routes_pipeline import router as pipeline_router
from app.api.routes_voice import router as voice_router
from app.config import get_settings
from app.dashboard.routes import router as dashboard_router

settings = get_settings()
logging.basicConfig(level=settings.log_level)

app = FastAPI(
    title="Bilingual Mortgage Loan Assistant",
    description="EN/VI AI loan assistant: document analysis, guideline Q&A, pipeline management.",
    version="0.1.0",
)

app.include_router(auth_router)
app.include_router(pipeline_router)
app.include_router(documents_router)
app.include_router(voice_router)
app.include_router(admin_router)
app.include_router(dashboard_router)
app.mount("/dashboard/static", StaticFiles(directory="app/dashboard/static"), name="dashboard_static")


@app.get("/health")
def health():
    return {"status": "ok", "env": settings.app_env}
