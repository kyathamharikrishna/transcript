from datetime import datetime
import base64
import binascii
import hashlib
import hmac
import importlib.util
import json
import mimetypes
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
    make_response,
    send_from_directory,
    session,
    url_for,
)
import mysql.connector
import requests
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from qa_engine import answer_question
from summarizer import summarize_text
from transcription_features import (
    build_meeting_insights,
    build_speaker_profile,
    build_segments,
    clean_speaker_transcript,
    create_speaker_turns,
    create_srt,
    create_timestamped_transcript,
    extract_action_items,
    format_duration,
    language_display,
    word_count,
)
from translator import TRANSLATION_LANGUAGE_OPTIONS, normalize_translation_payload, translate_transcript_bundle


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "100")) * 1024 * 1024

JWT_COOKIE_NAME = "tf_access_token"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = int(os.getenv("JWT_EXPIRY_DAYS", "7")) * 24 * 60 * 60
DATA_ROOT = os.getenv("PERSISTENT_DATA_DIR", "uploads")
BASE_FOLDER = os.path.join(DATA_ROOT, "transcriber")
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
    ("hi", "Hindi"),
    ("te", "Telugu"),
    ("ta", "Tamil"),
    ("kn", "Kannada"),
    ("ml", "Malayalam"),
    ("mr", "Marathi"),
    ("gu", "Gujarati"),
    ("bn", "Bengali"),
    ("ur", "Urdu"),
    ("pa", "Punjabi"),
    ("or", "Odia"),
    ("as", "Assamese"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("ar", "Arabic"),
    ("zh", "Chinese"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("ru", "Russian"),
]

DOWNLOADS = {
    "json": (JSON_FOLDER, "json_file"),
    "combined": (COMBINED_FOLDER, "combined_file"),
    "srt": (SRT_FOLDER, "srt_file"),
    "transcript": (TRANSCRIPT_FOLDER, "transcript_file"),
    "summary": (SUMMARY_FOLDER, "summary_file"),
}

ALLOWED_UPLOAD_EXTENSIONS = {
    "flac",
    "m4a",
    "mp3",
    "mp4",
    "mpeg",
    "mpga",
    "oga",
    "ogg",
    "wav",
    "webm",
}

JOBS = {}
JOBS_LOCK = threading.Lock()
MODEL_LOCK = threading.Lock()
SCHEMA_LOCK = threading.Lock()
ASR_MODEL = None
SCHEMA_READY = False


class OpenAIQuotaError(RuntimeError):
    pass


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


def normalize_email(email):
    return (email or "").strip().lower()


def normalize_password(password):
    return (password or "").strip()


def show_all_transcriptions():
    return os.getenv("SHOW_ALL_TRANSCRIPTIONS", "1").lower() in {"1", "true", "yes", "all"}


def jwt_secret():
    return os.getenv("JWT_SECRET_KEY") or app.secret_key


def _b64url_encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value):
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_jwt(payload):
    now = int(time.time())
    claims = {
        "iat": now,
        "exp": now + JWT_EXPIRY_SECONDS,
        **payload,
    }
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    signing_input = ".".join(
        [
            _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(jwt_secret().encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def decode_jwt(token):
    try:
        header_part, payload_part, signature_part = token.split(".")
        signing_input = f"{header_part}.{payload_part}"
        expected = hmac.new(jwt_secret().encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
        actual = _b64url_decode(signature_part)
        if not hmac.compare_digest(expected, actual):
            return None

        header = json.loads(_b64url_decode(header_part))
        if header.get("alg") != JWT_ALGORITHM:
            return None

        claims = json.loads(_b64url_decode(payload_part))
        if int(claims.get("exp", 0)) < int(time.time()):
            return None
        if not claims.get("sub"):
            return None
        return claims
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error, UnicodeDecodeError):
        return None


def jwt_from_request():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return request.cookies.get(JWT_COOKIE_NAME)


def set_auth_cookie(response, token):
    secure_cookie = os.getenv("JWT_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}
    response.set_cookie(
        JWT_COOKIE_NAME,
        token,
        max_age=JWT_EXPIRY_SECONDS,
        httponly=True,
        secure=secure_cookie,
        samesite="Lax",
    )
    return response


def clear_auth_cookie(response):
    response.delete_cookie(JWT_COOKIE_NAME)
    return response


def is_database_locked_error(exc):
    return using_sqlite() and isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def db_retry(operation, attempts=6, base_delay=0.18):
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_database_locked_error(exc) or attempt == attempts - 1:
                raise
            time.sleep(base_delay * (attempt + 1))


def get_db_connection():
    if using_sqlite():
        db_path = os.getenv("SQLITE_DB_PATH", os.path.join(BASE_FOLDER, "transcribeflow.sqlite"))
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        conn = sqlite3.connect(
            db_path,
            timeout=float(os.getenv("SQLITE_TIMEOUT", "30")),
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA foreign_keys = ON")
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

    with SCHEMA_LOCK:
        if SCHEMA_READY:
            return
        return db_retry(_ensure_database_schema_once)


def _ensure_database_schema_once():
    global SCHEMA_READY

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
                user_id INTEGER,
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )

        cursor.execute("PRAGMA table_info(transcriptions)")
        existing_columns = {row["name"] for row in cursor.fetchall()}
        additions = {
            "user_id": "INTEGER",
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

        cursor.execute(
            """
            UPDATE transcriptions
            SET user_id = (
                SELECT users.id
                FROM users
                WHERE LOWER(users.email) = LOWER(transcriptions.user_email)
                ORDER BY users.id DESC
                LIMIT 1
            )
            WHERE user_id IS NULL
              AND user_email IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM users
                  WHERE LOWER(users.email) = LOWER(transcriptions.user_email)
              )
            """
        )

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
            user_id INT,
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )

    additions = {
        "user_id": "INT",
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

    cursor.execute(
        """
        UPDATE transcriptions t
        JOIN (
            SELECT LOWER(email) AS email_key, MAX(id) AS user_id
            FROM users
            GROUP BY LOWER(email)
        ) u ON u.email_key = LOWER(t.user_email)
        SET t.user_id = u.user_id
        WHERE t.user_id IS NULL
        """
    )

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
            import whisper

            model_name = os.getenv("WHISPER_MODEL", "small")
            ASR_MODEL = whisper.load_model(model_name)
    return ASR_MODEL


def render_runtime():
    return any(
        os.getenv(name)
        for name in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL", "RENDER_SERVICE_NAME")
    )


def transcription_backend():
    backend = os.getenv("TRANSCRIPTION_BACKEND", "whisper").strip().lower()

    if backend in {"openai_api", "api"}:
        return "openai"

    if backend in {"groq_api", "groq-whisper"}:
        return "groq"

    if backend in {"auto", "demo"}:
        if os.getenv("GROQ_API_KEY"):
            return "groq"
        if os.getenv("OPENAI_API_KEY") or render_runtime():
            return "openai"
        return "whisper"

    if backend == "whisper" and render_runtime() and os.getenv("ALLOW_RENDER_WHISPER", "0") != "1":
        return "groq" if os.getenv("GROQ_API_KEY") else "openai"

    return backend


def whisper_available():
    return importlib.util.find_spec("whisper") is not None


def allowed_upload(filename):
    if not filename or "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in ALLOWED_UPLOAD_EXTENSIONS


def normalize_api_segments(payload):
    text = (payload.get("text") or "").strip()
    duration = float(payload.get("duration") or 0)
    segments = payload.get("segments") or []
    words = payload.get("words") or []

    if not segments and text:
        segments = [{"start": 0, "end": duration or 1, "text": text}]

    normalized_segments = []
    for segment in segments:
        segment_start = float(segment.get("start") or 0)
        segment_end = float(segment.get("end") or segment_start + 1)
        segment_words = segment.get("words") or [
            word
            for word in words
            if float(word.get("start") or 0) >= segment_start
            and float(word.get("end") or 0) <= segment_end + 0.05
        ]

        normalized_segments.append(
            {
                "start": segment_start,
                "end": segment_end,
                "text": (segment.get("text") or "").strip(),
                "words": [
                    {
                        "word": word.get("word") or word.get("text") or "",
                        "start": word.get("start", segment_start),
                        "end": word.get("end", segment_end),
                        "probability": word.get("probability"),
                    }
                    for word in segment_words
                ],
            }
        )

    return {
        "text": text,
        "language": payload.get("language") or "unknown",
        "segments": normalized_segments,
    }


def openai_key_help_message():
    return (
        "Render OPENAI_API_KEY is invalid or still a placeholder. "
        "Open Render Dashboard → transcribeflow → Environment, replace OPENAI_API_KEY "
        "with a real OpenAI API key, save changes, then redeploy."
    )


def groq_key_help_message():
    return (
        "GROQ_API_KEY is missing or invalid. Add a valid Groq API key in Render "
        "Environment, or switch TRANSCRIPTION_BACKEND to openai/whisper."
    )


def openai_quota_help_message():
    return (
        "OpenAI transcription quota is exhausted for this API key. "
        "To avoid OpenAI billing, add GROQ_API_KEY in Render and set "
        "TRANSCRIPTION_BACKEND=auto or groq. Groq will transcribe with Whisper-compatible "
        "timestamps. Another option is TRANSCRIPTION_BACKEND=whisper on a server with enough memory."
    )


def is_placeholder_openai_key(api_key):
    normalized = (api_key or "").strip().lower()
    if not normalized:
        return True

    placeholder_markers = (
        "your_",
        "your-",
        "replace",
        "placeholder",
        "example",
        "dummy",
        "test-key",
    )
    return any(marker in normalized for marker in placeholder_markers)


def api_error_details(response):
    try:
        return response.json()
    except ValueError:
        return response.text[:500]


def transcribe_with_openai_api(audio_path, forced_language):
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if is_placeholder_openai_key(api_key):
        raise RuntimeError(openai_key_help_message())

    import requests

    model = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
    content_type = mimetypes.guess_type(audio_path)[0] or "application/octet-stream"
    data = [
        ("model", model),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "segment"),
        ("timestamp_granularities[]", "word"),
    ]
    if forced_language and forced_language != "auto":
        data.append(("language", forced_language))

    with open(audio_path, "rb") as audio_file:
        response = requests.post(
            os.getenv("OPENAI_TRANSCRIBE_URL", "https://api.openai.com/v1/audio/transcriptions"),
            headers={"Authorization": f"Bearer {api_key}"},
            data=data,
            files={"file": (os.path.basename(audio_path), audio_file, content_type)},
            timeout=600,
        )

    if response.status_code >= 400:
        details = api_error_details(response)
        api_error = details.get("error", {}) if isinstance(details, dict) else {}
        if response.status_code == 401 or api_error.get("code") == "invalid_api_key":
            raise RuntimeError(openai_key_help_message())
        if (
            response.status_code == 429
            or api_error.get("code") == "insufficient_quota"
            or api_error.get("type") == "insufficient_quota"
            or "quota" in str(api_error.get("message", "")).lower()
        ):
            raise OpenAIQuotaError(openai_quota_help_message())

        raise RuntimeError(f"Transcription API failed: {details}")

    return normalize_api_segments(response.json())


def transcribe_with_groq_api(audio_path, forced_language):
    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if is_placeholder_openai_key(api_key):
        raise RuntimeError(groq_key_help_message())

    import requests

    model = os.getenv("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
    content_type = mimetypes.guess_type(audio_path)[0] or "application/octet-stream"
    data = [
        ("model", model),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "segment"),
        ("timestamp_granularities[]", "word"),
    ]
    if forced_language and forced_language != "auto":
        data.append(("language", forced_language))

    with open(audio_path, "rb") as audio_file:
        response = requests.post(
            os.getenv("GROQ_TRANSCRIBE_URL", "https://api.groq.com/openai/v1/audio/transcriptions"),
            headers={"Authorization": f"Bearer {api_key}"},
            data=data,
            files={"file": (os.path.basename(audio_path), audio_file, content_type)},
            timeout=600,
        )

    if response.status_code >= 400:
        details = api_error_details(response)
        if response.status_code in {401, 403}:
            raise RuntimeError(groq_key_help_message())
        raise RuntimeError(f"Groq transcription failed: {details}")

    return normalize_api_segments(response.json())


def transcribe_with_local_whisper(audio_path, forced_language):
    options = {
        "word_timestamps": True,
        "fp16": os.getenv("WHISPER_FP16", "0") == "1",
    }
    if forced_language and forced_language != "auto":
        options["language"] = forced_language
    return get_asr_model().transcribe(audio_path, **options)


def set_job(job_id, **updates):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(updates)


def get_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def create_job(user_id, user_email, original_filename):
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "user_id": user_id,
            "user_email": user_email,
            "original_filename": original_filename,
            "status": "queued",
            "progress": 5,
            "message": "Queued for transcription",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    return job_id


def insert_transcription_record(record):
    def operation():
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn)
        try:
            cursor.execute(
                adapt_sql(
                    """
                INSERT INTO transcriptions
                (
                    user_id,
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                ),
                (
                    record.get("user_id"),
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
            return cursor.lastrowid
        finally:
            cursor.close()
            conn.close()

    return db_retry(operation)


def get_transcription_by_job(job_id, user_id=None, user_email=None):
    def operation():
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn, dictionary=True)
        try:
            if show_all_transcriptions():
                cursor.execute(
                    adapt_sql(
                        """
                    SELECT *
                    FROM transcriptions
                    WHERE job_id = %s
                    LIMIT 1
                    """
                    ),
                    (job_id,),
                )
            elif user_id:
                cursor.execute(
                    adapt_sql(
                        """
                    SELECT *
                    FROM transcriptions
                    WHERE job_id = %s
                      AND (
                          user_id = %s
                          OR (user_id IS NULL AND LOWER(user_email) = LOWER(%s))
                      )
                    LIMIT 1
                    """
                    ),
                    (job_id, user_id, user_email),
                )
            else:
                cursor.execute(
                    adapt_sql(
                        """
                    SELECT *
                    FROM transcriptions
                    WHERE job_id = %s AND LOWER(user_email) = LOWER(%s)
                    LIMIT 1
                    """
                    ),
                    (job_id, user_email),
                )
            return row_to_dict(cursor.fetchone())
        finally:
            cursor.close()
            conn.close()

    return db_retry(operation)


def get_history(user_id=None, user_email=None, limit=50):
    def operation():
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn, dictionary=True)
        try:
            query = """
            SELECT
                id,
                user_id,
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
                action_items_count,
                created_at
            FROM transcriptions
            """
            params = []
            if not show_all_transcriptions():
                if user_id:
                    query += " WHERE user_id = %s OR (user_id IS NULL AND LOWER(user_email) = LOWER(%s))"
                    params.extend([user_id, user_email])
                else:
                    query += " WHERE LOWER(user_email) = LOWER(%s)"
                    params.append(user_email)
            query += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)
            cursor.execute(adapt_sql(query), tuple(params))
            return rows_to_dicts(cursor.fetchall())
        finally:
            cursor.close()
            conn.close()

    records = db_retry(operation)

    for record in records:
        record["duration_label"] = format_duration(record.get("duration_seconds") or 0)
        record["language_label"] = language_display(record.get("detected_language"))
    return records


def get_dashboard_stats(user_id=None, user_email=None):
    def operation():
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn, dictionary=True)
        try:
            query = """
            SELECT
                COUNT(*) AS total_transcriptions,
                COALESCE(SUM(word_count), 0) AS total_words,
                COALESCE(SUM(action_items_count), 0) AS total_actions,
                COALESCE(SUM(duration_seconds), 0) AS total_seconds
            FROM transcriptions
            """
            params = []
            if not show_all_transcriptions():
                if user_id:
                    query += " WHERE user_id = %s OR (user_id IS NULL AND LOWER(user_email) = LOWER(%s))"
                    params.extend([user_id, user_email])
                else:
                    query += " WHERE LOWER(user_email) = LOWER(%s)"
                    params.append(user_email)
            cursor.execute(adapt_sql(query), tuple(params))
            return row_to_dict(cursor.fetchone()) or {}
        finally:
            cursor.close()
            conn.close()

    stats = db_retry(operation)
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
            payload.setdefault("insights", {})
            payload.setdefault("speaker_profile", {})
            payload.setdefault("speaker_turns", create_speaker_turns(payload.get("segments", [])))
            payload.setdefault("translation", {})
            payload["translation"] = normalize_translation_payload(payload.get("translation"))
            payload["timestamped_transcript"] = clean_speaker_transcript(
                payload.get("timestamped_transcript") or payload.get("transcript") or ""
            )
            return payload

    transcript = ""
    summary = ""
    transcript_file = record.get("transcript_file")
    summary_file = record.get("summary_file")
    if transcript_file:
        path = os.path.join(TRANSCRIPT_FOLDER, secure_filename(transcript_file))
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as file:
                transcript = clean_speaker_transcript(file.read())
    if summary_file:
        path = os.path.join(SUMMARY_FOLDER, secure_filename(summary_file))
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as file:
                summary = file.read()

    return {
        "transcript": transcript,
        "timestamped_transcript": clean_speaker_transcript(transcript),
        "summary": summary,
        "segments": [],
        "speaker_turns": [],
        "action_items": [],
        "insights": {},
        "speaker_profile": {},
        "translation": {},
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
    insights = payload.get("insights", {})
    speaker_profile = payload.get("speaker_profile", {})
    translation = payload.get("translation", {})
    keywords = ", ".join(item["word"] for item in insights.get("top_keywords", [])[:8]) or "No keywords detected."
    translated_block = ""
    if translation.get("translated_text"):
        translated_block = f"""
========== TRANSLATION ({translation.get('target_language')}) ==========

{translation['translated_text']}
"""
    translated_summary_block = ""
    if translation.get("translated_summary"):
        translated_summary_block = f"""
========== TRANSLATED SUMMARY ({translation.get('target_language')}) ==========

{translation['translated_summary']}
"""
    return f"""========== TRANSCRIBEFLOW REPORT ==========

Original File: {payload['original_filename']}
Detected Language: {stats['detected_language']}
Duration: {stats['duration_label']}
Word Count: {stats['word_count']}
Processing Time: {stats['processing_seconds']}s
Action Items: {len(payload['action_items'])}
Questions Asked: {insights.get('question_count', 0)}
Decision Signals: {insights.get('decision_count', 0)}
Low Confidence Words: {insights.get('low_confidence_words', 0)}
Top Keywords: {keywords}
Speaking Pace: {speaker_profile.get('pace_label', 'Unknown')} ({speaker_profile.get('speaking_rate_wpm', 0)} WPM)

========== SUMMARY ==========

{payload['summary']}
{translated_block}
{translated_summary_block}

========== ACTION ITEMS ==========

{actions}

========== TRANSCRIPT ==========

{payload['timestamped_transcript']}
"""


def process_transcription_job(
    job_id,
    audio_path,
    audio_filename,
    original_filename,
    user_id,
    user_email,
    forced_language,
    target_translation_language,
):
    started_at = time.perf_counter()

    try:
        backend = transcription_backend()
        if backend not in {"openai", "groq", "whisper"}:
            raise RuntimeError(
                f"Unsupported TRANSCRIPTION_BACKEND={backend}. Use whisper, openai, groq, or auto."
            )

        if backend == "openai":
            set_job(
                job_id,
                status="processing",
                progress=35,
                message="Sending audio to transcription API",
            )
            try:
                result = transcribe_with_openai_api(audio_path, forced_language)
            except OpenAIQuotaError:
                if os.getenv("GROQ_API_KEY"):
                    set_job(
                        job_id,
                        status="processing",
                        progress=42,
                        message="OpenAI quota exhausted, trying Groq Whisper",
                    )
                    result = transcribe_with_groq_api(audio_path, forced_language)
                    backend = "groq-fallback"
                elif whisper_available() and os.getenv("OPENAI_FALLBACK_TO_WHISPER", "1") == "1":
                    set_job(
                        job_id,
                        status="processing",
                        progress=42,
                        message="API quota exhausted, falling back to local Whisper",
                    )
                    result = transcribe_with_local_whisper(audio_path, forced_language)
                    backend = "whisper-fallback"
                else:
                    raise
        elif backend == "groq":
            set_job(
                job_id,
                status="processing",
                progress=35,
                message="Sending audio to Groq Whisper API",
            )
            result = transcribe_with_groq_api(audio_path, forced_language)
        elif backend == "whisper" and not whisper_available():
            raise RuntimeError(
                "Whisper is not installed. Run pip install -r requirements.txt, "
                "or set TRANSCRIPTION_BACKEND=openai/groq on Render."
            )
        else:
            set_job(
                job_id,
                status="processing",
                progress=18,
                message="Preparing audio for transcription",
            )

            set_job(
                job_id,
                status="processing",
                progress=35,
                message="Loading Whisper and reading the audio",
            )
            result = transcribe_with_local_whisper(audio_path, forced_language)

        set_job(
            job_id,
            status="processing",
            progress=55,
            message="Building speaker transcript",
        )

        segments = build_segments(result.get("segments", []))
        transcript = (result.get("text") or "").strip()
        if not transcript:
            transcript = " ".join(segment["text"] for segment in segments).strip()

        detected_language_code = result.get("language") or forced_language or "unknown"
        detected_language = language_display(detected_language_code)
        timestamped_transcript = create_timestamped_transcript(segments)
        speaker_turns = create_speaker_turns(segments)
        action_items = extract_action_items(segments)
        insights = build_meeting_insights(segments, transcript, action_items)
        speaker_profile = build_speaker_profile(segments, transcript, detected_language)
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
        translation = translate_transcript_bundle(
            timestamped_transcript or transcript,
            summary,
            detected_language_code,
            target_translation_language,
        )
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
            "user_id": user_id,
            "user_email": user_email,
            "original_filename": original_filename,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "audio_file": audio_filename,
            "transcript": transcript,
            "timestamped_transcript": timestamped_transcript,
            "speaker_turns": speaker_turns,
            "summary": summary,
            "detected_language": detected_language,
            "detected_language_code": detected_language_code,
            "transcription_backend": backend,
            "segments": segments,
            "action_items": action_items,
            "insights": insights,
            "speaker_profile": speaker_profile,
            "translation": translation,
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
                "user_id": user_id,
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


def dashboard_context(user_id, user_email, limit=5):
    try:
        return {
            "recent_records": get_history(user_id, user_email, limit=limit),
            "stats": get_dashboard_stats(user_id, user_email),
            "show_all_history": show_all_transcriptions(),
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
            "show_all_history": show_all_transcriptions(),
        }


def wants_json_response():
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.path.startswith("/api/")
        or request.path.startswith("/jobs/")
    )


def password_matches(stored_password, password):
    stored_password = stored_password or ""
    try:
        if check_password_hash(stored_password, password):
            return True
    except ValueError:
        pass
    return stored_password == password


def login_user_session(user, email, password=None):
    user_id = user.get("id")
    session["user_id"] = user_id
    session["user_email"] = email
    session["user_name"] = user.get("name") or email.split("@")[0]
    link_legacy_transcriptions_to_user(user_id, email)
    if password and (user.get("password") or "") == password:
        update_user_password_hash(user_id, password)


def authenticated_redirect(user, email, password=None, endpoint="dashboard"):
    login_user_session(user, email, password)
    token = create_jwt(
        {
            "sub": str(user.get("id") or email),
            "email": email,
            "name": session.get("user_name") or email.split("@")[0],
            "scope": "transcriptions:read transcriptions:write",
        }
    )
    response = make_response(redirect(url_for(endpoint)))
    return set_auth_cookie(response, token)


@app.before_request
def load_user_from_jwt():
    if session.get("user_email"):
        return

    token = jwt_from_request()
    if not token:
        return

    claims = decode_jwt(token)
    if not claims:
        return

    subject = str(claims.get("sub") or "")
    email = normalize_email(claims.get("email") or (subject if "@" in subject else ""))
    user = None
    if subject.isdigit():
        user = fetch_user_by_id(int(subject))
        if user:
            email = normalize_email(user.get("email"))
    if not user and email:
        user = fetch_user_by_email(email)
    if not user:
        return

    session["user_id"] = user.get("id")
    session["user_email"] = normalize_email(user.get("email"))
    session["user_name"] = user.get("name") or session["user_email"].split("@")[0]


def current_user_id():
    return session.get("user_id")


def current_user_email():
    return session.get("user_email")


def record_access_identity():
    if show_all_transcriptions():
        return None, None
    return current_user_id(), current_user_email()


def fetch_user_by_id(user_id):
    if not user_id:
        return None

    def operation():
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn, dictionary=True)
        try:
            cursor.execute(adapt_sql("SELECT * FROM users WHERE id = %s LIMIT 1"), (user_id,))
            return row_to_dict(cursor.fetchone())
        finally:
            cursor.close()
            conn.close()

    return db_retry(operation)


def fetch_user_by_email(email):
    users = fetch_users_by_email(email)
    return users[0] if users else None


def fetch_users_by_email(email):
    def operation():
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn, dictionary=True)
        try:
            cursor.execute(adapt_sql("SELECT * FROM users WHERE LOWER(email) = LOWER(%s) ORDER BY id DESC"), (email,))
            return rows_to_dicts(cursor.fetchall())
        finally:
            cursor.close()
            conn.close()

    return db_retry(operation)


def find_matching_user(email, password):
    users = fetch_users_by_email(email)
    for user in users:
        if password_matches(user.get("password"), password):
            return user
    return None


def link_legacy_transcriptions_to_user(user_id, email):
    if not user_id or not email:
        return None

    def operation():
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn)
        try:
            cursor.execute(
                adapt_sql(
                    """
                    UPDATE transcriptions
                    SET user_id = %s
                    WHERE LOWER(user_email) = LOWER(%s)
                    """
                ),
                (user_id, email),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    return db_retry(operation)


def create_user(name, email, password_hash):
    def operation():
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn)
        try:
            cursor.execute(
                adapt_sql("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)"),
                (name, email, password_hash),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            cursor.close()
            conn.close()

    user_id = db_retry(operation)
    return fetch_user_by_id(user_id) or fetch_user_by_email(email)


def update_user_password_hash(user_id, password):
    if not user_id:
        return None

    def operation():
        conn = get_db_connection()
        cursor = get_cursor(conn)
        try:
            cursor.execute(
                adapt_sql("UPDATE users SET password = %s WHERE id = %s"),
                (generate_password_hash(password), user_id),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    return db_retry(operation)


def google_client_id():
    return (os.getenv("GOOGLE_CLIENT_ID") or "").strip()


def verify_google_id_token(credential):
    client_id = google_client_id()
    if not client_id:
        raise RuntimeError("Google sign-in is not configured. Add GOOGLE_CLIENT_ID in Render.")

    response = requests.get(
        "https://oauth2.googleapis.com/tokeninfo",
        params={"id_token": credential},
        timeout=15,
    )
    if response.status_code >= 400:
        raise RuntimeError("Google sign-in token could not be verified.")

    payload = response.json()
    if payload.get("aud") != client_id:
        raise RuntimeError("Google sign-in token is for a different app.")
    if payload.get("email_verified") not in {True, "true", "True", "1", 1}:
        raise RuntimeError("Google account email is not verified.")

    email = normalize_email(payload.get("email"))
    if not email:
        raise RuntimeError("Google did not return an email address.")
    return {
        "email": email,
        "name": (payload.get("name") or email.split("@")[0]).strip(),
    }


def get_or_create_google_user(profile):
    user = fetch_user_by_email(profile["email"])
    if user:
        return user

    password_hash = generate_password_hash(f"google:{uuid.uuid4().hex}")
    return create_user(profile["name"], profile["email"], password_hash)


@app.context_processor
def inject_auth_settings():
    return {"google_client_id": google_client_id()}


@app.errorhandler(413)
def file_too_large(error):
    message = f"File is too large. Maximum upload size is {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)}MB."
    if wants_json_response():
        return jsonify({"error": message}), 413
    return message, 413


@app.errorhandler(404)
def not_found(error):
    if wants_json_response():
        return jsonify({"error": "Requested resource was not found. Please retry the upload."}), 404
    return "Page not found", 404


@app.errorhandler(500)
def internal_server_error(error):
    if wants_json_response():
        return jsonify({"error": "Server error while processing the request. Please try again."}), 500
    return render_template("login.html", error="Server error. Please try again."), 500


@app.route("/")
def login_page():
    if "user_email" in session:
        return redirect(url_for("dashboard"))
    registered = request.args.get("registered") == "1"
    return render_template(
        "login.html",
        email=normalize_email(request.args.get("email")),
        success="Registration complete. Please log in with your new credentials." if registered else None,
    )


@app.route("/register", methods=["GET"])
def register_page():
    if "user_email" in session:
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/register", methods=["POST"])
def register():
    name = (request.form.get("name") or "").strip()
    email = normalize_email(request.form.get("email"))
    raw_password = normalize_password(request.form.get("password"))

    if not name or not email or not raw_password:
        return render_template(
            "register.html",
            error="Please enter your name, email, and password.",
            name=name,
            email=email,
        )
    if len(raw_password) < 6:
        return render_template(
            "register.html",
            error="Password must be at least 6 characters.",
            name=name,
            email=email,
        )

    password = generate_password_hash(raw_password)

    try:
        if fetch_users_by_email(email):
            return render_template(
                "register.html",
                error="This email is already registered. Please log in with your existing credentials.",
                name=name,
                email=email,
            )

        create_user(name, email, password)
    except db_integrity_errors():
        return render_template(
            "register.html",
            error="This email is already registered. Please log in with your existing credentials.",
            name=name,
            email=email,
        )
    except db_errors() as exc:
        return render_template(
            "register.html",
            error="Database is busy. Please try again in a few seconds." if is_database_locked_error(exc) else f"Database error: {exc}",
            name=name,
            email=email,
        )

    return redirect(url_for("login_page", registered="1", email=email))


@app.route("/login", methods=["POST"])
def login():
    email = normalize_email(request.form["email"])
    password = normalize_password(request.form["password"])

    try:
        user = find_matching_user(email, password)
    except db_errors() as exc:
        return render_template(
            "login.html",
            error="Database is busy. Please try again in a few seconds." if is_database_locked_error(exc) else f"Database error: {exc}",
            email=email,
        )

    if user:
        return authenticated_redirect(user, email, password)

    return render_template("login.html", error="Invalid email or password.", email=email)


@app.route("/login/google", methods=["POST"])
def google_login():
    data = request.get_json(silent=True) or request.form
    credential = (data.get("credential") or "").strip()
    if not credential:
        return jsonify({"error": "Google sign-in did not return a credential."}), 400

    try:
        profile = verify_google_id_token(credential)
        user = get_or_create_google_user(profile)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    except db_errors() as exc:
        return jsonify({"error": str(exc)}), 400

    login_user_session(user, normalize_email(user.get("email") or profile["email"]))
    token = create_jwt(
        {
            "sub": str(user.get("id")),
            "email": normalize_email(user.get("email") or profile["email"]),
            "name": session.get("user_name") or profile["name"],
            "scope": "transcriptions:read transcriptions:write",
        }
    )
    response = make_response(jsonify({"redirect_url": url_for("dashboard")}))
    return set_auth_cookie(response, token)


@app.route("/dashboard")
def dashboard():
    if "user_email" not in session:
        return redirect(url_for("login_page"))

    context = dashboard_context(current_user_id(), current_user_email())
    return render_template(
        "dashboard.html",
        language_options=LANGUAGE_OPTIONS,
        translation_language_options=TRANSLATION_LANGUAGE_OPTIONS,
        **context,
    )


@app.route("/history")
def history():
    if "user_email" not in session:
        return redirect(url_for("login_page"))

    try:
        records = get_history(current_user_id(), current_user_email(), limit=100)
        db_error = None
    except db_errors() as exc:
        records = []
        db_error = str(exc)

    return render_template(
        "history.html",
        records=records,
        db_error=db_error,
        show_all_history=show_all_transcriptions(),
    )


@app.route("/upload", methods=["POST"])
def upload():
    try:
        if "user_email" not in session:
            return jsonify({"error": "Please log in again."}), 401

        if "audiofile" not in request.files:
            return jsonify({"error": "No audio file uploaded."}), 400

        file = request.files["audiofile"]
        if file.filename == "":
            return jsonify({"error": "Choose or record an audio file first."}), 400

        original_filename = secure_filename(file.filename) or "recorded_audio.webm"
        if not allowed_upload(original_filename):
            supported = ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))
            return jsonify({"error": f"Unsupported file type. Supported formats: {supported}."}), 400

        job_id = create_job(current_user_id(), current_user_email(), original_filename)
        audio_filename = f"{job_id}_{original_filename}"
        audio_path = os.path.join(AUDIO_FOLDER, audio_filename)
        file.save(audio_path)

        forced_language = request.form.get("language", "auto")
        target_translation_language = request.form.get("translation_language", "none")
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
                current_user_id(),
                current_user_email(),
                forced_language,
                target_translation_language,
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
    except Exception as exc:
        return jsonify({"error": f"Upload failed before transcription started: {exc}"}), 500


@app.route("/jobs/<job_id>")
def job_status(job_id):
    if "user_email" not in session:
        return jsonify({"error": "Please log in again."}), 401

    job = get_job(job_id)
    if not job or (
        job.get("user_id") != current_user_id()
        and normalize_email(job.get("user_email")) != current_user_email()
    ):
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

    user_id, user_email = record_access_identity()
    record = get_transcription_by_job(job_id, user_id, user_email)
    if not record:
        abort(404)

    payload = load_payload(record)
    return render_template(
        "result.html",
        record=record,
        payload=payload,
        stats=payload.get("stats", {}),
        insights=payload.get("insights", {}),
        speaker_profile=payload.get("speaker_profile", {}),
        translation=payload.get("translation", {}),
        segments=payload.get("segments", []),
        speaker_turns=payload.get("speaker_turns") or create_speaker_turns(payload.get("segments", [])),
        action_items=payload.get("action_items", []),
        files=payload.get("files", {}),
    )


@app.route("/api/ask/<job_id>", methods=["POST"])
def ask_transcript(job_id):
    if "user_email" not in session:
        return jsonify({"error": "Please log in again."}), 401

    user_id, user_email = record_access_identity()
    record = get_transcription_by_job(job_id, user_id, user_email)
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

    def operation():
        ensure_database_schema()
        conn = get_db_connection()
        cursor = get_cursor(conn, dictionary=True)
        try:
            if show_all_transcriptions():
                cursor.execute(
                    adapt_sql(
                        f"""
                    SELECT id
                    FROM transcriptions
                    WHERE {column} = %s
                    LIMIT 1
                    """
                    ),
                    (filename,),
                )
            else:
                user_id = current_user_id()
                user_email = current_user_email()
                if user_id:
                    cursor.execute(
                        adapt_sql(
                            f"""
                        SELECT id
                        FROM transcriptions
                        WHERE {column} = %s
                          AND (
                              user_id = %s
                              OR (user_id IS NULL AND LOWER(user_email) = LOWER(%s))
                          )
                        LIMIT 1
                        """
                        ),
                        (filename, user_id, user_email),
                    )
                else:
                    cursor.execute(
                        adapt_sql(
                            f"""
                        SELECT id
                        FROM transcriptions
                        WHERE LOWER(user_email) = LOWER(%s) AND {column} = %s
                        LIMIT 1
                        """
                        ),
                        (user_email, filename),
                    )
            return row_to_dict(cursor.fetchone())
        finally:
            cursor.close()
            conn.close()

    record = db_retry(operation)

    if not record:
        abort(404)

    return send_from_directory(folder, filename, as_attachment=True, download_name=filename)


@app.route("/health")
def health():
    sqlite_db_path = None
    if using_sqlite():
        sqlite_db_path = os.getenv("SQLITE_DB_PATH", os.path.join(BASE_FOLDER, "transcribeflow.sqlite"))

    return jsonify(
        {
            "status": "ok",
            "database": "sqlite" if using_sqlite() else "mysql",
            "data_root": DATA_ROOT,
            "sqlite_db_path": sqlite_db_path,
            "transcription_backend": transcription_backend(),
            "groq_configured": bool(os.getenv("GROQ_API_KEY")),
            "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
            "google_configured": bool(google_client_id()),
            "show_all_transcriptions": show_all_transcriptions(),
        }
    )


@app.route("/logout")
def logout():
    response = make_response(redirect(url_for("login_page")))
    session.clear()
    return clear_auth_cookie(response)


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        threaded=True,
    )
