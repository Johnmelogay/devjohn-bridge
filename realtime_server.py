#!/Users/joaomelo/.hermes/tools/python-3.14.7+20260901-darwin-arm64/bin/python3
"""
DEVJOHN REALTIME WHATSAPP-SPEED BRIDGE SERVER
Runs locally on John's MacBook Air (port 8765) + exposes an HTTPS Cloudflare Tunnel.
- Sub-100ms Server-Sent Events (SSE) & JSON state
- Instant Local OpenAI Whisper (`faster-whisper` via Hermes STT) voice command transcription
- Voice notes are treated as direct voice commands to devjohn (not file drops)
- Executes real devjohn Hermes Agent turns (`hermes chat -c phone-bridge`) so John is talking directly to devjohn
- Instant multipart/binary file & screenshot sharing (`~/Desktop/PhoneBridge`)
- Publishes active tunnel URL to `Johnmelogay/devjohn-bridge/state/endpoint.json` for GitHub Pages auto-discovery
"""

import base64
import datetime
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

# Inject Hermes Agent environment for local OpenAI Whisper STT
sys.path.insert(0, "/Users/joaomelo/.hermes/hermes-agent")
os.environ["HERMES_HOME"] = "/Users/joaomelo/.hermes/profiles/devjohn"
try:
    import hermes_bootstrap  # noqa: F401
    from tools.transcription_tools import transcribe_audio
except Exception as e:
    print(f"[WARN] Could not preload transcribe_audio: {e}")
    transcribe_audio = None

HOME = "/Users/joaomelo"
BRIDGE_DIR = "/Users/joaomelo/.hermes/profiles/devjohn/projects/devjohn-bridge"
DESKTOP_INBOX = os.path.join(HOME, "Desktop", "PhoneBridge")
OUTBOX_DIR = os.path.join(DESKTOP_INBOX, "SendToPhone")
UPLOADS_DIR = os.path.join(BRIDGE_DIR, "live_uploads")
STATE_FILE = os.path.join(BRIDGE_DIR, "live_state.json")
HERMES_BIN = "/Users/joaomelo/.hermes/hermes-agent/.hermes/bin/hermes"
REPO = "Johnmelogay/devjohn-bridge"
PORT = 8765

PROJECT_DIRS = [
    os.path.join(HOME, "REPARAÍ"),
    os.path.join(HOME, "TaskParser"),
    os.path.join(HOME, "Zenaide_Simoes"),
    os.path.join(HOME, "balneario-rio-preto-site"),
    os.path.join(HOME, "website_riopreto"),
    os.path.join(HOME, "Calculadora Locação Rio Preto"),
    os.path.join(HOME, "graphic elements extractor"),
]

os.makedirs(DESKTOP_INBOX, exist_ok=True)
os.makedirs(OUTBOX_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

STATE_LOCK = threading.Lock()
SSE_CLIENTS = []
SSE_LOCK = threading.Lock()

LIVE_STATE = {
    "version": 1,
    "messages": [],
    "status": {
        "hostname": "John's MacBook Air",
        "daemonStatus": "Online • Ready",
        "agentBusy": False,
        "agentActivity": "Listening for voice/text commands",
        "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "uptime": "--",
        "battery": "--",
        "projects": [],
        "latestScreen": None,
    },
}


def bump_version():
    LIVE_STATE["version"] = int(time.time() * 1000)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data.get("messages"), list):
                    LIVE_STATE["messages"] = [
                        m for m in data["messages"]
                        if "timed out after 240 seconds" not in (m.get("text") or "")
                    ]
        except Exception:
            pass
    if not LIVE_STATE["messages"]:
        LIVE_STATE["messages"] = [
            {
                "id": "welcome_1",
                "sender": "devjohn",
                "type": "text",
                "text": "Hey John — I'm connected directly to you over the low-latency stream. Tap the mic and talk to me anytime: I transcribe your voice instantly with Whisper and execute your instructions right on your MacBook Air.",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        ]
    bump_version()
    save_state()


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"messages": LIVE_STATE["messages"][-200:]}, f, indent=2)
    except Exception as e:
        print(f"[WARN] save_state error: {e}")


def broadcast_event(event_type="full", payload=None):
    data = json.dumps({
        "type": event_type,
        "payload": payload or LIVE_STATE,
        "version": LIVE_STATE["version"],
        "ts": time.time(),
    })
    pad = ":" + (" " * 2048) + "\n"
    msg = (pad + f"data: {data}\n\n").encode("utf-8")
    with SSE_LOCK:
        dead = []
        for wfile in SSE_CLIENTS:
            try:
                wfile.write(msg)
                wfile.flush()
            except Exception:
                dead.append(wfile)
        for d in dead:
            SSE_CLIENTS.remove(d)


def add_message(msg):
    with STATE_LOCK:
        LIVE_STATE["messages"].append(msg)
        LIVE_STATE["messages"] = LIVE_STATE["messages"][-200:]
        bump_version()
        save_state()
    broadcast_event("full", LIVE_STATE)
    return msg


def update_message(msg_id, **updates):
    with STATE_LOCK:
        for m in LIVE_STATE["messages"]:
            if m.get("id") == msg_id:
                m.update(updates)
                break
        bump_version()
        save_state()
    broadcast_event("full", LIVE_STATE)


def set_agent_status(busy: bool, activity: str):
    with STATE_LOCK:
        LIVE_STATE["status"]["agentBusy"] = busy
        LIVE_STATE["status"]["agentActivity"] = activity
        LIVE_STATE["status"]["daemonStatus"] = f"⚡ {activity}" if busy else "Online • Ready"
        LIVE_STATE["status"]["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        bump_version()
    broadcast_event("full", LIVE_STATE)


def inspect_git_repo(path):
    if not os.path.isdir(path):
        return None
    name = os.path.basename(path.rstrip("/"))
    try:
        branch = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        branch = "unknown"

    try:
        last_commit = subprocess.check_output(
            ["git", "-C", path, "log", "-1", "--pretty=format:%h • %s (%cr)"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        last_commit = "No commits"

    try:
        status_out = subprocess.check_output(
            ["git", "-C", path, "status", "--short"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        changed = [line.strip() for line in status_out.splitlines() if line.strip()]
    except Exception:
        changed = []

    return {
        "name": name,
        "path": path.replace(HOME, "~"),
        "branch": branch,
        "lastCommit": last_commit,
        "dirtyCount": len(changed),
        "changedFiles": changed[:12],
    }


def refresh_telemetry():
    try:
        uptime_raw = subprocess.check_output(["uptime"], text=True).strip()
        uptime_part = uptime_raw.split("up ")[1].split(",")[0].strip() if "up " in uptime_raw else uptime_raw
    except Exception:
        uptime_part = "Active"

    try:
        batt_raw = subprocess.check_output(["pmset", "-g", "batt"], text=True)
        batt_pct = "AC Power"
        for line in batt_raw.splitlines():
            if "%" in line:
                batt_pct = line.split("\t")[-1].split(";")[0].strip()
                break
    except Exception:
        batt_pct = "Unknown"

    projects = []
    for d in PROJECT_DIRS:
        info = inspect_git_repo(d)
        if info:
            projects.append(info)

    with STATE_LOCK:
        LIVE_STATE["status"]["uptime"] = uptime_part
        LIVE_STATE["status"]["battery"] = batt_pct
        LIVE_STATE["status"]["projects"] = projects
        LIVE_STATE["status"]["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    broadcast_event("status", LIVE_STATE["status"])


def capture_both_screens():
    scratch = "/Users/joaomelo/.hermes/profiles/devjohn/cache/scratch"
    os.makedirs(scratch, exist_ok=True)
    s1 = os.path.join(scratch, "screen1.png")
    s2 = os.path.join(scratch, "screen2.png")
    for p in (s1, s2):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    fname = f"screens_{int(time.time())}.jpg"
    dest_path = os.path.join(UPLOADS_DIR, fname)

    subprocess.run(["/usr/sbin/screencapture", "-x", "-D", "1", s1], check=False, timeout=10)
    subprocess.run(["/usr/sbin/screencapture", "-x", "-D", "2", s2], check=False, timeout=10)

    try:
        from PIL import Image, ImageOps
        imgs = [Image.open(p).convert("RGB") for p in (s1, s2) if os.path.exists(p)]
        if len(imgs) == 2:
            # Equal size tiles (1920x1080 each) stacked vertically (one on top of the other)
            tile_w, tile_h = 1920, 1080
            gap = 8
            top_img = ImageOps.pad(imgs[0], (tile_w, tile_h), method=Image.Resampling.LANCZOS, color=(9, 9, 11))
            bot_img = ImageOps.pad(imgs[1], (tile_w, tile_h), method=Image.Resampling.LANCZOS, color=(9, 9, 11))
            canvas = Image.new("RGB", (tile_w, tile_h * 2 + gap), (24, 24, 28))
            canvas.paste(top_img, (0, 0))
            canvas.paste(bot_img, (0, tile_h + gap))
            canvas.save(dest_path, format="JPEG", quality=85)
        elif len(imgs) == 1:
            imgs[0].save(dest_path, format="JPEG", quality=85)
    except Exception:
        if os.path.exists(s1):
            shutil.copy2(s1, dest_path)
        elif os.path.exists(s2):
            shutil.copy2(s2, dest_path)

    if not os.path.exists(dest_path):
        return None

    shutil.copy2(dest_path, os.path.join(DESKTOP_INBOX, "latest_both_screens.jpg"))
    size_kb = f"{os.path.getsize(dest_path) / 1024:.1f} KB"
    media_url = f"/media/{fname}"
    with STATE_LOCK:
        LIVE_STATE["status"]["latestScreen"] = {
            "url": media_url,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        bump_version()
    return media_url, fname, size_kb


def is_screenshot_intent(text: str) -> bool:
    t = text.lower()
    keywords = (
        "screenshot",
        "screen shot",
        "snap screen",
        "snap both screen",
        "show me my screen",
        "show me a screenshot",
        "show my screen",
        "capture screen",
        "capture dual-screen",
        "what is on my screen",
        "what's on my screen",
        "print da tela",
        "mostra a tela",
        "mostrar a tela",
    )
    return any(k in t for k in keywords)


def run_devjohn_agent(user_command_text: str, source_type: str = "text", attached_file: Optional[str] = None):
    """Runs John's prompt directly through devjohn (Hermes Agent) and streams the reply to the app."""
    lower = user_command_text.lower().strip()

    # Fast-path: if John asks for a dual-screen capture in natural language (voice or text!)
    if is_screenshot_intent(lower):
        set_agent_status(True, "Capturing both displays...")
        res = capture_both_screens()
        set_agent_status(False, "Online • Ready")
        if res:
            media_url, fname, size_kb = res
            add_message({
                "id": f"dj_{int(time.time()*1000)}",
                "sender": "devjohn",
                "type": "file",
                "text": "Here is your live dual-screen capture right now (Display 1 Samsung on top + Display 2 Liquid Retina on bottom, equal size).",
                "mediaUrl": media_url,
                "fileName": fname,
                "fileSize": size_kb,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            refresh_telemetry()
            return

    if lower in ("📡 refresh all project git statuses", "sync dev radar"):
        set_agent_status(True, "Scanning local git repositories...")
        refresh_telemetry()
        set_agent_status(False, "Online • Ready")
        projects = LIVE_STATE["status"].get("projects", [])
        lines = [f"**Dev Radar Synced ({len(projects)} repos):**"]
        for p in projects:
            status_tag = f"⚠️ {p['dirtyCount']} modified" if p["dirtyCount"] > 0 else "✅ clean"
            lines.append(f"• **{p['name']}** (`{p['branch']}`) — {status_tag} — _{p['lastCommit']}_")
        add_message({
            "id": f"dj_{int(time.time()*1000)}",
            "sender": "devjohn",
            "type": "text",
            "text": "\n".join(lines),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        return

    # Otherwise, invoke the real devjohn Hermes Agent session (`phone-bridge`)
    placeholder_id = f"dj_thinking_{int(time.time()*1000)}"
    add_message({
        "id": placeholder_id,
        "sender": "devjohn",
        "type": "thinking",
        "text": "Thinking & executing on your MacBook Air...",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    set_agent_status(True, "Executing command via devjohn...")

    try:
        context_note = (
            f"[Phone Bridge Direct Message from John Melo ({source_type})]\n"
            f"Respond directly to John as devjohn (conversational, concise, technical, no fluff). "
            f"If John asked you to inspect, build, edit, or check a project on his MacBook Air, do it with your tools and report the result.\n"
        )
        if attached_file:
            context_note += f"John also attached a file saved at: {attached_file}\n"
        full_prompt = f"{context_note}\nJohn says: {user_command_text}"

        qfile = os.path.join("/Users/joaomelo/.hermes/profiles/devjohn/cache/scratch", f"prompt_{int(time.time()*1000)}.txt")
        with open(qfile, "w", encoding="utf-8") as f:
            f.write(full_prompt)

        env = os.environ.copy()
        env["HERMES_HOME"] = "/Users/joaomelo/.hermes/profiles/devjohn"

        proc = subprocess.run(
            [
                HERMES_BIN,
                "chat",
                "-c",
                "phone-bridge",
                "--create-if-missing",
                "-Q",
                "--yolo",
                "--provider",
                "gemini",
                "-m",
                "gemini-2.0-flash-exp",
                "--query-file",
                qfile,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
        try:
            os.remove(qfile)
        except Exception:
            pass

        raw_out = (proc.stdout or "").strip()
        # Strip session_id footer and banner lines
        cleaned_lines = []
        for line in raw_out.splitlines():
            if line.startswith("session_id:") or (line.startswith("Session ") and "starting fresh" in line.lower()):
                continue
            cleaned_lines.append(line)
        reply_text = "\n".join(cleaned_lines).strip()
        if not reply_text:
            reply_text = (proc.stderr or "").strip() or "Done."

        # Check if devjohn generated or referenced any MEDIA:/path file to attach inline
        media_match = re.search(r"MEDIA:([^\s\n]+)", reply_text)
        media_url = None
        media_name = None
        if media_match:
            mpath = media_match.group(1).strip()
            if os.path.isfile(mpath):
                media_name = f"{int(time.time())}_{os.path.basename(mpath)}"
                shutil.copy2(mpath, os.path.join(UPLOADS_DIR, media_name))
                media_url = f"/media/{media_name}"

        update_message(
            placeholder_id,
            type="file" if media_url else "text",
            text=reply_text,
            mediaUrl=media_url,
            fileName=media_name,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
    except Exception as e:
        update_message(
            placeholder_id,
            type="text",
            text=f"⚠️ Execution error: {e}",
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
    finally:
        set_agent_status(False, "Online • Ready")
        refresh_telemetry()


def handle_voice_command_async(audio_bytes: bytes, ext: str, duration_str: str):
    """Transcribes John's voice note immediately with Whisper and sends it as a direct command to devjohn."""
    msg_id = f"voice_{int(time.time()*1000)}"
    fname = f"{msg_id}.{ext}"
    audio_path = os.path.join(UPLOADS_DIR, fname)
    with open(audio_path, "wb") as f:
        f.write(audio_bytes)

    # Create the voice command bubble immediately
    add_message({
        "id": msg_id,
        "sender": "john",
        "type": "voice_command",
        "text": "🎙️ Transcribing voice command with Whisper...",
        "transcript": "",
        "mediaUrl": f"/media/{fname}",
        "duration": duration_str,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })

    set_agent_status(True, "Transcribing voice with OpenAI Whisper...")

    transcript_text = ""
    try:
        # Convert to 16kHz mono WAV first via ffmpeg so iOS m4a/mp4/webm decodes cleanly in <0.2s
        wav_path = os.path.join("/Users/joaomelo/.hermes/profiles/devjohn/cache/scratch", f"{msg_id}.wav")
        subprocess.run(
            ["/opt/homebrew/bin/ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", wav_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        target_for_stt = wav_path if os.path.exists(wav_path) else audio_path
        if transcribe_audio is not None:
            res = transcribe_audio(target_for_stt)
            transcript_text = (res.get("transcript") or "").strip()
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass
    except Exception as e:
        print(f"[WARN] STT error: {e}")

    if not transcript_text:
        transcript_text = "(Could not detect speech in audio — please try speaking again)"
        update_message(msg_id, text=transcript_text, transcript=transcript_text)
        set_agent_status(False, "Online • Ready")
        return

    # Update John's message bubble to show the spoken text as the primary command
    update_message(msg_id, text=transcript_text, transcript=transcript_text)

    # Now feed the transcribed voice command directly to devjohn!
    run_devjohn_agent(transcript_text, source_type="voice command")


class BridgeHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Keep stdout clean

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-File-Name, X-Duration, X-Caption")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            index_file = os.path.join(BRIDGE_DIR, "index.html")
            if os.path.exists(index_file):
                with open(index_file, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if path == "/api/state":
            qs = parse_qs(parsed.query)
            client_v = int(qs.get("v", ["0"])[0] or 0)
            with STATE_LOCK:
                if client_v and client_v == LIVE_STATE.get("version"):
                    body = json.dumps({
                        "notModified": True,
                        "version": LIVE_STATE.get("version"),
                        "agentBusy": LIVE_STATE["status"]["agentBusy"],
                        "agentActivity": LIVE_STATE["status"]["agentActivity"],
                    }).encode("utf-8")
                else:
                    body = json.dumps(LIVE_STATE).encode("utf-8")
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/stream":
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            with SSE_LOCK:
                SSE_CLIENTS.append(self.wfile)
            try:
                pad = ":" + (" " * 2048) + "\n"
                init_payload = json.dumps({"type": "full", "payload": LIVE_STATE, "version": LIVE_STATE.get("version"), "ts": time.time()})
                self.wfile.write((pad + f"data: {init_payload}\n\n").encode("utf-8"))
                self.wfile.flush()
                while True:
                    time.sleep(10)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with SSE_LOCK:
                    if self.wfile in SSE_CLIENTS:
                        SSE_CLIENTS.remove(self.wfile)
            return

        if path.startswith("/media/"):
            fname = os.path.basename(path[len("/media/"):])
            fpath = os.path.join(UPLOADS_DIR, fname)
            if os.path.isfile(fpath):
                ctype, _ = mimetypes.guess_type(fpath)
                with open(fpath, "rb") as f:
                    raw = f.read()
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", ctype or "application/octet-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

        self.send_response(404)
        self._send_cors()
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length > 0 else b""

        if path == "/api/message":
            data = json.loads(raw_body.decode("utf-8"))
            text = (data.get("text") or "").strip()
            sender = data.get("sender") or "john"
            if text:
                msg = add_message({
                    "id": f"msg_{int(time.time()*1000)}",
                    "sender": sender,
                    "type": "text",
                    "text": text,
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
                if sender == "john":
                    threading.Thread(
                        target=run_devjohn_agent,
                        args=(text, "text"),
                        daemon=True,
                    ).start()
            self._json_ok({"ok": True})
            return

        if path == "/api/voice":
            # Raw binary audio body or JSON base64
            ctype = self.headers.get("Content-Type", "")
            duration = self.headers.get("X-Duration", "Voice")
            ext = "m4a" if ("mp4" in ctype or "aac" in ctype or "m4a" in ctype) else "webm"
            if "application/json" in ctype:
                j = json.loads(raw_body.decode("utf-8"))
                audio_bytes = base64.b64decode(j["audioBase64"])
                duration = j.get("duration") or duration
                if "mp4" in (j.get("mimeType") or ""):
                    ext = "m4a"
            else:
                audio_bytes = raw_body

            threading.Thread(
                target=handle_voice_command_async,
                args=(audio_bytes, ext, duration),
                daemon=True,
            ).start()
            self._json_ok({"ok": True, "status": "transcribing"})
            return

        if path == "/api/file":
            raw_fname = unquote(self.headers.get("X-File-Name", f"file_{int(time.time())}"))
            caption = unquote(self.headers.get("X-Caption", "")).strip()
            safe_fname = f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9._-]', '_', raw_fname)}"

            # Save both to live_uploads (for phone preview) and ~/Desktop/PhoneBridge (for John's MacBook)
            upload_path = os.path.join(UPLOADS_DIR, safe_fname)
            desktop_path = os.path.join(DESKTOP_INBOX, safe_fname)
            with open(upload_path, "wb") as f:
                f.write(raw_body)
            shutil.copy2(upload_path, desktop_path)

            size_str = (
                f"{len(raw_body) / 1048576:.2f} MB"
                if len(raw_body) > 1048576
                else f"{len(raw_body) / 1024:.1f} KB"
            )
            add_message({
                "id": f"file_{int(time.time()*1000)}",
                "sender": "john",
                "type": "file",
                "text": caption or f"Shared file `{raw_fname}` to MacBook (`~/Desktop/PhoneBridge`)",
                "mediaUrl": f"/media/{safe_fname}",
                "fileName": raw_fname,
                "fileSize": size_str,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })

            prompt = caption if caption else f"I just sent you a file `{raw_fname}` (saved at `{desktop_path}`). Acknowledge or inspect it if relevant."
            threading.Thread(
                target=run_devjohn_agent,
                args=(prompt, "file share", desktop_path),
                daemon=True,
            ).start()
            self._json_ok({"ok": True, "mediaUrl": f"/media/{safe_fname}"})
            return

        if path == "/api/clear":
            with STATE_LOCK:
                LIVE_STATE["messages"] = LIVE_STATE["messages"][-1:]
                save_state()
            broadcast_event("full", LIVE_STATE)
            self._json_ok({"ok": True})
            return

        self.send_response(404)
        self._send_cors()
        self.end_headers()

    def _json_ok(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self._send_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def background_watcher():
    """Watches ~/Desktop/PhoneBridge/SendToPhone for files dropped on MacBook and refreshes telemetry."""
    tick = 0
    while True:
        try:
            for fname in os.listdir(OUTBOX_DIR):
                if fname.startswith("."):
                    continue
                fpath = os.path.join(OUTBOX_DIR, fname)
                if os.path.isfile(fpath):
                    safe_name = f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9._-]', '_', fname)}"
                    dest = os.path.join(UPLOADS_DIR, safe_name)
                    shutil.copy2(fpath, dest)
                    size_b = os.path.getsize(dest)
                    os.remove(fpath)
                    add_message({
                        "id": f"macout_{int(time.time()*1000)}",
                        "sender": "devjohn",
                        "type": "file",
                        "text": f"📤 Sent from MacBook Air (`SendToPhone`): `{fname}`",
                        "mediaUrl": f"/media/{safe_name}",
                        "fileName": fname,
                        "fileSize": f"{size_b/1024:.1f} KB",
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    })
            if tick % 10 == 0:
                refresh_telemetry()
        except Exception as e:
            print(f"[WARN] Watcher error: {e}")
        tick += 1
        time.sleep(2)


def main():
    load_state()
    refresh_telemetry()
    threading.Thread(target=background_watcher, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), BridgeHTTPHandler)
    print(f"[ONLINE] devjohn realtime server listening on http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
