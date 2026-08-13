#!/usr/bin/env python3
"""Gridge Server: headless web UI for adding Steam shortcuts from another
device while the target machine is in Game Mode. No JavaScript by
design -- plain HTML forms, server-rendered, one request per step.

Reuses the exact same backend create_webapp.py/sgdb_client.py/
shortcuts_vdf.py already use for the GTK app; the only new pieces here
are the maintenance-window sequencing (see maintenance.py, needed
because a shortcuts.vdf write while Steam is running gets silently
clobbered) and screen-pairing auth (see auth.py).

Layout and styling deliberately mirror the GTK desktop app's own
MainWindow (gui.py): a fixed-width search/matches column on the left,
an artwork picker on the right, same accent blue (#3584e4) and
boxed-list look. ARTWORK_CATEGORIES below is a plain-data mirror of
gui.py's own list -- gui.py itself isn't importable here since it pulls
in GTK4/Adwaita at module level, which this headless server must not
depend on.
"""
import html
import http.cookies
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import auth_display
import create_webapp
import maintenance
import service_resolver
import sgdb_client as sgdb

PORT = int(os.environ.get("GRIDGE_SERVER_PORT", "8845"))
SESSION_COOKIE = "gridge_session"
_DARKREADER_PATH = os.path.join(os.path.dirname(__file__), "vendor", "darkreader.js")

# (basename, display title, candidate-fetcher, cell width, cell height)
# -- basenames and the *relative* cell proportions match gui.py's own
# ARTWORK_CATEGORIES exactly (170x255, 260x121, 320x104, 160x100,
# 100x100), scaled up by _ARTWORK_SCALE. Using a single shared width
# for every category (an earlier version of this) made Icon (a small
# square asset) render as big as Hero, and Hero (a wide short banner)
# render tiny -- each category needs its own real proportions, not a
# derived ratio against one shared width.
_ARTWORK_SCALE = 1.8
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
<style>
:root {
  --accent: #3584e4;
  --success: #26a269;
  --bg: #fafafb;
  --card-bg: #ffffff;
  --border: rgba(0,0,0,0.09);
  --text: #1c1c1c;
  --text-dim: #6e6e6e;
}
* { box-sizing: border-box; }
html { font-size: 18px; }
body {
  font-family: Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); margin: 0;
}
header.gridge-header {
  background: var(--card-bg); border-bottom: 1px solid var(--border);
  padding: 0.4rem 1.5rem; font-size: 1.1rem; font-weight: 600;
  display: flex; align-items: center; justify-content: space-between;
}
#gridge-dark-toggle {
  margin: 0; padding: 0.25rem 0.7rem; font-size: 1.1rem; line-height: 1;
  background: var(--bg); color: var(--text); border: 1px solid var(--border);
}
main { width: 100%; padding: 1.5rem 2rem; }
.gridge-columns { display: flex; gap: 1.5rem; align-items: flex-start; flex-wrap: wrap; }
.gridge-left { flex: 0 0 380px; max-width: 100%; }
.gridge-right { flex: 1 1 600px; min-width: 0; }
@media (max-width: 720px) {
  .gridge-left { flex-basis: 100%; }
}
.card {
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: 12px; padding: 1.25rem; margin-bottom: 1.25rem;
}
.card h2 {
  font-size: 1rem; text-transform: uppercase; letter-spacing: 0.03em;
  color: var(--text-dim); margin: 0 0 0.75rem 0;
}
input[type=text] {
  width: 100%; padding: 0.7rem 0.85rem; font-size: 1.15rem;
  border: 1px solid var(--border); border-radius: 8px; background: var(--bg);
}
input[type=text]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
button, .btn {
  padding: 0.75rem 1.4rem; font-size: 1.1rem; margin-top: 0.9rem;
  border: none; border-radius: 8px; background: var(--accent); color: white;
  cursor: pointer; font-weight: 500;
}
button.secondary { background: var(--bg); color: var(--text); border: 1px solid var(--border); }
.boxed-list { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.boxed-list a {
  display: block; padding: 0.8rem 0.9rem; border-bottom: 1px solid var(--border);
  cursor: pointer; font-size: 1.1rem; color: var(--text); text-decoration: none;
}
.boxed-list a:last-child { border-bottom: none; }
.boxed-list a.selected { background: rgba(53,132,228,0.08); font-weight: 500; }
.match-tag { color: var(--text-dim); font-size: 0.9rem; }
.artwork-category { margin-bottom: 1.5rem; }
.artwork-category h3 { font-size: 1.05rem; margin: 0 0 0.6rem 0; color: var(--text-dim); }
.artwork-row { display: flex; gap: 0.75rem; overflow-x: auto; padding-bottom: 0.5rem; }
.artwork-cell { flex: 0 0 auto; }
.artwork-cell input[type=radio] { display: none; }
.artwork-cell label {
  display: flex; align-items: center; justify-content: center;
  border: 3px solid transparent; border-radius: 6px;
  cursor: pointer; overflow: hidden; background: var(--bg);
}
.artwork-cell input[type=radio]:checked + label { border-color: var(--accent); }
/* CONTAIN, not cover: cover crops to fill the box, which mangled
   irregularly-shaped artwork (logos especially -- a wide transparent
   logo showed up as a cropped square). gui.py's own picker uses
   Gtk.ContentFit.CONTAIN for exactly this reason; match it here. */
.artwork-cell img { display: block; max-width: 100%; max-height: 100%; object-fit: contain; }
.artwork-empty { color: var(--text-dim); font-size: 0.95rem; font-style: italic; }
.switch-row { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.6rem; font-size: 1rem; }
</style></head><body>
<header class="gridge-header">
  <span>Gridge Server</span>
  <button id="gridge-dark-toggle" type="button" title="Toggle dark mode">&#9789;</button>
</header>
<main>
"""
# Dark Reader (vendor/darkreader.js, MIT, see vendor/DARKREADER-LICENSE.txt)
# is the one deliberate exception to this app's no-JavaScript design --
# explicit user request, scoped to just the toggle. Preference persists
# via localStorage so it survives across page loads (every click here is
# a real navigation, not an SPA).
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


def render(body):
    return (PAGE_HEAD + body + PAGE_TAIL).encode()


def _search_form_html(query="", couch_mode=False):
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
            hint = f'<p style="color:#e5a50a;font-size:0.9rem;margin:0.5rem 0 0">{html.escape(resolved.warning)}</p>'
        elif resolved.url:
            shown = resolved.url.removeprefix("https://").removeprefix("http://")
            hint = f'<p style="color:var(--text-dim);font-size:0.9rem;margin:0.5rem 0 0">Shortcut for {html.escape(shown)} will be added</p>'

    return f"""
<div class="card">
  <h2>Search</h2>
  <form action="/search" method="get">
    <input type="text" name="q" value="{html.escape(query)}" placeholder="Service name or URL" required autofocus>{couch_row}
    {hint}
    <button type="submit">Search</button>
  </form>
</div>
"""


def render_home():
    left = _search_form_html()
    right = '<div class="card"><span class="artwork-empty">Search for a service to see artwork options here.</span></div>'
    return render(f"""
<div class="gridge-columns">
  <div class="gridge-left">{left}</div>
  <div class="gridge-right">{right}</div>
</div>
""")


def render_unresolved(query):
    # _search_form_html already shows the "isn't a recognized service
    # name or a URL" warning inline -- this is just the right-column
    # placeholder for that state.
    left = _search_form_html(query)
    right = '<div class="card"><span class="artwork-empty">Fix the search above to continue.</span></div>'
    return render(f"""
<div class="gridge-columns">
  <div class="gridge-left">{left}</div>
  <div class="gridge-right">{right}</div>
</div>
""")


def render_no_matches(query):
    left = _search_form_html(query)
    right = f'<div class="card"><span class="artwork-empty">No SteamGridDB results for "{html.escape(query)}".</span></div>'
    return render(f"""
<div class="gridge-columns">
  <div class="gridge-left">{left}</div>
  <div class="gridge-right">{right}</div>
</div>
""")


def _match_list_html(query, couch_mode, matches, match_index):
    # Plain links, not radio+submit-button: clicking one navigates
    # straight to that match's artwork (a real GET, no JS needed) --
    # a radio selection alone doesn't submit anything by itself, which
    # read as "artwork doesn't change when I pick a different match".
    rows = []
    qs_base = f"q={urllib.parse.quote(query)}"
    if couch_mode:
        qs_base += "&couch_mode=1"
    for i, m in enumerate(matches):
        tag = " (verified)" if m.get("verified") else ""
        selected = " selected" if i == match_index else ""
        rows.append(
            f'<a class="{selected.strip()}" href="/search?{qs_base}&match_index={i}">'
            f"{html.escape(m['name'])}<span class=\"match-tag\">{tag}</span></a>"
        )
    return f"""
<div class="card">
  <h2>SGDB matches</h2>
  <div class="boxed-list">{''.join(rows)}</div>
</div>
"""


def _artwork_picker_html(candidates_by_category):
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
        cell_h = round(base_h * _ARTWORK_SCALE)
        cells = []
        for i, cand in enumerate(candidates):
            checked = "checked" if i == 0 else ""
            input_id = f"art-{basename}-{i}"
            thumb = html.escape(cand.get("thumb") or cand["url"])
            url = html.escape(cand["url"])
            cells.append(f"""
<div class="artwork-cell">
  <input type="radio" id="{input_id}" name="artwork_{basename}" value="{url}" {checked}>
  <label for="{input_id}" style="width:{cell_w}px;height:{cell_h}px">
    <img src="{thumb}" width="{cell_w}" height="{cell_h}" loading="lazy" alt="">
  </label>
</div>""")
        sections.append(f"""
<div class="artwork-category">
  <h3>{html.escape(title)}</h3>
  <div class="artwork-row">{''.join(cells)}</div>
</div>""")
    return "".join(sections)


def render_workspace(query, couch_mode, matches, match_index, candidates_by_category, resolved_url):
    chosen = matches[match_index]
    couch_field = '<input type="hidden" name="couch_mode" value="1">' if couch_mode else ""
    artwork_html = _artwork_picker_html(candidates_by_category)
    # One form spans both columns -- the Add button lives on the left
    # (with search/matches) while the artwork radios stay on the
    # right; a single <form> can have descendants scattered anywhere
    # in the DOM and still submit them all together.
    left = f"""
{_search_form_html(query, couch_mode)}
{_match_list_html(query, couch_mode, matches, match_index)}
<div class="card">
  <button type="submit">Add "{html.escape(chosen['name'])}" to Steam</button>
</div>
"""
    right = f'<div class="card">{artwork_html}</div>'
    return render(f"""
<form action="/add" method="post">
  <input type="hidden" name="query" value="{html.escape(query)}">
  <input type="hidden" name="match_index" value="{match_index}">
  <input type="hidden" name="match_name" value="{html.escape(chosen['name'])}">
  <input type="hidden" name="resolved_url" value="{html.escape(resolved_url or '')}">
  {couch_field}
  <div class="gridge-columns">
    <div class="gridge-left">{left}</div>
    <div class="gridge-right">{right}</div>
  </div>
</form>
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
    <button type="submit">Continue</button>
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
            self._send_html(render_home())
            return

        if parsed.path == "/search":
            query = (params.get("q") or [""])[0].strip()
            couch_mode = bool(params.get("couch_mode"))
            match_index = int((params.get("match_index") or ["0"])[0])
            if not query:
                self._send_html(render_home())
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
                self._send_html(render_unresolved(query))
                return

            if resolved.sgdb_id is not None:
                matches = [sgdb.get_game(resolved.sgdb_id)]
            else:
                matches = sgdb.search(resolved.name or create_webapp.clean_shortcut_name(query))
            if not matches:
                self._send_html(render_no_matches(query))
                return
            match_index = min(match_index, len(matches) - 1)
            candidates = _fetch_candidates(matches[match_index]["id"])
            self._send_html(render_workspace(query, couch_mode, matches, match_index, candidates, resolved.url))
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
