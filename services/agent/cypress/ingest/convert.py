"""Turn fetched HTML and PDFs into Markdown. Scanned PDF pages are OCR'd with Gemini."""

import logging
import re
from urllib.parse import unquote, urlparse
from collections.abc import Callable
from dataclasses import dataclass, field

import pymupdf
import pymupdf4llm
import trafilatura

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 40  # a page with less extractable text than this is treated as scanned

@dataclass
class Converted:
    title: str
    markdown: str
    meta: dict = field(default_factory=dict)
    needs_review: bool = False


def html_to_md(html: bytes, url: str) -> Converted | None:
    text = html.decode("utf-8", "replace")
    md = trafilatura.extract(
        text, url=url, output_format="markdown", include_links=True, include_tables=True,
        include_formatting=True, favor_recall=True,
    )
    if not md or len(md.strip()) < 30:
        return None
    meta = trafilatura.extract_metadata(text, default_url=url)
    title = _html_title(meta.title if meta and meta.title else "", meta.sitename if meta else "", md, url)
    out_meta = {"date": meta.date} if meta and meta.date else {}
    return Converted(title=title.strip(), markdown=md.strip(), meta=out_meta)


SITE_NAMES = {"lake hills ca", "lake hills community association", "home"}
SCANNER_TITLE = re.compile(r"^(KMBT|SCAN|IMG|DOC|Microsoft Word)[\s_-]", re.I)


def _from_url(url: str) -> str:
    """'…/LHPA%20_Lap%20Swim%20Waiver.pdf?ver=1' -> 'LHPA Lap Swim Waiver'; '/park-hours-and-rules' -> 'Park Hours And Rules'."""
    last = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    last = re.sub(r"\.(pdf|html?)$", "", last, flags=re.I)
    words = re.sub(r"[_\-\s]+", " ", last).strip()
    return words if any(c.isupper() for c in words) else words.title()


def _html_title(title: str, site: str, md: str, url: str) -> str:
    """Site builders (GoDaddy) often give every page the site name as its title."""
    t = title.strip()
    if not t or t.lower() in SITE_NAMES or (site and t.lower() == site.lower()):
        path = urlparse(url).path.strip("/")
        if not path:
            return t or "Home"
        h = re.search(r"^#{1,3}\s+(.+)$", md, re.M)
        return h.group(1).strip(" *#") if h else _from_url(url)
    return t


def _pdf_title(meta_title: str, url: str) -> str:
    t = meta_title.strip()
    if not t or "%20" in t or re.search(r"\.(pdf|docx?)\b", t, re.I) or SCANNER_TITLE.match(t):
        return _from_url(url)
    return t


OcrFn = Callable[[bytes], str]  # PNG bytes -> Markdown


def pdf_to_md(data: bytes, url: str, ocr_mode: str, ocr: OcrFn | None) -> Converted:
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        pages = pymupdf4llm.to_markdown(doc, page_chunks=True, show_progress=False, use_ocr=False)  # we OCR with Gemini
        parts, ocr_pages = [], []
        for i, page in enumerate(pages):
            text = (page.get("text") or "").strip()
            scanned = len(text) < MIN_TEXT_CHARS
            if ocr and (ocr_mode == "always" or (ocr_mode == "auto" and scanned)):
                png = doc[i].get_pixmap(dpi=200).tobytes("png")
                try:
                    text = ocr(png).strip()
                    ocr_pages.append(i + 1)
                except Exception:
                    log.exception("OCR failed on %s page %d", url, i + 1)
            parts.append(f"<!-- page {i + 1} -->\n{text}")
        title = _pdf_title((doc.metadata or {}).get("title") or "", url)
        markdown = "\n\n".join(parts).strip()
        meta = {"pages": len(pages), "ocr_pages": ocr_pages}
        # Mostly-OCR'd documents go to the admin review queue before anyone relies on them.
        needs_review = bool(pages) and len(ocr_pages) / len(pages) > 0.5
        return Converted(title=title.strip(), markdown=markdown, meta=meta, needs_review=needs_review)
    finally:
        doc.close()


def gemini_ocr(project: str | None = None) -> OcrFn:
    """OCR with the model assigned to the "ocr" node in the console; every page is metered."""
    from .. import models

    def run(png: bytes) -> str:
        from ..agent import prompts
        return models.ocr_page(png, prompts.system("ocr"))

    return run
