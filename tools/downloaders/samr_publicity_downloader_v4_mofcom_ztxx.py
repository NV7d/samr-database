#!/usr/bin/env python3
"""Download MOFCOM ztxx notices with body and attachments.

Source:
  https://fldj.mofcom.gov.cn/article/ztxx/

Output layout (independent dataset):
  <out-dir>/mofcom_penalty_notices/
    - files/{YYYY}/{MM}/{article_id}_{safe_title}/body.html
    - files/{YYYY}/{MM}/{article_id}_{safe_title}/body.md
    - files/{YYYY}/{MM}/{article_id}_{safe_title}/{article_id}_{idx}_{safe_original_name}
    - manifest.jsonl
    - manifest.csv
    - run_report.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen


BASE = "https://fldj.mofcom.gov.cn"
LIST_ROOT = f"{BASE}/article/ztxx/"


@dataclass
class Config:
    out_dir: Path
    start_page: int
    end_page: int
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

    preferred = [
        "dedup_key",
        "source",
        "record_type",
        "list_page",
        "list_date",
        "pub_date",
        "detail_url",
        "attachment_url",
        "id",
        "caseName",
        "saved_at",
        "file_name",
        "file_ext",
        "file_size",
        "sha256",
        "file_path",
    ]
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
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_request(url: str, *, cfg: Config, referer: str = "") -> Request:
    req = Request(url=url, method="GET")
    req.add_header("User-Agent", cfg.user_agent)
    req.add_header("Referer", referer or LIST_ROOT)
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
        referer or LIST_ROOT,
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
    # mofcom endpoints can hit legacy TLS/cipher issues; curl fallback is required.
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
    try:
        return str(curl_get(url, cfg=cfg, referer=referer or LIST_ROOT, binary=False))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"GET text failed: {url}: {err or exc}") from exc


def http_get_bytes(url: str, *, cfg: Config, referer: str = "") -> bytes:
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
    try:
        return bytes(curl_get(url, cfg=cfg, referer=referer or LIST_ROOT, binary=True))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"GET bytes failed: {url}: {err or exc}") from exc


def list_url(page_no: int) -> str:
    return LIST_ROOT if page_no == 1 else f"{LIST_ROOT}?{page_no}"


def parse_total_pages(list_html: str) -> int:
    m = re.search(r'totalpage\s*=\s*"(\d+)"', list_html)
    if m:
        return max(1, int(m.group(1)))
    return 1


def parse_list_items(list_html: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    # old static page usually uses this stable li + span format
    pat = re.compile(
        r"<li>\s*<a[^>]*href=\"([^\"]+\.shtml)\"[^>]*>(.*?)</a>\s*<span>([^<]+)</span>\s*</li>",
        re.S | re.I,
    )
    for m in pat.finditer(list_html):
        detail_rel = m.group(1).strip()
        title_html = m.group(2)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title_html)).strip()
        if not title:
            title_attr = re.search(r'title="([^"]+)"', title_html, re.I)
            title = title_attr.group(1).strip() if title_attr else ""
        items.append(
            {
                "detail_url": urljoin(BASE, detail_rel),
                "title": html.unescape(title or "untitled"),
                "list_date": m.group(3).strip(),
            }
        )
    return items


def article_id(detail_url: str) -> str:
    name = Path(urlparse(detail_url).path).name
    m = re.search(r"(\d+)\.shtml$", name)
    if m:
        return m.group(1)
    return hashlib.md5(detail_url.encode("utf-8")).hexdigest()[:16]


def parse_title(detail_html: str) -> str:
    for pat in (
        r'<meta[^>]+name="ArticleTitle"[^>]+content="([^"]+)"',
        r'<h3[^>]*id="artitle"[^>]*>([\s\S]*?)</h3>',
        r"<title>([\s\S]*?)</title>",
    ):
        m = re.search(pat, detail_html, re.I)
        if m:
            text = re.sub(r"<[^>]+>", "", m.group(1))
            text = html.unescape(re.sub(r"\s+", " ", text).strip())
            if text:
                return text
    return "untitled"


def parse_pub_date(detail_html: str) -> str:
    for pat in (
        r'<meta[^>]+name="PubDate"[^>]+content="([^"]+)"',
        r"发布时间[:：]?\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?)",
        r"tm\s*=\s*'([^']+)'",
    ):
        m = re.search(pat, detail_html, re.I)
        if m:
            dt = parse_ymd(m.group(1))
            if dt:
                return dt.strftime("%Y-%m-%d")
    return ""


class ZoomExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._collecting = False
        self._depth = 0
        self._done = False
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self._done:
            return
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if not self._collecting and attrs_dict.get("id", "").lower() == "zoom":
            self._collecting = True
            self._depth = 1
            self.parts.append(self.get_starttag_text())
            return
        if self._collecting:
            self._depth += 1
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        if self._collecting and not self._done:
            self.parts.append(f"</{tag}>")
            self._depth -= 1
            if self._depth <= 0:
                self._done = True
                self._collecting = False

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self._collecting and not self._done:
            self.parts.append(self.get_starttag_text())

    def handle_data(self, data: str) -> None:
        if self._collecting and not self._done:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._collecting and not self._done:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._collecting and not self._done:
            self.parts.append(f"&#{name};")


def extract_zoom_html(detail_html: str) -> str:
    parser = ZoomExtractor()
    parser.feed(detail_html)
    body = "".join(parser.parts).strip()
    if body:
        return body
    # fallback if parser fails on malformed html
    m = re.search(r'(<div[^>]+id="zoom"[^>]*>[\s\S]*?</div>)', detail_html, re.I)
    return m.group(1).strip() if m else ""


class BodyMarkdownParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: List[str] = []
        self.in_li = 0
        self.link_stack: List[Tuple[str, List[str]]] = []

    def _append(self, text: str) -> None:
        if not text:
            return
        self.out.append(text)

    def _block_break(self) -> None:
        if not self.out:
            return
        if not "".join(self.out).endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if t in {"p", "div", "section", "article", "tr"}:
            self._block_break()
        elif t in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._block_break()
            level = int(t[1])
            self._append("#" * level + " ")
        elif t == "br":
            self._append("\n")
        elif t in {"ul", "ol"}:
            self._block_break()
        elif t == "li":
            self.in_li += 1
            self._block_break()
            self._append("- ")
        elif t == "a":
            href = urljoin(self.base_url, attrs_dict.get("href", "").strip())
            self.link_stack.append((href, []))
        elif t in {"td", "th"}:
            self._append(" | ")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "a" and self.link_stack:
            href, parts = self.link_stack.pop()
            text = re.sub(r"\s+", " ", "".join(parts)).strip()
            if text:
                self._append(f"[{text}]({href})")
            elif href:
                self._append(href)
        elif t in {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}:
            self._block_break()
        elif t == "li":
            self.in_li = max(0, self.in_li - 1)
            self._block_break()

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            return
        if self.link_stack:
            self.link_stack[-1][1].append(text)
        else:
            self._append(text)

    def to_markdown(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"\n[ \t]+\n", "\n\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n" if text.strip() else ""


def html_to_markdown(body_html: str, detail_url: str) -> str:
    parser = BodyMarkdownParser(detail_url)
    parser.feed(body_html)
    return parser.to_markdown()


def parse_attachment_links(detail_html: str, detail_url: str, body_html: str) -> List[Dict[str, str]]:
    scope_html = body_html or detail_html
    links: List[Dict[str, str]] = []
    seen = set()
    for m in re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", scope_html, re.I):
        href = (m.group(1) or "").strip()
        if not href:
            continue
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip())
        abs_url = urljoin(detail_url, href)
        lower_href = abs_url.lower()
        if re.search(r"\.s?html?(?:[?#]|$)", lower_href):
            continue
        ext_match = bool(
            re.search(
                r"\.(doc|docx|pdf|zip|rar|7z|xls|xlsx|ppt|pptx|wps|rtf|txt|csv)(?:[?#]|$)",
                lower_href,
            )
        )
        text_hint = bool(re.search(r"附件|下载|word|pdf|doc|docx|点击", text, re.I))
        if not ext_match and not text_hint:
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        links.append({"attachment_url": abs_url, "attachment_text": text})
    return links


def ext_from_name(name: str) -> str:
    return Path(name).suffix.lower()


def guess_file_name(attachment_url: str, attachment_text: str, idx: int) -> str:
    path_name = Path(urlparse(attachment_url).path).name
    if path_name:
        name = safe_name(unquote(path_name))
        if name:
            return name
    text = safe_name(unquote(attachment_text))
    if text:
        return text
    return f"attachment_{idx}"


def save_file_with_manifest(
    *,
    existing: Dict[str, Dict[str, Any]],
    report: Dict[str, Any],
    dedup_key: str,
    record: Dict[str, Any],
    content: bytes,
    target_path: Path,
    dry_run: bool,
) -> None:
    old = existing.get(dedup_key)
    if old and old.get("file_path") and Path(old["file_path"]).exists():
        report["already_downloaded"] += 1
        return
    if old and old.get("file_path") and not Path(old["file_path"]).exists():
        report["recovered_missing_file"] += 1

    if dry_run:
        report["would_download"] += 1
        return

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(content)
    record["saved_at"] = now_iso()
    record["file_name"] = target_path.name
    record["file_ext"] = ext_from_name(target_path.name)
    record["file_size"] = len(content)
    record["sha256"] = sha256sum(content)
    record["file_path"] = str(target_path)
    existing[dedup_key] = record
    report["download_success"] += 1


def parse_args() -> Config:
    p = argparse.ArgumentParser(description="Download MOFCOM ztxx notices (body + attachments).")
    p.add_argument("--out-dir", default="~/Downloads/samr_publicity")
    p.add_argument("--start-page", type=int, default=1)
    p.add_argument("--end-page", type=int, default=0, help="0 means auto to last page")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--retry", type=int, default=3)
    p.add_argument("--sleep-ms", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--user-agent", default="Mozilla/5.0 (compatible; samr-publicity-downloader-v4-mofcom-ztxx/1.0)")
    p.add_argument("--cookie", default=os.environ.get("SAMR_COOKIE", ""))
    a = p.parse_args()
    return Config(
        out_dir=Path(os.path.expanduser(a.out_dir)).resolve(),
        start_page=max(1, a.start_page),
        end_page=max(0, a.end_page),
        timeout=max(1.0, a.timeout),
        retry=max(1, a.retry),
        sleep_ms=max(0, a.sleep_ms),
        dry_run=a.dry_run,
        user_agent=(a.user_agent or "").strip(),
        cookie=(a.cookie or "").strip(),
    )


def main() -> int:
    cfg = parse_args()

    dataset_root = cfg.out_dir / "mofcom_penalty_notices"
    files_root = dataset_root / "files"
    dataset_root.mkdir(parents=True, exist_ok=True)
    files_root.mkdir(parents=True, exist_ok=True)

    manifest_jsonl = dataset_root / "manifest.jsonl"
    manifest_csv = dataset_root / "manifest.csv"
    run_report_path = dataset_root / "run_report.json"

    existing = load_manifest_jsonl(manifest_jsonl)
    report: Dict[str, Any] = {
        "started_at": now_iso(),
        "source": "mofcom_ztxx",
        "dry_run": cfg.dry_run,
        "out_dir": str(cfg.out_dir),
        "dataset_root": str(dataset_root),
        "timeout": cfg.timeout,
        "retry": cfg.retry,
        "sleep_ms": cfg.sleep_ms,
        "start_page": cfg.start_page,
        "end_page_arg": cfg.end_page,
        "existing_manifest_records": len(existing),
        "api_total_pages_snapshot": None,
        "scanned_records": 0,
        "body_candidates": 0,
        "attachment_candidates": 0,
        "already_downloaded": 0,
        "recovered_missing_file": 0,
        "download_success": 0,
        "would_download": 0,
        "download_failed": 0,
        "detail_fetch_failed": 0,
        "skipped_no_body": 0,
        "errors": [],
        "skips": [],
    }

    first_html = http_get_text(list_url(cfg.start_page), cfg=cfg, referer=LIST_ROOT)
    total_pages = parse_total_pages(first_html)
    end_page = total_pages if cfg.end_page == 0 else min(cfg.end_page, total_pages)
    report["api_total_pages_snapshot"] = total_pages
    report["end_page_resolved"] = end_page

    print(f"[info] mofcom_ztxx: total_pages={total_pages}, pages={cfg.start_page}-{end_page}")

    for page_no in range(cfg.start_page, end_page + 1):
        try:
            page_html = first_html if page_no == cfg.start_page else http_get_text(list_url(page_no), cfg=cfg, referer=LIST_ROOT)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"type": "list_page_failed", "page_no": page_no, "error": str(exc)})
            continue

        items = parse_list_items(page_html)
        for item in items:
            report["scanned_records"] += 1
            detail_url = item["detail_url"]
            list_date = item["list_date"]

            try:
                detail_html = http_get_text(detail_url, cfg=cfg, referer=list_url(page_no))
            except Exception as exc:  # noqa: BLE001
                report["detail_fetch_failed"] += 1
                report["errors"].append(
                    {"type": "detail_fetch_failed", "page_no": page_no, "detail_url": detail_url, "error": str(exc)}
                )
                continue

            title = parse_title(detail_html) or item["title"] or "untitled"
            pub_date = parse_pub_date(detail_html)
            y, m = choose_year_month(list_date, pub_date)
            aid = article_id(detail_url)
            article_dir = files_root / y / m / safe_name(f"{aid}_{title}")

            body_html = extract_zoom_html(detail_html)
            if not body_html:
                report["skipped_no_body"] += 1
                report["skips"].append({"type": "no_body", "page_no": page_no, "detail_url": detail_url})
            else:
                report["body_candidates"] += 1
                body_html_bytes = body_html.encode("utf-8")
                body_md = html_to_markdown(body_html, detail_url)
                body_md_bytes = body_md.encode("utf-8")

                body_record_base = {
                    "source": "mofcom_ztxx",
                    "record_type": "article_body",
                    "list_page": page_no,
                    "list_date": list_date,
                    "pub_date": pub_date,
                    "detail_url": detail_url,
                    "attachment_url": "",
                    "id": aid,
                    "caseName": title,
                }

                key_html = f"{detail_url}::body_html"
                rec_html = dict(body_record_base)
                rec_html["dedup_key"] = key_html
                save_file_with_manifest(
                    existing=existing,
                    report=report,
                    dedup_key=key_html,
                    record=rec_html,
                    content=body_html_bytes,
                    target_path=article_dir / "body.html",
                    dry_run=cfg.dry_run,
                )

                key_md = f"{detail_url}::body"
                rec_md = dict(body_record_base)
                rec_md["dedup_key"] = key_md
                save_file_with_manifest(
                    existing=existing,
                    report=report,
                    dedup_key=key_md,
                    record=rec_md,
                    content=body_md_bytes,
                    target_path=article_dir / "body.md",
                    dry_run=cfg.dry_run,
                )

            attachments = parse_attachment_links(detail_html, detail_url, body_html)
            for idx, att in enumerate(attachments, 1):
                report["attachment_candidates"] += 1
                aurl = att["attachment_url"]
                atext = att.get("attachment_text", "")
                dedup_key = f"{detail_url}::{aurl}"
                old = existing.get(dedup_key)
                if old and old.get("file_path") and Path(old["file_path"]).exists():
                    report["already_downloaded"] += 1
                    continue
                if old and old.get("file_path") and not Path(old["file_path"]).exists():
                    report["recovered_missing_file"] += 1

                guess_name = guess_file_name(aurl, atext, idx)
                final_name = safe_name(f"{aid}_{idx:03d}_{guess_name}")
                target_path = article_dir / final_name
                if target_path.exists():
                    n = 1
                    while True:
                        cand = target_path.with_name(f"{target_path.stem}_{n}{target_path.suffix}")
                        if not cand.exists():
                            target_path = cand
                            break
                        n += 1

                if cfg.dry_run:
                    report["would_download"] += 1
                    continue

                try:
                    content = http_get_bytes(aurl, cfg=cfg, referer=detail_url)
                except Exception as exc:  # noqa: BLE001
                    report["download_failed"] += 1
                    report["errors"].append(
                        {
                            "type": "attachment_download_failed",
                            "page_no": page_no,
                            "detail_url": detail_url,
                            "attachment_url": aurl,
                            "error": str(exc),
                        }
                    )
                    continue

                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(content)
                existing[dedup_key] = {
                    "dedup_key": dedup_key,
                    "source": "mofcom_ztxx",
                    "record_type": "attachment",
                    "list_page": page_no,
                    "list_date": list_date,
                    "pub_date": pub_date,
                    "detail_url": detail_url,
                    "attachment_url": aurl,
                    "id": aid,
                    "caseName": title,
                    "saved_at": now_iso(),
                    "file_name": target_path.name,
                    "file_ext": ext_from_name(target_path.name),
                    "file_size": len(content),
                    "sha256": sha256sum(content),
                    "file_path": str(target_path),
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
    run_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "[done] source={source} scanned={scanned} body={body} attachments={att} already={already} success={success} would={would} failed={failed} manifest_total={mt}".format(
            source=report["source"],
            scanned=report["scanned_records"],
            body=report["body_candidates"],
            att=report["attachment_candidates"],
            already=report["already_downloaded"],
            success=report["download_success"],
            would=report["would_download"],
            failed=report["download_failed"],
            mt=report["manifest_total_records"],
        )
    )
    print(f"[done] report: {run_report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
