import os, json, secrets
from datetime import datetime
import sqlite3, io, zipfile
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, abort, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from io import BytesIO

app=Flask(__name__)
app.config["SECRET_KEY"]=os.getenv("SECRET_KEY","dev-only-change-me")
dburl=os.getenv("DATABASE_URL","sqlite:///tooba_dev.db")
if dburl.startswith("postgres://"): dburl=dburl.replace("postgres://","postgresql://",1)
app.config["SQLALCHEMY_DATABASE_URI"]=dburl
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"]=False
app.config["MAX_CONTENT_LENGTH"]=15*1024*1024
app.config["UPLOAD_FOLDER"]=os.getenv("UPLOAD_FOLDER","private_uploads")
os.makedirs(app.config["UPLOAD_FOLDER"],exist_ok=True)
db=SQLAlchemy(app)
lm=LoginManager(app); lm.login_view="login"

class User(UserMixin,db.Model):
    id=db.Column(db.Integer,primary_key=True)
    username=db.Column(db.String(80),unique=True,nullable=False)
    password_hash=db.Column(db.String(255),nullable=False)
    role=db.Column(db.String(20),default="Staff")
    active=db.Column(db.Boolean,default=True)
    def set_password(self,p): self.password_hash=generate_password_hash(p)
    def check_password(self,p): return check_password_hash(self.password_hash,p)

class Application(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    app_id=db.Column(db.String(20),unique=True,index=True)
    applicant_name=db.Column(db.String(200),default="")
    cnic=db.Column(db.String(30),index=True,default="")
    status=db.Column(db.String(30),default="Draft")
    assigned_to=db.Column(db.Integer,db.ForeignKey("user.id"),nullable=True)
    form_json=db.Column(db.Text,default="{}")
    created_by=db.Column(db.Integer,db.ForeignKey("user.id"))
    created_at=db.Column(db.DateTime,default=datetime.utcnow)
    updated_at=db.Column(db.DateTime,default=datetime.utcnow,onupdate=datetime.utcnow)

class Document(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    application_id=db.Column(db.Integer,db.ForeignKey("application.id"),nullable=False)
    original_name=db.Column(db.String(255))
    stored_name=db.Column(db.String(255),unique=True)
    uploaded_by=db.Column(db.Integer,db.ForeignKey("user.id"))
    created_at=db.Column(db.DateTime,default=datetime.utcnow)

class Log(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    user_id=db.Column(db.Integer,db.ForeignKey("user.id"))
    action=db.Column(db.String(200))
    application_id=db.Column(db.Integer,nullable=True)
    created_at=db.Column(db.DateTime,default=datetime.utcnow)

@lm.user_loader
def load_user(uid): return db.session.get(User,int(uid))

def admin_required(fn):
    @wraps(fn)
    @login_required
    def inner(*args,**kwargs):
        if current_user.role!="Admin": abort(403)
        return fn(*args,**kwargs)
    return inner

def log_action(action,app_id=None):
    db.session.add(Log(user_id=current_user.id,action=action,application_id=app_id))
    db.session.commit()

def allowed_app(a):
    return current_user.role=="Admin" or a.created_by==current_user.id or a.assigned_to==current_user.id

@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        u=User.query.filter_by(username=request.form.get("username","")).first()
        if u and u.active and u.check_password(request.form.get("password","")):
            login_user(u); return redirect(url_for("dashboard"))
        flash("Login details ghalat hain ya account disabled hai.")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout(): logout_user(); return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    q=request.args.get("q","").strip()
    query=Application.query
    if current_user.role!="Admin":
        query=query.filter((Application.created_by==current_user.id)|(Application.assigned_to==current_user.id))
    if q:
        query=query.filter((Application.applicant_name.ilike(f"%{q}%"))|(Application.cnic.ilike(f"%{q}%"))|(Application.app_id.ilike(f"%{q}%")))
    apps=query.order_by(Application.created_at.desc()).limit(100).all()
    count=query.count()
    return render_template("dashboard.html",apps=apps,total=count,
        pending=query.filter_by(status="Pending").count(),
        completed=query.filter_by(status="Completed").count(),
        draft=query.filter_by(status="Draft").count(),q=q)

@app.route("/application/new",methods=["GET","POST"])
@login_required
def new_application():
    if request.method=="POST":
        cnic=request.form.get("cnic","").strip()
        if cnic and Application.query.filter_by(cnic=cnic).first():
            flash("Is CNIC par application pehle se mojood hai.")
            return redirect(url_for("new_application"))
        last=Application.query.order_by(Application.id.desc()).first()
        number=(last.id+1) if last else 1
        a=Application(app_id=f"TBF-{number:04d}",applicant_name=request.form.get("name","").strip(),
            cnic=cnic,status=request.form.get("status","Draft"),created_by=current_user.id,
            assigned_to=current_user.id if current_user.role=="Staff" else None,
            form_json=json.dumps({}))
        db.session.add(a); db.session.commit(); log_action("Created "+a.app_id,a.id)
        flash("Application create ho gayi.")
        return redirect(url_for("edit_application",app_id=a.id))
    return render_template("new_application.html")

@app.route("/application/<int:app_id>/edit",methods=["GET","POST"])
@login_required
def edit_application(app_id):
    a=Application.query.get_or_404(app_id)
    if not allowed_app(a): abort(403)
    if request.method=="POST":
        a.applicant_name=request.form.get("name",a.applicant_name).strip()
        a.cnic=request.form.get("cnic",a.cnic).strip()
        a.status=request.form.get("status",a.status)
        db.session.commit(); log_action("Updated "+a.app_id,a.id)
        flash("Record update ho gaya.")
        return redirect(url_for("edit_application",app_id=a.id))
    docs=Document.query.filter_by(application_id=a.id).all()
    return render_template("edit_application.html",a=a,docs=docs,staff=User.query.filter_by(role="Staff",active=True).all())

@app.route("/application/<int:app_id>/form")
@login_required
def original_form(app_id):
    a=Application.query.get_or_404(app_id)
    if not allowed_app(a): abort(403)
    return render_template("original_form.html")

@app.route("/application/<int:app_id>/upload",methods=["POST"])
@login_required
def upload(app_id):
    a=Application.query.get_or_404(app_id)
    if not allowed_app(a): abort(403)
    f=request.files.get("file")
    if not f or not f.filename: flash("File select karein."); return redirect(url_for("edit_application",app_id=a.id))
    ext=os.path.splitext(secure_filename(f.filename))[1].lower()
    if ext not in {".pdf",".jpg",".jpeg",".png",".doc",".docx"}: abort(400)
    stored=secrets.token_hex(16)+ext
    f.save(os.path.join(app.config["UPLOAD_FOLDER"],stored))
    db.session.add(Document(application_id=a.id,original_name=secure_filename(f.filename),stored_name=stored,uploaded_by=current_user.id))
    db.session.commit(); log_action("Uploaded document to "+a.app_id,a.id)
    flash("Document upload ho gaya.")
    return redirect(url_for("edit_application",app_id=a.id))

@app.route("/document/<int:doc_id>")
@login_required
def document(doc_id):
    d=Document.query.get_or_404(doc_id); a=Application.query.get_or_404(d.application_id)
    if not allowed_app(a): abort(403)
    return send_file(os.path.join(app.config["UPLOAD_FOLDER"],d.stored_name),download_name=d.original_name,as_attachment=True)

@app.route("/admin/users",methods=["GET","POST"])
@admin_required
def users():
    if request.method=="POST":
        username=request.form.get("username","").strip()
        password=request.form.get("password","")
        role=request.form.get("role","Staff")
        if role not in ("Admin","Staff") or not username or len(password)<12:
            flash("Username aur kam az kam 12 character password, role Admin/Staff zaroori hai.")
        elif User.query.filter_by(username=username).first():
            flash("Username already exists.")
        else:
            u=User(username=username,role=role); u.set_password(password); db.session.add(u); db.session.commit()
            flash("User add ho gaya.")
    return render_template("users.html",users=User.query.all())

@app.route("/admin/users/<int:uid>/toggle",methods=["POST"])
@admin_required
def toggle_user(uid):
    u=User.query.get_or_404(uid)
    if u.id==current_user.id: flash("Apna account disable nahi kar sakte.")
    else: u.active=not u.active; db.session.commit()
    return redirect(url_for("users"))

@app.route("/admin/export")
@admin_required
def export_csv():
    import csv,io
    output=io.StringIO(); w=csv.writer(output)
    w.writerow(["Application ID","Applicant","CNIC","Status","Created","Assigned user ID"])
    for a in Application.query.order_by(Application.id).all():
        w.writerow([a.app_id,a.applicant_name,a.cnic,a.status,a.created_at,a.assigned_to])
    from flask import Response
    return Response(output.getvalue(),mimetype="text/csv",headers={"Content-Disposition":"attachment; filename=applications.csv"})

@app.route("/admin/reports")
@admin_required
def reports():
    from sqlalchemy import func
    by_status=db.session.query(Application.status,func.count(Application.id)).group_by(Application.status).all()
    by_staff=db.session.query(User.username,func.count(Application.id)).outerjoin(Application,Application.assigned_to==User.id).filter(User.role=="Staff").group_by(User.id).all()
    return render_template("reports.html",by_status=by_status,by_staff=by_staff,total=Application.query.count())

@app.route("/admin/assign/<int:app_id>",methods=["POST"])
@admin_required
def assign_application(app_id):
    a=Application.query.get_or_404(app_id)
    uid=request.form.get("staff_id")
    staff=User.query.filter_by(id=uid,role="Staff",active=True).first() if uid else None
    a.assigned_to=staff.id if staff else None
    db.session.commit()
    log_action("Assigned "+a.app_id,a.id)
    flash("Assignment update ho gayi.")
    return redirect(url_for("edit_application",app_id=a.id))

@app.route("/admin/backup")
@admin_required
def backup_export():
    payload={
      "created_at":datetime.utcnow().isoformat()+"Z",
      "applications":[{"id":a.id,"app_id":a.app_id,"name":a.applicant_name,"cnic":a.cnic,
        "status":a.status,"assigned_to":a.assigned_to,"form_json":a.form_json,
        "created_by":a.created_by,"created_at":a.created_at.isoformat() if a.created_at else None}
        for a in Application.query.all()],
      "users":[{"id":u.id,"username":u.username,"role":u.role,"active":u.active} for u in User.query.all()],
      "documents":[{"id":d.id,"application_id":d.application_id,"original_name":d.original_name,
        "stored_name":d.stored_name,"uploaded_by":d.uploaded_by} for d in Document.query.all()]
    }
    from flask import jsonify
    return jsonify(payload)

@app.errorhandler(403)
def forbidden(e): return "Access denied — aapko is page ki permission nahi.",403

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["X-Frame-Options"]="SAMEORIGIN"
    response.headers["Referrer-Policy"]="strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"]="default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline';"
    return response

with app.app_context():
    db.create_all()
    if not User.query.filter_by(username=os.getenv("ADMIN_USERNAME","admin")).first():
        admin=User(username=os.getenv("ADMIN_USERNAME","admin"),role="Admin")
        admin.set_password(os.getenv("ADMIN_PASSWORD","ChangeThisAdminPassword123!"))
        db.session.add(admin); db.session.commit()

if __name__=="__main__":
    app.run(debug=False)