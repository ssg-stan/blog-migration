import json
import uuid
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from app.services.validation import EDIT_FIELDS, fingerprint, slug_key, validate_post


def now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY,
                    hubspot_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    content TEXT NOT NULL,
                    meta_description TEXT NOT NULL,
                    featured_image TEXT NOT NULL,
                    featured_image_alt TEXT NOT NULL,
                    author TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    publish_date TEXT,
                    source_state TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    blog_id TEXT NOT NULL,
                    source_updated_at TEXT,
                    raw_json TEXT NOT NULL,
                    migration_status TEXT NOT NULL DEFAULT 'PENDING',
                    highlevel_id TEXT,
                    imported_at TEXT,
                    synced_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS posts_date ON posts(publish_date DESC);
                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    fetched INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL DEFAULT 0,
                    updated INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT ''
                );
            """)
            self._upgrade(conn)
            self._initialize_imports(conn)
            conn.execute("UPDATE sync_runs SET status='ERROR', finished_at=?, message=? WHERE status='RUNNING'",
                         (now(), "Sync interrupted by an application restart. Sync again to retry.",))

    def _upgrade(self, conn):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(posts)")}
        additions = {
            "local_edits": "TEXT NOT NULL DEFAULT '{}'",
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "validation_json": "TEXT NOT NULL DEFAULT '{}'",
            "validation_status": "TEXT NOT NULL DEFAULT 'UNCHECKED'",
            "link_report": "TEXT NOT NULL DEFAULT '{}'",
            "review_notes": "TEXT NOT NULL DEFAULT ''",
            "reviewed_at": "TEXT",
            "approved_at": "TEXT",
            "source_changed": "INTEGER NOT NULL DEFAULT 0",
            "import_state": "TEXT NOT NULL DEFAULT 'IDLE'",
            "import_message": "TEXT NOT NULL DEFAULT ''",
            "destination_location_id": "TEXT NOT NULL DEFAULT ''",
            "destination_blog_id": "TEXT NOT NULL DEFAULT ''",
        }
        if "local_edits" not in columns and conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]:
            backup = self.path.with_name(self.path.stem + ".phase1-backup-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f") + ".db")
            with sqlite3.connect(backup) as target:
                conn.backup(target)
        if "local_edits" in columns and "import_state" not in columns and conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]:
            backup = self.path.with_name(self.path.stem + ".phase2-backup-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f") + ".db")
            with sqlite3.connect(backup) as target:
                conn.backup(target)
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {name} {definition}")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS review_events (
                id INTEGER PRIMARY KEY,
                post_id INTEGER NOT NULL REFERENCES posts(id),
                action TEXT NOT NULL,
                revision INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS post_revisions (
                id INTEGER PRIMARY KEY,
                post_id INTEGER NOT NULL REFERENCES posts(id),
                revision INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        self._revalidate(conn)

    def _revalidate(self, conn):
        rows = conn.execute("SELECT * FROM posts").fetchall()
        posts = [self._post(row) for row in rows]
        counts = {}
        for post in posts:
            key = slug_key(post["slug"])
            counts[key] = counts.get(key, 0) + 1
        for post in posts:
            result = validate_post(post, counts.get(slug_key(post["slug"]), 0) > 1)
            conn.execute("UPDATE posts SET validation_json=?, validation_status=? WHERE id=?",
                         (json.dumps(result), result["status"], post["id"]))
            if result["errors"] and post["migration_status"] == "APPROVED" and not post["highlevel_id"]:
                conn.execute("UPDATE posts SET migration_status='PENDING', approved_at=NULL WHERE id=?", (post["id"],))
                self._event(conn, post["id"], "APPROVAL_CLEARED", post["revision"], "Validation errors require another review.")

    @staticmethod
    def _event(conn, post_id, action, revision, notes=""):
        conn.execute("INSERT INTO review_events(post_id,action,revision,notes,created_at) VALUES(?,?,?,?,?)",
                     (post_id, action, revision, notes, now()))

    def start_sync(self):
        with self.connect() as conn:
            return conn.execute("INSERT INTO sync_runs(status, started_at) VALUES('RUNNING',?)", (now(),)).lastrowid

    def progress(self, run_id, fetched):
        with self.connect() as conn:
            conn.execute("UPDATE sync_runs SET fetched=? WHERE id=?", (fetched, run_id))

    def finish_error(self, run_id, message):
        with self.connect() as conn:
            conn.execute("UPDATE sync_runs SET status='ERROR', finished_at=?, message=? WHERE id=?",
                         (now(), message, run_id))

    def save_snapshot(self, posts, run_id):
        # Commit the entire successful fetch together: an API failure never leaves a partial sync.
        created = updated = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for post in posts:
                busy = conn.execute("SELECT import_state FROM posts WHERE hubspot_id=?", (post["hubspot_id"],)).fetchone()
                if busy and busy["import_state"] in {"PREPARING", "SENDING"}:
                    raise ValueError("A post is being imported. Wait for its import to finish before syncing.")
                exists = conn.execute("SELECT * FROM posts WHERE hubspot_id=?", (post["hubspot_id"],)).fetchone()
                columns = list(post)
                values = [post[key] for key in columns]
                updates = ",".join(f"{key}=excluded.{key}" for key in columns if key != "hubspot_id")
                conn.execute(f"INSERT INTO posts ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                             f"ON CONFLICT(hubspot_id) DO UPDATE SET {updates}", values)
                if exists:
                    changed = any(exists[key] != post[key] for key in (*EDIT_FIELDS, "source_url") if key in post)
                    if changed:
                        conn.execute("UPDATE posts SET revision=revision+1, link_report='{}', source_changed=? WHERE id=?",
                                     (int(bool(json.loads(exists["local_edits"]))), exists["id"]))
                        if not exists["highlevel_id"]:
                            conn.execute("UPDATE posts SET migration_status='PENDING', approved_at=NULL WHERE id=?", (exists["id"],))
                        self._event(conn, exists["id"], "SOURCE_REFRESHED", exists["revision"] + 1, "Source changed; local edits were preserved.")
                created += not bool(exists)
                updated += bool(exists)
            self._revalidate(conn)
            conn.execute("UPDATE sync_runs SET status='SUCCESS', finished_at=?, fetched=?, created=?, updated=?, message=? WHERE id=?",
                         (now(), len(posts), created, updated,
                          f"Synced {len(posts)} posts: {created} new, {updated} refreshed.", run_id))
        return created, updated

    def latest_sync(self):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def sync_history(self, limit=5):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def stats(self):
        with self.connect() as conn:
            row = conn.execute("""SELECT COUNT(*) AS total,
                SUM(source_state='PUBLISHED') AS published,
                SUM(source_state='DRAFT') AS drafts,
                SUM(source_state='SCHEDULED') AS scheduled FROM posts""").fetchone()
            return {key: row[key] or 0 for key in row.keys()}

    def list_posts(self, query="", state="", page=1, page_size=20, review="", validation=""):

        clauses, args = [], []
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(COALESCE(json_extract(local_edits, '$.title'), title) LIKE ? ESCAPE '\\' OR COALESCE(json_extract(local_edits, '$.author'), author) LIKE ? ESCAPE '\\' OR COALESCE(json_extract(local_edits, '$.slug'), slug) LIKE ? ESCAPE '\\')")
            args.extend([f"%{escaped}%"] * 3)
        if state:
            clauses.append("source_state=?")
            args.append(state)
        if review:
            clauses.append("migration_status=?")
            args.append(review)
        if validation:
            clauses.append("validation_status=?")
            args.append(validation)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM posts" + where, args).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(max(1, page), pages)
            rows = conn.execute("SELECT * FROM posts" + where +
                                " ORDER BY publish_date DESC, id DESC LIMIT ? OFFSET ?",
                                args + [page_size, (page - 1) * page_size]).fetchall()
            return [self._post(row) for row in rows], total, page, pages

    def get_post(self, post_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            return self._post(row) if row else None

    def review_stats(self):
        with self.connect() as conn:
            row = conn.execute("""SELECT SUM(migration_status='PENDING') AS pending,
                SUM(migration_status='APPROVED') AS approved, SUM(migration_status='REJECTED') AS rejected,
                SUM(validation_status='ERROR') AS errors, SUM(validation_status='WARNING') AS warnings,
                SUM(validation_status='READY') AS ready, SUM(migration_status='IMPORTED') AS imported FROM posts""").fetchone()
            return {key: row[key] or 0 for key in row.keys()}

    def review_history(self, post_id):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM review_events WHERE post_id=? ORDER BY id DESC LIMIT 10", (post_id,))]

    def _editable(self, conn, post_id, revision):
        row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row:
            raise LookupError("Post not found.")
        if row["import_state"] in {"PREPARING", "SENDING", "UNKNOWN"}:
            raise ValueError("This post has an active or uncertain import. Check the import result before editing or reviewing.")
        if row["highlevel_id"] or row["migration_status"] == "IMPORTED":
            raise ValueError("Imported posts cannot be edited in this local review workflow.")
        if row["revision"] != revision:
            raise ValueError("This post changed in another tab or during a sync. Reload before saving or reviewing.")
        return row

    def edit_post(self, post_id, revision, fields, notes=""):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._editable(conn, post_id, revision)
            old = self._post(row)
            overrides = {}
            for key in EDIT_FIELDS:
                value = fields[key]
                source = old["source"].get(key, [] if key == "categories" else None)
                if value != source:
                    overrides[key] = value
            changed = any(old.get(key) != fields[key] for key in EDIT_FIELDS)
            if changed:
                conn.execute("INSERT INTO post_revisions(post_id,revision,snapshot_json,created_at) VALUES(?,?,?,?)",
                             (post_id, revision, json.dumps({key: old.get(key) for key in EDIT_FIELDS}), now()))
                conn.execute("UPDATE posts SET local_edits=?, revision=revision+1, migration_status='PENDING', approved_at=NULL, link_report='{}', review_notes=?, source_changed=? WHERE id=?",
                             (json.dumps(overrides, ensure_ascii=False), notes, row["source_changed"] if overrides else 0, post_id))
                self._event(conn, post_id, "EDITED", revision + 1, notes)
            elif notes != row["review_notes"]:
                conn.execute("UPDATE posts SET review_notes=?, revision=revision+1 WHERE id=?", (notes, post_id))
                self._event(conn, post_id, "NOTES_UPDATED", revision + 1, notes)
            self._revalidate(conn)
        return self.get_post(post_id)

    def review_post(self, post_id, revision, action, acknowledge=False, notes=""):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._editable(conn, post_id, revision)
            self._revalidate(conn)
            post = self._post(conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone())
            if action == "APPROVED":
                if post["validation"]["errors"]:
                    raise ValueError("Resolve validation errors before approving this post.")
                if post["validation"]["warnings"] and not acknowledge:
                    raise ValueError("Acknowledge the validation warnings before approving this post.")
            if action not in {"APPROVED", "REJECTED", "PENDING"}:
                raise ValueError("Invalid review action.")
            conn.execute("UPDATE posts SET migration_status=?, reviewed_at=?, approved_at=?, review_notes=?, revision=revision+1 WHERE id=?",
                         (action, now(), now() if action == "APPROVED" else None, notes, post_id))
            if action == "APPROVED":
                conn.execute("UPDATE posts SET source_changed=0 WHERE id=?", (post_id,))
                self._revalidate(conn)
            self._event(conn, post_id, action, revision + 1, notes)
        return self.get_post(post_id)

    def reset_edits(self, post_id, revision):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._editable(conn, post_id, revision)
            post = self._post(row)
            conn.execute("INSERT INTO post_revisions(post_id,revision,snapshot_json,created_at) VALUES(?,?,?,?)",
                         (post_id, revision, json.dumps({key: post.get(key) for key in EDIT_FIELDS}), now()))
            conn.execute("UPDATE posts SET local_edits='{}', source_changed=0, revision=revision+1, migration_status='PENDING', approved_at=NULL, link_report='{}' WHERE id=?", (post_id,))
            self._event(conn, post_id, "RESET_TO_SOURCE", revision + 1)
            self._revalidate(conn)
        return self.get_post(post_id)

    def save_link_report(self, post_id, revision, report):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            if not row or row["import_state"] not in {"IDLE", "ERROR"} or row["highlevel_id"] or row["revision"] != revision or fingerprint(self._post(row)) != report["fingerprint"]:
                return False
            conn.execute("UPDATE posts SET link_report=?, revision=revision+1 WHERE id=?", (json.dumps(report), post_id))
            self._event(conn, post_id, "URL_CHECKED", revision + 1)
            self._revalidate(conn)
        return True

    @staticmethod
    def _post(row):
        post = dict(row)
        post["tags"] = json.loads(post["tags"])
        source = {key: post.get(key) for key in EDIT_FIELDS if key != "categories"}
        source["categories"] = []
        source["source_url"] = post["source_url"]
        post["source"] = source
        post["local_edits"] = json.loads(post.get("local_edits", "{}"))
        post["categories"] = []
        post.update(post["local_edits"])
        post["validation"] = json.loads(post.get("validation_json", "{}"))
        post["link_report"] = json.loads(post.get("link_report", "{}"))
        return post

    def _initialize_imports(self, conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS import_attempts (
                id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                post_id INTEGER NOT NULL REFERENCES posts(id), revision INTEGER NOT NULL,
                status TEXT NOT NULL, simulated INTEGER NOT NULL,
                location_id TEXT NOT NULL, blog_id TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}', destination_id TEXT,
                message TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_import ON import_attempts((1))
                WHERE status IN ('PREPARING','SENDING');
        """)
        for attempt in conn.execute("SELECT * FROM import_attempts WHERE status IN ('PREPARING','SENDING')").fetchall():
            status = "UNKNOWN" if attempt["status"] == "SENDING" else "ERROR"
            message = ("Application restarted during creation. Check the destination; automatic retry is blocked."
                       if status == "UNKNOWN" else "Preparation was interrupted. No create request was sent; you can retry.")
            conn.execute("UPDATE import_attempts SET status=?, message=?, finished_at=? WHERE id=?", (status, message, now(), attempt["id"]))
            conn.execute("UPDATE posts SET import_state=?, import_message=?, revision=revision+1 WHERE id=?", (status, message, attempt["post_id"]))
            self._event(conn, attempt["post_id"], "IMPORT_" + status, attempt["revision"] + 1, message)

    def start_import(self, post_id, revision, location_id, blog_id, simulated):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._editable(conn, post_id, revision)
            self._revalidate(conn)
            post = self._post(conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone())
            if post["migration_status"] != "APPROVED" or post["validation"]["errors"]:
                raise ValueError("Only an approved post without validation errors can be imported.")
            if conn.execute("SELECT 1 FROM import_attempts WHERE status IN ('PREPARING','SENDING')").fetchone():
                raise ValueError("Another post is being imported. Wait for it to finish.")
            operation = uuid.uuid4().hex
            conn.execute("UPDATE posts SET import_state='PREPARING', import_message='Preparing draft…', revision=revision+1 WHERE id=?", (post_id,))
            attempt_id = conn.execute("INSERT INTO import_attempts(operation_id,post_id,revision,status,simulated,location_id,blog_id,started_at) VALUES(?,?,?,'PREPARING',?,?,?,?)",
                (operation, post_id, revision + 1, int(simulated), location_id, blog_id, now())).lastrowid
            self._event(conn, post_id, "IMPORT_STARTED", revision + 1, "Simulated draft" if simulated else "the destination draft")
            conn.execute("INSERT INTO post_revisions(post_id,revision,snapshot_json,created_at) VALUES(?,?,?,?)",
                (post_id, revision + 1, json.dumps({**{key: post.get(key) for key in EDIT_FIELDS}, "source_url": post["source_url"]}, ensure_ascii=False), now()))
            post["revision"] = revision + 1
            return attempt_id, operation, post

    def sending_import(self, attempt_id, payload):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute("SELECT * FROM import_attempts WHERE id=?", (attempt_id,)).fetchone()
            row = conn.execute("SELECT * FROM posts WHERE id=?", (attempt["post_id"],)).fetchone()
            self._revalidate(conn)
            post = self._post(conn.execute("SELECT * FROM posts WHERE id=?", (attempt["post_id"],)).fetchone())
            if attempt["status"] != "PREPARING" or row["revision"] != attempt["revision"] or post["migration_status"] != "APPROVED" or post["validation"]["errors"]:
                raise ValueError("The approved copy changed while preparing. No draft was created.")
            duplicate = conn.execute("""SELECT 1 FROM import_attempts WHERE id<>? AND location_id=?
                AND simulated=? AND status IN ('SUCCESS','UNKNOWN','SENDING')
                AND json_extract(payload_json,'$.urlSlug')=?""",
                (attempt_id, attempt["location_id"], attempt["simulated"], payload["urlSlug"])).fetchone()
            if duplicate:
                raise ValueError("This destination slug already has a completed or uncertain local import. Check its result first.")
            conn.execute("UPDATE import_attempts SET status='SENDING', payload_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), attempt_id))
            conn.execute("UPDATE posts SET import_state='SENDING', import_message='Creating draft…' WHERE id=?", (attempt["post_id"],))

    def finish_import(self, attempt_id, state, message, destination_id=None):
        if state not in {"SUCCESS", "ERROR", "UNKNOWN"}:
            raise ValueError("Invalid import result.")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute("SELECT * FROM import_attempts WHERE id=?", (attempt_id,)).fetchone()
            if not attempt or attempt["status"] not in {"PREPARING", "SENDING"}:
                raise ValueError("Import is no longer active.")
            conn.execute("UPDATE import_attempts SET status=?, message=?, destination_id=?, finished_at=? WHERE id=?",
                (state, message, destination_id, now(), attempt_id))
            conn.execute("UPDATE posts SET import_state=?, import_message=?, revision=revision+1 WHERE id=?", (state, message, attempt["post_id"]))
            if state == "SUCCESS":
                if not destination_id:
                    raise ValueError("A successful import must include a destination ID.")
                conn.execute("UPDATE posts SET highlevel_id=?, migration_status='IMPORTED', imported_at=?, destination_location_id=?, destination_blog_id=? WHERE id=?",
                    (destination_id, now(), attempt["location_id"], attempt["blog_id"], attempt["post_id"]))
            self._event(conn, attempt["post_id"], "IMPORT_" + state, attempt["revision"] + 1, message)
        return self.get_post(attempt["post_id"])

    def latest_import(self, post_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM import_attempts WHERE post_id=? ORDER BY id DESC LIMIT 1", (post_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["payload"] = json.loads(result.pop("payload_json"))
            snapshot = conn.execute("SELECT snapshot_json FROM post_revisions WHERE post_id=? AND revision=? ORDER BY id DESC LIMIT 1",
                (post_id, result["revision"])).fetchone()
            result["approved_copy"] = json.loads(snapshot[0]) if snapshot else {}
            return result
