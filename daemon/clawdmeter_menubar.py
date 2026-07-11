#!/usr/bin/env python3
"""Clawdmeter menu bar app — a visible front-end for the BLE usage daemon.

Design: this rumps app owns the macOS main thread and does NOTHING with
Bluetooth itself. It spawns the proven ``claude_usage_daemon.py`` as a child
process and reads its stdout, parsing the status/usage lines to drive the menu
bar. Running CoreBluetooth inside the same process as rumps suppresses the
status-bar item (CoreBluetooth on a secondary thread races NSApplication's
launch), so we keep BLE fully isolated in the child.

Environment (set by the .app launcher stub):
  CLAWDMETER_ICON  path to the menu-bar icon PNG (optional)
  CLAWDMETER_APP   absolute path to Clawdmeter.app (for the Launch-at-Login item)
"""

import collections
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps

HERE = Path(__file__).resolve().parent
DAEMON_PY = HERE / "claude_usage_daemon.py"
# Burn-rate ETA tuning.
BURN_WINDOW = 25 * 60      # seconds of session-% history used for the slope
BURN_MIN_SPAN = 5 * 60     # need this much history before projecting
BURN_IDLE_HR = 1.0         # below this %/hr we call usage "steady, not climbing"

LOG_OUT = Path.home() / "Library" / "Logs" / "clawdmeter-app.log"
LOCK_FILE = Path.home() / "Library" / "Application Support" / "Clawdmeter" / "app.lock"
LOGIN_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.user.clawdmeter.plist"
SETTINGS_FILE = (
    Path.home() / "Library" / "Application Support" / "Clawdmeter" / "settings.json"
)

# What the menu bar title can show. (key, menu label); order = menu order.
TITLE_MODES = [
    ("session", "Session usage %"),
    ("weekly", "Weekly usage %"),
    ("session_reset", "Session reset countdown"),
    ("burn", "Burn rate (%/hr)"),
    ("eta", "Time to session limit"),
    ("icon", "Icon only"),
]

_MSG_RE = re.compile(r"^\[\d\d:\d\d:\d\d\]\s+(.*)$")
_DISCONNECT_MARKERS = (
    "Device disconnected",
    "Connection failed",
    "Stopping",
    "Device not held",
    "Device not found",
    "Daemon stopping",
)

_LOGIN_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.user.clawdmeter</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-a</string>
        <string>{app}</string>
    </array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>
"""


def _fmt_reset(mins) -> str:
    """Minutes-until-reset -> compact human string (e.g. '4h 22m', '6d 3h')."""
    try:
        m = int(mins)
    except (TypeError, ValueError):
        return "—"
    if m <= 0:
        return "now"
    d, rem = divmod(m, 1440)
    h, mm = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {mm}m"
    return f"{mm}m"


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _ble_tier(rate_per_min: float) -> tuple[str, bool]:
    """Map a measured send rate (bytes/min) to a lay-friendly impact tier.

    Returns (label, is_warning). Calibrated for Clawdmeter, which normally
    sends ~70 B/min — so anything past "Low" means it's behaving abnormally
    (e.g. a reconnect/write loop) and is worth flagging, long before it could
    actually affect other Bluetooth devices.
    """
    if rate_per_min < 2_048:            # < 2 KB/min  (normal is ~70 B/min)
        return ("No noticeable impact", False)
    if rate_per_min < 51_200:           # < 50 KB/min
        return ("Low impact", False)
    if rate_per_min < 512_000:          # < 500 KB/min
        return ("Higher than normal", True)
    return ("Unusually high — possible issue", True)


def _fmt_compact(mins) -> str:
    """Space-free reset time for the tight menu bar title (e.g. '1h23m')."""
    return _fmt_reset(mins).replace(" ", "")


def _acct_label(payload: dict) -> str:
    return {"pro": "Pro / Max", "ent": "Enterprise"}.get(
        payload.get("acct"), str(payload.get("acct", "—"))
    )


def _single_instance_or_exit():
    """Hold an exclusive lock so a second launch can't spawn a second daemon.

    Two BLE centrals connected to one peripheral corrupt each other's writes.
    Returns the held file object (keep a reference for the process lifetime);
    exits if another instance already holds the lock.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another Clawdmeter instance is already running; exiting.", flush=True)
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


class ClawdmeterApp(rumps.App):
    def __init__(self) -> None:
        icon = os.environ.get("CLAWDMETER_ICON")
        icon = icon if icon and os.path.exists(icon) else None
        super().__init__(
            "Clawdmeter",
            title=" …",
            icon=icon,
            template=False,  # keep the Clawd mascot in color
            quit_button=None,
        )
        self._app_path = os.environ.get("CLAWDMETER_APP", "")

        # Persisted settings (menu bar title mode + whether onboarding is done).
        _settings = self._load_settings()
        self._title_mode = _settings.get("title_mode", "session")
        self._onboarded = bool(_settings.get("onboarded", False))
        self._ob = None          # onboarding controller (lazily created)
        self._ob_shown = False   # first-run decision made this launch?
        self._start_ts = time.time()
        self.item_mode = rumps.MenuItem("Menu Bar Shows")
        self._mode_items: dict[str, rumps.MenuItem] = {}
        for key, label in TITLE_MODES:
            mi = rumps.MenuItem(label, callback=lambda s, k=key: self._set_title_mode(k))
            mi.state = 1 if key == self._title_mode else 0
            self._mode_items[key] = mi
            self.item_mode.add(mi)

        # The status line carries a no-op callback so macOS renders it in normal
        # (enabled) text — it needs to read clearly as "running", not disabled.
        # The data rows below are read-only info with nothing to click, so leave
        # them without a callback (macOS greys them, which is fine/conventional).
        self.item_conn = rumps.MenuItem("Starting…", callback=self._noop)
        self.item_session = rumps.MenuItem("Session usage: —")
        self.item_session_reset = rumps.MenuItem("Session resets: —")
        self.item_burn = rumps.MenuItem("Burn rate: —")
        self.item_weekly = rumps.MenuItem("Weekly usage: —")
        self.item_weekly_reset = rumps.MenuItem("Weekly resets: —")
        self.item_account = rumps.MenuItem("Account: —")
        self.item_updated = rumps.MenuItem("Last update: —")
        self.item_ble = rumps.MenuItem("Bluetooth use: —")
        self.item_login = rumps.MenuItem("Launch at Login", callback=self.toggle_login)
        self.menu = [
            self.item_conn,
            None,
            self.item_session,
            self.item_session_reset,
            self.item_burn,
            self.item_weekly,
            self.item_weekly_reset,
            self.item_account,
            self.item_updated,
            self.item_ble,
            None,
            self.item_mode,
            rumps.MenuItem("Set Up / Connection…", callback=self.open_onboarding),
            rumps.MenuItem("Open Log", callback=self.open_log),
            self.item_login,
            None,
            rumps.MenuItem("Quit Clawdmeter", callback=self.quit_app),
        ]

        # Shared state: written by the reader thread, read by the UI timer.
        # Attribute assignment is atomic enough under the GIL for this use.
        self._connected = False
        self._payload: dict | None = None
        self._last_update: float | None = None
        self._auth_error = False  # daemon got a 401 (token expired/invalid)
        self._last_refresh = 0.0  # last auto token-refresh attempt (throttle)
        self._proc: subprocess.Popen | None = None
        self._started = False
        # BLE traffic accounting (bytes actually written to the board per poll).
        self._bytes_last = 0
        self._bytes_total = 0
        self._poll_count = 0
        self._first_write: float | None = None
        self._writes: "collections.deque[tuple[float, int]]" = collections.deque()
        # Session-% samples (wall_time, pct) for the burn-rate projection.
        self._usage: "collections.deque[tuple[float, float]]" = collections.deque()

        # rumps.Timer fires on the main thread — the only safe place to touch UI.
        self._timer = rumps.Timer(self._tick, 2)
        self._timer.start()

    # ---- child daemon + stdout reader (background thread) ----------------
    def _start_daemon(self) -> None:
        LOG_OUT.parent.mkdir(parents=True, exist_ok=True)
        # CoreBluetooth aborts any process lacking NSBluetoothAlwaysUsageDescription.
        # Launch the child through the app bundle's own executable (--daemon) so it
        # inherits the bundle's usage string and Bluetooth grant. Fall back to a
        # bare interpreter only for dev runs outside the bundle (BLE won't work
        # there, but the UI will).
        exec_path = os.environ.get("CLAWDMETER_EXEC")
        if exec_path and os.path.exists(exec_path):
            cmd = [exec_path, "--daemon"]
        else:
            cmd = [sys.executable, str(DAEMON_PY)]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
            env=dict(os.environ),
        )
        threading.Thread(target=self._read_output, name="clawd-reader", daemon=True).start()

    def _read_output(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            with open(LOG_OUT, "a") as logf:
                for line in self._proc.stdout:
                    logf.write(line)
                    logf.flush()
                    self._handle_line(line.rstrip("\n"))
        except Exception:
            pass

    def _handle_line(self, line: str) -> None:
        m = _MSG_RE.match(line)
        msg = m.group(1) if m else line
        if msg.startswith("Sending: "):
            raw = msg[len("Sending: "):]
            try:
                self._payload = json.loads(raw)
                self._last_update = time.time()
                self._connected = True
                self._auth_error = False  # a successful poll clears the flag
                # Exact bytes written to the GATT characteristic this poll.
                self._bytes_last = len(raw.encode())
                self._bytes_total += self._bytes_last
                self._poll_count += 1
                now = time.time()
                if self._first_write is None:
                    self._first_write = now
                self._writes.append((now, self._bytes_last))
                cutoff = now - 300  # keep a rolling 5-minute window
                while self._writes and self._writes[0][0] < cutoff:
                    self._writes.popleft()
                # Session-% sample for burn rate. A drop means the 5h window
                # just reset — start a fresh trend so we don't project on it.
                s_val = self._payload.get("s")
                if isinstance(s_val, (int, float)):
                    if self._usage and s_val < self._usage[-1][1] - 2:
                        self._usage.clear()
                    self._usage.append((now, float(s_val)))
                    ucut = now - BURN_WINDOW
                    while self._usage and self._usage[0][0] < ucut:
                        self._usage.popleft()
            except json.JSONDecodeError:
                pass
        elif msg == "Connected":
            self._connected = True
        elif msg.startswith("API HTTP 401") or msg.startswith("API HTTP 403"):
            self._auth_error = True
        elif any(msg.startswith(x) for x in _DISCONNECT_MARKERS):
            self._connected = False

    # ---- main thread UI --------------------------------------------------
    def _noop(self, _) -> None:
        """No-op so info rows render as enabled (non-gray) text."""

    def _ble_rate_per_min(self) -> float | None:
        """Measured send rate (bytes/min) over the rolling window.

        None until there's ~a minute of history — too early to characterize.
        """
        if self._first_write is None or not self._writes:
            return None
        now = time.time()
        elapsed = now - self._first_write
        if elapsed < 60:
            return None
        window_secs = min(elapsed, 300.0)
        window_bytes = sum(b for _, b in self._writes)
        return window_bytes / (window_secs / 60.0)

    def _burn_info(self, payload: dict) -> tuple[str, float, int | None]:
        """(status, per_hr, eta_min) for the session burn rate.

        status ∈ measuring | steady | limit | reset_first | climbing. Slope is a
        least-squares fit over the recent window (robust to bursty polling)."""
        cur = payload.get("s", 0) or 0
        if cur >= 100:
            return ("limit", 0.0, 0)
        pts = list(self._usage)
        if len(pts) < 2 or (pts[-1][0] - pts[0][0]) < BURN_MIN_SPAN:
            return ("measuring", 0.0, None)
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        ms = sum(s for _, s in pts) / n
        den = sum((t - mt) ** 2 for t, _ in pts)
        if den == 0:
            return ("measuring", 0.0, None)
        per_min = (sum((t - mt) * (s - ms) for t, s in pts) / den) * 60.0  # %/min
        per_hr = per_min * 60.0
        if per_hr < BURN_IDLE_HR:
            return ("steady", per_hr, None)
        eta_min = int(round((100.0 - cur) / per_min))
        sr = payload.get("sr")
        if isinstance(sr, (int, float)) and sr > 0 and eta_min > sr:
            return ("reset_first", per_hr, eta_min)
        return ("climbing", per_hr, eta_min)

    def _burn_text(self, payload: dict) -> str:
        """The 'Burn rate' dropdown row. Key insight: if you'll reset before
        hitting 100%, that's good news, not a countdown."""
        status, per_hr, eta_min = self._burn_info(payload)
        if status == "limit":
            return "Burn rate:  at session limit"
        if status == "measuring":
            return "Burn rate:  measuring…"
        if status == "steady":
            return "Burn rate:  steady — not climbing"
        if status == "reset_first":
            return f"Burn rate:  ~{per_hr:.0f}%/hr · resets before limit ✓"
        warn = "⚠️ " if eta_min is not None and eta_min <= 30 else ""
        return f"Burn rate:  {warn}~{per_hr:.0f}%/hr · limit in ~{_fmt_reset(eta_min)}"

    def _title_for(self, p: dict) -> str:
        """The menu bar title text for the user's chosen mode (compact)."""
        mode = self._title_mode
        if mode == "icon":
            return ""
        if mode == "weekly":
            return f" {p.get('w', 0)}% w"
        if mode == "session_reset":
            return " " + _fmt_compact(p.get("sr"))
        if mode in ("burn", "eta"):
            status, per_hr, eta_min = self._burn_info(p)
            if status == "measuring":
                return " …"
            if status == "limit":
                return " max"
            if mode == "burn":
                return " 0%/h" if status == "steady" else f" {per_hr:.0f}%/h"
            if status in ("steady", "reset_first"):
                return " safe"
            return " " + _fmt_compact(eta_min)
        return f" {p.get('s', 0)}%"  # default: session usage %

    def _tick(self, _timer) -> None:
        # Spawn the daemon on the first tick — the status-bar item is guaranteed
        # to exist by now (rumps created it during launch, before any timer).
        if not self._started:
            self._started = True
            self._start_daemon()

        # First-run onboarding: after giving the daemon a few seconds to connect,
        # show the setup window — unless everything already works (don't nag
        # people who are already set up; just mark them onboarded).
        if not self._onboarded and not self._ob_shown and time.time() - self._start_ts > 8:
            self._ob_shown = True
            if self._connected and self._payload is not None:
                self._mark_onboarded()
            else:
                self.open_onboarding(None)

        p = self._payload
        if self._connected and p:
            s, w = p.get("s", 0), p.get("w", 0)
            self.title = self._title_for(p)
            self.item_conn.title = "🟢 Connected to Clawdmeter"
            self.item_session.title = f"Session usage:  {s}%"
            self.item_session_reset.title = f"Session resets in:  {_fmt_reset(p.get('sr'))}"
            self.item_burn.title = self._burn_text(p)
            self.item_weekly.title = f"Weekly usage:  {w}%"
            self.item_weekly_reset.title = f"Weekly resets in:  {_fmt_reset(p.get('wr'))}"
            self.item_account.title = f"Account:  {_acct_label(p)}  ({p.get('st', '—')})"
            when = time.strftime("%-I:%M:%S %p", time.localtime(self._last_update))
            self.item_updated.title = f"Last update:  {when}"
            # Plain-language impact tier + the raw rate for the curious. Normal
            # Clawdmeter (~70 B/min) reads "No noticeable impact"; a ⚠️ appears
            # only if it ever climbs into abnormal territory.
            rate = self._ble_rate_per_min()
            if rate is None:
                self.item_ble.title = (
                    f"Bluetooth:  measuring… · {_human_bytes(self._bytes_total)} so far"
                )
            else:
                label, warn = _ble_tier(rate)
                prefix = "⚠️ " if warn else ""
                self.item_ble.title = (
                    f"Bluetooth:  {prefix}{label} · ~{_human_bytes(int(rate))}/min "
                    f"({_human_bytes(self._bytes_total)} total)"
                )
        elif self._connected and self._auth_error:
            self._attempt_token_refresh()
            refreshing = time.time() - self._last_refresh < 70
            self.title = " ⚠"
            self.item_conn.title = ("🟡 Refreshing Claude sign-in…" if refreshing
                                    else "🔴 Claude sign-in expired — open Set Up")
            self.item_burn.title = "Burn rate: —"
            self.item_ble.title = "Bluetooth use: —"
        elif self._connected:
            self.title = " …"
            self.item_conn.title = "🟡 Connected — waiting for data…"
            self.item_burn.title = "Burn rate: —"
            self.item_ble.title = "Bluetooth use: —"
        else:
            self.title = " ⚠"
            self.item_conn.title = "🔴 Searching for Clawdmeter…"
            self.item_burn.title = "Burn rate: —"
            self.item_ble.title = "Bluetooth use: —"
        self.item_login.state = 1 if LOGIN_PLIST.exists() else 0

    # ---- settings + menu callbacks --------------------------------------
    def _load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_settings(self) -> None:
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(
                {"title_mode": self._title_mode, "onboarded": self._onboarded}))
        except OSError:
            pass

    def open_onboarding(self, _) -> None:
        try:
            if self._ob is None:
                from clawdmeter_onboarding import OnboardingController
                self._ob = OnboardingController.alloc().initWithApp_(self)
            self._ob.show()
        except Exception:
            import traceback
            traceback.print_exc()

    def _mark_onboarded(self) -> None:
        self._onboarded = True
        self._save_settings()

    def _attempt_token_refresh(self) -> None:
        """Self-heal an expired token. Claude Code refreshes and re-persists the
        stored OAuth token whenever it makes an authenticated call, so we just
        nudge it with one minimal `claude -p` request (no MCP, cheapest model).
        The daemon re-reads the refreshed token on its next poll. Throttled, and
        run on a background thread. This delegates entirely to Claude Code's own
        auth — no OAuth flow is reimplemented here. If the refresh token itself
        has expired, this is a no-op and the user re-signs in via Set Up."""
        now = time.time()
        if now - self._last_refresh < 300:  # at most once every 5 min
            return
        self._last_refresh = now

        def worker():
            print(f"[{time.strftime('%H:%M:%S')}] Token expired — nudging Claude "
                  f"Code to refresh it…", flush=True)
            try:
                subprocess.run(
                    ["/bin/zsh", "-lic",
                     "cd ~ && claude -p 'ok' --model haiku --strict-mcp-config "
                     ">/dev/null 2>&1"],
                    timeout=90,
                )
                print(f"[{time.strftime('%H:%M:%S')}] Refresh nudge done; daemon "
                      f"retries on its next poll.", flush=True)
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] Refresh nudge failed: {e}",
                      flush=True)

        threading.Thread(target=worker, name="clawd-refresh", daemon=True).start()

    def _set_title_mode(self, key: str) -> None:
        self._title_mode = key
        for k, mi in self._mode_items.items():
            mi.state = 1 if k == key else 0
        self._save_settings()
        if self._connected and self._payload:  # reflect the change immediately
            self.title = self._title_for(self._payload)

    def open_log(self, _) -> None:
        subprocess.run(["open", str(LOG_OUT)], check=False)

    def toggle_login(self, sender) -> None:
        # No rumps.notification() here — that triggers a "Python" notification
        # permission prompt. The menu checkmark (updated each tick) is the signal.
        if LOGIN_PLIST.exists():
            LOGIN_PLIST.unlink(missing_ok=True)
            sender.state = 0
        elif self._app_path:
            LOGIN_PLIST.parent.mkdir(parents=True, exist_ok=True)
            LOGIN_PLIST.write_text(_LOGIN_PLIST_TEMPLATE.format(app=self._app_path))
            sender.state = 1

    def quit_app(self, _) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.send_signal(signal.SIGTERM)
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        rumps.quit_application()


def _set_menu_bar_only() -> None:
    """Hide the Dock icon (menu-bar-only). The app is launched detached from the
    bundle so the .app's LSUIElement doesn't apply to this process — set the
    accessory activation policy directly instead."""
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass


def main() -> None:
    _lock = _single_instance_or_exit()  # noqa: F841 — held for process lifetime
    _set_menu_bar_only()
    ClawdmeterApp().run()


if __name__ == "__main__":
    main()
