"""Google Sheets copy of the disbursement sheets (server side only).

Supabase is the source of truth. This module only *writes* to Google:
  * one spreadsheet per calendar year, "Tooba Foundation – Master Disbursement <year>",
    with tabs January … December and "Yearly Summary";
  * a month tab is always rewritten in full from the database, so rows can never be
    duplicated and edits/removals in the app show up correctly.

Credentials come from environment variables (never sent to the browser):
  GOOGLE_SERVICE_ACCOUNT_JSON  – service-account key JSON (needs a Shared Drive folder), or
  GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REFRESH_TOKEN
                               – a Google account authorised once by the Admin.
  GOOGLE_DRIVE_FOLDER_ID       – folder (or Shared Drive folder) the yearly files go into.
"""
import json
import os

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
SUMMARY = "Yearly Summary"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file"]
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3/files"


def title_for(year):
    return f"Tooba Foundation – Master Disbursement {year}"


def configured():
    return bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN"))


def mode():
    if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
        return "service account"
    if os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN"):
        return "Google account (OAuth)"
    return None


class GoogleError(Exception):
    pass


class GoogleClient:
    """Thin REST client. Tests replace it with a fake that has the same three methods."""

    def __init__(self, timeout=20):
        self.timeout = timeout
        self._session = None

    def _authed(self):
        if self._session is not None:
            return self._session
        from google.auth.transport.requests import AuthorizedSession
        if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
            from google.oauth2 import service_account
            info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        elif os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN"):
            from google.oauth2.credentials import Credentials
            creds = Credentials(None, refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
                                client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
                                client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
                                token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)
        else:
            raise GoogleError("Google Sheets is not configured")
        self._session = AuthorizedSession(creds)
        return self._session

    def _call(self, method, url, **kw):
        r = self._authed().request(method, url, timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise GoogleError(f"{method} {url.split('?')[0]} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def create_year(self, year):
        """Create the yearly spreadsheet with its 13 tabs. Returns (id, url, {tab: sheetId})."""
        meta = {"name": title_for(year), "mimeType": "application/vnd.google-apps.spreadsheet"}
        folder = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        if folder:
            meta["parents"] = [folder]
        f = self._call("POST", DRIVE + "?supportsAllDrives=true&fields=id", json=meta)
        sid = f["id"]
        info = self._call("GET", f"{SHEETS}/{sid}?fields=sheets.properties")
        first = info["sheets"][0]["properties"]["sheetId"]
        reqs = [{"updateSheetProperties": {"properties": {"sheetId": first, "title": MONTHS[0]},
                                           "fields": "title"}}]
        reqs += [{"addSheet": {"properties": {"title": t}}} for t in MONTHS[1:] + [SUMMARY]]
        self._call("POST", f"{SHEETS}/{sid}:batchUpdate", json={"requests": reqs})
        info = self._call("GET", f"{SHEETS}/{sid}?fields=sheets.properties,spreadsheetUrl")
        tabs = {s["properties"]["title"]: s["properties"]["sheetId"] for s in info["sheets"]}
        return sid, info.get("spreadsheetUrl", f"https://docs.google.com/spreadsheets/d/{sid}"), tabs

    def find_year(self, year):
        """An existing spreadsheet for the year — found by "Master Disbursement <year>" in its name,
        so a file the Admin created by hand (needed with a plain Gmail account, where a service
        account cannot own files) is used too. Missing month / summary tabs are added."""
        q = (f"name contains 'Master Disbursement {int(year)}' and trashed = false "
             "and mimeType = 'application/vnd.google-apps.spreadsheet'")
        folder = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        if folder:
            q += f" and '{folder}' in parents"
        from urllib.parse import quote
        res = self._call("GET", f"{DRIVE}?q={quote(q)}&supportsAllDrives=true&includeItemsFromAllDrives=true"
                                f"&fields=files(id)")
        files = res.get("files", [])
        if not files:
            return None
        sid = files[0]["id"]
        info = self._call("GET", f"{SHEETS}/{sid}?fields=sheets.properties,spreadsheetUrl")
        tabs = {s["properties"]["title"]: s["properties"]["sheetId"] for s in info["sheets"]}
        missing = [t for t in MONTHS + [SUMMARY] if t not in tabs]
        if missing:
            self._call("POST", f"{SHEETS}/{sid}:batchUpdate",
                       json={"requests": [{"addSheet": {"properties": {"title": t}}} for t in missing]})
            info = self._call("GET", f"{SHEETS}/{sid}?fields=sheets.properties,spreadsheetUrl")
            tabs = {s["properties"]["title"]: s["properties"]["sheetId"] for s in info["sheets"]}
        return sid, info.get("spreadsheetUrl"), tabs

    def write_tab(self, spreadsheet_id, tab, sheet_id, rows, bold_rows=()):
        """Replace everything on one tab with `rows` (list of lists of plain values)."""
        rng = f"'{tab}'!A1:Z3000"
        self._call("POST", f"{SHEETS}/{spreadsheet_id}/values/{rng}:clear", json={})
        if rows:
            self._call("PUT", f"{SHEETS}/{spreadsheet_id}/values/'{tab}'!A1?valueInputOption=RAW",
                       json={"values": rows})
        reqs = [{"repeatCell": {"range": {"sheetId": sheet_id}, "cell": {"userEnteredFormat": {
            "textFormat": {"bold": False}}}, "fields": "userEnteredFormat.textFormat.bold"}}]
        for r in bold_rows:
            reqs.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1},
                                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                                        "fields": "userEnteredFormat.textFormat.bold"}})
        reqs.append({"autoResizeDimensions": {"dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                             "startIndex": 0, "endIndex": 12}}})
        self._call("POST", f"{SHEETS}/{spreadsheet_id}:batchUpdate", json={"requests": reqs})
