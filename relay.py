"""Alertmanager webhook -> Telegram.

POST /alert with the standard Alertmanager webhook payload.
GET /healthz, GET /metrics (Prometheus).
Config (env):
  TELEGRAM_TOKEN or TELEGRAM_TOKEN_FILE  bot token
  TELEGRAM_CHAT_ID                       chat or channel id
  TELEGRAM_THREAD_ID                     optional topic id in a forum group
  WEBHOOK_TOKEN or WEBHOOK_TOKEN_FILE    optional: require "Authorization: Bearer <token>" on /alert
  PORT                                   default 9087
"""

import hmac
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEVERITY_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
MAX_LEN = 4000  # Telegram limit is 4096


def parse_time(value: str | None) -> datetime | None:
    """Alertmanager RFC 3339 timestamp, or None for empty / the zero time it uses for "unset"."""
    if not value or value.startswith("0001-"):
        return None
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))  # nanoseconds -> micro
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def human(seconds: float) -> str:
    """5s, 14m, 2h5m, 3d4h: precise enough for a chat message."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d{h}h" if h else f"{d}d"


def link(url: str | None, text: str) -> str:
    """An <a> tag for http(s) URLs only; anything else from a label is dropped."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>'


def silence_url(payload: dict) -> str | None:
    """Alertmanager UI link that pre-fills a silence for this group's labels."""
    base = payload.get("externalURL")
    group = payload.get("groupLabels") or {}
    if not base or not group:
        return None
    matchers = ",".join(f'{k}="{v}"' for k, v in sorted(group.items()))
    return f"{base.rstrip('/')}/#/silences/new?filter={urllib.parse.quote('{' + matchers + '}')}"


def format_messages(payload: dict, now: datetime | None = None) -> list[str]:
    """Render a webhook payload as one or more Telegram messages.

    Each alert gets how long it has been firing (or how long it lasted, once
    resolved) and links to its runbook and graph when Alertmanager has them.
    A firing group gets a one-tap link to silence it.

    Big alert groups are split at line boundaries instead of being cut off,
    so nothing is lost; every part repeats the header with its number.
    """
    now = now or datetime.now(UTC)
    status = payload.get("status", "firing")
    alerts = payload.get("alerts", [])
    common = payload.get("commonLabels", {})
    name = html.escape(common.get("alertname", "alert"))
    head = "✅ RESOLVED" if status == "resolved" else "🚨 FIRING"
    header = f"<b>{head}: {name}</b> ({len(alerts)})"
    if status != "resolved" and (silence := link(silence_url(payload), "🔕 silence")):
        header += f" · {silence}"
    lines = []
    for a in alerts:
        labels, ann = a.get("labels", {}), a.get("annotations", {})
        resolved = a.get("status") == "resolved"
        icon = "✅" if resolved else SEVERITY_ICON.get(labels.get("severity", ""), "⚪")
        summary = html.escape(ann.get("summary") or ann.get("description") or labels.get("alertname", ""))
        where = html.escape(labels.get("instance") or labels.get("pod") or labels.get("job") or "")
        line = f"{icon} {summary}" + (f" <code>{where}</code>" if where else "")
        if len(line) > MAX_LEN // 2:
            line = line[: MAX_LEN // 2 - 1] + "…"
        extras = []
        if started := parse_time(a.get("startsAt")):
            ended = parse_time(a.get("endsAt")) if resolved else None
            took = human(((ended or now) - started).total_seconds())
            extras.append(f"lasted {took}" if resolved else f"for {took}")
        extras += [
            x for x in (link(ann.get("runbook_url"), "runbook"), link(a.get("generatorURL"), "graph")) if x
        ]
        lines.append(line + (" · " + " · ".join(extras) if extras else ""))

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


class SendError(RuntimeError):
    pass


def telegram_sender(
    token: str,
    chat_id: str,
    thread_id: str = "",
    *,
    deadline: float = 8.0,
    urlopen: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[str], None]:
    """Return send(text). Retries 429 (honouring retry_after) and 5xx/network
    errors with backoff, but gives up before `deadline` seconds so the
    webhook still answers before Alertmanager's own timeout and it can retry."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(text: str) -> None:
        fields = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if thread_id:
            fields["message_thread_id"] = thread_id  # a topic in a forum-style group
        body = urllib.parse.urlencode(fields).encode()
        start, attempt = clock(), 0
        while True:
            attempt += 1
            try:
                with urlopen(url, data=body, timeout=5) as r:
                    if r.status == 200:
                        return
                    wait, why = None, f"telegram returned {r.status}"
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait, why = retry_after(e), "rate limited (429)"
                elif e.code >= 500:
                    wait, why = None, f"telegram returned {e.code}"
                else:  # 400/401/403: retrying won't help, e.g. bad token or chat id
                    raise SendError(f"telegram returned {e.code}: {error_text(e)}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                wait, why = None, f"network error: {getattr(e, 'reason', e)}"
            if wait is None:
                wait = min(0.5 * 2 ** (attempt - 1), 4.0)
            if clock() - start + wait > deadline:
                raise SendError(f"{why}, gave up after {attempt} attempt(s)")
            sleep(wait)

    return send


def retry_after(e: urllib.error.HTTPError) -> float:
    try:
        return float(json.loads(e.read() or b"{}").get("parameters", {}).get("retry_after", 1))
    except (ValueError, AttributeError):
        return 1.0


def error_text(e: urllib.error.HTTPError) -> str:
    try:
        return json.loads(e.read() or b"{}").get("description", "")
    except ValueError:
        return ""


class Metrics:
    """Prometheus counters in text exposition format, no client library needed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.webhooks = {"firing": 0, "resolved": 0}
        self.alerts = 0
        self.sent = 0
        self.failed = 0
        self.send_seconds = 0.0
        self.last_success = 0.0
        self.rejected: dict[str, int] = {}

    def webhook(self, payload: dict) -> None:
        with self._lock:
            key = "resolved" if payload.get("status") == "resolved" else "firing"
            self.webhooks[key] += 1
            self.alerts += len(payload.get("alerts", []))

    def delivered(self, messages: int, seconds: float) -> None:
        with self._lock:
            self.sent += messages
            self.send_seconds += seconds
            self.last_success = time.time()

    def reject(self, reason: str) -> None:
        with self._lock:
            self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def failure(self, seconds: float) -> None:
        with self._lock:
            self.failed += 1
            self.send_seconds += seconds

    def render(self) -> str:
        with self._lock:
            out = [
                "# HELP relay_webhooks_total Webhooks received from Alertmanager.",
                "# TYPE relay_webhooks_total counter",
                *(f'relay_webhooks_total{{status="{k}"}} {v}' for k, v in sorted(self.webhooks.items())),
                "# HELP relay_alerts_total Alerts contained in received webhooks.",
                "# TYPE relay_alerts_total counter",
                f"relay_alerts_total {self.alerts}",
                "# HELP relay_messages_sent_total Telegram messages delivered.",
                "# TYPE relay_messages_sent_total counter",
                f"relay_messages_sent_total {self.sent}",
                "# HELP relay_send_failures_total Webhooks that could not be delivered (answered 502).",
                "# TYPE relay_send_failures_total counter",
                f"relay_send_failures_total {self.failed}",
                "# HELP relay_send_seconds_total Time spent talking to Telegram, retries included.",
                "# TYPE relay_send_seconds_total counter",
                f"relay_send_seconds_total {self.send_seconds:.3f}",
                "# HELP relay_last_success_timestamp_seconds Unix time of the last delivered webhook.",
                "# TYPE relay_last_success_timestamp_seconds gauge",
                f"relay_last_success_timestamp_seconds {self.last_success:.0f}",
                "# HELP relay_rejected_requests_total Requests to /alert refused before delivery.",
                "# TYPE relay_rejected_requests_total counter",
                *(
                    f'relay_rejected_requests_total{{reason="{k}"}} {v}'
                    for k, v in sorted(self.rejected.items())
                ),
            ]
        return "\n".join(out) + "\n"


def make_handler(send: Callable[[str], None], metrics: Metrics | None = None, token: str = ""):
    """token: when set, POST /alert needs "Authorization: Bearer <token>".
    Without it anyone who can reach the port can post to your on-call chat."""
    metrics = metrics or Metrics()
    expected = f"Bearer {token}".encode() if token else b""

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
            elif self.path == "/metrics":
                data = metrics.render().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._reply(404, "not found")

        def do_POST(self) -> None:
            if self.path != "/alert":
                self._reply(404, "not found")
                return
            if expected and not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
                metrics.reject("unauthorized")
                self._reply(401, "unauthorized")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._reply(400, "bad json")
                return
            metrics.webhook(payload)
            started, messages = time.monotonic(), format_messages(payload)
            try:
                for text in messages:
                    send(text)
            except Exception as e:  # report any delivery failure to Alertmanager
                metrics.failure(time.monotonic() - started)
                self._reply(502, f"send failed: {e}")
                return
            metrics.delivered(len(messages), time.monotonic() - started)
            self._reply(200, "sent")

        def log_message(self, fmt: str, *args) -> None:
            print(json.dumps({"client": self.client_address[0], "msg": fmt % args}), flush=True)

    return Handler


def load_secret(name: str, required: bool = True) -> str:
    """NAME_FILE (a mounted secret) wins over NAME."""
    path = os.getenv(f"{name}_FILE")
    if path:
        with open(path) as f:
            return f.read().strip()
    return os.environ[name] if required else os.getenv(name, "")


def load_token() -> str:
    return load_secret("TELEGRAM_TOKEN")


def main() -> None:
    send = telegram_sender(load_token(), os.environ["TELEGRAM_CHAT_ID"], os.getenv("TELEGRAM_THREAD_ID", ""))
    port = int(os.getenv("PORT", "9087"))
    print(f"listening on :{port}", flush=True)
    token = load_secret("WEBHOOK_TOKEN", required=False)
    if not token:
        print("WEBHOOK_TOKEN is not set: /alert accepts unauthenticated requests", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(send, token=token)).serve_forever()


if __name__ == "__main__":
    main()
