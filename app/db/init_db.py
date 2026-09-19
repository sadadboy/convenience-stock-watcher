import time

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db.base import Base
from app.db.session import engine
from app.models import device
from app.models import location
from app.models import notification
from app.models import product
from app.models import restock
from app.models import store
from app.models import watch

# Lightweight, idempotent column additions for tables that already exist
# (create_all does not ALTER existing tables; we run these instead of Alembic).
_COLUMN_MIGRATIONS = (
    "ALTER TABLE stock_watches ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION",
    "ALTER TABLE stock_watches ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION",
    "ALTER TABLE stock_watches ADD COLUMN IF NOT EXISTS radius_meters INTEGER",
)


def init_db() -> None:
    attempts = 10
    for attempt in range(1, attempts + 1):
        try:
            Base.metadata.create_all(bind=engine)
            with engine.begin() as conn:
                for statement in _COLUMN_MIGRATIONS:
                    conn.execute(text(statement))
            return
        except OperationalError:
            if attempt == attempts:
                raise
            time.sleep(2)
