"""The REST client itself, with Google's HTTP replaced by a recorder (no network)."""
import gsheets


class Recorder(gsheets.GoogleClient):
    def __init__(self, existing_tabs):
        super().__init__()
        self.calls, self.tabs = [], dict(existing_tabs)

    def _call(self, method, url, **kw):
        self.calls.append((method, url, kw.get("json")))
        if url.startswith(gsheets.DRIVE) and method == "GET":
            return {"files": [{"id": "other", "name": "Budget 2026"},
                              {"id": "abc", "name": "Tooba Foundation - Master  Disbursement 2026"},
                              {"id": "old", "name": "Tooba Foundation - Master Disbursement 2025"}]}
        if url.endswith(":batchUpdate"):
            for r in kw["json"]["requests"]:
                if "addSheet" in r:
                    t = r["addSheet"]["properties"]["title"]
                    self.tabs[t] = 100 + len(self.tabs)
            return {}
        if "fields=sheets.properties" in url:
            return {"spreadsheetUrl": "https://docs.google.com/spreadsheets/d/abc",
                    "sheets": [{"properties": {"title": t, "sheetId": i}} for t, i in self.tabs.items()]}
        return {}


def test_hand_made_sheet_gets_its_tabs():
    c = Recorder({"Sheet1": 0})
    sid, url, tabs = c.find_year(2026)
    assert sid == "abc" and url.endswith("/abc")
    assert all(t in tabs for _, t in gsheets.all_tabs(2026))
    q = c.calls[0][1]
    assert "trashed%20%3D%20false" in q and "name%20contains" not in q


def test_no_matching_sheet_returns_none():
    c = Recorder({"Sheet1": 0})
    assert c.find_year(2031) is None


def test_complete_sheet_is_left_alone():
    c = Recorder({t: i for i, (_, t) in enumerate(gsheets.all_tabs(2027))})
    c.find_year(2027)
    assert not any(u.endswith(":batchUpdate") for _, u, _ in c.calls)


def test_service_account_can_see_shared_files(monkeypatch):
    """drive.file would hide files the app did not create, such as the hand-made yearly sheet."""
    import json
    captured = {}

    class FakeCreds:
        @staticmethod
        def from_service_account_info(info, scopes):
            captured["scopes"] = scopes
            return object()

    import google.oauth2.service_account as sa
    import google.auth.transport.requests as tr
    monkeypatch.setattr(sa, "Credentials", FakeCreds)
    monkeypatch.setattr(tr, "AuthorizedSession", lambda creds: "session")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps({"type": "service_account"}))
    assert gsheets.GoogleClient()._authed() == "session"
    assert "https://www.googleapis.com/auth/drive" in captured["scopes"]
    assert "https://www.googleapis.com/auth/drive.file" not in captured["scopes"]


class DriveRecorder(gsheets.GoogleClient):
    def __init__(self, names, quota_error=False):
        super().__init__()
        self.calls, self.names, self.quota_error = [], names, quota_error

    def _call(self, method, url, **kw):
        self.calls.append((method, url, kw.get("data")))
        if method == "GET":
            return {"files": [{"id": f"id{i}", "name": n} for i, n in enumerate(self.names)]}
        if method == "POST" and self.quota_error:
            raise gsheets.GoogleError("POST -> 403: storageQuotaExceeded")
        return {"id": "new"}


def test_backup_overwrites_existing_file():
    c = DriveRecorder(["Tooba Backup - Sunday.json.gz", "Tooba Backup - Monday.json.gz"])
    assert c.put_file("Tooba Backup - Monday.json.gz", b"data") == "id1"
    method, url, data = c.calls[-1]
    assert method == "PATCH" and url.startswith(gsheets.DRIVE_UPLOAD + "/id1?uploadType=media") and data == b"data"


def test_backup_missing_file_explains_what_to_do():
    import pytest
    c = DriveRecorder([], quota_error=True)
    with pytest.raises(gsheets.GoogleError, match="file maujood nahi"):
        c.put_file("Tooba Backup - Friday.json.gz", b"data")


def test_tab_names_carry_the_year():
    assert gsheets.tab_title(11, 2026) == "November 2026"
    assert gsheets.tab_title(0, 2026) == "Yearly Summary 2026"


class Renamer(gsheets.GoogleClient):
    """Old spreadsheet with tabs named "January" … "Yearly Summary"."""
    def __init__(self, tabs):
        super().__init__()
        self.tabs, self.posts = dict(tabs), []

    def _call(self, method, url, **kw):
        if method == "GET":
            return {"sheets": [{"properties": {"title": t, "sheetId": i}} for t, i in self.tabs.items()]}
        self.posts.append(kw["json"]["requests"])
        replies = []
        for r in kw["json"]["requests"]:
            if "updateSheetProperties" in r:
                p = r["updateSheetProperties"]["properties"]
                old = [t for t, i in self.tabs.items() if i == p["sheetId"]][0]
                self.tabs[p["title"]] = self.tabs.pop(old)
                replies.append({})
            elif "addSheet" in r:
                self.tabs[r["addSheet"]["properties"]["title"]] = 500 + len(self.tabs)
                replies.append({"addSheet": {"properties": {"sheetId": self.tabs[r["addSheet"]["properties"]["title"]]}}})
        return {"replies": replies}


def test_old_month_tab_is_renamed_not_duplicated():
    c = Renamer({"November": 11, "Yearly Summary": 99})
    assert c.tab_id("s", 2026, 11) == 11                     # same tab, now "November 2026"
    assert "November 2026" in c.tabs and "November" not in c.tabs
    assert c.tab_id("s", 2026, 11) == 11 and len(c.posts) == 1   # second time: nothing to change
    assert c.tab_id("s", 2026, 0) == 99 and "Yearly Summary 2026" in c.tabs
    new = c.tab_id("s", 2026, 3)                             # missing month gets added
    assert c.tabs["March 2026"] == new


def test_find_year_renames_legacy_tabs():
    c = Recorder({t: i for i, t in enumerate(gsheets.MONTHS + [gsheets.SUMMARY])})
    c.find_year(2026)
    reqs = [r for _, u, body in c.calls if u.endswith(":batchUpdate") for r in body["requests"]]
    assert len(reqs) == 13 and all("updateSheetProperties" in r for r in reqs)
