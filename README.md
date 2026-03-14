# SAMR Merger Control Database

Structured dataset of China's Anti-Monopoly Merger Control public notices, with unified download tooling.

统一下载器，合并了三套来源逻辑（v1/v2/v3），并保持同一套输出目录、命名与增量清单机制。

- 主脚本：`/Users/nv7d/Downloads/samr_publicity/samr_publicity_downloader_unified.py`
- 可选修复脚本（去掉历史 `NOCASE_` 前缀）：`/Users/nv7d/Downloads/samr_publicity/samr_publicity_fix_nocase_prefix.py`

## 0. 快速开始（推荐）

### 推荐执行方式（避免路径错误）

```bash
cd /Users/nv7d/Downloads/samr_publicity
/Users/nv7d/miniconda3/bin/python ./samr_publicity_downloader_unified.py --help
```

### 每日/每周增量更新（只下载新文件）

```bash
/Users/nv7d/miniconda3/bin/python /Users/nv7d/Downloads/samr_publicity/samr_publicity_downloader_unified.py --source v1 --out-dir ~/Downloads/samr_publicity --max-pages 0
/Users/nv7d/miniconda3/bin/python /Users/nv7d/Downloads/samr_publicity/samr_publicity_downloader_unified.py --source v2 --out-dir ~/Downloads/samr_publicity --start-page 136 --end-page 0 --cutoff-date 2022-08-31
/Users/nv7d/miniconda3/bin/python /Users/nv7d/Downloads/samr_publicity/samr_publicity_downloader_unified.py --source v3 --out-dir ~/Downloads/samr_publicity --start-page 1 --end-page 0 --cutoff-date 2022-08-31
```

### 先预览不下载（建议首次先跑）

```bash
/Users/nv7d/miniconda3/bin/python /Users/nv7d/Downloads/samr_publicity/samr_publicity_downloader_unified.py --source v1 --out-dir ~/Downloads/samr_publicity --max-pages 0 --dry-run
```

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
/Users/nv7d/miniconda3/bin/python /Users/nv7d/Downloads/samr_publicity/samr_publicity_downloader_unified.py --help
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
/Users/nv7d/miniconda3/bin/python /Users/nv7d/Downloads/samr_publicity/samr_publicity_downloader_unified.py \
  --source v1 \
  --out-dir ~/Downloads/samr_publicity \
  --max-pages 0
```

### v2（`www.samr.gov.cn`，第136页起）

```bash
/Users/nv7d/miniconda3/bin/python /Users/nv7d/Downloads/samr_publicity/samr_publicity_downloader_unified.py \
  --source v2 \
  --out-dir ~/Downloads/samr_publicity \
  --start-page 136 \
  --end-page 0 \
  --cutoff-date 2022-08-31
```

### v3（`fldj.mofcom.gov.cn`，更早数据）

```bash
/Users/nv7d/miniconda3/bin/python /Users/nv7d/Downloads/samr_publicity/samr_publicity_downloader_unified.py \
  --source v3 \
  --out-dir ~/Downloads/samr_publicity \
  --start-page 1 \
  --end-page 0 \
  --cutoff-date 2022-08-31
```

## 5. 首次全量建议顺序

建议按下面顺序跑一次，减少重叠数据造成的重复处理：

1. `v1`（jyzjz.samr.gov.cn）
2. `v2`（samr.gov.cn 136 页以后）
3. `v3`（mofcom 更早数据）

## 6. 增量机制

- 去重键：
  - v1: `id::fileId`
  - v2/v3: `detail_url::attachment_url`
- 已下载且文件存在：自动跳过
- 清单有记录但文件丢失：自动补下（`recovered_missing_file`）

## 7. 文件命名规则

已统一去除 `NOCASE_` 前缀：

- v1: `{caseNo或id}_{caseName}.{ext}`
- v2/v3: `{article_id}_{caseName}.{ext}`

## 8. 如何判断本次是否成功

每次运行后查看 `run_report.json`，重点字段：

- `download_success`：本次实际新下载数量
- `already_downloaded`：已存在而跳过数量
- `download_failed`：下载失败数量
- `skipped_non_doc`：非 doc/docx 跳过数量（常见为 pdf）
- `errors`：错误明细

快速查看：

```bash
cat ~/Downloads/samr_publicity/run_report.json
```

## 9. 历史文件名修复（可选）

如果历史数据里有 `NOCASE_` 前缀，运行：

```bash
# 先预览
/Users/nv7d/miniconda3/bin/python /Users/nv7d/Downloads/samr_publicity/samr_publicity_fix_nocase_prefix.py --out-dir ~/Downloads/samr_publicity --dry-run

# 正式修复
/Users/nv7d/miniconda3/bin/python /Users/nv7d/Downloads/samr_publicity/samr_publicity_fix_nocase_prefix.py --out-dir ~/Downloads/samr_publicity
```

修复会同步更新：

- 文件名
- `manifest.jsonl`
- `manifest.csv`

## 10. 同步到 GitHub（可选）

如果本地已初始化仓库并设置远程，更新后执行：

```bash
git -C ~/Downloads/samr_publicity add .
git -C ~/Downloads/samr_publicity commit -m "Update dataset"
git -C ~/Downloads/samr_publicity push
```

## 11. 说明

- 旧脚本 `samr_publicity_downloader.py / _v2.py / _v3_mofcom.py` 仍保留，便于回滚。
- 统一脚本优先用于后续新任务。
