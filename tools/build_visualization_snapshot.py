#!/usr/bin/env python3
"""Build a compact, case-level data snapshot for the SAMR visualizations.

The repository stores one manifest row per downloaded file.  This module keeps
that source of truth intact and derives a case-level view by grouping rows on
``dataset + id``.  The output is a JavaScript global so the dashboard can be
opened directly from disk without a web server or runtime dependencies.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DATASET_LABELS = {
    "samr_simple_case_notices": "简易案件公示",
    "samr_enforcement_cases": "SAMR 执法案件",
    "mofcom_penalty_notices": "商务部公告",
}

TRANSACTION_TYPES = ("收购", "合并", "新设合营", "其他")
DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?(?!\d)"
)
CHINESE_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})年(\d{1,2})月(?:(\d{1,2})日)?"
)
PATH_YEAR_MONTH_RE = re.compile(r"(?:^|/)(20\d{2})/(0?[1-9]|1[0-2])(?:/|$)")
ENTITY_SPLIT_RE = re.compile(r"[，、,;；|\r\n]")
LEGAL_SUFFIX_RE = re.compile(
    r"^(?:inc\.?|ltd\.?|llc|l\.?p\.?|lp|pte(?:\.?\s*ltd\.?)?|co\.?|corp\.?|corporation|limited|holdings|group)\b",
    re.IGNORECASE,
)
KNOWN_ATTACHMENT_EXTENSIONS = {".doc", ".docx", ".pdf", ".wps", ".zip"}


def clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_path(value: Any) -> str:
    return clean(value).replace("\\", "/")


def parse_int(value: Any) -> int:
    try:
        return int(float(clean(value)))
    except (TypeError, ValueError):
        return 0


def normalize_entity(value: Any) -> str:
    text = " ".join(clean(value).split())
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\s*，\s*", " ", text)
    text = text.strip(" \t\"'“”‘’[]【】")
    return text


def split_entities(value: Any) -> List[str]:
    text = clean(value)
    parts: List[str] = []
    buffer: List[str] = []
    bracket_pairs = {"（": "）", "(": ")", "[": "]", "【": "】", "{": "}"}
    closing_brackets = set(bracket_pairs.values())
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in bracket_pairs:
            depth += 1
            buffer.append(char)
            index += 1
            continue
        if char in closing_brackets:
            depth = max(0, depth - 1)
            buffer.append(char)
            index += 1
            continue
        if depth == 0 and ENTITY_SPLIT_RE.fullmatch(char):
            lookahead = text[index + 1 :]
            next_match = re.match(r"\s*([^，、,;；|\r\n]+)", lookahead)
            next_piece = next_match.group(1).strip() if next_match else ""
            current_piece = "".join(buffer).strip()
            # Chinese commas also appear inside Latin legal names, e.g.
            # ``AIC Parent， Inc.`` or ``HRA Investment GP， Ltd.``.
            # Keep those continuations together while still splitting the
            # next company in ``... Ltd，Other Company``.
            if char == "，" and next_piece and LEGAL_SUFFIX_RE.match(next_piece):
                buffer.append(char)
                index += 1
                continue
            parts.append(current_piece)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    parts.append("".join(buffer).strip())

    entities = {entity for entity in (normalize_entity(part) for part in parts) if entity}
    return sorted(entities, key=lambda item: (item.casefold(), item))


def classify_transaction_type(dataset: str, case_name: str) -> Optional[str]:
    if dataset != "samr_simple_case_notices":
        return None
    # Keep the classification deterministic when a title contains more than
    # one operation word.  The most specific simple-case label wins.
    if "新设合营" in case_name:
        return "新设合营"
    if "合并" in case_name:
        return "合并"
    if "收购" in case_name:
        return "收购"
    return "其他"


def parse_date_text(value: Any) -> Optional[Dict[str, Any]]:
    text = clean(value)
    if not text:
        return None

    match = DATE_RE.search(text) or CHINESE_DATE_RE.search(text)
    if not match:
        return None

    year = int(match.group(1))
    month = int(match.group(2))
    day = int(match.group(3)) if match.group(3) else None
    if not 1 <= month <= 12:
        return None
    if day is not None and not 1 <= day <= 31:
        return None

    date_value = f"{year:04d}-{month:02d}"
    granularity = "month"
    sort_value = date_value
    if day is not None:
        date_value = f"{year:04d}-{month:02d}-{day:02d}"
        granularity = "day"
        sort_value = date_value

    return {
        "date": date_value,
        "year": year,
        "month": month,
        "day": day,
        "granularity": granularity,
        "sort": sort_value,
    }


def parse_path_year_month(file_path: Any) -> Optional[Dict[str, Any]]:
    path = normalize_path(file_path)
    match = PATH_YEAR_MONTH_RE.search(path)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2))
    return {
        "date": f"{year:04d}-{month:02d}",
        "year": year,
        "month": month,
        "day": None,
        "granularity": "month",
        "sort": f"{year:04d}-{month:02d}",
    }


def choose_event_date(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    dataset = clean(row.get("dataset"))
    if dataset == "samr_simple_case_notices":
        fields = ("receiveTime", "list_date", "pub_date")
    else:
        fields = ("list_date", "pub_date", "receiveTime")

    for priority, field in enumerate(fields):
        parsed = parse_date_text(row.get(field))
        if parsed:
            parsed = dict(parsed)
            parsed["source"] = field
            parsed["priority"] = priority
            return parsed

    parsed = parse_path_year_month(row.get("file_path"))
    if parsed:
        parsed = dict(parsed)
        parsed["source"] = "file_path"
        parsed["priority"] = len(fields)
        return parsed
    return None


def case_key(row: Mapping[str, Any]) -> str:
    dataset = clean(row.get("dataset")) or "unknown"
    identifier = clean(row.get("id"))
    if not identifier:
        identifier = clean(row.get("dedup_key")) or clean(row.get("file_path"))
    return f"{dataset}::{identifier}"


def first_nonempty(rows: Sequence[Mapping[str, Any]], field: str) -> str:
    for row in rows:
        value = clean(row.get(field))
        if value:
            return value
    return ""


def choose_case_date(rows: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for row in rows:
        parsed = choose_event_date(row)
        if parsed:
            candidates.append(parsed)
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item["priority"], item["sort"]))


def derived_record_type(row: Mapping[str, Any]) -> str:
    raw = clean(row.get("record_type"))
    if raw:
        return raw
    extension = clean(row.get("file_ext")).lower()
    if extension in KNOWN_ATTACHMENT_EXTENSIONS:
        return "attachment"
    if extension in {".md", ".html"} and clean(row.get("file_name")).lower().startswith("body"):
        return "article_body"
    return "unknown"


def make_file_reference(row: Mapping[str, Any]) -> Dict[str, Any]:
    detail_url = clean(row.get("detail_url"))
    attachment_url = clean(row.get("attachment_url"))
    source_url = detail_url or attachment_url
    return {
        "key": clean(row.get("dedup_key")) or normalize_path(row.get("file_path")),
        "recordType": derived_record_type(row),
        "fileName": clean(row.get("file_name")),
        "fileExt": clean(row.get("file_ext")).lower(),
        "fileSize": parse_int(row.get("file_size")),
        "filePath": normalize_path(row.get("file_path")),
        "detailUrl": detail_url,
        "attachmentUrl": attachment_url,
        "sourceUrl": source_url,
        "sha256": clean(row.get("sha256")),
    }


def make_case(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    ordered_rows = list(rows)
    dataset = clean(ordered_rows[0].get("dataset")) or "unknown"
    name = first_nonempty(ordered_rows, "caseName")
    participant_text = first_nonempty(ordered_rows, "empName")
    event_date = choose_case_date(ordered_rows)
    files: List[Dict[str, Any]] = []
    seen_files = set()
    for row in ordered_rows:
        file_ref = make_file_reference(row)
        if file_ref["key"] in seen_files:
            continue
        seen_files.add(file_ref["key"])
        files.append(file_ref)

    category_label = first_nonempty(ordered_rows, "category_label")
    transaction_type = classify_transaction_type(dataset, name)
    sources = sorted({clean(row.get("source")) for row in ordered_rows if clean(row.get("source"))})
    extensions = sorted({file_ref["fileExt"] for file_ref in files if file_ref["fileExt"]})
    record_types = sorted({file_ref["recordType"] for file_ref in files if file_ref["recordType"]})
    case_date = event_date or {}
    return {
        "key": case_key(ordered_rows[0]),
        "dataset": dataset,
        "datasetLabel": DATASET_LABELS.get(dataset, dataset),
        "source": sources[0] if len(sources) == 1 else ", ".join(sources),
        "sources": sources,
        "caseNo": first_nonempty(ordered_rows, "caseNo"),
        "caseName": name,
        "participants": participant_text,
        "entities": split_entities(participant_text),
        "category": category_label or DATASET_LABELS.get(dataset, dataset),
        "categoryLabel": category_label,
        "transactionType": transaction_type,
        "date": case_date.get("date", ""),
        "year": case_date.get("year"),
        "month": case_date.get("month"),
        "dateSource": case_date.get("source", "unknown"),
        "dateGranularity": case_date.get("granularity", "unknown"),
        "fileCount": len(files),
        "fileTypes": extensions,
        "recordTypes": record_types,
        "hasBody": "article_body" in record_types,
        "hasAttachment": "attachment" in record_types or any(
            extension in KNOWN_ATTACHMENT_EXTENSIONS for extension in extensions
        ),
        "files": files,
    }


def sorted_counter_rows(counter: Counter, key_name: str = "label") -> List[Dict[str, Any]]:
    return [
        {key_name: label, "count": count}
        for label, count in sorted(counter.items(), key=lambda item: (-item[1], str(item[0])))
    ]


def build_entity_graph(cases: Sequence[Mapping[str, Any]], max_nodes: int = 200, max_edges: int = 1000) -> Dict[str, Any]:
    node_counts: Counter = Counter()
    edge_counts: Counter = Counter()
    cases_with_entities = 0
    for case in cases:
        entities = sorted(set(clean(entity) for entity in case.get("entities", []) if clean(entity)))
        if not entities:
            continue
        cases_with_entities += 1
        node_counts.update(entities)
        edge_counts.update(itertools.combinations(entities, 2))

    top_nodes = node_counts.most_common(max_nodes)
    allowed = {label for label, _ in top_nodes}
    nodes = [{"id": label, "label": label, "cases": count} for label, count in top_nodes]
    edges = [
        {"source": source, "target": target, "cases": count}
        for (source, target), count in sorted(
            edge_counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
        )
        if source in allowed and target in allowed
    ][:max_edges]
    return {
        "coverage": {
            "casesWithEntities": cases_with_entities,
            "caseCount": len(cases),
            "ratio": round(cases_with_entities / len(cases), 4) if cases else 0,
            "uniqueEntities": len(node_counts),
            "uniquePairs": len(edge_counts),
        },
        "nodes": nodes,
        "edges": edges,
    }


def build_snapshot(
    rows: Sequence[Mapping[str, Any]],
    manifest_path: Optional[Path] = None,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[case_key(row)].append(row)
    cases = [make_case(group) for group in groups.values()]
    cases.sort(key=lambda item: (item.get("date", ""), item["key"]), reverse=True)

    dataset_rows: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    dataset_cases: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        dataset_rows[clean(row.get("dataset")) or "unknown"].append(row)
    for case in cases:
        dataset_cases[case["dataset"]].append(case)

    dataset_summaries: List[Dict[str, Any]] = []
    all_file_extensions: Counter = Counter()
    all_record_types: Counter = Counter()
    all_duplicate_dedup_keys: Counter = Counter(clean(row.get("dedup_key")) for row in rows if clean(row.get("dedup_key")))
    for dataset in sorted(dataset_rows):
        rows_for_dataset = dataset_rows[dataset]
        cases_for_dataset = dataset_cases.get(dataset, [])
        sources = Counter(case["source"] for case in cases_for_dataset if case["source"])
        categories = Counter(case["category"] for case in cases_for_dataset if case["category"])
        extensions = Counter(clean(row.get("file_ext")).lower() for row in rows_for_dataset if clean(row.get("file_ext")))
        record_types = Counter(derived_record_type(row) for row in rows_for_dataset)
        all_file_extensions.update(extensions)
        all_record_types.update(record_types)
        dated = Counter(case["dateGranularity"] for case in cases_for_dataset)
        field_names = ("caseName", "caseNo", "participants", "date")
        completeness = {
            field: sum(1 for case in cases_for_dataset if clean(case.get(field)))
            for field in field_names
        }
        total_bytes = sum(parse_int(row.get("file_size")) for row in rows_for_dataset)
        dataset_summaries.append(
            {
                "key": dataset,
                "label": DATASET_LABELS.get(dataset, dataset),
                "caseCount": len(cases_for_dataset),
                "fileCount": len(rows_for_dataset),
                "multiFileCases": sum(1 for case in cases_for_dataset if case["fileCount"] > 1),
                "totalBytes": total_bytes,
                "sources": sorted_counter_rows(sources),
                "categories": sorted_counter_rows(categories),
                "fileExtensions": sorted_counter_rows(extensions, "extension"),
                "recordTypes": sorted_counter_rows(record_types, "recordType"),
                "dateCoverage": {
                    "day": dated.get("day", 0),
                    "month": dated.get("month", 0),
                    "unknown": dated.get("unknown", 0),
                },
                "fieldCompleteness": [
                    {
                        "field": field,
                        "filled": completeness[field],
                        "total": len(cases_for_dataset),
                        "ratio": round(completeness[field] / len(cases_for_dataset), 4)
                        if cases_for_dataset
                        else 0,
                    }
                    for field in field_names
                ],
            }
        )

    years = sorted({case["year"] for case in cases if case.get("year")})
    annual_cases: List[Dict[str, Any]] = []
    annual_files: List[Dict[str, Any]] = []
    for year in years:
        case_counts = Counter(case["dataset"] for case in cases if case.get("year") == year)
        file_counts = Counter(
            file_dataset
            for case in cases
            if case.get("year") == year
            for file_dataset in [case["dataset"]]
            for _ in range(case["fileCount"])
        )
        annual_cases.append(
            {
                "year": year,
                "datasets": {dataset: case_counts.get(dataset, 0) for dataset in DATASET_LABELS},
                "total": sum(case_counts.values()),
            }
        )
        annual_files.append(
            {
                "year": year,
                "datasets": {dataset: file_counts.get(dataset, 0) for dataset in DATASET_LABELS},
                "total": sum(file_counts.values()),
            }
        )

    monthly_cases_counter: Counter = Counter(
        case["date"][:7] for case in cases if case.get("date") and case.get("year")
    )
    monthly_cases = [
        {"month": month, "count": count}
        for month, count in sorted(monthly_cases_counter.items())
    ]

    transaction_counter: Dict[int, Counter] = defaultdict(Counter)
    for case in cases:
        if case["dataset"] == "samr_simple_case_notices" and case.get("year"):
            transaction_counter[case["year"]][case["transactionType"] or "其他"] += 1
    transaction_types = [
        {
            "year": year,
            "types": {transaction_type: counter.get(transaction_type, 0) for transaction_type in TRANSACTION_TYPES},
            "total": sum(counter.values()),
        }
        for year, counter in sorted(transaction_counter.items())
    ]

    enforcement_counter: Dict[int, Counter] = defaultdict(Counter)
    for case in cases:
        if case["dataset"] == "samr_enforcement_cases" and case.get("year"):
            enforcement_counter[case["year"]][case["category"]] += 1
    enforcement_categories = [
        {"year": year, "categories": dict(sorted(counter.items())), "total": sum(counter.values())}
        for year, counter in sorted(enforcement_counter.items())
    ]

    missing_fields = {
        field: sum(1 for case in cases if not clean(case.get(field)))
        for field in ("caseName", "caseNo", "participants", "date")
    }
    duplicate_dedup_keys = {
        key: count for key, count in all_duplicate_dedup_keys.items() if count > 1
    }
    missing_paths = sum(
        1
        for case in cases
        for file_ref in case["files"]
        if not file_ref["filePath"]
    )
    unknown_date_cases = sum(1 for case in cases if case["dateGranularity"] == "unknown")

    manifest_sha256 = ""
    manifest_mtime = ""
    if manifest_path and manifest_path.exists():
        digest = hashlib.sha256()
        with manifest_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest_sha256 = digest.hexdigest()
        manifest_mtime = datetime.fromtimestamp(manifest_path.stat().st_mtime).astimezone().isoformat(timespec="seconds")

    return {
        "meta": {
            "schemaVersion": 1,
            "generatedAt": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
            "manifestMtime": manifest_mtime,
            "manifestSha256": manifest_sha256,
            "fileCount": len(rows),
            "caseCount": len(cases),
            "datasetCount": len(dataset_summaries),
            "dateRange": {"minYear": min(years) if years else None, "maxYear": max(years) if years else None},
        },
        "datasets": dataset_summaries,
        "cases": cases,
        "series": {
            "annualCases": annual_cases,
            "annualFiles": annual_files,
            "monthlyCases": monthly_cases,
            "transactionTypes": transaction_types,
            "enforcementCategories": enforcement_categories,
        },
        "entityGraph": build_entity_graph(cases),
        "quality": {
            "missingFields": missing_fields,
            "missingPaths": missing_paths,
            "unknownDateCases": unknown_date_cases,
            "duplicateDedupKeys": duplicate_dedup_keys,
            "fileExtensions": sorted_counter_rows(all_file_extensions, "extension"),
            "recordTypes": sorted_counter_rows(all_record_types, "recordType"),
        },
    }


def read_manifest(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_javascript(snapshot: Mapping[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    output_path.write_text(
        "// Generated by tools/build_visualization_snapshot.py. Do not edit manually.\n"
        f"window.SAMR_VIZ_DATA = {payload};\n",
        encoding="utf-8",
    )


def write_json(snapshot: Mapping[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="manifest.csv", help="Root manifest CSV path")
    parser.add_argument(
        "--output",
        default="dashboard/data/samr-viz-data.js",
        help="JavaScript snapshot path used by the dashboard",
    )
    parser.add_argument(
        "--json-output",
        default="",
        help="Optional pretty-printed JSON snapshot path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        raise SystemExit(f"manifest not found: {manifest_path}")
    rows = read_manifest(manifest_path)
    snapshot = build_snapshot(rows, manifest_path=manifest_path)
    write_javascript(snapshot, Path(args.output))
    if args.json_output:
        write_json(snapshot, Path(args.json_output))
    print(
        f"Built {snapshot['meta']['caseCount']:,} cases from {snapshot['meta']['fileCount']:,} files "
        f"into {Path(args.output)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
