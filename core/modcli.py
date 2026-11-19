"""`hub <command>` commands from core modules (the cli point) and `hub doctor` (the doctor point) — Module API, spec §2.1.

A module declares a command in module.json: `"commands": {"todo": "help"}` (the cli right). Core commands win: a name
that bin/hub knows never goes to a module. One command declared by two modules — a refusal naming them, not a guess.
The core calls the module's /hook/cli {command, args, caller, cwd} and prints its answer {out, err, code}; no answer
within the budget — code 1 and a line saying what to look at (`hub doctor`), a failure — module.error in the core
mailbox, as for any call. The answer {stdin: true} (2.8) — the command needs stdin: the core reads it (up to MAX_STDIN
characters) and calls the module again with the stdin field.
"""
import os
import shutil
import subprocess
import sys
import time

from core import catalog, i18n, journal, modrun, modules, version

ALIVE_FRESH = 60   # the hubd pulse (hubd.alive) is older — the core is not running
MAX_STDIN = 64 * 1024   # stdin of a module command — a token, a key, short text; a request to a module — up to 1 MB


def commands(state_dir):
    """{command: [(module, help)]} for modules hubd says to keep, in load order."""
    out = {}
    for name, w in sorted(modrun.wants(state_dir).items(), key=lambda x: (x[1].get("order", 0), x[0])):
        cmds = modrun.manifest_of(state_dir, name).get("commands")
        for cmd, about in (cmds.items() if isinstance(cmds, dict) else ()):
            out.setdefault(cmd, []).append((name, str(about)))
    return out


def caller(session):
    """Who calls the command: a session, a plugin or module process (hub-up) — otherwise a person at the terminal, the owner.
    A schedule job's command or check (HUB_SCHEDULE_JOB, modules/scheduler/clock.py) also names its job (schedule_job):
    a command job runs as the owner without a session, and a module that keeps something for a session (an owner's reply
    goes to it) finds the job's session by it — as bin/hub speaker() marks the facts of such a command."""
    name = os.environ.get("HUB_SCHEDULE_JOB")
    job = {"schedule_job": name} if name else {}
    if session:
        return {"session": session, **job}
    for env, who in (("HUB_EXTENSION", "plugin"), ("HUB_MODULE", "module")):
        if os.environ.get(env):
            return {who: os.environ[env]}
    return {"owner": True, **job}


def run(state_dir, cmd, args, caller, core=()):
    """A module command: the exit code, or None — modules have no such command (and for the core — help). core — core names."""
    if cmd in core:
        return None
    owners = commands(state_dir).get(cmd)
    if not owners:
        return None
    if len(owners) > 1:
        print(i18n.t("cli.modcmd.two_owners", cmd=cmd, modules=", ".join(n for n, _ in owners)), file=sys.stderr)
        return 2
    name = owners[0][0]
    st = journal.load_state(state_dir)
    mods = modrun.Modules(state_dir, records=lambda: st.get("modules") or {})
    if mods.ready(name) is None:
        print(i18n.t("cli.modcmd.not_ready", name=name), file=sys.stderr)
        return 1
    ask = {"command": cmd, "args": list(args), "caller": caller, "cwd": os.getcwd()}
    got = mods.call(name, "cli", ask)
    if got is not None and got.get("stdin"):
        # the module needs stdin (the `hub accounts add` token — not as an argument, not in terminal history): read only now
        if sys.stdin is not None and sys.stdin.isatty():   # a person at the terminal: show what is expected, not silence
            import platforms
            print(i18n.t("cli.modcmd.paste", cmd=cmd, keys=i18n.t(platforms.current.EOF_KEYS)), file=sys.stderr, flush=True)
        data = sys.stdin.read(MAX_STDIN + 1) if sys.stdin is not None else ""
        if len(data) > MAX_STDIN:
            print(i18n.t("cli.modcmd.stdin_big", cmd=cmd, max=MAX_STDIN), file=sys.stderr)
            return 1
        got = mods.call(name, "cli", {**ask, "stdin": data})
        if got is not None and got.get("stdin"):
            print(i18n.t("cli.modcmd.stdin_again", name=name), file=sys.stderr)
            return 1
    if got is None:
        print(i18n.t("cli.modcmd.no_answer", name=name, cmd=cmd), file=sys.stderr)
        return 1
    sys.stdout.write(got["out"])
    sys.stderr.write(got["err"])
    sys.stdout.flush()
    return got["code"]


def _program(run, folder):
    """Whether the module process program exists: a path from the module folder or a name in PATH."""
    prog = run[0] if run else ""
    if prog in modrun.PYTHON_WORDS:   # the launch will take the hub's Python (modrun.command)
        return True
    if os.path.isabs(prog) or "/" in prog or os.sep in prog:
        return os.path.exists(os.path.join(folder, prog))
    return bool(shutil.which(prog) or os.path.exists(os.path.join(folder, prog)))


def codex_copy_rows(state_dir):
    """The hub's Codex sign-in is the same one as another hub's of this machine (a copied folder: the spec of several
    hubs, 6) — the first token refresh of either breaks the other's."""
    from core import instance
    try:
        other = instance.codex_copy_of(state_dir)
    except Exception:  # noqa: BLE001 — the registry unreadable: this row must not bring down the whole hub doctor
        return []
    if not other:
        return []
    from core import codexmodel
    own = codexmodel.hub_command(state_dir)
    return [("fail", i18n.t("cli.doctor.what_codex_login"), i18n.t("cli.doctor.codex_copied", command=other, cmd=own))]


def codex_checks(state_dir):
    """Codex (experimental): a hub on Codex — the codex binary and its version, the hub's Codex sign-in (account/read
    in its Codex folder), the plan and the limits."""
    from core import codexcli, codexmodel
    from hosts.codex import codex_bin
    exe = codex_bin(state_dir)
    found = exe if os.path.isfile(exe) else shutil.which(exe)
    if not found:
        return [("fail", "codex", i18n.t("cli.doctor.codex_missing", exe=exe, cmd=codexmodel.hub_command(state_dir)))]
    try:
        # with the hub's CODEX_HOME (codex refuses one that does not exist — created first): without it even --version
        # writes its helper folder into the personal ~/.codex/tmp
        codexcli._prepare(state_dir)
        ver = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL, env=codexmodel.env(state_dir)).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        ver = ""
    from core import codexinstall
    if ver and codexinstall.VERSION.search(ver) and not codexinstall.newer_or_same(ver, codexinstall.MIN_CODEX):
        key = "cli.doctor.codex_old_own" if codexinstall.mine(found, state=state_dir) else "cli.doctor.codex_old"
        return [("fail", "codex", i18n.t(key, have=codexinstall.VERSION.search(ver).group(0), exe=found,
                                         need=codexinstall.MIN_CODEX, cmd=codexmodel.hub_command(state_dir)))]
    if ver and not codexinstall.complete(found, state=state_dir):   # the helper or the sandbox's bubblewrap is not beside it
        gone = next(p for p in codexinstall.needs(found) if not os.path.isfile(p))   # the first one that is missing
        if gone == codexinstall.helper_of(found):
            return [("fail", "codex", i18n.t("cli.doctor.codex_no_helper", exe=found, helper=os.path.basename(gone),
                                             cmd=codexmodel.hub_command(state_dir)))]
        return [("fail", "codex", i18n.t("cli.doctor.codex_no_bwrap", exe=found, file=os.path.relpath(
            gone, os.path.dirname(found)), cmd=codexmodel.hub_command(state_dir)))]
    out = [("ok", "codex", f"{found}" + (f" ({ver})" if ver else ""))]
    login, limits = i18n.t("cli.doctor.what_codex_login"), i18n.t("cli.doctor.what_codex_limits")
    try:
        rpc = codexcli.Rpc(state_dir)
        try:
            got = codexcli.account(rpc)
            if not got:
                return out + [("fail", login, i18n.t("cli.doctor.codex_not_signed", cmd=codexmodel.hub_command(state_dir)))]
            out.append(("ok", login, f"{got[0]}" + (", " + i18n.t("cli.codex.plan", plan=got[1]) if got[1] else "")))
            try:
                out.append(("ok", limits, codexcli.windows_line(rpc.call("account/rateLimits/read", {}))))
            except RuntimeError as e:
                out.append(("warn", limits, i18n.t("cli.doctor.limits_unread", error=e)))
        finally:
            rpc.close()
    except (OSError, RuntimeError) as e:
        out.append(("fail", login, i18n.t("cli.doctor.codex_silent", error=e)))
    return out


def checks(state_dir, claude, repo=None, ports=None, login=None):
    """[(ok|warn|fail, what, details)]: settings, claude, its sign-in, hubd, an update waiting for
    modules, the page and "Files" against proxies, modules and their own checks. repo — the core working copy (this one by
    default); ports — {"web": n, "files": n} of this hub (none — the page is not asked); login(claude, env) — Claude Code's
    own sign-in status (core/claudeauth.status; none — the row is not there: tests do not start a real claude)."""
    out = []
    path = catalog.config_path(state_dir)
    try:
        cfg = catalog.load_config(state_dir)
        out.append(("ok", i18n.t("cli.doctor.what_settings"), path))
    except (OSError, ValueError) as e:
        cfg = {}
        out.append(("fail", i18n.t("cli.doctor.what_settings"), f"{path}: {e}"))
    found = None
    if cfg.get("host") == "codex":   # Codex (experimental): the whole hub on Codex — claude is not needed
        out += codex_checks(state_dir)
    else:
        found = claude[0] if os.path.isfile(claude[0]) else shutil.which(claude[0])
        out.append(("ok", "claude", found) if found else
                   ("fail", "claude", i18n.t("cli.doctor.claude_missing", exe=claude[0])))
    out += codex_copy_rows(state_dir)
    try:
        fresh = time.time() - os.path.getmtime(os.path.join(state_dir, "hubd.alive")) < ALIVE_FRESH
    except OSError:
        fresh = False
    out.append(("ok", "hubd", i18n.t("cli.doctor.hubd_up")) if fresh else
               ("warn", "hubd", i18n.t("cli.doctor.hubd_down")))
    try:
        st = journal.load_state(state_dir)
    except (OSError, ValueError):
        st = {}
    if found and login is not None:
        out.append(login_row([found, *claude[1:]], st, cfg, login))
    waits = st.get("update_check") if isinstance(st.get("update_check"), dict) else {}
    if waits.get("result") == "needs-module":   # a release without a function's built-in code waits for its module (update)
        out.append(("warn", i18n.t("cli.doctor.what_update"),
                    i18n.t("cli.doctor.update_waits", modules="; ".join(waits.get("need_module") or []) or "?")))
    from core import profiles   # gateway of the built-in pool or of a login module
    if profiles.gateway_enabled(cfg) or st.get("gateway"):   # account gateway: if it was enabled — its state, trouble — fail
        prof = profiles.Profiles(state_dir, config=lambda: cfg, providers=lambda: st.get("providers"))
        level, text = prof.gateway_summary(st.get("gateway"), time.time(), cfg)
        out.append(({"bad": "fail", "warn": "warn"}.get(level, "ok"), i18n.t("cli.doctor.what_gateway"), text))
    out += proxy_rows(ports or {}, state_dir)
    out += password_rows()
    try:   # remote access through Cloudflare: nothing while never set up; keys, tunnel and reach apart
        from modules.cloudflare import cli as cloudflare
        out += cloudflare.doctor_rows(state_dir)
    except Exception:  # noqa: BLE001 — the provider's rows must not bring down the whole hub doctor
        pass
    try:   # the hub bus: off — one line; on — the keys, the library, OpenSSH, the relay, the last failed setup step
        from core import busflow
        out += busflow.doctor_rows(state_dir, st)
    except Exception:  # noqa: BLE001 — the bus rows must not bring down the whole hub doctor
        pass
    recs = st.get("modules") or {}
    found = modules.discover(cfg, state_dir)
    if not found:
        out.append(("ok", i18n.t("cli.doctor.what_modules"), i18n.t("cli.doctor.no_modules")))
        return out
    blocked = {n: r.get("reason") or i18n.t("cli.doctor.stopped_failures") for n, r in recs.items()
               if r.get("state") in modrun.STOPPED}
    plan = modules.resolve(found, cfg, catalog=set(st["catalog"]) if isinstance(st.get("catalog"), dict) else None,
                           blocked=blocked)
    now = time.time()
    for r in plan:
        what, rec = i18n.t("cli.doctor.what_module", name=r["name"]), recs.get(r["name"]) or {}
        if r["state"] == "disabled":
            out.append(("ok", what, r["reason"]))
        elif r["state"] == "blocked":
            out.append(("fail", what, r["reason"]))
        elif modules.processless(r["manifest"]):
            plugin = r["manifest"].get("web")
            if isinstance(plugin, str) and not modules.web_error(plugin) \
                    and not os.path.isfile(os.path.join(r["dir"], *plugin.split("/"))):
                out.append(("fail", what, i18n.t("cli.doctor.static_no_file", file=plugin)))
            else:
                out.append(("ok", what, i18n.t("cli.doctor.module_static", version=r["version"])))
        elif not _program(r["manifest"].get("run") or [], r["dir"]):
            out.append(("fail", what, i18n.t("cli.doctor.no_program", program=r["manifest"]["run"][0])))
        elif modrun.status(rec, now) == "suspended":
            last = rec.get("last") or {}
            out.append(("warn", what, i18n.t("cli.doctor.suspended", until=time.strftime("%H:%M", time.localtime(rec["until"])),
                                             hook=last.get("hook"), error=last.get("error"))))
        elif rec.get("ready"):
            out.append(("ok", what, i18n.t("cli.doctor.module_ready", version=r["version"])))
        else:
            out.append(("warn", what, i18n.t("cli.doctor.module_not_ready", version=r["version"])))
    mods = modrun.Modules(state_dir, records=lambda: recs)
    for name in mods.answering("doctor"):
        got = mods.call(name, "doctor", {"api": version.MODULE_API})
        if got is None:
            out.append(("fail", f"{name}", i18n.t("cli.doctor.module_silent")))
            continue
        out += [("ok" if c["ok"] else "fail", f"{name} · {c['name']}", c["detail"]) for c in got]
    return out


def login_row(command, st, cfg, login):
    """Whether Claude Code on this machine is signed in, as the host asks it before a spawn (hosts/streamjson signed_in):
    no sign-in and no pool — sessions do not come up; a login module's pool — a warning (its accounts are not asked
    about); the built-in accounts pool with a usable account — ok, the normal state of a hub on its own accounts.
    Called from a session — without the session's own login keys (they are
    its pool profile, not the machine's)."""
    from core import modlaunch, profiles, providers
    env = dict(os.environ)
    if env.get("HUB_SESSION"):
        for k in (*modlaunch.AUTH_ENV, "ANTHROPIC_BASE_URL", modlaunch.HEADERS, profiles.STUB_ENV, profiles.UPSTREAM_ENV):
            env.pop(k, None)
    what = i18n.t("cli.doctor.what_claude_login")
    if any(env.get(k) for k in modlaunch.AUTH_ENV) or modlaunch.provider(env) or profiles.on_gateway(env):
        return "ok", what, i18n.t("cli.doctor.claude_env")   # the host does not ask either
    try:
        got = login(command, env)
    except Exception:  # noqa: BLE001 — the row must not bring down the whole hub doctor
        got = None
    pool = providers.active("profile", cfg, st.get("providers") or {})
    if got is None:
        if not pool and _builtin_pool():   # the answer is not needed: the host gives the sessions the pool's tokens
            return "ok", what, i18n.t("cli.doctor.claude_pool_builtin")
        return "warn", what, i18n.t("cli.doctor.claude_unknown")
    if got.get("loggedIn"):
        return "ok", what, (" · ".join(str(got[k]) for k in ("authMethod", "email") if got.get(k))
                            or i18n.t("cli.doctor.claude_signed"))
    if pool:
        return "warn", what, i18n.t("cli.doctor.claude_pool_module", pool=pool)
    if _builtin_pool():   # the hub's own accounts (accounts.json): the host gives their tokens
        return "ok", what, i18n.t("cli.doctor.claude_pool_builtin")   # the normal state of a hub on its own accounts
    from core import packaged   # the app's text names only the band, no terminal command (app spec §4a)
    return "fail", what, i18n.t("cli.doctor.claude_not_signed_app" if packaged.is_app(packaged.info())
                                else "cli.doctor.claude_not_signed")


def _builtin_pool():
    """The built-in accounts pool works (accounts.json: not paused) and has an account with a token: the host gives sessions
    its tokens (or a command that issues one). Read from the file, without asking a login module. Not in the tree (the
    open flavor, core/cutout.py) — no."""
    from core import cutout
    if not cutout.present("profile"):
        return False
    from modules.accounts import accounts
    pool = accounts.load()
    return accounts.enabled(pool) and any(a.get("token") or a.get("command") for a in pool["accounts"])   # not "login"


def password_rows():
    """The page's password (core/pagepass.py): set; a damaged file (closed for everyone); the old link with a key on and
    no password yet; none — no row."""
    from core import instance, pagepass
    st = pagepass.state()
    if st == "open":
        return []
    cmd = instance.command_name(instance.name())
    level, key = {"password": ("ok", "cli.doctor.password_password"), "key": ("warn", "cli.doctor.password_key"),
                  "broken": ("fail", "cli.doctor.password_broken")}[st]
    return [(level, i18n.t("cli.doctor.what_password"), i18n.t(key, cmd=cmd))]


def proxy_rows(ports, state_dir=None):
    """The page and "Files" refuse a request a proxy or tunnel passed on to localhost (modules/tailnet/access.py): a
    self-probe of each running process, and a hint about proxy programs connected to its port (cloudflared not, while
    the Cloudflare provider is on and has the keys: the hub checks who comes through it)."""
    from modules.tailnet import access
    checked = set()
    if state_dir:
        try:
            from modules.cloudflare import remote as cloudflare
            checked = {"cloudflared"} if cloudflare.status(state_dir)["ok"] else set()
        except Exception:  # noqa: BLE001 — the hint stays as it was
            pass
    out = []
    for part, port in ports.items():
        refuses = access.refuses_proxied(port)
        if refuses is False:
            out.append(("warn", i18n.t("cli.doctor.what_proxy", part=part),
                        i18n.t("cli.doctor.proxy_old", port=port, part=part)))
        elif refuses:
            out.append(("ok", i18n.t("cli.doctor.what_proxy", part=part), i18n.t("cli.doctor.proxy_ok", port=port)))
    for part, port in ports.items():
        found = [p for p in access.proxy_programs(port) if p not in checked]
        if found:
            out.append(("warn", i18n.t("cli.doctor.what_proxy", part=part),
                        i18n.t("cli.doctor.proxy_programs", port=port, programs=", ".join(found))))
    return out


def report(rows):
    width = max((len(what) for _, what, _ in rows), default=0)
    return "\n".join(f"{level:<5} {what:<{width}}" + (f"  {detail}" if detail else "") for level, what, detail in rows)
