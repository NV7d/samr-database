#!/usr/bin/env python3
"""Migrate v1 simple-case files to standard dataset layout.

This script scans root manifest (`manifest.jsonl`) for v1-like records
(`dedup_key` shape: `id::fileId` without URL), moves files into:

  <out-dir>/samr_simple_case_notices/files/{YYYY}/{MM}/{id}_{caseName}/

and updates:
  - root manifest.jsonl / manifest.csv (file_path and file metadata)
  - dedicated v1 manifest under samr_simple_case_notices/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_name(name: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", (name or "").strip())
    value = re.sub(r"_+", "_", value).strip("._ ")
    return value[:180] if value else "untitled"


def parse_ymd(text: str) -> Optional[datetime]:
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            return None
    return None


def choose_year_month(record: Dict[str, Any], old_path: Path) -> Tuple[str, str]:
    parts = old_path.parts
    try:
        idx = parts.index("files")
        yy = parts[idx + 1]
        mm = parts[idx + 2]
        if re.fullmatch(r"\d{4}", yy) and re.fullmatch(r"\d{2}", mm):
            return yy, mm
    except Exception:
        pass

    for k in ("receiveTime", "createTime", "list_date", "pub_date"):
        dt = parse_ymd(str(record.get(k) or ""))
        if dt:
            return f"{dt.year:04d}", f"{dt.month:02d}"
    return "unknown", "unknown"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_v1_like(record: Dict[str, Any]) -> bool:
    key = str(record.get("dedup_key") or "")
    if "::" not in key:
        return False
    left, right = key.split("::", 1)
    if any(x in left for x in ("http://", "https://")):
        return False
    if any(x in right for x in ("http://", "https://")):
        return False
    fp = str(record.get("file_path") or "")
    return "/files/" in fp


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: List[Dict[str, Any]], extra_first: List[str] | None = None) -> None:
    if not rows:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["dedup_key"])
        return

    preferred = [
        "dedup_key",
        "source",
        "record_type",
        "dataset",
        "id",
        "fileId",
        "caseNo",
        "caseName",
        "empName",
        "receiveTime",
        "createTime",
        "list_page",
        "list_date",
        "pub_date",
        "detail_url",
        "attachment_url",
        "saved_at",
        "file_name",
        "file_ext",
        "file_size",
        "sha256",
        "file_path",
    ]
    if extra_first:
        preferred = extra_first + preferred

    fields: List[str] = []
    seen = set()
    for k in preferred:
        if any(k in r for r in rows):
            fields.append(k)
            seen.add(k)
    for r in rows:
        for k in r.keys():
            if k not in seen:
                fields.append(k)
                seen.add(k)

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser(description="Migrate v1 simple-case records to standard dataset layout.")
    p.add_argument("--out-dir", default="~/Downloads/samr_publicity")
    p.add_argument("--dataset-subdir", default="samr_simple_case_notices")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    root = Path(args.out_dir).expanduser().resolve()
    dataset_subdir = safe_name(args.dataset_subdir) or "samr_simple_case_notices"
    dataset_root = root / dataset_subdir
    files_root = dataset_root / "files"

    manifest_jsonl = root / "manifest.jsonl"
    manifest_csv = root / "manifest.csv"
    if not manifest_jsonl.exists():
        raise SystemExit(f"manifest not found: {manifest_jsonl}")

    records: List[Dict[str, Any]] = []
    with manifest_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))

    report: Dict[str, Any] = {
        "started_at": now_iso(),
        "action": "migrate_v1_to_standard_layout",
        "dry_run": args.dry_run,
        "source_manifest_records": len(records),
        "v1_candidates": 0,
        "migrated": 0,
        "missing_source_file": 0,
        "path_updated": 0,
        "errors": [],
    }

    if not args.dry_run:
        dataset_root.mkdir(parents=True, exist_ok=True)
        files_root.mkdir(parents=True, exist_ok=True)

    v1_records: List[Dict[str, Any]] = []

    for rec in records:
        if not is_v1_like(rec):
            continue
        report["v1_candidates"] += 1

        old_fp = Path(str(rec.get("file_path") or ""))
        if not old_fp.exists() or not old_fp.is_file():
            report["missing_source_file"] += 1
            report["errors"].append({"type": "missing_source_file", "dedup_key": rec.get("dedup_key"), "old_path": str(old_fp)})
            continue

        cid = str(rec.get("id") or "").strip()
        case_name = str(rec.get("caseName") or rec.get("case_name") or "untitled").strip() or "untitled"
        yy, mm = choose_year_month(rec, old_fp)
        article_dir = files_root / yy / mm / safe_name(f"{cid}_{case_name}")

        ext = old_fp.suffix.lower()
        old_base = safe_name(old_fp.stem)
        new_name = safe_name(f"v1_{cid}_001_{old_base}") + ext
        new_fp = article_dir / new_name
        if new_fp.exists() and old_fp.resolve() != new_fp.resolve():
            i = 1
            while True:
                cand = article_dir / f"{new_fp.stem}_{i}{new_fp.suffix}"
                if not cand.exists():
                    new_fp = cand
                    break
                i += 1

        if not args.dry_run:
            article_dir.mkdir(parents=True, exist_ok=True)
            if old_fp.resolve() != new_fp.resolve():
                old_fp.rename(new_fp)

            rec["source"] = "v1"
            rec["record_type"] = "attachment"
            rec["file_path"] = str(new_fp)
            rec["file_name"] = new_fp.name
            rec["file_ext"] = new_fp.suffix.lower()
            rec["file_size"] = new_fp.stat().st_size
            rec["sha256"] = sha256_file(new_fp)
            if not rec.get("saved_at"):
                rec["saved_at"] = now_iso()

        report["migrated"] += 1
        report["path_updated"] += 1

        v1_row = dict(rec)
        v1_row["source"] = "v1_simple_cases"
        v1_row["dataset"] = dataset_subdir
        if args.dry_run:
            v1_row["file_path"] = str(new_fp)
            v1_row["file_name"] = new_fp.name
            v1_row["file_ext"] = new_fp.suffix.lower()
        v1_records.append(v1_row)

    root_sorted = sorted(records, key=lambda x: str(x.get("dedup_key") or ""))
    v1_sorted = sorted(v1_records, key=lambda x: str(x.get("dedup_key") or ""))

    v1_manifest_jsonl = dataset_root / "manifest.jsonl"
    v1_manifest_csv = dataset_root / "manifest.csv"
    v1_run_report = dataset_root / "run_report.json"

    if not args.dry_run:
        write_jsonl(manifest_jsonl, root_sorted)
        write_csv(manifest_csv, root_sorted)
        write_jsonl(v1_manifest_jsonl, v1_sorted)
        write_csv(v1_manifest_csv, v1_sorted)
    else:
        dataset_root.mkdir(parents=True, exist_ok=True)

    report["ended_at"] = now_iso()
    report["v1_manifest_total_records"] = len(v1_sorted)
    report["dataset_root"] = str(dataset_root)
    report["manifest_jsonl"] = str(v1_manifest_jsonl)
    report["manifest_csv"] = str(v1_manifest_csv)
    v1_run_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "[done] dry_run={dry} candidates={cand} migrated={mig} missing={miss} v1_manifest={vm}".format(
            dry=args.dry_run,
            cand=report["v1_candidates"],
            mig=report["migrated"],
            miss=report["missing_source_file"],
            vm=report["v1_manifest_total_records"],
        )
    )
    print(f"[done] report: {v1_run_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
