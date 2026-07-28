# packaging/launchd/

No static plist is checked in here -- paths (`.venv/bin/vodp`,
`WorkingDirectory`, `VODP_HOME`) are host-specific, so the plist is always
generated on the target machine:

```bash
vodp launchd generate \
  --venv-python /abs/path/to/.venv/bin/vodp \
  --working-directory /abs/path/to/repo \
  --out ~/Library/LaunchAgents/com.vodp.vods-upload.plist
```

See the "Unattended operation (launchd)" section of the top-level
README.md for the full setup (load/unload, no `KeepAlive`, why there's no
shell wrapper).
