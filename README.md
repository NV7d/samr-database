# SAMR Merger Control Database

Structured dataset of China's Anti-Monopoly Merger Control public notices, with unified download tooling.

统一下载器，合并了三套来源逻辑（v1/v2/v3），并保持同一套输出目录、命名与增量清单机制。

- 主脚本：`samr_publicity_downloader_unified.py`
- MOFCOM `ztxx` 独立脚本：`samr_publicity_downloader_v4_mofcom_ztxx.py`
- SAMR 行政执法三栏目独立脚本：`samr_publicity_downloader_v5_samr_enforcement.py`
- 可选修复脚本（去掉历史 `NOCASE_` 前缀）：`tools/legacy/samr_publicity_fix_nocase_prefix.py`

## 0. 快速开始（推荐）

### 推荐执行方式（避免路径错误）

```bash
cd samr_publicity
python samr_publicity_downloader_unified.py --help
```

### 每日/每周增量更新（只下载新文件）

```bash
python samr_publicity_downloader_unified.py --source v1 --out-dir ./ --max-pages 0
python samr_publicity_downloader_unified.py --source v2 --out-dir ./ --start-page 136 --end-page 0 --cutoff-date 2022-08-31
python samr_publicity_downloader_unified.py --source v3 --out-dir ./ --start-page 1 --end-page 0 --cutoff-date 2022-08-31
```

### 先预览不下载（建议首次先跑）

```bash
python samr_publicity_downloader_unified.py --source v1 --out-dir ./ --max-pages 0 --dry-run
```

### MOFCOM `ztxx`（正文+附件，独立目录）

```bash
python3 samr_publicity_downloader_v4_mofcom_ztxx.py --out-dir ./ --dry-run
python3 samr_publicity_downloader_v4_mofcom_ztxx.py --out-dir ./ --start-page 1 --end-page 0
```

### SAMR 三栏目行政执法公告（正文+附件，独立目录）

```bash
# 首次先探测
python3 samr_publicity_downloader_v5_samr_enforcement.py --out-dir ./ --dry-run

# 全量抓取（xzcf + ftj + xzjj）
python3 samr_publicity_downloader_v5_samr_enforcement.py --out-dir ./ --categories all --start-page 1 --end-page 0

# 后续增量更新（同一命令重复执行）
python3 samr_publicity_downloader_v5_samr_enforcement.py --out-dir ./ --categories all
```

参数说明（v5）：

- `--categories all|xzcf,ftj,xzjj`：抓取栏目，默认 `all`
- `--start-page`：起始页，默认 `1`
- `--end-page`：结束页，默认 `0`（自动到末页）
- `--dataset-subdir`：子目录名，默认 `samr_enforcement_cases`
- `--dry-run`：仅扫描不下载
- `--timeout --retry --sleep-ms --cookie --user-agent`：网络与风控参数

Windows 示例：

```powershell
py .\\samr_publicity_downloader_v5_samr_enforcement.py --out-dir .\\ --categories all
```

## 1. 环境要求

Python 3.9+。

macOS/Linux：

```bash
python3 --version
```

Windows（CMD/PowerShell）：

```bash
python --version
# or
py --version
```

## 2. 输出结构

默认输出目录：`~/Downloads/samr_publicity`

- 文件：`files/{YYYY}/{MM}/...`
- 增量清单：`manifest.jsonl`
- 表格清单：`manifest.csv`
- 本次运行报告：`run_report.json`

MOFCOM `ztxx` 独立脚本会写入单独子目录（与简易公示表数据分开）：

- `mofcom_penalty_notices/files/{YYYY}/{MM}/{article_id}_{title}/`
- `mofcom_penalty_notices/manifest.jsonl`
- `mofcom_penalty_notices/manifest.csv`
- `mofcom_penalty_notices/run_report.json`

SAMR 三栏目行政执法独立脚本会写入：

- `samr_enforcement_cases/files/{category}/{YYYY}/{MM}/{article_id}_{title}/`
- `samr_enforcement_cases/manifest.jsonl`
- `samr_enforcement_cases/manifest.csv`
- `samr_enforcement_cases/run_report.json`

## 3. 统一脚本参数

```bash
python samr_publicity_downloader_unified.py --help
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
python samr_publicity_downloader_unified.py \
  --source v1 \
  --out-dir ./ \
  --max-pages 0
```

### v2（`www.samr.gov.cn`，第136页起）

```bash
python samr_publicity_downloader_unified.py \
  --source v2 \
  --out-dir ./ \
  --start-page 136 \
  --end-page 0 \
  --cutoff-date 2022-08-31
```

### v3（`fldj.mofcom.gov.cn`，更早数据）

```bash
python samr_publicity_downloader_unified.py \
  --source v3 \
  --out-dir ./ \
  --start-page 1 \
  --end-page 0 \
  --cutoff-date 2022-08-31
```

Windows 示例：

```powershell
py .\\samr_publicity_downloader_unified.py --source v1 --out-dir .\\ --max-pages 0
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
cat run_report.json
```

## 9. 历史文件名修复（可选）

如果历史数据里有 `NOCASE_` 前缀，运行：

```bash
# 先预览
python tools/legacy/samr_publicity_fix_nocase_prefix.py --out-dir ./ --dry-run

# 正式修复
python tools/legacy/samr_publicity_fix_nocase_prefix.py --out-dir ./
```

修复会同步更新：

- 文件名
- `manifest.jsonl`
- `manifest.csv`

## 10. 同步到 GitHub（可选）

如果本地已初始化仓库并设置远程，更新后执行：

```bash
git add .
git commit -m "Update dataset"
git push
```

## 11. 说明

- 旧脚本已移动到 `tools/legacy/`（含 v1/v2/v3 与修复脚本），根目录仅保留统一下载器与核心索引文件。
- 统一脚本优先用于后续新任务。
