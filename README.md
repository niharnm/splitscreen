<h1 align="center">splitscreen</h1>

<p align="center">
  <b>Local multiplayer for coding agents.</b><br>
  Every agent tests its own copy of your web or Electron app: its own worktree, its own ports, its own browser, attached over CDP.<br>
  No computer use. No agents clicking in each other's windows.
</p>

<p align="center">
  <a href="https://skills.sh/niharnm/splitscreen"><img src="https://skills.sh/b/niharnm/splitscreen" alt="skills.sh installs"></a>
  <a href="https://github.com/niharnm/splitscreen/actions/workflows/test.yml"><img src="https://github.com/niharnm/splitscreen/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2563eb" alt="MIT license"></a>
</p>

```bash
npx skills add niharnm/splitscreen -g
```

Works with Claude Code, Codex, Cursor, Gemini CLI, GitHub Copilot, OpenCode, and 70+ other agents through the [skills CLI](https://github.com/vercel-labs/skills).

![Two coding agents test the same app in the same minute. Agent A, on branch agent-a, loads 3 orders with a 200 and a clean console. Agent B, on branch agent-b, gets a 500, the failing request, and the console error that explains it.](assets/demo.png)

*Real output from two agents on two worktrees of one app. Agent B's branch has a bug; agent A's does not. Neither agent could see or touch the other's app.*

## The problem

Run three agents in three worktrees and they all break at the same step: testing.

- **They share one screen.** Computer use drives one mouse on one desktop, so parallel agents click in each other's windows.
- **They test the wrong build.** Every worktree's dev server wants port 3000, and the agent checks whichever one answered.
- **They see pixels, not causes.** A screenshot shows the red banner. It does not show the 500 or the console error behind it.

## The fix

Each agent claims a **screen**:

| A screen has | For example |
| --- | --- |
| A Git worktree | `../app-fix-login` on branch `fix-login` |
| An app port for its own dev server | `4444` |
| A CDP port for its own Chrome or Electron instance | `9644` |
| An [agent-browser](https://github.com/vercel-labs/agent-browser) session attached to that port | `screen-fix-login-367661` |

The same worktree always gets the same ports. Two worktrees never share them, and a port held by anything else is never taken. The agent drives its own browser through the Chrome DevTools Protocol, so it reads the DOM, the accessibility tree, console messages, page errors, and every network request.

| | Computer use | One shared browser | splitscreen |
| --- | --- | --- | --- |
| What the agent reads | Screenshots | The DOM of whatever tab is open | DOM, accessibility tree, console, page errors, network |
| Parallel agents | Share one mouse and screen | Navigate each other's tabs | Separate worktree, ports, and browser each |
| Which build it tests | Whatever is on screen | Whatever answers on :3000 | Its own worktree's server, checked by process group |
| Where it runs | A desktop session | Anywhere | macOS, Linux, a VPS, or CI, headless |
| Cleanup | By hand | By hand | `stop` ends only what that screen started |

## Quick start

Install the skill and the CDP client it drives:

```bash
npx skills add niharnm/splitscreen -g
npm install -g agent-browser
```

You also need Python 3.9 or newer and Chrome or Chromium, on macOS or Linux.

Then ask your agent:

```text
Use splitscreen to test the checkout flow in this worktree. Report console errors and failed requests.
```

Under the hood it runs:

```bash
screen="$(python3 <skill-dir>/scripts/splitscreen.py env)" && eval "$screen"
python3 <skill-dir>/scripts/splitscreen.py serve --cmd 'pnpm dev'   # dev server on $APP_PORT
python3 <skill-dir>/scripts/splitscreen.py chrome                   # dedicated Chrome on $CDP_PORT
agent-browser connect "$CDP_PORT" && agent-browser open "$APP_URL"
agent-browser snapshot -i
agent-browser console && agent-browser errors && agent-browser network requests
python3 <skill-dir>/scripts/splitscreen.py stop
```

### Claude Code plugin

```text
/plugin marketplace add niharnm/splitscreen
/plugin install splitscreen@splitscreen
```

## Electron

Electron apps are Chromium, so each running copy can expose its own CDP port. Add this to your main process. The `app.isPackaged` check keeps shipped builds from ever opening a debugging port.

```js
if (!app.isPackaged && process.env.CDP_PORT) {
  app.commandLine.appendSwitch('remote-debugging-port', process.env.CDP_PORT);
}
if (!app.isPackaged && process.env.ELECTRON_USER_DATA_DIR) {
  fs.mkdirSync(process.env.ELECTRON_USER_DATA_DIR, { recursive: true });
  app.setPath('userData', process.env.ELECTRON_USER_DATA_DIR);
}
```

Now every agent can run its own dev build of the same app at once, each with its own port and user data directory:

```bash
python3 <skill-dir>/scripts/splitscreen.py serve --wait cdp --cmd 'npm run dev'
agent-browser connect "$CDP_PORT"
agent-browser tab   # windows and webviews are separate targets
```

## Commands

| Command | What it does |
| --- | --- |
| `env` | Claims this worktree's screen and prints `APP_PORT`, `CDP_PORT`, `APP_URL`, `AGENT_BROWSER_SESSION`, and more |
| `serve --cmd '...'` | Starts your dev server, or your Electron app with `--wait cdp`, and waits until this screen owns the port |
| `chrome` | Starts a dedicated headless Chrome on the screen's CDP port. Add `--headed` to watch |
| `status` | Shows ports, listeners, processes, and log paths |
| `stop` | Closes the browser session and stops what this screen started. Add `--release` to free the ports |
| `list` | Shows every screen on the machine. Add `--prune` to drop deleted worktrees |

## Safety

- **It only stops what it started.** Each process is tracked by PID, start time, and process group. If ownership cannot be proven, `stop` lists the processes and signals nothing.
- **It never takes a busy port.** Ports held by other processes are refused, and a server that drifts to another port fails the start.
- **Throwaway profiles.** Every Chrome and Electron instance gets its own profile, never your real one.
- **Localhost only.** Chrome and Electron bind the debugging port to localhost. Stop screens when you are done.

## Tested

- CI runs 21 tests on macOS and Linux with Python 3.9 and 3.13.
- Two agents on two worktrees of one app at the same time, as in the image above.
- Two VS Code (Electron) instances at once, each on its own CDP port and profile.
- Two worktrees of a private Next.js 16 pnpm monorepo, served and tested in parallel.

## FAQ

**Do I need an Electron app?**
No. Web apps get a dedicated headless Chrome per screen. Electron is just where computer use hurts most.

**Why agent-browser instead of Playwright MCP or Chrome DevTools MCP?**
Its snapshots with element refs are compact and built for agents. Any CDP client can attach to a screen's `CDP_PORT`, so use the one you like.

**Does it work on a VPS or in CI?**
Yes. Everything runs headless on Linux. Set `CHROME_PATH` if your Chrome is not on `PATH`.

**What about native macOS, iOS, or Windows apps?**
Not supported. CDP only reaches Chromium surfaces such as web pages and Electron. Native UI needs XCUITest or accessibility tooling.

**Where does state live?**
In `~/.local/state/splitscreen`. Override it with `SPLITSCREEN_HOME`, and move the port ranges with `SPLITSCREEN_APP_BASE`, `SPLITSCREEN_CDP_BASE`, and `SPLITSCREEN_SLOTS`.

## Credits

Built on [agent-browser](https://github.com/vercel-labs/agent-browser) by Vercel Labs. Inspired by [@isaacdyor](https://www.instagram.com/reel/DdlNfHyFOGM/)'s tip on testing Electron apps with agent-browser over CDP.

If splitscreen stops your agents from fighting over the browser, a star helps other people find it.

[MIT](LICENSE)
