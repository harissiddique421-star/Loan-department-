import gzip, io, json, os
import pytest
from conftest import new_app, appmod


class FakeDrive:
    def __init__(self, fail=False):
        self.files, self.fail = {}, fail

    def put_file(self, name, data, mime="application/gzip"):
        if self.fail:
            raise RuntimeError("Drive is down")
        self.files[name] = data
        return "file-id"


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(appmod.alerts, "configured", lambda: True)
    monkeypatch.setattr(appmod.alerts, "send", lambda subject, body: sent.append((subject, body)) or True)
    with appmod.app.app_context():
        appmod.Log.query.filter(appmod.Log.action.like("ALERT %")).count()   # warm up
    appmod._alert_mem.clear()
    return sent


def test_full_excel_export(admin, staff):
    from openpyxl import load_workbook
    new_app(staff, name="=HYPERLINK(\"http://evil\")", cnic="4210188800001")
    assert staff.get("/admin/export.xlsx").status_code == 403
    r = admin.get("/admin/export.xlsx")
    assert r.status_code == 200 and "tooba-all-data-" in r.headers["Content-Disposition"]
    wb = load_workbook(io.BytesIO(r.data))
    assert wb.sheetnames == ["Applications", "Disbursements", "Users"]
    names = [row[3] for row in wb["Applications"].iter_rows(min_row=2, values_only=True)]
    assert "'=HYPERLINK(\"http://evil\")" in names            # formula injection neutralised
    users = [row[0] for row in wb["Users"].iter_rows(min_row=2, values_only=True)]
    assert "admin" in users


def test_drive_backup_now_and_cron(admin, staff, monkeypatch, mail):
    fake = FakeDrive()
    monkeypatch.setattr(appmod, "GOOGLE_CLIENT", fake)
    new_app(staff, name="Backup Person", cnic="4210188800002")
    assert staff.post("/admin/backup/drive").status_code == 403
    admin.post("/admin/backup/drive")
    (name, data), = fake.files.items()
    assert name.startswith("Tooba Backup - ") and name.endswith(".json.gz")
    dump = json.loads(gzip.decompress(data))
    tables = dump["tables"]
    assert any(a["applicant_name"] == "Backup Person" for a in tables["application"])
    assert tables["document"] and any("base64" in (d["data"] or {}) for d in tables["document"] if d["data"])
    assert tables["user"] and all("password_hash" not in u for u in tables["user"])
    assert dump["counts"]["application"] == len(tables["application"])
    # the nightly cron needs the secret
    os.environ["CRON_SECRET"] = "cron-test"
    c = appmod.app.test_client()
    assert c.get("/cron/backup").status_code == 401
    assert c.get("/cron/backup", headers={"Authorization": "Bearer cron-test"}).get_json()["ok"] is True
    # a failing backup emails the Admin (once)
    monkeypatch.setattr(appmod, "GOOGLE_CLIENT", FakeDrive(fail=True))
    assert c.get("/cron/backup", headers={"Authorization": "Bearer cron-test"}).status_code == 500
    assert c.get("/cron/backup", headers={"Authorization": "Bearer cron-test"}).status_code == 500
    assert [s for s, _ in mail] == ["Backup FAILED"]
    assert "Drive is down" in mail[0][1]


def test_lock_alert_sent_once(mail):
    c = appmod.app.test_client()
    for _ in range(appmod.LOGIN_MAX_FAILS + 2):
        c.post("/login/admin", data={"username": "alertlock", "password": "wrong"},
               environ_base={"REMOTE_ADDR": "10.9.9.9"})
    assert [s for s, _ in mail] == ["Login lock: alertlock"]
    assert "10.9.9.9" in mail[0][1] and "wrong" not in mail[0][1]


def test_error_alert(admin, monkeypatch, mail):
    def boom(year):
        raise RuntimeError("report broke")
    monkeypatch.setattr(appmod, "report_data", boom)
    monkeypatch.setitem(appmod.app.config, "PROPAGATE_EXCEPTIONS", False)
    assert admin.get("/admin/reports").status_code == 500
    assert admin.get("/admin/reports").status_code == 500
    assert [s for s, _ in mail] == ["Website error: RuntimeError"]
    assert "/admin/reports" in mail[0][1]


def test_alerts_off_without_settings(monkeypatch):
    for k in ("SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert appmod.alerts.configured() is False
    assert appmod.alerts.send("x", "y") is False


def test_dashboard_activity_is_short(admin, staff):
    for i in range(3):
        new_app(staff, name=f"Activity {i}", cnic=f"42101999000{i:02d}")
    page = admin.get("/admin").get_data(as_text=True)
    head, _, rest = page.partition('<details class="more">')
    assert "Sab dekhein (" in rest and head.count("<li><span><b>") <= 4 + 8   # 4 activities (+ queue rows)
    assert rest.split("</details>")[0].count("<li><span><b>") >= 1
