#!/Users/joaomelo/.hermes/tools/python-3.14.7+20260901-darwin-arm64/bin/python3
"""
DEVJOHN REALTIME SUPERVISOR
Starts:
1. `realtime_server.py` on port 8765
2. `cloudflared tunnel --url http://127.0.0.1:8765`
3. Auto-publishes the live HTTPS tunnel URL to `Johnmelogay/devjohn-bridge/state/endpoint.json`
   so `https://johnmelogay.github.io/devjohn-bridge/` connects automatically with zero manual setup.
"""

import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

BRIDGE_DIR = "/Users/joaomelo/.hermes/profiles/devjohn/projects/devjohn-bridge"
REPO = "Johnmelogay/devjohn-bridge"
HERMES_PY = "/Users/joaomelo/.hermes/tools/python-3.14.7+20260901-darwin-arm64/bin/python3"
CLOUDFLARED = "/opt/homebrew/bin/cloudflared"


def publish_endpoint(tunnel_url: str):
    try:
        token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
        api_url = f"https://api.github.com/repos/{REPO}/contents/state/endpoint.json"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {token}",
            "User-Agent": "devjohn-bridge",
        }
        sha = None
        try:
            req = urllib.request.Request(api_url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=15) as r:
                sha = json.loads(r.read().decode("utf-8")).get("sha")
        except Exception:
            pass

        payload_data = {
            "url": tunnel_url,
            "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        body = {
            "message": f"chore(tunnel): update live endpoint {tunnel_url}",
            "content": base64.b64encode(json.dumps(payload_data, indent=2).encode("utf-8")).decode("ascii"),
        }
        if sha:
            body["sha"] = sha

        req = urllib.request.Request(
            api_url,
            data=json.dumps(body).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()

        # Also save locally to ~/Desktop/PhoneBridge/DIRECT_APP_LINK.txt
        desktop_file = "/Users/joaomelo/Desktop/PhoneBridge/DIRECT_APP_LINK.txt"
        with open(desktop_file, "w") as f:
            f.write(
                f"DEVJOHN // DIRECT LINE (WhatsApp-Speed Voice & File App)\n\n"
                f"1. Permanent GitHub Pages URL (Auto-connects to MacBook):\n"
                f"   https://johnmelogay.github.io/devjohn-bridge/\n\n"
                f"2. Direct Instant Tunnel URL:\n"
                f"   {tunnel_url}\n"
            )
        print(f"[PUBLISHED] Endpoint synced to GitHub & Desktop: {tunnel_url}", flush=True)
    except Exception as e:
        print(f"[WARN] Failed to publish endpoint: {e}", flush=True)


def main():
    # Kill any stale processes on port 8765
    subprocess.run(["pkill", "-f", "realtime_server.py"], check=False)
    subprocess.run(["pkill", "-f", "cloudflared tunnel --url http://127.0.0.1:8765"], check=False)
    time.sleep(1)

    server_proc = subprocess.Popen(
        [HERMES_PY, os.path.join(BRIDGE_DIR, "realtime_server.py")],
        cwd=BRIDGE_DIR,
    )
    time.sleep(1.5)

    # Watchdog loop: if cloudflared ever drops or restarts, auto-relaunch and re-publish endpoint.json
    while True:
        if server_proc.poll() is not None:
            server_proc = subprocess.Popen(
                [HERMES_PY, os.path.join(BRIDGE_DIR, "realtime_server.py")],
                cwd=BRIDGE_DIR,
            )
            time.sleep(1.5)

        tunnel_proc = subprocess.Popen(
            [CLOUDFLARED, "tunnel", "--url", "http://127.0.0.1:8765", "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        found_url = None
        assert tunnel_proc.stdout is not None
        for line in tunnel_proc.stdout:
            m = re.search(r"(https://[a-zA-Z0-9-]+\.trycloudflare\.com)", line)
            if m and not found_url:
                found_url = m.group(1)
                publish_endpoint(found_url)

        tunnel_proc.wait()
        time.sleep(3)


if __name__ == "__main__":
    main()
