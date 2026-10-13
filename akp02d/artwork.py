"""Cover art: the picture behind a URL a media player hands out.

MPRIS players describe the cover as `mpris:artUrl`, in one of three
forms, all handled here:

* file:///path/cover.jpg -- a file on this machine (mpv, VLC, Chromium's
  temporary copies). Read directly.
* data:image/jpeg;base64,/9j/4AAQ... -- the picture itself, inline. Some
  players send embedded art this way rather than writing a file.
  Decoded directly.
* https://i.scdn.co/image/... -- a download (Spotify, most web
  players). Fetched on a background thread: the loop never waits on the
  network. Until it arrives `picture()` says PENDING, and the widget
  keeps showing what it had.

Decoded pictures are kept in a small cache keyed by URL (a data: URL by
a digest of it, since it can be megabytes), so going back to a track,
or several widgets showing the same cover, costs nothing. A URL that
fails is remembered as failed, and not retried until it leaves the
cache.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

from PIL import Image, UnidentifiedImageError

log = logging.getLogger(__name__)

# Covers kept decoded. A handful covers a playlist going back and forth.
CACHE_SIZE = 16
# Larger than any cover; protects against a URL that is not one.
MAX_BYTES = 16 * 2**20
# Covers are shown at panel sizes; a 3000 px scan is scaled down once.
MAX_SIDE = 1024
TIMEOUT_SEC = 5.0


class _Pending:
    def __repr__(self) -> str:
        return "PENDING"


PENDING = _Pending()

_lock = threading.Lock()
_cache: OrderedDict[str, Image.Image | None] = OrderedDict()
_fetching: set[str] = set()


def picture(url: str) -> Image.Image | None | _Pending:
    """The cover at `url` as RGBA, None if there is none or it failed, or
    PENDING while a download is under way. The image is shared: callers
    must not draw on it."""
    if not url:
        return None
    key = _key(url)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        if key in _fetching:
            return PENDING
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme in ("http", "https"):
        with _lock:
            _fetching.add(key)
        threading.Thread(target=_fetch, args=(url, key), name="artwork",
                         daemon=True).start()
        return PENDING
    image = _load(url)
    _store(key, image)
    return image


def _key(url: str) -> str:
    if url.startswith("data:"):
        return "data:" + hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()
    if url.startswith(("file:", "/")):
        # Some players rewrite one file for every track: its state is
        # part of which picture it is.
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path) if parsed.scheme == "file" else url
        try:
            stat = os.stat(path)
            return f"{url}#{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            pass
    return url


def _store(key: str, image: Image.Image | None) -> None:
    with _lock:
        _cache[key] = image
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


def _fetch(url: str, key: str) -> None:
    image = _load(url)
    _store(key, image)
    with _lock:
        _fetching.discard(key)


def _load(url: str) -> Image.Image | None:
    try:
        return _decode(_bytes(url))
    except (OSError, ValueError, binascii.Error, UnidentifiedImageError,
            urllib.error.URLError, Image.DecompressionBombError) as exc:
        log.warning("cover art %s: %s", _short(url), exc)
        return None


def _bytes(url: str) -> bytes:
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        if ";base64" in header.lower():
            # Players are not always strict: tolerate missing padding.
            payload = payload.strip()
            return base64.b64decode(payload + "=" * (-len(payload) % 4))
        return urllib.parse.unquote_to_bytes(payload)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https"):
        request = urllib.request.Request(url, headers={"User-Agent": "akp02d"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            data = response.read(MAX_BYTES + 1)
    else:
        # file:///path, or a bare path.
        path = urllib.parse.unquote(parsed.path) if parsed.scheme == "file" else url
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"larger than {MAX_BYTES // 2**20} MiB")
    return data


def _decode(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGBA")
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    return image


def _short(url: str) -> str:
    """A URL for a log line: a data: URL is not worth printing whole."""
    if url.startswith("data:"):
        return f"{url[:30]}... ({len(url)} chars)"
    return url


def clear() -> None:
    """Forget every cached cover, for tests."""
    with _lock:
        _cache.clear()
