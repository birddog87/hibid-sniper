from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session
import os

class Base(DeclarativeBase):
    pass

def get_engine(db_path: str = None):
    path = db_path or os.environ.get("HIBID_DB_PATH", "hibid_sniper.db")
    return create_engine(f"sqlite:///{path}")

def get_session(engine=None) -> Session:
    if engine is None:
        engine = get_engine()
    return Session(engine)

def _add_column_if_missing(conn, table, column, col_type="TEXT"):
    result = conn.execute(text(f"PRAGMA table_info({table})"))
    columns = [row[1] for row in result]
    if column not in columns:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
        conn.commit()


def run_migrations(engine):
    with engine.connect() as conn:
        # Snipes table
        _add_column_if_missing(conn, "snipes", "winning_bid", "REAL")

        # Settings table - driving cost fields
        _add_column_if_missing(conn, "settings", "home_address", "TEXT")
        _add_column_if_missing(conn, "settings", "gas_price_per_liter", "REAL DEFAULT 1.80")
        _add_column_if_missing(conn, "settings", "fuel_consumption", "REAL DEFAULT 11.6")

        # Auction houses table - location fields
        _add_column_if_missing(conn, "auction_houses", "per_item_fee", "REAL DEFAULT 0.0")
        _add_column_if_missing(conn, "auction_houses", "address", "TEXT")
        _add_column_if_missing(conn, "auction_houses", "distance_km", "REAL")
        _add_column_if_missing(conn, "auction_houses", "drive_minutes", "REAL")

def ensure_settings(engine):
    from backend.models import Settings
    with Session(engine) as session:
        if session.get(Settings, 1) is None:
            session.add(Settings(id=1))
            session.commit()

def init_db(engine=None):
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)
    run_migrations(engine)
    ensure_settings(engine)
