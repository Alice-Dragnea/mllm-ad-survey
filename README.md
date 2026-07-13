# Advertisement Annotation Survey

A shared web survey that shows advertisements one at a time, asks two questions, and saves all responses in DuckDB. Images are reserved while someone annotates them, preventing two people from doing the same image. Abandoned reservations return to the queue after 30 minutes.

## 1. Add your images

Put all advertisement images in the `images/` folder. Subfolders are supported. Accepted formats are JPG, JPEG, PNG, WebP, GIF, BMP, and TIFF.

Images are discovered when the server starts. Existing progress is preserved if more images are added later.

## 2. Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://localhost:5000>. The server listens on all network interfaces, so people on the same network can use `http://YOUR-COMPUTER-IP:5000`.

## 3. Deploy for annotators anywhere

Deploy this folder to a service with **persistent disk storage** (a VPS, Railway volume, Render disk, Fly volume, etc.). Then start it with:

```bash
gunicorn --workers 1 --threads 8 --bind 0.0.0.0:${PORT:-5000} app:app
```

Use exactly one Gunicorn worker. DuckDB supports concurrent threads in one process, while multiple web-server processes should not write to the same database file. Keep both `annotations.duckdb` and the `images/` directory on persistent storage.

A `Dockerfile` is included if your hosting service deploys containers. Mount persistent storage at a location such as `/data`, then set `DB_PATH=/data/annotations.duckdb` and `IMAGE_DIR=/data/images`.

Optional environment variables:

- `IMAGE_DIR`: absolute path to the image folder
- `DB_PATH`: absolute path for `annotations.duckdb`
- `LEASE_MINUTES`: reservation duration (default: `30`)
- `PORT`: web server port (default: `5000`)

## Data

Results are stored in `annotations.duckdb`:

- `images`: filename and queue status (`pending`, `in_progress`, or `completed`)
- `annotations`: the two answers, anonymized browser ID, and submission time

Example export to CSV:

```bash
python -c "import duckdb; duckdb.connect('annotations.duckdb').execute(\"COPY (SELECT i.filename, a.action_answer, a.reason_answer, a.submitted_at FROM annotations a JOIN images i ON i.id=a.image_id ORDER BY i.id) TO 'annotations.csv' (HEADER, DELIMITER ',')\")"
```

Back up `annotations.duckdb` regularly while collecting responses.
