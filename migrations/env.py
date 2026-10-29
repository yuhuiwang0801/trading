from alembic import context
from sqlalchemy import engine_from_config, pool, MetaData
from trading.config import DATABASE_URL
from trading.db import Base
from trading import models
from trading.runtime import metadata as runtime_metadata
config=context.config
config.set_main_option('sqlalchemy.url', DATABASE_URL.replace('%', '%%'))
target_metadata=[Base.metadata, runtime_metadata]
if context.is_offline_mode():
    context.configure(url=DATABASE_URL, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction(): context.run_migrations()
else:
    engine=engine_from_config(config.get_section(config.config_ini_section),prefix='sqlalchemy.',poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection,target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction(): context.run_migrations()
