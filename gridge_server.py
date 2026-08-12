#!/usr/bin/env python3
"""Gridge Server: headless web UI for adding Steam shortcuts from another
device while the target machine is in Game Mode. No JavaScript by
design -- plain HTML forms, server-rendered, one request per step.

Reuses the exact same backend create_webapp.py/sgdb_client.py/
shortcuts_vdf.py already use for the GTK app; the only new piece here
is the maintenance-window sequencing (see maintenance.py) needed
because a shortcuts.vdf write while Steam is running gets silently
clobbered.
"""
import html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import create_webapp
import maintenance
import sgdb_client as sgdb

PORT = int(os.environ.get("GRIDGE_SERVER_PORT", "8845"))

PAGE_HEAD = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gridge Server</title>
<style>
body { font-family: sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }
input[type=text] { width: 100%; padding: 0.5rem; font-size: 1rem; box-sizing: border-box; }
button { padding: 0.6rem 1.2rem; font-size: 1rem; margin-top: 1rem; }
.match { border: 1px solid #ccc; border-radius: 6px; padding: 0.75rem; margin: 0.5rem 0; }
.match label { display: block; cursor: pointer; }
</style></head><body>
"""
PAGE_TAIL = "</body></html>"


def render(body):
    return (PAGE_HEAD + body + PAGE_TAIL).encode()


def render_home():
    return render("""
<h1>Gridge Server</h1>
<form action="/search" method="get">
  <label for="q">Service name or URL</label>
  <input type="text" id="q" name="q" required autofocus>
  <label><input type="checkbox" name="couch_mode"> Couch Mode (YouTube TV interface)</label>
  <button type="submit">Search</button>
</form>
""")


def render_search_results(query, couch_mode, matches):
    if not matches:
        return render(f"""
<h1>No matches</h1>
<p>No SteamGridDB results for "{html.escape(query)}".</p>
<p><a href="/">Back</a></p>
""")

    items = []
    for i, m in enumerate(matches):
        tag = " (verified)" if m.get("verified") else ""
        items.append(f"""
<div class="match">
  <label>
    <input type="radio" name="match_index" value="{i}" {"checked" if i == 0 else ""}>
    {html.escape(m['name'])}{tag}
  </label>
</div>""")

    couch_field = '<input type="hidden" name="couch_mode" value="1">' if couch_mode else ""
    return render(f"""
<h1>Pick a match</h1>
<form action="/add" method="post">
  <input type="hidden" name="query" value="{html.escape(query)}">
  {couch_field}
  {''.join(items)}
  <button type="submit">Add to Steam</button>
</form>
<p><a href="/">Back</a></p>
""")


def render_done(name, ok, error=None):
    if ok:
        return render(f"""
<h1>Done</h1>
<p><strong>{html.escape(name)}</strong> was added. Steam has restarted -- it should
show up in your library now.</p>
<p><a href="/">Add another</a></p>
""")
    return render(f"""
<h1>Failed</h1>
<p>Couldn't add <strong>{html.escape(name)}</strong>: {html.escape(str(error))}</p>
<p><a href="/">Back</a></p>
""")


class Handler(BaseHTTPRequestHandler):
    def _send_html(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/":
            self._send_html(render_home())
            return

        if parsed.path == "/search":
            query = (params.get("q") or [""])[0].strip()
            couch_mode = bool(params.get("couch_mode"))
            if not query:
                self._send_html(render_home())
                return
            matches = sgdb.search(create_webapp.clean_shortcut_name(query))
            self._send_html(render_search_results(query, couch_mode, matches))
            return

        self._send_html(render(f"<p>Not found: {html.escape(parsed.path)}</p>"), status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/add":
            self._send_html(render("<p>Not found</p>"), status=404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = urllib.parse.parse_qs(body)

        query = (params.get("query") or [""])[0]
        couch_mode = bool(params.get("couch_mode"))
        match_index = int((params.get("match_index") or ["0"])[0])

        clean_name = create_webapp.clean_shortcut_name(query)
        matches = sgdb.search(clean_name)
        if not matches or match_index >= len(matches):
            self._send_html(render_done(query, ok=False, error="match no longer available, please search again"))
            return
        chosen = matches[match_index]

        try:
            slug = create_webapp.slugify(chosen["name"])
            asset_paths = create_webapp.fetch_assets(chosen["id"], slug)
            url = query if query.startswith("http") else f"https://{query}"

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
