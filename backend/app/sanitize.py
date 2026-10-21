"""Saneado de HTML del catálogo con una lista blanca compartida por API y build."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

ALLOWED_TAGS = {"a", "code", "strong", "em", "br"}
ALLOWED_ATTRS = {"a": {"href", "title"}}
ALLOWED_SCHEMES = {"http", "https", "mailto"}

try:  # bleach está en requirements; el fallback permite tests sin instalarlo.
    import bleach  # type: ignore
except ImportError:  # pragma: no cover - depende del entorno
    bleach = None


def _valid_href(value):
    if not value:
        return None
    parsed = urlparse(value)
    return value if parsed.scheme.lower() in ALLOWED_SCHEMES else None


def _bleach_clean(value):
    cleaned = bleach.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_SCHEMES,
        strip=True,
        strip_comments=True,
    )
    return re.sub(r"<a\s", '<a rel="noopener noreferrer" target="_blank" ', cleaned)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            self.out.append("<br>")
            return
        if tag not in ALLOWED_TAGS:
            return
        clean = []
        for key, value in attrs:
            key = key.lower()
            if key not in ALLOWED_ATTRS.get(tag, set()):
                continue
            if key == "href":
                value = _valid_href(value)
                if not value:
                    continue
            if value is not None:
                clean.append((key, value))
        if tag == "a":
            clean.extend([("rel", "noopener noreferrer"), ("target", "_blank")])
        rendered = " ".join(
            f'{key}="{html.escape(value, quote=True)}"' for key, value in clean
        )
        self.out.append(f"<{tag}{(' ' + rendered) if rendered else ''}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ALLOWED_TAGS and tag != "br":
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))


def sanitize_html(value):
    if value is None:
        return ""
    text = str(value)
    if bleach is not None:
        return _bleach_clean(text)
    parser = _Sanitizer()
    parser.feed(text)
    parser.close()
    return "".join(parser.out)
