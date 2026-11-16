#!/usr/bin/env python3
"""Serve one PHD Comics strip per day as a page for a dashboard iFrame.

The service selects one comic from the phdcomics.com archive for each
day. All clients get the same comic on the same day. The comic changes
at midnight in the time zone that TZ sets.

The service gets the comic page and its images one time each day. It
keeps the images in memory and sends them from its own address. Thus
the browser does not connect to phdcomics.com.

Endpoints:
    /            HTML page for the Homarr iFrame widget.
    /api/today   JSON data for the comic of the day.
    /image/N/I   Image number I of comic N, from the cache.
    /healthz     Health check. Returns "ok".

Query parameters for "/":
    theme    "auto" (default), "light" or "dark". Set the same value
             as the Homarr board. Then the iFrame stays transparent.
    fit      "contain" (default) shows the full strip in the widget.
             "width" fills the widget width. The page can then scroll.
    caption  "1" (default) shows the title and the credit line.
             "0" hides them.
    id       Show this comic ID. Use this to test the layout.

Environment variables:
    TZ              Time zone for the day change. Default: UTC.
    PHD_HOST        Address to listen on. Default: 0.0.0.0.
    PHD_PORT        TCP port. Default: 8080.
    PHD_SALT        Text that sets the shuffle order. Change it to get
                    a different sequence.
    PHD_LATEST_ID   Highest comic ID. Default: read it from the site.
    PHD_SKIP_IDS    Comma-separated comic IDs to skip.
    PHD_BASE_URL    Site root. Default: https://phdcomics.com.
    PHD_LOG_LEVEL   Log level. Default: INFO.

Comics: "Piled Higher and Deeper" by Jorge Cham, www.phdcomics.com.
"""

from __future__ import annotations

import datetime as dt
import html
import http.client
import json
import logging
import os
import random
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger("phd_daily")

BASE_URL = os.environ.get("PHD_BASE_URL", "https://phdcomics.com").rstrip("/")
HOST = os.environ.get("PHD_HOST", "0.0.0.0")
PORT = int(os.environ.get("PHD_PORT", "8080"))
SALT = os.environ.get("PHD_SALT", "phd")

# Comic 2051 is the newest comic in November 2026. The strip has stopped.
# The service uses this value when the site does not give a value.
FALLBACK_LATEST_ID = 2051

USER_AGENT = "phd-daily/1.0 (personal dashboard widget)"
TIMEOUT_S = 15
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ATTEMPTS = 12
CACHE_SIZE = 16
LATEST_ID_TTL = dt.timedelta(days=7)
RETRY_DELAY = dt.timedelta(minutes=5)
STALE_REFRESH_S = 300

# These pages have a video, not a comic.
DEFAULT_SKIP_IDS = frozenset({1669, 1880})
# Announcement pages use this image. It is not a comic.
PLACEHOLDER_MARKERS = ("phd083123s",)

COMIC_IMG_KEY_RE = re.compile(r"comic\d*")
IMAGE_EXT_RE = re.compile(r"\.(?:gif|png|jpe?g|webp)$", re.IGNORECASE)
NAME_DATE_RE = re.compile(r"phd(\d{2})(\d{2})(\d{2})s?\.[a-z]+$", re.I)
COMIC_ID_RE = re.compile(r"comicid=(\d+)")
TITLE_PREFIX_RE = re.compile(r"^\s*PHD\s+Comics?\s*:\s*", re.IGNORECASE)
IMAGE_ROUTE_RE = re.compile(r"/image/(\d+)/(\d+)")

THEMES = ("auto", "light", "dark")
FITS = ("contain", "width")


class SiteError(Exception):
    """The site does not answer. Do not try more pages now."""


class PageError(Exception):
    """One page or image is not available. Try a different comic."""


@dataclass
class Comic:
    """One comic, with its images in memory."""

    comic_id: int
    title: str
    page_url: str
    image_urls: list[str]
    published: dt.date | None
    images: list[tuple[bytes, str]] = field(repr=False)


class ComicPageParser(HTMLParser):
    """Collect the comic images, the meta tags and the title of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.comic_srcs: list[str] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr = {name: (value or "").strip() for name, value in attrs}
        if tag == "img":
            # The page has the strip two times: id=comic and id=comic2.
            key = (attr.get("id") or attr.get("name") or "").lower()
            if COMIC_IMG_KEY_RE.fullmatch(key) and attr.get("src"):
                self.comic_srcs.append(attr["src"])
        elif tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").lower()
            if key and "content" in attr:
                self.meta.setdefault(key, attr["content"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def http_get(url: str, limit: int) -> tuple[bytes, str]:
    """Get a URL. Return the body and the content type."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = response.read(limit + 1)
            content_type = response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            raise SiteError(f"HTTP {exc.code} for {url}") from exc
        raise PageError(f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, http.client.HTTPException,
            OSError) as exc:
        raise SiteError(f"Cannot get {url}: {exc}") from exc
    if len(body) > limit:
        raise PageError(f"Response is larger than {limit} bytes: {url}")
    return body, content_type


def url_variants(url: str) -> list[str]:
    """Return the HTTPS form of a phdcomics.com URL first, then the URL."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "http" and host.endswith("phdcomics.com"):
        return [parts._replace(scheme="https").geturl(), url]
    return [url]


def fetch_image(url: str) -> tuple[bytes, str]:
    """Get one comic image. Use HTTPS if the site supports it."""
    last_error: Exception | None = None
    for candidate in url_variants(url):
        try:
            body, content_type = http_get(candidate, MAX_IMAGE_BYTES)
        except (SiteError, PageError) as exc:
            last_error = exc
            continue
        if not content_type.startswith("image/"):
            raise PageError(f"Not an image ({content_type}): {candidate}")
        return body, content_type
    assert last_error is not None
    raise last_error


def is_comic_image(url: str) -> bool:
    """Return True if the URL looks like a comic image."""
    path = urllib.parse.urlsplit(url).path
    if not IMAGE_EXT_RE.search(path):
        return False
    return not any(marker in path for marker in PLACEHOLDER_MARKERS)


def date_from_image(url: str) -> dt.date | None:
    """Read the date from a file name such as phd101212s.gif."""
    match = NAME_DATE_RE.search(urllib.parse.urlsplit(url).path)
    if not match:
        return None
    month, day, year = (int(part) for part in match.groups())
    year += 1900 if year >= 97 else 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def page_title(parser: ComicPageParser, comic_id: int) -> str:
    """Return the comic title from the meta tags or the title tag."""
    for key in ("twitter:title", "og:title"):
        if parser.meta.get(key):
            return parser.meta[key]
    for raw in (parser.meta.get("title", ""), parser.title):
        cleaned = TITLE_PREFIX_RE.sub("", raw).strip()
        if cleaned:
            return cleaned
    return f"Comic {comic_id}"


def parse_comic_page(text: str) -> ComicPageParser:
    """Parse the HTML of one comic page."""
    # Some pages close comments with "--!>". Change it to "-->".
    parser = ComicPageParser()
    parser.feed(text.replace("--!>", "-->"))
    parser.close()
    return parser


def comic_image_urls(parser: ComicPageParser, page_url: str) -> list[str]:
    """Return the comic image URLs of a page, without duplicates."""
    urls: list[str] = []
    for src in parser.comic_srcs:
        url = urllib.parse.urljoin(page_url, src)
        if is_comic_image(url) and url not in urls:
            urls.append(url)
    if not urls:
        # Some pages have no comic image tag. Use the share image.
        og_image = parser.meta.get("og:image", "")
        url = urllib.parse.urljoin(page_url, og_image) if og_image else ""
        if url and is_comic_image(url):
            urls.append(url)
    return urls


def fetch_comic(comic_id: int) -> Comic:
    """Get one comic page and its images.

    Raise PageError if the page has no comic. Raise SiteError if the
    site does not answer.
    """
    page_url = f"{BASE_URL}/comics/archive.php?comicid={comic_id}"
    body, _ = http_get(page_url, MAX_PAGE_BYTES)
    parser = parse_comic_page(body.decode("utf-8", "replace"))
    image_urls = comic_image_urls(parser, page_url)
    if not image_urls:
        raise PageError(f"Comic {comic_id} has no comic image")
    images = [fetch_image(url) for url in image_urls]
    return Comic(
        comic_id=comic_id,
        title=page_title(parser, comic_id),
        page_url=page_url,
        image_urls=image_urls,
        published=date_from_image(image_urls[0]),
        images=images,
    )


def candidate_ids(day: dt.date, latest_id: int, salt: str = SALT,
                  attempts: int = MAX_ATTEMPTS):
    """Yield comic IDs for one day. The first ID is the main choice.

    The main choice comes from one fixed shuffle of all IDs. Each day
    moves one step in that shuffle. Thus no comic repeats until all
    comics have had one day. The other IDs are backups for pages that
    have no comic.
    """
    order = list(range(1, latest_id + 1))
    random.Random(f"{salt}:{latest_id}").shuffle(order)
    yield order[day.toordinal() % latest_id]
    backup = random.Random(f"{salt}:{day.isoformat()}")
    for _ in range(attempts - 1):
        yield backup.randint(1, latest_id)


def utc_now() -> dt.datetime:
    """Return the current time in UTC."""
    return dt.datetime.now(dt.timezone.utc)


class ComicService:
    """Select, get and keep the comics."""

    def __init__(self, tz: ZoneInfo, skip_ids: frozenset[int],
                 latest_override: int | None) -> None:
        self._tz = tz
        self._skip_ids = skip_ids
        self._latest_override = latest_override
        self._lock = threading.Lock()
        self._comics: dict[int, Comic] = {}
        self._daily: tuple[dt.date, int] | None = None
        self._latest: tuple[int, dt.datetime] | None = None
        self._retry_at: dt.datetime | None = None
        self.last_good: Comic | None = None

    def today(self) -> dt.date:
        """Return the current date in the configured time zone."""
        return dt.datetime.now(self._tz).date()

    def seconds_to_next_day(self) -> int:
        """Return the seconds until one minute after the next midnight."""
        now = dt.datetime.now(self._tz)
        midnight = dt.datetime.combine(
            now.date() + dt.timedelta(days=1), dt.time(), self._tz)
        # Use timestamps. Then a change to or from summer time is correct.
        return max(60, int(midnight.timestamp() - now.timestamp()) + 60)

    def image(self, comic_id: int, index: int) -> tuple[bytes, str] | None:
        """Return one image from the cache, or None."""
        comic = self._comics.get(comic_id)
        if comic is None or index >= len(comic.images):
            return None
        return comic.images[index]

    def comic_for_day(self, day: dt.date) -> Comic:
        """Return the comic for one day. Get it from the site if necessary."""
        with self._lock:
            if self._daily and self._daily[0] == day:
                comic = self._comics.get(self._daily[1])
                if comic is not None:
                    return comic
            if self._retry_at and utc_now() < self._retry_at:
                raise SiteError("The site did not answer. Retry later.")
            latest_id = self._latest_id()
            try:
                comic = self._first_valid(day, latest_id)
            except SiteError:
                self._retry_at = utc_now() + RETRY_DELAY
                raise
            self._retry_at = None
            self._daily = (day, comic.comic_id)
            self.last_good = comic
            LOG.info("Comic for %s: %d %r", day, comic.comic_id, comic.title)
            return comic

    def comic_by_id(self, comic_id: int) -> Comic:
        """Return one comic by its ID."""
        with self._lock:
            return self._get(comic_id)

    def _first_valid(self, day: dt.date, latest_id: int) -> Comic:
        for comic_id in candidate_ids(day, latest_id):
            if comic_id in self._skip_ids:
                continue
            try:
                return self._get(comic_id)
            except PageError as exc:
                LOG.info("Skip comic %d: %s", comic_id, exc)
        raise SiteError(f"No valid comic in {MAX_ATTEMPTS} attempts")

    def _get(self, comic_id: int) -> Comic:
        comic = self._comics.get(comic_id)
        if comic is not None:
            return comic
        comic = fetch_comic(comic_id)
        self._comics[comic_id] = comic
        self._trim_cache()
        return comic

    def _trim_cache(self) -> None:
        keep = {self._daily[1] if self._daily else None,
                self.last_good.comic_id if self.last_good else None}
        for comic_id in list(self._comics):
            if len(self._comics) <= CACHE_SIZE:
                break
            if comic_id not in keep:
                del self._comics[comic_id]

    def _latest_id(self) -> int:
        if self._latest_override:
            return self._latest_override
        now = utc_now()
        if self._latest and now - self._latest[1] < LATEST_ID_TTL:
            return self._latest[0]
        try:
            body, _ = http_get(f"{BASE_URL}/comics.php", MAX_PAGE_BYTES)
            ids = [int(value) for value in
                   COMIC_ID_RE.findall(body.decode("utf-8", "replace"))]
            latest_id = max(ids) if ids else FALLBACK_LATEST_ID
            checked_at = now
        except (SiteError, PageError) as exc:
            LOG.warning("Cannot read the newest comic ID: %s", exc)
            latest_id = self._latest[0] if self._latest else (
                FALLBACK_LATEST_ID)
            # Try again in one hour, not in one week.
            checked_at = now - LATEST_ID_TTL + dt.timedelta(hours=1)
        self._latest = (latest_id, checked_at)
        LOG.info("Newest comic ID: %d", latest_id)
        return latest_id


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="{theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>PHD Comics: {title}</title>
<style>
:root {{ color-scheme: light dark; }}
html[data-theme="light"] {{ color-scheme: light; }}
html[data-theme="dark"] {{ color-scheme: dark; }}
html, body {{ margin: 0; background: transparent; }}
body {{
  display: flex; flex-direction: column; gap: 4px;
  font: 12px/1.3 system-ui, sans-serif; color: CanvasText;
}}
body.fit-contain {{ height: 100vh; overflow: hidden; }}
.strip {{
  flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 4px;
}}
.strip img {{ display: block; max-width: 100%; }}
.fit-contain .strip img {{
  flex: 1 1 0; min-height: 0; width: 100%; object-fit: contain;
}}
.fit-width .strip img {{ width: 100%; height: auto; }}
footer {{
  flex: none; display: flex; justify-content: space-between; gap: 8px;
  padding: 0 2px; opacity: 0.7; white-space: nowrap;
}}
footer a {{
  min-width: 0; color: inherit; text-decoration: none;
  overflow: hidden; text-overflow: ellipsis;
}}
footer a:last-child {{ flex-shrink: 10; }}
footer a:hover {{ text-decoration: underline; }}
.stale {{ color: #e8590c; }}
</style>
</head>
<body class="fit-{fit}">
<a class="strip" href="{page_url}" target="_blank" rel="noopener" \
title="{title}">
{images}
</a>
{footer}
</body>
</html>
"""

FOOTER_TEMPLATE = """<footer>
<a href="{page_url}" target="_blank" rel="noopener">{label}</a>
<a href="https://phdcomics.com/" target="_blank" rel="noopener">\
"Piled Higher and Deeper" by Jorge Cham</a>
</footer>"""

ERROR_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
<style>html, body {{ margin: 0; background: transparent;
color-scheme: light dark; font: 12px system-ui, sans-serif; }}</style>
</head><body><p>PHD Comics is not available now: {message}</p></body></html>
"""


def render_page(comic: Comic, theme: str, fit: str, caption: bool,
                refresh: int, stale: bool) -> str:
    """Make the HTML page for one comic."""
    title = html.escape(comic.title)
    page_url = html.escape(comic.page_url)
    # Relative URLs work behind a reverse proxy with a path prefix.
    images = "\n".join(
        f'<img src="image/{comic.comic_id}/{index}" alt="{title}">'
        for index in range(len(comic.images)))
    footer = ""
    if caption:
        label = title
        if comic.published:
            day = comic.published
            label += f" ({day:%b} {day.day}, {day.year})"
        if stale:
            label = f'<span class="stale">Offline.</span> {label}'
        footer = FOOTER_TEMPLATE.format(page_url=page_url, label=label)
    return PAGE_TEMPLATE.format(
        theme=theme, fit=fit, refresh=refresh, title=title,
        page_url=page_url, images=images, footer=footer)


def comic_json(comic: Comic, day: dt.date | None) -> dict:
    """Make the JSON data for one comic."""
    return {
        "day": day.isoformat() if day else None,
        "id": comic.comic_id,
        "title": comic.title,
        "published": (comic.published.isoformat()
                      if comic.published else None),
        "page_url": comic.page_url,
        "image_urls": comic.image_urls,
        "local_images": [f"image/{comic.comic_id}/{index}"
                         for index in range(len(comic.images))],
    }


def choose(params: dict[str, list[str]], name: str,
           allowed: tuple[str, ...]) -> str:
    """Return a query value if it is allowed. Else return the first one."""
    value = params.get(name, [allowed[0]])[0].lower()
    return value if value in allowed else allowed[0]


class Handler(BaseHTTPRequestHandler):
    """Send pages, images and JSON data."""

    server_version = "phd-daily/1.0"
    service: ComicService

    def do_GET(self) -> None:  # noqa: N802 (name from the base class)
        """Send the response for one GET request."""
        url = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(url.query)
        route = url.path.rstrip("/") or "/"
        image_match = IMAGE_ROUTE_RE.fullmatch(route)
        if route == "/":
            self._send_page(params)
        elif route == "/api/today":
            self._send_json()
        elif route == "/healthz":
            self._send(HTTPStatus.OK, b"ok\n", "text/plain")
        elif image_match:
            self._send_image(int(image_match[1]), int(image_match[2]))
        else:
            self._send(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain")

    def log_message(self, fmt: str, *args) -> None:
        """Send access log lines to the debug log."""
        LOG.debug("%s %s", self.address_string(), fmt % args)

    def _send_page(self, params: dict[str, list[str]]) -> None:
        theme = choose(params, "theme", THEMES)
        fit = choose(params, "fit", FITS)
        caption = params.get("caption", ["1"])[0] != "0"
        forced_id = params.get("id", [""])[0]
        stale = False
        try:
            if forced_id.isdigit():
                comic = self.service.comic_by_id(int(forced_id))
            else:
                comic = self.service.comic_for_day(self.service.today())
            refresh = self.service.seconds_to_next_day()
        except (SiteError, PageError) as exc:
            LOG.warning("Cannot get the comic: %s", exc)
            comic = self.service.last_good
            if comic is None or forced_id:
                body = ERROR_TEMPLATE.format(
                    refresh=STALE_REFRESH_S,
                    message=html.escape(str(exc)))
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, body.encode(),
                           "text/html; charset=utf-8", cache=False)
                return
            refresh, stale = STALE_REFRESH_S, True
        body = render_page(comic, theme, fit, caption, refresh, stale)
        self._send(HTTPStatus.OK, body.encode(), "text/html; charset=utf-8",
                   cache=False)

    def _send_json(self) -> None:
        day = self.service.today()
        try:
            data = comic_json(self.service.comic_for_day(day), day)
            status = HTTPStatus.OK
        except (SiteError, PageError) as exc:
            data, status = {"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE
        self._send(status, json.dumps(data, indent=2).encode(),
                   "application/json", cache=False)

    def _send_image(self, comic_id: int, index: int) -> None:
        image = self.service.image(comic_id, index)
        if image is None:
            self._send(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain")
            return
        body, content_type = image
        # The images of one comic ID do not change.
        self._send(HTTPStatus.OK, body, content_type, cache=True)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str,
              cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400, immutable"
                         if cache else "no-cache")
        self.end_headers()
        self.wfile.write(body)


def load_tz(name: str) -> ZoneInfo:
    """Return the time zone for a name. Use UTC if the name is not valid."""
    try:
        return ZoneInfo(name.lstrip(":") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        LOG.warning("Unknown time zone %r. Use UTC.", name)
        return ZoneInfo("UTC")


def parse_ids(text: str) -> frozenset[int]:
    """Read comma-separated IDs, for example "12, 345"."""
    return frozenset(int(part) for part in text.split(",")
                     if part.strip().isdigit())


def main() -> None:
    """Start the HTTP server."""
    logging.basicConfig(
        level=os.environ.get("PHD_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s")
    latest_env = os.environ.get("PHD_LATEST_ID", "").strip()
    Handler.service = ComicService(
        tz=load_tz(os.environ.get("TZ", "UTC")),
        skip_ids=DEFAULT_SKIP_IDS | parse_ids(
            os.environ.get("PHD_SKIP_IDS", "")),
        latest_override=int(latest_env) if latest_env.isdigit() else None,
    )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    LOG.info("Listen on http://%s:%d", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
