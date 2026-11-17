"""Complete database export formats and backup validation (server side only).

All formats are built from the same snapshot that backup.build() makes (every table, every
row, original IDs, documents included, password hashes left out), so JSON, CSV and Excel always
agree with each other.

Restoring is never done over live data from here: validate() checks a backup on its own and
compare() lists what the live database is missing, without writing anything. See restore_backup.py
for putting a backup into a separate, empty database.
"""
import base64
import csv
import gzip
import hashlib
import io
import json
import zipfile

CSV_PREFIX = "tooba-db"
XLSX_CELL_MAX = 32000


def load(data):
    """A backup file (.json.gz, .json or the weekly .zip) -> payload dict."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    elif data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next((n for n in z.namelist() if n.endswith("database.json.gz")), None)
            if name:
                return load(z.read(name))
            name = next(n for n in z.namelist() if n.endswith(".json"))
            data = z.read(name)
    payload = json.loads(data.decode("utf-8"))
    if "tables" not in payload:
        raise ValueError("Not a complete Tooba database backup (no 'tables').")
    return payload


def _cell(v):
    if isinstance(v, dict) and set(v) == {"base64"}:
        return f"<{len(base64.b64decode(v['base64']))} bytes — see JSON backup>"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return "" if v is None else v


def csv_zip(payload):
    """One CSV per table (UTF-8 with BOM so Excel shows Urdu correctly), zipped."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for table, rows in payload["tables"].items():
            buf = io.StringIO()
            cols = list(rows[0].keys()) if rows else []
            w = csv.writer(buf)
            w.writerow(cols)
            for r in rows:
                w.writerow([_safe(_cell(r.get(c))) for c in cols])
            z.writestr(f"{CSV_PREFIX}-{table}.csv", "﻿" + buf.getvalue())
        z.writestr("README.txt", "Tooba Foundation Loan Department — database export.\n"
                                 f"Created {payload.get('created_at')}. One CSV per table; the id columns are the "
                                 "original record IDs. File contents are only in the JSON backup.\n")
    return out.getvalue()


def _safe(v):
    """Stop spreadsheet formula injection from typed values."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") and not _number(v):
        return "'" + v
    return v


def _number(v):
    try:
        float(v)
        return True
    except ValueError:
        return False


def xlsx(payload):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    summary = wb.create_sheet("Summary")
    summary.append(["Table", "Rows"])
    for c in summary[1]:
        c.font = Font(bold=True)
    for table, rows in payload["tables"].items():
        summary.append([table, len(rows)])
        ws = wb.create_sheet(table[:31])
        cols = list(rows[0].keys()) if rows else []
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = Font(bold=True), PatternFill("solid", fgColor="ECF7EF")
        for r in rows:
            vals = []
            for c in cols:
                v = _safe(_cell(r.get(c)))
                vals.append(v[:XLSX_CELL_MAX] if isinstance(v, str) else v)
            ws.append(vals)
        ws.freeze_panes = "A2"
    summary.append([])
    summary.append(["Created", payload.get("created_at")])
    summary.append(["Note", payload.get("note")])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _fk_map(metadata):
    """{table: [(column, parent_table, parent_column)]} from the SQLAlchemy models."""
    out = {}
    for t in metadata.sorted_tables:
        for fk in t.foreign_keys:
            out.setdefault(t.name, []).append((fk.parent.name, fk.column.table.name, fk.column.name))
    return out


def validate(payload, metadata):
    """Check a backup on its own: row counts, unique IDs, relationships, document checksums.
    Returns {"ok": bool, "problems": [...], "counts": {...}}."""
    problems = []
    tables = payload.get("tables") or {}
    declared = payload.get("counts") or {}
    counts = {t: len(rows) for t, rows in tables.items()}
    for t, n in declared.items():
        if counts.get(t) != n:
            problems.append(f"{t}: backup says {n} rows but contains {counts.get(t)}")
    ids = {}
    for t, rows in tables.items():
        if rows and "id" in rows[0]:
            seen = [r["id"] for r in rows]
            if len(seen) != len(set(seen)):
                problems.append(f"{t}: duplicate IDs")
            ids[t] = set(seen)
    for t, fks in _fk_map(metadata).items():
        for col, parent, pcol in fks:
            if t not in tables or parent not in ids or pcol != "id":
                continue
            bad = [r.get("id") for r in tables[t] if r.get(col) is not None and r[col] not in ids[parent]]
            if bad:
                problems.append(f"{t}.{col}: {len(bad)} row(s) point to missing {parent} (e.g. id {bad[0]})")
    for d in tables.get("document", []):
        data = d.get("data")
        if isinstance(data, dict) and "base64" in data and d.get("sha256"):
            if hashlib.sha256(base64.b64decode(data["base64"])).hexdigest() != d["sha256"]:
                problems.append(f"document {d.get('id')}: file bytes do not match its SHA-256")
    for a in tables.get("application", []):
        try:
            json.loads(a.get("form_json") or "{}")
        except ValueError:
            problems.append(f"application {a.get('id')}: form data is not valid JSON")
    return {"ok": not problems, "problems": problems, "counts": counts}


def compare(payload, live_ids):
    """live_ids: {table: set(ids) in the live database}. Lists, per table, the backup rows that
    the live database does not have (by original ID). Writes nothing."""
    out = {}
    for t, rows in (payload.get("tables") or {}).items():
        if not rows or "id" not in rows[0] or t not in live_ids:
            continue
        missing = sorted(r["id"] for r in rows if r["id"] not in live_ids[t])
        out[t] = {"backup": len(rows), "live": len(live_ids[t]), "missing_in_live": missing}
    return out


# ---------------------------------------------------------------- restore into a SEPARATE database
SEED_TABLES = {"role_permission", "status_transition", "purpose_category", "fee_category", "app_id_counter"}


def _typed(col, v):
    import datetime as dt
    import decimal
    from sqlalchemy import Date, DateTime, LargeBinary, Numeric
    if v is None:
        return None
    if isinstance(v, dict) and set(v) == {"base64"}:
        return base64.b64decode(v["base64"])
    t = col.type
    if isinstance(t, LargeBinary) and isinstance(v, str):
        return base64.b64decode(v)
    if isinstance(t, DateTime) and isinstance(v, str):
        return dt.datetime.fromisoformat(v.replace("Z", ""))
    if isinstance(t, Date) and isinstance(v, str):
        return dt.date.fromisoformat(v[:10])
    if isinstance(t, Numeric) and isinstance(v, str):
        return decimal.Decimal(v)
    return v


def restore_into(conn, payload, metadata, unusable_password_hash):
    """Copy every backup row into an EMPTY database, keeping the original IDs and relationships.

    `conn` is a SQLAlchemy connection to the separate (test / recovery) database, already in a
    transaction. Refuses to run if any data table there already has rows, so it can never write
    over real data. Users get an unusable password (backups never hold password hashes); the
    Admin sets new ones after the restore. Returns {table: rows restored}."""
    from sqlalchemy import func, insert, select, text, delete
    tables = payload["tables"]
    by_name = {t.name: t for t in metadata.sorted_tables}
    busy = [t for t in by_name if t not in SEED_TABLES
            and conn.execute(select(func.count()).select_from(by_name[t])).scalar()]
    if busy:
        raise RuntimeError("Target database is not empty (" + ", ".join(busy) + "). Restore only into a new, "
                           "separate database — never over live data.")
    pg = conn.dialect.name == "postgresql"
    if pg:
        conn.execute(text("set local session_replication_role = replica"))   # skip triggers (owner only)
    done = {}
    for t in metadata.sorted_tables:
        rows = tables.get(t.name)
        if rows is None:
            continue
        if t.name in SEED_TABLES:
            conn.execute(delete(t))
        cols = {c.name: c for c in t.columns}
        batch = []
        for r in rows:
            row = {k: _typed(cols[k], v) for k, v in r.items() if k in cols}
            if "password_hash" in cols and not row.get("password_hash"):
                row["password_hash"] = unusable_password_hash
            batch.append(row)
        for i in range(0, len(batch), 500):
            conn.execute(insert(t), batch[i:i + 500])
        done[t.name] = len(batch)
        if pg and "id" in cols and batch:
            conn.execute(text(f"select setval(pg_get_serial_sequence('public.\"{t.name}\"', 'id'), "
                              f"(select max(id) from public.\"{t.name}\"))"))
    for name, n in done.items():
        got = conn.execute(select(func.count()).select_from(by_name[name])).scalar()
        if got != n:
            raise RuntimeError(f"{name}: restored {got} rows, expected {n}")
    return done
