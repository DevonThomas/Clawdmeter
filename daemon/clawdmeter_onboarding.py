#!/usr/bin/env python3
"""Clawdmeter onboarding — a native AppKit window that walks the user from a
fresh board to live usage: Bluetooth access → pair the board → sign in to Claude.

It's a live checklist: a background timer re-checks each requirement and the row
turns green on its own. Whatever's incomplete shows a contextual button. It
doubles as a "connection / setup" window reachable from the menu anytime.

Detection is delegated to supported surfaces (no Keychain scraping):
  - Bluetooth:  CBCentralManager.authorization() (a class-method TCC query)
  - Paired:     the daemon reports a live connection (app._connected)
  - Claude:     `claude auth status` JSON  + the daemon actually sending data
Sign-in launches the real `claude auth login --claudeai` in Terminal.
"""

import json
import os
import subprocess
import threading
import time

import objc

from AppKit import (
    NSApp,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSImage,
    NSImageView,
    NSLineBreakByWordWrapping,
    NSMakeRect,
    NSTextAlignmentCenter,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject, NSTimer

W, H = 470, 545


def _claude_status_blocking() -> dict:
    """`claude auth status` as JSON. Run via a login shell so nvm's `claude` is
    on PATH. Called on a background thread only (it can take ~0.5s)."""
    try:
        out = subprocess.run(
            ["/bin/zsh", "-lic", "claude auth status 2>/dev/null"],
            capture_output=True, text=True, timeout=20,
        )
        s = out.stdout
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            return json.loads(s[i:j + 1])
    except Exception:
        pass
    return {}


class FlippedView(NSView):
    def isFlipped(self):  # top-left origin, so y grows downward
        return True


class OnboardingController(NSObject):
    # ---- construction ----------------------------------------------------
    def initWithApp_(self, app):
        self = objc.super(OnboardingController, self).init()
        if self is None:
            return None
        self.app = app
        self.window = None
        self.timer = None
        self._auth = None
        self._auth_ts = 0.0
        self._auth_running = False
        self._signing_in = False
        return self

    @objc.python_method
    def _mklabel(self, text, x, y, w, h, size, bold=False, color=None, wrap=False):
        f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        f.setStringValue_(text)
        f.setBezeled_(False)
        f.setDrawsBackground_(False)
        f.setEditable_(False)
        f.setSelectable_(False)
        f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                   else NSFont.systemFontOfSize_(size))
        if color is not None:
            f.setTextColor_(color)
        if wrap:
            f.setLineBreakMode_(NSLineBreakByWordWrapping)
            f.cell().setWraps_(True)
        return f

    @objc.python_method
    def _mkbutton(self, title, x, y, w, h, action):
        b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        b.setTitle_(title)
        b.setBezelStyle_(NSBezelStyleRounded)
        b.setTarget_(self)
        b.setAction_(action)
        return b

    @objc.python_method
    def _build(self):
        rect = NSMakeRect(0, 0, W, H)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        win.setTitle_("Set up Clawdmeter")
        win.setReleasedWhenClosed_(False)
        win.center()
        win.setDelegate_(self)

        content = FlippedView.alloc().initWithFrame_(rect)
        win.setContentView_(content)
        gray = NSColor.secondaryLabelColor()

        # header
        logo = self._load_logo()
        if logo is not None:
            iv = NSImageView.alloc().initWithFrame_(NSMakeRect((W - 64) / 2, 20, 64, 64))
            iv.setImage_(logo)
            content.addSubview_(iv)
        t = self._mklabel("Set up Clawdmeter", 0, 92, W, 26, 20, bold=True)
        t.setAlignment_(NSTextAlignmentCenter)  # center
        content.addSubview_(t)
        st = self._mklabel("Three quick steps to get usage on your board.",
                           0, 120, W, 18, 12, color=gray)
        st.setAlignment_(NSTextAlignmentCenter)
        content.addSubview_(st)

        # step rows
        def row(y, num, title):
            g = self._mklabel("○", 26, y, 26, 24, 17)
            content.addSubview_(g)
            lab = self._mklabel(f"{num}   {title}", 60, y, W - 90, 20, 13, bold=True)
            content.addSubview_(lab)
            d = self._mklabel("", 60, y + 22, W - 96, 34, 11, color=gray, wrap=True)
            content.addSubview_(d)
            return g, d

        self.g_bt, self.d_bt = row(164, "1", "Bluetooth access")
        self.g_pair, self.d_pair = row(240, "2", "Pair your board")
        self.b_pair = self._mkbutton("Open Bluetooth Settings", 60, 296, 210, 26,
                                     "openBluetooth:")
        content.addSubview_(self.b_pair)
        self.g_claude, self.d_claude = row(344, "3", "Sign in to Claude")
        self.b_claude = self._mkbutton("Sign in to Claude", 60, 400, 210, 26,
                                       "signIn:")
        content.addSubview_(self.b_claude)

        # footer
        self.footer = self._mklabel("", 24, 458, W - 48, 20, 12, bold=True)
        self.footer.setAlignment_(NSTextAlignmentCenter)
        content.addSubview_(self.footer)
        self.b_done = self._mkbutton("Done", W - 110 - 24, 492, 110, 30, "done:")
        self.b_done.setKeyEquivalent_("\r")
        content.addSubview_(self.b_done)

        self.window = win

    @objc.python_method
    def _load_logo(self):
        for p in (
            os.path.join(os.environ.get("CLAWDMETER_APP", ""),
                         "Contents", "Resources", "Clawdmeter.icns"),
            os.environ.get("CLAWDMETER_ICON", ""),
        ):
            if p and os.path.exists(p):
                img = NSImage.alloc().initByReferencingFile_(p)
                if img is not None:
                    return img
        return None

    # ---- lifecycle -------------------------------------------------------
    @objc.python_method
    def show(self):
        if self.window is None:
            self._build()
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        if self.timer is None:
            self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.5, self, "tick:", None, True)
        self._refresh()

    def windowWillClose_(self, note):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        # back to a menu-bar-only app (no Dock icon)
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    # ---- detection -------------------------------------------------------
    @objc.python_method
    def _bt_ok(self):
        try:
            from CoreBluetooth import CBCentralManager
            return int(CBCentralManager.authorization()) == 3  # allowedAlways
        except Exception:
            return bool(getattr(self.app, "_connected", False))

    @objc.python_method
    def _maybe_check_auth(self):
        now = time.time()
        if self._auth_running or (self._auth is not None and now - self._auth_ts < 4):
            return
        self._auth_running = True

        def worker():
            data = _claude_status_blocking()
            self._auth = data
            self._auth_ts = time.time()
            self._auth_running = False

        threading.Thread(target=worker, daemon=True).start()

    @objc.python_method
    def _claude_state(self):
        """(ok, label). ok means the daemon can actually use this login.

        The daemon actively sending data is the ground truth. A live 401 means
        the token expired — trust that over `claude auth status`, which reports
        loggedIn:true even when the stored access token is stale."""
        daemon_ok = bool(getattr(self.app, "_connected", False)) and \
            getattr(self.app, "_payload", None) is not None
        data = self._auth or {}
        logged = bool(data.get("loggedIn"))
        method = data.get("authMethod")
        email = data.get("email", "")
        sub = (data.get("subscriptionType") or "").title()
        who = f"{email} ({sub})" if email else "your Claude subscription"
        if daemon_ok:
            return True, f"Signed in as {who}."
        if getattr(self.app, "_auth_error", False):
            if time.time() - getattr(self.app, "_last_refresh", 0) < 90:
                return False, "Refreshing your Claude sign-in automatically — one moment…"
            return False, "Your Claude sign-in expired — sign in again to refresh it."
        if logged and method == "claude.ai":
            return True, f"Signed in as {who}."
        if logged and method:
            return False, ("Signed in with API billing — the board needs a Claude "
                           "subscription. Re-sign in below.")
        if self._signing_in:
            return False, "Finish signing in in the browser…"
        return False, "Sign in with your Claude Pro or Max subscription."

    # ---- timer / UI update ----------------------------------------------
    def tick_(self, timer):
        self._refresh()

    @objc.python_method
    def _refresh(self):
        green = NSColor.systemGreenColor()
        gray = NSColor.tertiaryLabelColor()
        self._maybe_check_auth()

        bt = self._bt_ok()
        self.g_bt.setStringValue_("✓" if bt else "○")
        self.g_bt.setTextColor_(green if bt else gray)
        self.d_bt.setStringValue_(
            "Allowed." if bt else "Click Allow if macOS asks for Bluetooth access.")

        paired = bool(getattr(self.app, "_connected", False))
        self.g_pair.setStringValue_("✓" if paired else "○")
        self.g_pair.setTextColor_(green if paired else gray)
        self.d_pair.setStringValue_(
            "Connected to your board." if paired else
            "Hold the board's middle button ~3s, then open Bluetooth settings and "
            "connect “Clawdmeter”.")
        self.b_pair.setHidden_(paired)

        claude_ok, claude_msg = self._claude_state()
        self.g_claude.setStringValue_("✓" if claude_ok else "○")
        self.g_claude.setTextColor_(green if claude_ok else gray)
        self.d_claude.setStringValue_(claude_msg)
        self.b_claude.setHidden_(claude_ok)

        if bt and paired and claude_ok:
            self._signing_in = False
            p = getattr(self.app, "_payload", None) or {}
            extra = f" — {p.get('s')}% on your board" if p.get("s") is not None else ""
            self.footer.setStringValue_(f"🎉 All set{extra}.")
            self.footer.setTextColor_(green)
            self.b_done.setTitle_("Done")
        else:
            self.footer.setStringValue_("Complete the steps above — this updates itself.")
            self.footer.setTextColor_(NSColor.secondaryLabelColor())
            self.b_done.setTitle_("Close")

    # ---- actions ---------------------------------------------------------
    def openBluetooth_(self, sender):
        for url in ("x-apple.systempreferences:com.apple.BluetoothSettings",
                    "x-apple.systempreferences:com.apple.preference.bluetooth"):
            if subprocess.run(["open", url]).returncode == 0:
                break

    def signIn_(self, sender):
        self._signing_in = True
        self._auth_ts = 0.0  # force a fresh status check soon
        subprocess.run([
            "osascript",
            "-e", 'tell application "Terminal" to do script "claude auth login --claudeai"',
            "-e", 'tell application "Terminal" to activate',
        ])
        self._refresh()

    def done_(self, sender):
        try:
            self.app._mark_onboarded()
        except Exception:
            pass
        if self.window is not None:
            self.window.performClose_(None)
