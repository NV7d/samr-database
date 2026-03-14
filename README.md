# SAMR Merger Control Database

Structured dataset of China's Anti-Monopoly Merger Control public notices, with unified download tooling.

统一下载器，合并了三套来源逻辑（v1/v2/v3），并保持同一套输出目录、命名与增量清单机制。

- 主脚本：`/Users/nv7d/samr_publicity_downloader_unified.py`
- 可选修复脚本（去掉历史 `NOCASE_` 前缀）：`/Users/nv7d/samr_publicity_fix_nocase_prefix.py`

## 1. 环境要求

推荐使用你当前可用的 Python：

```bash
/Users/nv7d/miniconda3/bin/python --version
```

## 2. 输出结构

默认输出目录：`~/Downloads/samr_publicity`

- 文件：`files/{YYYY}/{MM}/...`
- 增量清单：`manifest.jsonl`
- 表格清单：`manifest.csv`
- 本次运行报告：`run_report.json`

## 3. 统一脚本参数

```bash
/Users/nv7d/miniconda3/bin/python /Users/nv7d/samr_publicity_downloader_unified.py --help
```

核心参数：

- `--source v1|v2|v3` 必填
- `--out-dir` 输出目录
- `--dry-run` 仅探测不落盘
- `--timeout --retry --sleep-ms --user-agent --cookie`

v1 专属：

- `--page-size`（默认 20）
- `--max-pages`（默认 0=全量）

v2/v3 专属：

- `--start-page --end-page`（`end-page=0` 表示到末页）
- `--cutoff-date`（默认 `2022-08-31`）

## 4. 三个来源的推荐命令

### v1（`jyzjz.samr.gov.cn`）

```bash
/Users/nv7d/miniconda3/bin/python /Users/nv7d/samr_publicity_downloader_unified.py \
  --source v1 \
  --out-dir ~/Downloads/samr_publicity \
  --max-pages 0
```

### v2（`www.samr.gov.cn`，第136页起）

```bash
/Users/nv7d/miniconda3/bin/python /Users/nv7d/samr_publicity_downloader_unified.py \
  --source v2 \
  --out-dir ~/Downloads/samr_publicity \
  --start-page 136 \
  --end-page 0 \
  --cutoff-date 2022-08-31
```

### v3（`fldj.mofcom.gov.cn`，更早数据）

```bash
/Users/nv7d/miniconda3/bin/python /Users/nv7d/samr_publicity_downloader_unified.py \
  --source v3 \
  --out-dir ~/Downloads/samr_publicity \
  --start-page 1 \
  --end-page 0 \
  --cutoff-date 2022-08-31
```

## 5. 增量机制

- 去重键：
  - v1: `id::fileId`
  - v2/v3: `detail_url::attachment_url`
- 已下载且文件存在：自动跳过
- 清单有记录但文件丢失：自动补下（`recovered_missing_file`）

## 6. 文件命名规则

已统一去除 `NOCASE_` 前缀：

- v1: `{caseNo或id}_{caseName}.{ext}`
- v2/v3: `{article_id}_{caseName}.{ext}`

## 7. 历史文件名修复（可选）

如果历史数据里有 `NOCASE_` 前缀，运行：

```bash
# 先预览
/Users/nv7d/miniconda3/bin/python /Users/nv7d/samr_publicity_fix_nocase_prefix.py --out-dir ~/Downloads/samr_publicity --dry-run

# 正式修复
/Users/nv7d/miniconda3/bin/python /Users/nv7d/samr_publicity_fix_nocase_prefix.py --out-dir ~/Downloads/samr_publicity
```

修复会同步更新：

- 文件名
- `manifest.jsonl`
- `manifest.csv`

## 8. 说明

- 旧脚本 `samr_publicity_downloader.py / _v2.py / _v3_mofcom.py` 仍保留，便于回滚。
- 统一脚本优先用于后续新任务。
