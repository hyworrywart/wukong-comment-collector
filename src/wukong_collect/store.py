from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile

from .common import utc_iso

FIELDS = ["platform", "kind", "native_id", "parent_id", "root_id", "author_hash", "text", "created_at", "updated_at", "language", "like_count", "reply_count", "url", "metadata", "first_collected_at", "collected_at"]


class Store:
    def __init__(self, database, key_file):
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key_path = Path(key_file)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            try:
                fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(secrets.token_bytes(32))
            except FileExistsError:
                pass
        self.key = key_path.read_bytes()
        if len(self.key) < 32:
            raise ValueError("作者 HMAC 密钥不足 32 字节")
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=30)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS records (
                platform TEXT NOT NULL, kind TEXT NOT NULL, native_id TEXT NOT NULL,
                parent_id TEXT, root_id TEXT, author_hash TEXT, text TEXT,
                created_at TEXT, updated_at TEXT, language TEXT,
                like_count INTEGER, reply_count INTEGER, url TEXT,
                metadata TEXT NOT NULL, first_collected_at TEXT NOT NULL, collected_at TEXT NOT NULL,
                PRIMARY KEY(platform,kind,native_id));
            CREATE INDEX IF NOT EXISTS idx_records_created ON records(platform,created_at);
            CREATE INDEX IF NOT EXISTS idx_records_parent ON records(platform,parent_id);
            CREATE TABLE IF NOT EXISTS checkpoints (key TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, platform TEXT, started_at TEXT, ended_at TEXT,
                status TEXT, config TEXT, result TEXT);
        """)
        fingerprint = hashlib.sha256(self.key).hexdigest()
        old = self.db.execute("SELECT value FROM settings WHERE key='hmac_fingerprint'").fetchone()
        if old and old[0] != fingerprint:
            self.close()
            raise ValueError("HMAC 密钥与数据库不匹配；请恢复原 data/.author_hmac.key")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('hmac_fingerprint',?)", (fingerprint,))

    def close(self):
        self.db.close()

    def author_hash(self, platform, raw_id):
        if raw_id in (None, "", "[deleted]", "[removed]"):
            return None
        return hmac.new(self.key, f"{platform}:{raw_id}".encode(), hashlib.sha256).hexdigest()

    def save_batch(self, records, checkpoint_key=None, state=None):
        now = utc_iso()
        with self.db:
            for rec in records:
                if not rec.get("platform") or not rec.get("kind") or not rec.get("native_id"):
                    raise ValueError("记录必须含 platform/kind/native_id")
                row = {k: rec.get(k) for k in FIELDS}
                row["native_id"] = str(row["native_id"])
                for key in ("created_at", "updated_at"):
                    if row[key] not in (None, ""):
                        row[key] = utc_iso(row[key])
                row["metadata"] = json.dumps(row["metadata"] or {}, ensure_ascii=False, default=str)
                row["first_collected_at"] = now
                row["collected_at"] = now
                cols = ",".join(FIELDS)
                assignments = ",".join(f"{k}=excluded.{k}" for k in FIELDS if k not in ("platform", "kind", "native_id", "first_collected_at"))
                self.db.execute(f"INSERT INTO records ({cols}) VALUES ({','.join('?' for _ in FIELDS)}) ON CONFLICT(platform,kind,native_id) DO UPDATE SET {assignments}", [row[k] for k in FIELDS])
            if checkpoint_key is not None:
                self.db.execute("INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state", (checkpoint_key, json.dumps(state or {}, ensure_ascii=False, default=str)))

    def get_state(self, key):
        row = self.db.execute("SELECT state FROM checkpoints WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_state(self, key, state):
        self.save_batch([], key, state)

    def iter_records(self, platform=None, kind=None):
        clauses, args = [], []
        for key, val in (("platform", platform), ("kind", kind)):
            if val:
                clauses.append(key + "=?")
                args.append(val)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        for row in self.db.execute("SELECT * FROM records" + where + " ORDER BY platform,kind,native_id", args):
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            yield item

    def summary(self):
        return {"counts": [dict(r) for r in self.db.execute("SELECT platform,kind,COUNT(*) AS count,MIN(created_at) AS earliest,MAX(created_at) AS latest FROM records GROUP BY platform,kind")],
                "checkpoints": [{"key": r[0], "state": json.loads(r[1])} for r in self.db.execute("SELECT * FROM checkpoints")],
                "recent_runs": [dict(r) for r in self.db.execute("SELECT id,platform,started_at,ended_at,status FROM runs ORDER BY id DESC LIMIT 10")]}

    def start_run(self, platform, config):
        # Config contains acquisition parameters only. Secrets live in .env.
        with self.db:
            cur = self.db.execute("INSERT INTO runs(platform,started_at,status,config) VALUES (?,?,'running',?)", (platform, utc_iso(), json.dumps(config, ensure_ascii=False, default=str)))
            return cur.lastrowid

    def finish_run(self, run_id, status, result):
        with self.db:
            self.db.execute("UPDATE runs SET ended_at=?,status=?,result=? WHERE id=?", (utc_iso(), status, json.dumps(result, ensure_ascii=False, default=str), run_id))

    def export(self, path, fmt="jsonl", platform=None, kind=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        fd, temp = tempfile.mkstemp(prefix=".export-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8-sig" if fmt == "csv" else "utf-8", newline="") as f:
                count = self._write_export(f, fmt, platform, kind)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return count

    def _write_export(self, f, fmt, platform, kind):
        count = 0
        writer = csv.DictWriter(f, fieldnames=FIELDS) if fmt == "csv" else None
        if writer:
            writer.writeheader()
        for rec in self.iter_records(platform, kind):
            if writer:
                rec["metadata"] = json.dumps(rec["metadata"], ensure_ascii=False)
                # CSV intended for spreadsheets: prevent formula execution.
                rec = {k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in rec.items()}
                writer.writerow(rec)
            else:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1
        return count

    def purge_stale(self, platform, before):
        with self.db:
            cur = self.db.execute("DELETE FROM records WHERE platform=? AND collected_at<?", (platform, utc_iso(before)))
            # A future collect must traverse again instead of skipping completed jobs.
            self.db.execute("DELETE FROM checkpoints WHERE key LIKE ? OR key LIKE ? ESCAPE '\\' OR key LIKE ?", (platform + ":%", platform + "\\_%:%", platform + "-%:%"))
            return cur.rowcount
