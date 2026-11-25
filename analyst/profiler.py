"""Automatic schema profiling for an unseen database.

For every table: row count, columns, types, a 'kind' for each column (id / category / text /
number / date), distinct values of low-cardinality text columns (used for domain-word correction),
min/max for numbers and dates, and relationships (declared foreign keys + ones inferred from names).
"""
import hashlib
import json
import re

from sqlalchemy import column, func, inspect, select, table
from sqlalchemy.types import Date, DateTime, Numeric, Float, Integer

from . import config
from .sources import DataSource

MAX_VOCAB = 10000      # max distinct values kept per categorical column
SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast", "mysql", "performance_schema", "sys"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")
HEX_ID_RE = re.compile(r"^[0-9a-fA-F-]{16,}$")


def _singular(name: str) -> str:
    name = name.lower()
    for suf in ("ies", "es", "s"):
        if name.endswith(suf) and len(name) > len(suf) + 2:
            return name[: -len(suf)] + ("y" if suf == "ies" else "")
    return name


def _is_id_name(col: str) -> bool:
    c = col.lower()
    return c == "id" or c.endswith("_id") or (col.endswith("Id") and len(col) > 2) or c.endswith("_uuid")


def _tables(insp, src: DataSource):
    try:
        default = insp.default_schema_name
        schemas = [s for s in insp.get_schema_names()
                   if s not in SYSTEM_SCHEMAS and not s.startswith("pg_")]
    except Exception:
        default, schemas = None, [None]
    for s in schemas or [None]:
        for t in insp.get_table_names(schema=s) + insp.get_view_names(schema=s):
            if t.startswith("sqlite_"):
                continue
            full = t if (s is None or s == default) else f"{s}.{t}"
            yield full, s, t


def profile_source(src: DataSource, use_cache: bool = True) -> dict:
    key = hashlib.sha1(src.url.encode()).hexdigest()[:12]
    cache = config.CACHE_DIR / "profiles" / f"{src.name}_{key}.json"
    if use_cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    insp = inspect(src.engine)
    tables = {}
    with src.engine.connect() as conn:
        for full, schema, name in _tables(insp, src):
            t = table(name, schema=schema) if schema else table(name)
            rows = conn.execute(select(func.count()).select_from(t)).scalar() or 0
            cols = []
            for c in insp.get_columns(name, schema=schema):
                cols.append(_profile_column(conn, t, c, rows))
            try:
                pk = insp.get_pk_constraint(name, schema=schema).get("constrained_columns", [])
            except Exception:
                pk = []
            fks = []
            try:
                for fk in insp.get_foreign_keys(name, schema=schema):
                    for a, b in zip(fk["constrained_columns"], fk["referred_columns"]):
                        fks.append({"col": a, "ref_table": fk["referred_table"], "ref_col": b, "inferred": False})
            except Exception:
                pass
            tables[full] = {"rows": rows, "columns": cols, "pk": pk, "fks": fks}
        conn.rollback()

    _infer_relationships(tables)
    prof = {"source": src.name, "dialect": src.dialect, "tables": tables}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(prof, default=str), encoding="utf-8")
    return prof


def _profile_column(conn, t, c, rows: int) -> dict:
    name, typ = c["name"], c["type"]
    info = {"name": name, "type": str(typ)}
    col = column(name)
    if isinstance(typ, (Date, DateTime)):
        lo, hi = conn.execute(select(func.min(col), func.max(col)).select_from(t)).one()
        info.update(kind="date", min=str(lo), max=str(hi))
        return info
    if isinstance(typ, (Integer, Numeric, Float)):
        lo, hi, nd = conn.execute(select(func.min(col), func.max(col), func.count(col.distinct()))
                                  .select_from(t)).one()
        kind = "id" if _is_id_name(name) else ("category" if nd and nd <= 20 else "number")
        info.update(kind=kind, min=lo, max=hi, n_distinct=nd)
        if kind == "category":
            info["values"] = [r[0] for r in conn.execute(
                select(col).select_from(t).group_by(col).order_by(col)) if r[0] is not None]
        return info

    # text-like column: most frequent values
    top = conn.execute(select(col, func.count()).select_from(t).group_by(col)
                       .order_by(func.count().desc()).limit(MAX_VOCAB + 1)).all()
    values = [str(v) for v, _ in top if v is not None]
    info["samples"] = [v[:80] for v in values[:5]]
    if values and all(DATE_RE.match(v) for v in values[:20]):
        lo, hi = conn.execute(select(func.min(col), func.max(col)).select_from(t)).one()
        info.update(kind="date", min=str(lo), max=str(hi))
    elif _is_id_name(name) or (values and all(HEX_ID_RE.match(v) for v in values[:20])):
        info["kind"] = "id"
    elif values and sum(len(v) for v in values[:50]) / min(len(values), 50) > 40:
        info["kind"] = "text"  # free text (comments, descriptions): no vocabulary
    else:
        info.update(kind="category", values=values[:MAX_VOCAB], n_distinct=len(values),
                    truncated=len(values) > MAX_VOCAB)
    return info


def _infer_relationships(tables: dict):
    """Link columns that share a name (after stripping the table-name prefix) and look like keys."""
    def key_name(tname, col):
        base = _singular(tname.split(".")[-1])
        c = re.sub(r"(?<=[a-z])Id$", "_id", col).lower()  # CustomerId -> customer_id
        if c == "id":  # customers.id  <->  orders.customer_id
            return base + "_id"
        return c[len(base) + 1:] if c.startswith(base + "_") and not c.endswith("_id") else c

    index = {}
    for tname, t in tables.items():
        for c in t["columns"]:
            if c.get("kind") in ("id",) or c["name"].lower().endswith(("_code", "_prefix", "_key")):
                index.setdefault(key_name(tname, c["name"]), []).append((tname, c["name"]))
    for members in index.values():
        if len(members) < 2:
            continue
        for a_t, a_c in members:
            for b_t, b_c in members:
                if a_t < b_t and not any(f["ref_table"] == b_t and f["col"] == a_c for f in tables[a_t]["fks"]):
                    tables[a_t]["fks"].append({"col": a_c, "ref_table": b_t, "ref_col": b_c, "inferred": True})


def table_card(tname: str, t: dict) -> str:
    """Compact text description of one table, used in prompts and for retrieval."""
    lines = [f"TABLE {tname} ({t['rows']:,} rows)"]
    for c in t["columns"]:
        kind = c.get("kind", "")
        desc = f"  {c['name']} {c['type']}"
        if kind == "id":
            desc += " [key]"
        elif kind == "date":
            desc += f" [date {c.get('min')} .. {c.get('max')}]"
        elif kind == "number":
            desc += f" [{c.get('min')} .. {c.get('max')}]"
        elif kind == "category":
            vals = c.get("values", [])
            shown = ", ".join(map(str, vals[:12]))
            desc += f" values: {shown}" + (f" ... ({c.get('n_distinct')} distinct)" if len(vals) > 12 else "")
        elif kind == "text":
            desc += " [free text]"
        lines.append(desc)
    for f in t["fks"]:
        lines.append(f"  JOIN {tname}.{f['col']} = {f['ref_table']}.{f['ref_col']}")
    return "\n".join(lines)
