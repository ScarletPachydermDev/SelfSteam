# Install

## Requirements

- An **x86_64 (x64)** device: a Steam Deck, a Steam Machine, or an x86_64 PC
  running a Steam gamescope session. SelfSteam is only built for x86_64.
- SteamOS, or another distribution with a gamescope session (Bazzite,
  ChimeraOS). SteamOS is the most tested.
- The Flathub remote, for RetroArch, apps and emulators.
- A [SteamGridDB](https://www.steamgriddb.com/profile/preferences/api) API key, for artwork.

## Installing

In Desktop Mode, run in a terminal:

```
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo && flatpak remote-add --user --if-not-exists selfsteam https://raw.githubusercontent.com/ScarletPachydermDev/SelfSteam/master/packaging/io.github.ScarletPachydermDev.SelfSteam.flatpakrepo && flatpak install --user selfsteam io.github.ScarletPachydermDev.SelfSteam
```

Then open **SelfSteam** once from the app menu. It:

1. starts its background service, which keeps running in Game Mode and
   after a reboot;
2. adds a **SelfSteam** shortcut to your Steam library, with its artwork;
3. restarts Steam so the shortcut shows up (after any game you are playing);
4. shows the code screen, to pair your first device (see [Using SelfSteam](USING.md)).

You can also install the `.flatpak` file from a
[release](https://github.com/ScarletPachydermDev/SelfSteam/releases); it
carries the same update address.

## Updating

`flatpak update`, or Discover, picks up new releases. The background service
restarts onto the new version by itself within 15 minutes, or straight away
when you open SelfSteam.

## Uninstalling

```
systemctl --user disable --now selfsteam.service
rm ~/.config/systemd/user/selfsteam.service
flatpak uninstall --user io.github.ScarletPachydermDev.SelfSteam
```

What stays until you remove it yourself:

- `~/.var/app/io.github.ScarletPachydermDev.SelfSteam`: settings, your
  SteamGridDB key, paired devices.
- `~/.local/share/selfsteam`: uploaded ROMs, converted files, AppImages,
  Preflight.
- Your Steam shortcuts, and the launcher they use
  (`~/.local/share/Steam/selfsteam-launcher`).
