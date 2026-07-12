"""Web-based remote desktop viewer served over HTTP + WebSocket.

Provides a browser-based KVM interface: JPEG video stream over WebSocket
(binary frames) and keyboard/mouse input as JSON text messages.  HTML/JS
is embedded directly — no npm or build step required.

Usage (integrated into server.py):
    serial-hid-kvm --headless --web
    serial-hid-kvm --web --web-port 9330 --web-fps 20 --web-quality 60
"""

import asyncio
import hashlib
import hmac
import importlib.util
import ipaddress
import json
import logging
import queue
import re
import secrets
import socket
import ssl
import time
from pathlib import Path

import websockets
from websockets.http11 import Response

from ._audio import AudioCapture
from .hid_protocol import (
    build_keyboard_report,
    build_mouse_abs_packet,
    build_mouse_rel_packet,
)

logger = logging.getLogger(__name__)

# WebRTC (H.264) streaming is optional: pip install serial-hid-kvm[webrtc].
# find_spec only checks installability — the actual import happens lazily on
# the first webrtc_offer, so startup cost is unaffected.
_WEBRTC_AVAILABLE = importlib.util.find_spec("aiortc") is not None


def _is_private_address(ip: str) -> bool:
    """Whether *ip* is loopback / LAN / link-local (vs. a public address).

    Drives the per-connection LAN/WAN tuning split: LAN clients keep the
    original loopback-tuned behaviour (fixed fps, max quality, zero jitter
    buffer, always-on audio), WAN clients get the adaptive treatment.
    Unparseable addresses are treated as WAN — the safe direction.
    """
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


# WAN stream tuning (per-connection; LAN connections are unaffected).
_WAN_CREDITS = 2            # max unacked JPEG frames in flight
_WAN_FPS_CAP = 30           # WAN JPEG target fps (never above web_fps)
_WAN_QUALITY_MIN = 25       # adaptive JPEG quality floor
_WAN_QUALITY_CAP = 80       # adaptive JPEG quality ceiling (≤ web_quality)
_WAN_ACK_RESET_S = 5.0      # assume acks lost after this long at full credit
_WAN_RTC_FPS_CAP = 30       # WAN H264 default fps (never above webrtc_fps)
_WAN_RTC_START = 4_000_000  # WAN H264 starting bitrate (bits/s)


# ---------------------------------------------------------------------------
# W3C KeyboardEvent.code → HID keycode mapping
# ---------------------------------------------------------------------------

_JS_CODE_TO_HID: dict[str, int] = {
    # Row 0: Escape + Function keys
    "Escape": 0x29,
    "F1": 0x3A, "F2": 0x3B, "F3": 0x3C, "F4": 0x3D,
    "F5": 0x3E, "F6": 0x3F, "F7": 0x40, "F8": 0x41,
    "F9": 0x42, "F10": 0x43, "F11": 0x44, "F12": 0x45,
    "PrintScreen": 0x46, "ScrollLock": 0x47, "Pause": 0x48,

    # Row 1: Digits
    "Backquote": 0x35,
    "Digit1": 0x1E, "Digit2": 0x1F, "Digit3": 0x20, "Digit4": 0x21,
    "Digit5": 0x22, "Digit6": 0x23, "Digit7": 0x24, "Digit8": 0x25,
    "Digit9": 0x26, "Digit0": 0x27,
    "Minus": 0x2D, "Equal": 0x2E, "Backspace": 0x2A,

    # Row 2: QWERTY
    "Tab": 0x2B,
    "KeyQ": 0x14, "KeyW": 0x1A, "KeyE": 0x08, "KeyR": 0x15,
    "KeyT": 0x17, "KeyY": 0x1C, "KeyU": 0x18, "KeyI": 0x0C,
    "KeyO": 0x12, "KeyP": 0x13,
    "BracketLeft": 0x2F, "BracketRight": 0x30, "Backslash": 0x31,

    # Row 3: ASDF
    "CapsLock": 0x39,
    "KeyA": 0x04, "KeyS": 0x16, "KeyD": 0x07, "KeyF": 0x09,
    "KeyG": 0x0A, "KeyH": 0x0B, "KeyJ": 0x0D, "KeyK": 0x0E,
    "KeyL": 0x0F,
    "Semicolon": 0x33, "Quote": 0x34, "Enter": 0x28,

    # Row 4: ZXCV
    "KeyZ": 0x1D, "KeyX": 0x1B, "KeyC": 0x06, "KeyV": 0x19,
    "KeyB": 0x05, "KeyN": 0x11, "KeyM": 0x10,
    "Comma": 0x36, "Period": 0x37, "Slash": 0x38,

    # Row 5
    "Space": 0x2C,

    # Navigation cluster
    "Insert": 0x49, "Home": 0x4A, "PageUp": 0x4B,
    "Delete": 0x4C, "End": 0x4D, "PageDown": 0x4E,

    # Arrow keys
    "ArrowUp": 0x52, "ArrowDown": 0x51,
    "ArrowLeft": 0x50, "ArrowRight": 0x4F,

    # Numpad
    "NumLock": 0x53,
    "NumpadDivide": 0x54, "NumpadMultiply": 0x55,
    "NumpadSubtract": 0x56, "NumpadAdd": 0x57,
    "NumpadEnter": 0x58, "NumpadDecimal": 0x63,
    "Numpad0": 0x62, "Numpad1": 0x59, "Numpad2": 0x5A,
    "Numpad3": 0x5B, "Numpad4": 0x5C, "Numpad5": 0x5D,
    "Numpad6": 0x5E, "Numpad7": 0x5F, "Numpad8": 0x60,
    "Numpad9": 0x61,

    # ISO extra key (left of Z on non-US keyboards)
    "IntlBackslash": 0x64,

    # JIS-specific
    "IntlRo": 0x87,       # International1 (ろ / _\)
    "IntlYen": 0x89,      # International3 (¥|)
    "KanaMode": 0x88,     # Katakana/Hiragana
    "Convert": 0x8A,      # 変換
    "NonConvert": 0x8B,    # 無変換
    "Lang1": 0x90,
    "Lang2": 0x91,

    # Context menu
    "ContextMenu": 0x65,
}

# Modifier codes → bitmask
_JS_MOD_BITS: dict[str, int] = {
    "ShiftLeft": 0x02, "ShiftRight": 0x20,
    "ControlLeft": 0x01, "ControlRight": 0x10,
    "AltLeft": 0x04, "AltRight": 0x40,
    "MetaLeft": 0x08, "MetaRight": 0x80,
}


# ---------------------------------------------------------------------------
# PWA assets (manifest, service worker, icon)
# ---------------------------------------------------------------------------

_MANIFEST_JSON = json.dumps({
    "name": "serial-hid-kvm",
    "short_name": "KVM",
    "description": "Remote KVM via CH9329 USB HID + HDMI capture",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#1a1a2e",
    "theme_color": "#16213e",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
    ],
}, separators=(",", ":"))

# Minimal service worker — just enough for PWA installability, no caching.
_SW_JS = """\
self.addEventListener("fetch", (e) => e.respondWith(fetch(e.request)));
"""

# Simple SVG icon: monitor with "KVM" label
_ICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="64" fill="#16213e"/>
<rect x="56" y="80" width="400" height="260" rx="16" fill="#0a0a1a" stroke="#0f3460" stroke-width="12"/>
<rect x="200" y="360" width="112" height="24" rx="4" fill="#0f3460"/>
<rect x="160" y="392" width="192" height="16" rx="8" fill="#0f3460"/>
<text x="256" y="240" text-anchor="middle" font-family="system-ui,sans-serif" font-weight="bold" font-size="96" fill="#e0e0e0">KVM</text>
</svg>
"""

# ---------------------------------------------------------------------------
# Embedded HTML/JS viewer
# ---------------------------------------------------------------------------

_VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#16213e">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<title>serial-hid-kvm</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden;background:#1a1a2e;font-family:system-ui,sans-serif;color:#e0e0e0}
#toolbar{display:flex;align-items:center;gap:8px;padding:4px 12px;background:#16213e;height:36px;user-select:none}
#toolbar button{background:#0f3460;border:1px solid #1a1a5e;color:#e0e0e0;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:13px}
#toolbar button:hover{background:#1a4a8a}
#toolbar button:active{background:#0a2a50}
#toolbar select{background:#0f3460;border:1px solid #1a1a5e;color:#e0e0e0;padding:4px 6px;border-radius:4px;cursor:pointer;font-size:13px}
#status{margin-left:auto;font-size:12px;color:#8888aa}
#fps{font-size:12px;color:#8888aa;min-width:70px;text-align:right}
#container{display:flex;align-items:center;justify-content:center;width:100%;height:calc(100% - 36px);background:#0a0a1a;overflow:auto;cursor:none;outline:none}
#screen,#video{display:block;image-rendering:auto;background:#0a0a1a}
#toolbar button.active{background:#2a6a2a;border-color:#3a8a3a}
#btnRec.recording{background:#7a1a1a;border-color:#c03030;animation:recpulse 1.5s ease-in-out infinite}
@keyframes recpulse{0%,100%{opacity:1}50%{opacity:.55}}
body.fs-autohide #toolbar{position:fixed;top:0;z-index:10;transform:translateY(-100%);transition:transform .3s ease;pointer-events:none;border-radius:0 0 8px 8px;box-shadow:0 2px 12px rgba(0,0,0,.5);cursor:grab}
body.fs-autohide #toolbar.visible{transform:translateY(0);pointer-events:auto}
body.fs-autohide #toolbar.dragging{cursor:grabbing;transition:none}
body.fs-autohide #container{height:100%}
body.tb-hidden #toolbar{display:none}
body.tb-hidden #container{height:100%}
#hint{position:fixed;top:8px;left:50%;transform:translateX(-50%);background:rgba(22,33,62,.92);border:1px solid #0f3460;color:#e0e0e0;padding:4px 12px;border-radius:6px;font-size:12px;z-index:20;opacity:0;transition:opacity .4s;pointer-events:none;white-space:nowrap}
#hint.show{opacity:1}
#login{position:fixed;inset:0;background:rgba(10,10,26,.9);display:none;align-items:center;justify-content:center;z-index:30}
#login.show{display:flex}
#login form{background:#16213e;border:1px solid #0f3460;border-radius:10px;padding:28px 32px;display:flex;flex-direction:column;gap:12px;min-width:280px}
#login h1{font-size:16px;font-weight:600;text-align:center}
#loginPw{background:#0a0a1a;border:1px solid #0f3460;border-radius:4px;color:#e0e0e0;padding:8px 10px;font-size:14px;outline:none}
#loginPw:focus{border-color:#1a4a8a}
#login button{background:#0f3460;border:1px solid #1a1a5e;color:#e0e0e0;padding:8px 12px;border-radius:4px;cursor:pointer;font-size:14px}
#login button:hover{background:#1a4a8a}
#loginErr{color:#e07070;font-size:12px;min-height:14px}
#login label{font-size:12px;color:#8888aa;display:flex;align-items:center;gap:6px;user-select:none}
</style>
</head>
<body>
<div id="toolbar">
  <button id="btnCad" title="Send Ctrl+Alt+Delete to target">Ctrl+Alt+Del</button>
  <button id="btnAltTab" title="Send Alt+Tab to target">Alt+Tab</button>
  <!-- <button id="btnIme" title="Toggle target IME (sends &#x534a;&#x89d2;/&#x5168;&#x89d2;) — bypasses host IME">IME &#x3042;/A</button> -->
  <!-- <button id="btnCaps" title="Toggle Caps Lock (sends Shift+CapsLock for JIS keyboards)">Caps</button> -->
  <!-- <button id="btnViewOnly" title="Toggle view-only mode (no input sent)">View Only</button> -->
  <button id="btnAudio" title="Toggle audio playback" style="display:none">&#x1f507; Audio</button>
  <button id="btnRec" title="Record screen + audio to the server's recording folder">&#x23fa; Record</button>
  <button id="btnCursor" title="Toggle local cursor visibility">Cursor</button>
  <button id="btnScale" title="Toggle 1:1 / Fit scaling">Fit</button>
  <button id="btnRtc" title="Low-latency H.264 video over WebRTC (server keeps the capture device, so OCR/MCP stay available)">H264</button>
  <select id="rtcQuality" title="H264 quality preset — Auto picks 16M/60 on LAN and 4M/30 for remote viewers">
    <option value="auto">Auto</option>
    <option value="16000000/60">16M/60</option>
    <option value="8000000/30">8M/30</option>
    <option value="4000000/30">4M/30</option>
    <option value="2000000/30">2M/30</option>
  </select>
  <button id="btnDirect" title="Direct GPU video straight from the capture card (smoothest; uses this PC's device)">Direct</button>
  <button id="btnFs" title="Toggle fullscreen">Fullscreen</button>
  <span id="status">Connecting…</span>
  <span id="fps"></span>
</div>
<div id="container" tabindex="0"><canvas id="screen"></canvas><video id="video" playsinline muted style="display:none"></video></div>
<div id="hint"></div>
<div id="login"><form id="loginForm">
  <h1>serial-hid-kvm</h1>
  <input id="loginPw" type="password" placeholder="Password" autocomplete="current-password">
  <label><input id="loginRemember" type="checkbox"> Remember on this device</label>
  <div id="loginErr"></div>
  <button type="submit">Connect</button>
</form></div>
<script>
"use strict";
const canvas = document.getElementById("screen");
const ctx = canvas.getContext("2d");
const statusEl = document.getElementById("status");
const fpsEl = document.getElementById("fps");
const container = document.getElementById("container");
const toolbar = document.getElementById("toolbar");
const video = document.getElementById("video");
const hint = document.getElementById("hint");
document.getElementById("btnFs").hidden =
  new URLSearchParams(location.search).has("kiosk");

let toolbarHidden = true;   // toolbar starts hidden; Ctrl+Alt+Enter toggles it
let hintTimer = null;

let ws = null;
let frameCount = 0;
let lastFpsTime = performance.now();

// LAN = this page reaches the server via a private/loopback address.
// Several behaviours differ for remote (WAN) viewers — jitter buffer,
// frame acks, audio default — and the server makes the same LAN/WAN call
// from its side using the connection's source address.
const IS_LAN = isPrivateHost(location.hostname);
let adaptiveAck = false;   // server asked us to ack frames (hello.adaptive)

// Direct (native) video mode: the browser opens the capture card itself via
// getUserMedia and renders a real <video> element — same path the official app
// uses — instead of decoding server JPEG frames onto the canvas.
let videoMode = false;
let directStream = null;       // active getUserMedia MediaStream in direct mode
let serverCaptureLabel = "";   // capture-device name reported by the server

// WebRTC (H264) mode: the server encodes the capture as H.264 and streams it
// over a local RTCPeerConnection; the browser renders a native <video>.
// Unlike Direct mode the server KEEPS the capture device, so server-side
// OCR / MCP / capture_frame keep working while the viewer streams.
let rtcMode = false;
let rtcPc = null;              // active RTCPeerConnection
let _rtcWait = null;           // {res, rej} while an offer awaits its answer
let _rtcGen = 0;               // negotiation generation: pairs answers with
                               // offers so a late answer for an abandoned
                               // offer can't corrupt a newer connection

function wsSend(obj) {  // control messages, not gated by view-only
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// --- Authentication (server started with --web-password) ---
// The server gates everything behind an "auth" message on the WebSocket.
// A successful password login yields a reconnect token, stored per-tab in
// sessionStorage (plus localStorage with "Remember") so reconnects and tab
// reloads re-authenticate silently.  Tokens die with the server process;
// when one expires the login overlay reappears.
const login = document.getElementById("login");
const loginPw = document.getElementById("loginPw");
const loginErr = document.getElementById("loginErr");
const loginRemember = document.getElementById("loginRemember");
let triedToken = false;   // one silent token attempt per connection

function storedToken() {
  return sessionStorage.getItem("kvmToken") || localStorage.getItem("kvmToken");
}
function storeToken(t) {
  sessionStorage.setItem("kvmToken", t);
  if (loginRemember.checked) localStorage.setItem("kvmToken", t);
}
function clearToken() {
  sessionStorage.removeItem("kvmToken");
  localStorage.removeItem("kvmToken");
}
function onAuthRequired() {
  statusEl.textContent = "Authentication required";
  const t = storedToken();
  if (t && !triedToken) {
    triedToken = true;
    wsSend({type: "auth", token: t});
  } else {
    login.classList.add("show");
    loginPw.focus();
  }
}
function onAuthOk(msg) {
  if (msg.token) storeToken(msg.token);
  login.classList.remove("show");
  loginErr.textContent = "";
  loginPw.value = "";
}
function onAuthFailed(msg) {
  if (msg.expired) clearToken();   // stale token → fall back to password
  else loginErr.textContent = msg.error || "Authentication failed";
  login.classList.add("show");
  loginPw.focus();
}
document.getElementById("loginForm").addEventListener("submit", (e) => {
  e.preventDefault();
  loginErr.textContent = "";
  wsSend({type: "auth", password: loginPw.value});
});

// --- Auto-reload when the server's frontend changed ---
// The server sends a fingerprint of its embedded HTML/JS on connect. We record
// the first one; if a later (reconnected) server reports a different build, the
// process was restarted with new code, so reload to pick it up. A plain restart
// keeps the same fingerprint and never reloads.
let _build = null;
function checkBuild(b) {
  if (!b) return;
  if (_build === null) { _build = b; return; }
  if (b !== _build) location.reload();
}

// --- WebSocket ---
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(proto + "//" + location.host + "/ws");
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    // Session setup (mode restore) happens on the server's "hello", which
    // arrives after authentication when a password is configured — anything
    // sent before that would be dropped by the server's auth gate.
    triedToken = false;
    statusEl.textContent = "Connecting…";
  };
  ws.onclose = () => {
    statusEl.textContent = "Disconnected — reconnecting…";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => { ws.close(); };
  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) {
      const view = new Uint8Array(ev.data);
      const type = view[0];
      const payload = ev.data.slice(1);
      if (type === 1) {
        // Video frame (JPEG) — hand to the drop-stale renderer.  On the
        // WAN-adaptive stream, ack receipt so the server's credit pacing
        // knows what the link actually delivered.
        queueFrame(payload);
        if (adaptiveAck) wsSend({type: "frame_ack"});
      } else if (type === 2) {
        // Audio chunk (PCM int16 LE)
        feedAudio(payload);
      }
    } else {
      // Text message (JSON)
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "auth_required") { onAuthRequired(); }
        else if (msg.type === "auth_ok") { onAuthOk(msg); }
        else if (msg.type === "auth_failed") { onAuthFailed(msg); }
        else if (msg.type === "hello") {
          checkBuild(msg.build);
          adaptiveAck = !!msg.adaptive;
          if (msg.webrtc === false) {
            const b = document.getElementById("btnRtc");
            b.disabled = true;
            b.title = "Server lacks aiortc (pip install serial-hid-kvm[webrtc])";
          }
          if (directStream) {
            statusEl.textContent = "Direct video (native)";
            // A reconnected server starts in stream mode; if we're showing
            // native video, tell it to release the capture device again.
            wsSend({type: "stream", on: false});
          } else if (rtcMode) {
            // The server-side peer connection died with the old socket or
            // server — renegotiate a fresh one.
            restartRtc();
          } else {
            statusEl.textContent = "Connected";
          }
          // Direct video defaults OFF so the server keeps the capture device
          // and can share frames with server-side OCR / MCP / capture_frame
          // while the viewer is open.
        }
        else if (msg.type === "audio_config") { setupAudioConfig(msg); }
        else if (msg.type === "capture_device") { serverCaptureLabel = msg.label || ""; }
        else if (msg.type === "webrtc_answer") {
          // Only apply the answer that matches the current offer; a late
          // answer for an abandoned offer must be dropped.
          if (rtcPc && msg.gen === _rtcGen &&
              rtcPc.signalingState === "have-local-offer") {
            rtcPc.setRemoteDescription({type: "answer", sdp: msg.sdp})
              .catch((e) => { if (_rtcWait) _rtcWait.rej(e); });
          }
        }
        else if (msg.type === "webrtc_error") {
          if (msg.gen !== undefined && msg.gen !== _rtcGen) { /* stale */ }
          else if (_rtcWait) _rtcWait.rej(new Error(msg.error));
          else {
            statusEl.textContent = "WebRTC error: " + msg.error;
            if (toolbarHidden) showHint("WebRTC error: " + msg.error);
          }
        }
        else if (msg.type === "rec_saved") {
          statusEl.textContent = "Saved: " + msg.path;
          if (toolbarHidden) showHint("Saved: " + msg.path);
          setTimeout(() => { statusEl.textContent = "Connected"; }, 6000);
        }
        else if (msg.type === "rec_error") {
          statusEl.textContent = "Recording error: " + msg.error;
          if (toolbarHidden) showHint("Recording error: " + msg.error);
        }
      } catch(e) {}
    }
  };
}

// --- FPS counter ---
setInterval(() => {
  const now = performance.now();
  const elapsed = (now - lastFpsTime) / 1000;
  const fps = frameCount / elapsed;
  frameCount = 0;
  lastFpsTime = now;
  // <video> renders itself in direct/webrtc mode, so there's no per-frame count.
  fpsEl.textContent = videoMode ? (rtcMode ? "h264" : "native")
                                : fps.toFixed(1) + " fps";
}, 2000);

// --- Video frame rendering (decode off-thread, always draw the freshest) ---
let _pendingFrame = null;   // newest received-but-undrawn JPEG ArrayBuffer
let _decoding = false;
function queueFrame(buf) {
  _pendingFrame = buf;       // keep only the most recent frame
  if (!_decoding) renderLoop();
}
async function renderLoop() {
  _decoding = true;
  while (_pendingFrame) {
    const buf = _pendingFrame;
    _pendingFrame = null;    // anything that arrives during decode replaces this
    try {
      const bmp = await createImageBitmap(new Blob([buf], {type: "image/jpeg"}));
      if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
        canvas.width = bmp.width;
        canvas.height = bmp.height;
        updateCanvasSize();
      }
      ctx.drawImage(bmp, 0, 0);
      bmp.close();
      frameCount++;
    } catch (e) { /* skip undecodable frame */ }
  }
  _decoding = false;
}

// --- Display sizing (works for both the canvas and the direct <video>) ---
let scaleMode = "fit";  // "native" = 1:1 pixel, "fit" = fit to window (default)

function activeEl() { return videoMode ? video : canvas; }
function mediaW() { return videoMode ? video.videoWidth : canvas.width; }
function mediaH() { return videoMode ? video.videoHeight : canvas.height; }

function updateCanvasSize() {
  const el = activeEl();
  const mw = mediaW(), mh = mediaH();
  if (!mw || !mh) return;
  if (scaleMode === "fit") {
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    const ar = mw / mh;
    let dw, dh;
    if (cw / ch > ar) { dh = ch; dw = ch * ar; }
    else { dw = cw; dh = cw / ar; }
    el.style.width = dw + "px";
    el.style.height = dh + "px";
  } else {
    el.style.width = mw + "px";
    el.style.height = mh + "px";
  }
}
window.addEventListener("resize", updateCanvasSize);
video.addEventListener("loadedmetadata", () => { if (videoMode) updateCanvasSize(); });

// --- Mouse coordinate normalisation (0-4095) ---
function mouseCoords(e) {
  const rect = activeEl().getBoundingClientRect();
  if (!rect.width || !rect.height) return {x: 0, y: 0};
  const fx = (e.clientX - rect.left) / rect.width;
  const fy = (e.clientY - rect.top) / rect.height;
  const x = Math.max(0, Math.min(4095, Math.round(fx * 4095)));
  const y = Math.max(0, Math.min(4095, Math.round(fy * 4095)));
  return {x, y};
}

let viewOnly = false;
let showCursor = true;   // local cursor visible by default

function send(obj) {
  if (viewOnly) return;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// --- Mouse events ---
// Throttle moves to ~60Hz, but with a trailing send so the *final* position is
// always delivered — otherwise the target cursor settles a few pixels off from
// the host cursor whenever the last move falls inside the throttle window.
let lastMouseSend = 0;
let pendingMouse = null;
let mouseTrailTimer = null;
function flushMouse() {
  if (!pendingMouse) return;
  send({type:"mousemove", x: pendingMouse.x, y: pendingMouse.y,
        buttons: pendingMouse.buttons});
  pendingMouse = null;
  lastMouseSend = performance.now();
}
container.addEventListener("mousemove", (e) => {
  const {x, y} = mouseCoords(e);
  pendingMouse = {x, y, buttons: e.buttons};
  const dt = performance.now() - lastMouseSend;
  if (dt >= 16) {
    if (mouseTrailTimer) { clearTimeout(mouseTrailTimer); mouseTrailTimer = null; }
    flushMouse();
  } else if (!mouseTrailTimer) {
    mouseTrailTimer = setTimeout(() => { mouseTrailTimer = null; flushMouse(); },
                                 16 - dt);
  }
});
container.addEventListener("mousedown", (e) => {
  e.preventDefault();
  container.focus();
  const {x, y} = mouseCoords(e);
  send({type:"mousedown", x, y, buttons: e.buttons});
});
container.addEventListener("mouseup", (e) => {
  e.preventDefault();
  const {x, y} = mouseCoords(e);
  send({type:"mouseup", x, y, buttons: e.buttons});
});
container.addEventListener("wheel", (e) => {
  e.preventDefault();
  const dy = e.deltaY > 0 ? -3 : 3;
  send({type:"scroll", deltaY: dy});
}, {passive: false});
container.addEventListener("contextmenu", (e) => e.preventDefault());

// --- Keyboard events ---
container.setAttribute("tabindex", "0");
container.addEventListener("keydown", (e) => {
  if (viewOnly) return;
  e.preventDefault();
  e.stopPropagation();
  if (e.repeat) return;
  send({type:"keydown", code: e.code});
});
container.addEventListener("keyup", (e) => {
  if (viewOnly) return;
  e.preventDefault();
  e.stopPropagation();
  send({type:"keyup", code: e.code});
});
container.addEventListener("blur", () => { send({type:"release_all"}); });

// --- Toolbar ---
document.getElementById("btnCad").addEventListener("click", () => {
  send({type:"keydown", code:"ControlLeft"});
  send({type:"keydown", code:"AltLeft"});
  send({type:"keydown", code:"Delete"});
  setTimeout(() => {
    send({type:"keyup", code:"Delete"});
    send({type:"keyup", code:"AltLeft"});
    send({type:"keyup", code:"ControlLeft"});
  }, 100);
  container.focus();
});
// document.getElementById("btnViewOnly").addEventListener("click", () => {
//   const btn = document.getElementById("btnViewOnly");
//   viewOnly = !viewOnly;
//   btn.classList.toggle("active", viewOnly);
//   if (viewOnly) showCursor = true;
//   updateCursor();
// });
document.getElementById("btnCursor").addEventListener("click", () => {
  const btn = document.getElementById("btnCursor");
  showCursor = !showCursor;
  btn.classList.toggle("active", showCursor);
  updateCursor();
  container.focus();
});
function updateCursor() {
  container.style.cursor = showCursor ? "default" : "none";
  document.getElementById("btnCursor").classList.toggle("active", showCursor);
}
document.getElementById("btnScale").addEventListener("click", () => {
  const btn = document.getElementById("btnScale");
  if (scaleMode === "native") { scaleMode = "fit"; btn.textContent = "Fit"; }
  else { scaleMode = "native"; btn.textContent = "1:1"; }
  updateCanvasSize();
  container.focus();
});
document.getElementById("btnAltTab").addEventListener("click", () => {
  send({type:"keydown", code:"AltLeft"});
  send({type:"keydown", code:"Tab"});
  setTimeout(() => {
    send({type:"keyup", code:"Tab"});
    send({type:"keyup", code:"AltLeft"});
  }, 100);
  container.focus();
});
// document.getElementById("btnIme").addEventListener("click", () => {
//   // Toggle target IME via 半角/全角 (Backquote, HID 0x35). Synthesised in JS,
//   // so it bypasses the controller's own IME, which swallows the physical key.
//   send({type:"keydown", code:"Backquote"});
//   setTimeout(() => { send({type:"keyup", code:"Backquote"}); }, 100);
//   container.focus();
// });
// document.getElementById("btnCaps").addEventListener("click", () => {
//   // Japanese (JIS) keyboards toggle Caps Lock with Shift+CapsLock(英数).
//   send({type:"keydown", code:"ShiftLeft"});
//   send({type:"keydown", code:"CapsLock"});
//   setTimeout(() => {
//     send({type:"keyup", code:"CapsLock"});
//     send({type:"keyup", code:"ShiftLeft"});
//   }, 100);
//   container.focus();
// });
document.getElementById("btnFs").addEventListener("click", () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen();
  else document.exitFullscreen();
  container.focus();
});
// Fullscreen: keyboard lock + toolbar auto-hide
let _tbLeft = 0;
let _tbWidth = 0;
function _tbCenter() {
  _tbWidth = toolbar.offsetWidth;
  _tbLeft = (window.innerWidth - _tbWidth) / 2;
  toolbar.style.left = _tbLeft + "px";
}
document.addEventListener("fullscreenchange", () => {
  const isFs = !!document.fullscreenElement;
  document.body.classList.toggle("fs-autohide", isFs);
  if (isFs) {
    if (navigator.keyboard && navigator.keyboard.lock)
      navigator.keyboard.lock(["AltLeft","AltRight","Tab","MetaLeft","MetaRight","Escape"]).catch(() => {});
    requestAnimationFrame(_tbCenter);
  } else {
    toolbar.classList.remove("visible");
    toolbar.style.left = "";
  }
  updateCanvasSize();
});
window.addEventListener("resize", () => { if (document.fullscreenElement) _tbCenter(); });

// Auto-show/hide toolbar in fullscreen when mouse nears center-top
let _tbTimer = null;
function _tbShow() { toolbar.classList.add("visible"); clearTimeout(_tbTimer); }
function _tbScheduleHide() {
  clearTimeout(_tbTimer);
  _tbTimer = setTimeout(() => toolbar.classList.remove("visible"), 1500);
}
document.addEventListener("mousemove", (e) => {
  if (!document.body.classList.contains("fs-autohide")) return;
  if (_tbDragging) return;
  const pad = 80;
  const inX = e.clientX >= _tbLeft - pad && e.clientX <= _tbLeft + _tbWidth + pad;
  if (inX && e.clientY < 50) _tbShow();
  else if (toolbar.classList.contains("visible")) _tbScheduleHide();
});
toolbar.addEventListener("mouseenter", () => { if (document.body.classList.contains("fs-autohide")) _tbShow(); });
toolbar.addEventListener("mouseleave", () => { if (document.body.classList.contains("fs-autohide") && !_tbDragging) _tbScheduleHide(); });

// Drag toolbar horizontally
let _tbDragging = false, _tbDragX0 = 0, _tbDragL0 = 0;
toolbar.addEventListener("mousedown", (e) => {
  if (!document.body.classList.contains("fs-autohide")) return;
  if (e.target.tagName === "BUTTON") return;
  _tbDragging = true;
  _tbDragX0 = e.clientX;
  _tbDragL0 = _tbLeft;
  toolbar.classList.add("dragging");
  e.preventDefault();
});
document.addEventListener("mousemove", (e) => {
  if (!_tbDragging) return;
  _tbLeft = Math.max(0, Math.min(window.innerWidth - _tbWidth, _tbDragL0 + e.clientX - _tbDragX0));
  toolbar.style.left = _tbLeft + "px";
});
document.addEventListener("mouseup", () => {
  if (!_tbDragging) return;
  _tbDragging = false;
  toolbar.classList.remove("dragging");
});

// --- Audio playback ---
let audioCtx = null;
let audioNode = null;
let audioCfg = null;
let playGain = null;   // controls speaker playback volume (mute = 0)
let recDest = null;    // MediaStreamDestination feeding the recorder
let audioOn = true;    // speaker playback enabled by default

const WORKLET_SRC = `
class P extends AudioWorkletProcessor {
  constructor() {
    super();
    this.b = new Float32Array(0);
    this.port.onmessage = e => {
      const o = this.b, n = e.data;
      const m = new Float32Array(o.length + n.length);
      m.set(o); m.set(n, o.length);
      this.b = m;
    };
  }
  process(ins, outs) {
    const out = outs[0], ch = out.length, fr = out[0].length;
    const need = fr * ch;
    if (this.b.length >= need) {
      for (let i = 0; i < fr; i++)
        for (let c = 0; c < ch; c++)
          out[c][i] = this.b[i * ch + c];
      this.b = this.b.slice(need);
    }
    return true;
  }
}
registerProcessor("p", P);
`;

function setupAudioConfig(cfg) {
  audioCfg = cfg;
  const btn = document.getElementById("btnAudio");
  btn.style.display = "";
  // The server tells us the subscription state it started this client
  // with: LAN clients get the always-on feed (original behaviour), WAN
  // clients start unsubscribed to keep ~1.5 Mbps of PCM off the uplink.
  audioOn = cfg.on !== false;
  if (audioOn) {
    enableAudioPlayback();   // default-on, no manual click needed
  } else {
    btn.textContent = "\u{1f507} Audio";
    btn.classList.remove("active");
  }
}

// Start speaker playback and reflect the on-state on the button. Browsers block
// AudioContext until a user gesture, so if it's still suspended we resume on the
// first interaction (this is best-effort; recording is unaffected either way).
let _audioArmed = false;
async function enableAudioPlayback() {
  if (!audioCtx) await startAudio();
  if (audioCtx && audioCtx.state === "suspended") {
    try { await audioCtx.resume(); } catch (e) {}
  }
  if (audioCtx && audioCtx.state === "suspended" && !_audioArmed) {
    _audioArmed = true;
    const resume = () => {
      if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
      window.removeEventListener("pointerdown", resume, true);
      window.removeEventListener("keydown", resume, true);
      _audioArmed = false;
    };
    window.addEventListener("pointerdown", resume, true);
    window.addEventListener("keydown", resume, true);
  }
  audioOn = true;
  if (playGain) playGain.gain.value = 1;
  const btn = document.getElementById("btnAudio");
  btn.textContent = "\u{1f50a} Audio";
  btn.classList.add("active");
}

// Build the audio graph once.  audioNode → playGain → speakers (gain gates
// playback) and audioNode → recDest (always full-volume, for the recorder),
// so recording captures audio even when speaker playback is muted.
async function startAudio() {
  if (!audioCfg || audioCtx) return;
  audioCtx = new AudioContext({sampleRate: audioCfg.sampleRate});
  const blob = new Blob([WORKLET_SRC], {type: "application/javascript"});
  const url = URL.createObjectURL(blob);
  await audioCtx.audioWorklet.addModule(url);
  URL.revokeObjectURL(url);
  audioNode = new AudioWorkletNode(audioCtx, "p",
    {outputChannelCount: [audioCfg.channels]});
  playGain = audioCtx.createGain();
  playGain.gain.value = audioOn ? 1 : 0;
  audioNode.connect(playGain);
  playGain.connect(audioCtx.destination);
}

function feedAudio(buf) {
  if (!audioNode) return;
  const i16 = new Int16Array(buf);
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768.0;
  audioNode.port.postMessage(f32, [f32.buffer]);
}

document.getElementById("btnAudio").addEventListener("click", async () => {
  const btn = document.getElementById("btnAudio");
  if (!audioCtx) await startAudio();
  if (audioCtx.state === "suspended") await audioCtx.resume();
  audioOn = !audioOn;
  if (playGain) playGain.gain.value = audioOn ? 1 : 0;
  // Remote viewers toggle the PCM feed itself so a muted stream costs no
  // uplink; while recording, the feed must keep flowing for the recorder
  // (LAN keeps the always-on feed, mute is just a local gain).
  if (!IS_LAN) {
    if (audioOn) wsSend({type: "audio", on: true});
    else if (!recording) wsSend({type: "audio", on: false});
  }
  btn.textContent = (audioOn ? "\u{1f50a}" : "\u{1f507}") + " Audio";
  btn.classList.toggle("active", audioOn);
  container.focus();
});

// --- Screen recording ---
// Records the live canvas (video) plus streamed audio via MediaRecorder, and
// streams the resulting WebM chunks back to the server over the WebSocket.
// The server writes them to its configured recording folder — no save dialog.
let mediaRecorder = null;
let recStream = null;
let recording = false;
let recStartTime = 0;
let recTimer = null;

async function ensureAudioForRecording() {
  if (!audioCfg) return;            // no audio device on this server
  if (!audioCtx) await startAudio();
  if (audioCtx.state === "suspended") await audioCtx.resume();
  if (!recDest) {
    recDest = audioCtx.createMediaStreamDestination();
    audioNode.connect(recDest);    // independent of playGain → always captured
  }
}

function pickMimeType() {
  const prefs = [
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm;codecs=vp8",
    "video/webm",
  ];
  for (const t of prefs) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

async function startRecording() {
  if (recording) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    statusEl.textContent = "Cannot record: not connected";
    return;
  }
  if (!mediaW() || !mediaH()) {
    statusEl.textContent = "Cannot record: no video yet";
    return;
  }
  // Video from the active display (canvas in stream mode, <video> in direct).
  recStream = captureDisplayStream();
  // Mix in audio if available.  Remote viewers may have the PCM feed
  // unsubscribed (muted) — turn it on for the recorder; speaker playback
  // stays muted because playGain is untouched.
  if (!IS_LAN && audioCfg && !audioOn) wsSend({type: "audio", on: true});
  await ensureAudioForRecording();
  if (recDest) {
    for (const t of recDest.stream.getAudioTracks()) recStream.addTrack(t);
  }

  const mimeType = pickMimeType();
  const filename = "recording-" +
    new Date().toISOString().replace(/[:.]/g, "-") + ".webm";
  ws.send(JSON.stringify({type: "rec_start", filename}));

  try {
    mediaRecorder = new MediaRecorder(recStream,
      mimeType ? {mimeType} : undefined);
  } catch (e) {
    statusEl.textContent = "Recording unsupported: " + e;
    ws.send(JSON.stringify({type: "rec_stop"}));
    return;
  }

  mediaRecorder.ondataavailable = (ev) => {
    if (!ev.data || ev.data.size === 0) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ev.data.arrayBuffer().then((buf) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const out = new Uint8Array(buf.byteLength + 1);
      out[0] = 0x10;                // client→server recording-chunk tag
      out.set(new Uint8Array(buf), 1);
      ws.send(out.buffer);
    });
  };
  mediaRecorder.onstop = () => {
    if (ws && ws.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({type: "rec_stop"}));
  };

  mediaRecorder.start(1000);        // flush a chunk every second
  recording = true;
  recStartTime = Date.now();
  const btn = document.getElementById("btnRec");
  btn.classList.add("recording");
  const tick = () => {
    const s = Math.floor((Date.now() - recStartTime) / 1000);
    const mm = String(Math.floor(s / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    btn.textContent = "⏹ " + mm + ":" + ss;
  };
  tick();
  recTimer = setInterval(tick, 500);
}

function stopRecording() {
  if (!recording) return;
  recording = false;
  clearInterval(recTimer);
  recTimer = null;
  // Drop the recorder-only PCM subscription again (remote viewers).
  if (!IS_LAN && audioCfg && !audioOn) wsSend({type: "audio", on: false});
  try { mediaRecorder.stop(); } catch (e) {}
  if (recStream) {
    for (const t of recStream.getVideoTracks()) t.stop();
    recStream = null;
  }
  const btn = document.getElementById("btnRec");
  btn.classList.remove("recording");
  btn.textContent = "⏺ Record";
}

function captureDisplayStream() {
  if (videoMode) {
    if (video.captureStream) return video.captureStream();
    if (video.mozCaptureStream) return video.mozCaptureStream();
  }
  return canvas.captureStream();
}

document.getElementById("btnRec").addEventListener("click", () => {
  if (recording) stopRecording(); else startRecording();
  container.focus();
});

// --- Toolbar show/hide hotkey (Ctrl+Alt+Enter) ---
// The toolbar is hidden by default so it never overlaps the target's top edge.
// Ctrl+Alt+Enter toggles it; the combo is captured at the window level (capture
// phase) and swallowed so it is never forwarded to the target.
function showHint(text) {
  if (text) hint.textContent = text;
  hint.classList.add("show");
  clearTimeout(hintTimer);
  hintTimer = setTimeout(() => hint.classList.remove("show"), 2500);
}
function hideHint() {
  clearTimeout(hintTimer);
  hint.classList.remove("show");
}
function toggleToolbar() {
  toolbarHidden = !toolbarHidden;
  document.body.classList.toggle("tb-hidden", toolbarHidden);
  if (toolbarHidden) {
    toolbar.classList.remove("visible");
    showHint("Ctrl+Alt+Enter: toolbar");
  } else {
    hideHint();
    if (document.fullscreenElement) {   // make it appear despite fs auto-hide
      toolbar.classList.add("visible");
      requestAnimationFrame(_tbCenter);
    }
  }
  updateCanvasSize();
  container.focus();
}

let _hotkeyEnter = false;   // suppress the matching Enter keyup to the target
window.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.altKey && (e.code === "Enter" || e.code === "NumpadEnter")) {
    e.preventDefault();
    e.stopPropagation();
    _hotkeyEnter = true;
    if (!e.repeat) toggleToolbar();
  }
}, true);
window.addEventListener("keyup", (e) => {
  if (_hotkeyEnter && (e.code === "Enter" || e.code === "NumpadEnter")) {
    e.preventDefault();
    e.stopPropagation();
    _hotkeyEnter = false;
  }
}, true);

// --- Direct (native) video mode ---
// The browser opens the capture card itself (getUserMedia) and renders a real
// <video>, matching the official app's smoothness. Input keeps flowing over the
// WebSocket. The server releases the capture device while direct mode is on.
function isWebcamLabel(l) {
  l = (l || "").toLowerCase();
  return ["webcam", "camera", "ir camera", "facetime", "front", "rear"]
    .some((k) => l.includes(k));
}

function openCapture(deviceId) {
  const v = {width: {ideal: 1920}, height: {ideal: 1280}, frameRate: {ideal: 60}};
  if (deviceId) v.deviceId = {exact: deviceId};
  return navigator.mediaDevices.getUserMedia({video: v, audio: false});
}

// The server needs a moment to release the capture device; retry while it's
// still busy (but never retry a denied-permission error).
async function openCaptureRetry(deviceId, tries) {
  let lastErr;
  for (let i = 0; i < tries; i++) {
    try { return await openCapture(deviceId); }
    catch (e) {
      if (e && e.name === "NotAllowedError") throw e;
      lastErr = e;
      await new Promise((r) => setTimeout(r, 300));
    }
  }
  throw lastErr;
}

async function findCaptureDeviceId() {
  try {
    const devs = await navigator.mediaDevices.enumerateDevices();
    const cams = devs.filter((d) => d.kind === "videoinput" && d.label);
    if (serverCaptureLabel) {
      const m = cams.find((d) => d.label.includes(serverCaptureLabel) ||
                                 serverCaptureLabel.includes(d.label));
      if (m) return m.deviceId;
    }
    const nonCam = cams.find((d) => !isWebcamLabel(d.label));
    return nonCam ? nonCam.deviceId : null;
  } catch (e) { return null; }
}

async function enableDirect() {
  const btn = document.getElementById("btnDirect");
  btn.disabled = true;
  statusEl.textContent = "Starting direct video…";
  wsSend({type: "stream", on: false});   // ask server to release the device
  try {
    // First open (any device) to obtain permission, then refine by label.
    let stream = await openCaptureRetry(null, 15);
    const wantId = await findCaptureDeviceId();
    const curId = stream.getVideoTracks()[0] &&
                  stream.getVideoTracks()[0].getSettings().deviceId;
    if (wantId && curId && wantId !== curId) {
      stream.getTracks().forEach((t) => t.stop());
      stream = await openCaptureRetry(wantId, 15);
    }
    directStream = stream;
    video.srcObject = stream;
    await video.play().catch(() => {});
    videoMode = true;
    canvas.style.display = "none";
    video.style.display = "block";
    updateCanvasSize();
    btn.classList.add("active");
    btn.textContent = "Direct ●";
    fpsEl.textContent = "native";
    statusEl.textContent = "Direct video (native)";
  } catch (e) {
    videoMode = false;
    wsSend({type: "stream", on: true});  // resume server stream on failure
    const emsg = "Direct failed: " + (e && e.message ? e.message : e);
    statusEl.textContent = emsg;
    if (toolbarHidden) showHint(emsg);
  }
  btn.disabled = false;
  container.focus();
}

function disableDirect() {
  const btn = document.getElementById("btnDirect");
  videoMode = false;
  if (directStream) {
    directStream.getTracks().forEach((t) => t.stop());
    directStream = null;
  }
  video.srcObject = null;
  video.style.display = "none";
  canvas.style.display = "block";
  btn.classList.remove("active");
  btn.textContent = "Direct";
  // Give the browser a moment to fully release the device before the server
  // reopens it for the JPEG stream.
  setTimeout(() => wsSend({type: "stream", on: true}), 400);
  updateCanvasSize();
  statusEl.textContent = "Connected";
  container.focus();
}

document.getElementById("btnDirect").addEventListener("click", () => {
  if (directStream) { disableDirect(); }
  else {
    if (rtcMode) disableRtc();   // the two <video> modes are exclusive
    enableDirect();
  }
});

// --- WebRTC (H264) mode ---
// The server encodes the shared capture as H.264 and streams it over a
// loopback RTCPeerConnection. Signaling is non-trickle over the WebSocket:
// send a complete offer (ICE gathering finished), get a complete answer.
function iceComplete(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((res) => {
    const check = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", check);
        res();
      }
    };
    pc.addEventListener("icegatheringstatechange", check);
  });
}

// STUN is only useful when the viewer reaches the server across the internet
// (NAT traversal for the H264 media path; the server side defaults to the
// same STUN server via aiortc).  Private/local hosts skip it so offline LANs
// don't sit out the STUN timeout during ICE gathering.
function isPrivateHost(h) {
  h = (h || "").replace(/^\[|\]$/g, "").toLowerCase();
  if (h === "localhost" || h === "::1" || h.endsWith(".local")) return true;
  if (/^127\./.test(h) || /^10\./.test(h) || /^192\.168\./.test(h)) return true;
  if (/^172\.(1[6-9]|2[0-9]|3[01])\./.test(h)) return true;
  if (/^169\.254\./.test(h) || /^fe80:/.test(h) || /^f[cd]/.test(h)) return true;
  return false;
}
const RTC_CONFIG = isPrivateHost(location.hostname) ? {} :
  {iceServers: [{urls: "stun:stun.l.google.com:19302"}]};

async function enableRtc() {
  if (rtcMode) return;
  const btn = document.getElementById("btnRtc");
  if (btn.disabled) return;
  btn.disabled = true;
  if (directStream) disableDirect();   // hand the device back to the server
  statusEl.textContent = "Starting H.264 stream…";
  _rtcGen++;                           // new negotiation generation
  try {
    rtcPc = new RTCPeerConnection(RTC_CONFIG);
    const pc = rtcPc;
    pc.addTransceiver("video", {direction: "recvonly"});
    // Generous timeout: on a cold server the first offer can sit queued
    // behind the capture-device open (~tens of seconds on MSMF).
    const streamReady = new Promise((res, rej) => {
      _rtcWait = {res, rej};
      setTimeout(() => rej(new Error("timed out")), 30000);
    });
    pc.ontrack = (ev) => {
      // Minimise the receive-side buffer on loopback/LAN links only.
      // Across the internet, jitter is real: forcing a zero buffer turns
      // every delivery wobble into a visible stutter, so remote viewers
      // keep the browser's adaptive jitter buffer.
      try {
        if (IS_LAN) {
          if ("jitterBufferTarget" in ev.receiver) ev.receiver.jitterBufferTarget = 0;
          if ("playoutDelayHint" in ev.receiver) ev.receiver.playoutDelayHint = 0;
        }
      } catch (e) {}
      video.srcObject = (ev.streams && ev.streams[0])
        ? ev.streams[0] : new MediaStream([ev.track]);
      const ok = () => { if (_rtcWait) _rtcWait.res(); };
      if (video.readyState >= 1) ok();
      else video.addEventListener("loadedmetadata", ok, {once: true});
    };
    pc.onconnectionstatechange = () => {
      if (pc !== rtcPc) return;
      const st = pc.connectionState;
      if (rtcMode && (st === "failed" || st === "disconnected" || st === "closed")) {
        const emsg = "H.264 stream lost (" + st + ")";
        disableRtc();
        statusEl.textContent = emsg;
        if (toolbarHidden) showHint(emsg);
      }
    };
    await pc.setLocalDescription(await pc.createOffer());
    await iceComplete(pc);
    // The quality preset rides in the offer: "auto" lets the server pick
    // (LAN: configured fps/bitrate; WAN: 30 fps / 4 Mbps start with free
    // REMB adaptation), explicit presets set the cap/fps directly.
    const offerMsg = {type: "webrtc_offer", sdp: pc.localDescription.sdp,
                      gen: _rtcGen};
    const preset = document.getElementById("rtcQuality").value;
    if (preset !== "auto") {
      const parts = preset.split("/");
      offerMsg.bitrate = parseInt(parts[0], 10);
      offerMsg.fps = parseInt(parts[1], 10);
    }
    wsSend(offerMsg);
    await streamReady;
    _rtcWait = null;
    await video.play().catch(() => {});
    rtcMode = true;
    videoMode = true;
    canvas.style.display = "none";
    video.style.display = "block";
    updateCanvasSize();
    btn.classList.add("active");
    btn.textContent = "H264 ●";
    fpsEl.textContent = "h264";
    statusEl.textContent = "H.264 (WebRTC)";
  } catch (e) {
    _rtcWait = null;
    if (rtcPc) { try { rtcPc.close(); } catch (e2) {} rtcPc = null; }
    wsSend({type: "webrtc_stop"});   // server resumes the JPEG stream
    const emsg = "H264 failed: " + (e && e.message ? e.message : e);
    statusEl.textContent = emsg;
    if (toolbarHidden) showHint(emsg);
  }
  btn.disabled = false;
  container.focus();
}

function disableRtc() {
  const btn = document.getElementById("btnRtc");
  rtcMode = false;
  _rtcWait = null;
  _rtcGen++;                           // invalidate any in-flight answer
  if (rtcPc) { try { rtcPc.close(); } catch (e) {} rtcPc = null; }
  wsSend({type: "webrtc_stop"});     // server resumes the JPEG stream
  if (!directStream) {
    videoMode = false;
    video.srcObject = null;
    video.style.display = "none";
    canvas.style.display = "block";
    updateCanvasSize();
    statusEl.textContent = "Connected";
  }
  btn.classList.remove("active");
  btn.textContent = "H264";
  container.focus();
}

function restartRtc() {
  // Drop the local pc without touching the display mode, then renegotiate.
  rtcMode = false;
  _rtcWait = null;
  _rtcGen++;                           // invalidate any in-flight answer
  if (rtcPc) { try { rtcPc.close(); } catch (e) {} rtcPc = null; }
  enableRtc();
}

document.getElementById("btnRtc").addEventListener("click", () => {
  if (rtcMode) disableRtc(); else enableRtc();
});

document.getElementById("rtcQuality").addEventListener("change", () => {
  if (rtcMode) restartRtc();   // renegotiate with the new preset
  container.focus();
});

// --- PWA ---
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");

// --- Start ---
document.body.classList.add("tb-hidden");   // toolbar hidden until Ctrl+Alt+Enter
updateCursor();                             // apply default cursor visibility
showHint("Ctrl+Alt+Enter: toolbar");
container.focus();
connect();

// ?rtc=1 auto-starts the low-latency H.264 stream once the socket is up,
// retrying while the server is still warming up (cold device open).
if (new URLSearchParams(location.search).has("rtc")) {
  let autoRtcTries = 0;
  const autoRtc = async () => {
    if (rtcMode || autoRtcTries >= 8) return;
    if (document.getElementById("btnRtc").disabled) return;  // no aiortc
    if (ws && ws.readyState === WebSocket.OPEN) {
      autoRtcTries++;
      await enableRtc();
      if (!rtcMode) setTimeout(autoRtc, 4000);
    } else {
      setTimeout(autoRtc, 500);
    }
  };
  setTimeout(autoRtc, 500);
}
</script>
</body>
</html>"""

# Short fingerprint of the embedded frontend. Sent to each client on connect so
# an already-open viewer can auto-reload itself after the server is restarted
# with changed HTML/JS — otherwise the tab silently reconnects its WebSocket and
# keeps running the OLD code, making edits look like they "didn't take effect".
# It only changes when _VIEWER_HTML changes, so a plain restart never forces a
# needless reload.
_BUILD_ID = hashlib.md5(_VIEWER_HTML.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# WebViewerServer
# ---------------------------------------------------------------------------

class WebViewerServer:
    """WebSocket server that streams JPEG frames and accepts input events.

    Uses a single port for both HTTP (HTML delivery via process_request)
    and WebSocket (video + input).
    """

    def __init__(self, hardware, config, audio: AudioCapture | None = None):
        """
        Args:
            hardware: KvmHardware instance.
            config: Config instance with web_port, web_fps, web_quality.
            audio: Optional shared AudioCapture instance.
        """
        self._hw = hardware
        self._config = config
        self._server = None
        self._clients: set = set()
        self._audio = audio
        # Number of clients currently consuming the server JPEG stream.  The
        # capture device is only held while this is > 0, so a client switching
        # to "direct" (native getUserMedia) video frees the device for the
        # browser to open it.
        self._stream_count = 0
        # Serialises the (slow, backgrounded) device open/close operations so
        # a release can never interleave with an open still in flight.
        self._device_lock = asyncio.Lock()
        self._cap_label: str | None = None  # cached capture-device name
        # Auth state (only used when config.web_password is set): reconnect
        # tokens issued after a successful password login, and per-IP failed
        # attempt throttling.  Both live in memory only, so a server restart
        # invalidates every session.
        self._auth_tokens: set[str] = set()
        self._auth_fails: dict[str, tuple[int, float]] = {}

    def _map_mouse_abs(self, x: int, y: int) -> tuple[int, int]:
        """Apply target-specific absolute HID axis mapping."""
        x = max(0, min(4095, int(x)))
        y = max(0, min(4095, int(y)))
        if self._config.mouse_invert_x:
            x = 4095 - x
        if self._config.mouse_invert_y:
            y = 4095 - y
        return x, y

    async def start(self):
        host = self._config.web_host
        port = self._config.web_port
        ssl_ctx = self._build_ssl_context()
        self._server = await websockets.serve(
            self._handle_client,
            host,
            port,
            process_request=self._process_http,
            ssl=ssl_ctx,
        )
        scheme = "https" if ssl_ctx else "http"
        auth = " (password required)" if self._config.web_password else ""
        logger.info(f"Web viewer listening on {scheme}://{host}:{port}{auth}")
        if host not in ("127.0.0.1", "localhost", "::1"):
            if not self._config.web_password:
                logger.warning(
                    "Web viewer is reachable from the network WITHOUT a "
                    "password — anyone who can connect controls the target. "
                    "Set --web-password / SHKVM_WEB_PASSWORD.")
            elif ssl_ctx is None:
                logger.warning(
                    "Web viewer password will travel unencrypted (no TLS). "
                    "Use --web-tls-cert/--web-tls-key or terminate TLS at a "
                    "reverse proxy / tunnel.")

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """Build the TLS context from config, or None for plain HTTP."""
        cert = self._config.web_tls_cert
        key = self._config.web_tls_key
        if not cert and not key:
            return None
        if not (cert and key):
            raise ValueError(
                "--web-tls-cert and --web-tls-key must be given together")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        return ctx

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # Pre-encoded PWA assets (immutable after module load)
    _pwa_routes: dict[str, tuple[str, bytes]] = {
        "/manifest.json": ("application/manifest+json",
                           _MANIFEST_JSON.encode()),
        "/sw.js": ("application/javascript; charset=utf-8",
                   _SW_JS.encode()),
        "/icon.svg": ("image/svg+xml", _ICON_SVG.encode()),
    }

    async def _process_http(self, connection, request):
        """Serve the HTML viewer and PWA assets for HTTP requests."""
        if request.path == "/ws":
            # Stash User-Agent for logging when _handle_client runs
            ua = request.headers.get("User-Agent", "")
            connection._kvm_user_agent = ua
            return None  # let WebSocket handshake proceed

        pwa = self._pwa_routes.get(request.path)
        if pwa is not None:
            content_type, body = pwa
            return Response(
                200, "OK",
                websockets.Headers({
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-cache",
                }),
                body,
            )

        html_bytes = _VIEWER_HTML.encode("utf-8")
        return Response(
            200, "OK",
            websockets.Headers({
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(html_bytes)),
                "Cache-Control": "no-cache",
            }),
            html_bytes,
        )

    def _client_is_wan(self, ip: str) -> bool:
        """Whether this client connects across the public internet.

        WAN clients get the adaptive stream treatment; LAN/loopback clients
        keep the original latency/quality tuning.  Separate method so tests
        can override the classification.
        """
        return not _is_private_address(ip)

    async def _handle_client(self, ws):
        """Handle a single WebSocket client: stream frames + process input."""
        self._clients.add(ws)
        addr = ws.remote_address
        ip = f"{addr[0]}:{addr[1]}" if addr else "unknown"
        wan = self._client_is_wan(addr[0]) if addr else True
        ua = getattr(ws, "_kvm_user_agent", "")
        ua_short = ua[:120] + "…" if len(ua) > 120 else ua
        msg = (f"Web client connected from {ip} "
               f"({'WAN' if wan else 'LAN'}, {len(self._clients)} total)")
        if ua_short:
            msg += f"  UA: {ua_short}"
        logger.info(msg)

        # Disable Nagle so small writes (input echoes, audio chunks, control
        # messages) aren't batched behind an unacked segment — on a WAN RTT
        # that batching is a visible latency wobble.  websockets' asyncio
        # implementation does not set this itself.
        try:
            sock = ws.transport.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

        # Per-client stream state.  "event" gates _send_frames so the client
        # can pause the server stream (e.g. when using direct/native video).
        # "webrtc" pauses JPEG frames while the same frames flow over a
        # WebRTC peer connection instead (device stays with the server).
        # "wan" selects the adaptive JPEG stream + on-demand audio;
        # "inflight"/"ack_event" are its frame-credit bookkeeping and
        # "audio_queue"/"audio_event" the dynamic audio subscription.
        state = {"stream": False, "event": asyncio.Event(),
                 "webrtc": False, "rtc_session": None,
                 "wan": wan,
                 "inflight": 0, "ack_event": asyncio.Event(),
                 "audio_queue": None, "audio_event": asyncio.Event()}

        loop = asyncio.get_running_loop()
        try:
            # Auth gate: when a password is configured, nothing happens (no
            # frames, no input, no capture-device open) until this client
            # authenticates over the WebSocket.
            if self._config.web_password:
                if not await self._authenticate(ws, addr[0] if addr else "?"):
                    return

            # Client starts in server-stream mode (acquires the capture device).
            await self._acquire_stream(state)

            # Frontend fingerprint: lets an open viewer auto-reload when the
            # server has been restarted with changed HTML/JS.  "webrtc" tells
            # the client whether the H264 (WebRTC) mode is available;
            # "adaptive" tells a WAN client to ack received frames so the
            # credit-based stream can pace itself.
            await ws.send(json.dumps({
                "type": "hello", "build": _BUILD_ID,
                "webrtc": _WEBRTC_AVAILABLE,
                "adaptive": wan,
            }))

            # Tell the client which capture device the server uses, so direct
            # mode can pick the matching device via getUserMedia.
            if self._cap_label is None:
                self._cap_label = await loop.run_in_executor(
                    None, self._capture_label)
            await ws.send(json.dumps({
                "type": "capture_device", "label": self._cap_label,
            }))

            # Notify client about audio availability.  LAN clients are
            # subscribed immediately (original always-on behaviour); WAN
            # clients start unsubscribed — uncompressed PCM is ~1.5 Mbps
            # that would compete with video on the uplink — and opt in
            # with an {"type": "audio", "on": true} message.
            if self._audio is not None:
                if not wan:
                    self._set_audio(state, True)
                await ws.send(json.dumps({
                    "type": "audio_config",
                    "sampleRate": self._audio.samplerate,
                    "channels": self._audio.channels,
                    "on": not wan,
                }))

            tasks = [
                asyncio.create_task(self._send_frames(ws, state)),
                asyncio.create_task(self._recv_input(ws, state)),
            ]
            if self._audio is not None:
                tasks.append(asyncio.create_task(
                    self._send_audio(ws, state)))
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            for t in done:
                # Retrieve the exception (e.g. ConnectionClosedError from an
                # abrupt disconnect) so asyncio doesn't log "Task exception
                # was never retrieved" for every dead client.
                if not t.cancelled():
                    t.exception()
        finally:
            self._set_audio(state, False)
            self._clients.discard(ws)
            await self._stop_webrtc(state)
            await self._release_stream(state)
            logger.info(f"Web client disconnected from {ip} ({len(self._clients)} total)")

    # Auth throttle: after _AUTH_MAX_FAILS consecutive wrong passwords from
    # one IP, that IP is locked out for _AUTH_LOCKOUT_S seconds.  A client
    # that never authenticates is dropped after _AUTH_TIMEOUT_S.
    _AUTH_MAX_FAILS = 5
    _AUTH_LOCKOUT_S = 30.0
    _AUTH_TIMEOUT_S = 300.0
    _AUTH_FAIL_DELAY_S = 1.5

    async def _authenticate(self, ws, ip: str) -> bool:
        """WebSocket-level auth gate; True once the client is authenticated.

        Protocol (client ↔ server, JSON text messages):
            ← {"type": "auth_required"}
            → {"type": "auth", "password": "..."}   or  {"token": "..."}
            ← {"type": "auth_ok", "token": "..."}   on success
            ← {"type": "auth_failed", "error": "...", "expired"?: true}

        A successful password login issues a random reconnect token so the
        browser can re-authenticate silently after a WebSocket reconnect or
        tab reload.  Tokens live in server memory only.  Every other message
        type is dropped until authentication succeeds.
        """
        loop = asyncio.get_running_loop()
        password = self._config.web_password
        try:
            fails, locked_until = self._auth_fails.get(ip, (0, 0.0))
            if time.monotonic() < locked_until:
                await ws.send(json.dumps({
                    "type": "auth_failed",
                    "error": "too many attempts — try again later",
                }))
                return False

            await ws.send(json.dumps({"type": "auth_required"}))
            deadline = loop.time() + self._AUTH_TIMEOUT_S
            while True:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    return False
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout)
                except asyncio.TimeoutError:
                    return False
                if not isinstance(message, str):
                    continue
                try:
                    ev = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "auth":
                    continue

                token = ev.get("token")
                if token:
                    if isinstance(token, str) and token in self._auth_tokens:
                        await ws.send(json.dumps(
                            {"type": "auth_ok", "token": token}))
                        return True
                    # Stale token (server restarted): tell the client to fall
                    # back to the password prompt.  Not a brute-force signal.
                    await ws.send(json.dumps({
                        "type": "auth_failed", "expired": True,
                        "error": "session expired",
                    }))
                    continue

                attempt = ev.get("password", "")
                if isinstance(attempt, str) and hmac.compare_digest(
                        attempt.encode(), password.encode()):
                    self._auth_fails.pop(ip, None)
                    token = secrets.token_urlsafe(24)
                    self._auth_tokens.add(token)
                    while len(self._auth_tokens) > 256:
                        self._auth_tokens.pop()
                    await ws.send(json.dumps(
                        {"type": "auth_ok", "token": token}))
                    return True

                # Wrong password: throttle and either lock out or let the
                # client retry on the same connection.
                fails += 1
                if len(self._auth_fails) > 1024:
                    self._auth_fails.clear()
                locked = fails >= self._AUTH_MAX_FAILS
                if locked:
                    self._auth_fails[ip] = (
                        0, time.monotonic() + self._AUTH_LOCKOUT_S)
                else:
                    self._auth_fails[ip] = (fails, 0.0)
                logger.warning(f"Web viewer auth failure from {ip}")
                await asyncio.sleep(self._AUTH_FAIL_DELAY_S)
                await ws.send(json.dumps({
                    "type": "auth_failed",
                    "error": ("too many attempts — try again later"
                              if locked else "wrong password"),
                }))
                if locked:
                    return False
        except websockets.ConnectionClosed:
            return False

    @staticmethod
    def _update_frame_gate(state: dict):
        """(Re)compute the _send_frames gate for *state*.

        JPEG frames flow only while the client consumes the server stream
        AND is not receiving the same frames over WebRTC instead — WebRTC
        keeps the device with the server but makes the JPEG encode per
        frame pure waste.
        """
        if state["stream"] and not state["webrtc"]:
            state["event"].set()
        else:
            state["event"].clear()

    async def _acquire_stream(self, state: dict):
        """Mark *state* as consuming the server stream; open device if first.

        The device open runs in a background task: an MSMF open can take
        tens of seconds (measured ~28 s right after a browser hands the
        device back from direct mode), and every caller of this method sits
        on the client's message loop, where such a stall freezes input and
        delays WebRTC signaling past the client's timeout.  Frames simply
        start flowing when the device is ready.
        """
        if state["stream"]:
            return
        state["stream"] = True
        self._update_frame_gate(state)
        self._stream_count += 1
        if self._stream_count == 1:
            capture = self._hw.get_capture()
            loop = asyncio.get_running_loop()

            async def _open():
                async with self._device_lock:
                    if self._stream_count == 0:
                        return  # released again before the open got the lock
                    # Non-fatal: the device may still be held by a browser in
                    # direct mode (e.g. right after a reconnect). The capture
                    # loop retries opening, so nothing is torn down here.
                    try:
                        await loop.run_in_executor(
                            None, capture.start_capture_thread)
                        logger.info("Capture thread started (stream active)")
                    except Exception as e:
                        logger.warning(f"Capture start failed (device busy?): {e}")

            asyncio.create_task(_open())

    async def _release_stream(self, state: dict):
        """Mark *state* as no longer streaming; release device if last.

        Like the open, the close is backgrounded and serialised behind
        ``_device_lock`` so it can never interleave with an in-flight open.
        """
        if not state["stream"]:
            return
        state["stream"] = False
        self._update_frame_gate(state)
        self._stream_count -= 1
        if self._stream_count == 0:
            capture = self._hw.get_capture()
            loop = asyncio.get_running_loop()

            async def _close_device():
                async with self._device_lock:
                    if self._stream_count > 0:
                        return  # re-acquired while the close was pending
                    # Fully release the device (close, not just stop the
                    # thread) so a browser in direct mode can open it via
                    # getUserMedia. stop alone leaves the OpenCV VideoCapture
                    # holding the device → "Device in use".
                    await loop.run_in_executor(None, capture.close)
                    logger.info("Capture device released (no active streams)")

            asyncio.create_task(_close_device())

    async def _start_webrtc(self, ws, state: dict, ev: dict):
        """Negotiate a WebRTC session streaming the capture to this client.

        The server keeps the capture device open (unlike direct mode), so
        API/OCR/MCP consumers stay functional; only this client's JPEG
        WebSocket stream is paused to avoid encoding the frames twice.

        The client's ``gen`` value is echoed back in the answer/error so it
        can discard replies that belong to an offer it has already abandoned
        (a late answer applied to a newer RTCPeerConnection corrupts it).
        """
        sdp = ev.get("sdp", "")
        gen = ev.get("gen")
        try:
            from ._webrtc import WebRtcSession
        except ImportError:
            await ws.send(json.dumps({
                "type": "webrtc_error", "gen": gen,
                "error": "aiortc is not installed on the server "
                         "(pip install serial-hid-kvm[webrtc])",
            }))
            return

        # The client may come straight from direct mode, which released the
        # device — make sure the server owns it again.
        await self._acquire_stream(state)

        # Re-offer replaces any previous session (e.g. after a WS reconnect).
        # Close the old one in the background — see _stop_webrtc.
        old = state.get("rtc_session")
        state["rtc_session"] = None
        if old is not None:
            asyncio.create_task(old.close())

        session = None

        def on_closed():
            # Runs when the peer connection dies (browser gone, ICE failure).
            # Resume the JPEG stream so the client is never left frameless.
            if state.get("rtc_session") is session:
                state["rtc_session"] = None
                state["webrtc"] = False
                self._update_frame_gate(state)

        fps, cap, start, floor = self._webrtc_params(
            self._config, state["wan"], ev)
        session = WebRtcSession(
            self._hw.get_capture(),
            fps=fps,
            bitrate=cap,
            start_bitrate=start,
            min_bitrate=floor,
            on_closed=on_closed,
        )
        try:
            answer = await session.handle_offer(sdp)
        except Exception as e:
            logger.warning(f"WebRTC negotiation failed: {e}")
            await session.close()
            await ws.send(json.dumps({
                "type": "webrtc_error", "gen": gen, "error": str(e),
            }))
            return

        state["rtc_session"] = session
        state["webrtc"] = True
        self._update_frame_gate(state)
        await ws.send(json.dumps({
            "type": "webrtc_answer", "sdp": answer, "gen": gen,
        }))
        logger.info(
            f"WebRTC session established "
            f"({'WAN' if state['wan'] else 'LAN'}, {fps} fps, "
            f"start {start // 1000} kbps, cap {cap // 1000} kbps)")

    @staticmethod
    def _webrtc_params(config, wan: bool, ev: dict
                       ) -> tuple[int, int, int, int | None]:
        """Resolve ``(fps, bitrate_cap, start_bitrate, min_bitrate)`` for an offer.

        LAN: configured fps/bitrate with a floor at half the cap (REMB dips
        would blur desktop text).  WAN: 30 fps / 4 Mbps start with NO floor,
        so REMB congestion feedback can settle wherever the uplink allows.
        An explicit client preset ("bitrate"/"fps" in the offer message,
        from the toolbar quality selector) overrides the defaults but is
        clamped to the configured maxima.
        """
        fps_max = config.webrtc_fps
        cap_max = config.webrtc_bitrate

        req_fps = ev.get("fps")
        req_bitrate = ev.get("bitrate")

        if isinstance(req_fps, (int, float)) and req_fps > 0:
            fps = int(max(5, min(req_fps, fps_max)))
        else:
            fps = min(fps_max, _WAN_RTC_FPS_CAP) if wan else fps_max

        if isinstance(req_bitrate, (int, float)) and req_bitrate > 0:
            cap = int(max(200_000, min(req_bitrate, cap_max)))
        else:
            cap = cap_max

        if wan:
            start = min(_WAN_RTC_START, cap)
            floor = None
        else:
            start = cap
            floor = cap // 2
        return fps, cap, start, floor

    async def _stop_webrtc(self, state: dict):
        """Close this client's WebRTC session (if any) and resume JPEG.

        The actual peer-connection teardown is backgrounded: aiortc's
        ``pc.close()`` can take a while (DTLS shutdown against a browser pc
        that is already gone), and awaiting it here would stall the input
        loop — including a follow-up ``webrtc_offer`` from the same client.
        """
        session = state.get("rtc_session")
        state["rtc_session"] = None
        state["webrtc"] = False
        self._update_frame_gate(state)
        if session is not None:
            async def _close():
                t0 = asyncio.get_running_loop().time()
                await session.close()
                dt = asyncio.get_running_loop().time() - t0
                logger.info(f"WebRTC session closed ({dt:.2f}s)")
            asyncio.create_task(_close())

    def _capture_label(self) -> str:
        """Best-effort human-readable name of the configured capture device.

        Lets the browser match the same device via ``getUserMedia`` in direct
        mode.  Runs device enumeration (may shell out on Windows) so callers
        should invoke it off the event loop and cache the result.
        """
        try:
            from .capture import list_capture_devices, _is_webcam_name
            devices = list_capture_devices()
            dev = self._config.capture_device
            if dev is not None:
                dev_str = str(dev)
                for d in devices:
                    if d["device"] == dev_str:
                        return d["name"]
            # Auto-detect: first non-webcam, else first device.
            for d in devices:
                if not _is_webcam_name(d["name"]):
                    return d["name"]
            if devices:
                return devices[0]["name"]
        except Exception as e:
            logger.debug(f"Capture label lookup failed: {e}")
        return ""

    async def _send_frames(self, ws, state: dict):
        """Stream JPEG frames: fixed-pace for LAN, adaptive for WAN."""
        if state["wan"]:
            await self._send_frames_wan(ws, state)
        else:
            await self._send_frames_lan(ws, state)

    async def _send_frames_lan(self, ws, state: dict):
        """Stream JPEG frames to the client at configured FPS (original)."""
        fps = self._config.web_fps
        quality = self._config.web_quality
        interval = 1.0 / fps
        capture = self._hw.get_capture()
        loop = asyncio.get_running_loop()

        # Pace against a moving deadline so encode time is absorbed into the
        # frame interval instead of added on top of it (keeps the real frame
        # rate close to the configured target).
        next_t = loop.time()
        while True:
            # Pause while the client is in direct mode (device released).
            if not state["event"].is_set():
                await state["event"].wait()
                next_t = loop.time()
            result = await loop.run_in_executor(
                None, capture.get_frame_jpeg, quality
            )
            if result is not None:
                jpeg_bytes, _w, _h = result
                try:
                    await ws.send(b"\x01" + jpeg_bytes)
                except websockets.ConnectionClosed:
                    break
            next_t += interval
            delay = next_t - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Fell behind; reset the schedule to avoid a catch-up burst.
                next_t = loop.time()

    async def _send_frames_wan(self, ws, state: dict):
        """Credit-paced, quality-adaptive JPEG stream for WAN clients.

        Two things make the fixed-pace LAN loop miserable across the
        internet: frames pile up in TCP buffers (bufferbloat — the client
        drops stale frames, but only after they crossed the link), and the
        configured fps/quality assume LAN bandwidth.

        - Credits: at most ``_WAN_CREDITS`` frames are unacknowledged at a
          time; the client acks every received frame (``hello.adaptive``
          told it to).  In-flight data stays bounded regardless of OS
          socket buffering, so added latency is ~one frame transfer and the
          send rate automatically tracks what the link actually delivers.
        - Quality: achieved fps over a sliding window drives the JPEG
          quality down when the link falls behind (cheaper frames preserve
          motion) and back up when it keeps up.
        """
        target_fps = min(self._config.web_fps, _WAN_FPS_CAP)
        interval = 1.0 / target_fps
        q_hi = min(self._config.web_quality, _WAN_QUALITY_CAP)
        quality = min(q_hi, 60)  # modest start; adapts both ways from here
        capture = self._hw.get_capture()
        loop = asyncio.get_running_loop()

        window_start = loop.time()
        window_sent = 0
        next_t = loop.time()
        while True:
            # Pause while the client is in direct/H264 mode.
            if not state["event"].is_set():
                await state["event"].wait()
                next_t = loop.time()

            # Credit gate: wait until the client acked in-flight frames.
            # If acks stop entirely (stalled link, tab in background),
            # reset after a timeout so the stream can never freeze forever.
            credit_deadline = loop.time() + _WAN_ACK_RESET_S
            while state["inflight"] >= _WAN_CREDITS:
                state["ack_event"].clear()
                if state["inflight"] < _WAN_CREDITS:
                    break  # ack raced the clear
                remaining = credit_deadline - loop.time()
                if remaining <= 0:
                    state["inflight"] = 0
                    break
                try:
                    await asyncio.wait_for(
                        state["ack_event"].wait(), remaining)
                except asyncio.TimeoutError:
                    state["inflight"] = 0
                    break

            result = await loop.run_in_executor(
                None, capture.get_frame_jpeg, quality, True)
            if result is not None:
                jpeg_bytes, _w, _h = result
                try:
                    await ws.send(b"\x01" + jpeg_bytes)
                except websockets.ConnectionClosed:
                    break
                state["inflight"] += 1
                window_sent += 1

            # Quality controller: evaluate every ~2 s.
            now = loop.time()
            elapsed = now - window_start
            if elapsed >= 2.0:
                achieved = window_sent / elapsed
                if achieved < target_fps * 0.7 and quality > _WAN_QUALITY_MIN:
                    quality = max(_WAN_QUALITY_MIN, quality - 10)
                    logger.debug(
                        f"WAN stream {achieved:.1f}/{target_fps} fps, "
                        f"quality -> {quality}")
                elif achieved >= target_fps * 0.95 and quality < q_hi:
                    quality = min(q_hi, quality + 5)
                window_start = now
                window_sent = 0

            next_t += interval
            delay = next_t - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_t = loop.time()

    def _set_audio(self, state: dict, on: bool):
        """Subscribe/unsubscribe this client's dynamic audio feed.

        While unsubscribed no PCM is queued for (or sent to) the client at
        all — that's the WAN bandwidth saving, not just a client-side mute.
        LAN clients are subscribed at connect and stay subscribed (their
        mute is a local gain, so recordings keep audio — original
        behaviour); WAN clients toggle via {"type": "audio"} messages.
        """
        if self._audio is None:
            return
        if on and state["audio_queue"] is None:
            state["audio_queue"] = self._audio.subscribe()
            state["audio_event"].set()
        elif not on and state["audio_queue"] is not None:
            q = state["audio_queue"]
            state["audio_queue"] = None
            state["audio_event"].clear()
            self._audio.unsubscribe(q)

    async def _send_audio(self, ws, state: dict):
        """Stream PCM audio chunks to the client while it is subscribed."""
        loop = asyncio.get_running_loop()
        while True:
            audio_queue = state["audio_queue"]
            if audio_queue is None:
                await state["audio_event"].wait()
                continue
            chunk = await loop.run_in_executor(
                None, self._get_audio_chunk, audio_queue
            )
            if chunk is not None and state["audio_queue"] is audio_queue:
                try:
                    await ws.send(b"\x02" + chunk)
                except websockets.ConnectionClosed:
                    break

    @staticmethod
    def _get_audio_chunk(q: queue.Queue) -> bytes | None:
        try:
            return q.get(timeout=0.1)
        except queue.Empty:
            return None

    def _open_recording(self, filename: str):
        """Open a recording file in the configured directory for writing.

        The client-supplied *filename* is reduced to a safe basename inside
        ``config.recording_dir`` and forced to a ``.webm`` extension, so a
        malicious or odd name can never escape the recording folder.

        Returns:
            ``(file_handle, Path)`` — caller is responsible for closing.
        """
        rec_dir = Path(self._config.recording_dir).expanduser()
        rec_dir.mkdir(parents=True, exist_ok=True)
        name = Path(filename or "").name  # drop any directory components
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        if not name or name in (".", ".."):
            name = "recording-" + time.strftime("%Y%m%d-%H%M%S")
        if not name.lower().endswith(".webm"):
            name += ".webm"
        path = rec_dir / name
        return open(path, "wb"), path

    async def _recv_input(self, ws, state: dict):
        """Receive and process input events from the client.

        Serial writes are decoupled from the WebSocket receive loop by a
        background *sender* task.  This prevents a flood of mouse-move events
        from backing up behind the (relatively slow) serial port and making
        the target cursor lag further and further behind: consecutive mouse
        moves are coalesced so only the most recent position is ever sent,
        while clicks, scrolls and keystrokes stay strictly ordered.
        """
        held_modifiers = 0
        held_keys: set[int] = set()  # currently pressed non-modifier HID keycodes
        ch9329 = self._hw.get_ch9329()
        loop = asyncio.get_running_loop()
        rec_file = None  # open file handle while a recording is in progress

        # (is_move, packet) entries.  Consecutive moves collapse to the latest.
        send_queue: list[tuple[bool, bytes]] = []
        wake = asyncio.Event()
        stopping = False

        async def sender():
            while True:
                await wake.wait()
                wake.clear()
                while send_queue:
                    _is_move, pkt = send_queue.pop(0)
                    await loop.run_in_executor(None, ch9329.send, pkt)
                if stopping:
                    return

        def push(pkt: bytes):
            send_queue.append((False, pkt))
            wake.set()

        def push_move(pkt: bytes):
            # Coalesce with the previous pending move (if no discrete event has
            # been queued since) so stale intermediate positions are dropped.
            if send_queue and send_queue[-1][0]:
                send_queue[-1] = (True, pkt)
            else:
                send_queue.append((True, pkt))
            wake.set()

        sender_task = asyncio.create_task(sender())
        try:
            async for message in ws:
                # Binary frames from the client are screen-recording chunks
                # (tag byte 0x10 + WebM data), written to the open recording.
                if not isinstance(message, str):
                    if (rec_file is not None and len(message) >= 1
                            and message[0] == 0x10):
                        await loop.run_in_executor(
                            None, rec_file.write, message[1:])
                    continue
                try:
                    ev = json.loads(message)
                except json.JSONDecodeError:
                    continue

                ev_type = ev.get("type")

                if ev_type == "stream":
                    # Client toggling the server JPEG stream on/off (direct mode
                    # turns it off so the browser can open the device natively).
                    if ev.get("on"):
                        await self._acquire_stream(state)
                    else:
                        await self._release_stream(state)
                    continue

                elif ev_type == "webrtc_offer":
                    # Client requesting the low-latency H.264 (WebRTC) stream.
                    logger.info("webrtc_offer received")
                    await self._start_webrtc(ws, state, ev)
                    continue

                elif ev_type == "webrtc_stop":
                    logger.info("webrtc_stop received")
                    await self._stop_webrtc(state)
                    continue

                elif ev_type == "frame_ack":
                    # WAN adaptive stream: one in-flight credit returned.
                    if state["inflight"] > 0:
                        state["inflight"] -= 1
                    state["ack_event"].set()
                    continue

                elif ev_type == "audio":
                    # Dynamic audio subscription; while off, no PCM is sent
                    # at all (saves ~1.5 Mbps of uplink for WAN clients).
                    self._set_audio(state, bool(ev.get("on")))
                    continue

                elif ev_type == "rec_start":
                    if rec_file is not None:
                        rec_file.close()
                        rec_file = None
                    try:
                        rec_file, path = self._open_recording(
                            ev.get("filename", ""))
                        logger.info(f"Recording started: {path}")
                    except Exception as e:
                        logger.warning(f"Failed to start recording: {e}")
                        await ws.send(json.dumps(
                            {"type": "rec_error", "error": str(e)}))
                    continue

                elif ev_type == "rec_stop":
                    if rec_file is not None:
                        path = rec_file.name
                        rec_file.close()
                        rec_file = None
                        logger.info(f"Recording saved: {path}")
                        await ws.send(json.dumps(
                            {"type": "rec_saved", "path": str(path)}))
                    continue

                elif ev_type == "keydown":
                    code = ev.get("code", "")
                    # Modifier key?
                    mod_bit = _JS_MOD_BITS.get(code)
                    if mod_bit is not None:
                        held_modifiers |= mod_bit
                        push(build_keyboard_report(held_modifiers, held_keys))
                        continue
                    # Regular key
                    hid = _JS_CODE_TO_HID.get(code)
                    if hid is not None:
                        held_keys.add(hid)
                        push(build_keyboard_report(held_modifiers, held_keys))

                elif ev_type == "keyup":
                    code = ev.get("code", "")
                    mod_bit = _JS_MOD_BITS.get(code)
                    if mod_bit is not None:
                        held_modifiers &= ~mod_bit
                        push(build_keyboard_report(held_modifiers, held_keys))
                        continue
                    # Release key
                    hid = _JS_CODE_TO_HID.get(code)
                    if hid is not None:
                        held_keys.discard(hid)
                    push(build_keyboard_report(held_modifiers, held_keys))

                elif ev_type == "mousemove":
                    x = ev.get("x", 0)
                    y = ev.get("y", 0)
                    buttons = ev.get("buttons", 0)
                    x, y = self._map_mouse_abs(x, y)
                    push_move(build_mouse_abs_packet(buttons, x, y))

                elif ev_type == "mousedown":
                    x = ev.get("x", 0)
                    y = ev.get("y", 0)
                    buttons = ev.get("buttons", 0)
                    x, y = self._map_mouse_abs(x, y)
                    push(build_mouse_abs_packet(buttons, x, y))

                elif ev_type == "mouseup":
                    x = ev.get("x", 0)
                    y = ev.get("y", 0)
                    buttons = ev.get("buttons", 0)
                    x, y = self._map_mouse_abs(x, y)
                    push(build_mouse_abs_packet(buttons, x, y))

                elif ev_type == "scroll":
                    dy = ev.get("deltaY", 0)
                    scroll = max(-127, min(127, int(dy)))
                    push(build_mouse_rel_packet(0, 0, 0, scroll=scroll))

                elif ev_type == "release_all":
                    held_modifiers = 0
                    held_keys.clear()
                    push(build_keyboard_report(0, ()))
                    push(build_mouse_rel_packet(0, 0, 0))
        finally:
            stopping = True
            wake.set()
            try:
                await sender_task
            except Exception:
                pass
            if rec_file is not None:
                try:
                    rec_file.close()
                    logger.info(f"Recording saved (client gone): {rec_file.name}")
                except Exception:
                    pass
