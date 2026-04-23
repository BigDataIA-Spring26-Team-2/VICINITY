"""Application settings — Pydantic BaseSettings loaded from .env.

All secrets, connection strings, and feature flags live here.
Each field maps to an environment variable of the same name
(case-insensitive). SecretStr fields are never logged or serialized.

Usage:
    from app.config import get_settings
    settings = get_settings()
    settings.snowflake_account  # str
    settings.snowflake_password.get_secret_value()  # reveals secret
"""

from pydantic_settings import BaseSettings
from pydantic import SecretStr
from functools import lru_cache


class Settings(BaseSettings):
    # Snowflake
    snowflake_account: str
    snowflake_user: str
    snowflake_password: SecretStr
    snowflake_database: str
    snowflake_schema_name: str = "RAW"
    snowflake_warehouse: str
    snowflake_role: str

    # LLM and VECTORDB
    deepseek_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    pinecone_index: str
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    pinecone_api_key: SecretStr = SecretStr("")

    # Redis (empty = disabled, cache degrades to no-op)
    redis_url: str = ""

    # S3 backup (leave S3_BUCKET empty to disable)
    s3_bucket: str = ""
    aws_region: str = "us-east-1"

    # Authentication
    jwt_secret: str = "vicinity-dev-secret-change-me"
    jwt_expiry_hours: int = 72

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()