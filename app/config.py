"""
Centralized app configuration, loaded from environment variables / .env.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    secret_key: str = "change-me"
    # Publicly reachable base URL for this API (e.g. your ngrok URL in
    # dev, or your real domain in production) -- used to build the TwiML
    # callback URL for agent-initiated outbound calls.
    public_base_url: str = "https://your-domain.example.com"

    database_url: str = "postgresql://mortgage_user:mortgage_pass@localhost:5432/mortgage_agent"
    redis_url: str = "redis://localhost:6379/0"

    # Gemini Live (speech-to-speech)
    google_genai_use_vertexai: bool = True
    google_cloud_project: str | None = None
    google_cloud_location: str = "us-central1"
    gemini_api_key: str | None = None
    gemini_live_model: str = "gemini-live-2.5-flash-native-audio"

    # Document storage / OCR
    doc_storage_bucket: str = "mortgage-agent-docs"
    ocr_engine: str = "tesseract"

    # Knowledge base (guideline vector index)
    chroma_persist_dir: str = "/data/chroma"

    # Notifications
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    sendgrid_api_key: str | None = None
    notify_from_email: str = "loans@example.com"

    # Compliance
    record_calls: bool = True
    call_retention_days: int = 2555


@lru_cache
def get_settings() -> Settings:
    return Settings()
