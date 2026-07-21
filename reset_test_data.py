"""
Wipes all annotations and consent records, and resets every image back to
'pending' so the survey starts from a clean slate.
Run with: python reset_test_data.py
On Railway, run: DB_PATH=/data/annotations.duckdb python reset_test_data.py
"""

import os
from pathlib import Path

import duckdb

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent / "annotations.duckdb"))

con = duckdb.connect(DB_PATH)

before = con.execute("SELECT count(*) FROM annotations").fetchone()[0]

con.execute("DELETE FROM annotations")
con.execute("DELETE FROM consents")
con.execute(
    """
    UPDATE images
    SET status = 'pending', reserved_by = NULL, reservation_token = NULL,
        reserved_at = NULL, reserved_until = NULL, completed_at = NULL
    """
)

after = con.execute("SELECT count(*) FROM annotations").fetchone()[0]
total_images = con.execute("SELECT count(*) FROM images").fetchone()[0]
pending_images = con.execute("SELECT count(*) FROM images WHERE status = 'pending'").fetchone()[0]

print(f"Deleted {before} annotation(s). Annotations remaining: {after}.")
print(f"Images reset to pending: {pending_images} of {total_images} total.")