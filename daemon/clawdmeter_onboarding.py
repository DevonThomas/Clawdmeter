#!/usr/bin/env python3
"""Clawdmeter onboarding — a polished setup window (native NSWindow whose content
is a WKWebView rendering an HTML/CSS design). Walks the user from a fresh board
to live usage: Bluetooth access → pair the board → sign in to Claude.

It's a live checklist: a timer re-pushes state and the step badges fill in green
on their own; the current step is highlighted with a contextual button.

Detection is delegated to supported surfaces (no Keychain scraping):
  - Bluetooth:  CBCentralManager.authorization()
  - Paired:     the daemon reports a live connection (app._connected)
  - Claude:     `claude auth status` JSON + the daemon actually sending data
                (and a live 401 → "expired", trusted over auth status)
Sign-in launches the real `claude auth login --claudeai` in Terminal.
"""

import base64
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSMakeRect,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
)
from Foundation import NSObject, NSTimer
from WebKit import WKWebView, WKWebViewConfiguration

W, H = 460, 610

HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
:root{
  --accent:#D97757; --accent-d:#c25e42;
  --bg:#ffffff; --fg:#1d1d1f; --muted:#86868b; --line:#e9e9ec;
  --card:#f5f5f7; --done:#30b85a; --shadow:rgba(217,119,87,.35);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#1c1c1e; --fg:#f5f5f7; --muted:#98989d; --line:#38383b;
  --card:#2b2b2e; --done:#32d15b;
}}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font:14px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
  background:var(--bg);color:var(--fg);-webkit-user-select:none;cursor:default;
  display:flex;flex-direction:column}
.header{text-align:center;padding:34px 32px 6px;
  background:radial-gradient(120% 90% at 50% -10%,rgba(217,119,87,.14),transparent 60%)}
.logo{width:64px;height:64px;image-rendering:pixelated;display:block;margin:0 auto 12px;
  filter:drop-shadow(0 4px 10px rgba(217,119,87,.28))}
.title{font-size:22px;font-weight:700;letter-spacing:-.02em}
.subtitle{font-size:13px;color:var(--muted);margin-top:5px}
.steps{padding:22px 30px 4px;flex:1}
.step{display:flex;gap:15px;position:relative;padding-bottom:24px}
.step:last-child{padding-bottom:6px}
.step:not(:last-child)::before{content:"";position:absolute;left:16px;top:34px;
  bottom:0;width:2px;background:var(--line);transition:background .3s}
.step.done:not(:last-child)::before{background:var(--done)}
.badge{flex:0 0 34px;height:34px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;font-weight:700;font-size:15px;z-index:1;background:var(--card);
  color:var(--muted);border:2px solid var(--line);transition:.25s}
.step.active .badge{background:var(--accent);color:#fff;border-color:var(--accent);
  box-shadow:0 3px 12px var(--shadow);transform:scale(1.04)}
.step.done .badge{background:var(--done);color:#fff;border-color:var(--done)}
.body{flex:1;min-width:0;padding-top:5px}
.stitle{font-size:15px;font-weight:600;letter-spacing:-.01em}
.step.pending .stitle,.step.pending .sdesc{opacity:.45}
.sdesc{font-size:12.5px;color:var(--muted);margin-top:4px}
.btn{margin-top:11px;background:var(--accent);color:#fff;border:none;border-radius:9px;
  padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;transition:.15s;
  box-shadow:0 2px 8px var(--shadow)}
.btn:hover{background:var(--accent-d)}
.btn:active{transform:translateY(1px)}
.btn[hidden]{display:none}
.footer{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:15px 30px 22px;border-top:1px solid var(--line)}
.status{font-size:12.5px;color:var(--muted);font-weight:500}
.status.ok{color:var(--done);font-weight:600}
.done-btn{background:var(--card);color:var(--fg);border:1px solid var(--line);
  border-radius:9px;padding:8px 22px;font-size:13px;font-weight:600;cursor:pointer;
  transition:.15s;white-space:nowrap}
.done-btn:hover{border-color:var(--muted)}
.done-btn.primary{background:var(--accent);color:#fff;border-color:var(--accent);
  box-shadow:0 2px 8px var(--shadow)}
.done-btn.primary:hover{background:var(--accent-d)}
</style></head><body>
<div class="header">
  <img class="logo" src="__LOGO__">
  <div class="title">Set up Clawdmeter</div>
  <div class="subtitle">Three quick steps to get usage on your board.</div>
</div>
<div class="steps">
  <div class="step" id="s-bt"><div class="badge">1</div><div class="body">
    <div class="stitle">Bluetooth access</div><div class="sdesc"></div>
    <button class="btn" hidden></button></div></div>
  <div class="step" id="s-pair"><div class="badge">2</div><div class="body">
    <div class="stitle">Pair your board</div><div class="sdesc"></div>
    <button class="btn" hidden></button></div></div>
  <div class="step" id="s-claude"><div class="badge">3</div><div class="body">
    <div class="stitle">Sign in to Claude</div><div class="sdesc"></div>
    <button class="btn" hidden></button></div></div>
</div>
<div class="footer">
  <div class="status"></div>
  <button class="done-btn">Close</button>
</div>
<script>
function send(a){window.webkit.messageHandlers.clawd.postMessage({action:a});}
function applyState(s){
  s.steps.forEach(function(st){
    var el=document.getElementById("s-"+st.id);
    el.className="step "+st.status;
    var b=el.querySelector(".badge");
    b.textContent=st.status==="done"?"✓":st.num;
    el.querySelector(".sdesc").textContent=st.desc;
    var btn=el.querySelector(".btn");
    if(st.button){btn.hidden=false;btn.textContent=st.button.label;btn.dataset.action=st.button.action;}
    else{btn.hidden=true;}
  });
  var f=document.querySelector(".status");
  f.textContent=s.footer; f.className="status"+(s.allSet?" ok":"");
  var d=document.querySelector(".done-btn");
  d.textContent=s.allSet?"Done":"Close"; d.className="done-btn"+(s.allSet?" primary":"");
}
document.addEventListener("click",function(e){
  var t=e.target;
  if(t.classList.contains("btn")) send(t.dataset.action);
  else if(t.classList.contains("done-btn")) send("done");
});
</script></body></html>"""


def _logo_data_uri() -> str:
    for p in (
        Path(__file__).resolve().parent.parent / "assets" / "logo_80.png",
        Path(os.environ.get("CLAWDMETER_APP", "")) / "Contents" / "Resources" / "menubar.png",
        Path(os.environ.get("CLAWDMETER_ICON", "") or "."),
    ):
        try:
            if p.is_file():
                return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
        except Exception:
            pass
    return ""


def _claude_status_blocking() -> dict:
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


class OnboardingController(NSObject):
    def initWithApp_(self, app):
        self = objc.super(OnboardingController, self).init()
        if self is None:
            return None
        self.app = app
        self.window = None
        self.webview = None
        self.timer = None
        self._loaded = False
        self._auth = None
        self._auth_ts = 0.0
        self._auth_running = False
        self._signing_in = False
        return self

    # ---- build -----------------------------------------------------------
    @objc.python_method
    def _build(self):
        rect = NSMakeRect(0, 0, W, H)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskFullSizeContentView)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        win.setTitle_("Set up Clawdmeter")
        win.setTitlebarAppearsTransparent_(True)
        win.setTitleVisibility_(NSWindowTitleHidden)
        win.setMovableByWindowBackground_(True)
        win.setReleasedWhenClosed_(False)
        win.center()
        win.setDelegate_(self)

        cfg = WKWebViewConfiguration.alloc().init()
        cfg.userContentController().addScriptMessageHandler_name_(self, "clawd")
        wv = WKWebView.alloc().initWithFrame_configuration_(rect, cfg)
        wv.setNavigationDelegate_(self)
        try:
            wv.setValue_forKey_(False, "drawsBackground")  # let CSS bg show
        except Exception:
            pass
        wv.loadHTMLString_baseURL_(HTML.replace("__LOGO__", _logo_data_uri()), None)
        win.setContentView_(wv)
        self.window, self.webview = win, wv

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
        self._push()

    def windowWillClose_(self, note):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    # ---- WKNavigationDelegate / WKScriptMessageHandler -------------------
    def webView_didFinishNavigation_(self, wv, nav):
        self._loaded = True
        self._push()

    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        try:
            action = message.body().objectForKey_("action")
        except Exception:
            action = None
        if action == "openBluetooth":
            self._open(("x-apple.systempreferences:com.apple.BluetoothSettings",
                        "x-apple.systempreferences:com.apple.preference.bluetooth"))
        elif action == "openBluetoothPrivacy":
            self._open(("x-apple.systempreferences:com.apple.preference.security"
                        "?Privacy_Bluetooth",))
        elif action == "signIn":
            self._do_signin()
        elif action == "done":
            self._do_done()

    def tick_(self, timer):
        self._maybe_check_auth()
        self._push()

    # ---- detection -------------------------------------------------------
    @objc.python_method
    def _bt_ok(self):
        try:
            from CoreBluetooth import CBCentralManager
            return int(CBCentralManager.authorization()) == 3
        except Exception:
            return bool(getattr(self.app, "_connected", False))

    @objc.python_method
    def _maybe_check_auth(self):
        now = time.time()
        if self._auth_running or (self._auth is not None and now - self._auth_ts < 4):
            return
        self._auth_running = True

        def worker():
            self._auth = _claude_status_blocking()
            self._auth_ts = time.time()
            self._auth_running = False

        threading.Thread(target=worker, daemon=True).start()

    @objc.python_method
    def _claude_state(self):
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

    @objc.python_method
    def _state(self):
        bt = self._bt_ok()
        paired = bool(getattr(self.app, "_connected", False))
        claude_ok, claude_msg = self._claude_state()
        done = {"bt": bt, "pair": paired, "claude": claude_ok}
        order = ["bt", "pair", "claude"]
        active = next((k for k in order if not done[k]), None)

        def st(k):
            return "done" if done[k] else ("active" if k == active else "pending")

        def btn(k, label, action):
            return {"label": label, "action": action} if st(k) == "active" else None

        steps = [
            {"id": "bt", "num": 1, "status": st("bt"),
             "desc": "Allowed." if bt else "Grant Bluetooth access when macOS asks.",
             "button": btn("bt", "Open Privacy Settings", "openBluetoothPrivacy")},
            {"id": "pair", "num": 2, "status": st("pair"),
             "desc": "Connected to your board." if paired else
                     "Hold the board's middle button ~3s, then connect “Clawdmeter”.",
             "button": btn("pair", "Open Bluetooth Settings", "openBluetooth")},
            {"id": "claude", "num": 3, "status": st("claude"), "desc": claude_msg,
             "button": btn("claude", "Sign in to Claude", "signIn")},
        ]
        all_set = bt and paired and claude_ok
        if all_set:
            self._signing_in = False
            p = getattr(self.app, "_payload", None) or {}
            extra = f" — {p.get('s')}% on your board" if p.get("s") is not None else ""
            footer = f"🎉 All set{extra}."
        else:
            footer = "Follow the highlighted step — it updates on its own."
        return {"steps": steps, "allSet": all_set, "footer": footer}

    @objc.python_method
    def _push(self):
        if not self._loaded or self.webview is None:
            return
        js = "applyState(%s)" % json.dumps(self._state())
        self.webview.evaluateJavaScript_completionHandler_(js, None)

    # ---- actions ---------------------------------------------------------
    @objc.python_method
    def _open(self, urls):
        for u in urls:
            if subprocess.run(["open", u]).returncode == 0:
                return

    @objc.python_method
    def _do_signin(self):
        self._signing_in = True
        self._auth_ts = 0.0
        subprocess.run([
            "osascript",
            "-e", 'tell application "Terminal" to do script "claude auth login --claudeai"',
            "-e", 'tell application "Terminal" to activate',
        ])
        self._push()

    @objc.python_method
    def _do_done(self):
        try:
            self.app._mark_onboarded()
        except Exception:
            pass
        if self.window is not None:
            self.window.performClose_(None)
