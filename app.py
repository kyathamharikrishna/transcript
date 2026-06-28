from datetime import datetime
import json
import os
import sqlite3
import threading
import time
import traceback
import uuid

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
import mysql.connector
import whisper
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from qa_engine import answer_question
from summarizer import summarize_text
from transcription_features import (
    build_segments,
    create_srt,
    create_timestamped_transcript,
    extract_action_items,
    format_duration,
    language_display,
    word_count,
)


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

BASE_FOLDER = os.path.join("uploads", "transcriber")
AUDIO_FOLDER = os.path.join(BASE_FOLDER, "audio")
TRANSCRIPT_FOLDER = os.path.join(BASE_FOLDER, "transcript")
SUMMARY_FOLDER = os.path.join(BASE_FOLDER, "summary")
JSON_FOLDER = os.path.join(BASE_FOLDER, "json")
COMBINED_FOLDER = os.path.join(BASE_FOLDER, "combined")
SRT_FOLDER = os.path.join(BASE_FOLDER, "srt")

for folder in (
    AUDIO_FOLDER,
    TRANSCRIPT_FOLDER,
    SUMMARY_FOLDER,
    JSON_FOLDER,
    COMBINED_FOLDER,
    SRT_FOLDER,
):
    os.makedirs(folder, exist_ok=True)

LANGUAGE_OPTIONS = [
    ("auto", "Auto detect"),
    ("en", "English"),
    ("te", "Telugu"),
    ("hi", "Hindi"),
    ("ta", "Tamil"),
    ("kn", "Kannada"),
    ("ml", "Malayalam"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
]

DOWNLOADS = {
    "json": (JSON_FOLDER, "json_file"),
    "combined": (COMBINED_FOLDER, "combined_file"),
    "srt": (SRT_FOLDER, "srt_file"),
    "transcript": (TRANSCRIPT_FOLDER, "transcript_file"),
    "summary": (SUMMARY_FOLDER, "summary_file"),
}

JOBS = {}
JOBS_LOCK = threading.Lock()
MODEL_LOCK = threading.Lock()
ASR_MODEL = None
SCHEMA_READY = False


def using_sqlite():
    return os.getenv("DB_BACKEND", "mysql").lower() == "sqlite"


def db_errors():
    return (mysql.connector.Error, sqlite3.Error)


def db_integrity_errors():
    return (mysql.connector.IntegrityError, sqlite3.IntegrityError)


def adapt_sql(query):
    if using_sqlite():
        return query.replace("%s", "?")
    return query


def get_cursor(conn, dictionary=False):
    if using_sqlite():
        return conn.cursor()
    return conn.cursor(dictionary=dictionary)


def row_to_dict(row):
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return row


def rows_to_dicts(rows):
    return [row_to_dict(row) for row in rows]


def get_db_connection():
    if using_sqlite():
        db_path = os.getenv("SQLITE_DB_PATH", os.path.join(BASE_FOLDER, "transcribeflow.sqlite"))
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "transcribeflow"),
    )


def ensure_database_schema():
    global SCHEMA_READY

    if SCHEMA_READY:
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    if using_sqlite():
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS transcriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT,
                job_id TEXT,
                original_filename TEXT,
                audio_file TEXT,
                transcript_file TEXT,
                summary_file TEXT,
                json_file TEXT,
                combined_file TEXT,
                srt_file TEXT,
                detected_language TEXT,
                duration_seconds REAL DEFAULT 0,
                word_count INTEGER DEFAULT 0,
                processing_seconds REAL DEFAULT 0,
                action_items_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cursor.execute("PRAGMA table_info(transcriptions)")
        existing_columns = {row["name"] for row in cursor.fetchall()}
        additions = {
            "job_id": "TEXT",
            "original_filename": "TEXT",
            "combined_file": "TEXT",
            "srt_file": "TEXT",
            "detected_language": "TEXT",
            "duration_seconds": "REAL DEFAULT 0",
            "word_count": "INTEGER DEFAULT 0",
            "processing_seconds": "REAL DEFAULT 0",
            "action_items_count": "INTEGER DEFAULT 0",
        }

        for column, definition in additions.items():
            if column not in existing_columns:
                cursor.execute(f"ALTER TABLE transcriptions ADD COLUMN {column} {definition}")

        conn.commit()
        cursor.close()
        conn.close()
        SCHEMA_READY = True
        return

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            email VARCHAR(100) NOT NULL UNIQUE,
            password VARCHAR(255) NOT NULL
        )
        """
    )
    cursor.execute("SHOW COLUMNS FROM users LIKE 'password'")
    password_column = cursor.fetchone()
    if password_column and "varchar(255)" not in str(password_column[1]).lower():
        cursor.execute("ALTER TABLE users MODIFY COLUMN password VARCHAR(255) NOT NULL")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS transcriptions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_email VARCHAR(100),
            job_id VARCHAR(64),
            original_filename VARCHAR(255),
            audio_file VARCHAR(255),
            transcript_file VARCHAR(255),
            summary_file VARCHAR(255),
            json_file VARCHAR(255),
            combined_file VARCHAR(255),
            srt_file VARCHAR(255),
            detected_language VARCHAR(80),
            duration_seconds FLOAT DEFAULT 0,
            word_count INT DEFAULT 0,
            processing_seconds FLOAT DEFAULT 0,
            action_items_count INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    additions = {
        "job_id": "VARCHAR(64)",
        "original_filename": "VARCHAR(255)",
        "combined_file": "VARCHAR(255)",
        "srt_file": "VARCHAR(255)",
        "detected_language": "VARCHAR(80)",
        "duration_seconds": "FLOAT DEFAULT 0",
        "word_count": "INT DEFAULT 0",
        "processing_seconds": "FLOAT DEFAULT 0",
        "action_items_count": "INT DEFAULT 0",
    }

    for column, definition in additions.items():
        cursor.execute("SHOW COLUMNS FROM transcriptions LIKE %s", (column,))
        if cursor.fetchone() is None:
            cursor.execute(f"ALTER TABLE transcriptions ADD COLUMN {column} {definition}")

    conn.commit()
    cursor.close()
    conn.close()
    SCHEMA_READY = True


def get_asr_model():
    global ASR_MODEL

    if ASR_MODEL is not None:
        return ASR_MODEL

    with MODEL_LOCK:
        if ASR_MODEL is None:
            model_name = os.getenv("WHISPER_MODEL", "small")
            ASR_MODEL = whisper.load_model(model_name)
    return ASR_MODEL


def set_job(job_id, **updates):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(updates)


def get_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def create_job(user_email, original_filename):
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "user_email": user_email,
            "original_filename": original_filename,
            "status": "queued",
            "progress": 5,
            "message": "Queued for transcription",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    return job_id


def insert_transcription_record(record):
    ensure_database_schema()
    conn = get_db_connection()
    cursor = get_cursor(conn)
    cursor.execute(
        adapt_sql(
            """
        INSERT INTO transcriptions
        (
            user_email,
            job_id,
            original_filename,
            audio_file,
            transcript_file,
            summary_file,
            json_file,
            combined_file,
            srt_file,
            detected_language,
            duration_seconds,
            word_count,
            processing_seconds,
            action_items_count
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        ),
        (
            record["user_email"],
            record["job_id"],
            record["original_filename"],
            record["audio_file"],
            record["transcript_file"],
            record["summary_file"],
            record["json_file"],
            record["combined_file"],
            record["srt_file"],
            record["detected_language"],
            record["duration_seconds"],
            record["word_count"],
            record["processing_seconds"],
            record["action_items_count"],
        ),
    )
    conn.commit()
    record_id = cursor.lastrowid
    cursor.close()
    conn.close()
    return record_id


def get_transcription_by_job(job_id, user_email):
    ensure_database_schema()
    conn = get_db_connection()
    cursor = get_cursor(conn, dictionary=True)
    cursor.execute(
        adapt_sql(
            """
        SELECT *
        FROM transcriptions
        WHERE job_id = %s AND user_email = %s
        LIMIT 1
        """
        ),
        (job_id, user_email),
    )
    record = row_to_dict(cursor.fetchone())
    cursor.close()
    conn.close()
    return record


def get_history(user_email, limit=50):
    ensure_database_schema()
    conn = get_db_connection()
    cursor = get_cursor(conn, dictionary=True)
    cursor.execute(
        adapt_sql(
            """
        SELECT
            id,
            job_id,
            original_filename,
            audio_file,
            transcript_file,
            summary_file,
            json_file,
            combined_file,
            srt_file,
            detected_language,
            duration_seconds,
            word_count,
            processing_seconds,
            action_items_count,
            created_at
        FROM transcriptions
        WHERE user_email = %s
        ORDER BY created_at DESC
        LIMIT %s
        """
        ),
        (user_email, limit),
    )
    records = rows_to_dicts(cursor.fetchall())
    cursor.close()
    conn.close()

    for record in records:
        record["duration_label"] = format_duration(record.get("duration_seconds") or 0)
        record["language_label"] = language_display(record.get("detected_language"))
    return records


def get_dashboard_stats(user_email):
    ensure_database_schema()
    conn = get_db_connection()
    cursor = get_cursor(conn, dictionary=True)
    cursor.execute(
        adapt_sql(
            """
        SELECT
            COUNT(*) AS total_transcriptions,
            COALESCE(SUM(word_count), 0) AS total_words,
            COALESCE(SUM(action_items_count), 0) AS total_actions,
            COALESCE(SUM(duration_seconds), 0) AS total_seconds
        FROM transcriptions
        WHERE user_email = %s
        """
        ),
        (user_email,),
    )
    stats = row_to_dict(cursor.fetchone()) or {}
    cursor.close()
    conn.close()
    stats["total_duration_label"] = format_duration(stats.get("total_seconds") or 0)
    return stats


def load_payload(record):
    json_filename = record.get("json_file")
    if json_filename:
        json_path = os.path.join(JSON_FOLDER, secure_filename(json_filename))
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as file:
                payload = json.load(file)
                payload.setdefault("segments", [])
                payload.setdefault("action_items", [])
                payload.setdefault("files", {})
                payload.setdefault("stats", {})
                return payload

    transcript = ""
    summary = ""
    transcript_file = record.get("transcript_file")
    summary_file = record.get("summary_file")
    if transcript_file:
        path = os.path.join(TRANSCRIPT_FOLDER, secure_filename(transcript_file))
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as file:
                transcript = file.read()
    if summary_file:
        path = os.path.join(SUMMARY_FOLDER, secure_filename(summary_file))
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as file:
                summary = file.read()

    return {
        "transcript": transcript,
        "timestamped_transcript": transcript,
        "summary": summary,
        "segments": [],
        "action_items": [],
        "files": {
            "transcript": record.get("transcript_file"),
            "summary": record.get("summary_file"),
            "json": record.get("json_file"),
            "combined": record.get("combined_file"),
            "srt": record.get("srt_file"),
        },
        "stats": {
            "detected_language": language_display(record.get("detected_language")),
            "duration_label": format_duration(record.get("duration_seconds") or 0),
            "word_count": record.get("word_count") or word_count(transcript),
            "processing_seconds": record.get("processing_seconds") or 0,
        },
    }


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as file:
        file.write(text or "")


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=4, ensure_ascii=False)


def build_combined_report(payload):
    stats = payload["stats"]
    action_lines = []
    for item in payload["action_items"]:
        labels = ", ".join(item["labels"])
        action_lines.append(f"- {item['timestamp']} [{labels}] {item['text']}")

    actions = "\n".join(action_lines) if action_lines else "No action items detected."
    return f"""========== TRANSCRIBEFLOW AI REPORT ==========

Original File: {payload['original_filename']}
Detected Language: {stats['detected_language']}
Duration: {stats['duration_label']}
Word Count: {stats['word_count']}
Processing Time: {stats['processing_seconds']}s
Action Items: {len(payload['action_items'])}

========== SUMMARY ==========

{payload['summary']}

========== ACTION ITEMS ==========

{actions}

========== TIMESTAMPED TRANSCRIPT ==========

{payload['timestamped_transcript']}
"""


def process_transcription_job(
    job_id,
    audio_path,
    audio_filename,
    original_filename,
    user_email,
    forced_language,
):
    started_at = time.perf_counter()

    try:
        set_job(
            job_id,
            status="processing",
            progress=18,
            message="Loading Whisper and reading the audio",
        )

        options = {
            "word_timestamps": True,
            "fp16": os.getenv("WHISPER_FP16", "0") == "1",
        }
        if forced_language and forced_language != "auto":
            options["language"] = forced_language

        result = get_asr_model().transcribe(audio_path, **options)

        set_job(
            job_id,
            status="processing",
            progress=55,
            message="Building timestamped transcript",
        )

        segments = build_segments(result.get("segments", []))
        transcript = (result.get("text") or "").strip()
        if not transcript:
            transcript = " ".join(segment["text"] for segment in segments).strip()

        detected_language_code = result.get("language") or forced_language or "unknown"
        detected_language = language_display(detected_language_code)
        timestamped_transcript = create_timestamped_transcript(segments)
        action_items = extract_action_items(segments)
        transcript_word_count = word_count(transcript)
        duration_seconds = segments[-1]["end"] if segments else 0
        processing_seconds = round(time.perf_counter() - started_at, 2)

        set_job(
            job_id,
            status="processing",
            progress=72,
            message="Summarizing and extracting action items",
        )

        summary = summarize_text(transcript)
        srt_text = create_srt(segments)

        transcript_filename = f"{job_id}.txt"
        summary_filename = f"{job_id}.txt"
        json_filename = f"{job_id}.json"
        combined_filename = f"{job_id}_report.txt"
        srt_filename = f"{job_id}.srt"

        files = {
            "audio": audio_filename,
            "transcript": transcript_filename,
            "summary": summary_filename,
            "json": json_filename,
            "combined": combined_filename,
            "srt": srt_filename,
        }

        payload = {
            "job_id": job_id,
            "user_email": user_email,
            "original_filename": original_filename,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "audio_file": audio_filename,
            "transcript": transcript,
            "timestamped_transcript": timestamped_transcript,
            "summary": summary,
            "detected_language": detected_language,
            "detected_language_code": detected_language_code,
            "segments": segments,
            "action_items": action_items,
            "files": files,
            "stats": {
                "detected_language": detected_language,
                "duration_seconds": round(duration_seconds, 2),
                "duration_label": format_duration(duration_seconds),
                "word_count": transcript_word_count,
                "processing_seconds": processing_seconds,
            },
        }

        set_job(
            job_id,
            status="processing",
            progress=88,
            message="Saving reports and history",
        )

        write_text(os.path.join(TRANSCRIPT_FOLDER, transcript_filename), timestamped_transcript)
        write_text(os.path.join(SUMMARY_FOLDER, summary_filename), summary)
        write_text(os.path.join(SRT_FOLDER, srt_filename), srt_text)
        write_json(os.path.join(JSON_FOLDER, json_filename), payload)
        write_text(os.path.join(COMBINED_FOLDER, combined_filename), build_combined_report(payload))

        record_id = insert_transcription_record(
            {
                "user_email": user_email,
                "job_id": job_id,
                "original_filename": original_filename,
                "audio_file": audio_filename,
                "transcript_file": transcript_filename,
                "summary_file": summary_filename,
                "json_file": json_filename,
                "combined_file": combined_filename,
                "srt_file": srt_filename,
                "detected_language": detected_language_code,
                "duration_seconds": round(duration_seconds, 2),
                "word_count": transcript_word_count,
                "processing_seconds": processing_seconds,
                "action_items_count": len(action_items),
            }
        )

        set_job(
            job_id,
            status="completed",
            progress=100,
            message="Transcription ready",
            record_id=record_id,
            result_url=f"/transcription/{job_id}",
        )
    except Exception as exc:
        set_job(
            job_id,
            status="failed",
            progress=100,
            message="Processing failed",
            error=str(exc),
            traceback=traceback.format_exc(),
        )


def dashboard_context(user_email, limit=5):
    try:
        return {
            "recent_records": get_history(user_email, limit=limit),
            "stats": get_dashboard_stats(user_email),
            "db_error": None,
        }
    except db_errors() as exc:
        return {
            "recent_records": [],
            "stats": {
                "total_transcriptions": 0,
                "total_words": 0,
                "total_actions": 0,
                "total_duration_label": "0s",
            },
            "db_error": str(exc),
        }


@app.route("/")
def login_page():
    if "user_email" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/register", methods=["GET"])
def register_page():
    if "user_email" in session:
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/register", methods=["POST"])
def register():
    name = request.form["name"].strip()
    email = request.form["email"].strip().lower()
    password = generate_password_hash(request.form["password"])

    try:
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn)
        cursor.execute(
            adapt_sql("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)"),
            (name, email, password),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except db_integrity_errors():
        return render_template("register.html", error="An account already exists for this email.")
    except db_errors() as exc:
        return render_template("register.html", error=f"Database error: {exc}")

    return redirect(url_for("login_page"))


@app.route("/login", methods=["POST"])
def login():
    email = request.form["email"].strip().lower()
    password = request.form["password"]

    try:
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn, dictionary=True)
        cursor.execute(adapt_sql("SELECT * FROM users WHERE email = %s"), (email,))
        user = row_to_dict(cursor.fetchone())
        if user:
            stored_password = user.get("password") or ""
            password_ok = check_password_hash(stored_password, password)
            if not password_ok and stored_password == password:
                password_ok = True
                cursor.execute(
                    adapt_sql("UPDATE users SET password = %s WHERE email = %s"),
                    (generate_password_hash(password), email),
                )
                conn.commit()
        else:
            password_ok = False
        cursor.close()
        conn.close()
    except db_errors() as exc:
        return render_template("login.html", error=f"Database error: {exc}")

    if user and password_ok:
        session["user_email"] = email
        session["user_name"] = user.get("name") or email.split("@")[0]
        return redirect(url_for("dashboard"))

    return render_template("login.html", error="Invalid email or password.")


@app.route("/dashboard")
def dashboard():
    if "user_email" not in session:
        return redirect(url_for("login_page"))

    context = dashboard_context(session["user_email"])
    return render_template(
        "dashboard.html",
        language_options=LANGUAGE_OPTIONS,
        **context,
    )


@app.route("/history")
def history():
    if "user_email" not in session:
        return redirect(url_for("login_page"))

    try:
        records = get_history(session["user_email"], limit=100)
        db_error = None
    except db_errors() as exc:
        records = []
        db_error = str(exc)

    return render_template("history.html", records=records, db_error=db_error)


@app.route("/upload", methods=["POST"])
def upload():
    if "user_email" not in session:
        return jsonify({"error": "Please log in again."}), 401

    if "audiofile" not in request.files:
        return jsonify({"error": "No audio file uploaded."}), 400

    file = request.files["audiofile"]
    if file.filename == "":
        return jsonify({"error": "Choose or record an audio file first."}), 400

    original_filename = secure_filename(file.filename)
    job_id = create_job(session["user_email"], original_filename)
    audio_filename = f"{job_id}_{original_filename}"
    audio_path = os.path.join(AUDIO_FOLDER, audio_filename)
    file.save(audio_path)

    forced_language = request.form.get("language", "auto")
    set_job(
        job_id,
        audio_file=audio_filename,
        progress=10,
        message="Audio uploaded, worker starting",
    )

    worker = threading.Thread(
        target=process_transcription_job,
        args=(
            job_id,
            audio_path,
            audio_filename,
            original_filename,
            session["user_email"],
            forced_language,
        ),
        daemon=True,
    )
    worker.start()

    return jsonify(
        {
            "job_id": job_id,
            "status_url": url_for("job_status", job_id=job_id),
            "result_url": url_for("transcription_result", job_id=job_id),
        }
    )


@app.route("/jobs/<job_id>")
def job_status(job_id):
    if "user_email" not in session:
        return jsonify({"error": "Please log in again."}), 401

    job = get_job(job_id)
    if not job or job.get("user_email") != session["user_email"]:
        abort(404)

    return jsonify(
        {
            "job_id": job["job_id"],
            "status": job["status"],
            "progress": job["progress"],
            "message": job.get("message", ""),
            "error": job.get("error"),
            "result_url": job.get("result_url") or url_for("transcription_result", job_id=job_id),
        }
    )


@app.route("/transcription/<job_id>")
def transcription_result(job_id):
    if "user_email" not in session:
        return redirect(url_for("login_page"))

    record = get_transcription_by_job(job_id, session["user_email"])
    if not record:
        abort(404)

    payload = load_payload(record)
    return render_template(
        "result.html",
        record=record,
        payload=payload,
        stats=payload.get("stats", {}),
        segments=payload.get("segments", []),
        action_items=payload.get("action_items", []),
        files=payload.get("files", {}),
    )


@app.route("/api/ask/<job_id>", methods=["POST"])
def ask_transcript(job_id):
    if "user_email" not in session:
        return jsonify({"error": "Please log in again."}), 401

    record = get_transcription_by_job(job_id, session["user_email"])
    if not record:
        abort(404)

    payload = load_payload(record)
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Ask a question first."}), 400

    answer = answer_question(
        question=question,
        transcript=payload.get("transcript", ""),
        segments=payload.get("segments", []),
    )
    return jsonify(answer)


@app.route("/download/<filetype>/<filename>")
def download_file(filetype, filename):
    if "user_email" not in session:
        return redirect(url_for("login_page"))

    if filetype not in DOWNLOADS:
        abort(404)

    safe_filename = secure_filename(filename)
    if safe_filename != filename:
        abort(404)

    folder, column = DOWNLOADS[filetype]
    ensure_database_schema()
    conn = get_db_connection()
    cursor = get_cursor(conn, dictionary=True)
    cursor.execute(
        adapt_sql(
            f"""
        SELECT id
        FROM transcriptions
        WHERE user_email = %s AND {column} = %s
        LIMIT 1
        """
        ),
        (session["user_email"], filename),
    )
    record = row_to_dict(cursor.fetchone())
    cursor.close()
    conn.close()

    if not record:
        abort(404)

    return send_from_directory(folder, filename, as_attachment=True, download_name=filename)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "database": "sqlite" if using_sqlite() else "mysql"})


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        threaded=True,
    )
