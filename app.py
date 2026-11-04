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
import gsheets

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
# Short passwords are allowed so staff can log in easily; the lock above (5 wrong tries)
# is what stops guessing.
MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "5"))
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
    "cheque.manage": "Disbursement sheet par cheque number likhna / badalna",
    "users.manage": "Users, roles aur permissions manage karna",
    "app.purge": "Application hamesha ke liye mitana",
}
# Admin-section powers are never assignable to Staff/Reviewer (their pages live under /admin).
ADMIN_ONLY = {"users.manage", "app.purge", "app.delete", "app.restore", "app.assign", "reports.view",
              "export", "audit.view", "settings.manage", "period.close", "gsheets.manage"}
# disbursement.manage and cheque.manage stay assignable (e.g. Reviewers enter cheque numbers).

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
    "Reviewer": {"app.review", "app.decide", "cheque.manage"},
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
    ("Approved", "Submitted Again", "app.edit_any"),
    ("Ready for Disbursement", "Submitted Again", "app.edit_any"),
    ("Rejected", "In Review", "app.decide"),
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
    "application.details": "Purpose / fee set ki",
    "application.rereview": "Raqam badli — dobara review ke liye bheji",
    "application.family_cnic": "Family CNIC ki zaroorat badli",
    "disbursement.created": "Disbursement sheet banayi",
    "disbursement.added": "Disbursement sheet mein add ki",
    "disbursement.removed": "Disbursement sheet se nikali",
    "disbursement.disbursed": "Raqam ada (disbursed) mark ki",
    "disbursement.closed": "Disbursement sheet close ki",
    "disbursement.reopened": "Disbursement sheet dobara kholi",
    "disbursement.deleted": "Disbursement sheet delete ki",
    "cheque.saved": "Cheque number likha",
    "cheque.status": "Cheque status badla",
    "cheque.cancelled": "Cheque cancel kiya",
    "gsheets.synced": "Google Sheets sync",
    "gsheets.year_created": "Google yearly sheet banayi",
}

# Single combined CNIC DOCUMENTS page: (group, section title, form field holding the name)
CNIC_GROUPS = [("app", "Applicant CNIC", "name"), ("w1", "First Witness CNIC", "g1name"),
               ("w2", "Second Witness CNIC", "g2name"), ("fam", "Family Member CNIC", None)]
CNIC_KINDS = [f"cnic_{g}_{side}" for g, _, _ in CNIC_GROUPS for side in ("front", "back")]
CNIC_LABELS = {f"cnic_{g}_{side}": f"{title} — {side.title()} Side"
               for g, title, _ in CNIC_GROUPS for side in ("front", "back")}
IMAGE_KINDS = set(PHOTO_KINDS) | set(CNIC_KINDS)
SLOT_KINDS = list(PHOTO_KINDS) + CNIC_KINDS          # one current picture per slot
FINANCIAL_LOCKED = {"Added to Disbursement Sheet", "Disbursed", "Completed"}
REREVIEW_FROM = {"Approved", "Ready for Disbursement"}

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


class DisbursementBatch(db.Model):
    __tablename__ = "disbursement_batch"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, unique=True, nullable=False)
    disb_date = db.Column(db.Date, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(10), default="open", nullable=False)
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    closed_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    closed_at = db.Column(db.DateTime)
    reopened_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    reopened_at = db.Column(db.DateTime)
    reopen_reason = db.Column(db.Text)
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    delete_reason = db.Column(db.Text)

    @property
    def closed(self):
        return self.status == "closed"


class DisbursementEntry(db.Model):
    __tablename__ = "disbursement_entry"
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("disbursement_batch.id"), nullable=False)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=False)
    name = db.Column(db.String(200))
    cnic = db.Column(db.String(30))
    phone = db.Column(db.String(30))
    amount = db.Column(db.Numeric(12, 2))
    purpose = db.Column(db.String(80))
    loan_number = db.Column(db.String(30))
    fee_amount = db.Column(db.Numeric(12, 2))
    added_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    added_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    disbursed_at = db.Column(db.DateTime)
    disbursed_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    removed_at = db.Column(db.DateTime)
    removed_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    remove_reason = db.Column(db.Text)


class Cheque(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("disbursement_entry.id"), nullable=False)
    cheque_no = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(12), default="Issued", nullable=False)
    cancel_reason = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    updated_at = db.Column(db.DateTime)
    cancelled_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    cancelled_at = db.Column(db.DateTime)


class GsheetYear(db.Model):
    __tablename__ = "gsheet_year"
    year = db.Column(db.Integer, primary_key=True, autoincrement=False)
    spreadsheet_id = db.Column(db.String(100))
    url = db.Column(db.Text)
    tab_ids = db.Column(db.JSON)
    status = db.Column(db.String(10), default="creating", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class GsheetSync(db.Model):
    __tablename__ = "gsheet_sync"
    year = db.Column(db.Integer, primary_key=True, autoincrement=False)
    month = db.Column(db.Integer, primary_key=True, autoincrement=False)
    dirty = db.Column(db.Boolean, default=True, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.Text)
    requested_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    synced_at = db.Column(db.DateTime)


class GsheetSyncLog(db.Model):
    __tablename__ = "gsheet_sync_log"
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer)
    month = db.Column(db.Integer)
    ok = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.Text)
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
    if kind in IMAGE_KINDS and ext not in IMAGE_EXT:
        return None, "Photo / CNIC ke liye JPG, PNG ya WEBP file chunein."
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
        # sheet statuses change only through the disbursement sheet, so sheet and status always agree
        sheet_only = {"Added to Disbursement Sheet", "Disbursed"}
        return [s for s in STATUSES if s != a.status and s not in sheet_only]
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
                ("disbursement_list", "Disbursement", "cash", ("disbursement_sheet",)),
                ("reports", "Reports", "chart", ()),
                ("audit_log", "Audit Log", "clock", ()),
                ("google_sheets", "Google Sheets", "refresh", ()),
                ("deleted_applications", "Deleted", "trash", ())]
    sheets = ([("disbursement_list", "Disbursement Sheets", "cash", ("disbursement_sheet",))]
              if can("disbursement.manage") or can("cheque.manage") else [])
    if role == "Reviewer":
        return [("reviewer_dashboard", "Dashboard", "grid", ()),
                ("reviewer_applications", "Assigned Applications", "files",
                 ("view_application", "print_application"))] + sheets
    return [("staff_dashboard", "Dashboard", "grid", ()),
            ("staff_applications", "My Applications", "files",
             ("view_application", "loan_form", "print_application"))] + sheets


@app.context_processor
def inject_globals():
    role = current_user.role if current_user and current_user.is_authenticated else None
    return {"STATUSES": STATUSES, "USING_SQLITE": USING_SQLITE, "ON_VERCEL": ON_VERCEL,
            "FORM_SECTIONS": FORM_SECTIONS, "FIELD_LABELS": FIELD_LABELS,
            "PHOTO_SLOTS": PHOTO_SLOTS, "PHOTO_LABELS": PHOTO_LABELS, "can": can,
            "ACTION_LABELS": ACTION_LABELS, "ROLES": ROLES, "PORTAL_INFO": PORTAL_INFO,
            "CNIC_GROUPS": CNIC_GROUPS, "MIN_PASSWORD_LEN": MIN_PASSWORD_LEN, "CNIC_LABELS": CNIC_LABELS, "money": money,
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
        elif len(request.form.get("new", "")) < MIN_PASSWORD_LEN:
            flash(f"Naya password kam az kam {MIN_PASSWORD_LEN} characters ka ho.")
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
    docs = [d for d in a.active_docs().all() if d.kind not in CNIC_KINDS]   # CNICs have their own card
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
                           cnic_docs={k: a.latest_photo(k) for k in CNIC_KINDS},
                           purposes=PurposeCategory.query.order_by(PurposeCategory.sort_order).all(),
                           fees=FeeCategory.query.order_by(FeeCategory.sort_order).all(),
                           can_details=a.status not in FINANCIAL_LOCKED and (can_edit_form(a) or can("app.edit_any")),
                           problems=submit_problems(a) if a.status in EDITABLE_STATUSES else [],
                           entry=DisbursementEntry.query.filter_by(application_id=a.id, removed_at=None).first(),
                           duplicates=cnic_matches(a.cnic, a.id))


# Old URL kept so existing links still work.
@app.route("/application/<int:app_id>/edit")
@login_required
def edit_application(app_id):
    return redirect(url_for("view_application", app_id=app_id))


def form_payload(a, with_cnic=False):
    """Everything static/loanform.js needs to draw the 3 scanned pages (+ the CNIC page)."""
    payload = {"appId": a.app_id, "values": a.form_values,
               "photos": {k: url_for("document", doc_id=p.id, inline=1)
                          for k in PHOTO_KINDS if (p := a.latest_photo(k))}}
    if with_cnic:
        payload["cnic"] = cnic_page_rows(a)
    return payload


def cnic_groups_for(a):
    return [grp for grp in CNIC_GROUPS if grp[0] != "fam" or a.family_cnic_required]


def cnic_page_rows(a):
    """Rows for the single CNIC DOCUMENTS page. Family Member only when the reviewer requires it."""
    vals = a.form_values
    rows = []
    for i, (g, title, name_field) in enumerate(cnic_groups_for(a), 1):
        row = {"section": i, "title": title.upper(), "name": vals.get(name_field, "") if name_field else ""}
        for side in ("front", "back"):
            d = a.latest_photo(f"cnic_{g}_{side}")
            row[side] = url_for("document", doc_id=d.id, inline=1) if d else None
        rows.append(row)
    return rows


def required_kinds(a):
    return [f"cnic_{g}_{side}" for g, _, _ in cnic_groups_for(a) for side in ("front", "back")]


def submit_problems(a):
    have = {d.kind for d in a.active_docs().all()}
    out = [CNIC_LABELS[k] for k in required_kinds(a) if k not in have]
    if not a.loan_amount:
        out.append("Loan amount")
    if not a.purpose_id:
        out.append("Loan purpose")
    if a.fee_amount is None:
        out.append("Form fee")
    return out


def auto_fee(a):
    """Pick the fee slab whose loan amount matches (only when none chosen yet)."""
    if a.fee_category_id or not a.loan_amount:
        return
    f = FeeCategory.query.filter(FeeCategory.active.is_(True), FeeCategory.loan_amount == a.loan_amount).first()
    if f:
        a.fee_category_id, a.fee_amount = f.id, f.fee_amount


def send_for_rereview(a, why):
    old = a.status
    a.status = "Submitted Again"
    a.approved_by = a.approved_at = None
    a.submitted_at = datetime.utcnow()
    audit("application.rereview", a, reason=why, old={"status": old}, new={"status": a.status})


def money(v):
    if v is None or v == "":
        return ""
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return str(v)


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
    new_amount = parse_amount(values.get("amount"))
    if a.status in FINANCIAL_LOCKED and new_amount != (int(a.loan_amount) if a.loan_amount is not None else None):
        db.session.rollback()
        return fail("Ye application disbursement sheet par hai; raqam badalne se pehle use sheet se nikalein.")
    money_changed = new_amount != (int(a.loan_amount) if a.loan_amount is not None else None)
    a.applicant_name, a.cnic = values["name"], cnic
    a.loan_amount = new_amount
    if money_changed:
        auto_fee(a)
        if a.status in REREVIEW_FROM:
            send_for_rereview(a, "Loan amount badla")
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
    return render_template("print.html", a=a, payload=form_payload(a, with_cnic=True))


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
    if s in ("Submitted", "Submitted Again"):
        problems = submit_problems(a)
        if problems:
            flash("Submit nahi ho saki — ye zaroori hai: " + "; ".join(problems))
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
    kind = request.form.get("kind") if request.form.get("kind") in SLOT_KINDS else "document"
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
        if role not in ROLES or not username or len(password) < MIN_PASSWORD_LEN:
            flash(f"Username aur kam az kam {MIN_PASSWORD_LEN} character password zaroori hai.")
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
    if len(p) < MIN_PASSWORD_LEN:
        flash(f"Password kam az kam {MIN_PASSWORD_LEN} characters ka ho.")
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
    year = request.args.get("year", type=int) or (datetime.utcnow() + timedelta(hours=5)).year
    return render_template("reports.html", r=report_data(year))


@app.route("/admin/reports/export")
@requires("reports.view")
def reports_export():
    kind = request.args.get("kind", "monthly")
    if kind not in REPORT_COLUMNS:
        abort(404)
    year = request.args.get("year", type=int) or (datetime.utcnow() + timedelta(hours=5)).year
    data = report_data(year)[kind]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([label for _, label in REPORT_COLUMNS[kind]])
    for row in data:
        w.writerow([csv_safe(row.get(k, "")) for k, _ in REPORT_COLUMNS[kind]])
    audit("export.csv", new={"report": kind, "year": year})
    db.session.commit()
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=report-{kind}-{year}.csv"})


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


# ---------------------------------------------------------------- application: details, family CNIC
@app.route("/application/<int:app_id>/details", methods=["POST"])
@login_required
def save_details(app_id):
    """Purpose and form fee. The chosen label/amount are copied onto the application, so
    later changes to the categories never change this application's history."""
    a = get_app(app_id)
    back = redirect(url_for("view_application", app_id=a.id))
    if not (can_edit_form(a) or (can("app.edit_any") and a.status not in FINANCIAL_LOCKED)):
        abort(403)
    if a.status in FINANCIAL_LOCKED:
        flash("Ye application disbursement sheet par hai; pehle use sheet se nikalein.")
        return back
    pid, fid = request.form.get("purpose_id", ""), request.form.get("fee_category_id", "")
    p = db.session.get(PurposeCategory, int(pid)) if pid.isdigit() else None
    f = db.session.get(FeeCategory, int(fid)) if fid.isdigit() else None
    old = {"purpose": a.purpose_label, "fee": money(a.fee_amount)}
    if p and (p.active or p.id == a.purpose_id):
        a.purpose_id, a.purpose_label = p.id, p.name
    if f and (f.active or f.id == a.fee_category_id):
        a.fee_category_id, a.fee_amount = f.id, f.fee_amount
    new = {"purpose": a.purpose_label, "fee": money(a.fee_amount)}
    if new != old:
        a.updated_by, a.updated_at = current_user.id, datetime.utcnow()
        audit("application.details", a, old=old, new=new)
        if a.status in REREVIEW_FROM:
            send_for_rereview(a, "Purpose / fee badli")
        db.session.commit()
        flash("Purpose / fee save ho gayi.")
    return back


@app.route("/application/<int:app_id>/family-cnic", methods=["POST"])
@login_required
def set_family_cnic(app_id):
    a = get_app(app_id)
    if not (current_user.role == "Admin" or (can("app.review") and a.reviewer_id == current_user.id)):
        abort(403)
    want = request.form.get("required") == "1"
    if want != a.family_cnic_required:
        a.family_cnic_required = want
        audit("application.family_cnic", a, old={"required": not want}, new={"required": want})
        db.session.commit()
        flash("Family Member CNIC ab " + ("zaroori hai." if want else "zaroori nahi."))
    return redirect(url_for("view_application", app_id=a.id))


# ---------------------------------------------------------------- disbursement sheets
def ordinal(n):
    n = int(n)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fmt_cnic(c):
    d = "".join(ch for ch in (c or "") if ch.isdigit())
    return f"{d[:5]}-{d[5:12]}-{d[12]}" if len(d) == 13 else (c or "")


def requires_sheet_access(fn):
    @wraps(fn)
    @login_required
    def inner(*args, **kwargs):
        if not (can("disbursement.manage") or can("cheque.manage")):
            abort(403)
        return fn(*args, **kwargs)
    return inner


def active_entries(batch):
    return (DisbursementEntry.query.filter_by(batch_id=batch.id, removed_at=None)
            .order_by(DisbursementEntry.id).all())


def active_cheque(entry_id):
    return Cheque.query.filter(Cheque.entry_id == entry_id, Cheque.status != "Cancelled").first()


def sheet_data(batch, with_photos=False):
    """Everything a disbursement sheet shows — used for the page, Excel and Google Sheets."""
    rows, fee_map, purpose_map = [], {}, {}
    for i, e in enumerate(active_entries(batch), 1):
        ch = active_cheque(e.id)
        amount = int(e.amount or 0)
        fee = int(e.fee_amount or 0)
        row = {"sno": i, "entry": e, "name": e.name, "cnic": fmt_cnic(e.cnic), "phone": e.phone or "",
               "amount": amount, "purpose": e.purpose or "", "loan_no": e.loan_number or "",
               "cheque_no": ch.cheque_no if ch else "", "cheque_status": ch.status if ch else "",
               "fee": fee, "disbursed": bool(e.disbursed_at), "application_id": e.application_id}
        if with_photos:
            a = db.session.get(Application, e.application_id)
            p = a.latest_photo("photo") if a else None
            row["photo"] = read_file(p) if p else None
        rows.append(row)
        fee_map.setdefault(fee, {"fee": fee, "loan": amount, "count": 0, "sum": 0})
        fee_map[fee]["count"] += 1
        fee_map[fee]["sum"] += fee
        purpose_map.setdefault(row["purpose"], {"name": row["purpose"], "count": 0, "sum": 0})
        purpose_map[row["purpose"]]["count"] += 1
        purpose_map[row["purpose"]]["sum"] += amount
    # list every active fee slab and purpose (like the reference sheet), zeros included
    for fc in FeeCategory.query.filter_by(active=True).order_by(FeeCategory.sort_order).all():
        key = int(fc.fee_amount)
        fee_map.setdefault(key, {"fee": key, "loan": int(fc.loan_amount or 0), "count": 0, "sum": 0})
    for pc in PurposeCategory.query.filter_by(active=True).order_by(PurposeCategory.sort_order).all():
        purpose_map.setdefault(pc.name, {"name": pc.name, "count": 0, "sum": 0})
    order = {pc.name: pc.sort_order for pc in PurposeCategory.query.all()}
    return {"batch": batch, "number": batch.number, "number_text": ordinal(batch.number),
            "date_text": batch.disb_date.strftime("%A, %B %d, %Y").replace(" 0", " "),
            "rows": rows, "count": len(rows), "total_amount": sum(r["amount"] for r in rows),
            "total_fees": sum(r["fee"] for r in rows),
            "disbursed_amount": sum(r["amount"] for r in rows if r["disbursed"]),
            "fee_table": sorted(fee_map.values(), key=lambda x: -x["fee"]),
            "purpose_table": sorted(purpose_map.values(), key=lambda x: (order.get(x["name"], 999), x["name"]))}


def eligible_for_sheet():
    taken = {e.application_id for e in DisbursementEntry.query.filter_by(removed_at=None)}
    return [a for a in live(Application.query).filter(
        Application.status.in_(["Approved", "Ready for Disbursement"])).order_by(Application.approved_at).all()
        if a.id not in taken]


@app.route("/disbursement", methods=["GET", "POST"])
@requires_sheet_access
def disbursement_list():
    if request.method == "POST":
        if not can("disbursement.manage"):
            abort(403)
        try:
            number = int(request.form.get("number", ""))
            when = datetime.strptime(request.form.get("disb_date", ""), "%Y-%m-%d").date()
        except ValueError:
            flash("Disbursement number aur date sahih likhein.")
            return redirect(url_for("disbursement_list"))
        if number <= 0 or DisbursementBatch.query.filter_by(number=number).first():
            flash(f"Disbursement number {number} pehle se mojood hai ya ghalat hai.")
            return redirect(url_for("disbursement_list"))
        b = DisbursementBatch(number=number, disb_date=when, month=when.month, year=when.year,
                              created_by=current_user.id, notes=request.form.get("notes", "").strip() or None)
        db.session.add(b)
        db.session.flush()
        audit("disbursement.created", entity="disbursement_batch", entity_id=b.id,
              new={"number": number, "date": when.isoformat()})
        db.session.commit()
        return redirect(url_for("disbursement_sheet", bid=b.id))
    batches = (DisbursementBatch.query.filter(DisbursementBatch.deleted_at.is_(None))
               .order_by(DisbursementBatch.disb_date.desc(), DisbursementBatch.number.desc()).all())
    summary = {b.id: (len(active_entries(b)), sum(int(e.amount or 0) for e in active_entries(b))) for b in batches}
    last = db.session.query(db.func.max(DisbursementBatch.number)).scalar()
    return render_template("disbursement_list.html", batches=batches, summary=summary,
                           next_number=(last + 1) if last else "", today=datetime.utcnow().date().isoformat(),
                           ready=len(eligible_for_sheet()) if can("disbursement.manage") else 0)


@app.route("/disbursement/<int:bid>")
@requires_sheet_access
def disbursement_sheet(bid):
    b = live_batch(bid)
    gy = db.session.get(GsheetYear, b.year)
    history = {r["entry"].id: Cheque.query.filter_by(entry_id=r["entry"].id).order_by(Cheque.id).all()
               for r in sheet_data(b)["rows"]}
    return render_template("disbursement.html", s=sheet_data(b), b=b, history=history,
                           eligible=eligible_for_sheet() if can("disbursement.manage") and not b.closed else [],
                           gsheet=gy if gy and gy.status == "ready" else None, names=usernames())


def live_batch(bid):
    b = db.get_or_404(DisbursementBatch, bid)
    if b.deleted_at:
        abort(404)
    return b


def _batch_open_or_back(b):
    if b.closed:
        flash(f"Disbursement sheet {b.number} close hai. Pehle Admin isay dobara khole.")
        return redirect(url_for("disbursement_sheet", bid=b.id))
    return None


@app.route("/disbursement/<int:bid>/add", methods=["POST"])
@requires("disbursement.manage")
def disbursement_add(bid):
    b = live_batch(bid)
    if (r := _batch_open_or_back(b)):
        return r
    eligible = {a.id: a for a in eligible_for_sheet()}
    added, problems = 0, []
    for raw in request.form.getlist("app_id"):
        a = eligible.get(int(raw)) if raw.isdigit() else None
        if not a:
            continue
        missing = [n for n, v in (("Loan No.", a.loan_number), ("amount", a.loan_amount),
                                  ("purpose", a.purpose_label), ("fee", a.fee_amount)) if not v and v != 0]
        if missing:
            problems.append(f"{a.app_id}: {', '.join(missing)} missing")
            continue
        vals = a.form_values
        e = DisbursementEntry(batch_id=b.id, application_id=a.id, name=a.applicant_name, cnic=a.cnic,
                              phone=vals.get("phone", ""), amount=a.loan_amount, purpose=a.purpose_label,
                              loan_number=a.loan_number, fee_amount=a.fee_amount, added_by=current_user.id)
        db.session.add(e)
        old = a.status
        a.status, a.updated_by, a.updated_at = "Added to Disbursement Sheet", current_user.id, datetime.utcnow()
        db.session.flush()
        audit("disbursement.added", a, entity="disbursement_entry", entity_id=e.id,
              old={"status": old}, new={"status": a.status, "sheet": b.number})
        added += 1
    if added:
        mark_dirty(b)
    db.session.commit()
    if added:
        flash(f"{added} application(s) sheet {b.number} mein add ho gayin.")
        try_sync()
    if problems:
        flash("Add nahi hui (pehle ye bharein): " + "; ".join(problems))
    return redirect(url_for("disbursement_sheet", bid=b.id))


def _entry(eid):
    e = db.get_or_404(DisbursementEntry, eid)
    if e.removed_at:
        abort(404)
    return e, db.session.get(DisbursementBatch, e.batch_id)


@app.route("/disbursement/entry/<int:eid>/remove", methods=["POST"])
@requires("disbursement.manage")
def disbursement_remove(eid):
    e, b = _entry(eid)
    if (r := _batch_open_or_back(b)):
        return r
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Sheet se nikalne ki wajah likhna zaroori hai.")
        return redirect(url_for("disbursement_sheet", bid=b.id))
    if e.disbursed_at:
        flash("Raqam ada ho chuki hai; ye row nahi nikali ja sakti.")
        return redirect(url_for("disbursement_sheet", bid=b.id))
    for ch in Cheque.query.filter(Cheque.entry_id == e.id, Cheque.status != "Cancelled"):
        ch.status, ch.cancel_reason = "Cancelled", "Row removed from sheet: " + reason
        ch.cancelled_by, ch.cancelled_at = current_user.id, datetime.utcnow()
    e.removed_at, e.removed_by, e.remove_reason = datetime.utcnow(), current_user.id, reason
    a = db.session.get(Application, e.application_id)
    a.status, a.updated_by, a.updated_at = "Ready for Disbursement", current_user.id, datetime.utcnow()
    audit("disbursement.removed", a, reason=reason, entity="disbursement_entry", entity_id=e.id,
          old={"sheet": b.number}, new={"status": a.status})
    mark_dirty(b)
    db.session.commit()
    flash("Row sheet se nikal di gayi.")
    try_sync()
    return redirect(url_for("disbursement_sheet", bid=b.id))


def _mark_disbursed(e, b):
    if e.disbursed_at:
        return False
    e.disbursed_at, e.disbursed_by = datetime.utcnow(), current_user.id
    a = db.session.get(Application, e.application_id)
    old = a.status
    a.status, a.updated_by, a.updated_at = "Disbursed", current_user.id, datetime.utcnow()
    audit("disbursement.disbursed", a, entity="disbursement_entry", entity_id=e.id,
          old={"status": old}, new={"status": "Disbursed", "sheet": b.number})
    return True


@app.route("/disbursement/entry/<int:eid>/disbursed", methods=["POST"])
@requires("disbursement.manage")
def disbursement_mark(eid):
    e, b = _entry(eid)
    if (r := _batch_open_or_back(b)):
        return r
    if _mark_disbursed(e, b):
        mark_dirty(b)
        db.session.commit()
        try_sync()
    return redirect(url_for("disbursement_sheet", bid=b.id))


@app.route("/disbursement/<int:bid>/disburse-all", methods=["POST"])
@requires("disbursement.manage")
def disbursement_mark_all(bid):
    b = live_batch(bid)
    if (r := _batch_open_or_back(b)):
        return r
    n = sum(_mark_disbursed(e, b) for e in active_entries(b))
    if n:
        mark_dirty(b)
        db.session.commit()
        flash(f"{n} row(s) disbursed mark ho gayin.")
        try_sync()
    return redirect(url_for("disbursement_sheet", bid=b.id))


@app.route("/disbursement/entry/<int:eid>/cheque", methods=["POST"])
@requires_sheet_access
def save_cheque(eid):
    e, b = _entry(eid)
    if (r := _batch_open_or_back(b)):
        return r
    back = redirect(url_for("disbursement_sheet", bid=b.id) + f"#e{e.id}")
    number = request.form.get("cheque_no", "").strip()[:40]
    status = request.form.get("status", "Issued")
    reason = request.form.get("reason", "").strip()
    if status not in ("Pending", "Issued", "Cleared", "Cancelled"):
        abort(400)
    cur = active_cheque(e.id)
    a = db.session.get(Application, e.application_id)
    now = datetime.utcnow()
    if status == "Cancelled":
        if not cur:
            return back
        if not reason:
            flash("Cheque cancel karne ki wajah likhna zaroori hai.")
            return back
        cur.status, cur.cancel_reason, cur.cancelled_by, cur.cancelled_at = "Cancelled", reason, current_user.id, now
        audit("cheque.cancelled", a, reason=reason, entity="cheque", entity_id=cur.id,
              old={"cheque_no": cur.cheque_no}, new={"status": "Cancelled"})
    elif not number:
        flash("Cheque number likhein.")
        return back
    elif cur and cur.cheque_no == number:
        if cur.status != status:
            old = cur.status
            cur.status, cur.updated_by, cur.updated_at = status, current_user.id, now
            audit("cheque.status", a, entity="cheque", entity_id=cur.id, old={"status": old},
                  new={"status": status, "cheque_no": number})
    else:
        clash = Cheque.query.filter(Cheque.cheque_no == number, Cheque.status != "Cancelled").first()
        if clash:
            flash(f"Cheque number {number} pehle se kisi aur row par active hai.")
            return back
        if cur:
            if not reason:
                flash("Purana cheque badalne ki wajah likhna zaroori hai (purana cheque cancel hoga).")
                return back
            cur.status, cur.cancel_reason, cur.cancelled_by, cur.cancelled_at = "Cancelled", reason, current_user.id, now
            audit("cheque.cancelled", a, reason=reason, entity="cheque", entity_id=cur.id,
                  old={"cheque_no": cur.cheque_no}, new={"status": "Cancelled", "replaced_by": number})
            db.session.flush()
        ch = Cheque(entry_id=e.id, cheque_no=number, status=status, created_by=current_user.id)
        db.session.add(ch)
        db.session.flush()
        audit("cheque.saved", a, entity="cheque", entity_id=ch.id, new={"cheque_no": number, "status": status,
                                                                       "sheet": b.number})
    mark_dirty(b)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(f"Cheque number {number} pehle se active hai.")
        return back
    try_sync()
    return back


@app.route("/disbursement/<int:bid>/close", methods=["POST"])
@requires("period.close")
def disbursement_close(bid):
    b = live_batch(bid)
    if not b.closed:
        b.status, b.closed_by, b.closed_at = "closed", current_user.id, datetime.utcnow()
        audit("disbursement.closed", entity="disbursement_batch", entity_id=b.id, new={"number": b.number})
        db.session.commit()
        flash(f"Sheet {b.number} close ho gayi. Ab is mein tabdeeli nahi ho sakti.")
    return redirect(url_for("disbursement_sheet", bid=b.id))


@app.route("/disbursement/<int:bid>/reopen", methods=["POST"])
@requires("period.close")
def disbursement_reopen(bid):
    b = live_batch(bid)
    reason = request.form.get("reason", "").strip()
    if b.closed:
        if not reason:
            flash("Dobara kholne ki wajah likhna zaroori hai.")
            return redirect(url_for("disbursement_sheet", bid=b.id))
        b.status, b.reopened_by, b.reopened_at, b.reopen_reason = "open", current_user.id, datetime.utcnow(), reason
        audit("disbursement.reopened", entity="disbursement_batch", entity_id=b.id, reason=reason,
              new={"number": b.number})
        db.session.commit()
        flash(f"Sheet {b.number} dobara khul gayi.")
    return redirect(url_for("disbursement_sheet", bid=b.id))


@app.route("/disbursement/<int:bid>/delete", methods=["POST"])
@login_required
def disbursement_delete(bid):
    """Admin only. The sheet is hidden (soft delete); its rows are taken off so the
    applications go back to "Ready for Disbursement"; paid sheets can never be deleted."""
    if current_user.role != "Admin":
        abort(403)
    b = live_batch(bid)
    back = redirect(url_for("disbursement_sheet", bid=b.id))
    reason = request.form.get("reason", "").strip()
    if b.closed:
        flash("Close sheet delete nahi ho sakti. Pehle wajah likh kar dobara kholein.")
        return back
    if not reason:
        flash("Sheet delete karne ki wajah likhna zaroori hai.")
        return back
    rows = active_entries(b)
    if any(e.disbursed_at for e in rows):
        flash("Is sheet mein raqam ada ho chuki hai (Paid rows); ye sheet delete nahi ho sakti.")
        return back
    now = datetime.utcnow()
    for e in rows:
        for ch in Cheque.query.filter(Cheque.entry_id == e.id, Cheque.status != "Cancelled"):
            ch.status, ch.cancel_reason = "Cancelled", "Sheet deleted: " + reason
            ch.cancelled_by, ch.cancelled_at = current_user.id, now
        e.removed_at, e.removed_by, e.remove_reason = now, current_user.id, "Sheet deleted: " + reason
        a = db.session.get(Application, e.application_id)
        if a and a.status == "Added to Disbursement Sheet":
            a.status, a.updated_by, a.updated_at = "Ready for Disbursement", current_user.id, now
    db.session.flush()
    b.deleted_at, b.deleted_by, b.delete_reason = now, current_user.id, reason
    audit("disbursement.deleted", entity="disbursement_batch", entity_id=b.id, reason=reason,
          old={"number": b.number, "rows": len(rows)})
    mark_dirty(b)
    db.session.commit()
    flash(f"Disbursement sheet {b.number} delete ho gayi. Us ki applications dobara 'Ready for Disbursement' hain.")
    try_sync()
    return redirect(url_for("disbursement_list"))


@app.route("/disbursement/entry/<int:eid>/photo")
@requires_sheet_access
def disbursement_photo(eid):
    e = db.get_or_404(DisbursementEntry, eid)
    a = db.session.get(Application, e.application_id)
    p = a.latest_photo("photo") if a else None
    if not p:
        return redirect(url_for("static", filename="no-photo.svg"))
    resp = send_file(io.BytesIO(read_file(p)), mimetype=p.content_type)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.route("/disbursement/<int:bid>/xlsx")
@requires_sheet_access
def disbursement_xlsx(bid):
    from sheet_export import build_xlsx
    b = live_batch(bid)
    with open(os.path.join(app.static_folder, "tooba-logo.png"), "rb") as fh:
        logo = fh.read()
    data = build_xlsx(sheet_data(b, with_photos=True), logo)
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f"Disbursement_{b.number}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- Google Sheets sync
GOOGLE_CLIENT = None          # tests put a fake client here


def google_client():
    return GOOGLE_CLIENT or gsheets.GoogleClient()


def mark_dirty(batch):
    for m in (batch.month, 0):
        row = db.session.get(GsheetSync, (batch.year, m))
        if row is None:
            db.session.add(GsheetSync(year=batch.year, month=m, dirty=True))
        else:
            row.dirty, row.requested_at = True, datetime.utcnow()


def app_link(app_pk):
    base = os.getenv("APP_BASE_URL") or (request.url_root if has_request_context() else "")
    return base.rstrip("/") + f"/application/{app_pk}" if base else ""


def month_rows(year, month):
    rows, bold = [], []
    batches = (DisbursementBatch.query.filter_by(year=year, month=month).filter(DisbursementBatch.deleted_at.is_(None))
               .order_by(DisbursementBatch.disb_date, DisbursementBatch.number).all())
    total_n = total_amt = total_fee = 0
    for b in batches:
        s = sheet_data(b)
        bold += [len(rows), len(rows) + 1, len(rows) + 2]
        rows.append(["TOOBA FOUNDATION", "", "", "", "", "", "", "", "", "", "",
                     "Closed" if b.closed else "Open"])
        rows.append([f"Disbursement Number {s['number_text']}", "", "", s["date_text"]])
        rows.append(["S.No", "Name", "CNIC No.", "Cell No.", "Amount", "Purpose", "Loan No.", "Cheque No.",
                     "Photo", "Signature", "Thumb", "Form Fees"])
        for r in s["rows"]:
            rows.append([r["sno"], r["name"], r["cnic"], r["phone"], r["amount"], r["purpose"], r["loan_no"],
                         r["cheque_no"], app_link(r["application_id"]), "", "", r["fee"]])
        bold.append(len(rows))
        rows.append(["", "Total Amount", "", "", s["total_amount"], "", f"Applicants: {s['count']}", "", "",
                     "", "", s["total_fees"]])
        rows.append([])
        total_n, total_amt, total_fee = total_n + s["count"], total_amt + s["total_amount"], total_fee + s["total_fees"]
    if batches:
        bold.append(len(rows))
        rows.append(["", "Month total", "", "", total_amt, "", f"Applicants: {total_n}", "", "", "", "", total_fee])
    return rows, bold


def summary_rows(year):
    rows = [["Tooba Foundation – Yearly Summary", str(year)], [],
            ["Month", "Sheets", "Applicants", "Total Amount", "Disbursed Amount", "Form Fees"]]
    bold = [0, 2]
    tot = [0, 0, 0, 0, 0]
    purpose, fees = {}, {}
    for m in range(1, 13):
        bs = DisbursementBatch.query.filter_by(year=year, month=m).filter(DisbursementBatch.deleted_at.is_(None)).all()
        vals = [len(bs), 0, 0, 0, 0]
        for b in bs:
            s = sheet_data(b)
            vals[1] += s["count"]
            vals[2] += s["total_amount"]
            vals[3] += s["disbursed_amount"]
            vals[4] += s["total_fees"]
            for r in s["rows"]:
                purpose.setdefault(r["purpose"], [0, 0])
                purpose[r["purpose"]][0] += 1
                purpose[r["purpose"]][1] += r["amount"]
                fees.setdefault(r["fee"], [0, 0])
                fees[r["fee"]][0] += 1
                fees[r["fee"]][1] += r["fee"]
        rows.append([gsheets.MONTHS[m - 1]] + vals)
        tot = [x + y for x, y in zip(tot, vals)]
    bold.append(len(rows))
    rows.append(["Total"] + tot)
    rows += [[], ["Purpose", "Number of Loans", "Loan Amount"]]
    bold.append(len(rows) - 1)
    rows += [[k, v[0], v[1]] for k, v in sorted(purpose.items())]
    rows += [[], ["Form Fee", "Fee Collection", "Fee Amount"]]
    bold.append(len(rows) - 1)
    rows += [[k, v[0], v[1]] for k, v in sorted(fees.items(), reverse=True)]
    return rows, bold


def ensure_year(year, client):
    """One spreadsheet per year — never a duplicate. Returns the GsheetYear row or None."""
    gy = db.session.get(GsheetYear, year)
    if gy and gy.status == "ready":
        return gy
    if gy is None:
        gy = GsheetYear(year=year, status="creating")
        db.session.add(gy)
        try:
            db.session.commit()            # the row is the lock: a second request sees it
        except IntegrityError:
            db.session.rollback()
            return None
    elif gy.status == "creating" and gy.created_at and gy.created_at > datetime.utcnow() - timedelta(minutes=5) \
            and not getattr(g, "_creating_year", False):
        return None                        # another request is creating it right now
    g._creating_year = True
    try:
        found = client.find_year(year)
        sid, url, tabs = found if found else client.create_year(year)
    except Exception:
        db.session.rollback()
        row = db.session.get(GsheetYear, year)
        if row is not None and row.status != "ready":
            row.status = "failed"          # next attempt tries again (and still checks for an existing file)
            db.session.commit()
        raise
    gy.spreadsheet_id, gy.url, gy.tab_ids, gy.status = sid, url, tabs, "ready"
    if not found:
        audit("gsheets.year_created", new={"year": year, "url": url})
    db.session.add(GsheetSyncLog(year=year, month=None, ok=True,
                                 message=("Found existing " if found else "Created ") + gsheets.title_for(year)))
    db.session.commit()
    return gy


def run_sync(limit=4):
    """Write dirty month tabs (and the yearly summary) to Google. Failures are logged and retried."""
    if not (gsheets.configured() or GOOGLE_CLIENT):
        return 0
    client = google_client()
    done = 0
    for row in GsheetSync.query.filter_by(dirty=True).order_by(GsheetSync.month.desc()).limit(limit).all():
        key = (row.year, row.month)
        try:
            gy = ensure_year(row.year, client)
            if gy is None:
                continue
            tab = gsheets.SUMMARY if row.month == 0 else gsheets.MONTHS[row.month - 1]
            values, bold = summary_rows(row.year) if row.month == 0 else month_rows(row.year, row.month)
            client.write_tab(gy.spreadsheet_id, tab, (gy.tab_ids or {}).get(tab), values, bold)
            row = db.session.get(GsheetSync, key)
            row.dirty, row.synced_at, row.last_error = False, datetime.utcnow(), None
            db.session.add(GsheetSyncLog(year=key[0], month=key[1], ok=True, message=f"Synced {tab}"))
            db.session.commit()
            done += 1
        except Exception as exc:              # Google failing must never break the website
            db.session.rollback()
            row = db.session.get(GsheetSync, key)
            if row is not None:
                row.attempts, row.last_error = (row.attempts or 0) + 1, str(exc)[:500]
            db.session.add(GsheetSyncLog(year=key[0], month=key[1], ok=False, message=str(exc)[:500]))
            db.session.commit()
    return done


def try_sync():
    try:
        run_sync(limit=2)
    except Exception:
        db.session.rollback()


@app.route("/admin/google-sheets", methods=["GET", "POST"])
@requires("gsheets.manage")
def google_sheets():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "year":
            y = request.form.get("year", type=int)
            if y and not db.session.get(GsheetSync, (y, 0)):
                db.session.add(GsheetSync(year=y, month=0, dirty=True))
                db.session.commit()
        elif action == "resync":
            for b in DisbursementBatch.query.filter(DisbursementBatch.deleted_at.is_(None)):
                mark_dirty(b)
            db.session.commit()
        n = run_sync(limit=30) if (gsheets.configured() or GOOGLE_CLIENT) else 0
        flash(f"{n} tab(s) sync hue." if gsheets.configured() or GOOGLE_CLIENT
              else "Google Sheets abhi configure nahi — neeche di gayi settings Vercel mein lagayein.")
        return redirect(url_for("google_sheets"))
    return render_template("google_sheets.html", configured=gsheets.configured() or bool(GOOGLE_CLIENT),
                           mode=gsheets.mode(), years=GsheetYear.query.order_by(GsheetYear.year.desc()).all(),
                           pending=GsheetSync.query.filter_by(dirty=True).all(),
                           log=GsheetSyncLog.query.order_by(GsheetSyncLog.id.desc()).limit(40).all(),
                           this_year=(datetime.utcnow() + timedelta(hours=5)).year)


@app.route("/cron/gsheets")
@csrf.exempt
def cron_gsheets():
    """Called by Vercel Cron. Retries failed syncs and creates the new year's spreadsheet in time."""
    secret = os.getenv("CRON_SECRET")
    if not secret or request.headers.get("Authorization") != f"Bearer {secret}":
        abort(401)
    g._db_actor = ("", "system")
    if not USING_SQLITE and db.session().in_transaction():
        db.session.execute(_SET_ACTOR, {"u": "", "r": "system"})
    year = (datetime.utcnow() + timedelta(hours=5)).year
    if not db.session.get(GsheetSync, (year, 0)) and (gsheets.configured() or GOOGLE_CLIENT):
        db.session.add(GsheetSync(year=year, month=0, dirty=True))
        db.session.commit()
    return jsonify(synced=run_sync(limit=20))


# ---------------------------------------------------------------- reports
def report_data(year):
    apps = live(Application.query).all()
    names = usernames()
    entries = (db.session.query(DisbursementEntry, DisbursementBatch)
               .join(DisbursementBatch, DisbursementBatch.id == DisbursementEntry.batch_id)
               .filter(DisbursementEntry.removed_at.is_(None)).all())
    rejected_at = {}
    for h in AuditLog.query.filter_by(action="application.status").all():
        if (h.new_value or {}).get("status") == "Rejected":
            rejected_at[h.application_id] = h.created_at
    amt = lambda a: int(a.loan_amount or 0)
    monthly = []
    for m in range(1, 13):
        inm = lambda d: d is not None and d.year == year and d.month == m
        ents = [e for e, b in entries if b.year == year and b.month == m]
        monthly.append({
            "month": gsheets.MONTHS[m - 1],
            "created": sum(1 for a in apps if inm(a.created_at)),
            "approved": sum(1 for a in apps if inm(a.approved_at)),
            "approved_amount": sum(amt(a) for a in apps if inm(a.approved_at)),
            "rejected": sum(1 for a in apps if inm(rejected_at.get(a.id))),
            "sheet_count": len(ents),
            "disbursed": sum(1 for e in ents if e.disbursed_at),
            "disbursed_amount": sum(int(e.amount or 0) for e in ents if e.disbursed_at),
            "fees": sum(int(e.fee_amount or 0) for e in ents)})
    years = sorted({a.created_at.year for a in apps if a.created_at} | {b.year for _, b in entries} | {year})
    yearly = []
    for y in years:
        ents = [e for e, b in entries if b.year == y]
        yearly.append({"year": y, "created": sum(1 for a in apps if a.created_at and a.created_at.year == y),
                       "approved": sum(1 for a in apps if a.approved_at and a.approved_at.year == y),
                       "disbursed_amount": sum(int(e.amount or 0) for e in ents if e.disbursed_at),
                       "fees": sum(int(e.fee_amount or 0) for e in ents)})
    yr = [a for a in apps if a.created_at and a.created_at.year == year]
    pending_set = {"Submitted", "Pending Review", "In Review", "Correction Required", "Submitted Again"}
    done_set = {"Approved", "Ready for Disbursement", "Added to Disbursement Sheet", "Disbursed", "Completed"}
    staff = {}
    for a in yr:
        k = names.get(a.created_by, "?")
        r = staff.setdefault(k, {"name": k, "total": 0, "draft": 0, "pending": 0, "approved": 0, "rejected": 0})
        r["total"] += 1
        if a.status == "Draft":
            r["draft"] += 1
        elif a.status in pending_set:
            r["pending"] += 1
        elif a.status in done_set:
            r["approved"] += 1
        elif a.status == "Rejected":
            r["rejected"] += 1
    reviewers = {}
    for a in yr:
        if not a.reviewer_id:
            continue
        k = names.get(a.reviewer_id, "?")
        r = reviewers.setdefault(k, {"name": k, "assigned": 0, "pending": 0, "approved": 0, "rejected": 0})
        r["assigned"] += 1
        if a.status in pending_set:
            r["pending"] += 1
        elif a.status in done_set:
            r["approved"] += 1
        elif a.status == "Rejected":
            r["rejected"] += 1
    purpose = {}
    for a in yr:
        k = a.purpose_label or "—"
        r = purpose.setdefault(k, {"name": k, "count": 0, "amount": 0, "disbursed": 0})
        r["count"] += 1
        r["amount"] += amt(a)
    for e, b in entries:
        if b.year == year and e.disbursed_at:
            purpose.setdefault(e.purpose or "—", {"name": e.purpose or "—", "count": 0, "amount": 0, "disbursed": 0})
            purpose[e.purpose or "—"]["disbursed"] += int(e.amount or 0)
    fees = {}
    for e, b in entries:
        if b.year == year:
            k = int(e.fee_amount or 0)
            r = fees.setdefault(k, {"fee": k, "count": 0, "sum": 0})
            r["count"] += 1
            r["sum"] += k
    totals = {"approved": sum(1 for a in yr if a.status in done_set),
              "approved_amount": sum(amt(a) for a in yr if a.status in done_set),
              "rejected": sum(1 for a in yr if a.status == "Rejected"),
              "pending": sum(1 for a in yr if a.status in pending_set),
              "drafts": sum(1 for a in yr if a.status == "Draft"),
              "disbursed": sum(1 for e, b in entries if b.year == year and e.disbursed_at),
              "disbursed_amount": sum(int(e.amount or 0) for e, b in entries if b.year == year and e.disbursed_at),
              "fees": sum(int(e.fee_amount or 0) for e, b in entries if b.year == year)}
    return {"year": year, "years": years, "monthly": monthly, "yearly": yearly,
            "staff": sorted(staff.values(), key=lambda r: -r["total"]),
            "reviewers": sorted(reviewers.values(), key=lambda r: -r["assigned"]),
            "purpose": sorted(purpose.values(), key=lambda r: -r["count"]),
            "fees": sorted(fees.values(), key=lambda r: -r["fee"]), "totals": totals}


REPORT_COLUMNS = {
    "monthly": [("month", "Month"), ("created", "Applications"), ("approved", "Approved"),
                ("approved_amount", "Approved amount"), ("rejected", "Rejected"), ("sheet_count", "On sheets"),
                ("disbursed", "Disbursed"), ("disbursed_amount", "Disbursed amount"), ("fees", "Form fees")],
    "yearly": [("year", "Year"), ("created", "Applications"), ("approved", "Approved"),
               ("disbursed_amount", "Disbursed amount"), ("fees", "Form fees")],
    "staff": [("name", "Staff"), ("total", "Total"), ("draft", "Draft"), ("pending", "In review"),
              ("approved", "Approved+"), ("rejected", "Rejected")],
    "reviewers": [("name", "Reviewer"), ("assigned", "Assigned"), ("pending", "Pending"),
                  ("approved", "Approved+"), ("rejected", "Rejected")],
    "purpose": [("name", "Purpose"), ("count", "Applications"), ("amount", "Requested amount"),
                ("disbursed", "Disbursed amount")],
    "fees": [("fee", "Form fee"), ("count", "Fee collection"), ("sum", "Fee amount")],
}


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
        _insp = inspect(db.engine)
        # every table the code needs; a missing one means a migration has not been applied yet
        MIGRATION_PENDING = not all(_insp.has_table(t) for t in (
            "audit_log", "role_permission", "disbursement_batch", "disbursement_entry", "cheque",
            "gsheet_year", "gsheet_sync", "gsheet_sync_log")) or not any(
            c["name"] == "deleted_at" for c in _insp.get_columns("disbursement_batch"))
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
