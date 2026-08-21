#!/bin/sh
# -u (unbuffered) so print()/traceback output shows up live in a
# terminal instead of sitting in Python's stdout buffer -- useful when
# asking a user to run `flatpak run io.github.ScarletPachydermDev.SelfSteam`
# from a terminal to capture verbose output for a bug report. Same
# reasoning as gridge-desktop's own packaging/gridge.sh.
exec python3 -u /app/share/selfsteam/selfsteam_launcher.py "$@"
