"""Data sources: any SQLAlchemy connection string, SQLite files, and uploaded CSVs.

Every connection is opened READ-ONLY, so even a query that slipped past the SQL guard
could not change data.
"""
import re
import threading
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, event, text

from . import config

SQLGLOT_DIALECT = {"postgresql": "postgres", "mssql": "tsql", "mysql": "mysql", "mariadb": "mysql",
                   "sqlite": "sqlite", "duckdb": "duckdb", "oracle": "oracle", "snowflake": "snowflake"}


def _readonly_sqlite_url(url: str) -> str:
    path = url.split("sqlite:///", 1)[1].split("?")[0]
    p = Path(path)
    if not p.is_absolute():
        p = config.ROOT / p
    if not p.exists():
        raise FileNotFoundError(f"SQLite file not found: {p}")
    return f"sqlite:///file:{p.as_posix()}?mode=ro&uri=true"


class DataSource:
    def __init__(self, name: str, url: str):
        self.name, self.url = name, url
        timeout_ms = config.QUERY_TIMEOUT_S * 1000
        if url.startswith("sqlite"):
            self.engine = create_engine(_readonly_sqlite_url(url))
        elif url.startswith("postgres"):
            if url.startswith("postgresql://") or url.startswith("postgres://"):
                url = re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", url)
            self.engine = create_engine(url, connect_args={
                "options": f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms}"})
        else:
            self.engine = create_engine(url)

            @event.listens_for(self.engine, "connect")
            def _ro(dbapi_conn, _):  # best effort for other engines (MySQL etc.)
                try:
                    cur = dbapi_conn.cursor()
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
                    cur.close()
                except Exception:
                    pass
        self.dialect = SQLGLOT_DIALECT.get(self.engine.dialect.name, self.engine.dialect.name)

    def run(self, sql: str, max_rows: int = config.MAX_ROWS) -> pd.DataFrame:
        """Run one (already guarded) SELECT and return at most max_rows rows. Always rolls back."""
        with self.engine.connect() as conn:
            timer = None
            if self.engine.dialect.name == "sqlite":  # SQLite has no statement timeout: interrupt it
                raw = conn.connection.dbapi_connection
                timer = threading.Timer(config.QUERY_TIMEOUT_S, raw.interrupt)
                timer.start()
            try:
                result = conn.execute(text(sql))
                rows = result.fetchmany(max_rows)
                df = pd.DataFrame(rows, columns=list(result.keys()))
            finally:
                if timer:
                    timer.cancel()
                conn.rollback()
        return df


def load_csvs_into_upload_db(files: list[tuple[str, bytes]]) -> list[str]:
    """Store uploaded CSV files as tables in data/uploads.db. Returns the table names."""
    import io
    config.UPLOAD_DB.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(f"sqlite:///{config.UPLOAD_DB.as_posix()}")
    names = []
    for filename, content in files:
        table = re.sub(r"\W+", "_", Path(filename).stem.lower()).strip("_") or "uploaded"
        df = pd.read_csv(io.BytesIO(content), sep=None, engine="python")
        df.columns = [re.sub(r"\W+", "_", str(c).strip().lower()).strip("_") for c in df.columns]
        df.to_sql(table, eng, if_exists="replace", index=False)
        names.append(table)
    eng.dispose()
    return names
