"""Safety: refuse dangerous requests and never run a dangerous query.

Three layers:
1. `dangerous_request` - quick check on the user's words (delete / drop / passwords ...).
2. `check_sql` - parses the SQL with sqlglot; only a single read-only SELECT is allowed, with no
   system tables and no dangerous functions.
3. The database connection itself is read-only (see sources.py).
"""
import re

import sqlglot
from sqlglot import exp

REQUEST_PATTERNS = [
    r"\b(drop|truncate|delete|insert|alter|grant|revoke|vacuum|reindex)\b",
    r"\bupdate\b.*\b(set|to|change|price|status|value)\b",
    r"\b(wipe|erase|destroy|overwrite|corrupt|shut ?down)\b",
    r"\b(remove|clear)\b.*\b(all|every|table|tables|data|records|rows|database)\b",
    r"\b(create|add|make)\b.*\b(table|user|index|column|role)\b",
    r"\b(password|passwords|credential|credentials|api key|secret|ssh|hash(ed)? password)\b",
    r"\bignore\b.*\b(previous|above|prior|all)\b.*\b(instruction|instructions|rules|prompt)\b",
    r"\b(system prompt|your instructions|developer message)\b",
    r"\b(execute|run)\b.*\b(command|shell|script|code)\b",
]

BLOCKED_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Merge,
                 exp.TruncateTable, exp.Command, exp.Into, exp.Set, exp.Use, exp.Grant, exp.Revoke, exp.Transaction,
                 exp.Commit, exp.Rollback, exp.Copy, exp.Pragma, exp.Attach, exp.Detach, exp.LoadData)

BLOCKED_FUNCS = {
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "sleep", "benchmark", "pg_read_file",
    "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export", "lo_get", "lo_put",
    "dblink", "dblink_exec", "pg_terminate_backend", "pg_cancel_backend", "set_config", "pg_reload_conf",
    "load_extension", "load_file", "readfile", "writefile", "edit", "fts3_tokenizer", "query_to_xml",
    "pg_logdir_ls", "pg_file_write", "copy", "xp_cmdshell", "openrowset", "opendatasource",
    "current_user", "session_user", "user", "version", "inet_server_addr", "pg_backend_pid",
}
SAFE_CURRENT = {"current_date", "current_time", "current_timestamp", "current_datetime"}  # other current_* = system info
BLOCKED_TABLE_PREFIXES = ("pg_", "sqlite_", "information_schema", "mysql.", "sys.", "performance_schema")


def dangerous_request(question: str) -> str | None:
    q = question.lower()
    for p in REQUEST_PATTERNS:
        if re.search(p, q):
            return ("I can only read and analyse the data. I can't change, delete or create anything, "
                    "and I can't reveal credentials or system details.")
    return None


def check_sql(sql: str, dialect: str) -> tuple[bool, str]:
    """Return (ok, reason)."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception as e:  # cannot be parsed -> cannot be proven safe
        return False, f"SQL could not be parsed: {str(e).splitlines()[0][:200]}"
    if len(statements) != 1:
        return False, "Only a single statement is allowed."
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        return False, f"Only SELECT queries are allowed (got {tree.key.upper()})."
    for node in tree.walk():
        if isinstance(node, BLOCKED_NODES):
            return False, f"Blocked operation: {node.key.upper()}."
        if isinstance(node, exp.Func):
            fname = (node.sql_name() if not isinstance(node, exp.Anonymous) else node.name).lower()
            if fname in BLOCKED_FUNCS or (fname.startswith("current_") and fname not in SAFE_CURRENT):
                return False, f"Blocked function: {fname}."
        if isinstance(node, (exp.CurrentUser,)):
            return False, "System information is not allowed."
        if isinstance(node, exp.Table):
            full = ".".join(p for p in (node.catalog, node.db, node.name) if p).lower()
            if full.startswith(BLOCKED_TABLE_PREFIXES) or node.name.lower().startswith(("pg_", "sqlite_")):
                return False, f"System table {full} is not allowed."
        if isinstance(node, exp.Lock) or (isinstance(node, exp.Select) and node.args.get("locks")):
            return False, "Locking reads are not allowed."
    return True, "ok"
