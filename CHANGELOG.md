# Changelog

## 0.1.0 (2026-09-28)

First public release.

- `splitscreen.py` with `env`, `serve`, `chrome`, `stop`, `status`, and `list`.
- Stable app and CDP ports per worktree. Busy ports are refused, and every listener on a screen's port must belong to that screen.
- Cleanup that stops only process groups it can prove it started, and lists anything it cannot verify.
- Electron support through a main-process snippet gated on `app.isPackaged`.
- Installs through the skills CLI or as a Claude Code plugin.
- CI on macOS and Linux with Python 3.9 and 3.13.
