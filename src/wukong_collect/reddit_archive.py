"""Arctic Shift public archive adapter with loss-detecting time partitioning.

The API has no cursor or completeness guarantee. Fixed-size requests bisect
saturated windows. Optional auto_time uses the documented dynamic 100–1000
capacity and commits only the complete prefix before the last returned second,
then repeats that second. A saturated single second fails instead of losing IDs.
"""
from __future__ import annotations

import copy
from datetime import datetime
import math
import re

from .common import job_key, utc_iso
from .http import ApiError
from .reddit import RedditConfigError, normalize

BASE = "https://arctic-shift.photon-reddit.com"
LIMIT = 100
AUTO_LIMIT = 1000
_COMMUNITY = re.compile(r"^[A-Za-z0-9_]{1,100}$")
_ID = re.compile(r"^[a-z0-9]+$")
_COMMON = {"native_id": "id", "created_at": "created_utc", "subreddit": "subreddit",
           "author_hash": "author", "score": "score"}
_COMMENT_FIELDS = {**_COMMON, "text": "body", "root_id": "link_id", "parent_id": "parent_id"}
_POST_FIELDS = {**_COMMON, "text": "selftext", "title": "title", "reply_count": "num_comments"}


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RedditConfigError(f"{name} 必须是正整数。")
    return value


def _date(value, name):
    if value in (None, ""):
        raise RedditConfigError(f"Arctic Shift 必须配置 {name}，避免无界检索。")
    try:
        iso = utc_iso(value)
        seconds = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        raise RedditConfigError(f"{name} 日期无效。") from None
    return iso, seconds


def _communities(values, name):
    if not isinstance(values, list):
        raise RedditConfigError(f"{name} 应为社区名列表。")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise RedditConfigError(f"{name} 应为社区名列表。")
        value = value.removeprefix("r/").strip().lower()
        if not _COMMUNITY.fullmatch(value):
            raise RedditConfigError(f"{name} 含无效社区名。")
        if value not in result:
            result.append(value)
    return result


def _settings(config, max_pages):
    settings = config.get("reddit", {})
    scope = config.get("collection", {})
    start, start_s = _date(scope.get("start"), "collection.start")
    end, end_s = _date(scope.get("end"), "collection.end")
    posts_start, posts_s = _date(settings.get("archive_posts_start", "2005-01-01"), "reddit.archive_posts_start")
    if start_s >= end_s or posts_s >= end_s:
        raise RedditConfigError("Arctic Shift 起始日期必须早于 collection.end。")
    communities = _communities(settings.get("subreddits", []), "reddit.subreddits")
    all_comments = _communities(settings.get("archive_all_comments_subreddits", ["BlackMythWukong"]),
                                "reddit.archive_all_comments_subreddits")
    if not communities:
        raise RedditConfigError("Arctic Shift 必须指定至少一个 reddit.subreddits。")
    if not set(all_comments) <= set(communities):
        raise RedditConfigError("archive_all_comments_subreddits 必须是 reddit.subreddits 的子集。")
    raw_keywords = settings.get("keywords", [])
    if not isinstance(raw_keywords, list) or any(not isinstance(k, str) for k in raw_keywords):
        raise RedditConfigError("reddit.keywords 应为关键词字符串列表。")
    keywords = sorted(set(k.strip() for k in raw_keywords if k.strip()))
    if set(communities) - set(all_comments) and not keywords:
        raise RedditConfigError("非全量评论社区需要配置 reddit.keywords，用于先选帖再采整帖回复。")
    page_limit = _positive(max_pages if max_pages is not None else settings.get("max_pages", 10), "max_pages")
    if settings.get("archive_page_mode", "fixed") not in ("fixed", "auto_time"):
        raise RedditConfigError("reddit.archive_page_mode 必须为 fixed 或 auto_time。")
    identity = {"adapter_version": 1, "source": "arctic_shift", "start": start, "end": end,
                "posts_start": posts_start, "subreddits": sorted(communities),
                "all_comments_subreddits": sorted(all_comments), "keywords": keywords}
    return identity, page_limit, (math.ceil(start_s), math.ceil(end_s)), math.ceil(posts_s)


def _bare_id(value, kind):
    prefix = "t1_" if kind == "comment" else "t3_"
    if not isinstance(value, str):
        raise RedditConfigError("Arctic Shift 返回无效记录 ID。")
    if value.startswith(("t1_", "t3_")):
        if not value.startswith(prefix):
            raise RedditConfigError("Arctic Shift 返回 ID 类型前缀错误。")
        value = value[len(prefix):]
    if not _ID.fullmatch(value):
        raise RedditConfigError("Arctic Shift 返回非 base36 记录 ID。")
    return value


def _epoch(value):
    if isinstance(value, bool):
        raise RedditConfigError("Arctic Shift created_utc 应为整数秒。")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    raise RedditConfigError("Arctic Shift created_utc 应为整数秒。")


def _deleted(row, kind):
    meta = row.get("_meta", {})
    if not isinstance(meta, dict):
        raise RedditConfigError("Arctic Shift _meta 应为对象。")
    later = meta.get("was_deleted_later", False)
    if later not in (True, False, 0, 1, "true", "false", "1", "0", None):
        raise RedditConfigError("Arctic Shift was_deleted_later 标志无效。")
    if later is True or later == 1 or later in ("true", "1"):
        return True
    names = ["body"] if kind == "comment" else ["selftext", "title"]
    return any(row[name].strip() in {"[removed]", "[deleted]"} for name in names)


def _rows(payload, task, *, auto_time=False):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or "error" in payload:
        raise RedditConfigError("Arctic Shift 响应不是预期的 data 列表；断点未推进。")
    rows = payload["data"]
    if len(rows) > (AUTO_LIMIT if auto_time else LIMIT):
        raise RedditConfigError("Arctic Shift 未遵守请求的返回上限；断点未推进。")
    previous = None
    ids = set()
    kind = task["kind"]
    required = set((_COMMENT_FIELDS if kind == "comment" else _POST_FIELDS).values())
    for row in rows:
        if not isinstance(row, dict) or not required <= row.keys():
            raise RedditConfigError("Arctic Shift 来源记录缺少必要字段；断点未推进。")
        ident = _bare_id(row["id"], kind)
        if ident in ids:
            raise RedditConfigError("Arctic Shift 单页出现重复 ID；无法判断覆盖范围。")
        ids.add(ident)
        when = _epoch(row["created_utc"])
        if not task["lo"] <= when < task["hi"]:
            raise RedditConfigError("Arctic Shift 返回超出请求时间切片的记录；断点未推进。")
        if previous is not None and when < previous:
            raise RedditConfigError("Arctic Shift 未遵守 sort=asc；断点未推进。")
        previous = when
        if not isinstance(row["subreddit"], str) or row["subreddit"].removeprefix("r/").lower() != task["subreddit"]:
            raise RedditConfigError("Arctic Shift 返回了错误社区的记录；断点未推进。")
        if not isinstance(row["score"], int) or isinstance(row["score"], bool):
            raise RedditConfigError("Arctic Shift score 应为整数。")
        text_fields = ["body"] if kind == "comment" else ["selftext", "title"]
        if any(not isinstance(row[name], str) for name in text_fields):
            raise RedditConfigError("Arctic Shift 返回非文本正文。")
        if kind == "comment":
            root = _bare_id(row["link_id"], "post")
            parent = row["parent_id"]
            if not isinstance(parent, str) or not parent.startswith(("t1_", "t3_")):
                raise RedditConfigError("Arctic Shift parent_id 缺少评论/帖子类型前缀。")
            _bare_id(parent, "comment" if parent.startswith("t1_") else "post")
            if task.get("root_id") and root != task["root_id"]:
                raise RedditConfigError("Arctic Shift 返回了不属于所选帖子的评论。")
        elif not isinstance(row["num_comments"], int) or isinstance(row["num_comments"], bool) or row["num_comments"] < 0:
            raise RedditConfigError("Arctic Shift num_comments 应为非负整数。")
        _deleted(row, kind)  # Validate optional archive deletion metadata too.
    return rows


def _mapped_config(config):
    result = copy.deepcopy(config)
    result["reddit"] = {**result.get("reddit", {}), "fields": _COMMENT_FIELDS,
                        "post_fields": _POST_FIELDS, "timestamp_format": "epoch_seconds", "source": "arctic_shift"}
    return result


def _records(rows, task, config, store):
    records = []
    for row in rows:
        if _deleted(row, task["kind"]):
            continue
        record = normalize(row, task["kind"], config, store)
        if record is None:
            continue
        bare = _bare_id(row["id"], task["kind"])
        root = _bare_id(row["link_id"], "post") if task["kind"] == "comment" else bare
        record["url"] = f"https://www.reddit.com/r/{task['subreddit']}/comments/{root}/"
        if task["kind"] == "comment":
            record["url"] += f"_/{bare}/"
        record["metadata"].update({"source": "arctic_shift", "archive_snapshot": True,
                                    "archive_snapshot_collected_at": utc_iso(),
                                    "archive_endpoint": f"/api/{'comments' if task['kind'] == 'comment' else 'posts'}/search",
                                    "selection": "keyword_matched_post" if task.get("query") else "entire_subreddit"})
        if task.get("query"):
            record["metadata"]["seed_query"] = task["query"]
        if row.get("retrieved_on") is not None:
            retrieved = _epoch(row["retrieved_on"])
            record["metadata"].update(retrieved_on=retrieved, archive_retrieved_at=utc_iso(retrieved))
        records.append(record)
    return records


def _task(kind, subreddit, lo, hi, *, query=None, root_id=None):
    return {"kind": kind, "subreddit": subreddit, "lo": lo, "hi": hi,
            **({"query": query} if query else {}), **({"root_id": root_id} if root_id else {})}


def _initial(identity, bounds, posts_start):
    lo, hi = bounds
    tasks = []
    if lo < hi:
        for community in identity["subreddits"]:
            if community in identity["all_comments_subreddits"]:
                tasks.append(_task("comment", community, lo, hi))
            elif posts_start < hi:
                tasks.extend(_task("post", community, posts_start, hi, query=query) for query in identity["keywords"])
    return {"pending": list(reversed(tasks)), "seen_posts": [], "done": not tasks,
            "pages_total": 0, "saved_total": 0, "split_windows": 0, "leaf_windows": 0}


def _split_task(state, task):
    mid = (task["lo"] + task["hi"]) // 2
    state["pending"].pop()
    state["pending"].append({**task, "lo": mid})
    state["pending"].append({**task, "hi": mid})
    state["split_windows"] += 1


def run(config: dict, store, client, *, restart=False, max_pages=None) -> dict:
    """Collect a reproducible archive scope; completion is relative to archive.

    Requests are serial through the shared retry/throttle client. Every successful
    response and all resulting child tasks commit with its records atomically.
    """
    identity, page_limit, bounds, posts_start = _settings(config, max_pages)
    auto_time = config.get("reddit", {}).get("archive_page_mode", "fixed") == "auto_time"
    if client is None:
        raise RedditConfigError("Arctic Shift 模式需要共享 HttpClient。")
    key = job_key("reddit-archive", identity)
    cached = None if restart else store.get_state(key)
    state = copy.deepcopy(cached) if cached else _initial(identity, bounds, posts_start)
    mapped = _mapped_config(config)
    pages, saved = 0, 0
    if not cached:
        store.save_batch([], checkpoint_key=key, state=state)
    while state["pending"] and pages < page_limit:
        task = state["pending"][-1]
        plural = "comments" if task["kind"] == "comment" else "posts"
        params = {"subreddit": task["subreddit"], "after": task["lo"] - 1,
                  "before": task["hi"], "sort": "asc", "limit": "auto" if auto_time else LIMIT}
        if task["kind"] == "post":
            params["query"] = task["query"]
        elif task.get("root_id"):
            params["link_id"] = task["root_id"]
        try:
            payload = client.get_json(f"{BASE}/api/{plural}/search", params=params)
        except ApiError as exc:
            # Only the service's identified query timeout justifies a narrower
            # retry. Invalid filters, other 422s and network failures retain the
            # exact original task and propagate without silently changing scope.
            if exc.reason != "query_timeout":
                raise
            updated = copy.deepcopy(state)
            updated["pages_total"] += 1
            updated["timeout_splits"] = updated.get("timeout_splits", 0) + 1
            pages += 1
            if task["hi"] - task["lo"] <= 1:
                updated["blocked_window"] = task
                store.save_batch([], checkpoint_key=key, state=updated)
                raise RedditConfigError("Arctic Shift 单秒窗口查询仍超时；窗口与断点已保留，未标完成。请稍后重试或改用该月 dump 文件。") from None
            _split_task(updated, task)
            store.save_batch([], checkpoint_key=key, state=updated)
            state = updated
            continue
        rows = _rows(payload, task, auto_time=auto_time)
        updated = copy.deepcopy(state)
        updated["pages_total"] += 1
        pages += 1
        processed_rows = []
        if len(rows) >= LIMIT:
            if auto_time and _epoch(rows[-1]["created_utc"]) > task["lo"]:
                # Auto's effective cap changes with server load. A response of
                # 300 is not proof that only 300 exist. Ascending order does
                # establish the prefix before its last second as complete.
                # Repeat every row in that tail second on the next request.
                tail = _epoch(rows[-1]["created_utc"])
                processed_rows = [row for row in rows if _epoch(row["created_utc"]) < tail]
                updated["pending"][-1] = {**task, "lo": tail}
                updated["prefix_windows"] = updated.get("prefix_windows", 0) + 1
            elif task["hi"] - task["lo"] <= 1:
                updated["blocked_window"] = task
                store.save_batch([], checkpoint_key=key, state=updated)
                raise RedditConfigError("Arctic Shift 单秒窗口仍返回至少 100 条，无法保证同秒记录完整；断点保留且未标完成，请改用该月 dump 文件。")
            else:
                _split_task(updated, task)
        else:
            # Even auto always has capacity for at least 100 per its docs.
            # Fewer than 100 therefore exhausts this requested time window.
            processed_rows = rows
            updated["pending"].pop()
            updated["leaf_windows"] += 1
        records = _records(processed_rows, task, mapped, store)
        if task["kind"] == "post" and processed_rows:
            seen = set(updated["seen_posts"])
            comment_tasks = []
            for row in processed_rows:
                if _deleted(row, "post"):
                    continue
                root = _bare_id(row["id"], "post")
                if root not in seen:
                    seen.add(root)
                    comment_tasks.append(_task("comment", task["subreddit"], *bounds, query=task["query"], root_id=root))
            updated["seen_posts"] = sorted(seen)
            updated["pending"].extend(reversed(comment_tasks))
        updated["done"] = not updated["pending"]
        updated["saved_total"] += len(records)
        store.save_batch(records, checkpoint_key=key, state=updated)
        saved += len(records)
        state = updated
    return {"platform": "reddit", "mode": "arctic_shift", "source": "arctic_shift",
            "page_mode": "auto_time" if auto_time else "fixed",
            "status": "complete" if state["done"] else "partial", "pages": pages,
            "saved": saved, "records": saved, "pending_tasks": len(state["pending"]),
            "pages_total": state["pages_total"], "saved_total": state["saved_total"],
            "coverage": "third_party_archive_snapshot", "checkpoint": key}
