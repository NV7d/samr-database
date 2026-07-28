#!/usr/bin/env python3
"""
Shorten long file/dir names to avoid Windows path length errors.

It renames existing downloaded files and updates all manifest csv/jsonl
that contain `file_path` + `file_name`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]


def safe_name(name: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", (name or "").strip())
    value = re.sub(r"_+", "_", value).strip("._ ")
    return value if value else "untitled"


def short_name(name: str, max_len: int) -> str:
    value = safe_name(name)
    if len(value) <= max_len:
        return value
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    head = value[: max(1, max_len - 9)].rstrip("._ ")
    return f"{head}_{digest}"


def find_index_from_name(name: str, default_idx: int = 1) -> int:
    m = re.search(r"_(\d{3})(?:_|$)", name or "")
    if m:
        return int(m.group(1))
    return default_idx


def stable_attachment_name(rec: Dict[str, str], fallback_name: str, prefix: str, max_len: int = 72) -> str:
    attachment_url = str(rec.get("attachment_url") or "")
    url_name = unquote(Path(urlparse(attachment_url).path).name)
    source_name = safe_name(url_name or fallback_name)
    source_path = Path(source_name)
    ext = source_path.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = ""
    stem = source_path.stem if ext else source_name
    return short_name(f"{prefix}_{stem}", max(16, max_len - len(ext))) + ext


def build_target_relpath(rec: Dict[str, str], old_rel: str) -> str:
    p = Path(old_rel)
    ext = p.suffix.lower()
    source = str(rec.get("source") or "")
    rid = str(rec.get("id") or "unknown")
    case_name = str(rec.get("caseName") or "untitled")
    record_type = str(rec.get("record_type") or "")
    fname = p.name

    if old_rel.startswith("samr_simple_case_notices/files/"):
        parts = p.parts
        y, m = "unknown", "unknown"
        for i in range(2, len(parts) - 1):
            if re.fullmatch(r"\d{4}", parts[i]) and i + 1 < len(parts) and re.fullmatch(r"\d{2}", parts[i + 1]):
                y, m = parts[i], parts[i + 1]
                break
        article_dir = short_name(f"{rid}_{case_name}", 64)
        idx = find_index_from_name(fname, 1)
        new_file = short_name(f"{source}_{rid}_{idx:03d}_{case_name}", 72) + ext
        return str(Path("samr_simple_case_notices/files") / y / m / article_dir / new_file)

    if old_rel.startswith("mofcom_penalty_notices/files/"):
        parts = p.parts
        if len(parts) < 5:
            return old_rel
        y, m = parts[2], parts[3]
        article_dir = short_name(f"{rid}_{case_name}", 64)
        if record_type == "article_body":
            body_ext = ext if ext in (".md", ".html") else ".md"
            new_file = f"body{body_ext}"
        else:
            idx = find_index_from_name(fname, 1)
            new_file = stable_attachment_name(rec, fname, f"{rid}_{idx:03d}")
        return str(Path("mofcom_penalty_notices/files") / y / m / article_dir / new_file)

    if old_rel.startswith("samr_enforcement_cases/files/"):
        parts = p.parts
        if len(parts) < 6:
            return old_rel
        category, y, m = parts[2], parts[3], parts[4]
        article_dir = short_name(f"{rid}_{case_name}", 64)
        if record_type == "article_body":
            body_ext = ext if ext in (".md", ".html") else ".md"
            new_file = f"body{body_ext}"
        else:
            idx = find_index_from_name(fname, 1)
            new_file = stable_attachment_name(rec, fname, f"{category}_{rid}_{idx:03d}")
        return str(Path("samr_enforcement_cases/files") / category / y / m / article_dir / new_file)

    return old_rel


def unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not cand.exists():
            return cand
        i += 1


def stable_collision_target(path: Path, rec: Dict[str, str], old_abs: Path) -> Path:
    if not path.exists() or path == old_abs:
        return path
    key = str(rec.get("dedup_key") or rec.get("attachment_url") or path.name)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    candidate = path.with_name(f"{path.stem}_{digest}{path.suffix}")
    if not candidate.exists() or candidate == old_abs:
        return candidate
    return unique_target(candidate)


def resolve_record_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def rel_for_matching(value: str) -> str:
    path = resolve_record_path(value)
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return value


def format_record_path(original: str, new_abs: Path) -> str:
    try:
        return str(new_abs.relative_to(ROOT))
    except ValueError:
        return str(new_abs)


def find_existing_moved_file(old_rel: str) -> Path | None:
    parts = Path(old_rel).parts
    if not parts:
        return None

    if old_rel.startswith("samr_simple_case_notices/files/") and len(parts) >= 5:
        base = ROOT / Path(*parts[:4])
    elif old_rel.startswith("mofcom_penalty_notices/files/") and len(parts) >= 5:
        base = ROOT / Path(*parts[:4])
    elif old_rel.startswith("samr_enforcement_cases/files/") and len(parts) >= 6:
        base = ROOT / Path(*parts[:5])
    else:
        return None

    if not base.exists():
        return None
    matches = [p for p in base.rglob(parts[-1]) if p.is_file()]
    return matches[0] if len(matches) == 1 else None


def load_jsonl(path: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            t = line.strip()
            if t:
                rows.append(json.loads(t))
    return rows


def sweep_long_files(path_map: Dict[str, str]) -> int:
    moved = 0
    roots = [
        ROOT / "samr_simple_case_notices/files",
        ROOT / "mofcom_penalty_notices/files",
        ROOT / "samr_enforcement_cases/files",
    ]
    for base in roots:
        if not base.exists():
            continue
        for abs_path in list(base.rglob("*")):
            if not abs_path.is_file():
                continue
            rel = str(abs_path.relative_to(ROOT))
            if len(rel) <= 200:
                continue

            parts = Path(rel).parts
            ext = abs_path.suffix.lower()
            fname_short = short_name(Path(rel).stem, 64) + ext

            if rel.startswith("samr_simple_case_notices/files/"):
                y, m = "unknown", "unknown"
                for i in range(2, len(parts) - 1):
                    if re.fullmatch(r"\d{4}", parts[i]) and i + 1 < len(parts) and re.fullmatch(r"\d{2}", parts[i + 1]):
                        y, m = parts[i], parts[i + 1]
                        break
                article = short_name(parts[-2], 48) if len(parts) >= 2 else "untitled"
                target = ROOT / "samr_simple_case_notices/files" / y / m / article / fname_short
            elif rel.startswith("mofcom_penalty_notices/files/"):
                y = parts[2] if len(parts) > 2 else "unknown"
                m = parts[3] if len(parts) > 3 else "unknown"
                article = short_name(parts[-2], 48) if len(parts) >= 2 else "untitled"
                target = ROOT / "mofcom_penalty_notices/files" / y / m / article / fname_short
            else:
                category = parts[2] if len(parts) > 2 else "unknown"
                y = parts[3] if len(parts) > 3 else "unknown"
                m = parts[4] if len(parts) > 4 else "unknown"
                article = short_name(parts[-2], 48) if len(parts) >= 2 else "untitled"
                target = ROOT / "samr_enforcement_cases/files" / category / y / m / article / fname_short

            target.parent.mkdir(parents=True, exist_ok=True)
            target = unique_target(target)
            abs_path.rename(target)
            path_map[rel] = str(target.relative_to(ROOT))
            moved += 1
    return moved


def write_jsonl(path: Path, rows: List[Dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def sync_jsonl_paths(path: Path, path_map: Dict[str, str]) -> int:
    rows = load_jsonl(path)
    changed = 0
    for rec in rows:
        old = str(rec.get("file_path") or "")
        if not old:
            continue
        mapped = path_map.get(old) or path_map.get(rel_for_matching(old))
        if not mapped:
            continue
        mapped_abs = Path(mapped) if Path(mapped).is_absolute() else ROOT / mapped
        new = format_record_path(old, mapped_abs)
        if new == old:
            continue
        rec["file_path"] = new
        rec["file_name"] = mapped_abs.name
        rec["file_ext"] = mapped_abs.suffix.lower()
        changed += 1
    if changed:
        write_jsonl(path, rows)
    return changed


def update_csv(path: Path, path_map: Dict[str, str]) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = reader.fieldnames or []
    changed = 0
    for row in rows:
        old = row.get("file_path") or ""
        new = path_map.get(old)
        if not new:
            continue
        row["file_path"] = new
        row["file_name"] = Path(new).name
        changed += 1
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return changed


def rebuild_root_manifest(jsonl_paths: List[Path]) -> int:
    rows: List[Dict[str, str]] = []
    for path in jsonl_paths:
        dataset = path.parent.name
        for rec in load_jsonl(path):
            rows.append({"dataset": dataset, **rec})

    preferred = [
        "dataset", "dedup_key", "source", "category", "category_label", "record_type",
        "id", "fileId", "caseNo", "caseName", "empName", "list_page", "list_date",
        "pub_date", "receiveTime", "createTime", "detail_url", "attachment_url",
        "source_page", "source_total_count_snapshot", "saved_at", "file_name",
        "file_ext", "file_size", "sha256", "file_path",
    ]
    fields = preferred + sorted({key for row in rows for key in row if key not in preferred})
    with (ROOT / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def cleanup_empty_dirs(root: Path) -> None:
    for path in sorted([p for p in root.rglob("*") if p.is_dir()], key=lambda x: len(x.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def main() -> None:
    jsonl_paths = [
        ROOT / "samr_simple_case_notices/manifest.jsonl",
        ROOT / "mofcom_penalty_notices/manifest.jsonl",
        ROOT / "samr_enforcement_cases/manifest.jsonl",
    ]

    path_map: Dict[str, str] = {}
    moved = 0
    missing = 0

    for jp in jsonl_paths:
        rows = load_jsonl(jp)
        changed_rows = 0
        for rec in rows:
            old_rel = str(rec.get("file_path") or "")
            if not old_rel:
                continue
            old_match = rel_for_matching(old_rel)
            if old_rel in path_map or old_match in path_map:
                new_value = path_map.get(old_rel) or path_map[old_match]
                rec["file_path"] = new_value
                rec["file_name"] = Path(new_value).name
                changed_rows += 1
                continue

            old_abs = resolve_record_path(old_rel)
            if not old_abs.exists():
                moved_abs = find_existing_moved_file(old_match)
                if moved_abs:
                    new_value = format_record_path(old_rel, moved_abs)
                    path_map[old_rel] = new_value
                    path_map[old_match] = new_value
                    rec["file_path"] = new_value
                    rec["file_name"] = moved_abs.name
                    changed_rows += 1
                    continue
                missing += 1
                continue

            new_rel = build_target_relpath(rec, old_match)
            new_abs = ROOT / new_rel
            if new_abs == old_abs:
                new_value = format_record_path(old_rel, new_abs)
                if new_value != old_rel:
                    path_map[old_rel] = new_value
                    path_map[old_match] = new_value
                    rec["file_path"] = new_value
                    rec["file_name"] = new_abs.name
                    rec["file_ext"] = new_abs.suffix.lower()
                    changed_rows += 1
                continue
            new_abs.parent.mkdir(parents=True, exist_ok=True)
            new_abs = stable_collision_target(new_abs, rec, old_abs)
            if new_abs == old_abs:
                new_value = format_record_path(old_rel, new_abs)
                if new_value != old_rel:
                    path_map[old_rel] = new_value
                    path_map[old_match] = new_value
                    rec["file_path"] = new_value
                    rec["file_name"] = new_abs.name
                    rec["file_ext"] = new_abs.suffix.lower()
                    changed_rows += 1
                continue
            old_abs.rename(new_abs)

            new_value = format_record_path(old_rel, new_abs)
            path_map[old_rel] = new_value
            path_map[old_match] = new_value
            rec["file_path"] = new_value
            rec["file_name"] = new_abs.name
            changed_rows += 1
            moved += 1

        write_jsonl(jp, rows)
        print(f"[jsonl] {jp.relative_to(ROOT)} changed={changed_rows}")

    swept = sweep_long_files(path_map)
    if swept:
        print(f"[sweep] extra_moved={swept}")
        for jp in jsonl_paths:
            synced = sync_jsonl_paths(jp, path_map)
            if synced:
                print(f"[jsonl-sync] {jp.relative_to(ROOT)} changed={synced}")

    csv_paths = [
        ROOT / "samr_simple_case_notices/manifest.csv",
        ROOT / "mofcom_penalty_notices/manifest.csv",
        ROOT / "samr_enforcement_cases/manifest.csv",
    ]
    csv_changed = 0
    for cp in csv_paths:
        c = update_csv(cp, path_map)
        csv_changed += c
        print(f"[csv] {cp.relative_to(ROOT)} changed={c}")

    catalog_rows = rebuild_root_manifest(jsonl_paths)
    print(f"[catalog] manifest.csv rows={catalog_rows}")

    cleanup_empty_dirs(ROOT / "samr_simple_case_notices/files")
    cleanup_empty_dirs(ROOT / "mofcom_penalty_notices/files")
    cleanup_empty_dirs(ROOT / "samr_enforcement_cases/files")

    print(f"[done] moved={moved + swept} csv_changed={csv_changed} missing={missing}")


if __name__ == "__main__":
    main()
