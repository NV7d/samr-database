#!/usr/bin/env python3
"""Download SAMR enforcement notices from three dynamic columns.

Sources:
  - https://www.samr.gov.cn/fldys/tzgg/xzcf/index.html
  - https://www.samr.gov.cn/fldys/tzgg/ftj/index.html
  - https://www.samr.gov.cn/fldys/tzgg/xzjj/index.html
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
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
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen


BASE = "https://www.samr.gov.cn"
LIST_API = f"{BASE}/api-gateway/jpaas-publish-server/front/page/build/unit"

CATEGORY_CONFIG: Dict[str, Dict[str, str]] = {
    "xzcf": {
        "label": "行政处罚案件",
        "index_url": f"{BASE}/fldys/tzgg/xzcf/index.html",
        "page_id": "6a368386ec874dd0a7648b56325f14df",
    },
    "ftj": {
        "label": "附条件批准/禁止经营者集中案件",
        "index_url": f"{BASE}/fldys/tzgg/ftj/index.html",
        "page_id": "a89139454bab4e3aba2563eaff90fb76",
    },
    "xzjj": {
        "label": "滥用行政权力排除、限制竞争案件",
        "index_url": f"{BASE}/fldys/tzgg/xzjj/index.html",
        "page_id": "343816ad039b4f15a5e41656c0615ad2",
    },
}

LIST_FIXED_PARAMS = {
    "parseType": "bulidstatic",
    "webId": "29e9522dc89d4e088a953d8cede72f4c",
    "tplSetId": "5c30fb89ae5e48b9aefe3cdf49853830",
    "pageType": "column",
    "tagId": "ajax分页",
    "editType": "null",
}

ATTACH_EXTS = {
    ".pdf",
    ".doc",
    ".docx",
    ".zip",
    ".rar",
    ".7z",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".csv",
    ".wps",
    ".ofd",
}


@dataclass
class Config:
    out_dir: Path
    dataset_subdir: str
    categories: List[str]
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
    for item in candidates:
        dt = parse_ymd(item)
        if dt:
            return f"{dt.year:04d}", f"{dt.month:02d}"
    return "unknown", "unknown"


def sha256sum(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def strip_html(text: str) -> str:
    value = re.sub(r"<[^>]+>", "", text or "")
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


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
        "category",
        "category_label",
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
    req = Request(url=normalize_url_for_request(url), method="GET")
    req.add_header("User-Agent", cfg.user_agent)
    req.add_header("Referer", referer or BASE)
    if cfg.cookie:
        req.add_header("Cookie", cfg.cookie)
    return req


def normalize_url_for_request(url: str) -> str:
    """Percent-encode non-ASCII chars in URL path/query for urllib/curl compatibility."""
    parts = urlsplit((url or "").strip())
    path = quote(parts.path, safe="/%:@")
    query = quote(parts.query, safe="=&%:@,+-_.!*'()")
    fragment = quote(parts.fragment, safe="%:@/?&=,+-_.!*'()")
    return urlunsplit((parts.scheme, parts.netloc, path, query, fragment))


def curl_get(url: str, *, cfg: Config, referer: str, binary: bool) -> bytes | str:
    url = normalize_url_for_request(url)
    cmd = [
        "curl",
        "-sSL",
        "--max-time",
        str(int(cfg.timeout)),
        "-A",
        cfg.user_agent,
        "-e",
        referer or BASE,
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
        return str(curl_get(url, cfg=cfg, referer=referer or url, binary=False))
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
        return bytes(curl_get(url, cfg=cfg, referer=referer or url, binary=True))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"GET bytes failed: {url}: {err or exc}") from exc


def list_api_url(category: str, page_no: int) -> str:
    page_id = CATEGORY_CONFIG[category]["page_id"]
    params = dict(LIST_FIXED_PARAMS)
    params["pageId"] = page_id
    params["paramJson"] = json.dumps({"pageNo": page_no, "pageSize": 10}, ensure_ascii=False)
    return f"{LIST_API}?{urlencode(params)}"


def parse_pagination(list_html: str) -> Tuple[int, int]:
    count_m = re.search(r'\bcount="(\d+)"', list_html)
    rows_m = re.search(r'\brows="(\d+)"', list_html)
    count = int(count_m.group(1)) if count_m else 0
    rows = int(rows_m.group(1)) if rows_m else 10
    total_pages = max(1, math.ceil(count / max(rows, 1)))
    return count, total_pages


def parse_list_items(list_html: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    blocks = re.findall(
        r'<li class="content-3-left-text imgContent01new">([\s\S]*?)</li>',
        list_html,
        flags=re.I,
    )
    for block in blocks:
        a_m = re.search(r"(<a[^>]*href=\"([^\"]+)\"[^>]*>[\s\S]*?</a>)", block, flags=re.I)
        if not a_m:
            continue
        a_tag = a_m.group(1)
        href = a_m.group(2).strip()
        title_attr = re.search(r'title="([\s\S]*?)"', a_tag, flags=re.I)
        if title_attr:
            title = strip_html(title_attr.group(1))
        else:
            inner = re.search(r">([\s\S]*?)</a>", a_tag, flags=re.I)
            title = strip_html(inner.group(1) if inner else "")
        date_m = re.search(r'contentRight01time">([^<]+)<', block, flags=re.I)
        list_date = date_m.group(1).strip() if date_m else ""
        items.append(
            {
                "detail_url": urljoin(BASE, href),
                "title": title or "untitled",
                "list_date": list_date,
            }
        )
    return items


def article_id(detail_url: str) -> str:
    m = re.search(r"/art/\d+/art_([a-zA-Z0-9]+)\.html", detail_url)
    if m:
        return m.group(1)
    return hashlib.md5(detail_url.encode("utf-8")).hexdigest()[:16]


def parse_detail_title(detail_html: str) -> str:
    for pat in (
        r'<meta[^>]+name="ArticleTitle"[^>]+content="([^"]+)"',
        r'<h3[^>]*id="artitle"[^>]*>([\s\S]*?)</h3>',
        r"<title>([\s\S]*?)</title>",
    ):
        m = re.search(pat, detail_html, flags=re.I)
        if m:
            text = strip_html(m.group(1))
            if text:
                return text
    return "untitled"


def parse_pub_date(detail_html: str) -> str:
    for pat in (
        r'<meta[^>]+name="PubDate"[^>]+content="([^"]+)"',
        r"发布时间[:：]\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)",
    ):
        m = re.search(pat, detail_html, flags=re.I)
        if m:
            dt = parse_ymd(m.group(1))
            if dt:
                return dt.strftime("%Y-%m-%d")
    return ""


class DivExtractor(HTMLParser):
    def __init__(self, class_name: str) -> None:
        super().__init__(convert_charrefs=False)
        self.class_name = class_name
        self._collecting = False
        self._done = False
        self._depth = 0
        self.parts: List[str] = []

    def _has_target_class(self, attrs: List[Tuple[str, Optional[str]]]) -> bool:
        for k, v in attrs:
            if k.lower() == "class" and v:
                classes = {c.strip() for c in v.split() if c.strip()}
                if self.class_name in classes:
                    return True
        return False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self._done:
            return
        if not self._collecting and tag.lower() == "div" and self._has_target_class(attrs):
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
                self._collecting = False
                self._done = True

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


def extract_body_html(detail_html: str) -> str:
    parser = DivExtractor("zt_xilan_07")
    parser.feed(detail_html)
    text = "".join(parser.parts).strip()
    if text:
        return text
    m = re.search(r'(<div[^>]+class="[^"]*\bzt_xilan_07\b[^"]*"[^>]*>[\s\S]*?</div>)', detail_html, flags=re.I)
    return m.group(1).strip() if m else ""


class BodyMarkdownParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: List[str] = []
        self.link_stack: List[Tuple[str, List[str]]] = []

    def _append(self, text: str) -> None:
        if text:
            self.out.append(text)

    def _line_break(self) -> None:
        if not self.out:
            return
        if not "".join(self.out).endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if t in {"p", "div", "section", "article", "tr"}:
            self._line_break()
        elif t in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._line_break()
            self._append("#" * int(t[1]) + " ")
        elif t == "br":
            self._append("\n")
        elif t == "li":
            self._line_break()
            self._append("- ")
        elif t == "a":
            href = urljoin(self.base_url, attrs_dict.get("href", "").strip())
            self.link_stack.append((href, []))

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "a" and self.link_stack:
            href, parts = self.link_stack.pop()
            text = re.sub(r"\s+", " ", "".join(parts)).strip()
            if text:
                self._append(f"[{text}]({href})")
            elif href:
                self._append(href)
        elif t in {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"}:
            self._line_break()

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data or "")
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


def is_attachment_url(abs_url: str, link_text: str) -> bool:
    parsed = urlparse(abs_url)
    if parsed.scheme not in {"http", "https"}:
        return False
    lower_url = abs_url.lower()
    if re.search(r"\.s?html?(?:[?#]|$)", lower_url):
        return False
    path_ext = Path(parsed.path).suffix.lower()
    if path_ext in ATTACH_EXTS:
        return True
    if "/attach/" in parsed.path.lower():
        return True
    qs = parse_qs(parsed.query)
    if "fileName" in qs or "filename" in qs:
        return True
    if re.search(r"附件|下载|决定书|全文|点击下载", link_text, flags=re.I):
        return True
    return False


def parse_attachment_links(detail_html: str, detail_url: str) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    seen = set()
    for m in re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", detail_html, flags=re.I):
        href = (m.group(1) or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_url = urljoin(detail_url, href)
        text = strip_html(m.group(2))
        if not is_attachment_url(abs_url, text):
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        links.append({"attachment_url": abs_url, "attachment_text": text})
    return links


def file_name_from_attachment(attachment_url: str, attachment_text: str, idx: int) -> str:
    parsed = urlparse(attachment_url)
    qs = parse_qs(parsed.query)
    qname = ""
    for key in ("fileName", "filename"):
        if key in qs and qs[key]:
            qname = qs[key][0]
            break
    qname = unquote(qname)
    qname = strip_html(qname)
    path_name = unquote(Path(parsed.path).name)
    text_name = strip_html(attachment_text)

    base = qname or path_name or text_name or f"attachment_{idx:03d}"
    base = safe_name(base)

    if not Path(base).suffix:
        ext = Path(path_name).suffix.lower()
        if ext and ext in ATTACH_EXTS:
            base = f"{base}{ext}"
    return base


def save_manifest_record(
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
    record["file_ext"] = Path(target_path.name).suffix.lower()
    record["file_size"] = len(content)
    record["sha256"] = sha256sum(content)
    record["file_path"] = str(target_path)
    existing[dedup_key] = record
    report["download_success"] += 1


def run_category(
    *,
    category: str,
    cfg: Config,
    existing: Dict[str, Dict[str, Any]],
    files_root: Path,
    report: Dict[str, Any],
) -> None:
    meta = CATEGORY_CONFIG[category]
    label = meta["label"]
    index_url = meta["index_url"]

    first_obj = json.loads(http_get_text(list_api_url(category, cfg.start_page), cfg=cfg, referer=index_url))
    first_html = (first_obj.get("data") or {}).get("html", "")
    total_count, total_pages = parse_pagination(first_html)
    end_page = total_pages if cfg.end_page == 0 else min(cfg.end_page, total_pages)

    report["categories"][category] = {
        "label": label,
        "total_count_snapshot": total_count,
        "total_pages_snapshot": total_pages,
        "start_page": cfg.start_page,
        "end_page_resolved": end_page,
        "scanned_records": 0,
        "detail_fetch_failed": 0,
        "body_candidates": 0,
        "attachment_candidates": 0,
    }
    print(f"[info] {category}: total_count={total_count}, total_pages={total_pages}, pages={cfg.start_page}-{end_page}")

    for page_no in range(cfg.start_page, end_page + 1):
        try:
            if page_no == cfg.start_page:
                list_html = first_html
            else:
                obj = json.loads(http_get_text(list_api_url(category, page_no), cfg=cfg, referer=index_url))
                list_html = (obj.get("data") or {}).get("html", "")
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"type": "list_page_failed", "category": category, "page_no": page_no, "error": str(exc)})
            continue

        items = parse_list_items(list_html)
        for item in items:
            report["scanned_records"] += 1
            report["categories"][category]["scanned_records"] += 1
            detail_url = item["detail_url"]
            list_date = item["list_date"]

            try:
                detail_html = http_get_text(detail_url, cfg=cfg, referer=index_url)
            except Exception as exc:  # noqa: BLE001
                report["detail_fetch_failed"] += 1
                report["categories"][category]["detail_fetch_failed"] += 1
                report["errors"].append(
                    {
                        "type": "detail_fetch_failed",
                        "category": category,
                        "page_no": page_no,
                        "detail_url": detail_url,
                        "error": str(exc),
                    }
                )
                continue

            title = parse_detail_title(detail_html) or item["title"] or "untitled"
            pub_date = parse_pub_date(detail_html)
            y, m = choose_year_month(list_date, pub_date)
            aid = article_id(detail_url)
            article_dir = files_root / category / y / m / short_name(f"{aid}_{title}", max_len=64)

            base_record = {
                "source": "samr_enforcement_cases",
                "category": category,
                "category_label": label,
                "list_page": page_no,
                "list_date": list_date,
                "pub_date": pub_date,
                "detail_url": detail_url,
                "id": aid,
                "caseName": title,
            }

            body_html = extract_body_html(detail_html)
            if body_html:
                report["body_candidates"] += 1
                report["categories"][category]["body_candidates"] += 1

                html_key = f"{detail_url}::body_html"
                html_record = dict(base_record)
                html_record["record_type"] = "article_body"
                html_record["attachment_url"] = ""
                html_record["dedup_key"] = html_key
                save_manifest_record(
                    existing=existing,
                    report=report,
                    dedup_key=html_key,
                    record=html_record,
                    content=body_html.encode("utf-8"),
                    target_path=article_dir / "body.html",
                    dry_run=cfg.dry_run,
                )

                md_key = f"{detail_url}::body"
                md_record = dict(base_record)
                md_record["record_type"] = "article_body"
                md_record["attachment_url"] = ""
                md_record["dedup_key"] = md_key
                body_md = html_to_markdown(body_html, detail_url)
                save_manifest_record(
                    existing=existing,
                    report=report,
                    dedup_key=md_key,
                    record=md_record,
                    content=body_md.encode("utf-8"),
                    target_path=article_dir / "body.md",
                    dry_run=cfg.dry_run,
                )
            else:
                report["skipped_no_body"] += 1
                report["skips"].append({"type": "no_body", "category": category, "detail_url": detail_url})

            attachments = parse_attachment_links(detail_html, detail_url)
            for idx, att in enumerate(attachments, 1):
                report["attachment_candidates"] += 1
                report["categories"][category]["attachment_candidates"] += 1
                attachment_url = att["attachment_url"]
                attachment_text = att.get("attachment_text", "")

                dedup_key = f"{detail_url}::{attachment_url}"
                old = existing.get(dedup_key)
                if old and old.get("file_path") and Path(old["file_path"]).exists():
                    report["already_downloaded"] += 1
                    continue
                if old and old.get("file_path") and not Path(old["file_path"]).exists():
                    report["recovered_missing_file"] += 1

                raw_name = file_name_from_attachment(attachment_url, attachment_text, idx)
                final_name = short_name(f"{category}_{aid}_{idx:03d}_{raw_name}", max_len=72)
                target_path = article_dir / final_name
                if target_path.exists():
                    n = 1
                    while True:
                        candidate = target_path.with_name(f"{target_path.stem}_{n}{target_path.suffix}")
                        if not candidate.exists():
                            target_path = candidate
                            break
                        n += 1

                if cfg.dry_run:
                    report["would_download"] += 1
                    continue

                try:
                    content = http_get_bytes(attachment_url, cfg=cfg, referer=detail_url)
                except Exception as exc:  # noqa: BLE001
                    report["download_failed"] += 1
                    report["errors"].append(
                        {
                            "type": "attachment_download_failed",
                            "category": category,
                            "detail_url": detail_url,
                            "attachment_url": attachment_url,
                            "error": str(exc),
                        }
                    )
                    continue

                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(content)
                existing[dedup_key] = {
                    "dedup_key": dedup_key,
                    "source": "samr_enforcement_cases",
                    "category": category,
                    "category_label": label,
                    "record_type": "attachment",
                    "list_page": page_no,
                    "list_date": list_date,
                    "pub_date": pub_date,
                    "detail_url": detail_url,
                    "attachment_url": attachment_url,
                    "id": aid,
                    "caseName": title,
                    "saved_at": now_iso(),
                    "file_name": target_path.name,
                    "file_ext": Path(target_path.name).suffix.lower(),
                    "file_size": len(content),
                    "sha256": sha256sum(content),
                    "file_path": str(target_path),
                }
                report["download_success"] += 1

            if cfg.sleep_ms:
                time.sleep(cfg.sleep_ms / 1000.0)


def parse_categories(arg_value: str) -> List[str]:
    raw = (arg_value or "all").strip().lower()
    if raw == "all":
        return list(CATEGORY_CONFIG.keys())
    values = [x.strip().lower() for x in raw.split(",") if x.strip()]
    valid = []
    for c in values:
        if c not in CATEGORY_CONFIG:
            raise ValueError(f"Unsupported category: {c}")
        if c not in valid:
            valid.append(c)
    if not valid:
        raise ValueError("No valid categories provided")
    return valid


def parse_args() -> Config:
    p = argparse.ArgumentParser(description="Download SAMR enforcement notices from xzcf/ftj/xzjj.")
    p.add_argument("--out-dir", default="~/Downloads/samr_publicity")
    p.add_argument("--dataset-subdir", default="samr_enforcement_cases")
    p.add_argument("--categories", default="all", help="all or comma-separated: xzcf,ftj,xzjj")
    p.add_argument("--start-page", type=int, default=1)
    p.add_argument("--end-page", type=int, default=0, help="0 means auto to last page")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--retry", type=int, default=3)
    p.add_argument("--sleep-ms", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--user-agent", default="Mozilla/5.0 (compatible; samr-publicity-downloader-v5-samr-enforcement/1.0)")
    p.add_argument("--cookie", default=os.environ.get("SAMR_COOKIE", ""))
    args = p.parse_args()

    categories = parse_categories(args.categories)
    return Config(
        out_dir=Path(os.path.expanduser(args.out_dir)).resolve(),
        dataset_subdir=safe_name(args.dataset_subdir) or "samr_enforcement_cases",
        categories=categories,
        start_page=max(1, args.start_page),
        end_page=max(0, args.end_page),
        timeout=max(1.0, args.timeout),
        retry=max(1, args.retry),
        sleep_ms=max(0, args.sleep_ms),
        dry_run=args.dry_run,
        user_agent=(args.user_agent or "").strip(),
        cookie=(args.cookie or "").strip(),
    )


def main() -> int:
    cfg = parse_args()
    dataset_root = cfg.out_dir / cfg.dataset_subdir
    files_root = dataset_root / "files"
    dataset_root.mkdir(parents=True, exist_ok=True)
    files_root.mkdir(parents=True, exist_ok=True)

    manifest_jsonl = dataset_root / "manifest.jsonl"
    manifest_csv = dataset_root / "manifest.csv"
    run_report = dataset_root / "run_report.json"

    existing = load_manifest_jsonl(manifest_jsonl)
    report: Dict[str, Any] = {
        "started_at": now_iso(),
        "source": "samr_enforcement_cases",
        "categories_requested": cfg.categories,
        "dry_run": cfg.dry_run,
        "out_dir": str(cfg.out_dir),
        "dataset_root": str(dataset_root),
        "timeout": cfg.timeout,
        "retry": cfg.retry,
        "sleep_ms": cfg.sleep_ms,
        "start_page": cfg.start_page,
        "end_page_arg": cfg.end_page,
        "existing_manifest_records": len(existing),
        "scanned_records": 0,
        "detail_fetch_failed": 0,
        "body_candidates": 0,
        "attachment_candidates": 0,
        "already_downloaded": 0,
        "recovered_missing_file": 0,
        "download_success": 0,
        "would_download": 0,
        "download_failed": 0,
        "skipped_no_body": 0,
        "errors": [],
        "skips": [],
        "categories": {},
    }

    for category in cfg.categories:
        run_category(category=category, cfg=cfg, existing=existing, files_root=files_root, report=report)

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
        "[done] categories={cats} scanned={scanned} detail_failed={detail_failed} body={body} attachments={atts} already={already} success={success} would={would} failed={failed} manifest_total={mt}".format(
            cats=",".join(cfg.categories),
            scanned=report["scanned_records"],
            detail_failed=report["detail_fetch_failed"],
            body=report["body_candidates"],
            atts=report["attachment_candidates"],
            already=report["already_downloaded"],
            success=report["download_success"],
            would=report["would_download"],
            failed=report["download_failed"],
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
