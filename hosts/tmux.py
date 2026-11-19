"""Session host on tmux. Decides nothing: carries out the core's actions and answers with facts.

The core's sessions live on their own tmux server (`tmux -L hub`), `claude` is the pane's direct command, without a shell:
the launch line is not printed and cannot get lost or truncated (failure 3). The process exited, so the tmux session is gone.
"""
import os
import subprocess
import time

import platforms
from core import ownclaude, perms
from core.catalog import MCP_TIMEOUT, user_env, write_private
from hosts.common import Trees, carry_transcript, existing_cwd, resume_on_disk, write_hook_settings

# The pane's program reads the session's environment and umask from a 0600 file and removes it before claude starts
# (platforms spec_loader): `tmux -e KEY=value` put them on the tmux client's command line, which every user of the
# machine reads in `ps` (the login token among them).
LAUNCH = "launch"        # the files' folder in the state folder (a session name may be too long for a file name)
STALE_LAUNCH = 600       # s: a file no pane read (its program did not start) is removed at the next spawn or host start

class TmuxHost(Trees):
    kind = "tmux"          # no turn results (turn.result), so the core has nothing to put a 👍 on (core/reducer.py, _take)
    screen_dialogs = True  # the window is visible only through the Claude Code registry (status: waiting)

    def __init__(self, state_dir, claude=("claude",), socket="hub", prefix="hub-", env=None):
        self.state_dir = state_dir
        self.claude = list(claude)
        self.socket = socket
        self.prefix = prefix
        self.env = dict(env or {})
        self.settings = write_hook_settings(os.path.join(state_dir, "hooks-settings.json"))
        self._sweep_launch()
        self.tool_flags = lambda **_: []
        self.session_env = lambda: {}   # hubd fills in variables from the hub settings (catalog.session_env)
        self.providers = lambda: {}     # and the required sign-in modules (session_providers): no launch.env hook here

    # --- tmux ------------------------------------------------------------------------------------------------
    def _tmux(self, *args, input=None, check=True):
        return subprocess.run(["tmux", "-L", self.socket, *args], input=input, capture_output=True,
                              text=True, check=check)

    def target(self, name):
        return self.prefix + name

    # --- facts -----------------------------------------------------------------------------------------------
    def alive(self, name):
        return self._tmux("has-session", "-t", "=" + self.target(name), check=False).returncode == 0

    def sessions(self):
        out = self._tmux("list-sessions", "-F", "#{session_name}", check=False)
        if out.returncode != 0:
            return []
        return [s[len(self.prefix):] for s in out.stdout.split() if s.startswith(self.prefix)]

    def poll(self):
        return []  # everything is visible through hooks and the registry

    def info(self, name):
        """{"pid", "pane"} of the pane's process, or None."""
        out = self._tmux("display-message", "-p", "-t", "=" + self.target(name) + ":", "#{pane_pid} #{pane_id}",
                         check=False)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        pid, pane = out.stdout.split()
        return {"pid": int(pid), "pane": pane}

    def screen(self, name):
        return self._tmux("capture-pane", "-p", "-t", "=" + self.target(name) + ":", check=False).stdout

    # --- actions ---------------------------------------------------------------------------------------------
    def spawn(self, name, session_id, resume=False, cwd=None, mode=None, tools=(), project="all", kind="work",
              effort=None, model=None, minihub=False, bus=None, liaison=False, spec=None, **_):
        if liaison:   # the hub bus's liaison needs its profile and guard (hosts/streamjson): not raised without them
            raise RuntimeError("the bus liaison works only in the stream-json host (the profile and the shell guard)")
        if self.alive(name):
            return "already running"
        if name in self.providers():   # only stream-json calls launch.env modules: if the required sign-in can't be given, don't start
            raise RuntimeError(f"the required sign-in module (session_providers) for {name}: the tmux host does not "
                               "call sign-in modules")
        cwd = existing_cwd(cwd)  # the folder is gone (working copy removed) or never existed: don't fail on spawn
        resume = resume_on_disk(f"tmux: {name}", cwd, session_id, resume)
        if resume:
            carry_transcript(cwd, session_id)
        # spec mode (core/specmode.py): the guard on file edits; the line is in its brief (the window has no suffix)
        settings = self.spec_settings() if (spec or {}).get("open") else self.settings
        cmd = self.claude + ["--resume" if resume else "--session-id", session_id, "-n", name,
                             "--settings", settings]
        if mode:
            cmd += ["--permission-mode", mode]
        from core import minihub as mh   # mini-hub: edits are closed in its own folder (the terminal window has no suffix)
        deny = mh.deny(cwd, keep=(self.state_dir,)) if minihub else ("AskUserQuestion",) if bus else ()   # and the bus
        cmd += self.tool_flags(name=name, tools=tools, kind=kind, cwd=cwd, project=project, effort=effort, model=model,
                               **({"deny": deny} if deny else {}))
        # the tmux server may outlive the hub-up that started it: the pane of the release's own copy turns its updates off
        own = ownclaude.ENV if self.claude and ownclaude.owned(self.claude[0], self.state_dir) else {}
        env = {"MCP_TIMEOUT": MCP_TIMEOUT, **own, **user_env(), "HUB_SESSION": name, "HUB_STATE_DIR": self.state_dir,
               **self.session_env(), **self.env,
               platforms.TREE_ENV: self.tree_tag(name)}
        # the pane takes umask from the tmux server, which the hub started with 077: session files in projects get the
        # user's umask (core/perms.py)
        spec = self._launch_file({"env": env, "umask": perms.user_umask()})
        args = ["new-session", "-d", "-s", self.target(name), "-x", "200", "-y", "50", "-c", cwd]
        try:
            # the pane's program starts below the hub's parts (platforms below_hub), whoever started the tmux server
            self._tmux(*args, "--", *platforms.current.below_hub([*platforms.current.spec_loader(), spec, *cmd]))
        except BaseException:
            _unlink(spec)
            raise
        return "spawned"

    def _launch_file(self, spec):
        """A 0600 file with the pane's environment and umask in the state folder; older ones nobody read are removed."""
        folder = os.path.join(self.state_dir, LAUNCH)
        platforms.current.makedirs_private(folder)
        self._sweep_launch()
        path = os.path.join(folder, f"launch-{os.urandom(8).hex()}.json")
        write_private(path, spec)
        return path

    def _sweep_launch(self):
        """Remove launch files no pane read within STALE_LAUNCH (its program did not start): they hold a login."""
        folder = os.path.join(self.state_dir, LAUNCH)
        for n in os.listdir(folder) if os.path.isdir(folder) else ():
            p = os.path.join(folder, n)
            try:
                if time.time() - os.path.getmtime(p) > STALE_LAUNCH:
                    os.unlink(p)
            except OSError:
                pass

    def type(self, name, text, message=None, **_):  # attachments go as paths in the text (images can't be pasted into the TUI)
        """Type the message and press Enter.

        The `[hub:mN] ` label is typed letter by letter, the body is pasted: Claude Code collapses a long paste into
        "[Pasted text #1 +40 lines]", and the label would otherwise vanish from the screen. If the label is already on
        the screen (the paste went through, Enter was lost: failure 2), only Enter is pressed, so as not to paste twice."""
        t = "=" + self.target(name) + ":"
        # the label is everything up to the first "] ": `[hub:mN]` or `[hub:mN from sender]`
        marker = text[:text.index("] ") + 1] if message and text.startswith(f"[hub:{message}") and "] " in text else None
        if marker and marker in self.screen(name):
            self._tmux("send-keys", "-t", t, "Enter")
            return "enter only"
        body = text
        if marker:
            self._tmux("send-keys", "-t", t, "-l", marker + " ")
            body = text[len(marker) + 1:]
        if body:
            buf = f"hub-{message or 'x'}"
            self._tmux("load-buffer", "-b", buf, "-", input=body)
            self._tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", t)
        time.sleep(0.3)  # let the TUI finish drawing the input; Enter is a separate keypress
        self._tmux("send-keys", "-t", t, "Enter")
        return "typed"

    def release(self, name, **_):
        return self.kill(name)

    def respond(self, name, **_):
        raise NotImplementedError("tmux: only the stream-json host can answer permission requests")

    def recap(self, name, **_):
        """Claude Code recap: only on the stream-json host (the /recap command without a turn); not supported here."""
        return "unsupported"

    def compact(self, name, **_):
        """/compact: only on the stream-json host; not supported here."""
        return "unsupported"

    def mcp_reconnect(self, name, **_):
        """Reconnect MCP in place (core/mcpwatch): only on the stream-json host; not supported here."""
        return "unsupported"

    def mcp_set(self, name, **_):
        """Add MCP servers mid-turn (a grant, reducer._pump): only on the stream-json host; the restart brings them here."""
        return "unsupported"

    def kill(self, name, **_):
        self._tmux("kill-session", "-t", "=" + self.target(name), check=False)
        return "killed"

    def kill_server(self):
        self._tmux("kill-server", check=False)


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass
