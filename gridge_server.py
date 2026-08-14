#!/usr/bin/env python3
"""Gridge Server: headless web UI for adding Steam shortcuts from another
device while the target machine is in Game Mode. No JavaScript by
design -- plain HTML forms, server-rendered, one request per step,
except two small deliberate exceptions (dark-mode toggle, login
auto-submit), both explicit user requests.

Reuses the exact same backend create_webapp.py/sgdb_client.py/
shortcuts_vdf.py already use for the GTK app; the only new pieces here
are the maintenance-window sequencing (see maintenance.py, needed
because a shortcuts.vdf write while Steam is running gets silently
clobbered) and screen-pairing auth (see auth.py).

Three-column layout: left (search + browser picker + Add button,
pinned to the bottom), middle (SGDB matches, or a direct-SGDB-search
override via the magnifying glass), right (artwork, all 5 categories
always shown). Styling mirrors the GTK desktop app's own MainWindow
(gui.py): same accent blue, boxed-list look, Helvetica. gui.py itself
isn't importable here -- it requires libadwaita at module level, not
available for this headless server's native Python on the host.

Browser picker note: the <select> is real and populated from actually
installed Flatpak browsers (browser_picker.py, detected via each
app's exported .desktop Categories=WebBrowser / MimeType=x-scheme-
handler/https). The selection threads through the request, but doesn't
change the shortcut's actual launch command yet -- that needs
generalizing create_webapp.py's Edge-only kiosk-arg construction to
handle other browsers' very different kiosk syntax (Firefox-based
browsers like LibreWolf use "-kiosk", not "--app="/"--kiosk", and have
no direct equivalent to Chromium's --app= at all), which is the
separately-scoped "browser-picker rework" -- not attempted here to
avoid silently shipping a selector that produces a broken shortcut for
non-Chromium browsers.
"""
import html
import http.cookies
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import auth_display
import browser_picker
import config
import create_webapp
import maintenance
import service_resolver
import sgdb_client as sgdb

PORT = int(os.environ.get("GRIDGE_SERVER_PORT", "8845"))
SESSION_COOKIE = "gridge_session"
_DARKREADER_PATH = os.path.join(os.path.dirname(__file__), "vendor", "darkreader.js")
_ADD_FORM_ID = "gridge-add-form"

# (basename, display title, candidate-fetcher, cell width, cell height)
# -- basenames and the *relative* cell proportions match gui.py's own
# ARTWORK_CATEGORIES exactly (170x255, 260x121, 320x104, 160x100,
# 100x100), scaled up by _ARTWORK_SCALE. Kept small enough (rather than
# the desktop app's own 1.8x) that all 5 categories fit within the
# column's now-bounded height without needing to scroll on typical
# screens; .card's overflow-y:auto is still there as a safety net for
# short windows.
_ARTWORK_SCALE = 0.85
ARTWORK_CATEGORIES = [
    ("grid_vertical", "Vertical Grid", sgdb.get_vertical_grid_candidates, 170, 255),
    ("grid_horizontal", "Horizontal Grid", sgdb.get_horizontal_grid_candidates, 260, 121),
    ("hero", "Hero", sgdb.get_hero_candidates, 320, 104),
    ("logo", "Logo", sgdb.get_logo_candidates, 160, 100),
    ("icon", "Icon", sgdb.get_icon_candidates, 100, 100),
]

PAGE_HEAD = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gridge Server</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* Palette/spacing/radii lifted from the "Three-column UI draft" design
   handoff (Claude Design mockup) -- draft placeholder tokens per its
   own README, reconciled here as the real values rather than kept
   pixel-for-pixel, but the shape (pill inputs, flat cards, segmented
   tab bar, pastel accent) is the deliberate target look. */
:root {
  --accent: #b8e0b0;
  --accent-text: #1f4d1a;
  --success-bg: #eaf7ec; --success-border: #bfe3c4; --success-text: #2f7a3d;
  --bg: #f2f2f2;
  --card-bg: #ffffff;
  --border: #e6e6e6;
  --input-border: #ddd;
  --text: #1a1a1a;
  --text-dim: #8a8a8a;
  --skeleton: #e4e4e4;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg); color: var(--text); margin: 0;
  display: flex; flex-direction: column; height: 100vh;
}
::placeholder { color: #9a9a9a; }
header.gridge-header {
  background: var(--card-bg); border-bottom: 1px solid var(--border);
  padding: 1.1rem 2rem; display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 1rem; flex: 0 0 auto;
}
.gridge-header-title { display: flex; flex-direction: column; gap: 0.1rem; }
.gridge-header-title strong { font-size: 1.2rem; font-weight: 700; letter-spacing: -0.01em; }
.gridge-header-title span { font-size: 0.8rem; color: var(--text-dim); }
.gridge-header-actions { display: flex; gap: 0.6rem; align-items: center; }
.icon-btn-round {
  width: 2.6rem; margin: 0; padding: 0; height: 2.6rem; border-radius: 10px;
  border: 1px solid var(--border); background: var(--card-bg); color: var(--text-dim);
  display: flex; align-items: center; justify-content: center; font-size: 1.2rem; cursor: pointer;
}
.sgdb-key-badge {
  width: auto; margin: 0; padding: 0.5rem 1rem; border-radius: 20px; font-size: 0.8rem; font-weight: 700;
  display: inline-flex; align-items: center; gap: 0.4rem; cursor: default;
  background: var(--success-bg); border: 1px solid var(--success-border); color: var(--success-text);
}
.sgdb-key-badge.unverified { background: #fbeceb; border-color: #f0c4c0; color: #a13a2f; }
/* min-height:0 everywhere down this flex chain is load-bearing: flex
   items default to min-height:auto, which refuses to shrink below
   their content size and lets real content (e.g. many artwork
   thumbnails) push main/.gridge-columns/the cards taller than the
   viewport -- which then cascades into align-items:stretch handing
   every column that same inflated height, so a column with only one
   real content row (flex:1) stretches it to fill that whole inflated
   height instead of a sane share of the actual screen. Setting
   min-height:0 lets each level actually respect its bounded height
   and overflow internally (see .card's overflow-y:auto) instead. */
main { width: 100%; padding: 2rem; flex: 1; min-height: 0; display: flex; flex-direction: column; }
/* flex-wrap deliberately off here: with wrap enabled, a flex line's
   cross size gets computed from its items' content instead of the
   container's own (bounded) height, so align-items:stretch silently
   stopped capping the columns and one tall column's real content
   (e.g. the artwork column's images) pushed the whole page taller.
   Wrapping only ever matters for the stacked mobile layout below,
   which sets its own rules including flex-wrap. */
.gridge-columns { display: flex; gap: 1.5rem; align-items: stretch; flex-wrap: nowrap; flex: 1; min-height: 0; }
.gridge-left, .gridge-middle, .gridge-right { display: flex; flex-direction: column; min-height: 0; }
.gridge-left, .gridge-middle { flex: 1 1 300px; min-width: 280px; }
.gridge-right { flex: 1.4 1 400px; min-width: 320px; }
/* Pins the Add button to the bottom of the left column regardless of
   how much is above it, as long as the column has real height to grow
   into -- which align-items:stretch on .gridge-columns guarantees. */
.gridge-spacer { flex: 1 1 auto; }
.card {
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: 12px; padding: 1.15rem; display: flex; flex-direction: column; gap: 0.9rem;
  flex: 1; min-height: 0; overflow-y: auto;
}
.card h2 { font-size: 0.85rem; font-weight: 700; color: var(--text); margin: 0; }
/* Groups a label with its own field so the form's larger inter-field
   gap (0.9rem, set on the URL tab's <form>) doesn't also apply between
   a label and the input right below it -- that read as titles being
   too far from their fields. */
.field-group { display: flex; flex-direction: column; gap: 0.35rem; }
label.field-label {
  display: block; font-size: 0.85rem; font-weight: 600; color: var(--text-dim);
  letter-spacing: 0.01em; margin: 0;
}
input[type=text], select {
  width: 100%; padding: 0.8rem 1.1rem; font-size: 0.9rem; font-family: inherit;
  border: 1px solid var(--input-border); border-radius: 20px; background: #fff; color: var(--text);
  appearance: none; outline: none;
}
input[type=text]:focus, select:focus { border-color: var(--accent); }
.field-with-clear {
  display: flex; align-items: center; background: #fff; border: 1px solid var(--input-border);
  border-radius: 20px; padding: 0 0.4rem 0 1.1rem;
}
.field-with-clear input[type=text] { flex: 1; min-width: 0; border: none; padding: 0.85rem 0; border-radius: 0; }
.field-with-clear input[type=text]:focus { border: none; }
.field-clear-btn {
  width: 1.8rem; height: 1.8rem; flex: 0 0 auto; margin: 0; padding: 0; font-size: 0.9rem; line-height: 1;
  border-radius: 50%; background: transparent; color: #4a4a4a; border: none; text-decoration: none;
  display: flex; align-items: center; justify-content: center;
}
.hint-row { display: flex; align-items: flex-start; gap: 0.5rem; font-size: 0.95rem; color: var(--text-dim); margin: 0; }
.hint-row.warning { color: #c25b1f; }
.info-icon {
  flex: 0 0 auto; width: 1.15rem; height: 1.15rem; border-radius: 50%; margin-top: 0.05rem;
  background: var(--text-dim); color: #fff; font-size: 0.7rem; font-weight: 700; font-style: italic;
  display: flex; align-items: center; justify-content: center;
}
.hint-row.warning .info-icon { background: #c25b1f; font-style: normal; }
button, .btn {
  width: 100%; padding: 0.85rem 1.4rem; font-size: 0.9rem; margin-top: 0; font-family: inherit;
  border: none; border-radius: 10px; background: var(--accent); color: var(--accent-text);
  cursor: pointer; font-weight: 700; text-align: center; text-decoration: none; display: block;
}
button.secondary { background: var(--bg); color: var(--text); border: 1px solid var(--input-border); }
/* Fixed compact row height (not flex-grown per row -- that made a
   single real match balloon to fill the whole column). The list
   itself still flexes to fill the card; a generous placeholder row
   count (see _PLACEHOLDER_ROW_COUNT) is what actually keeps the empty
   state looking "full of zebra" rather than the row height doing it. */
.boxed-list { border-radius: 8px; overflow: hidden; display: flex; flex-direction: column; gap: 2px; flex: 1; }
.boxed-list a {
  display: flex; align-items: center; flex: 0 0 auto; height: 2.3rem; padding: 0 0.9rem;
  cursor: pointer; font-size: 0.9rem; color: var(--text); text-decoration: none;
}
.boxed-list a:nth-child(odd) { background: #ececec; }
.boxed-list a:nth-child(even) { background: #f3f3f3; }
.boxed-list a.selected { outline: 2px solid var(--accent); outline-offset: -2px; font-weight: 600; }
/* Placeholder rows before any search -- reserves the middle column's
   space instead of it looking like an empty gap. */
.placeholder-row { flex: 0 0 auto; height: 2.3rem; }
.placeholder-row:nth-child(odd) { background: #ececec; }
.placeholder-row:nth-child(even) { background: #f3f3f3; }
.artwork-card { gap: 0.5rem; }
.artwork-category h3 { font-size: 0.85rem; font-weight: 700; margin: 0 0 0.4rem 0; color: var(--text); }
.artwork-row { display: flex; gap: 0.7rem; overflow-x: auto; padding-bottom: 0.15rem; }
.artwork-cell { flex: 0 0 auto; }
.artwork-cell input[type=radio] { display: none; }
.artwork-cell label {
  display: flex; align-items: center; justify-content: center;
  border: 3px solid transparent; border-radius: 8px;
  cursor: pointer; overflow: hidden;
  /* Deliberately not var(--skeleton) (light gray): white/light logos
     with a transparent background were invisible against it. A
     neutral dark gray works as a "light box" for artwork of any
     color, light or dark. Dark Reader (the toggle) recolors it
     automatically along with everything else, so no separate
     dark-mode value needed. */
  background: #3a3a3a;
}
.artwork-cell input[type=radio]:checked + label { border-color: var(--accent); }
/* CONTAIN, not cover: cover crops to fill the box, which mangled
   irregularly-shaped artwork (logos especially -- a wide transparent
   logo showed up as a cropped square). gui.py's own picker uses
   Gtk.ContentFit.CONTAIN for exactly this reason; match it here. */
.artwork-cell img { display: block; width: 100%; height: 100%; object-fit: contain; }
.artwork-empty { color: var(--text-dim); font-size: 0.85rem; font-style: italic; }
.switch-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; }
/* Segmented tab bar (URL / Apps / Chimera / Emulators): a pure-CSS
   radio hack, same technique as the sgdb-search reveal used to be --
   no page reload needed to switch tabs, since nothing server-side
   depends on which one is showing. */
.tab-radio { position: absolute; opacity: 0; pointer-events: none; }
.tab-bar { display: flex; gap: 4px; background: var(--bg); border-radius: 12px; padding: 4px; }
.tab-label {
  flex: 1; padding: 0.55rem 0.25rem; border-radius: 9px; font-size: 0.8rem; font-weight: 600;
  text-align: center; cursor: pointer; color: var(--text-dim);
}
#tab-url:checked ~ .tab-bar label[for="tab-url"],
#tab-apps:checked ~ .tab-bar label[for="tab-apps"],
#tab-chimera:checked ~ .tab-bar label[for="tab-chimera"],
#tab-emulators:checked ~ .tab-bar label[for="tab-emulators"] {
  background: #fff; color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
.tab-panels { display: flex; flex-direction: column; }
.tab-panel { display: none; flex-direction: column; gap: 0.9rem; }
#tab-url:checked ~ .tab-panels .tab-panel-url,
#tab-apps:checked ~ .tab-panels .tab-panel-apps,
#tab-chimera:checked ~ .tab-panels .tab-panel-chimera,
#tab-emulators:checked ~ .tab-panels .tab-panel-emulators { display: flex; }
.coming-soon { color: var(--text-dim); font-size: 0.85rem; padding: 1rem 0; text-align: center; }
@media (max-width: 960px) {
  /* Stacked columns don't work with the bounded-height/internal-scroll
     trick above -- three independently-scrolling panels stacked
     vertically is worse than just letting the whole page scroll
     normally. Real mobile layout is still a later pass; this just
     keeps today's fix from making narrow viewports worse. */
  body { height: auto; min-height: 100vh; }
  main { min-height: auto; }
  .gridge-columns { align-items: flex-start; min-height: auto; flex-wrap: wrap; }
  .gridge-left, .gridge-middle, .gridge-right { flex-basis: 100%; min-height: auto; }
  .card { overflow-y: visible; }
  .gridge-spacer { flex: 0 0 0; }
}
</style></head><body>
<header class="gridge-header">
  <div class="gridge-header-title">
    <strong>Add Steam Shortcut</strong>
    <span>Create a shortcut, find matching artwork, and preview it before saving</span>
  </div>
  <div class="gridge-header-actions">
    <button class="icon-btn-round" type="button" title="Favorite" style="color:#e0568c">&#9825;</button>
    <!--SGDB_KEY_BADGE-->
    <button id="gridge-dark-toggle" class="icon-btn-round" type="button" title="Toggle dark mode">&#9789;</button>
  </div>
</header>
<main>
"""
# Dark Reader (vendor/darkreader.js, MIT, see vendor/DARKREADER-LICENSE.txt)
# is one of two deliberate exceptions to this app's no-JavaScript
# design -- explicit user requests, scoped narrowly. Preference
# persists via localStorage so it survives across page loads (every
# click here is a real navigation, not an SPA).
PAGE_TAIL = """</main>
<script src="/vendor/darkreader.js"></script>
<script>
(function () {
  var KEY = "gridge-dark-mode";
  var enable = function () { DarkReader.enable({brightness: 100, contrast: 100, sepia: 0}); };
  var disable = function () { DarkReader.disable(); };
  if (localStorage.getItem(KEY) === "1") enable();
  document.getElementById("gridge-dark-toggle").addEventListener("click", function () {
    if (localStorage.getItem(KEY) === "1") {
      localStorage.setItem(KEY, "0");
      disable();
    } else {
      localStorage.setItem(KEY, "1");
      enable();
    }
  });
})();
</script>
</body></html>"""


def _sgdb_key_badge_html():
    if sgdb.has_api_key():
        return '<span class="sgdb-key-badge">&#10003; SGDB API key configured</span>'
    return '<span class="sgdb-key-badge unverified">&#33; No SGDB API key</span>'


def render(body):
    head = PAGE_HEAD.replace("<!--SGDB_KEY_BADGE-->", _sgdb_key_badge_html())
    return (head + body + PAGE_TAIL).encode()


def _hidden_state_fields(query, couch_mode, browser):
    fields = f'<input type="hidden" name="q" value="{html.escape(query)}">'
    if couch_mode:
        fields += '<input type="hidden" name="couch_mode" value="1">'
    if browser:
        fields += f'<input type="hidden" name="browser" value="{html.escape(browser)}">'
    return fields


def _state_qs(query, couch_mode, browser, **extra):
    qs = f"q={urllib.parse.quote(query)}"
    if couch_mode:
        qs += "&couch_mode=1"
    if browser:
        qs += f"&browser={urllib.parse.quote(browser)}"
    for key, value in extra.items():
        if value:
            qs += f"&{key}={urllib.parse.quote(str(value))}"
    return qs


def _default_browser(browser_param):
    # Explicit param (threaded through the current request) wins; failing
    # that, whatever browser was used for the last shortcut actually
    # created (config.py, shared with the desktop app's own config.json
    # but under a server-only key); failing that, just the first detected
    # browser so the select always has a real, launchable choice selected
    # rather than an inert "System default" placeholder.
    if browser_param:
        return browser_param
    remembered = config.get_last_browser()
    if remembered:
        return remembered
    browsers = browser_picker.list_installed_browsers()
    return browsers[0][0] if browsers else ""


def _browser_select_html(selected_browser):
    browsers = browser_picker.list_installed_browsers()
    if not browsers:
        return ""
    default = _default_browser(selected_browser)
    options = []
    for app_id, name in browsers:
        sel = " selected" if app_id == default else ""
        options.append(f'<option value="{html.escape(app_id)}"{sel}>{html.escape(name)}</option>')
    return f"""
  <div class="field-group">
    <label class="field-label" for="gridge-browser-select">Browser</label>
    <select name="browser" id="gridge-browser-select">{''.join(options)}</select>
  </div>"""


def _url_tab_panel_html(query="", couch_mode=False, browser=""):
    resolved = service_resolver.resolve(query) if query else None

    # Couch Mode only makes sense for the plain youtube.com site --
    # hidden otherwise, matching gui.py's own couch_mode_row visibility
    # (Adw.SwitchRow(..., visible=False), only shown once the resolved
    # URL is youtube.com, not tv.youtube.com or a youtu.be link).
    couch_row = ""
    is_youtube = bool(resolved and resolved.url and resolved.url.removeprefix("https://").removeprefix("http://").removeprefix("www.") == "youtube.com")
    if is_youtube:
        checked = "checked" if couch_mode else ""
        couch_row = f"""
    <div class="switch-row">
      <label><input type="checkbox" name="couch_mode" {checked}> Couch Mode (YouTube TV interface)</label>
    </div>"""

    hint = ""
    if resolved:
        if resolved.warning:
            hint = f'<div class="hint-row warning"><span class="info-icon">!</span><span>{html.escape(resolved.warning)}</span></div>'
        elif resolved.url:
            shown = resolved.url.removeprefix("https://").removeprefix("http://")
            hint = f'<div class="hint-row"><span class="info-icon">i</span><span>Shortcut for {html.escape(shown)} will be added</span></div>'

    # Clear is a real link (not a type=reset button): reset only restores
    # a field to its HTML value attribute, which after a search *is* the
    # current query -- clicking it did nothing visible, reported as
    # "clear fields not working". A link back to / with the field
    # actually absent is a real clear, still zero-JS.
    return f"""
  <div class="field-group">
    <label class="field-label">Streaming service or URL</label>
    <div class="field-with-clear">
      <input type="text" name="q" value="{html.escape(query)}" placeholder="e.g. Netflix" required autofocus>
      <a href="/" class="field-clear-btn" title="Clear">&#10005;</a>
    </div>
  </div>{couch_row}
  {hint}
  {_browser_select_html(browser)}"""


_FORM_TABS = [("tab-url", "URL"), ("tab-apps", "Apps"), ("tab-chimera", "Chimera"), ("tab-emulators", "Emulators")]


def _tab_bar_html():
    # Segmented control switching the four shortcut-source tabs. CSS-only
    # (radio hack): the radios sit flat alongside .tab-bar and .tab-panels
    # (not nested inside either) so the general-sibling selectors in the
    # stylesheet (#tab-url:checked ~ .tab-bar label[for="tab-url"], etc.)
    # can reach both the matching label and the matching panel from a
    # single :checked radio. Only the URL tab is functional for now;
    # Apps/Chimera/Emulators are placeholders per the design handoff.
    radios = "".join(
        f'<input type="radio" name="gridge-form-tab" id="{tab_id}" class="tab-radio"{" checked" if tab_id == "tab-url" else ""}>'
        for tab_id, _label in _FORM_TABS
    )
    labels = "".join(f'<label for="{tab_id}" class="tab-label">{label}</label>' for tab_id, label in _FORM_TABS)
    return radios + f'<div class="tab-bar">{labels}</div>'


# Generous fixed count, not a computed fit: row height is fixed/compact
# (not flex-grown per row -- that was the earlier "one giant row" bug),
# so this just needs to be enough to fill any reasonably tall column;
# .card's own overflow-y:auto quietly clips/scrolls any excess.
_PLACEHOLDER_ROW_COUNT = 30


def _placeholder_matches_html():
    rows = ['<div class="placeholder-row"></div>' for _ in range(_PLACEHOLDER_ROW_COUNT)]
    return f'<div class="boxed-list">{"".join(rows)}</div>'


def _sgdb_search_bar_html(query, couch_mode, browser, sgdb_q):
    # Always-visible override search (matches the design handoff's
    # column-2 "SGDB search" pill) rather than the earlier magnifying-
    # glass reveal -- lets a user search SteamGridDB directly,
    # independent of whatever the URL/service field resolves to, while
    # /add still uses the original resolved URL for the shortcut itself.
    # Pre-filled with the term actually driving the current results
    # (the explicit override if there is one, else whatever the URL/
    # service field itself resolved to) rather than sitting empty until
    # touched -- same behavior planned for the Apps/Chimera/Emulators
    # tabs once they're built out, not just the URL tab.
    if sgdb_q:
        display_term = sgdb_q
    else:
        resolved = service_resolver.resolve(query) if query else None
        display_term = resolved.name if resolved and resolved.name else ""
    clear_href = f"/search?{_state_qs(query, couch_mode, browser)}"
    return f"""
<form action="/search" method="get">
  {_hidden_state_fields(query, couch_mode, browser)}
  <div class="field-with-clear">
    <input type="text" name="sgdb_q" value="{html.escape(display_term)}" placeholder="SGDB search">
    <a href="{clear_href}" class="field-clear-btn" title="Clear">&#10005;</a>
  </div>
</form>
"""


def _match_list_html(query, couch_mode, browser, sgdb_q, matches, match_index):
    # Plain links, not radio+submit-button: clicking one navigates
    # straight to that match's artwork (a real GET, no JS needed) --
    # a radio selection alone doesn't submit anything by itself, which
    # read as "artwork doesn't change when I pick a different match".
    rows = []
    qs = _state_qs(query, couch_mode, browser, sgdb_q=sgdb_q)
    for i, m in enumerate(matches):
        selected = " selected" if i == match_index else ""
        rows.append(
            f'<a class="{selected.strip()}" href="/search?{qs}&match_index={i}">{html.escape(m["name"])}</a>'
        )
    return f'<div class="boxed-list">{"".join(rows)}</div>'


def _middle_column_html(query, couch_mode, browser, sgdb_q, matches, match_index):
    list_html = _match_list_html(query, couch_mode, browser, sgdb_q, matches, match_index) if matches else _placeholder_matches_html()
    return f"""
<div class="card">
  {_sgdb_search_bar_html(query, couch_mode, browser, sgdb_q)}
  <h2>SGDB matches</h2>
  {list_html}
</div>
"""


def _artwork_picker_html(candidates_by_category):
    # Always renders all 5 categories, even with zero candidates
    # (candidates_by_category can be {}) -- shown before any search
    # too, so the right column never collapses to a placeholder message
    # and always occupies its full share of the row.
    sections = []
    for basename, title, _fetch, base_w, base_h in ARTWORK_CATEGORIES:
        candidates = candidates_by_category.get(basename) or []
        if not candidates:
            sections.append(f"""
<div class="artwork-category">
  <h3>{html.escape(title)}</h3>
  <span class="artwork-empty">No {html.escape(title.lower())} available.</span>
</div>""")
            continue
        cell_w = round(base_w * _ARTWORK_SCALE)
        # clamp() keeps categories responsive: cells shrink to fit
        # narrower screens (down to a 70px floor) instead of forcing
        # horizontal scrolling or overflowing, and aspect-ratio (not a
        # fixed height) keeps each category's real proportions at
        # whatever width it lands on.
        cell_style = f"width:clamp(70px, {round(cell_w / 12)}vw, {cell_w}px); aspect-ratio: {base_w} / {base_h};"
        cells = []
        for i, cand in enumerate(candidates):
            checked = "checked" if i == 0 else ""
            input_id = f"art-{basename}-{i}"
            thumb = html.escape(cand.get("thumb") or cand["url"])
            url = html.escape(cand["url"])
            cells.append(f"""
<div class="artwork-cell">
  <input type="radio" id="{input_id}" name="artwork_{basename}" value="{url}" form="{_ADD_FORM_ID}" {checked}>
  <label for="{input_id}" style="{cell_style}">
    <img src="{thumb}" loading="lazy" alt="">
  </label>
</div>""")
        sections.append(f"""
<div class="artwork-category">
  <h3>{html.escape(title)}</h3>
  <div class="artwork-row">{''.join(cells)}</div>
</div>""")
    return "".join(sections)


def render_page(query="", couch_mode=False, browser="", sgdb_q="", matches=None, match_index=0,
                 candidates_by_category=None, resolved_url=None, chosen=None):
    """Single page-builder for every state (home, unresolved input, no
    matches, a real workspace) -- all three columns are always present
    and always fully populated (placeholders when empty), rather than
    each state having its own bespoke partial layout."""
    matches = matches or []
    candidates_by_category = candidates_by_category or {}

    add_form = ""
    # Always present and pinned to the bottom, per the design handoff --
    # inert (not tied to any form) until a match/artwork exists to add.
    add_button = '<button type="button" disabled style="opacity:0.45;cursor:not-allowed">Create Steam Shortcut</button>'
    if chosen is not None:
        couch_field = '<input type="hidden" name="couch_mode" value="1">' if couch_mode else ""
        # The Add form is declared standalone (no visible children) and
        # everything that belongs to it -- the button, the artwork
        # radios -- is associated via form="..." instead of DOM
        # nesting. It must NOT visually wrap the URL tab panel's own
        # <form action="/search">: a <form> nested inside another
        # <form> is invalid HTML, and browsers resolve that by silently
        # merging the inner form's fields/buttons into the outer one --
        # confirmed live, this made clicking "Search" submit /add (with
        # stale data) instead of actually searching.
        add_form = f"""
<form id="{_ADD_FORM_ID}" action="/add" method="post">
  <input type="hidden" name="query" value="{html.escape(query)}">
  <input type="hidden" name="match_index" value="{match_index}">
  <input type="hidden" name="match_name" value="{html.escape(chosen['name'])}">
  <input type="hidden" name="resolved_url" value="{html.escape(resolved_url or '')}">
  <input type="hidden" name="browser" value="{html.escape(_default_browser(browser))}">
  {couch_field}
</form>
"""
        add_button = f'<button type="submit" form="{_ADD_FORM_ID}">Create Steam Shortcut</button>'

    left = f"""
<div class="card">
  {_tab_bar_html()}
  <div class="tab-panels">
    <div class="tab-panel tab-panel-url">
      <form action="/search" method="get" style="display:flex;flex-direction:column;gap:0.9rem">
        {_url_tab_panel_html(query, couch_mode, browser)}
      </form>
    </div>
    <div class="tab-panel tab-panel-apps"><div class="coming-soon">Apps (Flathub/Installed) -- coming soon</div></div>
    <div class="tab-panel tab-panel-chimera"><div class="coming-soon">Chimera platforms -- coming soon</div></div>
    <div class="tab-panel tab-panel-emulators"><div class="coming-soon">Emulators -- coming soon</div></div>
  </div>
  <div class="gridge-spacer"></div>
  {add_button}
</div>
"""
    middle = _middle_column_html(query, couch_mode, browser, sgdb_q, matches, match_index)
    right = f'<div class="card artwork-card">{_artwork_picker_html(candidates_by_category)}</div>'
    return render(f"""
{add_form}
<div class="gridge-columns">
  <div class="gridge-left">{left}</div>
  <div class="gridge-middle">{middle}</div>
  <div class="gridge-right">{right}</div>
</div>
""")


def render_login(error=None):
    error_html = f'<p style="color:#c00">{html.escape(error)}</p>' if error else ""
    return render(f"""
<div class="card" style="max-width:360px;margin:2rem auto">
  <h2>Enter the code</h2>
  <p>A 6-character code is shown on the TV. It's only displayed there --
  this proves you can see the screen, so no password to remember.</p>
  {error_html}
  <form id="gridge-login-form" action="/login" method="post">
    <input type="text" name="code" id="gridge-login-code" required autofocus maxlength="6"
           style="text-transform:uppercase; text-align:center; font-size:1.6rem; letter-spacing:0.4rem">
  </form>
  <script>
  // Second deliberate JS exception (after the dark-mode toggle), same
  // reasoning: auto-submitting on the 6th character typed has no
  // pure-HTML equivalent. Wrong code -> the page reloads with an error
  // and an empty field (server-side, see render_login's error path)
  // -- so this naturally loops until the right code goes in, no extra
  // logic needed here.
  document.getElementById("gridge-login-code").addEventListener("input", function (e) {{
    e.target.value = e.target.value.toUpperCase();
    if (e.target.value.length === 6) document.getElementById("gridge-login-form").submit();
  }});
  </script>
</div>
""")


def render_done(name, ok, error=None):
    if ok:
        body = f"""
<div class="card" style="max-width:420px;margin:2rem auto">
  <h2 style="color:var(--success)">Done</h2>
  <p><strong>{html.escape(name)}</strong> was added. Steam has restarted -- it should
  show up in your library now.</p>
  <a class="btn" href="/" style="display:inline-block;text-decoration:none">Add another</a>
</div>
"""
    else:
        body = f"""
<div class="card" style="max-width:420px;margin:2rem auto">
  <h2 style="color:#c00">Failed</h2>
  <p>Couldn't add <strong>{html.escape(name)}</strong>: {html.escape(str(error))}</p>
  <a class="btn secondary" href="/" style="display:inline-block;text-decoration:none">Back</a>
</div>
"""
    return render(body)


def _fetch_candidates(game_id):
    return {basename: fetch(game_id) for basename, _title, fetch, _w, _h in ARTWORK_CATEGORIES}


class Handler(BaseHTTPRequestHandler):
    def _send_html(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location, set_cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _session_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        jar.load(raw)
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _is_authenticated(self):
        return auth.is_authenticated(self._session_token())

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/vendor/darkreader.js":
            # Unauthenticated on purpose: the login page itself needs
            # the dark-mode toggle to work too.
            with open(_DARKREADER_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/login":
            # Show the code whenever anyone lands on /login while not
            # authenticated -- whether they got here via the redirect
            # below, or navigated straight to /login themselves.
            if not self._is_authenticated():
                auth_display.ensure_shown()
            self._send_html(render_login())
            return

        if not self._is_authenticated():
            auth_display.ensure_shown()
            self._redirect("/login")
            return

        if parsed.path == "/":
            self._send_html(render_page())
            return

        if parsed.path == "/search":
            query = (params.get("q") or [""])[0].strip()
            couch_mode = bool(params.get("couch_mode"))
            browser = (params.get("browser") or [""])[0]
            sgdb_q = (params.get("sgdb_q") or [""])[0].strip()
            match_index = int((params.get("match_index") or ["0"])[0])
            if not query:
                self._send_html(render_page(browser=browser))
                return

            # Recognized service (e.g. "netflix" -> netflix.com) or a
            # literal URL resolves to a real URL and a canonical SGDB
            # search term here, exactly like gui.py's own
            # resolve_url_input() -- matches gui.py's stricter
            # behaviour too: unresolvable input (not a known service,
            # doesn't look like a URL) doesn't fall through to a raw
            # text search, since that's exactly what previously built
            # an invalid "https://netflix" (missing .com) shortcut URL.
            resolved = service_resolver.resolve(query)
            if not resolved.url:
                self._send_html(render_page(query, couch_mode, browser))
                return

            # sgdb_q (the magnifying-glass direct search) overrides
            # what SGDB is searched for, independent of what the URL
            # itself resolves to -- e.g. keep adding netflix.com while
            # picking artwork from an entirely different SGDB entry.
            if sgdb_q:
                matches = sgdb.search(sgdb_q)
            elif resolved.sgdb_id is not None:
                matches = [sgdb.get_game(resolved.sgdb_id)]
            else:
                matches = sgdb.search(resolved.name or create_webapp.clean_shortcut_name(query))
            if not matches:
                self._send_html(render_page(query, couch_mode, browser, sgdb_q))
                return
            match_index = min(match_index, len(matches) - 1)
            candidates = _fetch_candidates(matches[match_index]["id"])
            self._send_html(render_page(
                query, couch_mode, browser, sgdb_q, matches, match_index,
                candidates, resolved.url, chosen=matches[match_index],
            ))
            return

        self._send_html(render(f"<p>Not found: {html.escape(parsed.path)}</p>"), status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = urllib.parse.parse_qs(body)

        if parsed.path == "/login":
            submitted = (params.get("code") or [""])[0]
            token = auth.try_login(submitted)
            if token is None:
                self._send_html(render_login(error="Wrong or expired code -- check the TV for the current one."))
                return
            auth_display.dismiss()
            cookie = http.cookies.SimpleCookie()
            cookie[SESSION_COOKIE] = token
            cookie[SESSION_COOKIE]["path"] = "/"
            cookie[SESSION_COOKIE]["max-age"] = auth.SESSION_TTL
            self._redirect("/", set_cookie=cookie[SESSION_COOKIE].OutputString())
            return

        if not self._is_authenticated():
            auth_display.ensure_shown()
            self._redirect("/login")
            return

        if parsed.path != "/add":
            self._send_html(render("<p>Not found</p>"), status=404)
            return

        query = (params.get("query") or [""])[0]
        couch_mode = bool(params.get("couch_mode"))
        match_index = int((params.get("match_index") or ["0"])[0])
        match_name = (params.get("match_name") or [""])[0]
        url = (params.get("resolved_url") or [""])[0]
        browser = (params.get("browser") or [""])[0]
        if browser:
            config.set_last_browser(browser)

        # Re-resolve the same way /search did rather than a fresh raw
        # text search: a resolved.sgdb_id lookup produces a single-item
        # match list, and re-searching by raw query text here wouldn't
        # reproduce that list at all, breaking match_index for those.
        resolved = service_resolver.resolve(query)
        if resolved.sgdb_id is not None:
            matches = [sgdb.get_game(resolved.sgdb_id)]
        else:
            matches = sgdb.search(resolved.name or create_webapp.clean_shortcut_name(query))
        if not matches or match_index >= len(matches):
            self._send_html(render_done(match_name or query, ok=False, error="match no longer available, please search again"))
            return
        chosen = matches[match_index]

        if not url:
            self._send_html(render_done(chosen["name"], ok=False, error="couldn't resolve a URL for this shortcut, please search again"))
            return

        try:
            slug = create_webapp.slugify(chosen["name"])
            selections = {}
            for basename, _title, _fetch, _w, _h in ARTWORK_CATEGORIES:
                selection_url = (params.get(f"artwork_{basename}") or [None])[0]
                selections[basename] = {"url": selection_url} if selection_url else None
            asset_paths = create_webapp.download_selected_assets(slug, selections)

            def apply():
                create_webapp.register_steam_shortcut(chosen["name"], url, asset_paths, couch_mode=couch_mode)

            maintenance.run_with_steam_stopped(apply, message=f"Adding {chosen['name']}…")
            self._send_html(render_done(chosen["name"], ok=True))
        except Exception as e:  # noqa: BLE001 -- surfaced to the user, not swallowed
            self._send_html(render_done(chosen["name"], ok=False, error=e))

    def log_message(self, fmt, *args):
        print(f"[gridge-server] {self.address_string()} - {fmt % args}")


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Gridge Server listening on http://0.0.0.0:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
