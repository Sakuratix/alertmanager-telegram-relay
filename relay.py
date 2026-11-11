"""Alertmanager webhook -> Telegram.

POST /alert with the standard Alertmanager webhook payload.
Config (env):
  TELEGRAM_TOKEN or TELEGRAM_TOKEN_FILE  bot token
  TELEGRAM_CHAT_ID                       chat or channel id
  PORT                                   default 9087
"""

import html
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEVERITY_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
MAX_LEN = 4000  # Telegram limit is 4096


def format_message(payload: dict) -> str:
    status = payload.get("status", "firing")
    alerts = payload.get("alerts", [])
    common = payload.get("commonLabels", {})
    name = html.escape(common.get("alertname", "alert"))
    head = "✅ RESOLVED" if status == "resolved" else "🚨 FIRING"
    lines = [f"<b>{head}: {name}</b> ({len(alerts)})"]
    for a in alerts:
        labels, ann = a.get("labels", {}), a.get("annotations", {})
        icon = "✅" if a.get("status") == "resolved" else SEVERITY_ICON.get(labels.get("severity", ""), "⚪")
        summary = html.escape(ann.get("summary") or ann.get("description") or labels.get("alertname", ""))
        where = html.escape(labels.get("instance") or labels.get("pod") or labels.get("job") or "")
        lines.append(f"{icon} {summary}" + (f" <code>{where}</code>" if where else ""))
    text = "\n".join(lines)
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 20] + "\n… (truncated)"


def telegram_sender(token: str, chat_id: str) -> Callable[[str], None]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(text: str) -> None:
        body = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        ).encode()
        with urllib.request.urlopen(url, data=body, timeout=10) as r:
            if r.status != 200:
                raise RuntimeError(f"telegram returned {r.status}")

    return send


def make_handler(send: Callable[[str], None]):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, msg: str) -> None:
            data = json.dumps({"status": msg}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._reply(200, "ok")
            else:
                self._reply(404, "not found")

        def do_POST(self) -> None:
            if self.path != "/alert":
                self._reply(404, "not found")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._reply(400, "bad json")
                return
            try:
                send(format_message(payload))
            except Exception as e:  # report any delivery failure to Alertmanager
                self._reply(502, f"send failed: {e}")
                return
            self._reply(200, "sent")

        def log_message(self, fmt: str, *args) -> None:
            print(json.dumps({"client": self.client_address[0], "msg": fmt % args}), flush=True)

    return Handler


def load_token() -> str:
    path = os.getenv("TELEGRAM_TOKEN_FILE")
    if path:
        with open(path) as f:
            return f.read().strip()
    return os.environ["TELEGRAM_TOKEN"]


def main() -> None:
    send = telegram_sender(load_token(), os.environ["TELEGRAM_CHAT_ID"])
    port = int(os.getenv("PORT", "9087"))
    print(f"listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(send)).serve_forever()


if __name__ == "__main__":
    main()
