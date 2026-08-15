"""Kiosk-mode launch command construction for browsers other than Edge.

edge_launcher.py already handles Edge's own first-run/onboarding
suppression quirks separately and stays untouched -- this module only
covers the *other* Flatpak browsers browser_picker.py can detect.
Confirmed live this session (LibreWolf, Zen; Chromium family inferred
from Edge's own proven flags, which are standard Chromium behavior
shared across derivatives, not Edge-specific):

- Chromium-family: identical flag set to Edge's own (--app=/--kiosk/
  --start-fullscreen/--hide-scrollbars/--no-first-run/
  --no-default-browser-check), no persistent state needed -- kiosk
  mode is entirely a launch-time flag, so the user's regular browsing
  in that same browser/profile is completely unaffected either way.
- Plain Firefox-family (LibreWolf confirmed): --kiosk --new-instance
  plus the URL is enough on its own, same "flag-only" property as
  Chromium.
- Zen specifically: --kiosk alone only hides the tab bar, not Zen's
  own vertical sidebar -- that needs persistent prefs
  (zen.view.compact.*), which is NOT flag-only like the others. Since
  those prefs would also apply the next time the user opens Zen
  normally (same profile, same persistent state), Zen gets a
  dedicated Gridge-only profile instead of the shared default one --
  an explicit tradeoff (no shared logins/addons for Zen specifically)
  the user chose over affecting their regular Zen browsing.
"""
import os
import shlex
import subprocess

import host_exec

CHROMIUM_APP_IDS = {
    "com.microsoft.Edge",  # handled by edge_launcher.py instead, listed for classification only
    "com.google.Chrome",
    "com.google.ChromeDev",
    "org.chromium.Chromium",
    "io.github.ungoogled_software.ungoogled_chromium",
    "com.brave.Browser",
    "com.vivaldi.Vivaldi",
    "com.opera.Opera",
    "com.opera.opera-gx",
}

FIREFOX_APP_IDS = {
    "org.mozilla.firefox",
    "io.gitlab.librewolf-community",
    "net.waterfox.waterfox",
    "one.ablaze.floorp",
}

ZEN_APP_ID = "app.zen_browser.zen"

# Gridge-dedicated Zen profile -- see module docstring for why this
# can't just be Zen's own default profile.
ZEN_PROFILE_DIR = os.path.expanduser("~/.var/app/app.zen_browser.zen/gridge-kiosk-profile")

# Confirmed live by reading Zen's own source (ZenCompactMode.mjs) --
# zen.view.compact.enable-at-startup is the actual persistent switch
# behind Zen's Ctrl+S "Compact Mode" toggle; --kiosk alone doesn't
# touch it at all. The rest suppress Zen's own onboarding (separate
# from stock Firefox's browser.aboutwelcome) so a fresh profile doesn't
# show a first-run wizard in place of the target URL.
_ZEN_KIOSK_PREFS = {
    "zen.view.compact.enable-at-startup": True,
    "zen.view.compact.hide-tabbar": True,
    "zen.view.compact.hide-toolbar": True,
    "zen.view.compact.show-sidebar-and-toolbar-on-hover": False,
    "zen.view.experimental-no-window-controls": True,
    "browser.aboutwelcome.enabled": False,
    "startup.homepage_welcome_url": "",
    "startup.homepage_welcome_url.additional": "",
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.upgradeDialog.enabled": False,
    "browser.uitour.enabled": False,
    "zen.welcome-screen.seen": True,
}


class UnsupportedBrowserError(RuntimeError):
    pass


def _pref_line(name, value):
    if isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, str):
        literal = f'"{value}"'
    else:
        literal = str(value)
    return f'user_pref("{name}", {literal});'


def _ensure_zen_profile():
    """(Re)writes the dedicated profile's user.js on every call --
    Firefox-family browsers read user.js fresh at each startup, so this
    is cheap and keeps the seeded prefs in sync if this module's own
    list of them ever changes, not just on first creation."""
    os.makedirs(ZEN_PROFILE_DIR, exist_ok=True)
    with open(os.path.join(ZEN_PROFILE_DIR, "user.js"), "w") as f:
        f.write("\n".join(_pref_line(k, v) for k, v in _ZEN_KIOSK_PREFS.items()) + "\n")


def _flatpak_installed(flatpak_exe, app_id):
    result = subprocess.run(
        host_exec.wrap([flatpak_exe, "info", app_id]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def kiosk_launch_args(browser_app_id, url, couch_mode, youtube_tv_user_agent=None):
    """argv for launching `browser_app_id` (a Flatpak app id, not Edge)
    in kiosk mode against `url`, already shell-quoted (safe to
    " ".join() directly into a shortcut's LaunchOptions string) --
    quoting happens here, not in the caller, since only this function
    actually knows which element (if any) needs it per browser family.
    Only covers browsers confirmed working this session -- raises
    UnsupportedBrowserError rather than guessing flags for anything
    else, since a wrong guess produces a shortcut that silently fails
    to open correctly instead of erroring loudly.

    couch_mode's YouTube TV user-agent spoof is Chromium-only for now
    (Firefox has no --user-agent command-line flag at all, only a
    profile pref -- general.useragent.override -- not implemented here
    yet): Firefox-family/Zen shortcuts still navigate to the TV URL
    under couch_mode, just without the UA override that makes YouTube
    actually serve the TV interface for it, so it isn't wired up
    silently as if it worked."""
    flatpak = host_exec.which("flatpak")
    if not flatpak:
        raise UnsupportedBrowserError("flatpak isn't available on this host")
    if not _flatpak_installed(flatpak, browser_app_id):
        raise UnsupportedBrowserError(f"{browser_app_id} is not installed as a Flatpak")

    if browser_app_id in CHROMIUM_APP_IDS:
        args = [
            flatpak, "run", browser_app_id,
            f"--app={url}", "--kiosk", "--start-fullscreen", "--hide-scrollbars",
            "--no-first-run", "--no-default-browser-check",
        ]
        if couch_mode and youtube_tv_user_agent:
            # LaunchOptions is stored/parsed as one shell-like string,
            # and the TV user-agent has spaces/parens/semicolons in it --
            # unquoted, it gets word-split into several bogus arguments.
            args.append(shlex.quote(f"--user-agent={youtube_tv_user_agent}"))
        return args

    if browser_app_id == ZEN_APP_ID:
        _ensure_zen_profile()
        return [flatpak, "run", browser_app_id, "--kiosk", "--new-instance", "--profile", ZEN_PROFILE_DIR, url]

    if browser_app_id in FIREFOX_APP_IDS:
        return [flatpak, "run", browser_app_id, "--kiosk", "--new-instance", url]

    raise UnsupportedBrowserError(f"{browser_app_id} isn't a supported kiosk-mode browser yet")
