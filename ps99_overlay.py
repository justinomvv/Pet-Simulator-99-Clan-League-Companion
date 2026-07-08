"""
PS99 Companion Overlay
----------------------
Small always-on-top overlay for Pet Simulator 99. Search a clan or league
and get its roster, points, gems, all that. Built with plain tkinter (no
Electron, nothing fancy) so it barely uses any RAM/CPU.

Everything comes from BIG Games' public API (https://ps99.biggamesapi.io):
  - legacy /api/clan/{name}            -> roster, medals, country, created date
  - legacy /api/clans + /api/clansTotal-> full leaderboard, used to binary-search
                                           a clan's current rank
  - v1     /v1/clans/players           -> per-member points & diamonds, but only
                                           for the ~top 25 clans (that's an API
                                           limitation, not something we can fix)
  - v1     /v1/leagues/{name}          -> league roster + exact per-member points
  - v1     /v1/leagues (paginated)     -> same leaderboard trick as clans
  - users.roblox.com/v1/users          -> turns UserIDs into real usernames so
                                           smaller clans don't just show numbers

Known limitations (API's fault, not a bug here):
  - Gems + live battle points for clans only exist for the sampled top ~25.
    Anyone outside that shows "-" for those two columns, roster still works fine.
  - Leagues get exact points for every member, no sampling, but no gems at all.
  - There's no hourly-history endpoint anywhere. "Hourly" mode is just a local
    estimate based on how much points changed between the last two polls, so
    give it a bit before trusting the number.
  - Rank lookup uses a binary search over the leaderboard instead of scanning
    the whole thing, so it stays fast even with 100k+ clans/leagues.
"""

import io
import json
import math
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from datetime import datetime

import requests

try:
    from PIL import Image, ImageTk
    PIL_OK = True
except ImportError:
    PIL_OK = False

# ---------------------------------------------------------------------------
# API stuff
# ---------------------------------------------------------------------------

LEGACY = "https://ps99.biggamesapi.io/api"
V1 = "https://ps99.biggamesapi.io/v1"
IMG_PROXY = "https://ps99.biggamesapi.io/image/{}"
TIMEOUT = 8

# Settings live in %APPDATA%\PS99Overlay\settings.json on Windows. Falls back
# to a folder in the home dir on other OSes just so it doesn't blow up there.
APP_DIR = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), "PS99Overlay")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")


class ApiError(Exception):
    pass


def _get(url, params=None):
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise ApiError(data.get("error", {}).get("message", "Unknown error"))
    return data["data"]


def fetch_clan(name):
    """Roster + metadata for a single clan (legacy endpoint)."""
    return _get(f"{LEGACY}/clan/{name}")


def fetch_league(name):
    """Roster + exact per-member points for a single league (v1 endpoint)."""
    return _get(f"{V1}/leagues/{name}")


def fetch_clan_player_sample():
    """Top-25-sampled clan players, keyed by UserID -> stats. Used to enrich
    clan rosters with per-member points/gems when available."""
    try:
        data = _get(f"{V1}/clans/players")
    except Exception:
        return {}
    return {p["UserID"]: p for p in data.get("players", [])}


RANK_PAGE_SIZE = 100
RANK_LINEAR_CAP_PAGES = 50  # fallback-only cap: 50 * 100 = top 5000


def find_leaderboard_rank(kind, name, target_points):
    """Figure out where this clan/league currently sits on the real points
    leaderboard (same one BIG Games' site uses).

    If we already know its Points total (leagues always give it, clans
    usually do), we binary-search the sorted leaderboard for it — only
    ~log2(total/100) requests, so it's fine even with 100k+ entries. If we
    don't have a points total to search against, fall back to scanning the
    top of the leaderboard instead of just guessing, capped so it doesn't
    get out of hand."""
    name_lower = name.lower()
    if kind == "clan":
        list_url = f"{LEGACY}/clans"
        total = _get(f"{LEGACY}/clansTotal")

        def get_page(p):
            return _get(list_url, params={"page": p, "pageSize": RANK_PAGE_SIZE,
                                           "sort": "Points", "sortOrder": "desc"})
    else:
        list_url = f"{V1}/leagues"

        def get_page(p):
            data = _get(list_url, params={"page": p, "pageSize": RANK_PAGE_SIZE,
                                           "sort": "Points", "sortOrder": "desc"})
            return data["leagues"]

        first = _get(list_url, params={"page": 1, "pageSize": RANK_PAGE_SIZE,
                                        "sort": "Points", "sortOrder": "desc"})
        total = first.get("total", 0)

    if not total:
        return None, total

    if target_points is None:
        # can't binary search without a points total, so just scan the top
        # of the leaderboard instead — capped so it doesn't take forever
        pages = min(RANK_LINEAR_CAP_PAGES, math.ceil(total / RANK_PAGE_SIZE))
        for p in range(1, pages + 1):
            items = get_page(p)
            if not items:
                break
            for idx, it in enumerate(items):
                if it.get("Name", "").lower() == name_lower:
                    return (p - 1) * RANK_PAGE_SIZE + idx + 1, total
        return None, total

    hi_page = max(1, math.ceil(total / RANK_PAGE_SIZE))
    lo, hi = 1, hi_page
    while lo <= hi:
        mid = (lo + hi) // 2
        items = get_page(mid)
        if not items:
            hi = mid - 1
            continue
        top_pts, bottom_pts = items[0]["Points"], items[-1]["Points"]
        if target_points > top_pts:
            hi = mid - 1
        elif target_points < bottom_pts:
            lo = mid + 1
        else:
            for idx, it in enumerate(items):
                if it.get("Name", "").lower() == name_lower:
                    return (mid - 1) * RANK_PAGE_SIZE + idx + 1, total
            # tie on points / name didn't match exactly — just grab the
            # first entry at or below the target as a close-enough spot
            for idx, it in enumerate(items):
                if it["Points"] <= target_points:
                    return (mid - 1) * RANK_PAGE_SIZE + idx + 1, total
            return None, total
    return None, total


_name_cache = {}
_name_lock = threading.Lock()


_ROBLOX_HEADERS = {
    # Roblox's Cloudflare will silently 403 anything that looks like a bare
    # python script hitting the endpoint (no User-Agent etc). Found that out
    # the hard way — without this, resolve_usernames just quietly returns
    # nothing and everyone shows up as a raw UserID instead of a name.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def resolve_usernames(uids):
    """Turn Roblox UserIDs into actual display names, for every member of
    a clan — not just the ones in the top-25 sample."""
    uids = [u for u in {u for u in uids if u is not None}]
    result = {}
    uncached = []
    with _name_lock:
        for u in uids:
            if u in _name_cache:
                result[u] = _name_cache[u]
            else:
                uncached.append(u)
    for i in range(0, len(uncached), 100):
        chunk = uncached[i:i + 100]
        for attempt in range(2):  # give it one retry, a lot of the failures
                                   # are just a random 429/5xx blip
            try:
                r = requests.post("https://users.roblox.com/v1/users",
                                   json={"userIds": chunk, "excludeBannedUsers": False},
                                   headers=_ROBLOX_HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                for item in r.json().get("data", []):
                    name = item.get("displayName") or item.get("name") or str(item["id"])
                    result[item["id"]] = name
                    with _name_lock:
                        _name_cache[item["id"]] = name
                break
            except Exception:
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                # still failed after the retry, oh well — leave this chunk
                # unresolved, caller just shows raw UserIDs for them
    return result


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

_FLAG_OFFSET = 127397  # offset from ASCII letters to regional indicator symbols


def flag_emoji(code):
    if not code or len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(ord(c.upper()) + _FLAG_OFFSET) for c in code)


def fmt_num(n):
    if n is None:
        return "-"
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for suffix in ("", "K", "M", "B", "T", "Q"):
        if n < 1000:
            txt = f"{n:.1f}".rstrip("0").rstrip(".") if suffix else f"{int(n)}"
            return f"{sign}{txt}{suffix}"
        n /= 1000
    return f"{sign}{n:.1f}Qi"


def fmt_date(ts):
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return "-"


def asset_id_of(rbx_str):
    if not rbx_str:
        return None
    return rbx_str.replace("rbxassetid://", "").strip()


# ---------------------------------------------------------------------------
# icon cache — fetched in the background, tiny in-memory cache by asset+size
# ---------------------------------------------------------------------------

_icon_cache = {}
_icon_lock = threading.Lock()


def get_icon_async(asset_str, size, callback, root):
    """Grab an icon and resize it off the UI thread, then pass the
    PhotoImage back to `callback` through `root.after` since tkinter
    doesn't like being touched from a background thread."""
    if not PIL_OK or not asset_str:
        return
    aid = asset_id_of(asset_str)
    key = (aid, size)
    with _icon_lock:
        cached = _icon_cache.get(key)
    if cached is not None:
        root.after(0, callback, cached)
        return

    def worker():
        try:
            r = requests.get(IMG_PROXY.format(aid), timeout=TIMEOUT)
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            # keep the aspect ratio — shrink to fit in (size, size) and
            # center it on a transparent canvas instead of stretching it
            img.thumbnail((size, size), Image.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            offset = ((size - img.width) // 2, (size - img.height) // 2)
            canvas.paste(img, offset, img)
            photo = ImageTk.PhotoImage(canvas)
            with _icon_lock:
                _icon_cache[key] = photo
            root.after(0, callback, photo)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# theme
# ---------------------------------------------------------------------------

BG = "#2b2d31"
BG2 = "#232428"
ROW_A = "#2b2d31"
ROW_B = "#313338"
FG = "#dcddde"
FG_DIM = "#949ba4"
ACCENT = "#5865f2"
FONT = ("Segoe UI", 9)
FONT_B = ("Segoe UI", 9, "bold")
FONT_TITLE = ("Segoe UI", 10, "bold")
# Plain "Segoe UI" renders emoji as flat/monochrome or just empty boxes on a
# lot of systems. Segoe UI Emoji is the actual color-emoji font on Windows,
# so anything that's purely an emoji (no mixed-in text) uses this instead.
EMOJI_FONT = ("Segoe UI Emoji", 10)
EMOJI_FONT_SM = ("Segoe UI Emoji", 9)


class Overlay(tk.Tk):
    def __init__(self):
        super().__init__()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=BG)
        self.geometry("360x480+120+120")
        self.minsize(320, 300)

        self.mode = tk.StringVar(value="clan")
        self.interval = tk.IntVar(value=30)
        self.view_mode = tk.StringVar(value="live")  # "live" or "hourly"
        self.show_created = tk.BooleanVar(value=True)
        self.show_country = tk.BooleanVar(value=True)
        self.show_points = tk.BooleanVar(value=True)
        self.show_gems = tk.BooleanVar(value=True)
        self.show_joined = tk.BooleanVar(value=False)
        self.show_average = tk.BooleanVar(value=True)
        self.member_count = tk.IntVar(value=15)
        self.username_filter = tk.StringVar(value="")
        self.pinned = tk.BooleanVar(value=True)
        self.webhook_enabled = tk.BooleanVar(value=False)
        self.webhook_url = tk.StringVar(value="")
        self.stall_alert_enabled = tk.BooleanVar(value=False)
        self.stall_target_player = tk.StringVar(value="")   # blank = whole clan/league total
        self.stall_ping_id = tk.StringVar(value="")          # Discord user ID to @ping, optional

        self._entity = None          # last clan/league we fetched
        self._entity_kind = None
        self._member_sample = {}     # UserID -> stats from /v1/clans/players
        self._resolved_names = {}    # UserID -> real Roblox username (clans)
        self._rank = None            # current spot on the points leaderboard
        self._rank_total = None      # total number of clans/leagues that exist
        self._prev_points = {}       # UserID -> (timestamp, points). Only touched
                                      # once per real poll, never on a re-render
        self._last_rate_raw = {}     # UserID -> (delta_points, delta_seconds) since last poll
        self._gen = 0                # bumped on every search, so stale fetches know to bail
        self._settings_open = False
        self._webhook_tab_open = False
        self._credits_tab_open = False
        self._icon_photo = None
        self._seconds_left = 0       # counts down in the status bar between polls
        self._save_job = None        # debounce handle for autosaving settings
        self._spinner_job = None     # after() handle for the loading spinner
        self._spinner_gen = None     # which search started the spinner that's running
        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_idx = 0
        self._pending_last_search = None
        self._stall_last_points = None     # last-seen points value for the stall target
        self._stall_last_change_ts = None  # when that value last actually changed
        self._stall_alerting = False       # whether we're currently in "stalled" state
        self._stall_loop_started = False

        self._load_settings()

        self._build_titlebar()
        self._build_search_row()
        self._build_settings_panel()
        self._build_header()
        self._build_table()
        self._build_status_bar()

        self._apply_icon()
        self.update_idletasks()
        self._show_in_taskbar()

        self._drag = {"x": 0, "y": 0}

        # apply the loaded "pinned" value — window's already always-on-top
        # by default, this just syncs the pin icon's color to match
        self.attributes("-topmost", self.pinned.get())
        self.pin_lbl.configure(fg=ACCENT if self.pinned.get() else FG_DIM)

        # save to disk whenever any of these change, debounced a bit so
        # typing in the filter box doesn't write to disk on every keystroke
        for var in (self.mode, self.interval, self.view_mode, self.show_created,
                    self.show_country, self.show_points, self.show_gems,
                    self.show_joined, self.show_average, self.member_count,
                    self.username_filter, self.pinned, self.webhook_enabled,
                    self.webhook_url, self.stall_alert_enabled,
                    self.stall_target_player, self.stall_ping_id):
            var.trace_add("write", self._schedule_save)

        # switching who/what we're watching for a stall should restart the clock
        self.stall_target_player.trace_add("write", lambda *_: self._reset_stall_tracking())

        self._start_stall_loop()

        if self._pending_last_search:
            self.search_entry.insert(0, self._pending_last_search)
            self.after(200, self.search)

    # ---- settings persistence (%APPDATA%\PS99Overlay\settings.json) ------
    def _settings_snapshot(self):
        return {
            "mode": self.mode.get(),
            "interval": self.interval.get(),
            "view_mode": self.view_mode.get(),
            "show_created": self.show_created.get(),
            "show_country": self.show_country.get(),
            "show_points": self.show_points.get(),
            "show_gems": self.show_gems.get(),
            "show_joined": self.show_joined.get(),
            "show_average": self.show_average.get(),
            "member_count": self.member_count.get(),
            "username_filter": self.username_filter.get(),
            "pinned": self.pinned.get(),
            "webhook_enabled": self.webhook_enabled.get(),
            "webhook_url": self.webhook_url.get(),
            "stall_alert_enabled": self.stall_alert_enabled.get(),
            "stall_target_player": self.stall_target_player.get(),
            "stall_ping_id": self.stall_ping_id.get(),
            "last_search": self.search_entry.get() if hasattr(self, "search_entry") else "",
            "window_pos": self.geometry(),
        }

    def _save_settings(self):
        self._save_job = None
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._settings_snapshot(), f, indent=2)
        except Exception:
            pass  # eh, if saving fails just move on, not worth crashing over

    def _schedule_save(self, *_):
        # debounced so we're not writing to disk on literally every keystroke
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(600, self._save_settings)

    def _load_settings(self):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        try:
            self.mode.set(data.get("mode", self.mode.get()))
            self.interval.set(int(data.get("interval", self.interval.get())))
            self.view_mode.set(data.get("view_mode", self.view_mode.get()))
            self.show_created.set(bool(data.get("show_created", True)))
            self.show_country.set(bool(data.get("show_country", True)))
            self.show_points.set(bool(data.get("show_points", True)))
            self.show_gems.set(bool(data.get("show_gems", True)))
            self.show_joined.set(bool(data.get("show_joined", False)))
            self.show_average.set(bool(data.get("show_average", True)))
            self.member_count.set(int(data.get("member_count", 15)))
            self.username_filter.set(data.get("username_filter", ""))
            self.pinned.set(bool(data.get("pinned", True)))
            self.webhook_enabled.set(bool(data.get("webhook_enabled", False)))
            self.webhook_url.set(data.get("webhook_url", ""))
            self.stall_alert_enabled.set(bool(data.get("stall_alert_enabled", False)))
            self.stall_target_player.set(data.get("stall_target_player", ""))
            self.stall_ping_id.set(data.get("stall_ping_id", ""))
            geo = data.get("window_pos")
            if geo:
                self.geometry(geo)
            self._pending_last_search = data.get("last_search") or None
        except Exception:
            pass  # if the settings file is busted somehow, just start fresh

    def _on_close(self):
        self._save_settings()
        self.destroy()

    # ---- window chrome (titlebar, pin, close) -----------------------------
    def _build_titlebar(self):
        bar = tk.Frame(self, bg=BG2, height=28)
        bar.pack(fill="x")
        bar.bind("<Button-1>", self._start_drag)
        bar.bind("<B1-Motion>", self._do_drag)

        bolt = tk.Label(bar, text="⚡", bg=BG2, fg=FG, font=EMOJI_FONT_SM)
        bolt.pack(side="left", padx=(8, 2), pady=4)
        bolt.bind("<Button-1>", self._start_drag)
        bolt.bind("<B1-Motion>", self._do_drag)

        title = tk.Label(bar, text="PS99 Companion", bg=BG2, fg=FG, font=FONT_TITLE)
        title.pack(side="left", pady=4)
        title.bind("<Button-1>", self._start_drag)
        title.bind("<B1-Motion>", self._do_drag)

        close = tk.Label(bar, text="✕", bg=BG2, fg=FG_DIM, font=FONT, cursor="hand2")
        close.pack(side="right", padx=8)
        close.bind("<Button-1>", lambda e: self._on_close())

        self.pin_lbl = tk.Label(bar, text="📌", bg=BG2, fg=ACCENT, font=EMOJI_FONT_SM, cursor="hand2")
        self.pin_lbl.pack(side="right", padx=4)
        self.pin_lbl.bind("<Button-1>", self._toggle_pin)

        gear = tk.Label(bar, text="⚙", bg=BG2, fg=FG_DIM, font=EMOJI_FONT_SM, cursor="hand2")
        gear.pack(side="right", padx=4)
        gear.bind("<Button-1>", lambda e: self._toggle_settings())

    def _start_drag(self, e):
        self._drag = {"x": e.x, "y": e.y}

    def _do_drag(self, e):
        x = self.winfo_pointerx() - self._drag["x"]
        y = self.winfo_pointery() - self._drag["y"]
        self.geometry(f"+{x}+{y}")

    def _toggle_pin(self, e=None):
        self.pinned.set(not self.pinned.get())
        self.attributes("-topmost", self.pinned.get())
        self.pin_lbl.configure(fg=ACCENT if self.pinned.get() else FG_DIM)

    def _apply_icon(self):
        """Set the window icon when running from source. When frozen into
        an exe, PyInstaller already baked icon.ico in via --icon, so there's
        nothing to do here — Windows picks it up on its own."""
        if getattr(sys, "frozen", False):
            return
        candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(candidate):
            try:
                self.iconbitmap(candidate)
            except Exception:
                pass  # not fatal, just means no icon this run

    def _show_in_taskbar(self):
        """overrideredirect(True) makes the window a plain WS_POPUP under
        Windows, which Explorer never gives a taskbar button — that's why
        this app has basically been invisible outside alt-tab. This flips
        the window's extended style to WS_EX_APPWINDOW (what a normal
        top-level app uses) instead of WS_EX_TOOLWINDOW, then hides and
        re-shows the window once, since Windows only creates the taskbar
        button on that hidden->shown transition."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            self.withdraw()
            self.after(10, self.deiconify)
        except Exception:
            pass  # worst case it just stays taskbar-less like before

    # ---- search row ---------------------------------------------------
    def _style_combobox(self, box):
        # A readonly combobox still lets you click-drag a text selection in
        # the field, which paints it grey by default. Matching the select
        # colors to the normal ones basically makes that highlight invisible.
        box.configure(style="Overlay.TCombobox")
        box.bind("<<ComboboxSelected>>", lambda e: box.selection_clear())

    def _build_search_row(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Overlay.TCombobox", fieldbackground=BG2, background=BG2,
                         foreground=FG, arrowcolor=FG, bordercolor=BG2,
                         lightcolor=BG2, darkcolor=BG2, selectbackground=BG2,
                         selectforeground=FG)
        style.map("Overlay.TCombobox",
                  fieldbackground=[("readonly", BG2)],
                  selectbackground=[("readonly", BG2), ("!disabled", BG2)],
                  selectforeground=[("readonly", FG), ("!disabled", FG)])

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=8, pady=(8, 4))

        self.mode_box = ttk.Combobox(row, textvariable=self.mode, values=["clan", "league"],
                                      width=6, state="readonly")
        self._style_combobox(self.mode_box)
        self.mode_box.pack(side="left")

        self.search_entry = tk.Entry(row, bg=BG2, fg=FG, insertbackground=FG,
                                      relief="flat", font=FONT)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=6, ipady=3)
        self.search_entry.bind("<Return>", lambda e: self.search())

        go = tk.Label(row, text="🔍", bg=ACCENT, fg="white", font=EMOJI_FONT_SM, cursor="hand2", padx=8)
        go.pack(side="left", ipady=2)
        go.bind("<Button-1>", lambda e: self.search())

    # ---- settings -------------------------------------------------------
    def _build_settings_panel(self):
        self.settings = tk.Frame(self, bg=BG2)
        # not packed until toggled open

        # Everything below lives in self.main_settings so the whole block
        # can be hidden in one shot when the webhook tab is opened.
        self.main_settings = tk.Frame(self.settings, bg=BG2)
        self.main_settings.pack(fill="x")

        def chk(parent, text, var):
            c = tk.Checkbutton(parent, text=text, variable=var, bg=BG2, fg=FG,
                                selectcolor=BG2, activebackground=BG2, activeforeground=FG,
                                font=FONT, anchor="w", command=self.render)
            return c

        grid = tk.Frame(self.main_settings, bg=BG2)
        grid.pack(fill="x", padx=8, pady=6)

        chk(grid, "Created date", self.show_created).grid(row=0, column=0, sticky="w")
        chk(grid, "Country", self.show_country).grid(row=0, column=1, sticky="w")
        chk(grid, "Points", self.show_points).grid(row=1, column=0, sticky="w")
        chk(grid, "Gems", self.show_gems).grid(row=1, column=1, sticky="w")
        chk(grid, "Joined date", self.show_joined).grid(row=2, column=0, sticky="w")
        chk(grid, "Averages", self.show_average).grid(row=2, column=1, sticky="w")

        row2 = tk.Frame(self.main_settings, bg=BG2)
        row2.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(row2, text="Show top:", bg=BG2, fg=FG, font=FONT).pack(side="left")
        spin = tk.Spinbox(row2, from_=1, to=75, width=4, textvariable=self.member_count,
                           bg=BG, fg=FG, insertbackground=FG, relief="flat",
                           command=self.render)
        spin.pack(side="left", padx=4)

        tk.Label(row2, text="Refresh:", bg=BG2, fg=FG, font=FONT).pack(side="left", padx=(12, 0))
        refresh_box = ttk.Combobox(row2, textvariable=self.interval, values=[15, 30, 60],
                                    width=4, state="readonly")
        self._style_combobox(refresh_box)
        refresh_box.pack(side="left", padx=4)

        row3 = tk.Frame(self.main_settings, bg=BG2)
        row3.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(row3, text="View:", bg=BG2, fg=FG, font=FONT).pack(side="left")
        for label, val in (("Live", "live"), ("Hourly ≈", "hourly")):
            tk.Radiobutton(row3, text=label, value=val, variable=self.view_mode,
                            bg=BG2, fg=FG, selectcolor=BG2, activebackground=BG2,
                            activeforeground=FG, font=FONT, command=self.render).pack(side="left", padx=4)

        row4 = tk.Frame(self.main_settings, bg=BG2)
        row4.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(row4, text="Filter user:", bg=BG2, fg=FG, font=FONT).pack(side="left")
        f_entry = tk.Entry(row4, textvariable=self.username_filter, bg=BG, fg=FG,
                            insertbackground=FG, relief="flat", font=FONT, width=16)
        f_entry.pack(side="left", padx=4, ipady=2)
        f_entry.bind("<KeyRelease>", lambda e: self.render())

        row5 = tk.Frame(self.main_settings, bg=BG2)
        row5.pack(fill="x", padx=8, pady=(0, 8))
        webhook_btn = tk.Label(row5, text="🔔 Webhook alerts  ›", bg=BG2, fg=ACCENT,
                                font=FONT, cursor="hand2")
        webhook_btn.pack(side="left")
        webhook_btn.bind("<Button-1>", lambda e: self._show_webhook_tab())

        credits_btn = tk.Label(row5, text="✨ Credits  ›", bg=BG2, fg=ACCENT,
                                font=FONT, cursor="hand2")
        credits_btn.pack(side="left", padx=(14, 0))
        credits_btn.bind("<Button-1>", lambda e: self._show_credits_tab())

        self._build_webhook_panel()
        self._build_credits_panel()

    def _build_webhook_panel(self):
        """A second 'tab' inside the settings drawer: on/off toggle + a
        Discord webhook URL field. When enabled, every successful poll
        posts the currently tracked clan/league's placement and points
        to that webhook."""
        self.webhook_panel = tk.Frame(self.settings, bg=BG2)
        # not packed until the user opens it

        top = tk.Frame(self.webhook_panel, bg=BG2)
        top.pack(fill="x", padx=8, pady=(6, 4))
        back = tk.Label(top, text="‹ Back", bg=BG2, fg=ACCENT, font=FONT, cursor="hand2")
        back.pack(side="left")
        back.bind("<Button-1>", lambda e: self._hide_webhook_tab())
        tk.Label(top, text="Discord Webhook", bg=BG2, fg=FG, font=FONT_B).pack(side="left", padx=(10, 0))

        chk_row = tk.Frame(self.webhook_panel, bg=BG2)
        chk_row.pack(fill="x", padx=8, pady=(2, 4))
        tk.Checkbutton(chk_row, text="Send updates to webhook", variable=self.webhook_enabled,
                        bg=BG2, fg=FG, selectcolor=BG2, activebackground=BG2,
                        activeforeground=FG, font=FONT, anchor="w").pack(side="left")

        url_row = tk.Frame(self.webhook_panel, bg=BG2)
        url_row.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(url_row, text="URL:", bg=BG2, fg=FG, font=FONT).pack(side="left")
        url_entry = tk.Entry(url_row, textvariable=self.webhook_url, bg=BG, fg=FG,
                              insertbackground=FG, relief="flat", font=FONT, width=28)
        url_entry.pack(side="left", padx=4, ipady=2, fill="x", expand=True)

        btn_row = tk.Frame(self.webhook_panel, bg=BG2)
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        test_btn = tk.Label(btn_row, text="Send test message", bg=ACCENT, fg="white",
                             font=FONT, cursor="hand2", padx=6, pady=2)
        test_btn.pack(side="left")
        test_btn.bind("<Button-1>", lambda e: self._send_webhook_update(force=True))
        self.webhook_status = tk.Label(btn_row, text="", bg=BG2, fg=FG_DIM, font=FONT)
        self.webhook_status.pack(side="left", padx=8)

        sep = tk.Frame(self.webhook_panel, bg="#3a3c42", height=1)
        sep.pack(fill="x", padx=8, pady=(2, 6))

        tk.Label(self.webhook_panel, text="⚠ Stall Alert", bg=BG2, fg=FG, font=FONT_B,
                 anchor="w").pack(fill="x", padx=8)
        tk.Label(self.webhook_panel,
                 text="Pings the webhook every 30s if points stop moving for 5+ minutes.",
                 bg=BG2, fg=FG_DIM, font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=280).pack(fill="x", padx=8, pady=(0, 4))

        stall_chk_row = tk.Frame(self.webhook_panel, bg=BG2)
        stall_chk_row.pack(fill="x", padx=8, pady=(0, 4))
        tk.Checkbutton(stall_chk_row, text="Enable stall alert", variable=self.stall_alert_enabled,
                        bg=BG2, fg=FG, selectcolor=BG2, activebackground=BG2,
                        activeforeground=FG, font=FONT, anchor="w").pack(side="left")

        target_row = tk.Frame(self.webhook_panel, bg=BG2)
        target_row.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(target_row, text="Player (blank = whole clan/league):", bg=BG2, fg=FG,
                 font=FONT).pack(side="left")
        target_entry = tk.Entry(target_row, textvariable=self.stall_target_player, bg=BG, fg=FG,
                                 insertbackground=FG, relief="flat", font=FONT, width=14)
        target_entry.pack(side="left", padx=4, ipady=2)

        ping_row = tk.Frame(self.webhook_panel, bg=BG2)
        ping_row.pack(fill="x", padx=8, pady=(0, 10))
        tk.Label(ping_row, text="Discord User ID to ping:", bg=BG2, fg=FG, font=FONT).pack(side="left")
        ping_entry = tk.Entry(ping_row, textvariable=self.stall_ping_id, bg=BG, fg=FG,
                               insertbackground=FG, relief="flat", font=FONT, width=18)
        ping_entry.pack(side="left", padx=4, ipady=2)

    def _show_webhook_tab(self):
        self._webhook_tab_open = True
        self.main_settings.pack_forget()
        self.webhook_panel.pack(fill="x")

    def _hide_webhook_tab(self):
        self._webhook_tab_open = False
        self.webhook_panel.pack_forget()
        self.main_settings.pack(fill="x")

    def _build_credits_panel(self):
        """Third settings 'tab': who made this thing."""
        self.credits_panel = tk.Frame(self.settings, bg=BG2)
        # not packed until the user opens it

        top = tk.Frame(self.credits_panel, bg=BG2)
        top.pack(fill="x", padx=8, pady=(6, 4))
        back = tk.Label(top, text="‹ Back", bg=BG2, fg=ACCENT, font=FONT, cursor="hand2")
        back.pack(side="left")
        back.bind("<Button-1>", lambda e: self._hide_credits_tab())
        tk.Label(top, text="Credits", bg=BG2, fg=FG, font=FONT_B).pack(side="left", padx=(10, 0))

        body = tk.Frame(self.credits_panel, bg=BG2)
        body.pack(fill="x", padx=8, pady=(2, 10))

        def credit_row(role, name):
            row = tk.Frame(body, bg=BG2)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=role, bg=BG2, fg=FG_DIM, font=FONT, width=14, anchor="w").pack(side="left")
            tk.Label(row, text=name, bg=BG2, fg=FG, font=FONT_B, anchor="w").pack(side="left")

        credit_row("Head of Project", "Justinomvv")
        credit_row("Assistant Dev", "Claude")

        tk.Label(body, text="Roblox: @Justinomvv", bg=BG2, fg=FG_DIM, font=FONT, anchor="w").pack(fill="x", pady=(6, 0))
        tk.Label(body, text="Discord: @6ypb", bg=BG2, fg=FG_DIM, font=FONT, anchor="w").pack(fill="x")
        tk.Label(body, text="GitHub: @Justinomvv", bg=BG2, fg=FG_DIM, font=FONT, anchor="w").pack(fill="x")

    def _show_credits_tab(self):
        self._credits_tab_open = True
        self.main_settings.pack_forget()
        self.credits_panel.pack(fill="x")

    def _hide_credits_tab(self):
        self._credits_tab_open = False
        self.credits_panel.pack_forget()
        self.main_settings.pack(fill="x")

    def _toggle_settings(self):
        self._settings_open = not self._settings_open
        if self._settings_open:
            self.settings.pack(fill="x", after=self.children[list(self.children)[1]])
        else:
            self.settings.pack_forget()
            if self._webhook_tab_open:
                self._hide_webhook_tab()
            if self._credits_tab_open:
                self._hide_credits_tab()

    # ---- header (entity summary) ---------------------------------------
    def _build_header(self):
        self.header = tk.Frame(self, bg=BG, height=54)
        self.header.pack(fill="x", padx=8, pady=(2, 4))

        icon_wrap = tk.Frame(self.header, bg=BG, width=44, height=44)
        icon_wrap.pack(side="left")
        icon_wrap.pack_propagate(False)  # keep the box fixed size no matter what's inside
        self.icon_lbl = tk.Label(icon_wrap, text="🐾", bg=BG, fg=FG_DIM, font=EMOJI_FONT)
        self.icon_lbl.pack(expand=True, fill="both")

        info = tk.Frame(self.header, bg=BG)
        info.pack(side="left", fill="x", expand=True, padx=6)

        self.name_lbl = tk.Label(info, text="Search a clan or league", bg=BG, fg=FG, font=FONT_TITLE, anchor="w")
        self.name_lbl.pack(fill="x")

        self.sub_lbl = tk.Label(info, text="", bg=BG, fg=FG_DIM, font=FONT, anchor="w", justify="left")
        self.sub_lbl.pack(fill="x")

        self.rank_lbl = tk.Label(info, text="", bg=BG, fg=ACCENT, font=FONT_B, anchor="w")
        self.rank_lbl.pack(fill="x")

        self.avg_lbl = tk.Label(info, text="", bg=BG, fg=FG_DIM, font=FONT, anchor="w")
        self.avg_lbl.pack(fill="x")

    # ---- table -----------------------------------------------------------
    def _build_table(self):
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Overlay.Treeview", background=ROW_A, fieldbackground=ROW_A,
                         foreground=FG, rowheight=22, borderwidth=0, font=FONT)
        style.configure("Overlay.Treeview.Heading", background=BG2, foreground=FG_DIM,
                         font=FONT_B, borderwidth=0)
        style.map("Overlay.Treeview", background=[("selected", ACCENT)])

        cols = ("rank", "name", "points", "rate", "gems", "joined")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", style="Overlay.Treeview")
        widths = {"rank": 28, "name": 108, "points": 62, "rate": 62, "gems": 60, "joined": 74}
        heads = {"rank": "#", "name": "Name", "points": "Points", "rate": "Δ",
                 "gems": "💎", "joined": "Joined"}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            anchor = "w" if c == "name" else "center"
            self.tree.column(c, width=widths[c], anchor=anchor, stretch=(c == "name"))
        self.tree.tag_configure("odd", background=ROW_B)
        self.tree.tag_configure("even", background=ROW_A)

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _build_status_bar(self):
        bar = tk.Frame(self, bg=BG2)
        bar.pack(fill="x", side="bottom")
        self.spinner_lbl = tk.Label(bar, text="", bg=BG2, fg=ACCENT, font=EMOJI_FONT_SM, width=2)
        self.spinner_lbl.pack(side="left", padx=(6, 0))
        self.status = tk.Label(bar, text="Ready", bg=BG2, fg=FG_DIM, font=("Segoe UI", 8), anchor="w")
        self.status.pack(side="left", fill="x", expand=True, pady=2)

    def _start_spinner(self):
        if self._spinner_job:
            self.after_cancel(self._spinner_job)
            self._spinner_job = None
        self._spinner_gen = self._gen
        self._spinner_idx = 0
        self._spin_tick()

    def _spin_tick(self):
        # if a newer search kicked off since this loop started, this one's
        # an orphan — its _on_fetched already bailed on the gen check and
        # will never call _stop_spinner, so kill it here instead
        if self._spinner_gen != self._gen:
            self._spinner_job = None
            self.spinner_lbl.configure(text="")
            return
        self.spinner_lbl.configure(text=self._spinner_frames[self._spinner_idx % len(self._spinner_frames)])
        self._spinner_idx += 1
        self._spinner_job = self.after(80, self._spin_tick)

    def _stop_spinner(self):
        if self._spinner_job:
            self.after_cancel(self._spinner_job)
            self._spinner_job = None
        self.spinner_lbl.configure(text="")
        self.spinner_lbl.configure(text="")

    # ---- search / refresh logic ------------------------------------------
    def search(self):
        name = self.search_entry.get().strip()
        if not name:
            return
        self._prev_points = {}
        self._last_rate_raw = {}
        self._reset_stall_tracking()
        self._fetch(name, self.mode.get())

    def _fetch(self, name, kind):
        self._gen += 1
        gen = self._gen
        self.status.configure(text=f"Loading {kind} '{name}'…")
        self._start_spinner()

        def worker():
            try:
                if kind == "clan":
                    entity = fetch_clan(name)
                    sample = fetch_clan_player_sample()
                    uids = [m.get("UserID") for m in entity.get("Members", [])]
                    names = resolve_usernames(uids)
                else:
                    entity = fetch_league(name)
                    sample = {}
                    names = {}
                rank, total = None, None
                try:
                    rank, total = find_leaderboard_rank(kind, entity.get("Name", name),
                                                          entity.get("Points"))
                except Exception:
                    pass  # rank is a nice-to-have; never block the roster on it
                self.after(0, self._on_fetched, gen, kind, name, entity, sample, names, rank, total, None)
            except Exception as exc:
                self.after(0, self._on_fetched, gen, kind, name, None, {}, {}, None, None, exc)

        threading.Thread(target=worker, daemon=True).start()

    def _on_fetched(self, gen, kind, name, entity, sample, names, rank, total, err):
        if gen != self._gen:
            return  # a newer search superseded this one
        self._stop_spinner()
        if err or entity is None:
            self.status.configure(text=f"Not found: '{name}'")
            return
        self._entity = entity
        self._entity_kind = kind
        self._member_sample = sample
        self._resolved_names = names
        self._rank = rank
        self._rank_total = total
        self._advance_rate_history()
        self._update_stall_tracking()
        self._status_base = f"Updated {datetime.now().strftime('%H:%M:%S')}"
        self._seconds_left = self.interval.get()
        self._render_status()
        self.render()
        self._send_webhook_update()
        self.after(1000, self._tick_countdown, gen)
        self.after(self.interval.get() * 1000, self._auto_refresh, gen, kind, name)

    def _render_status(self):
        self.status.configure(text=f"{self._status_base}  ·  next refresh in {self._seconds_left}s")

    def _tick_countdown(self, gen):
        if gen != self._gen:
            return  # a newer search superseded this one; stop ticking
        self._seconds_left = max(0, self._seconds_left - 1)
        self._render_status()
        if self._seconds_left > 0:
            self.after(1000, self._tick_countdown, gen)

    def _auto_refresh(self, gen, kind, name):
        if gen != self._gen:
            return  # user searched something else meanwhile
        self._fetch(name, kind)

    # ---- rendering ---------------------------------------------------------
    def render(self):
        e = self._entity
        if e is None:
            return
        kind = self._entity_kind

        icon = e.get("Icon")
        country = e.get("CountryCode", "")
        created = e.get("Created")
        points = e.get("Points")

        if kind == "clan":
            self.name_lbl.configure(text=e.get("Name", "?"))
            subparts = []
            if self.show_created.get():
                subparts.append(f"since {fmt_date(created)}")
            if self.show_country.get() and country:
                subparts.append(flag_emoji(country) + " " + country)
            subparts.append(f"{len(e.get('Members', []))}/{e.get('MemberCapacity', '?')} members")
        else:
            self.name_lbl.configure(text=e.get("Name", "?"))
            subparts = []
            if self.show_created.get():
                subparts.append(f"since {fmt_date(created)}")
            subparts.append(f"Lv.{e.get('Level', '?')}")
            if self.show_points.get():
                subparts.append(f"{fmt_num(points)} pts total")

        self.sub_lbl.configure(text="   ·   ".join(subparts))

        if self._rank:
            noun = "clans" if kind == "clan" else "leagues"
            self.rank_lbl.configure(
                text=f"🏆 #{self._rank:,} of {self._rank_total:,} {noun} by points")
        elif self._rank_total:
            self.rank_lbl.configure(text=f"🏆 outside the checked top ranks (of {self._rank_total:,})")
        else:
            self.rank_lbl.configure(text="")

        if icon:
            get_icon_async(icon, 36, self._set_icon, self)

        self._populate_rows(e, kind)

    def _set_icon(self, photo):
        self._icon_photo = photo  # keep a reference alive
        self.icon_lbl.configure(image=photo, text="")

    def _populate_rows(self, e, kind):
        self.tree.delete(*self.tree.get_children())

        cols = ["rank", "name"]
        if self.show_points.get():
            cols.append("points")
        cols.append("rate")
        if kind == "clan" and self.show_gems.get():
            cols.append("gems")
        if self.show_joined.get():
            cols.append("joined")
        self.tree["displaycolumns"] = cols
        self.tree.heading("rate", text="Δ" if self.view_mode.get() == "live" else "≈/hr")

        all_rows = self._build_row_data(e, kind)
        rows = all_rows

        needle = self.username_filter.get().strip().lower()
        if needle:
            rows = [r for r in rows if needle in r["name"].lower()]
        rows = rows[: self.member_count.get()]

        if self.show_average.get() and all_rows:
            # Averages are computed over the WHOLE tracked roster, not just
            # whatever happens to be visible — otherwise "avg" would swing
            # around any time you touched the username filter or the "show
            # top" count, or read as the average of a single filtered-down
            # row, which isn't really an average of anything.
            pts_vals = [r["points"] for r in all_rows if r["points"] is not None]
            gem_vals = [r["gems"] for r in all_rows if r["gems"] is not None]
            parts = []
            if pts_vals:
                parts.append(f"avg {fmt_num(sum(pts_vals) / len(pts_vals))} pts/member")
            if gem_vals:
                parts.append(f"avg {fmt_num(sum(gem_vals) / len(gem_vals))} 💎/member")

            rate_vals = []
            hourly = self.view_mode.get() == "hourly"
            for r in all_rows:
                raw = self._last_rate_raw.get(r["uid"])
                if raw is None:
                    continue
                delta, dt = raw
                rate_vals.append(delta * 3600 / dt if hourly else delta)
            if rate_vals:
                avg_rate = sum(rate_vals) / len(rate_vals)
                sign = "+" if avg_rate > 0 else ""
                unit = "≈/hr" if hourly else "Δ"
                parts.append(f"avg {sign}{fmt_num(avg_rate)} {unit}/member")

            self.avg_lbl.configure(text="   ·   ".join(parts) if parts else "")
        else:
            self.avg_lbl.configure(text="")

        for i, r in enumerate(rows, start=1):
            uid = r["uid"]
            rate_txt = "-"
            raw = self._last_rate_raw.get(uid)
            if raw is not None:
                delta, dt = raw
                if self.view_mode.get() == "hourly":
                    rate_txt = fmt_num(delta * 3600 / dt)
                else:
                    rate_txt = fmt_num(delta) if delta else "0"
            tag = "odd" if i % 2 else "even"
            self.tree.insert("", "end", values=(
                i, r["name"], fmt_num(r["points"]), rate_txt,
                fmt_num(r["gems"]), fmt_date(r["joined"]),
            ), tags=(tag,))

    def _advance_rate_history(self):
        """Update everyone's points history — but only once per real API
        poll. This used to live inside _populate_rows, which also runs on
        every checkbox/filter/view-mode toggle, so any random UI click was
        resetting the 'previous points' baseline and the Hourly rate (and
        even the live Δ) came out basically meaningless. Now the raw
        (delta, seconds) pair gets stored once here, and _populate_rows
        just formats it however the current view wants it shown."""
        if self._entity is None:
            return
        rows = self._build_row_data(self._entity, self._entity_kind)
        now = time.time()
        new_prev = {}
        raw = {}
        for r in rows:
            uid, pts = r["uid"], r["points"]
            if pts is None:
                continue
            prev = self._prev_points.get(uid)
            new_prev[uid] = (now, pts)
            if prev:
                p_ts, p_pts = prev
                dt = max(now - p_ts, 1)
                raw[uid] = (pts - p_pts, dt)
        self._prev_points = new_prev
        self._last_rate_raw = raw

    # ---- discord webhook ---------------------------------------------------
    def _post_webhook(self, content, status_ok="Sent ✓", status_fail="Failed to send"):
        """Fire-and-forget POST to the configured webhook URL. Shared by the
        regular update message and the stall alert so there's only one place
        that actually talks to Discord."""
        url = self.webhook_url.get().strip()
        if not url:
            return

        def worker():
            try:
                requests.post(url, json={"content": content}, timeout=TIMEOUT)
                ok, msg = True, status_ok
            except Exception:
                ok, msg = False, status_fail
            if hasattr(self, "webhook_status"):
                self.after(0, lambda: self.webhook_status.configure(
                    text=msg, fg=("#3ba55d" if ok else "#ed4245")))

        threading.Thread(target=worker, daemon=True).start()

    def _send_webhook_update(self, force=False):
        """Post whatever clan/league we're tracking — rank + points — to
        the Discord webhook URL, if one's set. `force=True` is for the test
        button — sends even if the toggle's off, so you can check the URL
        works before actually turning live updates on."""
        if not self.webhook_url.get().strip() or self._entity is None:
            return
        if not force and not self.webhook_enabled.get():
            return

        e, kind = self._entity, self._entity_kind
        name = e.get("Name", "?")
        points = e.get("Points")
        lines = [f"**{name}** ({kind})"]
        if self._rank:
            noun = "clans" if kind == "clan" else "leagues"
            lines.append(f"🏆 Rank #{self._rank:,} of {self._rank_total:,} {noun}")
        elif self._rank_total:
            lines.append(f"Outside the checked top ranks (of {self._rank_total:,})")
        if points is not None:
            lines.append(f"Points: {fmt_num(points)}")
        self._post_webhook("\n".join(lines))

    # ---- stall alert ---------------------------------------------------
    def _reset_stall_tracking(self):
        """Clear the stall clock — called on a new search, or when the
        watched target (whole clan/league vs a specific player) changes,
        so a fresh baseline gets picked up on the next poll instead of
        immediately looking "stalled" against stale data."""
        self._stall_last_points = None
        self._stall_last_change_ts = None
        self._stall_alerting = False

    def _get_stall_target_points(self):
        """Current points for whatever the stall alert is watching — the
        clan/league's total, or one specific member if a name was given."""
        target = self.stall_target_player.get().strip()
        if not target:
            return self._entity.get("Points")
        for r in self._build_row_data(self._entity, self._entity_kind):
            if r["name"].lower() == target.lower():
                return r["points"]
        return None  # named player isn't in the currently loaded roster

    def _update_stall_tracking(self):
        """Called once per real poll. Just remembers the target's points
        and when they last actually changed — the every-30s alert loop
        below reads this independently of the refresh interval."""
        if self._entity is None:
            return
        pts = self._get_stall_target_points()
        if pts is None:
            return
        now = time.time()
        if self._stall_last_change_ts is None:
            self._stall_last_points, self._stall_last_change_ts = pts, now
            return
        if pts != self._stall_last_points:
            was_alerting = self._stall_alerting
            self._stall_last_points, self._stall_last_change_ts = pts, now
            if was_alerting:
                self._stall_alerting = False
                self._send_stall_alert(recovered=True)

    def _start_stall_loop(self):
        if self._stall_loop_started:
            return
        self._stall_loop_started = True
        self._stall_tick()

    def _stall_tick(self):
        # Runs on its own 30s clock, independent of the refresh interval —
        # that's what makes "alert every 30s" actually mean every 30s even
        # if the person's refresh rate is set to 60s.
        if (self.stall_alert_enabled.get() and self._entity is not None
                and self._stall_last_change_ts is not None):
            stalled_for = time.time() - self._stall_last_change_ts
            if stalled_for >= 300:  # 5 minutes
                self._stall_alerting = True
                self._send_stall_alert(recovered=False)
        self.after(30000, self._stall_tick)

    def _send_stall_alert(self, recovered):
        if not self.webhook_url.get().strip() or self._entity is None:
            return
        ping = self.stall_ping_id.get().strip()
        mention = f"<@{ping}> " if ping else ""
        target = self.stall_target_player.get().strip()
        label = target if target else f"{self._entity.get('Name', '?')} (total)"
        if recovered:
            content = f"{mention}✅ **{label}** is gaining points again."
        else:
            mins = int((time.time() - self._stall_last_change_ts) / 60)
            content = f"{mention}⚠️ **{label}** hasn't gained any points in {mins}+ minutes."
        self._post_webhook(content)

    def _build_row_data(self, e, kind):
        rows = []
        if kind == "clan":
            for m in e.get("Members", []):
                uid = m.get("UserID")
                sample = self._member_sample.get(uid)
                # Prefer the sampled battle stats' name (matches that data),
                # otherwise fall back to a real Roblox username we resolved
                # ourselves so smaller clans don't just show numeric IDs.
                name = str(sample["DisplayName"]) if sample else self._resolved_names.get(uid, str(uid))
                rows.append({
                    "uid": uid,
                    "name": name,
                    "points": sample["ActiveBattlePoints"] if sample else None,
                    "gems": sample["AllTimeDiamonds"] if sample else None,
                    "joined": m.get("JoinTime"),
                })
            rows.sort(key=lambda r: (r["points"] is None, -(r["points"] or 0)))
        else:
            contrib = {c["UserID"]: c for c in e.get("PointContributions", [])}
            for m in e.get("Members", []):
                uid = m.get("UserID")
                c = contrib.get(uid)
                rows.append({
                    "uid": uid,
                    "name": m.get("DisplayName", str(uid)),
                    "points": c["Points"] if c else 0,
                    "gems": None,  # leagues have no diamond stat
                    "joined": m.get("JoinTime"),
                })
            owner = e.get("Owner") or {}
            if owner.get("UserID"):
                c = contrib.get(owner["UserID"])
                rows.append({
                    "uid": owner["UserID"],
                    "name": owner.get("DisplayName", str(owner["UserID"])) + " (owner)",
                    "points": c["Points"] if c else 0,
                    "gems": None,
                    "joined": e.get("Created"),
                })
            rows.sort(key=lambda r: -(r["points"] or 0))
        return rows


if __name__ == "__main__":
    app = Overlay()
    app.mainloop()
