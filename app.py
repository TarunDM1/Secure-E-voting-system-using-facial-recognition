# app.py
from __future__ import annotations

from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
import os
import logging
import traceback
import base64
import numpy as np
import cv2
import json
from datetime import datetime
from werkzeug.utils import secure_filename
import random
import secrets
import string
from io import BytesIO
from flask import send_file
from openpyxl import Workbook


FACE_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
PROFILE_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")



LIVENESS_MIRRORED = True
from werkzeug.security import generate_password_hash

def make_temp_password(length=10):
    # easy to read, avoids confusing chars
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "VOTE-" + "".join(secrets.choice(alphabet) for _ in range(length))
VOTES_TABLE = "votes_new"

# ---------------- Paths / App ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "voters.db")

# Uploads for candidate logos
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads", "candidates")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB
app.secret_key = "secret123"  # change later

ADMIN_EMAIL = "tarundm@gmail.com"  # <-- put your admin gmail here

# Password hash for a fixed password (example password: Admin@12345)
# Generate your own hash once (steps below) and paste it here.
#  python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('Admin@12345'))"
ADMIN_PASSWORD_HASH = "scrypt:32768:8:1$EF9zZN0hHuOaKxSB$5271c57f2f898385ae9cf4265f43fbb11711d4096697cb02791bf0217ecb4046b6a6b838a260ed599762056c13c710a186d49b7fe3247edf6d0e4edb5439afa0"

# ADMIN_EMAIL = "admin@evote.com"   # CHANGE THIS to your admin email

@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

from functools import wraps
from flask import request, render_template, redirect, session, flash

@app.errorhandler(Exception)
def handle_exception(e):
    # show real error in browser (temporary for debugging)
    return f"<h2>Internal Error</h2><pre>{e}</pre>", 500

# ---------------- Logging ----------------
logging.basicConfig(
    filename=os.path.join(LOG_DIR, "app.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# Save captured frames ONLY during voting/login time (audit)
VOTING_CAPTURES_DIR = os.path.join(DATA_DIR, "voting_captures")
os.makedirs(VOTING_CAPTURES_DIR, exist_ok=True)

# ---------------- Duplicate-face policy ----------------
DUPLICATE_MIN_MARGIN = 0.30       # higher => less false blocks
DUPLICATE_REQUIRE_2_SHOTS = True  # require same duplicate twice before blocking

# ---------------- Votes table selection (handles old schema safely) ----------------
# If your DB already has old votes table (national_id, choice), we will use votes_new automatically.
VOTES_TABLE = "votes"


# ---------------- DB helpers ----------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ===== AUTH TABLES =====
from werkzeug.security import generate_password_hash, check_password_hash

def ensure_auth_tables():
    with db_connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          email TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL,
          created_at TEXT DEFAULT (datetime('now'))
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS voter_users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          email TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL,
          national_id TEXT NOT NULL,
          created_at TEXT DEFAULT (datetime('now'))
        );
        """)
        conn.commit()
        
def ensure_pending_voter_table():
    with db_connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_voters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            national_id TEXT NOT NULL,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING / APPROVED / REJECTED
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """)
        # prevent duplicates
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_nid ON pending_voters(national_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_email ON pending_voters(email)")
        conn.commit()
        
from werkzeug.security import generate_password_hash

def ensure_single_admin():
    # set your initial admin password here (change after first login)
    initial_password = "Admin@12345"

    with db_connect() as conn:
        # delete any other admins (optional but enforces ONE)
        conn.execute("DELETE FROM admin_users WHERE email <> ?", (ADMIN_EMAIL,))

        # ensure the one admin exists
        row = conn.execute("SELECT email FROM admin_users WHERE email = ?", (ADMIN_EMAIL,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO admin_users (email, password_hash) VALUES (?, ?)",
                (ADMIN_EMAIL, generate_password_hash(initial_password))
            )
        conn.commit()
        
# ===== AUTH HELPERS =====
def current_user_role():
    return session.get("role")

def require_role(role: str):
    return session.get("role") == role

def require_admin_login():
    return require_role("admin")

def require_voter_login():
    return require_role("voter")


# ---- Voting table for position/candidate voting ----
VOTES_TABLE = "votes_new"

def ensure_votes_new_table():
    with db_connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS votes_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          national_id TEXT NOT NULL,
          position_id INTEGER NOT NULL,
          candidate_id INTEGER NOT NULL,
          cast_at TEXT DEFAULT (datetime('now')),
          UNIQUE(national_id, position_id)
        );
        """)
        conn.commit()
        

def ensure_schema_and_choose_votes_table():
    """
    Creates positions/candidates tables if missing.
    Chooses VOTES_TABLE:
      - If existing 'votes' matches new schema (national_id, position_id, candidate_id) => use 'votes'
      - If existing 'votes' is old schema (has 'choice') => create/use 'votes_new'
      - If 'votes' doesn't exist => create new schema 'votes' and use it
    """
    global VOTES_TABLE

    with db_connect() as conn:
        cur = conn.cursor()

        # Create positions + candidates
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL UNIQUE,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS candidates (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              position_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              party TEXT,
              logo_path TEXT,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT DEFAULT (datetime('now')),
              FOREIGN KEY(position_id) REFERENCES positions(id) ON DELETE CASCADE
            );
            """
        )
        
        
        # Detect votes table schema
        def table_exists(name: str) -> bool:
            r = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            return r is not None

        def columns_of(name: str) -> set[str]:
            cols = cur.execute(f"PRAGMA table_info({name});").fetchall()
            return {c["name"] for c in cols}

        if not table_exists("votes"):
            # create new votes schema in 'votes'
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS votes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  national_id TEXT NOT NULL,
                  position_id INTEGER NOT NULL,
                  candidate_id INTEGER NOT NULL,
                  cast_at TEXT DEFAULT (datetime('now')),
                  UNIQUE(national_id, position_id),
                  FOREIGN KEY(position_id) REFERENCES positions(id) ON DELETE CASCADE,
                  FOREIGN KEY(candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
                );
                """
            )
            VOTES_TABLE = "votes"
        else:
            cols = columns_of("votes")
            # old schema likely has "choice"
            if "choice" in cols and ("position_id" not in cols or "candidate_id" not in cols):
                # create votes_new with correct schema and use it
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS votes_new (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      national_id TEXT NOT NULL,
                      position_id INTEGER NOT NULL,
                      candidate_id INTEGER NOT NULL,
                      cast_at TEXT DEFAULT (datetime('now')),
                      UNIQUE(national_id, position_id),
                      FOREIGN KEY(position_id) REFERENCES positions(id) ON DELETE CASCADE,
                      FOREIGN KEY(candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
                    );
                    """
                )
                VOTES_TABLE = "votes_new"
            else:
                # assume votes is already new schema
                # ensure constraints/columns exist (safe no-op if already correct)
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS votes (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      national_id TEXT NOT NULL,
                      position_id INTEGER NOT NULL,
                      candidate_id INTEGER NOT NULL,
                      cast_at TEXT DEFAULT (datetime('now')),
                      UNIQUE(national_id, position_id),
                      FOREIGN KEY(position_id) REFERENCES positions(id) ON DELETE CASCADE,
                      FOREIGN KEY(candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
                    );
                    """
                )
                VOTES_TABLE = "votes"

        conn.commit()
    logging.info("Using votes table: %s", VOTES_TABLE)


def get_voter(national_id: str):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM voters WHERE national_id = ?",
            (national_id,),
        ).fetchone()


def save_template(national_id: str, method: str, blob: bytes):
    with db_connect() as conn:
        conn.execute(
            "UPDATE voters SET template_method=?, template_blob=? WHERE national_id=?",
            (method, blob, national_id),
        )
        conn.commit()
        
def find_voted_face_match(face_norm, exclude_national_id=None):
    """
    If face_norm matches any voter who already voted (has_voted=1),
    return (national_id, full_name). Else return (None, None).
    """
    from face.matcher import distance, is_match

    with db_connect() as conn:
        rows = conn.execute("""
            SELECT national_id, full_name, template_method, template_blob
            FROM voters
            WHERE has_voted = 1
              AND template_method IS NOT NULL
              AND template_blob IS NOT NULL
        """).fetchall()

    for r in rows:
        nid = r["national_id"]
        if exclude_national_id and nid == exclude_national_id:
            continue

        dist = distance(face_norm, r["template_method"], r["template_blob"])
        if is_match(dist, r["template_method"]):
            return nid, r["full_name"]

    return None, None
        
# ---------------- Positions/Candidates helpers ----------------
def get_positions(active_only=True):
    with db_connect() as conn:
        if active_only:
            return conn.execute(
                "SELECT * FROM positions WHERE is_active=1 ORDER BY id DESC"
            ).fetchall()
        return conn.execute(
            "SELECT * FROM positions ORDER BY id DESC"
        ).fetchall()


def add_position(title: str):
    with db_connect() as conn:
        conn.execute("INSERT INTO positions (title) VALUES (?)", (title,))
        conn.commit()


def delete_position(pos_id: int):
    with db_connect() as conn:
        conn.execute("DELETE FROM positions WHERE id=?", (pos_id,))
        conn.commit()


def get_candidates(position_id=None, active_only=True):
    q = """
    SELECT c.*, p.title as position_title
    FROM candidates c
    JOIN positions p ON p.id = c.position_id
    """
    cond = []
    params = []

    if active_only:
        cond.append("c.is_active=1 AND p.is_active=1")
    if position_id is not None:
        cond.append("c.position_id=?")
        params.append(position_id)

    if cond:
        q += " WHERE " + " AND ".join(cond)

    q += " ORDER BY c.id DESC"

    with db_connect() as conn:
        return conn.execute(q, params).fetchall()


def add_candidate(position_id: int, name: str, party: str, logo_path: str | None):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO candidates (position_id, name, party, logo_path) VALUES (?,?,?,?)",
            (position_id, name, party, logo_path),
        )
        conn.commit()


def delete_candidate(cand_id: int):
    with db_connect() as conn:
        conn.execute("DELETE FROM candidates WHERE id=?", (cand_id,))
        conn.commit()


# ---------------- Voting (per position) ----------------
def mark_vote(national_id: str, position_id: int, candidate_id: int):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")

        voter = cur.execute(
            "SELECT eligible FROM voters WHERE national_id=?",
            (national_id,),
        ).fetchone()

        if not voter:
            conn.rollback()
            raise ValueError("Voter not found")
        if voter["eligible"] != 1:
            conn.rollback()
            raise ValueError("Not eligible")

        ok = cur.execute(
            "SELECT 1 FROM candidates WHERE id=? AND position_id=? AND is_active=1",
            (candidate_id, position_id),
        ).fetchone()
        if not ok:
            conn.rollback()
            raise ValueError("Invalid candidate for this position")

        # insert vote (UNIQUE prevents multiple)
        try:
            cur.execute(
                "INSERT INTO votes_new (national_id, position_id, candidate_id) VALUES (?,?,?)",
                (national_id, position_id, candidate_id),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            raise ValueError("Already voted for this position")

        # mark has_voted if ALL active positions are voted
        total_positions = cur.execute(
            "SELECT COUNT(*) AS c FROM positions WHERE is_active=1"
        ).fetchone()["c"]

        voted_positions = cur.execute(
            f"SELECT COUNT(DISTINCT position_id) AS c FROM {VOTES_TABLE} WHERE national_id=?",
            (national_id,),
        ).fetchone()["c"]

        if total_positions > 0 and voted_positions >= total_positions:
            cur.execute("UPDATE voters SET has_voted=1 WHERE national_id=?", (national_id,))

        conn.commit()


# ---------------- One face -> One national_id helpers ----------------
def iter_voters_with_templates(exclude_national_id=None):
    with db_connect() as conn:
        if exclude_national_id:
            return conn.execute(
                """
                SELECT national_id, full_name, template_method, template_blob
                FROM voters
                WHERE template_method IS NOT NULL AND template_blob IS NOT NULL
                  AND national_id <> ?
                """,
                (exclude_national_id,),
            ).fetchall()
        return conn.execute(
            """
            SELECT national_id, full_name, template_method, template_blob
            FROM voters
            WHERE template_method IS NOT NULL AND template_blob IS NOT NULL
            """
        ).fetchall()


def face_already_registered(face_norm, exclude_national_id: str):
    """
    Strict duplicate check:
      1) Must pass normal is_match()
      2) Must have margin vs 2nd-best >= DUPLICATE_MIN_MARGIN
    """
    from face.matcher import distance, is_match

    rows = iter_voters_with_templates(exclude_national_id=exclude_national_id)
    if not rows:
        return None, None

    scored = []
    for v in rows:
        d = distance(face_norm, v["template_method"], v["template_blob"])
        scored.append((d, v))

    scored.sort(key=lambda x: x[0])
    best_dist, best_voter = scored[0]
    second_dist = scored[1][0] if len(scored) > 1 else None

    if not is_match(best_dist, best_voter["template_method"]):
        return None, best_dist

    if second_dist is not None:
        margin = second_dist - best_dist
        if margin < DUPLICATE_MIN_MARGIN:
            return None, best_dist

    return best_voter, best_dist


# ---------------- Liveness helpers (OpenCV only) ----------------
def decode_frames(frames_json: str):
    """
    frames_json = JSON list of data URLs (base64 frames)
    returns list of BGR frames
    """
    if not frames_json:
        return []

    frames_list = json.loads(frames_json)
    if not isinstance(frames_list, list):
        return []

    out = []
    for data_url in frames_list:
        if not isinstance(data_url, str) or "," not in data_url:
            continue
        _, b64 = data_url.split(",", 1)
        try:
            img_bytes = base64.b64decode(b64)
            arr = np.frombuffer(img_bytes, np.uint8)
            f = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if f is not None:
                out.append(f)
        except Exception:
            continue
    return out



def _detect_largest_face_center_x(gray):
    """
    Returns center_x of the largest detected face rectangle in a grayscale frame.
    Uses frontal face first; falls back to profile face (both directions).
    """
    # Preprocess to improve Haar stability
    gray = cv2.equalizeHist(gray)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    # 1) Frontal face
    faces = FACE_CASCADE.detectMultiScale(
        gray,
        scaleFactor=1.10,
        minNeighbors=3,
        flags=cv2.CASCADE_SCALE_IMAGE,
        minSize=(60, 60),
    )
    if len(faces) > 0:
        x, y, w, h = max(faces, key=lambda r: r[2] * r[3])
        return x + w / 2.0

    # 2) Profile face (right-facing)
    prof = PROFILE_CASCADE.detectMultiScale(
        gray,
        scaleFactor=1.10,
        minNeighbors=3,
        flags=cv2.CASCADE_SCALE_IMAGE,
        minSize=(50, 50),
    )
    if len(prof) > 0:
        x, y, w, h = max(prof, key=lambda r: r[2] * r[3])
        return x + w / 2.0

    # 3) Profile face (left-facing) -> flip frame and detect again
    gray_flip = cv2.flip(gray, 1)
    prof2 = PROFILE_CASCADE.detectMultiScale(
        gray_flip,
        scaleFactor=1.10,
        minNeighbors=3,
        flags=cv2.CASCADE_SCALE_IMAGE,
        minSize=(50, 50),
    )
    if len(prof2) > 0:
        x, y, w, h = max(prof2, key=lambda r: r[2] * r[3])
        # Convert x from flipped coordinates back to original
        width = gray.shape[1]
        center_x_flip = x + w / 2.0
        center_x = width - center_x_flip
        return center_x

    return None


def face_center_x_series(frames_bgr):
    xs = []
    for f in frames_bgr:
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        cx = _detect_largest_face_center_x(g)
        if cx is None:
            continue
        xs.append(float(cx))
    return xs


LIVENESS_MIRRORED = False  # set True only if RIGHT/LEFT are always swapped

def passes_turn(frames_bgr, direction: str) -> bool:
    # optional swap (normally keep False now)
    if LIVENESS_MIRRORED:
        direction = "LEFT" if direction == "RIGHT" else "RIGHT"

    xs = face_center_x_series(frames_bgr)
    print("LIVENESS dir:", direction, "frames:", len(frames_bgr), "detections:", len(xs))

    MIN_DETECTIONS = 8
    if len(xs) < MIN_DETECTIONS:
        print("LIVENESS fail: not enough detections")
        return False

    k = min(4, max(2, len(xs)//3))   # more stable with 18 frames
    start = float(np.median(xs[:k]))
    end   = float(np.median(xs[-k:]))
    delta = end - start

    print("LIVENESS start:", start, "end:", end)
    print("LIVENESS delta:", delta)
    
    rng = float(np.max(xs) - np.min(xs))
    print("LIVENESS range:", rng)

    if rng < 6.0:
        print("LIVENESS fail: overall movement too small")
        return False


    THRESH = 2.0

    if direction == "LEFT":
        ok = (delta <= -THRESH)
        print("LIVENESS check LEFT ok?", ok)
        return ok

    if direction == "RIGHT":
        ok = (delta >= THRESH)
        print("LIVENESS check RIGHT ok?", ok)
        return ok

    return False

def check_motion_liveness(frames_bgr) -> bool:
    """
    Simple motion check: mean abs diff in face ROI between first and last frames.
    Helps reject totally static photos.
    """
    if len(frames_bgr) < 2:
        return False

    f1 = frames_bgr[0]
    f2 = frames_bgr[-1]

    g1 = cv2.cvtColor(f1, cv2.COLOR_BGR2GRAY)
    g2 = cv2.cvtColor(f2, cv2.COLOR_BGR2GRAY)

    face_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )

    faces = face_cascade.detectMultiScale(g1, scaleFactor=1.2, minNeighbors=5, minSize=(80, 80))
    if len(faces) == 0:
        return False

    x, y, w, h = max(faces, key=lambda r: r[2] * r[3])

    r1 = g1[y:y + h, x:x + w]
    r2 = g2[y:y + h, x:x + w]
    if r1.size == 0 or r2.size == 0:
        return False

    r2 = cv2.resize(r2, (r1.shape[1], r1.shape[0]))
    diff = cv2.absdiff(r1, r2)
    motion_score = float(np.mean(diff))

    return motion_score >= 4.0  # tune if needed


# ---------------- Admin auth ----------------
def require_admin_key():
    return request.args.get("key") == "admin123"

# ---------------- Routes ----------------
@app.route("/admin", methods=["GET"])
def admin():

    if not require_admin_login():
        flash("Admin login required.", "danger")
        return redirect(url_for("auth_login"))

    # existing code continues below
    # Always define these (prevents Undefined in template)
    charts = []
    recent = []
    total_votes = 0
    total_voters = 0
    voters_voted = 0
    positions_count = 0
    candidates_count = 0

    try:
        with db_connect() as conn:
            total_voters = conn.execute("SELECT COUNT(*) AS c FROM voters").fetchone()["c"]
            voters_voted = conn.execute("SELECT COUNT(*) AS c FROM voters WHERE has_voted=1").fetchone()["c"]
            positions_count = conn.execute("SELECT COUNT(*) AS c FROM positions").fetchone()["c"]
            candidates_count = conn.execute("SELECT COUNT(*) AS c FROM candidates").fetchone()["c"]
            total_votes = conn.execute(f"SELECT COUNT(*) AS c FROM {VOTES_TABLE}").fetchone()["c"]

            recent = conn.execute(
                f"""
                SELECT v.full_name, vt.national_id, vt.cast_at,
                       p.title AS position_title,
                       c.name  AS candidate_name
                FROM {VOTES_TABLE} vt
                LEFT JOIN voters v     ON v.national_id = vt.national_id
                LEFT JOIN positions p  ON p.id = vt.position_id
                LEFT JOIN candidates c ON c.id = vt.candidate_id
                ORDER BY vt.cast_at DESC
                LIMIT 200
                """
            ).fetchall()

            positions = conn.execute(
                "SELECT id, title FROM positions WHERE is_active=1 ORDER BY id ASC"
            ).fetchall()

            for p in positions:
                rows = conn.execute(
                    f"""
                    SELECT c.name AS label, COUNT(*) AS value
                    FROM {VOTES_TABLE} vt
                    JOIN candidates c ON c.id = vt.candidate_id
                    WHERE vt.position_id = ?
                    GROUP BY vt.candidate_id
                    ORDER BY value DESC
                    """,
                    (p["id"],),
                ).fetchall()

                charts.append({
                    "position_id": int(p["id"]),
                    "position_title": p["title"],
                    "labels": [r["label"] for r in rows],
                    "values": [int(r["value"]) for r in rows]
                })

    except Exception as e:
        logging.exception("Admin dashboard error: %s", e)
        flash(f"Admin dashboard error: {e}", "danger")


     # Hard guarantee charts is JSON-safe
    if charts is None:
        charts = []
    else:
        fixed = []
    for ch in charts:
        fixed.append({
            "position_id": int(ch.get("position_id", 0)) if isinstance(ch, dict) else 0,
            "position_title": (ch.get("position_title") if isinstance(ch, dict) else "") or "",
            "labels": list((ch.get("labels") if isinstance(ch, dict) else []) or []),
            "values": [int(x) for x in list((ch.get("values") if isinstance(ch, dict) else []) or [])]
        })
    charts = fixed
    print("ADMIN charts type:", type(charts), "len:", len(charts))
    return render_template(
        "admin.html",
        title="Admin Dashboard",
        total_votes=total_votes,
        total_voters=total_voters,
        voters_voted=voters_voted,
        positions_count=positions_count,
        candidates_count=candidates_count,
        votes=recent,
        charts=charts,  # ✅ ALWAYS passed
    )

@app.route("/login", methods=["POST"])
def login():
    """
    Flow:
      1) National ID -> fetch voter
      2) 2-step head-turn liveness (frames_step1, frames_step2)
      3) Decode image_b64 -> face_norm
      4) If no template -> enroll now (optionally block duplicates)
         else -> match face
      5) After face verified -> check has_voted
    """
    seq = session.get("challenge_seq")
    if not seq or len(seq) != 2:
        flash("Challenge expired. Refresh page.", "danger")
        return redirect(url_for("home"))

    national_id = request.form.get("national_id", "").strip()
    if not national_id:
        flash("Enter National ID.", "warning")
        return redirect(url_for("home"))

    voter = get_voter(national_id)
    if not voter:
        flash("National ID not found.", "danger")
        return redirect(url_for("home"))

    if voter["eligible"] != 1:
        flash("Not eligible to vote.", "danger")
        return redirect(url_for("home"))

    # -------- LIVENESS (2-step) --------
    try:
        frames_step1 = request.form.get("frames_step1", "")
        frames_step2 = request.form.get("frames_step2", "")

        b1 = decode_frames(frames_step1)
        b2 = decode_frames(frames_step2)

        if len(b1) < 4 or len(b2) < 4:
            flash("Not enough liveness frames. Try again.", "danger")
            return redirect(url_for("home"))

        if not passes_turn(b1, seq[0]) or not passes_turn(b2, seq[1]):
            flash("Liveness failed. Follow both steps exactly and try again.", "danger")
            return redirect(url_for("home"))

        if not check_motion_liveness(b1) and not check_motion_liveness(b2):
            flash("Liveness failed: please move naturally and try again.", "danger")
            return redirect(url_for("home"))

    except Exception as e:
        logging.exception("Liveness error: %s", e)
        flash("Liveness error. Please try again.", "danger")
        return redirect(url_for("home"))

    # -------- FACE IMAGE (image_b64) --------
    try:
        from face.preprocess import to_gray, detect_face, normalize_face

        image_b64 = request.form.get("image_b64", "")
        if not image_b64 or "," not in image_b64:
            flash("Please capture a face frame first.", "warning")
            return redirect(url_for("home"))

        _, b64data = image_b64.split(",", 1)
        img_bytes = base64.b64decode(b64data)
        nparr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is None:
            flash("Invalid image received from browser.", "danger")
            return redirect(url_for("home"))

        # Audit save
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        audit_path = os.path.join(VOTING_CAPTURES_DIR, f"{national_id}_{ts}.jpg")
        cv2.imwrite(audit_path, frame)

        gray = to_gray(frame)
        face = detect_face(gray)
        face_norm = normalize_face(face)

    except Exception as e:
        logging.exception("Browser image/face error for %s: %s", national_id, e)
        flash(f"Camera/face error: {e}", "danger")
        return redirect(url_for("home"))

    # -------- Enrollment or matching --------
    if voter["template_blob"] is None or voter["template_method"] is None:
        try:
            from face.matcher import build_template

            matched, _best_dist = face_already_registered(face_norm, exclude_national_id=national_id)

            if not matched:
                session.pop("dup_check_id", None)

            if matched:
                prev = session.get("dup_check_id")
                if DUPLICATE_REQUIRE_2_SHOTS and prev != matched["national_id"]:
                    session["dup_check_id"] = matched["national_id"]
                    flash("Possible duplicate detected. Please authenticate again to confirm.", "warning")
                    return redirect(url_for("home"))

                session.pop("dup_check_id", None)
                flash(
                    f"This face is already registered with National ID {matched['national_id']} "
                    f"({matched['full_name']}). Cannot use same face for multiple IDs.",
                    "danger",
                )
                return redirect(url_for("home"))

            method, blob = build_template(face_norm)
            save_template(national_id, method, blob)
            flash("Face enrolled successfully.", "success")

        except Exception as e:
            logging.exception("Template save error for %s: %s", national_id, e)
            flash(f"Template save error: {e}", "danger")
            return redirect(url_for("home"))
    else:
        try:
            from face.matcher import distance, is_match

            dist = distance(face_norm, voter["template_method"], voter["template_blob"])
            if not is_match(dist, voter["template_method"]):
                flash("Face does not match this National ID. Access denied.", "danger")
                return redirect(url_for("home"))
        except Exception as e:
            logging.exception("Face match error for %s: %s", national_id, e)
            flash(f"Face match error: {e}", "danger")
            return redirect(url_for("home"))
        
        
        # ---- FACE-BASED DOUBLE VOTE CHECK (ADD HERE) ----
    voted_nid, voted_name = find_voted_face_match(face_norm, exclude_national_id=national_id)
    if voted_nid is not None:
        flash(f"Face recognized as {voted_name} (ID {voted_nid}) — already voted.", "danger")
        return redirect(url_for("home"))
    # ---- END FACE CHECK ----

    # ---- Check voted AFTER face verified ----
    if voter["has_voted"] == 1:
        flash("Face verified, but you have already voted.", "warning")
        return redirect(url_for("home"))



    # ---- Check voted AFTER face verified ----
    if voter["has_voted"] == 1:
        flash("Face verified, but you have already voted.", "warning")
        return redirect(url_for("home"))

    session["nid"] = national_id
    session["name"] = voter["full_name"]
    flash("Face verified. You may vote now.", "success")
    return redirect(url_for("vote_page"))
    

@app.route("/vote", methods=["GET"])
def vote_page():
    if "nid" not in session:
        flash("Please login first.", "warning")
        return redirect(url_for("home"))

    voter = get_voter(session["nid"])
    if not voter:
        session.clear()
        flash("Session invalid. Please login again.", "danger")
        return redirect(url_for("home"))

    # get all active positions
    positions = get_positions(active_only=True)

    if not positions:
        flash("No active positions configured. Admin must add positions and candidates.", "warning")
        return redirect(url_for("home"))

    # find first position not yet voted by this voter
    with db_connect() as conn:
        voted = conn.execute(
            f"SELECT position_id FROM {VOTES_TABLE} WHERE national_id=?",
            (session["nid"],),
        ).fetchall()
    voted_set = {r["position_id"] for r in voted}

    next_pos = None
    for p in positions:
        if p["id"] not in voted_set:
            next_pos = p
            break

    if not next_pos:
        session.clear()
        return render_template("result.html", ok=True, message="You have completed voting. Thank you!")

    candidates = get_candidates(position_id=next_pos["id"], active_only=True)
    if not candidates:
        flash("No active candidates for this position. Admin must add candidates.", "warning")
        return redirect(url_for("home"))

    return render_template(
        "vote_positions.html",
        full_name=session.get("name", ""),
        position=next_pos,
        candidates=candidates,
    )


@app.route("/vote", methods=["POST"])
def submit_vote():
    if "nid" not in session:
        flash("Please login first.", "warning")
        return redirect(url_for("home"))

    try:
        position_id = int(request.form.get("position_id"))
        candidate_id = int(request.form.get("candidate_id"))
    except Exception:
        flash("Invalid vote submission.", "danger")
        return redirect(url_for("vote_page"))

    try:
        mark_vote(session["nid"], position_id, candidate_id)
        logging.info("Vote cast: %s position=%s candidate=%s", session["nid"], position_id, candidate_id)
        flash("Vote recorded. Proceed to next position.", "success")
        return redirect(url_for("vote_page"))
    except Exception as e:
        flash(str(e), "danger")
        return redirect(url_for("vote_page"))


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("auth_login"))


# ---------------- Admin: Positions CRUD ----------------
@app.route("/admin/positions", methods=["GET", "POST"])
def admin_positions():
    if not require_admin_key():
        return "Unauthorized", 401

    if request.method == "POST":
        action = request.form.get("action", "add")

        if action == "add":
            title = request.form.get("title", "").strip()
            if not title:
                flash("Enter position title.", "warning")
            else:
                try:
                    add_position(title)
                    flash("Position added.", "success")
                except Exception as e:
                    flash(f"Error: {e}", "danger")

        elif action == "edit":
            try:
                pos_id = int(request.form.get("id"))
                title = request.form.get("title", "").strip()
                if not title:
                    flash("Title cannot be empty.", "warning")
                else:
                    with db_connect() as conn:
                        conn.execute("UPDATE positions SET title=? WHERE id=?", (title, pos_id))
                        conn.commit()
                    flash("Position updated.", "success")
            except Exception as e:
                flash(f"Error: {e}", "danger")

        elif action == "toggle":
            try:
                pos_id = int(request.form.get("id"))
                with db_connect() as conn:
                    row = conn.execute("SELECT is_active FROM positions WHERE id=?", (pos_id,)).fetchone()
                    if row:
                        new_val = 0 if row["is_active"] == 1 else 1
                        conn.execute("UPDATE positions SET is_active=? WHERE id=?", (new_val, pos_id))
                        conn.commit()
                flash("Position status updated.", "info")
            except Exception as e:
                flash(f"Error: {e}", "danger")

        elif action == "delete":
            try:
                pos_id = int(request.form.get("id"))
                delete_position(pos_id)
                flash("Position deleted.", "info")
            except Exception as e:
                flash(f"Error: {e}", "danger")

        return redirect(url_for("admin_positions", key="admin123"))

    positions = get_positions(active_only=False)
    return render_template("admin_positions.html", positions=positions, title="Positions")


# ---------------- Admin: Candidates CRUD + Logo upload ----------------
@app.route("/admin/candidates", methods=["GET", "POST"])
def admin_candidates():
    if not require_admin_key():
        return "Unauthorized", 401

    if request.method == "POST":
        
    # ---------- TOGGLE candidate active/inactive ----------
        if request.form.get("action") == "toggle":
            cand_id = int(request.form.get("id"))
            with db_connect() as conn:
                row = conn.execute("SELECT is_active FROM candidates WHERE id=?", (cand_id,)).fetchone()
                if not row:
                    flash("Candidate not found.", "danger")
                    return redirect(url_for("admin_candidates", key="admin123"))

                new_val = 0 if row["is_active"] == 1 else 1
                conn.execute("UPDATE candidates SET is_active=? WHERE id=?", (new_val, cand_id))
                conn.commit()

            flash("Candidate status updated.", "info")
            return redirect(url_for("admin_candidates", key="admin123"))


        # ---------- DELETE candidate ----------
        if request.form.get("action") == "delete":
            cand_id = int(request.form.get("id"))
            delete_candidate(cand_id)
            flash("Candidate deleted.", "info")
            return redirect(url_for("admin_candidates", key="admin123"))


        # ---------- EDIT candidate ----------
        if request.form.get("action") == "edit":
            cand_id = int(request.form.get("id"))
            position_id = int(request.form.get("position_id"))
            name = request.form.get("name", "").strip()
            party = request.form.get("party", "").strip()

            if not name:
                flash("Candidate name required.", "warning")
                return redirect(url_for("admin_candidates", key="admin123"))

            logo_path = None
            file = request.files.get("logo")
            if file and file.filename:
                if not allowed_file(file.filename):
                    flash("Logo must be png/jpg/jpeg/webp.", "danger")
                    return redirect(url_for("admin_candidates", key="admin123"))

                fname = secure_filename(file.filename)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = f"{stamp}_{fname}"
                save_path = os.path.join(UPLOAD_DIR, fname)
                file.save(save_path)
                logo_path = f"uploads/candidates/{fname}"

            with db_connect() as conn:
                if logo_path:
                    conn.execute("""
                        UPDATE candidates
                        SET position_id=?, name=?, party=?, logo_path=?
                        WHERE id=?
                    """, (position_id, name, party, logo_path, cand_id))
                else:
                    conn.execute("""
                        UPDATE candidates
                        SET position_id=?, name=?, party=?
                        WHERE id=?
                    """, (position_id, name, party, cand_id))
                conn.commit()

            flash("Candidate updated.", "success")
            return redirect(url_for("admin_candidates", key="admin123"))
        action = request.form.get("action", "add")

        if action == "add":
            try:
                
                flash("Candidate added.", "success")

            except Exception as e:
                flash(f"Error: {e}", "danger")

            return redirect(url_for("admin_candidates", key="admin123"))

        if action == "edit":
            try:
                cand_id = int(request.form.get("id"))
                position_id = int(request.form.get("position_id"))
                name = request.form.get("name", "").strip()
                party = request.form.get("party", "").strip()

                if not name:
                    flash("Candidate name required.", "warning")
                    return redirect(url_for("admin_candidates", key="admin123"))

                file = request.files.get("logo")
                logo_path = None
                if file and file.filename:
                    if not allowed_file(file.filename):
                        flash("Logo must be png/jpg/jpeg/webp.", "danger")
                        return redirect(url_for("admin_candidates", key="admin123"))

                    fname = secure_filename(file.filename)
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    fname = f"{stamp}_{fname}"
                    save_path = os.path.join(UPLOAD_DIR, fname)
                    file.save(save_path)
                    logo_path = f"uploads/candidates/{fname}"

                with db_connect() as conn:
                    if logo_path:
                        conn.execute(
                            "UPDATE candidates SET position_id=?, name=?, party=?, logo_path=? WHERE id=?",
                            (position_id, name, party, logo_path, cand_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE candidates SET position_id=?, name=?, party=? WHERE id=?",
                            (position_id, name, party, cand_id),
                        )
                    conn.commit()

                flash("Candidate updated.", "success")
            except Exception as e:
                flash(f"Error: {e}", "danger")

            return redirect(url_for("admin_candidates", key="admin123"))

        if action == "toggle":
            try:
                cand_id = int(request.form.get("id"))
                with db_connect() as conn:
                    row = conn.execute("SELECT is_active FROM candidates WHERE id=?", (cand_id,)).fetchone()
                    if row:
                        new_val = 0 if row["is_active"] == 1 else 1
                        conn.execute("UPDATE candidates SET is_active=? WHERE id=?", (new_val, cand_id))
                        conn.commit()
                flash("Candidate status updated.", "info")
            except Exception as e:
                flash(f"Error: {e}", "danger")
            return redirect(url_for("admin_candidates", key="admin123"))

        if action == "delete":
            try:
                cand_id = int(request.form.get("id"))
                delete_candidate(cand_id)
                flash("Candidate deleted.", "info")
            except Exception as e:
                flash(f"Error: {e}", "danger")
            return redirect(url_for("admin_candidates", key="admin123"))

    positions = get_positions(active_only=False)
    candidates = get_candidates(active_only=False)
    return render_template("admin_candidates.html", positions=positions, candidates=candidates, title="Candidates")


# ================= ADMIN VOTERS =================
@app.route("/admin/voters", methods=["GET", "POST"])
def admin_voters():
    if not require_admin_key():
        return "Unauthorized", 401

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        # -------- ADD VOTER --------
        if action == "add":
            national_id = request.form.get("national_id", "").strip()
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            eligible = request.form.get("eligible", "1").strip()

            if not national_id or not full_name or not email or not password :
                flash("National ID, Full Name and Email and password are required.", "danger")
                return redirect(url_for("admin_voters", key="admin123"))

            try:
                eligible_val = 1 if eligible == "1" else 0

                # temp_pw = make_temp_password(6)  # e.g., VOTE-AB12CD
               # pw_hash = generate_password_hash(temp_pw)
                pw_hash = generate_password_hash(password)

                with db_connect() as conn:
                    # 1) Add voter to voter list
                    conn.execute(
                    "INSERT INTO voters (national_id, full_name, email, eligible, has_voted) VALUES (?,?,?,?,0)",
                    (national_id, full_name, email, eligible_val),
                    )

                    # 2) Create voter login account (only if not already exists)
                   # existing = conn.execute(
                   # "SELECT 1 FROM voter_users WHERE national_id=? OR email=?",
                    #(national_id, email),
                   # ).fetchone()

                   # if existing:
                        # Voter exists already; keep the old password
                    #    flash("Voter added to list. Login already exists for this voter.", "warning")
                   # else:
                    #    conn.execute(
                     #   "INSERT INTO voter_users (email, password_hash, national_id) VALUES (?,?,?)",
                     #   (email, pw_hash, national_id),
                  #  )
                   # flash(f"Voter added. TEMP PASSWORD for {email}: {temp_pw}", "success")
                   
             # 2) Create OR reset voter login (admin sets password)
                    conn.execute("""
                        INSERT INTO voter_users (email, password_hash, national_id)
                        VALUES (?,?,?)
                        ON CONFLICT(email) DO UPDATE SET
                            password_hash=excluded.password_hash,
                            national_id=excluded.national_id
                    """, (email, pw_hash, national_id))

                    flash(f"Voter added/updated. Login Password set for {email}.", "success")
                    # flash(f"Voter login created/reset for {email}.", "success")

                    conn.commit()

            except sqlite3.IntegrityError:
                flash("This National ID already exists in voters list.", "danger")
            except Exception as e:
                flash(f"Error adding voter: {e}", "danger")
                
            return redirect(url_for("admin_voters", key="admin123"))
        
        
        # -------- RESET VOTER PASSWORD --------
        if action == "reset_pw":
            nid = request.form.get("national_id", "").strip()
            new_pw = request.form.get("new_password", "")

            if not nid or not new_pw:
                flash("National ID and new password are required.", "danger")
                return redirect(url_for("admin_voters", key="admin123"))

            try:
                from werkzeug.security import generate_password_hash
                pw_hash = generate_password_hash(new_pw)

                with db_connect() as conn:
                    # Get voter email from voters table
                    row = conn.execute(
                        "SELECT email FROM voters WHERE national_id=?",
                        (nid,),
                    ).fetchone()

                    if not row or not row["email"]:
                        flash("This voter has no email saved. Add email first.", "danger")
                        return redirect(url_for("admin_voters", key="admin123"))

                    email = row["email"].strip().lower()

                    # Update password in voter_users
                    updated = conn.execute(
                        "UPDATE voter_users SET password_hash=? WHERE email=?",
                        (pw_hash, email),
                    ).rowcount

                    if updated == 0:
                        # If login row does not exist yet, create it
                        conn.execute(
                            "INSERT INTO voter_users (email, password_hash, national_id) VALUES (?,?,?)",
                            (email, pw_hash, nid),
                        )

                    conn.commit()

                flash(f"Password reset successfully for {email}.", "success")
                return redirect(url_for("admin_voters", key="admin123"))

            except Exception as e:
                flash(f"Password reset failed: {e}", "danger")
                return redirect(url_for("admin_voters", key="admin123"))

        # -------- TOGGLE ACTIVE/INACTIVE (eligible) --------
        if action == "toggle":
            nid = request.form.get("national_id", "").strip()
            if not nid:
                flash("Missing voter ID.", "danger")
                return redirect(url_for("admin_voters", key="admin123"))

            try:
                with db_connect() as conn:
                    row = conn.execute(
                        "SELECT eligible FROM voters WHERE national_id=?",
                        (nid,),
                    ).fetchone()

                    if not row:
                        flash("Voter not found.", "danger")
                        return redirect(url_for("admin_voters", key="admin123"))

                    new_val = 0 if row["eligible"] == 1 else 1
                    conn.execute(
                        "UPDATE voters SET eligible=? WHERE national_id=?",
                        (new_val, nid),
                    )
                    conn.commit()

                flash("Voter status updated.", "info")

            except Exception as e:
                flash(f"Update failed: {e}", "danger")

            return redirect(url_for("admin_voters", key="admin123"))

        flash("Invalid action.", "danger")
        return redirect(url_for("admin_voters", key="admin123"))

    # -------- GET --------
    with db_connect() as conn:
        voters = conn.execute("""
            SELECT national_id, full_name, email, eligible, has_voted
            FROM voters
            ORDER BY CAST(national_id AS INTEGER)
        """).fetchall()

    return render_template("admin_voters.html", voters=voters, title="Voters")


@app.route("/admin/voters/delete/<national_id>", methods=["POST"])
def admin_voters_delete(national_id):
    if not require_admin_key():
        return "Unauthorized", 401

    try:
        with db_connect() as conn:
            # delete votes (new system)
            try:
                conn.execute("DELETE FROM votes_new WHERE national_id=?", (national_id,))
            except Exception:
                pass

            # delete votes (old system - choice)
            try:
                conn.execute("DELETE FROM votes WHERE national_id=?", (national_id,))
            except Exception:
                pass

            conn.execute("DELETE FROM voters WHERE national_id=?", (national_id,))
            conn.commit()

        flash("Voter deleted.", "info")
    except Exception as e:
        flash(f"Delete failed: {e}", "danger")

    return redirect(url_for("admin_voters", key="admin123"))


@app.route("/admin/export/voters.xlsx")
def admin_export_voters():
    if not require_admin_key():
        return "Unauthorized", 401

    with db_connect() as conn:
        rows = conn.execute("""
            SELECT national_id, full_name, email, eligible, has_voted
            FROM voters
            ORDER BY CAST(national_id AS INTEGER)
        """).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Voters"

    ws.append(["National ID", "Full Name", "Email", "Eligible", "Has Voted"])
    for r in rows:
        ws.append([
            r["national_id"],
            r["full_name"],
            r["email"],
            "Active" if r["eligible"] == 1 else "Inactive",
            "Yes" if r["has_voted"] == 1 else "No",
        ])

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    return send_file(
        bio,
        as_attachment=True,
        download_name="voters.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ================= ADMIN VOTES =================

@app.route("/admin/votes", methods=["GET"])
def admin_votes():
    if not require_admin_key():
        return "Unauthorized", 401

    with db_connect() as conn:
        votes = conn.execute("""
    SELECT vt.cast_at, vt.national_id,
           v.full_name,
           p.title AS position_title,
           c.name AS candidate_name
    FROM votes_new vt
    LEFT JOIN voters v ON v.national_id = vt.national_id
    LEFT JOIN positions p ON p.id = vt.position_id
    LEFT JOIN candidates c ON c.id = vt.candidate_id
    ORDER BY vt.cast_at DESC
    """).fetchall()
    return render_template("admin_votes.html", votes=votes, title="Votes")


@app.route("/admin/export/votes.xlsx")
def admin_export_votes():
    if not require_admin_key():
        return "Unauthorized", 401

    with db_connect() as conn:
        # Detect which votes table exists
        tbl = None
        for name in ["votes_new", "votes", "ballots"]:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            if exists:
                tbl = name
                break

        if not tbl:
            flash("Votes table not found.", "danger")
            return redirect(url_for("admin", key="admin123"))

        rows = conn.execute(f"SELECT * FROM {tbl} ORDER BY rowid DESC").fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Votes"

    if rows:
        headers = list(rows[0].keys())
        ws.append(headers)
        for r in rows:
            ws.append([r[h] for h in headers])
    else:
        ws.append(["No votes found"])

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    return send_file(
        bio,
        as_attachment=True,
        download_name="votes.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# ---------------- Startup ----------------
ensure_schema_and_choose_votes_table()

@app.route("/", methods=["GET"])
def home():
    import random
    seq = random.choice([["LEFT", "RIGHT"], ["RIGHT", "LEFT"]])
    session["challenge_seq"] = seq
    return render_template("login.html", challenge_seq=seq)

# ================= RESET ELECTION =================
@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    if not require_admin_key():
        return "Unauthorized", 401

    try:
        with db_connect() as conn:
            # Clear votes table (new system)
            try:
                conn.execute("DELETE FROM votes_new")
            except Exception:
                pass

            # Reset voter status
            conn.execute("UPDATE voters SET has_voted=0")
            conn.commit()

        flash("Election reset successfully.", "success")

    except Exception as e:
        flash(f"Reset failed: {e}", "danger")

    return redirect(url_for("admin", key="admin123"))


@app.route("/auth", methods=["GET", "POST"])
def auth_login():
    if request.method == "POST":
        role = request.form.get("role", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        # ---------- 1) ADMIN (fixed single credential, NO DB) ----------
        if role == "admin":
            if email != ADMIN_EMAIL or not check_password_hash(ADMIN_PASSWORD_HASH, password):
                flash("Invalid admin credentials.", "danger")
                return redirect(url_for("auth_login"))

            session.clear()
            session["role"] = "admin"
            session["email"] = ADMIN_EMAIL
            flash("Admin login successful.", "success")
            return redirect(url_for("admin_dashboard"))

        # ---------- 2) FROM HERE ONWARD: VOTER LOGIN USING DB ----------
        if role != "voter":
            flash("Select a valid role.", "danger")
            return redirect(url_for("auth_login"))

        if not email or not password:
            flash("Email and password are required.", "danger")
            return redirect(url_for("auth_login"))

        # voter table (your existing table)
        table = "voter_users"

        with db_connect() as conn:
            user = conn.execute(f"SELECT * FROM {table} WHERE email=?", (email,)).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid voter credentials.", "danger")
            return redirect(url_for("auth_login"))

        # voter login OK
        session.clear()
        session["role"] = "voter"
        session["email"] = email
        session["national_id"] = user["national_id"]  # if column exists

        if request.form.get("remember") == "1":
            session.permanent = True

        flash("Voter login successful.", "success")
        return redirect(url_for("voter_dashboard"))

    # GET request
    return render_template("auth_login.html", title="Login")

@app.route("/auth/register/voter", methods=["GET", "POST"])
def auth_register_voter():
    if request.method == "POST":
        national_id = request.form.get("national_id", "").strip()
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not national_id or not full_name or not email or not password or not confirm:
            flash("All fields are required.", "danger")
            return redirect(url_for("auth_register_voter"))

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return redirect(url_for("auth_register_voter"))

        # If already in voters list -> allow direct account creation in voter_users
        voter = get_voter(national_id)
        pw_hash = generate_password_hash(password)

        try:
            with db_connect() as conn:
                # If voter exists already in voters list, create login (if not exists)
                if voter:
                    existing = conn.execute(
                        "SELECT 1 FROM voter_users WHERE national_id=? OR email=?",
                        (national_id, email),
                    ).fetchone()

                    if existing:
                        flash("Account already exists. Please login.", "info")
                        return redirect(url_for("auth_login"))

                    conn.execute(
                        "INSERT INTO voter_users (email, password_hash, national_id) VALUES (?,?,?)",
                        (email, pw_hash, national_id),
                    )
                    conn.commit()

                    flash("Account created. Please login.", "success")
                    return redirect(url_for("auth_login"))

                # Otherwise, create a pending request (await admin approval)
                conn.execute("""
                    INSERT INTO pending_voters (national_id, full_name, email, password_hash, status)
                    VALUES (?,?,?,?, 'PENDING')
                """, (national_id, full_name, email, pw_hash))
                conn.commit()

            flash("Registration request submitted. Please wait for admin approval.", "success")
            return redirect(url_for("auth_login"))

        except sqlite3.IntegrityError:
            flash("A request/account with this National ID or Email already exists.", "danger")
            return redirect(url_for("auth_register_voter"))

        except Exception as e:
            flash(f"Registration failed: {e}", "danger")
            return redirect(url_for("auth_register_voter"))

    return render_template("auth_register_voter.html", title="Voter Registration")


@app.route("/auth/register/admin", methods=["GET", "POST"])
def auth_register_admin():
    return ("Not Found", 404)
    # OPTIONAL: protect with admin123 key so random people can't create admins
    if request.args.get("key") != "admin123":
        return "Unauthorized", 401

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Email and password required.", "danger")
            return redirect(url_for("auth_register_admin", key="admin123"))

        pw_hash = generate_password_hash(password)

        try:
            with db_connect() as conn:
                conn.execute("""
                    INSERT INTO admin_users (email, password_hash)
                    VALUES (?,?)
                """, (email, pw_hash))
                conn.commit()

            flash("Admin created. Please login.", "success")
            return redirect(url_for("auth_login"))

        except sqlite3.IntegrityError:
            flash("Admin email already exists.", "danger")
            return redirect(url_for("auth_register_admin", key="admin123"))

    return render_template("auth_register_admin.html", title="Admin Registration")

@app.route("/auth/logout")
def auth_logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("auth_login"))

def require_role(role):
    def wrapper(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            if session.get("role") != role:
                return redirect("/auth")
            return fn(*args, **kwargs)
        return inner
    return wrapper

@app.route("/admin/dashboard")
@require_role("admin")
def admin_dashboard():
    return render_template("admin.html")

@app.route("/admin/pending-voters", methods=["GET", "POST"])
def admin_pending_voters():
    if not require_admin_key():
        return "Unauthorized", 401

    if request.method == "POST":
        action = request.form.get("action", "")
        pid = request.form.get("id", "")

        with db_connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_voters WHERE id=? AND status='PENDING'",
                (pid,),
            ).fetchone()

            if not row:
                flash("Pending request not found.", "danger")
                return redirect(url_for("admin_pending_voters", key="admin123"))

            if action == "approve":
                # Add to voters list (eligible = 1 by default; you can set 0 if you want)
                conn.execute("""
                    INSERT OR IGNORE INTO voters (national_id, full_name, email, eligible, has_voted)
                    VALUES (?,?,?,1,0)
                """, (row["national_id"], row["full_name"], row["email"]))

                # Create login if not exists
                existing = conn.execute(
                    "SELECT 1 FROM voter_users WHERE national_id=? OR email=?",
                    (row["national_id"], row["email"]),
                ).fetchone()

                if not existing:
                    conn.execute("""
                        INSERT INTO voter_users (email, password_hash, national_id)
                        VALUES (?,?,?)
                    """, (row["email"], row["password_hash"], row["national_id"]))

                conn.execute("UPDATE pending_voters SET status='APPROVED' WHERE id=?", (pid,))
                conn.commit()

                flash("Voter approved and added to voter list.", "success")
                return redirect(url_for("admin_pending_voters", key="admin123"))

            if action == "reject":
                conn.execute("UPDATE pending_voters SET status='REJECTED' WHERE id=?", (pid,))
                conn.commit()
                flash("Request rejected.", "info")
                return redirect(url_for("admin_pending_voters", key="admin123"))

        flash("Invalid action.", "danger")
        return redirect(url_for("admin_pending_voters", key="admin123"))

    # GET
    with db_connect() as conn:
        pending = conn.execute("""
            SELECT id, national_id, full_name, email, created_at
            FROM pending_voters
            WHERE status='PENDING'
            ORDER BY created_at DESC
        """).fetchall()

    return render_template("admin_pending_voters.html", pending=pending, title="Pending Voters")

@app.route("/voter/dashboard")
@require_role("voter")
def voter_dashboard():
    challenge_seq = session.get("challenge_seq", ["LEFT", "RIGHT", "BLINK"])
    return render_template("login.html",challenge_seq=challenge_seq)

@app.route("/auth/help")
def auth_help():
    return render_template("auth_help.html")


def ensure_voters_email_column():
    with db_connect() as conn:
        cols = conn.execute("PRAGMA table_info(voters)").fetchall()
        colnames = {c["name"] for c in cols}
        if "email" not in colnames:
            conn.execute("ALTER TABLE voters ADD COLUMN email TEXT")
            conn.commit()
            
            
if __name__ == "__main__":
    print("STARTING_SERVER...")
    ensure_votes_new_table()
    ensure_auth_tables()   # <-- ADD THIS LINE
    ensure_voters_email_column()  # <-- ADD THIS
    ensure_pending_voter_table()   # <-- ADD THIS LINE
    ensure_single_admin()
    app.run(host="127.0.0.1", port=8050, debug=False, threaded=True)