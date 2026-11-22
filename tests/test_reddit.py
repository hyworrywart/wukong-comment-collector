from __future__ import annotations

import json
from copy import deepcopy

import pytest

from wukong_collect import reddit


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.states = {}

    def author_hash(self, platform, value):
        return "hashed:" + value

    def save_batch(self, records, checkpoint_key=None, state=None):
        for record in records:
            self.records[(record["kind"], record["native_id"])] = deepcopy(record)
        if checkpoint_key:
            self.states[checkpoint_key] = deepcopy(state)

    def get_state(self, key):
        return deepcopy(self.states.get(key))

    def set_state(self, key, state):
        self.states[key] = deepcopy(state)


def config():
    return {
        "collection": {"start": "2024-08-20", "end": "2025-01-01"},
        "reddit": {
            "mode": "import", "subreddits": ["BlackMythWukong"], "keywords": [],
            "page_size": 2, "timestamp_format": "epoch_seconds",
            "fields": {"native_id": "id", "parent_id": "parent", "root_id": "post", "text": "body", "created_at": "created", "subreddit": "sub", "author_hash": "hashed_user", "score": "score"},
            "post_fields": {"native_id": "id", "text": "body", "created_at": "created", "subreddit": "sub", "title": "title"},
        },
    }


def row(index=1):
    return {"id": f"c{index}", "parent": "t3_p1" if index == 1 else "t1_c1", "post": "t3_p1", "body": "Beautiful architecture", "created": 1724200000, "sub": "BlackMythWukong", "hashed_user": "already-hashed", "score": 7}


def write_rows(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_import_resume_preserves_all_comments_without_duplicates(tmp_path):
    cfg, store = config(), MemoryStore()
    path = tmp_path / "comments.jsonl"
    write_rows(path, [row(1), row(2), row(3)])
    cfg["reddit"]["import_files"] = [str(path)]
    first = reddit.run(cfg, store, max_pages=1)
    assert first["status"] == "partial"
    assert len(store.records) == 2
    second = reddit.run(cfg, store)
    assert second["status"] == "complete"
    assert len(store.records) == 3
    assert reddit.run(cfg, store)["saved"] == 0
    reply = store.records[("comment", "t1_c2")]
    assert reply["parent_id"] == "t1_c1" and reply["root_id"] == "t3_p1"
    assert reply["author_hash"] == "hashed:already-hashed"
    assert reply["metadata"]["score"] == 7 and reply["like_count"] is None
    assert "hashed_user" not in reply and "author" not in reply


def test_import_changed_file_starts_new_idempotent_pass(tmp_path):
    cfg, store = config(), MemoryStore()
    path = tmp_path / "comments.jsonl"
    write_rows(path, [row(1)])
    cfg["reddit"]["import_files"] = [str(path)]
    reddit.run(cfg, store)
    write_rows(path, [row(1), row(2)])
    assert reddit.run(cfg, store)["saved"] == 2
    assert len(store.records) == 2


def test_missing_mapped_column_fails_with_position(tmp_path):
    cfg, store = config(), MemoryStore()
    path = tmp_path / "comments.jsonl"
    value = row()
    del value["post"]
    write_rows(path, [value])
    cfg["reddit"]["import_files"] = [str(path)]
    with pytest.raises(reddit.RedditConfigError, match="记录 1"):
        reddit.run(cfg, store)
    assert not store.records and not store.states


def test_keyword_thread_filter_keeps_keyword_free_reply(tmp_path):
    cfg, store = config(), MemoryStore()
    posts, comments = tmp_path / "posts.jsonl", tmp_path / "comments.jsonl"
    post = {"id": "p1", "body": "", "title": "Black Myth Wukong cultural references", "created": 1724200000, "sub": "BlackMythWukong"}
    write_rows(posts, [post])
    unrelated = row(3)
    unrelated["post"] = "t3_other"
    write_rows(comments, [row(1), row(2), unrelated])
    cfg["reddit"].update(keywords=["wukong"], keyword_scope="thread", import_files=[{"path": str(posts), "kind": "post"}, str(comments)])
    reddit.run(cfg, store)
    assert ("comment", "t1_c2") in store.records
    assert ("comment", "t1_c3") not in store.records
    assert ("post", "t3_p1") in store.records


def test_changing_seed_post_rechecks_completed_comment_import(tmp_path):
    cfg, store = config(), MemoryStore()
    posts, comments = tmp_path / "posts.jsonl", tmp_path / "comments.jsonl"
    post = {"id": "p1", "body": "", "title": "Some unrelated title", "created": 1724200000, "sub": "BlackMythWukong"}
    write_rows(posts, [post])
    write_rows(comments, [row(1), row(2)])
    cfg["reddit"].update(keywords=["wukong"], import_files=[{"path": str(posts), "kind": "post"}, str(comments)])
    reddit.run(cfg, store)
    assert not store.records
    post["title"] = "Wukong"
    write_rows(posts, [post])
    reddit.run(cfg, store)
    assert ("comment", "t1_c2") in store.records


def test_filter_excludes_removed_and_out_of_window_records():
    cfg, store = config(), MemoryStore()
    removed = row()
    removed["body"] = "[removed]"
    assert reddit.normalize(removed, "comment", cfg, store) is None
    outside = row()
    outside["created"] = 1
    assert reddit.normalize(outside, "comment", cfg, store) is None


def test_query_is_parameterized_and_replies_are_selected_via_post():
    cfg = config()
    cfg["reddit"].update(comments_table="approved.dataset.comments", posts_table="approved.dataset.posts", keywords=["wukong' OR TRUE --"])
    query = reddit.build_query(cfg)
    assert "wukong'" not in query["sql"]
    assert query["parameters"]["keywords"] == ["wukong' or true --"]
    assert "FROM `approved.dataset.posts` p" in query["sql"]
    assert "p.`created`" not in query["sql"]  # old posts can have new comments
    cfg["reddit"]["comments_table"] = "foo`; DELETE FROM bar; --"
    with pytest.raises(reddit.RedditConfigError):
        reddit.build_query(cfg)
    cfg["reddit"]["comments_table"] = "approved.dataset.comments"
    cfg["reddit"]["fields"]["evil`; DELETE FROM x --"] = "body"
    with pytest.raises(reddit.RedditConfigError, match="未知规范字段"):
        reddit.build_query(cfg)


def test_bigquery_resume_reuses_job_and_row_offset(monkeypatch):
    cfg, store = config(), MemoryStore()
    cfg["reddit"].update(mode="bigquery", project="approved", comments_table="approved.dataset.comments")
    values = [{key: reddit._get(row(i), path) for key, path in cfg["reddit"]["fields"].items()} for i in (1, 2, 3)]

    class Rows:
        def __init__(self, start):
            self.start = start
            self.next_page_token = None

        @property
        def pages(self):
            for start in range(self.start, len(values), 2):
                self.next_page_token = "next" if start + 2 < len(values) else None
                yield values[start:start + 2]

    class Job:
        job_id = "stable-approved-job"
        calls = []

        def result(self, **kwargs):
            self.calls.append(kwargs)
            return Rows(kwargs["start_index"])

    job = Job()

    class Client:
        queries = 0
        gets = 0

        def query(self, *args, **kwargs):
            self.queries += 1
            return job

        def get_job(self, *args, **kwargs):
            self.gets += 1
            return job

        def close(self):
            pass

    client = Client()
    monkeypatch.setattr(reddit, "_client", lambda c: client)
    monkeypatch.setattr(reddit, "_query_config", lambda *args: None)
    monkeypatch.setattr(reddit, "_validate_schema", lambda *args: None)
    assert reddit.run(cfg, store, max_pages=1)["status"] == "partial"
    assert reddit.run(cfg, store)["status"] == "complete"
    assert client.queries == 1 and client.gets == 1
    assert [c["start_index"] for c in job.calls] == [0, 2]
    assert len(store.records) == 3


def test_no_public_json_or_oauth_fallback():
    cfg = config()
    cfg["reddit"]["mode"] = "oauth"
    with pytest.raises(reddit.RedditConfigError, match="RFR"):
        reddit.run(cfg, MemoryStore())
