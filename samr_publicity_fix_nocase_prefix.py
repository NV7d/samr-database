#!/usr/bin/env python3
"""Repair existing downloaded files/manifests by removing NOCASE_ prefix."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any, Dict, List


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["dedup_key"])
        return
    keys = []
    seen = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    if "dedup_key" in keys:
        keys.insert(0, keys.pop(keys.index("dedup_key")))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def pick_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        cand = path.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def main() -> int:
    p = argparse.ArgumentParser(description="Remove NOCASE_ prefix from existing files and manifests.")
    p.add_argument("--out-dir", default="~/Downloads/samr_publicity")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    out_dir = Path(os.path.expanduser(args.out_dir)).resolve()
    manifest_jsonl = out_dir / "manifest.jsonl"
    manifest_csv = out_dir / "manifest.csv"
    rows = load_jsonl(manifest_jsonl)
    if not rows:
        print(f"[info] no manifest records found: {manifest_jsonl}")
        return 0

    changed = 0
    renamed_files = 0
    missing_files = 0
    conflict_renamed = 0

    for row in rows:
        file_name = str(row.get("file_name", ""))
        file_path = str(row.get("file_path", ""))
        if not file_name.startswith("NOCASE_"):
            continue

        new_name = file_name[len("NOCASE_") :]
        changed += 1

        if row.get("caseNo") == "NOCASE":
            row["caseNo"] = ""

        if not file_path:
            row["file_name"] = new_name
            continue

        old = Path(file_path)
        new = old.with_name(new_name)
        if old.exists():
            if new.exists() and new != old:
                new2 = pick_target(new)
                if new2 != new:
                    conflict_renamed += 1
                new = new2
            if not args.dry_run:
                old.rename(new)
            renamed_files += 1
            row["file_name"] = new.name
            row["file_path"] = str(new)
        else:
            missing_files += 1
            row["file_name"] = new_name
            row["file_path"] = str(new)

    if not args.dry_run:
        rows.sort(key=lambda x: x.get("dedup_key", ""))
        write_jsonl(manifest_jsonl, rows)
        write_csv(manifest_csv, rows)

    print(
        f"[done] records_with_prefix={changed} renamed_files={renamed_files} missing_files={missing_files} conflict_renamed={conflict_renamed} dry_run={args.dry_run}"
    )
    if not args.dry_run:
        print(f"[done] updated: {manifest_jsonl}")
        print(f"[done] updated: {manifest_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
