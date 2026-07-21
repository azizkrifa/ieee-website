"""
recruitment_server.py
----------------------
Standalone Flask backend for recruitment.html ONLY (IEEE FSM Student Branch).

index.html stays a plain static file (GitHub Pages / Netlify, etc.) and is
NOT served or touched by this backend in any way. This server only serves:
  - recruitment.html itself
  - Logos/* (needed for the logo images on that page)
  - its own /api/* and /admin/* routes

What it does:
  - Serves recruitment.html and Logos/* as static files, so you can run this
    and open http://127.0.0.1:5000/recruitment.html
  - POST /api/applications  -> validates + stores an application, books the
    interview slot, enforces the security rules below.
  - GET  /api/slots         -> returns which interview slots are already
    taken, so the front-end can grey them out.
  - GET  /admin/applications -> simple HTTP-Basic-Auth protected list of
    all applications (for the branch team), with a CSV export link.

Security / anti-spam rules:
  1. Max MAX_SUBMISSIONS_PER_IP successful applications per IP address (default 5).
  2. Minimum MIN_SECONDS_BETWEEN_SUBMISSIONS seconds between two attempts from
     the same IP (blocks rapid-fire bot bursts), tracked in-memory.
  3. Honeypot field ("company"): real users never see or fill this input
     (it's hidden by CSS on the front-end). If it's filled, the request is
     silently accepted-looking but dropped -> bots don't learn they were caught.
  4. Full server-side re-validation of every field (never trust the client),
     including the interview date/time against the exact allowed slot list.
  5. One interview slot can only ever be booked once (DB UNIQUE constraint +
     pre-check), so two people can't double-book 10:00 on the 15th.
  6. One application per email / per ID number (no re-applying many times).
  7. Request body size is capped (16 KB) to reject junk/flood payloads.

Run it:
  pip install flask
  python recruitment_server.py
  -> open http://127.0.0.1:5000/recruitment.html

  Put this file next to recruitment.html and the Logos/ folder (index.html
  does not need to be there — it's not served by this app).

Configuration (optional environment variables):
  ADMIN_USER, ADMIN_PASS   -> credentials for /admin/applications (defaults below - CHANGE THEM)
  MAX_SUBMISSIONS_PER_IP   -> default 5
  PORT                     -> default 5000
"""

import csv
import io
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from functools import wraps
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

from flask import Flask, request, jsonify, g, Response, send_from_directory

# ===================== CONFIG =====================

load_dotenv()

# ==================== PATHS ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(BASE_DIR, "applications.db")
)

BLACKLIST_PATH = os.path.join(BASE_DIR, "blacklist.txt")

# ==================== SECURITY ====================
MAX_SUBMISSIONS_PER_IP = int(
    os.getenv("MAX_SUBMISSIONS_PER_IP")
)

MIN_SECONDS_BETWEEN_SUBMISSIONS = int(
    os.getenv("MIN_SECONDS_BETWEEN_SUBMISSIONS")
)

# ==================== ADMIN ====================
ADMIN_USER = os.getenv("ADMIN_USERNAME")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD")

# ==================== INTERVIEW ====================
INTERVIEW_DATES = [
    d.strip()
    for d in os.getenv("INTERVIEW_DATES",).split(",")
]

START_HOUR = int(os.getenv("START_HOUR"))
END_HOUR = int(os.getenv("END_HOUR"))
SLOT_MIN = int(os.getenv("SLOT_MIN"))

# ==================== EMAIL ====================
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT"))

SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

EMAIL_TEMPLATE = os.path.join(BASE_DIR, "E-Mail Template (Interview).html")

def build_allowed_slots():
    slots = []
    h, m = START_HOUR, 0
    while h < END_HOUR:
        slots.append(f"{h:02d}:{m:02d}")
        m += SLOT_MIN
        if m >= 60:
            m -= 60
            h += 1
    return slots


ALLOWED_TIMES = build_allowed_slots()  # ['10:00', '10:15', ..., '18:45']

STUDY_LEVELS = {"L1", "L2", "L3", "M1", "M2", "Engineering cycle", "PhD", "Other"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # 16 KB request body cap

# Werkzeug's built-in per-request console line (e.g. "127.0.0.1 - - [..] ...")
# logs the raw TCP peer address, colorized by status code. Behind a tunnel
# (ngrok / Cloudflare Tunnel) that peer is always localhost, not the real
# visitor -- so we silence it and print our own line using client_ip()
# (defined below), re-using the same color scheme Werkzeug uses.
logging.getLogger("werkzeug").setLevel(logging.ERROR)

try:
    import colorama
    colorama.init(autoreset=True)  # makes ANSI colors work in Windows cmd/PowerShell too
except ImportError:
    pass

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_WHITE = "\033[37m"
_ANSI_CYAN = "\033[36m"
_ANSI_GREEN = "\033[32m"
_ANSI_YELLOW = "\033[33m"
_ANSI_RED = "\033[31m"
_ANSI_MAGENTA = "\033[35m"


def _status_color(status_code):
    code = str(status_code)
    if code =="201":
        return _ANSI_MAGENTA + _ANSI_BOLD
    if code.startswith("1"):
        return _ANSI_BOLD
    if code.startswith("2"):
        return _ANSI_WHITE
    if code == "304":
        return _ANSI_CYAN
    if code.startswith("3"):
        return _ANSI_GREEN
    if code == "404":
        return _ANSI_YELLOW
    if code.startswith("4"):
        return _ANSI_RED + _ANSI_BOLD
    return _ANSI_MAGENTA + _ANSI_BOLD  # 5xx


@app.after_request
def log_with_real_client_ip(response):
    color = _status_color(response.status_code)
    timestamp = datetime.now().strftime("%d/%b/%Y %H:%M:%S")
    print(f'{client_ip()} - [{timestamp}] "{color}{request.method}{color} {request.path}" {color}{response.status_code}{_ANSI_RESET}')
    return response


# in-memory last-attempt timestamps per IP (fine for a single-process app)
_last_attempt_by_ip = {}


# ===================== DB =====================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT NOT NULL,
            study_level TEXT NOT NULL,
            study_field TEXT NOT NULL,
            birthday TEXT NOT NULL,
            id_number TEXT NOT NULL,
            interview_date TEXT NOT NULL,
            interview_time TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(interview_date, interview_time),
            UNIQUE(email),
            UNIQUE(id_number)
        )
    """)
    conn.commit()
    conn.close()


# ===================== VALIDATION HELPERS =====================

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"^[+0-9 ()-]{6,20}$")
ID_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")


def clean_str(value, max_len=200):
    if not isinstance(value, str):
        return ""
    # strip control characters, collapse whitespace, cap length
    value = "".join(ch for ch in value if ch.isprintable() or ch == " ")
    return value.strip()[:max_len]


def validate_payload(data):
    """Returns (errors_dict, cleaned_data). errors_dict is empty if valid."""
    errors = {}

    full_name = clean_str(data.get("fullName"), 100)
    phone = clean_str(data.get("phone"), 30)
    email = clean_str(data.get("email"), 120).lower()
    study_level = clean_str(data.get("studyLevel"), 40)
    study_field = clean_str(data.get("studyField"), 100)
    birthday = clean_str(data.get("birthday"), 20)
    id_number = clean_str(data.get("idNumber"), 20)
    interview_date = clean_str(data.get("interviewDate"), 20)
    interview_time = clean_str(data.get("interviewTime"), 10)

    if len(full_name) < 2:
        errors["fullName"] = "Please enter your full name."
    if not PHONE_RE.match(phone):
        errors["phone"] = "Please enter a valid phone number."
    if not EMAIL_RE.match(email):
        errors["email"] = "Please enter a valid email."
    if study_level not in STUDY_LEVELS:
        errors["studyLevel"] = "Please select a valid study level."
    if len(study_field) < 2:
        errors["studyField"] = "Please enter your field of study."
    try:
        b_date = date.fromisoformat(birthday)
        if b_date >= date.today():
            errors["birthday"] = "Please enter a valid date of birth."
    except (ValueError, TypeError):
        errors["birthday"] = "Please enter a valid date of birth."
    if not ID_RE.match(id_number):
        errors["idNumber"] = "Please enter a valid ID number."
    if interview_date not in INTERVIEW_DATES:
        errors["interview"] = "Please choose a valid interview date and time."
    elif interview_time not in ALLOWED_TIMES:
        errors["interview"] = "Please choose a valid interview date and time."

    cleaned = {
        "full_name": full_name,
        "phone": phone,
        "email": email,
        "study_level": study_level,
        "study_field": study_field,
        "birthday": birthday,
        "id_number": id_number,
        "interview_date": interview_date,
        "interview_time": interview_time,
    }
    return errors, cleaned


def client_ip():
    # Behind a tunnel (ngrok, Cloudflare Tunnel) or reverse proxy, the raw
    # TCP connection Flask sees is always localhost -- the real visitor's IP
    # travels in a header instead. Checked in order of trustworthiness:
    #   - CF-Connecting-IP / True-Client-IP: set by Cloudflare (incl. Cloudflare
    #     Tunnel / cloudflared) directly to the real visitor IP, no parsing needed.
    #   - X-Forwarded-For: set by ngrok and most other proxies; may contain a
    #     comma-separated chain, so take the first (original client) entry.
    #   - falls back to the raw socket address if none of the above are present
    #     (e.g. running with no tunnel/proxy at all, straight on localhost/LAN).
    for header in ("CF-Connecting-IP", "True-Client-IP"):
        value = request.headers.get(header)
        if value:
            return value.strip()
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr


# ===================== BLACKLIST (blacklist.txt) =====================
# One IP per line. '#' starts a comment (whole-line or trailing after the
# IP). Re-read on every check, so an admin can edit it by hand (e.g. to
# manually block someone) without restarting the server.

def load_blacklist():
    ips = set()
    if not os.path.exists(BLACKLIST_PATH):
        return ips
    with open(BLACKLIST_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()  # drop comments
            if line:
                ips.add(line)
    return ips


def is_blacklisted(ip):
    return ip in load_blacklist()


def add_to_blacklist(ip, reason=""):
    if ip in load_blacklist():
        return  # already there, don't add a duplicate line
    timestamp = datetime.now(ZoneInfo("Africa/Tunis")).replace(tzinfo=None).isoformat(timespec="seconds")
    comment = f"  # auto-blocked {timestamp}" + (f" - {reason}" if reason else "")
    with open(BLACKLIST_PATH, "a", encoding="utf-8") as f:
        f.write(f"{ip}{comment}\n")


# ===================== AUTH (for /admin) =====================

def requires_admin_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASS:
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="IEEE FSM Admin"'}
            )
        return f(*args, **kwargs)
    return wrapper


# ===================== ROUTES: STATIC PAGES =====================
# Only recruitment.html and its assets are served here. index.html is a
# separate static site (GitHub Pages / Netlify) and is deliberately not
# part of this backend at all.

@app.route("/")
def root():
    return send_from_directory(BASE_DIR, "recruitment.html")


@app.route("/recruitment.html")
def recruitment_page():
    return send_from_directory(BASE_DIR, "recruitment.html")


@app.route("/Logos/<path:filename>")
def logos(filename):
    return send_from_directory(os.path.join(BASE_DIR, "Logos"), filename)

def send_interview_email(name, recipient, interview_date, interview_time):
    try:

        with open(EMAIL_TEMPLATE, "r", encoding="utf-8") as f:
            html = f.read()

        html = html.replace("{{NAME}}", name)
        html = html.replace("{{DATE}}", interview_date)
        html = html.replace("{{TIME}}", interview_time)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "IEEE FSM SB - Interview Confirmation"
        msg["From"] = SMTP_EMAIL
        msg["To"] = recipient

        msg.attach(MIMEText(html, "html"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)

        server.send_message(msg)

        server.quit()

        print("Confirmation email sent to", recipient)

    except Exception as e:
        print("Email sending failed:", e)


# ===================== ROUTES: API =====================

@app.route("/api/slots", methods=["GET"])
def get_slots():
    db = get_db()
    rows = db.execute(
        "SELECT interview_date, interview_time FROM applications"
    ).fetchall()

    taken = {d: [] for d in INTERVIEW_DATES}
    for row in rows:
        if row["interview_date"] in taken:
            taken[row["interview_date"]].append(row["interview_time"])

    return jsonify({"taken": taken, "allDates": INTERVIEW_DATES, "allTimes": ALLOWED_TIMES})


@app.route("/api/applications", methods=["POST"])
def create_application():
    ip = client_ip()
    now = time.time()

    # ---- blacklist: blocked IPs (manual or auto) can't submit at all ----
    if is_blacklisted(ip):
        return jsonify({
            "error": "You've been blocked from submitting applications. "
                     "If you think this is a mistake, please contact the branch directly."
        }), 403

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Invalid request."}), 400

    # ---- honeypot: bots fill every input, humans never see this one ----
    if clean_str(data.get("company")):
        # look successful to the bot, but don't actually store anything
        return jsonify({"ok": True, "reference": "N/A"}), 201

    # ---- rate limiting: burst throttle ----
    last = _last_attempt_by_ip.get(ip)
    if last is not None and (now - last) < MIN_SECONDS_BETWEEN_SUBMISSIONS:
        return jsonify({"error": "You're submitting too fast. Please wait a few seconds and try again."}), 429
    _last_attempt_by_ip[ip] = now

    # ---- rate limiting: lifetime cap per IP ----
    db = get_db()
    ip_count = db.execute(
        "SELECT COUNT(*) AS c FROM applications WHERE ip_address = ?", (ip,)
    ).fetchone()["c"]
    if ip_count >= MAX_SUBMISSIONS_PER_IP:
        add_to_blacklist(ip, reason=f"reached max of {MAX_SUBMISSIONS_PER_IP} submissions")
        return jsonify({
            "error": f"Maximum of {MAX_SUBMISSIONS_PER_IP} applications reached from this network. "
                     f"If this is a mistake, please contact the branch directly."
        }), 429

    # ---- validation ----
    errors, cleaned = validate_payload(data)
    if errors:
        return jsonify({"error": "Please fix the highlighted fields.", "fields": errors}), 400

    # ---- duplicate person guard ----
    dup = db.execute(
        "SELECT id FROM applications WHERE email = ? OR id_number = ?",
        (cleaned["email"], cleaned["id_number"]),
    ).fetchone()
    if dup:
        return jsonify({"error": "An application with this email or ID number already exists."}), 409

    # ---- slot booking (UNIQUE constraint is the real guarantee against races) ----
    try:
        cur = db.execute(
            """INSERT INTO applications
               (full_name, phone, email, study_level, study_field, birthday, id_number,
                interview_date, interview_time, ip_address, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cleaned["full_name"], cleaned["phone"], cleaned["email"],
                cleaned["study_level"], cleaned["study_field"], cleaned["birthday"],
                cleaned["id_number"], cleaned["interview_date"], cleaned["interview_time"],
                ip, datetime.now(ZoneInfo("Africa/Tunis")).replace(tzinfo=None).isoformat(timespec="seconds"),
            ),
        )
        db.commit()

        send_interview_email(cleaned["full_name"],cleaned["email"],cleaned["interview_date"],cleaned["interview_time"]
)
    except sqlite3.IntegrityError as e:
        db.rollback()
        msg = str(e)
        if "interview_date" in msg or "interview_time" in msg:
            return jsonify({"error": "That interview slot was just taken. Please pick another one."}), 409
        return jsonify({"error": "An application with this email or ID number already exists."}), 409

    return jsonify({"ok": True, "reference": cur.lastrowid}), 201

@app.route("/admin/applications/<int:app_id>", methods=["DELETE"])
@requires_admin_auth
def delete_application(app_id):
    db = get_db()

    cur = db.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    db.commit()

    if cur.rowcount == 0:
        return jsonify({"error": "Application not found"}), 404

    return jsonify({"ok": True})


@app.route("/admin/applications/<int:app_id>", methods=["PUT"])
@requires_admin_auth
def update_application(app_id):
    db = get_db()
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Invalid request."}), 400

    errors, cleaned = validate_payload(data)
    if errors:
        return jsonify({"error": "Please fix the highlighted fields.", "fields": errors}), 400

    existing = db.execute("SELECT id FROM applications WHERE id = ?", (app_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Application not found"}), 404

    # duplicate guard, excluding this record itself
    dup = db.execute(
        "SELECT id FROM applications WHERE (email = ? OR id_number = ?) AND id != ?",
        (cleaned["email"], cleaned["id_number"], app_id),
    ).fetchone()
    if dup:
        return jsonify({"error": "An application with this email or ID number already exists."}), 409

    try:
        db.execute(
            """UPDATE applications SET
                full_name = ?, phone = ?, email = ?, study_level = ?, study_field = ?,
                birthday = ?, id_number = ?, interview_date = ?, interview_time = ?
               WHERE id = ?""",
            (
                cleaned["full_name"], cleaned["phone"], cleaned["email"],
                cleaned["study_level"], cleaned["study_field"], cleaned["birthday"],
                cleaned["id_number"], cleaned["interview_date"], cleaned["interview_time"],
                app_id,
            ),
        )
        db.commit()

        send_interview_email(cleaned["full_name"],cleaned["email"],cleaned["interview_date"],cleaned["interview_time"])

    except sqlite3.IntegrityError as e:
        db.rollback()
        msg = str(e)
        if "interview_date" in msg or "interview_time" in msg:
            return jsonify({"error": "That interview slot is already taken. Please pick another one."}), 409
        return jsonify({"error": "An application with this email or ID number already exists."}), 409

    return jsonify({"ok": True})

# ===================== ADMIN =====================

ADMIN_PAGE_PATH = os.path.join(BASE_DIR, "admin_applications.html")


def _load_admin_page_template():
    with open(ADMIN_PAGE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _fetch_all_applications(db):
    return db.execute(
        "SELECT * FROM applications ORDER BY interview_date, interview_time"
    ).fetchall()


@app.route("/admin/applications")
@requires_admin_auth
def admin_applications():
    db = get_db()
    apps = _fetch_all_applications(db)
    rows_html = "".join(
        f"<tr data-id=\"{a['id']}\" "
        f"data-birthday=\"{a['birthday']}\" "
        f"data-interview=\"{a['interview_date']} {a['interview_time']}\" "
        f"data-submitted=\"{a['created_at']}\" "
        f"onclick=\"handleRowClick(event, '{a['id']}')\">"
        f"<td>{a['id']}</td><td>{a['full_name']}</td><td>{a['phone']}</td>"
        f"<td>{a['email']}</td><td>{a['study_level']}</td><td>{a['study_field']}</td>"
        f"<td>{a['birthday']}</td><td>{a['id_number']}</td>"
        f"<td>{a['interview_date']} {a['interview_time']}</td>"
        f"<td>{a['ip_address']}</td><td>{a['created_at']}</td>"
        f"</tr>"
        for a in apps
    )
    return (
        _load_admin_page_template()
        .replace("__COUNT__", str(len(apps)))
        .replace("__ROWS__", rows_html)
    )

@app.route("/admin/applications/json")
@requires_admin_auth
def admin_applications_json():
    db = get_db()
    apps = _fetch_all_applications(db)

    return jsonify([
        dict(a) for a in apps
    ])

@app.route("/admin/applications.csv")
@requires_admin_auth
def admin_applications_csv():
    db = get_db()
    apps = _fetch_all_applications(db)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "full_name", "phone", "email", "study_level", "study_field",
                      "birthday", "id_number", "interview_date", "interview_time",
                      "ip_address", "created_at"])
    for a in apps:
        writer.writerow([a[k] for k in a.keys()])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=applications.csv"},
    )


# ===================== ENTRY POINT =====================

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT1"))
    print(f" * Applications DB: {DB_PATH}")
    print(f" * Recruitment page: http://127.0.0.1:{port}/recruitment.html")
    print(f" * Admin panel:      http://127.0.0.1:{port}/admin/applications")
    app.run(host="127.0.0.1", port=port, debug=False)