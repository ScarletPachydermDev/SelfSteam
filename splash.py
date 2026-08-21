#!/usr/bin/env python3
"""Standalone fullscreen "please wait" window shown while Steam is down
for a shortcuts.vdf/artwork write from the SelfSteam server. Separate
entrypoint from selfsteam_server.py (the main server loop) since this
needs to run headless-launched via gamescope_splash.launch_foregrounded(),
not opened by a user.
"""
import signal
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import window_titles

# Functions:
#   class SplashWindow -- the fullscreen "please wait" GTK window itself.
#   main() -- entrypoint: parses argv, builds and shows a SplashWindow.

# Plain Gtk4, deliberately no libadwaita: this runs natively on the host
# (not inside SelfSteam's own Flatpak sandbox, which is where Adw
# normally comes from), and SteamOS doesn't ship libadwaita for host
# Python.

_CSS = b"""
label.selfsteam-splash-message { font-size: 24px; }
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
        label.add_css_class("selfsteam-splash-message")
        box.append(spinner)
        box.append(label)
        self.set_child(box)


def main():
    message = sys.argv[1] if len(sys.argv) > 1 else "Applying changes…"

    app = Gtk.Application(application_id="io.github.ScarletPachydermDev.SelfSteam.Splash")

    def on_activate(app):
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        win = SplashWindow(app, message)
        win.present()
        # See auth_screen.py's on_activate for why this matters: a bare
        # SIGTERM (what maintenance.py's own teardown sends) skips all
        # GTK/Wayland cleanup, and confirmed live that leaves Mutter's
        # window stacking broken enough to freeze mouse input entirely,
        # not just this window.
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, app.quit)

    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    main()
