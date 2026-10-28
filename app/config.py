from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: SecretStr
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")
    internal_api_key: SecretStr
    token_encryption_key: SecretStr
    enable_autopublish: bool = False
    meta_app_id: str = ""
    meta_app_secret: SecretStr = SecretStr("")
    meta_redirect_uri: str = ""
    meta_api_version: str = "v26.0"
    supabase_url: str = ""
    supabase_service_role_key: SecretStr = SecretStr("")
    supabase_bucket: str = "originals"
    publishing_quota_cap: int = Field(default=25, ge=1, le=50)

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
