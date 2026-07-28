"""``vodp launchd generate`` (M16, M21改): a launchd plist that runs `vodp`
directly -- no sh wrapper (G18's `| tee` exit-code-swallowing is retired
for good, per requirements.md J⑥)."""

from __future__ import annotations

import sys

from vodp.core import exitcodes, jsonout

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{venv_python}</string>
        <string>vods</string>
        <string>upload</string>
        <string>--non-interactive</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{working_directory}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>VODP_HOME</key>
        <string>{vodp_home}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_dir}/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/launchd-stderr.log</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>ThrottleInterval</key>
    <integer>600</integer>
</dict>
</plist>
"""
# No <key>KeepAlive</key>: a failed run must NOT be relaunched immediately
# (M16 -- the channel flock already makes overlap fail fast, but retrying a
# just-failed unattended job every few seconds would just spam the log).


def run_generate(args, ctx):
    log_dir = f"{ctx.home}/logs"
    plist = PLIST_TEMPLATE.format(
        label=args.label,
        venv_python=args.venv_python,
        working_directory=args.working_directory,
        vodp_home=str(ctx.home),
        log_dir=log_dir,
    )
    if args.out:
        from pathlib import Path

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(plist, encoding="utf-8")
        written_to = str(out_path)
    else:
        print(plist, file=sys.stderr)
        written_to = None

    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        "label": args.label, "written_to": written_to,
    }])
    return exitcodes.OK, envelope
