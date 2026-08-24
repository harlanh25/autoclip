"""SQLite-compatible wrapper over psycopg2.

Lets existing call sites keep using conn.execute(sql, params).fetchone()
with ? placeholders and polymorphic row access, while talking to Postgres.

Backend selected by DB_BACKEND env var: "postgres" or "sqlite" (default).
"""

import os
import sqlite3
from functools import lru_cache


# ---------------------------------------------------------------- rows

class Row:
    """Mirrors sqlite3.Row: row['col'], row[0], dict(row), .keys()."""

    __slots__ = ("_cols", "_vals", "_idx")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = tuple(vals)
        self._idx = {c.lower(): i for i, c in enumerate(cols)}

    def keys(self):
        return list(self._cols)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._vals[key]
        try:
            return self._vals[self._idx[key.lower()]]
        except KeyError:
            raise KeyError(key) from None

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def __contains__(self, key):
        return isinstance(key, str) and key.lower() in self._idx

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._cols == other._cols and self._vals == other._vals
        if isinstance(other, (tuple, list)):
            return self._vals == tuple(other)
        return NotImplemented

    def __repr__(self):
        return "<Row %s>" % dict(zip(self._cols, self._vals))


# ------------------------------------------------- placeholder translation

@lru_cache(maxsize=2048)
def translate(sql, has_params):
    """? -> %s outside string literals and comments.

    When params are present, literal % is doubled everywhere (including
    inside literals and comments) because psycopg2 applies % formatting
    across the entire statement.
    """
    out = []
    i, n = 0, len(sql)

    def emit(text):
        out.append(text.replace("%", "%%") if has_params else text)

    while i < n:
        c = sql[i]

        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            emit(sql[i:j])
            i = j
            continue

        if c == '"':
            j = sql.find('"', i + 1)
            j = n if j == -1 else j + 1
            emit(sql[i:j])
            i = j
            continue

        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            emit(sql[i:j])
            i = j
            continue

        if c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            emit(sql[i:j])
            i = j
            continue

        if c == "?":
            out.append("%s")
            i += 1
            continue

        emit(c)
        i += 1

    return "".join(out)


def _norm_params(params):
    if params is None:
        return None
    if isinstance(params, dict):
        return params or None
    seq = tuple(params)
    return seq or None


# ------------------------------------------------------------- results

class Result:
    """Eagerly-fetched result. Cursor is closed before this is returned."""

    def __init__(self, cursor):
        self.rowcount = cursor.rowcount
        self.description = cursor.description
        self._rows = []
        self._pos = 0
        if cursor.description is not None:
            cols = [d[0] for d in cursor.description]
            self._rows = [Row(cols, r) for r in cursor.fetchall()]

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def fetchmany(self, size=1):
        rows = self._rows[self._pos:self._pos + size]
        self._pos += len(rows)
        return rows

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def __len__(self):
        return len(self._rows)

    @property
    def lastrowid(self):
        raise RuntimeError(
            "lastrowid is not available on Postgres. Use "
            "conn.insert_returning_id(sql, params) instead."
        )


# ---------------------------------------------------------- connection

class PGConnection:
    """Quacks like sqlite3.Connection for the patterns this app uses."""

    def __init__(self, raw):
        self._raw = raw
        self.row_factory = None  # assigned by callers; ignored here

    def execute(self, sql, params=()):
        p = _norm_params(params)
        cur = self._raw.cursor()
        try:
            cur.execute(translate(sql, p is not None), p)
            return Result(cur)
        finally:
            cur.close()

    def executemany(self, sql, seq_of_params):
        seq = [_norm_params(p) for p in seq_of_params]
        cur = self._raw.cursor()
        try:
            if seq:
                cur.executemany(translate(sql, seq[0] is not None), seq)
            return Result(cur)
        finally:
            cur.close()

    def executescript(self, script):
        cur = self._raw.cursor()
        try:
            cur.execute(script)
        finally:
            cur.close()
        self._raw.commit()

    def insert_returning_id(self, sql, params=(), id_column="id"):
        """Single-row INSERT, returns the new id. Replaces .lastrowid."""
        stripped = sql.rstrip().rstrip(";")
        if "returning" not in stripped.lower():
            stripped += " RETURNING %s" % id_column
        row = self.execute(stripped, params).fetchone()
        return None if row is None else row[0]

    def cursor(self):
        return self._raw.cursor()

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()
        return False

    def __getattr__(self, name):
        return getattr(self._raw, name)


# ------------------------------------------------------------- connect

def backend():
    return os.environ.get("DB_BACKEND", "sqlite").strip().lower()


def _pg_connect_kwargs():
    """Socket path derived from INSTANCE_CONNECTION_NAME, or TCP via DB_HOST."""
    kw = {
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASS", ""),
        "dbname": os.environ.get("DB_NAME", "autoclip"),
    }
    host = os.environ.get("DB_HOST")
    if host:
        kw["host"] = host
        kw["port"] = int(os.environ.get("DB_PORT", "5432"))
        return kw

    icn = os.environ.get("INSTANCE_CONNECTION_NAME")
    if not icn:
        raise RuntimeError(
            "Set INSTANCE_CONNECTION_NAME (Auth Proxy / Cloud Run connector) "
            "or DB_HOST for a direct TCP connection."
        )
    prefix = os.environ.get("DB_SOCKET_DIR", "/cloudsql")
    kw["host"] = "%s/%s" % (prefix.rstrip("/"), icn)
    return kw


def connect(sqlite_path=None):
    """Return a connection for the configured backend."""
    if backend() == "postgres":
        import psycopg2
        return PGConnection(psycopg2.connect(**_pg_connect_kwargs()))

    if sqlite_path is None:
        raise RuntimeError("sqlite_path required when DB_BACKEND is sqlite")
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    return conn
