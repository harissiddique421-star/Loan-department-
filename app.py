import os, json, secrets, csv, io
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, flash, abort,
                   send_file, Response, jsonify)
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import inspect, text
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
        import hashlib
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

# Every logged-in user can open/edit every application unless this is set to 1.
RESTRICT_STAFF = os.getenv("RESTRICT_STAFF_ACCESS", "0") == "1"
STATUSES = ["Draft", "Pending", "In Review", "Approved", "Rejected", "Completed"]
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

    def set_password(self, p): self.password_hash = generate_password_hash(p)
    def check_password(self, p): return check_password_hash(self.password_hash, p)

    def session_token(self):
        # Changes whenever the password changes, so old sessions (other browsers, stolen
        # cookies) stop working after a password change or admin reset.
        import hashlib
        return hashlib.sha256(("sess:" + self.password_hash).encode()).hexdigest()[:16]

    def get_id(self):
        return f"{self.id}:{self.session_token()}"


class Application(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    app_id = db.Column(db.String(20), unique=True, index=True)
    applicant_name = db.Column(db.String(200), default="")
    cnic = db.Column(db.String(30), index=True, default="")
    status = db.Column(db.String(30), default="Draft")
    assigned_to = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    form_json = db.Column(db.Text, default="{}")
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    updated_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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

    def latest_photo(self, kind):
        return (Document.query.filter_by(application_id=self.id, kind=kind)
                .order_by(Document.id.desc()).first())

    @property
    def photo(self):
        return self.latest_photo("photo")


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=False)
    original_name = db.Column(db.String(255))
    stored_name = db.Column(db.String(255), unique=True)
    kind = db.Column(db.String(20), default="document")      # "photo" or "document"
    content_type = db.Column(db.String(120), default="application/octet-stream")
    size = db.Column(db.Integer, default=0)
    storage = db.Column(db.String(20), default="db")          # "db", "supabase" or "local" (legacy)
    data = db.Column(db.LargeBinary, nullable=True)            # file bytes when storage == "db"
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_image(self):
        return (self.content_type or "").startswith("image/")


class Log(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    action = db.Column(db.String(200))
    application_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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
                   uploaded_by=current_user.id)
    bucket = supabase_bucket()
    if bucket is not None:
        bucket.upload(stored, blob, {"content-type": doc.content_type})
        doc.storage = "supabase"
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


def delete_file(doc):
    if doc.storage == "supabase":
        bucket = supabase_bucket()
        if bucket is not None:
            bucket.remove([doc.stored_name])


# ---------------------------------------------------------------- helpers
@lm.user_loader
def load_user(uid):
    sid, _, token = str(uid).partition(":")
    if not sid.isdigit() or not token:
        return None
    u = db.session.get(User, int(sid))
    if not u or not u.active or not secrets.compare_digest(token, u.session_token()):
        return None
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


def admin_required(fn):
    @wraps(fn)
    @login_required
    def inner(*args, **kwargs):
        if current_user.role != "Admin":
            abort(403)
        return fn(*args, **kwargs)
    return inner


def log_action(action, app_id=None, user_id=None):
    uid = user_id if user_id is not None else (current_user.id if current_user.is_authenticated else None)
    db.session.add(Log(user_id=uid, action=action[:200], application_id=app_id))
    db.session.commit()


def csv_safe(v):
    """Stop CSV/Excel formula injection (=HYPERLINK(...), +cmd, @SUM...) from applicant data."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def allowed_app(a):
    if not RESTRICT_STAFF or current_user.role == "Admin":
        return True
    return a.created_by == current_user.id or a.assigned_to == current_user.id


def get_app(app_id):
    a = db.get_or_404(Application, app_id)
    if not allowed_app(a):
        abort(403)
    return a


def usernames():
    return {u.id: u.username for u in User.query.all()}


@app.context_processor
def inject_globals():
    return {"STATUSES": STATUSES, "USING_SQLITE": USING_SQLITE, "ON_VERCEL": ON_VERCEL,
            "FORM_SECTIONS": FORM_SECTIONS, "FIELD_LABELS": FIELD_LABELS,
            "PHOTO_SLOTS": PHOTO_SLOTS, "PHOTO_LABELS": PHOTO_LABELS}


# ---------------------------------------------------------------- auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()[:80]
        ip = client_ip()
        if login_blocked(username, ip):
            flash(f"Bohat zyada ghalat koshishein. {int(LOGIN_WINDOW.total_seconds() // 60)} minute baad try karein.")
            return render_template("login.html"), 429
        u = User.query.filter_by(username=username).first()
        password = request.form.get("password", "")
        # Always run one hash check so response time doesn't reveal which usernames exist.
        ok = u.check_password(password) if u else (check_password_hash(_DUMMY_HASH, password) and False)
        if u and u.active and ok:
            login_user(u)
            log_action("Login ip=" + ip, user_id=u.id)
            return redirect(url_for("dashboard"))
        log_action(f"LOGIN_FAIL user={username[:60]} ip={ip}", user_id=u.id if u else None)
        flash("Username ya password ghalat hai, ya account disabled hai.")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


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
            current_user.set_password(request.form["new"])
            db.session.commit()
            login_user(current_user._get_current_object())   # keep this session; all other sessions are now logged out
            log_action("Changed own password")
            flash("Password change ho gaya.")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html")


# ---------------------------------------------------------------- applications
@app.route("/")
@login_required
def dashboard():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    base = Application.query
    if RESTRICT_STAFF and current_user.role != "Admin":
        base = base.filter((Application.created_by == current_user.id) | (Application.assigned_to == current_user.id))
    counts = {s: base.filter_by(status=s).count() for s in STATUSES}
    total = base.count()
    query = base
    if q:
        like = f"%{q}%"
        query = query.filter(Application.applicant_name.ilike(like) | Application.cnic.ilike(like)
                             | Application.app_id.ilike(like))
    if status:
        query = query.filter_by(status=status)
    apps = query.order_by(Application.updated_at.desc()).limit(200).all()
    return render_template("dashboard.html", apps=apps, total=total, counts=counts, q=q,
                           status=status, names=usernames())


def next_app_id():
    last = Application.query.order_by(Application.id.desc()).first()
    n = (last.id + 1) if last else 1
    while Application.query.filter_by(app_id=f"TBF-{n:04d}").first():
        n += 1
    return f"TBF-{n:04d}"


@app.route("/application/new", methods=["POST", "GET"])
@login_required
def new_application():
    if request.method == "GET":
        return redirect(url_for("dashboard"))
    a = Application(app_id=next_app_id(), status="Draft", created_by=current_user.id,
                    updated_by=current_user.id, form_json="{}")
    db.session.add(a)
    db.session.commit()
    log_action("Created " + a.app_id, a.id)
    return redirect(url_for("loan_form", app_id=a.id))


@app.route("/application/<int:app_id>")
@login_required
def view_application(app_id):
    a = get_app(app_id)
    docs = Document.query.filter_by(application_id=a.id).order_by(Document.id.desc()).all()
    logs = Log.query.filter_by(application_id=a.id).order_by(Log.id.desc()).limit(30).all()
    staff = User.query.filter_by(active=True).order_by(User.username).all()
    return render_template("application.html", a=a, docs=docs, logs=logs, staff=staff, names=usernames())


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
    old = Document.query.filter_by(application_id=a.id, kind=kind).all()
    doc, err = save_file(a, f, kind)
    if not err:
        for d in old:
            delete_file(d)
            db.session.delete(d)
    return err


@app.route("/application/<int:app_id>/form", methods=["GET", "POST"])
@login_required
def loan_form(app_id):
    a = get_app(app_id)
    if request.method == "GET":
        return render_template("original_form.html", a=a, payload=form_payload(a))

    wants_json = request.headers.get("X-Requested-With") == "fetch"

    def fail(msg):
        if wants_json:
            return jsonify(ok=False, error=msg), 400
        flash(msg)
        return redirect(url_for("loan_form", app_id=a.id))

    values = a.form_values
    values.pop("full_name", None)
    for name in FIELD_NAMES:
        if name in request.form:
            values[name] = request.form.get(name, "").strip()
    for name in ("cnic", "g1cnic", "g2cnic"):
        values[name] = "".join(ch for ch in values.get(name, "") if ch.isdigit())[:13]
    if not values.get("name"):
        return fail("Applicant ka naam (Name) zaroori hai.")
    cnic = values.get("cnic", "")
    if cnic and Application.query.filter(Application.cnic == cnic, Application.id != a.id).first():
        return fail("Is CNIC par doosri application pehle se mojood hai.")

    errors = []
    for kind in PHOTO_KINDS:
        f = request.files.get("photo_" + kind)
        if f and f.filename:
            err = replace_photo(a, kind, f)
            if err:
                errors.append(f"{PHOTO_LABELS[kind]}: {err}")
        elif request.form.get("remove_" + kind) == "1":
            for d in Document.query.filter_by(application_id=a.id, kind=kind).all():
                delete_file(d)
                db.session.delete(d)
    for f in request.files.getlist("documents"):
        if f and f.filename:
            _, err = save_file(a, f, "document")
            if err:
                errors.append(f"{f.filename}: {err}")

    a.applicant_name, a.cnic = values["name"], cnic
    if request.form.get("status") in STATUSES:
        a.status = request.form["status"]
    a.form_json = json.dumps(values, ensure_ascii=False)
    a.updated_by, a.updated_at = current_user.id, datetime.utcnow()
    db.session.commit()
    log_action("Saved form " + a.app_id, a.id)
    flash("Form save ho gaya." + ("" if not errors else " Lekin: " + "; ".join(errors)))
    target = url_for("view_application", app_id=a.id)
    return jsonify(ok=True, redirect=target) if wants_json else redirect(target)


@app.route("/application/<int:app_id>/print")
@login_required
def print_application(app_id):
    a = get_app(app_id)
    log_action("Opened print " + a.app_id, a.id)
    return render_template("print.html", a=a, payload=form_payload(a))


@app.route("/application/<int:app_id>/status", methods=["POST"])
@login_required
def update_status(app_id):
    a = get_app(app_id)
    s = request.form.get("status")
    if s in STATUSES:
        a.status, a.updated_by = s, current_user.id
        db.session.commit()
        log_action(f"Status {s} " + a.app_id, a.id)
        flash("Status update ho gaya.")
    return redirect(url_for("view_application", app_id=a.id))


@app.route("/application/<int:app_id>/assign", methods=["POST"])
@login_required
def assign_application(app_id):
    a = get_app(app_id)
    uid = request.form.get("staff_id")
    staff = User.query.filter_by(id=uid, active=True).first() if uid else None
    a.assigned_to = staff.id if staff else None
    db.session.commit()
    log_action("Assigned " + a.app_id + (f" to {staff.username}" if staff else ""), a.id)
    flash("Assignment update ho gayi.")
    return redirect(url_for("view_application", app_id=a.id))


@app.route("/application/<int:app_id>/delete", methods=["POST"])
@admin_required
def delete_application(app_id):
    a = db.get_or_404(Application, app_id)
    for d in Document.query.filter_by(application_id=a.id).all():
        delete_file(d)
        db.session.delete(d)
    db.session.flush()   # documents must go first: document.application_id references application
    db.session.delete(a)
    db.session.commit()
    log_action("Deleted " + a.app_id)
    flash(a.app_id + " delete ho gayi.")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- documents / pictures
@app.route("/application/<int:app_id>/upload", methods=["POST"])
@login_required
def upload(app_id):
    a = get_app(app_id)
    kind = request.form.get("kind") if request.form.get("kind") in PHOTO_KINDS else "document"
    files = [f for f in request.files.getlist("file") if f and f.filename]
    if not files:
        flash("File select karein.")
        return redirect(url_for("view_application", app_id=a.id))
    if kind != "document":
        files = files[:1]   # one picture per photo slot; it replaces the old one
    ok = 0
    for f in files:
        err = replace_photo(a, kind, f) if kind != "document" else save_file(a, f, kind)[1]
        if err:
            flash(f"{f.filename}: {err}")
        else:
            ok += 1
    a.updated_by, a.updated_at = current_user.id, datetime.utcnow()
    db.session.commit()
    if ok:
        log_action(f"Uploaded {ok} {kind}(s) to " + a.app_id, a.id)
        flash(f"{ok} file upload ho gayi.")
    return redirect(url_for("view_application", app_id=a.id))


@app.route("/document/<int:doc_id>")
@login_required
def document(doc_id):
    d = db.get_or_404(Document, doc_id)
    get_app(d.application_id)
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
    delete_file(d)
    db.session.delete(d)
    db.session.commit()
    log_action(f"Deleted file {d.original_name} from " + a.app_id, a.id)
    flash("File delete ho gayi.")
    return redirect(url_for("view_application", app_id=a.id))


# ---------------------------------------------------------------- admin
@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "Staff")
        if role not in ("Admin", "Staff") or not username or len(password) < 8:
            flash("Username aur kam az kam 8 character password zaroori hai.")
        elif User.query.filter_by(username=username).first():
            flash("Ye username pehle se mojood hai.")
        else:
            u = User(username=username, role=role)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            log_action("Added user " + username)
            flash(f"User '{username}' add ho gaya.")
        return redirect(url_for("users"))
    return render_template("users.html", users=User.query.order_by(User.username).all())


@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
@admin_required
def toggle_user(uid):
    u = db.get_or_404(User, uid)
    if u.id == current_user.id:
        flash("Apna account disable nahi kar sakte.")
    else:
        u.active = not u.active
        db.session.commit()
    return redirect(url_for("users"))


@app.route("/admin/users/<int:uid>/password", methods=["POST"])
@admin_required
def reset_password(uid):
    u = db.get_or_404(User, uid)
    p = request.form.get("password", "")
    if len(p) < 8:
        flash("Password kam az kam 8 characters ka ho.")
    else:
        u.set_password(p)
        db.session.commit()
        log_action("Reset password for " + u.username)
        flash(f"{u.username} ka password change ho gaya.")
    return redirect(url_for("users"))


@app.route("/admin/export")
@admin_required
def export_csv():
    apps = Application.query.order_by(Application.id).all()
    known = FIELD_NAMES
    keys = known + sorted({k for a in apps for k in a.data} - set(known))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([csv_safe(x) for x in ["Application ID", "Status", "Created", "Updated"]
                + [FIELD_LABELS.get(k, k) for k in keys]])
    for a in apps:
        d = a.data
        w.writerow([csv_safe(x) for x in [a.app_id, a.status, a.created_at, a.updated_at]
                    + [d.get(k, "") for k in keys]])
    log_action("Exported CSV")
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=applications.csv"})


@app.route("/admin/reports")
@admin_required
def reports():
    from sqlalchemy import func
    by_status = db.session.query(Application.status, func.count(Application.id)).group_by(Application.status).all()
    by_staff = (db.session.query(User.username, func.count(Application.id))
                .outerjoin(Application, Application.assigned_to == User.id).group_by(User.id).all())
    return render_template("reports.html", by_status=by_status, by_staff=by_staff,
                           total=Application.query.count())


@app.route("/admin/backup")
@admin_required
def backup_export():
    payload = {
        "created_at": datetime.utcnow().isoformat() + "Z",
        "applications": [{"id": a.id, "app_id": a.app_id, "name": a.applicant_name, "cnic": a.cnic,
                          "status": a.status, "assigned_to": a.assigned_to, "form": a.data,
                          "created_by": a.created_by,
                          "created_at": a.created_at.isoformat() if a.created_at else None}
                         for a in Application.query.all()],
        "users": [{"id": u.id, "username": u.username, "role": u.role, "active": u.active}
                  for u in User.query.all()],
        "documents": [{"id": d.id, "application_id": d.application_id, "name": d.original_name,
                       "kind": d.kind, "storage": d.storage, "size": d.size}
                      for d in Document.query.all()],
    }
    log_action("Downloaded backup")
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
    if current_user.is_authenticated and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'")
    return response


# ---------------------------------------------------------------- startup
def add_missing_columns():
    """db.create_all() does not add new columns to old tables, so add them here."""
    insp = inspect(db.engine)
    blob = "BYTEA" if db.engine.dialect.name == "postgresql" else "BLOB"
    wanted = {
        "application": {"updated_by": "INTEGER"},
        "document": {"kind": "VARCHAR(20) DEFAULT 'document'",
                     "content_type": "VARCHAR(120) DEFAULT 'application/octet-stream'",
                     "size": "INTEGER DEFAULT 0", "storage": "VARCHAR(20) DEFAULT 'local'",
                     "data": blob},
    }
    with db.engine.begin() as conn:
        for table, cols in wanted.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}'))


with app.app_context():
    db.create_all()
    add_missing_columns()
    # Create the first admin only when the database has no admin yet, and only from
    # ADMIN_USERNAME/ADMIN_PASSWORD. Never fall back to a built-in password on a real database.
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

if __name__ == "__main__":
    app.run(debug=False)
