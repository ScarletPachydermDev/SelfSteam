"""Browses Flathub's real catalog for the Apps tab -- categories and
the apps within them, via Flathub's own public v2 API (confirmed real
and unauthenticated via its own OpenAPI spec at
https://flathub.org/api/v2/openapi.json). Installing/checking an app
found here is standalone_emulators.install_flathub_app_id/
flathub_app_id_installed instead -- this module only ever talks to
Flathub's own API, never flatpak itself.

Functions:
  search_category(category, page) -- one page of real Flathub apps in a category.
  search_apps(query, page) -- one page of real Flathub apps matching a text query.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

# The real, complete MainCategory enum (confirmed via Flathub's own
# OpenAPI schema) -- (slug, label) so the UI can show a friendlier name
# than the API's own bare identifier (e.g. "healthfitness").
CATEGORIES = [
    ("game", "Games"),
    ("graphics", "Graphics"),
    ("audiovideo", "Audio & Video"),
    ("network", "Internet"),
    ("office", "Office"),
    ("development", "Developer Tools"),
    ("science", "Science"),
    ("education", "Education"),
    ("system", "System"),
    ("utility", "Utilities"),
    ("healthfitness", "Health & Fitness"),
]
CATEGORY_SLUGS = {slug for slug, _label in CATEGORIES}

_API_BASE = "https://flathub.org/api/v2"
_PER_PAGE = 24

# Never offered as a browsable/installable app here -- SelfSteam already
# assumes exactly one real Steam install exists on the machine it's
# managing shortcuts for (steam_paths.py's own resolution, shortcuts.vdf
# writes, the restart/maintenance cycle); creating a shortcut *to*
# Steam, or letting someone install a second Steam instance through
# this picker, both risk exactly that assumption breaking. Filtered out
# of every hit list here (not just skipped at render time) so it also
# never counts as a category's own installed-apps-first candidate.
_BLOCKED_APP_IDS = {"com.valvesoftware.Steam"}


def _drop_blocked(hits):
    return [h for h in hits if h.get("app_id") not in _BLOCKED_APP_IDS]


def search_category(category, page=1):
    """Returns (hits, total_pages) for one page of real Flathub apps in
    category -- each hit a dict with at least app_id/name/summary/icon,
    exactly as Flathub's own API returns it (passed through, not
    reshaped, so the renderer can pull whatever fields it wants).
    Raises RuntimeError on a network/API failure -- surfaced to the
    user the same way every other real network call in this app is
    (standalone_emulators' own release-API fetches, SGDB searches),
    not silently swallowed into an empty list."""
    if category not in CATEGORY_SLUGS:
        category = "game"
    page = max(1, page)
    qs = urllib.parse.urlencode({"per_page": _PER_PAGE, "page": page})
    url = f"{_API_BASE}/collection/category/{category}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "SelfSteam"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Flathub category browse failed: {e}")
    return _drop_blocked(data.get("hits", [])), data.get("totalPages", 1)


def search_apps(query, page=1):
    """Returns (hits, total_pages) for one page of real Flathub apps
    matching a free-text query -- Flathub's own real search endpoint
    (POST /search, confirmed via its own OpenAPI schema), same hit
    shape as search_category so both feed the same card renderer.
    Empty query short-circuits to (empty list, 1) rather than sending a
    real request -- Flathub's own /search doesn't accept an empty
    query at all (it's a required field), and this is also what lets
    the Apps tab's own live-as-you-type search box clear back to
    nothing without a request per empty keystroke."""
    query = (query or "").strip()
    if not query:
        return [], 1
    page = max(1, page)
    body = json.dumps({"query": query, "hits_per_page": _PER_PAGE, "page": page}).encode()
    req = urllib.request.Request(
        f"{_API_BASE}/search", data=body, method="POST",
        headers={"User-Agent": "SelfSteam", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Flathub search failed: {e}")
    return _drop_blocked(data.get("hits", [])), data.get("totalPages", 1)
