#!/usr/bin/env python3
"""Standalone fullscreen window showing the current pairing code.
Separate entrypoint (like splash.py) since it's launched headless via
gamescope_splash.launch_foregrounded(), not opened by a user. Same
plain-Gtk4-no-Adw reasoning as splash.py: runs natively on the host,
outside SelfSteam's own Flatpak sandbox where libadwaita normally comes
from.
"""
import signal
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import window_titles

# Sizes below are tuned by eye against a 1080p TV -- everything scales
# from that baseline by actual screen height (_screen_scale) so a 720p
# or 4K display reads at the same *proportional* size instead of the
# code shrinking to a sliver on a 4K set or overflowing a 720p one.
# GTK CSS has no vw/vh-style unit to do this on its own (that's a web
# CSS thing), so the px values are computed and interpolated in Python
# before the stylesheet is ever loaded.
# Functions:
#   _screen_scale() -- proportional font/margin scale factor for the real screen height.
#   _build_css() -- builds the GTK stylesheet from _BASE_SIZES scaled by _screen_scale().
#   class AuthScreen -- the fullscreen GTK window itself, showing the code + LAN address.
#   main() -- entrypoint: parses argv, builds and shows an AuthScreen.
_BASE_SIZES = {
    "title_font": 90, "code_font": 420, "code_spacing": 20, "code_margin_top": 40,
    "address_font": 46, "address_margin_top": 24, "address_margin_left": 32,
    "hint_font": 46, "hint_margin_bottom": 24,
}


def _screen_scale():
    display = Gdk.Display.get_default()
    monitors = display.get_monitors()
    if monitors.get_n_items() == 0:
        return 1.0
    geometry = monitors.get_item(0).get_geometry()
    return geometry.height / 1080.0


def _build_css(scale):
    s = {name: round(value * scale) for name, value in _BASE_SIZES.items()}
    return f"""
window {{ background-color: #000000; color: #ffffff; }}
label.selfsteam-auth-title {{
  font-family: Helvetica, Arial, sans-serif; font-size: {s['title_font']}px; font-weight: 400;
}}
label.selfsteam-auth-code {{
  font-family: Helvetica, Arial, sans-serif; font-weight: 700;
  font-size: {s['code_font']}px; letter-spacing: {s['code_spacing']}px; margin-top: {s['code_margin_top']}px;
}}
label.selfsteam-auth-address {{
  font-family: Helvetica, Arial, sans-serif; font-size: {s['address_font']}px; font-weight: 400;
  margin: {s['address_margin_top']}px 0 0 {s['address_margin_left']}px;
}}
label.selfsteam-auth-hint {{
  font-family: Helvetica, Arial, sans-serif; font-size: {s['hint_font']}px; font-weight: 400;
  margin-bottom: {s['hint_margin_bottom']}px;
}}
""".encode()


class AuthScreen(Gtk.ApplicationWindow):
    # Same layout as ChimeraOS's own authenticator screen
    # (chimera_app/authenticator.py): host/IP pinned top-left, the code
    # itself centered, an exit hint pinned to the bottom -- this is a
    # living-room device meant to be read from across the room, so
    # nothing here is a fresh design, just matching the screen users
    # already know from that flow.
    def __init__(self, app, code, hostname, ip, port):
        super().__init__(application=app)
        self.set_title(window_titles.AUTH_SCREEN_TITLE)
        self.fullscreen()

        overlay = Gtk.Overlay()

        center_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        title = Gtk.Label(label="SelfSteam authentication code")
        title.add_css_class("selfsteam-auth-title")
        code_label = Gtk.Label(label=code)
        code_label.add_css_class("selfsteam-auth-code")
        center_box.append(title)
        center_box.append(code_label)
        overlay.set_child(center_box)

        address_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START, valign=Gtk.Align.START)
        for line in (f"{hostname}:{port}", f"{ip}:{port}"):
            line_label = Gtk.Label(label=line, halign=Gtk.Align.START)
            line_label.add_css_class("selfsteam-auth-address")
            address_box.append(line_label)
        overlay.add_overlay(address_box)

        hint_label = Gtk.Label(label="Press any button or ESC to exit this screen",
                                halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
        hint_label.add_css_class("selfsteam-auth-hint")
        overlay.add_overlay(hint_label)

        self.set_child(overlay)

        # Matches the hint above: any keypress dismisses the screen
        # locally. This doesn't touch the code's validity server-side --
        # auth_display.ensure_shown() just puts it right back up on the
        # next unauthenticated request, same as if this window had
        # never been closed.
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", lambda *_args: app.quit())
        self.add_controller(key_controller)


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else "??????"
    hostname = sys.argv[2] if len(sys.argv) > 2 else ""
    ip = sys.argv[3] if len(sys.argv) > 3 else ""
    port = sys.argv[4] if len(sys.argv) > 4 else ""

    app = Gtk.Application(application_id="io.github.ScarletPachydermDev.SelfSteam.AuthScreen")

    def on_activate(app):
        provider = Gtk.CssProvider()
        provider.load_from_data(_build_css(_screen_scale()))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        win = AuthScreen(app, code, hostname, ip, port)
        win.present()
        # auth_display.py tears this window down with a plain SIGTERM
        # (either a fresh code replacing this one, or the code being
        # used/expiring) -- Python/GTK run no cleanup at all on a bare
        # SIGTERM by default, so the fullscreen Wayland surface never
        # gets unmapped through the normal protocol path. Confirmed
        # live: that left Mutter's window stacking in a bad enough
        # state to freeze mouse input system-wide, not just break this
        # window. GLib.unix_signal_add (not Python's own signal.signal)
        # is the safe way to handle a Unix signal inside a GLib main
        # loop -- routes it through the loop itself instead of
        # interrupting a C call at a random point.
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, app.quit)

    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    main()
