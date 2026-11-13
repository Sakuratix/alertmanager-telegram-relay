import io
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from http.server import ThreadingHTTPServer

import relay

PAYLOAD = {
    "status": "firing",
    "commonLabels": {"alertname": "AppDown"},
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "AppDown", "severity": "critical", "instance": "app:8080"},
            "annotations": {"summary": "app is down <now>"},
        }
    ],
}


def test_format_firing():
    text = relay.format_message(PAYLOAD)
    assert "FIRING: AppDown" in text
    assert "🔴" in text
    assert "&lt;now&gt;" in text  # html escaped
    assert "<code>app:8080</code>" in text


def test_format_resolved():
    p = dict(PAYLOAD, status="resolved")
    p["alerts"] = [dict(PAYLOAD["alerts"][0], status="resolved")]
    assert relay.format_message(p).startswith("<b>✅ RESOLVED")


def test_splits_long_groups_without_losing_alerts():
    p = dict(PAYLOAD)
    p["alerts"] = [
        {**PAYLOAD["alerts"][0], "labels": {**PAYLOAD["alerts"][0]["labels"], "instance": f"app-{i}:8080"}}
        for i in range(500)
    ]
    parts = relay.format_messages(p)
    assert len(parts) > 1
    assert all(len(m) <= relay.MAX_LEN for m in parts)
    assert parts[0].startswith("<b>🚨 FIRING: AppDown</b> (500) · part 1/")
    body = "\n".join(parts)
    assert all(f"app-{i}:8080" in body for i in range(500))


def test_single_huge_annotation_is_shortened():
    p = dict(PAYLOAD)
    p["alerts"] = [{**PAYLOAD["alerts"][0], "annotations": {"summary": "x" * 10000}}]
    parts = relay.format_messages(p)
    assert len(parts) == 1 and len(parts[0]) <= relay.MAX_LEN


def post(url, body):
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_http_roundtrip():
    sent = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), relay.make_handler(sent.append))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert post(base + "/alert", json.dumps(PAYLOAD).encode()) == 200
        assert post(base + "/alert", b"{not json") == 400
        assert len(sent) == 1 and "AppDown" in sent[0]
    finally:
        server.shutdown()


class FakeTelegram:
    """Plays back a list of outcomes: an int HTTP status or an exception."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, url, data=None, timeout=None):
        self.calls.append(data)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        if out != 200:
            body = json.dumps({"ok": False, "description": "nope", "parameters": {"retry_after": 3}}).encode()
            raise urllib.error.HTTPError(url, out, "err", {}, io.BytesIO(body))
        return _Resp(200)


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sender(fake, deadline=8.0):
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    send = relay.telegram_sender("T", "42", deadline=deadline, urlopen=fake, sleep=sleep, clock=lambda: t[0])
    return send, slept


def test_retries_rate_limit_using_retry_after():
    fake = FakeTelegram(429, 200)
    send, slept = sender(fake)
    send("hi")
    assert slept == [3.0] and len(fake.calls) == 2


def test_retries_server_errors_with_backoff():
    fake = FakeTelegram(502, urllib.error.URLError("timed out"), 200)
    send, slept = sender(fake)
    send("hi")
    assert slept == [0.5, 1.0]


def test_does_not_retry_client_errors():
    fake = FakeTelegram(400)
    send, slept = sender(fake)
    with pytest.raises(relay.SendError, match="400: nope"):
        send("hi")
    assert slept == [] and len(fake.calls) == 1


def test_gives_up_before_deadline():
    fake = FakeTelegram(*([503] * 10))
    send, slept = sender(fake, deadline=2.0)
    with pytest.raises(relay.SendError, match="gave up"):
        send("hi")
    assert sum(slept) <= 2.0


def test_thread_id_is_sent():
    fake = FakeTelegram(200)
    t = relay.telegram_sender("T", "42", "7", urlopen=fake, sleep=lambda s: None)
    t("hi")
    assert b"message_thread_id=7" in fake.calls[0]


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


def test_metrics_count_deliveries_and_failures():
    calls = {"n": 0}

    def flaky(text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise relay.SendError("down")

    server = ThreadingHTTPServer(("127.0.0.1", 0), relay.make_handler(flaky, relay.Metrics()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert post(base + "/alert", json.dumps(PAYLOAD).encode()) == 200
        assert post(base + "/alert", json.dumps(dict(PAYLOAD, status="resolved")).encode()) == 502
        m = get(base + "/metrics")
    finally:
        server.shutdown()
    assert 'relay_webhooks_total{status="firing"} 1' in m
    assert 'relay_webhooks_total{status="resolved"} 1' in m
    assert "relay_alerts_total 2" in m
    assert "relay_messages_sent_total 1" in m
    assert "relay_send_failures_total 1" in m
    assert "# TYPE relay_messages_sent_total counter" in m
