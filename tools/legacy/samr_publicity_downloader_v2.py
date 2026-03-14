#!/usr/bin/env python3
"""Download SAMR (samr.gov.cn) simple-case doc/docx attachments.

Target source:
  https://www.samr.gov.cn/fldes/ajgs/jyaj/index.html

Rules:
  - Scan from page 136 (inclusive) onward by default.
  - Apply cutoff date on list date: <= 2022-08-31 by default.
  - Save files by year/month and keep CSV + JSONL manifests.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


BASE = "https://www.samr.gov.cn"
INDEX_URL = f"{BASE}/fldes/ajgs/jyaj/index.html"
LIST_API = f"{BASE}/api-gateway/jpaas-publish-server/front/page/build/unit"

FIXED_LIST_PARAMS = {
    "parseType": "bulidstatic",
    "webId": "29e9522dc89d4e088a953d8cede72f4c",
    "tplSetId": "5c30fb89ae5e48b9aefe3cdf49853830",
    "pageType": "column",
    "tagId": "ajax分页",
    "editType": "null",
    "pageId": "52e2e352f3b84eeaa3e010400866831d",
}

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
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
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
    req.add_header("Referer", INDEX_URL)
    if cfg.cookie:
        req.add_header("Cookie", cfg.cookie)
    return req


def http_get_text(url: str, *, cfg: Config) -> str:
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
    raise RuntimeError(f"GET text failed: {url}: {err}") from err


def http_get_bytes(url: str, *, cfg: Config) -> bytes:
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
    raise RuntimeError(f"GET bytes failed: {url}: {err}") from err


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


def parse_pagination(html: str) -> Tuple[int, int]:
    count_m = re.search(r'\bcount="(\d+)"', html)
    rows_m = re.search(r'\brows="(\d+)"', html)
    count = int(count_m.group(1)) if count_m else 0
    rows = int(rows_m.group(1)) if rows_m else 10
    total_pages = max(1, math.ceil(count / max(rows, 1)))
    return count, total_pages


def parse_list_items(html: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    li_pattern = re.compile(
        r'<li class="content-3-left-text imgContent01new">(.+?)</li>', re.S
    )
    for li in li_pattern.findall(html):
        a_m = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
        d_m = re.search(r'contentRight01time">([^<]+)<', li)
        if not a_m or not d_m:
            continue
        href = a_m.group(1).strip()
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", a_m.group(2))).strip()
        list_date = d_m.group(1).strip()
        items.append(
            {
                "detail_url": urljoin(BASE, href),
                "case_name": title,
                "list_date": list_date,
            }
        )
    return items


def parse_detail_pub_date(html: str) -> str:
    m = re.search(r"发布时间：\s*(\d{4}-\d{2}-\d{2})", html)
    if m:
        return m.group(1)
    m = re.search(r'<meta name="PubDate" content="([^"]+)"', html)
    if m:
        d = parse_ymd(m.group(1))
        return d.strftime("%Y-%m-%d") if d else ""
    return ""


def parse_attachments(html: str, detail_url: str) -> List[str]:
    urls = []
    for m in re.finditer(r'href="([^"]+\.(?:docx?|pdf))"', html, re.I):
        href = m.group(1).strip()
        urls.append(urljoin(detail_url, href))
    # keep order, unique
    seen = set()
    result = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        result.append(u)
    return result


def ext_from_url(url: str) -> str:
    path = urlparse(url).path
    return Path(path).suffix.lower()


def filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = Path(path).name
    return name or "download.bin"


def article_id_from_detail_url(detail_url: str) -> str:
    m = re.search(r"/art/\d+/art_([a-zA-Z0-9]+)\.html", detail_url)
    if m:
        return m.group(1)
    h = hashlib.md5(detail_url.encode("utf-8")).hexdigest()
    return h[:16]


def build_list_url(page_no: int) -> str:
    params = dict(FIXED_LIST_PARAMS)
    params["paramJson"] = json.dumps({"pageNo": page_no, "pageSize": 10}, ensure_ascii=False)
    return f"{LIST_API}?{urlencode(params)}"


def parse_args() -> Config:
    p = argparse.ArgumentParser(
        description="Download SAMR simple-case doc/docx from samr.gov.cn (page 136+)."
    )
    p.add_argument("--out-dir", default="~/Downloads/samr_publicity")
    p.add_argument("--start-page", type=int, default=136)
    p.add_argument(
        "--end-page",
        type=int,
        default=0,
        help="0 means auto to the last page detected from list response.",
    )
    p.add_argument("--cutoff-date", default="2022-08-31")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--retry", type=int, default=3)
    p.add_argument("--sleep-ms", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--user-agent",
        default="Mozilla/5.0 (compatible; samr-publicity-downloader-v2/1.0)",
    )
    p.add_argument(
        "--cookie",
        default=os.environ.get("SAMR_COOKIE", ""),
        help="Optional cookie for anti-bot scenarios.",
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
        "source": "samr.gov.cn-jyaj",
        "index_url": INDEX_URL,
        "list_api": LIST_API,
        "dry_run": cfg.dry_run,
        "start_page": cfg.start_page,
        "end_page_arg": cfg.end_page,
        "cutoff_date": cfg.cutoff_date,
        "timeout": cfg.timeout,
        "retry": cfg.retry,
        "sleep_ms": cfg.sleep_ms,
        "existing_manifest_records": len(existing),
        "api_total_count_snapshot": 0,
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

    first_raw = http_get_text(build_list_url(cfg.start_page), cfg=cfg)
    first_json = json.loads(first_raw)
    if not first_json.get("success") or not first_json.get("data"):
        raise RuntimeError("List API first page returned invalid payload")
    first_html = first_json["data"].get("html", "")
    total_count, total_pages = parse_pagination(first_html)
    report["api_total_count_snapshot"] = total_count
    report["api_total_pages_snapshot"] = total_pages

    end_page = total_pages if cfg.end_page == 0 else min(cfg.end_page, total_pages)
    if cfg.start_page > end_page:
        raise ValueError(f"start-page({cfg.start_page}) > end-page({end_page})")

    print(
        f"[info] start scan: total_count={total_count}, total_pages={total_pages}, pages={cfg.start_page}-{end_page}, cutoff={cfg.cutoff_date}, dry_run={cfg.dry_run}"
    )

    page_htmls: Dict[int, str] = {cfg.start_page: first_html}
    for page_no in range(cfg.start_page, end_page + 1):
        if page_no != cfg.start_page:
            if cfg.sleep_ms:
                time.sleep(cfg.sleep_ms / 1000.0)
            try:
                raw = http_get_text(build_list_url(page_no), cfg=cfg)
                obj = json.loads(raw)
                if not obj.get("success") or not obj.get("data"):
                    report["errors"].append(
                        {"type": "list_page_failed", "page_no": page_no, "reason": "invalid_payload"}
                    )
                    continue
                page_htmls[page_no] = obj["data"].get("html", "")
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(
                    {"type": "list_page_failed", "page_no": page_no, "error": str(exc)}
                )
                continue

        items = parse_list_items(page_htmls[page_no])
        for item in items:
            report["scanned_list_items"] += 1
            list_dt = parse_ymd(item["list_date"])
            if list_dt is None:
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
            if list_dt > cutoff_dt:
                report["filtered_by_date"] += 1
                continue

            detail_url = item["detail_url"]
            if not detail_url.startswith(f"{BASE}/fldes/ajgs/jyaj/art/"):
                report["skipped_no_attachment"] += 1
                report["skips"].append(
                    {
                        "type": "non_target_detail_domain",
                        "page_no": page_no,
                        "detail_url": detail_url,
                    }
                )
                continue

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
            attachments = parse_attachments(detail_html, detail_url)
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

                dedup_key = f"{detail_url}::{attachment_url}"
                old = existing.get(dedup_key)
                if old and old.get("file_path") and Path(old["file_path"]).exists():
                    report["already_downloaded"] += 1
                    continue
                if old and old.get("file_path") and not Path(old["file_path"]).exists():
                    report["recovered_missing_file"] += 1

                year, month = choose_year_month(item["list_date"], pub_date)
                target_dir = files_root / year / month
                target_dir.mkdir(parents=True, exist_ok=True)

                article_id = article_id_from_detail_url(detail_url)
                case_no = ""
                case_name = item["case_name"] or "untitled"
                save_name = safe_name(f"{article_id}_{case_name}") + ext
                file_path = target_dir / save_name
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
                row = {
                    "dedup_key": dedup_key,
                    "source": "samr.gov.cn-jyaj",
                    "list_page": page_no,
                    "list_date": item["list_date"],
                    "pub_date": pub_date,
                    "detail_url": detail_url,
                    "attachment_url": attachment_url,
                    "id": article_id,
                    "caseNo": case_no,
                    "caseName": case_name,
                    "saved_at": now_iso(),
                    "file_name": file_path.name,
                    "file_ext": ext,
                    "file_size": len(content),
                    "sha256": sha256sum(content),
                    "file_path": str(file_path),
                }
                existing[dedup_key] = row
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
