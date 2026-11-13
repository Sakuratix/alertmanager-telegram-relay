import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

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
