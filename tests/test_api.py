WINDOW = {
    "start": "2026-01-01T00:00:00Z",
    "end": "2026-01-01T10:00:00Z",
    "temp_min": 18.0,
    "temp_max": 22.0,
    "max_continuous_exceed_seconds": 1800,
    "max_sampling_interval_seconds": 3700,
}


def create_window(client, **overrides):
    body = dict(WINDOW)
    body.update(overrides)
    resp = client.post("/windows", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def submit(client, window_id, reading_id, revision, ts, temp, expected=(200, 201)):
    resp = client.post(
        f"/windows/{window_id}/readings",
        json={"reading_id": reading_id, "revision": revision,
              "timestamp": ts, "temperature": temp},
    )
    assert resp.status_code in expected, resp.text
    return resp


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_full_pass_workflow_and_certificate(client):
    wid = create_window(client)
    ts = ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z",
          "2026-01-01T02:00:00Z", "2026-01-01T03:00:00Z"]
    for i, t in enumerate(ts):
        r = submit(client, wid, f"r{i}", 1, t, 20.0)
        assert r.json()["status"] == "accepted"

    ev = client.get(f"/windows/{wid}/evaluation").json()
    assert len(ev["segments"]) == 2
    assert ev["segments"][0]["status"] == "normal"
    assert ev["normal_seconds"] == 10800.0
    assert ev["unknown_seconds"] == 25200.0  # 03:00 -> 10:00 边界无读数

    # 有未知 -> 待定
    assert ev["verdict"] == "inconclusive"

    # 补齐到窗口终点
    for i, t in enumerate(["2026-01-01T04:00:00Z", "2026-01-01T05:00:00Z",
                           "2026-01-01T06:00:00Z", "2026-01-01T07:00:00Z",
                           "2026-01-01T08:00:00Z", "2026-01-01T09:00:00Z",
                           "2026-01-01T10:00:00Z"], start=4):
        submit(client, wid, f"r{i}", 1, t, 20.0)

    ev = client.get(f"/windows/{wid}/evaluation").json()
    assert ev["verdict"] == "pass"
    assert ev["unknown_seconds"] == 0.0
    assert ev["window_seconds"] == 36000.0

    cert = client.post(f"/windows/{wid}/certificates")
    assert cert.status_code == 201
    cert_json = cert.json()
    assert cert_json["version"] == 1
    assert cert_json["verdict"] == "pass"
    assert len(cert_json["readings"]) == 11
    assert cert_json["result"]["window_id"] == wid

    listed = client.get(f"/windows/{wid}/certificates").json()
    assert [c["version"] for c in listed] == [1]
    assert client.get(f"/windows/{wid}/certificates/1").json()["id"] == cert_json["id"]


def test_exceeded_window_fails(client):
    wid = create_window(client)
    submit(client, wid, "a", 1, "2026-01-01T00:00:00Z", 20.0)
    submit(client, wid, "b", 1, "2026-01-01T00:30:00Z", 23.0)
    submit(client, wid, "c", 1, "2026-01-01T01:00:00Z", 23.0)
    submit(client, wid, "d", 1, "2026-01-01T01:30:30Z", 20.0)

    ev = client.get(f"/windows/{wid}/evaluation").json()
    # 连续超限 3630s > 1800s
    assert ev["longest_continuous_exceeded_seconds"] == 3630.0
    assert ev["verdict"] == "fail"

    cert = client.post(f"/windows/{wid}/certificates").json()
    assert cert["verdict"] == "fail"
    assert cert["version"] == 1


def test_idempotent_same_revision(client):
    wid = create_window(client)
    first = submit(client, wid, "a", 1, "2026-01-01T00:00:00Z", 20.0).json()
    assert first["status"] == "accepted"

    again = submit(client, wid, "a", 1, "2026-01-01T00:00:00Z", 20.0)
    assert again.status_code == 200
    assert again.json()["status"] == "idempotent"

    readings = client.get(f"/windows/{wid}/readings").json()
    assert len(readings) == 1


def test_same_revision_content_conflict_rejected(client):
    wid = create_window(client)
    submit(client, wid, "a", 1, "2026-01-01T00:00:00Z", 20.0)
    resp = client.post(
        f"/windows/{wid}/readings",
        json={"reading_id": "a", "revision": 1,
              "timestamp": "2026-01-01T00:00:00Z", "temperature": 21.5},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflicting_revision"


def test_higher_revision_replaces_and_reissues_version(client):
    wid = create_window(client)
    submit(client, wid, "a", 1, "2026-01-01T00:00:00Z", 20.0)
    submit(client, wid, "b", 1, "2026-01-01T01:00:00Z", 20.0)
    submit(client, wid, "c", 1, "2026-01-01T02:00:00Z", 20.0)
    submit(client, wid, "d", 1, "2026-01-01T03:00:00Z", 20.0)

    cert1 = client.post(f"/windows/{wid}/certificates").json()
    assert cert1["version"] == 1
    snap_temp = cert1["readings"][0]["temperature"]
    assert snap_temp == 20.0

    # 高修订号把 a 改为超限温度，并让连续超限持续超过 1800s
    repl = submit(client, wid, "a", 2, "2026-01-01T00:00:00Z", 23.0)
    assert repl.json()["status"] == "replaced"
    assert repl.json()["replaced_revision"] == 1

    # 旧修订号再次提交被拒绝
    stale = client.post(
        f"/windows/{wid}/readings",
        json={"reading_id": "a", "revision": 1,
              "timestamp": "2026-01-01T00:00:00Z", "temperature": 20.0},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"

    ev = client.get(f"/windows/{wid}/evaluation").json()
    assert ev["verdict"] == "fail"
    assert ev["longest_continuous_exceeded_seconds"] == 3600.0

    # 原签发结论保留快照不变；重新签发形成新版本
    cert1_fetched = client.get(f"/windows/{wid}/certificates/1").json()
    assert cert1_fetched["readings"][0]["temperature"] == 20.0
    assert cert1_fetched["verdict"] == cert1["verdict"]

    cert2 = client.post(f"/windows/{wid}/certificates").json()
    assert cert2["version"] == 2
    assert cert2["verdict"] == "fail"
    assert cert2["readings"][0]["revision"] == 2

    versions = [c["version"] for c in client.get(f"/windows/{wid}/certificates").json()]
    assert versions == [1, 2]

    all_readings = client.get(f"/windows/{wid}/readings?include_superseded=true").json()
    assert len(all_readings) == 5  # 4 生效 + 1 已替换
    active = client.get(f"/windows/{wid}/readings").json()
    assert all(not r["superseded"] for r in active)
    assert len(active) == 4


def test_outside_window_rejected(client):
    wid = create_window(client)
    for ts in ("2025-12-31T23:59:59Z", "2026-01-01T10:00:01Z"):
        resp = client.post(
            f"/windows/{wid}/readings",
            json={"reading_id": "a", "revision": 1,
                  "timestamp": ts, "temperature": 20.0},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "outside_window"


def test_same_timestamp_different_reading_rejected(client):
    wid = create_window(client)
    submit(client, wid, "a", 1, "2026-01-01T00:00:00Z", 20.0)
    resp = client.post(
        f"/windows/{wid}/readings",
        json={"reading_id": "b", "revision": 1,
              "timestamp": "2026-01-01T00:00:00Z", "temperature": 20.5},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "duplicate_timestamp"


def test_invalid_window_rejected(client):
    resp = client.post(
        "/windows",
        json={**WINDOW, "start": "2026-01-01T10:00:00Z", "end": "2026-01-01T00:00:00Z"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_window"

    resp = client.post("/windows", json={**WINDOW, "temp_min": 23.0, "temp_max": 18.0})
    assert resp.status_code == 422


def test_boundary_timestamps_accepted(client):
    wid = create_window(client)
    submit(client, wid, "a", 1, "2026-01-01T00:00:00Z", 20.0)
    submit(client, wid, "b", 1, "2026-01-01T10:00:00Z", 20.0)
    ev = client.get(f"/windows/{wid}/evaluation").json()
    # 两条读数间隔 10h > 3700s：(00:00,10:00) 整段未知
    assert ev["unknown_seconds"] == 36000.0
    assert ev["verdict"] == "inconclusive"


def test_unknown_window_and_reading_404(client):
    assert client.get("/windows/nope").status_code == 404
    assert client.post("/windows/nope/certificates").status_code == 404


def test_windows_listing(client):
    wid = create_window(client)
    rows = client.get("/windows").json()
    assert any(w["id"] == wid for w in rows)
