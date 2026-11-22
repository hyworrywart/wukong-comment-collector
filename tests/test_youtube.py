"""Offline tests focus on reply completeness and transactional resume."""
import copy
from urllib.parse import urlparse

import pytest

from wukong_collect import youtube

VIDEO = "abcdefghijk"


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.states = {}

    def author_hash(self, platform, raw_id):
        return "hashed:" + str(raw_id) if raw_id else None

    def iter_records(self, platform=None, kind=None):
        return (copy.deepcopy(value) for value in self.records.values()
                if (not platform or value["platform"] == platform) and (not kind or value["kind"] == kind))

    def save_batch(self, records, checkpoint_key=None, state=None):
        for record in records:
            self.records[(record["platform"], record["kind"], record["native_id"])] = copy.deepcopy(record)
        if checkpoint_key:
            self.set_state(checkpoint_key, state)

    def set_state(self, key, state):
        self.states[key] = copy.deepcopy(state)

    def get_state(self, key):
        return copy.deepcopy(self.states.get(key))


class Client:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        endpoint = urlparse(url).path.rsplit("/", 1)[-1]
        self.calls.append((endpoint, dict(params or {})))
        value = self.routes[(endpoint, (params or {}).get("pageToken"))]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "secret-test-key")
    return {
        "collection": {"start": "2024-08-20", "end": "2025-01-01"},
        "youtube": {"video_ids": [VIDEO], "include_replies": True, "max_pages": 20, "max_requests": 30},
    }


def video():
    return {"id": VIDEO, "snippet": {"title": "Wukong review", "publishedAt": "2024-08-20T00:00:00Z",
             "channelId": "creator-private-id"}, "statistics": {"viewCount": "10", "commentCount": "5"}}


def comment(ident, when="2024-09-01T00:00:00Z", parent=None):
    return {"id": ident, "snippet": {"textOriginal": "Journey to the West", "publishedAt": when,
            "updatedAt": when, "likeCount": 2, "authorChannelId": {"value": "private-author-id"},
            "authorDisplayName": "do-not-retain-me", **({"parentId": parent} if parent else {})}}


def thread(ident, replies=0, when="2024-09-01T00:00:00Z"):
    return {"id": "thread-" + ident, "snippet": {"topLevelComment": comment(ident, when), "totalReplyCount": replies},
            "replies": {"comments": [comment("embedded-not-complete")]}}


def comments(store):
    return {record["native_id"]: record for record in store.iter_records(platform="youtube", kind="comment")}


def test_all_reply_pages_and_parent_relations(config):
    store = MemoryStore()
    client = Client({
        ("videos", None): {"items": [video()]},
        ("commentThreads", None): {"items": [thread("top", replies=3)], "nextPageToken": "top-page-2"},
        ("comments", None): {"items": [comment("reply-1", parent="top"), comment("reply-2", parent="top")],
                            "nextPageToken": "reply-page-2"},
        ("comments", "reply-page-2"): {"items": [comment("reply-3", parent="top")]},
        ("commentThreads", "top-page-2"): {"items": [thread("top-2")]},
    })
    result = youtube.run(config, store, client)
    assert result["status"] == "complete"
    assert result["records"] == 5
    assert set(comments(store)) == {"top", "reply-1", "reply-2", "reply-3", "top-2"}
    assert comments(store)["reply-3"]["parent_id"] == "top"
    assert comments(store)["reply-3"]["root_id"] == VIDEO
    assert "do-not-retain-me" not in str(store.records)
    assert "secret-test-key" not in str(store.states)


def test_pause_mid_replies_resume_without_skipping(config):
    store = MemoryStore()
    routes = {
        ("videos", None): {"items": [video()]},
        ("commentThreads", None): {"items": [thread("top", replies=2)], "nextPageToken": "top-page-2"},
        ("comments", None): {"items": [comment("reply-1", parent="top")], "nextPageToken": "reply-page-2"},
        ("comments", "reply-page-2"): {"items": [comment("reply-2", parent="top")]},
        ("commentThreads", "top-page-2"): {"items": [thread("top-2")]},
    }
    result = youtube.run(config, store, Client(routes), max_pages=2)
    assert result["status"] == "partial"
    assert result["records"] == 2
    assert set(comments(store)) == {"top", "reply-1"}
    state = next(iter(store.states.values()))
    assert len(state["pending_replies"]) == 1
    assert state["pending_replies"][0]["parent_id"] == "top"
    assert state["pending_replies"][0]["page_token"] == "reply-page-2"
    client = Client(routes)
    resumed = youtube.run(config, store, client, max_pages=2)
    assert resumed["status"] == "complete"
    assert set(comments(store)) == {"top", "reply-1", "reply-2", "top-2"}
    assert client.calls[0][0] == "comments"
    assert client.calls[0][1]["pageToken"] == "reply-page-2"
    assert all(endpoint != "videos" for endpoint, _ in client.calls)
    cached_client = Client({})
    youtube.run(config, store, cached_client)
    assert cached_client.calls == []


def test_recent_reply_on_old_parent_is_retained(config):
    store = MemoryStore()
    client = Client({
        ("videos", None): {"items": [video()]},
        ("commentThreads", None): {"items": [thread("old-parent", replies=1, when="2020-01-01T00:00:00Z")]},
        ("comments", None): {"items": [comment("recent-reply", parent="old-parent")]},
    })
    youtube.run(config, store, client)
    assert set(comments(store)) == {"recent-reply"}


def test_no_replies_mode_does_not_request_comments(config):
    config["youtube"]["include_replies"] = False
    store = MemoryStore()
    client = Client({
        ("videos", None): {"items": [video()]},
        ("commentThreads", None): {"items": [thread("top", replies=100)]},
    })
    result = youtube.run(config, store, client)
    assert result["status"] == "complete"
    assert [endpoint for endpoint, _ in client.calls] == ["videos", "commentThreads"]


def test_discovery_pagination_and_query_provenance(config):
    config["youtube"].update(queries=["Black Myth Wukong"], search_pages=1)
    store = MemoryStore()
    search_item = video()
    search_item["id"] = {"videoId": VIDEO}
    client = Client({
        ("search", None): {"items": [search_item], "nextPageToken": "search-next"},
        ("videos", None): {"items": [video()]},
    })
    first = youtube.discover(config, store, client)
    assert first["status"] == "partial"
    assert "publishedAfter" not in client.calls[0][1]
    assert next(store.iter_records(kind="video"))["metadata"]["discovery_queries"] == ["Black Myth Wukong"]
    client = Client({
        ("search", "search-next"): {"items": []},
        ("videos", None): {"items": [video()]},
    })
    resumed = youtube.discover(config, store, client)
    assert resumed["status"] == "complete"
    assert client.calls[0][1]["pageToken"] == "search-next"


def test_metadata_request_budget_reports_partial(config):
    config["youtube"]["max_requests"] = 1
    store = MemoryStore()
    client = Client({("videos", None): {"items": [video()]}})
    result = youtube.run(config, store, client)
    assert result["status"] == "partial"
    assert result["records"] == 0


def test_one_request_budget_resumes_comments_after_metadata(config):
    config["youtube"]["max_requests"] = 1
    store = MemoryStore()
    first = youtube.run(config, store, Client({("videos", None): {"items": [video()]}}))
    assert first["status"] == "partial"
    state = next(iter(store.states.values()))
    assert state["metadata_refreshed"] and state["pages_total"] == 0

    client = Client({("commentThreads", None): {"items": [thread("top")]}})
    resumed = youtube.run(config, store, client)
    assert resumed["status"] == "complete"
    assert set(comments(store)) == {"top"}
    assert [endpoint for endpoint, _ in client.calls] == ["commentThreads"]

    # Starting a new traversal intentionally observes metadata again.
    client = Client({("videos", None): {"items": [video()]}})
    restarted = youtube.run(config, store, client, restart=True)
    assert restarted["status"] == "partial"
    assert [endpoint for endpoint, _ in client.calls] == ["videos"]


def test_small_budget_moves_past_multiple_metadata_batches(config):
    ids = [f"{index:011d}" for index in range(51)]
    config["youtube"].update(video_ids=ids, max_requests=1)
    store = MemoryStore()

    class BatchClient:
        def __init__(self):
            self.calls = []

        def get_json(self, url, params=None, headers=None):
            endpoint = urlparse(url).path.rsplit("/", 1)[-1]
            self.calls.append((endpoint, dict(params)))
            if endpoint == "videos":
                return {"items": [{**video(), "id": ident} for ident in params["id"].split(",")]}
            assert endpoint == "commentThreads"
            return {"items": [thread("top-" + params["videoId"])]}

    client = BatchClient()
    first = youtube.run(config, store, client)
    assert first["status"] == "partial" and first["records"] == 0
    assert client.calls[-1][1]["id"].split(",") == ids[:50]
    assert sum(state.get("metadata_refreshed", False) for state in store.states.values()) == 50

    second = youtube.run(config, store, client)
    assert second["status"] == "partial" and second["records"] == 0
    assert client.calls[-1] == ("videos", {"part": "snippet,statistics", "id": ids[-1], "key": "secret-test-key"})

    for expected_count in range(1, len(ids) + 1):
        result = youtube.run(config, store, client)
        assert result["requests"] == 1
        assert len(comments(store)) == expected_count
    assert result["status"] == "complete"
    assert sum(endpoint == "videos" for endpoint, _ in client.calls) == 2
    assert {record["root_id"] for record in comments(store).values()} == set(ids)


def test_discovery_metadata_budget_reports_partial(config):
    config["youtube"].update(queries=["Wukong"], search_pages=1, max_requests=1)
    store = MemoryStore()
    search_item = video()
    search_item["id"] = {"videoId": VIDEO}
    client = Client({("search", None): {"items": [search_item]}})
    result = youtube.discover(config, store, client)
    assert result["status"] == "partial"
    assert result["metadata_status"] == "partial"


def test_disabled_comments_has_explicit_terminal_status(config):
    error = youtube.ApiError("disabled", 403, "commentsDisabled")
    store = MemoryStore()
    client = Client({("videos", None): {"items": [video()]}, ("commentThreads", None): error})
    result = youtube.run(config, store, client)
    assert result["videos"][0]["status"] == "comments_disabled"
    assert next(iter(store.states.values()))["done"]


def test_inaccessible_video_has_explicit_status(config):
    store = MemoryStore()
    client = Client({("videos", None): {"items": []}})
    result = youtube.run(config, store, client)
    assert result["videos"][0]["status"] == "unavailable"
    assert result["records"] == 0


def test_api_failure_does_not_advance_reply_cursor(config):
    # Use the real ApiError constructor through its signature-independent
    # __new__ path; only status_code/reason are collector-facing attributes.
    error = youtube.ApiError.__new__(youtube.ApiError)
    Exception.__init__(error, "quota exhausted")
    error.status_code = 403
    error.reason = "quotaExceeded"
    store = MemoryStore()
    client = Client({
        ("videos", None): {"items": [video()]},
        ("commentThreads", None): {"items": [thread("top", replies=2)]},
        ("comments", None): error,
    })
    with pytest.raises(youtube.ApiError):
        youtube.run(config, store, client)
    state = next(iter(store.states.values()))
    assert not state["done"]
    assert state["pending_replies"] == [{"parent_id": "top", "page_token": None}]
    assert set(comments(store)) == {"top"}


def test_video_url_parsing():
    assert youtube.video_id("https://youtu.be/abcdefghijk?t=10") == VIDEO
    assert youtube.video_id("https://www.youtube.com/watch?v=abcdefghijk") == VIDEO
    assert youtube.video_id("https://www.youtube.com/shorts/abcdefghijk") == VIDEO
    with pytest.raises(ValueError):
        youtube.video_id("https://not-youtube.test/watch?v=abcdefghijk")


def test_missing_credentials_is_actionable(config, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY")
    with pytest.raises(ValueError, match="YOUTUBE_API_KEY"):
        youtube.run(config, MemoryStore(), Client({}))


@pytest.mark.parametrize("field", ["max_requests", "max_pages", "search_pages"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_budgets_fail_before_network(config, field, value):
    config["youtube"][field] = value
    for operation in (youtube.run, youtube.discover):
        client = Client({})
        with pytest.raises(ValueError, match=field):
            operation(config, MemoryStore(), client)
        assert client.calls == []


@pytest.mark.parametrize("endpoint", ["videos", "commentThreads", "comments", "search"])
def test_malformed_http_200_never_commits_exhaustion(config, endpoint):
    store = MemoryStore()
    routes = {("videos", None): {"items": [video()]},
              ("commentThreads", None): {"items": [thread("top", replies=1)]},
              ("comments", None): {"items": [comment("reply", parent="top")]},
              ("search", None): {"items": []}}
    routes[(endpoint, None)] = {"message": "proxy or schema changed"}
    config["youtube"]["queries"] = ["Wukong"]
    operation = youtube.discover if endpoint == "search" else youtube.run
    with pytest.raises(youtube.ApiError) as failure:
        operation(config, store, Client(routes))
    assert failure.value.reason == "invalid_payload"
    assert all(not state.get("done") for state in store.states.values())
    assert "reply" not in comments(store)


def test_malformed_thread_without_reply_count_does_not_skip_replies(config):
    store = MemoryStore()
    item = thread("top", replies=10)
    del item["snippet"]["totalReplyCount"]
    client = Client({("videos", None): {"items": [video()]},
                     ("commentThreads", None): {"items": [item]}})
    with pytest.raises(youtube.ApiError, match="结构不完整"):
        youtube.run(config, store, client)
    state = next(iter(store.states.values()))
    assert state["metadata_refreshed"] and not state["done"]
    assert state["page_token"] is None and state["pages_total"] == 0
    assert not comments(store)


def test_repeated_thread_token_stops_without_advancing_checkpoint(config):
    store = MemoryStore()
    routes = {("videos", None): {"items": [video()]},
              ("commentThreads", None): {"items": [thread("top")], "nextPageToken": "same-token"},
              ("commentThreads", "same-token"): {"items": [thread("must-not-commit")], "nextPageToken": "same-token"}}
    youtube.run(config, store, Client(routes), max_pages=1)
    before = copy.deepcopy(store.states)
    with pytest.raises(youtube.ApiError) as failure:
        youtube.run(config, store, Client(routes))
    assert failure.value.reason == "stalled_pagination"
    assert store.states == before
    assert set(comments(store)) == {"top"}


def test_repeated_reply_token_stops_without_losing_pending_job(config):
    store = MemoryStore()
    routes = {("videos", None): {"items": [video()]},
              ("commentThreads", None): {"items": [thread("top", replies=4)]},
              ("comments", None): {"items": [comment("reply-1", parent="top")], "nextPageToken": "same-token"},
              ("comments", "same-token"): {"items": [comment("must-not-commit", parent="top")], "nextPageToken": "same-token"}}
    youtube.run(config, store, Client(routes), max_pages=2)
    before = copy.deepcopy(store.states)
    with pytest.raises(youtube.ApiError) as failure:
        youtube.run(config, store, Client(routes))
    assert failure.value.reason == "stalled_pagination"
    assert store.states == before
    assert set(comments(store)) == {"top", "reply-1"}


def test_search_token_cycle_detected_across_multiple_tokens(config):
    store = MemoryStore()
    config["youtube"].update(queries=["Wukong"], search_pages=3)
    routes = {("search", None): {"items": [], "nextPageToken": "A"},
              ("search", "A"): {"items": [], "nextPageToken": "B"},
              ("search", "B"): {"items": [], "nextPageToken": "A"}}
    with pytest.raises(youtube.ApiError) as failure:
        youtube.discover(config, store, Client(routes))
    assert failure.value.reason == "stalled_pagination"
    assert next(iter(store.states.values()))["page_token"] == "B"
    assert not next(iter(store.states.values()))["done"]


def test_storage_paths_do_not_change_collection_identity(config):
    store = MemoryStore()
    client = Client({("videos", None): {"items": [video()]}, ("commentThreads", None): {"items": []}})
    youtube.run(config, store, client)
    original_keys = set(store.states)
    config["collection"].update(database="moved.sqlite3", hash_key_file="moved.key", start="2024-08-20T00:00:00Z")
    cached_client = Client({})
    youtube.run(config, store, cached_client)
    assert cached_client.calls == []
    assert set(store.states) == original_keys


def test_complete_embedded_replies_save_without_extra_request(config):
    store = MemoryStore()
    item = thread("top", replies=2, when="2020-01-01T00:00:00Z")
    item["replies"] = {"comments": [comment("r1", parent="top"), comment("r2", parent="top")]}
    client = Client({("videos", None): {"items": [video()]}, ("commentThreads", None): {"items": [item]}})
    result = youtube.run(config, store, client)
    assert result["status"] == "complete"
    assert set(comments(store)) == {"r1", "r2"}
    assert [endpoint for endpoint, _ in client.calls] == ["videos", "commentThreads"]
    assert comments(store)["r1"]["metadata"]["api_method"] == "commentThreads.list.replies"


@pytest.mark.parametrize("invalid", ["duplicate", "wrong_parent", "wrong_video"])
def test_untrusted_embedded_replies_fall_back_to_full_api(config, invalid):
    item = thread("top", replies=2)
    first, second = comment("r1", parent="top"), comment("r2", parent="top")
    if invalid == "duplicate":
        second["id"] = "r1"
    elif invalid == "wrong_parent":
        second["snippet"]["parentId"] = "other"
    else:
        second["snippet"]["videoId"] = "other-video"
    item["replies"] = {"comments": [first, second]}
    client = Client({("videos", None): {"items": [video()]}, ("commentThreads", None): {"items": [item]},
                     ("comments", None): {"items": [comment("true-reply", parent="top")]}})
    store = MemoryStore()
    youtube.run(config, store, client)
    assert set(comments(store)) == {"top", "true-reply"}
    assert client.calls[-1][0] == "comments"


def test_search_only_discovery_preserves_time_window_provenance(config):
    config["youtube"].update(queries=["Black Myth Wukong"], refresh_video_metadata=False,
                             published_after="2024-08-01T00:00:00Z", published_before="2024-09-01T00:00:00Z")
    item = video()
    item["id"] = {"videoId": VIDEO}
    client = Client({("search", None): {"items": [item]}})
    store = MemoryStore()
    result = youtube.discover(config, store, client)
    assert result["metadata_status"] == "deferred" and result["status"] == "complete"
    assert [endpoint for endpoint, _ in client.calls] == ["search"]
    context = next(store.iter_records(kind="video"))["metadata"]["discovery_contexts"][0]
    assert context == {"query": "Black Myth Wukong", "published_after": "2024-08-01T00:00:00Z",
                       "published_before": "2024-09-01T00:00:00Z"}
