import atexit
import os
import shlex
import sys
import tempfile

import platforms
from core import embed as _embed
from core import net  # noqa: F401 — the tests' own requests to 127.0.0.1 go direct, bypassing the machine's proxy, as in the hub

# an embed's tests/ folder joins this package (core/embed.py): its tests are tests.test_<name> like the core's
__path__ = _embed.extend(__path__, "tests")

# One directory per run, and everything temporary lives in it: test mkdtemp, subprocesses (TMPDIR), hubd, CLI and the
# e2e Chrome; it is removed on exit (2026-10-04: the disk filled up — 16k directories from tests, ~10k a day, the system
# cleans them only after 3 days). On start, clear what was left behind (a run killed by an update rollback or a timeout):
# runs older than an hour with no live process (pid marker) and Chrome litter that it puts outside TMPDIR (~5k directories)
_base = tempfile.gettempdir()
try:
    from modules.testchrome import litter as _litter
    if os.name != "nt":
        _litter.stop_run_chromes(_base)   # before sweeping directories: a swept run is also recognised by its profile path
    _litter.sweep_runs(_base)
    if os.name != "nt":
        _litter.sweep(_base)
except Exception:  # noqa: BLE001 — cleanup must not get in the way of the run
    pass
_RUN = tempfile.mkdtemp(prefix="hub-core-tests-run-", dir=_base)
with open(os.path.join(_RUN, "pid"), "w") as _f:
    _f.write(str(os.getpid()))
tempfile.tempdir = _RUN
os.environ["TMPDIR"] = _RUN


atexit.register(platforms.current.rmtree, _RUN)

# Tests do not see the machine's real settings (~/.config/hub-core/hub.json): on a VM it has the hub and projects enabled.
# Except the tests' browser (testchrome.path): regular Chrome on macOS blinks in the Dock on every launch (187 times in
# 8 minutes of runs from sessions) — take it before the substitution, as the hub update does (modules/update)
try:
    from modules.testchrome import chrome as _chrome
    if _chrome.configured():
        os.environ[_chrome.ENV] = _chrome.configured()
except Exception:  # noqa: BLE001 — no setting — headless-shell or Chrome for Testing
    pass
# Tests do not see the environment of hub processes: a run from a hub process or its session carries HUB_* (the earlier
# umask HUB_USER_UMASK, HUB_PERMS_KEEP, the service, ports, state), and running a test directly went red (2026-10-26:
# test_perms.Kept with HUB_USER_UMASK=0o2). Drop every HUB_* except the run's own inputs: HUB_TEST_* (browser, claude,
# selections), the real claude and the modules repo; HUB_COMPAT_* — the release. The values below we set ourselves
# (HUB_CONFIG, HUB_STATE_DIR, HUB_LANG…). The update self-check does the same for tools/test.py (modules/update
# run_tests). After the tests' browser is chosen above: it looks for the machine's hub.json by the HUB_CONFIG/HUB_STATE_DIR
# of the hub process
for _name in [k for k in os.environ if k.startswith("HUB_") and not k.startswith(("HUB_TEST_", "HUB_COMPAT_"))
              and k not in ("HUB_REAL_CLAUDE", "HUB_REPO", "HUB_CORE_REPO")]:
    os.environ.pop(_name)
for _name in ("AGENTS_HUB_SELF_CHECK_BROWSER", "AGENTS_HUB_SELF_CHECK_UPSTREAM", "AGENTS_HUB_SELF_CHECK_SECONDS"):   # the updater's word to its self-check:
    os.environ.pop(_name, None)   # not to the tests, which build their own updaters and runners (tools/test.py)
os.environ["PYTHONUTF8"] = "1"   # for test subprocesses (CLI, hubd): on Windows their output would otherwise be cp1251, and tests read UTF-8
os.environ["HUB_CONFIG"] = os.path.join(tempfile.gettempdir(), "hub-core-tests-config.json")
# by default a new hub has an update from the shared repo and system agents (core/system.py) — core tests have none of
# them, except their own tests
with open(os.environ["HUB_CONFIG"], "w") as _f:
    _f.write('{"update": {"enabled": false}}')
os.environ["HUB_NO_SYSTEM_AGENTS"] = "1"
os.environ["HUB_NO_INVITE"] = "1"   # a hub session in a test is no new hub to greet its owner (core/system.invite_off)
# there is no official marketplace catalog (core/marketplaces.OFFICIAL): the tests' hub-up does not download it from GitHub
os.environ["HUB_NO_OFFICIAL_MARKETPLACE"] = "1"
# the language of the core's human-facing texts (core/i18n.py current) is English: tests check them in English; the Russian
# owner's path has its own tests (tests/test_ru_owner.py, the page's RussianOwner class); another language — i18n.use()
os.environ["HUB_LANG"] = "en"
# ...and do not live inside a live hub session. Tests are also run from a session (dev on a Mac): its HUB_SESSION made
# the CLI commands in tests act as «the session's word» (the schedule e2e failed), and HUB_STATE_DIR pointed at the live
# core — a test without its own directory would write into it. Own empty state directory, own empty bot setting.
for _name in ("HUB_SESSION", "HUB_CLI_WAIT", "HUB_CONFIG_DIR", "HUB_UP_STOP", "HUB_UP_INNER", "HUB_UP_RESTARTED",
              "HUB_UP_CHILD", "HUB_TREE"):
    os.environ.pop(_name, None)
# the machine's own endpoint or model (core/modelrule.py: then the executor pairs are empty) — not in core tests: a hub
# update with them in the environment would otherwise be red and rolled back (PR review)
for _name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
    os.environ.pop(_name, None)
# ...and the hub session's environment on the account gateway (modules/gateway session_env) and the pool (modules/accounts
# AUTH_ENV): from such a session tests would otherwise see a foreign header, the login stub and the gateway flags (a false
# red test_gateway.Off)
for _name in ("ANTHROPIC_CUSTOM_HEADERS", "ENABLE_TOOL_SEARCH", "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL",
              "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HUB_ACCOUNT",
              "HUB_GATEWAY_STUB", "HUB_GATEWAY_UPSTREAM"):
    os.environ.pop(_name, None)
_PROXIED = any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"))
# gateway additions to NO_PROXY — only when there is no proxy in the environment: with a proxy they keep the tests' 127.0.0.1 out of it
if not _PROXIED:
    for _name in ("NO_PROXY", "no_proxy"):
        _rest = [x for x in os.environ.get(_name, "").split(",") if x.strip() and x.strip() not in ("127.0.0.1", "localhost")]
        if _rest:
            os.environ[_name] = ",".join(_rest)
        else:
            os.environ.pop(_name, None)


def _own_addresses_past_the_proxy():
    """With a machine proxy, git and curl of the tests (subprocesses read only the environment; urllib — core.net) reach
    the tests' own servers on 127.0.0.1 directly: the machine's NO_PROXY of both cases plus our addresses, in both cases
    (2026-11-03: behind a company proxy the git of tests.test_update_net went to 127.0.0.1 through it — a red update
    self-check). On Windows the two cases are one variable."""
    if _PROXIED:
        both = [x.strip() for k in ("no_proxy", "NO_PROXY") for x in os.environ.get(k, "").split(",") if x.strip()]
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = net.no_proxy({"NO_PROXY": ",".join(dict.fromkeys(both))})


_own_addresses_past_the_proxy()
os.environ["HUB_STATE_DIR"] = tempfile.mkdtemp(prefix="hub-core-tests-state-")
# and its own settings directory: the real one holds the Claude account pool (accounts.json), the bot, contacts — an
# enabled owner pool used to switch the «account» in core tests and needlessly brought up a session (two restart tests went red)
os.environ["HUB_CONFIG_DIR"] = tempfile.mkdtemp(prefix="hub-core-tests-config-")
os.environ["HUB_TELEGRAM_CONFIG"] = os.path.join(tempfile.gettempdir(), "hub-core-tests-no-telegram.json")
# Tests do not call a live model (a run of the personal hub's update tests once launched the watchdog's `claude -p`) —
# a test that needs a model substitutes it itself
os.environ["HUB_NO_MODEL"] = "1"
# ...and do not send hub statistics to the real server and do not ask GitHub for a login (modules/stats): sending goes
# only to their own receiver on 127.0.0.1
os.environ["HUB_NO_STATS"] = "1"
# ...and do not send a report of a failed update to the real Sentry (modules/update/report.py): only to a receiver on
# 127.0.0.1
os.environ["HUB_NO_REPORT"] = "1"
# ...and do not download Claude Code from Anthropic (core/claudeinstall: an install script run with -y and no claude would
# fetch ~300 MB): a test that needs the downloads starts its own fake server and points this at it
os.environ["HUB_CLAUDE_DOWNLOADS"] = "http://127.0.0.1:9"
# ...and expect one Claude Code version of their own: publish.sh writes the stable channel's version into every snapshot's
# core/version.py CLAUDE_TESTED, and the hubs' update self-check runs these tests on that snapshot — fake claudes of a fixed
# version (2.1.309, 2.1.400) must not turn red the day stable passes them (core/version.py reads it, subprocesses too)
os.environ["HUB_TEST_MIN_CLAUDE"] = "2.1.218"
if "core.version" in sys.modules:   # imported above already: the same value in this process
    sys.modules["core.version"].MIN_CLAUDE = os.environ["HUB_TEST_MIN_CLAUDE"]
# ...and as a working copy, which names no Claude Code of its own (core/ownclaude.release): a snapshot's pinned version
# would send the update's self-check to the fake downloads; a test that needs a release sets its own
os.environ["HUB_TEST_CLAUDE_RELEASE"] = ""
# ...and do not reach Cloudflare (modules/cloudflare: the API of `hub remote cloudflare setup`, the cloudflared download):
# a test that needs them starts its own fake server and points these at it
os.environ["HUB_CLOUDFLARE_API"] = "http://127.0.0.1:9/client/v4"
os.environ["HUB_CLOUDFLARED_DOWNLOADS"] = "http://127.0.0.1:9"
# ...and do not see the machine's git settings: the update self-check runs on everyone's machine with their git (a global
# ignore `**/.claude/settings.local.json` on a Mac turned the test «not in .gitignore — do not write» red, and v1.3.0 was
# rolled back). Own global config instead of ~/.gitconfig and XDG git/config, no system one; the default ignore and
# attributes live in XDG_CONFIG_HOME — give them empty files. The machine's XDG_CONFIG_HOME itself is also removed: it is
# where systemd units and the hub settings directory are looked up, while tests put them in their own HOME (with the
# machine's XDG_CONFIG_HOME the service test went red)
os.environ.pop("XDG_CONFIG_HOME", None)
_empty = os.path.join(_RUN, "git-empty")
open(_empty, "w").close()
os.environ["GIT_CONFIG_GLOBAL"] = os.path.join(_RUN, "gitconfig")
with open(os.environ["GIT_CONFIG_GLOBAL"], "w") as _f:
    _f.write('[core]\n\texcludesFile = "%s"\n\tattributesFile = "%s"\n' % ((_empty.replace("\\", "/"),) * 2))
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
# ...and git settings from the environment: the caller's `git -c` (GIT_CONFIG_PARAMETERS, GIT_CONFIG_COUNT/KEY_n/VALUE_n), a foreign name
for _name in [k for k in os.environ if k in ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")
              or k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_AUTHOR_", "GIT_COMMITTER_"))]:
    os.environ.pop(_name)
# ...and do not touch the Mac keychain: a real claude in the tests (a finished MCP sign-in, test_mcp_login_live) stores its
# token through `security add-generic-password`, and with the tests' temporary HOME macOS finds no login keychain and
# shows the owner «A keychain cannot be found to store …» during the update self-check (2026-10-30, v1.6.0). A `security`
# first in PATH answers as an empty keychain that takes no writes: a lookup or a removal — 44 «no such item», anything
# else (a write, also `-i`) — 50 «no default keychain». Claude Code then keeps the sign-in in its credentials file (any
# other code on a read it takes for «unreadable» and does not save the sign-in at all; 36 is «locked, retry later»).
# Written here, not kept in the tree: an archive without the X bit would let the real one through
if os.name != "nt":
    _bin = os.path.join(_RUN, "bin")
    os.makedirs(_bin)
    with open(os.path.join(_bin, "security"), "w") as _f:
        _f.write('#!/bin/sh\ncase "$1" in\n'
                 'find-*|delete-*) echo "security: The specified item could not be found in the keychain (hub tests)." >&2'
                 '; exit 44;;\n'
                 '-i) cat >/dev/null;;\nesac\n'
                 'echo "security: A default keychain could not be found (hub tests)." >&2\nexit 50\n')
    os.chmod(os.path.join(_bin, "security"), 0o755)
    # ...and `python3` from PATH — the fakes (Chrome, MCP, claude) and tools/publish.sh start it by their `#!` line — is the
    # run's interpreter: the shim of a version manager (mise, asdf) finds its version through the profile, and the run's home
    # below has none (a full run with such a shim: test_publish and test_browser red)
    with open(os.path.join(_bin, "python3"), "w") as _f:
        _f.write('#!/bin/sh\nexec %s "$@"\n' % shlex.quote(sys.executable))
    os.chmod(os.path.join(_bin, "python3"), 0o755)
    os.environ["PATH"] = _bin + os.pathsep + (os.environ.get("PATH") or "/usr/bin:/bin")
# Hub sessions get the env from the owner's Claude Code settings (catalog.user_env): login and proxy are there. Tests get an empty directory.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="hub-core-tests-claude-")
# ...and do not write into the person's profile: the update self-check runs the suite in everyone's real profile, and a
# test that forgets its own home (`home=`, HOME) would write into ~/.claude.json, ~/.local/bin, ~/Library/LaunchAgents,
# %APPDATA% (test research 03.11, finding 18). The run gets its own home folder; of the Windows and XDG ones only those the
# machine has are replaced (the XDG ones then follow HOME). Taken from the real profile before that, as a test saw them:
# the Python user packages for child processes (pip --user: tzdata on Windows, cryptography), REAL_HOME — only to find a
# program installed there (the real claude of the live tests), never to write — and the tests' browser, if it was found
# there (chrome-headless-shell in ~/.cache): a set HUB_TEST_CHROME is the run's input and stays as it is
import site  # noqa: E402
os.environ.setdefault("PYTHONUSERBASE", site.getuserbase())
REAL_HOME = os.environ.setdefault("HUB_TEST_REAL_HOME", os.path.expanduser("~"))   # a nested run: still the person's
# ...and no codex of a test runs without a CODEX_HOME of its own: the guard first in PATH refuses one without it or with
# the person's ~/.codex (tools/codex_guard.py; a real codex on the machine only — without one nothing changes)
from tools import codex_guard as _codex_guard  # noqa: E402
_guard = os.path.join(_RUN, "codex-guard")
if _codex_guard.install(_guard, REAL_HOME):
    os.environ["PATH"] = _guard + os.pathsep + (os.environ.get("PATH") or "")
try:
    from modules.testchrome import chrome as _chrome
    _found = None if os.environ.get(_chrome.ENV) else _chrome.find()
except Exception:  # noqa: BLE001 — none found — the page tests are skipped, as without the substitution
    _found = None
_home = os.path.join(_RUN, "home", "user")
_dirs = {"HOME": _home, "USERPROFILE": _home, "APPDATA": os.path.join(_home, "AppData", "Roaming"),
         "LOCALAPPDATA": os.path.join(_home, "AppData", "Local")}
for _name, _path in _dirs.items():
    if _name == "HOME" or _name in os.environ:
        os.makedirs(_path, exist_ok=True)
        os.environ[_name] = _path
for _name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
    os.environ.pop(_name, None)
try:
    if _found and _chrome.find() != _found:
        os.environ[_chrome.ENV] = _found
except Exception:  # noqa: BLE001 — without the profile none is found: the one from it
    os.environ[_chrome.ENV] = _found
