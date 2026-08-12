#!/usr/bin/env python3
"""Standalone fullscreen window showing the current pairing code.
Separate entrypoint (like splash.py) since it's launched headless via
gamescope_splash.launch_foregrounded(), not opened by a user. Same
plain-Gtk4-no-Adw reasoning as splash.py: runs natively on the host,
outside Gridge's own Flatpak sandbox where libadwaita normally comes
from.
"""
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

import window_titles

_CSS = b"""
window { background-color: #1e1e1e; color: #ffffff; }
label.gridge-auth-title { font-size: 24px; }
label.gridge-auth-code { font-size: 96px; font-family: monospace; letter-spacing: 12px; margin-top: 24px; }
"""


class AuthScreen(Gtk.ApplicationWindow):
    def __init__(self, app, code):
        super().__init__(application=app)
        self.set_title(window_titles.AUTH_SCREEN_TITLE)
        self.fullscreen()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        title = Gtk.Label(label="Enter this code on your device to add shortcuts:")
        title.add_css_class("gridge-auth-title")
        code_label = Gtk.Label(label=code)
        code_label.add_css_class("gridge-auth-code")
        box.append(title)
        box.append(code_label)
        self.set_child(box)


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else "??????"

    app = Gtk.Application(application_id="io.github.ScarletPachydermDev.Gridge.AuthScreen")

    def on_activate(app):
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        win = AuthScreen(app, code)
        win.present()

    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    main()
