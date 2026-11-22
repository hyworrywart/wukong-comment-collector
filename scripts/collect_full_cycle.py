"""Bounded midterm backfill. All dates are Beijing time; existing jobs stay intact."""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import json
import random
import shutil

from wukong_collect import reddit_archive as archive, steam, youtube
from wukong_collect.cli import load_config, safe_error
from wukong_collect.common import in_window, job_key, utc_iso
from wukong_collect.http import ApiError, HttpClient
from wukong_collect.store import Store

BEIJING = timezone(timedelta(hours=8))
START, END = date(2024, 8, 20), date(2026, 11, 9)
DAY = timedelta(days=1)


def local(day, hour=0):
    return datetime.combine(day, time(hour), BEIJING)


def scope(config, start, end):
    cfg = deepcopy(config)
    cfg["collection"].update(start=utc_iso(start), end=utc_iso(end))
    return cfg


def emit(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def check_disk(store):
    if shutil.disk_usage(store.path.parent).free < 1.5 * 1024**3:
        raise RuntimeError("可用空间少于 1.5 GiB，已保留断点")


def new_records(store, records):
    seen = set()
    for record in records:
        ident = record["platform"], record["kind"], record["native_id"]
        if ident in seen:
            continue
        seen.add(ident)
        if not store.db.execute(
            "SELECT 1 FROM records WHERE platform=? AND kind=? AND native_id=?", ident
        ).fetchone():
            yield record


def steam_gaps(config, store, client):
    weeks = defaultdict(list)
    # Freeze the gap plan so reruns cannot increase a week's sample budget.
    for first, end in [(START, date(2024, 9, 7)), (date(2024, 10, 1), date(2025, 3, 5))]:
        for offset in range((end - first).days):
            day = first + offset * DAY
            weeks[(day - START).days // 7].append(day)
    details = []
    for index, days in weeks.items():
        check_disk(store)
        n = min(len(days), 3)
        chosen = [days[(2 * i + 1) * len(days) // (2 * n)] for i in range(n)]
        added = 0
        for day in chosen:
            cfg = scope(config, local(day), local(day + DAY))
            lo, hi = (cfg["collection"][k] for k in ("start", "end"))
            existing = store.db.execute(
                """SELECT count(*) FROM records WHERE platform='steam' AND kind='review'
                   AND created_at>=? AND created_at<?""", (lo, hi)
            ).fetchone()[0]
            if existing >= 80:
                continue
            cfg["steam"]["page_size"] = 80 - existing
            result = steam.run(cfg, store, client, max_pages=1)
            added += sum(item.get("saved", 0) for item in result["languages"])
        entry = {"week_start": str(START + index * 7 * DAY),
                 "selected_days": list(map(str, chosen)), "saved": added}
        details.append(entry)
        emit(platform="steam", **entry)
    return details


def reddit_slice(client, task, remaining_splits=2):
    """Recover from archive timeouts with shorter queries, keeping any holes visible."""
    try:
        payload = client.get_json(f"{archive.BASE}/api/comments/search", params={
            "subreddit": task["subreddit"], "after": task["lo"] - 1,
            "before": task["hi"], "sort": "asc", "limit": archive.LIMIT,
        })
    except ApiError as exc:
        if exc.reason != "query_timeout":
            raise
        if not remaining_splits:
            return [], 1, False
        mid = (task["lo"] + task["hi"]) // 2
        left, left_missing, left_capped = reddit_slice(
            client, {**task, "hi": mid}, remaining_splits - 1
        )
        right, right_missing, right_capped = reddit_slice(
            client, {**task, "lo": mid}, remaining_splits - 1
        )
        return left + right, left_missing + right_missing, left_capped or right_capped
    rows = archive._rows(payload, task)
    return rows, 0, len(rows) == archive.LIMIT


REDDIT_PILOT_WEEKS = (START, date(2024, 10, 15), date(2025, 6, 17),
                      date(2025, 11, 18), date(2026, 5, 19), date(2026, 11, 3))
REDDIT_ROUND_LIMIT = 15_000
REDDIT_WEEK_LIMIT = 150


def reddit_weeks(config, store, client, weeks, *, pilot):
    label = "midterm_pilot_time_slice" if pilot else "midterm_weekly_time_slice"
    namespace = "reddit-midterm-pilot" if pilot else "reddit-midterm-week"
    details = []
    for week in weeks:
        check_disk(store)
        key = job_key(namespace, {"week": str(week), "v": 1})
        previous = store.get_state(key)
        if previous:
            details.append(previous)
            continue
        threads = Counter()
        selected = []
        strata = []
        for i, offset in enumerate((1, 2, 4, 5)):
            day = week + offset * DAY
            count = 0
            # Alternate the order of the four six-hour slices by weekday.
            for j in range(4):
                hour = (i + j) % 4 * 6
                start = local(day, hour)
                end = min(start + timedelta(hours=6), local(END))
                if start >= end:
                    continue
                lo, hi = int(start.timestamp()), int(end.timestamp())
                task = archive._task("comment", "blackmythwukong", lo, hi)
                rows, unavailable, possibly_truncated = reddit_slice(client, task)
                cfg = archive._mapped_config(scope(config, start, end))
                records = list(new_records(store, archive._records(rows, task, cfg, store)))
                random.Random(f"{day}/{hour}").shuffle(records)
                limit = (38 if i < 2 else 37)
                # Spread the day budget across slices before taking later records.
                slice_limit = limit * (j + 1) // 4
                for record in records:
                    if count >= slice_limit:
                        break
                    root = record["root_id"]
                    if threads[root] >= 25:
                        continue
                    threads[root] += 1
                    count += 1
                    record["metadata"].update(selection=label, sample_week=str(week))
                    selected.append(record)
                strata.append({"date": str(day), "hour": hour, "returned": len(rows),
                               "possibly_truncated": possibly_truncated, "timeout_segments": unavailable})
            emit(platform="reddit", week_start=str(week), day=str(day), selected=count)
        entry = {"week_start": str(week), "new": len(selected),
                 "threads": len(threads), "strata": strata, "done": True}
        store.save_batch(selected, key, entry)
        details.append(entry)
        emit(platform="reddit", event="week_saved", week_start=str(week),
             new=len(selected), threads=len(threads))
    return details


def reddit_pilot(config, store, client):
    return reddit_weeks(config, store, client, REDDIT_PILOT_WEEKS, pilot=True)


def reddit_expand_weeks(config, store, client):
    plan_key = job_key("reddit-midterm-week-plan", {
        "version": 1, "start": str(START), "end": str(END),
    })
    plan = store.get_state(plan_key)
    if plan is None:
        coverage = Counter()
        lo, hi = (config["collection"][key] for key in ("start", "end"))
        for row in store.db.execute(
            """SELECT date(created_at,'+8 hours') AS day, COUNT(*) AS n
               FROM records WHERE platform='reddit' AND kind='comment'
               AND created_at>=? AND created_at<? GROUP BY day""", (lo, hi),
        ):
            index = (date.fromisoformat(row["day"]) - START).days // 7
            coverage[index] += row["n"]
        pilot_count = sum(
            (store.get_state(job_key("reddit-midterm-pilot", {"week": str(week), "v": 1})) or {}).get("new", 0)
            for week in REDDIT_PILOT_WEEKS
        )
        candidates = [(coverage[index], START + index * 7 * DAY)
                      for index in range(((END - START).days + 6) // 7)
                      if coverage[index] < 100]
        candidates.sort(key=lambda item: (item[0] > 0, item[1]))
        budget = (REDDIT_ROUND_LIMIT - pilot_count) // REDDIT_WEEK_LIMIT
        plan = {"weeks": [{"start": str(week), "prior_count": count}
                          for count, week in candidates[:budget]],
                "budget_excluded_weeks": [str(week) for _, week in candidates[budget:]],
                "pilot_count": pilot_count, "round_limit": REDDIT_ROUND_LIMIT}
        store.set_state(plan_key, plan)
    emit(platform="reddit", event="plan", weeks=len(plan["weeks"]),
         blank_weeks=sum(w["prior_count"] == 0 for w in plan["weeks"]),
         round_limit=plan["round_limit"], budget_excluded_weeks=plan["budget_excluded_weeks"])
    return reddit_weeks(config, store, client,
                        [date.fromisoformat(w["start"]) for w in plan["weeks"]], pilot=False)


def youtube_plan(store, config):
    lo, hi = (config["collection"][key] for key in ("start", "end"))
    pool = defaultdict(list)
    months = Counter()
    seen_videos = Counter()
    seen_channels = Counter()
    for row in store.db.execute(
        """SELECT strftime('%Y-%m',created_at,'+8 hours') AS month,root_id,count(*) AS n
           FROM records WHERE platform='youtube' AND kind='comment' AND created_at>=?
           AND created_at<? GROUP BY 1,2""", (lo, hi),
    ):
        months[row["month"]] += row["n"]
        seen_videos[row["root_id"]] += row["n"]
    for row in store.db.execute(
        """SELECT native_id, author_hash, reply_count, metadata,
                  strftime('%Y-%m',created_at,'+8 hours') AS month FROM records
           WHERE platform='youtube' AND kind='video' AND created_at>=? AND created_at<?""",
        (lo, hi),
    ):
        if seen_videos[row["native_id"]]:
            seen_channels[row["author_hash"]] += 1
            continue
        if row["reply_count"] is not None and row["reply_count"] < 10:
            continue
        pool[row["month"]].append(dict(row))
    chosen = []
    allocations = Counter()
    while pool and len(chosen) < 40:
        month = min(pool, key=lambda m: (months[m] + allocations[m] * 125, m))
        group = pool[month]
        video = min(group, key=lambda v: (
            seen_channels[v["author_hash"]], -min(v["reply_count"] or 0, 150), v["native_id"]
        ))
        group.remove(video)
        if not group:
            del pool[month]
        allocations[month] += 1
        seen_channels[video["author_hash"]] += 1
        chosen.append({"id": video["native_id"], "video_month": month})
    return chosen


def youtube_backfill(config, store, client):
    api_key = youtube._api_key()
    plan_key = job_key("youtube-midterm-plan", {"v": 1, "end": config["collection"]["end"]})
    plan = store.get_state(plan_key)
    if plan is None:
        plan = {"videos": youtube_plan(store, config)}
        store.set_state(plan_key, plan)
    details = []
    for video in plan["videos"]:
        check_disk(store)
        ident = video["id"]
        key = job_key("youtube-midterm-video", {"id": ident, "plan": plan_key})
        state = store.get_state(key) or {"new": 0, "pages": 0, "token": None, "seen": [], "months": {}}
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
                snip = item["snippet"]
                top = snip["topLevelComment"]
                records.append(youtube._comment_record(top, ident, store, replies=snip.get("totalReplyCount")))
                records.extend(youtube._comment_record(reply, ident, store, parent=top["id"])
                               for reply in item.get("replies", {}).get("comments", []))
            # Follow publication cohorts from sparse months. Only admit comment
            # dates in the shared window, and keep the per-video cap exact.
            records = list(new_records(store, (r for r in records if in_window(r["created_at"], config))))
            random.Random(f"{ident}/{state['pages']}").shuffle(records)
            records = records[:150 - state["new"]]
            for record in records:
                month = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00")).astimezone(BEIJING).strftime("%Y-%m")
                state["months"][month] = state["months"].get(month, 0) + 1
                record["metadata"]["selection"] = "midterm_sparse_video_cohort"
            pages, total = state["pages"] + 1, state["new"] + len(records)
            reason = "comment_cap" if total >= 150 else "exhausted" if not token else "page_cap" if pages >= 12 else None
            state.update(pages=pages, new=total, token=token, seen=seen, done=bool(reason), reason=reason)
            store.save_batch(records, key, state)
        entry = {**video, **{k: state[k] for k in ("new", "pages", "months", "reason")}}
        details.append(entry)
        emit(platform="youtube", **entry)
    return details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("steam", "reddit", "youtube"))
    parser.add_argument("--expand-weeks", action="store_true",
                        help="Reddit only: extend the pilot to blank and sparse weeks")
    args = parser.parse_args()
    if args.expand_weeks and args.platform != "reddit":
        parser.error("--expand-weeks requires the reddit platform")
    config = scope(load_config("config.toml"), local(START), local(END))
    config["network"].update(min_interval={"steam": 0.45, "reddit": 0.6, "youtube": 0.35}[args.platform])
    jobs = {"steam": steam_gaps, "reddit": reddit_expand_weeks if args.expand_weeks else reddit_pilot,
            "youtube": youtube_backfill}
    collection = config["collection"]
    store = Store(collection["database"], collection["hash_key_file"])
    client = HttpClient(config)
    job = "midterm_reddit_weekly_expansion" if args.expand_weeks else "midterm_full_cycle_backfill"
    run_id = store.start_run(args.platform, {**config, "job": job})
    try:
        details = jobs[args.platform](config, store, client)
        result = {"platform": args.platform, "details": details, "requests": client.requests_made}
        store.finish_run(run_id, "complete", result)
        emit(event="complete", run_id=run_id, platform=args.platform,
             requests=client.requests_made, scopes=len(details),
             new=sum(d.get("new", d.get("saved", 0)) for d in details))
    except (Exception, KeyboardInterrupt) as exc:
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"
        error = safe_error(exc)
        store.finish_run(run_id, status, {"error": error, "requests": client.requests_made})
        emit(event=status, run_id=run_id, platform=args.platform, error=error)
        raise SystemExit(130 if status == "interrupted" else 1)
    finally:
        client.close()
        store.close()


if __name__ == "__main__":
    main()
