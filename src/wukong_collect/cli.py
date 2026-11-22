from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import sys
import tomllib

from dotenv import load_dotenv

from .common import utc_iso
from .http import ApiError, HttpClient
from .store import Store


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def load_config(path):
    path = Path(path).resolve()
    load_dotenv(path.parent / ".env", override=False)
    with path.open("rb") as f:
        config = tomllib.load(f)
    cfg = config.setdefault("collection", {})
    for name, default in (("database", "data/comments.sqlite3"), ("hash_key_file", "data/.author_hmac.key")):
        p = Path(cfg.get(name, default)).expanduser()
        cfg[name] = str(p if p.is_absolute() else path.parent / p)
    if cfg.get("start") and cfg.get("end") and utc_iso(cfg["start"]) >= utc_iso(cfg["end"]):
        raise ValueError("collection.start 必须早于 collection.end（UTC，右端不包含）")
    reddit = config.setdefault("reddit", {})
    resolved = []
    for entry in reddit.get("import_files", []):
        val = dict(entry) if isinstance(entry, dict) else {"path": entry}
        p = Path(val["path"]).expanduser()
        val["path"] = str(p if p.is_absolute() else path.parent / p)
        resolved.append(val)
    reddit["import_files"] = resolved
    return config


def safe_error(exc):
    msg = str(exc)
    for name in ("YOUTUBE_API_KEY", "COLLECT_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        value = os.environ.get(name)
        if value:
            msg = msg.replace(value, "[redacted]")
    return msg


def parser():
    p = argparse.ArgumentParser(description="《黑神话：悟空》跨平台评论采集")
    p.add_argument("--config", default="config.toml", help="TOML 配置路径；数据路径相对该文件")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect", help="分页采集；重复执行从断点继续")
    c.add_argument("platform", choices=["steam", "youtube", "reddit", "all"])
    c.add_argument("--max-pages", type=int, help="覆盖本次每平台页数限制")
    c.add_argument("--restart", action="store_true", help="从第一页重新遍历并更新已有记录")
    d = sub.add_parser("youtube-discover", help="按关键词发现视频并缓存候选集")
    d.add_argument("--max-pages", type=int)
    d.add_argument("--restart", action="store_true")
    sub.add_parser("reddit-schema", help="查看可访问 BigQuery 表的真实字段")
    q = sub.add_parser("reddit-query", help="生成参数化 SQL；不提交查询")
    q.add_argument("--kind", choices=["post", "comment"], default="comment")
    d = sub.add_parser("doctor", help="检查本地配置；不输出密钥")
    d.add_argument("--network", action="store_true", help="额外探测当前数据源，可能消耗少量配额")
    sub.add_parser("status", help="查看计数、断点及最近运行")
    e = sub.add_parser("export", help="导出假名化语料；CSV 兼容 Excel")
    e.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    e.add_argument("--output", required=True)
    e.add_argument("--platform", choices=["steam", "reddit", "youtube"])
    e.add_argument("--kind", choices=["review", "comment", "post", "video"])
    prune = sub.add_parser("purge-stale", help="删除数据库内过期平台记录并重置该平台断点")
    prune.add_argument("--platform", choices=["steam", "reddit", "youtube"], required=True)
    prune.add_argument("--days", type=int, default=30)
    return p


def doctor(config, client, network=False):
    reddit = config.get("reddit", {})
    out = {"python": sys.version.split()[0], "youtube_api_key_set": bool(os.environ.get("YOUTUBE_API_KEY")),
           "proxy_configured": any(os.environ.get(k) for k in ("COLLECT_PROXY", "HTTPS_PROXY", "ALL_PROXY")),
           "reddit": {"mode": reddit.get("mode", "import"), "import_files": len(reddit.get("import_files", [])), "project_set": bool(reddit.get("project")), "table_set": bool(reddit.get("comments_table")), "fields_mapped": bool(reddit.get("fields"))},
           "next_steps": []}
    if not out["youtube_api_key_set"]:
        out["next_steps"].append("按 docs/credentials.md 开通 YouTube Data API v3 并将 Key 写入 .env")
    if reddit.get("mode") == "arctic_shift":
        out["reddit"]["source"] = "arctic_shift"
        out["reddit"]["all_comments_subreddits"] = reddit.get("archive_all_comments_subreddits", ["BlackMythWukong"])
    elif not reddit.get("import_files") and not reddit.get("comments_table"):
        out["next_steps"].append("Reddit 可选 arctic_shift 公开档案，或配置可访问的 BigQuery 表/数据文件；参见 docs/credentials.md")
    if network:
        out["network"] = {}
        probes = [("steam", "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/", {"input_json": json.dumps({"appid": config.get("steam", {}).get("app_id", 2358720), "num_per_page": 1, "filter": 1})}),
                  ("youtube", "https://www.googleapis.com/youtube/v3/i18nLanguages", {"part": "snippet", "key": os.environ.get("YOUTUBE_API_KEY")})]
        if reddit.get("mode") == "arctic_shift":
            probes.append(("reddit", "https://arctic-shift.photon-reddit.com/api/comments/search", {
                "subreddit": (reddit.get("subreddits") or ["BlackMythWukong"])[0],
                "after": config["collection"]["start"], "before": config["collection"]["end"],
                "limit": 1, "fields": "id,created_utc,subreddit,link_id,parent_id"}))
        for name, url, params in probes:
            if name == "youtube" and not out["youtube_api_key_set"]:
                out["network"][name] = "skipped_missing_key"
                continue
            try:
                client.get_json(url, params=params)
                out["network"][name] = "ok"
            except ApiError as exc:
                out["network"][name] = safe_error(exc)
    return out


def main(argv=None):
    args = parser().parse_args(argv)
    store = client = None
    try:
        if hasattr(args, "max_pages") and args.max_pages is not None and args.max_pages < 1:
            raise ValueError("--max-pages 必须为正整数")
        config = load_config(args.config)
        client = HttpClient(config)
        if args.command == "doctor":
            emit(doctor(config, client, args.network))
            return 0
        if args.command in ("reddit-schema", "reddit-query"):
            from . import reddit
            emit(reddit.inspect_schema(config) if args.command == "reddit-schema" else reddit.build_query(config, args.kind))
            return 0
        store = Store(config["collection"]["database"], config["collection"]["hash_key_file"])
        if args.command == "status":
            emit(store.summary())
        elif args.command == "export":
            n = store.export(args.output, args.format, args.platform, args.kind)
            manifest = {"exported_at": utc_iso(), "records": n, "platform": args.platform, "kind": args.kind, "format": args.format,
                        "collection": {k: config["collection"].get(k) for k in ("start", "end")}, "date_filtered": False, "pseudonymous": True,
                        "text_redacted": False, "csv_formula_escaped": args.format == "csv", "summary": store.summary()["counts"],
                        "configured_reddit_source": config.get("reddit", {}).get("source", "reddit_for_researchers"),
                        "reddit_record_provenance": "Each record's metadata.source identifies its source; a database may contain multiple sources.",
                        "retention": "YouTube API data: refresh/delete within 30 days, including this export; Reddit: record the actual source and follow its applicable data-use/removal requirements."}
            mp = Path(str(args.output) + ".manifest.json")
            mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            emit({"records": n, "output": str(Path(args.output).resolve()), "manifest": str(mp.resolve())})
        elif args.command == "purge-stale":
            if args.days < 1:
                raise ValueError("--days 必须为正整数")
            before = datetime.now(timezone.utc) - timedelta(days=args.days)
            n = store.purge_stale(args.platform, before)
            emit({"deleted": n, "platform": args.platform, "before": utc_iso(before), "exports": "导出文件、外部备份及输入文件需单独刷新或删除"})
        else:
            names = ["youtube"] if args.command == "youtube-discover" else ["steam", "youtube", "reddit"] if args.platform == "all" else [args.platform]
            results, failed = [], False
            for platform in names:
                mod = importlib.import_module("." + platform, __package__)
                run_id = store.start_run(platform, config)
                try:
                    fn = mod.discover if args.command == "youtube-discover" else mod.run
                    result = fn(config, store, client, restart=args.restart, max_pages=args.max_pages)
                    store.finish_run(run_id, result.get("status", "completed"), result)
                    results.append(result)
                except KeyboardInterrupt:
                    store.finish_run(run_id, "interrupted", {"message": "已停止；下次从已提交断点继续"})
                    raise
                except Exception as exc:
                    failed = True
                    result = {"platform": platform, "status": "error", "error": safe_error(exc)}
                    store.finish_run(run_id, "error", result)
                    results.append(result)
            emit(results)
            return 2 if failed else 0
        return 0
    except KeyboardInterrupt:
        emit({"status": "interrupted", "message": "断点已保留"})
        return 130
    except Exception as exc:
        emit({"status": "error", "error": safe_error(exc)})
        return 2
    finally:
        if store:
            store.close()
        if client:
            client.close()
