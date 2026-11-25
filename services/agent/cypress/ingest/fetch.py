"""Polite HTTP fetching and a bounded, same-site crawler."""

import fnmatch
import logging
import re
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import settings

log = logging.getLogger(__name__)

USER_AGENT = f"CypressBot/0.1 (Lake Hills Community Association; +https://lakehillsca.org; {settings.support_email})"
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 40 * 1024 * 1024
TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|_hs)")
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|css|js|zip|mp4|mov|mp3|docx?|xlsx?|pptx?)$", re.I)
HREF = re.compile(r"""<a\s[^>]*?href\s*=\s*["']([^"'#]+)""", re.I)


@dataclass
class Page:
    url: str           # final URL after redirects, normalized
    mime: str          # "text/html" or "application/pdf"
    body: bytes        # empty when not_modified
    depth: int
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False           # server said 304: same as what we stored
    links: list[str] | None = None       # outgoing links (HTML only)


@dataclass
class Known:
    """What we stored for a URL last time; lets fetch() ask the server 'changed since?'."""
    mime: str
    etag: str | None = None
    last_modified: str | None = None
    links: list[str] | None = None


KnownFn = Callable[[str], "Known | None"]


def client() -> httpx.Client:
    return httpx.Client(
        headers={"user-agent": USER_AGENT}, follow_redirects=True, timeout=httpx.Timeout(20, connect=10)
    )


def normalize(url: str) -> str:
    """Drop fragments and tracking params, lowercase the host, so the same page isn't fetched twice."""
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not TRACKING.match(k)])
    path = p.path or "/"
    return urlunparse((p.scheme.lower(), (p.hostname or "").lower() + (f":{p.port}" if p.port else ""),
                       path, "", query, ""))


def kind_of(content_type: str, url: str) -> str | None:
    ct = content_type.split(";")[0].strip().lower()
    if ct == "application/pdf" or (ct in ("application/octet-stream", "") and url.lower().endswith(".pdf")):
        return "application/pdf"
    if ct in ("text/html", "application/xhtml+xml"):
        return "text/html"
    return None


def fetch(http: httpx.Client, url: str, known: Known | None = None, depth: int = 0) -> Page | None:
    """GET a URL with a size cap; conditional when we have validators from last time.
    Returns a Page (not_modified=True on 304), or None if it's not HTML/PDF."""
    headers = {}
    if known and known.etag:
        headers["if-none-match"] = known.etag
    if known and known.last_modified:
        headers["if-modified-since"] = known.last_modified
    with http.stream("GET", url, headers=headers) as r:
        final = normalize(str(r.url))
        if r.status_code == 304 and known:
            return Page(final, known.mime, b"", depth, known.etag, known.last_modified, True, known.links)
        r.raise_for_status()
        etag, modified = r.headers.get("etag"), r.headers.get("last-modified")
        mime = kind_of(r.headers.get("content-type", ""), final)
        if mime is None:
            return None
        cap = MAX_PDF_BYTES if mime == "application/pdf" else MAX_HTML_BYTES
        if int(r.headers.get("content-length") or 0) > cap:
            raise ValueError(f"{final} is larger than {cap // 1024 // 1024} MB")
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > cap:
                raise ValueError(f"{final} is larger than {cap // 1024 // 1024} MB")
        body = bytes(buf)
        page_links = links(body.decode("utf-8", "replace"), final) if mime == "text/html" else None
        return Page(final, mime, body, depth, etag, modified, False, page_links)


def _matches(path: str, patterns: list[str]) -> bool:
    return any(path.startswith(p) if not any(c in p for c in "*?[") else fnmatch.fnmatch(path, p) for p in patterns)


def links(html: str, base: str) -> list[str]:
    return [urljoin(base, h.strip()) for h in HREF.findall(html)]


class Robots:
    def __init__(self, http: httpx.Client):
        self.http, self.cache = http, {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.cache:
            rp = RobotFileParser()
            try:
                r = self.http.get(origin + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self.cache[origin] = rp
        return self.cache[origin].can_fetch(USER_AGENT, url)


def crawl(http: httpx.Client, seed: str, cfg: dict, on_error: Callable[[str, Exception], None],
          should_stop: Callable[[], bool] = lambda: False, known: KnownFn = lambda url: None) -> Iterator[Page]:
    """Breadth-first crawl from `seed`, bounded by depth, page count and allowed domains."""
    allow = {d.lower().removeprefix("www.") for d in cfg.get("allow_domains") or [urlparse(seed).hostname]}
    include, exclude = cfg.get("include") or [], cfg.get("exclude") or []
    max_depth, max_pages = cfg.get("max_depth", 2), cfg.get("max_pages", 200)
    follow_pdfs, robots = cfg.get("follow_pdfs", True), Robots(http) if cfg.get("respect_robots", True) else None

    def in_scope(url: str) -> bool:
        p = urlparse(url)
        host = (p.hostname or "").removeprefix("www.")
        if p.scheme not in ("http", "https") or host not in allow or SKIP_EXT.search(p.path):
            return False
        if include and not _matches(p.path, include):
            return False
        return not _matches(p.path, exclude)

    seed = normalize(seed)
    queue, seen, yielded = deque([(seed, 0)]), {seed}, 0
    while queue and yielded < max_pages and not should_stop():
        url, depth = queue.popleft()
        if robots and not robots.allowed(url):
            log.info("robots.txt disallows %s", url)
            continue
        try:
            page = fetch(http, url, known(url), depth)
        except Exception as e:  # one bad page shouldn't sink the crawl
            on_error(url, e)
            continue
        if page is None:
            continue
        seen.add(page.url)
        if page.mime == "application/pdf" and not follow_pdfs and url != seed:
            continue
        yielded += 1
        yield page
        if page.mime == "text/html" and depth < max_depth:
            for link in page.links or []:
                n = normalize(link)
                is_pdf = urlparse(n).path.lower().endswith(".pdf")
                if n not in seen and (in_scope(n) or (is_pdf and follow_pdfs and _pdf_host_ok(n, allow))):
                    seen.add(n)
                    queue.append((n, depth + 1))


def _pdf_host_ok(url: str, allow: set[str]) -> bool:
    """Linked PDFs are often on a CDN subdomain (GoDaddy uses img1.wsimg.com); allow those hosts."""
    host = (urlparse(url).hostname or "").removeprefix("www.")
    return host in allow or host.endswith(".wsimg.com") or any(host.endswith("." + d) for d in allow)
