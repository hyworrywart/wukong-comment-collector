"""Public YouTube comments via Data API v3, with durable reply pagination.

Only public read endpoints are used. Embedded replies are complete only when
their unique, valid IDs and trusted parent links match totalReplyCount exactly.
All other nonempty threads use comments.list pagination.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
from urllib.parse import parse_qs, urlparse

from .common import in_window, job_key, utc_iso
from .http import ApiError

BASE = "https://www.googleapis.com/youtube/v3"
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _api_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise ValueError("缺少 YOUTUBE_API_KEY：请在 .env 填入已启用 YouTube Data API v3 的 API Key。")
    return key


def video_id(value: str) -> str:
    """Accept an ID or a standard public watch/shorts/embed URL."""
    value = str(value).strip()
    if _VIDEO_ID.fullmatch(value):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        parts = parsed.path.strip("/").split("/")
        candidate = parse_qs(parsed.query).get("v", [""])[0]
        if len(parts) > 1 and parts[0] in {"shorts", "embed", "live"}:
            candidate = parts[1]
    else:
        candidate = ""
    if not _VIDEO_ID.fullmatch(candidate):
        raise ValueError(f"无效的 YouTube 视频 ID/URL：{value}")
    return candidate


class _LimitReached(Exception):
    pass


def _positive_limit(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"youtube.{name} 必须是正整数")
    return value


def _invalid_payload(endpoint):
    return ApiError(f"YouTube {endpoint} 返回的结构不完整；断点未推进。", 200, "invalid_payload")


def _items(payload, endpoint):
    # An error proxy can return HTTP 200 with arbitrary JSON. Such a response
    # must never be interpreted as an empty, successfully exhausted listing.
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise _invalid_payload(endpoint)
    if not all(isinstance(item, dict) for item in payload["items"]):
        raise _invalid_payload(endpoint)
    token = payload.get("nextPageToken")
    if token is not None and (not isinstance(token, str) or not token):
        raise _invalid_payload(endpoint)
    return payload["items"]


def _next_token(payload, current, seen, endpoint):
    token = payload.get("nextPageToken")
    history = list(seen or [])
    if token:
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        if token == current or fingerprint in history:
            raise ApiError(f"YouTube {endpoint} 分页令牌重复；断点未推进，可稍后重试或使用 --restart。", 200, "stalled_pagination")
        history.append(fingerprint)
    return token, history


def _published_at(snippet, endpoint):
    if not isinstance(snippet, dict) or not snippet.get("publishedAt"):
        raise _invalid_payload(endpoint)
    try:
        return utc_iso(snippet["publishedAt"])
    except (TypeError, ValueError, OverflowError):
        raise _invalid_payload(endpoint) from None


class _Budget:
    def __init__(self, config: dict, max_pages: int | None):
        cfg = config.get("youtube", {})
        self.limit = _positive_limit(cfg.get("max_requests", 1000), "max_requests")
        _positive_limit(cfg.get("max_pages", 100), "max_pages")
        _positive_limit(cfg.get("search_pages", 1), "search_pages")
        self.max_pages = _positive_limit(max_pages, "max_pages") if max_pages is not None else None
        self.requests = 0
        self.pages = 0

    def get(self, client, endpoint: str, params: dict, *, page=False):
        if self.requests >= self.limit or (page and self.max_pages is not None and self.pages >= self.max_pages):
            raise _LimitReached
        self.requests += 1
        if page:
            self.pages += 1
        return client.get_json(f"{BASE}/{endpoint}", params=params)


def _integer(value):
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise _invalid_payload("numeric field") from None
    if isinstance(value, bool) or number < 0 or isinstance(value, float) and number != value:
        raise _invalid_payload("numeric field")
    return number


def _video_record(item: dict, store, previous: dict | None = None, query: str | None = None, discovery_context: dict | None = None) -> dict:
    ident = item.get("id")
    if isinstance(ident, dict):
        ident = ident.get("videoId")
    if not isinstance(ident, str) or not _VIDEO_ID.fullmatch(ident):
        raise _invalid_payload("video")
    snippet = item.get("snippet")
    created_at = _published_at(snippet, "video")
    if not isinstance(snippet.get("title"), str) or not isinstance(snippet.get("description", ""), str):
        raise _invalid_payload("video")
    old_metadata = (previous or {}).get("metadata", {})
    queries = list(old_metadata.get("discovery_queries", []))
    if query and query not in queries:
        queries.append(query)
    statistics = item.get("statistics", {})
    if not isinstance(statistics, dict):
        raise _invalid_payload("videos")
    metadata = {
        **old_metadata,
        "title": snippet.get("title", old_metadata.get("title", "")),
        "description": snippet.get("description", old_metadata.get("description", "")),
        "discovery_queries": queries,
        "api_method": "videos.list" if "statistics" in item else "search.list",
    }
    contexts = list(old_metadata.get("discovery_contexts", []))
    if discovery_context and discovery_context not in contexts:
        contexts.append(discovery_context)
    if contexts:
        metadata["discovery_contexts"] = contexts
    for source, target in (("viewCount", "view_count"), ("commentCount", "comment_count")):
        if source in statistics:
            metadata[target] = _integer(statistics[source])
    if "categoryId" in snippet:
        metadata["category_id"] = snippet["categoryId"]
    return {
        "platform": "youtube", "kind": "video", "native_id": ident,
        "parent_id": None, "root_id": ident,
        "author_hash": store.author_hash("youtube", snippet.get("channelId")),
        "text": metadata["title"] + ("\n" + metadata["description"] if metadata["description"] else ""),
        "created_at": created_at,
        "updated_at": None,
        "language": snippet.get("defaultLanguage") or (previous or {}).get("language"),
        "like_count": _integer(statistics.get("likeCount", (previous or {}).get("like_count"))),
        "reply_count": _integer(statistics.get("commentCount", (previous or {}).get("reply_count"))),
        "url": f"https://www.youtube.com/watch?v={ident}", "metadata": metadata,
    }


def _comment_record(item: dict, ident: str, store, *, parent=None, replies=None) -> dict:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        raise _invalid_payload("comment")
    snippet = item.get("snippet")
    created_at = _published_at(snippet, "comment")
    text = snippet.get("textOriginal", snippet.get("textDisplay"))
    if not isinstance(text, str):
        raise _invalid_payload("comment")
    author = snippet.get("authorChannelId", {})
    author = author.get("value") if isinstance(author, dict) else author
    native_id = item["id"]
    return {
        "platform": "youtube", "kind": "comment", "native_id": native_id,
        "parent_id": parent or snippet.get("parentId"), "root_id": ident,
        "author_hash": store.author_hash("youtube", author),
        "text": text,
        "created_at": created_at,
        "updated_at": utc_iso(snippet["updatedAt"]) if snippet.get("updatedAt") else None,
        "language": None,  # The comments API supplies no reliable comment-language field.
        "like_count": _integer(snippet.get("likeCount")), "reply_count": replies,
        "url": f"https://www.youtube.com/watch?v={ident}&lc={native_id}",
        "metadata": {"video_id": ident, "is_reply": bool(parent or snippet.get("parentId")),
                     "api_method": "comments.list" if parent else "commentThreads.list"},
    }


def _complete_embedded_replies(thread, parent_id, count, ident, store):
    embedded = thread.get("replies", {}).get("comments") if isinstance(thread.get("replies", {}), dict) else None
    if not isinstance(embedded, list) or len(embedded) != count:
        return None
    ids = set()
    records = []
    for item in embedded:
        if not isinstance(item, dict) or not isinstance(item.get("snippet"), dict):
            return None
        native_id = item.get("id")
        snippet = item["snippet"]
        if not isinstance(native_id, str) or not native_id or native_id == parent_id or native_id in ids:
            return None
        if snippet.get("parentId") != parent_id or snippet.get("videoId", ident) != ident:
            return None
        ids.add(native_id)
        try:
            record = _comment_record(item, ident, store, parent=parent_id)
        except (ApiError, TypeError, ValueError):
            return None
        record["metadata"]["api_method"] = "commentThreads.list.replies"
        records.append(record)
    return records


def _refresh_videos(ids, store, client, key, budget, existing):
    missing = set()
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        data = budget.get(client, "videos", {"part": "snippet,statistics", "id": ",".join(batch), "key": key})
        items = _items(data, "videos")
        if any(not isinstance(item.get("id"), str) for item in items):
            raise _invalid_payload("videos")
        records = [_video_record(item, store, existing.get(item.get("id"))) for item in items]
        if any(record["native_id"] not in batch for record in records):
            raise _invalid_payload("videos")
        store.save_batch(records)
        existing.update({record["native_id"]: record for record in records})
        missing.update(set(batch) - {record["native_id"] for record in records})
    return missing


def discover(config: dict, store, client, *, restart=False, max_pages=None) -> dict:
    """Search candidate videos, preserving query provenance and resume tokens.

    search_pages is a per-query/per-invocation sampling limit. Search is not an
    exhaustive historical enumeration. Video dates are only restricted by the
    optional youtube.published_after/before, never by comment collection dates.
    """
    key = _api_key()
    cfg = config.get("youtube", {})
    queries = list(dict.fromkeys(cfg.get("queries", [])))
    budget = _Budget(config, max_pages)
    existing = {r["native_id"]: r for r in store.iter_records(platform="youtube", kind="video")}
    summary = {"platform": "youtube", "operation": "discover", "queries": [], "records": 0}
    stopped = False
    for query in queries:
        settings = {k: cfg.get(k) for k in ("published_after", "published_before", "relevance_language", "region_code", "search_order")}
        settings.update(query=query, operation="search")
        checkpoint = job_key("youtube", settings)
        state = None if restart else store.get_state(checkpoint)
        state = copy.deepcopy(state) if state else {"page_token": None, "done": False, "pages_total": 0}
        if state.get("done"):
            summary["queries"].append({"query": query, "status": "complete", "cached": True})
            continue
        per_query_pages = cfg.get("search_pages", 1)
        try:
            for _ in range(per_query_pages):
                params = {"part": "snippet", "type": "video", "q": query, "maxResults": 50,
                          "order": cfg.get("search_order", "relevance"), "key": key}
                for option, api_name in (("published_after", "publishedAfter"), ("published_before", "publishedBefore"),
                                         ("relevance_language", "relevanceLanguage"), ("region_code", "regionCode")):
                    if cfg.get(option):
                        params[api_name] = cfg[option]
                if state.get("page_token"):
                    params["pageToken"] = state["page_token"]
                data = budget.get(client, "search", params, page=True)
                items = _items(data, "search")
                next_token, token_history = _next_token(data, state.get("page_token"), state.get("seen_tokens"), "search")
                records = []
                for item in items:
                    ident = item.get("id", {}).get("videoId") if isinstance(item.get("id"), dict) else None
                    if not ident:
                        raise _invalid_payload("search")
                    context = {"query": query, **{name: cfg[name] for name in
                               ("published_after", "published_before", "relevance_language", "region_code") if cfg.get(name)}}
                    record = _video_record(item, store, existing.get(ident), query, context)
                    records.append(record)
                    existing[ident] = record
                state.update(page_token=next_token, seen_tokens=token_history, done=not bool(next_token),
                             pages_total=state.get("pages_total", 0) + 1)
                store.save_batch(records, checkpoint_key=checkpoint, state=state)
                summary["records"] += len(records)
                if state["done"]:
                    break
        except _LimitReached:
            stopped = True
        summary["queries"].append({"query": query, "status": "complete" if state.get("done") else "partial"})
        if stopped:
            break
    # Fetch statistics for the discovered sample and explicit seeds. If budget
    # ends here, run() refreshes these details before collecting comments.
    ids = list(dict.fromkeys([*existing, *(video_id(v) for v in cfg.get("video_ids", []))]))
    if cfg.get("refresh_video_metadata", True):
        try:
            missing = _refresh_videos(ids, store, client, key, budget, existing)
            summary["unavailable_videos"] = sorted(missing)
        except _LimitReached:
            summary["metadata_status"] = "partial"
    else:
        # Search has an independent quota bucket. Candidate snippets must remain
        # collectible after the general videos/comments quota is exhausted.
        summary["metadata_status"] = "deferred"
    summary.update(requests=budget.requests, pages=budget.pages,
                   status="partial" if stopped or summary.get("metadata_status") == "partial"
                   or any(x["status"] == "partial" for x in summary["queries"]) else "complete")
    return summary


def _initial_state():
    return {"page_token": None, "threads_exhausted": False, "pending_replies": [],
            "done": False, "status": "partial", "pages_total": 0, "skipped_reply_threads": 0}


def _collect_video(ident, config, store, client, key, budget, checkpoint, state, include_replies):
    processed = 0
    while not state.get("done"):
        pending = state.get("pending_replies", [])
        if not pending and state.get("threads_exhausted"):
            state.update(done=True, status="complete")
            store.set_state(checkpoint, state)
            break
        is_reply = bool(pending)
        if is_reply:
            current = pending[0]
            endpoint = "comments"
            params = {"part": "snippet", "parentId": current["parent_id"], "maxResults": 100,
                      "textFormat": "plainText", "key": key}
            token = current.get("page_token")
        else:
            endpoint = "commentThreads"
            params = {"part": "snippet,replies", "videoId": ident, "maxResults": 100,
                      "order": "time", "textFormat": "plainText", "key": key}
            token = state.get("page_token")
        if token:
            params["pageToken"] = token
        try:
            data = budget.get(client, endpoint, params, page=True)
        except _LimitReached:
            return processed, True
        except ApiError as exc:
            if exc.reason == "commentsDisabled":
                state.update(done=True, status="comments_disabled")
                store.set_state(checkpoint, state)
                break
            if exc.reason in {"videoNotFound", "notFound"} and not is_reply:
                state.update(done=True, status="unavailable")
                store.set_state(checkpoint, state)
                break
            if exc.reason in {"commentNotFound", "notFound"} and is_reply:
                state["pending_replies"].pop(0)
                state["skipped_reply_threads"] = state.get("skipped_reply_threads", 0) + 1
                store.set_state(checkpoint, state)
                continue
            # Keep the cursor unchanged on quota/auth/network failures.
            raise
        records = []
        items = _items(data, endpoint)
        cursor_state = current if is_reply else state
        next_token, token_history = _next_token(data, token, cursor_state.get("seen_tokens"), endpoint)
        if is_reply:
            for item in items:
                record = _comment_record(item, ident, store, parent=current["parent_id"])
                if record["created_at"] and in_window(record["created_at"], config):
                    records.append(record)
            if next_token:
                state["pending_replies"][0].update(page_token=next_token, seen_tokens=token_history)
            else:
                state["pending_replies"].pop(0)
        else:
            for item in items:
                snippet = item.get("snippet")
                if not isinstance(snippet, dict) or not isinstance(snippet.get("topLevelComment"), dict):
                    raise _invalid_payload("commentThreads")
                top = snippet["topLevelComment"]
                reply_count = _integer(snippet.get("totalReplyCount"))
                if reply_count is None:
                    raise _invalid_payload("commentThreads")
                record = _comment_record(top, ident, store, replies=reply_count)
                if record["created_at"] and in_window(record["created_at"], config):
                    records.append(record)
                # Even an old parent may have recent replies. Do not stop on
                # parent dates, and do not rely on the embedded reply subset.
                if include_replies and reply_count:
                    embedded = _complete_embedded_replies(item, top["id"], reply_count, ident, store)
                    if embedded is None:
                        state["pending_replies"].append({"parent_id": top["id"], "page_token": None})
                    else:
                        records.extend(reply for reply in embedded if in_window(reply["created_at"], config))
            state.update(page_token=next_token, seen_tokens=token_history, threads_exhausted=not bool(next_token))
        state["pages_total"] = state.get("pages_total", 0) + 1
        if state["threads_exhausted"] and not state["pending_replies"]:
            state.update(done=True, status="complete")
        # This transaction is the resume guarantee: a next-page token never
        # commits before the records and all outstanding reply jobs commit.
        store.save_batch(records, checkpoint_key=checkpoint, state=state)
        processed += len(records)
    return processed, False


def run(config: dict, store, client, *, restart=False, max_pages=None) -> dict:
    key = _api_key()
    cfg = config.get("youtube", {})
    include_replies = bool(cfg.get("include_replies", True))
    existing = {r["native_id"]: r for r in store.iter_records(platform="youtube", kind="video")}
    ids = list(dict.fromkeys([*(video_id(v) for v in cfg.get("video_ids", [])), *existing]))
    if not ids:
        raise ValueError("尚无 YouTube 视频：请先执行 youtube-discover，或在 youtube.video_ids 配置视频 ID/URL。")
    budget = _Budget(config, cfg.get("max_pages", 100) if max_pages is None else max_pages)
    summary = {"platform": "youtube", "operation": "comments", "videos": [], "records": 0}
    jobs = []
    for ident in ids:
        scope = config.get("collection", {})
        dates = {name: utc_iso(scope[name]) if scope.get(name) else None for name in ("start", "end")}
        settings = {"video_id": ident, "include_replies": include_replies, "collection": dates, "operation": "comments"}
        checkpoint = job_key("youtube", settings)
        cached = None if restart else store.get_state(checkpoint)
        state = copy.deepcopy(cached) if cached else _initial_state()
        jobs.append((ident, checkpoint, state))
    # Refresh once per resumable comment traversal, including when metadata
    # alone fills this invocation's request budget. Otherwise a small budget
    # repeatedly refreshes the first batch and never reaches comment pages.
    # --restart builds fresh states and therefore refreshes metadata again.
    metadata_jobs = [job for job in jobs
                     if not job[2].get("done") and not job[2].get("metadata_refreshed")]
    try:
        for offset in range(0, len(metadata_jobs), 50):
            batch = metadata_jobs[offset:offset + 50]
            missing = _refresh_videos([ident for ident, _, _ in batch], store, client, key, budget, existing)
            # The records have committed before these flags. An interruption
            # between the commits only replays a metadata refresh; a failed
            # batch cannot be mistaken for successfully refreshed metadata.
            for ident, checkpoint, state in batch:
                state["metadata_refreshed"] = True
                if ident in missing:
                    state.update(done=True, status="unavailable")
                store.set_state(checkpoint, state)
    except _LimitReached:
        summary.update(status="partial", requests=budget.requests, pages=budget.pages)
        summary["videos"] = [{"video_id": ident, "status": state.get("status", "partial")} for ident, _, state in jobs]
        return summary
    stopped = False
    for ident, checkpoint, state in jobs:
        if not state.get("done"):
            processed, stopped = _collect_video(ident, config, store, client, key, budget, checkpoint, state, include_replies)
            summary["records"] += processed
        summary["videos"].append({"video_id": ident, "status": state.get("status", "partial"),
                                  "pages_total": state.get("pages_total", 0),
                                  "pending_reply_threads": len(state.get("pending_replies", [])),
                                  "skipped_reply_threads": state.get("skipped_reply_threads", 0)})
        if stopped:
            # Include unvisited jobs explicitly so a limited run cannot look
            # as though its remaining videos were successfully enumerated.
            seen = {entry["video_id"] for entry in summary["videos"]}
            summary["videos"].extend({"video_id": other, "status": other_state.get("status", "partial")}
                                     for other, _, other_state in jobs if other not in seen)
            break
    summary.update(requests=budget.requests, pages=budget.pages,
                   status="partial" if stopped else "complete")
    return summary
