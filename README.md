# 黑神话：悟空跨平台评论采集

采集 Steam 游戏评测、Reddit 社区讨论和 YouTube 视频评论。三条链路共用 Python 命令行、SQLite 断点、去重与 JSONL/CSV 导出。默认研究窗口是北京时间 2024-08-20 至 2026-11-08（包含两日）。

| 平台 | 默认入口 | 需要的凭据 |
| --- | --- | --- |
| Steam | Steam 游戏评测接口，App ID `2358720` | 无 |
| Reddit | Arctic Shift 公开历史档案，默认 `r/BlackMythWukong` | 默认模式无。BigQuery 模式需要 Google ADC 和数据表权限 |
| YouTube | YouTube Data API v3，按已知视频 ID 或关键词发现视频 | Google Cloud 项目中的 API Key |

## 安装和运行

需要 Python 3.11 或更新版本。下面使用 uv，从仓库根目录运行。

```bash
uv sync --locked
uv run wukong-collect doctor
uv run wukong-collect collect steam --max-pages 2
uv run wukong-collect collect reddit --max-pages 2
```

采集 YouTube 时，先复制 `.env.example` 为 `.env`，在本地填写 `YOUTUBE_API_KEY`。可以在 `[youtube].video_ids` 中指定视频 ID 或 URL，或运行发现命令将关键词搜索的结果存入当前数据库。只采指定视频时，使用独立数据库，避免带入此前发现的视频。

```bash
cp .env.example .env
# 填好 .env 后运行
uv run wukong-collect youtube-discover --max-pages 1
uv run wukong-collect collect youtube --max-pages 2
```

`--max-pages` 限制一次运行的页数。重复运行相同命令会接续已提交的断点。YouTube 评论还有 `youtube.max_requests` 单次请求预算。检查配置、各平台停止状态和导出时可使用以下命令。

```bash
uv run wukong-collect doctor
uv run wukong-collect status
uv run wukong-collect export --platform steam --kind review --format jsonl --output exports/steam.jsonl
uv run wukong-collect export --platform reddit --kind comment --format jsonl --output exports/reddit.jsonl
uv run wukong-collect export --platform youtube --kind comment --format csv --output exports/youtube.csv
```

如使用另一份配置，将 `--config config.local.toml` 放到子命令前。配置中的相对数据路径以该 TOML 所在目录为基准。导出按平台和记录类型筛选，涵盖该数据库的所有采集日期。为每个研究窗口单独建库，便于归档和导出。

## 文档与维护

- [各平台采集流程](docs/collection_workflow.md)包含范围、分页、断点、字段与历史补采策略。
- [API 凭据与可选数据源](docs/credentials.md)包含 YouTube Key 开通、代理、BigQuery 和本地文件导入。
- `src/wukong_collect/` 是日常运行入口，`scripts/` 保留规模化 Steam 运行和研究补采脚本，`tests/` 包含离线测试。

```bash
uv sync --locked --extra dev
uv run pytest -q
```

数据库、导出、日志、作者 HMAC 密钥和 `.env` 均保存在本地。原始评论正文及来源链接可能包含识别信息，仓库只维护代码、配置模板、文档和测试。
