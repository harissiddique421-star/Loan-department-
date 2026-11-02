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
from form_fields import FORM_SECTIONS, FIELD_LABELS

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-only-change-me")

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

app.config.update(
    SQLALCHEMY_DATABASE_URI=dburl,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    MAX_CONTENT_LENGTH=15 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=ON_VERCEL,
    REMEMBER_COOKIE_SECURE=ON_VERCEL,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    WTF_CSRF_TIME_LIMIT=None,
)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
lm = LoginManager(app)
lm.login_view = "login"
lm.login_message = "Pehle login karein."

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
    def photo(self):
        return (Document.query.filter_by(application_id=self.id, kind="photo")
                .order_by(Document.id.desc()).first())


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
    if kind == "photo" and ext not in IMAGE_EXT:
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
    u = db.session.get(User, int(uid))
    return u if u and u.active else None


def admin_required(fn):
    @wraps(fn)
    @login_required
    def inner(*args, **kwargs):
        if current_user.role != "Admin":
            abort(403)
        return fn(*args, **kwargs)
    return inner


def log_action(action, app_id=None):
    db.session.add(Log(user_id=current_user.id, action=action[:200], application_id=app_id))
    db.session.commit()


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
            "FORM_SECTIONS": FORM_SECTIONS, "FIELD_LABELS": FIELD_LABELS}


# ---------------------------------------------------------------- auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        u = User.query.filter_by(username=request.form.get("username", "").strip()).first()
        if u and u.active and u.check_password(request.form.get("password", "")):
            login_user(u)
            return redirect(url_for("dashboard"))
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


@app.route("/application/<int:app_id>/form", methods=["GET", "POST"])
@login_required
def loan_form(app_id):
    a = get_app(app_id)
    form_data = a.data
    if request.method == "POST":
        # Every named field in templates/original_form.html is saved automatically.
        form_data = {k: v.strip() for k, v in request.form.items()
                     if k not in ("csrf_token", "status")}
        full_name = form_data.get("full_name", "")
        cnic = form_data.get("cnic", "")
        if not full_name:
            flash("Applicant ka naam zaroori hai.")
            return render_template("original_form.html", a=a, data=form_data)
        if cnic and Application.query.filter(Application.cnic == cnic, Application.id != a.id).first():
            flash("Is CNIC par doosri application pehle se mojood hai.")
            return render_template("original_form.html", a=a, data=form_data)
        a.applicant_name, a.cnic = full_name, cnic
        new_status = request.form.get("status")
        if new_status in STATUSES:
            a.status = new_status
        a.form_json = json.dumps(form_data, ensure_ascii=False)
        a.updated_by = current_user.id
        a.updated_at = datetime.utcnow()
        photo = request.files.get("applicant_photo")
        if photo and photo.filename:
            _, err = save_file(a, photo, "photo")
            if err:
                flash(err)
        for f in request.files.getlist("documents"):
            if f and f.filename:
                _, err = save_file(a, f, "document")
                if err:
                    flash(f"{f.filename}: {err}")
        db.session.commit()
        log_action("Saved form " + a.app_id, a.id)
        flash("Form save ho gaya.")
        return redirect(url_for("view_application", app_id=a.id))
    return render_template("original_form.html", a=a, data=form_data)


@app.route("/application/<int:app_id>/print")
@login_required
def print_application(app_id):
    a = get_app(app_id)
    docs = Document.query.filter_by(application_id=a.id, kind="document").all()
    log_action("Printed " + a.app_id, a.id)
    return render_template("print.html", a=a, d=a.data, docs=docs, names=usernames(),
                           now=datetime.utcnow())


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
    kind = "photo" if request.form.get("kind") == "photo" else "document"
    files = [f for f in request.files.getlist("file") if f and f.filename]
    if not files:
        flash("File select karein.")
        return redirect(url_for("view_application", app_id=a.id))
    ok = 0
    for f in files:
        _, err = save_file(a, f, kind)
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
    resp.headers["Cache-Control"] = "private, max-age=300"
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
    known = [n for _, fields in FORM_SECTIONS for n, _, _ in fields]
    keys = known + sorted({k for a in apps for k in a.data} - set(known))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Application ID", "Status", "Created", "Updated"] + [FIELD_LABELS.get(k, k) for k in keys])
    for a in apps:
        d = a.data
        w.writerow([a.app_id, a.status, a.created_at, a.updated_at] + [d.get(k, "") for k in keys])
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
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
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
    admin_name = os.getenv("ADMIN_USERNAME", "admin")
    if not User.query.filter_by(username=admin_name).first():
        admin = User(username=admin_name, role="Admin")
        admin.set_password(os.getenv("ADMIN_PASSWORD", "ChangeThisAdminPassword123!"))
        db.session.add(admin)
        db.session.commit()

if __name__ == "__main__":
    app.run(debug=False)
