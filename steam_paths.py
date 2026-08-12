"""Locate Steam's userdata directory across native and Flatpak installs."""
import os

NATIVE_ROOTS = ["~/.local/share/Steam", "~/.steam/steam"]
FLATPAK_ROOT = "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"


class SteamNotFoundError(RuntimeError):
    pass


def find_steam_root():
    """Return the first Steam install root that has a userdata dir."""
    for path in NATIVE_ROOTS + [FLATPAK_ROOT]:
        root = os.path.expanduser(path)
        if os.path.isdir(os.path.join(root, "userdata")):
            return root
    raise SteamNotFoundError(
        "No Steam install found (checked native ~/.local/share/Steam, "
        "~/.steam/steam, and Flatpak com.valvesoftware.Steam)"
    )


def find_userdata_dir(user_id=None):
    """Return <steam_root>/userdata/<user_id>. Picks the sole numeric user
    dir automatically unless there's more than one, in which case user_id
    must be given."""
    root = find_steam_root()
    userdata = os.path.join(root, "userdata")
    user_ids = [d for d in os.listdir(userdata) if d.isdigit()]
    if not user_ids:
        raise SteamNotFoundError(f"No Steam user directories found under {userdata}")
    if user_id is None:
        if len(user_ids) > 1:
            raise SteamNotFoundError(
                f"Multiple Steam users found under {userdata}: {user_ids}. "
                "Pass a user_id to pick one."
            )
        user_id = user_ids[0]
    elif user_id not in user_ids:
        raise SteamNotFoundError(f"Steam user '{user_id}' not found under {userdata}")
    return os.path.join(userdata, user_id)
