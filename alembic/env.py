from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
from alembic.ddl.impl import DefaultImpl
from app.config import get_settings


class SnowflakeImpl(DefaultImpl):
    __dialect__ = 'snowflake'


config = context.config
fileConfig(config.config_file_name)

settings = get_settings()

config.set_main_option(
    'sqlalchemy.url',
    f"snowflake://{settings.snowflake_user}:{settings.snowflake_password.get_secret_value()}"
    f"@{settings.snowflake_account}/{settings.snowflake_database}/{settings.snowflake_schema_name}"
    f"?warehouse={settings.snowflake_warehouse}&role={settings.snowflake_role}"
)

target_metadata = None


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()