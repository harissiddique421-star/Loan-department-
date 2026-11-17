"""Google Drive archive: filled Word forms, versions, documents, daily/weekly backups,
retry after a failed upload, verification and the restore check. Uses a fake Drive."""
import gzip
import hashlib
import io
import json
import os
import zipfile

import pytest
from conftest import appmod, new_app, upload_kind, TINY_JPG

import word_form
import db_export


class FakeDrive:
    """In-memory Drive with the same methods as drive_archive.ArchiveDrive."""

    def __init__(self):
        self.files, self.n, self.fail_next = {}, 0, 0
        self.revisions = {}

    def _id(self):
        self.n += 1
        return f"f{self.n}"

    def _maybe_fail(self):
        if self.fail_next:
            self.fail_next -= 1
            raise appmod.drive_archive.DriveError("Drive is down (simulated)", 503)

    def ensure_folder(self, name, parent=None):
        for fid, f in self.files.items():
            if f["name"] == name and f.get("parent") == parent and f["folder"] and not f.get("trashed"):
                return fid
        fid = self._id()
        self.files[fid] = {"id": fid, "name": name, "parent": parent, "folder": True}
        return fid

    def _meta(self, f):
        return {"id": f["id"], "name": f["name"], "md5Checksum": hashlib.md5(f["data"]).hexdigest(),
                "size": str(len(f["data"])), "trashed": f.get("trashed", False), "appProperties": f.get("props"),
                "parents": [f["parent"]]}

    def upload(self, name, data, mime, parent, props=None, lock=False):
        self._maybe_fail()
        fid = self._id()
        self.files[fid] = {"id": fid, "name": name, "parent": parent, "folder": False, "data": data, "mime": mime,
                           "props": {k: str(v) for k, v in (props or {}).items()}, "locked": lock}
        return self._meta(self.files[fid])

    def update(self, file_id, data, mime, name=None):
        self._maybe_fail()
        f = self.files[file_id]
        self.revisions.setdefault(file_id, []).append(f["data"])
        f["data"] = data
        if name:
            f["name"] = name
        return self._meta(f)

    def get(self, file_id):
        f = self.files.get(file_id)
        return None if f is None else (self._meta(f) if not f["folder"] else {"id": file_id, "trashed": False})

    def list_archive(self):
        return [self._meta(f) for f in self.files.values()
                if not f["folder"] and not f.get("trashed") and (f.get("props") or {}).get("tooba") == "1"]

    def rename(self, file_id, name):
        self.files[file_id]["name"] = name
        return {"id": file_id, "name": name}

    def move(self, file_id, new_parent):
        self.files[file_id]["parent"] = new_parent

    def trash(self, file_id):
        self.files[file_id]["trashed"] = True

    def children(self, parent):
        return [{"id": fid, "name": f["name"],
                 "mimeType": "application/vnd.google-apps.folder" if f["folder"] else f["mime"]}
                for fid, f in self.files.items() if f.get("parent") == parent and not f.get("trashed")]

    def kind(self, kind):
        return [f for f in self.files.values() if not f["folder"] and not f.get("trashed")
                and (f.get("props") or {}).get("kind") == kind]

    def path(self, f):
        parts, p = [f["name"]], f["parent"]
        while p:
            parts.append(self.files[p]["name"])
            p = self.files[p].get("parent")
        return "/".join(reversed(parts))

    def in_folder(self, name):
        ids = {fid for fid, f in self.files.items() if f["folder"] and f["name"] == name}
        out = []
        for f in self.files.values():
            if f["folder"]:
                continue
            p = f["parent"]
            while p:
                if p in ids:
                    out.append(f)
                    break
                p = self.files[p].get("parent")
        return out


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(appmod, "ARCHIVE_CLIENT", fake)
    import conftest
    if conftest.PG:               # the app role itself may never delete archive rows
        with conftest.pg_admin(conftest.DBNAME) as c:
            c.execute("delete from archive_setting; delete from archive_file")
    else:
        with appmod.app.app_context():
            appmod.ArchiveSetting.query.delete()
            appmod.ArchiveFile.query.delete()
            appmod.db.session.commit()
    return fake


def docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("word/document.xml").decode()


def test_new_application_is_archived_as_filled_word_form(staff, drive):
    pk = new_app(staff, name="Archive Person", cnic="4210111111111", father="Karim Bux", g1name="محمد اسلم",
                 amount="40000", address="Orangi Town")
    forms = drive.kind("form")
    assert len(forms) == 1
    f = forms[0]
    with appmod.app.app_context():
        app_id = appmod.db.session.get(appmod.Application, pk).app_id
    assert f["name"].startswith(app_id) and f["name"].endswith(".docx")
    xml = docx_text(f["data"])
    for v in ("Archive Person", "Karim Bux", "محمد اسلم", "Orangi Town", "40000", app_id):
        assert v in xml
    assert word_form.verify(f["data"], app_id, {"name": "Archive Person"}) == []
    # every saved step is its own version file, never overwritten
    versions = drive.kind("version")
    assert len(versions) >= 2 and len({v["name"] for v in versions}) == len(versions)
    assert drive.kind("guarantor")
    # the 6 CNIC pictures are in Supporting Documents, byte for byte
    docs = drive.kind("document")
    assert len(docs) == 6 and all(d["data"] == TINY_JPG for d in docs)
    # simple layout: everything about one applicant in one folder
    root = appmod.drive_archive.ROOT_NAME
    who = f"{root}/Applicants/{app_id} - Archive Person"
    assert drive.path(f) == f"{who}/{f['name']}"
    assert drive.path(drive.kind("guarantor")[0]).startswith(who + "/")
    assert all(drive.path(v).startswith(who + "/Old Versions/") for v in versions)
    assert all(drive.path(d).startswith(who + "/Documents/") for d in docs)
    top = {x["name"] for x in drive.files.values() if x["folder"] and drive.files.get(x.get("parent") or "", {}).get("name") == root}
    assert top == {"Applicants", "Daily Backups", "Weekly Backups"}
    with appmod.app.app_context():
        st = appmod.archive_state()
        assert pk in st["backed_up"] and pk not in st["pending"]


def test_edit_keeps_history_and_updates_current_form(staff, drive):
    pk = new_app(staff, name="Edit Person", cnic="4210122222222")
    form = drive.kind("form")[0]
    n_versions = len(drive.kind("version"))
    r = staff.post(f"/application/{pk}/form", data={"name": "Edit Person", "cnic": "4210122222222",
                                                   "amount": "50000", "address": "New Address 9"})
    assert r.status_code == 302
    assert len(drive.kind("version")) == n_versions + 1
    assert "New Address 9" in docx_text(drive.files[form["id"]]["data"])
    assert drive.revisions[form["id"]]                       # earlier content kept as a revision
    assert len(drive.kind("form")) == 1
    # nothing changed -> nothing new uploaded
    with appmod.app.app_context():
        s = appmod.run_archive(budget=30, only=[pk])
    assert s["unchanged"] == 1 and s["archived"] == 0 and s["failed"] == 0


def test_failed_upload_is_recorded_and_retried(staff, drive, monkeypatch):
    drive.fail_next = 100                                    # Drive down while staff saves
    pk = new_app(staff, name="Retry Person", cnic="4210133333333", complete=False)
    assert staff.get(f"/application/{pk}").status_code == 200     # the website keeps working
    with appmod.app.app_context():
        st = appmod.archive_state()
        assert pk in st["failed"]
        assert appmod.ArchiveFile.query.filter_by(application_id=pk, status="failed").count() >= 1
    drive.fail_next = 0
    os.environ["CRON_SECRET"] = "cron-test"
    c = appmod.app.test_client()
    assert c.get("/cron/archive").status_code == 401
    r = c.get("/cron/archive", headers={"Authorization": "Bearer cron-test"})
    body = r.get_json()
    assert body["archived"] >= 1 and body["failed"] == 0
    with appmod.app.app_context():
        st = appmod.archive_state()
        assert pk in st["backed_up"] and pk not in st["failed"]


def test_verification_finds_missing_and_changed_files(staff, drive):
    pk = new_app(staff, name="Verify Person", cnic="4210144444444", complete=False)
    with appmod.app.app_context():
        rep = appmod.verify_archive()
    assert rep["missing"] == [] and rep["corrupt"] == []
    form = drive.kind("form")[0]
    drive.files[form["id"]]["data"] = b"tampered"
    doc_versions = drive.kind("version")
    drive.files[doc_versions[0]["id"]]["trashed"] = True
    with appmod.app.app_context():
        rep = appmod.verify_archive()
        assert any(form["name"] == n for n in rep["corrupt"])
        assert rep["missing"] and not rep["ok"]
        assert pk in appmod.archive_state()["failed"]
        s = appmod.run_archive(budget=30)                   # repaired on the next run
        assert s["failed"] == 0
        rep = appmod.verify_archive()
        assert rep["corrupt"] == []
    assert drive.kind("report")


def test_daily_and_weekly_backup_and_dashboard(admin, staff, drive):
    new_app(staff, name="Backup Person", cnic="4210155555555", complete=False)
    r = admin.post("/admin/backup-recovery/run", data={"action": "backup"})
    assert r.status_code == 302
    daily = drive.in_folder("Daily Backups")
    names = sorted(f["name"] for f in daily)
    assert any(n.endswith(".json.gz") for n in names) and any(n.endswith("CSV.zip") for n in names) \
        and any(n.endswith(".xlsx") for n in names)
    js = next(f for f in daily if f["name"].endswith(".json.gz"))
    payload = db_export.load(js["data"])
    assert any(a["applicant_name"] == "Backup Person" for a in payload["tables"]["application"])
    assert all("password_hash" not in u for u in payload["tables"]["user"])
    wf = next(f for f in daily if f["name"].startswith("Tooba Word Forms"))
    with zipfile.ZipFile(io.BytesIO(wf["data"])) as z:
        forms = [n for n in z.namelist() if n.endswith(".docx") and not n.startswith("Guarantors/")]
        hit = next(n for n in forms if "Backup Person" in n)
        assert not word_form.verify(z.read(hit), hit.split(" - ")[0], {"name": "Backup Person"})
        assert any(n.startswith("Guarantors/") for n in z.namelist())
    new_app(staff, name="Second Person", cnic="4210166666666", complete=False)
    admin.post("/admin/backup-recovery/run", data={"action": "backup"})     # "latest" files updated in place
    with appmod.app.app_context():
        rep = appmod.verify_archive()
    assert rep["corrupt"] == [] and rep["missing"] == [] and rep["ok"]
    daily = drive.in_folder("Daily Backups")
    assert all(f["locked"] for f in daily)                       # dated copies are read-only in Drive
    assert not any(f["locked"] for f in drive.kind("form"))       # updated in place by the app
    xl = next(f for f in daily if f["name"].endswith(".xlsx"))
    drive.files[xl["id"]]["data"] = b"opened and saved in Drive"   # owner unlocked and edited it
    with appmod.app.app_context():
        rep = appmod.verify_archive()
    assert rep["edited_database_copies"] == [xl["name"]] and rep["corrupt"] == [] and rep["ok"]
    r = admin.post("/admin/backup-recovery/run", data={"action": "weekly"})
    wk = drive.in_folder("Weekly Backups")
    assert len(wk) == 1 and wk[0]["name"].endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(wk[0]["data"])) as z:
        assert "database.json.gz" in z.namelist() and "archive-manifest.json" in z.namelist()
        assert "Word Forms.zip" in z.namelist()
    page = admin.get("/admin/backup-recovery")
    assert page.status_code == 200 and b"Backup Now" in page.data and b"Connected" in page.data
    assert staff.get("/admin/backup-recovery").status_code in (302, 403)


def test_download_complete_backup_and_restore_check(admin, staff, drive):
    new_app(staff, name="Restore Person", cnic="4210166666666", complete=False)
    r = admin.get("/admin/backup-recovery/download")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        data = z.read("database.json.gz")
    payload = db_export.load(data)
    assert db_export.validate(payload, appmod.db.metadata)["ok"]
    # remove one application from the copy's "live" view: the check lists it as missing
    r = admin.post("/admin/backup-recovery/restore",
                   data={"backup": (io.BytesIO(data), "backup.json.gz")}, content_type="multipart/form-data")
    assert r.status_code == 200 and "Backup sahih hai".encode() in r.data
    # a damaged backup is caught
    payload["tables"]["document"].append(dict(payload["tables"]["document"][0], id=999999, application_id=888888)) \
        if payload["tables"]["document"] else payload["tables"]["application"].append(
            dict(payload["tables"]["application"][0]))
    bad = gzip.compress(json.dumps(payload).encode())
    r = admin.post("/admin/backup-recovery/restore",
                   data={"backup": (io.BytesIO(bad), "bad.json.gz")}, content_type="multipart/form-data")
    assert "Backup mein masle".encode() in r.data


def test_word_form_download(admin, staff, drive):
    pk = new_app(staff, name="Download Person", cnic="4210177777777", complete=False)
    r = admin.get(f"/admin/backup-recovery/form/{pk}.docx")
    assert r.status_code == 200 and "Download Person" in docx_text(r.data)


def test_restore_into_separate_empty_database(tmp_path, staff, drive):
    """Full restore into a new SQLite database: every row, original IDs, relationships."""
    new_app(staff, name="Separate Env", cnic="4210188888888")
    with appmod.app.app_context():
        data, counts = appmod.backup.build(appmod.db.session, appmod.db.metadata)
    payload = db_export.load(data)
    from sqlalchemy import create_engine, select, func
    eng = create_engine(f"sqlite:///{tmp_path}/restored.db")
    appmod.db.metadata.create_all(eng)
    with eng.begin() as conn:
        done = db_export.restore_into(conn, payload, appmod.db.metadata, "x")
    assert done["application"] == counts["application"]
    with eng.connect() as conn:
        t = appmod.db.metadata.tables["application"]
        names = {r.applicant_name for r in conn.execute(select(t))}
        assert "Separate Env" in names
        ids = sorted(r.id for r in conn.execute(select(t.c.id)))
        assert ids == sorted(a["id"] for a in payload["tables"]["application"])
    # never over existing data
    with pytest.raises(RuntimeError):
        with eng.begin() as conn:
            db_export.restore_into(conn, payload, appmod.db.metadata, "x")


def test_rest_client_requests(monkeypatch):
    """The real Drive client: multipart and resumable uploads, revisions kept, retry on 503."""
    import drive_archive as da
    calls = []

    class Resp:
        def __init__(self, code, body=None, headers=None):
            self.status_code, self._b, self.headers = code, body or {}, headers or {}
            self.content = json.dumps(self._b).encode()
            self.text = self.content.decode()

        def json(self):
            return self._b

    plan = [Resp(503, {"error": "busy"}), Resp(200, {"id": "x1", "md5Checksum": "m", "size": "3"})]

    class Sess:
        def request(self, method, url, timeout=None, **kw):
            calls.append((method, url, kw))
            if plan:
                return plan.pop(0)
            if "uploadType=resumable" in url:
                return Resp(200, {}, {"Location": "https://upload.example/session"})
            return Resp(200, {"id": "x2", "md5Checksum": "m", "size": "1"})

    monkeypatch.setattr(da.time, "sleep", lambda s: None)
    c = da.ArchiveDrive(refresh_token="t")
    c._session = Sess()
    meta = c.upload("a.docx", b"abc", da.DOCX_MIME, "folder1", {"tooba": "1", "app": 5})
    assert meta["id"] == "x1" and len(calls) == 2                      # retried once after 503
    body = calls[-1][2]["data"]
    assert b'"parents": ["folder1"]' in body and b'"app": "5"' in body and b"abc" in body
    c.update("x1", b"new", da.DOCX_MIME)
    assert "keepRevisionForever=true" in calls[-1][1]
    c.upload("locked.json", b"{}", "application/json", "folder1", lock=True)
    assert b'"contentRestrictions": [{"readOnly": true' in calls[-1][2]["data"]
    plan.extend([Resp(200, {"parents": ["oldf"]}), Resp(200, {"id": "x1"})])
    c.move("x1", "newf")
    assert calls[-1][0] == "PATCH" and "addParents=newf" in calls[-1][1] and "removeParents=oldf" in calls[-1][1]
    c.trash("x1")
    assert calls[-1][2]["json"] == {"trashed": True}
    big = b"0" * (da.RESUMABLE_OVER + 1)
    c.upload("big.zip", big, "application/zip", "folder1")
    assert calls[-2][1].startswith(da.UPLOAD) and "resumable" in calls[-2][1]
    assert calls[-1][1] == "https://upload.example/session" and calls[-1][2]["data"] == big
    plan.append(Resp(403, {"error": {"message": "Service Accounts do not have storage quota"}}))
    with pytest.raises(da.DriveError) as e:
        c.upload("a.docx", b"abc", da.DOCX_MIME, "folder1")
    assert "Connect the owner's Google account" in str(e.value)
    sealed = da.seal("refresh-123", "k")
    assert "refresh-123" not in sealed and da.unseal(sealed, "k") == "refresh-123" and da.unseal(sealed, "other") is None


def test_old_folder_layout_is_tidied_without_losing_files(staff, drive):
    """An archive made with the older 8-folder layout is moved into Applicants / Daily Backups once."""
    pk = new_app(staff, name="Old Layout", cnic="4210177777777")
    root = drive.ensure_folder(appmod.drive_archive.ROOT_NAME)
    old = {n: drive.ensure_folder(n, root) for n in appmod.drive_archive.OLD_FOLDERS + ["Daily Backups", "Weekly Backups"]}
    monkey_old = {"form": "Individual Applicant Forms", "guarantor": "Guarantor Records",
                  "version": "Updated Application Versions"}
    with appmod.app.app_context():
        appmod.set_archive_setting("folders", json.dumps(dict(old, _root=root)))
        appmod.db.session.commit()
        a = appmod.db.session.get(appmod.Application, pk)
        app_id = a.app_id
        # put the files where the old layout had them, and add an old 'latest' database copy and a report
        for r in appmod.ArchiveFile.query.filter_by(application_id=pk):
            drive.files[r.drive_file_id]["parent"] = old[monkey_old.get(r.category, "Supporting Documents")]
            r.drive_folder_id = old[monkey_old.get(r.category, "Supporting Documents")]
        latest = drive.upload("Tooba Database - latest.xlsx", b"x", "a", old["Complete Database Backup"], {"tooba": "1"})
        rep = drive.upload("Verification Report 2026-11-17 2155.json", b"{}", "a", old["Backup Reports"], {"tooba": "1"})
        appmod.db.session.add_all([
            appmod.ArchiveFile(category="database", name="Tooba Database - latest.xlsx", status="verified",
                               drive_file_id=latest["id"], md5=latest["md5Checksum"]),
            appmod.ArchiveFile(category="report", name=rep["name"], status="verified", drive_file_id=rep["id"],
                               md5=rep["md5Checksum"])])
        appmod.db.session.commit()
        n_files = sum(1 for f in drive.files.values() if not f["folder"] and not f.get("trashed"))
        folders = appmod.archive_folders(drive, check=True)
        assert "_old" not in folders
        alive = [f for f in drive.files.values() if not f["folder"] and not f.get("trashed")]
        assert len(alive) == n_files - 1                                  # only the 'latest' duplicate is binned
        assert drive.files[latest["id"]]["trashed"]
        assert drive.path(drive.files[rep["id"]]).endswith("Daily Backups/2026-11-17/" + rep["name"])
        who = f"Applicants/{app_id} - Old Layout"
        for f in alive:
            if (f.get("props") or {}).get("app") == str(pk):
                assert who in drive.path(f)
        for n in appmod.drive_archive.OLD_FOLDERS:                        # emptied old folders are binned
            assert drive.files[old[n]].get("trashed")
        assert not drive.files[old["Daily Backups"]].get("trashed")
        rep2 = appmod.verify_archive()
        assert rep2["missing"] == [] and rep2["corrupt"] == []
