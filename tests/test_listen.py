"""listen(dog_id, seconds): robot command, listen scope, audio clip kept 1 hour (2026-11-07)."""
import os
import time

import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.mcp_server import authz
from app.services import media_files, robot_control

CONN = Actor(kind="connector", id="claude", user_id="user_t1")
AGENT = Actor(kind="agent", id="agent", user_id="user_t1")
DESC = "Asks the robot to listen for up to 15 seconds and returns what it heard, with the recording."


@pytest.mark.parametrize("seconds", [1, 8, 15])
def test_listen_command(temp_db, seconds):
    msg, _ = robot_control.build_robot_message("listen", {"seconds": seconds}, CONN, "dog_t1")
    assert msg["command"] == "listen"
    assert msg["data"] == {"seconds": seconds, "source": "connector", "command_id": msg["command_id"]}


@pytest.mark.parametrize("seconds", [0, 16, -1, None, "loud", True])
def test_listen_seconds_rejected(temp_db, seconds):
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("listen", {"seconds": seconds}, AGENT, "dog_t1")
    assert e.value.status_code == 422 and e.value.detail["code"] == "listen_seconds_out_of_range"


def test_listen_scope_is_opt_in():
    assert authz.TOOL_SCOPES["listen"] == "listen"
    assert "listen" in authz.ALL_SCOPES and "listen" in authz.OPT_IN_SCOPES and authz.SCOPE_LABELS["listen"]


def test_listen_result():
    fields, clip = robot_control.listen_tool_result({"command_id": "c", "result": {"ok": True, "result": {
        "labels": ["bark", "whine"], "db": 62.5, "peak_db": 81, "seconds": 10,
        "clip_url": "/api/media/download/wimz_robot_t_ab12.wav"}}})
    assert clip == "/api/media/download/wimz_robot_t_ab12.wav"
    assert fields == {"ok": True, "command_id": "c", "labels": [{"label": "bark"}, {"label": "whine"}], "db": 62.5,
                      "peak_db": 81, "seconds": 10, "clip_expires_in_s": 3600}
    assert robot_control.listen_tool_result({"timed_out": True, "command_id": "c"})[0]["reason"] == "listen_timed_out"
    f, c = robot_control.listen_tool_result({"command_id": "c", "result": {"ok": False, "error": "mic_busy"}})
    assert c is None and f["ok"] is False and f["error"] == "mic_busy"


def test_audio_clip_expires_after_one_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(media_files, "media_dir", lambda: tmp_path)
    clip = tmp_path / "wimz_robot_t_ab12.wav"
    clip.write_bytes(b"RIFF")
    assert media_files.resolve(clip.name) == clip
    assert media_files.media_type_for(clip) == "audio/wav"
    old = time.time() - media_files.AUDIO_TTL_S - 5
    os.utime(clip, (old, old))
    assert media_files.resolve(clip.name) is None          # expired even before cleanup runs
    assert media_files.cleanup_old_files() == 1 and not clip.exists()


def test_listen_descriptions():
    from app.voice_agent import tools as vt
    by_name = {t["name"]: t for t in vt.TOOLS}
    assert by_name["listen"]["description"] == DESC
    assert "listen" in vt.PHYSICAL_TOOLS
    import inspect
    from app.mcp_server import tools as mt
    assert f'"""{DESC}"""' in inspect.getsource(mt)


def test_listen_result_robot_shape():
    """Exact shape from wimz_robot_05 on 2026-11-07 (labels as objects, mean_db, absolute audio_url)."""
    fields, clip = robot_control.listen_tool_result({"command_id": "c", "result": {"ok": True, "result": {
        "seconds": 3.0, "bytes": 96044, "filepath": "/data/media/_unassigned/x.wav",
        "labels": [{"label": "Speech", "confidence": 0.97}, {"label": "Silence", "confidence": 0.005}],
        "mean_db": -39.8, "audio_url": "https://api.wimzai.com/api/media/download/wimz_robot_05_e1810ea8.wav"}}})
    assert clip == "https://api.wimzai.com/api/media/download/wimz_robot_05_e1810ea8.wav"
    assert fields == {"ok": True, "command_id": "c", "seconds": 3.0, "db": -39.8, "clip_expires_in_s": 3600,
                      "labels": [{"label": "Speech", "confidence": 0.97}, {"label": "Silence", "confidence": 0.005}]}
    assert "filepath" not in fields   # robot-local paths never reach the assistant
