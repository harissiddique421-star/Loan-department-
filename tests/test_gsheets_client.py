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
    assert all(t in tabs for t in gsheets.MONTHS + [gsheets.SUMMARY])
    q = c.calls[0][1]
    assert "trashed%20%3D%20false" in q and "name%20contains" not in q


def test_no_matching_sheet_returns_none():
    c = Recorder({"Sheet1": 0})
    assert c.find_year(2031) is None


def test_complete_sheet_is_left_alone():
    c = Recorder({t: i for i, t in enumerate(gsheets.MONTHS + [gsheets.SUMMARY])})
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
