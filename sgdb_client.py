"""Minimal SteamGridDB API v2 client (stdlib only, no third-party deps)."""
import json
import os
import re
import urllib.request
import urllib.parse

import config

API_BASE = "https://www.steamgriddb.com/api/v2"

# SGDB excludes the "Humor" tag from results unless explicitly asked for
# (same convention as its nsfw filter) -- "any" includes both tagged and
# untagged images instead of narrowing to humor-only.
INCLUDE_HUMOR = {"humor": "any"}

# Steam's non-Steam-shortcut grid folder wants these shapes:
VERTICAL_DIMENSIONS = "600x900,342x482,660x930"
HORIZONTAL_DIMENSIONS = "460x215,920x430"

_ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")


class SGDBError(RuntimeError):
    pass


def _load_dotenv():
    if not os.path.exists(_ENV_FILE):
        return
    with open(_ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _api_key():
    _load_dotenv()
    key = os.environ.get("STEAMGRIDDB_API_KEY") or config.get_sgdb_api_key()
    if not key:
        raise SGDBError(
            "No SteamGridDB API key configured. Set it in the app's Settings, "
            "or set STEAMGRIDDB_API_KEY in your environment for CLI use "
            "(get a free key at steamgriddb.com/profile/preferences/api)"
        )
    return key


def has_api_key():
    try:
        _api_key()
        return True
    except SGDBError:
        return False


def verify_api_key(key):
    """True/False for whether `key` is actually accepted by SGDB's API
    (a real request, not just "is a string configured") -- checked
    against the key being submitted, not whatever's already saved.
    Raises SGDBError only for transport-level failures (network down,
    timeout, unexpected response) so callers can tell "definitely
    invalid" apart from "couldn't check right now"."""
    url = f"{API_BASE}/search/autocomplete/a"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "gridge/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        return bool(data.get("success"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False
        raise SGDBError(f"SGDB API error {e.code} verifying key: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        raise SGDBError(f"Network error verifying SGDB key: {e.reason}")


def _get(path, params=None):
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "User-Agent": "gridge/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        raise SGDBError(f"SGDB API error {e.code} for {path}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        # Broader than HTTPError -- covers DNS failures, connection
        # refused, no route to host, etc. Confirmed this silently
        # crashed a background thread instead of surfacing an error
        # (froze the UI on "Checking key..." forever, no exception ever
        # reaching a caller's except SGDBError) when the sandbox had no
        # network access at all; the same gap would just as easily hit
        # a real user's dropped WiFi mid-search outside any sandbox too.
        raise SGDBError(f"Network error reaching SGDB for {path}: {e.reason}")
    if not data.get("success"):
        raise SGDBError(f"SGDB API returned failure for {path}: {data}")
    return data["data"]


_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def clean_sgdb_name(name):
    """Strips any SGDB category-tag suffix in trailing parentheses --
    "Stremio (Program)", "Arte (Website)", etc. -- that shouldn't end up
    in a Steam shortcut's actual name. NOT applied inside search()/
    get_game() themselves: the raw tag is a genuinely useful
    disambiguator in the match list (multiple same-named entries), so
    callers strip it only once they're using a name for the shortcut
    itself, not for display."""
    stripped = _TRAILING_PAREN_RE.sub("", name)
    return stripped or name


def search(term):
    """Return list of {id, name, verified} candidate matches for a query."""
    results = _get(f"/search/autocomplete/{urllib.parse.quote(term)}")
    return [{"id": r["id"], "name": r["name"], "verified": r.get("verified", False)} for r in results]


def get_game(game_id):
    """Fetch a single game entry directly by its known SGDB id, bypassing
    autocomplete -- used for entries confirmed to return unreliable
    autocomplete matches (e.g. Disney+)."""
    data = _get(f"/games/id/{game_id}")
    return {"id": data["id"], "name": data["name"], "verified": data.get("verified", False)}


def get_vertical_grid(game_id):
    grids = _get(f"/grids/game/{game_id}", {"dimensions": VERTICAL_DIMENSIONS, **INCLUDE_HUMOR})
    return grids[0]["url"] if grids else None


def get_horizontal_grid(game_id):
    grids = _get(f"/grids/game/{game_id}", {"dimensions": HORIZONTAL_DIMENSIONS, **INCLUDE_HUMOR})
    return grids[0]["url"] if grids else None


def get_hero(game_id):
    heroes = _get(f"/heroes/game/{game_id}", INCLUDE_HUMOR)
    return heroes[0]["url"] if heroes else None


def get_logo(game_id):
    logos = _get(f"/logos/game/{game_id}", INCLUDE_HUMOR)
    return logos[0]["url"] if logos else None


def get_icon(game_id):
    icons = _get(f"/icons/game/{game_id}", INCLUDE_HUMOR)
    if not icons:
        return None
    # Prefer a plain PNG/APNG over .ico -- .ico isn't part of the
    # freedesktop icon spec and renders blank in some app menus.
    for icon in icons:
        if icon["url"].lower().endswith(".png"):
            return icon["url"]
    return icons[0]["url"]


# Candidate-list variants for the artwork picker -- each SGDB entry
# already carries both "url" (full-res) and "thumb" (small preview), so
# the picker can load thumbnails cheaply and only download the full
# image for whatever the user actually selects. No cap on how many
# come back: a fixed limit silently excluded real results for popular
# titles (confirmed: Netflix alone has 28 vertical grids, more than
# even a generous cap), and the picker now throttles concurrent
# thumbnail *downloads* instead of truncating the candidate list
# itself, so showing everything no longer means firing off unbounded
# simultaneous requests.


def get_vertical_grid_candidates(game_id):
    return _get(f"/grids/game/{game_id}", {"dimensions": VERTICAL_DIMENSIONS, **INCLUDE_HUMOR})


def get_horizontal_grid_candidates(game_id):
    return _get(f"/grids/game/{game_id}", {"dimensions": HORIZONTAL_DIMENSIONS, **INCLUDE_HUMOR})


def get_hero_candidates(game_id):
    return _get(f"/heroes/game/{game_id}", INCLUDE_HUMOR)


def get_logo_candidates(game_id):
    return _get(f"/logos/game/{game_id}", INCLUDE_HUMOR)


def get_icon_candidates(game_id):
    # Same .png-over-.ico preference as get_icon(), just applied across
    # the whole list instead of picking just one.
    icons = _get(f"/icons/game/{game_id}", INCLUDE_HUMOR)
    return sorted(icons, key=lambda i: not i["url"].lower().endswith(".png"))


def extract_largest_png_from_ico(ico_bytes):
    """Modern .ico files embed PNG-compressed frames for larger sizes.
    Pull out the biggest one so we can save a real .png (no image lib needed).
    Returns None if the .ico only contains legacy BMP/DIB frames."""
    count = int.from_bytes(ico_bytes[4:6], "little")
    best = None
    for i in range(count):
        entry = ico_bytes[6 + i * 16 : 6 + i * 16 + 16]
        width = entry[0] or 256
        height = entry[1] or 256
        size = int.from_bytes(entry[8:12], "little")
        offset = int.from_bytes(entry[12:16], "little")
        data = ico_bytes[offset : offset + size]
        if data[:8] == b"\x89PNG\r\n\x1a\n" and (best is None or width * height > best[0]):
            best = (width * height, data)
    return best[1] if best else None


def download(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": "gridge/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    with open(dest_path, "wb") as f:
        f.write(data)
    return dest_path
