#!/usr/bin/env python3
"""SelfSteam: headless web UI for adding Steam shortcuts from another
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
import json
import os
import re
import socket
import threading
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import auth_display
import browser_picker
import config
import create_webapp
import maintenance
import pending_queue
import multipart_upload
import retroarch_cores
import standalone_emulators
import service_resolver
import sgdb_client as sgdb
import steam_paths

PORT = int(os.environ.get("SELFSTEAM_SERVER_PORT", "8845"))
SESSION_COOKIE = "selfsteam_session"
REMEMBER_COOKIE = "selfsteam_remember"
_DARKREADER_PATH = os.path.join(os.path.dirname(__file__), "vendor", "darkreader.js")
_POSTER_FRAME_PATH = os.path.join(os.path.dirname(__file__), "vendor", "poster-frame.webp")
_NAME_FIELD_WAND_PATH = os.path.join(os.path.dirname(__file__), "vendor", "name-field-wand.webp")
_ADD_FORM_ID = "selfsteam-add-form"
_SEARCH_ICON_SVG = (
    '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="white" '
    'stroke-width="3" stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle>'
    '<line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>'
)
# Original artwork (not copied from anywhere) -- same crescent-moon/sun
# toggle *concept* as sites like dekudeals.com use, but their actual
# icon is a Font Awesome 6 Pro glyph (a paid, licensed icon font), so
# it isn't something to copy/bundle here. currentColor picks up
# .icon-btn-round's color, so it matches the other header icons.
_MOON_ICON_SVG = (
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">'
    '<path d="M20.5 14.7A8.6 8.6 0 0 1 9.3 3.5a.6.6 0 0 0-.7-.9A10 10 0 1 0 21.4 15.4a.6.6 0 0 0-.9-.7Z"></path></svg>'
)
_SUN_ICON_SVG = (
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.5"></circle>'
    '<path d="M12 2.5v2.5M12 19v2.5M4.6 4.6l1.8 1.8M17.6 17.6l1.8 1.8M2.5 12h2.5M19 12h2.5'
    'M4.6 19.4l1.8-1.8M17.6 6.4l1.8-1.8"></path></svg>'
)
_HEART_ICON_SVG = (
    '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor">'
    '<path d="M12 21s-6.7-4.35-9.3-8.2C.8 9.9 1.8 6.3 4.7 5c2.2-1 4.6-.2 5.9 1.6C11.9 8.4 12 8.6 12 8.6'
    's.1-.2 1.4-2C14.7 4.8 17.1 4 19.3 5c2.9 1.3 3.9 4.9 2 7.8C18.7 16.65 12 21 12 21Z"></path></svg>'
)
_BACK_ICON_SVG = (
    '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M15 5 7 12l8 7"></path></svg>'
)
# Standard "none/prohibited" pictograph (circle + diagonal slash) --
# generic enough it isn't anyone's particular icon set, safe to draw
# directly rather than needing to source one. Sized in % so it scales
# with whatever cell it lands in, from the small Icon category up to
# the much bigger Vertical Grid one.
_NO_ARTWORK_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="#9a9a9a" stroke-width="2" '
    'stroke-linecap="round" style="width:35%;height:35%">'
    '<circle cx="12" cy="12" r="9"></circle><line x1="6" y1="18" x2="18" y2="6"></line></svg>'
)
# Poster overlay icons (gallery/home page): edit, remove -- exact path
# from the design handoff's own HTML source (steam-webapp-creator/
# "Copy of Three-column UI draft"), not reconstructed from scratch.
# stroke="currentColor" so it follows .poster-icon-btn's own color
# instead of a hardcoded white.
_EDIT_NAME_ICON_SVG = (
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<path d="M12 20h9"></path><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"></path></svg>'
)
_TRASH_ICON_SVG = (
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<polyline points="3 6 5 6 21 6"></polyline>'
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>'
    '<path d="M10 11v6"></path><path d="M14 11v6"></path>'
    '<path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>'
)
# Real SVG (stroke-width/stroke-linecap control thickness and give
# exact centering) instead of the "x" text glyph used first -- a font
# glyph's own metrics/baseline made it look thin and slightly
# off-center inside the circle, not reliably fixable with CSS alone.
_X_ICON_SVG = (
    '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round">'
    '<path d="M5 5L19 19M19 5L5 19"></path></svg>'
)
# Real icon (from info-circle-svgrepo-com.svg, fill swapped to
# currentColor so it follows .info-icon's own color) rather than a
# CSS circle + "i" glyph -- same reasoning as _X_ICON_SVG's own switch
# away from a text glyph, an italic "i" character's own metrics never
# sat centered in a small circle no matter how it was nudged.
_INFO_ICON_SVG = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">'
    '<path d="M12 2C6.486 2 2 6.486 2 12s4.486 10 10 10 10-4.486 10-10S17.514 2 12 2zm0 18c-4.411 0-8-3.589-8-8s3.589-8 8-8 8 3.589 8 8-3.589 8-8 8z"/>'
    '<path d="M11 11h2v6h-2zm0-4h2v2h-2z"/></svg>'
)


def _info_tooltip_icon_html(tooltip_text):
    """Default inline "hover for info" icon next to a label -- native
    title tooltip (no JS needed), _INFO_ICON_SVG sized/positioned to sit
    inline with surrounding label text rather than dropping to its own
    line or looking vertically off. Use this for any future info icon
    instead of re-deriving the styling each time."""
    return (
        '<span style="cursor:help;display:inline-flex;align-items:center;justify-content:center;'
        'vertical-align:middle;position:relative;top:-0.15rem;color:var(--text-dim)" '
        f'title="{html.escape(tooltip_text)}">{_INFO_ICON_SVG}</span>'
    )


# (basename, display title, candidate-fetcher, cell width, cell height)
# -- basenames and the *relative* proportions between categories match
# gui.py's own ARTWORK_CATEGORIES exactly (170x255, 260x121, 320x104,
# 160x100, 100x100); actual on-screen size is computed per category in
# _artwork_picker_html from the real viewport height, not a fixed scale.
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
<title>SelfSteam</title>
<!--EXTRA_HEAD-->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* Palette/spacing/radii lifted from the "Three-column UI draft" design
   handoff (Claude Design mockup) -- draft placeholder tokens per its
   own README, reconciled here as the real values rather than kept
   pixel-for-pixel, but the shape (pill inputs, flat cards, segmented
   tab bar, pastel accent) is the deliberate target look. */
:root {
  --accent: #0095ff;
  --accent-text: #ffffff;
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
header.selfsteam-header {
  background: var(--card-bg); border-bottom: 1px solid var(--border);
  padding: 1.1rem 2rem; display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 1rem; flex: 0 0 auto;
}
.selfsteam-header-left { display: flex; align-items: center; gap: 1rem; }
.selfsteam-header-title strong { font-size: 1.2rem; font-weight: 700; letter-spacing: -0.01em; }
.selfsteam-header-actions { display: flex; gap: 0.6rem; align-items: center; }
.icon-btn-round {
  width: 3rem; margin: 0; padding: 0; height: 3rem; border-radius: 10px;
  border: none; background: var(--card-bg); color: var(--text-dim);
  display: flex; align-items: center; justify-content: center; font-size: 1.2rem; cursor: pointer;
  text-decoration: none;
}
/* Links back to the shortcut gallery (the real home page) from
   anywhere else in the app. */
.back-btn { width: 3.2rem; height: 3.2rem; }
.queue-actions { display: flex; align-items: center; gap: 0.6rem; }
.restart-btn {
  width: auto; margin: 0; padding: 0.65rem 1.3rem; border-radius: 20px; font-size: 0.9rem;
  background: #1a1a1a; color: #fff; white-space: nowrap;
  /* A near-black fill on a light page is exactly the kind of color
     Dark Reader's inversion tends to map close to its own dark-mode
     background -- confirmed live, the button became indistinguishable
     from the page once dark mode was on. A border isn't dependent on
     that fill/background transform at all, so the button stays
     visible regardless of what Dark Reader does with the background
     color. */
  border: 1px solid #4a4a4a;
}
.restart-btn:disabled { background: #d8d8d8; color: #9a9a9a; border-color: #d8d8d8; cursor: not-allowed; }
.queue-counter {
  width: 1.9rem; height: 1.9rem; flex: 0 0 auto; border-radius: 50%;
  background: var(--accent); color: #fff; display: flex; align-items: center; justify-content: center;
  font-size: 0.85rem; font-weight: 700; text-decoration: none;
}
.queue-counter.empty { background: #d8d8d8; color: #9a9a9a; }
.sgdb-key-badge {
  width: auto; margin: 0; padding: 0.5rem 1rem; border-radius: 20px; font-size: 0.8rem; font-weight: 700;
  display: inline-flex; align-items: center; gap: 0.4rem; cursor: pointer; text-decoration: none;
  background: var(--success-bg); border: 1px solid var(--success-border); color: var(--success-text);
}
.sgdb-key-badge.unverified {
  background: #fdf3d9; border-color: #f0d68a; color: #8a6d1a;
}
.steam-warning-banner {
  background: #fdf3d9; border-bottom: 1px solid #f0d68a; color: #8a6d1a;
  padding: 0.7rem 2rem; font-size: 0.9rem; text-align: center; flex: 0 0 auto;
}
/* min-height:0 everywhere down this flex chain is load-bearing: flex
   items default to min-height:auto, which refuses to shrink below
   their content size and lets real content (e.g. many artwork
   thumbnails) push main/.selfsteam-columns/the cards taller than the
   viewport -- which then cascades into align-items:stretch handing
   every column that same inflated height, so a column with only one
   real content row (flex:1) stretches it to fill that whole inflated
   height instead of a sane share of the actual screen. Setting
   min-height:0 lets each level actually respect its bounded height
   and overflow internally (see .card's overflow-y:auto) instead. */
main { width: 100%; padding: 1.25rem; flex: 1; min-height: 0; display: flex; flex-direction: column; }
/* flex-wrap deliberately off here: with wrap enabled, a flex line's
   cross size gets computed from its items' content instead of the
   container's own (bounded) height, so align-items:stretch silently
   stopped capping the columns and one tall column's real content
   (e.g. the artwork column's images) pushed the whole page taller.
   Wrapping only ever matters for the stacked mobile layout below,
   which sets its own rules including flex-wrap. */
.selfsteam-columns { display: flex; gap: 1rem; align-items: stretch; flex-wrap: nowrap; flex: 1; min-height: 0; }
.selfsteam-left, .selfsteam-middle, .selfsteam-right { display: flex; flex-direction: column; min-height: 0; }
.selfsteam-left, .selfsteam-middle { flex: 1 1 300px; min-width: 280px; }
.selfsteam-right { flex: 1.4 1 400px; min-width: 320px; }
/* Pins the Add button to the bottom of the left column regardless of
   how much is above it, as long as the column has real height to grow
   into -- which align-items:stretch on .selfsteam-columns guarantees. */
.selfsteam-spacer { flex: 1 1 auto; }
.card {
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: 12px; padding: 0.85rem; display: flex; flex-direction: column; gap: 0.9rem;
  flex: 1; min-height: 0; overflow-y: auto;
}
.card h2 { font-size: 0.85rem; font-weight: 700; color: var(--text); margin: 0; }
/* Groups a label with its own field so the form's larger inter-field
   gap (0.9rem, set on the URL tab's <form>) doesn't also apply between
   a label and the input right below it -- that read as titles being
   too far from their fields. */
.field-group { display: flex; flex-direction: column; gap: 0.35rem; }
label.field-label {
  display: block; font-size: 1.05rem; font-weight: 700; color: var(--text);
  letter-spacing: 0.01em; margin: 0;
}
.required-asterisk { color: #c00; }
input[type=text], select {
  width: 100%; padding: 0.8rem 1.1rem; font-size: 0.9rem; font-family: inherit;
  border: 1px solid var(--input-border); border-radius: 20px; background: #fff; color: var(--text);
  appearance: none; outline: none;
}
input[type=text]:focus, select:focus { border-color: var(--accent); }
/* Pairs the pill input with a separate circular search button beside
   it (not an icon glued inside the pill) -- a real type=submit, not
   just decoration, so there's now an explicit click target for
   searching instead of relying on Enter alone. */
.search-field-row { display: flex; align-items: center; gap: 0.6rem; }
.search-field-row .field-with-clear { flex: 1; min-width: 0; }
.field-with-clear {
  display: flex; align-items: center; background: #fff; border: 1px solid var(--input-border);
  border-radius: 20px; padding: 0 0.4rem 0 1.1rem;
}
.field-with-clear input[type=text] { flex: 1; min-width: 0; border: none; padding: 0.85rem 0; border-radius: 0; }
.field-with-clear input[type=text]:focus { border: none; }
/* Accent-filled circle, white icon -- not the bare emoji this used to
   be, since browsers render emoji glyphs in their own fixed colors
   regardless of CSS `color`, so an actual "white icon" needs a real
   SVG instead. */
.search-submit-btn {
  width: 2.9rem; height: 2.9rem; flex: 0 0 auto; margin: 0; padding: 0; border-radius: 50%;
  background: var(--accent); border: none; display: flex; align-items: center; justify-content: center;
  cursor: pointer;
}
.search-submit-btn:disabled { background: #d8d8d8; cursor: not-allowed; }
/* overflow:hidden on the pill container is the real fix here -- a
   disabled <input>'s own UA-drawn background is a plain rectangle that
   otherwise pokes out past the container's rounded corners at the two
   ends (confirmed live: showed up as small grey marks just outside the
   pill's left/right curve). appearance:none alone doesn't suppress it,
   since that swatch isn't part of the textfield "appearance" the
   property controls. */
.field-with-clear input[type=text]:disabled {
  color: var(--text-dim); cursor: not-allowed; background: transparent; border: none;
}
.field-with-clear:has(input[type=text]:disabled) { background: #f0f0f0; overflow: hidden; }
.field-clear-btn {
  width: 1.8rem; height: 1.8rem; flex: 0 0 auto; margin: 0; padding: 0; font-size: 0.9rem; line-height: 1;
  border-radius: 50%; background: transparent; color: #4a4a4a; border: none; text-decoration: none;
  display: flex; align-items: center; justify-content: center;
}
.name-field-icon {
  flex: 0 0 auto; width: 26px; height: 26px; margin-right: 0.5rem; object-fit: contain;
}
/* Hidden once the field actually has a name in it (typed or already
   auto-filled) -- it's a "this can be auto-filled" cue, not permanent
   decoration, so it'd just be clutter sitting next to real text.
   :placeholder-shown is false the moment there's a value, whether that
   value came from typing or from the server's own auto-fill. */
.field-with-clear:has(input:not(:placeholder-shown)) .name-field-icon { display: none; }
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
/* The list's own rounded corners (.boxed-list's overflow:hidden) clip
   flush against the first/last row -- without matching border-radius
   here too, a selected first/last row's square outline corners get cut
   off by that clip instead of following it. */
.boxed-list a:first-child { border-top-left-radius: 8px; border-top-right-radius: 8px; }
.boxed-list a:last-child { border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; }
/* Placeholder rows before any search -- reserves the middle column's
   space instead of it looking like an empty gap. */
.placeholder-row { flex: 0 0 auto; height: 2.3rem; }
.placeholder-row:nth-child(odd) { background: #ececec; }
.placeholder-row:nth-child(even) { background: #f3f3f3; }
/* Tightened chrome (padding/gaps/label size) vs the default .card,
   specifically to reclaim vertical space for the actual artwork --
   every pixel trimmed here is a pixel the category rows below get to
   flex-grow into instead, i.e. directly bigger tiles. */
/* Extra space here (between category blocks) only ever shows above
   the 2nd-5th categories -- flex gap doesn't add anything before the
   first item, so Vertical Grid stays flush with the card's own top
   padding while Horizontal Grid/Hero/Logo/Icon each get breathing
   room above their title. */
.artwork-card { gap: 0.65rem; padding: 0.7rem 1.15rem 0.35rem; }
/* Same title-to-content gap (0.35rem) as .field-group elsewhere, for
   consistency across all three columns -- gap instead of margin so it
   matches exactly rather than approximately. */
.artwork-category { display: flex; flex-direction: column; gap: 0.35rem; }
.artwork-category h3 { font-size: 0.85rem; font-weight: 700; margin: 0; color: var(--text); }
/* overflow-x set to anything but visible here (hidden normally, auto
   for .has-artwork below) has a real CSS Overflow spec consequence:
   the *computed* value of overflow-y gets silently forced to auto too
   -- confirmed live via actual computed styles that this can't be
   opted back out of by just also declaring overflow-y:visible (tried
   first; had no effect at all, the spec overrides the authored value
   at used-value computation time regardless). So overflow-y:auto here
   is unavoidable given overflow-x isn't visible -- the actual fix for
   "auto genuinely clipping/scrolling a row" is making sure no row's
   real content is ever taller than its own box in the first place
   (see the mobile @media block's own min-height:0 override, right
   next to this same row's --mobile-cell-height). */
.artwork-row { display: flex; gap: 0.7rem; overflow-x: hidden; padding-bottom: 0.05rem; }
/* Scrollable only once there's real artwork to scroll through -- the
   blank/skeleton state (no search run yet, or nothing found) has
   nothing behind the extra filler tiles (see _SKELETON_TILE_COUNTS'
   own comment on why there are more of them than typically fit), so
   letting that scroll just invited dragging through empty placeholder
   tiles for no reason. */
.artwork-row.has-artwork { overflow-x: auto; }
.artwork-cell { flex: 0 0 auto; }
.artwork-cell input[type=radio] { display: none; }
.artwork-cell label {
  display: flex; align-items: center; justify-content: center;
  border: 3px solid transparent; border-radius: 8px;
  cursor: pointer; overflow: hidden;
  /* Matches the blank-state skeleton color -- an earlier dark #3a3a3a
     version kept white/transparent logos visible more reliably, but
     read as "too black" against real (mostly non-white) artwork;
     explicit tradeoff accepted in favor of visual consistency with
     the skeleton placeholders. */
  background: var(--skeleton);
}
.artwork-cell input[type=radio]:checked + label { border-color: var(--accent); }
/* CONTAIN, not cover: cover crops to fill the box, which mangled
   irregularly-shaped artwork (logos especially -- a wide transparent
   logo showed up as a cropped square). gui.py's own picker uses
   Gtk.ContentFit.CONTAIN for exactly this reason; match it here. */
.artwork-cell img { display: block; width: 100%; height: 100%; object-fit: contain; }
/* Blank-state tiles before any search -- light gray skeletons (not the
   dark #3a3a3a real cells use, which exists specifically to keep white
   logos visible) matching the design handoff's own placeholder look. */
.artwork-skeleton { background: var(--skeleton); border-radius: 8px; }
.switch-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; }
/* Segmented tab bar (URL / Apps / RetroArch / Emulators): CSS :target,
   not a radio hack -- no page reload needed to switch tabs (:target is
   driven purely by the URL's own #fragment), and unlike a radio's
   :checked state, no browser has any "helpful" previous-value
   restoration behavior for :target, since it isn't form state at all.
   (A radio-hack version of this shipped first and looked correct in
   every direct test, including this project's own Chromium-based
   testing tool, but still broke in real Firefox -- confirmed Firefox
   restores a checked radio's state across navigation more aggressively
   than autocomplete="off" reliably suppresses. :target sidesteps the
   whole category of bug rather than chasing it further.) */
.tab-bar { display: flex; gap: 4px; background: var(--bg); border-radius: 12px; padding: 4px; }
.tab-label {
  flex: 1; padding: 0.6rem 0.25rem; border-radius: 9px; font-size: 1rem; font-weight: 600;
  text-align: center; cursor: pointer; color: var(--text-dim); text-decoration: none; display: block;
}
/* URL is the default-active tab (also what Apps/Emulators, with no
   content of their own yet, still fall back to showing) -- only
   switches away when a *different* tab's own #target marker is
   actually the current URL fragment. ~ .selfsteam-columns (a descendant
   combinator after it, not a direct sibling) since the #tab-X marker
   spans sit just above .selfsteam-columns (see _tab_bar_targets_html),
   so this same :target state can also reach the middle/right columns
   below (see .middle-panel-retroarch etc.), not just the left
   column's own .tab-bar/.tab-panels. */
.tab-bar a[href="#tab-url"] { background: #fff; color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,0.12); }
#tab-apps:target ~ .selfsteam-columns .tab-bar a[href="#tab-url"],
#tab-retroarch:target ~ .selfsteam-columns .tab-bar a[href="#tab-url"],
#tab-emulators:target ~ .selfsteam-columns .tab-bar a[href="#tab-url"] {
  background: transparent; color: var(--text-dim); box-shadow: none;
}
#tab-apps:target ~ .selfsteam-columns .tab-bar a[href="#tab-apps"],
#tab-retroarch:target ~ .selfsteam-columns .tab-bar a[href="#tab-retroarch"],
#tab-emulators:target ~ .selfsteam-columns .tab-bar a[href="#tab-emulators"] {
  background: #fff; color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
/* flex:1 on both so whichever tab is active can stretch to fill the
   card -- needed so a tab that wants its own content pinned to the
   bottom (RetroArch's Name field, right above Create Steam Shortcut)
   has real height to grow a spacer into. The URL tab doesn't use this
   (its own Name field sits near the top instead), so it just gets
   blank space below its content -- same net look as before, since the
   button was already being pushed to the card's bottom either way. */
.tab-panels { display: flex; flex-direction: column; flex: 1; min-height: 0; }
/* overflow-y:auto here (not just relying on .card's own) -- without
   it, content taller than the panel's flex-computed height doesn't
   get clipped, it just visually spills out past the panel's own box
   while the button (next sibling, outside .tab-panels) still lays out
   based on that box's short height -- confirmed live, the RetroArch
   tab's BIOS+ROM pickers together are tall enough to trigger exactly
   this, landing the button on top of/before its own Name field
   instead of below it. */
.tab-panel { display: none; flex-direction: column; gap: 0.9rem; flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; scrollbar-width: thin; scrollbar-gutter: stable; }
/* Firefox's own scrollbar-width covers it there; Chromium/WebKit need
   this instead -- both together make the scrollbar an always-visible,
   easy-to-notice thin bar rather than the OS's own auto-hide/overlay
   style, which is easy to miss entirely once there's enough content
   (multiple file pickers stacked) to actually need scrolling. */
.tab-panel::-webkit-scrollbar { width: 8px; }
.tab-panel::-webkit-scrollbar-track { background: transparent; }
.tab-panel::-webkit-scrollbar-thumb { background: var(--text-dim); border-radius: 4px; }
.tab-panel-url { display: flex; }
#tab-apps:target ~ .selfsteam-columns .tab-panels .tab-panel-url,
#tab-retroarch:target ~ .selfsteam-columns .tab-panels .tab-panel-url,
#tab-emulators:target ~ .selfsteam-columns .tab-panels .tab-panel-url { display: none; }
#tab-apps:target ~ .selfsteam-columns .tab-panels .tab-panel-apps,
#tab-retroarch:target ~ .selfsteam-columns .tab-panels .tab-panel-retroarch,
#tab-emulators:target ~ .selfsteam-columns .tab-panels .tab-panel-emulators { display: flex; }
/* Middle/right columns: URL's own content is the default (also what
   Apps falls back to showing, same as before RetroArch/Emulators had
   any content of their own) -- only switches away from it when that
   specific tab is the targeted one. */
.middle-panel-retroarch, .right-panel-retroarch,
.middle-panel-emulators, .right-panel-emulators { display: none; }
#tab-retroarch:target ~ .selfsteam-columns .middle-panel-url,
#tab-retroarch:target ~ .selfsteam-columns .right-panel-url { display: none; }
#tab-retroarch:target ~ .selfsteam-columns .middle-panel-retroarch,
#tab-retroarch:target ~ .selfsteam-columns .right-panel-retroarch { display: flex; }
#tab-emulators:target ~ .selfsteam-columns .middle-panel-url,
#tab-emulators:target ~ .selfsteam-columns .right-panel-url { display: none; }
#tab-emulators:target ~ .selfsteam-columns .middle-panel-emulators,
#tab-emulators:target ~ .selfsteam-columns .right-panel-emulators { display: flex; }
.coming-soon { color: var(--text-dim); font-size: 0.85rem; padding: 1rem 0; text-align: center; }
/* RetroArch tab: BIOS/ROM source toggles + embedded server file picker.
   Plain links, not a CSS-radio-hack -- that trick is client-side only
   and can't survive a page reload, but this toggle needs to be
   *remembered* across every other click (console change, folder
   navigation), which only a real server-tracked value can do. */
.source-toggle { display: flex; gap: 4px; background: var(--bg); border-radius: 12px; padding: 4px; }
.source-label { flex: 1; padding: 0.55rem 0.25rem; border-radius: 9px; font-size: 0.85rem; font-weight: 600;
  text-align: center; cursor: pointer; color: var(--text-dim); text-decoration: none; display: block; }
.source-label.active { background: #fff; color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,0.12); }
.breadcrumbs { font-size: 0.8rem; color: var(--text-dim); margin-bottom: 0.5rem; }
.breadcrumbs a { color: var(--accent); text-decoration: none; }
.breadcrumbs a:hover { text-decoration: underline; }
.folder-icon, .file-icon { flex: 0 0 auto; width: 1rem; text-align: center; }
/* Was tightened to 130px to avoid the tab itself needing to scroll --
   reverted taller now that scrolling in the tab is an accepted
   tradeoff (the panel itself scrolls cleanly, see .tab-panel's own
   overflow-y:auto), and a short list made browsing a real ROMs folder
   feel cramped. */
.picker-list { flex: 0 0 auto; max-height: 280px; overflow-y: auto; }
.selected-file-name {
  color: var(--success-text); font-weight: 600; font-size: 0.85rem; flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.remove-file-btn {
  flex: 0 0 auto; width: 1.4rem; height: 1.4rem; border-radius: 50%; background: #c00; color: #fff;
  display: flex; align-items: center; justify-content: center; text-decoration: none;
}
/* margin-left:auto pushes this to the far right of its label row
   regardless of what else is (or isn't) already there -- the required-
   asterisk row has nothing else claiming that space, and the selected-
   file row's own flex:1 filename span already eats the rest, so this
   still lands past the remove button either way. */
.upload-status {
  margin-left: auto; flex: 0 0 auto; align-items: center; gap: 0.3rem;
  color: var(--text-dim); font-size: 0.85rem; font-weight: 600; white-space: nowrap;
}
/* Pure CSS spinner (no JS needed for the animation) -- shown next to
   an artwork category's title while its SGDB search is still running
   (see the ra_resolved meta-refresh loading phase). */
@keyframes selfsteam-spin { to { transform: rotate(360deg); } }
.spinner {
  display: inline-block; width: 0.9rem; height: 0.9rem; margin-left: 0.4rem; vertical-align: -2px;
  border: 2px solid var(--skeleton); border-top-color: var(--accent); border-radius: 50%;
  animation: selfsteam-spin 0.8s linear infinite;
}
/* ::file-selector-button is a real, standard CSS pseudo-element for
   the browser's own "Choose File" button (Chromium/Firefox/Safari all
   support it) -- themed to match the app's pill inputs/buttons without
   any JS, unlike the native picker dialog itself (out of reach for any
   web page regardless). */
input[type=file] { width: 100%; padding: 0.5rem; font-size: 0.85rem; font-family: inherit; color: var(--text-dim);
  border: 1px solid var(--input-border); border-radius: 20px; background: #fff; }
input[type=file]::file-selector-button {
  padding: 0.55rem 1.1rem; margin-right: 0.7rem; border: none; border-radius: 20px;
  background: var(--accent); color: var(--accent-text); font-weight: 700; font-family: inherit;
  font-size: 0.85rem; cursor: pointer;
}
/* Shortcut gallery (the real home page: "/"). main's own flex column
   already scrolls the whole page here -- unlike the 3-column workspace,
   there's no reason to bound this to the viewport height, since a
   library of hundreds of shortcuts is expected to need real scrolling. */
.gallery-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; }
.gallery-header h2 { font-size: 1.3rem; margin: 0; }
/* Real grid, not flex-wrap -- auto-fill computes however many fixed-
   230px columns actually fit the viewport and reflows on resize, same
   behavior flex-wrap already gave for a normal left-to-right fill, but
   without flex-wrap's own quirk of leaving a short last row's items
   stretched/misaligned against the column grid the earlier rows
   established. minmax(230px, 230px), not (230px, 1fr) -- these cards
   are fixed-pixel-positioned inside (.poster-frame/.poster-art/
   .add-poster are all absolute-positioned to exact 230x270 coordinates,
   see .shortcut-poster's own comment), so letting the grid track
   stretch wider than 230px would leave the art not covering its own
   cell. */
.gallery-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 230px)); gap: 24px; }
/* Fixed pixel sizes throughout this poster, not responsive/aspect-ratio
   based -- scaled up (195x229 -> 230x270, same ~0.852 aspect ratio) from
   the design handoff's own exact coordinates (steam-webapp-creator/
   "Copy of Three-column UI draft"), not invented. The frame itself is
   now a real image (vendor/poster-frame.webp, user-supplied PNG
   re-exported as WebP for transparency + size) instead of a CSS
   clip-path -- the clip-path version was technically applying
   correctly but read as no chamfer at all in practice, since the icon
   cluster sitting on that exact corner hid the one visual cue (page
   background through the cut) that would have shown it. The image
   approach has no such gap: it's just always visibly there. */
.shortcut-poster { position: relative; width: 230px; height: 270px; flex: 0 0 auto; }
/* Queued for removal (not yet committed) -- Edit/Remove stay clickable
   (still a real /pending item, cancelable there), this is purely a
   visible "this is going away" signal on the gallery itself. */
.shortcut-poster.pending-removal { opacity: 0.4; filter: grayscale(1); }
.poster-frame {
  position: absolute; left: 0; top: 0; width: 230px; height: 270px;
  background: url(/vendor/poster-frame.webp) no-repeat; background-size: 100% 100%;
}
.poster-art {
  position: absolute; left: 11px; top: 11px; width: 166px; height: 249px; border-radius: 8px;
  object-fit: cover; display: block; background: var(--skeleton);
}
.poster-art-noimg {
  display: flex; align-items: center; justify-content: center; text-align: center;
  padding: 12px; box-sizing: border-box;
}
.poster-art-noimg span { font-size: 0.95rem; font-weight: 600; color: var(--text-dim); word-break: break-word; }
.poster-icons { position: absolute; right: 7px; bottom: 7px; display: flex; flex-direction: column; gap: 7px; }
.poster-icon-btn {
  width: 33px; height: 33px; flex: 0 0 auto; margin: 0; padding: 0; border-radius: 7px;
  background: rgba(0,0,0,0.55); border: none; display: flex; align-items: center; justify-content: center;
  cursor: pointer; text-decoration: none; color: #fff;
}
.poster-icon-btn:disabled { opacity: 0.55; cursor: not-allowed; }
/* Outer box matches .shortcut-poster's own flex-item footprint exactly
   (230x270) so row alignment holds no matter where this tile sits in
   the grid -- confirmed live: with only the inner 166x249 box as the
   flex item (its old shape), moving this tile to the front of the grid
   made its shorter flex-item height throw off the whole first row's
   vertical alignment, not just this one tile. The visible "+" box
   itself stays inset the same way as a real poster's own artwork
   region (.poster-art's own left:11px/top:11px/166x249) -- the add
   card still has no blue frame graphic of its own, so aligning by
   outer edges alone would leave its "+" sitting visibly higher than
   every other poster's real content. */
.add-poster-frame { position: relative; width: 230px; height: 270px; flex: 0 0 auto; text-decoration: none; }
.add-poster {
  position: absolute; left: 11px; top: 11px; width: 166px; height: 249px; border-radius: 8px; background: var(--skeleton);
  display: flex; align-items: center; justify-content: center;
  color: var(--text-dim);
}
.add-poster-plus { font-size: 3.5rem; line-height: 1; font-weight: 300; }
/* Steam Deck-class widths (~1280px) still fit 3 columns side by side
   (the 960px breakpoint below is what actually stacks them), but
   min-width:280px/320px per column plus the base padding/gaps above
   left very little room for the cards' own content -- confirmed as
   the real complaint ("looked bad" on Deck specifically, not on a
   narrower phone-width screen where columns already stack). Tightens
   the same three paddings further without changing the layout itself. */
@media (max-width: 1400px) {
  main { padding: 0.75rem; }
  .selfsteam-columns { gap: 0.65rem; }
  .card { padding: 0.65rem; }
}
@media (max-width: 960px) {
  /* Stacked columns don't work with the bounded-height/internal-scroll
     trick above -- three independently-scrolling panels stacked
     vertically is worse than just letting the whole page scroll
     normally. Real mobile layout is still a later pass; this just
     keeps today's fix from making narrow viewports worse. */
  body { height: auto; min-height: 100vh; }
  main { min-height: auto; }
  .selfsteam-columns { align-items: flex-start; min-height: auto; flex-wrap: wrap; }
  .selfsteam-left, .selfsteam-middle, .selfsteam-right { flex-basis: 100%; min-height: auto; }
  .card { overflow-y: visible; }
  .selfsteam-spacer { flex: 0 0 0; }
  /* The header itself was never given a narrow-screen pass -- its own
     flex-wrap (needed on desktop for a very long hostname/page title)
     let .selfsteam-header-actions wrap unpredictably against
     .selfsteam-header-left on a real phone width, reading as
     misaligned icons rather than a deliberate layout. Rebuilt as 3
     explicit rows instead, via flex-wrap + flex-basis:100% on the two
     pieces that need their own line -- no HTML restructuring needed,
     since .queue-actions and .selfsteam-header-actions are already
     real elements, just forced onto their own full-width line each:
       1. back button + title (unchanged, stays put -- .selfsteam-
          header-left's own children fit on one line already)
       2. .queue-actions (the restart button + its counter), centered
       3. .selfsteam-header-actions (favorite/key badge/dark toggle),
          centered
     flex-basis:100% is what forces each onto a fresh line (nothing
     else fits alongside something that already claims the full row
     width), same trick .placeholder-row's own removal doesn't need but
     this does. */
  header.selfsteam-header { flex-wrap: wrap; padding: 0.8rem 1rem; gap: 0.6rem; }
  .selfsteam-header-left { flex-wrap: wrap; gap: 0.5rem; min-width: 0; flex: 1 1 auto; }
  .selfsteam-header-title { min-width: 0; overflow: hidden; flex: 1 1 auto; text-align: center; }
  .selfsteam-header-title strong {
    display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 1.05rem;
  }
  .queue-actions { flex: 1 1 100%; justify-content: center; }
  .selfsteam-header-actions { flex: 1 1 100%; justify-content: center; gap: 0.6rem; }
  .restart-btn { padding: 0.5rem 0.9rem; font-size: 0.8rem; }
  /* _PLACEHOLDER_ROW_COUNT (30 fixed rows) exists to fill a viewport-
     bound desktop column's real height -- once columns stack instead
     of sitting side by side, that column's height is just its own
     content, so the same 30 rows (30 * 2.3rem ≈ 69rem) turned into a
     genuinely excessive empty scroll before ever reaching the search
     box or real results below it. .placeholder-row only ever wraps
     empty skeleton rows (real match rows are <a> elements sharing the
     same .boxed-list, see _match_list_html/_em_list_rows/_ra_list_rows),
     so this hides only the placeholder skeleton, never real results. */
  .placeholder-row { display: none; }
  /* The desktop category/row flex-grow chain (see .artwork-category's
     own comment) needs *some* definite height upstream to distribute --
     .card's own flex:1 inside a viewport-bound .selfsteam-right no
     longer provides one once columns stack (flex-basis:100%;
     min-height:auto), so every category/row/cell in that chain
     collapsed to zero height. A fixed vh height + internal overflow-y
     scroll was tried here first, but that fights this project's own
     established mobile philosophy (see this block's very first comment
     above: "letting the whole page scroll normally" instead of nested
     independently-scrolling panels) and still cropped Logo/Icon in
     practice (confirmed live via screenshot -- min-height:60px per
     category pushing real content past whatever fixed vh was picked,
     no matter how generous).

     Simpler and more consistent instead: don't flex-grow at all here.
     !important overrides the inline flex/height styles those elements
     carry from the desktop render (category_style/row_style/cell_style
     in _artwork_picker_html) -- each category gets its own natural
     height (via --mobile-cell-height, a per-category CSS custom
     property set alongside data-artwork-ratio -- proportional to the
     same base_h weighting used everywhere else, not one flat height
     for every category, which flattened Vertical Grid down to Icon's
     own size instead of staying visibly the biggest/most prominent
     category the way it is in every other state), the row's real
     height comes from its tallest child the same way any other block
     content would, and the whole page scrolls to reach Logo/Icon like
     every other mobile card already does. selfsteamSizeArtworkCells
     still runs and still works correctly here -- it reads each row's
     real clientHeight (now content-driven instead of percentage-of-
     flex-parent) to set pixel widths, no changes needed there at all. */
  .artwork-card { height: auto; flex: none; overflow-y: visible; }
  .artwork-category { flex: none !important; min-height: auto !important; }
  .artwork-row { flex: none !important; height: auto !important; }
  /* .artwork-cell label too, not just .artwork-cell itself -- confirmed
     live via screenshot with real search results: only skeleton tiles
     (.artwork-skeleton) apply cell_style directly to .artwork-cell, so
     only those actually picked up the override above. The "none" cell
     and every real-candidate cell instead wrap their sizing in a child
     <label> (cell_style lives on the label, see none_cell/the real-
     candidates loop in _artwork_picker_html) -- invisible with an
     all-skeleton blank state (one mis-sized "none" cell blended in),
     but broke completely once real results loaded (every cell is
     label-wrapped, so none of them picked up --mobile-cell-height,
     all falling back to the old desktop height:100% of an ancestor
     that's no longer flex-grown, collapsing unpredictably). */
  /* min-height:0 too, not just height -- confirmed live via actual
     computed styles (getComputedStyle) that overflow-y:visible cannot
     actually be declared back in once overflow-x is non-visible
     (.artwork-row's own overflow-x:hidden/auto): per the CSS Overflow
     spec, the *computed* value of overflow-y is forced to auto
     whenever overflow-x isn't visible, regardless of what's explicitly
     authored for overflow-y -- fighting that coercion is a dead end.
     The actual fix is making sure content never overflows the row's
     box in the first place: cell_style's own inline min-height:60px
     (untouched by the height override above) was taller than the
     smaller categories' real mobile height (Logo/Icon at 55-57px),
     which is exactly what auto had real content to clip/scroll. */
  .artwork-cell, .artwork-cell label { height: var(--mobile-cell-height, 140px) !important; min-height: 0 !important; }
}
</style></head><body>
<header class="selfsteam-header">
  <div class="selfsteam-header-left">
    <!--BACK_BTN-->
    <div class="selfsteam-header-title">
      <strong><!--PAGE_TITLE--></strong>
    </div>
    <!--QUEUE_ACTIONS-->
  </div>
  <div class="selfsteam-header-actions">
    <button class="icon-btn-round" type="button" title="Favorite" style="color:#e0568c"><!--HEART_ICON--></button>
    <!--SGDB_KEY_BADGE-->
    <button id="selfsteam-dark-toggle" class="icon-btn-round" type="button" title="Toggle dark mode"><!--DARK_ICON--></button>
  </div>
</header>
<!--STEAM_WARNING-->
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
  var KEY = "selfsteam-dark-mode";
  var MOON_SVG = "<!--MOON_SVG_JS-->";
  var SUN_SVG = "<!--SUN_SVG_JS-->";
  var btn = document.getElementById("selfsteam-dark-toggle");
  var enable = function () { DarkReader.enable({brightness: 100, contrast: 100, sepia: 0}); btn.innerHTML = SUN_SVG; };
  var disable = function () { DarkReader.disable(); btn.innerHTML = MOON_SVG; };
  if (localStorage.getItem(KEY) === "1") enable(); else disable();
  btn.addEventListener("click", function () {
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
<script>
// Fourth deliberate JS exception (after dark mode, login auto-submit,
// and the RetroArch console auto-submit): the RA Upload/host toggle
// used to be a real navigation link, which meant a full-page reload --
// and visible flicker -- just to flip which of two already-rendered
// panels is showing. The choice itself still lives in real server
// state (ra_romsource/ra_biossource, part of _RA_STATE_KEYS) so it
// survives an actual reload same as everything else on the tab; this
// only handles the *instant* visual flip for a same-page click.
function selfsteamToggleSource(prefix, mode, stateKey) {
  var upload = document.getElementById(prefix + "-upload-panel");
  var local = document.getElementById(prefix + "-local-panel");
  var uploadLabel = document.getElementById(prefix + "-upload-label");
  var localLabel = document.getElementById(prefix + "-local-label");
  upload.style.display = mode === "upload" ? "" : "none";
  local.style.display = mode === "upload" ? "none" : "";
  uploadLabel.className = mode === "upload" ? "source-label active" : "source-label";
  localLabel.className = mode === "upload" ? "source-label" : "source-label active";
  // Keeps the *next* real navigation (changing console/emulator, which
  // auto-submits its own <form> with hidden ...source fields baked in
  // at page-load time) in sync with a toggle click that happened after
  // that load -- without this, switching right after toggling would
  // silently submit the stale, pre-toggle source. prefix is always
  // "<tab>-<field>-source" (e.g. "ra-rom-source", "em-bios-source"),
  // so its own leading tab abbreviation picks the right console-form's
  // hidden field without this needing a tab-specific version of itself.
  var tabPrefix = prefix.split("-")[0];
  var hidden = document.getElementById(tabPrefix + "-console-form-" + stateKey);
  if (hidden) hidden.value = mode;
}

// Called directly from the file input's own onchange, right before it
// triggers the form's submit() -- a multi-hundred-MB ROM over real wifi
// can take a while, and the browser gives zero visible feedback of its
// own during that (the previous page just sits there). Runs
// synchronously before the browser starts navigating away, so it stays
// visible for the whole upload; the eventual redirect (see
// _handle_ra_upload) replaces the page outright once it's done.
function selfsteamShowUploading(prefix) {
  var status = document.getElementById(prefix + "-upload-status");
  if (status) status.style.display = "inline-flex";
}

// Fires on the Add form's own submit (not the button's click -- see the
// form's own onsubmit comment in _emulators_tab_panel_html's caller for
// why) when an Emulators-tab shortcut is ready -- installing a Flathub/
// AppImage emulator on first use is a real, sometimes-slow blocking
// step inside /add (see _add_standalone_emulator_shortcut), and the
// button otherwise gives zero feedback that the click registered while
// that runs. Skipped entirely when data-installed says it's already
// there (set from a real standalone_emulators.installed() check at
// render time) -- /add still re-checks that for real regardless, but
// that check alone is fast (one `flatpak info`), not the actual
// multi-minute first-time download this spinner exists for. "Downloading"
// is the one phase actually shown -- a single `flatpak install` call is
// one opaque blocking step with no real download-vs-install boundary we
// can observe (confirming a real percentage was already ruled out for
// the same reason), and download is genuinely almost all of its wall-
// clock time. A future binary/AppImage-type emulator (download an
// archive, then extract/chmod it -- a real, distinct second phase, the
// same shape retroarch_cores.py's own core install already has) is
// where switching this to "Installing" partway through would actually
// mean something.
// Broadened from an Emulators-tab-only "Downloading <emulator>" spinner
// (still shown for a genuine fresh Flatpak install, that one real case
// where /add's own blocking work is a multi-minute download) into a
// spinner shown on every Create click regardless of tab -- the URL and
// RetroArch tabs' own /add work (SGDB artwork fetch, then the full
// Steam stop/splash/write/restart maintenance cycle) is real, visible-
// on-a-slow-connection-or-slow-Steam-restart blocking time too, and
// previously gave zero feedback that the click registered at all.
function selfsteamShowCreating(form) {
  var button = document.getElementById("selfsteam-add-button");
  if (!button) return;
  button.disabled = true;
  if (form.dataset.emulator && !form.dataset.installed) {
    button.innerHTML = "Downloading " + form.dataset.emulator + '<span class="spinner"></span>';
  } else {
    button.innerHTML = "Creating Shortcut" + '<span class="spinner"></span>';
  }
}

// Fifth deliberate JS exception: every other RA-tab interaction that
// just changes which console/folder/file is picked (console select,
// breadcrumbs, folder/file rows, remove-file) was still a full-page
// reload -- a visible blink for what's conceptually a small in-place
// edit, same complaint the source toggle already got the no-reload
// treatment for. selfsteamTabFetch fetches the exact URL a real click
// would have navigated to, then swaps just the given ids' page regions
// in place instead of replacing the whole document -- shared by every
// tab that wants this (RA, then Emulators), parameterized by which
// element ids belong to that tab. Every element keeps its real href/
// onchange-driven navigation as a fallback -- if fetch ever fails (or
// JS is off entirely), it just behaves like a normal link/auto-submit
// again, nothing here is the only way to reach a URL.
// Shared by selfsteamTabFetch (GET, url known upfront) and selfsteamUploadFetch
// (POST, final url only known once fetch() has followed the upload's own
// redirect) -- both just want "swap these ids in from this HTML, then
// chase any <meta refresh> the same way a real browser would have".
// Splitting this out is what let the upload path below drop its real
// top-level navigation without duplicating the meta-refresh chase logic.
// Sets each artwork tile's width as a real pixel value computed from
// its row's own actual rendered height (data-artwork-ratio, set server-
// side per category) instead of leaving that derivation to CSS
// aspect-ratio -- see cell_style's own comment (selfsteam_server.py)
// for why: a flex item stretched to a percentage/flex-derived height
// with aspect-ratio deriving its width from that is a genuine cross-
// engine interop gap (Firefox/Zen showed a real gap after the "none"
// tile that Chromium-family browsers never did, on the exact same
// markup -- matches Mozilla bug 1658441 and the wider flexbugs history
// here). Once every cell has an explicit pixel width, aspect-ratio has
// no missing dimension left to derive, so it can't disagree anymore.
function selfsteamSizeArtworkCells() {
  document.querySelectorAll(".artwork-row[data-artwork-ratio]").forEach(function (row) {
    var ratio = parseFloat(row.getAttribute("data-artwork-ratio"));
    var h = row.clientHeight;
    if (!ratio || !h) return;
    var w = Math.round(h * ratio) + "px";
    row.querySelectorAll(".artwork-cell").forEach(function (cell) { cell.style.width = w; });
  });
}

function selfsteamApplySwap(htmlText, url, swapIds) {
  history.replaceState(null, "", url);
  var doc = new DOMParser().parseFromString(htmlText, "text/html");
  swapIds.forEach(function (id) {
    var next = doc.getElementById(id);
    var cur = document.getElementById(id);
    if (next && cur) cur.replaceWith(next);
  });
  // A freshly-picked ROM's response is the loading-skeleton page
  // (see the /new handler's ra_loading/em_loading branches) carrying
  // a <meta refresh> to the real, possibly-slow resolved URL -- a
  // real browser would follow that automatically; here the swap
  // above already showed its spinner, so this just fetches the
  // follow-up itself instead of waiting for an actual page reload.
  var meta = doc.querySelector('meta[http-equiv="refresh"]');
  var match = meta && /url=(.*)$/.exec(meta.getAttribute("content") || "");
  if (match) selfsteamTabFetch(match[1], swapIds);
  selfsteamSizeArtworkCells();
}

function selfsteamTabFetch(url, swapIds) {
  fetch(url)
    .then(function (r) { if (!r.ok) throw new Error("bad status"); return r.text(); })
    .then(function (htmlText) { selfsteamApplySwap(htmlText, url, swapIds); })
    .catch(function () { window.location.href = url; });
}

// Sixth deliberate JS exception: a file upload used to be a real
// this.form.submit() -- a genuine top-level navigation, since
// multipart/form-data with a real file needs a real request body fetch()
// can build too (via FormData), it just wasn't being used. That real
// navigation, followed by the upload handler's own 303 redirect and then
// (for a ROM) the loading page's own 0-delay meta-refresh, was three
// full-document loads back to back -- enough for a real, visible flash
// of the page's default tab before each one's :target CSS caught up,
// self-correcting a moment later but still visible. Routing the upload
// itself through fetch (which follows the 303 automatically, landing
// r.url on the real post-redirect page) collapses that to the same
// in-place swap every other tab interaction already gets, with the same
// graceful real-navigation fallback if fetch throws (JS half-broken,
// network weirdness) rather than leaving the picked file stuck nowhere.
function selfsteamUploadFetch(input, swapIds) {
  var form = input.form;
  var data = new FormData(form);
  fetch(form.getAttribute("action"), { method: "POST", body: data })
    .then(function (r) { if (!r.ok) throw new Error("bad status"); return r.text().then(function (t) { return {text: t, url: r.url}; }); })
    .then(function (result) { selfsteamApplySwap(result.text, result.url, swapIds); })
    .catch(function () { form.submit(); });
}

var SELFSTEAM_RA_SWAP_IDS = [
  "selfsteam-ra-tab-panel", "selfsteam-ra-middle", "selfsteam-ra-right",
  "selfsteam-add-form-slot", "selfsteam-add-button",
];

function selfsteamRaFetch(url) { selfsteamTabFetch(url, SELFSTEAM_RA_SWAP_IDS); }

function selfsteamRaNav(a) {
  selfsteamRaFetch(a.getAttribute("href"));
  return false;
}

function selfsteamRaFormNav(form) {
  var qs = new URLSearchParams(new FormData(form)).toString();
  var action = form.getAttribute("action").split("#")[0];
  selfsteamRaFetch(action + "?" + qs + "#tab-retroarch");
}

// Same reasoning as selfsteamEmEmulatorChanged's own comment -- a BIOS
// picked for the previously-selected console/core is tied to that
// core specifically, not the ROM, so it shouldn't show as "already
// selected" once a different console/core is chosen.
function selfsteamRaConsoleChanged(select) {
  var form = select.form;
  var el = form.elements["ra_biosfile"];
  if (el) el.value = "";
  selfsteamRaFormNav(form);
}

var SELFSTEAM_EM_SWAP_IDS = [
  "selfsteam-em-tab-panel", "selfsteam-em-middle", "selfsteam-em-right",
  "selfsteam-add-form-slot", "selfsteam-add-button",
];

function selfsteamEmFetch(url) { selfsteamTabFetch(url, SELFSTEAM_EM_SWAP_IDS); }

function selfsteamEmNav(a) {
  selfsteamEmFetch(a.getAttribute("href"));
  return false;
}

function selfsteamEmFormNav(form) {
  var qs = new URLSearchParams(new FormData(form)).toString();
  var action = form.getAttribute("action").split("#")[0];
  selfsteamEmFetch(action + "?" + qs + "#tab-emulators");
}

// Switching the Emulator picker to a different emulator, not just
// re-submitting the same one -- the bios/keys/firmware fields carried
// forward as hidden inputs in this same form are real file picks made
// for whichever emulator was previously selected (e.g. Ryubing's own
// prod.keys), which would otherwise show up as "already selected" for
// the new one too even when that's a completely different, wrong file
// format for it. ROM selection itself is left alone -- switching
// engines for the same already-picked game is the common case this is
// for, unlike the BIOS/keys/firmware picks, which really are tied to
// the specific emulator, not the game.
function selfsteamEmEmulatorChanged(select) {
  var form = select.form;
  ["em_biosfile", "em_bios2file", "em_bios3file", "em_keysfile", "em_firmwarefile"].forEach(function (name) {
    var el = form.elements[name];
    if (el) el.value = "";
  });
  selfsteamEmFormNav(form);
}

// Initial pass on real page load (not just after an AJAX swap, which
// selfsteamApplySwap's own call already covers), plus on resize since
// the category rows' real heights are flex-grow-derived from the
// viewport (see .artwork-category's own comment) and therefore change
// whenever the window does. Debounced -- a resize fires continuously
// while dragging, not once at the end.
window.addEventListener("load", selfsteamSizeArtworkCells);
// The RA/Emulators tabs' own artwork columns were sized once at page
// load while display:none (0 height, since the URL tab is the default
// :target) and never recalculated after that -- switching to either
// tab is a plain #fragment change (see _tab_bar_targets_html's own
// comment on why that's deliberately not a JS click handler), which
// doesn't run any JS on its own, so nothing ever re-measured them once
// they actually became visible. hashchange covers every way the
// fragment can change (a tab click, back/forward, a pasted link) in
// one place without touching that link markup at all. setTimeout(…, 0)
// defers past the current tick so the browser has actually applied the
// :target-driven display change before clientHeight is read -- reading
// it synchronously inside the same handler that changed the fragment
// isn't guaranteed to see the new layout yet.
window.addEventListener("hashchange", function () { setTimeout(selfsteamSizeArtworkCells, 0); });
(function () {
  var resizeTimer;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(selfsteamSizeArtworkCells, 150);
  });
})();
</script>
</body></html>"""


def _sgdb_key_badge_html():
    # Always a link to /key, verified or not -- letting it update
    # an already-configured key (not just add a missing one) is what a
    # user actually expects from a clickable status badge. Reverted back
    # to the original text-pill treatment (a circular icon-only version
    # using the key-lock artwork was tried and explicitly reverted --
    # the header's own mobile layout was fixed separately instead, see
    # the @media (max-width: 960px) block's own comment on the 3-row
    # header structure).
    if sgdb.has_api_key():
        return '<a href="/key" class="sgdb-key-badge">&#10003; SGDB API key verified</a>'
    return '<a href="/key" class="sgdb-key-badge unverified">&#9888; No SGDB API key</a>'


def _hostname():
    # Login and the shortcut gallery both show this instead of a fixed
    # title -- it's the fastest way to confirm from the login screen
    # alone that you've reached the right machine, useful the moment
    # there's more than one SelfSteam on the same network.
    return socket.gethostname()


def _steam_warning_html():
    # Sanity check, not a hard requirement -- on real SteamOS this can
    # never fire (Steam owns the machine), but SelfSteam itself is
    # plain Python with no dependency on Steam being installed at all,
    # so running it on a bare Linux box without Steam yet would
    # otherwise fail silently/late, deep inside /add or /commit instead
    # of up front where it's actually useful.
    try:
        steam_paths.find_steam_root()
        return ""
    except steam_paths.SteamNotFoundError:
        return '<div class="steam-warning-banner">&#9888; Steam not found on this machine -- shortcuts can\'t be created until it\'s installed.</div>'


def _queue_actions_html():
    # Always present, but disabled/greyed out with nothing queued --
    # native button[disabled] blocks the form submit itself, no JS
    # needed to keep an empty commit from doing anything. Also disabled
    # while a commit is already running in the background (see
    # _run_commit_in_background/render_restarting) -- pending_queue
    # isn't cleared until that background thread actually finishes, so
    # without this a second click on /restarting itself (still showing
    # a stale non-empty count) could fire a second, overlapping commit.
    with _commit_status_lock:
        commit_running = _commit_status["running"]
    n = 0 if commit_running else pending_queue.count()
    disabled = "" if n else " disabled"
    counter_class = "queue-counter" if n else "queue-counter empty"
    return f"""
<div class="queue-actions">
  <form action="/commit" method="post" style="margin:0">
    <button type="submit" class="restart-btn"{disabled}>Save changes and restart Steam</button>
  </form>
  <a href="/pending" class="{counter_class}" title="View queued changes">{n}</a>
</div>"""


def render(body, page_title="Add Steam Shortcut", show_back=True, extra_head=""):
    # Even when there's no back button (top-level pages), its slot in
    # the header still needs to take up the same space -- an invisible
    # placeholder of the same size, not an empty string, or the title
    # next to it shifts left to fill the gap instead of staying put.
    back_btn_html = (
        f'<a class="icon-btn-round back-btn" href="/" title="Back to shortcuts">{_BACK_ICON_SVG}</a>'
        if show_back else '<span class="icon-btn-round back-btn" style="visibility:hidden"></span>'
    )
    head = PAGE_HEAD.replace("<!--EXTRA_HEAD-->", extra_head)
    head = head.replace("<!--SGDB_KEY_BADGE-->", _sgdb_key_badge_html())
    head = head.replace("<!--DARK_ICON-->", _MOON_ICON_SVG)
    head = head.replace("<!--BACK_BTN-->", back_btn_html)
    head = head.replace("<!--HEART_ICON-->", _HEART_ICON_SVG)
    head = head.replace("<!--QUEUE_ACTIONS-->", _queue_actions_html())
    head = head.replace("<!--STEAM_WARNING-->", _steam_warning_html())
    head = head.replace("<!--PAGE_TITLE-->", html.escape(page_title))
    # json.dumps for a safe JS string literal (handles the SVG's own
    # quotes) rather than hand-escaping -- these two go inside a JS
    # "..." literal in PAGE_TAIL's <script>, not raw HTML.
    tail = PAGE_TAIL.replace("\"<!--MOON_SVG_JS-->\"", json.dumps(_MOON_ICON_SVG))
    tail = tail.replace("\"<!--SUN_SVG_JS-->\"", json.dumps(_SUN_ICON_SVG))
    return (head + body + tail).encode()


def _hidden_state_fields(query, couch_mode, browser, ra_state=None, em_state=None):
    fields = f'<input type="hidden" name="q" value="{html.escape(query)}">'
    if couch_mode:
        fields += '<input type="hidden" name="couch_mode" value="1">'
    if browser:
        fields += f'<input type="hidden" name="browser" value="{html.escape(browser)}">'
    # Carries any in-progress RetroArch/Emulators pick across a URL-tab-
    # only navigation like a search submit -- without this, every normal
    # URL tab action (typing a URL and hitting Search, picking a
    # different SGDB match, the SGDB override search) silently wiped
    # whatever was chosen on another tab, since /search never knew ra_*/
    # em_* existed. Confirmed live (for ra_*) as the real root cause of
    # "SGDB search field gone" reports that survived an earlier Clear-
    # button-only fix -- em_* gets the same treatment from the start.
    fields += _ra_hidden_fields(ra_state)
    fields += _ra_hidden_fields(em_state)
    return fields


def _ra_hidden_fields(state):
    # Genuinely generic despite the name (built for the RA tab first) --
    # just turns any flat string-keyed dict into hidden inputs, reused
    # as-is by the Emulators tab's own state too.
    if not state:
        return ""
    return "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(v)}">'
        for k, v in state.items() if v
    )


def _state_qs(query, couch_mode, browser, ra_state=None, em_state=None, **extra):
    qs = f"q={urllib.parse.quote(query)}"
    if couch_mode:
        qs += "&couch_mode=1"
    if browser:
        qs += f"&browser={urllib.parse.quote(browser)}"
    for key, value in extra.items():
        if value:
            qs += f"&{key}={urllib.parse.quote(str(value))}"
    # Same reasoning as _hidden_state_fields -- links built from this
    # (Clear, match rows, name-reset) must not drop RetroArch/Emulators
    # state either.
    if ra_state:
        ra_qs = _ra_qs(ra_state)
        if ra_qs:
            qs += f"&{ra_qs}"
    if em_state:
        em_qs = _em_qs(em_state)
        if em_qs:
            qs += f"&{em_qs}"
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
    <label class="field-label" for="selfsteam-browser-select">Browser <span style="color:var(--text-dim);font-weight:400;font-size:0.85rem">Flatpak</span></label>
    <select name="browser" id="selfsteam-browser-select">{''.join(options)}</select>
  </div>"""


def _url_tab_panel_html(query="", couch_mode=False, browser="", chosen=None, name_reset_href="/", ra_state=None, em_state=None):
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
    # Cross-populated the same moment the SGDB search box is (both
    # ultimately come from resolved.name), then refined to the exact
    # match's own name once one is chosen -- but a real, separate,
    # directly editable field rather than that search box doing double
    # duty as the saved name. Owned by the standalone Add form (see
    # render_page) via form="..." rather than DOM nesting -- lets it
    # live here, under the URL field, while still submitting with
    # Create Steam Shortcut instead of this form's own GET search.
    # sgdb.clean_sgdb_name strips SGDB's own "(Website)"/"(Program)"-
    # style tags here (the actual saved name), but NOT from the match
    # list itself (see _match_list_html) -- there, the same tag is a
    # useful disambiguator between same-named entries.
    name_default = sgdb.clean_sgdb_name(chosen["name"]) if chosen else (resolved.name if resolved and resolved.name else "")
    name_field = f"""
  <div class="field-group">
    <label class="field-label" for="selfsteam-name-field">Name</label>
    <div class="field-with-clear">
      <img class="name-field-icon" src="/vendor/name-field-wand.webp" alt="">
      <input type="text" name="match_name" id="selfsteam-name-field" form="{_ADD_FORM_ID}"
             value="{html.escape(name_default)}" placeholder="Shortcut name">
      <a href="{name_reset_href}" class="field-clear-btn" title="Reset to guessed name">&#10005;</a>
    </div>
  </div>"""

    # Only drops the URL tab's own state (q and everything derived from
    # it) -- previously this went to a bare /new unconditionally, which
    # also wiped any in-progress RetroArch/Emulators pick sitting in the
    # same query string, even though "Clear" here only reads as "clear
    # the field I'm looking at." Confirmed live (for ra_*) as what left
    # a genuinely bare /new#tab-retroarch (no console, nothing) when
    # that tab was clicked back into afterward -- not a rendering bug,
    # the state really was gone. em_* gets the same preservation.
    other_qs = "&".join(q for q in (_ra_qs(ra_state) if ra_state else "", _em_qs(em_state) if em_state else "") if q)
    clear_href = f"/new?{other_qs}" if other_qs else "/new"
    return f"""
  <div class="field-group">
    <label class="field-label">Streaming service or URL <span class="required-asterisk">*</span></label>
    <div class="search-field-row">
      <div class="field-with-clear">
        <input type="text" name="q" value="{html.escape(query)}" placeholder="e.g. Netflix or www.arte.tv" required autofocus>
        <a href="{clear_href}" class="field-clear-btn" title="Clear">&#10005;</a>
      </div>
      <button type="submit" class="search-submit-btn" title="Search">{_SEARCH_ICON_SVG}</button>
    </div>
  </div>
  {couch_row}
  {hint}
  {_browser_select_html(browser)}
  <div class="selfsteam-spacer"></div>
  {name_field}"""


# RetroArch tab: all its own state lives on /new's query string
# alongside (never colliding with) the URL tab's own q/sgdb_q/etc, all
# ra_-prefixed. Threaded through every link/form here so nothing resets
# on an unrelated click -- picking a ROM shouldn't forget you'd chosen
# "Upload" for BIOS, changing console shouldn't lose the folder you
# were browsing, etc.
_RA_STATE_KEYS = [
    "ra_console", "ra_rompath", "ra_romfile", "ra_biospath", "ra_biosfile",
    "ra_resolved", "ra_sgdb_q", "ra_romsource", "ra_biossource", "ra_bios_skip",
    "ra_sgdb_cleared", "ra_name_cleared",
    # Set only when this /new session started from the gallery's own
    # Edit link (see its own comment on edit_href) -- their presence is
    # what switches the Add button to "Save Shortcut" and, if the Name
    # field also changes before submitting, is what lets the submit
    # handler clean up the shortcut being replaced instead of leaving it
    # behind as a duplicate (add_shortcut's own appname-based dedup only
    # catches that automatically when the name stays the same).
    "ra_edit_appid", "ra_edit_name",
]
_HOME_DIR = os.path.expanduser("~")
# The local file-picker's sandbox boundary is the real filesystem root,
# not just the home dir -- deliberately widened (was os.path.expanduser
# ("~")) so ROMs on an external drive mounted under /mnt or /run (a USB
# stick or SD card, e.g. on a Steam Machine's removable media) are
# actually reachable, not just whatever lives under the user's own home.
# _ra_safe_join's realpath+containment check still runs (guards against
# ".." tricks), it just has nothing left to meaningfully exclude once
# the boundary itself is "/" -- that's the whole point.
_RA_ROOT = "/"
# Where a picker with no navigation state yet (state.get(path_key) is
# falsy -- never set, or explicitly cleared) actually starts looking --
# see _ra_resolve_relpath. Widening _RA_ROOT to "/" would otherwise mean
# a completely fresh picker landed on a bare "/" directory listing
# (system dirs, no games in sight) instead of the one place a user's own
# files actually are by default.
_RA_DEFAULT_RELPATH = os.path.relpath(_HOME_DIR, _RA_ROOT)


def _ra_resolve_relpath(raw):
    # raw == "" covers two different real cases that both want the same
    # default: a genuinely fresh picker (path_key was never set), and
    # _ra_qs's own serialization, which omits falsy values entirely --
    # so an explicitly-cleared path_key is indistinguishable from never-
    # set once it round-trips through a URL. Both should land on the
    # home dir, not literal "/".
    #
    # True root is reachable a different way on purpose: the root
    # breadcrumb (_ra_breadcrumbs_html) links with path_key="/" -- a
    # real, truthy, single-character rel_path that survives
    # serialization -- which _ra_safe_join's own rel_path.lstrip("/")
    # then resolves back to _RA_ROOT itself. Same "explicit override vs.
    # unset" pattern already used elsewhere in this file (ra_bios_skip,
    # ra_sgdb_cleared, etc.), just for a path instead of a flag.
    if not raw:
        return _RA_DEFAULT_RELPATH
    if raw == "/":
        return ""
    return raw


# Under _HOME_DIR (not the now-much-wider _RA_ROOT) on purpose --
# uploaded files just become another real path the existing local-picker
# sandbox (_ra_safe_join) already handles, no special-casing needed once
# they land here; there's no reason for SelfSteam's own upload landing
# spot to move just because browsing itself got wider.
#
# "gridge", not "selfsteam" -- deliberately NOT renamed alongside the
# rest of this app's SelfSteam rebrand. Shortcuts already created before
# that rename have this exact absolute path baked into their own
# LaunchOptions (the ROM/BIOS/keys/firmware path RetroArch or the
# emulator was told to load); moving the directory a real file already
# lives under would break every one of them the moment Steam next tried
# to launch it. New uploads keep landing here too, alongside old ones --
# there's no separate "new" location to migrate to without the same
# problem recurring on the next rename.
_RA_UPLOAD_DIR = os.path.join(_HOME_DIR, ".local", "share", "gridge", "uploads")


def _ra_state_from_params(params):
    return {key: (params.get(key) or [""])[0] for key in _RA_STATE_KEYS}


def _ra_qs(state, **overrides):
    merged = dict(state)
    merged.update(overrides)
    return "&".join(f"{k}={urllib.parse.quote(str(merged[k]))}" for k in _RA_STATE_KEYS if merged.get(k))


def _ra_url(path, state, **overrides):
    # #tab-retroarch at the very end -- a URL fragment always has to be
    # the last thing on the URL (anything appended after it becomes
    # part of the fragment text, not a new query param), so this is the
    # one place that appends it rather than baking it into _ra_qs
    # itself, which some callers (the upload form's action=) still need
    # to concatenate an extra query param onto afterward.
    return f"{path}?{_ra_qs(state, **overrides)}#tab-retroarch"


def _ra_safe_join(rel_path):
    candidate = os.path.realpath(os.path.join(_RA_ROOT, rel_path.lstrip("/")))
    root_real = os.path.realpath(_RA_ROOT)
    # root_real == "/" (os.sep) needs special-casing here -- root_real +
    # os.sep would otherwise double up into "//", which no real path
    # (other than a literal, never-occurring "//...") ever starts with,
    # so every subpath of true root would wrongly fail containment the
    # moment _RA_ROOT was widened to "/" instead of the home dir.
    root_prefix = root_real if root_real == os.sep else root_real + os.sep
    if candidate != root_real and not candidate.startswith(root_prefix):
        return None
    return candidate


def _ra_breadcrumbs_html(rel_path, state, path_key):
    # onclick="return selfsteamRaNav(this)" here and on every other RA
    # navigation link below: an in-place AJAX swap (see PAGE_TAIL) when
    # JS is available, falling back to this same href as a real
    # navigation otherwise -- the href is never just a decoy.
    parts = [p for p in rel_path.split("/") if p]
    # path_key="/" here on purpose, not "" -- see _ra_resolve_relpath's
    # own comment. "" round-trips through _ra_qs indistinguishably from
    # "never set", which would land back on the home-dir default instead
    # of true root.
    root_crumb = f'<a href="{_ra_url("/new", state, **{path_key: "/"})}" onclick="return selfsteamRaNav(this)">{html.escape(_RA_ROOT)}</a>'
    crumbs = []
    built = ""
    for part in parts:
        built += f"/{part}"
        crumbs.append(
            f'<a href="{_ra_url("/new", state, **{path_key: built.lstrip("/")})}" onclick="return selfsteamRaNav(this)">{html.escape(part)}</a>'
        )
    # root_crumb's own text is already "/" -- joining it with " / " like
    # every other crumb produced a visible "/ / part" double-slash right
    # at the start, since the separator duplicates what the root label
    # already shows.
    return root_crumb + (" " + " / ".join(crumbs) if crumbs else "")


def _ra_list_rows(abs_path, rel_path, state, path_key, file_key):
    try:
        entries = sorted(os.scandir(abs_path), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return '<div class="row" style="color:var(--text-dim)">Permission denied</div>'
    # Dotfiles/dot-directories omitted -- not useful ROM/BIOS candidates,
    # and several (.ssh, .gnupg, .bash_history) shouldn't be casually
    # listed in a picker at all.
    entries = [e for e in entries if not e.name.startswith(".")]
    if not entries:
        return '<div class="row" style="color:var(--text-dim)">Nothing here.</div>'
    rows = []
    for entry in entries:
        entry_rel = f"{rel_path}/{entry.name}".lstrip("/")
        if entry.is_dir():
            href = _ra_url("/new", state, **{path_key: entry_rel})
            rows.append(f'<a href="{href}" onclick="return selfsteamRaNav(this)"><span class="folder-icon">&#128193;</span>{html.escape(entry.name)}</a>')
        else:
            overrides = {path_key: rel_path, file_key: entry_rel}
            if file_key == "ra_romfile":
                # A freshly-picked ROM needs a fresh SGDB search -- see
                # the /new handler's ra_loading branch, which only
                # skips straight to showing results when ra_resolved is
                # already set for this exact pick. ra_sgdb_q dropped too
                # -- otherwise a search term typed before any ROM was
                # even picked (which never did anything, since /new only
                # ever searches once a romfile exists) silently carried
                # forward and overrode the new ROM's own guessed-name
                # search once one was finally picked -- confirmed live,
                # typing "toy story" with no ROM yet, then picking Wave
                # Race 64, searched SGDB for "toy story" instead.
                overrides["ra_resolved"] = ""
                overrides["ra_sgdb_q"] = ""
                # A brand new ROM gets its own fresh one-time auto-fill
                # (see _ra_display_term/name_default) -- any earlier
                # "user explicitly cleared this" flag belonged to the
                # *previous* ROM, not this one.
                overrides["ra_sgdb_cleared"] = ""
                overrides["ra_name_cleared"] = ""
            href = _ra_url("/new", state, **overrides)
            rows.append(f'<a href="{href}" onclick="return selfsteamRaNav(this)"><span class="file-icon">&#128190;</span>{html.escape(entry.name)}</a>')
    return "".join(rows)


def _ra_picker_section(prefix, label, state, already_installed=None):
    path_key = f"ra_{prefix}path"
    file_key = f"ra_{prefix}file"
    source_key = f"ra_{prefix}source"
    skip_key = f"ra_{prefix}_skip"
    rel_path = _ra_resolve_relpath(state.get(path_key, ""))
    selected_file = state.get(file_key, "")
    dom_prefix = f"ra-{prefix}-source"
    # A console's BIOS is a one-time install shared by every game that
    # uses it, not per-shortcut state (same idea as standalone_emulators.
    # keys_installed/firmware_installed) -- so a second/third game for a
    # console that's already got its BIOS in place shows it as already
    # provided instead of demanding it be picked again. skip_key is the
    # one, deliberate way back to the real picker: clicking its own
    # "Remove" doesn't delete anything on disk (nothing was freshly
    # picked to remove), it just says "let me choose a different one"
    # for this session.
    show_installed = bool(already_installed and not selected_file and not state.get(skip_key))
    # Real server state (part of _RA_STATE_KEYS), not just a same-page JS
    # toggle -- survives a real reload (console change, upload finishing)
    # the same way every other RA field does, instead of always resetting
    # back to "local" the moment the page actually navigates.
    source = state.get(source_key) or "local"

    # Same-line upload-in-progress indicator, to the right of the label
    # row -- a multi-hundred-MB ROM over real wifi can take a while, and
    # the browser otherwise gives zero feedback that the click even
    # registered. Hidden by default, shown by selfsteamShowUploading (see
    # PAGE_TAIL) the instant the upload form is actually submitted.
    upload_status = (
        f'<span id="{dom_prefix}-upload-status" class="upload-status" style="display:none">'
        f'Uploading<span class="spinner"></span></span>'
    )

    # Removing the ROM also clears ra_resolved -- the SGDB search/Name
    # field are entirely derived from ra_romfile (see the /new handler),
    # so an emptied romfile naturally means no more search on the next
    # render; removing this specific field doesn't need to reset
    # ra_resolved for BIOS (nothing there drives a search).
    if selected_file:
        remove_overrides = {file_key: ""}
        if prefix == "rom":
            remove_overrides["ra_resolved"] = ""
        remove_href = _ra_url("/new", state, **remove_overrides)
        # Same line as the label, not its own row -- filename is
        # allowed to just get cropped by the label row's own overflow
        # rather than reserving a second line for it.
        label_row = f"""
    <label class="field-label" style="display:flex;align-items:center;gap:0.4rem;min-width:0">
      <span style="flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{label} <span class="required-asterisk">*</span></span>
      <span class="selected-file-name">&#10003; {html.escape(os.path.basename(selected_file))}</span>
      <a href="{remove_href}" class="remove-file-btn" title="Remove file" onclick="return selfsteamRaNav(this)">{_X_ICON_SVG}</a>
      {upload_status}
    </label>"""
    elif show_installed:
        remove_href = _ra_url("/new", state, **{skip_key: "1"})
        label_row = f"""
    <label class="field-label" style="display:flex;align-items:center;gap:0.4rem;min-width:0">
      <span style="flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{label} <span class="required-asterisk">*</span></span>
      <span class="selected-file-name">&#10003; {html.escape(already_installed)}</span>
      <a href="{remove_href}" class="remove-file-btn" title="Pick a different file" onclick="return selfsteamRaNav(this)">{_X_ICON_SVG}</a>
      {upload_status}
    </label>"""
    else:
        label_row = f"""
    <label class="field-label" style="display:flex;align-items:center;gap:0.4rem;min-width:0">
      <span style="flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{label} <span class="required-asterisk">*</span></span>
      {upload_status}
    </label>"""

    # Both panels are always rendered, with plain JS (selfsteamToggleSource
    # in PAGE_TAIL) instantly swapping which is visible on a same-page
    # click -- no navigation, so no reload flicker. Initial visibility on
    # an actual page load still comes from real server state (source),
    # so which one's showing survives a real reload same as everything
    # else on the tab.
    #
    # The rest of the RA state (which console, the other picker's own
    # path/file, etc.) rides in the action URL's own query string, not as
    # sibling hidden fields inside this multipart body -- keeps the body
    # down to exactly one part (the file itself), which is all
    # multipart_upload.py is built to stream. slot isn't part of
    # _RA_STATE_KEYS (it's specific to this one upload action, not
    # carried across other navigation), so it's appended directly rather
    # than routed through _ra_qs's own state-key filtering -- and
    # appended *before* the #fragment (via _ra_qs directly, not _ra_url),
    # since anything after a URL fragment becomes part of the fragment
    # text, not a real query param the server could read.
    upload_action = f"/new/upload?{_ra_qs(state)}&slot={prefix}#tab-retroarch"
    # Auto-submits on pick, no separate Upload button -- a real
    # user-initiated change event, not scripted navigation.
    # selfsteamShowUploading and selfsteamUploadFetch both run from onchange
    # directly, not a form onsubmit handler -- HTMLFormElement.submit()
    # (unlike the newer requestSubmit()) never fires the form's own
    # submit event at all per spec, so an onsubmit handler here would
    # silently never run; selfsteamUploadFetch itself falls back to a real
    # this.form.submit() if fetch throws, same as every other nav helper.
    upload_panel = f"""
    <form method="post" enctype="multipart/form-data" action="{upload_action}">
      <input type="file" name="file" onchange="selfsteamShowUploading('{dom_prefix}'); selfsteamUploadFetch(this, SELFSTEAM_RA_SWAP_IDS)">
    </form>"""

    abs_path = _ra_safe_join(rel_path)
    if abs_path is None or not os.path.isdir(abs_path):
        rel_path = _RA_DEFAULT_RELPATH
        abs_path = _ra_safe_join(rel_path) or _RA_ROOT
    local_panel = (
        f'<div class="breadcrumbs">{_ra_breadcrumbs_html(rel_path, state, path_key)}</div>'
        f'<div class="picker-list"><div class="boxed-list">{_ra_list_rows(abs_path, rel_path, state, path_key, file_key)}</div></div>'
    )

    upload_display = "" if source == "upload" else "none"
    local_display = "none" if source == "upload" else ""
    upload_active = "source-label active" if source == "upload" else "source-label"
    local_active = "source-label" if source == "upload" else "source-label active"

    # Once something's picked, the toggle + upload/local browsing UI is
    # dropped entirely rather than just left sitting there unused --
    # the picked file is the whole point once it exists, and "Remove
    # file" (the X on the label row above) is the one, deliberate way
    # back to picking again, not a second browsing UI competing for
    # attention alongside it.
    picker_ui = "" if (selected_file or show_installed) else f"""
    <div class="source-toggle">
      <a class="{upload_active}" href="javascript:void(0)" id="{dom_prefix}-upload-label" onclick="selfsteamToggleSource('{dom_prefix}', 'upload', '{source_key}')">Upload</a>
      <a class="{local_active}" href="javascript:void(0)" id="{dom_prefix}-local-label" onclick="selfsteamToggleSource('{dom_prefix}', 'local', '{source_key}')">{html.escape(_hostname())}</a>
    </div>
    <div id="{dom_prefix}-upload-panel" style="display:{upload_display};margin-top:0.6rem">{upload_panel}</div>
    <div id="{dom_prefix}-local-panel" style="display:{local_display}">{local_panel}</div>"""

    return f"""
  <div class="field-group">
    {label_row}
    {picker_ui}
  </div>"""


def _queue_edit_rename_cleanup(params, prefix, new_name):
    """If this /add submit started from the gallery's own Edit link (see
    edit_href's own comment) AND the Name field was changed before
    submitting, queues removal of the shortcut being replaced -- without
    this, it would be left behind as an orphaned duplicate rather than
    actually replaced, since add_shortcut's own dedup only matches by
    appname, and a changed name means the old entry's appname no longer
    matches the new one at all. Not needed (a no-op) when the name
    stayed the same: add_shortcut's own dedup already removes the old
    entry by appname match in that case, appid included, regardless of
    whether the emulator/core (and therefore exe, and therefore appid)
    also changed."""
    edit_appid = (params.get(f"{prefix}_edit_appid") or [""])[0]
    edit_name = (params.get(f"{prefix}_edit_name") or [""])[0]
    if edit_appid and edit_name and edit_name != new_name:
        pending_queue.add_removal(int(edit_appid), edit_name)


_RA_LEADING_CATALOG_NUM_RE = re.compile(r"^\d+\s*[-.]\s*")
_RA_PAREN_BRACKET_RE = re.compile(r"\(.*?\)|\[.*?\]")
_RA_WHITESPACE_RE = re.compile(r"\s+")
_RA_DASH_SUBTITLE_RE = re.compile(r"\s+-\s+")
_RA_TRAILING_THE_RE = re.compile(r"^(.*?),\s*(the)\b(.*)$", re.IGNORECASE)
_RA_SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of", "on", "or", "the", "to", "vs"}
_RA_ROMAN_NUMERAL_RE = re.compile(r"^(?=[MDCLXVI])M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$", re.IGNORECASE)


def _ra_guess_name_from_filename(rel_path):
    # Port of the same core cleanup steam-rom-manager's own
    # fuzzy-matcher.ts modifyString() does (confirmed via its source):
    # strip every parenthetical/bracketed tag -- region, language,
    # revision, dump-verification flags like "[!]" -- rather than
    # matching against a hardcoded handful of literal tag strings,
    # which missed anything not exactly in that list (e.g. a real report:
    # "(En,Fr,De,Es,It)" was never stripped since only "(USA)"-style
    # single-region tags were listed). Two extra steps beyond SRM's own,
    # both aimed at No-Intro/Redump-numbered romsets specifically: strip
    # a leading catalog/serial number ("3092 - "), and turn a
    # " - " subtitle separator into ": " (the actual title's usual
    # punctuation) before title-casing.
    base = os.path.splitext(os.path.basename(rel_path))[0]
    base = _RA_LEADING_CATALOG_NUM_RE.sub("", base)
    base = _RA_PAREN_BRACKET_RE.sub("", base)
    # Same trailing-", The" reorder SRM's own fuzzy-matcher.ts does
    # (/(.*?),\s*(the)/i), e.g. "Legend of Zelda, The" -> "The Legend of
    # Zelda" -- run after brackets are stripped so a trailing region tag
    # doesn't shield the comma from the end-of-string match.
    the_match = _RA_TRAILING_THE_RE.match(base.strip())
    if the_match:
        base = f"{the_match.group(2)} {the_match.group(1)}{the_match.group(3)}".strip()
    base = base.replace("_", " ").replace(".", " ")
    base = _RA_WHITESPACE_RE.sub(" ", base).strip(" -")
    base = _RA_DASH_SUBTITLE_RE.sub(": ", base)

    words = [w for w in base.split(" ") if w]
    titled = []
    for i, word in enumerate(words):
        # A word right after a ":" starts a new clause (the subtitle),
        # same as the very first/last word -- "the" in "Kirby 64: The
        # Crystal Shards" should stay capitalized, not get treated as a
        # mid-sentence small word.
        at_boundary = i == 0 or i == len(words) - 1 or titled[-1].endswith(":")
        if _RA_ROMAN_NUMERAL_RE.match(word):
            titled.append(word.upper())
        elif word.lower() in _RA_SMALL_WORDS and not at_boundary:
            titled.append(word.lower())
        else:
            titled.append(word[:1].upper() + word[1:])
    return " ".join(titled)


def _retroarch_tab_panel_html(state, chosen=None):
    console = state.get("ra_console", "")
    needs_bios = console in retroarch_cores.CONSOLES_NEEDING_BIOS

    console_options = "".join(
        f'<option value="{html.escape(c)}"{" selected" if c == console else ""}>'
        f'{"Pick your console" if not c else html.escape(c)}</option>'
        for c, _core, _needs in [("", None, False)] + retroarch_cores.CONSOLES
    )
    # id="ra-console-form-{k}" lets selfsteamToggleSource (PAGE_TAIL) sync
    # ra_romsource/ra_biossource here the instant they're toggled --
    # otherwise a toggle click followed immediately by a console change
    # (this form's own auto-submit) would submit the stale, pre-toggle
    # value baked in when the page was first rendered.
    hidden_fields = "".join(
        f'<input type="hidden" name="{k}" id="ra-console-form-{k}" value="{html.escape(state.get(k, ""))}">'
        for k in _RA_STATE_KEYS if k != "ra_console"
    )

    bios_block = (
        _ra_picker_section("bios", "Select BIOS", state, already_installed=retroarch_cores.bios_installed(console))
        if needs_bios else ""
    )
    rom_block = _ra_picker_section("rom", "Select ROM", state)

    # Own Name field, own input name (ra_match_name, not match_name) --
    # both tabs' Name fields exist in the DOM at once (only one visible
    # via CSS at a time), so sharing a name would submit two values for
    # the same field to the Add form. Cross-populated from the parsed
    # ROM filename via SGDB (see _ra_guess_name_from_filename / the
    # /new handler), same wand-icon/clear-to-reset pattern as the URL
    # tab's own Name field.
    romfile = state.get("ra_romfile", "")
    # ra_name_cleared: the auto cross-population from the resolved
    # match/guessed filename is meant to be a one-time courtesy fill-in,
    # not something that keeps overwriting the field after the user's
    # done something with it -- confirmed live, "clearing" this used to
    # just re-show the exact same guessed name right back, which read as
    # the clear button doing nothing. The icon toggles between Clear
    # (blanks it, sets the flag) and Reset to guessed name (drops the
    # flag, goes back to auto-fill) depending on which state it's in.
    name_cleared = bool(state.get("ra_name_cleared"))
    name_default = "" if name_cleared else (chosen["name"] if chosen else (_ra_guess_name_from_filename(romfile) if romfile else ""))
    name_reset_href = _ra_url("/new", state, ra_name_cleared=("" if name_cleared else "1"))
    name_reset_title = "Reset to guessed name" if name_cleared else "Clear"
    # onclick=selfsteamRaNav -- routes through the AJAX fetch layer instead
    # of a plain navigation so the swap always happens even when only a
    # same-page state flag changed (a plain <a> click to an identical
    # URL+fragment is a real no-op in the browser otherwise).
    name_field = f"""
  <div class="field-group">
    <label class="field-label" for="ra-name-field">Name</label>
    <div class="field-with-clear">
      <img class="name-field-icon" src="/vendor/name-field-wand.webp" alt="">
      <input type="text" name="ra_match_name" id="ra-name-field" form="{_ADD_FORM_ID}"
             value="{html.escape(name_default)}" placeholder="Shortcut name">
      <a href="{name_reset_href}" class="field-clear-btn" title="{name_reset_title}" onclick="return selfsteamRaNav(this)">&#10005;</a>
    </div>
  </div>"""

    return f"""
  <div class="field-group">
    <label class="field-label">Consoles <span class="required-asterisk">*</span> <span style="color:var(--text-dim);font-weight:400;font-size:0.85rem">Flatpak RetroArch Cores</span></label>
    <form method="get" action="/new#tab-retroarch" style="margin:0">
      {hidden_fields}
      <!-- Third deliberate JS exception (after the dark-mode toggle and
           login auto-submit) -- a <select> can't submit itself on
           change without it, and this is what the approved/tested demo
           used once the "Set console" button was removed. Since this
           already depends on JS to submit at all, routing that submit
           through selfsteamRaFormNav (fifth exception, see PAGE_TAIL) for
           an in-place swap instead of a real navigation costs nothing
           extra -- a JS-off browser was never going to auto-submit this
           either way. -->
      <select name="ra_console" onchange="selfsteamRaConsoleChanged(this)">
        {console_options}
      </select>
    </form>
  </div>
  {bios_block}
  {rom_block}
  <div class="selfsteam-spacer"></div>
  {name_field}"""


# Emulators tab: standalone (non-RetroArch) emulators, same picker/AJAX
# pattern as the RetroArch tab above -- state prefixed em_ instead of
# ra_ so the two never collide, same reasoning _RA_STATE_KEYS's own
# comment already gives for staying clear of the URL tab's q/sgdb_q/etc.
# em_install_source picks which half of standalone_emulators.EMULATORS
# populates the dropdown (Flathub vs AppImage) -- both source modes
# share this exact same rom/bios/keys picker + SGDB search flow, only
# the auto-install mechanism differs once Create Steam Shortcut is
# clicked (see _add_standalone_emulator_shortcut).
_EM_STATE_KEYS = [
    "em_install_source", "em_emulator",
    "em_rompath", "em_romfile", "em_romsource",
    "em_biospath", "em_biosfile", "em_biossource", "em_bios_skip",
    # bios2/bios3: extra BIOS-type file slots beyond the single one above
    # -- xemu is the first emulator here needing more than one (MCPX
    # bootrom, Xbox BIOS, EEPROM), see standalone_emulators.XEMU_BIOS_SLOTS.
    # Reuses the exact same em_<prefix>path/file/source/_skip shape as
    # every other picker, just with "bios2"/"bios3" as the prefix.
    "em_bios2path", "em_bios2file", "em_bios2source", "em_bios2_skip",
    "em_bios3path", "em_bios3file", "em_bios3source", "em_bios3_skip",
    "em_keyspath", "em_keysfile", "em_keyssource", "em_keys_skip",
    "em_firmwarepath", "em_firmwarefile", "em_firmwaresource", "em_firmware_skip",
    "em_resolved", "em_sgdb_q", "em_sgdb_cleared", "em_name_cleared",
    # Same reasoning as _RA_STATE_KEYS' own ra_edit_appid/ra_edit_name.
    "em_edit_appid", "em_edit_name",
]


def _em_state_from_params(params):
    return {key: (params.get(key) or [""])[0] for key in _EM_STATE_KEYS}


def _em_qs(state, **overrides):
    merged = dict(state)
    merged.update(overrides)
    return "&".join(f"{k}={urllib.parse.quote(str(merged[k]))}" for k in _EM_STATE_KEYS if merged.get(k))


def _em_url(path, state, **overrides):
    return f"{path}?{_em_qs(state, **overrides)}#tab-emulators"


def _em_breadcrumbs_html(rel_path, state, path_key):
    # _ra_safe_join/_RA_ROOT are reused as-is below -- the sandboxed
    # local-file-browsing logic they implement isn't actually RA-
    # specific despite the name, just built there first.
    parts = [p for p in rel_path.split("/") if p]
    # path_key="/" here on purpose, not "" -- see _ra_resolve_relpath's
    # own comment.
    root_crumb = f'<a href="{_em_url("/new", state, **{path_key: "/"})}" onclick="return selfsteamEmNav(this)">{html.escape(_RA_ROOT)}</a>'
    crumbs = []
    built = ""
    for part in parts:
        built += f"/{part}"
        crumbs.append(
            f'<a href="{_em_url("/new", state, **{path_key: built.lstrip("/")})}" onclick="return selfsteamEmNav(this)">{html.escape(part)}</a>'
        )
    # Same double-slash fix as _ra_breadcrumbs_html -- see its own comment.
    return root_crumb + (" " + " / ".join(crumbs) if crumbs else "")


def _em_list_rows(abs_path, rel_path, state, path_key, file_key):
    try:
        entries = sorted(os.scandir(abs_path), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return '<div class="row" style="color:var(--text-dim)">Permission denied</div>'
    entries = [e for e in entries if not e.name.startswith(".")]
    # ROM extensions the current emulator doesn't actually support (e.g.
    # Ryubing/Eden and .nsp, per real testing -- see standalone_emulators.
    # EMULATORS's own rom_exclude_extensions) are hidden from the picker
    # entirely rather than left selectable and failing later at launch.
    if file_key == "em_romfile":
        entry = standalone_emulators.EMULATORS.get(state.get("em_emulator", ""))
        exclude = entry.get("rom_exclude_extensions") if entry else None
        if exclude:
            entries = [e for e in entries if e.is_dir() or os.path.splitext(e.name)[1].lower() not in exclude]
    if not entries:
        return '<div class="row" style="color:var(--text-dim)">Nothing here.</div>'
    rows = []
    for entry in entries:
        entry_rel = f"{rel_path}/{entry.name}".lstrip("/")
        if entry.is_dir():
            href = _em_url("/new", state, **{path_key: entry_rel})
            rows.append(f'<a href="{href}" onclick="return selfsteamEmNav(this)"><span class="folder-icon">&#128193;</span>{html.escape(entry.name)}</a>')
        else:
            overrides = {path_key: rel_path, file_key: entry_rel}
            if file_key == "em_romfile":
                # A freshly-picked ROM needs a fresh SGDB search -- see
                # the /new handler's em_loading branch, mirroring the
                # RA tab's own ra_resolved reasoning exactly. em_sgdb_q
                # dropped too -- see _ra_list_rows's own comment on the
                # exact same fix, same root cause here.
                overrides["em_resolved"] = ""
                overrides["em_sgdb_q"] = ""
                # A brand new ROM gets its own fresh one-time auto-fill
                # -- see _ra_list_rows's own comment on the same reset,
                # same reasoning here.
                overrides["em_sgdb_cleared"] = ""
                overrides["em_name_cleared"] = ""
            href = _em_url("/new", state, **overrides)
            rows.append(f'<a href="{href}" onclick="return selfsteamEmNav(this)"><span class="file-icon">&#128190;</span>{html.escape(entry.name)}</a>')
    return "".join(rows)


def _em_picker_section(prefix, label, state, already_installed=None, info_tooltip=None, info_link=None):
    path_key = f"em_{prefix}path"
    file_key = f"em_{prefix}file"
    source_key = f"em_{prefix}source"
    skip_key = f"em_{prefix}_skip"
    rel_path = _ra_resolve_relpath(state.get(path_key, ""))
    selected_file = state.get(file_key, "")
    dom_prefix = f"em-{prefix}-source"
    source = state.get(source_key) or "local"
    # Keys/firmware are a one-time install for the emulator itself, not
    # per-shortcut state (see standalone_emulators.keys_installed/
    # firmware_installed) -- a second/third game for an emulator that's
    # already set up shows them as already provided instead of demanding
    # a re-pick every time. skip_key is the deliberate way back to the
    # real picker (see _ra_picker_section's own comment on the same
    # pattern for BIOS).
    show_installed = bool(already_installed and not selected_file and not state.get(skip_key))

    upload_status = (
        f'<span id="{dom_prefix}-upload-status" class="upload-status" style="display:none">'
        f'Uploading<span class="spinner"></span></span>'
    )
    label_text = f'{label} <span class="required-asterisk">*</span>'
    if info_tooltip:
        label_text += f' {_info_tooltip_icon_html(info_tooltip)}'
    # Hidden once a file is either freshly picked or already installed --
    # at that point there's nothing left to download, so the link would
    # just be clutter next to the checkmark state.
    if info_link and not selected_file and not show_installed:
        link_url, link_text = info_link
        # Plain inline flow, no position/display override -- vertical-
        # align defaults to "baseline", which is what actually lines up
        # a smaller font-size run with the rest of the label; the
        # earlier position:relative;top:-0.2em hack was fighting that
        # default instead of relying on it, which is what threw the
        # underline off.
        label_text += (
            f' <a href="{html.escape(link_url)}" target="_blank" rel="noopener" '
            f'style="font-size:0.8em;color:var(--text-dim);white-space:nowrap">{html.escape(link_text)} &#8599;</a>'
        )

    if selected_file:
        remove_overrides = {file_key: ""}
        if prefix == "rom":
            remove_overrides["em_resolved"] = ""
        remove_href = _em_url("/new", state, **remove_overrides)
        display_name = os.path.basename(selected_file)
        # A real Switch key dump usually has title.keys sitting right
        # alongside the picked prod.keys -- install_keys already copies
        # it too automatically when it's there (see its own docstring),
        # so the display reflects that instead of only ever showing the
        # one file the user actually clicked. Ryubing-specific (Cemu's
        # own keys.txt has no sibling-file concept at all).
        if prefix == "keys" and state.get("em_emulator") == "Ryubing":
            abs_selected = _ra_safe_join(selected_file)
            if abs_selected:
                sibling = os.path.join(os.path.dirname(abs_selected), "title.keys")
                if os.path.basename(abs_selected) != "title.keys" and os.path.isfile(sibling):
                    display_name = f"{display_name}, title.keys"
        label_row = f"""
    <label class="field-label" style="display:flex;align-items:center;gap:0.4rem;min-width:0">
      <span style="flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{label_text}</span>
      <span class="selected-file-name">&#10003; {html.escape(display_name)}</span>
      <a href="{remove_href}" class="remove-file-btn" title="Remove file" onclick="return selfsteamEmNav(this)">{_X_ICON_SVG}</a>
      {upload_status}
    </label>"""
    elif show_installed:
        remove_href = _em_url("/new", state, **{skip_key: "1"})
        label_row = f"""
    <label class="field-label" style="display:flex;align-items:center;gap:0.4rem;min-width:0">
      <span style="flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{label_text}</span>
      <span class="selected-file-name">&#10003; {html.escape(already_installed)}</span>
      <a href="{remove_href}" class="remove-file-btn" title="Pick a different file" onclick="return selfsteamEmNav(this)">{_X_ICON_SVG}</a>
      {upload_status}
    </label>"""
    else:
        label_row = f"""
    <label class="field-label" style="display:flex;align-items:center;gap:0.4rem;min-width:0">
      <span style="flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{label_text}</span>
      {upload_status}
    </label>"""

    upload_action = f"/new/upload-em?{_em_qs(state)}&slot={prefix}#tab-emulators"
    upload_panel = f"""
    <form method="post" enctype="multipart/form-data" action="{upload_action}">
      <input type="file" name="file" onchange="selfsteamShowUploading('{dom_prefix}'); selfsteamUploadFetch(this, SELFSTEAM_EM_SWAP_IDS)">
    </form>"""

    abs_path = _ra_safe_join(rel_path)
    if abs_path is None or not os.path.isdir(abs_path):
        rel_path = _RA_DEFAULT_RELPATH
        abs_path = _ra_safe_join(rel_path) or _RA_ROOT
    local_panel = (
        f'<div class="breadcrumbs">{_em_breadcrumbs_html(rel_path, state, path_key)}</div>'
        f'<div class="picker-list"><div class="boxed-list">{_em_list_rows(abs_path, rel_path, state, path_key, file_key)}</div></div>'
    )

    upload_display = "" if source == "upload" else "none"
    local_display = "none" if source == "upload" else ""
    upload_active = "source-label active" if source == "upload" else "source-label"
    local_active = "source-label" if source == "upload" else "source-label active"

    # Once something's picked, the toggle + upload/local browsing UI is
    # dropped entirely rather than just left sitting there unused --
    # the picked file is the whole point once it exists, and "Remove
    # file" (the X on the label row above) is the one, deliberate way
    # back to picking again, not a second browsing UI competing for
    # attention alongside it.
    picker_ui = "" if (selected_file or show_installed) else f"""
    <div class="source-toggle">
      <a class="{upload_active}" href="javascript:void(0)" id="{dom_prefix}-upload-label" onclick="selfsteamToggleSource('{dom_prefix}', 'upload', '{source_key}')">Upload</a>
      <a class="{local_active}" href="javascript:void(0)" id="{dom_prefix}-local-label" onclick="selfsteamToggleSource('{dom_prefix}', 'local', '{source_key}')">{html.escape(_hostname())}</a>
    </div>
    <div id="{dom_prefix}-upload-panel" style="display:{upload_display};margin-top:0.6rem">{upload_panel}</div>
    <div id="{dom_prefix}-local-panel" style="display:{local_display}">{local_panel}</div>"""

    return f"""
  <div class="field-group">
    {label_row}
    {picker_ui}
  </div>"""


def _emulators_tab_panel_html(state, chosen=None):
    install_source = state.get("em_install_source") or "flathub"
    emulator = state.get("em_emulator", "")
    entry = standalone_emulators.EMULATORS.get(emulator)
    needs_bios = bool(entry and entry.get("needs_bios"))
    needs_keys = bool(entry and entry.get("needs_keys"))
    needs_firmware = bool(entry and entry.get("needs_firmware"))

    # If the previously-picked emulator doesn't belong to whichever
    # install source is now active (e.g. toggled from Flathub to
    # AppImage), it's simply not a valid option in this dropdown --
    # shows "Pick your emulator" instead of a stale/wrong selection.
    # "<emulator> - <consoles>" as one plain-text label -- native
    # <option> elements can't mix two text colors/weights inside a
    # single option, so a separate grey "consoles" line was the other
    # option; this is what was actually asked for instead.
    def _emulator_option_label(name):
        e = standalone_emulators.EMULATORS.get(name, {})
        # display_name overrides the catalog's own (dict-key-unique)
        # name for what's actually shown -- e.g. "Ryubing (AppImage)"
        # displays as just "Ryubing" since the AppImage/Flathub toggle
        # already disambiguates it from the Flathub "Ryubing" entry.
        display = e.get("display_name", name)
        consoles = e.get("consoles")
        return f"{display} - {consoles}" if consoles else display

    # Sorted by emulator name -- unlike the RA tab's own "console - core"
    # picker (retroarch_cores.CONSOLES), where console is the meaningful
    # grouping, this tab's own label is "EmulatorName - Console", so
    # alphabetical-by-emulator is what the user actually asked for here.
    names = sorted(
        standalone_emulators.by_install_type(install_source),
        key=lambda n: n.lower(),
    )

    emulator_options = "".join(
        f'<option value="{html.escape(e)}"{" selected" if e == emulator else ""}>'
        f'{"Pick your emulator" if not e else html.escape(_emulator_option_label(e))}</option>'
        for e in [""] + names
    )

    # id="em-console-form-{k}" mirrors the RA tab's own "ra-console-
    # form-{k}" -- see selfsteamToggleSource's own comment on why its
    # lookup only needs the leading tab prefix, not a separate function.
    hidden_fields = "".join(
        f'<input type="hidden" name="{k}" id="em-console-form-{k}" value="{html.escape(state.get(k, ""))}">'
        for k in _EM_STATE_KEYS if k != "em_emulator"
    )

    def _source_toggle_link(value, text):
        active = "source-label active" if install_source == value else "source-label"
        # Switching source clears the emulator pick (and anything
        # downstream of it) rather than leaving a stale selection from
        # the other list sitting there -- Flathub/AppImage are disjoint
        # catalogs, so "Cemu (Flathub)" being selected while viewing the
        # AppImage list would just be wrong, not merely stale.
        href = _em_url(
            "/new", state, em_install_source=value, em_emulator="",
            em_romfile="", em_biosfile="", em_bios2file="", em_bios3file="",
            em_keysfile="", em_firmwarefile="", em_resolved="",
        )
        return f'<a class="{active}" href="{href}" onclick="return selfsteamEmNav(this)">{text}</a>'

    source_toggle = f"""
    <div class="source-toggle" style="margin-bottom:0.6rem">
      {_source_toggle_link("flathub", "Flathub")}
      {_source_toggle_link("binary", "AppImage")}
    </div>"""

    # bios_slots (xemu so far): more than one required BIOS-type file,
    # rendered as its own picker per slot rather than the single generic
    # one every other needs_bios emulator uses -- see
    # standalone_emulators.XEMU_BIOS_SLOTS.
    bios_slots = entry.get("bios_slots") if entry else None
    if bios_slots:
        bios_slot_links = entry.get("bios_slot_links") or {}
        bios_block = "".join(
            _em_picker_section(
                prefix, label, state,
                already_installed=standalone_emulators.bios_slot_installed(emulator, prefix),
                info_link=bios_slot_links.get(prefix),
            )
            for prefix, label, *_rest in bios_slots
        )
    else:
        bios_block = _em_picker_section("bios", "Select BIOS", state) if needs_bios else ""
    keys_block = (
        _em_picker_section(
            "keys", "Select Keys", state,
            already_installed=standalone_emulators.keys_installed(emulator),
            info_tooltip=standalone_emulators.EMULATORS.get(emulator, {}).get("keys_tooltip"),
        )
        if needs_keys else ""
    )
    firmware_block = (
        _em_picker_section("firmware", "Select Firmware", state,
                           already_installed=standalone_emulators.firmware_installed(emulator))
        if needs_firmware else ""
    )
    rom_block = _em_picker_section("rom", "Select ROM", state)

    romfile = state.get("em_romfile", "")
    # em_name_cleared -- see _retroarch_tab_panel_html's own comment on
    # the same flag/toggle, same reasoning here.
    name_cleared = bool(state.get("em_name_cleared"))
    name_default = "" if name_cleared else (chosen["name"] if chosen else (_ra_guess_name_from_filename(romfile) if romfile else ""))
    name_reset_href = _em_url("/new", state, em_name_cleared=("" if name_cleared else "1"))
    name_reset_title = "Reset to guessed name" if name_cleared else "Clear"
    # onclick=selfsteamEmNav -- see _retroarch_tab_panel_html's own comment
    # on the same fix, same identical-URL no-op bug here.
    name_field = f"""
  <div class="field-group">
    <label class="field-label" for="em-name-field">Name</label>
    <div class="field-with-clear">
      <img class="name-field-icon" src="/vendor/name-field-wand.webp" alt="">
      <input type="text" name="em_match_name" id="em-name-field" form="{_ADD_FORM_ID}"
             value="{html.escape(name_default)}" placeholder="Shortcut name">
      <a href="{name_reset_href}" class="field-clear-btn" title="{name_reset_title}" onclick="return selfsteamEmNav(this)">&#10005;</a>
    </div>
  </div>"""

    # Native title tooltip -- covers both install sources at once (not
    # conditional on which one's active) since it's a hover reference,
    # not a status message about what's about to happen this time.
    emulator_info_tooltip = (
        "Flatpaks will be downloaded from Flathub if not installed.\n\n"
        "AppImage emulators will be downloaded from developer's own source."
    )

    return f"""
  <div class="field-group">
    <label class="field-label">Emulator <span class="required-asterisk">*</span>
      {_info_tooltip_icon_html(emulator_info_tooltip)}
    </label>
    {source_toggle}
    <form method="get" action="/new#tab-emulators" style="margin:0">
      {hidden_fields}
      <select name="em_emulator" onchange="selfsteamEmEmulatorChanged(this)">
        {emulator_options}
      </select>
    </form>
  </div>
  {bios_block}
  {keys_block}
  {firmware_block}
  {rom_block}
  <div class="selfsteam-spacer"></div>
  {name_field}"""


def _em_display_term(state, chosen=None):
    # What's actually driving the current SGDB results: the explicit
    # override if there is one, else -- only if the user hasn't
    # explicitly cleared this box for the current ROM (em_sgdb_cleared,
    # set by the Clear link itself, dropped again the moment a genuinely
    # new ROM is picked -- see _em_list_rows) -- the real resolved
    # match's own name, else the raw filename guess. The cross-
    # population is meant to be a one-time courtesy fill-in, not
    # something that keeps overwriting whatever the user does with the
    # field afterward -- confirmed live: clearing this then had the
    # "cleared" search term stick, but the box's own displayed value
    # kept reverting to the freshly re-resolved game name anyway, which
    # read as "Clear doesn't actually clear it."
    if state.get("em_sgdb_q"):
        return state["em_sgdb_q"].lower()
    if state.get("em_sgdb_cleared"):
        return ""
    if chosen and chosen.get("name"):
        return chosen["name"].lower()
    romfile = state.get("em_romfile")
    return _ra_guess_name_from_filename(romfile).lower() if romfile else ""


def _em_sgdb_search_bar_html(state, chosen=None):
    display_term = _em_display_term(state, chosen)
    # em_resolved dropped along with em_sgdb_q -- carrying it forward
    # would skip the /new handler's em_loading branch entirely (it only
    # triggers when em_resolved is falsy), meaning a real search term
    # change would run its whole SGDB search synchronously in one
    # request with zero loading feedback, not just the very first
    # search a fresh ROM pick already triggers on its own. em_sgdb_cleared
    # set alongside them -- see _em_display_term's own comment for why:
    # without it, the fresh re-search this triggers would just
    # re-populate the box's own display right back with the newly
    # resolved match's name.
    clear_href = _em_url("/new", state, em_sgdb_q="", em_resolved="", em_sgdb_cleared="1")
    hidden = _ra_hidden_fields({k: v for k, v in state.items() if k not in ("em_sgdb_q", "em_resolved")})
    # Disabled until a ROM actually exists -- /new never runs a search at
    # all without one (there's nothing to guess a name from), so typing
    # here beforehand silently did nothing except sit in state, which
    # then wrongly overrode the real search once a ROM was finally
    # picked (confirmed live: typing a term with no ROM yet, then
    # picking one, searched SGDB for the stale term instead of the new
    # ROM's own guessed name -- see _em_list_rows's own fix for the
    # other half of this). A disabled input isn't included in its own
    # form submission at all, so it can't leave a stale value behind.
    disabled = "" if state.get("em_romfile") else " disabled"
    return f"""
<form action="/new#tab-emulators" method="get">
  {hidden}
  <div class="search-field-row">
    <div class="field-with-clear">
      <input type="text" name="em_sgdb_q" value="{html.escape(display_term)}" placeholder="SGDB search"{disabled}>
      <a href="{clear_href}" class="field-clear-btn" title="Clear">&#10005;</a>
    </div>
    <button type="submit" class="search-submit-btn" title="Search"{disabled}>{_SEARCH_ICON_SVG}</button>
  </div>
</form>
"""


def _em_middle_column_html(state, matches, extra_class=""):
    if not matches:
        list_html = _placeholder_matches_html()
    else:
        href = _em_url("/new", state)
        rows = []
        for i, m in enumerate(matches):
            cls = " selected" if i == 0 else ""
            rows.append(f'<a class="{cls.strip()}" href="{href}">{html.escape(m["name"])}</a>')
        list_html = f'<div class="boxed-list">{"".join(rows)}</div>'
    return f"""
<div class="card {extra_class}" id="selfsteam-em-middle">
  {_em_sgdb_search_bar_html(state, matches[0] if matches else None)}
  <div class="field-group" style="flex:1;min-height:0">
    <h2>SGDB matches</h2>
    {list_html}
  </div>
</div>
"""


_FORM_TABS = [("tab-url", "URL"), ("tab-apps", "Apps"), ("tab-retroarch", "RetroArch"), ("tab-emulators", "Emulators")]


def _tab_bar_targets_html():
    # Emitted just above .selfsteam-columns (see render_page) -- empty
    # :target anchor markers, not radio inputs. The radio-hack version
    # of this (an <input type=radio checked> per tab, server-picking
    # which one had "checked") looked right in every direct HTTP test
    # and in this project's own Chromium-based testing tool, but still
    # broke in real-world Firefox: Firefox restores a form control's
    # previous checked state across navigation more aggressively than
    # autocomplete="off" reliably suppresses (a known Firefox/Chromium
    # behavior gap, not something fixable by trying harder on the
    # attribute). :target sidesteps the whole category -- it's driven
    # purely by whether the URL's own #fragment matches this element's
    # id, nothing a browser could "helpfully" restore from a previous
    # page, since there's no form state involved at all.
    return "".join(f'<span id="{tab_id}"></span>' for tab_id, _label in _FORM_TABS)


def _tab_bar_html():
    # Plain links (#tab-url etc.), not radio-paired <label for=...> --
    # see _tab_bar_targets_html for why this moved off form controls.
    labels = "".join(f'<a href="#{tab_id}" class="tab-label">{label}</a>' for tab_id, label in _FORM_TABS)
    return f'<div class="tab-bar">{labels}</div>'


# Generous fixed count, not a computed fit: row height is fixed/compact
# (not flex-grown per row -- that was the earlier "one giant row" bug),
# so this just needs to be enough to fill any reasonably tall column;
# .card's own overflow-y:auto quietly clips/scrolls any excess.
_PLACEHOLDER_ROW_COUNT = 30


def _placeholder_matches_html():
    rows = ['<div class="placeholder-row"></div>' for _ in range(_PLACEHOLDER_ROW_COUNT)]
    return f'<div class="boxed-list">{"".join(rows)}</div>'


def _display_name(query, sgdb_q):
    # What's actually driving the current SGDB results: the explicit
    # override if there is one, else whatever the URL/service field
    # itself resolved to -- see _em_display_term's own comment for why
    # this fallback is safe (Clear drops the resolved state alongside
    # the override, so this never shows a stale term). Purely a search
    # term -- the separate Name field (see _url_tab_panel_html) is what
    # actually gets saved, so editing this doesn't rename anything on
    # its own.
    if sgdb_q:
        return sgdb_q.lower()
    resolved = service_resolver.resolve(query) if query else None
    return resolved.name.lower() if resolved and resolved.name else ""


def _sgdb_search_bar_html(query, couch_mode, browser, sgdb_q, ra_state=None, em_state=None, has_matches=False):
    # Always-visible override search (matches the design handoff's
    # column-2 "SGDB search" pill) rather than the earlier magnifying-
    # glass reveal -- lets a user search SteamGridDB directly,
    # independent of whatever the URL/service field resolves to, while
    # /add still uses the original resolved URL for the shortcut itself.
    # Pre-filled with the term actually driving the current results
    # (the explicit override if there is one, else whatever the URL/
    # service field itself resolved to) rather than sitting empty until
    # touched -- same behavior planned for the Apps/RetroArch/Emulators
    # tabs once they're built out, not just the URL tab.
    display_term = _display_name(query, sgdb_q)
    clear_href = f"/search?{_state_qs(query, couch_mode, browser, ra_state, em_state)}"
    # Disabled until the URL/service field has actually resolved to
    # something real -- same reasoning as the RA/Emulators tabs' own
    # search bars being disabled with no ROM picked yet (see
    # _ra_sgdb_search_bar_html): there's nothing to search SGDB for
    # otherwise, so a live-looking box that silently did nothing was the
    # same trap. has_matches (there's always at least one synthetic
    # match once resolution succeeds, see _resolve_matches) is this
    # tab's own version of "romfile picked."
    disabled = "" if has_matches else " disabled"
    return f"""
<form action="/search" method="get">
  {_hidden_state_fields(query, couch_mode, browser, ra_state, em_state)}
  <div class="search-field-row">
    <div class="field-with-clear">
      <input type="text" name="sgdb_q" value="{html.escape(display_term)}" placeholder="SGDB search"{disabled}>
      <a href="{clear_href}" class="field-clear-btn" title="Clear">&#10005;</a>
    </div>
    <button type="submit" class="search-submit-btn" title="Search"{disabled}>{_SEARCH_ICON_SVG}</button>
  </div>
</form>
"""


def _match_list_html(query, couch_mode, browser, sgdb_q, matches, match_index, ra_state=None, em_state=None):
    # Plain links, not radio+submit-button: clicking one navigates
    # straight to that match's artwork (a real GET, no JS needed) --
    # a radio selection alone doesn't submit anything by itself, which
    # read as "artwork doesn't change when I pick a different match".
    # sgdb_q stays whatever it already was across rows -- picking a
    # match no longer touches the search box, only which artwork/name
    # is chosen (the Name field, back in the left column, follows the
    # picked match's own name instead).
    rows = []
    qs = _state_qs(query, couch_mode, browser, ra_state, em_state, sgdb_q=sgdb_q)
    for i, m in enumerate(matches):
        selected = " selected" if i == match_index else ""
        rows.append(
            f'<a class="{selected.strip()}" href="/search?{qs}&match_index={i}">{html.escape(m["name"])}</a>'
        )
    return f'<div class="boxed-list">{"".join(rows)}</div>'


def _middle_column_html(query, couch_mode, browser, sgdb_q, matches, match_index, extra_class="", ra_state=None, em_state=None):
    list_html = _match_list_html(query, couch_mode, browser, sgdb_q, matches, match_index, ra_state, em_state) if matches else _placeholder_matches_html()
    return f"""
<div class="card {extra_class}">
  {_sgdb_search_bar_html(query, couch_mode, browser, sgdb_q, ra_state, em_state, has_matches=bool(matches))}
  <div class="field-group" style="flex:1;min-height:0">
    <h2>SGDB matches</h2>
    {list_html}
  </div>
</div>
"""


def _ra_display_term(state, chosen=None):
    # Same fallback chain (override -> resolved match's name -> filename
    # guess) as _em_display_term, with the same ra_sgdb_cleared guard --
    # see its own comment for the reasoning.
    if state.get("ra_sgdb_q"):
        return state["ra_sgdb_q"].lower()
    if state.get("ra_sgdb_cleared"):
        return ""
    if chosen and chosen.get("name"):
        return chosen["name"].lower()
    romfile = state.get("ra_romfile")
    return _ra_guess_name_from_filename(romfile).lower() if romfile else ""


def _ra_sgdb_search_bar_html(state, chosen=None):
    # Same override search as the URL tab's own _sgdb_search_bar_html --
    # a ROM's filename-derived guess (_ra_guess_name_from_filename) can
    # be wrong or unhelpfully generic ("rom (1)"), so this lets a search
    # term be typed directly, independent of the filename, same as
    # sgdb_q already does for the URL tab. ra_sgdb_q lives in
    # _RA_STATE_KEYS, so it's just another field carried by every
    # existing RA link/form for free -- no separate threading needed.
    display_term = _ra_display_term(state, chosen)
    # ra_resolved/ra_sgdb_cleared dropped alongside ra_sgdb_q -- see
    # _em_sgdb_search_bar_html's own comment on this exact same fix for
    # the reasoning.
    clear_href = _ra_url("/new", state, ra_sgdb_q="", ra_resolved="", ra_sgdb_cleared="1")
    hidden = _ra_hidden_fields({k: v for k, v in state.items() if k not in ("ra_sgdb_q", "ra_resolved")})
    # Disabled until a ROM exists -- see _em_sgdb_search_bar_html's own
    # comment on this exact same fix for the reasoning.
    disabled = "" if state.get("ra_romfile") else " disabled"
    return f"""
<form action="/new#tab-retroarch" method="get">
  {hidden}
  <div class="search-field-row">
    <div class="field-with-clear">
      <input type="text" name="ra_sgdb_q" value="{html.escape(display_term)}" placeholder="SGDB search"{disabled}>
      <a href="{clear_href}" class="field-clear-btn" title="Clear">&#10005;</a>
    </div>
    <button type="submit" class="search-submit-btn" title="Search"{disabled}>{_SEARCH_ICON_SVG}</button>
  </div>
</form>
"""


def _ra_middle_column_html(state, matches, extra_class=""):
    # No match switching yet (only one candidate is ever resolved) --
    # rows are informational display only, self-referential <a> hrefs so
    # they pick up the same .boxed-list/a.selected styling without new
    # CSS just for this. The editable Name field is still the real way
    # to correct a bad guess beyond re-searching SGDB outright.
    if not matches:
        list_html = _placeholder_matches_html()
    else:
        href = _ra_url("/new", state)
        rows = []
        for i, m in enumerate(matches):
            cls = " selected" if i == 0 else ""
            rows.append(f'<a class="{cls.strip()}" href="{href}">{html.escape(m["name"])}</a>')
        list_html = f'<div class="boxed-list">{"".join(rows)}</div>'
    return f"""
<div class="card {extra_class}" id="selfsteam-ra-middle">
  {_ra_sgdb_search_bar_html(state, matches[0] if matches else None)}
  <div class="field-group" style="flex:1;min-height:0">
    <h2>SGDB matches</h2>
    {list_html}
  </div>
</div>
"""


# Sum of gui.py's own category heights (255+121+104+100+100) -- used
# below to give each category a real flex-grow share of the column's
# actual height, proportional to its own natural size. Deliberately not
# a vh-based pixel estimate (tried and reverted twice): any fixed guess
# at "how much of the viewport isn't available for artwork" inevitably
# drifts from reality (a padding change elsewhere, a different screen)
# and leaves visible empty space wherever that estimate came up short.
# Real flex-grow instead means the categories collectively fill
# .artwork-card's exact real height, whatever it actually renders to,
# with no guessing involved at all.
_ARTWORK_HEIGHT_WEIGHT_SUM = sum(base_h for _b, _t, _f, _w, base_h in ARTWORK_CATEGORIES)


# Skeleton tile counts per category before any search. The design
# handoff's own placeholder counts (4/2/2/4/6) assumed a narrower
# column than this card can actually be on a wide screen -- confirmed
# live: on a 1920px-class display each row's real tiles ran out well
# before the row's own available width did, leaving a visible blank
# gap between the last skeleton and the card's right edge instead of
# looking like content that's still loading. Bumped generously higher
# (each row is horizontally scrollable regardless, see .artwork-row's
# own overflow-x:auto, so a few extra off-screen tiles on a narrower
# window cost nothing) rather than trying to compute an exact count for
# an arbitrary, unknown-server-side viewport width.
_SKELETON_TILE_COUNTS = {
    "grid_vertical": 10,
    "grid_horizontal": 8,
    "hero": 8,
    "logo": 12,
    "icon": 16,
}


def _ra_loading_artwork_html():
    # Shown only during the ra_resolved meta-refresh loading phase (see
    # the /new handler) -- a real SGDB search can take a few seconds,
    # and the earlier version gave zero visible feedback during that
    # wait (the previous page just sat there until the new one loaded),
    # read as "did my click even register?". Same skeleton grid as the
    # real empty state, just with a spinner next to each category
    # title instead of nothing.
    #
    # Uses the exact same weighted flex-grow sizing (category_style/
    # row_style/cell_style) as _artwork_picker_html's own blank and
    # real-results states, not the old fixed design-handoff pixel sizes
    # (width:{w}px;height:{h}px) this used to have -- confirmed live as
    # a real bug: the column's own proportions visibly jumped between
    # blank -> searching -> results instead of staying put, since this
    # was the one state still sized completely differently from the
    # other two.
    sections = []
    for basename, title, _fetch, base_w, base_h in ARTWORK_CATEGORIES:
        weight = base_h / _ARTWORK_HEIGHT_WEIGHT_SUM
        category_style = f' style="flex:{weight:.4f} 1 0; min-height:0;"'
        # --mobile-cell-height: mobile's own !important override (see
        # the @media (max-width: 960px) block's own comment) reads this
        # per-category instead of one flat height for every category --
        # confirmed live: a flat height made Vertical Grid (base_h=255,
        # by far the biggest/most prominent category by design) look no
        # more prominent than Icon (base_h=100) on mobile, unlike every
        # other state where its real proportions show through. 0.55 is
        # just a scale factor keeping it a reasonable on-screen size on
        # a phone (255 * 0.55 ≈ 140px), not a load-bearing number.
        row_style = (
            f' style="flex:1; min-height:0; height:100%; --mobile-cell-height:{base_h * 0.55:.0f}px;" '
            f'data-artwork-ratio="{base_w / base_h:.6f}"'
        )
        cell_style = f"height:100%; min-height:60px; aspect-ratio: {base_w} / {base_h};"
        skeletons = "".join(
            f'<div class="artwork-cell artwork-skeleton" style="{cell_style}"></div>'
            for _ in range(_SKELETON_TILE_COUNTS[basename])
        )
        sections.append(f"""
<div class="artwork-category"{category_style}>
  <h3>{html.escape(title)}<span class="spinner"></span></h3>
  <div class="artwork-row"{row_style}>{skeletons}</div>
</div>""")
    return "".join(sections)


def _artwork_picker_html(candidates_by_category, prefix=""):
    # Always renders all 5 categories, even with zero candidates
    # (candidates_by_category can be {}) -- shown before any search
    # too, so the right column never collapses to a placeholder message
    # and always occupies its full share of the row.
    #
    # prefix ("" for URL, "ra_" for RetroArch, "em_" for Emulators) keeps
    # each tab's own radio group and ids from colliding with the other
    # two -- every flavor of right column is always rendered at once
    # (CSS decides which is visible, see render_page's own comment), and
    # all three shared the exact same name="artwork_{basename}" AND the
    # same form="{_ADD_FORM_ID}" until this existed, meaning they were
    # really just one radio group per category spanning all three tabs:
    # whichever tab's picker happened to render *last* in the DOM (with
    # its own default checked) silently won the real submitted value,
    # regardless of which tab was actually visible or what got clicked --
    # confirmed live as exactly why a freshly resolved RA search's own
    # first result never looked selected (the Emulators tab's own
    # always-"no artwork" default, rendered after it, was overriding it).
    sections = []
    for basename, title, _fetch, base_w, base_h in ARTWORK_CATEGORIES:
        candidates = candidates_by_category.get(basename) or []
        # Real flex-grow, not a vh-based estimate -- every category's
        # own .artwork-category/.artwork-row gets flex-grow proportional
        # to its natural size (base_h), with min-height:0 so it can
        # shrink below its own content size (the standard flex-child
        # requirement for this to work at all). Collectively they always
        # fill .artwork-card's *real* height exactly, whatever it
        # actually is, with zero guessing about viewport/header/padding
        # overhead -- a fixed vh-based calc there was inherently
        # approximate and left visible empty space under whichever
        # category happened to sit last once that estimate drifted from
        # reality (confirmed live, twice: once from a padding change
        # elsewhere going unaccounted for, and once from only fixing
        # this for the last category instead of all of them). Cells are
        # height:100% of their row's own real (flex-determined) height;
        # aspect-ratio still derives width from that, same as before.
        weight = base_h / _ARTWORK_HEIGHT_WEIGHT_SUM
        category_style = f' style="flex:{weight:.4f} 1 0; min-height:0;"'
        # aspect-ratio is only a first-paint fallback here (before
        # selfsteamSizeArtworkCells below runs and sets a real pixel
        # width) -- confirmed live, real cross-browser interop gap: a
        # flex item stretched to a percentage/flex-derived height with
        # aspect-ratio deriving its width is a genuinely different
        # calculation between engines (Firefox/Gecko vs Chromium/Blink;
        # matches Mozilla bug 1658441 and the wider "flexbugs" history
        # around percentage heights + aspect-ratio in flex children),
        # not something fixable by moving the style between the cell
        # and its label -- Chromium rendered this fine, Firefox/Zen
        # both showed a real, reproducible gap on the exact same markup.
        # data-artwork-ratio on the row (read by that JS) is what
        # actually settles this identically everywhere: once JS sets an
        # explicit pixel width, aspect-ratio no longer has a missing
        # dimension left to derive, so it can no longer disagree.
        # --mobile-cell-height: mobile's own !important override (see
        # the @media (max-width: 960px) block's own comment) reads this
        # per-category instead of one flat height for every category --
        # confirmed live: a flat height made Vertical Grid (base_h=255,
        # by far the biggest/most prominent category by design) look no
        # more prominent than Icon (base_h=100) on mobile, unlike every
        # other state where its real proportions show through. 0.55 is
        # just a scale factor keeping it a reasonable on-screen size on
        # a phone (255 * 0.55 ≈ 140px), not a load-bearing number.
        row_style = (
            f' style="flex:1; min-height:0; height:100%; --mobile-cell-height:{base_h * 0.55:.0f}px;" '
            f'data-artwork-ratio="{base_w / base_h:.6f}"'
        )
        cell_style = f"height:100%; min-height:60px; aspect-ratio: {base_w} / {base_h};"
        # Always the first cell. value="" already flows through
        # do_POST's existing "falsy selection -> skip this category"
        # logic untouched, so a shortcut can always be created with no
        # artwork picked -- either because there's no SGDB key at all
        # (real candidates never loaded, this is the only selectable
        # cell in the row) or because the user actively wants to skip
        # artwork for this one category despite real candidates being
        # available. Checked by default only when there ARE no real
        # candidates to default to instead -- once a search actually
        # finds artwork, the first real result is the more useful
        # default (one fewer click for the common case), same as
        # before "none" existed at all.
        none_id = f"art-{prefix}{basename}-none"
        none_checked = "" if candidates else " checked"
        none_cell = f"""
<div class="artwork-cell">
  <input type="radio" id="{none_id}" name="artwork_{prefix}{basename}" value="" form="{_ADD_FORM_ID}"{none_checked}>
  <label for="{none_id}" style="{cell_style}">{_NO_ARTWORK_ICON_SVG}</label>
</div>"""
        if not candidates:
            # One real cell less of filler now that the "none" cell
            # itself occupies the first slot -- keeps the same total
            # tile count as before per category.
            filler_count = max(_SKELETON_TILE_COUNTS[basename] - 1, 0)
            skeletons = "".join(
                f'<div class="artwork-cell artwork-skeleton" style="{cell_style}"></div>'
                for _ in range(filler_count)
            )
            sections.append(f"""
<div class="artwork-category"{category_style}>
  <h3>{html.escape(title)}</h3>
  <div class="artwork-row"{row_style}>{none_cell}{skeletons}</div>
</div>""")
            continue
        cells = [none_cell]
        for i, cand in enumerate(candidates):
            checked = " checked" if i == 0 else ""
            input_id = f"art-{prefix}{basename}-{i}"
            thumb = html.escape(cand.get("thumb") or cand["url"])
            url = html.escape(cand["url"])
            cells.append(f"""
<div class="artwork-cell">
  <input type="radio" id="{input_id}" name="artwork_{prefix}{basename}" value="{url}" form="{_ADD_FORM_ID}"{checked}>
  <label for="{input_id}" style="{cell_style}">
    <img src="{thumb}" loading="lazy" alt="">
  </label>
</div>""")
        sections.append(f"""
<div class="artwork-category"{category_style}>
  <h3>{html.escape(title)}</h3>
  <div class="artwork-row has-artwork"{row_style}>{''.join(cells)}</div>
</div>""")
    return "".join(sections)


def render_page(query="", couch_mode=False, browser="", sgdb_q="", matches=None, match_index=0,
                 candidates_by_category=None, resolved_url=None, chosen=None,
                 url_edit_appid="", url_edit_name="", url_loading=False,
                 ra_state=None, ra_candidates_by_category=None, ra_chosen=None, ra_loading=False,
                 em_state=None, em_candidates_by_category=None, em_chosen=None, em_loading=False):
    """Single page-builder for every state (home, unresolved input, no
    matches, a real workspace) -- all three columns are always present
    and always fully populated (placeholders when empty), rather than
    each state having its own bespoke partial layout.

    ra_*/em_* cover the RetroArch/Emulators tabs' own flows, entirely
    separate from the URL tab's and each other (different state,
    different match_name fields -- ra_match_name/em_match_name -- so
    none of the three ever collide as same-named inputs on the same Add
    form). Only one flow drives the single shared Add button/artwork
    column at a time: RetroArch, then Emulators, take priority over the
    URL tab once their own required picks are complete, since that's
    the more specific signal one of them is actually in progress."""
    matches = matches or []
    candidates_by_category = candidates_by_category or {}
    ra_state = ra_state or {}
    ra_candidates_by_category = ra_candidates_by_category or {}
    em_state = em_state or {}
    em_candidates_by_category = em_candidates_by_category or {}

    ra_console = ra_state.get("ra_console", "")
    ra_needs_bios = ra_console in retroarch_cores.CONSOLES_NEEDING_BIOS
    # Same fallback as em_ready's own keys/firmware -- a console whose
    # BIOS is already in place (see retroarch_cores.bios_installed)
    # shouldn't block Create just because this particular game didn't
    # pick it again, unless the user explicitly asked to override it via
    # ra_bios_skip (see _ra_picker_section).
    #
    # Split into "prereqs" (console/rom/bios) and "resolved" (SGDB has
    # actually finished, real artwork candidates exist and one is
    # selected -- see _artwork_picker_html's own always-something-
    # checked default) on purpose: without requiring ra_resolved too,
    # Create was clickable the instant prereqs were met even while a
    # slow SGDB search was still in flight (the ra_loading meta-refresh
    # flash, or the real network call behind it) -- confirmed live as
    # exactly the case a poor connection made easy to hit, submitting
    # whatever artwork selection happened to still be on the page from
    # before this particular search even started.
    ra_prereqs_ready = bool(
        ra_console and ra_state.get("ra_romfile")
        and (
            (ra_state.get("ra_biosfile") or (retroarch_cores.bios_installed(ra_console) and not ra_state.get("ra_bios_skip")))
            if ra_needs_bios else True
        )
    )
    ra_ready = ra_prereqs_ready and bool(ra_state.get("ra_resolved"))
    ra_awaiting_artwork = ra_prereqs_ready and not ra_state.get("ra_resolved")

    em_emulator = em_state.get("em_emulator", "")
    em_entry = standalone_emulators.EMULATORS.get(em_emulator)
    # Keys/firmware presence falls back to a real on-disk check (see
    # standalone_emulators.keys_installed/firmware_installed) when
    # nothing's picked in this exact request -- they're a one-time
    # install for the emulator itself, not per-shortcut state, so
    # re-visiting the tab (e.g. via an existing shortcut's own Edit
    # link) shouldn't permanently block Create just because the picker
    # wasn't touched again this time. em_keys_skip/em_firmware_skip
    # override that fallback when the user explicitly asked to pick a
    # different one (see _em_picker_section). Same prereqs/resolved
    # split as ra_ready above, same reasoning.
    em_prereqs_ready = bool(
        em_entry and em_state.get("em_romfile")
        and (
            all(
                em_state.get(f"em_{prefix}file")
                or (standalone_emulators.bios_slot_installed(em_emulator, prefix) and not em_state.get(f"em_{prefix}_skip"))
                for prefix, _label, *_rest in em_entry.get("bios_slots")
            )
            if em_entry.get("bios_slots")
            else (em_state.get("em_biosfile") if em_entry.get("needs_bios") else True)
        )
        and (
            (em_state.get("em_keysfile") or (standalone_emulators.keys_installed(em_emulator) and not em_state.get("em_keys_skip")))
            if em_entry.get("needs_keys") else True
        )
        and (
            (em_state.get("em_firmwarefile") or (standalone_emulators.firmware_installed(em_emulator) and not em_state.get("em_firmware_skip")))
            if em_entry.get("needs_firmware") else True
        )
    )
    em_ready = em_prereqs_ready and bool(em_state.get("em_resolved"))
    em_awaiting_artwork = em_prereqs_ready and not em_state.get("em_resolved")

    add_form = ""
    # Always present and pinned to the bottom, per the design handoff --
    # inert (not tied to any form) until a match/artwork exists to add.
    # id="selfsteam-add-button" is a stable AJAX-swap target (see
    # selfsteamTabFetch/SELFSTEAM_RA_SWAP_IDS/SELFSTEAM_EM_SWAP_IDS in PAGE_TAIL)
    # -- unlike add_form this element always exists in every render_page
    # code path, so it never needs a separately-wrapped placeholder the
    # way add_form does below.
    add_button = '<button type="button" id="selfsteam-add-button" disabled style="opacity:0.45;cursor:not-allowed">Create Steam Shortcut</button>'
    # The Add form is declared standalone (no visible children) and
    # everything that belongs to it -- the button, the artwork radios,
    # the active tab's own Name field -- is associated via form="..."
    # instead of DOM nesting. It must NOT visually wrap the URL tab
    # panel's own <form action="/search">: a <form> nested inside
    # another <form> is invalid HTML, and browsers resolve that by
    # silently merging the inner form's fields/buttons into the outer
    # one -- confirmed live, this made clicking "Search" submit /add
    # (with stale data) instead of actually searching.
    if ra_ready:
        ra_edit_appid = ra_state.get("ra_edit_appid", "")
        add_form = f"""
<form id="{_ADD_FORM_ID}" action="/add" method="post" onsubmit="selfsteamShowCreating(this)">
  <input type="hidden" name="ra_console" value="{html.escape(ra_console)}">
  <input type="hidden" name="ra_romfile" value="{html.escape(ra_state.get('ra_romfile', ''))}">
  <input type="hidden" name="ra_biosfile" value="{html.escape(ra_state.get('ra_biosfile', ''))}">
  <input type="hidden" name="ra_edit_appid" value="{html.escape(ra_edit_appid)}">
  <input type="hidden" name="ra_edit_name" value="{html.escape(ra_state.get('ra_edit_name', ''))}">
</form>
"""
        add_button_text = "Save Shortcut" if ra_edit_appid else "Create Steam Shortcut"
        add_button = f'<button type="submit" id="selfsteam-add-button" form="{_ADD_FORM_ID}">{add_button_text}</button>'
    elif em_ready:
        # onsubmit here, not onclick on the button: disabling a submit
        # button synchronously inside its own onclick can stop that same
        # click's default action (the actual form submission) from ever
        # firing, in which case the button just sits showing "Installing
        # ..." forever with no request ever having gone out. onsubmit
        # fires as part of the submission itself, so disabling there is
        # always safe -- the submission already happened by that point.
        #
        # data-installed lets selfsteamShowCreating show the generic
        # "Creating Shortcut" spinner instead of "Downloading <emulator>"
        # when the emulator's already there -- Create still blocks
        # briefly on _add_standalone_emulator_shortcut's own already-
        # installed check either way, but that's fast (a single
        # `flatpak info`), not the multi-minute-first-time-only download
        # the more specific wording is for. Checked fresh at render
        # time; /add re-checks it again for real regardless of what
        # this says.
        em_already_installed = standalone_emulators.installed(em_emulator)
        em_edit_appid = em_state.get("em_edit_appid", "")
        add_form = f"""
<form id="{_ADD_FORM_ID}" action="/add" method="post" onsubmit="selfsteamShowCreating(this)"
      data-emulator="{html.escape(em_emulator)}" data-installed="{"1" if em_already_installed else ""}">
  <input type="hidden" name="em_emulator" value="{html.escape(em_emulator)}">
  <input type="hidden" name="em_romfile" value="{html.escape(em_state.get('em_romfile', ''))}">
  <input type="hidden" name="em_biosfile" value="{html.escape(em_state.get('em_biosfile', ''))}">
  <input type="hidden" name="em_bios2file" value="{html.escape(em_state.get('em_bios2file', ''))}">
  <input type="hidden" name="em_bios3file" value="{html.escape(em_state.get('em_bios3file', ''))}">
  <input type="hidden" name="em_keysfile" value="{html.escape(em_state.get('em_keysfile', ''))}">
  <input type="hidden" name="em_firmwarefile" value="{html.escape(em_state.get('em_firmwarefile', ''))}">
  <input type="hidden" name="em_edit_appid" value="{html.escape(em_edit_appid)}">
  <input type="hidden" name="em_edit_name" value="{html.escape(em_state.get('em_edit_name', ''))}">
</form>
"""
        add_button_text = "Save Shortcut" if em_edit_appid else "Create Steam Shortcut"
        add_button = f'<button type="submit" id="selfsteam-add-button" form="{_ADD_FORM_ID}">{add_button_text}</button>'
    elif chosen is not None:
        couch_field = '<input type="hidden" name="couch_mode" value="1">' if couch_mode else ""
        # The Name field itself (see _url_tab_panel_html) is what
        # actually carries match_name to /add -- it's a real visible
        # input tagged form="{_ADD_FORM_ID}" so its live value (whatever
        # was typed, not just whatever the server last rendered) is
        # what's submitted, without needing to nest it inside this form.
        add_form = f"""
<form id="{_ADD_FORM_ID}" action="/add" method="post" onsubmit="selfsteamShowCreating(this)">
  <input type="hidden" name="query" value="{html.escape(query)}">
  <input type="hidden" name="resolved_url" value="{html.escape(resolved_url or '')}">
  <input type="hidden" name="browser" value="{html.escape(_default_browser(browser))}">
  <input type="hidden" name="url_edit_appid" value="{html.escape(url_edit_appid)}">
  <input type="hidden" name="url_edit_name" value="{html.escape(url_edit_name)}">
  {couch_field}
</form>
"""
        add_button_text = "Save Shortcut" if url_edit_appid else "Create Steam Shortcut"
        add_button = f'<button type="submit" id="selfsteam-add-button" form="{_ADD_FORM_ID}">{add_button_text}</button>'
    elif ra_awaiting_artwork or em_awaiting_artwork or url_loading:
        # Everything else (console/rom/bios, or emulator/rom/keys/
        # firmware) is already picked -- just waiting on the SGDB fetch
        # itself (see ra_ready/em_ready's own comment on why this is
        # split out from the base disabled state below), which can
        # genuinely take a few seconds on a slow connection. url_loading
        # is the URL tab's own equivalent of ra_awaiting_artwork/
        # em_awaiting_artwork -- see this function's own url_loading
        # handling below for why it needed its own two-step meta-refresh
        # to exist at all (the URL tab previously had no loading
        # feedback whatsoever, unlike RA/Emulators).
        add_button = (
            '<button type="button" id="selfsteam-add-button" disabled style="opacity:0.45;cursor:not-allowed">'
            'Searching for artwork...<span class="spinner"></span></button>'
        )

    # Reloading with the exact same state discards whatever's currently
    # typed in the Name field and re-renders its default (chosen's own
    # name) -- same "clear means revert to default" meaning the SGDB
    # search box's own clear button already has, not a literal empty
    # field (there's no way to distinguish "explicitly cleared" from
    # "never touched" without JS to track that).
    name_reset_href = f"/search?{_state_qs(query, couch_mode, browser, ra_state, em_state, sgdb_q=sgdb_q)}&match_index={match_index}"

    left = f"""
<div class="card">
  {_tab_bar_html()}
  <div class="tab-panels">
    <div class="tab-panel tab-panel-url">
      <form action="/search" method="get" style="display:flex;flex-direction:column;gap:0.9rem;flex:1;min-height:0">
        {_ra_hidden_fields(ra_state)}
        {_ra_hidden_fields(em_state)}
        {_url_tab_panel_html(query, couch_mode, browser, chosen, name_reset_href, ra_state, em_state)}
      </form>
    </div>
    <div class="tab-panel tab-panel-apps"><div class="coming-soon">Apps (Flathub/Installed) -- coming soon</div></div>
    <div class="tab-panel tab-panel-retroarch" id="selfsteam-ra-tab-panel">
      {_retroarch_tab_panel_html(ra_state, ra_chosen)}
    </div>
    <div class="tab-panel tab-panel-emulators" id="selfsteam-em-tab-panel">
      {_emulators_tab_panel_html(em_state, em_chosen)}
    </div>
  </div>
  {add_button}
</div>
"""
    # Every flavor of middle/right is always rendered, CSS (not this
    # branching) decides which one is visible -- the tab bar itself
    # switches tabs purely client-side with no reload, so a middle/
    # right column "chosen" server-side by which flow the *last actual
    # page load* happened to be about would silently stay wrong the
    # moment someone clicked a different tab label afterward without
    # reloading. Confirmed live: that's exactly what made the SGDB
    # search field "disappear" after visiting the RetroArch tab and
    # clicking back to URL without a fresh request.
    extra_head = ""
    if ra_loading:
        # Meta-refresh, not JS: an instant response (no SGDB call yet)
        # showing spinners, immediately followed by a second request
        # that does the real (possibly slow) search -- the zero-JS way
        # to give visible feedback during a request that would
        # otherwise just leave the previous page sitting there
        # unchanged while it runs.
        refresh_url = _ra_url("/new", ra_state, ra_resolved="1")
        extra_head = f'<meta http-equiv="refresh" content="0;url={html.escape(refresh_url)}">'
        ra_middle_html = _ra_middle_column_html(ra_state, [], extra_class="middle-panel-retroarch")
        ra_right_content = _ra_loading_artwork_html()
    else:
        ra_middle_html = _ra_middle_column_html(ra_state, [ra_chosen] if ra_chosen else [], extra_class="middle-panel-retroarch")
        ra_right_content = _artwork_picker_html(ra_candidates_by_category, prefix="ra_")

    if em_loading:
        em_refresh_url = _em_url("/new", em_state, em_resolved="1")
        # ra_loading and em_loading are never both true at once (each
        # only triggers from that tab's own /new request), so the plain
        # "last one wins" overwrite here never actually loses one.
        extra_head = f'<meta http-equiv="refresh" content="0;url={html.escape(em_refresh_url)}">'
        em_middle_html = _em_middle_column_html(em_state, [], extra_class="middle-panel-emulators")
        em_right_content = _ra_loading_artwork_html()
    else:
        em_middle_html = _em_middle_column_html(em_state, [em_chosen] if em_chosen else [], extra_class="middle-panel-emulators")
        em_right_content = _artwork_picker_html(em_candidates_by_category, prefix="em_")

    if url_loading:
        # Same two-step meta-refresh trick as ra_loading/em_loading
        # above -- the URL tab previously had none of this at all, so a
        # search just sat there unchanged for however long the real
        # SGDB round-trip took, easy to mistake for the click not
        # registering (same problem RA/Emulators already had a fix
        # for). url_loading_ack marks the follow-up request as the one
        # that should actually do the real resolve+search work (see the
        # /search GET handler) -- its own absence is what triggers this
        # branch in the first place.
        refresh_url = "/search?" + _state_qs(
            query, couch_mode, browser, ra_state, em_state,
            sgdb_q=sgdb_q, url_loading_ack="1", match_index=match_index or None,
            url_edit_appid=url_edit_appid or None, url_edit_name=url_edit_name or None,
        )
        extra_head = f'<meta http-equiv="refresh" content="0;url={html.escape(refresh_url)}">'
        middle_url = _middle_column_html(query, couch_mode, browser, sgdb_q, [], match_index, extra_class="middle-panel-url", ra_state=ra_state, em_state=em_state)
        right_url = f'<div class="card artwork-card right-panel-url">{_ra_loading_artwork_html()}</div>'
    else:
        middle_url = _middle_column_html(query, couch_mode, browser, sgdb_q, matches, match_index, extra_class="middle-panel-url", ra_state=ra_state, em_state=em_state)
        right_url = f'<div class="card artwork-card right-panel-url">{_artwork_picker_html(candidates_by_category)}</div>'
    right_ra = f'<div class="card artwork-card right-panel-retroarch" id="selfsteam-ra-right">{ra_right_content}</div>'
    right_em = f'<div class="card artwork-card right-panel-emulators" id="selfsteam-em-right">{em_right_content}</div>'

    # id="selfsteam-add-form-slot" is a stable AJAX-swap target even when
    # add_form itself is empty (no id of its own to grab in that case) --
    # see selfsteamTabFetch/SELFSTEAM_RA_SWAP_IDS/SELFSTEAM_EM_SWAP_IDS in
    # PAGE_TAIL, and add_button's own comment above for why that one
    # didn't need the same wrapper treatment.
    return render(f"""
<div id="selfsteam-add-form-slot">{add_form}</div>
{_tab_bar_targets_html()}
<div class="selfsteam-columns">
  <div class="selfsteam-left">{left}</div>
  <div class="selfsteam-middle">{middle_url}{ra_middle_html}{em_middle_html}</div>
  <div class="selfsteam-right">{right_url}{right_ra}{right_em}</div>
</div>
""", extra_head=extra_head)


def render_login(error=None):
    error_html = f'<p style="color:#c00;margin:0">{html.escape(error)}</p>' if error else ""
    # flex:none overrides the base .card rule's flex:1 -- that's meant
    # for the 3-column workspace where a card should stretch to fill a
    # bounded row height, but here (a lone card in main's flex column,
    # with nothing else sharing the row) it just stretched this card to
    # the full page height instead of sizing to its own content --
    # "very narrow and tall" for what should just be a compact box.
    return render(f"""
<div class="card" style="width:100%;max-width:360px;margin:2rem auto;flex:none">
  <h2 style="font-size:1.5rem">Enter code</h2>
  {error_html}
  <form id="selfsteam-login-form" action="/login" method="post">
    <input type="text" name="code" id="selfsteam-login-code" required autofocus maxlength="6"
           autocomplete="off"
           style="text-transform:uppercase; text-align:center; font-size:1.6rem; letter-spacing:0.4rem">
    <label style="display:flex;align-items:center;justify-content:center;gap:0.4rem;margin-top:0.9rem;font-size:0.9rem">
      <input type="checkbox" name="remember"> Remember this device
    </label>
  </form>
  <script>
  // Second deliberate JS exception (after the dark-mode toggle), same
  // reasoning: auto-submitting on the 6th character typed has no
  // pure-HTML equivalent. Wrong code -> the page reloads with an error
  // and an empty field (server-side, see render_login's error path)
  // -- so this naturally loops until the right code goes in, no extra
  // logic needed here.
  document.getElementById("selfsteam-login-code").addEventListener("input", function (e) {{
    e.target.value = e.target.value.toUpperCase();
    if (e.target.value.length === 6) document.getElementById("selfsteam-login-form").submit();
  }});
  </script>
</div>
""", page_title=_hostname(), show_back=False)


def render_done(name, ok, error=None):
    if ok:
        body = f"""
<div class="card" style="width:100%;max-width:420px;margin:2rem auto">
  <h2 style="color:var(--success-text)">Done</h2>
  <p><strong>{html.escape(name)}</strong> added. Steam has restarted -- it should
  show up in your library now.</p>
  <a class="btn" href="/new" style="display:inline-block;text-decoration:none">Add another</a>
</div>
"""
    else:
        body = f"""
<div class="card" style="width:100%;max-width:420px;margin:2rem auto">
  <h2 style="color:#c00">Failed</h2>
  <p>Couldn't add <strong>{html.escape(name)}</strong>: {html.escape(str(error))}</p>
</div>
"""
    return render(body, page_title=_hostname())


# Shared between the request thread that kicks off a commit and the
# background thread that actually runs it -- a commit blocks on Steam
# fully stopping and restarting (steam_restart.LAUNCH_POLL_TIMEOUT alone
# allows up to 150s), which used to mean the "Save changes and restart
# Steam" form's own POST just hung with a blank/spinning tab for that
# whole stretch. Running it in a background thread and polling this
# instead lets /commit redirect to /restarting immediately, which is
# what actually shows something (big "Restarting Steam..." text) for
# the duration instead of nothing.
_commit_status = {"running": False, "done": False, "ok": None, "label": "", "error": None}
_commit_status_lock = threading.Lock()


def _run_commit_in_background(items, label):
    def apply():
        for item in items:
            if item.get("type") == "remove":
                create_webapp.remove_gridge_shortcut(item["appid"])
                romfile = item.get("romfile")
                if romfile and os.path.isfile(romfile):
                    os.remove(romfile)
            else:
                create_webapp.register_steam_shortcut(
                    item["name"], item["url"], item["asset_paths"],
                    couch_mode=item["couch_mode"], browser_app_id=item.get("browser_app_id"),
                    launch_args=item.get("launch_args"),
                )

    try:
        maintenance.run_with_steam_stopped(apply, message=f"Applying {label}…")
        pending_queue.clear()
        with _commit_status_lock:
            _commit_status.update(running=False, done=True, ok=True, error=None)
    except Exception as e:  # noqa: BLE001 -- surfaced to the polling page, not swallowed
        with _commit_status_lock:
            _commit_status.update(running=False, done=True, ok=False, error=str(e))


def render_restarting():
    # Big, minimal, on purpose -- this is what actually fills the gap
    # that used to be a blank/spinning tab for up to ~150s (Steam's own
    # stop+relaunch cycle, see steam_restart.LAUNCH_POLL_TIMEOUT). Polls
    # /commit/status every second; redirects to / the moment Steam is
    # confirmed back up (done && ok), or swaps in the error text in
    # place if the commit itself failed (done && !ok) rather than
    # bouncing anywhere.
    body = """
<div id="selfsteam-restarting-view" style="width:100%;height:70vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:1rem">
  <div style="font-size:3rem;font-weight:700">Restarting Steam&hellip;</div>
</div>
<script>
(function poll() {
  fetch("/commit/status").then(function (r) { return r.json(); }).then(function (s) {
    if (s.done && s.ok) {
      window.location.href = "/";
      return;
    }
    if (s.done && !s.ok) {
      var view = document.getElementById("selfsteam-restarting-view");
      view.innerHTML =
        '<div style="font-size:2rem;font-weight:700;color:#c00">Something went wrong</div>'
        + '<div id="selfsteam-restarting-error" style="color:var(--text-dim);font-size:1.1rem"></div>'
        + '<a class="btn" href="/" style="display:inline-block;text-decoration:none;margin-top:1rem">Back to shortcuts</a>';
      // textContent, not innerHTML -- s.error is a raw exception
      // message from the server, not markup to trust.
      document.getElementById("selfsteam-restarting-error").textContent = s.error || "Unknown error";
      return;
    }
    setTimeout(poll, 1000);
  }).catch(function () { setTimeout(poll, 1000); });
})();
</script>
"""
    return render(body, page_title=_hostname(), show_back=False)


def render_pending():
    items = pending_queue.all_items()
    if not items:
        body = """
<div class="card" style="width:100%;max-width:800px;margin:2rem auto">
  <h2>No changes queued</h2>
  <p style="color:var(--text-dim)">Add a shortcut and it'll show up here, staged until you
  save changes and restart SteamOS.</p>
</div>
"""
        return render(body, page_title=_hostname())
    rows = []
    for i, item in enumerate(items):
        is_removal = item.get("type") == "remove"
        action_label = "Removing" if is_removal else "Adding"
        # item["url"] is None for RetroArch/launch_args-based items (no
        # URL at all) -- html.escape(None) raises, which previously
        # killed the /pending request thread with no response sent at
        # all ("clicking the queue counter does nothing").
        detail = "" if is_removal or not item.get("url") else f'<div style="color:var(--text-dim);font-size:0.85rem">{html.escape(item["url"])}</div>'
        rows.append(f"""
<div class="card" style="width:100%;max-width:800px;margin:0 auto;flex-direction:row;align-items:center;justify-content:space-between">
  <div>
    <span style="color:var(--text-dim);font-size:0.8rem;text-transform:uppercase">{action_label}</span>
    <div><strong>{html.escape(item['name'])}</strong></div>
    {detail}
  </div>
  <form action="/pending/remove" method="post" style="margin:0">
    <input type="hidden" name="index" value="{i}">
    <button type="submit" class="secondary" style="width:auto;padding:0.5rem 1rem">Cancel</button>
  </form>
</div>""")
    body = f"""
<div style="width:100%;max-width:800px;margin:2rem auto;display:flex;flex-direction:column;gap:1rem">
  <h2>{len(items)} change{"s" if len(items) != 1 else ""} queued</h2>
  {"".join(rows)}
</div>
"""
    return render(body, page_title=_hostname())


def render_settings(error=None):
    has_key = sgdb.has_api_key()
    current_key = config.get_sgdb_api_key() or ""
    status_html = (
        '<p style="color:var(--success-text);text-align:center">&#10003; A key is currently configured and verified.</p>'
        if has_key else
        '<p style="color:#8a6d1a;text-align:center">&#9888; No key configured yet -- shortcuts can still be '
        'added without artwork, but SGDB search/matching won\'t work until one is set.</p>'
    )
    error_html = f'<p style="color:#c00;text-align:center">{html.escape(error)}</p>' if error else ""
    return render(f"""
<div class="card" style="width:100%;max-width:480px;margin:2rem auto;flex:none">
  {status_html}
  {error_html}
  <form action="/key" method="post" style="display:flex;flex-direction:column;gap:0.9rem">
    <div class="field-group">
      <label class="field-label" for="selfsteam-sgdb-key">SteamGridDB API key</label>
      <div class="field-with-clear">
        <input type="text" name="sgdb_api_key" id="selfsteam-sgdb-key" value="{html.escape(current_key)}"
               placeholder="Paste your key here" autocomplete="off">
        <button type="submit" formaction="/key/remove" formnovalidate class="field-clear-btn" title="Remove key">&#10005;</button>
      </div>
      <a href="https://www.steamgriddb.com/profile/preferences/api" target="_blank" rel="noopener" style="font-size:0.85rem">Get a free key at steamgriddb.com</a>
    </div>
    <button type="submit">Save</button>
  </form>
</div>
<div class="card" style="width:100%;max-width:480px;margin:0 auto 2rem;flex:none">
  <p style="text-align:center;color:var(--text-dim);margin-top:0">
    Sign out from server every device that checked "Remember this
    device" at login.
  </p>
  <form action="/key/forget-devices" method="post">
    <button type="submit" class="secondary" style="width:100%">Forget all remembered devices</button>
  </form>
</div>
""", page_title=_hostname())


def _resolve_matches(query, resolved, sgdb_q=None):
    """SGDB matches for a resolved query, falling back to a single
    synthetic match (id=None, no artwork step -- see _fetch_candidates's
    own id=None guard) whenever there's no real match to offer: no SGDB
    key configured, or a real search that came back with zero results
    (a valid URL/service with nothing on SGDB, e.g. a niche site).
    Either way a resolved URL must always be addable -- previously an
    empty real search left the caller with chosen=None and a disabled
    Add button, and before that, sgdb.search()/get_game() with no key
    at all raised straight through uncaught here, silently killing the
    request thread with no response sent at all ("the page didn't
    respond")."""
    name = resolved.name or create_webapp.clean_shortcut_name(query)
    if not sgdb.has_api_key():
        return [{"id": None, "name": name}]
    # Lowercased -- keeps what's actually sent to SGDB's search matching
    # what the search box displays (_display_name/_ra_display_term), so
    # the box is a true record of the term that produced these results.
    if sgdb_q:
        matches = sgdb.search(sgdb_q.lower())
    elif resolved.sgdb_id is not None:
        matches = [sgdb.get_game(resolved.sgdb_id)]
    else:
        matches = sgdb.search(name.lower())
    return matches or [{"id": None, "name": name}]


def _poster_card_html(shortcut, pending_removal_appids):
    appid = shortcut["appid"]
    name = shortcut["name"]
    grid_source = create_webapp.find_grid_image_for_appid(appid) if appid is not None else None
    has_artwork = grid_source is not None
    if has_artwork:
        # ?v=<source mtime> -- lets the browser cache the thumbnail
        # aggressively (see _serve_grid_image's own long max-age) while
        # still fetching a fresh one the moment the real artwork actually
        # changes (re-picking on Edit), since that's a different URL
        # rather than the same one now pointing at different bytes.
        cache_bust = int(os.path.getmtime(grid_source))
        art_html = f'<img class="poster-art" src="/grid-image/{appid}?v={cache_bust}" loading="lazy" alt="{html.escape(name)}">'
    else:
        # No grid image on disk -- rather than a broken <img> or a blank
        # panel, show the shortcut's own name so the poster still reads
        # as *that* shortcut instead of an empty tile.
        art_html = f'<div class="poster-art poster-art-noimg"><span>{html.escape(name)}</span></div>'
    # Reuses the exact same entry point real shortcut creation goes
    # through for whichever tab actually made it -- /search for a URL
    # tab shortcut, /new#tab-retroarch (with its console+romfile) for a
    # RetroArch one, /new#tab-emulators (with its emulator+romfile) for
    # a standalone-emulator one -- so Edit always lands back on the
    # right tab, pre-populated, instead of always dumping every
    # shortcut into the URL tab regardless of how it was really
    # created. Searching by the
    # shortcut's own already-known URL/ROM runs SGDB matching
    # immediately (no re-typing), and picking new artwork or editing the
    # Name field there and hitting Create Steam Shortcut replaces this
    # shortcut in place rather than duplicating it: add_shortcut's own
    # appid is deterministic from exe+name, and it already dedups any
    # existing entry with the same name before writing. One "Edit" icon,
    # not separate name/artwork ones -- both would point at this exact
    # same page anyway.
    if shortcut.get("ra_console"):
        # ra_romfile state is a path *relative* to _RA_ROOT (see
        # _ra_safe_join/_ra_list_rows), but LaunchOptions stores the
        # absolute path RetroArch actually needs -- feeding the absolute
        # path back in as-is would double-join against _RA_ROOT inside
        # _ra_safe_join and resolve to a bogus, always-missing path.
        romfile_rel = os.path.relpath(shortcut["ra_romfile"], _RA_ROOT)
        edit_href = _ra_url("/new", {
            "ra_console": shortcut["ra_console"],
            "ra_romfile": romfile_rel,
            # See _RA_STATE_KEYS' own comment on ra_edit_appid/
            # ra_edit_name -- carries this shortcut's current identity
            # forward so the Add form knows it's editing (swap the
            # button to "Save Shortcut") and can clean up the old entry
            # itself if the Name field changes before submitting.
            "ra_edit_appid": str(appid), "ra_edit_name": name,
        })
    elif shortcut.get("em_emulator"):
        # Same relative-vs-absolute reasoning as ra_romfile above.
        romfile_rel = os.path.relpath(shortcut["em_romfile"], _RA_ROOT)
        edit_href = _em_url("/new", {
            "em_emulator": shortcut["em_emulator"],
            "em_romfile": romfile_rel,
            "em_edit_appid": str(appid), "em_edit_name": name,
        })
    else:
        edit_href = (
            f"/search?q={urllib.parse.quote(shortcut['url'] or name)}"
            f"&url_edit_appid={urllib.parse.quote(str(appid))}&url_edit_name={urllib.parse.quote(name)}"
        )
    # A RetroArch/Emulators-tab shortcut's ROM might be a local pick
    # (referenced in place, wherever it already lives -- see the local
    # file browser) rather than something SelfSteam uploaded itself, so
    # "delete on remove" isn't automatically safe the way it'd be for a
    # copy SelfSteam owns: it could be the user's own real ROM sitting
    # somewhere in their existing library. A real confirmation page
    # (see render_remove_confirm) asks which they actually want instead
    # of ever silently deleting a file SelfSteam doesn't own the lifecycle
    # of. URL-tab shortcuts have no file at stake at all, so they skip
    # straight to the one-click removal below, same as before.
    romfile = shortcut.get("ra_romfile") or shortcut.get("em_romfile")
    has_romfile = bool(romfile)
    if has_romfile:
        remove_control = (
            f'<a href="/shortcuts/remove-confirm?appid={urllib.parse.quote(str(appid))}&name={urllib.parse.quote(name)}'
            f'&romfile={urllib.parse.quote(romfile)}" '
            f'class="poster-icon-btn" title="Remove shortcut">{_TRASH_ICON_SVG}</a>'
        )
    else:
        remove_control = f"""
    <form action="/shortcuts/remove" method="post" style="margin:0">
      <input type="hidden" name="appid" value="{html.escape(str(appid))}">
      <input type="hidden" name="name" value="{html.escape(name)}">
      <button type="submit" class="poster-icon-btn" title="Remove shortcut">{_TRASH_ICON_SVG}</button>
    </form>"""
    # Greyed out once queued for removal (not yet committed -- see
    # _commit_pending/the "Save changes and restart Steam" button) so
    # the gallery visibly reflects a pending change instead of looking
    # like nothing happened until the next actual commit.
    pending_class = " pending-removal" if str(appid) in pending_removal_appids else ""
    return f"""
<div class="shortcut-poster{pending_class}">
  <div class="poster-frame"></div>
  {art_html}
  <div class="poster-icons">
    <a href="{edit_href}" class="poster-icon-btn" title="Edit">{_EDIT_NAME_ICON_SVG}</a>
    {remove_control}
  </div>
</div>"""


def render_remove_confirm(appid, name, romfile):
    # Only reachable for shortcuts that actually have a romfile (see
    # _poster_card_html's has_romfile branch) -- URL-tab shortcuts skip
    # this page entirely and go straight to the one-click POST, since
    # there's no file to ask about. delete_file is opt-in, not the
    # default action, because a locally-picked ROM is referenced in
    # place wherever it already lives on disk, not copied into a
    # SelfSteam-owned folder -- deleting it by default could destroy a
    # file the user never asked SelfSteam to own.
    appid_html = html.escape(str(appid))
    name_html = html.escape(name)
    romfile_html = html.escape(romfile)
    return render(f"""
<div class="card" style="width:100%;max-width:420px;margin:2rem auto">
  <h2>Remove shortcut</h2>
  <p style="word-break:break-all;color:var(--text-dim)">{romfile_html}</p>
  <form action="/shortcuts/remove" method="post" style="display:flex;flex-direction:column;gap:0.6rem">
    <input type="hidden" name="appid" value="{appid_html}">
    <input type="hidden" name="name" value="{name_html}">
    <button type="submit" class="btn">Remove shortcut only</button>
    <button type="submit" name="delete_file" value="1" class="btn" style="background:#c00;color:#fff">Remove shortcut and delete ROM file</button>
  </form>
</div>
""", page_title=_hostname(), show_back=False)


def render_gallery():
    # No cap on how many render -- explicitly meant to hold however many
    # shortcuts exist (a user can have hundreds), not a paginated/lazy
    # subset. CSS grid + the browser's own image lazy-loading is what
    # keeps that reasonable, not limiting the query.
    shortcuts = create_webapp.list_gridge_shortcuts()
    pending_removal_appids = {
        str(item["appid"]) for item in pending_queue.all_items() if item.get("type") == "remove"
    }
    cards_html = "".join(_poster_card_html(s, pending_removal_appids) for s in shortcuts)
    return render(f"""
<div class="gallery-header">
  <h2>Non Steam shortcuts</h2>
</div>
<div class="gallery-grid">
  <a class="add-poster-frame" href="/new" title="Add a shortcut">
    <span class="add-poster">
      <span class="add-poster-plus">+</span>
    </span>
  </a>
  {cards_html}
</div>
""", page_title=_hostname(), show_back=False)


def _fetch_candidates(game_id):
    if game_id is None:
        return {}
    return {basename: fetch(game_id) for basename, _title, fetch, _w, _h in ARTWORK_CATEGORIES}


class Handler(BaseHTTPRequestHandler):
    # No timeout at all by default -- a stalled/dropped connection mid-
    # upload (flaky wifi, a VPN silently dropping a long-lived transfer,
    # a backgrounded browser tab) left rfile.read() blocking forever with
    # nothing to show for it, which is exactly what "keeps on uploading
    # forever" looked like: no error, no timeout, just an infinite spin.
    # This is a per-read idle timeout (StreamRequestHandler.setup() calls
    # self.connection.settimeout(self.timeout)), not a total-transfer
    # cap -- a genuinely slow-but-still-progressing large upload keeps
    # succeeding as long as *some* bytes arrive within each window: only
    # a connection that's truly gone silent gets cut loose.
    timeout = 60

    def _send_html(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location, set_cookie=None):
        # set_cookie may be a single Set-Cookie value or a list of them
        # (e.g. login setting both the session cookie and, if "remember
        # this device" was checked, the remember cookie in the same
        # response) -- HTTP allows repeating the header, it just can't
        # be combined into one.
        self.send_response(303)
        self.send_header("Location", location)
        if set_cookie is not None:
            cookies = set_cookie if isinstance(set_cookie, list) else [set_cookie]
            for c in cookies:
                self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    _IMAGE_CONTENT_TYPES = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }

    def _serve_grid_image(self, appid):
        # appid comes straight from the URL path -- validated as a bare
        # positive int before it ever reaches a filesystem call, same
        # reasoning as any other path built from request input.
        if not appid.isdigit():
            self._send_html(render("<p>Not found</p>"), status=404)
            return
        # A small cached webp thumbnail, not Steam's own (often multi-MB)
        # original -- see create_webapp.thumbnail_for_appid. Falls back
        # to serving the real original path if thumbnailing isn't
        # possible, so this never regresses to a broken image.
        path = create_webapp.thumbnail_for_appid(int(appid))
        if not path or not os.path.exists(path):
            self._send_html(render("<p>Not found</p>"), status=404)
            return
        ext = os.path.splitext(path)[1].lower()
        content_type = self._IMAGE_CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The browser's own cache, on top of the on-disk thumbnail cache --
        # skips even the local request entirely on a repeat gallery visit.
        # A week is safe: re-picking artwork changes the underlying file,
        # which changes what thumbnail_for_appid regenerates and serves
        # next time regardless of what a browser has cached under this
        # same URL.
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(body)

    def _cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        jar.load(raw)
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def _session_token(self):
        return self._cookie(SESSION_COOKIE)

    def _is_authenticated(self):
        # A valid remember-device cookie counts the same as a live
        # session -- that's the entire point of "remember this device":
        # skip re-entering the code, not just skip it once.
        if auth.is_authenticated(self._session_token()):
            return True
        return auth.is_remembered(self._cookie(REMEMBER_COOKIE))

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
            # A remembered device (or a still-live session) landing on
            # /login directly -- e.g. an old bookmark/tab -- has nothing
            # to do here, so send it straight to the real homepage
            # instead of making it look at a code entry box it doesn't
            # need.
            if self._is_authenticated():
                self._redirect("/")
                return
            # Otherwise show the code whenever anyone lands on /login
            # while not authenticated -- whether they got here via the
            # redirect below, or navigated straight to /login themselves.
            auth_display.ensure_shown()
            self._send_html(render_login())
            return

        if not self._is_authenticated():
            auth_display.ensure_shown()
            self._redirect("/login")
            return

        if parsed.path == "/vendor/poster-frame.webp":
            # Behind auth (unlike darkreader.js above, which the
            # unauthenticated login page itself needs) -- only used on
            # the gallery, which is never reachable unauthenticated.
            with open(_POSTER_FRAME_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/vendor/name-field-wand.webp":
            with open(_NAME_FIELD_WAND_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/":
            self._send_html(render_gallery())
            return

        if parsed.path == "/new":
            ra_state = _ra_state_from_params(params)
            em_state = _em_state_from_params(params)
            romfile = ra_state.get("ra_romfile")
            em_romfile = em_state.get("em_romfile")
            # A freshly-picked ROM (ra_resolved/em_resolved not yet set
            # for it -- see _ra_list_rows/_em_list_rows, which clear it
            # on every new pick) gets the fast loading response first:
            # the real SGDB search can take a few seconds, and skipping
            # straight to it left the previous page sitting there
            # unchanged the whole time, easy to mistake for the click
            # not registering. Only one of ra_*/em_* is ever the one
            # actually driving a given request (each tab's own links
            # only ever set its own romfile), so checking ra's first and
            # falling through to em's never actually races.
            if romfile and not ra_state.get("ra_resolved"):
                self._send_html(render_page(ra_state=ra_state, ra_loading=True, em_state=em_state))
                return
            if em_romfile and not em_state.get("em_resolved"):
                self._send_html(render_page(ra_state=ra_state, em_state=em_state, em_loading=True))
                return
            ra_chosen = None
            ra_candidates = {}
            if romfile:
                # Reuses _resolve_matches's own no-key/zero-results
                # fallback (a synthetic single "match" so a shortcut is
                # still addable either way) by handing it a bare
                # Resolved carrying just the guessed name -- same
                # contract the URL tab's own resolution already relies
                # on, not a separate RetroArch-specific fallback.
                guessed = _ra_guess_name_from_filename(romfile)
                ra_matches = _resolve_matches(
                    guessed, service_resolver.Resolved(name=guessed), ra_state.get("ra_sgdb_q"),
                )
                ra_chosen = ra_matches[0]
                ra_candidates = _fetch_candidates(ra_chosen["id"])
            em_chosen = None
            em_candidates = {}
            if em_romfile:
                em_guessed = _ra_guess_name_from_filename(em_romfile)
                em_matches = _resolve_matches(
                    em_guessed, service_resolver.Resolved(name=em_guessed), em_state.get("em_sgdb_q"),
                )
                em_chosen = em_matches[0]
                em_candidates = _fetch_candidates(em_chosen["id"])
            self._send_html(render_page(
                ra_state=ra_state, ra_candidates_by_category=ra_candidates, ra_chosen=ra_chosen,
                em_state=em_state, em_candidates_by_category=em_candidates, em_chosen=em_chosen,
            ))
            return

        if parsed.path.startswith("/grid-image/"):
            self._serve_grid_image(parsed.path[len("/grid-image/"):])
            return

        if parsed.path == "/pending":
            self._send_html(render_pending())
            return

        if parsed.path == "/restarting":
            self._send_html(render_restarting())
            return

        if parsed.path == "/commit/status":
            with _commit_status_lock:
                status = dict(_commit_status)
            self._send_json({"done": status["done"], "ok": status["ok"], "error": status["error"]})
            return

        if parsed.path == "/key":
            self._send_html(render_settings())
            return

        if parsed.path == "/shortcuts/remove-confirm":
            appid = (params.get("appid") or [""])[0]
            name = (params.get("name") or [""])[0]
            romfile = (params.get("romfile") or [""])[0]
            self._send_html(render_remove_confirm(appid, name, romfile))
            return

        if parsed.path == "/search":
            query = (params.get("q") or [""])[0].strip()
            couch_mode = bool(params.get("couch_mode"))
            browser = (params.get("browser") or [""])[0]
            sgdb_q = (params.get("sgdb_q") or [""])[0].strip()
            match_index = int((params.get("match_index") or ["0"])[0])
            # Every normal URL tab interaction (Search, picking a match,
            # the SGDB override) goes through this route -- without
            # reading and re-threading ra_state/em_state, each one
            # silently wiped any in-progress RetroArch/Emulators pick,
            # which is what actually made the SGDB search field
            # "disappear" after visiting the RetroArch tab (not a
            # rendering bug -- the state was really gone by the time
            # /new#tab-retroarch was reached again).
            ra_state = _ra_state_from_params(params)
            em_state = _em_state_from_params(params)
            # Carried through the same way as ra_state/em_state above --
            # set only when this /search came from the gallery's own
            # Edit link (see edit_href's own comment), and read back by
            # render_page to swap the Add button to "Save Shortcut" and
            # let /add clean up the old entry if the Name field changes.
            url_edit_appid = (params.get("url_edit_appid") or [""])[0]
            url_edit_name = (params.get("url_edit_name") or [""])[0]
            if not query:
                self._send_html(render_page(
                    browser=browser, ra_state=ra_state, em_state=em_state,
                    url_edit_appid=url_edit_appid, url_edit_name=url_edit_name,
                ))
                return

            # Same fast-instant-response-then-real-work split as ra_loading/
            # em_loading (see render_page's own url_loading comment) -- the
            # URL tab previously had none of this, so a search (or picking
            # a different match, or an SGDB override -- anything that gets
            # here with a query and reaches this point) just left the
            # previous page sitting there unchanged for however long the
            # real SGDB round-trip took. url_loading_ack's absence is what
            # triggers this; its presence (added by that same loading
            # render's own meta-refresh) is what lets this fall through to
            # the real work below on the follow-up request.
            if not params.get("url_loading_ack"):
                self._send_html(render_page(
                    query, couch_mode, browser, sgdb_q, match_index=match_index,
                    ra_state=ra_state, em_state=em_state, url_loading=True,
                    url_edit_appid=url_edit_appid, url_edit_name=url_edit_name,
                ))
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
                self._send_html(render_page(
                    query, couch_mode, browser, ra_state=ra_state, em_state=em_state,
                    url_edit_appid=url_edit_appid, url_edit_name=url_edit_name,
                ))
                return

            # sgdb_q (the magnifying-glass direct search) overrides
            # what SGDB is searched for, independent of what the URL
            # itself resolves to -- e.g. keep adding netflix.com while
            # picking artwork from an entirely different SGDB entry.
            matches = _resolve_matches(query, resolved, sgdb_q)
            match_index = min(match_index, len(matches) - 1)
            candidates = _fetch_candidates(matches[match_index]["id"])
            self._send_html(render_page(
                query, couch_mode, browser, sgdb_q, matches, match_index,
                candidates, resolved.url, chosen=matches[match_index],
                ra_state=ra_state, em_state=em_state,
                url_edit_appid=url_edit_appid, url_edit_name=url_edit_name,
            ))
            return

        self._send_html(render(f"<p>Not found: {html.escape(parsed.path)}</p>"), status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/new/upload":
            # Handled before the generic body-read below: that line
            # buffers the whole POST body in memory at once, which is
            # exactly what a multi-GB ROM/BIOS upload can't afford --
            # multipart_upload streams the file straight to disk
            # instead, so this route reads self.rfile itself rather
            # than through the shared `body` variable every other
            # route uses.
            if not self._is_authenticated():
                auth_display.ensure_shown()
                self._redirect("/login")
                return
            self._handle_ra_upload()
            return

        if parsed.path == "/new/upload-em":
            # Same streaming-upload reasoning as /new/upload above, for
            # the Emulators tab's own ROM/BIOS/keys pickers.
            if not self._is_authenticated():
                auth_display.ensure_shown()
                self._redirect("/login")
                return
            self._handle_em_upload()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = urllib.parse.parse_qs(body)

        if parsed.path == "/login":
            submitted = (params.get("code") or [""])[0]
            remember = bool(params.get("remember"))
            token = auth.try_login(submitted)
            if token is None:
                self._send_html(render_login(error="Wrong or expired code -- check the TV for the current one."))
                return
            auth_display.dismiss()
            cookie = http.cookies.SimpleCookie()
            cookie[SESSION_COOKIE] = token
            cookie[SESSION_COOKIE]["path"] = "/"
            cookie[SESSION_COOKIE]["max-age"] = auth.SESSION_TTL
            cookies = [cookie[SESSION_COOKIE].OutputString()]
            if remember:
                remember_cookie = http.cookies.SimpleCookie()
                remember_cookie[REMEMBER_COOKIE] = auth.remember_device()
                remember_cookie[REMEMBER_COOKIE]["path"] = "/"
                remember_cookie[REMEMBER_COOKIE]["max-age"] = auth.REMEMBER_TTL
                cookies.append(remember_cookie[REMEMBER_COOKIE].OutputString())
            self._redirect("/", set_cookie=cookies)
            return

        if not self._is_authenticated():
            auth_display.ensure_shown()
            self._redirect("/login")
            return

        if parsed.path == "/pending/remove":
            index = int((params.get("index") or ["-1"])[0])
            pending_queue.remove(index)
            self._redirect("/pending")
            return

        if parsed.path == "/shortcuts/remove":
            appid = (params.get("appid") or [""])[0]
            name = (params.get("name") or [""])[0]
            delete_file = bool(params.get("delete_file"))
            if appid:
                # romfile is only ever looked up (and only ever deleted
                # at commit time) when the user explicitly opted into it
                # on the /shortcuts/remove-confirm page -- a locally
                # picked ROM is referenced in place, not copied, so
                # deleting it by default could destroy a file the user
                # never asked SelfSteam to own. Looked up fresh (not
                # trusted from the form's own hidden fields) so this
                # can't be spoofed into deleting an arbitrary path --
                # only a romfile create_webapp itself already found by
                # scanning the real shortcuts.vdf for this exact appid
                # is ever considered.
                romfile = None
                if delete_file:
                    match = next((s for s in create_webapp.list_gridge_shortcuts() if str(s["appid"]) == appid), None)
                    if match:
                        romfile = match.get("ra_romfile") or match.get("em_romfile")
                pending_queue.add_removal(appid, name, romfile=romfile)
            self._redirect("/")
            return

        if parsed.path == "/commit":
            self._commit_pending()
            return

        if parsed.path == "/key":
            key = (params.get("sgdb_api_key") or [""])[0].strip()
            if key:
                try:
                    valid = sgdb.verify_api_key(key)
                except sgdb.SGDBError:
                    # Couldn't reach SGDB to check at all (network down,
                    # timeout) -- save anyway rather than blocking on a
                    # check that itself failed; a genuinely bad key still
                    # gets caught the next time something actually
                    # searches, just not immediately here.
                    valid = True
                if not valid:
                    self._send_html(render_settings(error="That key was rejected by SteamGridDB -- double check it and try again."))
                    return
                config.set_sgdb_api_key(key)
                self._redirect("/")
            else:
                # Submitting Save with the field emptied (manually
                # deleted, not just the dedicated clear button below)
                # removes the key too -- "empty" reads as "I want this
                # gone", not "do nothing".
                config.clear_sgdb_api_key()
                self._redirect("/key")
            return

        if parsed.path == "/key/remove":
            config.clear_sgdb_api_key()
            self._redirect("/key")
            return

        if parsed.path == "/key/forget-devices":
            auth.forget_all_devices()
            # Also expire the cookie on the browser making this request --
            # forget_all_devices() already invalidated it server-side, but
            # there's no reason to leave a now-useless cookie sitting
            # around on the one device that's guaranteed to be here.
            expired = http.cookies.SimpleCookie()
            expired[REMEMBER_COOKIE] = ""
            expired[REMEMBER_COOKIE]["path"] = "/"
            expired[REMEMBER_COOKIE]["max-age"] = 0
            self._redirect("/key", set_cookie=expired[REMEMBER_COOKIE].OutputString())
            return

        if parsed.path != "/add":
            self._send_html(render("<p>Not found</p>"), status=404)
            return

        ra_console = (params.get("ra_console") or [""])[0]
        ra_romfile = (params.get("ra_romfile") or [""])[0]
        if ra_console and ra_romfile:
            self._add_retroarch_shortcut(params, ra_console, ra_romfile)
            return

        em_emulator = (params.get("em_emulator") or [""])[0]
        em_romfile = (params.get("em_romfile") or [""])[0]
        if em_emulator and em_romfile:
            self._add_standalone_emulator_shortcut(params, em_emulator, em_romfile)
            return

        query = (params.get("query") or [""])[0]
        couch_mode = bool(params.get("couch_mode"))
        # match_name comes straight from the Name field's own live value
        # (see _url_tab_panel_html) -- used as-is here rather than
        # re-resolving matches from SGDB all over again just to recover
        # a name, so editing it doesn't require a second live SGDB
        # round-trip on every Add.
        match_name = (params.get("match_name") or [""])[0] or create_webapp.clean_shortcut_name(query)
        url = (params.get("resolved_url") or [""])[0]
        browser = (params.get("browser") or [""])[0]
        if browser:
            config.set_last_browser(browser)

        if not url:
            self._send_html(render_done(match_name, ok=False, error="couldn't resolve a URL for this shortcut, please search again"))
            return

        try:
            # Downloading/saving artwork doesn't touch Steam or
            # shortcuts.vdf at all, so it doesn't need a maintenance
            # window -- only actually queues the shortcut (name, url,
            # already-downloaded asset paths) for the next "Save Changes
            # and Restart Steam OS" commit, instead of stopping Steam
            # for every single shortcut added. Redirects to a blank,
            # cleaned /new#tab-url (not the home gallery) so the next
            # shortcut can be added immediately, on the same tab, without
            # detouring through the gallery in between.
            slug = create_webapp.slugify(match_name)
            selections = {}
            for basename, _title, _fetch, _w, _h in ARTWORK_CATEGORIES:
                selection_url = (params.get(f"artwork_{basename}") or [None])[0]
                selections[basename] = {"url": selection_url} if selection_url else None
            asset_paths = create_webapp.download_selected_assets(slug, selections)
            _queue_edit_rename_cleanup(params, "url", match_name)
            pending_queue.add(match_name, url, couch_mode, asset_paths, browser_app_id=browser or None)
            self._redirect("/new#tab-url")
        except Exception as e:  # noqa: BLE001 -- surfaced to the user, not swallowed
            self._send_html(render_done(match_name, ok=False, error=e))

    def _add_retroarch_shortcut(self, params, ra_console, ra_romfile):
        # ra_match_name comes straight from the RetroArch tab's own
        # Name field live value -- same reasoning as the URL tab's own
        # match_name, a separate field/name so the two Name fields
        # (only one visible at a time, but both real DOM inputs) never
        # collide as two values for one field on the shared Add form.
        ra_biosfile = (params.get("ra_biosfile") or [""])[0]
        match_name = (params.get("ra_match_name") or [""])[0] or _ra_guess_name_from_filename(ra_romfile) or ra_console

        romfile_abs = _ra_safe_join(ra_romfile)
        if romfile_abs is None or not os.path.isfile(romfile_abs):
            self._send_html(render_done(match_name, ok=False, error="ROM file not found -- please pick it again"))
            return
        biosfile_abs = None
        if ra_biosfile:
            biosfile_abs = _ra_safe_join(ra_biosfile)
            if biosfile_abs is None or not os.path.isfile(biosfile_abs):
                self._send_html(render_done(match_name, ok=False, error="BIOS file not found -- please pick it again"))
                return

        try:
            # Installing RetroArch itself is a one-time cost (confirmed
            # live: several minutes the first time, since it pulls a
            # full runtime dependency alongside it) -- blocking on it
            # here rather than a background/polling flow is a real,
            # deliberate v1 tradeoff: simple to build, but this one
            # click can be slow the very first time a machine ever adds
            # a RetroArch shortcut. Every install after that is instant
            # (already-installed check short-circuits). Core installs
            # are fast (a few MB each from libretro's buildbot) and
            # don't have this concern.
            if not retroarch_cores.retroarch_installed():
                retroarch_cores.install_retroarch()
            if not retroarch_cores.core_installed(ra_console):
                retroarch_cores.install_core(ra_console)
            if biosfile_abs:
                retroarch_cores.install_bios(biosfile_abs)

            args = retroarch_cores.launch_args(ra_console, romfile_abs)
            if args is None:
                raise RuntimeError("flatpak isn't available on this host")

            slug = create_webapp.slugify(match_name)
            selections = {}
            # artwork_ra_{basename}, not artwork_{basename} -- see
            # _artwork_picker_html's own comment on why each tab's own
            # radio group needs a distinct name, this being the RA tab's.
            for basename, _title, _fetch, _w, _h in ARTWORK_CATEGORIES:
                selection_url = (params.get(f"artwork_ra_{basename}") or [None])[0]
                selections[basename] = {"url": selection_url} if selection_url else None
            asset_paths = create_webapp.download_selected_assets(slug, selections)
            _queue_edit_rename_cleanup(params, "ra", match_name)
            pending_queue.add(match_name, None, False, asset_paths, launch_args=args)
            # Same "stay on this tab, cleaned" redirect as the URL tab's
            # own /add -- see its comment above. Console and the ROM
            # picker's own folder/source carried forward (everything
            # else dropped) -- someone queueing several games for the
            # same console, or several ROMs sitting in the same folder,
            # back to back shouldn't have to re-pick either every single
            # time, unlike the ROM file/BIOS/search state itself, which
            # really is specific to the one game just queued.
            ra_rompath = (params.get("ra_rompath") or [""])[0]
            ra_romsource = (params.get("ra_romsource") or [""])[0]
            self._redirect(_ra_url("/new", {
                "ra_console": ra_console, "ra_rompath": ra_rompath, "ra_romsource": ra_romsource,
            }))
        except Exception as e:  # noqa: BLE001 -- surfaced to the user, not swallowed
            self._send_html(render_done(match_name, ok=False, error=e))

    def _add_standalone_emulator_shortcut(self, params, em_emulator, em_romfile):
        # em_match_name comes straight from the Emulators tab's own Name
        # field live value -- same reasoning as ra_match_name/match_name.
        em_biosfile = (params.get("em_biosfile") or [""])[0]
        em_keysfile = (params.get("em_keysfile") or [""])[0]
        em_firmwarefile = (params.get("em_firmwarefile") or [""])[0]
        match_name = (params.get("em_match_name") or [""])[0] or _ra_guess_name_from_filename(em_romfile) or em_emulator

        romfile_abs = _ra_safe_join(em_romfile)
        if romfile_abs is None or not os.path.isfile(romfile_abs):
            self._send_html(render_done(match_name, ok=False, error="ROM file not found -- please pick it again"))
            return
        if em_biosfile and (_ra_safe_join(em_biosfile) is None or not os.path.isfile(_ra_safe_join(em_biosfile))):
            self._send_html(render_done(match_name, ok=False, error="BIOS file not found -- please pick it again"))
            return
        # bios_slots (xemu so far): the same file-not-found check as
        # em_biosfile above, just per-slot -- see
        # standalone_emulators.XEMU_BIOS_SLOTS.
        em_bios_slots = standalone_emulators.EMULATORS.get(em_emulator, {}).get("bios_slots") or []
        em_bios_slot_files = {}
        for prefix, label, *_rest in em_bios_slots:
            picked = (params.get(f"em_{prefix}file") or [""])[0]
            if not picked:
                continue
            picked_abs = _ra_safe_join(picked)
            if picked_abs is None or not os.path.isfile(picked_abs):
                self._send_html(render_done(match_name, ok=False, error=f"{label.replace('Select ', '')} file not found -- please pick it again"))
                return
            em_bios_slot_files[prefix] = picked_abs
        # Keys can be a file *or* a folder (see _em_picker_section's
        # "Select current folder" and standalone_emulators.install_keys,
        # which accepts either) -- os.path.isdir alongside os.path.isfile
        # here, unlike every other picked path in this file.
        em_keysfile_abs = _ra_safe_join(em_keysfile) if em_keysfile else None
        if em_keysfile and (em_keysfile_abs is None or not (os.path.isfile(em_keysfile_abs) or os.path.isdir(em_keysfile_abs))):
            self._send_html(render_done(match_name, ok=False, error="Keys file/folder not found -- please pick it again"))
            return
        em_firmwarefile_abs = _ra_safe_join(em_firmwarefile) if em_firmwarefile else None
        if em_firmwarefile and (em_firmwarefile_abs is None or not os.path.isfile(em_firmwarefile_abs)):
            self._send_html(render_done(match_name, ok=False, error="Firmware zip not found -- please pick it again"))
            return

        try:
            # Installing the emulator itself is a one-time cost (same
            # deliberate v1 tradeoff as RetroArch's own install above:
            # blocking here keeps this simple, at the cost of the first
            # click for any given emulator being slow) -- already-
            # installed check short-circuits every time after that.
            was_already_installed = standalone_emulators.installed(em_emulator)
            if not was_already_installed:
                standalone_emulators.install(em_emulator)
                # Only right after SelfSteam itself did a fresh install --
                # never for an emulator the user already had, whose own
                # game-directory settings (if any) stay untouched. See
                # configure_game_dir's own docstring for why this is
                # additive-only and safe to call unconditionally here.
                standalone_emulators.configure_game_dir(
                    em_emulator, os.path.join(_RA_UPLOAD_DIR, "em-rom"),
                )
            # Unlike install()/configure_game_dir above, this runs every
            # time regardless of was_already_installed -- it needs to
            # reach an emulator (gopher64) that was already installed,
            # by SelfSteam or otherwise, before this permission gap was
            # noticed, not just future fresh installs. No-op for every
            # other emulator (empty/absent grant_permissions list). See
            # grant_permissions' own docstring for the real bug this
            # fixes.
            standalone_emulators.grant_permissions(em_emulator)
            # Keys/firmware installs are real, verified (not guessed)
            # ports of Ryubing's own ContentManager.InstallKeys/
            # InstallFirmware -- see standalone_emulators.py's own
            # docstrings on each. Re-run every time a matching shortcut
            # is created, same as the emulator's own install() call
            # above -- cheap (a file copy / zip extract) once already
            # done, and picking up an updated keys/firmware file later
            # just needs creating the shortcut again.
            if em_keysfile_abs:
                standalone_emulators.install_keys(em_emulator, em_keysfile_abs)
            if em_firmwarefile_abs:
                standalone_emulators.install_firmware_zip(em_emulator, em_firmwarefile_abs)
            for prefix, bios_abs in em_bios_slot_files.items():
                standalone_emulators.install_bios_slot(em_emulator, prefix, bios_abs)

            args = standalone_emulators.launch_args(em_emulator, romfile_abs)
            if args is None:
                raise RuntimeError("flatpak isn't available on this host, or this emulator isn't installable yet")

            slug = create_webapp.slugify(match_name)
            selections = {}
            # artwork_em_{basename} -- see _add_retroarch_shortcut's own
            # comment on the same rename, this being the Emulators tab's.
            for basename, _title, _fetch, _w, _h in ARTWORK_CATEGORIES:
                selection_url = (params.get(f"artwork_em_{basename}") or [None])[0]
                selections[basename] = {"url": selection_url} if selection_url else None
            asset_paths = create_webapp.download_selected_assets(slug, selections)
            _queue_edit_rename_cleanup(params, "em", match_name)
            pending_queue.add(match_name, None, False, asset_paths, launch_args=args)
            # Emulator (and its Flathub/AppImage source), plus the ROM
            # picker's own folder/source, carried forward -- same
            # reasoning as _add_retroarch_shortcut's own carry-forward,
            # everything else genuinely is specific to the one game just
            # queued.
            em_install_source = (params.get("em_install_source") or [""])[0]
            em_rompath = (params.get("em_rompath") or [""])[0]
            em_romsource = (params.get("em_romsource") or [""])[0]
            self._redirect(_em_url("/new", {
                "em_emulator": em_emulator, "em_install_source": em_install_source,
                "em_rompath": em_rompath, "em_romsource": em_romsource,
            }))
        except Exception as e:  # noqa: BLE001 -- surfaced to the user, not swallowed
            self._send_html(render_done(match_name, ok=False, error=e))

    def _handle_ra_upload(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        ra_state = _ra_state_from_params(params)
        slot = (params.get("slot") or [""])[0]
        if slot not in ("rom", "bios"):
            self._send_html(render("<p>Invalid upload slot</p>"), status=400)
            return

        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        dest_dir = os.path.join(_RA_UPLOAD_DIR, "roms" if slot == "rom" else "bios")
        os.makedirs(dest_dir, exist_ok=True)

        # The real filename (and whether it collides with something
        # already there) isn't known until the multipart headers are
        # parsed, which happens as part of streaming the body itself --
        # so this writes to a private temp name first, then moves it
        # into place once save_uploaded_file returns.
        fd, tmp_path = tempfile.mkstemp(dir=dest_dir, prefix=".upload-")
        os.close(fd)
        try:
            filename = multipart_upload.save_uploaded_file(self.rfile, content_type, length, tmp_path)
        except (ValueError, OSError):
            # OSError alongside ValueError -- a stalled connection (see
            # Handler.timeout's own comment) raises socket.timeout here,
            # a subclass of OSError, not ValueError; same "please try
            # again" outcome either way, rather than an unhandled
            # exception with the upload just silently never finishing.
            os.remove(tmp_path)
            self._send_html(render_done("Upload", ok=False, error="Upload failed -- please try again"))
            return

        # basename() strips any directory components a browser might
        # send (mainly a legacy-IE thing, but cheap to guard regardless)
        # -- this is a filename choice, not a path, so that alone is
        # enough sanitization.
        safe_name = os.path.basename(filename) if filename else os.path.basename(tmp_path)
        dest_path = os.path.join(dest_dir, safe_name)
        # Re-uploading a file with a name that's already there just
        # overwrites it -- a retry or a newer dump replacing an old one
        # is the more expected outcome than silently failing or picking
        # a different name out from under the user.
        os.replace(tmp_path, dest_path)

        rel_path = os.path.relpath(dest_path, _RA_ROOT)
        file_key = "ra_romfile" if slot == "rom" else "ra_biosfile"
        source_key = f"ra_{slot}source"
        # Explicit, not just carried over from ra_state -- a toggle click
        # that happened after this page's own load (no reload in between)
        # means the upload form's action URL was still built with the
        # pre-toggle source baked in, even though the upload that just
        # succeeded is unambiguously an "upload" for this slot.
        overrides = {file_key: rel_path, source_key: "upload"}
        if slot == "rom":
            # Same reset _ra_list_rows already does for a fresh local
            # pick -- an uploaded ROM is just as fresh a pick as a
            # locally-browsed one, and skipping this here left an
            # uploaded replacement ROM silently inheriting the previous
            # ROM's cleared name/search fields instead of getting its
            # own guessed cross-population back.
            overrides["ra_resolved"] = ""
            overrides["ra_sgdb_q"] = ""
            overrides["ra_sgdb_cleared"] = ""
            overrides["ra_name_cleared"] = ""
        self._redirect(_ra_url("/new", ra_state, **overrides))

    def _handle_em_upload(self):
        # Same streaming-upload approach as _handle_ra_upload -- see its
        # own comments for why (never buffering a multi-GB file in
        # memory), just with extra em_-only slots (keys, firmware) and
        # its own em_-prefixed state/dest dirs so an upload never
        # collides with the RetroArch tab's own roms/bios uploads. A
        # folder-of-keys pick (see _em_picker_section's "Select current
        # folder") only ever comes from the local browser, never this
        # upload form -- a single <input type=file> can't upload a
        # whole directory, so "keys" here always means one file.
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        em_state = _em_state_from_params(params)
        slot = (params.get("slot") or [""])[0]
        if slot not in ("rom", "bios", "bios2", "bios3", "keys", "firmware"):
            self._send_html(render("<p>Invalid upload slot</p>"), status=400)
            return

        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        dest_dir = os.path.join(_RA_UPLOAD_DIR, f"em-{slot}")
        os.makedirs(dest_dir, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=dest_dir, prefix=".upload-")
        os.close(fd)
        try:
            filename = multipart_upload.save_uploaded_file(self.rfile, content_type, length, tmp_path)
        except (ValueError, OSError):
            # OSError alongside ValueError -- a stalled connection (see
            # Handler.timeout's own comment) raises socket.timeout here,
            # a subclass of OSError, not ValueError; same "please try
            # again" outcome either way, rather than an unhandled
            # exception with the upload just silently never finishing.
            os.remove(tmp_path)
            self._send_html(render_done("Upload", ok=False, error="Upload failed -- please try again"))
            return

        safe_name = os.path.basename(filename) if filename else os.path.basename(tmp_path)
        dest_path = os.path.join(dest_dir, safe_name)
        os.replace(tmp_path, dest_path)

        rel_path = os.path.relpath(dest_path, _RA_ROOT)
        file_key = f"em_{slot}file"
        source_key = f"em_{slot}source"
        overrides = {file_key: rel_path, source_key: "upload"}
        if slot == "rom":
            # Same reset _em_list_rows already does for a fresh local
            # pick -- see _handle_ra_upload's own comment for why this
            # was missing here too.
            overrides["em_resolved"] = ""
            overrides["em_sgdb_q"] = ""
            overrides["em_sgdb_cleared"] = ""
            overrides["em_name_cleared"] = ""
        self._redirect(_em_url("/new", em_state, **overrides))

    def _commit_pending(self):
        items = pending_queue.all_items()
        if not items:
            self._redirect("/")
            return
        added = sum(1 for i in items if i.get("type", "add") == "add")
        removed = sum(1 for i in items if i.get("type") == "remove")
        parts = []
        if added:
            parts.append(f"{added} added")
        if removed:
            parts.append(f"{removed} removed")
        label = " and ".join(parts) if parts else f"{len(items)} shortcut{'s' if len(items) != 1 else ''}"
        with _commit_status_lock:
            _commit_status.update(running=True, done=False, ok=None, label=label, error=None)
        threading.Thread(target=_run_commit_in_background, args=(items, label), daemon=True).start()
        self._redirect("/restarting")

    def log_message(self, fmt, *args):
        print(f"[selfsteam-server] {self.address_string()} - {fmt % args}")


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"SelfSteam listening on http://0.0.0.0:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
