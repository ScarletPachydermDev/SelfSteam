"""Finds a game's page on decomps.net (a directory of game decompilation
and PC-port projects) by name, for the button beside the RetroArch and
Emulators tabs' Name fields.

decomps.net publishes a sitemap whose game pages look like
/game/goldeneye-007-JJkpE8nh6v: the name as a slug plus a short ID. The
sitemap is fetched at most once a day and matched locally, so picking a
ROM never fetches more than that one file. The site's robots.txt allows
general clients, and its content signal allows search and reference use,
which is all this does: link to the page.

Functions:
  game_url(name) -- the decomps.net page for a game name, or None.
"""
import re
import time
import unicodedata
import urllib.request

_SITEMAP_URL = "https://decomps.net/sitemap.xml"
_GAME_RE = re.compile(r"<loc>(https://decomps\.net/game/([A-Za-z0-9-]+))</loc>")
_cache = {"at": 0.0, "games": None}


def _slug(name):
    """decomps.net's slug for a name: ASCII, lowercase, runs of anything
    else turned into one dash ("Demon's Souls" -> "demon-s-souls")."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def _games():
    """{slug: page URL}, from the cached sitemap. Keeps the last good
    copy when a refresh fails."""
    now = time.time()
    if _cache["games"] is not None and now - _cache["at"] < 86400:
        return _cache["games"]
    try:
        req = urllib.request.Request(_SITEMAP_URL, headers={"User-Agent": "SelfSteam"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("utf-8", "replace")
        games = {}
        for url, path in _GAME_RE.findall(text):
            # The ID is the part after the last dash.
            games.setdefault(path.rsplit("-", 1)[0], url)
        _cache.update(at=now, games=games)
    except Exception:  # noqa: BLE001 -- offline: no button, never an error
        _cache["at"] = now
        if _cache["games"] is None:
            _cache["games"] = {}
    return _cache["games"]


def game_url(name):
    if not name:
        return None
    games = _games()
    slug = _slug(name)
    for candidate in (slug, re.sub(r"^the-", "", slug), re.sub(r"-the$", "", slug)):
        if candidate in games:
            return games[candidate]
    return None
