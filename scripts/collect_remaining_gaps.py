"""Fill the remaining full-cycle date gaps with a frozen, bounded campaign."""
from argparse import ArgumentParser
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import random
import re

from collect_full_cycle import (
    BEIJING, DAY, END, START, REDDIT_ROUND_LIMIT, archive, check_disk, emit, local,
    new_records, reddit_slice, scope, steam, youtube,
)
from wukong_collect.cli import load_config, safe_error
from wukong_collect.common import in_window, job_key, utc_iso
from wukong_collect.http import ApiError, HttpClient
from wukong_collect.store import Store

LABEL = "midterm_remaining_gaps"
YOUTUBE_MONTH_TARGET = 500
YOUTUBE_VIDEO_LIMIT = 100
YOUTUBE_GAME_TITLE = re.compile(
    r"black[\W_]*myth[\W_]*wukong|黑神[话話][\W_]*悟空|"
    r"黒神話[\W_]*悟空|ブラックミス[\W_]*悟空|블랙\s*미스\s*오공", re.I
)


def day_counts(store, platform, config):
    lo, hi = (config["collection"][key] for key in ("start", "end"))
    return Counter(dict(store.db.execute(
        """SELECT date(created_at,'+8 hours'),count(*) FROM records
           WHERE platform=? AND kind=? AND created_at>=? AND created_at<? GROUP BY 1""",
        (platform, "review" if platform == "steam" else "comment", lo, hi),
    )))


def steam_day(config, store, client, day, limit, launch):
    app_id = config["steam"]["app_id"]
    selected = []
    returned = 0
    reported_totals = []
    hours = range(0, 24, 6) if launch else (0,)
    for index, hour in enumerate(hours, 1):
        start = local(day, hour)
        end = start + timedelta(hours=6 if launch else 24)
        params = {
            "appid": app_id, "filter": 1, "languages": ["all"], "num_per_page": 100,
            "review_type": 0, "purchase_type": 1,
            "filter_offtopic_activity": bool(config["steam"].get("filter_offtopic_activity")),
            "date_range_start": int(start.timestamp()), "date_range_end": int(end.timestamp()) - 1,
            "cursor": "*",
        }
        response = client.get_json("https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/",
                                   params={"input_json": json.dumps(params)})
        payload = response.get("response", {})
        total = payload.get("total_matching")
        rows = payload.get("reviews", [] if total == 0 else None)
        if not isinstance(rows, list):
            raise ApiError("Steam 响应缺少评测列表", reason="invalid_response")
        returned += len(rows)
        reported_totals.append(total)
        window = scope(config, start, end)
        records = list(new_records(store, (steam.normalize(row, app_id, store)
                       for row in rows if in_window(row["timestamp_created"], window))))
        random.Random(f"{LABEL}/steam/{day}/{hour}").shuffle(records)
        remaining = limit * index // len(hours) - len(selected)
        selected.extend(records[:remaining])
    reported_total = sum(reported_totals) if all(isinstance(n, int) for n in reported_totals) else None
    return selected, dict(returned=returned, api_total_matching=reported_total)


def reddit_day(config, store, client, day, limit):
    selected = []
    roots = set()
    returned = capped = timeouts = 0
    # Rotate the starting time; on low-activity days inspect the other slices.
    for i in range(4):
        hour = ((day - START).days + i) % 4 * 6
        start = local(day, hour)
        end = start + timedelta(hours=6)
        task = archive._task("comment", "blackmythwukong", int(start.timestamp()), int(end.timestamp()))
        rows, missing, cap = reddit_slice(client, task)
        returned += len(rows)
        capped += bool(cap)
        timeouts += missing
        mapped = archive._mapped_config(scope(config, start, end))
        records = list(new_records(store, archive._records(rows, task, mapped, store)))
        random.Random(f"{LABEL}/reddit/{day}/{hour}").shuffle(records)
        for record in records:
            if record["root_id"] in roots:
                continue
            roots.add(record["root_id"])
            selected.append(record)
            if len(selected) == limit:
                return selected, dict(returned=returned, capped_slices=capped, timeout_segments=timeouts)
    return selected, dict(returned=returned, capped_slices=capped, timeout_segments=timeouts)


def fill_days(platform, config, store, client):
    plan_key = job_key(LABEL, {"platform": platform, "v": 1})
    plan = store.get_state(plan_key)
    if plan is None:
        counts = day_counts(store, platform, config)
        days = [START + i * DAY for i in range((END - START).days)]
        if platform == "steam":
            plan = {}
            for day in days:
                launch = day < date(2024, 9, 7)
                prior = counts[str(day)]
                if launch and prior < 120:
                    plan[str(day)] = {"limit": 120 - prior, "launch": True}
                elif not prior:
                    plan[str(day)] = {"limit": 40, "launch": False}
        else:
            prior = store.db.execute(
                """SELECT count(*) FROM records WHERE platform='reddit' AND kind='comment'
                   AND json_extract(metadata,'$.selection') IN (
                   'midterm_pilot_time_slice','midterm_weekly_time_slice')"""
            ).fetchone()[0]
            budget = max(0, REDDIT_ROUND_LIMIT - prior)
            blanks = [str(day) for day in days if not counts[str(day)]]
            plan = {day: {"limit": min(3, budget - 3 * i)} for i, day in enumerate(blanks)
                    if 3 * i < budget}
        store.set_state(plan_key, plan)
    emit(platform=platform, event="plan", days=len(plan), limit=sum(d["limit"] for d in plan.values()))
    results = []
    for stamp, item in plan.items():
        check_disk(store)
        key = job_key(LABEL + "-day", {"plan": plan_key, "day": stamp})
        result = store.get_state(key)
        if result is None:
            kwargs = (config, store, client, date.fromisoformat(stamp), item["limit"])
            selected, detail = steam_day(*kwargs, item["launch"]) if platform == "steam" else reddit_day(*kwargs)
            for record in selected:
                record["metadata"].update(selection=LABEL, sample_day=stamp)
            result = {"day": stamp, **detail, "new": len(selected)}
            store.save_batch(selected, key, result)
            if len(results) % 20 == 0 or len(results) + 1 == len(plan):
                emit(platform=platform, event="day_saved", done=len(results) + 1, total=len(plan), **result)
        results.append(result)
    return results


def plan_youtube(config, store, excluded=()):
    months = Counter()
    used = Counter()
    for row in store.db.execute(
        """SELECT substr(date(created_at,'+8 hours'),1,7) AS month, root_id, count(*) AS n
           FROM records WHERE platform='youtube' AND kind='comment'
           AND created_at>=? AND created_at<? GROUP BY 1,2""",
        tuple(config["collection"][k] for k in ("start", "end")),
    ):
        months[row["month"]] += row["n"]
        used[row["root_id"]] += row["n"]
    # The opening and closing months are partial, so use the floor only
    # for complete months of publication time.
    candidates = {
        month: {"before": n, "videos": []}
        for month, n in sorted(months.items(), key=lambda item: (item[1], item[0]))
        if str(START)[:7] < month < str(END)[:7] and n < YOUTUBE_MONTH_TARGET
    }
    pools = defaultdict(list)
    channels = Counter()
    for row in store.db.execute(
        """SELECT native_id, author_hash, reply_count,
                  json_extract(metadata, '$.title') AS title,
                  substr(date(created_at,'+8 hours'),1,7) AS month
           FROM records WHERE platform='youtube' AND kind='video'"""
    ):
        if used[row["native_id"]]:
            channels[row["author_hash"]] += 1
            continue
        if row["native_id"] not in excluded and row["month"] in candidates and YOUTUBE_GAME_TITLE.search(row["title"] or ""):
            if row["reply_count"] is None or row["reply_count"] >= 10:
                pools[row["month"]].append(row)
    for month, detail in candidates.items():
        pool = pools[month]
        for _ in range(min(len(pool), 10)):
            video = min(pool, key=lambda row: (
                channels[row["author_hash"]], -min(row["reply_count"] or 0, 100), row["native_id"],
            ))
            detail["videos"].append(video["native_id"])
            channels[video["author_hash"]] += 1
            pool.remove(video)
    return candidates


def youtube_video(config, store, client, ident, api_key, key, limit):
    state = store.get_state(key) or {"new": 0, "pages": 0, "token": None, "seen": []}
    while not state.get("done"):
        params = {"part": "snippet,replies", "videoId": ident, "order": "time",
                  "maxResults": 100, "textFormat": "plainText", "key": api_key}
        if state["token"]:
            params["pageToken"] = state["token"]
        try:
            data = client.get_json(f"{youtube.BASE}/commentThreads", params=params)
        except ApiError as exc:
            if exc.reason not in {"commentsDisabled", "videoNotFound", "notFound"}:
                raise
            state.update(done=True, reason=exc.reason)
            store.set_state(key, state)
            break
        token, seen = youtube._next_token(data, state["token"], state["seen"], "commentThreads")
        records = []
        for item in youtube._items(data, "commentThreads"):
            snippet = item["snippet"]
            top = snippet["topLevelComment"]
            records.append(youtube._comment_record(top, ident, store, replies=snippet.get("totalReplyCount")))
            records.extend(
                youtube._comment_record(reply, ident, store, parent=top["id"])
                for reply in item.get("replies", {}).get("comments", [])
            )
        selected = list(new_records(store, (r for r in records if in_window(r["created_at"], config))))
        random.Random(f"{LABEL}/youtube/{ident}/{state['pages']}").shuffle(selected)
        selected = selected[:limit - state["new"]]
        for record in selected:
            record["metadata"].update(selection=LABEL, sample_month=config["collection"]["sample_month"])
        pages = state["pages"] + 1
        total = state["new"] + len(selected)
        reason = None
        if total >= limit:
            reason = "comment_cap"
        elif not token:
            reason = "exhausted"
        elif pages >= 8:
            reason = "page_cap"
        state.update(pages=pages, new=total, token=token, seen=seen, done=bool(reason), reason=reason)
        store.save_batch(selected, key, state)
    return {"id": ident, **{field: state[field] for field in ("new", "pages", "reason")}}


def month_dates(month):
    start = date.fromisoformat(month + "-01")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def discover_sparse_months(config, store, client, api_key):
    previous = store.get_state(job_key(LABEL, {"platform": "youtube", "v": 1})) or {}
    excluded = {ident for detail in previous.values() for ident in detail["videos"]}
    remaining = plan_youtube(config, store, excluded)
    for month in remaining:
        check_disk(store)
        key = job_key(LABEL + "-discovery", {"month": month})
        if store.get_state(key):
            continue
        start, end = month_dates(month)
        cfg = deepcopy(config)
        cfg["youtube"].update(
            queries=["Black Myth Wukong"], search_pages=1,
            search_order="viewCount", refresh_video_metadata=False,
            published_after=utc_iso(local(start)), published_before=utc_iso(local(end)),
        )
        try:
            result = youtube.discover(cfg, store, client)
        except ApiError as exc:
            if exc.reason not in {"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}:
                raise
            emit(platform="youtube", event="search_quota", reason=exc.reason)
            break
        store.set_state(key, {"returned": result["records"]})
        emit(platform="youtube", event="discovery", month=month, returned=result["records"])

    # Obtain comment totals for the title-screened candidates in the target
    # publication months. No comment pages are used for this ranking.
    existing = {r["native_id"]: r for r in store.iter_records(platform="youtube", kind="video")}
    ids = [ident for ident, row in existing.items() if ident not in excluded
           and row["created_at"]
           and datetime.fromisoformat(row["created_at"]).astimezone(BEIJING).strftime("%Y-%m") in remaining
           and YOUTUBE_GAME_TITLE.search(row["metadata"].get("title") or "")
           and row["reply_count"] is None]
    youtube._refresh_videos(ids, store, client, api_key, youtube._Budget(config, None), existing)
    return plan_youtube(config, store, excluded)


def fill_youtube(config, store, client, *, discover=False):
    api_key = youtube._api_key()
    plan_key = job_key(LABEL, {"platform": "youtube", "v": 2 if discover else 1})
    plan = store.get_state(plan_key)
    if plan is None:
        plan = discover_sparse_months(config, store, client, api_key) if discover else plan_youtube(config, store)
        store.set_state(plan_key, plan)
    emit(platform="youtube", event="plan", months=len(plan), target_per_month=YOUTUBE_MONTH_TARGET,
         limit=sum(YOUTUBE_MONTH_TARGET - detail["before"] for detail in plan.values()))
    results = []
    for month, detail in plan.items():
        start, end = month_dates(month)
        cfg = scope(config, local(start), local(end))
        cfg["collection"]["sample_month"] = month
        added = 0
        videos = []
        for ident in detail["videos"]:
            check_disk(store)
            limit = min(YOUTUBE_VIDEO_LIMIT, YOUTUBE_MONTH_TARGET - detail["before"] - added)
            if limit <= 0:
                break
            key = job_key(LABEL + "-video", {"plan": plan_key, "month": month, "id": ident})
            result = youtube_video(cfg, store, client, ident, api_key, key, limit)
            videos.append(result)
            added += result["new"]
        entry = {"month": month, "before": detail["before"], "new": added, "videos": videos}
        results.append(entry)
        emit(platform="youtube", event="month_saved", month=month, before=detail["before"], new=added, videos=len(videos))
    return results


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("steam", "reddit", "youtube"))
    parser.add_argument("--discover-youtube", action="store_true", help="Discover more videos in remaining sparse months")
    args = parser.parse_args()
    if args.discover_youtube and args.platform != "youtube":
        parser.error("--discover-youtube requires the youtube platform")
    platform = args.platform
    config = scope(load_config("config.toml"), local(START), local(END))
    config["network"]["min_interval"] = {"steam": 0.45, "reddit": 0.6, "youtube": 0.35}[platform]
    collection = config["collection"]
    store = Store(collection["database"], collection["hash_key_file"])
    client = HttpClient(config)
    run_id = store.start_run(platform, {**config, "job": LABEL})
    try:
        if platform == "youtube":
            details = fill_youtube(config, store, client, discover=args.discover_youtube)
        else:
            details = fill_days(platform, config, store, client)
        summary = dict(platform=platform, requests=client.requests_made, new=sum(row["new"] for row in details))
        store.finish_run(run_id, "complete", {**summary, "details": details})
        emit(event="complete", run_id=run_id, **summary)
    except (Exception, KeyboardInterrupt) as exc:
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"
        detail = {"error": safe_error(exc), "requests": client.requests_made}
        store.finish_run(run_id, status, detail)
        emit(event=status, platform=platform, run_id=run_id, **detail)
        raise SystemExit(130 if status == "interrupted" else 1)
    finally:
        client.close()
        store.close()


if __name__ == "__main__":
    main()
