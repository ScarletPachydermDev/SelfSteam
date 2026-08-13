"""Resolves free-text search input into a URL + canonical SGDB search
term, exactly like gui.py's own resolve_url_input()/ResolvedInput --
e.g. typing "netflix" resolves to https://netflix.com plus the known-
good SGDB search term "Netflix", instead of leaving the user to search
blind and (as the server did before this existed) building the final
shortcut's URL as "https://netflix" -- a real bug, since a bare
recognized-service word like that isn't a valid domain on its own.

A standalone reimplementation rather than importing gui.py: gui.py
requires libadwaita at module level (`gi.require_version("Adw", "1")`),
not available for this headless server's native (non-Flatpak) Python
on the host. This logic itself has no GTK dependency, so it's safe to
duplicate directly.
"""
from urllib.parse import urlparse

import streaming_services


class Resolved:
    def __init__(self, url=None, name=None, sgdb_id=None, warning=None):
        self.url = url
        self.name = name
        self.sgdb_id = sgdb_id
        self.warning = warning


def _looks_like_url(text):
    candidate = text if "://" in text else f"https://{text}"
    host = urlparse(candidate).netloc
    return " " not in text and "." in host


def _match_streaming_service(key):
    starts = {streaming_services.STREAMING_SERVICES[k] for k in streaming_services.STREAMING_SERVICES if k.startswith(key)}
    if len(starts) == 1:
        return next(iter(starts))
    contains = {streaming_services.STREAMING_SERVICES[k] for k in streaming_services.STREAMING_SERVICES if key in k}
    if len(contains) == 1:
        return next(iter(contains))
    return None


def guess_name_from_url(url):
    if "://" not in url:
        url = f"https://{url}"
    host = urlparse(url).netloc.removeprefix("www.")
    base = host.split(".")[0]
    return base.replace("-", " ").title()


def resolve(text):
    text = text.strip()
    if not text:
        return Resolved()

    known = streaming_services.STREAMING_SERVICES.get(text.lower()) or _match_streaming_service(text.lower())
    if known:
        domain, name, sgdb_id = known
        return Resolved(url=f"https://{domain}", name=name, sgdb_id=sgdb_id)

    if _looks_like_url(text):
        url = text if "://" in text else f"https://{text}"
        return Resolved(url=url, name=guess_name_from_url(url))

    return Resolved(warning=f'"{text}" isn\'t a recognized service name or a URL')


def is_plain_youtube(text):
    """True only for the main youtube.com site -- not tv.youtube.com or
    a youtu.be link -- gates showing the Couch Mode checkbox, matching
    gui.py's couch_mode_row visibility."""
    resolved = resolve(text)
    if not resolved.url:
        return False
    return urlparse(resolved.url).netloc.removeprefix("www.") == "youtube.com"
