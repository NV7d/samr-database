#!/usr/bin/env python3
"""Download MOFCOM old-site simple-case doc/docx attachments.

Source:
  https://fldj.mofcom.gov.cn/article/jyzjzjyajgs/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


BASE = "https://fldj.mofcom.gov.cn"
LIST_ROOT = f"{BASE}/article/jyzjzjyajgs/"
ALLOWED_EXTS = {".doc", ".docx"}


@dataclass
class Config:
    out_dir: Path
    start_page: int
    end_page: int
    cutoff_date: str
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


def parse_ymd(text: str) -> Optional[datetime]:
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
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


def choose_year_month(list_date: str, pub_date: str) -> Tuple[str, str]:
    for candidate in (list_date, pub_date):
        dt = parse_ymd(candidate)
        if dt:
            return f"{dt.year:04d}", f"{dt.month:02d}"
    return "unknown", "unknown"


def build_request(url: str, *, cfg: Config) -> Request:
    req = Request(url=url, method="GET")
    req.add_header("User-Agent", cfg.user_agent)
    req.add_header("Referer", LIST_ROOT)
    if cfg.cookie:
        req.add_header("Cookie", cfg.cookie)
    return req


def http_get_text(url: str, *, cfg: Config) -> str:
    host = urlparse(url).hostname or ""
    if host.endswith("mofcom.gov.cn"):
        return curl_get_text(url, cfg=cfg)
    err: Optional[Exception] = None
    for attempt in range(1, cfg.retry + 1):
        try:
            req = build_request(url, cfg=cfg)
            with urlopen(req, timeout=cfg.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            err = exc
            if attempt < cfg.retry:
                time.sleep(min(2 ** (attempt - 1), 5))
    # Fallback for legacy TLS endpoints that fail with Python's SSL stack.
    return curl_get_text(url, cfg=cfg)


def http_get_bytes(url: str, *, cfg: Config) -> bytes:
    host = urlparse(url).hostname or ""
    if host.endswith("mofcom.gov.cn"):
        return curl_get_bytes(url, cfg=cfg)
    err: Optional[Exception] = None
    for attempt in range(1, cfg.retry + 1):
        try:
            req = build_request(url, cfg=cfg)
            with urlopen(req, timeout=cfg.timeout) as resp:
                return resp.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            err = exc
            if attempt < cfg.retry:
                time.sleep(min(2 ** (attempt - 1), 5))
    # Fallback for legacy TLS endpoints that fail with Python's SSL stack.
    return curl_get_bytes(url, cfg=cfg)


def curl_get_text(url: str, *, cfg: Config) -> str:
    cmd = [
        "curl",
        "-sSL",
        "--max-time",
        str(int(cfg.timeout)),
        "-A",
        cfg.user_agent,
        "-e",
        LIST_ROOT,
    ]
    if cfg.cookie:
        cmd.extend(["-H", f"Cookie: {cfg.cookie}"])
    cmd.append(url)
    err: Optional[Exception] = None
    for attempt in range(1, cfg.retry + 1):
        try:
            out = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return out.stdout
        except Exception as exc:  # noqa: BLE001
            err = exc
            if attempt < cfg.retry:
                time.sleep(min(2 ** (attempt - 1), 5))
    raise RuntimeError(f"curl text failed: {url}: {err}") from err


def curl_get_bytes(url: str, *, cfg: Config) -> bytes:
    cmd = [
        "curl",
        "-sSL",
        "--max-time",
        str(int(cfg.timeout)),
        "-A",
        cfg.user_agent,
        "-e",
        LIST_ROOT,
    ]
    if cfg.cookie:
        cmd.extend(["-H", f"Cookie: {cfg.cookie}"])
    cmd.append(url)
    err: Optional[Exception] = None
    for attempt in range(1, cfg.retry + 1):
        try:
            out = subprocess.run(cmd, check=True, capture_output=True)
            return out.stdout
        except Exception as exc:  # noqa: BLE001
            err = exc
            if attempt < cfg.retry:
                time.sleep(min(2 ** (attempt - 1), 5))
    raise RuntimeError(f"curl bytes failed: {url}: {err}") from err


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
        "source",
        "list_page",
        "list_date",
        "pub_date",
        "detail_url",
        "attachment_url",
        "id",
        "caseNo",
        "caseName",
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


def list_url(page_no: int) -> str:
    return LIST_ROOT if page_no == 1 else f"{LIST_ROOT}?{page_no}"


def parse_total_pages(list_html: str) -> int:
    m = re.search(r'totalpage\s*=\s*"(\d+)"', list_html)
    if m:
        return max(1, int(m.group(1)))
    return 1


def parse_list_items(list_html: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    pattern = re.compile(
        r'<li>\s*<a[^>]*title="([^"]+)"[^>]*href="([^"]+\.shtml)"[^>]*>.*?</a><span>([^<]+)</span>\s*</li>',
        re.S,
    )
    for m in pattern.finditer(list_html):
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        href = m.group(2).strip()
        list_date = m.group(3).strip()
        items.append(
            {
                "case_name": title,
                "detail_url": urljoin(BASE, href),
                "list_date": list_date,
            }
        )
    return items


def parse_detail_pub_date(detail_html: str) -> str:
    m = re.search(r"发布时间：\s*<span>([^<]+)</span>", detail_html)
    if m:
        d = parse_ymd(m.group(1))
        return d.strftime("%Y-%m-%d") if d else ""
    return ""


def parse_detail_attachments(detail_html: str, detail_url: str) -> List[str]:
    urls = []
    for m in re.finditer(r'href="([^"]+\.(?:docx?|pdf))"', detail_html, re.I):
        href = m.group(1).strip()
        urls.append(urljoin(detail_url, href))
    seen = set()
    out: List[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def ext_from_url(url: str) -> str:
    return Path(urlparse(url).path).suffix.lower()


def article_id(detail_url: str) -> str:
    p = Path(urlparse(detail_url).path).name
    m = re.search(r"(\d+)\.shtml$", p)
    if m:
        return m.group(1)
    h = hashlib.md5(detail_url.encode("utf-8")).hexdigest()
    return h[:16]


def parse_args() -> Config:
    p = argparse.ArgumentParser(
        description="Download MOFCOM simple-case doc/docx from fldj.mofcom.gov.cn."
    )
    p.add_argument("--out-dir", default="~/Downloads/samr_publicity")
    p.add_argument("--start-page", type=int, default=1)
    p.add_argument(
        "--end-page",
        type=int,
        default=0,
        help="0 means auto to detected last page.",
    )
    p.add_argument("--cutoff-date", default="2022-08-31")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--retry", type=int, default=3)
    p.add_argument("--sleep-ms", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--user-agent",
        default="Mozilla/5.0 (compatible; samr-publicity-downloader-v3/1.0)",
    )
    p.add_argument(
        "--cookie",
        default=os.environ.get("SAMR_COOKIE", ""),
        help="Optional cookie if site anti-bot changes.",
    )
    args = p.parse_args()
    return Config(
        out_dir=Path(os.path.expanduser(args.out_dir)).resolve(),
        start_page=max(1, args.start_page),
        end_page=max(0, args.end_page),
        cutoff_date=args.cutoff_date,
        timeout=max(1.0, args.timeout),
        retry=max(1, args.retry),
        sleep_ms=max(0, args.sleep_ms),
        dry_run=args.dry_run,
        user_agent=args.user_agent,
        cookie=args.cookie.strip(),
    )


def main() -> int:
    cfg = parse_args()
    cutoff_dt = parse_ymd(cfg.cutoff_date)
    if cutoff_dt is None:
        raise ValueError(f"Invalid --cutoff-date: {cfg.cutoff_date}")

    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    files_root = cfg.out_dir / "files"
    files_root.mkdir(parents=True, exist_ok=True)

    manifest_jsonl = cfg.out_dir / "manifest.jsonl"
    manifest_csv = cfg.out_dir / "manifest.csv"
    run_report_path = cfg.out_dir / "run_report.json"

    existing = load_manifest_jsonl(manifest_jsonl)
    report: Dict[str, Any] = {
        "started_at": now_iso(),
        "source": "mofcom-fldj-jyaj",
        "list_root": LIST_ROOT,
        "dry_run": cfg.dry_run,
        "start_page": cfg.start_page,
        "end_page_arg": cfg.end_page,
        "cutoff_date": cfg.cutoff_date,
        "timeout": cfg.timeout,
        "retry": cfg.retry,
        "sleep_ms": cfg.sleep_ms,
        "existing_manifest_records": len(existing),
        "api_total_count_snapshot": None,
        "api_total_pages_snapshot": 0,
        "scanned_list_items": 0,
        "filtered_by_date": 0,
        "detail_fetch_failed": 0,
        "attachment_candidates": 0,
        "already_downloaded": 0,
        "recovered_missing_file": 0,
        "download_success": 0,
        "would_download": 0,
        "download_failed": 0,
        "skipped_non_doc": 0,
        "skipped_no_attachment": 0,
        "errors": [],
        "skips": [],
    }

    first_html = http_get_text(list_url(cfg.start_page), cfg=cfg)
    total_pages = parse_total_pages(first_html)
    report["api_total_pages_snapshot"] = total_pages
    end_page = total_pages if cfg.end_page == 0 else min(cfg.end_page, total_pages)
    if cfg.start_page > end_page:
        raise ValueError(f"start-page({cfg.start_page}) > end-page({end_page})")

    # Count is not directly exposed; infer by pages * approx 15 for observability only.
    report["api_total_count_snapshot"] = total_pages * 15
    print(
        f"[info] start scan: total_pages={total_pages}, pages={cfg.start_page}-{end_page}, cutoff={cfg.cutoff_date}, dry_run={cfg.dry_run}"
    )

    for page_no in range(cfg.start_page, end_page + 1):
        try:
            html = first_html if page_no == cfg.start_page else http_get_text(list_url(page_no), cfg=cfg)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(
                {"type": "list_page_failed", "page_no": page_no, "error": str(exc)}
            )
            continue

        items = parse_list_items(html)
        for item in items:
            report["scanned_list_items"] += 1
            dt = parse_ymd(item["list_date"])
            if dt is None:
                report["filtered_by_date"] += 1
                report["skips"].append(
                    {
                        "type": "invalid_list_date",
                        "page_no": page_no,
                        "detail_url": item["detail_url"],
                        "list_date": item["list_date"],
                    }
                )
                continue
            if dt > cutoff_dt:
                report["filtered_by_date"] += 1
                continue

            detail_url = item["detail_url"]
            try:
                detail_html = http_get_text(detail_url, cfg=cfg)
            except Exception as exc:  # noqa: BLE001
                report["detail_fetch_failed"] += 1
                report["errors"].append(
                    {
                        "type": "detail_fetch_failed",
                        "page_no": page_no,
                        "detail_url": detail_url,
                        "error": str(exc),
                    }
                )
                continue

            pub_date = parse_detail_pub_date(detail_html)
            attachments = parse_detail_attachments(detail_html, detail_url)
            if not attachments:
                report["skipped_no_attachment"] += 1
                report["skips"].append(
                    {"type": "no_attachment", "page_no": page_no, "detail_url": detail_url}
                )
                continue

            for attachment_url in attachments:
                report["attachment_candidates"] += 1
                ext = ext_from_url(attachment_url)
                if ext not in ALLOWED_EXTS:
                    report["skipped_non_doc"] += 1
                    report["skips"].append(
                        {
                            "type": "non_doc_ext",
                            "page_no": page_no,
                            "detail_url": detail_url,
                            "attachment_url": attachment_url,
                            "detected_ext": ext,
                        }
                    )
                    continue

                key = f"{detail_url}::{attachment_url}"
                old = existing.get(key)
                if old and old.get("file_path") and Path(old["file_path"]).exists():
                    report["already_downloaded"] += 1
                    continue
                if old and old.get("file_path") and not Path(old["file_path"]).exists():
                    report["recovered_missing_file"] += 1

                y, m = choose_year_month(item["list_date"], pub_date)
                target_dir = files_root / y / m
                target_dir.mkdir(parents=True, exist_ok=True)

                aid = article_id(detail_url)
                case_name = item["case_name"] or "untitled"
                name = safe_name(f"{aid}_{case_name}") + ext
                file_path = target_dir / name
                if file_path.exists():
                    stem = file_path.stem
                    suffix = file_path.suffix
                    i = 1
                    while True:
                        cand = file_path.with_name(f"{stem}_{i}{suffix}")
                        if not cand.exists():
                            file_path = cand
                            break
                        i += 1

                if cfg.dry_run:
                    report["would_download"] += 1
                    continue

                try:
                    content = http_get_bytes(attachment_url, cfg=cfg)
                except Exception as exc:  # noqa: BLE001
                    report["download_failed"] += 1
                    report["errors"].append(
                        {
                            "type": "download_failed",
                            "page_no": page_no,
                            "detail_url": detail_url,
                            "attachment_url": attachment_url,
                            "error": str(exc),
                        }
                    )
                    continue

                file_path.write_bytes(content)
                existing[key] = {
                    "dedup_key": key,
                    "source": "mofcom-fldj-jyaj",
                    "list_page": page_no,
                    "list_date": item["list_date"],
                    "pub_date": pub_date,
                    "detail_url": detail_url,
                    "attachment_url": attachment_url,
                    "id": aid,
                    "caseNo": "",
                    "caseName": case_name,
                    "saved_at": now_iso(),
                    "file_name": file_path.name,
                    "file_ext": ext,
                    "file_size": len(content),
                    "sha256": sha256sum(content),
                    "file_path": str(file_path),
                }
                report["download_success"] += 1

            if cfg.sleep_ms:
                time.sleep(cfg.sleep_ms / 1000.0)

    merged = sorted(existing.values(), key=lambda x: x.get("dedup_key", ""))
    if not cfg.dry_run:
        write_manifest_jsonl(manifest_jsonl, merged)
        write_manifest_csv(manifest_csv, merged)

    report["ended_at"] = now_iso()
    report["manifest_total_records"] = len(merged)
    report["manifest_jsonl"] = str(manifest_jsonl) if not cfg.dry_run else ""
    report["manifest_csv"] = str(manifest_csv) if not cfg.dry_run else ""
    run_report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        "[done] scanned_items={scanned} filtered_by_date={filtered} candidates={cands} already={already} success={success} would_download={would} failed={failed} non_doc={non_doc} manifest_total={manifest_total}".format(
            scanned=report["scanned_list_items"],
            filtered=report["filtered_by_date"],
            cands=report["attachment_candidates"],
            already=report["already_downloaded"],
            success=report["download_success"],
            would=report["would_download"],
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
