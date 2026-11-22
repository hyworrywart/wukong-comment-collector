from __future__ import annotations

from datetime import datetime
import json
import math

from .common import in_window, job_key, positive_int, utc_iso
from .http import ApiError


def normalize(review, app_id, store):
    author = review.get("author", {})
    keep = ("voted_up", "votes_funny", "weighted_vote_score", "steam_purchase", "received_for_free", "written_during_early_access", "primarily_steam_deck", "refunded", "timestamp_dev_responded", "review_quality", "hidden_in_steam_china")
    metadata = {key: review[key] for key in keep if key in review}
    metadata.update({"app_id": app_id, "playtime_forever": author.get("playtime_forever"), "playtime_at_review": author.get("playtime_at_review"), "playtime_last_two_weeks": author.get("playtime_last_two_weeks"), "language_source": "steam_review_language"})
    return {"platform": "steam", "kind": "review", "native_id": str(review["recommendationid"]),
            "parent_id": None, "root_id": str(app_id), "author_hash": store.author_hash("steam", author.get("steamid")),
            "text": review.get("review", ""), "created_at": utc_iso(review["timestamp_created"]),
            "updated_at": utc_iso(review.get("timestamp_updated", review["timestamp_created"])),
            "language": review.get("language"), "like_count": review.get("votes_up"), "reply_count": review.get("comment_count"),
            # Review permalinks contain a Steam account ID, so use the app page.
            "url": f"https://store.steampowered.com/app/{app_id}/#app_reviews_hash", "metadata": metadata}


def run(config, store, client, *, restart=False, max_pages=None):
    cfg = config.get("steam", {})
    app_id = positive_int(cfg.get("app_id", 2358720), "steam.app_id")
    languages = cfg.get("languages", ["all"])
    if not languages or not isinstance(languages, list) or ("all" in languages and len(languages) > 1):
        raise ValueError("steam.languages 应为语言代码列表；all 不应与其他语言混用")
    limit = positive_int(max_pages if max_pages is not None else cfg.get("max_pages", 10), "max_pages")
    size = positive_int(cfg.get("page_size", 100), "steam.page_size")
    if size > 100:
        raise ValueError("steam.page_size 不能超过 100")
    if cfg.get("review_type", "all") not in {"all", "positive", "negative"}:
        raise ValueError("steam.review_type 无效")
    if cfg.get("purchase_type", "all") not in {"all", "steam", "non_steam_purchase"}:
        raise ValueError("steam.purchase_type 无效")
    results = []
    pages_left = limit
    for language in dict.fromkeys(languages):
        params = {"appid": app_id, "filter": 1, "languages": [language], "num_per_page": size,
                  "review_type": {"all": 0, "positive": 1, "negative": 2}[cfg.get("review_type", "all")],
                  "purchase_type": {"steam": 0, "all": 1, "non_steam_purchase": 2}[cfg.get("purchase_type", "all")],
                  "filter_offtopic_activity": bool(cfg.get("filter_offtopic_activity", 0))}
        scope = config.get("collection", {})
        if scope.get("start") and scope.get("end"):
            # Steam accepts integer seconds and an inclusive upper bound.
            params["date_range_start"] = math.ceil(datetime.fromisoformat(utc_iso(scope["start"]).replace("Z", "+00:00")).timestamp())
            params["date_range_end"] = math.ceil(datetime.fromisoformat(utc_iso(scope["end"]).replace("Z", "+00:00")).timestamp()) - 1
        key = job_key("steam", {"app_id": app_id, "params": params, "collection": {k: config.get("collection", {}).get(k) for k in ("start", "end")}})
        state = {} if restart else (store.get_state(key) or {})
        if state.get("done"):
            results.append({"language": language, "status": "already_complete", "pages": 0, "saved": 0})
            continue
        if pages_left == 0:
            results.append({"language": language, "status": "partial", "pages": 0, "saved": 0})
            continue
        cursor = state.get("cursor", "*")
        pages, saved = 0, 0
        while pages < pages_left:
            result = client.get_json("https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/", params={"input_json": json.dumps({**params, "cursor": cursor})})
            payload = result.get("response", {})
            if not isinstance(payload, dict):
                raise ApiError("Steam 评论接口响应结构无效；本页未推进断点", reason="invalid_response")
            reviews = payload.get("reviews")
            summary = payload.get("query_summary")
            # Steam may omit the reviews array when it reports an empty page.
            if "reviews" not in payload and (
                payload.get("total_matching") == 0
                or (isinstance(summary, dict) and summary.get("num_reviews") == 0)
            ):
                reviews = []
            if not isinstance(reviews, list):
                raise ApiError("Steam 评论接口响应结构无效；本页未推进断点", reason="invalid_response")
            records = [normalize(r, app_id, store) for r in reviews if in_window(r.get("timestamp_created"), config)]
            pages += 1
            saved += len(records)
            next_cursor = payload.get("cursor")
            start = config.get("collection", {}).get("start")
            reached_start = bool(reviews and start and all(utc_iso(r["timestamp_created"]) < utc_iso(start) for r in reviews))
            exhausted = not reviews or reached_start
            if reviews and not exhausted and (not next_cursor or next_cursor == cursor):
                store.save_batch(records)
                raise ApiError("Steam 分页游标未推进；保留本页断点供排查", reason="cursor_stalled")
            state = {"cursor": next_cursor or cursor, "done": exhausted, "pages_total": state.get("pages_total", 0) + 1,
                     "last_collected_at": utc_iso(), "stop_reason": "start_boundary" if reached_start else "exhausted" if exhausted else "page_limit",
                     "query_summary": payload.get("query_summary", state.get("query_summary", {})), "total_matching": payload.get("total_matching", state.get("total_matching"))}
            store.save_batch(records, key, state)
            cursor = next_cursor
            if exhausted:
                break
        pages_left -= pages
        results.append({"language": language, "status": "complete" if state.get("done") else "partial", "pages": pages, "saved": saved, "checkpoint": key})
    return {"platform": "steam", "status": "complete" if all(x["status"] in ("complete", "already_complete") for x in results) else "partial", "languages": results}
