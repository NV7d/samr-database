#!/usr/bin/env python3
"""Unified downloader for three data sources:

- v1: jyzjz.samr.gov.cn (API + fileId download)
- v2: www.samr.gov.cn (list HTML fragment + detail page attachments)
- v3: fldj.mofcom.gov.cn (static paged list + detail page attachments)
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
from email.header import decode_header
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

ALLOWED_EXTS = {".doc", ".docx"}

# v1
V1_LIST_URL = "https://jyzjz.samr.gov.cn/fld_sb_server/tCasePublish/querySampleCase"
V1_DOWNLOAD_TMPL = "https://jyzjz.samr.gov.cn/fld_sb_server/fldFile/download?attachmentId={attachment_id}"

# v2
V2_BASE = "https://www.samr.gov.cn"
V2_INDEX_URL = f"{V2_BASE}/fldes/ajgs/jyaj/index.html"
V2_LIST_API = f"{V2_BASE}/api-gateway/jpaas-publish-server/front/page/build/unit"
V2_LIST_FIXED = {
    "parseType": "bulidstatic",
    "webId": "29e9522dc89d4e088a953d8cede72f4c",
    "tplSetId": "5c30fb89ae5e48b9aefe3cdf49853830",
    "pageType": "column",
    "tagId": "ajax分页",
    "editType": "null",
    "pageId": "52e2e352f3b84eeaa3e010400866831d",
}

# v3
V3_BASE = "https://fldj.mofcom.gov.cn"
V3_LIST_ROOT = f"{V3_BASE}/article/jyzjzjyajgs/"


@dataclass
class Config:
    source: str
    out_dir: Path
    timeout: float
    retry: int
    sleep_ms: int
    dry_run: bool
    user_agent: str
    cookie: str
    # v1
    page_size: int
    max_pages: int
    # v2/v3
    start_page: int
    end_page: int
    cutoff_date: str


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_name(name: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", (name or "").strip())
    value = re.sub(r"_+", "_", value).strip("._ ")
    return value[:180] if value else "untitled"


def short_name(name: str, max_len: int = 64) -> str:
    value = safe_name(name)
    if len(value) <= max_len:
        return value
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    head = value[: max(1, max_len - 9)].rstrip("._ ")
    return f"{head}_{digest}"


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


def choose_year_month(*candidates: str) -> Tuple[str, str]:
    for text in candidates:
        dt = parse_ymd(text)
        if dt:
            return f"{dt.year:04d}", f"{dt.month:02d}"
    return "unknown", "unknown"


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
    if not rows:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["dedup_key"])
        return

    fields: List[str] = []
    seen = set()
    preferred = [
        "dedup_key",
        "source",
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
        "source_page",
        "source_total_count_snapshot",
        "saved_at",
        "file_name",
        "file_ext",
        "file_size",
        "sha256",
        "file_path",
    ]
    for k in preferred:
        if any(k in r for r in rows):
            seen.add(k)
            fields.append(k)
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                fields.append(k)

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_request(url: str, *, cfg: Config, referer: str = "") -> Request:
    req = Request(url=url, method="GET")
    req.add_header("User-Agent", cfg.user_agent)
    if referer:
        req.add_header("Referer", referer)
    if cfg.cookie:
        req.add_header("Cookie", cfg.cookie)
    return req


def curl_get(url: str, *, cfg: Config, referer: str, binary: bool) -> bytes | str:
    cmd = [
        "curl",
        "-sSL",
        "--max-time",
        str(int(cfg.timeout)),
        "-A",
        cfg.user_agent,
        "-e",
        referer,
    ]
    if cfg.cookie:
        cmd.extend(["-H", f"Cookie: {cfg.cookie}"])
    cmd.append(url)
    err: Optional[Exception] = None
    for attempt in range(1, cfg.retry + 1):
        try:
            if binary:
                out = subprocess.run(cmd, check=True, capture_output=True)
                return out.stdout
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
    raise RuntimeError(f"curl failed: {url}: {err}") from err


def http_get_text(url: str, *, cfg: Config, referer: str = "") -> str:
    # v3 site has legacy TLS; use curl directly for mofcom domains.
    host = urlparse(url).hostname or ""
    if host.endswith("mofcom.gov.cn"):
        return str(curl_get(url, cfg=cfg, referer=referer or V3_LIST_ROOT, binary=False))

    err: Optional[Exception] = None
    for attempt in range(1, cfg.retry + 1):
        try:
            req = build_request(url, cfg=cfg, referer=referer)
            with urlopen(req, timeout=cfg.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            err = exc
            if attempt < cfg.retry:
                time.sleep(min(2 ** (attempt - 1), 5))

    return str(curl_get(url, cfg=cfg, referer=referer or url, binary=False))


def http_get_bytes(url: str, *, cfg: Config, referer: str = "") -> bytes:
    host = urlparse(url).hostname or ""
    if host.endswith("mofcom.gov.cn"):
        return bytes(curl_get(url, cfg=cfg, referer=referer or V3_LIST_ROOT, binary=True))

    err: Optional[Exception] = None
    for attempt in range(1, cfg.retry + 1):
        try:
            req = build_request(url, cfg=cfg, referer=referer)
            with urlopen(req, timeout=cfg.timeout) as resp:
                return resp.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            err = exc
            if attempt < cfg.retry:
                time.sleep(min(2 ** (attempt - 1), 5))

    return bytes(curl_get(url, cfg=cfg, referer=referer or url, binary=True))


def parse_filename_from_cd(header_value: str) -> Optional[str]:
    if not header_value:
        return None
    m = re.search(r"filename\*\s*=\s*([^;]+)", header_value, flags=re.I)
    if m:
        raw = m.group(1).strip().strip('"').strip("'")
        if "''" in raw:
            _, encoded = raw.split("''", 1)
            from urllib.parse import unquote

            return unquote(encoded)
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


def ext_from_url(url: str) -> str:
    return Path(urlparse(url).path).suffix.lower()


# ---------- v1 ----------
def v1_payload(page_no: int, limit: int) -> Dict[str, Any]:
    return {"parameters": [], "requestPage": {"pageNo": page_no, "limit": limit}, "sorts": []}


def v1_post_json(url: str, payload: Dict[str, Any], *, cfg: Config) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    err: Optional[Exception] = None
    for attempt in range(1, cfg.retry + 1):
        try:
            req = Request(url=url, data=body, method="POST")
            req.add_header("User-Agent", cfg.user_agent)
            req.add_header("Content-Type", "application/json")
            if cfg.cookie:
                req.add_header("Cookie", cfg.cookie)
            with urlopen(req, timeout=cfg.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            err = exc
            if attempt < cfg.retry:
                time.sleep(min(2 ** (attempt - 1), 5))
    raise RuntimeError(f"POST failed: {url}: {err}") from err


def run_v1(cfg: Config, existing: Dict[str, Dict[str, Any]], files_root: Path, report: Dict[str, Any]) -> None:
    first = v1_post_json(V1_LIST_URL, v1_payload(1, cfg.page_size), cfg=cfg)
    if first.get("state") != 200 or not first.get("data"):
        raise RuntimeError("v1 first page invalid")
    page_result = first["data"].get("pageResult") or {}
    total_pages = int(page_result.get("totalPages") or 1)
    total_count = int(page_result.get("totalCount") or 0)
    scan_pages = total_pages if cfg.max_pages == 0 else min(total_pages, cfg.max_pages)

    report["api_total_count_snapshot"] = total_count
    report["api_total_pages_snapshot"] = total_pages
    print(f"[info] v1: total_count={total_count}, total_pages={total_pages}, scan_pages={scan_pages}")

    pages = [(1, first)]
    for p in range(2, scan_pages + 1):
        if cfg.sleep_ms:
            time.sleep(cfg.sleep_ms / 1000.0)
        try:
            pages.append((p, v1_post_json(V1_LIST_URL, v1_payload(p, cfg.page_size), cfg=cfg)))
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"type": "list_page_failed", "page_no": p, "error": str(exc)})

    for page_no, resp in pages:
        rows = ((resp.get("data") or {}).get("dataResult") or [])
        for rec in rows:
            report["scanned_records"] += 1
            cid = str(rec.get("id") or "")
            fid = str(rec.get("fileId") or "")
            if not cid or not fid:
                report["download_failed"] += 1
                report["errors"].append({"type": "missing_id_or_fileid", "page_no": page_no, "id": cid, "fileId": fid})
                continue

            key = f"{cid}::{fid}"
            old = existing.get(key)
            if old and old.get("file_path") and Path(old["file_path"]).exists():
                report["already_downloaded"] += 1
                continue
            if old and old.get("file_path") and not Path(old["file_path"]).exists():
                report["recovered_missing_file"] += 1

            url = V1_DOWNLOAD_TMPL.format(attachment_id=quote(fid))
            try:
                req = build_request(url, cfg=cfg)
                with urlopen(req, timeout=cfg.timeout) as r:
                    content = r.read()
                    headers = {k.lower(): v for k, v in r.headers.items()}
            except Exception as exc:  # noqa: BLE001
                report["download_failed"] += 1
                report["errors"].append({"type": "download_error", "page_no": page_no, "id": cid, "fileId": fid, "error": str(exc)})
                continue

            filename = parse_filename_from_cd(headers.get("content-disposition", "")) or ""
            ext = Path(filename).suffix.lower()
            if ext not in ALLOWED_EXTS:
                report["skipped_non_doc"] += 1
                report["skips"].append({"type": "non_doc_ext", "page_no": page_no, "id": cid, "fileId": fid, "detected_ext": ext, "header_filename": filename})
                continue

            y, m = choose_year_month(str(rec.get("receiveTime") or ""), str(rec.get("createTime") or ""))
            target_dir = files_root / y / m
            target_dir.mkdir(parents=True, exist_ok=True)

            case_no = (rec.get("caseNo") or "").strip()
            case_name = rec.get("caseName") or "untitled"
            prefix = case_no or cid
            name = short_name(f"{prefix}_{case_name}", max_len=72) + ext
            fpath = target_dir / name
            if fpath.exists():
                i = 1
                while True:
                    cand = fpath.with_name(f"{fpath.stem}_{i}{fpath.suffix}")
                    if not cand.exists():
                        fpath = cand
                        break
                    i += 1

            if cfg.dry_run:
                report["would_download"] += 1
                continue

            fpath.write_bytes(content)
            existing[key] = {
                "dedup_key": key,
                "source": "v1",
                "id": cid,
                "fileId": fid,
                "caseNo": case_no,
                "caseName": case_name,
                "empName": rec.get("empName") or "",
                "receiveTime": rec.get("receiveTime") or "",
                "createTime": rec.get("createTime") or "",
                "source_page": page_no,
                "source_total_count_snapshot": total_count,
                "saved_at": now_iso(),
                "file_name": fpath.name,
                "file_ext": ext,
                "file_size": len(content),
                "sha256": sha256sum(content),
                "file_path": str(fpath),
            }
            report["download_success"] += 1


# ---------- v2 ----------
def v2_list_url(page_no: int) -> str:
    params = dict(V2_LIST_FIXED)
    params["paramJson"] = json.dumps({"pageNo": page_no, "pageSize": 10}, ensure_ascii=False)
    return f"{V2_LIST_API}?{urlencode(params)}"


def v2_parse_pagination(html: str) -> Tuple[int, int]:
    count_m = re.search(r'\bcount="(\d+)"', html)
    rows_m = re.search(r'\brows="(\d+)"', html)
    count = int(count_m.group(1)) if count_m else 0
    rows = int(rows_m.group(1)) if rows_m else 10
    return count, max(1, math.ceil(count / max(rows, 1)))


def v2_parse_list_items(html: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for li in re.findall(r'<li class="content-3-left-text imgContent01new">(.+?)</li>', html, re.S):
        a_m = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
        d_m = re.search(r'contentRight01time">([^<]+)<', li)
        if not a_m or not d_m:
            continue
        href = a_m.group(1).strip()
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", a_m.group(2))).strip()
        items.append({"detail_url": urljoin(V2_BASE, href), "case_name": title, "list_date": d_m.group(1).strip()})
    return items


def v2_parse_pub_date(html: str) -> str:
    m = re.search(r"发布时间：\s*(\d{4}-\d{2}-\d{2})", html)
    if m:
        return m.group(1)
    m = re.search(r'<meta name="PubDate" content="([^"]+)"', html)
    if m:
        d = parse_ymd(m.group(1))
        return d.strftime("%Y-%m-%d") if d else ""
    return ""


def parse_attachment_links(html: str, base_url: str) -> List[str]:
    urls = [urljoin(base_url, m.group(1).strip()) for m in re.finditer(r'href="([^"]+\.(?:docx?|pdf))"', html, re.I)]
    out, seen = [], set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def v2_article_id(detail_url: str) -> str:
    m = re.search(r"/art/\d+/art_([a-zA-Z0-9]+)\.html", detail_url)
    if m:
        return m.group(1)
    return hashlib.md5(detail_url.encode("utf-8")).hexdigest()[:16]


def run_v2(cfg: Config, existing: Dict[str, Dict[str, Any]], files_root: Path, report: Dict[str, Any]) -> None:
    cutoff = parse_ymd(cfg.cutoff_date)
    if cutoff is None:
        raise ValueError(f"Invalid cutoff date: {cfg.cutoff_date}")

    first = json.loads(http_get_text(v2_list_url(cfg.start_page), cfg=cfg, referer=V2_INDEX_URL))
    first_html = (first.get("data") or {}).get("html", "")
    total_count, total_pages = v2_parse_pagination(first_html)
    end_page = total_pages if cfg.end_page == 0 else min(cfg.end_page, total_pages)

    report["api_total_count_snapshot"] = total_count
    report["api_total_pages_snapshot"] = total_pages
    print(f"[info] v2: total_count={total_count}, total_pages={total_pages}, pages={cfg.start_page}-{end_page}, cutoff={cfg.cutoff_date}")

    for page_no in range(cfg.start_page, end_page + 1):
        if page_no == cfg.start_page:
            html = first_html
        else:
            try:
                obj = json.loads(http_get_text(v2_list_url(page_no), cfg=cfg, referer=V2_INDEX_URL))
                html = (obj.get("data") or {}).get("html", "")
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"type": "list_page_failed", "page_no": page_no, "error": str(exc)})
                continue

        for item in v2_parse_list_items(html):
            report["scanned_records"] += 1
            d = parse_ymd(item["list_date"])
            if d is None or d > cutoff:
                report["filtered_by_date"] += 1
                continue

            detail = item["detail_url"]
            if not detail.startswith(f"{V2_BASE}/fldes/ajgs/jyaj/art/"):
                report["skipped_no_attachment"] += 1
                report["skips"].append({"type": "non_target_detail_domain", "page_no": page_no, "detail_url": detail})
                continue

            try:
                dhtml = http_get_text(detail, cfg=cfg, referer=V2_INDEX_URL)
            except Exception as exc:  # noqa: BLE001
                report["detail_fetch_failed"] += 1
                report["errors"].append({"type": "detail_fetch_failed", "page_no": page_no, "detail_url": detail, "error": str(exc)})
                continue

            pub_date = v2_parse_pub_date(dhtml)
            attachments = parse_attachment_links(dhtml, detail)
            if not attachments:
                report["skipped_no_attachment"] += 1
                report["skips"].append({"type": "no_attachment", "page_no": page_no, "detail_url": detail})
                continue

            for aurl in attachments:
                report["attachment_candidates"] += 1
                ext = ext_from_url(aurl)
                if ext not in ALLOWED_EXTS:
                    report["skipped_non_doc"] += 1
                    report["skips"].append({"type": "non_doc_ext", "page_no": page_no, "detail_url": detail, "attachment_url": aurl, "detected_ext": ext})
                    continue

                key = f"{detail}::{aurl}"
                old = existing.get(key)
                if old and old.get("file_path") and Path(old["file_path"]).exists():
                    report["already_downloaded"] += 1
                    continue
                if old and old.get("file_path") and not Path(old["file_path"]).exists():
                    report["recovered_missing_file"] += 1

                y, m = choose_year_month(item["list_date"], pub_date)
                tdir = files_root / y / m
                tdir.mkdir(parents=True, exist_ok=True)
                aid = v2_article_id(detail)
                case_name = item["case_name"] or "untitled"
                name = short_name(f"{aid}_{case_name}", max_len=72) + ext
                fpath = tdir / name
                if fpath.exists():
                    i = 1
                    while True:
                        cand = fpath.with_name(f"{fpath.stem}_{i}{fpath.suffix}")
                        if not cand.exists():
                            fpath = cand
                            break
                        i += 1

                if cfg.dry_run:
                    report["would_download"] += 1
                    continue

                try:
                    content = http_get_bytes(aurl, cfg=cfg, referer=detail)
                except Exception as exc:  # noqa: BLE001
                    report["download_failed"] += 1
                    report["errors"].append({"type": "download_failed", "page_no": page_no, "detail_url": detail, "attachment_url": aurl, "error": str(exc)})
                    continue

                fpath.write_bytes(content)
                existing[key] = {
                    "dedup_key": key,
                    "source": "v2",
                    "list_page": page_no,
                    "list_date": item["list_date"],
                    "pub_date": pub_date,
                    "detail_url": detail,
                    "attachment_url": aurl,
                    "id": aid,
                    "caseNo": "",
                    "caseName": case_name,
                    "saved_at": now_iso(),
                    "file_name": fpath.name,
                    "file_ext": ext,
                    "file_size": len(content),
                    "sha256": sha256sum(content),
                    "file_path": str(fpath),
                }
                report["download_success"] += 1

            if cfg.sleep_ms:
                time.sleep(cfg.sleep_ms / 1000.0)


# ---------- v3 ----------
def v3_list_url(page_no: int) -> str:
    return V3_LIST_ROOT if page_no == 1 else f"{V3_LIST_ROOT}?{page_no}"


def v3_total_pages(html: str) -> int:
    m = re.search(r'totalpage\s*=\s*"(\d+)"', html)
    return max(1, int(m.group(1))) if m else 1


def v3_parse_list_items(html: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    pat = re.compile(r'<li>\s*<a[^>]*title="([^"]+)"[^>]*href="([^"]+\.shtml)"[^>]*>.*?</a><span>([^<]+)</span>\s*</li>', re.S)
    for m in pat.finditer(html):
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        items.append({
            "case_name": title,
            "detail_url": urljoin(V3_BASE, m.group(2).strip()),
            "list_date": m.group(3).strip(),
        })
    return items


def v3_pub_date(html: str) -> str:
    m = re.search(r"发布时间：\s*<span>([^<]+)</span>", html)
    if m:
        d = parse_ymd(m.group(1))
        return d.strftime("%Y-%m-%d") if d else ""
    return ""


def v3_article_id(detail_url: str) -> str:
    p = Path(urlparse(detail_url).path).name
    m = re.search(r"(\d+)\.shtml$", p)
    if m:
        return m.group(1)
    return hashlib.md5(detail_url.encode("utf-8")).hexdigest()[:16]


def run_v3(cfg: Config, existing: Dict[str, Dict[str, Any]], files_root: Path, report: Dict[str, Any]) -> None:
    cutoff = parse_ymd(cfg.cutoff_date)
    if cutoff is None:
        raise ValueError(f"Invalid cutoff date: {cfg.cutoff_date}")

    first_html = http_get_text(v3_list_url(cfg.start_page), cfg=cfg, referer=V3_LIST_ROOT)
    total_pages = v3_total_pages(first_html)
    end_page = total_pages if cfg.end_page == 0 else min(cfg.end_page, total_pages)

    report["api_total_pages_snapshot"] = total_pages
    report["api_total_count_snapshot"] = total_pages * 15
    print(f"[info] v3: total_pages={total_pages}, pages={cfg.start_page}-{end_page}, cutoff={cfg.cutoff_date}")

    for page_no in range(cfg.start_page, end_page + 1):
        try:
            html = first_html if page_no == cfg.start_page else http_get_text(v3_list_url(page_no), cfg=cfg, referer=V3_LIST_ROOT)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"type": "list_page_failed", "page_no": page_no, "error": str(exc)})
            continue

        for item in v3_parse_list_items(html):
            report["scanned_records"] += 1
            d = parse_ymd(item["list_date"])
            if d is None or d > cutoff:
                report["filtered_by_date"] += 1
                continue

            detail = item["detail_url"]
            try:
                dhtml = http_get_text(detail, cfg=cfg, referer=V3_LIST_ROOT)
            except Exception as exc:  # noqa: BLE001
                report["detail_fetch_failed"] += 1
                report["errors"].append({"type": "detail_fetch_failed", "page_no": page_no, "detail_url": detail, "error": str(exc)})
                continue

            pub_date = v3_pub_date(dhtml)
            attachments = parse_attachment_links(dhtml, detail)
            if not attachments:
                report["skipped_no_attachment"] += 1
                report["skips"].append({"type": "no_attachment", "page_no": page_no, "detail_url": detail})
                continue

            for aurl in attachments:
                report["attachment_candidates"] += 1
                ext = ext_from_url(aurl)
                if ext not in ALLOWED_EXTS:
                    report["skipped_non_doc"] += 1
                    report["skips"].append({"type": "non_doc_ext", "page_no": page_no, "detail_url": detail, "attachment_url": aurl, "detected_ext": ext})
                    continue

                key = f"{detail}::{aurl}"
                old = existing.get(key)
                if old and old.get("file_path") and Path(old["file_path"]).exists():
                    report["already_downloaded"] += 1
                    continue
                if old and old.get("file_path") and not Path(old["file_path"]).exists():
                    report["recovered_missing_file"] += 1

                y, m = choose_year_month(item["list_date"], pub_date)
                tdir = files_root / y / m
                tdir.mkdir(parents=True, exist_ok=True)
                aid = v3_article_id(detail)
                case_name = item["case_name"] or "untitled"
                name = short_name(f"{aid}_{case_name}", max_len=72) + ext
                fpath = tdir / name
                if fpath.exists():
                    i = 1
                    while True:
                        cand = fpath.with_name(f"{fpath.stem}_{i}{fpath.suffix}")
                        if not cand.exists():
                            fpath = cand
                            break
                        i += 1

                if cfg.dry_run:
                    report["would_download"] += 1
                    continue

                try:
                    content = http_get_bytes(aurl, cfg=cfg, referer=detail)
                except Exception as exc:  # noqa: BLE001
                    report["download_failed"] += 1
                    report["errors"].append({"type": "download_failed", "page_no": page_no, "detail_url": detail, "attachment_url": aurl, "error": str(exc)})
                    continue

                fpath.write_bytes(content)
                existing[key] = {
                    "dedup_key": key,
                    "source": "v3",
                    "list_page": page_no,
                    "list_date": item["list_date"],
                    "pub_date": pub_date,
                    "detail_url": detail,
                    "attachment_url": aurl,
                    "id": aid,
                    "caseNo": "",
                    "caseName": case_name,
                    "saved_at": now_iso(),
                    "file_name": fpath.name,
                    "file_ext": ext,
                    "file_size": len(content),
                    "sha256": sha256sum(content),
                    "file_path": str(fpath),
                }
                report["download_success"] += 1

            if cfg.sleep_ms:
                time.sleep(cfg.sleep_ms / 1000.0)


def parse_args() -> Config:
    p = argparse.ArgumentParser(description="Unified downloader for v1/v2/v3 sources.")
    p.add_argument("--source", choices=["v1", "v2", "v3"], required=True)
    p.add_argument("--out-dir", default="~/Downloads/samr_publicity")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--retry", type=int, default=3)
    p.add_argument("--sleep-ms", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--user-agent", default="Mozilla/5.0 (compatible; samr-publicity-downloader-unified/1.0)")
    p.add_argument("--cookie", default=os.environ.get("SAMR_COOKIE", ""))

    p.add_argument("--page-size", type=int, default=20, help="v1 only")
    p.add_argument("--max-pages", type=int, default=0, help="v1 only; 0=all")

    p.add_argument("--start-page", type=int, default=136, help="v2/v3")
    p.add_argument("--end-page", type=int, default=0, help="v2/v3; 0=all")
    p.add_argument("--cutoff-date", default="2022-08-31", help="v2/v3")

    a = p.parse_args()

    # Source-specific defaults
    start_page = a.start_page
    if a.source == "v3" and "--start-page" not in sys.argv:
        start_page = 1

    return Config(
        source=a.source,
        out_dir=Path(os.path.expanduser(a.out_dir)).resolve(),
        timeout=max(1.0, a.timeout),
        retry=max(1, a.retry),
        sleep_ms=max(0, a.sleep_ms),
        dry_run=a.dry_run,
        user_agent=a.user_agent,
        cookie=(a.cookie or "").strip(),
        page_size=max(1, a.page_size),
        max_pages=max(0, a.max_pages),
        start_page=max(1, start_page),
        end_page=max(0, a.end_page),
        cutoff_date=a.cutoff_date,
    )


def main() -> int:
    cfg = parse_args()
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    files_root = cfg.out_dir / "files"
    files_root.mkdir(parents=True, exist_ok=True)

    manifest_jsonl = cfg.out_dir / "manifest.jsonl"
    manifest_csv = cfg.out_dir / "manifest.csv"
    run_report = cfg.out_dir / "run_report.json"

    existing = load_manifest_jsonl(manifest_jsonl)
    report: Dict[str, Any] = {
        "started_at": now_iso(),
        "source": cfg.source,
        "dry_run": cfg.dry_run,
        "out_dir": str(cfg.out_dir),
        "timeout": cfg.timeout,
        "retry": cfg.retry,
        "sleep_ms": cfg.sleep_ms,
        "existing_manifest_records": len(existing),
        "api_total_count_snapshot": None,
        "api_total_pages_snapshot": None,
        "scanned_records": 0,
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

    if cfg.source == "v1":
        report["page_size"] = cfg.page_size
        report["max_pages"] = cfg.max_pages
        run_v1(cfg, existing, files_root, report)
    elif cfg.source == "v2":
        report["start_page"] = cfg.start_page
        report["end_page_arg"] = cfg.end_page
        report["cutoff_date"] = cfg.cutoff_date
        run_v2(cfg, existing, files_root, report)
    else:
        report["start_page"] = cfg.start_page
        report["end_page_arg"] = cfg.end_page
        report["cutoff_date"] = cfg.cutoff_date
        run_v3(cfg, existing, files_root, report)

    merged = sorted(existing.values(), key=lambda x: x.get("dedup_key", ""))
    if not cfg.dry_run:
        write_manifest_jsonl(manifest_jsonl, merged)
        write_manifest_csv(manifest_csv, merged)

    report["ended_at"] = now_iso()
    report["manifest_total_records"] = len(merged)
    report["manifest_jsonl"] = str(manifest_jsonl) if not cfg.dry_run else ""
    report["manifest_csv"] = str(manifest_csv) if not cfg.dry_run else ""
    run_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "[done] source={source} scanned={scanned} filtered={filtered} candidates={cands} already={already} success={success} would_download={would} failed={failed} non_doc={non_doc} manifest_total={mt}".format(
            source=cfg.source,
            scanned=report["scanned_records"],
            filtered=report["filtered_by_date"],
            cands=report["attachment_candidates"],
            already=report["already_downloaded"],
            success=report["download_success"],
            would=report["would_download"],
            failed=report["download_failed"],
            non_doc=report["skipped_non_doc"],
            mt=report["manifest_total_records"],
        )
    )
    print(f"[done] report: {run_report}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
