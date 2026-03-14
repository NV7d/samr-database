#!/usr/bin/env python3
"""Download SAMR publicity doc/docx attachments with incremental manifests.

Source:
  - List API: https://jyzjz.samr.gov.cn/fld_sb_server/tCasePublish/querySampleCase
  - File API:  https://jyzjz.samr.gov.cn/fld_sb_server/fldFile/download?attachmentId=...
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


LIST_URL = "https://jyzjz.samr.gov.cn/fld_sb_server/tCasePublish/querySampleCase"
DOWNLOAD_URL_TMPL = (
    "https://jyzjz.samr.gov.cn/fld_sb_server/fldFile/download?attachmentId={attachment_id}"
)
ALLOWED_EXTS = {".doc", ".docx"}


@dataclass
class Config:
    out_dir: Path
    page_size: int
    max_pages: int
    timeout: float
    retry: int
    sleep_ms: int
    dry_run: bool
    user_agent: str
    cookie: str


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_name(name: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", (name or "").strip())
    value = re.sub(r"_+", "_", value).strip("._ ")
    return value[:180] if value else "untitled"


def parse_date_ym(record: Dict[str, Any]) -> Tuple[str, str]:
    for key in ("receiveTime", "createTime"):
        raw = (record.get(key) or "").strip()
        if not raw:
            continue
        raw = raw.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw[:19], fmt)
                return f"{dt.year:04d}", f"{dt.month:02d}"
            except ValueError:
                continue
    return "unknown", "unknown"


def build_request(url: str, method: str = "GET", data: Optional[bytes] = None, *, cfg: Config) -> Request:
    req = Request(url=url, data=data, method=method)
    req.add_header("User-Agent", cfg.user_agent)
    if cfg.cookie:
        req.add_header("Cookie", cfg.cookie)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    return req


def http_json_post(url: str, payload: Dict[str, Any], *, cfg: Config) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    err: Optional[Exception] = None
    for attempt in range(1, cfg.retry + 1):
        try:
            req = build_request(url, method="POST", data=body, cfg=cfg)
            with urlopen(req, timeout=cfg.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            err = exc
            if attempt < cfg.retry:
                time.sleep(min(2 ** (attempt - 1), 5))
    raise RuntimeError(f"POST {url} failed after {cfg.retry} attempts: {err}") from err


def http_download(attachment_id: str, *, cfg: Config) -> Tuple[bytes, Dict[str, str]]:
    url = DOWNLOAD_URL_TMPL.format(attachment_id=quote(attachment_id))
    err: Optional[Exception] = None
    for attempt in range(1, cfg.retry + 1):
        try:
            req = build_request(url, cfg=cfg)
            with urlopen(req, timeout=cfg.timeout) as resp:
                data = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
            return data, headers
        except (HTTPError, URLError, TimeoutError) as exc:
            err = exc
            if attempt < cfg.retry:
                time.sleep(min(2 ** (attempt - 1), 5))
    raise RuntimeError(f"GET {url} failed after {cfg.retry} attempts: {err}") from err


def parse_filename_from_content_disposition(header_value: str) -> Optional[str]:
    if not header_value:
        return None
    # RFC 5987: filename*=UTF-8''...
    m = re.search(r"filename\*\s*=\s*([^;]+)", header_value, flags=re.I)
    if m:
        raw = m.group(1).strip().strip('"').strip("'")
        if "''" in raw:
            _, encoded = raw.split("''", 1)
            try:
                from urllib.parse import unquote

                return unquote(encoded)
            except Exception:
                pass
    m = re.search(r'filename\s*=\s*"([^"]+)"', header_value, flags=re.I)
    if not m:
        m = re.search(r"filename\s*=\s*([^;]+)", header_value, flags=re.I)
    if not m:
        return None
    raw_name = m.group(1).strip().strip('"')
    try:
        chunks = decode_header(raw_name)
        return "".join(
            (
                part.decode(enc or "utf-8", errors="replace")
                if isinstance(part, (bytes, bytearray))
                else str(part)
            )
            for part, enc in chunks
        )
    except Exception:
        return raw_name


def get_ext(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def sha256sum(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def load_manifest_jsonl(path: Path) -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                print(f"[warn] ignore malformed JSONL line {line_no}", file=sys.stderr)
                continue
            key = obj.get("dedup_key")
            if key:
                records[key] = obj
    return records


def write_manifest_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in records:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_manifest_csv(path: Path, records: Iterable[Dict[str, Any]]) -> None:
    rows = list(records)
    fields = [
        "dedup_key",
        "id",
        "fileId",
        "caseNo",
        "caseName",
        "empName",
        "receiveTime",
        "createTime",
        "source_page",
        "source_total_count_snapshot",
        "saved_at",
        "file_name",
        "file_ext",
        "file_size",
        "sha256",
        "file_path",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def make_payload(page_no: int, limit: int) -> Dict[str, Any]:
    return {
        "parameters": [],
        "requestPage": {"pageNo": page_no, "limit": limit},
        "sorts": [],
    }


def parse_args() -> Config:
    p = argparse.ArgumentParser(
        description="Batch download SAMR simple case publicity .doc/.docx attachments."
    )
    p.add_argument("--out-dir", default="~/Downloads/samr_publicity")
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="0 means all pages from API totalPages.",
    )
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--retry", type=int, default=3)
    p.add_argument("--sleep-ms", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--user-agent",
        default="Mozilla/5.0 (compatible; samr-publicity-downloader/1.0)",
    )
    p.add_argument(
        "--cookie",
        default=os.environ.get("SAMR_COOKIE", ""),
        help="Optional cookie for future anti-bot scenarios.",
    )
    args = p.parse_args()
    return Config(
        out_dir=Path(os.path.expanduser(args.out_dir)).resolve(),
        page_size=max(args.page_size, 1),
        max_pages=max(args.max_pages, 0),
        timeout=max(args.timeout, 1.0),
        retry=max(args.retry, 1),
        sleep_ms=max(args.sleep_ms, 0),
        dry_run=args.dry_run,
        user_agent=args.user_agent,
        cookie=args.cookie.strip(),
    )


def main() -> int:
    cfg = parse_args()
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    files_root = cfg.out_dir / "files"
    files_root.mkdir(parents=True, exist_ok=True)

    manifest_jsonl = cfg.out_dir / "manifest.jsonl"
    manifest_csv = cfg.out_dir / "manifest.csv"
    run_report_path = cfg.out_dir / "run_report.json"

    existing = load_manifest_jsonl(manifest_jsonl)
    report: Dict[str, Any] = {
        "started_at": now_iso(),
        "out_dir": str(cfg.out_dir),
        "list_url": LIST_URL,
        "download_url_template": DOWNLOAD_URL_TMPL,
        "dry_run": cfg.dry_run,
        "page_size": cfg.page_size,
        "max_pages": cfg.max_pages,
        "timeout": cfg.timeout,
        "retry": cfg.retry,
        "sleep_ms": cfg.sleep_ms,
        "existing_manifest_records": len(existing),
        "scanned_records": 0,
        "already_downloaded": 0,
        "download_success": 0,
        "would_download": 0,
        "download_failed": 0,
        "skipped_non_doc": 0,
        "recovered_missing_file": 0,
        "api_total_count_snapshot": None,
        "api_total_pages_snapshot": None,
        "errors": [],
        "skips": [],
    }

    first = http_json_post(LIST_URL, make_payload(1, cfg.page_size), cfg=cfg)
    if first.get("state") != 200 or not first.get("data"):
        raise RuntimeError(f"Unexpected list API response: state={first.get('state')}")
    page_result = first["data"].get("pageResult") or {}
    total_pages = int(page_result.get("totalPages") or 1)
    total_count = int(page_result.get("totalCount") or 0)
    report["api_total_count_snapshot"] = total_count
    report["api_total_pages_snapshot"] = total_pages

    scan_pages = total_pages if cfg.max_pages == 0 else min(total_pages, cfg.max_pages)
    print(
        f"[info] start scan: total_count={total_count}, total_pages={total_pages}, scan_pages={scan_pages}, dry_run={cfg.dry_run}"
    )

    all_pages_data: List[Tuple[int, Dict[str, Any]]] = [(1, first)]
    for page_no in range(2, scan_pages + 1):
        if cfg.sleep_ms:
            time.sleep(cfg.sleep_ms / 1000.0)
        resp = http_json_post(LIST_URL, make_payload(page_no, cfg.page_size), cfg=cfg)
        if resp.get("state") != 200:
            report["errors"].append(
                {"type": "list_page_failed", "page_no": page_no, "state": resp.get("state")}
            )
            continue
        all_pages_data.append((page_no, resp))

    for page_no, resp in all_pages_data:
        rows = ((resp.get("data") or {}).get("dataResult")) or []
        for rec in rows:
            report["scanned_records"] += 1
            case_id = str(rec.get("id") or "")
            file_id = str(rec.get("fileId") or "")
            if not case_id or not file_id:
                report["download_failed"] += 1
                report["errors"].append(
                    {
                        "type": "missing_id_or_fileid",
                        "page_no": page_no,
                        "id": case_id,
                        "fileId": file_id,
                    }
                )
                continue

            dedup_key = f"{case_id}::{file_id}"
            old = existing.get(dedup_key)
            if old and old.get("file_path") and Path(old["file_path"]).exists():
                report["already_downloaded"] += 1
                continue
            if old and old.get("file_path") and not Path(old["file_path"]).exists():
                report["recovered_missing_file"] += 1

            header_filename = ""
            ext = ""
            content = b""
            try:
                content, headers = http_download(file_id, cfg=cfg)
                header_filename = parse_filename_from_content_disposition(
                    headers.get("content-disposition", "")
                ) or ""
                ext = get_ext(header_filename)
            except Exception as exc:  # noqa: BLE001
                report["download_failed"] += 1
                report["errors"].append(
                    {
                        "type": "download_error",
                        "page_no": page_no,
                        "id": case_id,
                        "fileId": file_id,
                        "error": str(exc),
                    }
                )
                continue

            if ext not in ALLOWED_EXTS:
                report["skipped_non_doc"] += 1
                report["skips"].append(
                    {
                        "type": "non_doc_ext",
                        "page_no": page_no,
                        "id": case_id,
                        "fileId": file_id,
                        "header_filename": header_filename,
                        "detected_ext": ext,
                    }
                )
                continue

            year, month = parse_date_ym(rec)
            target_dir = files_root / year / month
            target_dir.mkdir(parents=True, exist_ok=True)

            case_no = (rec.get("caseNo") or "NOCASE").strip()
            case_name = rec.get("caseName") or "untitled"
            save_name = safe_name(f"{case_no}_{case_id}_{case_name}") + ext
            file_path = target_dir / save_name

            # Avoid accidental overwrite when normalized names collide.
            if file_path.exists():
                stem = file_path.stem
                suffix = file_path.suffix
                i = 1
                while True:
                    candidate = file_path.with_name(f"{stem}_{i}{suffix}")
                    if not candidate.exists():
                        file_path = candidate
                        break
                    i += 1

            file_size = 0
            file_sha = ""
            if cfg.dry_run:
                report["would_download"] += 1
                continue

            file_path.write_bytes(content)
            file_size = len(content)
            file_sha = sha256sum(content)

            row = {
                "dedup_key": dedup_key,
                "id": case_id,
                "fileId": file_id,
                "caseNo": rec.get("caseNo") or "",
                "caseName": rec.get("caseName") or "",
                "empName": rec.get("empName") or "",
                "receiveTime": rec.get("receiveTime") or "",
                "createTime": rec.get("createTime") or "",
                "source_page": page_no,
                "source_total_count_snapshot": total_count,
                "saved_at": now_iso(),
                "file_name": file_path.name,
                "file_ext": ext,
                "file_size": file_size,
                "sha256": file_sha,
                "file_path": str(file_path),
            }
            existing[dedup_key] = row
            report["download_success"] += 1

    merged_records = sorted(existing.values(), key=lambda x: x.get("dedup_key", ""))
    if not cfg.dry_run:
        # Manifests are always re-written from merged state so increment runs remain deterministic.
        write_manifest_jsonl(manifest_jsonl, merged_records)
        write_manifest_csv(manifest_csv, merged_records)

    report["ended_at"] = now_iso()
    report["manifest_total_records"] = len(merged_records)
    report["manifest_jsonl"] = str(manifest_jsonl) if not cfg.dry_run else ""
    report["manifest_csv"] = str(manifest_csv) if not cfg.dry_run else ""
    run_report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "[done] scanned={scanned} already={already} success={success} would_download={would_download} failed={failed} non_doc={non_doc} manifest_total={manifest_total}".format(
            scanned=report["scanned_records"],
            already=report["already_downloaded"],
            success=report["download_success"],
            would_download=report["would_download"],
            failed=report["download_failed"],
            non_doc=report["skipped_non_doc"],
            manifest_total=report["manifest_total_records"],
        )
    )
    print(f"[done] report: {run_report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
