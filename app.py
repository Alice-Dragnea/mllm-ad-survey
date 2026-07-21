from __future__ import annotations

import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

import duckdb
from flask import Flask, g, jsonify, render_template, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_IMAGE_DIR = BASE_DIR / "images"
DEFAULT_DB_PATH = BASE_DIR / "annotations.duckdb"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
LEASE_MINUTES = int(os.environ.get("LEASE_MINUTES", "30"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_app(test_config: Optional[dict] = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        IMAGE_DIR=Path(os.environ.get("IMAGE_DIR", DEFAULT_IMAGE_DIR)),
        DB_PATH=Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH)),
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["IMAGE_DIR"]).mkdir(parents=True, exist_ok=True)
    initialize_database(app)
    database_lock = threading.RLock()

    # DuckDB uses optimistic concurrency. Serializing these tiny queue transactions
    # avoids two simultaneous reservations conflicting on the same pending row.
    @app.before_request
    def lock_database_api():
        if request.path.startswith("/api/"):
            database_lock.acquire()
            g.database_lock_held = True

    @app.teardown_request
    def unlock_database_api(_error=None):
        if getattr(g, "database_lock_held", False):
            g.database_lock_held = False
            database_lock.release()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/images/<path:filename>")
    def image_file(filename: str):
        return send_from_directory(app.config["IMAGE_DIR"], filename)

    @app.post("/api/consent")
    def record_consent():
        body = request.get_json(silent=True) or {}
        worker_id = clean_worker_id(body.get("worker_id"))
        if not worker_id:
            return jsonify(error="A valid worker ID is required."), 400
        with connect(app) as con:
            con.execute(
                "INSERT INTO consents (worker_id, consented_at) VALUES (?, ?)",
                [worker_id, utcnow()],
            )
        return jsonify(ok=True)

    @app.get("/api/stats")
    def stats():
        with connect(app) as con:
            release_expired(con)
            return jsonify(get_stats(con))

    @app.post("/api/next")
    def next_image():
        body = request.get_json(silent=True) or {}
        worker_id = clean_worker_id(body.get("worker_id"))
        if not worker_id:
            return jsonify(error="A valid worker ID is required."), 400

        with connect(app) as con:
            now = utcnow()
            release_expired(con, now)
            con.execute("BEGIN TRANSACTION")
            try:
                # A browser refresh should return the same active assignment.
                row = con.execute(
                    """
                    SELECT id, filename, reservation_token
                    FROM images
                    WHERE status = 'in_progress' AND reserved_by = ? AND reserved_until > ?
                    ORDER BY reserved_at DESC
                    LIMIT 1
                    """,
                    [worker_id, now],
                ).fetchone()

                if row is None:
                    candidate = con.execute(
                        "SELECT id, filename FROM images WHERE status = 'pending' ORDER BY id LIMIT 1"
                    ).fetchone()
                    if candidate:
                        token = secrets.token_urlsafe(24)
                        reserved_until = now + timedelta(minutes=LEASE_MINUTES)
                        updated = con.execute(
                            """
                            UPDATE images
                            SET status = 'in_progress', reserved_by = ?, reservation_token = ?,
                                reserved_at = ?, reserved_until = ?
                            WHERE id = ? AND status = 'pending'
                            RETURNING id, filename, reservation_token
                            """,
                            [worker_id, token, now, reserved_until, candidate[0]],
                        ).fetchone()
                        row = updated
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

            current_stats = get_stats(con)
            if row:
                return jsonify(
                    state="assigned",
                    image={
                        "id": row[0],
                        "filename": row[1],
                        "url": f"/images/{row[1]}",
                        "reservation_token": row[2],
                    },
                    stats=current_stats,
                    lease_minutes=LEASE_MINUTES,
                )
            state = "complete" if current_stats["completed"] == current_stats["total"] else "waiting"
            return jsonify(state=state, stats=current_stats)

    @app.post("/api/heartbeat")
    def heartbeat():
        body = request.get_json(silent=True) or {}
        image_id = body.get("image_id")
        token = body.get("reservation_token")
        worker_id = clean_worker_id(body.get("worker_id"))
        if not image_id or not token or not worker_id:
            return jsonify(error="Invalid reservation."), 400

        now = utcnow()
        with connect(app) as con:
            row = con.execute(
                """
                UPDATE images SET reserved_until = ?
                WHERE id = ? AND status = 'in_progress' AND reserved_by = ?
                      AND reservation_token = ? AND reserved_until > ?
                RETURNING id
                """,
                [now + timedelta(minutes=LEASE_MINUTES), image_id, worker_id, token, now],
            ).fetchone()
        if row is None:
            return jsonify(error="This reservation has expired."), 409
        return jsonify(ok=True)

    @app.post("/api/submit")
    def submit_annotation():
        body = request.get_json(silent=True) or {}
        worker_id = clean_worker_id(body.get("worker_id"))
        action = clean_answer(body.get("action"))
        reason = clean_answer(body.get("reason"))
        image_id = body.get("image_id")
        token = body.get("reservation_token")

        if not worker_id or not image_id or not token:
            return jsonify(error="Invalid submission."), 400
        if not action or not reason:
            return jsonify(error="Please answer both questions."), 400
        if len(action) > 5000 or len(reason) > 5000:
            return jsonify(error="Each answer must be under 5,000 characters."), 400

        now = utcnow()
        with connect(app) as con:
            con.execute("BEGIN TRANSACTION")
            try:
                image = con.execute(
                    """
                    SELECT id FROM images
                    WHERE id = ? AND status = 'in_progress' AND reserved_by = ?
                          AND reservation_token = ? AND reserved_until > ?
                    """,
                    [image_id, worker_id, token, now],
                ).fetchone()
                if image is None:
                    con.execute("ROLLBACK")
                    return jsonify(error="This reservation expired or was already submitted."), 409

                con.execute(
                    """
                    INSERT INTO annotations (image_id, action_answer, reason_answer, annotator_id, submitted_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [image_id, action, reason, worker_id, now],
                )
                con.execute(
                    """
                    UPDATE images
                    SET status = 'completed', completed_at = ?, reserved_until = NULL
                    WHERE id = ?
                    """,
                    [now, image_id],
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            return jsonify(ok=True, stats=get_stats(con))

    return app


def connect(app: Flask):
    return duckdb.connect(str(app.config["DB_PATH"]))


def initialize_database(app: Flask) -> None:
    image_dir = Path(app.config["IMAGE_DIR"])
    with connect(app) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                id BIGINT PRIMARY KEY,
                filename VARCHAR UNIQUE NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'pending',
                reserved_by VARCHAR,
                reservation_token VARCHAR,
                reserved_at TIMESTAMP,
                reserved_until TIMESTAMP,
                completed_at TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE SEQUENCE IF NOT EXISTS annotation_id_seq START 1;
            CREATE TABLE IF NOT EXISTS annotations (
                id BIGINT PRIMARY KEY DEFAULT nextval('annotation_id_seq'),
                image_id BIGINT NOT NULL UNIQUE,
                action_answer VARCHAR NOT NULL,
                reason_answer VARCHAR NOT NULL,
                annotator_id VARCHAR NOT NULL,
                submitted_at TIMESTAMP NOT NULL
            )
            """
        )

        con.execute(
            """
            CREATE SEQUENCE IF NOT EXISTS consent_id_seq START 1;
            CREATE TABLE IF NOT EXISTS consents (
                id BIGINT PRIMARY KEY DEFAULT nextval('consent_id_seq'),
                worker_id VARCHAR NOT NULL,
                consented_at TIMESTAMP NOT NULL
            )
            """
        )

        next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM images").fetchone()[0]
        known = {row[0] for row in con.execute("SELECT filename FROM images").fetchall()}
        files = sorted(
            p.relative_to(image_dir).as_posix()
            for p in image_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        new_rows = [(next_id + index, name) for index, name in enumerate(name for name in files if name not in known)]
        if new_rows:
            con.executemany("INSERT INTO images (id, filename) VALUES (?, ?)", new_rows)


def release_expired(con, now: Optional[datetime] = None) -> None:
    con.execute(
        """
        UPDATE images
        SET status = 'pending', reserved_by = NULL, reservation_token = NULL,
            reserved_at = NULL, reserved_until = NULL
        WHERE status = 'in_progress' AND reserved_until <= ?
        """,
        [now or utcnow()],
    )


def get_stats(con) -> Dict[str, int]:
    row = con.execute(
        """
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE status = 'completed'),
               COUNT(*) FILTER (WHERE status = 'pending'),
               COUNT(*) FILTER (WHERE status = 'in_progress')
        FROM images
        """
    ).fetchone()
    return {"total": row[0], "completed": row[1], "pending": row[2], "in_progress": row[3]}


def clean_worker_id(value) -> Optional[str]:
    if not isinstance(value, str) or not 8 <= len(value) <= 100:
        return None
    return value


def clean_answer(value) -> str:
    return value.strip() if isinstance(value, str) else ""


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
