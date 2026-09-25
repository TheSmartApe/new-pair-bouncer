"""Rewrites every coingecko.com / docs.coingecko.com link in a set of files to carry a utm_content handle."""
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_URL = re.compile(r'https?://(?:www\.)?(?:docs\.)?coingecko\.com[^\s"\'<>)\]]*')
_API_PAGE = re.compile(r"^https?://(?:www\.)?coingecko\.com/en/api/?$")


def _normalize_handle(handle: str) -> str:
    """Strips a leading @ and lowercases, so `@Foo` and `foo` tag the same."""
    return handle.lstrip("@").lower()


def _rewrite_url(url: str, handle: str, source: str, url_override: str | None) -> str:
    """One URL, with utm_source/utm_content set (or swapped for url_override if it's the /en/api page)."""
    bare = url.split("?", 1)[0].split("#", 1)[0]
    if url_override and _API_PAGE.match(bare):
        return url_override
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["utm_source"] = source
    query["utm_content"] = handle
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def rewrite_text(text: str, handle: str, source: str = "github", url_override: str | None = None) -> str:
    """Rewrites every matching URL inside a block of text. Idempotent: running it twice is a no-op the second time."""
    handle = _normalize_handle(handle)
    return _URL.sub(lambda m: _rewrite_url(m.group(0), handle, source, url_override), text)


def set_link(paths: list[str], handle: str, source: str = "github", url_override: str | None = None):
    """Applies rewrite_text() to every file in `paths`, in place."""
    for p in paths:
        path = Path(p)
        path.write_text(rewrite_text(path.read_text(), handle, source, url_override))
