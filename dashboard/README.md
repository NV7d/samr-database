# SAMR 数据库 dashboard

这是一个无需后端和第三方运行时依赖的静态页面。页面使用根目录 `manifest.csv` 生成的案例级快照，按 `dataset + id` 将正文和附件归并为一个案例。

在仓库根目录重新生成快照：

```powershell
py tools/build_visualization_snapshot.py --manifest manifest.csv --output dashboard/data/samr-viz-data.js
```

生成后直接打开 `dashboard/index.html` 即可查看。新增数据后重新执行上述命令即可刷新页面数据。
