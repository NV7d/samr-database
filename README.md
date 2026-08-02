# SAMR Merger Control Database

本仓库收集中国经营者集中/反垄断公开数据，并提供可重复执行的下载脚本（支持增量）。

## 目录结构（当前）

```text
samr_publicity/
├── README.md
├── LICENSE
├── manifest.csv
├── samr_simple_case_notices/          # 简易案件公示（v1/v2/v3）标准化数据
├── mofcom_penalty_notices/            # MOFCOM ztxx 公告（正文+附件）
├── samr_enforcement_cases/            # SAMR 行政执法三栏目（正文+附件）
├── tools/
│   ├── downloaders/
│   │   ├── samr_publicity_downloader_unified.py
│   │   ├── samr_publicity_downloader_v4_mofcom_ztxx.py
│   │   └── samr_publicity_downloader_v5_samr_enforcement.py
│   ├── migrate_v1_to_standard_layout.py
│   └── legacy/
├── dashboard/                         # 案例级静态可视化页面与快照
└── indexes/root_catalog/              # 历史根清单归档
```

## 环境要求

- Python 3.9+

检查版本：

```bash
python3 --version
```

Windows：

```powershell
py --version
```

## 快速开始

进入仓库根目录后执行。

### 1) 统一下载器（v1/v2/v3）

```bash
python3 tools/downloaders/samr_publicity_downloader_unified.py --help
```

说明（新规则）：

- 默认会把数据写到：`<out-dir>/samr_simple_case_notices/`
- 即使你传 `--out-dir ./`，也不会再在仓库根目录生成 `files/manifest.jsonl`
- 只有加 `--legacy-flat-out-dir` 才使用旧的平铺输出

常用命令：

```bash
# v1 增量
python3 tools/downloaders/samr_publicity_downloader_unified.py --source v1 --out-dir ./ --max-pages 0

# v2（第136页起，含 2022-08-31 及以前）
python3 tools/downloaders/samr_publicity_downloader_unified.py --source v2 --out-dir ./ --start-page 136 --end-page 0 --cutoff-date 2022-08-31

# v3（MOFCOM 旧站）
python3 tools/downloaders/samr_publicity_downloader_unified.py --source v3 --out-dir ./ --start-page 1 --end-page 0 --cutoff-date 2022-08-31

# 如果你想显式指定到数据目录，也可以：
python3 tools/downloaders/samr_publicity_downloader_unified.py --source v1 --out-dir ./samr_simple_case_notices --max-pages 0
```

Windows：

```powershell
py .\tools\downloaders\samr_publicity_downloader_unified.py --source v1 --out-dir .\ --max-pages 0
```

### 2) MOFCOM ztxx（独立脚本）

```bash
# 先预览
python3 tools/downloaders/samr_publicity_downloader_v4_mofcom_ztxx.py --out-dir ./ --dry-run

# 全量
python3 tools/downloaders/samr_publicity_downloader_v4_mofcom_ztxx.py --out-dir ./ --start-page 1 --end-page 0
```

### 3) SAMR 行政执法三栏目（独立脚本）

```bash
# 先预览
python3 tools/downloaders/samr_publicity_downloader_v5_samr_enforcement.py --out-dir ./ --dry-run

# 全量或增量（重复执行即可）
python3 tools/downloaders/samr_publicity_downloader_v5_samr_enforcement.py --out-dir ./ --categories all --start-page 1 --end-page 0
```

Windows：

```powershell
py .\tools\downloaders\samr_publicity_downloader_v5_samr_enforcement.py --out-dir .\ --categories all
```

## 清单文件说明

### `manifest.csv`

- 面向人工查看的总索引（根目录保留这一份）。
- 记录下载条目、来源、链接、落盘路径、哈希等。

### `manifest.jsonl`

- 面向程序增量去重的明细索引。
- 各数据子目录中各自维护（例如 `samr_simple_case_notices/manifest.jsonl`）。

### `run_report.json`

- 单次运行报告（扫描量、成功、失败、跳过原因）。
- 每个数据子目录各自维护。

## 案例级可视化 dashboard

从根目录清单生成可视化快照：

```bash
python3 tools/build_visualization_snapshot.py --manifest manifest.csv --output dashboard/data/samr-viz-data.js
```

Windows：

```powershell
py .\tools\build_visualization_snapshot.py --manifest manifest.csv --output dashboard\data\samr-viz-data.js
```

然后直接打开 `dashboard/index.html`。页面按 `dataset + id` 将正文和附件归并为案例，提供年度趋势、交易类型、执法类别、参与方共现、字段质量和案例文件钻取。

## 增量机制

- 已下载且文件仍存在：自动跳过。
- 清单有记录但文件缺失：自动补下并记为 `recovered_missing_file`。
- 重复执行同一命令即可做增量更新。

## Windows 长路径修复（已下载文件批量缩短）

如果在 Windows 拉取/同步时报 `Filename too long`，可在仓库根目录执行：

```bash
python3 tools/shorten_paths_for_windows.py
```

该脚本会：

- 批量缩短目录名和文件名；
- 同步更新各数据集 `manifest.jsonl / manifest.csv`；
- 将清单路径统一为跨平台相对路径，并重建根目录总索引 `manifest.csv`；
- 清理空目录。

## v1 历史目录标准化迁移

```bash
# 预览
python3 tools/migrate_v1_to_standard_layout.py --out-dir ./ --dry-run

# 执行
python3 tools/migrate_v1_to_standard_layout.py --out-dir ./
```

## 可选：修复历史 NOCASE_ 前缀

```bash
# 预览
python3 tools/legacy/samr_publicity_fix_nocase_prefix.py --out-dir ./ --dry-run

# 执行
python3 tools/legacy/samr_publicity_fix_nocase_prefix.py --out-dir ./
```
