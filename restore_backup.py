"""Restore a Tooba backup into a SEPARATE, empty database (test or recovery environment).

    python restore_backup.py BACKUP_FILE TARGET_DATABASE_URL [--check-only]

BACKUP_FILE: "Tooba ... .json.gz" from Google Drive (Daily / Weekly / Complete Database Backup)
             or the weekly .zip.
TARGET_DATABASE_URL: a NEW database. For PostgreSQL, first run supabase/schema.sql and every file in
             supabase/migrations on it, and connect as its owner (postgres). The script refuses to
             write if any data table there already has rows, so it can never overwrite live data.

Steps: 1) validate the backup (counts, IDs, relationships, document checksums), 2) copy every row
with its original ID, 3) re-count every table. Users get unusable passwords; set new ones as
Admin afterwards. Only switch the website to the restored database after checking it.
"""
import os
import secrets
import sys


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    path, target = argv[1], argv[2]
    check_only = "--check-only" in argv
    live = os.getenv("DATABASE_URL", "")
    if live and target.split("@")[-1] == live.split("@")[-1]:
        print("Refusing: the target is the same database as DATABASE_URL (the live site).")
        return 2
    os.environ["DATABASE_URL"] = target
    os.environ.pop("ADMIN_PASSWORD", None)          # do not create an admin before restoring
    import db_export
    with open(path, "rb") as fh:
        payload = db_export.load(fh.read())
    import app as appmod                            # models only; connects to the TARGET database
    report = db_export.validate(payload, appmod.db.metadata)
    print("Backup created:", payload.get("created_at"))
    for t, n in sorted(report["counts"].items()):
        print(f"  {t:22} {n}")
    if not report["ok"]:
        print("Backup has problems:\n  " + "\n  ".join(report["problems"]))
        return 1
    print("Backup is valid.")
    if check_only:
        return 0
    from werkzeug.security import generate_password_hash
    with appmod.app.app_context(), appmod.db.engine.begin() as conn:
        done = db_export.restore_into(conn, payload, appmod.db.metadata,
                                      generate_password_hash(secrets.token_hex(32)))
    print("Restored and re-counted:", ", ".join(f"{k}={v}" for k, v in done.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
