import os, json, secrets, csv, io, hashlib
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, flash, abort,
                   send_file, Response, jsonify, g, has_request_context, session)
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import inspect, text, event, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from form_fields import (FORM_SECTIONS, FIELD_LABELS, FIELD_NAMES, PHOTO_SLOTS, PHOTO_KINDS,
                         PHOTO_LABELS)

app = Flask(__name__)

ON_VERCEL = bool(os.getenv("VERCEL"))
dburl = os.getenv("DATABASE_URL", "")
if not dburl:
    # Fallback for local testing only. On Vercel /tmp is wiped, so DATABASE_URL (Supabase) is required.
    dburl = "sqlite:////tmp/tooba_dev.db" if ON_VERCEL else "sqlite:///tooba_dev.db"
if dburl.startswith("postgres://"):
    dburl = dburl.replace("postgres://", "postgresql+psycopg://", 1)
elif dburl.startswith("postgresql://"):
    dburl = dburl.replace("postgresql://", "postgresql+psycopg://", 1)
USING_SQLITE = dburl.startswith("sqlite")


def _secret_key():
    """SECRET_KEY from the environment. Without one, derive a stable key from the (secret)
    DATABASE_URL so sessions can't be forged with a known default. The fixed development key
    is used only for a local SQLite database, never on any real deployment (Vercel, Render...)."""
    if os.getenv("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    if os.getenv("DATABASE_URL"):
        return hashlib.sha256(("tooba-session:" + os.environ["DATABASE_URL"]).encode()).hexdigest()
    if ON_VERCEL:
        return secrets.token_hex(32)
    return "dev-only-change-me"


# HTTPS-only cookies everywhere except a local SQLite test copy (set SECURE_COOKIES=0 to override).
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "1" if (ON_VERCEL or not USING_SQLITE) else "0") == "1"

app.config["SECRET_KEY"] = _secret_key()
ENGINE_OPTIONS = {"pool_pre_ping": True}
if not USING_SQLITE:
    # Supabase's transaction pooler (port 6543) cannot keep prepared statements, and each
    # Vercel function call is short-lived, so open a fresh pooled connection per request.
    from sqlalchemy.pool import NullPool
    ENGINE_OPTIONS = {"poolclass": NullPool, "connect_args": {"prepare_threshold": None}}

app.config.update(
    SQLALCHEMY_DATABASE_URI=dburl,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS=ENGINE_OPTIONS,
    MAX_CONTENT_LENGTH=15 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SECURE_COOKIES,
    REMEMBER_COOKIE_SECURE=SECURE_COOKIES,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    WTF_CSRF_TIME_LIMIT=None,
)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
lm = LoginManager(app)
lm.login_view = "login"
lm.login_message = "Pehle login karein."
lm.session_protection = "strong"   # a session cookie copied to another browser/IP is dropped

# Brute-force protection: after this many wrong passwords within the window, login is blocked.
LOGIN_MAX_FAILS = int(os.getenv("LOGIN_MAX_FAILS", "5"))
LOGIN_WINDOW = timedelta(minutes=int(os.getenv("LOGIN_LOCK_MINUTES", "15")))

# ---------------------------------------------------------------- roles, permissions, workflow
ROLES = ["Admin", "Reviewer", "Staff"]
STATUSES = ["Draft", "Submitted", "Pending Review", "In Review", "Correction Required",
            "Submitted Again", "Approved", "Rejected", "Ready for Disbursement",
            "Added to Disbursement Sheet", "Disbursed", "Completed"]
EDITABLE_STATUSES = {"Draft", "Correction Required"}
REASON_REQUIRED = {"Rejected", "Correction Required"}

# code -> label shown on the Admin "Permissions" page. Admin always has all of them.
PERMISSIONS = {
    "app.create": "Nayi application banana",
    "app.edit_own": "Apni / assigned application edit karna (Draft ya Correction Required)",
    "app.edit_any": "Koi bhi application kisi bhi status mein edit karna",
    "app.view_all": "Saari applications dekhna aur search karna",
    "app.submit": "Application review ke liye submit karna",
    "app.review": "Assigned application review karna, notes, correction request",
    "app.decide": "Assigned application approve / reject karna",
    "app.assign": "Staff / reviewer assign karna",
    "app.delete": "Application delete karna (soft delete, restore ho sakti hai)",
    "app.restore": "Deleted applications dekhna aur restore karna",
    "doc.upload": "Documents / photos upload aur replace karna",
    "reports.view": "Reports dekhna",
    "export": "CSV / backup export",
    "audit.view": "Audit log dekhna",
    "settings.manage": "Fee aur purpose categories manage karna",
    "disbursement.manage": "Disbursement sheet manage karna",
    "period.close": "Monthly period close / reopen karna",
    "gsheets.manage": "Google Sheets integration manage karna",
    "users.manage": "Users, roles aur permissions manage karna",
    "app.purge": "Application hamesha ke liye mitana",
}
# Admin-section powers are never assignable to Staff/Reviewer (their pages live under /admin).
ADMIN_ONLY = {"users.manage", "app.purge", "app.delete", "app.restore", "app.assign", "reports.view",
              "export", "audit.view", "settings.manage", "period.close", "gsheets.manage"}

# Three separate sections of the same site. One login system (the `user` table), one role
# per account; each role has its own login page, dashboard, menu and URL area.
PORTALS = {"admin": "Admin", "reviewer": "Reviewer", "staff": "Staff"}
SECTION_OF_ROLE = {v: k for k, v in PORTALS.items()}
PORTAL_INFO = {
    "Admin": {"title": "Admin Panel", "login": "Admin Login", "home": "admin_dashboard",
              "list": "admin_applications",
              "blurb": "Users, permissions, saari applications, reports, audit log aur settings."},
    "Reviewer": {"title": "Reviewer Desk", "login": "Reviewer Login", "home": "reviewer_dashboard",
                 "list": "reviewer_applications",
                 "blurb": "Assigned applications ka review, corrections, approve / reject."},
    "Staff": {"title": "Staff Desk", "login": "Staff Login", "home": "staff_dashboard",
              "list": "staff_applications",
              "blurb": "Nayi application, draft, documents upload aur review ke liye submit."},
}
# Same defaults as the Phase 3 migration (tests check they match).
DEFAULT_ROLE_PERMS = {
    "Staff": {"app.create", "app.edit_own", "app.view_all", "app.submit", "doc.upload"},
    "Reviewer": {"app.review", "app.decide"},
}
DEFAULT_TRANSITIONS = [
    ("Draft", "Submitted", "app.submit"),
    ("Submitted", "Pending Review", "app.assign"),
    ("Submitted", "In Review", "app.review"),
    ("Pending Review", "In Review", "app.review"),
    ("Submitted Again", "In Review", "app.review"),
    ("Submitted Again", "Pending Review", "app.assign"),
    ("In Review", "Correction Required", "app.review"),
    ("In Review", "Approved", "app.decide"),
    ("In Review", "Rejected", "app.decide"),
    ("Correction Required", "Submitted Again", "app.submit"),
    ("Approved", "Ready for Disbursement", "disbursement.manage"),
    ("Approved", "In Review", "app.review"),
    ("Ready for Disbursement", "Added to Disbursement Sheet", "disbursement.manage"),
    ("Added to Disbursement Sheet", "Ready for Disbursement", "disbursement.manage"),
    ("Added to Disbursement Sheet", "Disbursed", "disbursement.manage"),
    ("Disbursed", "Completed", "disbursement.manage"),
]
DEFAULT_PURPOSES = ["Business", "Education", "Liberation", "Marriage", "Medical", "Construction", "Other"]
DEFAULT_FEES = [(50000, 1700), (40000, 1500), (30000, 1300), (25000, 1200), (20000, 1100),
                (15000, 1000), (10000, 900)]

ACTION_LABELS = {
    "application.created": "Application banayi",
    "application.edited": "Form edit kiya",
    "application.status": "Status badla",
    "application.assigned": "Assignment badli",
    "application.loan_number": "Loan number set kiya",
    "application.deleted": "Application delete ki",
    "application.restored": "Application restore ki",
    "application.printed": "Print / PDF khola",
    "application.note": "Note likha",
    "document.uploaded": "File upload ki",
    "document.replaced": "File replace ki",
    "document.deleted": "File delete ki",
    "user.created": "User banaya",
    "user.role_changed": "User ka role badla",
    "user.status_changed": "User enable/disable kiya",
    "user.password_reset": "User ka password reset kiya",
    "user.password_changed": "Apna password badla",
    "permissions.changed": "Permissions badlin",
    "export.csv": "CSV export kiya",
    "export.backup": "Backup download kiya",
    "settings.purpose_added": "Purpose category add ki",
    "settings.purpose_changed": "Purpose category badli",
    "settings.fee_added": "Fee category add ki",
    "settings.fee_changed": "Fee category badli",
}

ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".doc", ".docx"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


# ---------------------------------------------------------------- models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="Staff")
    active = db.Column(db.Boolean, default=True)
    full_name = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    deleted_at = db.Column(db.DateTime)

    def set_password(self, p): self.password_hash = generate_password_hash(p)
    def check_password(self, p): return check_password_hash(self.password_hash, p)

    @property
    def display(self):
        return self.full_name or self.username

    def session_token(self):
        # Changes whenever the password changes, so old sessions (other browsers, stolen
        # cookies) stop working after a password change or admin reset.
        return hashlib.sha256(("sess:" + self.password_hash).encode()).hexdigest()[:16]

    def get_id(self):
        return f"{self.id}:{self.session_token()}"


class Application(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    app_id = db.Column(db.String(20), unique=True, index=True)
    applicant_name = db.Column(db.String(200), default="")
    cnic = db.Column(db.String(30), index=True, default="")
    status = db.Column(db.String(40), default="Draft")
    assigned_to = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    form_json = db.Column(db.Text, default="{}")
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    updated_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    loan_number = db.Column(db.String(30), index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    family_cnic_required = db.Column(db.Boolean, default=False, nullable=False)
    loan_amount = db.Column(db.Numeric(12, 2))
    purpose_id = db.Column(db.Integer, db.ForeignKey("purpose_category.id"))
    purpose_label = db.Column(db.String(80))
    fee_category_id = db.Column(db.Integer, db.ForeignKey("fee_category.id"))
    fee_amount = db.Column(db.Numeric(12, 2))
    submitted_at = db.Column(db.DateTime)
    approved_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    approved_at = db.Column(db.DateTime)
    rejection_reason = db.Column(db.Text)
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    version = db.Column(db.Integer, default=1, nullable=False)

    @property
    def data(self):
        try:
            return json.loads(self.form_json or "{}")
        except (TypeError, ValueError):
            return {}

    @property
    def form_values(self):
        """Saved form values, with name/CNIC filled from the record for older applications."""
        d = self.data
        if not d.get("name"):
            d["name"] = d.get("full_name") or self.applicant_name or ""
        if not d.get("cnic") and self.cnic:
            d["cnic"] = self.cnic
        return d

    def active_docs(self, kind=None):
        q = Document.query.filter(Document.application_id == self.id, Document.deleted_at.is_(None),
                                  Document.replaced_by.is_(None))
        if kind:
            q = q.filter(Document.kind == kind)
        return q.order_by(Document.id.desc())

    def latest_photo(self, kind):
        return self.active_docs(kind).first()

    @property
    def photo(self):
        return self.latest_photo("photo")


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=False)
    original_name = db.Column(db.String(255))
    stored_name = db.Column(db.String(255), unique=True)
    kind = db.Column(db.String(20), default="document")      # "photo", "g1Photo", ... or "document"
    content_type = db.Column(db.String(120), default="application/octet-stream")
    size = db.Column(db.Integer, default=0)
    storage = db.Column(db.String(20), default="db")          # "db", "supabase" or "local" (legacy)
    data = db.Column(db.LargeBinary, nullable=True)            # file bytes when storage == "db"
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    storage_path = db.Column(db.String(300))
    sha256 = db.Column(db.String(64))
    replaced_by = db.Column(db.Integer, db.ForeignKey("document.id"))
    replaced_at = db.Column(db.DateTime)
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    @property
    def is_image(self):
        return (self.content_type or "").startswith("image/")


class Log(db.Model):
    """Original activity log. Still used for login security events and shown in history."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    action = db.Column(db.String(200))
    application_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AuditLog(db.Model):
    """Append-only (enforced by a database trigger on PostgreSQL)."""
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    actor_name = db.Column(db.String(80))
    action = db.Column(db.String(60), nullable=False)
    application_id = db.Column(db.Integer)
    entity = db.Column(db.String(40))
    entity_id = db.Column(db.Integer)
    reason = db.Column(db.Text)
    old_value = db.Column(db.JSON)
    new_value = db.Column(db.JSON)
    ip = db.Column(db.String(45))


class RolePermission(db.Model):
    __tablename__ = "role_permission"
    role = db.Column(db.String(20), primary_key=True)
    permission = db.Column(db.String(50), primary_key=True)


class StatusTransition(db.Model):
    __tablename__ = "status_transition"
    from_status = db.Column(db.String(40), primary_key=True)
    to_status = db.Column(db.String(40), primary_key=True)
    permission = db.Column(db.String(50), nullable=False)


class PurposeCategory(db.Model):
    __tablename__ = "purpose_category"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FeeCategory(db.Model):
    __tablename__ = "fee_category"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    loan_amount = db.Column(db.Numeric(12, 2))
    fee_amount = db.Column(db.Numeric(12, 2), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ReviewNote(db.Model):
    __tablename__ = "review_note"
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    kind = db.Column(db.String(20), nullable=False)      # note | correction | recommend | decision
    body = db.Column(db.Text, nullable=False)
    resolved_at = db.Column(db.DateTime)
    resolved_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class AppIdCounter(db.Model):
    """Used only on SQLite; PostgreSQL uses the next_app_id() database function."""
    __tablename__ = "app_id_counter"
    year = db.Column(db.Integer, primary_key=True, autoincrement=False)
    last_value = db.Column(db.Integer, default=0, nullable=False)


# ---------------------------------------------------------------- database actor
# Every transaction tells PostgreSQL who is acting (app.user_id / app.role). Row Level
# Security policies and triggers use this, so permissions hold even if a route has a bug.
def _actor():
    if not has_request_context():
        return "", "system"          # startup / maintenance code
    return g.get("_db_actor") or ("", "anon")


_SET_ACTOR = text("select set_config('app.user_id', :u, true), set_config('app.role', :r, true)")


@event.listens_for(Session, "after_begin")
def _send_actor(session, transaction, connection):
    if connection.dialect.name == "postgresql":
        uid, role = _actor()
        connection.execute(_SET_ACTOR, {"u": uid, "r": role})


def set_db_actor(user):
    g._db_actor = (str(user.id), user.role) if user else ("", "anon")
    g.pop("_perm_cache", None)
    if not USING_SQLITE and db.session().in_transaction():
        uid, role = g._db_actor
        db.session.execute(_SET_ACTOR, {"u": uid, "r": role})


# ---------------------------------------------------------------- file storage
# If SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are set, files go to a private Supabase Storage bucket.
# Otherwise they are stored inside the database itself, which is also permanent.
_sb = None


def supabase_bucket():
    global _sb
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        return None
    if _sb is None:
        from supabase import create_client
        _sb = create_client(url, key)
    return _sb.storage.from_(os.getenv("SUPABASE_BUCKET", "tooba-private-documents"))


def save_file(app_row, f, kind):
    original = secure_filename(f.filename or "") or "file"
    ext = os.path.splitext(original)[1].lower()
    if ext not in ALLOWED_EXT:
        return None, "Sirf PDF, JPG, PNG, WEBP, DOC, DOCX allowed hain."
    if kind in PHOTO_KINDS and ext not in IMAGE_EXT:
        return None, "Photo ke liye JPG, PNG ya WEBP file chunein."
    blob = f.read()
    if not blob:
        return None, "File khaali hai."
    stored = f"{app_row.app_id}/{secrets.token_hex(16)}{ext}"
    doc = Document(application_id=app_row.id, original_name=original, stored_name=stored, kind=kind,
                   content_type=MIME.get(ext, "application/octet-stream"), size=len(blob),
                   uploaded_by=current_user.id, sha256=hashlib.sha256(blob).hexdigest())
    bucket = supabase_bucket()
    if bucket is not None:
        bucket.upload(stored, blob, {"content-type": doc.content_type})
        doc.storage, doc.storage_path = "supabase", stored
    else:
        doc.storage, doc.data = "db", blob
    db.session.add(doc)
    return doc, None


def read_file(doc):
    if doc.storage == "db":
        return doc.data or b""
    if doc.storage == "supabase":
        bucket = supabase_bucket()
        if bucket is None:
            abort(500)
        return bucket.download(doc.stored_name)
    path = os.path.join(os.getenv("UPLOAD_FOLDER", "/tmp/tooba_uploads"), doc.stored_name)
    if not os.path.exists(path):
        abort(404)
    with open(path, "rb") as fh:
        return fh.read()


# ---------------------------------------------------------------- helpers
@lm.user_loader
def load_user(uid):
    sid, _, token = str(uid).partition(":")
    if not sid.isdigit() or not token:
        return None
    u = db.session.get(User, int(sid))
    if not u or not u.active or u.deleted_at or not secrets.compare_digest(token, u.session_token()):
        return None
    set_db_actor(u)
    return u


def client_ip():
    # Vercel / most proxies put the real client address first in X-Forwarded-For.
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr or "")[:45]


def login_blocked(username, ip):
    since = datetime.utcnow() - LOGIN_WINDOW
    recent = Log.query.filter(Log.created_at >= since, Log.action.like("LOGIN_FAIL %"))
    esc = lambda v: v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    by_user = recent.filter(Log.action.like(f"LOGIN_FAIL user={esc(username[:60])} %", escape="\\")).count()
    by_ip = recent.filter(Log.action.like(f"% ip={esc(ip)}", escape="\\")).count()
    return by_user >= LOGIN_MAX_FAILS or by_ip >= LOGIN_MAX_FAILS * 4


_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def role_perms(role):
    if role == "Admin":
        return set(PERMISSIONS)
    cache = g.setdefault("_perm_cache", {}) if has_request_context() else {}
    if role not in cache:
        cache[role] = {p for (p,) in db.session.query(RolePermission.permission).filter_by(role=role)}
    return cache[role]


def can(perm):
    return bool(current_user and current_user.is_authenticated and perm in role_perms(current_user.role))


def requires(*perms):
    def deco(fn):
        @wraps(fn)
        @login_required
        def inner(*args, **kwargs):
            if not all(can(p) for p in perms):
                abort(403)
            return fn(*args, **kwargs)
        return inner
    return deco


def log_security(action, user_id=None):
    """Login events go to the original `log` table (used by the brute-force lock)."""
    db.session.add(Log(user_id=user_id, action=action[:200]))
    db.session.commit()


def audit(action, a=None, reason=None, old=None, new=None, entity=None, entity_id=None):
    """Adds an audit row to the current transaction; the caller commits."""
    who = current_user if current_user and current_user.is_authenticated else None
    db.session.add(AuditLog(actor_id=who.id if who else None, actor_name=who.username if who else None,
                            action=action, application_id=a.id if a else None, entity=entity,
                            entity_id=entity_id, reason=reason, old_value=old, new_value=new,
                            ip=client_ip() if has_request_context() else None))


def csv_safe(v):
    """Stop CSV/Excel formula injection (=HYPERLINK(...), +cmd, @SUM...) from applicant data."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def visible_apps():
    """Applications the current user may see — mirrors the database RLS policy."""
    q = Application.query
    if not can("app.restore"):
        q = q.filter(Application.deleted_at.is_(None))
    if not can("app.view_all"):
        uid = current_user.id
        q = q.filter(or_(Application.created_by == uid, Application.assigned_to == uid,
                         Application.reviewer_id == uid))
    return q


def get_app(app_id, allow_deleted=False):
    a = visible_apps().filter(Application.id == app_id).first()
    if a is None or (a.deleted_at and not allow_deleted):
        abort(404)
    return a


def is_owner(a):
    return current_user.id in (a.created_by, a.assigned_to)


def can_edit_form(a):
    if a.deleted_at:
        return False
    if can("app.edit_any"):
        return True
    return can("app.edit_own") and is_owner(a) and a.status in EDITABLE_STATUSES


def allowed_transitions(a):
    if a.deleted_at:
        return []
    if current_user.role == "Admin":
        return [s for s in STATUSES if s != a.status]
    out = []
    for t in StatusTransition.query.filter_by(from_status=a.status).all():
        if not can(t.permission):
            continue
        if t.permission in ("app.review", "app.decide") and a.reviewer_id != current_user.id:
            continue
        if t.permission == "app.submit" and not is_owner(a):
            continue
        out.append(t.to_status)
    return [s for s in STATUSES if s in out]


def next_app_id():
    if not USING_SQLITE:
        return db.session.execute(text("select public.next_app_id()")).scalar()
    year = (datetime.utcnow() + timedelta(hours=5)).year      # Pakistan time
    row = db.session.get(AppIdCounter, year)
    if row is None:
        row = AppIdCounter(year=year, last_value=0)
        db.session.add(row)
    while True:
        row.last_value += 1
        candidate = f"TBF-{year}-{row.last_value:05d}"
        if not Application.query.filter_by(app_id=candidate).first():
            return candidate


def cnic_matches(cnic, exclude_id):
    """Other applications with the same CNIC (ID/status/date only)."""
    if not cnic:
        return []
    if USING_SQLITE:
        rows = (db.session.query(Application.app_id, Application.status, Application.created_at)
                .filter(Application.cnic == cnic, Application.id != exclude_id,
                        Application.deleted_at.is_(None)).order_by(Application.id).all())
    else:
        rows = db.session.execute(text("select app_id, status, created_at from public.cnic_matches(:c, :x)"),
                                  {"c": cnic, "x": exclude_id}).all()
    return [tuple(r) for r in rows]


def parse_amount(v):
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    return int(digits) if digits else None


def usernames():
    return {u.id: u.display for u in User.query.all()}


@app.template_filter("slug")
def slug(s):
    return "s-" + "".join(ch if ch.isalnum() else "-" for ch in (s or "")).lower()


def nav_items(role):
    """Each role gets its own menu; no role ever sees another role's links."""
    if role == "Admin":
        return [("admin_dashboard", "Dashboard", "grid", ()),
                ("admin_applications", "Applications", "files",
                 ("view_application", "loan_form", "print_application")),
                ("users", "Staff & Reviewers", "users", ()),
                ("permissions", "Permissions", "key", ()),
                ("admin_settings", "Settings", "database", ()),
                ("reports", "Reports", "chart", ()),
                ("audit_log", "Audit Log", "clock", ()),
                ("deleted_applications", "Deleted", "trash", ())]
    if role == "Reviewer":
        return [("reviewer_dashboard", "Dashboard", "grid", ()),
                ("reviewer_applications", "Assigned Applications", "files",
                 ("view_application", "print_application"))]
    return [("staff_dashboard", "Dashboard", "grid", ()),
            ("staff_applications", "My Applications", "files",
             ("view_application", "loan_form", "print_application"))]


@app.context_processor
def inject_globals():
    role = current_user.role if current_user and current_user.is_authenticated else None
    return {"STATUSES": STATUSES, "USING_SQLITE": USING_SQLITE, "ON_VERCEL": ON_VERCEL,
            "FORM_SECTIONS": FORM_SECTIONS, "FIELD_LABELS": FIELD_LABELS,
            "PHOTO_SLOTS": PHOTO_SLOTS, "PHOTO_LABELS": PHOTO_LABELS, "can": can,
            "ACTION_LABELS": ACTION_LABELS, "ROLES": ROLES, "PORTAL_INFO": PORTAL_INFO,
            "portal": PORTAL_INFO.get(role), "portal_key": SECTION_OF_ROLE.get(role),
            "nav": nav_items(role) if role else []}


MIGRATION_PENDING = False


@app.before_request
def migration_gate():
    if MIGRATION_PENDING and request.endpoint != "static":
        return render_template("message.html", title="Database update pending",
                               msg="Database ka naya update abhi apply nahi hua. Admin se rabta karein."), 503


SECTION_PREFIX = {"/admin": "Admin", "/reviewer": "Reviewer", "/staff": "Staff"}


@app.before_request
def section_gate():
    """Keep the three sections apart on the server, whatever URL is typed in."""
    if request.endpoint in (None, "static"):
        return None
    if current_user.is_authenticated and session.get("portal") != current_user.role:
        # role changed by Admin, or session from before sections existed: log in again
        logout_user()
        session.pop("portal", None)
        flash("Dobara login karein.")
        return redirect(url_for("login"))
    for prefix, role in SECTION_PREFIX.items():
        if request.path == prefix or request.path.startswith(prefix + "/"):
            if not current_user.is_authenticated:
                return redirect(url_for("login_portal", portal=SECTION_OF_ROLE[role]))
            if current_user.role != role:
                abort(403)
    return None


# ---------------------------------------------------------------- auth
def home_url():
    return url_for(PORTAL_INFO[current_user.role]["home"])


@app.route("/login", methods=["GET", "POST"])
def login():
    """Entry page with the three separate sections."""
    if current_user.is_authenticated:
        return redirect(home_url())
    return render_template("login_choose.html")


@app.route("/login/<portal>", methods=["GET", "POST"])
def login_portal(portal):
    role = PORTALS.get(portal)
    if not role:
        abort(404)
    if current_user.is_authenticated:
        return redirect(home_url())
    info = PORTAL_INFO[role]
    if request.method == "POST":
        username = request.form.get("username", "").strip()[:80]
        ip = client_ip()
        if login_blocked(username, ip):
            flash(f"Bohat zyada ghalat koshishein. {int(LOGIN_WINDOW.total_seconds() // 60)} minute baad try karein.")
            return render_template("login.html", portal_key=portal, info=info), 429
        u = User.query.filter_by(username=username).first()
        password = request.form.get("password", "")
        # Always run one hash check so response time doesn't reveal which usernames exist.
        ok = u.check_password(password) if u else (check_password_hash(_DUMMY_HASH, password) and False)
        if u and u.active and not u.deleted_at and ok:
            if u.role != role:
                log_security(f"LOGIN_WRONG_SECTION user={username[:60]} section={portal} ip={ip}", user_id=u.id)
                flash(f"Ye account {info['title']} ka nahi hai. Apne section ka login istemal karein.")
                return render_template("login.html", portal_key=portal, info=info), 403
            login_user(u)
            session["portal"] = u.role
            set_db_actor(u)
            log_security(f"Login section={portal} ip={ip}", user_id=u.id)
            return redirect(url_for(info["home"]))
        log_security(f"LOGIN_FAIL user={username[:60]} ip={ip}", user_id=u.id if u else None)
        flash("Username ya password ghalat hai, ya account disabled hai.")
    return render_template("login.html", portal_key=portal, info=info)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    section = SECTION_OF_ROLE.get(current_user.role, "staff")
    logout_user()
    session.pop("portal", None)
    return redirect(url_for("login_portal", portal=section))


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        if not current_user.check_password(request.form.get("current", "")):
            flash("Purana password ghalat hai.")
        elif len(request.form.get("new", "")) < 8:
            flash("Naya password kam az kam 8 characters ka ho.")
        elif request.form.get("new") != request.form.get("confirm"):
            flash("Dono naye password match nahi karte.")
        else:
            me = current_user._get_current_object()
            me.set_password(request.form["new"])
            audit("user.password_changed", entity="user", entity_id=me.id)
            db.session.commit()
            login_user(me)   # keep this session; all other sessions are now logged out
            session["portal"] = me.role
            flash("Password change ho gaya.")
            return redirect(home_url())
    return render_template("change_password.html")


# ---------------------------------------------------------------- applications
@app.route("/")
@login_required
def dashboard():
    """Old address: sends each user to their own section."""
    return redirect(home_url())


def search(query, q):
    if not q:
        return query
    like = f"%{q}%"
    digits = "".join(ch for ch in q if ch.isdigit())
    conds = [Application.applicant_name.ilike(like), Application.app_id.ilike(like),
             Application.loan_number.ilike(like), Application.cnic.ilike(like)]
    if digits and digits != q:
        conds.append(Application.cnic.ilike(f"%{digits}%"))   # "42501-1470957-1" finds stored digits
    return query.filter(or_(*conds))


def list_page(base, title, eyebrow, endpoint, **extra):
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    counts = {s: base.filter(Application.status == s).count() for s in STATUSES}
    total = base.count()
    query = search(base, q)
    if status:
        query = query.filter(Application.status == status)
    apps = query.order_by(Application.updated_at.desc()).limit(200).all()
    return render_template("applications.html", apps=apps, total=total, counts=counts, q=q, status=status,
                           names=usernames(), title=title, eyebrow=eyebrow, list_endpoint=endpoint,
                           extra_args={k: v for k, v in extra.items() if k.startswith("arg_")}, **extra)


def live(query):
    return query.filter(Application.deleted_at.is_(None))


# ------------------------------- Admin section
@app.route("/admin")
@login_required
def admin_dashboard():
    base = live(Application.query)
    counts = {s: base.filter(Application.status == s).count() for s in STATUSES}
    queue = (base.filter(or_(Application.status.in_(["Submitted", "Submitted Again"]),
                             (Application.status == "Pending Review") & Application.reviewer_id.is_(None)))
             .order_by(Application.updated_at.desc()).limit(20).all())
    users_by_role = {r: User.query.filter(User.role == r, User.active.is_(True)).count() for r in ROLES}
    recent_audit = AuditLog.query.order_by(AuditLog.id.desc()).limit(12).all()
    recent = base.order_by(Application.updated_at.desc()).limit(8).all()
    return render_template("admin_dashboard.html", counts=counts, total=base.count(), queue=queue,
                           users_by_role=users_by_role, recent_audit=recent_audit, recent=recent,
                           names=usernames())


@app.route("/admin/applications")
@login_required
def admin_applications():
    return list_page(live(visible_apps()), "Saari applications", "Admin", "admin_applications",
                     show_new=can("app.create"), show_tools=True)


# ------------------------------- Reviewer section
def assigned_to_me():
    return live(Application.query.filter(Application.reviewer_id == current_user.id))


@app.route("/reviewer")
@login_required
def reviewer_dashboard():
    base = assigned_to_me()
    waiting = (base.filter(Application.status.in_(["Submitted", "Pending Review", "Submitted Again"]))
               .order_by(Application.updated_at).all())
    in_review = base.filter(Application.status == "In Review").order_by(Application.updated_at).all()
    stats = {"assigned": base.count(), "waiting": len(waiting), "in_review": len(in_review),
             "correction": base.filter(Application.status == "Correction Required").count(),
             "approved": base.filter(Application.approved_by == current_user.id).count(),
             "rejected": base.filter(Application.status == "Rejected").count()}
    return render_template("reviewer_dashboard.html", waiting=waiting, in_review=in_review, stats=stats,
                           names=usernames())


@app.route("/reviewer/applications")
@login_required
def reviewer_applications():
    return list_page(assigned_to_me(), "Mujhe assigned applications", "Reviewer", "reviewer_applications")


# ------------------------------- Staff section
def my_apps():
    uid = current_user.id
    return live(Application.query.filter(or_(Application.created_by == uid, Application.assigned_to == uid)))


@app.route("/staff")
@login_required
def staff_dashboard():
    base = my_apps()
    corrections = base.filter(Application.status == "Correction Required").order_by(Application.updated_at).all()
    open_notes = {}
    for a in corrections:
        n = (ReviewNote.query.filter_by(application_id=a.id, kind="correction", resolved_at=None)
             .order_by(ReviewNote.id.desc()).first())
        open_notes[a.id] = n.body if n else ""
    drafts = base.filter(Application.status == "Draft").order_by(Application.updated_at.desc()).all()
    progress = ["Submitted", "Pending Review", "In Review", "Submitted Again"]
    done = ["Approved", "Ready for Disbursement", "Added to Disbursement Sheet", "Disbursed", "Completed"]
    stats = {"total": base.count(), "drafts": len(drafts), "corrections": len(corrections),
             "progress": base.filter(Application.status.in_(progress)).count(),
             "approved": base.filter(Application.status.in_(done)).count(),
             "rejected": base.filter(Application.status == "Rejected").count()}
    recent = base.order_by(Application.updated_at.desc()).limit(10).all()
    return render_template("staff_dashboard.html", corrections=corrections, open_notes=open_notes,
                           drafts=drafts, stats=stats, recent=recent, names=usernames())


@app.route("/staff/applications")
@login_required
def staff_applications():
    scope = "all" if request.args.get("scope") == "all" and can("app.view_all") else "mine"
    base = live(visible_apps()) if scope == "all" else my_apps()
    title = "Saari applications (search)" if scope == "all" else "Meri applications"
    return list_page(base, title, "Staff", "staff_applications", show_new=can("app.create"),
                     scope=scope, arg_scope=scope)


@app.route("/application/new", methods=["POST", "GET"])
@requires("app.create")
def new_application():
    if request.method == "GET":
        return redirect(url_for("dashboard"))
    a = Application(app_id=next_app_id(), status="Draft", created_by=current_user.id,
                    updated_by=current_user.id, form_json="{}")
    db.session.add(a)
    db.session.flush()
    audit("application.created", a, new={"app_id": a.app_id})
    db.session.commit()
    return redirect(url_for("loan_form", app_id=a.id))


def history_for(a):
    rows = [{"who": h.actor_name or "?", "action": ACTION_LABELS.get(h.action, h.action),
             "reason": h.reason,
             "detail": h.new_value if h.action in ("application.status", "application.loan_number") else None,
             "at": h.created_at}
            for h in AuditLog.query.filter_by(application_id=a.id).order_by(AuditLog.id.desc()).limit(50)]
    names = usernames()
    rows += [{"who": names.get(l.user_id, "?"), "action": l.action, "reason": None, "detail": None,
              "at": l.created_at}
             for l in Log.query.filter_by(application_id=a.id).order_by(Log.id.desc()).limit(30)]
    return sorted(rows, key=lambda r: r["at"] or datetime.min, reverse=True)[:60]


@app.route("/application/<int:app_id>")
@login_required
def view_application(app_id):
    a = get_app(app_id, allow_deleted=True)
    docs = a.active_docs().all()
    notes = ReviewNote.query.filter_by(application_id=a.id).order_by(ReviewNote.id.desc()).all()
    if not can("app.review"):
        notes = [n for n in notes if n.kind != "note"]
    active = User.query.filter(User.active.is_(True), User.deleted_at.is_(None)).order_by(User.username).all()
    return render_template("application.html", a=a, docs=docs, history=history_for(a), notes=notes,
                           staff=[u for u in active if u.role in ("Staff", "Admin")],
                           reviewers=[u for u in active if u.role in ("Reviewer", "Admin")],
                           names=usernames(), transitions=allowed_transitions(a),
                           editable=can_edit_form(a),
                           can_note=can("app.review") and (current_user.role == "Admin"
                                                            or a.reviewer_id == current_user.id),
                           duplicates=cnic_matches(a.cnic, a.id))


# Old URL kept so existing links still work.
@app.route("/application/<int:app_id>/edit")
@login_required
def edit_application(app_id):
    return redirect(url_for("view_application", app_id=app_id))


def form_payload(a):
    """Everything static/loanform.js needs to draw the 3 scanned pages."""
    return {"appId": a.app_id, "values": a.form_values,
            "photos": {k: url_for("document", doc_id=p.id, inline=1)
                       for k in PHOTO_KINDS if (p := a.latest_photo(k))}}


def replace_photo(a, kind, f):
    """Upload a new picture for a slot. The old one is kept as history (marked replaced)."""
    old = a.active_docs(kind).all()
    doc, err = save_file(a, f, kind)
    if err:
        return err
    db.session.flush()
    for d in old:
        d.replaced_by, d.replaced_at = doc.id, datetime.utcnow()
    audit("document.replaced" if old else "document.uploaded", a, entity="document", entity_id=doc.id,
          old={"ids": [d.id for d in old]} if old else None, new={"kind": kind, "name": doc.original_name})
    return None


def soft_delete_doc(a, d):
    d.deleted_at, d.deleted_by = datetime.utcnow(), current_user.id
    audit("document.deleted", a, entity="document", entity_id=d.id,
          old={"kind": d.kind, "name": d.original_name})


@app.route("/application/<int:app_id>/form", methods=["GET", "POST"])
@login_required
def loan_form(app_id):
    a = get_app(app_id)
    if not can_edit_form(a):
        if request.method == "GET":
            flash("Is status mein ye application edit nahi ho sakti.")
            return redirect(url_for("view_application", app_id=a.id))
        abort(403)
    if request.method == "GET":
        return render_template("original_form.html", a=a, payload=form_payload(a))

    wants_json = request.headers.get("X-Requested-With") == "fetch"

    def fail(msg):
        if wants_json:
            return jsonify(ok=False, error=msg), 400
        flash(msg)
        return redirect(url_for("loan_form", app_id=a.id))

    before = a.form_values
    values = dict(before)
    values.pop("full_name", None)
    for name in FIELD_NAMES:
        if name in request.form:
            values[name] = request.form.get(name, "").strip()
    for name in ("cnic", "g1cnic", "g2cnic"):
        values[name] = "".join(ch for ch in values.get(name, "") if ch.isdigit())[:13]
    if not values.get("name"):
        return fail("Applicant ka naam (Name) zaroori hai.")
    cnic = values.get("cnic", "")

    errors = []
    has_files = any(f and f.filename for f in request.files.values())
    if has_files and not can("doc.upload"):
        errors.append("Aapko files upload karne ki permission nahi.")
    elif can("doc.upload"):
        for kind in PHOTO_KINDS:
            f = request.files.get("photo_" + kind)
            if f and f.filename:
                err = replace_photo(a, kind, f)
                if err:
                    errors.append(f"{PHOTO_LABELS[kind]}: {err}")
            elif request.form.get("remove_" + kind) == "1":
                for d in a.active_docs(kind).all():
                    soft_delete_doc(a, d)
        for f in request.files.getlist("documents"):
            if f and f.filename:
                doc, err = save_file(a, f, "document")
                if err:
                    errors.append(f"{f.filename}: {err}")
                else:
                    db.session.flush()
                    audit("document.uploaded", a, entity="document", entity_id=doc.id,
                          new={"kind": "document", "name": doc.original_name})

    changed = {k: [before.get(k, ""), values.get(k, "")] for k in FIELD_NAMES
               if (before.get(k) or "") != (values.get(k) or "")}
    a.applicant_name, a.cnic = values["name"], cnic
    a.loan_amount = parse_amount(values.get("amount"))
    a.form_json = json.dumps(values, ensure_ascii=False)
    a.updated_by, a.updated_at = current_user.id, datetime.utcnow()
    a.version = (a.version or 1) + 1
    if changed:
        audit("application.edited", a, old={k: v[0] for k, v in changed.items()},
              new={k: v[1] for k, v in changed.items()})
    db.session.commit()

    dups = cnic_matches(cnic, a.id)
    msg = "Form save ho gaya."
    if dups:
        msg += " Dhyan dein: is CNIC par pehle se application mojood hai — " + ", ".join(
            f"{d[0]} ({d[1]})" for d in dups[:5]) + "."
    if errors:
        msg += " Lekin: " + "; ".join(errors)
    flash(msg)
    target = url_for("view_application", app_id=a.id)
    return jsonify(ok=True, redirect=target) if wants_json else redirect(target)


@app.route("/application/<int:app_id>/print")
@login_required
def print_application(app_id):
    a = get_app(app_id)
    audit("application.printed", a)
    db.session.commit()
    return render_template("print.html", a=a, payload=form_payload(a))


@app.route("/application/<int:app_id>/status", methods=["POST"])
@login_required
def update_status(app_id):
    a = get_app(app_id)
    s = request.form.get("status", "")
    reason = request.form.get("reason", "").strip()
    if s not in allowed_transitions(a):
        abort(403)
    if s in REASON_REQUIRED and not reason:
        flash("Is status ke liye wajah (reason) likhna zaroori hai.")
        return redirect(url_for("view_application", app_id=a.id))
    old = a.status
    now = datetime.utcnow()
    if s == "Rejected":
        a.rejection_reason = reason
    if s == "Correction Required":
        db.session.add(ReviewNote(application_id=a.id, author_id=current_user.id, kind="correction", body=reason))
    if s in ("Submitted", "Submitted Again"):
        a.submitted_at = now
        for n in ReviewNote.query.filter_by(application_id=a.id, kind="correction", resolved_at=None):
            n.resolved_at, n.resolved_by = now, current_user.id
    if s == "Approved":
        a.approved_by, a.approved_at = current_user.id, now
    a.status, a.updated_by, a.updated_at = s, current_user.id, now
    audit("application.status", a, reason=reason or None, old={"status": old}, new={"status": s})
    db.session.commit()
    flash("Status update ho gaya.")
    return redirect(url_for("view_application", app_id=a.id))


@app.route("/application/<int:app_id>/assign", methods=["POST"])
@requires("app.assign")
def assign_application(app_id):
    a = get_app(app_id)

    def pick(field, roles):
        uid = request.form.get(field, "")
        if not uid.isdigit():
            return None
        u = User.query.filter(User.id == int(uid), User.active.is_(True), User.role.in_(roles)).first()
        return u.id if u else None

    old = {"assigned_to": a.assigned_to, "reviewer_id": a.reviewer_id, "status": a.status}
    if "staff_id" in request.form:
        a.assigned_to = pick("staff_id", ("Staff", "Admin"))
    if "reviewer_id" in request.form:
        a.reviewer_id = pick("reviewer_id", ("Reviewer", "Admin"))
        if a.reviewer_id and a.status in ("Submitted", "Submitted Again"):
            a.status = "Pending Review"
    new = {"assigned_to": a.assigned_to, "reviewer_id": a.reviewer_id, "status": a.status}
    if new != old:
        audit("application.assigned", a, old=old, new=new)
        db.session.commit()
        flash("Assignment update ho gayi.")
    return redirect(url_for("view_application", app_id=a.id))


@app.route("/application/<int:app_id>/loan-number", methods=["POST"])
@login_required
def set_loan_number(app_id):
    a = get_app(app_id)
    if not (can_edit_form(a) or can("disbursement.manage")):
        abort(403)
    value = request.form.get("loan_number", "").strip()[:30] or None
    back = redirect(url_for("view_application", app_id=a.id))
    if value == a.loan_number:
        return back
    if value and Application.query.filter(Application.loan_number == value, Application.id != a.id,
                                          Application.deleted_at.is_(None)).first():
        flash(f"Loan number {value} pehle se kisi aur application par hai.")
        return back
    old = a.loan_number
    a.loan_number, a.updated_by, a.updated_at = value, current_user.id, datetime.utcnow()
    audit("application.loan_number", a, old={"loan_number": old}, new={"loan_number": value})
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(f"Loan number {value} pehle se kisi aur application par hai.")
        return back
    flash("Loan number save ho gaya.")
    return back


@app.route("/application/<int:app_id>/note", methods=["POST"])
@requires("app.review")
def add_note(app_id):
    a = get_app(app_id)
    if current_user.role != "Admin" and a.reviewer_id != current_user.id:
        abort(403)
    body = request.form.get("body", "").strip()
    kind = request.form.get("kind") if request.form.get("kind") in ("note", "recommend") else "note"
    if body:
        n = ReviewNote(application_id=a.id, author_id=current_user.id, kind=kind, body=body[:4000])
        db.session.add(n)
        db.session.flush()
        audit("application.note", a, entity="review_note", entity_id=n.id, new={"kind": kind})
        db.session.commit()
        flash("Note save ho gaya.")
    return redirect(url_for("view_application", app_id=a.id))


@app.route("/application/<int:app_id>/delete", methods=["POST"])
@requires("app.delete")
def delete_application(app_id):
    """Soft delete: the application and its files are hidden, never erased. Admin can restore."""
    a = get_app(app_id)
    a.deleted_at, a.deleted_by = datetime.utcnow(), current_user.id
    audit("application.deleted", a, reason=request.form.get("reason", "").strip() or None)
    db.session.commit()
    flash(a.app_id + " delete ho gayi (Admin isse 'Deleted' list se wapas la sakta hai).")
    return redirect(url_for("admin_applications"))


@app.route("/admin/deleted")
@requires("app.restore")
def deleted_applications():
    apps = (Application.query.filter(Application.deleted_at.isnot(None))
            .order_by(Application.deleted_at.desc()).all())
    return render_template("deleted.html", apps=apps, names=usernames())


@app.route("/application/<int:app_id>/restore", methods=["POST"])
@requires("app.restore")
def restore_application(app_id):
    a = get_app(app_id, allow_deleted=True)
    if a.deleted_at:
        a.deleted_at, a.deleted_by = None, None
        audit("application.restored", a)
        db.session.commit()
        flash(a.app_id + " restore ho gayi.")
    return redirect(url_for("view_application", app_id=a.id))


# ---------------------------------------------------------------- documents / pictures
@app.route("/application/<int:app_id>/upload", methods=["POST"])
@requires("doc.upload")
def upload(app_id):
    a = get_app(app_id)
    if not can_edit_form(a):
        abort(403)
    kind = request.form.get("kind") if request.form.get("kind") in PHOTO_KINDS else "document"
    files = [f for f in request.files.getlist("file") if f and f.filename]
    if not files:
        flash("File select karein.")
        return redirect(url_for("view_application", app_id=a.id))
    if kind != "document":
        files = files[:1]   # one picture per photo slot; it replaces the old one (old kept as history)
    ok = 0
    for f in files:
        if kind != "document":
            err = replace_photo(a, kind, f)
        else:
            doc, err = save_file(a, f, kind)
            if not err:
                db.session.flush()
                audit("document.uploaded", a, entity="document", entity_id=doc.id,
                      new={"kind": kind, "name": doc.original_name})
        if err:
            flash(f"{f.filename}: {err}")
        else:
            ok += 1
    a.updated_by, a.updated_at = current_user.id, datetime.utcnow()
    db.session.commit()
    if ok:
        flash(f"{ok} file upload ho gayi.")
    return redirect(url_for("view_application", app_id=a.id))


@app.route("/document/<int:doc_id>")
@login_required
def document(doc_id):
    d = db.get_or_404(Document, doc_id)
    get_app(d.application_id, allow_deleted=can("app.restore"))
    if (d.deleted_at or d.replaced_by) and not can("app.restore"):
        abort(404)
    inline = request.args.get("inline") == "1" and (d.is_image or d.content_type == "application/pdf")
    resp = send_file(io.BytesIO(read_file(d)), mimetype=d.content_type, download_name=d.original_name,
                     as_attachment=not inline)
    resp.headers["Cache-Control"] = "private, no-store"   # CNIC photos must not stay in a shared PC's cache
    return resp


@app.route("/document/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(doc_id):
    d = db.get_or_404(Document, doc_id)
    a = get_app(d.application_id)
    if not ((can("doc.upload") and can_edit_form(a)) or can("app.delete")):
        abort(403)
    if not d.deleted_at:
        soft_delete_doc(a, d)
        db.session.commit()
        flash("File delete ho gayi.")
    return redirect(url_for("view_application", app_id=a.id))


# ---------------------------------------------------------------- admin
def active_admins():
    return User.query.filter(User.role == "Admin", User.active.is_(True), User.deleted_at.is_(None)).count()


@app.route("/admin/users", methods=["GET", "POST"])
@requires("users.manage")
def users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "Staff")
        full_name = request.form.get("full_name", "").strip()[:120] or None
        if role not in ROLES or not username or len(password) < 8:
            flash("Username aur kam az kam 8 character password zaroori hai.")
        elif User.query.filter_by(username=username).first():
            flash("Ye username pehle se mojood hai.")
        else:
            u = User(username=username, role=role, full_name=full_name)
            u.set_password(password)
            db.session.add(u)
            db.session.flush()
            audit("user.created", entity="user", entity_id=u.id, new={"username": username, "role": role})
            db.session.commit()
            flash(f"User '{username}' add ho gaya.")
        return redirect(url_for("users"))
    return render_template("users.html", users=User.query.order_by(User.username).all())


@app.route("/admin/users/<int:uid>/role", methods=["POST"])
@requires("users.manage")
def change_role(uid):
    u = db.get_or_404(User, uid)
    role = request.form.get("role")
    if role not in ROLES or role == u.role:
        return redirect(url_for("users"))
    if u.id == current_user.id:
        flash("Apna role khud nahi badal sakte.")
    elif u.role == "Admin" and active_admins() <= 1:
        flash("Kam az kam ek active Admin zaroori hai.")
    else:
        old = u.role
        u.role = role
        audit("user.role_changed", entity="user", entity_id=u.id, old={"role": old}, new={"role": role})
        db.session.commit()
        flash(f"{u.username} ab {role} hai.")
    return redirect(url_for("users"))


@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
@requires("users.manage")
def toggle_user(uid):
    u = db.get_or_404(User, uid)
    if u.id == current_user.id:
        flash("Apna account disable nahi kar sakte.")
    elif u.active and u.role == "Admin" and active_admins() <= 1:
        flash("Kam az kam ek active Admin zaroori hai.")
    else:
        u.active = not u.active
        audit("user.status_changed", entity="user", entity_id=u.id, old={"active": not u.active},
              new={"active": u.active})
        db.session.commit()
    return redirect(url_for("users"))


@app.route("/admin/users/<int:uid>/password", methods=["POST"])
@requires("users.manage")
def reset_password(uid):
    u = db.get_or_404(User, uid)
    p = request.form.get("password", "")
    if len(p) < 8:
        flash("Password kam az kam 8 characters ka ho.")
    else:
        u.set_password(p)
        audit("user.password_reset", entity="user", entity_id=u.id)
        db.session.commit()
        flash(f"{u.username} ka password change ho gaya.")
    return redirect(url_for("users"))


@app.route("/admin/permissions", methods=["GET", "POST"])
@requires("users.manage")
def permissions():
    assignable = [p for p in PERMISSIONS if p not in ADMIN_ONLY]
    current = {r: {p for (p,) in db.session.query(RolePermission.permission).filter_by(role=r)}
               for r in ("Staff", "Reviewer")}
    if request.method == "POST":
        for role in ("Staff", "Reviewer"):
            wanted = {p for p in request.form.getlist(role) if p in assignable}
            if wanted == current[role]:
                continue
            for p in current[role] - wanted:
                db.session.delete(db.session.get(RolePermission, (role, p)))
            for p in wanted - current[role]:
                db.session.add(RolePermission(role=role, permission=p))
            audit("permissions.changed", entity="role", reason=role,
                  old={"permissions": sorted(current[role])}, new={"permissions": sorted(wanted)})
        db.session.commit()
        flash("Permissions save ho gayin.")
        return redirect(url_for("permissions"))
    return render_template("permissions.html", assignable=assignable, current=current, labels=PERMISSIONS)


@app.route("/admin/settings")
@requires("settings.manage")
def admin_settings():
    return render_template("settings.html",
                           purposes=PurposeCategory.query.order_by(PurposeCategory.sort_order, PurposeCategory.id).all(),
                           fees=FeeCategory.query.order_by(FeeCategory.sort_order, FeeCategory.id).all())


def _money(v):
    try:
        x = float(str(v).replace(",", "").strip())
        return x if x >= 0 else None
    except ValueError:
        return None


@app.route("/admin/settings/purpose", methods=["POST"])
@app.route("/admin/settings/purpose/<int:pid>", methods=["POST"])
@requires("settings.manage")
def save_purpose(pid=None):
    name = request.form.get("name", "").strip()[:80]
    if not name:
        flash("Naam zaroori hai.")
        return redirect(url_for("admin_settings"))
    clash = PurposeCategory.query.filter(PurposeCategory.name == name, PurposeCategory.id != (pid or 0)).first()
    if clash:
        flash(f"'{name}' pehle se mojood hai.")
        return redirect(url_for("admin_settings"))
    if pid:
        p = db.get_or_404(PurposeCategory, pid)
        old = {"name": p.name, "active": p.active}
        p.name, p.active = name, request.form.get("active") == "1"
        audit("settings.purpose_changed", entity="purpose_category", entity_id=p.id, old=old,
              new={"name": p.name, "active": p.active})
    else:
        top = db.session.query(db.func.max(PurposeCategory.sort_order)).scalar() or 0
        p = PurposeCategory(name=name, sort_order=top + 1)
        db.session.add(p)
        db.session.flush()
        audit("settings.purpose_added", entity="purpose_category", entity_id=p.id, new={"name": name})
    db.session.commit()
    flash("Purpose save ho gaya. Purani applications par likha purpose nahi badalta.")
    return redirect(url_for("admin_settings"))


@app.route("/admin/settings/fee", methods=["POST"])
@app.route("/admin/settings/fee/<int:fid>", methods=["POST"])
@requires("settings.manage")
def save_fee(fid=None):
    name = request.form.get("name", "").strip()[:80]
    fee = _money(request.form.get("fee_amount", ""))
    loan = _money(request.form.get("loan_amount", "")) if request.form.get("loan_amount", "").strip() else None
    if not name or fee is None:
        flash("Naam aur sahih fee amount zaroori hai.")
        return redirect(url_for("admin_settings"))
    clash = FeeCategory.query.filter(FeeCategory.name == name, FeeCategory.id != (fid or 0)).first()
    if clash:
        flash(f"'{name}' pehle se mojood hai.")
        return redirect(url_for("admin_settings"))
    if fid:
        f = db.get_or_404(FeeCategory, fid)
        old = {"name": f.name, "loan_amount": float(f.loan_amount) if f.loan_amount is not None else None,
               "fee_amount": float(f.fee_amount), "active": f.active}
        f.name, f.loan_amount, f.fee_amount = name, loan, fee
        f.active = request.form.get("active") == "1"
        audit("settings.fee_changed", entity="fee_category", entity_id=f.id, old=old,
              new={"name": name, "loan_amount": loan, "fee_amount": fee, "active": f.active})
    else:
        top = db.session.query(db.func.max(FeeCategory.sort_order)).scalar() or 0
        f = FeeCategory(name=name, loan_amount=loan, fee_amount=fee, sort_order=top + 1)
        db.session.add(f)
        db.session.flush()
        audit("settings.fee_added", entity="fee_category", entity_id=f.id,
              new={"name": name, "loan_amount": loan, "fee_amount": fee})
    db.session.commit()
    flash("Fee category save ho gayi. Purani applications ki fee wahi rehti hai jo us waqt lagi thi.")
    return redirect(url_for("admin_settings"))


@app.route("/admin/audit")
@requires("audit.view")
def audit_log():
    q = AuditLog.query
    action = request.args.get("action", "").strip()
    actor = request.args.get("actor", "").strip()
    app_ref = request.args.get("app", "").strip()
    if action:
        q = q.filter(AuditLog.action == action)
    if actor:
        q = q.filter(AuditLog.actor_name.ilike(f"%{actor}%"))
    if app_ref:
        ids = [a.id for a in Application.query.filter(or_(Application.app_id.ilike(f"%{app_ref}%"),
                                                           Application.loan_number.ilike(f"%{app_ref}%")))]
        q = q.filter(AuditLog.application_id.in_(ids or [-1]))
    page = max(1, request.args.get("page", 1, type=int))
    rows = q.order_by(AuditLog.id.desc()).offset((page - 1) * 100).limit(101).all()
    app_ids = {a.id: a.app_id for a in Application.query.filter(
        Application.id.in_({r.application_id for r in rows if r.application_id} or {-1}))}
    return render_template("audit.html", rows=rows[:100], more=len(rows) > 100, page=page, app_ids=app_ids,
                           action=action, actor=actor, app_ref=app_ref)


@app.route("/admin/export")
@requires("export")
def export_csv():
    apps = Application.query.filter(Application.deleted_at.is_(None)).order_by(Application.id).all()
    known = FIELD_NAMES
    keys = known + sorted({k for a in apps for k in a.data} - set(known))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([csv_safe(x) for x in ["Application ID", "Loan No.", "Status", "Created", "Updated"]
                + [FIELD_LABELS.get(k, k) for k in keys]])
    for a in apps:
        d = a.data
        w.writerow([csv_safe(x) for x in [a.app_id, a.loan_number, a.status, a.created_at, a.updated_at]
                    + [d.get(k, "") for k in keys]])
    audit("export.csv", new={"rows": len(apps)})
    db.session.commit()
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=applications.csv"})


@app.route("/admin/reports")
@requires("reports.view")
def reports():
    from sqlalchemy import func
    live = Application.deleted_at.is_(None)
    by_status = (db.session.query(Application.status, func.count(Application.id)).filter(live)
                 .group_by(Application.status).all())
    by_staff = (db.session.query(User.username, func.count(Application.id))
                .outerjoin(Application, (Application.assigned_to == User.id) & live).group_by(User.id).all())
    return render_template("reports.html", by_status=by_status, by_staff=by_staff,
                           total=Application.query.filter(live).count())


@app.route("/admin/backup")
@requires("export")
def backup_export():
    payload = {
        "created_at": datetime.utcnow().isoformat() + "Z",
        "applications": [{"id": a.id, "app_id": a.app_id, "loan_number": a.loan_number,
                          "name": a.applicant_name, "cnic": a.cnic, "status": a.status,
                          "assigned_to": a.assigned_to, "reviewer_id": a.reviewer_id, "form": a.data,
                          "created_by": a.created_by, "deleted": bool(a.deleted_at),
                          "created_at": a.created_at.isoformat() if a.created_at else None}
                         for a in Application.query.all()],
        "users": [{"id": u.id, "username": u.username, "role": u.role, "active": u.active}
                  for u in User.query.all()],
        "documents": [{"id": d.id, "application_id": d.application_id, "name": d.original_name,
                       "kind": d.kind, "storage": d.storage, "size": d.size,
                       "replaced_by": d.replaced_by, "deleted": bool(d.deleted_at)}
                      for d in Document.query.all()],
    }
    audit("export.backup")
    db.session.commit()
    resp = jsonify(payload)
    resp.headers["Content-Disposition"] = "attachment; filename=tooba-backup.json"
    return resp


# ---------------------------------------------------------------- errors / headers
@app.errorhandler(403)
def forbidden(e):
    return render_template("message.html", title="Access denied",
                           msg="Aapko is page ki permission nahi."), 403


@app.errorhandler(413)
def too_large(e):
    return render_template("message.html", title="File bohat bari hai",
                           msg="Ek waqt mein 15 MB se zyada upload nahi ho sakta."), 413


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if SECURE_COOKIES:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if current_user and current_user.is_authenticated and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'")
    return response


# ---------------------------------------------------------------- startup
def sqlite_add_missing_columns():
    """Local SQLite only: db.create_all() does not add new columns to old tables."""
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        for t in db.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(t.name)}
            for c in t.columns:
                if c.name not in have:
                    conn.execute(text(f'ALTER TABLE "{t.name}" ADD COLUMN "{c.name}" '
                                      f'{c.type.compile(db.engine.dialect)}'))


def sqlite_seed():
    """Local SQLite only. On PostgreSQL the migration seeds the same rows."""
    for role, perms in DEFAULT_ROLE_PERMS.items():
        for p in perms:
            if not db.session.get(RolePermission, (role, p)):
                db.session.add(RolePermission(role=role, permission=p))
    for f, t, p in DEFAULT_TRANSITIONS:
        if not db.session.get(StatusTransition, (f, t)):
            db.session.add(StatusTransition(from_status=f, to_status=t, permission=p))
    for i, name in enumerate(DEFAULT_PURPOSES, 1):
        if not PurposeCategory.query.filter_by(name=name).first():
            db.session.add(PurposeCategory(name=name, sort_order=i))
    for i, (loan, fee) in enumerate(DEFAULT_FEES, 1):
        name = f"Loan {loan:,}"
        if not FeeCategory.query.filter_by(name=name).first():
            db.session.add(FeeCategory(name=name, loan_amount=loan, fee_amount=fee, sort_order=i))
    Application.query.filter_by(status="Pending").update({"status": "Pending Review"})
    db.session.commit()


with app.app_context():
    if USING_SQLITE:
        db.create_all()
        sqlite_add_missing_columns()
        sqlite_seed()
    else:
        # PostgreSQL schema changes are applied as reviewed migrations (supabase/migrations),
        # never automatically. Until the Phase 3 migration is applied, show a maintenance page.
        MIGRATION_PENDING = not inspect(db.engine).has_table("audit_log")
    if not MIGRATION_PENDING:
        # Local SQLite only: without ADMIN_PASSWORD a random one is generated and printed once.
        admin_pw = os.getenv("ADMIN_PASSWORD", "")
        if not admin_pw and USING_SQLITE and not ON_VERCEL and not User.query.filter_by(role="Admin").first():
            admin_pw = secrets.token_urlsafe(12)
            print(f"[tooba] Local admin created: {os.getenv('ADMIN_USERNAME', 'admin')} / {admin_pw}")
        if admin_pw and not User.query.filter_by(role="Admin").first():
            admin = User(username=os.getenv("ADMIN_USERNAME", "admin"), role="Admin")
            admin.set_password(admin_pw)
            db.session.add(admin)
            db.session.commit()
    db.session.remove()

if __name__ == "__main__":
    app.run(debug=False)
