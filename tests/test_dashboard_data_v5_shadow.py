import json
import time

import dashboard.data as data


def test_fetch_v5_shadow_status_missing_file_returns_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "V5_SHADOW_STATUS_FILE", tmp_path / "does_not_exist.json")
    status = data.fetch_v5_shadow_status()
    assert status.available is False


def test_fetch_v5_shadow_status_reads_fresh_file(monkeypatch, tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"MODE": "V5_SHADOW", "REAL_ORDERS": 0, "SCANS": 5}))
    monkeypatch.setattr(data, "V5_SHADOW_STATUS_FILE", path)
    status = data.fetch_v5_shadow_status()
    assert status.available is True
    assert status.stale is False
    assert status.raw["REAL_ORDERS"] == 0


def test_fetch_v5_shadow_status_reports_stale_when_old(monkeypatch, tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"MODE": "V5_SHADOW"}))
    monkeypatch.setattr(data, "V5_SHADOW_STATUS_FILE", path)
    monkeypatch.setattr(data, "V5_SHADOW_STATUS_STALE_AFTER_SECONDS", 0.0)
    time.sleep(0.01)
    status = data.fetch_v5_shadow_status()
    assert status.available is True
    assert status.stale is True


def test_fetch_v5_shadow_status_malformed_json_reports_unavailable(monkeypatch, tmp_path):
    path = tmp_path / "status.json"
    path.write_text("{not valid json")
    monkeypatch.setattr(data, "V5_SHADOW_STATUS_FILE", path)
    status = data.fetch_v5_shadow_status()
    assert status.available is False


def test_fetch_v5_shadow_observations_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "V5_SHADOW_OBSERVATIONS_FILE", tmp_path / "does_not_exist.jsonl")
    assert data.fetch_v5_shadow_observations() == []


def test_fetch_v5_shadow_observations_parses_valid_lines_and_skips_malformed(monkeypatch, tmp_path):
    path = tmp_path / "observations.jsonl"
    path.write_text(
        json.dumps({"symbol": "RVN/USDT"}) + "\n"
        + "not valid json\n"
        + json.dumps({"symbol": "ZIL/USDT"}) + "\n"
    )
    monkeypatch.setattr(data, "V5_SHADOW_OBSERVATIONS_FILE", path)
    observations = data.fetch_v5_shadow_observations()
    assert len(observations) == 2
    assert observations[0]["symbol"] == "RVN/USDT"
    assert observations[1]["symbol"] == "ZIL/USDT"


def test_fetch_v5_shadow_observations_respects_max_rows_tail(monkeypatch, tmp_path):
    path = tmp_path / "observations.jsonl"
    lines = [json.dumps({"symbol": f"S{i}/USDT"}) for i in range(10)]
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(data, "V5_SHADOW_OBSERVATIONS_FILE", path)
    observations = data.fetch_v5_shadow_observations(max_rows=3)
    assert len(observations) == 3
    assert [o["symbol"] for o in observations] == ["S7/USDT", "S8/USDT", "S9/USDT"]


def test_fetch_v5_live_status_missing_file_returns_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "V5_LIVE_STATUS_FILE", tmp_path / "does_not_exist.json")
    status = data.fetch_v5_live_status()
    assert status.available is False


def test_fetch_v5_live_status_reads_fresh_running_file(monkeypatch, tmp_path):
    path = tmp_path / "live_status.json"
    path.write_text(json.dumps({"LIVE_STATUS": "RUNNING", "REAL_TRADES": 3}))
    monkeypatch.setattr(data, "V5_LIVE_STATUS_FILE", path)
    status = data.fetch_v5_live_status()
    assert status.available is True
    assert status.stale is False
    assert status.raw["REAL_TRADES"] == 3


def test_fetch_v5_live_status_stopped_session_is_never_flagged_stale(monkeypatch, tmp_path):
    """A STOPPED session's status file is expected to age past the
    staleness window (the process legitimately exited) -- only a
    RUNNING/EXECUTING status going stale indicates a real problem."""
    path = tmp_path / "live_status.json"
    path.write_text(json.dumps({"LIVE_STATUS": "STOPPED"}))
    monkeypatch.setattr(data, "V5_LIVE_STATUS_FILE", path)
    monkeypatch.setattr(data, "V5_LIVE_STATUS_STALE_AFTER_SECONDS", 0.0)
    time.sleep(0.01)
    status = data.fetch_v5_live_status()
    assert status.available is True
    assert status.stale is False


def test_fetch_v5_live_status_running_session_reports_stale_when_old(monkeypatch, tmp_path):
    path = tmp_path / "live_status.json"
    path.write_text(json.dumps({"LIVE_STATUS": "EXECUTING"}))
    monkeypatch.setattr(data, "V5_LIVE_STATUS_FILE", path)
    monkeypatch.setattr(data, "V5_LIVE_STATUS_STALE_AFTER_SECONDS", 0.0)
    time.sleep(0.01)
    status = data.fetch_v5_live_status()
    assert status.stale is True


def test_fetch_v5_live_status_malformed_json_reports_unavailable(monkeypatch, tmp_path):
    path = tmp_path / "live_status.json"
    path.write_text("{not valid json")
    monkeypatch.setattr(data, "V5_LIVE_STATUS_FILE", path)
    status = data.fetch_v5_live_status()
    assert status.available is False
