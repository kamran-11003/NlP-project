import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def shop_db(tmp_path):
    """A tiny unseen database: customers, orders (with a foreign key by name only)."""
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, order_date TEXT, channel TEXT);
    """)
    cities = ["Curitiba", "Sao Paulo", "Florianopolis", "Guarulhos"]
    con.executemany("INSERT INTO customers VALUES (?,?,?)",
                    [(i, f"Customer {i}", cities[i % 4]) for i in range(1, 41)])
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?)",
                    [(i, 1 + i % 40, 10.0 * (i % 7 + 1), f"2018-{1 + i % 12:02d}-15", ["web", "store"][i % 2])
                     for i in range(1, 301)])
    con.commit()
    con.close()
    return f"sqlite:///{path.as_posix()}"
