"""The demo page is a client of the pipeline: fixture mode, no keys, no network."""

import json

from fastapi.testclient import TestClient

from prospect_brief.web.app import app

client = TestClient(app)


def _stream(url: str):
    lines, done, err = [], None, None
    with client.stream("GET", url) as r:
        event = None
        for raw in r.iter_lines():
            if raw.startswith("event: "):
                event = raw[7:]
            elif raw.startswith("data: "):
                data = json.loads(raw[6:])
                if event == "done":
                    done = data
                elif event == "error":
                    err = data
                else:
                    lines.append(data)
                event = None
    return lines, done, err


def test_fixture_brief_streams_and_exposes_traps():
    r = client.post("/api/briefs", json={"name": "Dorian Vexley-Marsh", "anchors": {"employer": "Halcyon Reef Capital", "school": "University of Southern California"}, "institution": "University of Southern California", "mode": "fixture", "confirm_identity": False})
    assert r.status_code == 200
    job = r.json()["job_id"]
    lines, done, err = _stream(f"/api/briefs/{job}/events")
    assert err is None and done is not None, err
    assert any(l.startswith("Identity card:") for l in lines) and any(l.startswith("[verify]") for l in lines)
    assert done["html_url"].startswith("/fixture/briefs/") and done["counts"]["claims_verified"] > 0
    assert client.get(done["html_url"]).status_code == 200
    ev = client.get(f"/api/briefs/{done['run_id']}/evidence").json()
    reasons = {(e["drop_reason"] or "").split(":")[0] for e in ev["rows"] if e["status"] != "verified"}
    assert {"quote_not_in_source", "specific_missing", "possibly_different_person", "quote_near_instruction", "entailment"} <= reasons
    runs = client.get("/api/briefs").json()
    assert any(x["run_id"] == done["run_id"] and x["fixture"] for x in runs)


def test_brief_requires_anchor():
    assert client.post("/api/briefs", json={"name": "X", "anchors": {}, "mode": "fixture"}).status_code == 400


def test_fixture_signals_job_and_latest():
    r = client.post("/api/signals", json={"institution": "University of Southern California", "days": 90, "mode": "fixture"})
    job = r.json()["job_id"]
    lines, done, err = _stream(f"/api/signals/{job}/events")
    assert err is None and done and any(l.startswith("[efts]") for l in lines)
    assert [row["name"] for row in done["rows"]] == ["Priya Ellsworth-Nakamura"]
    assert done["rows"][0]["brief_command"].startswith("prospect-brief run")
    latest = client.get("/api/signals/latest?mode=fixture").json()
    assert latest["rows"] and latest["html_url"].startswith("/fixture/signals/")
    assert client.get(latest["html_url"]).status_code == 200


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200 and "prospect-brief" in r.text


def _wait_for_confirm(job: str, timeout: float = 30.0) -> dict:
    """Poll /api/jobs instead of reading a partial SSE stream (closing a stream early can hang the test client)."""
    import time

    t0 = time.time()
    while time.time() - t0 < timeout:
        j = next(x for x in client.get("/api/jobs").json() if x["job_id"] == job)
        if j["awaiting_confirm"] or j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job never asked for confirmation")


def test_identity_confirmation_gate_and_reattach():
    r = client.post("/api/briefs", json={"name": "Dorian Vexley-Marsh", "anchors": {"employer": "Halcyon Reef Capital"}, "mode": "fixture", "confirm_identity": True})
    job = r.json()["job_id"]
    j = _wait_for_confirm(job)
    assert j["awaiting_confirm"] and j["status"] == "running"
    assert client.post(f"/api/briefs/{job}/confirm", json={"ok": True}).json() == {"ok": True}
    lines, done, err = _stream(f"/api/briefs/{job}/events")
    assert err is None and done and "[confirm] waiting for you to confirm the identity card" in lines and "[confirm] identity confirmed" in lines
    assert any(l.startswith("Identity card:") for l in lines)
    # a second attach (a reloaded page) replays the full log and the done event
    lines2, done2, _ = _stream(f"/api/briefs/{job}/events")
    assert lines2 == lines and done2 == done
    assert client.post(f"/api/briefs/{job}/confirm", json={"ok": True}).status_code == 409


def test_identity_rejection_stops_the_run():
    r = client.post("/api/briefs", json={"name": "Dorian Vexley-Marsh", "anchors": {"employer": "Halcyon Reef Capital"}, "mode": "fixture", "confirm_identity": True})
    job = r.json()["job_id"]
    _wait_for_confirm(job)
    client.post(f"/api/briefs/{job}/confirm", json={"ok": False})
    lines, done, err = _stream(f"/api/briefs/{job}/events")
    assert done is None and err and "identity" in err["error"].lower()
