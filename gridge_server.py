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
import json
import os
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import auth_display
import browser_picker
import config
import create_webapp
import maintenance
import pending_queue
import service_resolver
import sgdb_client as sgdb
import steam_paths

PORT = int(os.environ.get("GRIDGE_SERVER_PORT", "8845"))
SESSION_COOKIE = "gridge_session"
_DARKREADER_PATH = os.path.join(os.path.dirname(__file__), "vendor", "darkreader.js")
_POSTER_FRAME_PATH = os.path.join(os.path.dirname(__file__), "vendor", "poster-frame.webp")
_ADD_FORM_ID = "gridge-add-form"
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
# Poster overlay icons (gallery/home page): edit name, edit artwork,
# remove -- exact paths from the design handoff's own HTML source
# (steam-webapp-creator/"Copy of Three-column UI draft"), not
# reconstructed from scratch. stroke="currentColor" so it follows
# .poster-icon-btn's own color instead of a hardcoded white.
_EDIT_NAME_ICON_SVG = (
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<path d="M12 20h9"></path><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"></path></svg>'
)
_EDIT_ARTWORK_ICON_SVG = (
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<rect x="3" y="3" width="18" height="18" rx="2"></rect>'
    '<circle cx="8.5" cy="8.5" r="1.5"></circle><path d="M21 15l-5-5L5 21"></path></svg>'
)
_TRASH_ICON_SVG = (
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<polyline points="3 6 5 6 21 6"></polyline>'
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>'
    '<path d="M10 11v6"></path><path d="M14 11v6"></path>'
    '<path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>'
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
header.gridge-header {
  background: var(--card-bg); border-bottom: 1px solid var(--border);
  padding: 1.1rem 2rem; display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 1rem; flex: 0 0 auto;
}
.gridge-header-left { display: flex; align-items: center; gap: 1rem; }
.gridge-header-title strong { font-size: 1.2rem; font-weight: 700; letter-spacing: -0.01em; }
.gridge-header-actions { display: flex; gap: 0.6rem; align-items: center; }
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
  display: block; font-size: 1.05rem; font-weight: 700; color: var(--text);
  letter-spacing: 0.01em; margin: 0;
}
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
/* Tightened chrome (padding/gaps/label size) vs the default .card,
   specifically to reclaim vertical space for the actual artwork --
   every pixel trimmed here is a pixel _ARTWORK_VH_OVERHEAD_PX doesn't
   have to reserve, i.e. directly bigger tiles. */
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
.artwork-row { display: flex; gap: 0.7rem; overflow-x: auto; padding-bottom: 0.05rem; }
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
/* Segmented tab bar (URL / Apps / RetroArch / Emulators): a pure-CSS
   radio hack, same technique as the sgdb-search reveal used to be --
   no page reload needed to switch tabs, since nothing server-side
   depends on which one is showing. */
.tab-radio { position: absolute; opacity: 0; pointer-events: none; }
.tab-bar { display: flex; gap: 4px; background: var(--bg); border-radius: 12px; padding: 4px; }
.tab-label {
  flex: 1; padding: 0.6rem 0.25rem; border-radius: 9px; font-size: 1rem; font-weight: 600;
  text-align: center; cursor: pointer; color: var(--text-dim);
}
#tab-url:checked ~ .tab-bar label[for="tab-url"],
#tab-apps:checked ~ .tab-bar label[for="tab-apps"],
#tab-retroarch:checked ~ .tab-bar label[for="tab-retroarch"],
#tab-emulators:checked ~ .tab-bar label[for="tab-emulators"] {
  background: #fff; color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
.tab-panels { display: flex; flex-direction: column; }
.tab-panel { display: none; flex-direction: column; gap: 0.9rem; }
#tab-url:checked ~ .tab-panels .tab-panel-url,
#tab-apps:checked ~ .tab-panels .tab-panel-apps,
#tab-retroarch:checked ~ .tab-panels .tab-panel-retroarch,
#tab-emulators:checked ~ .tab-panels .tab-panel-emulators { display: flex; }
.coming-soon { color: var(--text-dim); font-size: 0.85rem; padding: 1rem 0; text-align: center; }
/* Shortcut gallery (the real home page: "/"). main's own flex column
   already scrolls the whole page here -- unlike the 3-column workspace,
   there's no reason to bound this to the viewport height, since a
   library of hundreds of shortcuts is expected to need real scrolling. */
.gallery-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; }
.gallery-header h2 { font-size: 1.3rem; margin: 0; }
.gallery-grid { display: flex; flex-wrap: wrap; gap: 24px; }
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
/* Sized/offset to match the *artwork* region of the other posters
   exactly (166x249, 11px inset), not their full outer box -- the add
   card has no blue frame of its own, so aligning by outer edges would
   leave its "+" sitting visibly higher than every other poster's real
   content. */
.add-poster {
  width: 166px; height: 249px; margin-top: 11px; border-radius: 8px; background: var(--skeleton);
  flex: 0 0 auto; display: flex; align-items: center; justify-content: center;
  color: var(--text-dim); text-decoration: none;
}
.add-poster-plus { font-size: 3.5rem; line-height: 1; font-weight: 300; }
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
  <div class="gridge-header-left">
    <!--BACK_BTN-->
    <div class="gridge-header-title">
      <strong><!--PAGE_TITLE--></strong>
    </div>
    <!--QUEUE_ACTIONS-->
  </div>
  <div class="gridge-header-actions">
    <button class="icon-btn-round" type="button" title="Favorite" style="color:#e0568c"><!--HEART_ICON--></button>
    <!--SGDB_KEY_BADGE-->
    <button id="gridge-dark-toggle" class="icon-btn-round" type="button" title="Toggle dark mode"><!--DARK_ICON--></button>
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
  var KEY = "gridge-dark-mode";
  var MOON_SVG = "<!--MOON_SVG_JS-->";
  var SUN_SVG = "<!--SUN_SVG_JS-->";
  var btn = document.getElementById("gridge-dark-toggle");
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
</body></html>"""


def _sgdb_key_badge_html():
    # Always a link to /key, verified or not -- letting it update
    # an already-configured key (not just add a missing one) is what a
    # user actually expects from a clickable status badge.
    if sgdb.has_api_key():
        return '<a href="/key" class="sgdb-key-badge">&#10003; SGDB API key verified</a>'
    return '<a href="/key" class="sgdb-key-badge unverified">&#9888; No SGDB API key</a>'


def _hostname():
    # Login and the shortcut gallery both show this instead of a fixed
    # title -- it's the fastest way to confirm from the login screen
    # alone that you've reached the right machine, useful the moment
    # there's more than one Gridge Server on the same network.
    return socket.gethostname()


def _steam_warning_html():
    # Sanity check, not a hard requirement -- on real SteamOS this can
    # never fire (Steam owns the machine), but Gridge Server itself is
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
    # needed to keep an empty commit from doing anything.
    n = pending_queue.count()
    disabled = "" if n else " disabled"
    counter_class = "queue-counter" if n else "queue-counter empty"
    return f"""
<div class="queue-actions">
  <form action="/commit" method="post" style="margin:0">
    <button type="submit" class="restart-btn"{disabled}>Save changes and restart Steam</button>
  </form>
  <a href="/pending" class="{counter_class}" title="View queued changes">{n}</a>
</div>"""


def render(body, page_title="Add Steam Shortcut", show_back=True):
    back_btn_html = (
        f'<a class="icon-btn-round back-btn" href="/" title="Back to shortcuts">{_BACK_ICON_SVG}</a>'
        if show_back else ""
    )
    head = PAGE_HEAD.replace("<!--SGDB_KEY_BADGE-->", _sgdb_key_badge_html())
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
    <label class="field-label" for="gridge-browser-select">Browser <span style="color:var(--text-dim);font-weight:400;font-size:0.85rem">Flatpak</span></label>
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
    <div class="search-field-row">
      <div class="field-with-clear">
        <input type="text" name="q" value="{html.escape(query)}" placeholder="e.g. Netflix" required autofocus>
        <a href="/" class="field-clear-btn" title="Clear">&#10005;</a>
      </div>
      <button type="submit" class="search-submit-btn" title="Search">{_SEARCH_ICON_SVG}</button>
    </div>
  </div>{couch_row}
  {hint}
  {_browser_select_html(browser)}"""


_FORM_TABS = [("tab-url", "URL"), ("tab-apps", "Apps"), ("tab-retroarch", "RetroArch"), ("tab-emulators", "Emulators")]


def _tab_bar_html():
    # Segmented control switching the four shortcut-source tabs. CSS-only
    # (radio hack): the radios sit flat alongside .tab-bar and .tab-panels
    # (not nested inside either) so the general-sibling selectors in the
    # stylesheet (#tab-url:checked ~ .tab-bar label[for="tab-url"], etc.)
    # can reach both the matching label and the matching panel from a
    # single :checked radio. Only the URL tab is functional for now;
    # Apps/RetroArch/Emulators are placeholders per the design handoff.
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
    # touched -- same behavior planned for the Apps/RetroArch/Emulators
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
  <div class="search-field-row">
    <div class="field-with-clear">
      <input type="text" name="sgdb_q" value="{html.escape(display_term)}" placeholder="SGDB search">
      <a href="{clear_href}" class="field-clear-btn" title="Clear">&#10005;</a>
    </div>
    <button type="submit" class="search-submit-btn" title="Search">{_SEARCH_ICON_SVG}</button>
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
  <div class="field-group" style="flex:1;min-height:0">
    <h2>SGDB matches</h2>
    {list_html}
  </div>
</div>
"""


# Sum of gui.py's own category heights (255+121+104+100+100) -- used
# below to give each category a share of the column's real height
# proportional to its own natural size, instead of a fixed px scale
# that leaves the column visibly short of its actual available space
# on any screen taller than whatever it was tuned against.
_ARTWORK_HEIGHT_WEIGHT_SUM = sum(base_h for _b, _t, _f, _w, base_h in ARTWORK_CATEGORIES)
# Rough fixed overhead per category row (its <h3> label, row gaps, the
# card's own padding/gaps) subtracted from 100vh before splitting the
# remainder by weight -- not exact (header height/main padding vary a
# little), just close enough that all 5 categories land within the
# column's real height rather than needing to scroll for one of them.
_ARTWORK_VH_OVERHEAD_PX = 393


# Skeleton tile counts per category before any search -- matches the
# design handoff's own placeholder counts exactly, so the blank state
# looks like real content is about to load rather than an empty column.
_SKELETON_TILE_COUNTS = {
    "grid_vertical": 4,
    "grid_horizontal": 2,
    "hero": 2,
    "logo": 4,
    "icon": 6,
}


def _artwork_picker_html(candidates_by_category):
    # Always renders all 5 categories, even with zero candidates
    # (candidates_by_category can be {}) -- shown before any search
    # too, so the right column never collapses to a placeholder message
    # and always occupies its full share of the row.
    sections = []
    for basename, title, _fetch, base_w, base_h in ARTWORK_CATEGORIES:
        candidates = candidates_by_category.get(basename) or []
        # Height-driven sizing (not width): each category's row height is
        # a share of the viewport proportional to its own natural size,
        # and aspect-ratio derives the width from that -- so the artwork
        # column actually fills the screen instead of sitting at a fixed
        # size tuned for one particular window height. Shared between
        # real cells and empty-state skeleton tiles so the layout doesn't
        # jump once a search actually returns candidates.
        weight = base_h / _ARTWORK_HEIGHT_WEIGHT_SUM
        cell_style = (
            f"height:calc((100vh - {_ARTWORK_VH_OVERHEAD_PX}px) * {weight:.4f}); "
            f"min-height:60px; aspect-ratio: {base_w} / {base_h};"
        )
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
        none_id = f"art-{basename}-none"
        none_checked = "" if candidates else " checked"
        none_cell = f"""
<div class="artwork-cell">
  <input type="radio" id="{none_id}" name="artwork_{basename}" value="" form="{_ADD_FORM_ID}"{none_checked}>
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
<div class="artwork-category">
  <h3>{html.escape(title)}</h3>
  <div class="artwork-row">{none_cell}{skeletons}</div>
</div>""")
            continue
        cells = [none_cell]
        for i, cand in enumerate(candidates):
            checked = " checked" if i == 0 else ""
            input_id = f"art-{basename}-{i}"
            thumb = html.escape(cand.get("thumb") or cand["url"])
            url = html.escape(cand["url"])
            cells.append(f"""
<div class="artwork-cell">
  <input type="radio" id="{input_id}" name="artwork_{basename}" value="{url}" form="{_ADD_FORM_ID}"{checked}>
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
    <div class="tab-panel tab-panel-retroarch"><div class="coming-soon">RetroArch platforms -- coming soon</div></div>
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
  <form id="gridge-login-form" action="/login" method="post">
    <input type="text" name="code" id="gridge-login-code" required autofocus maxlength="6"
           autocomplete="off"
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
        detail = "" if is_removal else f'<div style="color:var(--text-dim);font-size:0.85rem">{html.escape(item["url"])}</div>'
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
      <label class="field-label" for="gridge-sgdb-key">SteamGridDB API key</label>
      <div class="field-with-clear">
        <input type="text" name="sgdb_api_key" id="gridge-sgdb-key" value="{html.escape(current_key)}"
               placeholder="Paste your key here" autocomplete="off">
        <button type="submit" formaction="/key/remove" formnovalidate class="field-clear-btn" title="Remove key">&#10005;</button>
      </div>
      <a href="https://www.steamgriddb.com/profile/preferences/api" target="_blank" rel="noopener" style="font-size:0.85rem">Get a free key at steamgriddb.com</a>
    </div>
    <button type="submit">Save</button>
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
    if sgdb_q:
        matches = sgdb.search(sgdb_q)
    elif resolved.sgdb_id is not None:
        matches = [sgdb.get_game(resolved.sgdb_id)]
    else:
        matches = sgdb.search(name)
    return matches or [{"id": None, "name": name}]


def _poster_card_html(shortcut):
    appid = shortcut["appid"]
    name = shortcut["name"]
    has_artwork = appid is not None and create_webapp.find_grid_image_for_appid(appid) is not None
    if has_artwork:
        art_html = f'<img class="poster-art" src="/grid-image/{appid}" loading="lazy" alt="{html.escape(name)}">'
    else:
        # No grid image on disk -- rather than a broken <img> or a blank
        # panel, show the shortcut's own name so the poster still reads
        # as *that* shortcut instead of an empty tile.
        art_html = f'<div class="poster-art poster-art-noimg"><span>{html.escape(name)}</span></div>'
    # Reuses the exact same /search entry point real shortcut creation
    # goes through -- searching by the shortcut's own already-known URL
    # runs SGDB matching immediately (no re-typing), and picking new
    # artwork there and hitting Create Steam Shortcut replaces this
    # shortcut in place rather than duplicating it: add_shortcut's own
    # appid is deterministic from exe+name, and it already dedups any
    # existing entry with the same name before writing.
    edit_artwork_href = f"/search?q={urllib.parse.quote(shortcut['url'] or name)}"
    return f"""
<div class="shortcut-poster">
  <div class="poster-frame"></div>
  {art_html}
  <div class="poster-icons">
    <button type="button" class="poster-icon-btn" title="Edit name -- coming soon" disabled>{_EDIT_NAME_ICON_SVG}</button>
    <a href="{edit_artwork_href}" class="poster-icon-btn" title="Edit artwork">{_EDIT_ARTWORK_ICON_SVG}</a>
    <form action="/shortcuts/remove" method="post" style="margin:0">
      <input type="hidden" name="appid" value="{html.escape(str(appid))}">
      <input type="hidden" name="name" value="{html.escape(name)}">
      <button type="submit" class="poster-icon-btn" title="Remove shortcut">{_TRASH_ICON_SVG}</button>
    </form>
  </div>
</div>"""


def render_gallery():
    # No cap on how many render -- explicitly meant to hold however many
    # shortcuts exist (a user can have hundreds), not a paginated/lazy
    # subset. CSS grid + the browser's own image lazy-loading is what
    # keeps that reasonable, not limiting the query.
    shortcuts = create_webapp.list_gridge_shortcuts()
    cards_html = "".join(_poster_card_html(s) for s in shortcuts)
    return render(f"""
<div class="gallery-header">
  <h2>Non Steam shortcuts</h2>
</div>
<div class="gallery-grid">
  {cards_html}
  <a class="add-poster" href="/new" title="Add a shortcut">
    <span class="add-poster-plus">+</span>
  </a>
</div>
""", page_title=_hostname())


def _fetch_candidates(game_id):
    if game_id is None:
        return {}
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
        path = create_webapp.find_grid_image_for_appid(int(appid))
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
        self.end_headers()
        self.wfile.write(body)

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

        if parsed.path == "/":
            self._send_html(render_gallery())
            return

        if parsed.path == "/new":
            self._send_html(render_page())
            return

        if parsed.path.startswith("/grid-image/"):
            self._serve_grid_image(parsed.path[len("/grid-image/"):])
            return

        if parsed.path == "/pending":
            self._send_html(render_pending())
            return

        if parsed.path == "/key":
            self._send_html(render_settings())
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
            matches = _resolve_matches(query, resolved, sgdb_q)
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

        if parsed.path == "/pending/remove":
            index = int((params.get("index") or ["-1"])[0])
            pending_queue.remove(index)
            self._redirect("/pending")
            return

        if parsed.path == "/shortcuts/remove":
            appid = (params.get("appid") or [""])[0]
            name = (params.get("name") or [""])[0]
            if appid:
                pending_queue.add_removal(appid, name)
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
        matches = _resolve_matches(query, resolved)
        if not matches or match_index >= len(matches):
            self._send_html(render_done(match_name or query, ok=False, error="match no longer available, please search again"))
            return
        chosen = matches[match_index]

        if not url:
            self._send_html(render_done(chosen["name"], ok=False, error="couldn't resolve a URL for this shortcut, please search again"))
            return

        try:
            # Downloading/saving artwork doesn't touch Steam or
            # shortcuts.vdf at all, so it doesn't need a maintenance
            # window -- only actually queues the shortcut (name, url,
            # already-downloaded asset paths) for the next "Save Changes
            # and Restart Steam OS" commit, instead of stopping Steam
            # for every single shortcut added. Redirects straight back
            # to a blank home page ("clean slate") so the next shortcut
            # can be added immediately without an extra confirmation
            # step in the way.
            slug = create_webapp.slugify(chosen["name"])
            selections = {}
            for basename, _title, _fetch, _w, _h in ARTWORK_CATEGORIES:
                selection_url = (params.get(f"artwork_{basename}") or [None])[0]
                selections[basename] = {"url": selection_url} if selection_url else None
            asset_paths = create_webapp.download_selected_assets(slug, selections)
            pending_queue.add(chosen["name"], url, couch_mode, asset_paths, browser_app_id=browser or None)
            self._redirect("/")
        except Exception as e:  # noqa: BLE001 -- surfaced to the user, not swallowed
            self._send_html(render_done(chosen["name"], ok=False, error=e))

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
        try:
            def apply():
                for item in items:
                    if item.get("type") == "remove":
                        create_webapp.remove_gridge_shortcut(item["appid"])
                    else:
                        create_webapp.register_steam_shortcut(
                            item["name"], item["url"], item["asset_paths"],
                            couch_mode=item["couch_mode"], browser_app_id=item.get("browser_app_id"),
                        )

            maintenance.run_with_steam_stopped(apply, message=f"Applying {label}…")
            pending_queue.clear()
            self._send_html(render_done(label, ok=True))
        except Exception as e:  # noqa: BLE001 -- surfaced to the user, not swallowed
            self._send_html(render_done(label, ok=False, error=e))

    def log_message(self, fmt, *args):
        print(f"[gridge-server] {self.address_string()} - {fmt % args}")


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Gridge Server listening on http://0.0.0.0:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
