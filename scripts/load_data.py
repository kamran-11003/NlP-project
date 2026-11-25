"""Load the development datasets into the database engines.

  Olist e-commerce (9 CSVs)   -> PostgreSQL (POSTGRES_URL in .env)  and/or  SQLite data/olist.db
  Olist marketing funnel (2)  -> SQLite data/marketing.db
  Brazil states sample CSV    -> stays a CSV, for trying the "upload CSV" feature in the app

Usage:  python scripts/load_data.py            (SQLite only, plus PostgreSQL if POSTGRES_URL is set)
"""
import os
import re
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from analyst import config  # noqa: E402  (loads .env)


def table_name(csv: Path) -> str:
    return re.sub(r"^olist_|_dataset$", "", csv.stem)


def read(csv: Path) -> pd.DataFrame:
    df = pd.read_csv(csv, dtype={c: str for c in ["customer_zip_code_prefix", "seller_zip_code_prefix",
                                                  "geolocation_zip_code_prefix"]})
    for c in df.columns:
        if re.search(r"(date|timestamp|_at$)", c):
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def pg_copy(table, conn, keys, data_iter):
    """Fast bulk insert for PostgreSQL using COPY."""
    cols = ", ".join(f'"{k}"' for k in keys)
    with conn.connection.cursor() as cur:
        with cur.copy(f'COPY "{table.name}" ({cols}) FROM STDIN') as cp:
            for row in data_iter:
                cp.write_row(row)


def load(folder: Path, url: str, index_cols=True):
    eng = create_engine(url)
    for csv in sorted(folder.glob("*.csv")):
        name = table_name(csv)
        df = read(csv)
        df.to_sql(name, eng, if_exists="replace", index=False, chunksize=50000,
                  method=pg_copy if eng.dialect.name == "postgresql" else None)
        if index_cols:
            with eng.begin() as conn:
                for c in df.columns:
                    if c.endswith("_id") or c.endswith("_prefix"):
                        conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_{name}_{c}" ON "{name}" ("{c}")'))
        print(f"  {name}: {len(df):,} rows")
    eng.dispose()


if __name__ == "__main__":
    data = ROOT / "data"
    print("Olist -> SQLite data/olist.db")
    load(data / "olist", f"sqlite:///{(data / 'olist.db').as_posix()}")
    print("Marketing funnel -> SQLite data/marketing.db")
    load(data / "funnel", f"sqlite:///{(data / 'marketing.db').as_posix()}")
    pg = os.getenv("POSTGRES_URL")
    if pg:
        pg = re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", pg)
        base, dbname = pg.rsplit("/", 1)
        admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:  # create the database if it does not exist yet
            if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": dbname}).first():
                conn.execute(text(f'CREATE DATABASE "{dbname}"'))
        admin.dispose()
        print(f"Olist -> PostgreSQL database '{dbname}'")
        load(data / "olist", pg)
    else:
        print("POSTGRES_URL not set: skipped PostgreSQL")
    print("Done.")
