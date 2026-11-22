# API 凭据与可选数据源

## YouTube

在 Google Cloud 控制台创建或选择项目，启用 YouTube Data API v3。在「API 和服务 → 凭据」创建 API Key，将 API 限制设为 YouTube Data API v3。把 `.env.example` 复制为 `.env`，把密钥填到 `YOUTUBE_API_KEY`。运行 `uv run wukong-collect doctor` 确认读取结果。加 `--network` 会发探测请求并可能消耗配额。 [Google API 使用指南](https://developers.google.com/youtube/data/getting-started)

命令的页数限制与请求预算在本地生效，Google 项目的每日额度以控制台为准。长期保存、更新或发布采集数据时按数据来源的政策处理。本地清单、导出和数据库需要分别管理。 [YouTube API 政策](https://developers.google.com/youtube/terms/developer-policies/)

## 网络

只在需要时于 `.env` 设置 `COLLECT_PROXY`。留空时，HTTP 客户端遵循已设置的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 环境变量。将域名、用户名和密码留在本地配置。

## Reddit BigQuery

需要能查询的真实数据表，以及具有读表和创建查询作业权限的账号。先安装扩展依赖并配置 ADC，可使用 `gcloud auth application-default login`。也可通过本地环境变量指定获准使用的凭据文件。 [Google ADC 文档](https://cloud.google.com/docs/authentication/provide-credentials-adc) · [BigQuery 查询费用控制](https://cloud.google.com/bigquery/docs/best-practices-costs)

```bash
uv sync --locked --extra reddit
cp config.toml config.local.toml
```

替换 `config.local.toml` 中的 `[reddit]` 和相关字段小节。以下配置以名为 `id`、`body` 等的源字段为例。查询项目、表、区域、时间戳格式及字段需按实际来源填写。更改 `[collection].database` 和 `hash_key_file` 可隔离该来源的数据库。

```toml
[reddit]
mode = "bigquery"
source = "my_accessible_dataset"
project = "my-query-project"
comments_table = "my-source-project.my_dataset.my_comments_table"
subreddits = ["BlackMythWukong"]
keywords = []
kinds = ["comment"]
timestamp_format = "epoch_seconds"
maximum_bytes_billed = 1000000000
page_size = 500
max_pages = 10

[reddit.fields]
native_id = "id"
parent_id = "parent_id"
root_id = "link_id"
text = "body"
created_at = "created_utc"
subreddit = "subreddit"
score = "score"
```

```bash
uv run wukong-collect --config config.local.toml reddit-schema
uv run wukong-collect --config config.local.toml reddit-query --kind comment
uv run wukong-collect --config config.local.toml collect reddit --max-pages 2
```

若要从匹配关键词的父帖纳入线程内全部评论，添加 `posts_table` 与 `[reddit.post_fields]`，令 `keyword_scope = "thread"`。要直接匹配评论正文，使用 `keyword_scope = "record"`。配置了关键词的查询要留意来源表的扫描体量。

## Reddit 本地文件

使用自有或已获准使用的导出文件。沿用以上字段映射，根据文件字段修改，切换 `mode = "import"`，指定 `import_files = [{path = "data/reddit-comments.jsonl", kind = "comment"}]`。支持 JSONL/NDJSON 和带表头 CSV。保留真实来源标识 `source`。运行 `collect reddit` 逐批导入，再用 `status` 查看已写入的记录。
