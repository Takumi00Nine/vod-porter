"""``vodp launchd generate`` (M16/M21改): absolute venv path, no sh wrapper,
no KeepAlive, WorkingDirectory + EnvironmentVariables present."""

from __future__ import annotations

import json

from vodp.cli.main import main
from vodp.core import exitcodes


def test_launchd_generate_writes_plist_with_required_keys(vodp_home, tmp_path, capsys):
    out_path = tmp_path / "com.vodp.plist"
    code = main([
        "launchd", "generate", "--home", str(vodp_home),
        "--venv-python", "/abs/path/.venv/bin/vodp",
        "--working-directory", "/abs/path",
        "--out", str(out_path),
    ])
    envelope = json.loads(capsys.readouterr().out)
    assert code == exitcodes.OK
    content = out_path.read_text()
    assert "/abs/path/.venv/bin/vodp" in content
    assert "<key>WorkingDirectory</key>" in content
    assert "<string>/abs/path</string>" in content
    assert "<key>EnvironmentVariables</key>" in content
    assert "VODP_HOME" in content
    assert "KeepAlive" not in content  # a failed run must not be relaunched immediately
    assert "ThrottleInterval" in content
    assert "sh/run_upload.sh" not in content
    assert envelope["videos"][0]["written_to"] == str(out_path)
