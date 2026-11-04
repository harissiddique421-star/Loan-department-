"""Full database backup as gzip-compressed JSON (every table, documents included).

Password hashes are left out on purpose: a leaked backup must not let anyone crack the short
login passwords. After a restore, the Admin sets new passwords for the users.
"""
import base64
import datetime as dt
import decimal
import gzip
import json

from sqlalchemy import select

SKIP_COLUMNS = {("user", "password_hash")}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def file_name(day):
    """The 7 rotating backup files in the Drive folder, one per weekday."""
    return f"Tooba Backup - {DAYS[day.weekday()]}.json.gz"


def _plain(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    return v


def build(session, metadata, now=None):
    """Returns (gzip bytes, {table: row count})."""
    out, counts, skipped = {}, {}, []
    for table in metadata.sorted_tables:
        cols = [c for c in table.columns if (table.name, c.name) not in SKIP_COLUMNS]
        try:
            with session.begin_nested():       # a table the app may not read (e.g. the ID counter,
                rows = session.execute(        # which a restore rebuilds from the IDs) is skipped
                    select(*cols).order_by(*table.primary_key.columns)).all()
        except Exception as exc:
            if "permission denied" not in str(exc):
                raise
            skipped.append(table.name)
            continue
        out[table.name] = [{c.name: _plain(v) for c, v in zip(cols, r)} for r in rows]
        counts[table.name] = len(rows)
    payload = {"created_at": (now or dt.datetime.utcnow()).isoformat() + "Z",
               "note": "Tooba Foundation Loan Department backup. Password hashes are not included.",
               "counts": counts, "skipped_tables": skipped, "tables": out}
    return gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"), 9), counts
