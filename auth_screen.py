#!/usr/bin/env python3
"""Standalone fullscreen window showing the current pairing code.
Separate entrypoint (like splash.py) since it's launched headless via
gamescope_splash.launch_foregrounded(), not opened by a user. Same
plain-Gtk4-no-Adw reasoning as splash.py: runs natively on the host,
outside SelfSteam's own Flatpak sandbox where libadwaita normally comes
from.
"""
import os
import signal
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# evdev is what actually sees a real gamepad's own button presses --
# confirmed live (2026-08-21) that a plain Gtk.EventControllerKey (the
# existing keyboard handler below) never fires for one at all. Steam's
# own "lizard mode" translates a controller to synthetic keyboard/mouse
# events while idle/navigating menus, which is why keyboard exit alone
# happened to work for some controllers -- but a real joystick-class
# device (confirmed via a genuine Xbox-360-compatible pad on the Steam
# Machine) reports raw EV_KEY gamepad button codes instead, which only
# evdev sees. Optional import: not every host this runs on is
# guaranteed to have python3-evdev installed (SteamOS ships it, since
# Steam Input itself depends on it, but this app isn't SteamOS-only) --
# gamepad exit just silently isn't available without it, keyboard exit
# still works regardless.
try:
    import evdev
    from evdev import ecodes
except ImportError:
    evdev = None

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
#   _watch_gamepads(on_press) -- fires on_press() on the first raw gamepad button press, if evdev is available.
#   class AuthScreen -- the fullscreen GTK window itself, showing the code + LAN address.
#   main() -- entrypoint: parses argv, builds and shows an AuthScreen.
_BASE_SIZES = {
    "title_font": 90, "code_font": 420, "code_spacing": 20, "code_margin_top": 40,
    "address_font": 46, "address_margin_top": 24, "address_margin_left": 32,
    "hint_font": 46, "hint_margin_bottom": 24,
}


def _watch_gamepads(on_press):
    """Fires on_press() the first time any button is pressed on any
    currently-connected gamepad-class evdev device -- detected via the
    presence of BTN_GAMEPAD in a device's own EV_KEY capabilities, the
    same heuristic udev/SDL's own joystick detection already uses
    (BTN_GAMEPAD and BTN_SOUTH/BTN_A share event code 304 by kernel
    convention, so this also naturally covers plain BTN_SOUTH-only
    devices). No-op if evdev isn't importable. Devices are polled via
    GLib.io_add_watch on their own fd, integrated into the same GLib
    main loop the rest of this window already runs on -- not a
    separate thread, so no locking needed around on_press (which just
    calls app.quit(), same as the keyboard handler)."""
    if evdev is None:
        return
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        if ecodes.BTN_GAMEPAD in dev.capabilities().get(ecodes.EV_KEY, []):
            devices.append(dev)

    def _on_readable(_fd, _condition, dev):
        try:
            events = list(dev.read())
        except OSError:
            return False
        for event in events:
            if event.type == ecodes.EV_KEY and event.value == 1:
                on_press()
                return False
        return True

    for dev in devices:
        GLib.io_add_watch(dev.fd, GLib.IO_IN, lambda fd, cond, dev=dev: _on_readable(fd, cond, dev))


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
  /* At this font-size (420px base), Pango's own line-box height is
     dominated by the font's ascent/descent metrics, not the visible
     glyph height -- that extra internal space above the glyphs is what
     read as "too much gap" between title and code, not margin-top
     itself (which is comparatively small). line-height (GTK 4.12+)
     compresses that line-box directly instead of fighting it with a
     larger negative margin. Needs visual confirmation on a real
     screen -- not something verifiable from source alone. */
  line-height: 0.75;
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

    # GApplication uses D-Bus for single-instance uniqueness by app id --
    # confirmed live (2026-08-21) that running this dev-test copy
    # alongside the real installed one, both on the same D-Bus session
    # bus with the same hardcoded id, made the second one just remote-
    # activate the first (which already owned the name) and exit
    # immediately instead of showing its own window at all. Only ever
    # relevant for side-by-side dev-testing (see dev-test.sh) -- the
    # real app never sets SELFSTEAM_DEV_TEST, so its id is unaffected.
    app_id = "io.github.ScarletPachydermDev.SelfSteam.AuthScreen"
    if os.environ.get("SELFSTEAM_DEV_TEST"):
        app_id += ".DevTest"
    app = Gtk.Application(application_id=app_id)

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
        # Same "press anything to exit" as the keyboard handler above,
        # for a real gamepad -- see _watch_gamepads' own docstring for
        # why GTK's key controller alone doesn't already cover this.
        _watch_gamepads(app.quit)

    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    main()
