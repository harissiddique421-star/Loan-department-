import os, json, secrets, csv, io, hashlib, hmac, zipfile, tempfile
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, flash, abort,
                   send_file, Response, jsonify, g, has_request_context, session,
                   got_request_exception)
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import inspect, text, event, or_, func, select as sa_select, column as sa_column
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from form_fields import (FORM_SECTIONS, FIELD_LABELS, FIELD_NAMES, PHOTO_SLOTS, PHOTO_KINDS,
                         PHOTO_LABELS, SELECT_OPTIONS, OTHER_OF)
import gsheets
import alerts
import backup
import db_export
import drive_archive
import sheet_layout
import word_form
from cache import VersionedCache

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


READ_CACHE = VersionedCache(ttl=int(os.getenv("READ_CACHE_TTL", "300")))


def data_version():
    """Database "has anything changed?" stamp, read once per request (see cache.py)."""
    if not PERF_DB or not has_request_context():
        return None
    if "_data_version" not in g:
        g._data_version = db.session.execute(text("select public.data_version()")).scalar()
    return g._data_version


@event.listens_for(Session, "after_commit")
def _forget_version(session):
    # after this request changes something, later reads in the same request use the new data
    if has_request_context():
        g.pop("_data_version", None)


def viewer_key():
    """Who is looking: results that RLS filters per user are cached per role and user."""
    return (current_user.role, current_user.id) if current_user and current_user.is_authenticated else ("anon", 0)


def cached(name, compute, *parts, per_user=True):
    key = (name,) + (viewer_key() if per_user else ()) + parts
    return READ_CACHE.get(key, data_version(), compute)


def usernames():
    # user names are readable by every signed-in role (user_select policy is "true")
    if "_usernames" not in g:
        g._usernames = cached("usernames", lambda: {u.id: u.display for u in User.query.all()}, per_user=False)
    return g._usernames


def status_counts(base):
    """{status: count} for a query, in one GROUP BY instead of one COUNT per status."""
    rows = base.with_entities(Application.status, func.count()).group_by(Application.status).order_by(None).all()
    got = dict(rows)
    return {st: got.get(st, 0) for st in STATUSES}, sum(got.values())


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
                ("backup_recovery", "Backup & Recovery", "database", ()),
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
            "SELECT_OPTIONS": SELECT_OPTIONS, "OTHER_OF": OTHER_OF,
            "PHOTO_SLOTS": PHOTO_SLOTS, "PHOTO_LABELS": PHOTO_LABELS, "can": can,
            "ACTION_LABELS": ACTION_LABELS, "ROLES": ROLES, "PORTAL_INFO": PORTAL_INFO,
            "CNIC_GROUPS": CNIC_GROUPS, "MIN_PASSWORD_LEN": MIN_PASSWORD_LEN, "CNIC_LABELS": CNIC_LABELS, "money": money,
            "portal": PORTAL_INFO.get(role), "portal_key": SECTION_OF_ROLE.get(role),
            "nav": nav_items(role) if role else []}


MIGRATION_PENDING = False
PERF_DB = False            # set at startup when the performance migration is applied


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


@app.route("/privacy")
def privacy():
    """Public privacy policy (needed by Google for the Drive connection)."""
    return render_template("privacy.html", terms=False)


@app.route("/terms")
def terms():
    return render_template("privacy.html", terms=True)


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
        if login_blocked(username, ip):
            alert(f"lock:{username[:60]}", f"Login lock: {username[:60]}",
                  f"Username \"{username[:60]}\" par {LOGIN_MAX_FAILS} ya zyada ghalat password try hue "
                  f"aur login {int(LOGIN_WINDOW.total_seconds() // 60)} minute ke liye band ho gaya.\n"
                  f"Section: {portal}\nIP: {ip}\nWaqt (UTC): {datetime.utcnow():%Y-%m-%d %H:%M}\n\n"
                  "Agar ye aap ya aap ka staff nahi tha to koi password guess karne ki koshish kar raha hai.")
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
    if PERF_DB:
        # same matching as below, found with the trigram indexes (RLS still checks every row)
        ids = sa_select(sa_column("id")).select_from(
            text("public.search_application_ids(:sq, :sd) as found(id)").bindparams(
                sq=q, sd=digits if digits and digits != q else None))
        return query.filter(Application.id.in_(ids))
    conds = [Application.applicant_name.ilike(like), Application.app_id.ilike(like),
             Application.loan_number.ilike(like), Application.cnic.ilike(like)]
    if digits and digits != q:
        conds.append(Application.cnic.ilike(f"%{digits}%"))   # "42501-1470957-1" finds stored digits
    return query.filter(or_(*conds))


LIST_PAGE_SIZE = 200


def list_page(base, title, eyebrow, endpoint, **extra):
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    page = max(1, request.args.get("page", 1, type=int) or 1)
    counts, total = cached("list_counts", lambda: status_counts(base), endpoint, extra.get("scope", ""))
    query = search(base, q)
    if status:
        query = query.filter(Application.status == status)
    rows = (query.order_by(Application.updated_at.desc())
            .offset((page - 1) * LIST_PAGE_SIZE).limit(LIST_PAGE_SIZE + 1).all())
    apps, has_next = rows[:LIST_PAGE_SIZE], len(rows) > LIST_PAGE_SIZE
    return render_template("applications.html", apps=apps, total=total, counts=counts, q=q, status=status,
                           names=usernames(), title=title, eyebrow=eyebrow, list_endpoint=endpoint,
                           page=page, has_next=has_next,
                           extra_args={k: v for k, v in extra.items() if k.startswith("arg_")}, **extra)


def live(query):
    return query.filter(Application.deleted_at.is_(None))


# ------------------------------- Admin section
@app.route("/admin")
@login_required
def admin_dashboard():
    base = live(Application.query)

    def numbers():
        counts, total = status_counts(base)
        by_role = dict(db.session.query(User.role, func.count()).filter(User.active.is_(True))
                       .group_by(User.role).all())
        return counts, total, {r: by_role.get(r, 0) for r in ROLES}
    counts, total, users_by_role = cached("admin_dashboard", numbers)
    queue = (base.filter(or_(Application.status.in_(["Submitted", "Submitted Again"]),
                             (Application.status == "Pending Review") & Application.reviewer_id.is_(None)))
             .order_by(Application.updated_at.desc()).limit(20).all())
    recent_audit = AuditLog.query.order_by(AuditLog.id.desc()).limit(12).all()
    recent = base.order_by(Application.updated_at.desc()).limit(8).all()
    return render_template("admin_dashboard.html", counts=counts, total=total, queue=queue,
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
    by_status, assigned = status_counts(base)
    stats = {"assigned": assigned, "waiting": len(waiting), "in_review": len(in_review),
             "correction": by_status["Correction Required"],
             "approved": base.filter(Application.approved_by == current_user.id).count(),
             "rejected": by_status["Rejected"]}
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
    open_notes = {a.id: "" for a in corrections}
    if corrections:
        # newest open correction note per application (one query instead of one per application)
        for n in (ReviewNote.query.filter(ReviewNote.application_id.in_(list(open_notes)),
                                          ReviewNote.kind == "correction", ReviewNote.resolved_at.is_(None))
                  .order_by(ReviewNote.id)):
            open_notes[n.application_id] = n.body
    drafts = base.filter(Application.status == "Draft").order_by(Application.updated_at.desc()).all()
    progress = ["Submitted", "Pending Review", "In Review", "Submitted Again"]
    done = ["Approved", "Ready for Disbursement", "Added to Disbursement Sheet", "Disbursed", "Completed"]
    by_status, total = status_counts(base)
    stats = {"total": total, "drafts": len(drafts), "corrections": len(corrections),
             "progress": sum(by_status[x] for x in progress),
             "approved": sum(by_status[x] for x in done),
             "rejected": by_status["Rejected"]}
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
    for name, opts in SELECT_OPTIONS.items():          # dropdowns: keep only a listed choice
        if values.get(name) and values[name] not in opts:
            values[name] = ""
    for other, parent in OTHER_OF.items():             # typed value only counts when "Other" is picked
        if values.get(parent) != "Other":
            values[other] = ""
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
    entries = active_entries(batch)
    live_cheques = {c.entry_id: c for c in Cheque.query.filter(
        Cheque.entry_id.in_([e.id for e in entries] or [-1]), Cheque.status != "Cancelled")}
    for i, e in enumerate(entries, 1):
        ch = live_cheques.get(e.id)
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


def eligible_count():
    """How many applications are waiting for a sheet (same rule as eligible_for_sheet)."""
    taken = sa_select(DisbursementEntry.application_id).where(DisbursementEntry.removed_at.is_(None))
    return (live(Application.query).filter(Application.status.in_(["Approved", "Ready for Disbursement"]),
                                           Application.id.not_in(taken)).count())


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
    sums = dict((bid, (n, int(total or 0))) for bid, n, total in db.session.query(
        DisbursementEntry.batch_id, func.count(), func.sum(DisbursementEntry.amount))
        .filter(DisbursementEntry.removed_at.is_(None), DisbursementEntry.batch_id.in_([b.id for b in batches] or [-1]))
        .group_by(DisbursementEntry.batch_id))
    summary = {b.id: sums.get(b.id, (0, 0)) for b in batches}
    last = db.session.query(db.func.max(DisbursementBatch.number)).scalar()
    return render_template("disbursement_list.html", batches=batches, summary=summary,
                           next_number=(last + 1) if last else "", today=datetime.utcnow().date().isoformat(),
                           ready=eligible_count() if can("disbursement.manage") else 0)


@app.route("/disbursement/<int:bid>")
@requires_sheet_access
def disbursement_sheet(bid):
    b = live_batch(bid)
    gy = db.session.get(GsheetYear, b.year)
    s = sheet_data(b)
    history = {r["entry"].id: [] for r in s["rows"]}
    for c in Cheque.query.filter(Cheque.entry_id.in_(list(history) or [-1])).order_by(Cheque.id):
        history[c.entry_id].append(c)
    return render_template("disbursement.html", s=s, b=b, history=history,
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


@app.route("/disbursement/entry/<int:eid>/undo-paid", methods=["POST"])
@requires("disbursement.manage")
def disbursement_unmark(eid):
    """Admin only: undo a "Paid" mark made by mistake (reason required, kept in the audit log)."""
    if current_user.role != "Admin":
        abort(403)
    e, b = _entry(eid)
    if (r := _batch_open_or_back(b)):
        return r
    back = redirect(url_for("disbursement_sheet", bid=b.id))
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Paid hatane ki wajah likhna zaroori hai.")
        return back
    if not e.disbursed_at:
        return back
    old_at = e.disbursed_at
    e.disbursed_at, e.disbursed_by = None, None
    a = db.session.get(Application, e.application_id)
    old = a.status
    if a.status == "Disbursed":
        a.status, a.updated_by, a.updated_at = "Added to Disbursement Sheet", current_user.id, datetime.utcnow()
    audit("disbursement.undo_paid", a, reason=reason, entity="disbursement_entry", entity_id=e.id,
          old={"status": old, "disbursed_at": old_at.isoformat()}, new={"status": a.status, "sheet": b.number})
    mark_dirty(b)
    db.session.commit()
    flash("Paid mark hata diya gaya.")
    try_sync()
    return back


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


def gsheet_photo_sig(entry_id):
    """Secret link part that lets Google Sheets load one applicant photo without a login."""
    return hmac.new(app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key,
                    f"gsheet-photo:{entry_id}".encode(), hashlib.sha256).hexdigest()[:32]


def _public_base():
    base = os.getenv("APP_BASE_URL") or (request.url_root if has_request_context() else "")
    return base.rstrip("/")


def gsheet_photo_url(row):
    base = _public_base()
    if not base or os.getenv("GSHEET_PHOTOS", "1") == "0":
        return None
    eid = row["entry"].id
    return f"{base}/gs/photo/{eid}/{gsheet_photo_sig(eid)}"


@app.route("/gs/photo/<int:eid>/<sig>")
def gsheet_photo(eid, sig):
    """Applicant photo for the Google Sheets copy. No login; the link itself is the key
    (HMAC with SECRET_KEY), and it only works for rows still on a live disbursement sheet."""
    if os.getenv("GSHEET_PHOTOS", "1") == "0" or not secrets.compare_digest(sig, gsheet_photo_sig(eid)):
        abort(404)
    g._db_actor = ("", "system")
    if not USING_SQLITE and db.session().in_transaction():
        db.session.execute(_SET_ACTOR, {"u": "", "r": "system"})
    e = db.session.get(DisbursementEntry, eid)
    b = db.session.get(DisbursementBatch, e.batch_id) if e else None
    if not e or e.removed_at or not b or b.deleted_at:
        abort(404)
    a = db.session.get(Application, e.application_id)
    p = a.latest_photo("photo") if a else None
    if not p or not (p.content_type or "").startswith("image/"):
        return redirect(url_for("static", filename="no-photo.svg"))
    resp = send_file(io.BytesIO(read_file(p)), mimetype=p.content_type)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def month_layout(year, month):
    batches = (DisbursementBatch.query.filter_by(year=year, month=month).filter(DisbursementBatch.deleted_at.is_(None))
               .order_by(DisbursementBatch.disb_date, DisbursementBatch.number).all())
    base = _public_base()
    return sheet_layout.month_grid(
        [(sheet_data(b), "Closed" if b.closed else "Open") for b in batches],
        link=lambda r: app_link(r["application_id"]), photo=gsheet_photo_url,
        logo_url=f"{base}/static/tooba-logo.png" if base else None)


def summary_layout(year):
    months, purpose, fees = [], {}, {}
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
        months.append([gsheets.MONTHS[m - 1]] + vals)
    base = _public_base()
    return sheet_layout.summary_grid(
        year, months, [[k, v[0], v[1]] for k, v in sorted(purpose.items())],
        [[k, v[0], v[1]] for k, v in sorted(fees.items(), reverse=True)],
        logo_url=f"{base}/static/tooba-logo.png" if base else None)


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
            tab = gsheets.tab_title(row.month, row.year)
            grid = summary_layout(row.year) if row.month == 0 else month_layout(row.year, row.month)
            client.write_layout(gy.spreadsheet_id, row.year, row.month, grid)
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
    _cron_ok()
    year = (datetime.utcnow() + timedelta(hours=5)).year
    if not db.session.get(GsheetSync, (year, 0)) and (gsheets.configured() or GOOGLE_CLIENT):
        db.session.add(GsheetSync(year=year, month=0, dirty=True))
        db.session.commit()
    n = run_sync(limit=20)
    stuck = GsheetSync.query.filter(GsheetSync.dirty.is_(True), GsheetSync.attempts >= 3).all()
    if stuck:
        alert("gsheets", "Google Sheets sync FAILED",
              "Ye tabs kai dafa koshish ke baad bhi Google Sheet par nahi gaye:\n"
              + "\n".join(f"- {r.year} / {'Yearly Summary' if r.month == 0 else r.month}: {(r.last_error or '')[:200]}"
                           for r in stuck[:10])
              + "\n\nAdmin > Google Sheets page par 'Sync now' dabayein ya settings check karein.",
              every=timedelta(hours=20))
    return jsonify(synced=n)


def _cron_ok():
    secret = os.getenv("CRON_SECRET")
    if not secret or not secrets.compare_digest(request.headers.get("Authorization", ""), f"Bearer {secret}"):
        abort(401)
    g._db_actor = ("", "system")
    if not USING_SQLITE and db.session().in_transaction():
        db.session.execute(_SET_ACTOR, {"u": "", "r": "system"})


# ---------------------------------------------------------------- alerts
_alert_mem = {}


def alert(key, subject, body, every=timedelta(minutes=30)):
    """Email the Admin, at most once per `every` for the same key. Never raises."""
    if not alerts.configured():
        return False
    now = datetime.utcnow()
    try:
        recent = Log.query.filter(Log.action == f"ALERT {key}"[:200], Log.created_at >= now - every).count()
        if recent:
            return False
        db.session.add(Log(user_id=None, action=f"ALERT {key}"[:200]))
        db.session.commit()
    except Exception:                       # database down: fall back to this server's memory
        db.session.rollback()
        if _alert_mem.get(key) and now - _alert_mem[key] < every:
            return False
    _alert_mem[key] = now
    site = os.getenv("APP_BASE_URL", "")
    return alerts.send(subject, body + (f"\n\nSite: {site}" if site else ""))


@got_request_exception.connect_via(app)
def _alert_on_error(sender, exception, **extra):
    try:
        db.session.rollback()
    except Exception:
        pass
    path = request.path if has_request_context() else "?"
    alert("error", f"Website error: {type(exception).__name__}",
          f"Website par ek error aaya (users ko 'Server error' dikha).\n\nPage: {path}\n"
          f"Error: {type(exception).__name__}: {str(exception)[:300]}\n"
          f"Waqt (UTC): {datetime.utcnow():%Y-%m-%d %H:%M}\n\n"
          "Agar ye baar baar aaye to Vercel > Logs dekhein.")


# ---------------------------------------------------------------- backup to Google Drive
def drive_backup():
    """Write the full backup to this weekday's file in the Drive folder. Returns the file name."""
    if not (gsheets.configured() or GOOGLE_CLIENT):
        raise RuntimeError("Google Drive configure nahi (GOOGLE_SERVICE_ACCOUNT_JSON).")
    now = datetime.utcnow() + timedelta(hours=5)
    data, counts = backup.build(db.session, db.metadata)
    name = backup.file_name(now)
    google_client().put_file(name, data)
    audit("backup.drive", new={"file": name, "bytes": len(data), "rows": counts})
    db.session.commit()
    return name, len(data)


@app.route("/cron/backup")
@csrf.exempt
def cron_backup():
    """Called by Vercel Cron every night."""
    _cron_ok()
    result, errors = {}, []
    if gsheets.configured() or GOOGLE_CLIENT:           # the older rotating weekday file
        try:
            result["file"], result["bytes"] = drive_backup()
        except Exception as exc:
            db.session.rollback()
            errors.append(f"Weekday backup file: {str(exc)[:300]}")
    if ARCHIVE_DB and archive_configured():             # dated daily backup (+ weekly full archive)
        try:
            result["daily"] = bool(database_backup())
            if weekly_due():
                result["weekly"] = database_backup(weekly=True)
        except Exception as exc:
            db.session.rollback()
            _log_run("daily", False, {}, f"{type(exc).__name__}: {str(exc)[:500]}")
            errors.append(f"Archive daily backup: {str(exc)[:300]}")
    elif not (gsheets.configured() or GOOGLE_CLIENT):
        errors.append("Google Drive configure nahi.")
    if errors:
        alert("backup", "Backup FAILED", "Raat ka database backup Google Drive par nahi gaya.\n\n" + "\n".join(errors),
              every=timedelta(hours=20))
        return jsonify(ok=False, error="; ".join(errors)[:300], **result), 500
    return jsonify(ok=True, **result)


@app.route("/admin/backup/drive", methods=["POST"])
@requires("export")
def backup_drive_now():
    try:
        name, size = drive_backup()
        flash(f"Backup Google Drive par save ho gaya: {name} ({size // 1024 + 1} KB).")
    except Exception as exc:
        db.session.rollback()
        flash(f"Backup nahi hua: {str(exc)[:300]}")
    return redirect(request.referrer or url_for("admin_applications"))


# ---------------------------------------------------------------- Google Drive archive (Word forms + full backups)
# Every application is archived to Google Drive as a filled Word form, with every saved version
# kept, plus a daily / weekly complete database backup. Supabase stays the main record; the archive
# only reads from it. Nothing is ever deleted from Drive by the app.
#
# What still needs archiving is worked out from the data itself (a fingerprint of the application's
# version, status, last audit row, last document and last note), so no change can be missed even if
# the website crashed right after saving: the next request or the nightly job picks it up.
class ArchiveFile(db.Model):
    """One file the app put (or tried to put) in the Drive archive."""
    __tablename__ = "archive_file"
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, index=True)   # no FK: the archive must never block a purge
    document_id = db.Column(db.Integer)
    category = db.Column(db.String(20), nullable=False)   # form | version | guarantor | document | database | report
    version = db.Column(db.Integer)
    name = db.Column(db.String(300), nullable=False)
    drive_file_id = db.Column(db.String(100))
    drive_folder_id = db.Column(db.String(100))
    md5 = db.Column(db.String(32))
    size = db.Column(db.BigInteger)
    fingerprint = db.Column(db.String(200))
    status = db.Column(db.String(12), default="pending", nullable=False)  # pending|verified|failed|missing|corrupt
    attempts = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_at = db.Column(db.DateTime)
    verified_at = db.Column(db.DateTime)


class ArchiveRun(db.Model):
    """Backup / verification history shown on Admin → Backup & Recovery."""
    __tablename__ = "archive_run"
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False)       # application | daily | weekly | verify | retry | manual
    started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    finished_at = db.Column(db.DateTime)
    ok = db.Column(db.Boolean)
    message = db.Column(db.Text)
    summary = db.Column(db.JSON)


class ArchiveSetting(db.Model):
    __tablename__ = "archive_setting"
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


ARCHIVE_CLIENT = None           # tests put a fake Drive here
ARCHIVE_DB = True               # set at startup: False until the archive migration is applied
ARCHIVE_TABLES = ("archive_file", "archive_run", "archive_setting")
PK_HOURS = timedelta(hours=5)   # Pakistan time for file names and reports


def pk_now():
    return datetime.utcnow() + PK_HOURS


def archive_setting(key, default=None):
    row = db.session.get(ArchiveSetting, key)
    return row.value if row and row.value is not None else default


def set_archive_setting(key, value):
    row = db.session.get(ArchiveSetting, key)
    if row is None:
        db.session.add(ArchiveSetting(key=key, value=value))
    else:
        row.value = value


def stored_drive_token():
    blob = archive_setting("oauth_token")
    return drive_archive.unseal(blob, app.config["SECRET_KEY"]) if blob else None


def archive_configured():
    return bool(ARCHIVE_CLIENT) or (ARCHIVE_DB and drive_archive.configured(stored_drive_token()))


def archive_client():
    if ARCHIVE_CLIENT:
        return ARCHIVE_CLIENT
    if has_request_context() and g.get("_archive_inline"):    # while a user waits: fail fast, cron retries
        return drive_archive.ArchiveDrive(refresh_token=stored_drive_token(), timeout=20, tries=1)
    return drive_archive.ArchiveDrive(refresh_token=stored_drive_token())


def archive_folders(client, check=False):
    """{folder name: Drive id} for the archive root and its 3 folders (created when missing).
    An archive made with the older 8-folder layout is tidied into the new one once."""
    cached = json.loads(archive_setting("folders") or "{}")
    if cached.get("_root") and all(f in cached for f in drive_archive.FOLDERS) and not cached.get("_old"):
        if not check:
            return cached
        root = client.get(cached["_root"])
        if root and not root.get("trashed"):
            return cached
    parent = os.getenv("GOOGLE_ARCHIVE_PARENT_ID") or None
    root = client.ensure_folder(drive_archive.ROOT_NAME, parent)
    out = {"_root": root}
    for name in drive_archive.FOLDERS:
        out[name] = client.ensure_folder(name, root)
    old = {n: cached[n] for n in drive_archive.OLD_FOLDERS if cached.get(n)} or cached.get("_old") or {}
    if old:
        out["_old"] = old
    set_archive_setting("folders", json.dumps(out))
    db.session.commit()
    if old:
        reorganize_archive(client, out)
    return out


def subfolder(client, folders, top, name):
    """A dated folder inside Daily Backups / Weekly Backups (cached)."""
    key = f"sub2:{top}:{name}"
    fid = archive_setting(key)
    if not fid:
        fid = client.ensure_folder(name, folders[top])
        set_archive_setting(key, fid)
        db.session.commit()
    return fid


def applicant_folder(client, folders, a, sub=None):
    """Applicants/<app id - name>/ (renamed when the name changes), or its Documents / Old Versions."""
    person = _safe_name(a.applicant_name or (a.form_values or {}).get("name") or "", 60)
    name = f"{a.app_id} - {person}" if person != "unnamed" else a.app_id
    key = f"appdir:{a.id}"
    info = json.loads(archive_setting(key) or "{}")
    if not info.get("id"):
        info = {"id": client.ensure_folder(name, folders["Applicants"]), "name": name}
        set_archive_setting(key, json.dumps(info))
        db.session.commit()
    elif info.get("name") != name:
        client.rename(info["id"], name)
        info["name"] = name
        set_archive_setting(key, json.dumps(info))
        db.session.commit()
    if not sub:
        return info["id"]
    skey = f"appdir:{a.id}:{sub}"
    fid = archive_setting(skey)
    if not fid:
        fid = client.ensure_folder(sub, info["id"])
        set_archive_setting(skey, fid)
        db.session.commit()
    return fid


def reorganize_archive(client, folders):
    """One-time move from the older 8-folder layout: every file goes to its applicant's folder or to
    the dated backup folder. Nothing is deleted: only the old 'latest' copies of the database (the
    same data is in each dated daily folder) and the emptied old folders go to Drive's bin, where
    they stay recoverable for 30 days."""
    import re as _re
    moved = errors = 0
    seen = set()
    for r in ArchiveFile.query.filter(ArchiveFile.drive_file_id.isnot(None)).order_by(ArchiveFile.id.desc()):
        if r.drive_file_id in seen:
            continue
        seen.add(r.drive_file_id)
        try:
            target = None
            if r.application_id and r.category in ("form", "guarantor", "version", "document"):
                a = db.session.get(Application, r.application_id)
                if a is None:
                    continue
                target = applicant_folder(client, folders, a, {"version": "Old Versions",
                                                              "document": "Documents"}.get(r.category))
            elif r.category == "report":
                m = _re.search(r"(\d{4}-\d{2}-\d{2})", r.name or "")
                target = subfolder(client, folders, "Daily Backups",
                                   m.group(1) if m else pk_now().strftime("%Y-%m-%d"))
            elif r.category == "database" and "- latest" in (r.name or ""):
                client.trash(r.drive_file_id)
                (ArchiveFile.query.filter(ArchiveFile.drive_file_id == r.drive_file_id)
                 .update({"status": "superseded",
                          "last_error": "duplicate 'latest' copy put in Drive's bin when folders were simplified"},
                         synchronize_session=False))
                db.session.commit()
                continue
            if target and r.drive_folder_id != target:
                client.move(r.drive_file_id, target)
                (ArchiveFile.query.filter(ArchiveFile.drive_file_id == r.drive_file_id)
                 .update({"drive_folder_id": target}, synchronize_session=False))
                db.session.commit()
                moved += 1
        except Exception as exc:
            db.session.rollback()
            if getattr(exc, "status", None) == 404:       # already gone from Drive: verification handles it
                continue
            errors += 1
            app.logger.warning("archive reorganize %s: %s", r.id, exc)
    if errors:                                           # try again next time; old folders stay
        _log_run("reorganize", False, {"moved": moved, "errors": errors},
                 f"Folders simplified: {moved} files moved, {errors} not yet (retried next run)")
        return
    def has_files(fid):
        return any(c.get("mimeType") != drive_archive.FOLDER_MIME or has_files(c["id"])
                   for c in client.children(fid))
    kept = []
    for name, fid in folders.get("_old", {}).items():
        try:
            if has_files(fid):
                kept.append(name)
            else:
                client.trash(fid)
        except Exception:
            kept.append(name)
    out = {k: v for k, v in folders.items() if k != "_old"}
    if kept:
        out["_old"] = {n: folders["_old"][n] for n in kept}
    folders.clear()
    folders.update(out)
    set_archive_setting("folders", json.dumps(out))
    db.session.commit()
    _log_run("reorganize", not kept, {"moved": moved, "kept": kept},
             f"Folders simplified: {moved} files moved into Applicants / Daily Backups"
             + (f"; still holding files: {', '.join(kept)}" if kept else ""))


def _fingerprints(app_ids=None):
    """{application pk: fingerprint} — changes whenever anything about the application changes."""
    def maxes(model, col):
        q = db.session.query(col, func.max(model.id)).group_by(col)
        if app_ids is not None:
            q = q.filter(col.in_(app_ids))
        return dict(q.all())
    audits = maxes(AuditLog, AuditLog.application_id)
    docs = maxes(Document, Document.application_id)
    notes = maxes(ReviewNote, ReviewNote.application_id)
    q = db.session.query(Application.id, Application.version, Application.status, Application.updated_at,
                         Application.deleted_at)
    if app_ids is not None:
        q = q.filter(Application.id.in_(app_ids))
    return {i: f"v{v}|{s}|{u.isoformat() if u else ''}|a{audits.get(i, 0)}|d{docs.get(i, 0)}|n{notes.get(i, 0)}"
               f"|{'del' if dl else ''}"
            for i, v, s, u, dl in q.all()}


def _archived_fingerprints(app_ids=None):
    """{application pk: fingerprint of its newest verified archived version}."""
    q = (db.session.query(ArchiveFile.application_id, ArchiveFile.fingerprint)
         .filter(ArchiveFile.category == "version", ArchiveFile.status == "verified")
         .order_by(ArchiveFile.application_id, ArchiveFile.version))
    if app_ids is not None:
        q = q.filter(ArchiveFile.application_id.in_(app_ids))
    return {a: f for a, f in q.all()}


def _is_empty_draft(a):
    return a.status == "Draft" and not a.data and not a.cnic and not a.applicant_name


def archive_state(page_size=500):
    """Classify every application (fetched page by page, none skipped):
    {"backed_up": [...], "pending": [...], "failed": [...], "empty": [...]}."""
    out = {"backed_up": [], "pending": [], "failed": [], "empty": []}
    failed = {a for (a,) in db.session.query(ArchiveFile.application_id)
              .filter(ArchiveFile.status.in_(("failed", "missing", "corrupt")),
                      ArchiveFile.application_id.isnot(None)).distinct()}
    last = 0
    while True:
        page = (Application.query.filter(Application.id > last).order_by(Application.id).limit(page_size).all())
        if not page:
            return out
        ids = [a.id for a in page]
        now_fp, done_fp = _fingerprints(ids), _archived_fingerprints(ids)
        for a in page:
            if _is_empty_draft(a) and a.id not in done_fp:
                out["empty"].append(a.id)
            elif a.id in failed:
                out["failed"].append(a.id)
            elif done_fp.get(a.id) == now_fp.get(a.id):
                out["backed_up"].append(a.id)
            else:
                out["pending"].append(a.id)
        last = page[-1].id


def _safe_name(s, n=80):
    s = "".join(ch for ch in (s or "") if ch not in '\\/:*?"<>|\r\n\t').strip()
    return s[:n] or "unnamed"


def _doc_bytes(d):
    try:
        return read_file(d)
    except Exception:
        return None


def archive_record(a, version):
    """Everything about one application, as plain data for word_form."""
    names = {u.id: u.display for u in User.query.all()}
    when = lambda v: (v + PK_HOURS).strftime("%Y-%m-%d %H:%M") if v else ""
    vals = a.form_values
    photos = {}
    for k in PHOTO_KINDS:
        p = a.latest_photo(k)
        if p:
            photos[k] = _doc_bytes(p)
    cnic_rows = []
    for g_, title, name_field in cnic_groups_for(a):
        row = {"key": g_, "title": title.upper(), "name": vals.get(name_field, "") if name_field else ""}
        for side in ("front", "back"):
            d = a.latest_photo(f"cnic_{g_}_{side}")
            row[side] = _doc_bytes(d) if d else None
        cnic_rows.append(row)
    all_docs = Document.query.filter_by(application_id=a.id).order_by(Document.id).all()
    documents = [(d.id, d.kind, d.original_name, d.content_type, d.size, d.sha256 or "", when(d.created_at),
                  "deleted" if d.deleted_at else ("replaced" if d.replaced_by else "active")) for d in all_docs]
    images = [(f"Document {d.id}: {d.original_name}", b) for d in all_docs
              if d.kind == "document" and not d.deleted_at and (d.content_type or "").startswith("image/")
              and (b := _doc_bytes(d))]
    info = [("Application ID", a.app_id), ("Loan number", a.loan_number or ""), ("Status", a.status),
            ("Loan amount", money(a.loan_amount)), ("Purpose", a.purpose_label or ""),
            ("Form fee", money(a.fee_amount)), ("Created by", names.get(a.created_by, "")),
            ("Created", when(a.created_at)), ("Last updated", when(a.updated_at)),
            ("Updated by", names.get(a.updated_by, "")), ("Submitted", when(a.submitted_at)),
            ("Assigned staff", names.get(a.assigned_to, "")), ("Reviewer", names.get(a.reviewer_id, "")),
            ("Approved by", names.get(a.approved_by, "")), ("Approved", when(a.approved_at)),
            ("Rejection reason", a.rejection_reason or ""),
            ("Family member CNIC required", "Yes" if a.family_cnic_required else "No"),
            ("Deleted", when(a.deleted_at) if a.deleted_at else "No"), ("Record version", a.version)]
    notes = [(when(n.created_at), n.kind, names.get(n.author_id, ""),
              n.body + (f" (resolved {when(n.resolved_at)})" if n.resolved_at else ""))
             for n in ReviewNote.query.filter_by(application_id=a.id).order_by(ReviewNote.id)]
    disb = []
    for e in DisbursementEntry.query.filter_by(application_id=a.id).order_by(DisbursementEntry.id):
        b = db.session.get(DisbursementBatch, e.batch_id)
        disb.append((f"Sheet {ordinal(b.number) if b else '?'} ({b.disb_date if b else ''})",
                     f"Amount {money(e.amount)} · fee {money(e.fee_amount)} · "
                     + ("paid " + when(e.disbursed_at) if e.disbursed_at else "not paid")
                     + (f" · removed: {e.remove_reason or ''}" if e.removed_at else "")))
        for c in Cheque.query.filter_by(entry_id=e.id).order_by(Cheque.id):
            disb.append((f"  Cheque {c.cheque_no}", f"{c.status} · {when(c.created_at)}"
                         + (f" · {c.cancel_reason}" if c.cancel_reason else "")))
    history = []
    for h in AuditLog.query.filter_by(application_id=a.id).order_by(AuditLog.id):
        detail = ""
        if h.old_value or h.new_value:
            detail = json.dumps({"old": h.old_value, "new": h.new_value}, ensure_ascii=False)[:400]
        history.append((when(h.created_at), h.actor_name or "", ACTION_LABELS.get(h.action, h.action),
                        (h.reason + " " if h.reason else "") + detail))
    return {"app_id": a.app_id, "version": version, "generated_at": pk_now().strftime("%Y-%m-%d %H:%M"),
            "values": vals, "photos": photos, "cnic_rows": cnic_rows, "info": info, "notes": notes,
            "disbursement": disb, "documents": documents, "images": images, "history": history}


def _upload_verified(client, row, data, mime, parent, props, existing_id=None):
    """Upload (or replace the content of `existing_id`), then check Drive has exactly these bytes."""
    row.attempts = (row.attempts or 0) + 1
    want = drive_archive.md5(data)
    if existing_id:
        meta = client.update(existing_id, data, mime, name=row.name)
    else:
        meta = client.upload(row.name, data, mime, parent, props,
                             lock=(props or {}).get("kind") not in ("form", "guarantor", "latest"))
    row.drive_file_id, row.drive_folder_id = meta.get("id"), parent
    row.uploaded_at = datetime.utcnow()
    got = meta.get("md5Checksum")
    if not got:                                  # some responses omit it; ask again
        got = (client.get(meta["id"]) or {}).get("md5Checksum")
    if got != want or int(meta.get("size") or len(data)) != len(data):
        raise drive_archive.DriveError(f"Drive copy does not match (md5 {got} != {want})")
    row.md5, row.size, row.status = want, len(data), "verified"
    row.verified_at, row.last_error = datetime.utcnow(), None


def _fail(row, exc):
    row.status, row.last_error = "failed", f"{type(exc).__name__}: {str(exc)[:480]}"


def archive_application(app_pk, client, folders):
    """Archive one application if it changed since its last verified version.
    Returns "unchanged", "empty", "archived" or raises after recording the failure."""
    a = db.session.get(Application, app_pk)
    if a is None:
        return "gone"
    fp = _fingerprints([a.id]).get(a.id)
    done = _archived_fingerprints([a.id]).get(a.id)
    docs_todo = _documents_to_archive(a.id)
    latest_ok = _latest_ok(a.id)
    if done == fp and not docs_todo and latest_ok:
        _clear_failures(a.id)
        return "unchanged"
    if _is_empty_draft(a) and done is None:
        return "empty"
    tag = {"tooba": "1", "app": a.id, "app_id": a.app_id}
    # 1) supporting documents (each once; replaced / deleted ones are kept too)
    for d in docs_todo:
        _archive_document(client, folders, a, d, tag)
    new_version = done != fp
    last_version = (db.session.query(func.max(ArchiveFile.version))
                    .filter(ArchiveFile.application_id == a.id, ArchiveFile.category == "version").scalar() or 0)
    version = last_version + 1 if new_version else max(last_version, 1)
    if not new_version and latest_ok:
        _clear_failures(a.id)
        return "archived"
    # 2) the filled Word form, checked before it leaves the server
    record = archive_record(a, version)
    data = word_form.build(record)
    problems = word_form.verify(data, a.app_id, {k: v for k, v in record["values"].items()
                                                 if k in FIELD_NAMES and k not in ("cnic", "g1cnic", "g2cnic")})
    if problems:
        raise RuntimeError(f"Word form for {a.app_id} failed its check: {'; '.join(problems[:5])}")
    stamp = pk_now().strftime("%Y-%m-%d %H%M")
    person = _safe_name(a.applicant_name or record["values"].get("name"))
    if new_version:
        # 2a) this version, as its own file that is never overwritten
        vrow = ArchiveFile(application_id=a.id, category="version", version=version, fingerprint=fp,
                           name=f"{a.app_id} - v{version:03d} - {stamp} - {person}.docx")
        db.session.add(vrow)
        db.session.commit()
        try:
            _upload_verified(client, vrow, data, drive_archive.DOCX_MIME,
                             applicant_folder(client, folders, a, "Old Versions"),
                             dict(tag, kind="version", version=version))
        except Exception as exc:
            db.session.rollback()
            vrow = db.session.get(ArchiveFile, vrow.id)
            _fail(vrow, exc)
            db.session.commit()
            raise
        db.session.commit()
    # 2b) the current form (one file per applicant; older contents stay as Drive revisions)
    _archive_latest(client, folders, a, "form",
                    f"{a.app_id} - {person}.docx", data, version, fp, tag)
    # 2c) guarantor records
    _archive_latest(client, folders, a, "guarantor",
                    f"{a.app_id} - Guarantors - {person}.docx", word_form.build_guarantors(record), version, fp, tag)
    _clear_failures(a.id)
    return "archived"


def _latest_ok(app_pk):
    """Both the current form and the guarantor file are in Drive and verified."""
    have = {}
    for r in (ArchiveFile.query.filter(ArchiveFile.application_id == app_pk,
                                       ArchiveFile.category.in_(("form", "guarantor")))
              .order_by(ArchiveFile.id)):
        have[r.category] = r.status
    return have.get("form") == "verified" and have.get("guarantor") == "verified"


def _archive_latest(client, folders, a, category, name, data, version, fp, tag):
    row = (ArchiveFile.query.filter_by(application_id=a.id, category=category)
           .order_by(ArchiveFile.id.desc()).first())
    existing = row.drive_file_id if row and row.drive_file_id and row.status == "verified" else None
    if row is None or not existing:
        row = row or ArchiveFile(application_id=a.id, category=category)
        db.session.add(row)
    row.name, row.version, row.fingerprint, row.status = name, version, fp, "pending"
    db.session.commit()
    try:
        _upload_verified(client, row, data, drive_archive.DOCX_MIME, applicant_folder(client, folders, a),
                         dict(tag, kind=category), existing_id=existing)
    except Exception as exc:
        db.session.rollback()
        row = db.session.get(ArchiveFile, row.id)
        _fail(row, exc)
        db.session.commit()
        raise
    db.session.commit()


def _documents_to_archive(app_pk):
    done = {d for (d,) in db.session.query(ArchiveFile.document_id)
            .filter(ArchiveFile.application_id == app_pk, ArchiveFile.category == "document",
                    ArchiveFile.status == "verified")}
    return [d for d in Document.query.filter_by(application_id=app_pk).order_by(Document.id) if d.id not in done]


def _archive_document(client, folders, a, d, tag):
    row = (ArchiveFile.query.filter_by(application_id=a.id, category="document", document_id=d.id)
           .order_by(ArchiveFile.id.desc()).first())
    if row is None:
        state = "deleted " if d.deleted_at else ("replaced " if d.replaced_by else "")
        row = ArchiveFile(application_id=a.id, document_id=d.id, category="document",
                          name=f"{d.id} - {state}{d.kind} - {_safe_name(d.original_name, 120)}")
        db.session.add(row)
    row.status = "pending"
    db.session.commit()
    try:
        data = read_file(d)
        if d.sha256 and hashlib.sha256(data).hexdigest() != d.sha256:
            raise RuntimeError(f"document {d.id} bytes do not match its stored SHA-256")
        _upload_verified(client, row, data, d.content_type or "application/octet-stream",
                         applicant_folder(client, folders, a, "Documents"),
                         dict(tag, kind="document", doc=d.id))
    except Exception as exc:
        db.session.rollback()
        row = db.session.get(ArchiveFile, row.id)
        _fail(row, exc)
        db.session.commit()
        raise
    db.session.commit()


def _clear_failures(app_pk):
    """An application that is now fully archived has no open failures left."""
    n = (ArchiveFile.query.filter(ArchiveFile.application_id == app_pk,
                                  ArchiveFile.status.in_(("failed", "pending", "missing", "corrupt")),
                                  ArchiveFile.category.in_(("version",)))
         .update({"status": "superseded"}, synchronize_session=False))
    if n:
        db.session.commit()


def run_archive(budget=None, only=None, kind="retry"):
    """Archive every application that changed (or failed before), oldest first, within a time budget.
    Returns a summary dict. Failures are recorded and retried next time; they never stop the run."""
    import time as _t
    budget = budget if budget is not None else float(os.getenv("ARCHIVE_TIME_BUDGET", "45"))
    t0 = _t.monotonic()
    summary = {"archived": 0, "unchanged": 0, "empty": 0, "failed": 0, "left": 0, "errors": []}
    if not archive_configured():
        summary["errors"].append("Google Drive is not connected")
        return summary
    client = archive_client()
    folders = archive_folders(client)
    if only is not None:
        todo = list(only)
    else:
        st = archive_state()
        todo = st["failed"] + st["pending"]
    for i, pk in enumerate(todo):
        if _t.monotonic() - t0 > budget:
            summary["left"] = len(todo) - i
            break
        try:
            r = archive_application(pk, client, folders)
            summary[r if r in summary else "unchanged"] += 1
        except Exception as exc:
            db.session.rollback()
            summary["failed"] += 1
            summary["errors"].append(f"{pk}: {str(exc)[:200]}")
    if only is None or summary["failed"] or summary["archived"]:
        _log_run(kind, not summary["failed"] and not summary["left"], summary,
                 f"{summary['archived']} archived, {summary['failed']} failed, {summary['left']} left for later")
    return summary


def _log_run(kind, ok, summary, message):
    try:
        db.session.add(ArchiveRun(kind=kind, ok=ok, message=message[:2000], summary=summary,
                                  finished_at=datetime.utcnow()))
        db.session.commit()
    except Exception:
        db.session.rollback()


def archive_after_change(app_pk):
    """Called right after a user saved something: archive that application now (and one more
    pending one), as the system, without ever breaking the user's request."""
    if not (ARCHIVE_DB and archive_configured()) or os.getenv("ARCHIVE_INLINE", "1") == "0":
        return
    try:
        db.session.remove()
        g._db_actor, g._archive_inline = ("", "system"), True
        only = [app_pk] if app_pk else []
        run_archive(budget=float(os.getenv("ARCHIVE_INLINE_BUDGET", "25")), only=only, kind="application")
    except Exception:
        db.session.rollback()
    finally:
        db.session.remove()


ARCHIVE_TRIGGERS = {"loan_form", "update_status", "upload", "save_details", "set_family_cnic", "add_note",
                    "set_loan_number", "assign_application", "delete_document", "delete_application",
                    "restore_application"}


@app.after_request
def _archive_hook(response):
    for pk in g.pop("_archive_after", None) or []:
        archive_after_change(pk)
    if (request.method == "POST" and request.endpoint in ARCHIVE_TRIGGERS and response.status_code < 400
            and current_user and current_user.is_authenticated):
        pk = None
        if request.endpoint == "delete_document":
            d = db.session.get(Document, request.view_args.get("doc_id"))
            pk = d.application_id if d else None
        else:
            pk = request.view_args.get("app_id")
        archive_after_change(pk)
    return response


# ---- complete database backups
def _db_files(stamp):
    """(json.gz, csv zip, xlsx, payload) — all from one snapshot."""
    data, counts = backup.build(db.session, db.metadata)
    payload = db_export.load(data)
    return [(f"Tooba Database {stamp}.json.gz", data, "application/gzip"),
            (f"Tooba Database {stamp} - CSV.zip", db_export.csv_zip(payload), "application/zip"),
            (f"Tooba Database {stamp}.xlsx", db_export.xlsx(payload), drive_archive.XLSX_MIME)], payload


def _word_forms_zip(stamp, page_size=200):
    """Every application that has anything filled in, as its complete filled Word form plus its
    guarantor file, in one zip (deleted ones too, marked). Returns (bytes, count, problems)."""
    z, count, problems, last = io.BytesIO(), 0, [], 0
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        while True:
            page = (Application.query.filter(Application.id > last).order_by(Application.id)
                    .limit(page_size).all())
            if not page:
                break
            for a in page:
                if _is_empty_draft(a):
                    continue
                try:
                    version = (db.session.query(func.max(ArchiveFile.version))
                               .filter(ArchiveFile.application_id == a.id, ArchiveFile.category == "version")
                               .scalar() or a.version or 1)
                    rec = archive_record(a, version)
                    person = _safe_name(a.applicant_name or rec["values"].get("name"))
                    prefix = "Deleted applications/" if a.deleted_at else ""
                    zf.writestr(f"{prefix}{a.app_id} - {person}.docx", word_form.build(rec))
                    zf.writestr(f"{prefix}Guarantors/{a.app_id} - Guarantors - {person}.docx",
                                word_form.build_guarantors(rec))
                    count += 1
                except Exception as exc:
                    problems.append(f"{a.app_id}: {type(exc).__name__}: {str(exc)[:200]}")
            last = page[-1].id
        zf.writestr("README.txt", f"Tooba Loan Department - complete filled Word forms, {stamp}.\n"
                    f"{count} applications. One file per applicant (application form, all pages, CNIC "
                    "pictures and complete record), plus the guarantor file in Guarantors/.\n"
                    + ("Problems:\n" + "\n".join(problems) if problems else ""))
    return z.getvalue(), count, problems


def _manifest():
    return [{"id": f.id, "application_id": f.application_id, "document_id": f.document_id,
             "category": f.category, "version": f.version, "name": f.name, "drive_file_id": f.drive_file_id,
             "md5": f.md5, "size": f.size, "status": f.status,
             "verified_at": f.verified_at.isoformat() if f.verified_at else None}
            for f in ArchiveFile.query.order_by(ArchiveFile.id)]


def _put_db_file(client, folder, name, data, mime, kind, existing=None):
    row = ArchiveFile(category="database" if kind != "report" else "report", name=name)
    db.session.add(row)
    db.session.commit()
    try:
        _upload_verified(client, row, data, mime, folder, {"tooba": "1", "kind": kind}, existing_id=existing)
        if existing:                          # same Drive file, new content: older rows no longer describe it
            (ArchiveFile.query.filter(ArchiveFile.drive_file_id == row.drive_file_id, ArchiveFile.id != row.id)
             .update({"status": "superseded"}, synchronize_session=False))
    except Exception as exc:
        db.session.rollback()
        row = db.session.get(ArchiveFile, row.id)
        _fail(row, exc)
        db.session.commit()
        raise
    db.session.commit()
    return row


def database_backup(weekly=False):
    """Daily (or weekly) complete backup into Drive, each file checked after upload."""
    client = archive_client()
    folders = archive_folders(client, check=True)
    now = pk_now()
    files, payload = _db_files(now.strftime("%Y-%m-%d"))
    counts = {t: len(r) for t, r in payload["tables"].items()}
    forms, nforms, form_problems = _word_forms_zip(now.strftime("%Y-%m-%d"))
    if weekly:
        y, w, _ = now.isocalendar()
        label = f"{y}-W{w:02d}"
        folder = subfolder(client, folders, "Weekly Backups", label)
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data, _ in files:
                zf.writestr(("database.json.gz" if name.endswith(".json.gz") else name), data)
            zf.writestr("Word Forms.zip", forms)
            zf.writestr("archive-manifest.json", json.dumps(_manifest(), ensure_ascii=False, indent=1))
            zf.writestr("README.txt", "Tooba Loan Department weekly full archive.\n"
                        "database.json.gz holds every table and every uploaded document (original IDs).\n"
                        "Word Forms.zip holds the complete filled Word form of every application.\n"
                        "archive-manifest.json lists every Word form / document file in the Drive archive.\n"
                        "Restore with: python restore_backup.py <this zip> <NEW database URL>\n")
        _put_db_file(client, folder, f"Tooba Full Archive {label}.zip", z.getvalue(), "application/zip", "weekly")
        _log_run("weekly", not form_problems, {"counts": counts, "word_forms": nforms, "problems": form_problems},
                 f"Weekly full archive {label} saved ({nforms} Word forms)"
                 + (f"; Word form problems: {'; '.join(form_problems)[:600]}" if form_problems else ""))
        return label
    folder = subfolder(client, folders, "Daily Backups", now.strftime("%Y-%m-%d"))
    for name, data, mime in files:
        _put_db_file(client, folder, name, data, mime, "daily")
    _put_db_file(client, folder, f"Tooba Word Forms {now.strftime('%Y-%m-%d')}.zip", forms, "application/zip", "daily")
    _log_run("daily", not form_problems, {"counts": counts, "word_forms": nforms, "problems": form_problems},
             f"Daily complete database backup saved, with {nforms} filled Word forms"
             + (f"; Word form problems: {'; '.join(form_problems)[:600]}" if form_problems else ""))
    return counts


def weekly_due():
    last = (ArchiveRun.query.filter_by(kind="weekly", ok=True).order_by(ArchiveRun.id.desc()).first())
    return pk_now().weekday() == 6 or last is None or last.started_at < datetime.utcnow() - timedelta(days=7)


def verify_archive(write_report=True):
    """Compare the database with what is really in Drive. Marks missing / changed files so the next
    run uploads them again, finds duplicates, and counts. Returns the report dict."""
    client = archive_client()
    folders = archive_folders(client, check=True)
    drive = {f["id"]: f for f in client.list_archive()}
    rep = {"checked_at": pk_now().strftime("%Y-%m-%d %H:%M"), "missing": [], "corrupt": [], "duplicates": [],
           "untracked": [], "edited_database_copies": []}
    # a dated database copy that someone opened and saved in Drive: the data itself is in the other
    # copies (json / csv / later days), so it is reported, not treated as a broken application archive
    for r in ArchiveFile.query.filter(ArchiveFile.status == "corrupt",
                                      ArchiveFile.category.in_(("database", "report"))):
        r.status = "edited"
    db.session.commit()
    tracked = set()
    # a "latest" file is one Drive file updated in place: only its newest row describes it now
    newest = dict(db.session.query(ArchiveFile.drive_file_id, func.max(ArchiveFile.id))
                  .filter(ArchiveFile.drive_file_id.isnot(None)).group_by(ArchiveFile.drive_file_id).all())
    for r in ArchiveFile.query.filter(ArchiveFile.status.in_(("verified", "missing", "corrupt")),
                                      ArchiveFile.drive_file_id.isnot(None)):
        if newest.get(r.drive_file_id) != r.id:
            r.status = "superseded"
    db.session.commit()
    last = 0
    while True:                                       # every archived file, page by page
        rows = (ArchiveFile.query.filter(ArchiveFile.id > last, ArchiveFile.status == "verified")
                .order_by(ArchiveFile.id).limit(500).all())
        if not rows:
            break
        for r in rows:
            tracked.add(r.drive_file_id)
            f = drive.get(r.drive_file_id)
            if f is None:
                f = client.get(r.drive_file_id)      # not tagged / search lag: ask directly
            if f is None or f.get("trashed"):
                r.status, r.last_error = "missing", "not found in Drive during verification"
                rep["missing"].append(r.name)
            elif r.md5 and f.get("md5Checksum") and f["md5Checksum"] != r.md5:
                if r.category in ("database", "report"):
                    r.status = "edited"
                    r.last_error = "changed in Google Drive after upload (opened and saved there)"
                    rep["edited_database_copies"].append(r.name)
                else:
                    r.status, r.last_error = "corrupt", "Drive content differs from what was uploaded"
                    rep["corrupt"].append(r.name)
        db.session.commit()
        last = rows[-1].id
    groups = {}
    for f in drive.values():
        p = f.get("appProperties") or {}
        key = (p.get("kind"), p.get("app"), p.get("version") or p.get("doc"))
        if p.get("kind") in ("version", "document", "form", "guarantor"):
            groups.setdefault(key, []).append(f)
        if f["id"] not in tracked and p.get("kind") in ("version", "document"):
            rep["untracked"].append(f.get("name"))
    rep["duplicates"] = [[f.get("name") for f in fs] for fs in groups.values() if len(fs) > 1]
    st = archive_state()
    total = Application.query.count()
    archived_apps = {p.get("app") for f in drive.values() if (p := f.get("appProperties") or {}).get("kind") == "form"}
    rep.update({"applications_in_database": total,
                "applications_with_form_in_drive": len(archived_apps),
                "backed_up": len(st["backed_up"]), "pending": len(st["pending"]), "failed": len(st["failed"]),
                "empty_drafts": len(st["empty"]),
                "not_yet_archived": [a.app_id for a in Application.query.filter(
                    Application.id.in_(st["pending"] + st["failed"])).order_by(Application.id).limit(200)]})
    rep["ok"] = (not rep["missing"] and not rep["corrupt"] and not rep["duplicates"] and not st["failed"]
                 and not st["pending"] and len(archived_apps) == total - len(st["empty"]))
    if write_report:
        text_ = json.dumps(rep, ensure_ascii=False, indent=1)
        try:
            _put_db_file(client, subfolder(client, folders, "Daily Backups", pk_now().strftime("%Y-%m-%d")),
                         f"Verification Report {rep['checked_at'].replace(':', '')}.json",
                         text_.encode(), "application/json", "report")
        except Exception as exc:
            rep.setdefault("errors", []).append(f"report upload: {exc}")
    _log_run("verify", rep["ok"], rep,
             f"{rep['applications_with_form_in_drive']}/{total - rep['empty_drafts']} applications in Drive; "
             f"{len(rep['missing'])} missing, {len(rep['corrupt'])} changed, {len(rep['duplicates'])} duplicates, "
             + (f"{len(rep['edited_database_copies'])} database copies edited in Drive, "
                if rep["edited_database_copies"] else "") +
             f"{rep['pending']} pending, {rep['failed']} failed")
    return rep


@app.route("/cron/archive")
@csrf.exempt
def cron_archive():
    """Vercel Cron, daily: archive anything not yet in Drive (retrying failures), then verify."""
    _cron_ok()
    if not ARCHIVE_DB:
        return jsonify(ok=False, error="archive migration not applied"), 503
    if not archive_configured():
        alert("archive", "Google Drive archive NOT CONNECTED",
              "Applications Google Drive par archive nahi ho rahi: Drive connect nahi.\n"
              "Admin > Backup & Recovery > Connect Google Drive.", every=timedelta(hours=20))
        return jsonify(ok=False, error="Google Drive not connected"), 503
    try:
        s = run_archive()
        rep = verify_archive()
        if rep["missing"] or rep["corrupt"]:          # re-upload anything verification found missing
            s2 = run_archive(budget=60)
            s["archived"] += s2["archived"]
    except Exception as exc:
        db.session.rollback()
        _log_run("retry", False, {}, f"{type(exc).__name__}: {str(exc)[:500]}")
        alert("archive", "Google Drive archive FAILED", f"Roz ka archive / verification nahi hua.\n\n{str(exc)[:500]}",
              every=timedelta(hours=20))
        return jsonify(ok=False, error=str(exc)[:300]), 500
    if not rep["ok"]:
        alert("archive", "Google Drive archive needs attention",
              f"{rep['pending']} pending, {rep['failed']} failed, {len(rep['missing'])} missing, "
              f"{len(rep['corrupt'])} changed, {len(rep['duplicates'])} duplicate file group(s).\n"
              "Admin > Backup & Recovery dekhein.", every=timedelta(hours=20))
    return jsonify(ok=rep["ok"], archived=s["archived"], failed=s["failed"], left=s["left"],
                   missing=len(rep["missing"]), corrupt=len(rep["corrupt"]))


# ---- Admin → Backup & Recovery
def _redirect_uri():
    base = os.getenv("APP_BASE_URL") or request.url_root.rstrip("/")
    return base.rstrip("/") + url_for("backup_google_callback")


@app.route("/admin/backup-recovery")
@requires("export")
def backup_recovery():
    ctx = {"ready": ARCHIVE_DB, "configured": False, "mode": None}
    if ARCHIVE_DB:
        st = archive_state()
        token = stored_drive_token()
        last_ok = (db.session.query(func.max(ArchiveFile.verified_at)).filter(ArchiveFile.status == "verified")
                   .scalar())
        last_db = ArchiveRun.query.filter(ArchiveRun.kind.in_(("daily", "weekly")), ArchiveRun.ok.is_(True)) \
            .order_by(ArchiveRun.id.desc()).first()
        folders = json.loads(archive_setting("folders") or "{}")
        ctx.update(
            configured=archive_configured(), mode=("test client" if ARCHIVE_CLIENT else drive_archive.mode(token)),
            account=archive_setting("oauth_email"), can_connect=bool(drive_archive.oauth_client()),
            total=Application.query.count(), counts={k: len(v) for k, v in st.items()},
            failed_apps=Application.query.filter(Application.id.in_(st["failed"])).order_by(Application.id).all(),
            pending_apps=Application.query.filter(Application.id.in_(st["pending"])).order_by(Application.id)
            .limit(50).all(),
            last_ok=last_ok, last_db=last_db,
            failures=ArchiveFile.query.filter(ArchiveFile.status.in_(("failed", "missing", "corrupt")))
            .order_by(ArchiveFile.id.desc()).limit(30).all(),
            runs=ArchiveRun.query.order_by(ArchiveRun.id.desc()).limit(40).all(),
            folder_url=(f"https://drive.google.com/drive/folders/{folders['_root']}" if folders.get("_root") else None),
            redirect_uri=_redirect_uri())
    return render_template("backup_recovery.html", **ctx, pk=PK_HOURS)


@app.route("/admin/backup-recovery/run", methods=["POST"])
@requires("export")
def backup_recovery_run():
    action = request.form.get("action")
    if not (ARCHIVE_DB and archive_configured()):
        flash("Pehle Google Drive connect karein.")
        return redirect(url_for("backup_recovery"))
    try:
        if action == "backup":
            s = run_archive(kind="manual")
            database_backup()
            msg = (f"Backup mukammal: {s['archived']} application(s) archive hui, {s['failed']} fail, "
                   f"{s['left']} baqi (agli dafa). Database backup Drive par save aur verify ho gaya.")
            if s["failed"] or s["left"]:
                msg += " Kuch kaam baqi hai — dobara 'Backup Now' dabayein ya raat ka automatic run intezar karein."
        elif action == "verify":
            rep = verify_archive()
            msg = ("Verification: sab theek hai." if rep["ok"] else
                   f"Verification: {rep['pending']} pending, {rep['failed']} failed, {len(rep['missing'])} missing, "
                   f"{len(rep['corrupt'])} changed, {len(rep['duplicates'])} duplicate group(s).")
        elif action == "weekly":
            msg = f"Weekly full archive {database_backup(weekly=True)} Drive par save ho gaya."
        else:
            abort(400)
        audit("backup.archive", new={"action": action})
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        msg = f"Backup mein masla: {str(exc)[:300]}"
    flash(msg)
    return redirect(url_for("backup_recovery"))


@app.route("/admin/backup-recovery/download")
@requires("export")
def backup_recovery_download():
    stamp = pk_now().strftime("%Y-%m-%d")
    files, payload = _db_files(stamp)
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data, _ in files:
            zf.writestr(("database.json.gz" if name.endswith(".json.gz") else name), data)
        if ARCHIVE_DB:
            zf.writestr("archive-manifest.json", json.dumps(_manifest(), ensure_ascii=False, indent=1))
    audit("export.full_backup", new={"tables": {t: len(r) for t, r in payload["tables"].items()}})
    db.session.commit()
    return Response(z.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=tooba-complete-backup-{stamp}.zip"})


@app.route("/admin/backup-recovery/form/<int:app_id>.docx")
@requires("export")
def backup_recovery_form(app_id):
    """The filled Word form of one application, made on the spot (same file as in Drive)."""
    a = get_app(app_id, allow_deleted=True)
    data = word_form.build(archive_record(a, a.version))
    return Response(data, mimetype=drive_archive.DOCX_MIME,
                    headers={"Content-Disposition": f"attachment; filename={a.app_id}.docx"})


@app.route("/admin/backup-recovery/restore", methods=["POST"])
@requires("export")
def backup_recovery_restore():
    """Check an uploaded backup: validates it and lists what the live database is missing.
    Never writes to the live database — a real restore goes into a separate database first."""
    f = request.files.get("backup")
    if not f or not f.filename:
        flash("Backup file chunein.")
        return redirect(url_for("backup_recovery"))
    try:
        payload = db_export.load(f.read())
        report = db_export.validate(payload, db.metadata)
        live = {}
        for t in db.metadata.sorted_tables:
            if "id" in t.columns and t.name in payload["tables"]:
                try:
                    live[t.name] = {i for (i,) in db.session.execute(sa_select(t.c.id))}
                except Exception:
                    db.session.rollback()
        diff = db_export.compare(payload, live)
    except Exception as exc:
        flash(f"Ye file parhi nahi ja saki: {str(exc)[:200]}")
        return redirect(url_for("backup_recovery"))
    audit("backup.restore_check", new={"file": secure_filename(f.filename), "ok": report["ok"]})
    db.session.commit()
    return render_template("restore_check.html", payload=payload, report=report, diff=diff, filename=f.filename)


# ---------------------------------------------------------------- bring applications back from a Drive backup
def backup_choices():
    """Daily / weekly backup files in Drive that hold the whole database, newest first."""
    rows = (ArchiveFile.query.filter(ArchiveFile.category == "database", ArchiveFile.drive_file_id.isnot(None),
                                     ArchiveFile.status.in_(("verified", "edited")),
                                     or_(ArchiveFile.name.like("Tooba Database %.json.gz"),
                                         ArchiveFile.name.like("Tooba Full Archive %.zip")))
            .order_by(ArchiveFile.id.desc()).limit(400).all())
    seen, out = set(), []
    for r in rows:
        if r.name in seen or "latest" in r.name:
            continue
        seen.add(r.name)
        when = (r.verified_at or r.created_at)
        out.append({"id": r.id, "name": r.name, "when": when + PK_HOURS if when else None,
                    "weekly": r.name.endswith(".zip")})
    return out


def load_drive_backup(af_id):
    """(payload, label) of one backup file from Drive, checked against the md5 recorded at upload."""
    r = db.session.get(ArchiveFile, af_id)
    if r is None or r.category != "database" or not r.drive_file_id or "latest" in (r.name or ""):
        raise ValueError("Ye backup file nahi mili.")
    cache = os.path.join(tempfile.gettempdir(), f"tooba-backup-{r.drive_file_id}-{r.md5}")
    data = None
    if os.path.exists(cache):
        with open(cache, "rb") as fh:
            data = fh.read()
    if data is None or (r.md5 and drive_archive.md5(data) != r.md5):
        data = archive_client().download(r.drive_file_id)
        if r.md5 and drive_archive.md5(data) != r.md5:
            raise ValueError("Drive wali file badli hui hai (checksum match nahi); is backup par bharosa nahi kiya ja sakta.")
        try:
            with open(cache, "wb") as fh:
                fh.write(data)
        except OSError:
            pass
    return db_export.load(data), r.name


def backup_applications(payload):
    """Every application in a backup, with what the live database has for it."""
    tables = payload.get("tables") or {}
    apps = tables.get("application") or []
    ids = [a["id"] for a in apps]
    live = {}
    for i in range(0, len(ids), 500):
        for a in Application.query.filter(Application.id.in_(ids[i:i + 500])):
            live[a.id] = a
    live_codes = {code: i for i, code in db.session.query(Application.id, Application.app_id)}
    per = {}
    for t in ("document", "review_note", "audit_log", "disbursement_entry"):
        for r in tables.get(t) or []:
            per.setdefault(r.get("application_id"), {}).setdefault(t, 0)
            per[r.get("application_id")][t] += 1
    out = []
    for a in apps:
        form = {}
        try:
            form = json.loads(a.get("form_json") or "{}")
        except ValueError:
            pass
        l = live.get(a["id"])
        if l is not None:
            state = "deleted" if l.deleted_at else "live"
        elif live_codes.get(a.get("app_id")) not in (None, a["id"]):
            state = "clash"
        else:
            state = "missing"
        out.append({"id": a["id"], "app_id": a.get("app_id"), "name": a.get("applicant_name") or form.get("name") or "",
                    "cnic": a.get("cnic") or form.get("cnic") or "", "status": a.get("status"),
                    "deleted_in_backup": bool(a.get("deleted_at")), "state": state,
                    "documents": per.get(a["id"], {}).get("document", 0),
                    "notes": per.get(a["id"], {}).get("review_note", 0),
                    "history": per.get(a["id"], {}).get("audit_log", 0),
                    "disbursement": per.get(a["id"], {}).get("disbursement_entry", 0)})
    return out


def _restore_rows(table, rows, users, fix):
    """Typed rows for `table`; user references that no longer exist become empty (or the admin)."""
    cols = {c.name: c for c in table.columns}
    user_cols = [c.name for c in table.columns
                 if any(fk.column.table.name == "user" for fk in c.foreign_keys)]
    out = []
    for r in rows:
        row = {k: db_export._typed(cols[k], v) for k, v in r.items() if k in cols}
        for c in user_cols:
            if row.get(c) is not None and row[c] not in users:
                row[c] = None if cols[c].nullable else current_user.id
        fix(row)
        out.append(row)
    return out


def bring_back_application(payload, app_pk, label):
    """Put one application from a backup back into the live database, without changing anything
    that is already there. Returns a result dict. Never overwrites: a live application is only
    un-deleted; rows whose IDs already exist are left alone and counted."""
    from sqlalchemy import insert as sa_insert, update as sa_update
    tables = payload.get("tables") or {}
    src = next((a for a in tables.get("application") or [] if a["id"] == app_pk), None)
    if src is None:
        return {"id": app_pk, "ok": False, "message": "Ye application is backup mein nahi."}
    res = {"id": app_pk, "app_id": src.get("app_id"), "name": src.get("applicant_name") or ""}
    live = db.session.get(Application, app_pk)
    if live is not None:
        if live.deleted_at:
            live.deleted_at, live.deleted_by = None, None
            audit("application.restored", live, reason=f"Backup se wapas: {label}")
            db.session.commit()
            return dict(res, ok=True, message="Deleted list se wapas aa gayi (data pehle hi website par tha).")
        return dict(res, ok=True, message="Pehle se website par maujood hai; kuch nahi badla.")
    if Application.query.filter_by(app_id=src.get("app_id")).first():
        return dict(res, ok=False, message=f"ID {src.get('app_id')} ab kisi aur application ki hai; wapas nahi laayi gayi.")
    T = db.metadata.tables
    users = {i for (i,) in db.session.query(User.id)}
    purposes = {i for (i,) in db.session.query(T["purpose_category"].c.id)}
    fees = {i for (i,) in db.session.query(T["fee_category"].c.id)}
    names = {u["id"]: u.get("username") for u in tables.get("user") or []}

    def fix_app(row):
        if row.get("purpose_id") not in purposes:
            row["purpose_id"] = None
        if row.get("fee_category_id") not in fees:
            row["fee_category_id"] = None
    pg = not USING_SQLITE
    try:
        db.session.execute(sa_insert(T["application"]), _restore_rows(T["application"], [src], users, fix_app))
        counts = {}
        for t in ("document", "review_note", "audit_log"):
            rows = [r for r in tables.get(t) or [] if r.get("application_id") == app_pk]
            have = set()
            ids = [r["id"] for r in rows]
            for i in range(0, len(ids), 500):
                have |= {x for (x,) in db.session.execute(sa_select(T[t].c.id).where(T[t].c.id.in_(ids[i:i + 500])))}
            rows = [r for r in rows if r["id"] not in have]
            counts[t] = (len(rows), len(have))
            if not rows:
                continue
            if t == "document":
                links = {r["id"]: r.get("replaced_by") for r in rows if r.get("replaced_by")}
                typed = _restore_rows(T[t], rows, users, lambda row: row.update(replaced_by=None))
                db.session.execute(sa_insert(T[t]), typed)
                for did, to in links.items():
                    db.session.execute(sa_update(T[t]).where(T[t].c.id == did).values(replaced_by=to))
            elif t == "review_note":
                def note(row, src_rows={r["id"]: r for r in rows}):
                    orig = src_rows[row["id"]]
                    who = names.get(orig.get("author_id")) or "?"
                    row["body"] = f"[Backup se wapas laaya gaya · asal: {who}, {str(orig.get('created_at'))[:16]}] " \
                                  + (row.get("body") or "")
                    row["author_id"] = current_user.id          # the database only accepts notes by the person
                db.session.execute(sa_insert(T[t]), _restore_rows(T[t], rows, users, note))
            else:                                               # the history keeps its original authors
                if pg:
                    db.session.execute(_SET_ACTOR, {"u": "", "r": "system"})
                db.session.execute(sa_insert(T[t]), _restore_rows(T[t], rows, users, lambda row: None))
                if pg:
                    db.session.execute(_SET_ACTOR, {"u": str(current_user.id), "r": current_user.role})
        a = db.session.get(Application, app_pk)
        skipped = len([r for r in tables.get("disbursement_entry") or [] if r.get("application_id") == app_pk])
        audit("application.restored_from_backup", a, reason=f"Backup se wapas: {label}",
              new={"documents": counts["document"][0], "notes": counts["review_note"][0],
                   "history": counts["audit_log"][0], "disbursement_rows_not_restored": skipped})
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return dict(res, ok=False, message=f"Wapas nahi aa saki: {type(exc).__name__}: {str(exc)[:200]}")
    msg = f"Wapas aa gayi: {counts['document'][0]} photos/documents, {counts['review_note'][0]} notes, " \
          f"{counts['audit_log'][0]} history entries."
    clash = sum(v[1] for v in counts.values())
    if clash:
        msg += f" {clash} purani entries pehle se maujood thin, unhein nahi chheda."
    if skipped:
        msg += f" {skipped} disbursement sheet entries wapas nahi laayi gayin; zaroorat ho to sheet par dobara add karein."
    return dict(res, ok=True, message=msg)


@app.route("/admin/backup-recovery/bring-back")
@requires("export", "app.restore")
def bring_back_page():
    if not ARCHIVE_DB:
        return redirect(url_for("backup_recovery"))
    choices = backup_choices()
    bid = request.args.get("backup", type=int)
    apps, label, error = None, None, None
    if bid:
        try:
            payload, label = load_drive_backup(bid)
            apps = backup_applications(payload)
            q = request.args.get("q", "").strip().lower()
            if q:
                digits = "".join(ch for ch in q if ch.isdigit())
                apps = [a for a in apps if q in (a["app_id"] or "").lower() or q in (a["name"] or "").lower()
                        or (digits and digits in "".join(ch for ch in str(a["cnic"]) if ch.isdigit()))]
        except Exception as exc:
            db.session.rollback()
            error = str(exc)[:300]
    return render_template("bring_back.html", choices=choices, bid=bid, apps=apps, label=label, error=error,
                           configured=archive_configured(), q=request.args.get("q", ""))


@app.route("/admin/backup-recovery/bring-back", methods=["POST"])
@requires("export", "app.restore")
def bring_back_run():
    bid = request.form.get("backup", type=int)
    try:
        payload, label = load_drive_backup(bid)
    except Exception as exc:
        flash(f"Backup khul nahi saka: {str(exc)[:200]}")
        return redirect(url_for("bring_back_page"))
    apps = {a["id"]: a for a in backup_applications(payload)}
    if request.form.get("all") == "1":
        chosen = [i for i, a in apps.items() if a["state"] in ("missing", "deleted")]
    else:
        chosen = [i for i in (request.form.getlist("app", type=int)) if i in apps]
    chosen = [i for i in chosen if apps[i]["state"] in ("missing", "deleted")]
    if not chosen:
        flash("Koi application nahi chuni gayi jo wapas laani ho.")
        return redirect(url_for("bring_back_page", backup=bid))
    if request.form.get("confirm") != "yes":
        return render_template("bring_back_confirm.html", bid=bid, label=label,
                               apps=[apps[i] for i in chosen])
    results = [bring_back_application(payload, i, label) for i in chosen]
    done = [r for r in results if r.get("ok")]
    g._archive_after = [r["id"] for r in done[:3]]   # into Drive after the page; the nightly run does the rest
    return render_template("bring_back_done.html", label=label, results=results)


@app.route("/admin/backup-recovery/google/connect")
@requires("export")
def backup_google_connect():
    if not drive_archive.oauth_client():
        flash("Pehle Vercel mein GOOGLE_OAUTH_CLIENT_ID aur GOOGLE_OAUTH_CLIENT_SECRET lagayein.")
        return redirect(url_for("backup_recovery"))
    session["drive_oauth_state"] = state = secrets.token_urlsafe(24)
    return redirect(drive_archive.auth_url(_redirect_uri(), state))


@app.route("/admin/backup-recovery/google/callback")
@requires("export")
def backup_google_callback():
    state = session.pop("drive_oauth_state", None)
    if not state or not secrets.compare_digest(state, request.args.get("state", "")):
        abort(400)
    if request.args.get("error") or not request.args.get("code"):
        flash("Google Drive connect nahi hua: " + (request.args.get("error") or "no code"))
        return redirect(url_for("backup_recovery"))
    try:
        token, email = drive_archive.exchange_code(request.args["code"], _redirect_uri())
    except Exception as exc:
        flash(f"Google Drive connect nahi hua: {str(exc)[:200]}")
        return redirect(url_for("backup_recovery"))
    set_archive_setting("oauth_token", drive_archive.seal(token, app.config["SECRET_KEY"]))
    set_archive_setting("oauth_email", email or "")
    set_archive_setting("folders", None)
    audit("backup.drive_connected", new={"account": email})
    db.session.commit()
    try:
        archive_folders(archive_client(), check=True)
        flash(f"Google Drive connect ho gaya ({email or 'account'}). Archive folders ban gaye.")
    except Exception as exc:
        flash(f"Connect ho gaya lekin folders nahi bane: {str(exc)[:200]}")
    return redirect(url_for("backup_recovery"))


@app.route("/admin/backup-recovery/google/disconnect", methods=["POST"])
@requires("export")
def backup_google_disconnect():
    set_archive_setting("oauth_token", None)
    audit("backup.drive_disconnected", new={"account": archive_setting("oauth_email")})
    db.session.commit()
    flash("Google Drive disconnect ho gaya. Drive mein archive files waisi hi maujood hain.")
    return redirect(url_for("backup_recovery"))


# ---------------------------------------------------------------- full Excel export
@app.route("/admin/export.xlsx")
@requires("export")
def export_xlsx():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    names = usernames()
    wb = Workbook()

    def sheet(ws, header, rows):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = Font(bold=True), PatternFill("solid", fgColor="ECF7EF")
        for r in rows:
            ws.append([v if isinstance(v, (int, float)) or v is None else csv_safe(v) for v in r])
        ws.freeze_panes = "A2"
        for i, h in enumerate(header, 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(12, min(40, len(str(h)) + 4))

    num = lambda v: float(v) if v is not None else None
    when = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else None
    apps = Application.query.order_by(Application.id).all()
    keys = FIELD_NAMES + sorted({k for a in apps for k in a.data} - set(FIELD_NAMES))
    ws = wb.active
    ws.title = "Applications"
    sheet(ws, ["Application ID", "Loan No.", "Status", "Name", "CNIC", "Loan amount", "Purpose", "Form fee",
               "Created by", "Created", "Submitted", "Approved", "Reviewer", "Deleted"]
          + [FIELD_LABELS.get(k, k) for k in keys],
          [[a.app_id, a.loan_number, a.status, a.applicant_name, a.cnic, num(a.loan_amount), a.purpose_label,
            num(a.fee_amount), names.get(a.created_by), when(a.created_at), when(a.submitted_at),
            when(a.approved_at), names.get(a.reviewer_id), "Yes" if a.deleted_at else ""]
           + [a.data.get(k, "") for k in keys] for a in apps])
    by_app = {a.id: a.app_id for a in apps}
    rows = (db.session.query(DisbursementEntry, DisbursementBatch)
            .join(DisbursementBatch, DisbursementBatch.id == DisbursementEntry.batch_id)
            .order_by(DisbursementBatch.number, DisbursementEntry.id).all())
    cheques = {}
    for c in Cheque.query.order_by(Cheque.id):
        cheques.setdefault(c.entry_id, []).append(c)
    sheet(wb.create_sheet("Disbursements"),
          ["Sheet No.", "Sheet date", "Sheet status", "Application ID", "Name", "CNIC", "Cell", "Amount",
           "Purpose", "Loan No.", "Form fee", "Cheque No.", "Cheque status", "Paid", "Removed / deleted"],
          [[b.number, b.disb_date,
            "Deleted" if b.deleted_at else b.status, by_app.get(e.application_id), e.name, e.cnic, e.phone,
            num(e.amount), e.purpose, e.loan_number, num(e.fee_amount),
            ", ".join(c.cheque_no for c in cheques.get(e.id, [])),
            ", ".join(c.status for c in cheques.get(e.id, [])),
            when(e.disbursed_at), e.remove_reason or ("Yes" if e.removed_at else "")] for e, b in rows])
    sheet(wb.create_sheet("Users"), ["Username", "Name", "Role", "Active", "Created"],
          [[u.username, u.full_name, u.role, "Yes" if u.active and not u.deleted_at else "No", when(u.created_at)]
           for u in User.query.order_by(User.id)])
    out = io.BytesIO()
    wb.save(out)
    audit("export.xlsx", new={"applications": len(apps), "disbursement_rows": len(rows)})
    db.session.commit()
    stamp = (datetime.utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d")
    return Response(out.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=tooba-all-data-{stamp}.xlsx"})


# ---------------------------------------------------------------- reports
def report_data(year):
    return cached("report_data", lambda: _report_data(year), int(year))


def _report_data(year):
    # plain rows with just the columns used below (attribute access is the same as on objects)
    apps = live(Application.query).with_entities(
        Application.id, Application.created_at, Application.approved_at, Application.loan_amount,
        Application.status, Application.created_by, Application.reviewer_id, Application.purpose_label).all()
    names = usernames()
    entries = (db.session.query(DisbursementEntry, DisbursementBatch)
               .join(DisbursementBatch, DisbursementBatch.id == DisbursementEntry.batch_id)
               .filter(DisbursementEntry.removed_at.is_(None)).all())
    rejected_at = {}
    for h in (AuditLog.query.filter_by(action="application.status")
              .with_entities(AuditLog.application_id, AuditLog.new_value, AuditLog.created_at)
              .order_by(AuditLog.id).all()):
        if (h.new_value or {}).get("status") == "Rejected":
            rejected_at[h.application_id] = h.created_at
    amt = lambda a: int(a.loan_amount or 0)
    # one pass over the applications instead of four per month (same numbers)
    created_c, approved_c, approved_amt, rejected_c, created_y, approved_y = ({} for _ in range(6))
    for a in apps:
        if a.created_at is not None:
            k = (a.created_at.year, a.created_at.month)
            created_c[k] = created_c.get(k, 0) + 1
            created_y[k[0]] = created_y.get(k[0], 0) + 1
        if a.approved_at is not None:
            k = (a.approved_at.year, a.approved_at.month)
            approved_c[k] = approved_c.get(k, 0) + 1
            approved_amt[k] = approved_amt.get(k, 0) + amt(a)
            approved_y[k[0]] = approved_y.get(k[0], 0) + 1
        d = rejected_at.get(a.id)
        if d is not None:
            k = (d.year, d.month)
            rejected_c[k] = rejected_c.get(k, 0) + 1
    monthly = []
    for m in range(1, 13):
        ents = [e for e, b in entries if b.year == year and b.month == m]
        monthly.append({
            "month": gsheets.MONTHS[m - 1],
            "created": created_c.get((year, m), 0),
            "approved": approved_c.get((year, m), 0),
            "approved_amount": approved_amt.get((year, m), 0),
            "rejected": rejected_c.get((year, m), 0),
            "sheet_count": len(ents),
            "disbursed": sum(1 for e in ents if e.disbursed_at),
            "disbursed_amount": sum(int(e.amount or 0) for e in ents if e.disbursed_at),
            "fees": sum(int(e.fee_amount or 0) for e in ents)})
    years = sorted({a.created_at.year for a in apps if a.created_at} | {b.year for _, b in entries} | {year})
    yearly = []
    for y in years:
        ents = [e for e, b in entries if b.year == y]
        yearly.append({"year": y, "created": created_y.get(y, 0),
                       "approved": approved_y.get(y, 0),
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


# ---------------------------------------------------------------- static files: browser cache
# Every url_for('static', ...) gets ?v=<file fingerprint>, so the browser may keep CSS / JS /
# images for a year and still fetches the new file right after a deploy changes it.
_static_versions = {}


@app.url_defaults
def _static_version(endpoint, values):
    if endpoint == "static" and "filename" in values and "v" not in values:
        fn = values["filename"]
        if fn not in _static_versions:
            try:
                st = os.stat(os.path.join(app.static_folder, fn))
                _static_versions[fn] = hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:10]
            except OSError:
                _static_versions[fn] = None
        if _static_versions[fn]:
            values["v"] = _static_versions[fn]


def _static_max_age(filename):
    # fingerprinted URL: cache for a year; plain /static/... (e.g. the logo in Google Sheets): 1 hour
    return 31536000 if has_request_context() and request.args.get("v") else 3600


app.get_send_file_max_age = _static_max_age


# ---------------------------------------------------------------- rate limits
# Generous limits that a person never reaches, tight ones only on heavy downloads. Counted per
# signed-in user (staff in one office share one internet address) or per address when signed out.
# In-memory per server instance: Vercel runs several, so add a Vercel Firewall rate-limit rule
# for a global limit (see PERFORMANCE.md).
import threading as _threading
import time as _rl_time

RATE_LIMITS = {"heavy": (10, 60), "search": (120, 60), "photo": (300, 60), "any": (600, 60), "anon": (300, 60)}
HEAVY_ENDPOINTS = {"export_csv", "export_xlsx", "backup_export", "backup_drive_now", "reports_export",
                   "disbursement_xlsx", "backup_recovery_run", "backup_recovery_download", "backup_recovery_form",
                   "backup_recovery_restore"}
_rl_hits, _rl_lock = {}, _threading.Lock()


def _rate_ok(bucket, who):
    limit, window = RATE_LIMITS[bucket]
    now = _rl_time.monotonic()
    key = (bucket, who)
    with _rl_lock:
        times = [t for t in _rl_hits.get(key, ()) if now - t < window]
        if len(times) >= limit:
            _rl_hits[key] = times
            return False
        times.append(now)
        _rl_hits[key] = times
        if len(_rl_hits) > 20000:                 # forget idle keys
            for k in [k for k, v in _rl_hits.items() if not v or now - v[-1] > window][:10000]:
                _rl_hits.pop(k, None)
    return True


@app.before_request
def rate_limit():
    if app.config.get("TESTING") or request.endpoint in (None, "static", "cron_gsheets", "cron_backup", "cron_archive"):
        return None
    signed_in = current_user and current_user.is_authenticated
    who = f"u{current_user.id}" if signed_in else f"ip{client_ip()}"
    buckets = ["any" if signed_in else "anon"]
    if request.endpoint in HEAVY_ENDPOINTS:
        buckets.append("heavy")
    elif request.endpoint == "gsheet_photo":
        buckets.append("photo")
    elif request.args.get("q"):
        buckets.append("search")
    if all(_rate_ok(b, who) for b in buckets):
        return None
    return render_template("message.html", title="Thora intezar karein",
                           msg="Bohat zyada requests aa rahi hain. Ek minute baad dobara koshish karein."), 429


# ---------------------------------------------------------------- performance timing (off by default)
# PERF_TIMING=1 adds a Server-Timing header (database time + number of queries, total time) to
# every response, readable in the browser's Network tab and by the load test. No data, no SQL.
PERF_TIMING = os.getenv("PERF_TIMING") == "1"
if PERF_TIMING:
    import time as _time
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, "before_cursor_execute")
    def _q_start(conn, cursor, statement, parameters, context, executemany):
        if has_request_context():
            g._q_t0 = _time.perf_counter()

    @event.listens_for(Engine, "after_cursor_execute")
    def _q_end(conn, cursor, statement, parameters, context, executemany):
        if has_request_context() and "_q_t0" in g:
            g._db_ms = g.get("_db_ms", 0.0) + (_time.perf_counter() - g._q_t0) * 1000
            g._db_n = g.get("_db_n", 0) + 1

    @app.before_request
    def _req_start():
        g._req_t0 = _time.perf_counter()

    @app.after_request
    def _server_timing(response):
        total = (_time.perf_counter() - g.get("_req_t0", _time.perf_counter())) * 1000
        response.headers["Server-Timing"] = (f'db;dur={g.get("_db_ms", 0.0):.1f};desc="{g.get("_db_n", 0)} queries", '
                                             f"app;dur={total:.1f}")
        return response


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
        # Performance migration (20261107…) is optional: without it the app keeps the old
        # (slower) search and simply does not cache.
        # Drive archive migration (20261117…) is optional too: without it the archive stays off.
        ARCHIVE_DB = all(_insp.has_table(t) for t in ARCHIVE_TABLES)
        with db.engine.connect() as _c:
            PERF_DB = bool(_c.execute(text(
                "select count(*) = 2 from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                "where n.nspname = 'public' and p.proname in ('search_application_ids', 'data_version')")).scalar())
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
