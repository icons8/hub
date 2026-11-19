"""A file for support (installer spec §10): `hub support-bundle` and «Setup → A file for support» put into one zip
what helps find why a hub does not start — and nothing the person did not mean to give:

- versions.json: the hub (version, code commit, package), Python, the OS, claude, gh, git, node; on a hub entirely on
  Codex (experimental, hub.json "host": "codex") also codex — asked with the hub's own CODEX_HOME;
- doctor.txt: `hub doctor`;
- logs/hub-up.log and logs/setup.log: the last LOG_LINES lines, every line through core/redact.mask;
- hub.json: the settings with the values of key-like names, env and headers replaced by [masked];
- state-files.txt: names and sizes of the state folder's files (two levels; the journal's archives — a count).

The journal, conversations, accounts.json, tokens and the sign-in folders are never in it. Nothing is sent: the person
attaches the file to a letter or an issue themselves."""
import io
import json
import os
import platform
import platforms
import subprocess
import sys
import time
import zipfile

from core import redact

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_LINES = 2000
LOGS = ("hub-up.log", "setup.log")
SECRET_PARTS = ("env", "headers")   # whole sections: MCP servers' keys and tokens
SKIP_LIST = ("journal-archive", "workspaces", "browsers", "cache", "downloads", "runtime", "spool", "gh")
TIMEOUT = 60


def _tail(path, n=LOG_LINES):
    """The last n lines of a log, masked; "" when there is none."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4 * 1024 * 1024))
            data = f.read()
    except OSError:
        return ""
    lines = data.decode("utf-8", "replace").splitlines()[-n:]
    return "\n".join(redact.mask(x) for x in lines) + ("\n" if lines else "")


def _secret(name, value=None):
    """A key-like name; a plural one (api_tokens, passwords) only over a list or an object — a number under
    max_tokens is a limit, not a key."""
    name = str(name)
    return redact.secret_key(name) or isinstance(value, (list, dict)) and name.lower().endswith("s") \
        and redact.secret_key(name[:-1])


def _flag_secret(item):
    """`--token`, `--api-key`, `-password`: the next item of a command line is a secret."""
    return isinstance(item, str) and item.startswith("-") and bool(_secret(item.lstrip("-").replace("-", "_")))


def masked_settings(value):
    """hub.json for a stranger's eyes: everything under a key-like name (whatever its shape), everything under
    env/headers, the item after a key-like flag in a list (`--token x`), a key-like flag with its value (`--token=x`)
    -> [masked]; every other string through redact.mask."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in SECRET_PARTS and isinstance(v, dict):
                out[k] = {kk: redact.MASK for kk in v}
            elif _secret(k, v) and not (v is None or v == "" or isinstance(v, bool)):
                out[k] = redact.MASK
            else:
                out[k] = masked_settings(v)
        return out
    if isinstance(value, list):
        out, hide = [], False
        for v in value:
            if hide and isinstance(v, str):
                out.append(redact.MASK)
            elif isinstance(v, str) and v.startswith("-") and "=" in v and _flag_secret(v.split("=", 1)[0]):
                out.append(v.split("=", 1)[0] + "=" + redact.MASK)
            else:
                out.append(masked_settings(v))
            hide = _flag_secret(v) and "=" not in v   # `--token=x` carried its own value
        return out
    if isinstance(value, str):
        return redact.mask(value)
    return value


def _ask(argv, env=None):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return f"- ({type(e).__name__})"
    said = ((r.stdout or "") + (r.stderr or "")).strip()
    return redact.mask(said.splitlines()[0][:200]) if said else f"- (exit {r.returncode})"


def versions(state, claude=None):
    from core import ghauth, packaged, version
    try:
        head = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        head = None
    if claude is None:
        from core.daemon import claude_command
        claude = claude_command()
    gh = ghauth.gh_program()
    return {"hub": {"commit": head, "package": packaged.info(), "plugin_api": version.PLUGIN_API.split(" ")[0],
                    "module_api": version.MODULE_API, "min_claude": version.MIN_CLAUDE},
            "python": sys.version.split()[0], "os": platform.platform(), "machine": platform.machine(),
            "claude": _ask(claude + ["--version"]), "gh": _ask([gh, "--version"]) if gh else None,
            "git": _ask(["git", "--version"]), "node": _ask(["node", "--version"]),
            "hub_github_signed_in": ghauth.signed_in(state), "made": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **_codex(state)}


def _codex(state):
    """Codex (experimental): {"codex": its --version} on a hub entirely on Codex; elsewhere nothing, codex never starts."""
    from core import catalog
    if catalog.config_or_last(state).get("host") != "codex":
        return {}
    return {"codex": codex_version(state)}


def codex_version(state):
    """`codex --version` of the codex the hub spawns, asked with the hub's CODEX_HOME (created first — codex refuses a
    missing one; without it even --version writes into the personal ~/.codex); also «Setup» (modules/setup)."""
    from core import codexcli, codexmodel
    from hosts.codex import codex_bin
    try:
        codexcli._prepare(state)
    except OSError as e:
        return f"- ({type(e).__name__})"
    return _ask([codex_bin(), "--version"], env=codexmodel.env(state))


def files(state):
    out = []
    try:
        top = sorted(os.listdir(state))
    except OSError as e:
        return f"{state}: {e}\n"
    for name in top:
        p = os.path.join(state, name)
        if os.path.isdir(p) and not os.path.islink(p):
            try:
                inner = sorted(os.listdir(p))
            except OSError:
                inner = []
            out.append(f"{name}/  ({len(inner)})")
            if name in SKIP_LIST:
                continue
            for sub in inner[:200]:
                q = os.path.join(p, sub)
                try:
                    out.append(f"  {sub}{'/' if os.path.isdir(q) else ''}  {os.path.getsize(q)}")
                except OSError:
                    out.append(f"  {sub}")
        else:
            try:
                out.append(f"{name}  {os.path.getsize(p)}")
            except OSError:
                out.append(name)
    return "\n".join(out) + "\n"


def doctor(state):
    try:
        r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "doctor"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=180,
                           env=dict(os.environ, HUB_STATE_DIR=state))
    except (OSError, subprocess.SubprocessError) as e:
        return f"hub doctor: {type(e).__name__}: {e}\n"
    return redact.mask((r.stdout or "") + (r.stderr or "")) + f"\n(exit {r.returncode})\n"


def build(state, claude=None, doctor_text=None):
    """(file name, zip bytes)."""
    from core import catalog
    stamp = time.strftime("%Y%m%d-%H%M%S")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("versions.json", json.dumps(versions(state, claude), ensure_ascii=False, indent=1))
        z.writestr("doctor.txt", doctor_text if doctor_text is not None else doctor(state))
        for name in LOGS:
            text = _tail(os.path.join(state, "logs", name))
            if text:
                z.writestr(f"logs/{name}", text)
        path = catalog.config_path(state)
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            z.writestr("hub.json", json.dumps(masked_settings(raw), ensure_ascii=False, indent=1))
        except FileNotFoundError:
            z.writestr("hub.json", "{}\n")
        except (OSError, ValueError) as e:
            z.writestr("hub.json.error.txt", f"{path}: {type(e).__name__}\n")
        z.writestr("state-files.txt", files(state))
    return f"hub-support-{stamp}.zip", buf.getvalue()


def main(argv, state):
    """hub support-bundle [--out <file>] -> the exit code; the path is printed."""
    from core import i18n
    out = None
    if argv[:1] == ["--out"] and len(argv) == 2:
        out = argv[1]
    elif argv:
        print(i18n.t("cli.bundle.usage"), file=sys.stderr)
        return 2
    name, data = build(state)
    if not out:
        folder = os.path.join(state, "support")
        platforms.current.makedirs_private(folder)
        out = os.path.join(folder, name)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    print(i18n.t("cli.bundle.done", path=out, kb=max(1, len(data) // 1024)))
    return 0
