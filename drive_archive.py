"""Google Drive client for the application archive (server side only).

Folder layout, created automatically inside GOOGLE_ARCHIVE_PARENT_ID (or the Drive root):

  Tooba Loan Department – Complete Archive/
    Individual Applicant Forms/      latest filled Word form per applicant (old ones kept as revisions)
    Updated Application Versions/    every saved version, one file each, never overwritten
    Guarantor Records/               guarantor details per applicant
    Supporting Documents/            every uploaded photo / CNIC / document, one folder per applicant
    Complete Database Backup/        latest JSON + CSV + Excel of the whole database
    Daily Backups/                   one dated folder per day
    Weekly Backups/                  one dated folder per week (full archive)
    Backup Reports/                  verification and backup reports

Credentials (never sent to the browser):
  * an OAuth refresh token of the Google account that owns the archive — stored encrypted in
    the database by Admin → Backup & Recovery → "Connect Google Drive", or set as
    GOOGLE_OAUTH_REFRESH_TOKEN — together with GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET.
    Scope is drive.file: the app only ever sees files it created itself.
  * otherwise the service account in GOOGLE_SERVICE_ACCOUNT_JSON. A service account has no Drive
    storage of its own, so this only works when GOOGLE_ARCHIVE_PARENT_ID is a Shared Drive folder.
"""
import base64
import hashlib
import json
import os
import time
from urllib.parse import quote

ROOT_NAME = "Tooba Loan Department – Complete Archive"
FOLDERS = ["Individual Applicant Forms", "Updated Application Versions", "Guarantor Records",
           "Supporting Documents", "Complete Database Backup", "Daily Backups", "Weekly Backups",
           "Backup Reports"]
DRIVE = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OAUTH_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
SA_SCOPES = ["https://www.googleapis.com/auth/drive"]
FIELDS = "id,name,md5Checksum,size,trashed,parents,appProperties,webViewLink,modifiedTime"
RESUMABLE_OVER = 5 * 1024 * 1024


class DriveError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status

    @property
    def retryable(self):
        return self.status is None or self.status == 429 or self.status >= 500


def md5(data):
    return hashlib.md5(data).hexdigest()


def oauth_client():
    cid, secret = os.getenv("GOOGLE_OAUTH_CLIENT_ID"), os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    return (cid, secret) if cid and secret else None


def configured(stored_token=None):
    return bool((oauth_client() and (stored_token or os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN")))
                or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))


def mode(stored_token=None):
    if oauth_client() and stored_token:
        return "Google account (connected in the app)"
    if oauth_client() and os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN"):
        return "Google account (GOOGLE_OAUTH_REFRESH_TOKEN)"
    if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
        return "service account (needs a Shared Drive folder)"
    return None


# ---------------------------------------------------------------- token encryption at rest
def _fernet(secret_key):
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(("tooba-drive-token:" + secret_key).encode()).digest())
    return Fernet(key)


def seal(token, secret_key):
    return _fernet(secret_key).encrypt(token.encode()).decode()


def unseal(blob, secret_key):
    try:
        return _fernet(secret_key).decrypt(blob.encode()).decode()
    except Exception:
        return None


# ---------------------------------------------------------------- OAuth "Connect Google Drive"
def auth_url(redirect_uri, state):
    cid, _ = oauth_client()
    from urllib.parse import urlencode
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES + ["openid", "email"]), "access_type": "offline",
        "prompt": "consent", "include_granted_scopes": "false", "state": state})


def exchange_code(code, redirect_uri):
    """Returns (refresh_token, email)."""
    import requests
    cid, secret = oauth_client()
    r = requests.post("https://oauth2.googleapis.com/token", timeout=20, data={
        "code": code, "client_id": cid, "client_secret": secret, "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"})
    if r.status_code >= 400:
        raise DriveError(f"Google sign-in failed: {r.text[:300]}", r.status_code)
    body = r.json()
    email = None
    if body.get("id_token"):
        try:
            part = body["id_token"].split(".")[1]
            email = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email")
        except Exception:
            pass
    if not body.get("refresh_token"):
        raise DriveError("Google did not return a refresh token. Remove the app's access at "
                         "myaccount.google.com/permissions and connect again.")
    return body["refresh_token"], email


class ArchiveDrive:
    """Thin Drive v3 REST client. Tests replace it with a fake that has the same methods."""

    def __init__(self, refresh_token=None, timeout=60, tries=3):
        self.refresh_token = refresh_token or os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN")
        self.timeout, self.tries = timeout, tries
        self._session = None

    def _authed(self):
        if self._session is not None:
            return self._session
        from google.auth.transport.requests import AuthorizedSession
        if oauth_client() and self.refresh_token:
            from google.oauth2.credentials import Credentials
            cid, secret = oauth_client()
            creds = Credentials(None, refresh_token=self.refresh_token, client_id=cid, client_secret=secret,
                                token_uri="https://oauth2.googleapis.com/token", scopes=OAUTH_SCOPES)
        elif os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]), scopes=SA_SCOPES)
        else:
            raise DriveError("Google Drive is not connected")
        self._session = AuthorizedSession(creds)
        return self._session

    def _call(self, method, url, tries=None, **kw):
        tries = min(tries or self.tries, self.tries)
        last = None
        for i in range(tries):
            try:
                r = self._authed().request(method, url, timeout=self.timeout, **kw)
            except DriveError:
                raise
            except Exception as exc:                      # network / token refresh problem
                last = DriveError(f"{method} {url.split('?')[0]}: {type(exc).__name__}: {str(exc)[:200]}")
            else:
                if r.status_code < 400:
                    return r
                msg = r.text[:400]
                if "storageQuotaExceeded" in msg or "do not have storage quota" in msg:
                    raise DriveError("Service account has no Drive storage. Connect the owner's Google account "
                                     "on Admin → Backup & Recovery (or use a Shared Drive folder).", r.status_code)
                last = DriveError(f"{method} {url.split('?')[0]} -> {r.status_code}: {msg}", r.status_code)
                if not last.retryable:
                    raise last
            if i + 1 < tries:
                time.sleep(min(8, 1.5 * (2 ** i)))
        raise last

    def _json(self, method, url, **kw):
        r = self._call(method, url, **kw)
        return r.json() if r.content else {}

    # ---- folders
    def find_child(self, parent, name, folder=False):
        q = f"name = '{name.replace(chr(92), chr(92) * 2).replace(chr(39), chr(92) + chr(39))}' and trashed = false"
        if parent:
            q += f" and '{parent}' in parents"
        if folder:
            q += f" and mimeType = '{FOLDER_MIME}'"
        res = self._json("GET", f"{DRIVE}?q={quote(q)}&supportsAllDrives=true&includeItemsFromAllDrives=true"
                                f"&pageSize=10&fields=files({FIELDS})&orderBy=createdTime")
        files = res.get("files", [])
        return files[0] if files else None

    def ensure_folder(self, name, parent=None):
        found = self.find_child(parent, name, folder=True)
        if found:
            return found["id"]
        meta = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            meta["parents"] = [parent]
        return self._json("POST", f"{DRIVE}?supportsAllDrives=true&fields=id", json=meta)["id"]

    def folder_url(self, folder_id):
        return f"https://drive.google.com/drive/folders/{folder_id}"

    # ---- files
    def upload(self, name, data, mime, parent, props=None, lock=False):
        """Create a new file. Returns its metadata (id, md5Checksum, size...).
        lock=True makes it read-only in Drive (the owner can still unlock it), so a backup copy is not
        changed by opening and saving it there."""
        meta = {"name": name, "parents": [parent], "mimeType": mime}
        if lock:
            meta["contentRestrictions"] = [{"readOnly": True,
                                            "reason": "Tooba backup copy - kept exactly as saved"}]
        if props:
            meta["appProperties"] = {k: str(v) for k, v in props.items()}
        if len(data) > RESUMABLE_OVER:
            r = self._call("POST", f"{UPLOAD}?uploadType=resumable&supportsAllDrives=true&fields={FIELDS}",
                           json=meta, headers={"X-Upload-Content-Type": mime})
            loc = r.headers["Location"]
            return self._json("PUT", loc, data=data, headers={"Content-Type": mime})
        boundary = "tooba-archive-" + md5(data)[:16]
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--".encode()
        return self._json("POST", f"{UPLOAD}?uploadType=multipart&supportsAllDrives=true&fields={FIELDS}",
                          data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})

    def update(self, file_id, data, mime, name=None):
        """New content for an existing file. The previous content stays as a revision kept forever."""
        r = self._json("PATCH", f"{UPLOAD}/{file_id}?uploadType=media&supportsAllDrives=true"
                                f"&keepRevisionForever=true&fields={FIELDS}",
                       data=data, headers={"Content-Type": mime})
        if name and r.get("name") != name:
            r = self._json("PATCH", f"{DRIVE}/{file_id}?supportsAllDrives=true&fields={FIELDS}", json={"name": name})
        return r

    def get(self, file_id):
        """Metadata, or None when the file no longer exists / is not reachable."""
        try:
            return self._json("GET", f"{DRIVE}/{file_id}?supportsAllDrives=true&fields={FIELDS}", tries=2)
        except DriveError as exc:
            if exc.status == 404:
                return None
            raise

    def download(self, file_id):
        return self._call("GET", f"{DRIVE}/{file_id}?alt=media&supportsAllDrives=true").content

    def list_archive(self):
        """Every file this app put in the archive (tagged with appProperties tooba_kind),
        fetched page by page. Returns a list of metadata dicts."""
        q = "appProperties has { key='tooba' and value='1' } and trashed = false"
        out, token = [], ""
        while True:
            res = self._json("GET", f"{DRIVE}?q={quote(q)}&supportsAllDrives=true&includeItemsFromAllDrives=true"
                                    f"&pageSize=1000&fields=nextPageToken,files({FIELDS})"
                                    + (f"&pageToken={quote(token)}" if token else ""))
            out += res.get("files", [])
            token = res.get("nextPageToken") or ""
            if not token:
                return out
