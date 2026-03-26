import os
import csv
import io
from datetime import datetime, timedelta, timezone

from flask import (
    Flask, render_template, redirect, url_for, request,
    flash, abort, send_file
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix


# -------------------------------------------------
# App Config
# -------------------------------------------------
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-this")

import os

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-this")

database_url = os.environ.get("DATABASE_URL")
if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///ctf.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Safer production cookies
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Enable secure cookies only in production if running behind HTTPS
if os.environ.get("FLASK_ENV") == "production":
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["REMEMBER_COOKIE_SECURE"] = True

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


# -------------------------------------------------
# Models
# -------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

    total_score = db.Column(db.Integer, default=0)
    completed_stages = db.Column(db.Integer, default=0)
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)

    sessions = db.relationship("UserChallengeSession", backref="user", lazy=True, cascade="all, delete-orphan")
    submission_logs = db.relationship("SubmissionLog", backref="user", lazy=True, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Challenge(db.Model):
    __tablename__ = "challenges"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    apk_drive_link = db.Column(db.String(500), nullable=True)
    duration_minutes = db.Column(db.Integer, default=120)
    is_active = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    stages = db.relationship(
        "ChallengeStage",
        backref="challenge",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="ChallengeStage.stage_number"
    )
    sessions = db.relationship("UserChallengeSession", backref="challenge", lazy=True, cascade="all, delete-orphan")


class ChallengeStage(db.Model):
    __tablename__ = "challenge_stages"

    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id"), nullable=False)
    stage_number = db.Column(db.Integer, nullable=False)
    flag = db.Column(db.String(255), nullable=False)
    points = db.Column(db.Integer, default=100)
    hint = db.Column(db.String(255), nullable=True)


class UserChallengeSession(db.Model):
    __tablename__ = "user_challenge_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id"), nullable=False)

    started_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    is_started = db.Column(db.Boolean, default=False)
    is_completed = db.Column(db.Boolean, default=False)

    current_stage = db.Column(db.Integer, default=1)
    score = db.Column(db.Integer, default=0)

    # store solved stages as comma string: "1,2,3"
    solved_stages = db.Column(db.String(100), default="")


class SubmissionLog(db.Model):
    __tablename__ = "submission_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id"), nullable=False)

    stage_number = db.Column(db.Integer, nullable=False)
    submitted_flag = db.Column(db.String(255), nullable=False)
    is_correct = db.Column(db.Boolean, default=False)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    ip_address = db.Column(db.String(100), nullable=True)


# -------------------------------------------------
# Login Manager
# -------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# -------------------------------------------------
# Utility Helpers
# -------------------------------------------------
def get_active_challenge():
    return Challenge.query.filter_by(is_active=True).first()


def get_or_create_session(user, challenge):
    session = UserChallengeSession.query.filter_by(
        user_id=user.id,
        challenge_id=challenge.id
    ).first()

    if not session:
        session = UserChallengeSession(
            user_id=user.id,
            challenge_id=challenge.id,
            current_stage=1,
            solved_stages="",
            score=0
        )
        db.session.add(session)
        db.session.commit()

    return session


def session_time_left(session_obj):
    if not session_obj or not session_obj.is_started or not session_obj.ends_at:
        return None

    now = datetime.utcnow()
    remaining = session_obj.ends_at - now
    if remaining.total_seconds() <= 0:
        return 0
    return int(remaining.total_seconds())


def is_session_expired(session_obj):
    if not session_obj or not session_obj.is_started or not session_obj.ends_at:
        return False
    return datetime.utcnow() > session_obj.ends_at


def parse_solved_stages(solved_stages):
    if not solved_stages:
        return set()
    return set(int(x) for x in solved_stages.split(",") if x.strip().isdigit())


def save_solved_stages(stage_set):
    return ",".join(str(x) for x in sorted(stage_set))


def admin_required():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


# -------------------------------------------------
# Init Admin
# -------------------------------------------------
@app.route("/init-admin")
def init_admin():
    admin = User.query.filter_by(username="admin").first()
    if admin:
        return "Admin already exists. Username: admin"

    admin = User(
        username="admin",
        email="admin@example.com",
        is_admin=True
    )
    admin.set_password("admin123")
    db.session.add(admin)
    db.session.commit()
    return "Admin created successfully. Username: admin | Password: admin123"

@app.route("/admin/export/user/<int:user_id>")
@login_required
def export_single_user_csv(user_id):
    admin_required()

    user = User.query.get_or_404(user_id)
    logs = SubmissionLog.query.filter_by(user_id=user.id).order_by(SubmissionLog.submitted_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Username", "Stage", "Submitted Flag", "Correct", "Submitted At", "IP Address"])

    for log in logs:
        writer.writerow([
            user.username,
            log.stage_number,
            log.submitted_flag,
            log.is_correct,
            log.submitted_at,
            log.ip_address
        ])

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8"))
    mem.seek(0)
    output.close()

    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{user.username}_results.csv"
    )
# -------------------------------------------------
# Auth Routes
# -------------------------------------------------
@app.route("/")
def home():
    active_challenge = get_active_challenge()
    return render_template("home.html", active_challenge=active_challenge)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return redirect(url_for("register"))

        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Username or email already exists.", "danger")
            return redirect(url_for("register"))

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash("Registration successful. Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username_or_email = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter(
            (User.username == username_or_email) | (User.email == username_or_email)
        ).first()

        if user and user.check_password(password):
            login_user(user)
            flash("Login successful.", "success")

            if user.is_admin:
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("dashboard"))

        flash("Invalid credentials.", "danger")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "info")
    return redirect(url_for("home"))


# -------------------------------------------------
# User Routes
# -------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.is_admin:
        return redirect(url_for("admin_dashboard"))

    challenge = get_active_challenge()
    session_obj = None
    solved = set()
    time_left = None
    stages = []

    if challenge:
        session_obj = get_or_create_session(current_user, challenge)
        solved = parse_solved_stages(session_obj.solved_stages)
        time_left = session_time_left(session_obj)
        stages = challenge.stages

        if is_session_expired(session_obj) and not session_obj.is_completed:
            session_obj.is_completed = True
            db.session.commit()
            flash("Your challenge session has expired.", "warning")

    return render_template(
        "dashboard.html",
        challenge=challenge,
        session_obj=session_obj,
        solved=solved,
        time_left=time_left,
        stages=stages
    )


@app.route("/start-challenge", methods=["POST"])
@login_required
def start_challenge():
    if current_user.is_admin:
        abort(403)

    challenge = get_active_challenge()
    if not challenge:
        flash("No active challenge available.", "warning")
        return redirect(url_for("dashboard"))

    session_obj = get_or_create_session(current_user, challenge)

    if session_obj.is_started:
        flash("Challenge already started.", "info")
        return redirect(url_for("dashboard"))

    now = datetime.utcnow()
    session_obj.started_at = now
    session_obj.ends_at = now + timedelta(minutes=challenge.duration_minutes)
    session_obj.is_started = True
    session_obj.is_completed = False
    session_obj.current_stage = 1
    session_obj.score = 0
    session_obj.solved_stages = ""

    current_user.total_score = 0
    current_user.completed_stages = 0

    db.session.commit()
    flash("Challenge started. Timer is now running.", "success")
    return redirect(url_for("dashboard"))


@app.route("/submit-flag/<int:stage_number>", methods=["POST"])
@login_required
def submit_flag(stage_number):
    if current_user.is_admin:
        abort(403)

    challenge = get_active_challenge()
    if not challenge:
        return {
            "success": False,
            "message": "No active challenge found.",
            "status": "danger"
        }, 400

    session_obj = get_or_create_session(current_user, challenge)

    if not session_obj.is_started:
        return {
            "success": False,
            "message": "Start the challenge first.",
            "status": "warning"
        }, 400

    if session_obj.is_completed or is_session_expired(session_obj):
        session_obj.is_completed = True
        db.session.commit()
        return {
            "success": False,
            "message": "Your session is over.",
            "status": "danger",
            "session_completed": True
        }, 400

    submitted_flag = request.form.get("flag", "").strip()
    if not submitted_flag:
        return {
            "success": False,
            "message": f"Stage {stage_number}: Please enter a flag.",
            "status": "warning"
        }, 400

    solved = parse_solved_stages(session_obj.solved_stages)

    stage = ChallengeStage.query.filter_by(
        challenge_id=challenge.id,
        stage_number=stage_number
    ).first()

    if not stage:
        return {
            "success": False,
            "message": "Invalid stage.",
            "status": "danger"
        }, 400

    is_correct = submitted_flag == stage.flag

    log = SubmissionLog(
        user_id=current_user.id,
        challenge_id=challenge.id,
        stage_number=stage_number,
        submitted_flag=submitted_flag,
        is_correct=is_correct,
        ip_address=request.headers.get("X-Forwarded-For", request.remote_addr)
    )
    db.session.add(log)

    if is_correct:
        if stage_number in solved:
            db.session.commit()
            return {
                "success": False,
                "message": f"Stage {stage_number} already solved.",
                "status": "info",
                "already_solved": True,
                "stage_number": stage_number,
                "score": session_obj.score,
                "solved_count": len(solved),
                "next_unsolved_stage": session_obj.current_stage,
                "session_completed": session_obj.is_completed
            }, 200

        solved.add(stage_number)
        session_obj.solved_stages = save_solved_stages(solved)
        session_obj.score += stage.points
        current_user.total_score = session_obj.score
        current_user.completed_stages = len(solved)

        unsolved = [i for i in range(1, 10) if i not in solved]
        session_obj.current_stage = unsolved[0] if unsolved else 9

        completed_all = len(solved) == 9
        if completed_all:
            session_obj.is_completed = True
            message = "Congratulations! You completed all 9 stages."
        else:
            message = f"Correct! Stage {stage_number} solved."

        db.session.commit()

        return {
            "success": True,
            "message": message,
            "status": "success",
            "stage_number": stage_number,
            "score": session_obj.score,
            "solved_count": len(solved),
            "next_unsolved_stage": session_obj.current_stage,
            "session_completed": session_obj.is_completed
        }, 200

    db.session.commit()
    return {
        "success": False,
        "message": f"Wrong flag for Stage {stage_number}. Try again.",
        "status": "danger",
        "stage_number": stage_number,
        "score": session_obj.score,
        "solved_count": len(solved),
        "next_unsolved_stage": session_obj.current_stage,
        "session_completed": session_obj.is_completed
    }, 200

@app.route("/leaderboard")
@login_required
def leaderboard():
    users = User.query.filter_by(is_admin=False).order_by(
        User.total_score.desc(),
        User.completed_stages.desc(),
        User.registered_at.asc()
    ).all()
    return render_template("leaderboard.html", users=users)


@app.route("/my-results")
@login_required
def my_results():
    challenge = get_active_challenge()
    session_obj = None
    logs = []

    if challenge:
        session_obj = UserChallengeSession.query.filter_by(
            user_id=current_user.id,
            challenge_id=challenge.id
        ).first()

        logs = SubmissionLog.query.filter_by(
            user_id=current_user.id,
            challenge_id=challenge.id
        ).order_by(SubmissionLog.submitted_at.desc()).all()

    return render_template("my_results.html", session_obj=session_obj, logs=logs, challenge=challenge)


# -------------------------------------------------
# Admin Routes
# -------------------------------------------------
@app.route("/admin")
@login_required
def admin_dashboard():
    admin_required()

    username_query = request.args.get("username", "").strip()

    active_challenge = get_active_challenge()
    total_users = User.query.filter_by(is_admin=False).count()
    total_logs = SubmissionLog.query.count()
    started_sessions = UserChallengeSession.query.filter_by(is_started=True).count()
    completed_sessions = UserChallengeSession.query.filter_by(is_completed=True).count()

    recent_logs = SubmissionLog.query.order_by(SubmissionLog.submitted_at.desc()).limit(10).all()
    recent_users = User.query.filter_by(is_admin=False).order_by(User.registered_at.desc()).limit(10).all()

    leaderboard_query = User.query.filter_by(is_admin=False)
    if username_query:
        leaderboard_query = leaderboard_query.filter(User.username.ilike(f"%{username_query}%"))

    leaderboard_users = leaderboard_query.order_by(
        User.total_score.desc(),
        User.completed_stages.desc(),
        User.registered_at.asc()
    ).limit(50).all()

    return render_template(
        "admin/dashboard.html",
        active_challenge=active_challenge,
        total_users=total_users,
        total_logs=total_logs,
        started_sessions=started_sessions,
        completed_sessions=completed_sessions,
        recent_logs=recent_logs,
        recent_users=recent_users,
        leaderboard_users=leaderboard_users,
        username_query=username_query
    )

@app.route("/admin/manage-challenge", methods=["GET", "POST"])
@login_required
def manage_challenge():
    admin_required()

    challenge = get_active_challenge()
    if not challenge:
        challenge = Challenge(
            title="Mobile Pentesting CTF",
            description="Default challenge setup",
            duration_minutes=120,
            is_active=True
        )
        db.session.add(challenge)
        db.session.commit()

    if request.method == "POST":
        challenge.title = request.form.get("title", "").strip()
        challenge.description = request.form.get("description", "").strip()
        challenge.apk_drive_link = request.form.get("apk_drive_link", "").strip()
        challenge.duration_minutes = int(request.form.get("duration_minutes", 120))

        # Active state handling
        make_active = request.form.get("is_active")
        if make_active == "on":
            Challenge.query.update({Challenge.is_active: False})
            challenge.is_active = True
        else:
            challenge.is_active = False

        db.session.commit()

        # Update 9 stages
        for i in range(1, 10):
            flag = request.form.get(f"flag_{i}", "").strip()
            points = int(request.form.get(f"points_{i}", 100))
            hint = request.form.get(f"hint_{i}", "").strip()

            stage = ChallengeStage.query.filter_by(
                challenge_id=challenge.id,
                stage_number=i
            ).first()

            if stage:
                stage.flag = flag
                stage.points = points
                stage.hint = hint
            else:
                stage = ChallengeStage(
                    challenge_id=challenge.id,
                    stage_number=i,
                    flag=flag or f"FLAG_STAGE_{i}",
                    points=points,
                    hint=hint
                )
                db.session.add(stage)

        db.session.commit()
        flash("Challenge setup updated successfully.", "success")
        return redirect(url_for("manage_challenge"))

    stages = {stage.stage_number: stage for stage in challenge.stages}
    return render_template("admin/manage_challenge.html", challenge=challenge, stages=stages)


@app.route("/admin/users")
@login_required
def admin_users():
    admin_required()
    users = User.query.filter_by(is_admin=False).order_by(User.registered_at.desc()).all()
    return render_template("admin/users.html", users=users)


@app.route("/admin/logs")
@login_required
def admin_logs():
    admin_required()

    user_filter = request.args.get("user", "").strip()
    stage_filter = request.args.get("stage", "").strip()
    result_filter = request.args.get("result", "").strip()

    query = SubmissionLog.query.join(User, SubmissionLog.user_id == User.id)

    if user_filter:
        query = query.filter(User.username.ilike(f"%{user_filter}%"))

    if stage_filter.isdigit():
        query = query.filter(SubmissionLog.stage_number == int(stage_filter))

    if result_filter == "correct":
        query = query.filter(SubmissionLog.is_correct.is_(True))
    elif result_filter == "wrong":
        query = query.filter(SubmissionLog.is_correct.is_(False))

    logs = query.order_by(SubmissionLog.submitted_at.desc()).all()

    return render_template("admin/submission_logs.html", logs=logs)


@app.route("/admin/reset-session/<int:user_id>", methods=["POST"])
@login_required
def reset_user_session(user_id):
    admin_required()

    user = User.query.get_or_404(user_id)
    challenge = get_active_challenge()

    if not challenge:
        flash("No active challenge found.", "warning")
        return redirect(url_for("admin_users"))

    session_obj = UserChallengeSession.query.filter_by(
        user_id=user.id,
        challenge_id=challenge.id
    ).first()

    if session_obj:
        session_obj.started_at = None
        session_obj.ends_at = None
        session_obj.is_started = False
        session_obj.is_completed = False
        session_obj.current_stage = 1
        session_obj.score = 0
        session_obj.solved_stages = ""

    user.total_score = 0
    user.completed_stages = 0

    # optional: delete old submission logs for active challenge
    SubmissionLog.query.filter_by(
        user_id=user.id,
        challenge_id=challenge.id
    ).delete()

    db.session.commit()
    flash(f"Session reset for user '{user.username}'.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/export/<string:export_type>")
@login_required
def export_csv(export_type):
    admin_required()

    output = io.StringIO()
    writer = csv.writer(output)

    filename = "export.csv"

    if export_type == "users":
        filename = "users.csv"
        writer.writerow(["ID", "Username", "Email", "Score", "Completed Stages", "Registered At"])
        users = User.query.filter_by(is_admin=False).order_by(User.id.asc()).all()
        for u in users:
            writer.writerow([
                u.id, u.username, u.email, u.total_score,
                u.completed_stages, u.registered_at
            ])

    elif export_type == "leaderboard":
        filename = "leaderboard.csv"
        writer.writerow(["Rank", "Username", "Score", "Completed Stages"])
        users = User.query.filter_by(is_admin=False).order_by(
            User.total_score.desc(),
            User.completed_stages.desc(),
            User.registered_at.asc()
        ).all()
        for idx, u in enumerate(users, start=1):
            writer.writerow([idx, u.username, u.total_score, u.completed_stages])

    elif export_type == "logs":
        filename = "submission_logs.csv"
        writer.writerow([
            "Log ID", "Username", "Challenge ID", "Stage",
            "Submitted Flag", "Correct", "Submitted At", "IP Address"
        ])
        logs = SubmissionLog.query.order_by(SubmissionLog.submitted_at.desc()).all()
        for log in logs:
            user = User.query.get(log.user_id)
            writer.writerow([
                log.id,
                user.username if user else "Unknown",
                log.challenge_id,
                log.stage_number,
                log.submitted_flag,
                log.is_correct,
                log.submitted_at,
                log.ip_address
            ])
    else:
        abort(404)

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8"))
    mem.seek(0)
    output.close()

    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename
    )

@app.route("/admin/leaderboard-data")
@login_required
def admin_leaderboard_data():
    admin_required()

    username_query = request.args.get("username", "").strip()

    query = User.query.filter_by(is_admin=False)

    if username_query:
        query = query.filter(User.username.ilike(f"%{username_query}%"))

    leaderboard_users = query.order_by(
        User.total_score.desc(),
        User.completed_stages.desc(),
        User.registered_at.asc()
    ).all()

    data = []
    for idx, user in enumerate(leaderboard_users, start=1):
        data.append({
            "rank": idx,
            "username": user.username,
            "score": user.total_score,
            "completed_stages": user.completed_stages,
            "registered_at": user.registered_at.strftime("%Y-%m-%d %H:%M:%S") if user.registered_at else "-"
        })

    return {"users": data}, 200
# -------------------------------------------------
# CLI / Setup
# -------------------------------------------------
@app.route("/create-db")
def create_db():
    db.create_all()
    return "Database tables created."


# -------------------------------------------------
# Error Handlers
# -------------------------------------------------
@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("500.html"), 500


# -------------------------------------------------
# Main
# -------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True,port=8855)