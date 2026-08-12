#!/usr/bin/env python3
"""Standalone fullscreen "please wait" window shown while Steam is down
for a shortcuts.vdf/artwork write from Gridge Server. Separate
entrypoint from gui.py (Gridge's main interactive app) since this needs
to run headless-launched via gamescope_splash.launch_foregrounded(),
not opened by a user.
"""
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

import window_titles

# Plain Gtk4, deliberately no libadwaita: this runs natively on the host
# (not inside Gridge's own Flatpak sandbox, which is where Adw normally
# comes from), and SteamOS doesn't ship libadwaita for host Python.

_CSS = b"""
label.gridge-splash-message { font-size: 24px; }
window { background-color: #1e1e1e; color: #ffffff; }
"""


class SplashWindow(Gtk.ApplicationWindow):
    def __init__(self, app, message):
        super().__init__(application=app)
        self.set_title(window_titles.SPLASH_TITLE)
        self.fullscreen()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True, width_request=48, height_request=48)
        label = Gtk.Label(label=message)
        label.add_css_class("gridge-splash-message")
        box.append(spinner)
        box.append(label)
        self.set_child(box)


def main():
    message = sys.argv[1] if len(sys.argv) > 1 else "Applying changes…"

    app = Gtk.Application(application_id="io.github.ScarletPachydermDev.Gridge.Splash")

    def on_activate(app):
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        win = SplashWindow(app, message)
        win.present()

    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    main()
