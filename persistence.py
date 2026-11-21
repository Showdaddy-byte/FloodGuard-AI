"""Keeps subscriber data safe across Render restarts and deploys.

Render's free disk is wiped on every restart or deploy, and community.db lives
on it. This module mirrors the tables that cannot be re-created (who subscribed
to what, and the dam status you entered) into a free Turso database over its
HTTP API, and restores them into the empty local database at start-up.

How it works
  * Local SQLite stays the working database. Nothing else in the app changes.
  * At start-up each tracked table that is EMPTY locally is refilled from Turso.
  * A background thread then pushes only the rows that changed (every 60 s, and
    straight after any POST request such as a new subscription).
  * Turned on only when TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are both set.
    With them unset the app behaves exactly as before.

Safety rules
  * Changes are sent before the request that made them finishes (sync_now), and
    requests that would write subscriber data are refused with a 503 until the
    restore has finished, so no row can be written into a half-ready database.
  * Restore is all-or-nothing and never overwrites a table that already has rows.
  * Nothing is pushed until the start-up restore has finished, so an empty
    fresh database can never overwrite the saved copy.
  * A single sync will not delete more than half of a table's saved rows (once
    it holds 5 or more), which stops a wiped local table from erasing Turso.
  * The token is never logged; row contents are never logged.
"""
import base64
import hashlib
import json
import os
import sqlite3
import threading
import time

import requests

TABLES = (
    "alert_subscribers",
    "alert_locations",
    "dam_subscriptions",
    "dam_status",
    "telegram_subscribers",
    "telegram_locations",
    "contributions",
    "digest_sends",
)
SYNC_INTERVAL_SECONDS = 60
CHUNK = 40
RESTORE_ATTEMPTS = 3
REQUEST_TIMEOUT = 8
MASS_DELETE_MIN_ROWS = 5

_state = {
    "restored": False,
    "started": False,
    "last_sync_at": None,
    "last_error": None,
    "restored_rows": {},
    "remote_only": {},
    "pushed": {},
}
_lock = threading.RLock()
_wake = threading.Event()
_db_path = None


# ---------------------------------------------------------------------------
# Configuration and the HTTP API
# ---------------------------------------------------------------------------
def _cfg():
    url = os.getenv("TURSO_DATABASE_URL", "").strip()
    token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        return None
    for prefix in ("libsql://", "turso://"):
        if url.startswith(prefix):
            url = "https://" + url[len(prefix):]
    if not url.startswith("https://") and not url.startswith("http://"):
        url = "https://" + url
    return url.rstrip("/"), token


def enabled():
    return _cfg() is not None


def _encode(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode("ascii")}
    return {"type": "text", "value": str(value)}


def _decode(cell):
    kind = (cell or {}).get("type")
    if kind == "null" or kind is None:
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    if kind == "blob":
        return base64.b64decode(cell.get("base64", ""))
    return cell.get("value")


def _pipeline(statements, timeout=None):
    """Run statements [(sql, args), ...] in order; returns one result per statement."""
    cfg = _cfg()
    if not cfg:
        raise RuntimeError("Turso is not configured")
    url, token = cfg
    requests_body = [
        {"type": "execute", "stmt": {"sql": sql, "args": [_encode(a) for a in (args or [])]}}
        for sql, args in statements
    ]
    requests_body.append({"type": "close"})
    response = requests.post(
        url + "/v2/pipeline",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"requests": requests_body},
        timeout=timeout or REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    results = (response.json() or {}).get("results") or []
    out = []
    for item in results[: len(statements)]:
        if item.get("type") == "error":
            message = (item.get("error") or {}).get("message", "database error")
            raise RuntimeError(str(message)[:200])
        out.append(((item.get("response") or {}).get("result")) or {})
    if len(out) != len(statements):
        raise RuntimeError("incomplete answer from the database")
    return out


def _dict_rows(result):
    cols = [c.get("name") for c in result.get("cols") or []]
    return [dict(zip(cols, (_decode(c) for c in row))) for row in result.get("rows") or []]


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------
def _local_tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return [t for t in TABLES if t in have]
    finally:
        conn.close()


def _table_info(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    finally:
        conn.close()
    cols = [{"name": r[1], "type": r[2] or "", "pk": r[5]} for r in info]
    pk = next((c["name"] for c in cols if c["pk"] == 1), cols[0]["name"] if cols else None)
    return cols, pk, (sql[0] if sql else None)


def _read_local(db_path, table):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()


def _row_hash(row):
    return hashlib.md5(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Remote schema, restore, sync
# ---------------------------------------------------------------------------
def ensure_remote(db_path):
    for table in _local_tables(db_path):
        cols, _pk, sql = _table_info(db_path, table)
        if not sql:
            continue
        create = sql if "IF NOT EXISTS" in sql.upper() else sql.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1)
        _pipeline([(create, [])])
        have = {r["name"] for r in _dict_rows(_pipeline([(f"SELECT name FROM pragma_table_info('{table}')", [])])[0])}
        for col in cols:
            if col["name"] not in have:
                _pipeline([(f"ALTER TABLE {table} ADD COLUMN {col['name']} {col['type'] or ''}".strip(), [])])


def restore(db_path):
    """Refill tables that are empty locally from Turso, all-or-nothing.

    Local rows always win: a table that already holds rows is left alone, so a
    subscriber who was removed here is never brought back. Everything is read
    from Turso first and written in ONE local transaction, so a crash or a
    network error half-way leaves the database exactly as it was (empty), never
    half-restored. Returns {table: rows_restored}."""
    plan = {}
    for table in _local_tables(db_path):
        cols, pk, _ = _table_info(db_path, table)
        names = [c["name"] for c in cols]
        local = _read_local(db_path, table)
        remote = _dict_rows(_pipeline([(f"SELECT * FROM {table}", [])])[0])
        plan[table] = (names, pk, local, remote)

    counts = {}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN")
        for table, (names, pk, local, remote) in plan.items():
            usable = [n for n in names if remote and n in remote[0]]
            if local or not remote or not usable:
                counts[table] = 0
                continue
            for r in remote:
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({','.join(usable)}) VALUES ({','.join('?' * len(usable))})",
                    [r[n] for n in usable],
                )
            counts[table] = len(remote)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Baseline for later syncs = the saved rows this database also holds. Saved
    # rows it does NOT hold are left untouched in Turso (neither restored over
    # local data nor deleted), and are counted so you can see them.
    _state["remote_only"] = {}
    for table in plan:
        _cols, pk, _ = _table_info(db_path, table)
        have = {r[pk] for r in _read_local(db_path, table)}
        remote_rows = _dict_rows(_pipeline([(f"SELECT * FROM {table}", [])])[0])
        _state["pushed"][table] = {r[pk]: _row_hash(r) for r in remote_rows if r[pk] in have}
        _state["remote_only"][table] = sum(1 for r in remote_rows if r[pk] not in have)
    return counts


def sync(db_path):
    """Push rows that changed since the last sync. Returns the number of statements sent."""
    if not _state["restored"]:
        return 0
    sent = 0
    for table in _local_tables(db_path):
        cols, pk, _ = _table_info(db_path, table)
        names = [c["name"] for c in cols]
        local = {r[pk]: r for r in _read_local(db_path, table)}
        pushed = _state["pushed"].setdefault(table, {})
        gone = [k for k in pushed if k not in local]
        if len(pushed) >= MASS_DELETE_MIN_ROWS and len(gone) > len(pushed) / 2:
            _state["last_error"] = f"refused to delete {len(gone)} of {len(pushed)} saved rows in {table}"
            print(f"Persistence: {_state['last_error']}")
            gone = []
            local_changed = {k: r for k, r in local.items() if pushed.get(k) != _row_hash(r)}
        else:
            local_changed = {k: r for k, r in local.items() if pushed.get(k) != _row_hash(r)}
        statements, applied = [], []
        for key, row in local_changed.items():
            statements.append((
                f"INSERT OR REPLACE INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                [row[n] for n in names],
            ))
            applied.append(("up", key, _row_hash(row)))
        for key in gone:
            statements.append((f"DELETE FROM {table} WHERE {pk} = ?", [key]))
            applied.append(("del", key, None))
        for i in range(0, len(statements), CHUNK):
            _pipeline(statements[i:i + CHUNK])
            for kind, key, digest in applied[i:i + CHUNK]:
                if kind == "up":
                    pushed[key] = digest
                else:
                    pushed.pop(key, None)
            sent += len(statements[i:i + CHUNK])
    return sent


def _run_sync_once():
    with _lock:
        try:
            sync(_db_path)
            _state["last_sync_at"] = time.time()
            if not _state["last_error"] or not str(_state["last_error"]).startswith("refused"):
                _state["last_error"] = None
        except Exception as error:  # noqa: BLE001 - a sync problem must never break the site
            _state["last_error"] = str(error)[:200]
            print(f"Persistence sync failed: {_state['last_error']}")


def _loop():
    while True:
        _wake.wait(timeout=SYNC_INTERVAL_SECONDS)
        _wake.clear()
        if _state["restored"]:
            _run_sync_once()
        else:
            _try_restore()


def _try_restore():
    with _lock:
        if _state["restored"]:
            return True
        try:
            ensure_remote(_db_path)
            _state["restored_rows"] = restore(_db_path)
            _state["restored"] = True
            print(f"Persistence: restored {sum(_state['restored_rows'].values())} saved row(s).")
            return True
        except Exception as error:  # noqa: BLE001
            _state["last_error"] = str(error)[:200]
            print(f"Persistence restore failed: {_state['last_error']}")
            return False


def start(db_path):
    """Call once at start-up, after the local tables exist and before the app serves traffic."""
    global _db_path
    if not enabled() or _state["started"]:
        return False
    _db_path = db_path
    _state["started"] = True
    for attempt in range(RESTORE_ATTEMPTS):
        if _try_restore():
            break
        time.sleep(2)
    threading.Thread(target=_loop, daemon=True, name="persistence-sync").start()
    return True


def sync_now():
    """Push pending changes before returning (used by the request that made
    them, so an unsubscribe or /stop is in Turso before the user sees "done").
    Returns True when everything was sent. Does nothing, quickly, when there is
    nothing to send or Turso is not set up."""
    if not (_state["started"] and _state["restored"]):
        return False
    _run_sync_once()
    return _state["last_error"] is None


def ready():
    """True when saved data can be trusted to be loaded (or Turso is not used)."""
    return (not _state["started"]) or _state["restored"]


def request_sync():
    """Ask for a sync right away (cheap; does nothing when Turso is not configured)."""
    if _state["started"]:
        _wake.set()


def status():
    return {
        "enabled": enabled(),
        "started": _state["started"],
        "restored": _state["restored"],
        "restored_rows": dict(_state["restored_rows"]),
        "saved_rows": {t: len(v) for t, v in _state["pushed"].items()},
        "saved_rows_not_on_this_server": {t: n for t, n in _state["remote_only"].items() if n},
        "last_sync_at": _state["last_sync_at"],
        "last_error": _state["last_error"],
    }