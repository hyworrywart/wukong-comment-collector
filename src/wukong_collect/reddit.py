"""Configurable Reddit BigQuery and local-export adapters.

Field mappings describe the actual source schema, whether it is an accessible
public table, a university dataset, or Reddit for Researchers. The configured
source label records provenance; table and export access depend on that source.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .common import in_window, job_key, utc_iso


class RedditConfigError(ValueError):
    """An actionable Reddit configuration or source-schema problem."""


_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_TABLE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_CANONICAL_FIELDS = {"native_id", "parent_id", "root_id", "text", "created_at", "subreddit", "author_hash", "updated_at", "language", "score", "reply_count", "url", "title", "deleted"}
def _settings(config: dict) -> dict:
    return config.get("reddit", {})


def _mapping(config: dict, kind: str) -> dict:
    if kind not in {"post", "comment"}:
        raise RedditConfigError("Reddit kind 必须是 comment 或 post。")
    section = "fields" if kind == "comment" else "post_fields"
    fields = dict(_settings(config).get(section, {}))
    required = {"native_id", "text", "created_at", "subreddit"}
    if kind == "comment":
        required |= {"parent_id", "root_id"}
    missing = sorted(required - fields.keys())
    if missing:
        raise RedditConfigError(f"请按来源数据结构填写 [reddit.{section}]，缺少映射: {', '.join(missing)}")
    unknown = sorted(set(fields) - _CANONICAL_FIELDS)
    if unknown:
        raise RedditConfigError(f"reddit.{section} 中有未知规范字段: {', '.join(unknown)}；字段映射左侧只能使用本工具支持的规范键。")
    for key, value in fields.items():
        if not isinstance(value, str) or not _FIELD.fullmatch(value):
            raise RedditConfigError(f"reddit.{section}.{key} 需要有效字段路径（例如 comment_id 或 nested.id）。")
    return fields


def _field(path: str, alias: str = "r") -> str:
    if not _FIELD.fullmatch(path):
        raise RedditConfigError("无效 BigQuery 字段路径。")
    return alias + "." + ".".join(f"`{part}`" for part in path.split("."))


def _table(config: dict, kind: str) -> str:
    value = _settings(config).get("comments_table" if kind == "comment" else "posts_table", "")
    if not isinstance(value, str) or not _TABLE.fullmatch(value):
        raise RedditConfigError(f"请填写可访问的 {kind} 表，格式 project.dataset.table；不能填写任意 SQL。")
    return value


def _timestamp_sql(field: str, config: dict) -> str:
    mode = _settings(config).get("timestamp_format", "timestamp")
    if mode == "timestamp":
        return f"CAST({field} AS TIMESTAMP)"
    if mode == "epoch_seconds":
        return f"TIMESTAMP_SECONDS(CAST({field} AS INT64))"
    if mode == "epoch_millis":
        return f"TIMESTAMP_MILLIS(CAST({field} AS INT64))"
    raise RedditConfigError("reddit.timestamp_format 应为 timestamp、epoch_seconds 或 epoch_millis。")


def _bounds(config: dict) -> tuple[str, str]:
    values = config.get("collection", {})
    if not values.get("start") or not values.get("end"):
        raise RedditConfigError("Reddit BigQuery 必须指定 collection.start 和 end，避免无界查询。")
    return utc_iso(values["start"]), utc_iso(values["end"])


def build_query(config: dict, kind: str = "comment") -> dict:
    """Generate SELECT-only SQL and parameter values without importing Google SDK.

    keyword_scope='thread' selects comments of posts matching text/title; this
    preserves keyword-free replies. 'record' explicitly samples each record text.
    Parameter values, including search text, never become SQL fragments.
    """
    settings = _settings(config)
    fields, table = _mapping(config, kind), _table(config, kind)
    start, end = _bounds(config)
    subreddits = [str(x).removeprefix("r/").lower() for x in settings.get("subreddits", [])]
    keywords = [str(x).casefold() for x in settings.get("keywords", []) if str(x).strip()]
    scope = settings.get("keyword_scope", "thread")
    if scope not in {"thread", "record"}:
        raise RedditConfigError("reddit.keyword_scope 必须是 thread 或 record。")
    selected = [f"{_field(value)} AS `{key}`" for key, value in fields.items()]
    when = _timestamp_sql(_field(fields["created_at"]), config)
    where = [f"{when} >= @start", f"{when} < @end"]
    if subreddits:
        where.append(f"LOWER(CAST({_field(fields['subreddit'])} AS STRING)) IN UNNEST(@subreddits)")
    if keywords:
        if kind == "comment" and scope == "thread":
            pf, pt = _mapping(config, "post"), _table(config, "post")
            # Prefix stripping lets t3_abcd and abcd compare without changing
            # relationship IDs in the result. Do not restrict post date: a new
            # comment can belong to an old post.
            post_text = f"COALESCE(CAST({_field(pf['text'], 'p')} AS STRING), '')"
            if pf.get("title"):
                post_text = f"CONCAT({post_text}, ' ', COALESCE(CAST({_field(pf['title'], 'p')} AS STRING), ''))"
            seed = f"EXISTS (SELECT 1 FROM UNNEST(@keywords) keyword WHERE STRPOS(LOWER({post_text}), keyword) > 0)"
            if subreddits:
                seed += f" AND LOWER(CAST({_field(pf['subreddit'], 'p')} AS STRING)) IN UNNEST(@subreddits)"
            where.append(
                f"REGEXP_REPLACE(CAST({_field(fields['root_id'])} AS STRING), r'^t3_', '') IN "
                f"(SELECT REGEXP_REPLACE(CAST({_field(pf['native_id'], 'p')} AS STRING), r'^t3_', '') FROM `{pt}` p WHERE {seed})"
            )
        else:
            text = f"COALESCE(CAST({_field(fields['text'])} AS STRING), '')"
            if fields.get("title"):
                text = f"CONCAT({text}, ' ', COALESCE(CAST({_field(fields['title'])} AS STRING), ''))"
            where.append(f"EXISTS (SELECT 1 FROM UNNEST(@keywords) keyword WHERE STRPOS(LOWER({text}), keyword) > 0)")
    sql = "SELECT\n  " + ",\n  ".join(selected) + f"\nFROM `{table}` r\nWHERE " + "\n  AND ".join(where)
    return {"sql": sql, "parameters": {"start": start, "end": end, "subreddits": subreddits, "keywords": keywords}, "table": table, "kind": kind}


def _sdk():
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RedditConfigError("BigQuery 模式需要可选依赖：uv sync --extra reddit。然后配置 Google ADC。") from exc
    return bigquery


def _client(config: dict):
    bigquery = _sdk()
    settings = _settings(config)
    if not settings.get("project"):
        raise RedditConfigError("请填写用于运行查询的 reddit.project，并配置 Google ADC。")
    try:
        return bigquery.Client(project=settings["project"], location=settings.get("location") or None)
    except Exception as exc:
        raise RedditConfigError("无法建立 BigQuery 客户端。请配置 Google ADC，并确认查询项目的权限。") from exc


def _schema_rows(schema: Iterable, prefix: str = "") -> list[dict]:
    result = []
    for field in schema:
        path = prefix + field.name
        result.append({"path": path, "type": field.field_type, "mode": field.mode})
        result.extend(_schema_rows(getattr(field, "fields", ()), path + "."))
    return result


def inspect_schema(config: dict) -> dict:
    """Read authorized table schemas; performs no query and exports no content."""
    client = _client(config)
    result = {}
    try:
        for kind in _settings(config).get("kinds", ["comment"]):
            table = _table(config, kind)
            result[kind] = {"table": table, "fields": _schema_rows(client.get_table(table).schema)}
    except RedditConfigError:
        raise
    except Exception as exc:
        raise RedditConfigError("读取 BigQuery 表结构失败；请确认表名、区域以及当前账号的读取权限。") from exc
    finally:
        client.close()
    return result


def _get(row: dict, path: str) -> Any:
    if path in row:
        return row[path]
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise RedditConfigError(f"来源记录缺少配置字段 {path!r}；请核对字段映射。")
        value = value[part]
    return value


def _timestamp(value: Any, config: dict) -> str:
    mode = _settings(config).get("timestamp_format", "timestamp")
    if value is None or value == "":
        raise RedditConfigError("Reddit created_at 为空，不能建立时间语料。")
    if isinstance(value, datetime):
        return utc_iso(value.isoformat())
    if mode in {"epoch_seconds", "epoch_millis"}:
        try:
            return utc_iso(float(value) / (1000 if mode == "epoch_millis" else 1))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RedditConfigError("Reddit 时间戳不符合 timestamp_format。") from exc
    try:
        return utc_iso(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RedditConfigError("Reddit 日期无法解析；请配置 timestamp_format。") from exc


def _id(value: Any, kind: str) -> str:
    if value is None or not str(value).strip():
        raise RedditConfigError(f"Reddit {kind} ID 为空。")
    text = str(value).strip()
    expected = "t1_" if kind == "comment" else "t3_"
    # A source may use opaque IDs rather than Reddit's base36 IDs. Preserve opaque
    # identifiers exactly; normalize only the well-known fullname prefixes.
    if text.startswith(("t1_", "t3_")) and not text.startswith(expected):
        raise RedditConfigError(f"{kind} ID 的 Reddit fullname 类型前缀不符。")
    return text[len(expected):] if text.startswith(expected) else text


def normalize(row: dict, kind: str, config: dict, store, *, canonical: bool = False) -> dict | None:
    """Map a configured Reddit row, preserving links and hashing author IDs."""
    fields = _mapping(config, kind)
    data = dict(row) if canonical else {key: _get(row, path) for key, path in fields.items()}
    for key in ("native_id", "text", "created_at", "subreddit"):
        if key not in data:
            raise RedditConfigError(f"缺少规范字段 {key}。")
    native_id = _id(data["native_id"], kind)
    created = _timestamp(data["created_at"], config)
    if not in_window(created, config):
        return None
    text = str(data["text"] or "")
    deleted = data.get("deleted")
    if deleted is True or str(deleted).lower() in {"true", "1"} or text.strip() in {"[removed]", "[deleted]"}:
        return None
    subreddit = str(data.get("subreddit") or "").removeprefix("r/")
    allowed = [str(s).removeprefix("r/").casefold() for s in _settings(config).get("subreddits", [])]
    if allowed and subreddit.casefold() not in allowed:
        return None
    if kind == "comment":
        if data.get("parent_id") in (None, "") or data.get("root_id") in (None, ""):
            raise RedditConfigError("评论 parent_id/root_id 为空，无法保留回复网络。")
        root = _id(data["root_id"], "post")
        parent = str(data["parent_id"])
        # Prefix the IDs consistently in the unified store, including when the
        # export uses bare IDs. Bare root equality identifies top-level comments.
        parent = parent if parent.startswith(("t1_", "t3_")) else ("t3_" if parent == root else "t1_") + parent
        parent_id, root_id = parent, "t3_" + root
        normalized_id = "t1_" + native_id
    else:
        parent_id, root_id = None, "t3_" + native_id
        normalized_id = root_id
    author = data.get("author_hash")
    author_hash = store.author_hash("reddit", str(author)) if author not in (None, "", "[deleted]") else None
    def number(key):
        value = data.get(key)
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise RedditConfigError(f"{key} 必须为整数或空值。") from exc
    return {
        "platform": "reddit", "kind": kind, "native_id": normalized_id,
        "parent_id": parent_id, "root_id": root_id, "author_hash": author_hash,
        "text": text, "created_at": created,
        "updated_at": _timestamp(data["updated_at"], config) if data.get("updated_at") else None,
        "language": data.get("language") or None, "like_count": None,
        "reply_count": number("reply_count"), "url": data.get("url") or None,
        "metadata": {"subreddit": subreddit, "title": data.get("title"), "score": number("score"), "source": _settings(config).get("source", "reddit_for_researchers"), "timestamp_format": _settings(config).get("timestamp_format", "timestamp"), "time_basis": "source_created_at_not_collection_time"},
    }


def _source_rows(path: Path):
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        with path.open(encoding="utf-8-sig") as handle:
            for position, line in enumerate(handle, 1):
                if not line.strip():
                    yield position, None
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RedditConfigError(f"{path.name} 第 {position} 行不是合法 JSON。") from exc
                if not isinstance(value, dict):
                    raise RedditConfigError(f"{path.name} 第 {position} 行必须是 JSON 对象。")
                yield position, value
    elif path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for position, value in enumerate(csv.DictReader(handle), 1):
                yield position, value
    else:
        raise RedditConfigError("Reddit 导入仅支持 .jsonl、.ndjson 或 .csv。")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matches_keywords(row: dict, fields: dict, keywords: list[str]) -> bool:
    text = str(_get(row, fields["text"]) or "")
    if fields.get("title"):
        text += " " + str(_get(row, fields["title"]) or "")
    return any(keyword in text.casefold() for keyword in keywords)


def _import_keyword_filter(config: dict, files: list):
    settings = _settings(config)
    keywords = [str(k).casefold() for k in settings.get("keywords", []) if str(k).strip()]
    scope = settings.get("keyword_scope", "thread")
    if scope not in {"record", "thread"}:
        raise RedditConfigError("reddit.keyword_scope 必须是 thread 或 record。")
    if not keywords:
        return lambda row, kind: True
    if scope == "record":
        return lambda row, kind: _matches_keywords(row, _mapping(config, kind), keywords)
    post_files = [entry for entry in files if isinstance(entry, dict) and entry.get("kind") == "post"]
    if not post_files:
        raise RedditConfigError("按整帖关键词筛选需要 posts 导出文件（import_files 中 kind='post'）及 post_fields。若只按评论正文筛选，请显式设置 keyword_scope='record'；若来源文件已筛好整帖，请将 keywords 留空。")
    fields = _mapping(config, "post")
    roots = set()
    for spec in post_files:
        path = Path(spec["path"]).expanduser().resolve()
        if not path.is_file():
            raise RedditConfigError(f"Reddit 导入文件不存在：{path}")
        for _, row in _source_rows(path):
            if row is not None and _matches_keywords(row, fields, keywords):
                roots.add(_id(_get(row, fields["native_id"]), "post"))
    return lambda row, kind: (_id(_get(row, _mapping(config, kind)["root_id" if kind == "comment" else "native_id"]), "post") in roots)


def _run_import(config: dict, store, *, restart: bool, max_pages: int | None) -> dict:
    settings = _settings(config)
    files = settings.get("import_files", [])
    if not files:
        raise RedditConfigError("Reddit 尚未配置导入文件：请填写 reddit.import_files，或配置可访问的表并改用 bigquery 模式。")
    page_size = int(settings.get("page_size", 500))
    if page_size < 1:
        raise RedditConfigError("reddit.page_size 必须大于 0。")
    signatures = {}
    for entry in files:
        spec = entry if isinstance(entry, dict) else {"path": entry, "kind": "comment"}
        path = Path(spec["path"]).expanduser().resolve()
        if not path.is_file():
            raise RedditConfigError(f"Reddit 导入文件不存在：{path}")
        signatures[str(path)] = _digest(path)
    matches = _import_keyword_filter(config, files)
    pages, saved, scanned, finished = 0, 0, 0, 0
    for entry in files:
        spec = entry if isinstance(entry, dict) else {"path": entry, "kind": "comment"}
        kind = spec.get("kind", "comment")
        _mapping(config, kind)
        path = Path(spec["path"]).expanduser().resolve()
        if not path.is_file():
            raise RedditConfigError(f"Reddit 导入文件不存在：{path}")
        source_hash = signatures[str(path)]
        # The matching-post export is also a dependency. Changing a seed post
        # must re-evaluate a previously completed comment import.
        key = job_key("reddit-import", {"path": str(path), "sources": signatures, "kind": kind, "settings": settings, "collection": config.get("collection", {})})
        state = {} if restart else (store.get_state(key) or {})
        if state.get("done"):
            finished += 1
            continue
        position = int(state.get("position", 0))
        pending, batch_rows, last = [], 0, position
        for index, row in _source_rows(path):
            if index <= position:
                continue
            if max_pages is not None and pages >= max_pages:
                return {"platform": "reddit", "mode": "import", "status": "partial", "pages": pages, "saved": saved, "scanned": scanned, "files_done": finished}
            last = index
            if row is not None:
                try:
                    record = normalize(row, kind, config, store)
                    if record and not matches(row, kind):
                        record = None
                except RedditConfigError as exc:
                    raise RedditConfigError(f"{path.name} 记录 {index}: {exc}") from exc
                if record:
                    pending.append(record)
            scanned += 1
            batch_rows += 1
            if batch_rows >= page_size:
                store.save_batch(pending, key, {"position": index, "done": False, "source_sha256": source_hash})
                saved += len(pending)
                pages += 1
                pending, batch_rows = [], 0
        if batch_rows:
            store.save_batch(pending, key, {"position": last, "done": True})
            saved += len(pending)
            pages += 1
        else:
            store.set_state(key, {"position": last, "done": True})
        finished += 1
    return {"platform": "reddit", "mode": "import", "status": "complete", "pages": pages, "saved": saved, "scanned": scanned, "files_done": finished}


def _query_config(spec: dict, settings: dict):
    bigquery = _sdk()
    params = spec["parameters"]
    result = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start", "TIMESTAMP", params["start"]),
            bigquery.ScalarQueryParameter("end", "TIMESTAMP", params["end"]),
            bigquery.ArrayQueryParameter("subreddits", "STRING", params["subreddits"]),
            bigquery.ArrayQueryParameter("keywords", "STRING", params["keywords"]),
        ],
        use_legacy_sql=False,
        maximum_bytes_billed=int(settings.get("maximum_bytes_billed", 1_000_000_000)),
    )
    if result.maximum_bytes_billed < 1:
        raise RedditConfigError("maximum_bytes_billed 必须大于 0。")
    return result


def _validate_schema(client, config: dict, kind: str) -> None:
    fields = _mapping(config, kind)
    available = {f["path"] for f in _schema_rows(client.get_table(_table(config, kind)).schema)}
    missing = sorted(set(fields.values()) - available)
    if missing:
        raise RedditConfigError(f"BigQuery {kind} 表没有配置字段：{', '.join(missing)}；请先运行 reddit-schema。")


def _run_bigquery(config: dict, store, *, restart: bool, max_pages: int | None) -> dict:
    settings = _settings(config)
    # Build and validate identifiers before constructing any network client.
    specs = [build_query(config, kind) for kind in settings.get("kinds", ["comment"])]
    page_size = int(settings.get("page_size", 500))
    if page_size < 1:
        raise RedditConfigError("reddit.page_size 必须大于 0。")
    client = _client(config)
    pages, saved, done = 0, 0, 0
    try:
        for spec in specs:
            kind = spec["kind"]
            key = job_key("reddit-bigquery", {"spec": spec, "project": settings.get("project"), "location": settings.get("location")})
            state = {} if restart else (store.get_state(key) or {})
            if state.get("done"):
                done += 1
                continue
            if max_pages is not None and pages >= max_pages:
                return {"platform": "reddit", "mode": "bigquery", "status": "partial", "pages": pages, "saved": saved}
            _validate_schema(client, config, kind)
            if spec["parameters"]["keywords"] and kind == "comment" and settings.get("keyword_scope", "thread") == "thread":
                _validate_schema(client, config, "post")
            job_id = state.get("job_id")
            if job_id:
                # The result set of the exact same job is stable for resume.
                # If it expired, stop explicitly; --restart issues a new query.
                job = client.get_job(job_id, location=settings.get("location") or None)
            else:
                job = client.query(spec["sql"], job_config=_query_config(spec, settings))
                store.set_state(key, {"job_id": job.job_id, "position": 0, "done": False})
            position = int(state.get("position", 0))
            rows = job.result(page_size=page_size, start_index=position)
            for page in rows.pages:
                pending = []
                for row in page:
                    position += 1
                    record = normalize(dict(row.items()), kind, config, store, canonical=True)
                    if record:
                        pending.append(record)
                token = rows.next_page_token
                store.save_batch(pending, key, {"job_id": job.job_id, "position": position, "done": token is None})
                saved += len(pending)
                pages += 1
                if token is not None and max_pages is not None and pages >= max_pages:
                    return {"platform": "reddit", "mode": "bigquery", "status": "partial", "pages": pages, "saved": saved}
            store.set_state(key, {"job_id": job.job_id, "position": position, "done": True})
            done += 1
    except RedditConfigError:
        raise
    except Exception as exc:
        raise RedditConfigError("Reddit BigQuery 操作失败。检查 ADC、表名/区域、读取权限与计费上限；若续传作业已过期，使用 --restart。若来源限制导出，请按该来源要求在允许的环境内分析。") from exc
    finally:
        client.close()
    return {"platform": "reddit", "mode": "bigquery", "status": "complete", "pages": pages, "saved": saved, "queries_done": done}


def run(config: dict, store, client=None, *, restart: bool = False, max_pages: int | None = None) -> dict:
    """Collect from configured local files, BigQuery tables, or archive access."""
    settings = _settings(config)
    mode = settings.get("mode", "import")
    limit = max_pages if max_pages is not None else settings.get("max_pages")
    if limit is not None and int(limit) < 1:
        raise RedditConfigError("max_pages 必须大于 0。")
    limit = int(limit) if limit is not None else None
    if mode == "import":
        return _run_import(config, store, restart=restart, max_pages=limit)
    if mode == "bigquery":
        return _run_bigquery(config, store, restart=restart, max_pages=limit)
    if mode == "arctic_shift":
        from .reddit_archive import run as run_archive

        return run_archive(config, store, client, restart=restart, max_pages=limit)
    raise RedditConfigError("reddit.mode 仅支持 import、bigquery（公共表、学校表或 RFR 表）或 arctic_shift。")
