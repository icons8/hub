"""The one-line install's installer (spec §4.5, §5.1, §7): what the environment's install.py (tools/package/install.py)
hands over to — the setup.py of the newest code there is, run by the environment's Python with the arguments of
contract 1 (deploy/oneline/README.md). It belongs to the code layer, so a release brings its own: run from an older
code (an environment's seed stays on the delivery server across releases), before it reads anything of the state
folder it reads the channel's latest release and, when that one is newer, downloads and checks it into a folder of its
own apart from every hub and hands over to its setup.py (--handed) — what the line refuses, asks and does is always the
latest release's; that one puts its layer into the state folder itself. Of a seed only this first step runs.

What it does, in order (each step is harmless again; the hub's settings, journal, sessions, plugins and own modules are
never touched):
0. the hand-over above (no answer from the server, a layer that does not check out — this code goes on, said once);
1. the state folder, as the core finds it; what is there: nothing (a new hub), a hub of this install (<state>/app is
   its pointer — a run again is the repair and the update, §5.1), a hub from a git clone (`hub app find`: the one
   question, "Move / Stay on the clone / Later", in the terminal; nobody to ask — later), any other hub (refused,
   nothing changed); --instance <name> — `hub instance new <name>` of the main hub (its own folders, ports, service),
   unless ~/.hub-<name> holds that second hub from git: then it is the one asked about and moved, as itself (its
   settings folder, service, commands hub-<name>; the main hub untouched) — the same for HUB_STATE_DIR on its folder;
   the plain line says, for each such hub next to the main one, the line that moves it; with no main hub of this
   install it asks which to move, or a new main hub (nobody to ask — those lines, nothing installed, exit 3);
2. the build key of the line into layers/deliver.json (0600) — before the script's copy of it goes; the open build's
   line has no key (its releases are open to anyone): the address alone;
3. a new hub or a move: the layer this installer belongs to — the one step 0 handed over from (its archive kept for
   repairs), else the environment's own release; no answer from the server — the hub installs the latest itself once it
   is up (layers/fresh.json);
4. a move: the release's `hub app move` (waits for busy sessions, stops the clone's hub, saves what the install
   rewrites);
5. the layer (the environment's launch.py: the pointer <state>/code, PACKAGED, an edited layer put back with its copy
   kept — a run again), the pointer <state>/app → the environment, <state>/bin/python3 (and on a Mac the git wrapper
   dugite's git needs: GIT_EXEC_PATH, GIT_TEMPLATE_DIR), the hub session's folder, hub.json once (the environment's
   browser MCP and ffmpeg through the pointer — the paths stay right when the environment changes);
6. the commands `hub` and `hub-telegram` (and ~/.local/bin in PATH: one marked line in the shell's profile; Windows —
   the user's PATH);
7. the service — through the pointer, so a new environment needs no new service: a Mac — the launchd agent through
   the signed "Agents Hub.app" of the code (its folder access is granted once to it) running start.py of the
   environment; Linux — the systemd --user unit (and the hub kept without a login: linger, when the system lets the
   user set it — else the one sudo line; a shell without a login session reaches the manager linger starts);
   Windows — the scheduler task of deploy/windows/install.ps1 -App -Shell. Rewritten only when it is missing or
   differs; a hub that is up and runs on the right service is not restarted;
8. the page waited for, then the menu shortcut "Hub by Icons8" (Linux: a .desktop file in the applications menu; Windows: a .lnk
   in the Start menu; a Mac has none; a second hub has its own, named for it; a failure to make it is a line, not a
   failed install), then the page opened (`hub open`; --no-open — the address only); a move — `hub app move --done`, or
   `hub app undo --failed` when the page did not answer;
9. a run again: a newer release of the channel is installed as the Update button does (`hub update`: it waits for
   busy sessions, the watchdog of the previous release, a return when it does not come up).
A failure after the key is kept goes to the company's Sentry as the update's reports do (step install.<step>).

Exit codes: 0 done; 1 refused or failed (nothing half done); 3 a hub from git and nobody to ask; 4 the page did not
answer; 5 a move that did not come up, back on the clone; 64 a wrong argument. The stdlib only."""
import base64
import contextlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the code layer (or a clone)
sys.path.insert(0, ROOT)

import platforms  # noqa: E402
from core import appcode, i18n, instance, shortcut  # noqa: E402
from core.uninstall import PATH_LINE, PATH_MARK  # noqa: E402 — the PATH line hub uninstall takes away by its text

OK, FAILED, NOBODY, NOT_UP, MOVED_BACK, USAGE = 0, 1, 3, 4, 5, 64
PAGE_WAIT = 90        # s for the page to answer after the service started (a cold machine, the first start)
SETUP = os.path.join("deploy", "oneline", "setup.py")
KEY = re.compile(r"[A-Za-z0-9_-]{16,200}\Z")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
LABEL = "com.hub-core"
UNIT = "agents-hub.service"
BUNDLE = "Agents Hub.app"
# what only a hub leaves in its state folder besides its journal and rules (core/uninstall.marked): an empty journal
# and no hub-home are still a hub's data, never a folder to install a new hub over
# (not in marked() itself: `hub uninstall --purge` deletes by it)
HUB_DATA = ("state.json", "journal-archive")
ARCHIVES = "archives"   # in a folder of layers: their archives (modules/update/layer.py ARCHIVES)
ENV_MARK = ".agents-hub-shell.json"   # an environment folder the install script finished (deploy/oneline/unpack.py)
FLAGS = ("--move", "--stay", "--yes", "--no-open", "--no-input", "--handed")
VALUES = ("--contract", "--env", "--server", "--key-file", "--platform", "--flavor", "--runtime", "--channel", "--state",
          "--instance", "--as-instance")


class Refused(Exception):
    """Nothing (more) is done: what is in the way, for a person."""


class Step(Exception):
    """A step failed after the key was kept: its name for the report and the person."""

    def __init__(self, step, error):
        super().__init__(f"{step}: {error}")
        self.step, self.error = step, error


def t(key, **kw):
    return i18n.t(key, **kw)


def parse(argv):
    """-> {flag: value or True}; ValueError on a word it does not know or a value that is missing."""
    got, i = {}, 0
    while i < len(argv):
        a = argv[i]
        if a in FLAGS:
            got[a] = True
            i += 1
        elif a in VALUES and i + 1 < len(argv):
            got[a] = argv[i + 1]
            i += 2
        else:
            raise ValueError(a)
    for need in ("--contract", "--env", "--server", "--flavor", "--channel"):
        if need not in got:
            raise ValueError(need)
    if "--key-file" not in got and got["--flavor"] != "open":   # the open build's line has no key (its pages are open)
        raise ValueError("--key-file")
    if got["--contract"] != "1" or got["--channel"] not in ("stable", "nightly") or got["--flavor"] not in appcode.FLAVORS:
        raise ValueError(f"{got['--contract']} {got['--channel']} {got['--flavor']}")
    for flag in ("--instance", "--as-instance"):
        if flag in got and not NAME.match(got[flag]):
            raise ValueError(got[flag])
    if "--as-instance" in got and "--state" not in got:
        raise ValueError("--as-instance without --state")
    if got.get("--move") and got.get("--stay"):
        raise ValueError("--move --stay")
    return got


def _shell_module(env, name):
    """A frozen file of the environment (launch.py, app.py): the shell's own copy, as its service runs it."""
    spec = importlib.util.spec_from_file_location(f"hub_shell_{name}", os.path.join(env, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, env)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(env)
    return mod


def _read(path, kind=dict):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return kind()
    return data if isinstance(data, kind) else kind()


def _look_module():
    """look.py of this code layer (what the terminal shows; HUB_INSTALL_UI), loaded by its path: the script's folder is not on
    the path of an isolated run."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("hub_install_look", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                                    "look.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha(path):
    try:
        return appcode.sha256(path)
    except OSError:
        return None


class Install:
    def __init__(self, args, run=subprocess.run, ask=input, out=None, clock=time.monotonic, sleep=time.sleep,
                 osname=None, home=None, environ=None, spawn=None):
        self.args = args
        self.run, self.ask, self.clock, self.sleep = run, ask, clock, sleep
        self.out = out or sys.stderr
        self.os = osname or platforms.current.APP_OS
        self.home = home or os.path.expanduser("~")
        self.environ = os.environ if environ is None else environ
        self.env = os.path.abspath(args["--env"])
        self.launch = _shell_module(self.env, "launch")
        self.spawn = spawn or self.launch.detached   # a process apart, outliving this one (the watchdog, a hub)
        self.server = args["--server"].rstrip("/")
        # one channel (the owner's decision of 2026-11-09, m1917882): --channel nightly of an older line is taken and
        # read as stable — the hub reads stable only (modules/update/layer.py CHANNEL)
        self.channel = "stable"
        self.flavor = args["--flavor"]
        self.key = None
        self.instance = args.get("--as-instance")   # a second hub of this machine (`hub instance new`): its anchor
        self._found = {}         # state folder -> what `hub app find` said of it, this run
        self.anchor = {}
        self.notes = []          # lines said at the end (PATH, linger)
        self.step = "start"
        self.look = None         # what the terminal shows (look.py), when the install script asked for it: main() sets it

    # --- what is where

    def say(self, text):
        if not self._ui(lambda look: look.say(text)):
            print(text, file=self.out, flush=True)
        self.log(text)

    def _ui(self, draw):
        """draw(look) on the terminal's look. -> True when it was drawn; False when there is none (the words are said the plain
        way) or the drawing failed: it is turned off for good (said in the log), the install goes on as it always did."""
        if not self.look:
            return False
        try:
            draw(self.look)
            return True
        except Exception as e:  # noqa: BLE001 — a picture is never a reason to stop an install
            self.log(f"the look failed, the lines as they were: {type(e).__name__}: {e}")
            with contextlib.suppress(Exception):
                self.look.close()   # no more frames of the sign
            self.look = None
            return False

    def ask_person(self, prompt):
        """`ask`, with the live part of the screen taken off while the person types."""
        self._ui(lambda look: look.pause())
        try:
            return self.ask(prompt)
        finally:
            self._ui(lambda look: look.resume())

    def log(self, text):
        path = self.environ.get("HUB_INSTALL_LOG")
        if path:
            with contextlib.suppress(OSError), open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} setup.py: {text}\n")

    def state_dir(self):
        if self.args.get("--state"):
            return os.path.abspath(self.args["--state"])
        from core import daemon
        return daemon.default_state_dir()

    def windows(self):
        return self.os == "windows"

    def pointer(self, *parts):
        """A path through the hub's pointer <state>/app (the environment it runs from)."""
        return os.path.join(self.state, "app", *parts)

    def python(self):
        return self.pointer("runtime", "python", "python.exe") if self.windows() else \
            self.pointer("runtime", "python", "bin", "python3")

    def code(self):
        return os.path.join(self.state, "code")

    def layers(self):
        return os.path.join(self.state, "layers")

    def config_path(self):
        from core import catalog
        return catalog.config_path(self.state)

    def call(self, cmd, **kw):
        """A program of the system (systemctl, loginctl, launchctl): one that is not there, or hangs, is a failed run."""
        kw.setdefault("capture_output", True)
        kw.setdefault("timeout", 120)
        try:
            return self.run(cmd, **kw)
        except (OSError, subprocess.SubprocessError) as e:
            return subprocess.CompletedProcess(cmd, 127, "", str(e))

    def hub(self, code, *args, out=None, timeout=600, state=None, service=False):
        """`hub <args>` of a code folder with the environment's Python, as the owner in a terminal; service — it
        stops, starts or writes a service (a move, its undo, the update, a second hub): on Linux it needs the user's
        systemd within reach (user_manager)."""
        if service and self.os == "linux":
            self.user_manager()
        env = {k: v for k, v in self.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(self.user_env)
        env["HUB_STATE_DIR"] = state or self.state
        cmd = [sys.executable, "-B", *(["-X", "utf8"] if self.windows() else []), os.path.join(code, "bin", "hub"), *args]
        kw = {"stdout": out, "stderr": subprocess.STDOUT} if out is not None else {"capture_output": True}
        if out is not None:   # the program writes to the terminal itself: the live part is off meanwhile
            self._ui(lambda look: look.pause())
        with sigint_later() if service else contextlib.nullcontext():   # a move or its undo finishes its own way
            try:
                return self.run(cmd, stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=env,
                                timeout=timeout, cwd=self.home, **kw)
            except (OSError, subprocess.SubprocessError) as e:
                return subprocess.CompletedProcess(cmd, 1, "", str(e))
            finally:
                if out is not None:
                    self._ui(lambda look: look.resume())

    # --- 1. what is in the state folder

    def kind(self):
        """new | ours | git | other — and the clone's {"repo", "texts"} for git."""
        # its pointer to the environment, or this install's mark: `hub uninstall` without --purge took the pointer
        if ours(self.state):
            return "ours", None
        if self.args.get("--as-instance"):   # its folder holds the anchor `hub instance new` wrote, nothing of a hub yet
            return "new", None
        found = self.git_hub(self.state)
        if found:
            return "git", found
        if holds_hub(self.state):
            return "other", None
        return "new", None

    def git_hub(self, state):
        """`hub app find --json` of this code for a state folder (the environment's launch.py runs it), once a run."""
        if state not in self._found:
            self._found[state] = self.launch.git_hub(ROOT, state, sys.executable, run=self.run)
        return self._found[state]

    def git_instance(self):
        """A second hub of the machine from git that this line is for: --instance <name> whose folder (~/.hub-<name>)
        holds that hub, or the state folder given (HUB_STATE_DIR) holding one. -> (its state folder, what `hub app find`
        says of it) or (None, None): a new second hub, or a line of the main hub."""
        named = self.args.get("--instance")
        where = instance.folders(named)["state"] if named else self.state
        found = self.git_hub(where) if os.path.isdir(where) else None
        if second_from_git(found, named or (found or {}).get("instance")):
            return where, found
        return None, None

    def instance_hubs(self):
        """[(name, clone)] of the second hubs from git next to the default state folder (~/.hub-<name>): the line that
        moves each is said (the plain line moves only the main hub)."""
        import glob
        default = os.path.normpath(platforms.current.state_dir())
        head, base = os.path.split(default)
        out = []
        for path in sorted(glob.glob(os.path.join(glob.escape(head), glob.escape(base.replace("hub-core", "hub-", 1)) + "*"))):
            name = instance.name_of_folder(path)
            if not name or os.path.realpath(path) == os.path.realpath(self.state) \
                    or not os.path.isfile(os.path.join(path, "journal.jsonl")) or ours(path):
                continue
            found = self.git_hub(path)
            if second_from_git(found, name):
                out.append((name, found["repo"]))
        return out

    def hint_instances(self):
        """Each second hub from git: [(words, the line that moves it, that line without the key)]; their names in
        hint_names."""
        try:
            key = self.read_key()
        except Refused:
            key = ""
        hidden = None if self.flavor == "open" else "<key>"   # the open build's line has no key to hide
        out, self.hint_names = [], []
        for name, repo in self.instance_hubs():
            line = move_line(self.server, key or hidden, name, self.windows())
            out.append((t("cli.oneline.instance_git", name=name, repo=repo), line,
                        move_line(self.server, hidden, name, self.windows())))
            self.hint_names.append(name)
        return out

    hints = None   # not looked for yet
    hint_names = ()
    hints_said = False

    def say_hints(self):
        """The lines of hint_instances: to the terminal whole (the key is in the person's own line already), to the
        install log without the key."""
        if self.hints_said:   # the screen of the look said them; its closing failing must not say them twice
            return
        for words, line, masked in self.hints or ():
            if not self._ui(lambda look: look.say(words, verbatim=[line])):
                print(words, file=self.out, flush=True)
                print("  " + line, file=self.out, flush=True)
            self.log(f"{words} {masked}")
        self.hints_said = bool(self.look and self.hints)

    def choose(self, texts):
        """The move question: move | stay | later | None (nobody to ask)."""
        if self.args.get("--move"):
            return "move"
        if self.args.get("--stay"):
            return "stay"
        if self.args.get("--yes") or self.args.get("--no-input"):
            return None
        self.say(texts.get("text") or "")
        self.say(t("cli.oneline.choices", move=texts.get("move") or "move", stay=texts.get("stay") or "stay",
                   later=texts.get("later") or "later"))
        try:
            got = self.ask_person(t("cli.oneline.answer")).strip()
        except (EOFError, OSError):
            return "later"
        return {"1": "move", "2": "stay"}.get(got, "later")

    def no_main_hub(self):
        """The main hub's state folder (as the core finds it without HUB_STATE_DIR: the install's mark, else the OS's
        default) holds no hub: nothing of this install, of git or of anything else. Another folder the line was given
        (--state, HUB_STATE_DIR) is the person's choice: never asked about."""
        from core import statemark
        main = statemark.default_state() or platforms.current.state_dir()
        return os.path.normcase(os.path.realpath(self.state)) == os.path.normcase(os.path.realpath(main)) \
            and not ours(self.state) and not self.git_hub(self.state) and not holds_hub(self.state)

    def pick_instance(self):
        """No main hub here and second hubs from git next to its folder: the line never puts a new main hub unasked.
        -> an exit code (nothing installed), or None: the line goes on — for the hub chosen (--instance, --move) or for
        a new main hub next to them."""
        choice = self.choose_instance()
        self.log(f"no main hub, second hubs from git: {choice}")
        if choice is None:
            self.say(t("cli.oneline.instance_nobody"))
        elif choice == "which":
            self.say(t("cli.oneline.instance_which"))
        elif choice == "later":
            self.say(t("cli.oneline.later"))
        elif choice != "new":
            self.args = {**self.args, "--instance": choice, "--move": True}
            return None
        else:
            return None
        self.leave_unused()
        return OK if choice == "later" else NOBODY

    def leave_unused(self):
        """A line that installs nothing after an installer before this one (1.8.90) handed over: the state folder it
        made for the main hub holds only its layers (the key, this layer and its archive) — taken away again."""
        layers = self.layers()
        if not self.of_state():
            return
        try:
            if set(os.listdir(self.state)) - {".DS_Store"} != {"layers"} or \
                    not set(os.listdir(layers)) <= {"deliver.json", ARCHIVES, os.path.basename(ROOT), ".DS_Store"}:
                return
        except OSError:
            return
        self.log(f"{self.state}: only the layers an installer before made, taken away")
        with contextlib.suppress(OSError):
            appcode.remove_tree(self.state)

    def of_state(self):
        """This code is a layer of the state folder (<state>/layers/<name>)."""
        return os.path.normcase(os.path.dirname(os.path.realpath(ROOT))) == os.path.normcase(os.path.realpath(self.layers()))

    def choose_instance(self):
        """The name of the second hub from git to move | "new" (a main hub of its own next to them) | "later" | "which"
        (--move with more than one) | None (nobody to ask)."""
        names = list(self.hint_names)
        if self.args.get("--stay"):
            return "new"
        if self.args.get("--move"):
            return names[0] if len(names) == 1 else "which"
        if self.args.get("--yes") or self.args.get("--no-input"):
            return None
        self.say(t("cli.oneline.instance_ask"))
        options = [t("cli.oneline.instance_move", name=n) for n in names] + [t("cli.oneline.instance_new"),
                                                                            t("cli.oneline.instance_later")]
        for i, text in enumerate(options, 1):
            self.say(f"  {i} — {text}")
        try:
            got = self.ask_person(t("cli.oneline.answer") if len(options) == 3 else
                           t("cli.oneline.answer_any", last=len(options))).strip()
        except (EOFError, OSError):
            return "later"
        if got.isdigit() and 1 <= int(got) <= len(names):
            return names[int(got) - 1]
        return "new" if got == str(len(names) + 1) else "later"

    # --- 2. the key

    def read_key(self):
        """The build key of the line, or None: the open build has none (its releases are open to anyone)."""
        if self.flavor == "open":
            return None
        try:
            with open(self.args["--key-file"], encoding="utf-8") as f:
                key = f.read().strip()
        except OSError as e:
            raise Refused(t("cli.oneline.key_unread", error=str(e)))
        if not KEY.match(key):
            raise Refused(t("cli.oneline.key_bad"))
        self.key = key
        return key

    def keep_key(self):
        key = self.read_key()
        got = {"url": self.server, **({"key": key} if key else {})}
        shell = _read(os.path.join(self.env, "deliver.json"))
        if isinstance(shell.get("report"), str):   # the report address of the team's build
            got["report"] = shell["report"]
        from core import catalog
        platforms.current.makedirs_private(self.layers())
        path = os.path.join(self.layers(), "deliver.json")
        if _read(path) != got:
            catalog.write_private(path, got)

    # --- 3. the channel's latest layer

    def auth(self):
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    def latest(self):
        """The channel's release of releases.json, checked (modules/update/layer.entry_of), or None — no answer: said.
        Asked once a run."""
        if self._latest is not None:
            return self._latest[0]
        from core import fetch
        from modules.update import layer as updates
        try:
            status, body = fetch.small(self.server + "/v1/releases.json", private=self.auth())
            if status != 200:
                raise fetch.Failed("http", f"releases.json: HTTP {status}")
            self._latest = (updates.entry_of(json.loads(body), self.channel),)
        except (fetch.Failed, ValueError, updates.Refused) as e:
            self.say(self.network_words(e))
            self._latest = (None,)
        return self._latest[0]

    _latest = None

    def network_words(self, e):
        text = str(e)
        if "Authority Key Identifier" in text:
            return t("cli.oneline.tls_aki")
        if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
            return t("cli.oneline.tls_root", error=text[:300])
        return t("cli.oneline.offline", error=text[:300])

    def own_version(self):
        try:
            return appcode.load(ROOT)["version"]
        except appcode.Bad:
            return ""

    def take_layer(self, entry, parent):
        """The release's layer into the folder parent (its archive in parent/archives, as the update keeps it). -> its
        folder, or None — not taken (said: the install goes on with what it has)."""
        from core import fetch
        theirs = f"{entry['version']}-{self.flavor}"
        folder = os.path.join(parent, theirs)
        try:
            if appcode.diff(folder, appcode.load(folder)) == []:
                return folder
        except (OSError, appcode.Bad):
            pass
        lay = entry["layer"]
        url = lay["url"] if lay["url"].startswith(("https://", "http://")) else self.server + "/" + lay["url"].lstrip("/")
        if url.split("/")[2] != self.server.split("/")[2]:
            self.say(t("cli.oneline.bad_layer", error="releases.json: the layer is not on the delivery server"))
            return None
        archives = os.path.join(parent, ARCHIVES)
        os.makedirs(archives, exist_ok=True)
        archive = os.path.join(archives, f"code-{theirs}.tar.gz")
        self.say(t("cli.oneline.downloading", version=entry["version"], mb=f"{lay['size'] / 1e6:.0f}"))
        try:
            if not (os.path.isfile(archive) and os.path.getsize(archive) == lay["size"] and _sha(archive) == lay["sha256"]):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(archive)
                # the open build's: every redirect stays on its pages or the hosts of its releases' files
                allow = {"allow": lambda u: fetch.asset_ok(u, self.server)} if self.flavor == "open" else {}
                fetch.download(url, archive, lay["sha256"], lay["size"], f"code {entry['version']}",
                               "the install line", private=self.auth(), log=self.log, **allow)
            # another name is refused before anything is put in place: a layer of that name already there stays
            got, man = appcode.unpack(archive, parent, expected=theirs)
        except fetch.Failed as e:
            self.say(self.network_words(e) if e.why == "network" else t("cli.oneline.bad_layer", error=str(e)[:300]))
            return None
        except (appcode.Bad, OSError, EOFError, ValueError, tarfile.TarError) as e:
            with contextlib.suppress(OSError):
                os.unlink(archive)
            self.say(t("cli.oneline.bad_layer", error=str(e)[:300]))
            return None
        if entry.get("commit") and man.get("commit") != entry["commit"]:
            appcode.remove_tree(os.path.join(parent, got))
            self.say(t("cli.oneline.bad_layer", error=f"{got} is of the commit {man.get('commit')}, releases.json names "
                                                       f"{entry['commit']}"))
            return None
        return folder

    def early(self):
        """Step 0, before anything of the state folder is read: the channel's latest release, when it is newer than this
        code, downloaded and checked into a folder apart (the state folder may be another hub's, or one a question about
        must leave as it is) and its setup.py run with the same words. -> its exit code, or None: this code goes on (no
        newer release, no answer, a layer that does not check out, a key that is not one — each said by this code)."""
        if self.args.get("--handed"):
            return None
        try:
            self.read_key()
        except Refused:
            return None   # refused by the steps of this code
        entry = self.latest()
        if not entry or appcode.version_key(entry["version"]) <= appcode.version_key(self.own_version()):
            return None
        apart = self.apart()
        if not apart:
            return None
        code = None
        try:
            folder = self.take_layer(entry, apart)
            if not folder or not os.path.isfile(os.path.join(folder, SETUP)):
                return None
            self.log(f"handed over to {entry['version']} before the state folder is read")
            cmd = [sys.executable, "-I", "-B", *(["-X", "utf8"] if self.windows() else []), os.path.join(folder, SETUP),
                   *self.argv, "--handed"]
            def hand(look):   # the layer's setup.py draws the screen from here (its own words, the same step)
                os.environ["HUB_INSTALL_UI"] = look.hand_over()
            if not self._ui(hand):   # no look here, or it failed: the plain lines go on there too
                os.environ.pop("HUB_INSTALL_UI", None)
            code = self.run(cmd).returncode
            return code
        finally:   # the layer is the state folder's now; its archive stays for a run again after one that did not end
            keep = None if code == OK else f"code-{entry['version']}-{self.flavor}.tar.gz"
            for path in [os.path.join(apart, n) for n in _names(apart) if n != ARCHIVES] + \
                    [os.path.join(apart, ARCHIVES, n) for n in _names(os.path.join(apart, ARCHIVES)) if n != keep]:
                with contextlib.suppress(OSError):
                    if os.path.isdir(path) and not os.path.islink(path):
                        appcode.remove_tree(path)
                    else:
                        os.remove(path)

    def apart(self):
        """The folder step 0 checks a layer in: <data>/downloads/layer of the environment's folder (<data>/shells/<env>:
        the install's own, on the disk the script checked), or None — not one to write (the line goes on)."""
        shells = os.path.dirname(self.env)
        if os.path.basename(shells) != "shells":
            return None
        folder = os.path.join(os.path.dirname(shells), "downloads", "layer")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            self.log(f"{folder}: {e}")
            return None
        return folder

    argv = ()

    # --- 5. the layer, the pointers, the folders, the settings

    def place_layer(self, fresh):
        """<state>/code: a new hub or a move onto this code (a layer of this state folder: the handed-over one) or the
        environment's seed; then the environment's launch.py does the rest of a start (PACKAGED, the check). A run
        again: an edited layer gets its released files back, the edit kept as a copy."""
        link = self.code()
        mine = self.of_state()
        handed = not mine and self.handed_layer()
        if handed:   # the layer step 0 checked apart: its archive here — what a repair puts back from, what the update
            name = os.path.basename(ROOT)   # of a run again takes instead of a download
            archive = os.path.join(os.path.dirname(ROOT), ARCHIVES, f"code-{name}.tar.gz")
            kept = os.path.join(self.layers(), ARCHIVES, os.path.basename(archive))
            if os.path.isfile(archive) and _sha(kept) != _sha(archive):
                try:
                    os.makedirs(os.path.dirname(kept), exist_ok=True)
                    shutil.copyfile(archive, kept + ".part")
                    os.replace(kept + ".part", kept)
                except OSError as e:   # (the update downloads it then)
                    self.log(f"{kept}: {e}")
                    with contextlib.suppress(OSError):
                        os.remove(kept + ".part")
        if fresh and handed:   # and the layer itself (a copy checked against its MANIFEST)
            have = os.path.join(self.layers(), name)
            try:
                whole = appcode.diff(have, appcode.load(have)) == []
            except (OSError, appcode.Bad):
                whole = False
            if not whole:
                appcode.install(ROOT, self.layers())
            mine = True
        if fresh and mine:
            appcode.point(link, self.layers(), os.path.basename(ROOT))
        if not fresh:
            name = appcode.current(link, self.layers())
            if name:
                try:
                    changes = appcode.diff(os.path.join(self.layers(), name), appcode.load(os.path.join(self.layers(), name)))
                except (OSError, appcode.Bad):
                    changes = [{"path": appcode.MANIFEST}]
                if changes and self.launch.repair(self.state, name, "owner"):
                    rec = _read(os.path.join(self.layers(), "restored.json"))
                    self.say(t("cli.oneline.repaired", count=len(changes), copy=rec.get("copy") or "?"))
                    if self.page_up():   # the running hub moves onto the released files when its sessions are free
                        from core import catalog, restart
                        catalog.write_private(os.path.join(self.state, restart.FLAG),
                                              {"parts": [], "why": "the install line put the released code back"})
                    else:
                        self.restart_needed = True
        self.launch.ensure_layer(os.path.join(self.env, "code-seed"), os.path.join(self.env, "PACKAGED"), self.state,
                                 own_deliver=True, run=self.spawn)

    def handed_layer(self):
        """This code is a released layer apart from the state folder and the environment (the one step 0 of an
        installer checked there), as released: an edited one is not taken."""
        if not self.args.get("--handed") or \
                os.path.normcase(os.path.realpath(ROOT)) == os.path.normcase(os.path.realpath(os.path.join(self.env, "code-seed"))):
            return False
        try:
            man = appcode.load(ROOT)
            return appcode.name_of(man) == os.path.basename(ROOT) and appcode.diff(ROOT, man) == []
        except (OSError, appcode.Bad):
            return False

    restart_needed = False
    env_next = None   # a newer environment the line unpacked, left to the update's watchdog (a run again, spec §6.2)
    was_env = None    # the whole environment the pointer led to before this run moved it: the way back

    def env_whole(self, env):
        """The environment folder as unpacked (its sheet: deploy/oneline/unpack.py check) and its Python starting."""
        if not os.path.isfile(os.path.join(env, ENV_MARK)):
            return False
        r = self.call([env_python(env, self.windows()), "-I", "-B", os.path.join(ROOT, "deploy", "oneline", "unpack.py"), "check", env],
                      capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, timeout=900)
        return r.returncode == 0

    def choose_env(self):
        """A run again whose environment is not the one the hub runs on. That one broken: this run moves the pointer
        (the line put a whole one next to it). That one whole and older, with a watchdog that moves pointers, and the hub
        up: it stays, and the newer one is the update's to put in (it waits for busy sessions and comes back when the
        new one does not come up). Else the pointer moves, and the whole one it left is the way back."""
        link = os.path.join(self.state, "app")
        if not appcode.is_pointer(link):
            return
        cur = os.path.realpath(link)
        if os.path.normcase(cur) == os.path.normcase(os.path.realpath(self.env)) or not self.env_whole(cur):
            return
        cur_mark = _read(os.path.join(cur, "PACKAGED"))
        have, mine = cur_mark.get("runtime", 0), _read(os.path.join(self.env, "PACKAGED")).get("runtime", 0)
        if type(have) is not int or type(mine) is not int:
            have = mine = 0
        same_build = cur_mark.get("flavor") == _read(os.path.join(self.env, "PACKAGED")).get("flavor")
        if have > mine and same_build:   # the hub is on a newer one already (another channel's line): no step back
            self.log(f"the environment {cur} stays: {self.env} is older")
            self.env, self.launch = cur, _shell_module(cur, "launch")
            return
        if have < mine and "folder" in (cur_mark.get("swaps") or ()) and self.page_up():
            self.log(f"the environment {cur} stays; {self.env} is left to the update")
            self.env_next, self.env = self.env, cur
            self.launch = _shell_module(cur, "launch")
            from core import envs
            envs.keep_next(self.state, self.env_next)   # kept from the pruning until the update takes it
            return
        self.was_env = cur

    def point_env(self):
        """<state>/app → the environment (a link; a junction on Windows). A real folder there is not the install's.
        Moved under a hub of this install: its service starts it again on the new one."""
        link = os.path.join(self.state, "app")
        if os.path.lexists(link) and not appcode.is_pointer(link):
            raise Step("pointer", f"{link} is a folder, not the hub's pointer to its environment")
        from core import uninstall   # this install's hub from now on, also once the pointer is taken away
        uninstall.write_mark(self.state, os.path.dirname(os.path.dirname(self.env)))
        # Windows: a `hub uninstall` just before left these folders to go after it — they are this hub's again
        for folder in (self.env, os.path.dirname(os.path.dirname(self.env)), self.state):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(folder, uninstall.LATER))
        if appcode.is_pointer(link) and os.path.realpath(link) == os.path.realpath(self.env):
            return False
        ours = appcode.is_pointer(link)
        appcode.point(link, os.path.dirname(self.env), os.path.basename(self.env))
        if ours:
            self.log(f"the pointer moved to {self.env}")
            self.restart_needed = True
        return True

    def back_to(self, env):
        """The pointer back on the environment this run left, the service started again on it."""
        appcode.point(os.path.join(self.state, "app"), os.path.dirname(env), os.path.basename(env))
        self.restart_needed = True
        self.service_step()

    def folders(self):
        """<state>/bin with python3 (and git on a Mac), hub-home with the rules of the hub session, logs, sandbox, the
        mark of the state folder."""
        bin_dir = os.path.join(self.state, "bin")
        for d in ("hub-home", "logs", "bin") + (() if self.windows() else ("sandbox",)):
            os.makedirs(os.path.join(self.state, d), exist_ok=True)
        if self.windows():
            return   # hub-home, bin\hub.cmd: deploy/windows/setup.py (install.ps1)
        self._link(os.path.join(bin_dir, "python3"), self.python())
        if self.os == "mac":
            self._file(os.path.join(bin_dir, "git"), git_wrapper(self.pointer("runtime", "git")), 0o755)
        for name in ("CLAUDE.md", "ONBOARDING.md"):
            self._link(os.path.join(self.state, "hub-home", name), os.path.join(self.code(), "hub-home", name))
        if self.instance:
            return   # the mark names the main hub's folder: a second hub is found by its anchor
        from core import catalog, uninstall
        mark = os.path.join(catalog.config_dir(), "state-dir")
        named = _first_line(mark)
        # none, or one that names a folder holding no hub (a removed one): this hub is the main one now
        if not named or (os.path.realpath(named) != os.path.realpath(self.state) and not uninstall.marked(named)):
            os.makedirs(os.path.dirname(mark), exist_ok=True)
            with open(mark, "w", encoding="utf-8") as f:
                f.write(self.state + "\n")

    def _link(self, path, target):
        """A link at path to target, unless one is there already; a file a person put there stays (hub-home rules)."""
        if os.path.islink(path):
            if os.readlink(path) == target:
                return
            os.unlink(path)
        elif os.path.lexists(path):
            return
        os.symlink(target, path)

    def _file(self, path, text, mode=0o644):
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == text:
                    return False
        except (OSError, UnicodeDecodeError):
            pass
        tmp = f"{path}.{os.getpid()}.part"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return True

    def programs(self):
        """{"mcp": [...], "ffmpeg": path} of the environment, through the pointer (its paths.json)."""
        rel = _read(os.path.join(self.env, "paths.json"))
        where = lambda key: os.path.join(self.state, "app", *rel[key].split("/")) if isinstance(rel.get(key), str) else None  # noqa: E731
        out = {}
        if where("node") and where("mcp"):
            out["mcp"] = [where("node"), where("mcp")]
        if where("ffmpeg"):
            out["ffmpeg"] = where("ffmpeg")
        return out

    def settings(self, fresh):
        """hub.json: a new one with the hub session and the environment's programs; an existing one — only the programs
        that are unset, name this install's environments, or are gone (an earlier environment, a clone's npx path
        rewritten by the move) follow this environment. -> True when written."""
        path = self.config_path()
        progs = self.programs()
        if not os.path.exists(path):
            if self.windows():
                spec = importlib.util.spec_from_file_location("hub_windows_setup", os.path.join(ROOT, "deploy", "windows", "setup.py"))
                win = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(win)
                cfg = win.default_config(self.state)
            else:
                spec = importlib.util.spec_from_file_location("hub_first_run", os.path.join(ROOT, "tools", "package", "first_run.py"))
                first = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(first)
                cfg = first.config(self.state, root=self.env, mcp=progs.get("mcp"), ffmpeg=progs.get("ffmpeg"))
            for section, key, name in (("browser", "mcp", "mcp"), ("voice", "ffmpeg", "ffmpeg")):
                if name in progs:
                    cfg[section] = {key: progs[name]}
                else:   # the environment has no such program: the hub's own default, not a path that is not there
                    cfg.pop(section, None)
            if self.instance:   # its title, ports of its browsers, its update, engine (core/instance.py)
                cfg = instance.settings(cfg, self.anchor, self.state, instance._settings(
                    os.path.join(instance._default_config(), "hub.json")))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path + ".part", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(path + ".part", path)
            return True
        return refresh_programs(path, progs, self.env, os.path.dirname(os.path.realpath(self.env)))

    # --- 6. the commands

    def first_path(self):
        """The folders a terminal command of this hub puts first in PATH (Mac: git through the wrapper)."""
        first = [self.pointer("runtime", "node", "bin"), self.pointer("runtime", "gh", "bin")]
        if self.os == "mac":
            first.insert(0, os.path.join(self.state, "bin"))
        return first

    def commands(self):
        if self.windows():
            return   # bin\hub.cmd and the user's PATH: install.ps1
        if self.instance:   # hub-<name> and the <state>/bin/hub its sessions find first
            from deploy import commands
            commands.main(["--instance", self.instance, self.code(), self.python(), self.state,
                           self.anchor["config_dir"]])
            return
        from core import statemark
        default = statemark.default_state() or platforms.current.state_dir()
        if os.path.realpath(default) != os.path.realpath(self.state):   # `hub` of the terminal is the main hub's
            self.notes.append(t("cli.oneline.own_state", command=f'PATH="{":".join(self.first_path())}:$PATH" '
                                f'HUB_STATE_DIR="{self.state}" "{self.python()}" "{self.code()}/bin/hub"'))
            return
        from deploy import commands
        first = self.first_path()
        folder = os.path.join(self.home, ".local", "bin")
        os.makedirs(folder, exist_ok=True)
        for cmd in commands.NAMES:
            commands.put(folder, cmd, commands.wrapper(self.code(), self.python(), cmd, ":".join(first)),
                         say=lambda dst, cmd=cmd: self.say(t("cli.oneline.command_taken", path=dst,
                                                             command=f'"{self.python()}" "{self.code()}/bin/{cmd}"')))
        rcs = profile_line(self.home, self.environ)
        if rcs:
            self.notes.append(t("cli.oneline.path", rc=rcs[-1], files=", ".join(rcs)))

    # --- 7. the service

    def service_step(self):
        try:
            return self.service()
        except Step:
            raise
        except Exception as e:  # noqa: BLE001 — named for the person and the report, the move goes back
            raise Step("service", f"{type(e).__name__}: {e}") from e

    def service_id(self, kind):
        """The service's name of this hub: the main hub's, or a second hub's own (core/instance.py service_ids)."""
        if self.instance:
            return instance.service_ids(self.instance)[kind]
        return {"launchd": LABEL, "systemd": UNIT, "task": "Agents Hub"}[kind]

    def service(self):
        """-> True when the service was (re)written and started; False — it was right and running."""
        return {"mac": self.launchd, "linux": self.systemd, "windows": self.task}[self.os]()

    def start_cmd(self):
        return [self.python(), "-I", "-B", self.pointer("start.py")]

    def mac(self):
        """deploy/mac/app.py of this code: the agent's plist, the bundle's signature."""
        spec = importlib.util.spec_from_file_location("hub_mac_app", os.path.join(ROOT, "deploy", "mac", "app.py"))
        mac = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mac)
        return mac

    def launchd(self):
        mac = self.mac()
        # the code's ready bundle: the team's "Agents Hub.app" or the open flavor's "Hub by Icons8.app" (app.py NAMES)
        src = mac.shipped(os.path.join(ROOT, "deploy", "mac")) or os.path.join(ROOT, "deploy", "mac", BUNDLE)
        bundle = os.path.join(self.state, os.path.basename(src))
        label = self.service_id("launchd")
        if self.instance:   # the signed bundle is com.hub-core, the main hub's (its Login Items, its folder access): an
            # own com.hub-<name> where clang is (deploy/mac/install.sh does the same), else launchd runs Python itself
            if not (os.path.isdir(bundle) and mac.verified(bundle)) and shutil.which("clang"):
                try:
                    mac.build(bundle, identifier=label)
                except Exception as e:  # noqa: BLE001 — no bundle then, as without clang
                    self.log(f"the bundle of {label} did not build: {e!r}")
                    shutil.rmtree(bundle, ignore_errors=True)
        elif os.path.isdir(src) and _sha(mac.executable(bundle)) != _sha(mac.executable(src)):
            if os.path.lexists(bundle):
                shutil.rmtree(bundle)
            r = self.call(["ditto", src, bundle], capture_output=True, timeout=120)
            if r.returncode:
                shutil.rmtree(bundle, ignore_errors=True)
        ok = os.path.isdir(bundle) and mac.verified(bundle)
        agent = mac.agent_plist(self.python(), self.code(), self.state, bundle if ok else None, label=label,
                                env={**self.ports(), **self.own_config()})
        agent["ProgramArguments"] = ([mac.executable(bundle)] if ok else []) + self.start_cmd()
        agent["WorkingDirectory"] = self.state   # launchd starts no job whose folder is missing; start.py makes the code
        agent["EnvironmentVariables"]["PATH"] += ":/usr/sbin:/sbin"
        import plistlib
        path = os.path.join(self.home, "Library", "LaunchAgents", label + ".plist")
        try:
            with open(path, "rb") as f:
                have = plistlib.load(f)
        except Exception:  # noqa: BLE001 — no file, or one launchd cannot read either: written anew
            have = None
        if have is not None and not self.ours_launchd(have):
            raise Step("service", t("cli.oneline.service_foreign", path=path))
        target = f"gui/{os.getuid()}/{label}"
        loaded = self.call(["launchctl", "print", target], capture_output=True, timeout=30).returncode == 0
        if have == agent and loaded and not self.restart_needed:
            mac.drop_others(self.state, agent, run=self.call)
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as f:
            plistlib.dump(agent, f)
        os.replace(path + ".part", path)
        if ok:   # "Login Items" find the bundle by its identifier; the icon on its file for macOS without that
            self.call([mac.LSREGISTER, "-f", bundle], capture_output=True, timeout=60)
            with contextlib.suppress(Exception):
                mac.set_file_icon(bundle)
        self.call(["launchctl", "bootout", target], capture_output=True, timeout=120)
        for _ in range(3):   # bootout does not wait for the hub: launchd lets the label go after it exits, at most
            # ExitTimeOut (75 s) later, and a bootstrap before that is "error 5"; a refusal after it goes once more
            end = self.clock() + 90
            while self.call(["launchctl", "print", target], capture_output=True, timeout=30).returncode == 0 \
                    and self.clock() < end:
                self.sleep(1)
            r = self.call(["launchctl", "bootstrap", f"gui/{os.getuid()}", path], capture_output=True, text=True, timeout=60)
            if not r.returncode:
                break
            if "Input/output error" not in (r.stderr or ""):   # another refusal (the plist, the domain) goes at once
                raise Step("service", f"launchctl bootstrap: {(r.stderr or '').strip()[-300:]}")
            self.sleep(2)
        else:
            raise Step("service", f"launchctl bootstrap: {(r.stderr or '').strip()[-300:]}")
        mac.drop_others(self.state, agent, run=self.call)   # the other flavor's copy, once the agent is through this one
        return True

    def busy_words(self):
        return t("cli.oneline.busy", port=self.port()) + ("" if self.windows() else " " + t("cli.oneline.busy_ports"))

    def ours_launchd(self, agent):
        """The agent at com.hub-core is this state folder's hub (any install of it), not another program's."""
        env = agent.get("EnvironmentVariables") if isinstance(agent, dict) else None
        state = (env or {}).get("HUB_STATE_DIR") if isinstance(env, dict) else None
        return isinstance(state, str) and os.path.realpath(state) == os.path.realpath(self.state)

    def unit_text(self):
        q = _systemd_quote
        path = ":".join([os.path.join(self.state, "bin"), os.path.join(self.code(), "bin"),
                         os.path.join(self.home, ".local", "bin"), "/usr/local/bin", "/usr/bin", "/bin"])
        log = os.path.join(self.state, "logs", "hub-up.log")
        return (f"[Unit]\nDescription=Hub by Icons8{f' ({self.instance})' if self.instance else ''}\n"
                "After=network-online.target\n\n[Service]\n"
                f"ExecStart={' '.join(q(a) for a in self.start_cmd())}\nWorkingDirectory={q(self.state)}\n"
                f"Environment={q('PATH=' + path)}\nEnvironment={q('HUB_STATE_DIR=' + self.state)}\n"
                + "".join(f"Environment={q(k + '=' + v)}\n" for k, v in {**self.ports(), **self.own_config()}.items()) +
                "Restart=on-failure\nRestartSec=5\nOOMPolicy=continue\nTimeoutStopSec=75\n"
                f"StandardOutput=append:{log}\nStandardError=append:{log}\n\n[Install]\nWantedBy=default.target\n")

    def systemd(self):
        folder = os.path.join(self.environ.get("XDG_CONFIG_HOME") or os.path.join(self.home, ".config"), "systemd", "user")
        unit = self.service_id("systemd")
        path = os.path.join(folder, unit)
        text = self.unit_text()
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                have = f.read()
            if not unit_of(have, self.state):
                raise Step("service", t("cli.oneline.service_foreign", path=path))
        else:
            have = None
        if not self.user_manager():   # a container, WSL without systemd: the hub in the background until a restart
            if self.page_up():
                if self.restart_needed:   # the pointer moved under it: its own full restart comes up through it
                    self.hub(self.code(), "restart")
                    return True
                return False
            if self.restart_needed:   # a hub-up that does not answer (the way back): stopped, not a second one next to it
                r = self.hub(self.code(), "stop", timeout=180)
                if r.returncode and self.page_up():
                    raise Step("service", f"hub stop: {(r.stdout or '').strip()[-300:] or r.returncode}")
            self.spawn(self.start_cmd())
            self.notes.append(t("cli.oneline.no_systemd", command=" ".join(shlex.quote(a) for a in self.start_cmd())))
            return True
        active = self.systemctl("is-active", "--quiet", unit, timeout=30).returncode == 0
        if have == text and active and not self.restart_needed:
            return False
        os.makedirs(folder, exist_ok=True)
        self._file(path, text)
        for cmd in (["daemon-reload"], ["enable", unit], ["restart", unit]):
            r = self.systemctl(*cmd)
            if r.returncode:
                raise Step("service", f"systemctl --user {' '.join(cmd)}: {(r.stderr or '').strip()[-300:]}")
        return True

    SYSTEMD = "/run/systemd/system"   # there while systemd is the machine's init
    LINGER = "/var/lib/systemd/linger"   # logind's mark per user that lingers (readable by anyone)
    user_env = {}                     # what reaches the user's manager from a shell without a login session

    def systemctl(self, *args, timeout=120):
        env = {**self.environ, **self.user_env} if self.user_env else None
        return self.call(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout, env=env)

    def user_manager(self):
        """systemctl --user reaches this user's systemd: a login session (a terminal, ssh) gives it; a shell without
        one (`sudo -iu`, `su`, cron) has no XDG_RUNTIME_DIR — there linger starts the manager and its bus is at
        /run/user/<uid> (user_env: this run's systemctl and the `hub` it calls for a service get it). Either way
        linger is asked for: the hub of a server lives on after the person signs out. Asked once a run."""
        if self.manager is None:
            self.manager = self._user_manager()
        return self.manager

    manager = None

    def _user_manager(self):
        if self.systemctl("show-environment", timeout=30).returncode == 0:
            self.linger()
            return True
        if not os.path.isdir(self.SYSTEMD) or not self.linger():
            return False
        run = f"/run/user/{os.getuid()}"
        self.user_env = {"XDG_RUNTIME_DIR": run, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={run}/bus"}
        end = self.clock() + 20   # linger starts user@<uid>.service: its bus comes in a second or two
        while self.systemctl("show-environment", timeout=30).returncode:
            if self.clock() >= end:
                self.user_env = {}
                return False
            self.sleep(1)
        return True

    def user_name(self):
        try:
            import pwd
            return pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError):
            return self.environ.get("USER") or os.path.basename(self.home)

    def linger(self):
        """The hub without a login (a server, ssh): `loginctl enable-linger` for oneself — most systems let a user do
        it (polkit's set-self-linger: Ubuntu, Debian, Fedora); one that asks for a password gets the line with sudo
        (no password prompt here: the install runs on). -> linger is on."""
        user = self.user_name()
        if os.path.exists(os.path.join(self.LINGER, user)):
            return True
        r = self.call(["loginctl", "show-user", user, "-p", "Linger"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip() == "Linger=yes":
            return True
        cmd = ["loginctl", "--no-ask-password", "enable-linger", user]
        try:
            r = self.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        except FileNotFoundError:
            return False   # no loginctl at all: no systemd to keep the hub
        except (OSError, subprocess.SubprocessError) as e:   # it hung (logind, polkit): the person still gets the line
            r = subprocess.CompletedProcess(cmd, 1, "", str(e))
        self.log(f"loginctl enable-linger {user}: {r.returncode} {(r.stderr or '').strip()[-200:]}")
        if r.returncode == 0:
            # the install turned it on: `hub uninstall` of the last hub turns it off again (core/uninstall LINGER)
            from core import uninstall
            data = os.path.dirname(os.path.dirname(self.env))
            if os.path.basename(os.path.dirname(self.env)) == uninstall.SHELLS:
                with contextlib.suppress(OSError), open(os.path.join(data, uninstall.LINGER), "w", encoding="utf-8") as f:
                    f.write(json.dumps({"user": user}) + "\n")
            return True
        if t("cli.oneline.linger", user=user) not in self.notes:
            self.notes.append(t("cli.oneline.linger", user=user))
        return False

    def task(self):
        launcher = os.path.join(self.state, "hub-up.cmd")
        try:
            with open(launcher, "rb") as f:
                mine = b"start.py" in f.read(16384)
        except OSError:
            mine = False
        if mine and self.page_up() and not self.restart_needed and platforms.current.service_unit(self.state):
            return False
        ps1 = os.path.join(self.code(), "deploy", "windows", "install.ps1")
        env = {**self.environ, "HUB_INSTALL_NO_WAIT": "1", "HUB_INSTALL_OUTPUT": "utf-8"}
        env.pop("HUB_STATE_DIR", None)
        r = self.call(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ps1,
                       "-App", os.path.join(self.state, "app"), "-Shell", "-Yes",
                       *(["-Instance", self.instance] if self.instance else [])], cwd=self.home, env=env,
                      stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=900,
                      **platforms.current.apart())   # its own console: it writes UTF-8 for this log (install.ps1)
        self.log(f"install.ps1: exit {r.returncode}: {(r.stdout or '')[-1500:]} {(r.stderr or '')[-1500:]}")
        if r.returncode:
            raise Step("service", f"install.ps1: {((r.stderr or '') + (r.stdout or '')).strip()[-400:]}")
        return True

    # --- 8. the menu shortcut

    def shortcut(self):
        """The shortcut of this hub in the menu (core/shortcut.py): `hub open` of this state folder — Linux: the
        .desktop file, Windows: the .lnk through PowerShell. One file per hub, so a run again rewrites it and makes no
        second; a file of a person's under its name stays. Never a failed install: a line says what did not work."""
        name = self.instance or ""
        try:
            where = shortcut.path(self.os, name, self.home, self.environ)
            if not where:
                return
            made = self.shortcut_windows(where, name) if self.windows() else self.shortcut_linux(where, name)
            if self.windows():
                self.shortcut_former(where, name)
        except Exception as e:  # noqa: BLE001 — the hub works without a menu entry
            self.log(f"the shortcut: {type(e).__name__}: {e}")
            self.notes.append(t("cli.oneline.shortcut_failed", error=f"{type(e).__name__}: {e}"))
            return
        if made:
            self.notes.append(t("cli.oneline.shortcut_windows" if self.windows() else "cli.oneline.shortcut_linux",
                                name=shortcut.label(name)))

    def shortcut_linux(self, where, name):
        """-> True when the file was written (it was missing or different)."""
        if os.path.lexists(where) and not shortcut.ours(where):
            self.notes.append(t("cli.oneline.shortcut_foreign", path=where))
            return False
        png = os.path.join(self.code(), shortcut.ICON)
        argv = ["env", f"HUB_STATE_DIR={self.state}", *([f"HUB_CONFIG_DIR={self.anchor['config_dir']}"]
                                                        if self.instance else []),
                self.python(), "-B", os.path.join(self.code(), "bin", "hub"), "open"]
        os.makedirs(os.path.dirname(where), exist_ok=True)
        return self._file(where, shortcut.desktop(name, argv, t("cli.oneline.shortcut_comment"),
                                                  png if os.path.isfile(png) else None))

    def shortcut_windows(self, where, name):
        """The .lnk: cmd.exe, minimized, runs <state>\\bin\\hub.cmd open (it carries this hub's state folder, settings
        and environment); the icon from the picture of the code layer, made once. A .lnk that is not this install's (it
        names no hub.cmd) stays; one that is right, with its icon, is not made again (a run again starts no PowerShell).
        -> True when there was none."""
        hub_cmd = os.path.join(self.state, "bin", "hub.cmd")
        existed = os.path.lexists(where)
        if existed and not shortcut.ours(where):
            self.notes.append(t("cli.oneline.shortcut_foreign", path=where))
            return False
        ico = shortcut.icon_path(self.state)
        had_icon = os.path.isfile(ico)
        if not had_icon:   # a shortcut without a picture works as well
            tmp = f"{ico}.{os.getpid()}.part"
            try:
                with open(os.path.join(self.code(), shortcut.ICON), "rb") as f:
                    data = shortcut.ico(f.read())
                if data:
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, ico)
            except OSError as e:
                self.log(f"the shortcut's icon: {e}")
                with contextlib.suppress(OSError):
                    os.remove(tmp)
        icon = ico if os.path.isfile(ico) else ""
        if existed and shortcut.names(where, hub_cmd) and had_icon == bool(icon):
            return False
        env = {**self.environ, "HUB_LNK": where, "HUB_LNK_DIR": self.home, "HUB_LNK_ICON": icon,
               "HUB_LNK_TARGET": self.environ.get("ComSpec") or os.path.join(
                   self.environ.get("SystemRoot") or "C:\\Windows", "System32", "cmd.exe"),
               "HUB_LNK_ARGS": f'/c ""{hub_cmd}" open"', "HUB_LNK_TEXT": t("cli.oneline.shortcut_comment")}
        os.makedirs(os.path.dirname(where), exist_ok=True)
        script = base64.b64encode(shortcut.lnk_script().encode("utf-16-le")).decode()
        r = self.call(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-EncodedCommand", script], env=env, stdin=subprocess.DEVNULL, text=True, errors="replace",
                      timeout=120)
        if r.returncode:
            raise RuntimeError(f"powershell: {((r.stderr or '') + (r.stdout or '')).strip()[-300:] or r.returncode}")
        return not existed

    def shortcut_former(self, where, name):
        """This hub's .lnk under the name it had before (core/shortcut.former) goes once its own is in place: one
        entry in the Start menu."""
        hub_cmd = os.path.join(self.state, "bin", "hub.cmd")
        old = shortcut.former_path(where, name)
        if old and os.path.lexists(old) and shortcut.ours(old, hub_cmd) and shortcut.names(where, hub_cmd):
            try:
                os.remove(old)
                self.log(f"the shortcut under its former name went: {old}")
            except OSError as e:
                self.log(f"the shortcut under its former name stays: {e}")

    # --- 8. the page

    PORTS = ("HUB_WEB_PORT", "HUB_FILES_PORT")   # given to the line: the hub's own ports, kept in its service

    def ports(self):
        """The ports given to the line over the ones the hub's service already carries (a run again without them);
        read once a run."""
        if self._ports is None and self.instance:   # a second hub's own, handed out once (its anchor)
            got = self.anchor.get("ports") or {}
            self._ports = {k: str(got[f]) for k, f in zip(self.PORTS, ("web", "files")) if got.get(f)}
        if self._ports is None:
            given = {k: self.environ[k] for k in self.PORTS if str(self.environ.get(k) or "").isdigit()}
            have = {}
            if not self.windows():
                unit = platforms.current.service_unit(self.state, home=self.home) or {}
                have = {k: str(unit[f]) for k, f in zip(self.PORTS, ("port", "files_port")) if unit.get(f)}
            self._ports = {**have, **given}
        return self._ports

    _ports = None

    def own_config(self):
        """A second hub's settings folder for its service (the main hub's is the OS default one)."""
        return {"HUB_CONFIG_DIR": self.anchor["config_dir"]} if self.instance else {}

    def port(self):
        return instance.port("web", self.state, environ=self.ports())


    def page_up(self):
        import urllib.error
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # the machine's proxy is not for 127.0.0.1
        try:
            with opener.open(f"http://127.0.0.1:{self.port()}/api/hello", timeout=3):
                return True
        except urllib.error.HTTPError:
            return True   # the page's server answers
        except OSError:
            return False

    def wait_page(self, seconds=PAGE_WAIT):
        end = self.clock() + seconds
        while True:
            if self.page_up():
                return True
            if self.clock() >= end:
                return False
            self.sleep(1)

    def log_tail(self, n=5):
        try:
            with open(os.path.join(self.state, "logs", "hub-up.log"), encoding="utf-8", errors="replace") as f:
                return [x.rstrip() for x in f.readlines()[-n:]]
        except OSError:
            return []

    def open_page(self):
        """`hub open` (the link with the page key when that is on; no browser on a Linux without a display — the link
        only), --print for --no-open. -> the link it named."""
        r = self.hub(self.code(), "open", *(["--print"] if self.args.get("--no-open") else []), timeout=120,
                     service=True)
        lines = [x for x in (r.stdout or "").strip().splitlines() if x.startswith("http")]
        return lines[-1] if lines else f"http://127.0.0.1:{self.port()}/"

    # --- the report of a failed step

    def report(self, step, error, exc=None):
        try:
            from core import catalog
            from modules.update import report
            cfg = catalog.load_config(self.state) if os.path.exists(self.config_path()) else {}
            tail = ""
            with contextlib.suppress(OSError):
                with open(self.environ.get("HUB_INSTALL_LOG") or "", encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-20000:]
            report.Reporter(self.state, cfg).failed(f"install.{step}", self.own_version(), None, "failed", error, exc,
                                                    extra={"install": {"step": step, "platform": self.args.get("--platform"),
                                                                       "channel": self.channel, "env": self.env,
                                                                       "log": tail}})
        except Exception as e:  # noqa: BLE001 — a report never stops the install
            self.log(f"the report failed: {e!r}")

    # --- the whole

    def main(self):
        self.state = self.state_dir()
        os.environ["HUB_STATE_DIR"] = self.state   # the core's own reads (the settings, the ports) are of this hub
        if not self.instance and not self.args.get("--instance") and self.no_main_hub():
            self.hints = self.hint_instances()
            if self.hints:
                self.say_hints()
                code = self.pick_instance()
                if code is not None:
                    return code
        if not self.instance:
            code = self.broken_move(instance.folders(self.args["--instance"])["state"] if self.args.get("--instance")
                                    else self.state)
            if code is not None:
                return code
        there, own = (None, None) if self.instance else self.git_instance()
        if there:   # a second hub from git: moved as itself — its folder, its settings folder, its service and commands
            self.state, self.instance = there, own["instance"]
            self.anchor = instance.load(there) or {}   # `hub app move` writes it when the hub stands from before anchors
            os.environ["HUB_STATE_DIR"], os.environ["HUB_CONFIG_DIR"] = there, own["config_dir"]
        elif self.instance:
            self.anchor = instance.load(self.state) or {}
            if self.anchor.get("name") != self.instance or not self.anchor.get("config_dir"):
                raise Refused(t("cli.oneline.no_anchor", state=self.state, name=self.instance))
            os.environ["HUB_CONFIG_DIR"] = self.anchor["config_dir"]
        elif self.windows() and os.path.normcase(self.state) != os.path.normcase(platforms.current.state_dir()):
            raise Refused(t("cli.oneline.windows_state", state=platforms.current.state_dir()))
        if self.args.get("--instance") and not there:
            where = instance.folders(self.args["--instance"])["state"]
            if not ours(where) and holds_hub(where):   # a hub `hub instance new` would install over: not this line's
                raise Refused(foreign(where))
            return self.second_hub()
        kind, found = self.kind()
        self.log(f"state {self.state}: {kind}")
        if not self.instance and self.hints is None:   # (said already when the line asked about them)
            self.hints = self.hint_instances()
            self.say_hints()
        if kind == "other":
            raise Refused(foreign(self.state))
        if self.windows() and any(self.environ.get(k) for k in self.PORTS):
            raise Refused(t("cli.oneline.ports_windows"))
        if self.os == "linux" and kind != "ours" and re.search(r"[\s\"'\\]", self.state):   # a unit file cannot name it
            raise Refused(t("cli.oneline.state_spaces", state=self.state))
        if kind == "new" and self.page_up():   # another program, or another account's hub, answers on the port
            raise Refused(self.busy_words())
        moving = None
        if kind == "git":
            choice = self.choose(found["texts"])
            self.log(f"a hub from git ({found['repo']}): {choice}")
            if choice is None:
                self.say(t("cli.oneline.nobody", repo=found["repo"]))
                return NOBODY
            if choice == "stay":
                self.hub(ROOT, "app", "stay")
                self.say(t("cli.oneline.stayed", repo=found["repo"]))
                return OK
            if choice == "later":
                self.say(t("cli.oneline.later"))
                return OK
            moving = found
        self.keep_key()
        fresh = kind != "ours"
        try:
            return self.install(fresh, moving, self.latest())
        except Step as e:
            self.failed(t("cli.oneline.ui_failed_at", step=e.step, error=e.error),
                        t("cli.oneline.failed", step=e.step, error=e.error, log=self.environ.get("HUB_INSTALL_LOG") or ""))
            self.report(e.step, e.error, e)
            if moving and self.moved:
                return self.undo(moving)
            return FAILED
        except KeyboardInterrupt:   # Ctrl+C once the clone's hub may be stopped: never left so
            from core import appmove
            if moving and not self.move_done and (appmove.record(self.state) or {}).get("status") == "moving":
                self.undo(moving, words="cli.oneline.interrupted_back")
            raise

    moved = False
    move_done = False
    finish_move = False   # a hub up on the app whose move a run before did not mark done (broken_move)

    def broken_move(self, state):
        """A move of this folder's hub that a run of the line before broke off (the terminal closed, the machine went
        down): its record still says "moving" and no hub of this install runs the folder — the clone's hub stopped, on
        Linux its unit taken away and `hub` pointed at code not there yet. Undone first (every saved file back, the
        clone's service on), then the line goes on as asked. A hub up on the app whose `--done` was lost is marked done
        once it is up again. -> an exit code when the undo did not finish, else None."""
        from core import appmove, restart
        rec = appmove.record(state)
        if not rec or rec.get("status") != "moving":
            return None
        up = restart.running(state)
        if up and not runs_clone(up, rec.get("repo")) and not rec.get("returning"):
            self.finish_move = ours(state)   # this install's hub is up (its --done lost), or another install's
            return None
        self.log(f"a move broken off before: {rec.get('repo')}")
        code = os.path.join(state, "code")
        code = code if os.path.isfile(os.path.join(code, "bin", "hub")) else ROOT
        r = self.hub(code, "app", "undo", "--failed", out=self.out, state=state, service=True)
        self._found.pop(state, None)   # its clone's service is back: what `hub app find` says of it now
        if r.returncode:
            self.say(t("cli.oneline.undo_failed", log=self.environ.get("HUB_INSTALL_LOG") or "",
                       command=instance.command_name(rec.get("instance"))))
            return FAILED
        self.say(t("cli.oneline.broken_back", repo=rec.get("repo") or "?"))
        return None

    def install(self, fresh, moving, entry):
        if moving:
            self.step = "move"
            self.say(moving["texts"].get("waiting") or "")
            r = self.hub(ROOT, "app", "move", moving["repo"], "--app", self.env, out=self.out, service=True,
                         timeout=self.launch.MOVE_TIMEOUT)
            if r.returncode == 4:   # it failed midway and its return did not finish (appmove.NotBack, said above)
                self.say(t("cli.oneline.undo_failed", log=self.environ.get("HUB_INSTALL_LOG") or "",
                           command=instance.command_name(self.instance)))
                return FAILED
            if r.returncode:
                self.say(t("cli.oneline.move_failed", repo=moving["repo"]))
                return FAILED
            self.moved = True
            if self.instance:   # its anchor now (an adopted one is written by the move): its ports, settings folder
                self.anchor, self._ports = instance.load(self.state) or {}, None
                if not self.anchor.get("config_dir"):
                    raise Step("move", f"no anchor of the hub {self.instance} in {self.state}")
            if self.page_up():   # the clone's hub is stopped: what answers on the port is another program's
                raise Step("service", self.busy_words())
        steps = (("env", self.choose_env), ("layer", lambda: self.place_layer(fresh)), ("pointer", self.point_env),
                 ("folders", self.folders),
                 ("settings", lambda: self.settings(fresh)),
                 ("commands", self.commands))
        for name, step in steps:
            self.step = name
            try:
                step()
            except Step:
                raise
            except Exception as e:  # noqa: BLE001 — named for the person and the report
                raise Step(name, f"{type(e).__name__}: {e}") from e
        self._ui(lambda look: (look.done(t("cli.oneline.ui_setup_done")), look.begin(5, t("cli.oneline.ui_start_now"))))
        self.step = "service"
        started = self.service_step()
        if not self.look:   # (the live line of the step says it)
            self.say(t("cli.oneline.starting"))
        up = self.wait_page()
        if not up and not started:   # the service was right, the hub is not up: a restart of it
            self.restart_needed = True
            self.service_step()
            up = self.wait_page()
        if not up and self.was_env:   # the environment it left was whole: the hub goes back on it
            self.say(t("cli.oneline.env_back", env=self.env, was=self.was_env))
            self.report("page", "the page did not answer on the new environment", None)
            self.back_to(self.was_env)
            if self.wait_page():
                self.open_page()
                return NOT_UP
        if not up:
            words = t("cli.oneline.not_up", seconds=PAGE_WAIT, log=os.path.join(self.state, "logs", "hub-up.log"))
            tail = ["  " + line for line in self.log_tail()]
            self.failed(words + ("\n" + "\n".join(tail) if self.look else ""), words, tail)
            self.report("page", "the page did not answer", None)
            return self.undo(moving) if moving else NOT_UP
        if moving or self.finish_move:
            self.hub(self.code(), "app", "move", "--done", service=True)
            self.move_done = True
        if moving:
            self.notes.append(t("cli.oneline.moved", command=instance.command_name(self.instance)))
        if not fresh and entry and self.newer(entry):
            self.hub(self.code(), "update", service=True)
            self.notes.append(t("cli.oneline.update", version=entry["version"]))
        if self.env_next:
            self.notes.append(t("cli.oneline.env_next", env=self.env_next))
        self.shortcut()
        with contextlib.suppress(Exception):   # the environments no hub of the machine needs any more
            from core import envs
            envs.prune(self.state, say=self.log)
        url = self.open_page()
        command = instance.command_name(self.instance)
        if self.look:
            self.say_hints()
            tunnel = self.tunnel_lines(url)
            if self._ui(lambda look: (look.done(t("cli.oneline.ui_start_done", port=self.port())),
                                      look.finish(t("cli.oneline.ui_done"),
                                                  [(t("cli.oneline.ui_open"), url), (t("cli.oneline.ui_command"), command),
                                                   (t("cli.oneline.ui_next"), t("cli.oneline.ui_next_text"))],
                                                  self.notes + ([tunnel[0]] if tunnel else []),
                                                  verbatim=[tunnel[1]] if tunnel else []))):
                return OK
        for line in self.notes:
            self.say(line)
        self.say_hints()
        self.say(t("cli.oneline.done", url=url, command=command))
        self.say_tunnel(url)
        return OK

    def tunnel_lines(self, url):
        """A Linux without a display (a server, install.sh --server): the page is on 127.0.0.1 here — what to say and the one
        SSH command that brings it to the person's computer (install.sh --server shows the last such line at every SSH
        sign-in). -> (words, command), or None."""
        if self.os != "linux":
            return None
        from core import remote
        from platforms import posix
        if posix.has_browser(self.environ, "linux"):   # the rule of `hub open`
            return None
        return t("cli.service.tunnel", url=url), remote.tunnel(self.port(), instance.port("files", self.state, environ=self.ports()),
                                                               self.user_name(), self.environ)

    def say_tunnel(self, url):
        lines = self.tunnel_lines(url)
        if lines:
            self.say(lines[0])
            self.say("  " + lines[1])

    def failed(self, words, plain, more=()):
        """The install stopped: the words for the screen of the look, or `plain` (and `more`, line by line) as they were said."""
        if self._ui(lambda look: look.fail(words, log=self.environ.get("HUB_INSTALL_LOG") or "")):
            self.log(words)
            return
        self.say(plain)
        for line in more:
            self.say(line)

    def newer(self, entry):
        name = appcode.current(self.code(), self.layers())
        try:
            have = appcode.load(os.path.join(self.layers(), name))["version"] if name else ""
        except appcode.Bad:
            return False
        return appcode.version_key(entry["version"]) > appcode.version_key(have)

    def undo(self, moving, words="cli.oneline.undone"):
        code = self.code() if os.path.isfile(os.path.join(self.code(), "bin", "hub")) else ROOT
        if self.hub(code, "app", "undo", "--failed", out=self.out, service=True).returncode == 0:
            self.say(t(words, repo=moving["repo"], log=os.path.join(self.state, "logs", "hub-up.log")))
            return MOVED_BACK
        self.say(t("cli.oneline.undo_failed", log=self.environ.get("HUB_INSTALL_LOG") or "",
                   command=instance.command_name(self.instance)))
        return FAILED

    def second_hub(self):
        """--instance: `hub instance new <name>` of the main hub (spec of several hubs §2.5): its folders, ports,
        service and command; no main hub — first the install without it."""
        from core import uninstall
        name = self.args["--instance"]
        kind, found = self.kind()
        if not uninstall.marked(self.state) and kind == "new":
            raise Refused(t("cli.oneline.no_main"))
        code = found["repo"] if kind == "git" else self.code()
        if not os.path.isfile(os.path.join(code, "bin", "hub")):
            raise Refused(t("cli.oneline.no_main"))
        r = self.hub(code, "instance", "new", name, "--yes", out=self.out, timeout=900, service=True)
        return OK if r.returncode == 0 else FAILED


@contextlib.contextmanager
def sigint_later():
    """Ctrl+C while a `hub` that stops or starts a service runs (a move, its undo): the child has it from the terminal
    too and puts back what it changed — not killed 0.25 s later by subprocess.run's KeyboardInterrupt here; this run
    raises it once the child is done."""
    import signal
    import threading
    got = []
    if threading.current_thread() is not threading.main_thread() or \
            signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        yield
        return
    old = signal.signal(signal.SIGINT, lambda *a: got.append(True))
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, old)
    if got:
        raise KeyboardInterrupt


def runs_clone(up, repo):
    """The hub-up of hub-up.json (restart.running) is the clone's own: its command line names <repo>/bin/hub-up."""
    line = os.path.normcase(platforms.current.command_line(up.get("pid")) if isinstance(up, dict) else "")
    if not isinstance(repo, str) or not repo or not line:
        return False
    return any(os.path.normcase(os.path.join(r, "bin", "hub-up")) in line
               for r in {os.path.abspath(repo), os.path.realpath(repo)})


def _names(folder):
    try:
        return os.listdir(folder)
    except OSError:
        return []


def second_from_git(found, name):
    """What `hub app find` said of a folder is a second hub of this machine from git, of that name."""
    return bool(found) and isinstance(found.get("instance"), str) and found["instance"] == name \
        and isinstance(found.get("config_dir"), str)


def ours(state):
    """The hub of the state folder is this install's: its pointer to the environment, or the install's mark."""
    from core import uninstall
    return appcode.is_pointer(os.path.join(state, "app")) or uninstall.oneline(state)


def holds_hub(state):
    """The state folder holds a hub (its journal or rules, its data, or a service of it): never one to put a new hub in."""
    from core import uninstall
    return uninstall.marked(state) or any(os.path.lexists(os.path.join(state, n)) for n in HUB_DATA) \
        or bool(platforms.current.service_unit(state))


def foreign(state):
    """The words of a hub the line does not take: another install's, or a hub's data nothing runs (its hub moved to
    another machine) — said as such."""
    return t("cli.oneline.foreign" if platforms.current.service_unit(state) else "cli.oneline.foreign_stopped",
             state=state)


def move_line(server, key, name, windows):
    """The one line that moves the second hub `name` from git: the person's own line with --instance (key None: the
    open build's line, the scripts at the root of its pages)."""
    url = f"{server}/i/{key}/install" if key else f"{server}/install"
    if windows:
        return f"& ([scriptblock]::Create((irm {url}.ps1))) -Instance {name}"
    return f"curl -fsSL {url}.sh | bash -s -- --instance {name}"


def env_python(env, windows):
    """The Python of an environment folder (core/shellnext.py python_of, for an OS given here)."""
    return os.path.join(env, "runtime", "python", *(("python.exe",) if windows else ("bin", "python3")))


def git_wrapper(git):
    """The Mac environment's git (dugite) finds its helpers (https!) and templates only through these; through the
    pointer, so a new environment brings its own git with no new wrapper."""
    g = shlex.quote(git)
    return (f"#!/bin/sh\n# git of the hub's environment (deploy/oneline/setup.py): dugite's git needs these\nG={g}\n"
            'GIT_EXEC_PATH="$G/libexec/git-core" GIT_TEMPLATE_DIR="$G/share/git-core/templates" exec "$G/bin/git" "$@"\n')


def _first_line(path):
    """The folder a state-dir mark names, or "" (none, empty, unreadable)."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _systemd_quote(word):
    """A word of a unit file: quoted when it has a space or a quote (systemd's own quoting)."""
    if re.fullmatch(r"[^\s\"'\\]+", word):
        return word
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_of(text, state):
    """The unit text runs this state folder's hub (hub-up of an install from git, start.py of this one)."""
    lines = text.splitlines()
    env = {}
    for x in lines:
        if x.startswith("Environment="):
            with contextlib.suppress(ValueError):
                for w in shlex.split(x[len("Environment="):]):
                    if "=" in w:
                        k, v = w.split("=", 1)
                        env[k] = v
    runs = any(x.startswith("ExecStart=") and ("hub-up" in x or "start.py" in x) for x in lines)
    held = os.path.realpath(env.get("HUB_STATE_DIR") or os.path.expanduser("~/.hub-core"))
    return runs and held == os.path.realpath(state)


def refresh_programs(path, progs, env, shells):
    """browser.mcp and voice.ffmpeg of an existing hub.json follow this environment when they are unset, name programs
    of an environment of this install (the folder of the environments) or of the environment itself, or a program that
    is gone; a value the owner set otherwise stays. Written as the hub writes it (the lock). -> True when changed."""
    from core import catalog
    inside = [os.path.normcase(os.path.join(os.path.realpath(p), "")) for p in (env, shells)]

    def ours(value):
        if value is None:
            return True
        items = value if isinstance(value, list) else [value]
        if not items or not all(isinstance(v, str) for v in items):
            return False
        first = items[0]
        if not os.path.isabs(first):
            return False   # a bare name ("npx") is the owner's
        real = os.path.normcase(os.path.realpath(first))
        return any(real.startswith(p) for p in inside) or not os.path.exists(first)

    want = {"browser": {"mcp": progs.get("mcp")}, "voice": {"ffmpeg": progs.get("ffmpeg")}}
    changed = []
    try:
        with catalog.edit_settings(path) as cfg:
            for section, values in want.items():
                for key, value in values.items():
                    if value is None:
                        continue
                    have = cfg.get(section)
                    if have is None:
                        have = cfg[section] = {}
                    if not isinstance(have, dict):
                        continue
                    if have.get(key) != value and ours(have.get(key)):
                        have[key] = value
                        changed.append(f"{section}.{key}")
            if not changed:
                raise LookupError
    except LookupError:
        return False
    return True


def profile_line(home, environ):
    """~/.local/bin in PATH of new terminals: the one marked line in the profile the person's shell reads (zsh —
    ~/.zshrc; bash — the first of ~/.bash_profile, ~/.bash_login, ~/.profile, and on Linux ~/.bashrc too), as
    deploy/mac/link-commands.sh. -> the files written now (the last one to `source`), [] — in PATH already, there
    already, another shell."""
    folder = os.path.join(home, ".local", "bin")
    if folder in (environ.get("PATH") or "").split(os.pathsep):
        return []
    shell = os.path.basename(environ.get("SHELL") or "")
    if shell == "zsh" or (not shell and sys.platform == "darwin"):
        files = [os.path.join(home, ".zshrc")]
    elif shell == "bash":
        login = next((os.path.join(home, f) for f in (".bash_profile", ".bash_login", ".profile")
                      if os.path.exists(os.path.join(home, f))), os.path.join(home, ".bash_profile"))
        # a terminal window of a Linux desktop is a bash that is not a login one: it reads ~/.bashrc alone
        files = [login] + ([os.path.join(home, ".bashrc")] if sys.platform != "darwin" else [])
    else:
        return []
    wrote = []
    for rc in files:
        try:
            with open(rc, encoding="utf-8", errors="replace") as f:
                if PATH_MARK in f.read():
                    continue
        except FileNotFoundError:
            pass
        with open(rc, "a", encoding="utf-8") as f:
            f.write(f'\n# {PATH_MARK} in PATH — the commands hub and hub-telegram\n{PATH_LINE}\n')
        wrote.append(rc)
    return wrote


def _terminal_look(job):
    """What the terminal shows (look.py), when install.sh asked for it in HUB_INSTALL_UI and this output is a terminal; the
    install's own step 4 starts at once. None — the lines as they always were."""
    if not job.environ.get("HUB_INSTALL_UI"):
        return None
    try:
        names = {4: t("cli.oneline.ui_step_setup"), 5: t("cli.oneline.ui_step_start"),
                 "fail": t("cli.oneline.ui_stopped"), "log": t("cli.oneline.ui_log")}
        look = _look_module().Look.from_env(job.environ, job.out, names)
    except Exception:   # noqa: BLE001 — a picture is never a reason not to install
        return None
    if look:
        try:
            look.begin(look.step, t("cli.oneline.ui_setup_now"))
        except Exception:   # noqa: BLE001
            return None
    return look


def main(argv, **kw):
    try:
        args = parse(argv)
    except ValueError as e:
        print(f"Hub by Icons8 install: an argument the installer does not know: {e}", file=sys.stderr)
        return USAGE
    job = Install(args, **kw)
    job.argv = [a for a in argv if a != "--handed"]
    job.look = _terminal_look(job)
    try:
        code = job.early()
        return job.main() if code is None else code
    except Refused as e:
        job.failed(str(e), str(e))
        return FAILED
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 — a person reads it: the step and the cause, the traceback in the log
        import traceback
        job.log(traceback.format_exc())
        error = f"{type(e).__name__}: {e}"
        job.failed(t("cli.oneline.ui_failed_at", step=job.step, error=error),
                   t("cli.oneline.failed", step=job.step, error=error, log=job.environ.get("HUB_INSTALL_LOG") or ""))
        return FAILED
    finally:
        job._ui(lambda look: look.end())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
