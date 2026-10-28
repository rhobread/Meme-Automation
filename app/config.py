from functools import lru_cache
import sys
import uuid
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DiscoveryFeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(max_length=2000)
    account_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    ideas_use_permitted: bool = False

    @field_validator("url")
    @classmethod
    def public_https(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
            raise ValueError("Require a configured HTTPS feed without credentials")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")
    internal_api_key: SecretStr
    token_encryption_key: SecretStr
    enable_autopublish: bool = False
    allow_paid_services: bool = False
    ai_provider: Literal["ollama", "openai"] = "ollama"
    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_timeout_seconds: float = Field(default=60, ge=1, le=90)
    storage_provider: Literal["local", "supabase"] = "local"
    local_media_dir: str = "./media"
    media_public_base_url: str = ""
    media_url_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    meta_app_id: str = ""
    meta_app_secret: SecretStr = SecretStr("")
    meta_redirect_uri: str = ""
    meta_api_version: str = "v26.0"
    supabase_url: str = ""
    supabase_service_role_key: SecretStr = SecretStr("")
    supabase_bucket: str = "originals"
    publishing_quota_cap: int = Field(default=25, ge=1, le=50)
    discovery_feeds: list[DiscoveryFeed] = Field(default_factory=list, max_length=5)
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini-2024-07-18"
    openai_moderation_model: str = "omni-moderation-latest"
    openai_timeout_seconds: float = Field(default=25, ge=1, le=30)
    ai_prompt_version: str = Field(default="original-id-v1", min_length=1, max_length=100)
    ai_input_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    ai_cached_input_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    ai_output_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    enable_autoapproval: bool = False
    render_font_path: str = "C:/Windows/Fonts/arialbd.ttf" if sys.platform == "win32" else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    generic_job_max_attempts: int = Field(default=3, ge=1, le=5)

    @field_validator("internal_api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32 or value.get_secret_value().startswith("replace-"):
            raise ValueError("Set a generated internal API key of at least 32 characters")
        return value

    @field_validator("token_encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: SecretStr) -> SecretStr:
        from cryptography.fernet import Fernet

        Fernet(value.get_secret_value().encode())
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
