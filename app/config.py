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
    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()