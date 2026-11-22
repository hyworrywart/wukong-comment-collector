"""Synthetic archive service tests: tied seconds, resume, scope and provenance."""
from __future__ import annotations

import copy
from urllib.parse import urlparse

import pytest

from wukong_collect import reddit_archive as archive
from wukong_collect.common import utc_iso
from wukong_collect.http import ApiError
from wukong_collect.reddit import RedditConfigError
from wukong_collect.store import Store

T = 1724112000


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "records.sqlite3", tmp_path / "hash.key")
    yield result
    result.close()


@pytest.fixture
def config():
    return {"collection": {"start": utc_iso(T), "end": utc_iso(T + 4)},
            "reddit": {"mode": "arctic_shift", "subreddits": ["BlackMythWukong"],
                       "keywords": ["Wukong"], "max_pages": 100,
                       "archive_posts_start": utc_iso(T - 100)}}


def comment(ident="c0", when=T, *, subreddit="BlackMythWukong", root="p0", text="Beautiful temple"):
    return {"id": ident, "created_utc": when, "subreddit": subreddit, "body": text,
            "author": "private-person", "author_fullname": "t2_private", "score": 3,
            "link_id": "t3_" + root, "parent_id": "t3_" + root, "retrieved_on": when + 30}


def post(ident="p0", when=T, *, subreddit="gaming", title="Wukong cultural symbols"):
    return {"id": ident, "created_utc": when, "subreddit": subreddit, "selftext": "Review discussion",
            "title": title, "author": "private-poster", "score": 8, "num_comments": 5}


class ArchiveClient:
    """Implement the service's strict after/before and fixed 100-row cap."""
    def __init__(self, comments=(), posts=(), fail_at=None, override=None, failure=None, auto_cap=1000):
        self.comments = comments
        self.posts = posts
        self.fail_at = fail_at
        self.override = override
        self.failure = failure
        self.auto_cap = auto_cap
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        params = copy.deepcopy(params)
        endpoint = urlparse(url).path
        self.calls.append((endpoint, params))
        if len(self.calls) == self.fail_at:
            raise self.failure or ApiError("temporary archive failure", 503, "http_error")
        if self.override is not None:
            return copy.deepcopy(self.override)
        source = self.posts if endpoint == "/api/posts/search" else self.comments
        rows = [row for row in source if row["subreddit"].lower() == params["subreddit"].lower()
                and params["after"] < row["created_utc"] < params["before"]]
        if params.get("link_id"):
            rows = [row for row in rows if row["link_id"].removeprefix("t3_") == params["link_id"]]
        if params.get("query"):
            rows = [row for row in rows if params["query"].casefold() in (row["title"] + " " + row["selftext"]).casefold()]
        cap = self.auto_cap(len(self.calls)) if callable(self.auto_cap) else self.auto_cap
        limit = cap if params["limit"] == "auto" else params["limit"]
        return {"data": copy.deepcopy(sorted(rows, key=lambda row: row["created_utc"])[:limit])}


def records(store, kind="comment"):
    return {row["native_id"]: row for row in store.iter_records(platform="reddit", kind=kind)}


def test_bisection_preserves_all_tied_seconds(config, store):
    config["collection"]["end"] = utc_iso(T + 2)
    rows = [comment(f"c{i}", T + i // 60) for i in range(120)]
    client = ArchiveClient(rows)
    result = archive.run(config, store, client)
    assert result["status"] == "complete"
    assert result["pages"] == 3  # Saturation probe, then both exhaustive leaves.
    assert len(records(store)) == 120
    assert client.calls[0][1] == {"subreddit": "blackmythwukong", "after": T - 1,
                                   "before": T + 2, "sort": "asc", "limit": 100}
    assert all(params["limit"] == 100 for _, params in client.calls)
    assert next(iter(store.summary()["checkpoints"]))["state"]["pending"] == []


def test_single_second_saturation_fails_without_false_completion(config, store):
    config["collection"]["end"] = utc_iso(T + 1)
    client = ArchiveClient([comment(f"c{i}") for i in range(100)])
    with pytest.raises(RedditConfigError, match="dump"):
        archive.run(config, store, client)
    state = store.summary()["checkpoints"][0]["state"]
    assert not state["done"]
    assert len(state["pending"]) == 1
    assert state["blocked_window"]["lo"] == T
    assert not records(store)


def test_page_limit_and_resume_keep_entire_pending_stack(config, store):
    config["collection"]["end"] = utc_iso(T + 2)
    client = ArchiveClient([comment(f"c{i}", T + i // 60) for i in range(120)])
    first = archive.run(config, store, client, max_pages=1)
    assert first["status"] == "partial" and first["pending_tasks"] == 2
    second = archive.run(config, store, client, max_pages=1)
    assert second["status"] == "partial" and second["saved"] == 60
    final = archive.run(config, store, client, max_pages=2)
    assert final["status"] == "complete" and final["pages_total"] == 3
    assert len(records(store)) == 120
    cached = ArchiveClient()
    config["collection"]["database"] = "different-path.sqlite3"
    assert archive.run(config, store, cached, max_pages=3)["saved"] == 0
    assert cached.calls == []


def test_network_failure_does_not_advance_window(config, store):
    config["collection"]["end"] = utc_iso(T + 2)
    rows = [comment(f"c{i}", T + i // 60) for i in range(120)]
    client = ArchiveClient(rows, fail_at=2)
    with pytest.raises(ApiError):
        archive.run(config, store, client)
    state = store.summary()["checkpoints"][0]["state"]
    assert state["pages_total"] == 1 and len(state["pending"]) == 2
    resumed = archive.run(config, store, ArchiveClient(rows))
    assert resumed["status"] == "complete" and len(records(store)) == 120


def test_old_keyword_post_keeps_new_keyword_free_replies(config, store):
    config["reddit"].update(subreddits=["gaming"], archive_all_comments_subreddits=[])
    client = ArchiveClient(
        [comment("c1", T + 1, subreddit="gaming", root="p0", text="Absolutely amazing"),
         comment("c2", T + 1, subreddit="gaming", root="p2", text="Wukong")],
        [post("p0", T - 20), post("p2", T - 10, title="Other game")])
    result = archive.run(config, store, client)
    assert result["status"] == "complete"
    assert set(records(store)) == {"t1_c1"}
    assert not records(store, "post")  # Seed is older than the collection window.
    endpoint, params = client.calls[1]
    assert endpoint == "/api/comments/search" and params["link_id"] == "p0"
    assert "query" not in params and "body" not in params
    assert records(store)["t1_c1"]["root_id"] == "t3_p0"


def test_source_provenance_hash_and_fixed_mapping(config, store):
    config["reddit"]["fields"] = {"broken": "mapping"}
    row = comment()
    row["_meta"] = {"was_deleted_later": False, "arbitrary_user_field": "private-person"}
    result = archive.run(config, store, ArchiveClient([row]))
    assert result["coverage"] == "third_party_archive_snapshot"
    record = records(store)["t1_c0"]
    assert record["author_hash"] == store.author_hash("reddit", "private-person")
    assert record["metadata"]["source"] == "arctic_shift"
    assert record["metadata"]["archive_snapshot"] is True
    assert record["metadata"]["retrieved_on"] == T + 30
    assert "private-person" not in str(record) and "t2_private" not in str(record)
    assert record["url"] == "https://www.reddit.com/r/blackmythwukong/comments/p0/_/c0/"


def test_deleted_content_and_later_deleted_metadata_are_excluded(config, store):
    rows = [comment("c1", text="[deleted]"), comment("c2", text="[removed]"), comment("c3"), comment("c4")]
    rows[2]["_meta"] = {"was_deleted_later": True}
    archive.run(config, store, ArchiveClient(rows))
    assert set(records(store)) == {"t1_c4"}


def test_posts_in_window_stored_and_duplicate_keyword_seeds_enqueue_once(config, store):
    config["reddit"].update(subreddits=["gaming"], archive_all_comments_subreddits=[], keywords=["Wukong", "culture"])
    client = ArchiveClient([comment("c1", T + 1, subreddit="gaming")],
                           [post(title="Wukong culture")])
    archive.run(config, store, client)
    assert set(records(store, "post")) == {"t3_p0"}
    assert set(records(store)) == {"t1_c1"}
    assert sum(endpoint == "/api/comments/search" for endpoint, _ in client.calls) == 1


@pytest.mark.parametrize("payload", [[], {}, {"data": {}}, {"data": [None]}, {"data": [{"id": "c1"}]}])
def test_malformed_response_never_marks_complete(config, store, payload):
    with pytest.raises(RedditConfigError):
        archive.run(config, store, ArchiveClient(override=payload))
    assert not store.summary()["checkpoints"][0]["state"]["done"]
    assert not records(store)


@pytest.mark.parametrize("mutation", ["outside", "community", "unordered", "wrong_root", "missing_parent"])
def test_source_contract_violations_preserve_checkpoint(config, store, mutation):
    rows = [comment("c1", T), comment("c2", T + 1)]
    if mutation == "outside":
        rows[0]["created_utc"] = T - 1
    elif mutation == "community":
        rows[0]["subreddit"] = "OtherCommunity"
    elif mutation == "unordered":
        rows.reverse()
    elif mutation == "missing_parent":
        del rows[0]["parent_id"]
    else:
        # Seed a selected thread first; then return comments from a different post.
        config["reddit"].update(subreddits=["gaming"], archive_all_comments_subreddits=[])
        rows[0].update(subreddit="gaming", link_id="t3_wrong")
        client = ArchiveClient(posts=[post()])
        archive.run(config, store, client, max_pages=1)
        with pytest.raises(RedditConfigError, match="不属于"):
            archive.run(config, store, ArchiveClient(override={"data": rows[:1]}))
        assert store.summary()["checkpoints"][0]["state"]["pages_total"] == 1
        return
    with pytest.raises(RedditConfigError):
        archive.run(config, store, ArchiveClient(override={"data": rows}))
    assert store.summary()["checkpoints"][0]["state"]["pages_total"] == 0


def test_fractional_bounds_apply_integer_second_ceiling(config, store):
    config["collection"].update(start=utc_iso(T + 0.5), end=utc_iso(T + 1.5))
    client = ArchiveClient([comment("c0", T), comment("c1", T + 1), comment("c2", T + 2)])
    archive.run(config, store, client)
    assert set(records(store)) == {"t1_c1"}
    assert client.calls[0][1]["after"] == T and client.calls[0][1]["before"] == T + 2


def test_all_comments_subreddits_must_be_allowed_subset(config, store):
    config["reddit"]["subreddits"] = ["gaming"]
    client = ArchiveClient()
    with pytest.raises(RedditConfigError, match="子集"):
        archive.run(config, store, client)
    assert not client.calls


def test_empty_integer_time_window_completes_without_request(config, store):
    config["collection"].update(start=utc_iso(T + 0.1), end=utc_iso(T + 0.5))
    client = ArchiveClient()
    result = archive.run(config, store, client)
    assert result["status"] == "complete" and result["pages"] == 0
    assert not client.calls


def test_query_timeout_splits_window_counts_request_and_resumes(config, store):
    failure = ApiError("archive query timed out", 422, "query_timeout")
    first_client = ArchiveClient(fail_at=1, failure=failure)
    first = archive.run(config, store, first_client, max_pages=1)
    assert first["status"] == "partial" and first["pages"] == 1
    state = store.summary()["checkpoints"][0]["state"]
    assert state["pages_total"] == 1 and state["split_windows"] == 1
    assert [(task["lo"], task["hi"]) for task in state["pending"]] == [(T + 2, T + 4), (T, T + 2)]
    rows = [comment(f"c{i}", T + i) for i in range(4)]
    resumed = archive.run(config, store, ArchiveClient(rows))
    assert resumed["status"] == "complete" and resumed["pages_total"] == 3
    assert len(records(store)) == 4


def test_unidentified_422_does_not_split_or_advance(config, store):
    client = ArchiveClient(fail_at=1, failure=ApiError("invalid filter", 422, "http_error"))
    with pytest.raises(ApiError):
        archive.run(config, store, client)
    state = store.summary()["checkpoints"][0]["state"]
    assert state["pages_total"] == 0 and state["split_windows"] == 0
    assert len(state["pending"]) == 1 and not state["done"]


def test_single_second_query_timeout_preserves_window_and_fails(config, store):
    config["collection"]["end"] = utc_iso(T + 1)
    client = ArchiveClient(fail_at=1, failure=ApiError("timeout", 422, "query_timeout"))
    with pytest.raises(RedditConfigError, match="单秒窗口查询仍超时"):
        archive.run(config, store, client)
    state = store.summary()["checkpoints"][0]["state"]
    assert state["pages_total"] == 1
    assert state["pending"] == [state["blocked_window"]]
    assert not state["done"] and not records(store)


def test_auto_time_dynamic_caps_repeat_tail_second_and_keep_all_rows(config, store):
    config["reddit"]["archive_page_mode"] = "auto_time"
    config["collection"]["end"] = utc_iso(T + 100)
    # 30 comments per second: an auto cap of 100 cuts through the 4th second.
    rows = [comment(f"c{i}", T + i // 30) for i in range(2200)]
    client = ArchiveClient(rows, auto_cap=lambda page: 100 if page == 1 else 1000)
    first = archive.run(config, store, client, max_pages=1)
    assert first["status"] == "partial" and first["saved"] == 90
    state = store.summary()["checkpoints"][0]["state"]
    assert state["pending"][-1]["lo"] == T + 3
    final = archive.run(config, store, client)
    assert final["status"] == "complete" and len(records(store)) == 2200
    assert final["pages_total"] == 5
    assert client.calls[1][1]["after"] == T + 2
    assert all(params["limit"] == "auto" for _, params in client.calls)


def test_auto_time_less_than_1000_is_not_assumed_complete(config, store):
    config["reddit"]["archive_page_mode"] = "auto_time"
    config["collection"]["end"] = utc_iso(T + 500)
    rows = [comment(f"c{i}", T + i) for i in range(400)]
    client = ArchiveClient(rows, auto_cap=250)
    first = archive.run(config, store, client, max_pages=1)
    assert first["status"] == "partial" and first["saved"] == 249
    assert store.summary()["checkpoints"][0]["state"]["pending"][-1]["lo"] == T + 249
    final = archive.run(config, store, client)
    assert final["status"] == "complete" and len(records(store)) == 400


def test_auto_time_single_second_saturation_preserves_pending(config, store):
    config["reddit"]["archive_page_mode"] = "auto_time"
    config["collection"]["end"] = utc_iso(T + 1)
    with pytest.raises(RedditConfigError, match="单秒窗口"):
        archive.run(config, store, ArchiveClient([comment(f"c{i}") for i in range(101)]))
    state = store.summary()["checkpoints"][0]["state"]
    assert not state["done"] and state["pending"] == [state["blocked_window"]]
    assert not records(store)


def test_fixed_pending_checkpoint_can_resume_with_auto_time(config, store):
    config["collection"]["end"] = utc_iso(T + 4)
    rows = [comment(f"c{i}", T + i // 60) for i in range(240)]
    first = archive.run(config, store, ArchiveClient(rows), max_pages=1)
    config["reddit"]["archive_page_mode"] = "auto_time"
    final = archive.run(config, store, ArchiveClient(rows))
    assert first["checkpoint"] == final["checkpoint"]
    assert final["status"] == "complete" and len(records(store)) == 240


def test_auto_time_keyword_post_prefix_enqueues_replies_before_continuing(config, store):
    config["reddit"].update(subreddits=["gaming"], archive_all_comments_subreddits=[],
                            archive_page_mode="auto_time")
    config["collection"]["end"] = utc_iso(T + 500)
    posts = [post(f"p{i}", T + i) for i in range(110)]
    comments = [comment(f"c{i}", T + 1, subreddit="gaming", root=f"p{i}") for i in range(110)]
    final = archive.run(config, store, ArchiveClient(comments, posts), max_pages=200)
    assert final["status"] == "complete"
    assert len(records(store, "post")) == 110 and len(records(store)) == 110
