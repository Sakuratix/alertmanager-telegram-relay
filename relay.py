"""Alertmanager webhook -> Telegram.

POST /alert with the standard Alertmanager webhook payload.
Config (env):
  TELEGRAM_TOKEN or TELEGRAM_TOKEN_FILE  bot token
  TELEGRAM_CHAT_ID                       chat or channel id
  TELEGRAM_THREAD_ID                     optional topic id in a forum group
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


def format_messages(payload: dict) -> list[str]:
    """Render a webhook payload as one or more Telegram messages.

    Big alert groups are split at line boundaries instead of being cut off,
    so nothing is lost; every part repeats the header with its number.
    """
    status = payload.get("status", "firing")
    alerts = payload.get("alerts", [])
    common = payload.get("commonLabels", {})
    name = html.escape(common.get("alertname", "alert"))
    head = "✅ RESOLVED" if status == "resolved" else "🚨 FIRING"
    header = f"<b>{head}: {name}</b> ({len(alerts)})"
    lines = []
    for a in alerts:
        labels, ann = a.get("labels", {}), a.get("annotations", {})
        icon = "✅" if a.get("status") == "resolved" else SEVERITY_ICON.get(labels.get("severity", ""), "⚪")
        summary = html.escape(ann.get("summary") or ann.get("description") or labels.get("alertname", ""))
        where = html.escape(labels.get("instance") or labels.get("pod") or labels.get("job") or "")
        line = f"{icon} {summary}" + (f" <code>{where}</code>" if where else "")
        lines.append(line if len(line) <= MAX_LEN // 2 else line[: MAX_LEN // 2 - 1] + "…")

    chunks: list[list[str]] = [[]]
    size = len(header) + 16  # room for " · part 9/9"
    for line in lines:
        if chunks[-1] and size + len(line) + 1 > MAX_LEN:
            chunks.append([])
            size = len(header) + 16
        chunks[-1].append(line)
        size += len(line) + 1
    if len(chunks) == 1:
        return ["\n".join([header, *chunks[0]])]
    return ["\n".join([f"{header} · part {i}/{len(chunks)}", *c]) for i, c in enumerate(chunks, 1)]


def format_message(payload: dict) -> str:
    """First message only; kept for callers that expect a single string."""
    return format_messages(payload)[0]


def telegram_sender(token: str, chat_id: str, thread_id: str = "") -> Callable[[str], None]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(text: str) -> None:
        fields = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if thread_id:
            fields["message_thread_id"] = thread_id  # a topic in a forum-style group
        body = urllib.parse.urlencode(fields).encode()
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
                for text in format_messages(payload):
                    send(text)
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
    send = telegram_sender(load_token(), os.environ["TELEGRAM_CHAT_ID"], os.getenv("TELEGRAM_THREAD_ID", ""))
    port = int(os.getenv("PORT", "9087"))
    print(f"listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(send)).serve_forever()


if __name__ == "__main__":
    main()
