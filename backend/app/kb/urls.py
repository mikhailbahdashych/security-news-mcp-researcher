"""Canonicalising a URL so the same article is the same entry.

The knowledge base's strongest dedup key is the URL, and the same article reaches
it spelled several ways: from the feed with a ``utm_source``, from the chat with a
fragment, from the user's clipboard with a capital host. Without a canonical form
each spelling is a new entry, and "have we covered this?" answers "three times".

The rules are deliberately conservative — everything here either cannot change
which document is served (case in the scheme and host, the default port, the
fragment) or is a known click-tracking parameter. ``www.`` is **not** stripped and
query parameters are **not** reordered or sorted: plenty of sites serve different
documents for ``?page=2&sort=new`` than for ``?sort=new&page=2``, and an
over-eager canonicaliser silently merges two different articles into one entry,
which is not recoverable by anything short of a re-capture.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Click-tracking parameters, dropped wherever they appear. Matched case-insensitively.
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "oly_anon_id",
        "oly_enc_id",
        "vero_id",
        "wt_mc",
        "cmpid",
        "ref_src",
        "spm",
    }
)

#: Prefixes whose whole family is tracking (``utm_source``, ``utm_medium``, ...).
TRACKING_PREFIXES = ("utm_",)

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def canonical_url(url: str | None) -> str | None:
    """The stored form of *url*, or ``None`` when it is not a usable http(s) URL.

    ``None`` rather than a raised error: a feed item with no link, a note, and a
    ``javascript:`` URL are all ordinary inputs to capture, and the caller's
    answer to each is the same — fall back to the content hash as the dedup key.
    """
    raw = (url or "").strip()
    if not raw:
        return None

    try:
        parts = urlsplit(raw)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None

    host = parts.hostname.lower()
    netloc = host
    if parts.port is not None and str(parts.port) != _DEFAULT_PORTS[scheme]:
        netloc = f"{host}:{parts.port}"

    query = urlencode(
        [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking(name)
        ]
    )

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    # The fragment is dropped: it never reaches the server, so two URLs that
    # differ only there are the same document by definition.
    return urlunsplit((scheme, netloc, path, query, ""))


__all__ = ["TRACKING_PARAMS", "TRACKING_PREFIXES", "canonical_url"]
