# PS99 Companion Overlay

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

A tiny, always-on-top overlay for **Pet Simulator 99** (Roblox). Search a
clan or league and get its live roster, points, gems, and leaderboard rank —
right on top of your game, without a browser tab eating your RAM.

Built with plain `tkinter` — no Electron, no Chromium, no bloat. Just a
few MB of RAM and basically 0% idle CPU.

## Features

- 🔍 **Clan & league search** — full roster, points, gems, join dates
- 🏆 **Live leaderboard rank** — found via binary search, so it stays fast
  even with 100k+ clans/leagues
- 📈 **Live / Hourly rate tracking** — watch points climb in real time, or
  see an estimated points-per-hour rate
- 🔔 **Discord webhook alerts** — auto-post the tracked clan/league's rank
  and points to a webhook on every refresh
- ⚙️ **Fully customizable** — toggle columns, refresh rate, member count,
  username filter
- 💾 **Settings persist automatically** — everything's saved to
  `%APPDATA%\PS99Overlay\settings.json` and restored on next launch
- 📌 **Pin on top** — drag it anywhere, keep it above your game window

## Installation

### Option A — Run from source
```bash
pip install -r requirements.txt
python ps99_overlay.py
```
Or just double-click `run.bat` on Windows — it handles the Python check and
dependency install for you.

### Option B — Build a standalone .exe
If you've got an `icon.ico` in the project folder, run `build_exe.bat`. It
installs PyInstaller and spits out a single portable `PS99Overlay.exe` in
`dist\`.

### Option C — Use the .exe
Or well just use the .exe already provided in the Repo.

## Using it

- Pick **clan** or **league**, type a name, hit Enter or 🔍
- Click **⚙** for settings — toggle columns, refresh rate, member count,
  Live vs Hourly-estimate mode, and a username filter
- Inside settings, **🔔 Webhook alerts** opens a small tab to enable Discord
  webhook updates — every successful refresh posts the tracked
  clan/league's name, rank, and points
- **✨ Credits** shows who built this thing
- Drag the title bar to move the window. 📌 toggles always-on-top. ✕ closes.
- The status bar shows a loading spinner while fetching, and a live
  "next refresh in Ns" countdown between polls.

## Data source & known limits

Everything comes from BIG Games' public API (`ps99.biggamesapi.io`), plus
Roblox's own API for resolving usernames. A few things are limits of the
API itself, not bugs in this app:

- Per-member **points/gems for clans** only exist for the ~top 25 clans
  sampled by the API. Smaller clans show `-` for those two columns; the
  roster itself still works for any clan.
- **Leagues** expose exact per-member points for everyone, but have no
  gem/diamond stat at all.
- There's **no historical/hourly endpoint** anywhere. "Hourly ≈" mode is a
  local estimate based on the change between the last two polls — give it
  a little time running before trusting the number.

## Tech notes

- Pure stdlib `tkinter` + `requests`, with optional `Pillow` for clan/league
  icons (falls back gracefully if Pillow isn't installed)
- All network calls run on background threads — the UI never blocks
- A generation counter guards against stale requests clobbering a newer
  search (e.g. if you search something new while an old request is still
  in flight)

## Credits

| Role | Name |
|---|---|
| Head of Project | [@Justinomvv](https://github.com/Justinomvv) |
| Assistant Dev | Claude |

- Roblox: `@Justinomvv`
- Discord: `@6ypb`
- GitHub: `@Justinomvv`

## License

MIT — do whatever you want with it.
