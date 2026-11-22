import json
from pathlib import Path

import pytest

from wukong_collect.common import in_window, utc_iso
from wukong_collect.http import ApiError, HttpClient
from wukong_collect.store import Store
from wukong_collect import steam


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data.sqlite3", tmp_path / "key")
    yield s
    s.close()


def review(ident, created="2024-08-20T00:00:00Z"):
    from datetime import datetime
    return {"recommendationid": ident, "timestamp_created": int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()),
            "timestamp_updated": 1724112000, "review": "中文文化 comment", "language": "english", "voted_up": True,
            "author": {"steamid": "private-account-123", "playtime_at_review": 50}, "votes_up": 2, "comment_count": 1}


class Pages:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, json.loads(params["input_json"])))
        return {"response": self.pages.pop(0)}


def config():
    return {"collection": {"start": "2024-08-19", "end": "2024-08-22"}, "steam": {"languages": ["all"], "page_size": 100, "max_pages": 1}}


def test_utc_bounds_and_equivalent_offsets():
    assert utc_iso("2024-08-20T08:00:00+08:00") == utc_iso("2024-08-20T00:00:00Z")
    assert in_window("2024-08-19", config())
    assert not in_window("2024-08-22", config())


def test_steam_resume_atomic_and_no_raw_author(store):
    one = Pages([{"reviews": [review("r1")], "cursor": "a+/=", "query_summary": {"total_reviews": 3}}])
    assert steam.run(config(), store, one)["status"] == "partial"
    assert one.calls[0][0].startswith("https://api.steampowered.com/IUserReviewsService")
    assert one.calls[0][1]["filter"] == 1
    assert one.calls[0][1]["purchase_type"] == 1
    assert one.calls[0][1]["date_range_end"] == 1724284799
    two = Pages([{"reviews": [review("r2")], "cursor": "b"}, {"reviews": [], "cursor": "b"}])
    assert steam.run(config(), store, two, max_pages=2)["status"] == "complete"
    assert two.calls[0][1]["cursor"] == "a+/="
    records = list(store.iter_records())
    assert len(records) == 2
    assert "private-account-123" not in json.dumps(records)
    assert records[0]["author_hash"] == records[1]["author_hash"]
    assert steam.run(config(), store, Pages([]))["status"] == "complete"
    replay = Pages([{"reviews": [review("r1")], "cursor": "again"}])
    steam.run(config(), store, replay, restart=True)
    assert len(list(store.iter_records())) == 2


def test_steam_accepts_explicitly_empty_page_without_reviews(store):
    page = Pages([{"total_matching": 0, "query_summary": {"num_reviews": 0}}])
    assert steam.run(config(), store, page)["status"] == "complete"
    assert not list(store.iter_records())


def test_steam_missing_reviews_without_empty_indicator_preserves_checkpoint(store):
    steam.run(config(), store, Pages([{"reviews": [review("r1")], "cursor": "next"}]))
    with pytest.raises(ApiError, match="响应结构"):
        steam.run(config(), store, Pages([{"cursor": "new", "total_matching": 3}]))
    tail = Pages([{"query_summary": {"num_reviews": 0}}])
    assert steam.run(config(), store, tail)["status"] == "complete"
    assert tail.calls[0][1]["cursor"] == "next"


def test_stalled_cursor_does_not_advance(store):
    p = Pages([{"reviews": [review("r1")], "cursor": "*"}])
    with pytest.raises(ApiError, match="游标"):
        steam.run(config(), store, p)
    assert len(list(store.iter_records())) == 1
    assert store.summary()["checkpoints"] == []


def test_store_rolls_back_page_and_cursor_on_invalid_record(store):
    with pytest.raises(ValueError):
        store.save_batch([{"platform": "steam", "kind": "review", "native_id": "a"}, {"platform": "steam"}], "job", {"cursor": "next"})
    assert not list(store.iter_records())
    assert store.get_state("job") is None


def test_store_hash_key_mismatch(tmp_path):
    db, key = tmp_path / "db", tmp_path / "key"
    Store(db, key).close()
    key.write_bytes(b"z" * 32)
    with pytest.raises(ValueError, match="不匹配"):
        Store(db, key)


def test_export_csv_formula_and_jsonl_exact_text(store, tmp_path):
    rec = {"platform": "steam", "kind": "review", "native_id": "a", "text": "=HYPERLINK(1)\n中文"}
    store.save_batch([rec])
    csv_file, jl = tmp_path / "out.csv", tmp_path / "out.jsonl"
    assert store.export(csv_file, "csv") == 1
    store.export(jl)
    assert "'=HYPERLINK" in csv_file.read_text(encoding="utf-8-sig")
    assert json.loads(jl.read_text())["text"] == rec["text"]
    assert csv_file.stat().st_mode & 0o777 == 0o600


def test_purge_resets_only_target_namespaces(store):
    store.set_state("youtube:example", {"done": True})
    store.set_state("reddit-import:example", {"done": True})
    store.set_state("steam:example", {"done": True})
    store.purge_stale("reddit", "2024-01-01")
    assert store.get_state("reddit-import:example") is None
    assert store.get_state("youtube:example") is not None


class Response:
    def __init__(self, status, payload, headers=None):
        self.status_code, self.payload = status, payload
        self.headers = headers or {}
        self.ok = status < 400

    def json(self):
        return self.payload


def test_http_quota_not_retried_or_leaked(monkeypatch):
    c = HttpClient({"min_interval": 0})
    calls = []
    def get(*args, **kwargs):
        calls.append(args)
        return Response(403, {"error": {"message": "secret-key", "errors": [{"reason": "quotaExceeded"}]}})
    monkeypatch.setattr(c.session, "get", get)
    with pytest.raises(ApiError) as exc:
        c.get_json("https://example.test/api", params={"key": "secret-key"})
    assert exc.value.reason == "quotaExceeded"
    assert "secret-key" not in str(exc.value)
    assert len(calls) == 1
    c.close()


def test_http_respects_retry_after_and_throttle(monkeypatch):
    c = HttpClient({"min_interval": 0})
    responses = iter([Response(429, {}, {"Retry-After": "3"}), Response(200, {"ok": True})])
    sleeps = []
    monkeypatch.setattr(c.session, "get", lambda *a, **kw: next(responses))
    monkeypatch.setattr("wukong_collect.http.time.sleep", sleeps.append)
    assert c.get_json("https://example.test/api") == {"ok": True}
    assert sleeps == [3]
    c.close()


@pytest.mark.parametrize("headers", [{"X-RateLimit-Reset": "3"}, {"X-RateLimit-Reset-At": "103"}])
def test_archive_rate_limit_headers_delay_retry(monkeypatch, headers):
    c = HttpClient({"min_interval": 0})
    responses = iter([Response(429, {}, headers), Response(200, {"data": []})])
    sleeps = []
    monkeypatch.setattr(c.session, "get", lambda *a, **kw: next(responses))
    monkeypatch.setattr("wukong_collect.http.time.time", lambda: 100)
    monkeypatch.setattr("wukong_collect.http.time.sleep", sleeps.append)
    try:
        assert c.get_json("https://example.test/api") == {"data": []}
        assert sleeps == [3]
    finally:
        c.close()


def test_archive_long_rate_limit_stops_without_retry(monkeypatch):
    c = HttpClient({"min_interval": 0, "max_retry_wait": 60})
    monkeypatch.setattr(c.session, "get", lambda *a, **kw: Response(429, {}, {"X-RateLimit-Reset": "900"}))
    monkeypatch.setattr("wukong_collect.http.time.sleep", lambda _: pytest.fail("must stop for a long reset"))
    try:
        with pytest.raises(ApiError) as exc:
            c.get_json("https://example.test/api")
        assert exc.value.reason == "retry_later"
        assert c.requests_made == 1
    finally:
        c.close()


def test_archive_query_timeout_is_distinguished_from_other_422(monkeypatch):
    c = HttpClient({"min_interval": 0})
    responses = iter([Response(422, {"data": None, "error": "Timeout. Maybe slow down a bit"}),
                      Response(422, {"error": "Invalid parameter: private-value"})])
    monkeypatch.setattr(c.session, "get", lambda *a, **kw: next(responses))
    try:
        with pytest.raises(ApiError) as timeout:
            c.get_json("https://example.test/api")
        assert timeout.value.reason == "query_timeout"
        with pytest.raises(ApiError) as invalid:
            c.get_json("https://example.test/api")
        assert invalid.value.reason == "http_error"
        assert "private-value" not in str(invalid.value)
    finally:
        c.close()
