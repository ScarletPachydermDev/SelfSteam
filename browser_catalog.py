"""Curated Flathub browser catalog for the URL tab's own browser picker.

Replaces the old "list whatever Flatpak browsers happen to already be
installed" approach (browser_picker.py, now unused/removed) -- a
machine with zero Flatpak browsers installed had nothing to offer at
all. Small, hand-picked list, same spirit as appimage_apps.APPS/
standalone_emulators.EMULATORS but for browsers: installed the same
on-demand way via the existing generic Flathub install machinery
(standalone_emulators.install_flathub_app_id/flathub_app_id_installed/
installed_flathub_app_ids), not duplicated here.

Icon URLs and summaries confirmed live against Flathub's own appstream
API (flathub.org/api/v2/appstream/<app_id>), not guessed.

BROWSERS is the main list; OPERA is kept separate (see
selfsteam_server._url_browser_picker_html's own rendering) since it's
the standout pick for one specific, narrow reason -- not a general
recommendation like the other four.
"""

BROWSERS = [
    {
        "app_id": "com.google.Chrome",
        "name": "Google Chrome",
        "summary": "Full Widevine DRM support out of the box -- the most broadly compatible pick for streaming sites.",
        "icon": "https://dl.flathub.org/media/com/google/Chrome/3e01237da996c0857954ae1d08b2ab0f/icons/128x128/com.google.Chrome.png",
    },
    {
        "app_id": "com.microsoft.Edge",
        # Confirmed real, not guessed: the only Chromium derivative that
        # ships Dolby Digital Plus/Atmos audio decoding on Linux (Google
        # never licensed Dolby codecs into open-source Chromium) -- see
        # edge_launcher.py's own module docstring.
        "name": "Microsoft Edge",
        "summary": "The only browser here with native Dolby Digital Plus/Atmos audio decoding on Linux.",
        "icon": "https://dl.flathub.org/media/com/microsoft/Edge/ae772fac86008d6d9acbb176426183b6/icons/128x128/com.microsoft.Edge.png",
    },
    {
        "app_id": "com.vivaldi.Vivaldi",
        "name": "Vivaldi",
        "summary": "Chromium-based with full DRM support, no rewards program, crypto wallet, or account nagging.",
        "icon": "https://dl.flathub.org/media/com/vivaldi/Vivaldi/4081badeb24f9ae12d56005403cf5715/icons/128x128/com.vivaldi.Vivaldi.png",
    },
    {
        "app_id": "org.mozilla.firefox",
        "name": "Firefox",
        "summary": "Widevine DRM via its own plugin system; shows sponsored tiles on its New Tab page by default.",
        "icon": "https://dl.flathub.org/media/icons/128x128/org.mozilla.firefox.png",
    },
]

# Kept apart from BROWSERS -- Netflix's own help page (help.netflix.com/
# en/node/23742, confirmed live) lists Chrome/Firefox/Edge as capped at
# 720p on Linux, Opera at full 1080p. Its "--app=<url>" is silently
# ignored though (confirmed live, repeatedly, on fresh AND already-
# onboarded profiles -- it always opens Speed Dial regardless); the
# fix is passing the URL as a bare positional argument alongside
# --kiosk instead, which works correctly and even skips onboarding via
# plain --no-first-run (see browser_launcher.py's own OPERA_APP_IDS
# branch) -- so despite the different flag recipe, it's just as usable
# as the others once launched correctly.
OPERA = {
    "app_id": "com.opera.Opera",
    "name": "Opera",
    "summary": "The only browser here confirmed to stream Netflix at full 1080p on Linux -- every other one is capped at 720p.",
    "icon": "https://dl.flathub.org/media/com/opera/Opera/5f5b53cca0cfb8318927f085c8dbe6e4/icons/128x128/com.opera.Opera.png",
}

ALL_BROWSERS = BROWSERS + [OPERA]
