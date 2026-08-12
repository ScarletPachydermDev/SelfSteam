"""Window titles shared between the process that launches/foregrounds a
splash window (maintenance.py, auth_display.py -- no GTK dependency)
and the process that actually renders it (splash.py, auth_screen.py --
does need GTK). A plain constants module so the launching side never
has to import gi just to know what title to look for."""

SPLASH_TITLE = "Gridge Splash"
AUTH_SCREEN_TITLE = "Gridge Auth Screen"
