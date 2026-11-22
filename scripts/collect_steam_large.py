"""Supervised large Steam collection, using the project's existing checkpoint."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
import time

from wukong_collect.cli import load_config, safe_error
from wukong_collect.common import utc_iso
from wukong_collect.http import ApiError, HttpClient
from wukong_collect.store import Store
from wukong_collect import steam


class StorageLimit(RuntimeError):
    """The latest page is committed and collection must stop for disk space."""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--batch-pages", type=int, default=1000)
    args = parser.parse_args()
    if args.batch_pages < 1:
        parser.error("--batch-pages must be positive")
    config = load_config(args.config)
    config["network"]["min_interval"] = 0.3
    config["network"]["retries"] = 4
    config["network"]["max_retry_wait"] = 60
    config["steam"]["max_pages"] = args.batch_pages
    progress_path = Path(config["collection"]["database"]).parent / "steam_large_progress.jsonl"
    started = last_emit = time.monotonic()
    http_statuses = Counter()

    class ProgressStore(Store):
        initial_count = 0
        initial_storage_bytes = 0
        pages_since_storage_check = 0

        def storage_bytes(self):
            return sum(path.stat().st_size for path in (
                self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")
            ) if path.exists())

        def check_storage(self):
            free = shutil.disk_usage(self.path.parent).free
            if free < 1.5 * 1024 ** 3:
                raise StorageLimit("磁盘可用空间低于1.5GiB；最新页及游标已原子保存，停止采集。")
            self.pages_since_storage_check = 0

        def report(self, event, *, force=False, detail=None):
            nonlocal last_emit
            now = time.monotonic()
            if not force and now - last_emit < 60:
                return
            count, earliest, latest = self.db.execute(
                "SELECT COUNT(*),MIN(created_at),MAX(created_at) FROM records WHERE platform='steam' AND kind='review'"
            ).fetchone()
            checkpoints = [json.loads(row[0]) for row in self.db.execute(
                "SELECT state FROM checkpoints WHERE key LIKE 'steam:%'"
            )]
            total = max((state.get("total_matching") or 0 for state in checkpoints), default=0)
            rate = (count - self.initial_count) / max(now - started, 0.001)
            size = self.storage_bytes()
            new_count = count - self.initial_count
            bytes_per_review = max(0, size - self.initial_storage_bytes) / new_count if new_count else None
            record = {"at": utc_iso(), "event": event, "count": count,
                      "new_since_start": count - self.initial_count,
                      "elapsed_seconds": round(now - started, 1),
                      "reviews_per_second": round(rate, 2), "earliest": earliest, "latest": latest,
                      "reported_matching": total,
                      "estimated_remaining_seconds": round(max(0, total - count) / rate) if rate else None,
                      "http_statuses": dict(http_statuses),
                      "free_disk_bytes": shutil.disk_usage(self.path.parent).free,
                      "database_and_wal_bytes": size,
                      "approx_bytes_per_new_review": round(bytes_per_review) if bytes_per_review else None,
                      "projected_remaining_bytes": round(max(0, total - count) * bytes_per_review) if bytes_per_review else None,
                      "pages_total": sum(state.get("pages_total", 0) for state in checkpoints),
                      "full_window_complete": bool(checkpoints) and all(state.get("done") for state in checkpoints)}
            if detail is not None:
                record["detail"] = detail
            print(json.dumps(record, ensure_ascii=False), flush=True)
            with progress_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            progress_path.chmod(0o600)
            last_emit = now

        def save_batch(self, records, checkpoint_key=None, state=None):
            super().save_batch(records, checkpoint_key, state)
            if checkpoint_key and checkpoint_key.startswith("steam:"):
                self.report("progress")
                self.pages_since_storage_check += 1
                if self.pages_since_storage_check >= 100:
                    self.check_storage()

    store = ProgressStore(config["collection"]["database"], config["collection"]["hash_key_file"])
    client = HttpClient(config)
    store.initial_count = store.db.execute("SELECT COUNT(*) FROM records WHERE platform='steam' AND kind='review'").fetchone()[0]
    store.initial_storage_bytes = store.storage_bytes()

    def response_hook(response, *unused_args, **unused_kwargs):
        http_statuses[response.status_code] += 1
        if response.status_code == 429:
            store.report("rate_limited", force=True, detail={"retry_after": response.headers.get("Retry-After")})

    client.session.hooks["response"].append(response_hook)
    try:
        store.report("started", force=True)
        batch = 0
        while True:
            batch += 1
            run_id = store.start_run("steam", config)
            try:
                store.check_storage()
                result = steam.run(config, store, client, max_pages=args.batch_pages)
                store.check_storage()
                store.finish_run(run_id, result["status"], result)
                store.report("batch_finished", force=True, detail={"batch": batch, "result": result})
            except StorageLimit as exc:
                detail = {"status": "storage_limit", "error": str(exc), "checkpoint_preserved": True}
                store.finish_run(run_id, "storage_limit", detail)
                store.report("storage_limit", force=True, detail=detail)
                break
            except KeyboardInterrupt:
                store.finish_run(run_id, "interrupted", {"checkpoint_preserved": True})
                store.report("interrupted", force=True)
                raise
            except Exception as exc:
                detail = {"error": safe_error(exc), "checkpoint_preserved": True}
                store.finish_run(run_id, "error", detail)
                store.report("error", force=True, detail=detail)
                raise
            if result["status"] == "complete":
                break
    finally:
        client.close()
        store.close()


if __name__ == "__main__":
    main()
