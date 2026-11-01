# Using SelfSteam

## Pairing a device

1. In Game Mode, launch the **SelfSteam** shortcut (or open SelfSteam from
   the app menu in Desktop Mode).
2. The code screen shows the address to open, for example
   `http://192.168.1.20:8845`, and a 6-character code.
3. Open the address on your phone or computer and enter the code.

- A code works once, and expires after 10 minutes unused; the screen shows
  the new one by itself.
- A login lasts 12 hours. Tick **Remember this device** to stay logged in
  for 90 days, renewed each time you use it.
- Any controller button or key closes the code screen. It also closes by
  itself once you log in.

## Home

- **Add new shortcut** opens the tabs below.
- Your shortcuts are grouped into **Games** (RetroArch and Emulators tab),
  **Sites**, **Apps**, and **Others** (shortcuts added in Steam itself).
- Each poster has **Edit** (back to the tab it was made with, filled in) and
  **Remove**.

## Tabs

- **URL**: a website or streaming service, opened fullscreen in a browser.
- **Apps**: Flathub apps and a few AppImages. Installed for you if needed.
- **RetroArch**: pick a console and upload or pick a ROM. SelfSteam installs
  RetroArch and the core, and any BIOS files you upload.
- **Emulators**: pick an emulator and a game. Upload keys and firmware where
  the emulator needs them, and DLC and updates for Switch and PS4 games.
  Optional **Preflight** toggle for controllers.

Artwork comes from SteamGridDB; you can pick other images before creating
the shortcut.

## Saving

Changes are queued. **Save changes and restart Steam** writes them and
restarts Steam, since Steam only reads its shortcuts when it starts. A
"Restarting Steam…" screen covers the TV while Steam is down.

## Things SelfSteam does for you

- Archives (zip, 7z, rar) are unpacked next to themselves, and the archive
  goes to the bin.
- Switch `.nsz` games, updates and DLC are converted to `.nsp`.
- 3D consoles render at 1080p by default (one step lower on a Steam Deck);
  a resolution you set yourself is never changed.
- N64 games in RetroArch get their black borders cropped automatically.
