# Changelog

## 0.1.0 (2026-09-28)

First public release.

- `splitscreen.py` with `env`, `serve`, `chrome`, `stop`, `status`, and `list`.
- Stable app and CDP ports per worktree, kept until `env --move`, and never shared between worktrees.
- Busy ports are refused, and every socket on a screen's port, IPv4 and IPv6, must belong to that screen. Without `lsof` or `ss`, `serve` refuses to start.
- Cleanup that stops only process groups it can prove it started, and lists anything it cannot verify.
- Launches, stops, and moves in one screen are serialized, so concurrent agents cannot drop each other's records.
- Electron support through a main-process snippet gated on `app.isPackaged`.
- Installs through the skills CLI or as a Claude Code plugin.
- CI on macOS and Linux with Python 3.9 and 3.13.
