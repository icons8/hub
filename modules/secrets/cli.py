"""`hub secret list` — which secrets the owner keeps in the hub (names, sites, whether commands may have them; no values);
`hub secret fill <name> [password|username]` — put one into the form field where the session's cursor is (the page
process does it: modules/secrets/engine.py); `hub secret exec --env VAR=<name>[:login] … -- <command> [args]` — run a
command with the value in its environment, its output with the value masked; `hub secret add <name> --from-file
<file|-> [--key KEY] …` — put a value from a file the session was pointed to (or stdin) into the vault (spec d2441132).

The session gets the value in no answer: `fill` sends only the name to the page process; `exec` reads the vault in this
process (the value must reach the command anyway) and masks it in what the command prints. The mask is against printing
by accident (`env`, `set -x`, a debug line), not against a command that means to show it (base64, a file): that is the
boundary of the same OS user (spec d2345345 §5). `add` reads the file in this process — with the session's own rights
and sandbox, the page process never opens a path — and sends the value to the page process, the vault's only writer;
it prints names and keys, never a line of the file."""
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request

from core import i18n
from core import net  # noqa: F401 — the page is on 127.0.0.1: connect directly, bypassing the machine's proxy
from modules.secrets import engine

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
CHUNK = 65536
FILE_MAX = 1 << 20       # `add` reads at most this much of a file: a value is one line of up to 4 KB
ADD_TIMEOUT = 120        # `add` waits for no browser, only for the vault (a Keychain prompt on macOS may hold it a while)
DOTENV = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*)$")
KEY_SHOWN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")   # a JSON key named in an answer; anything else is only counted
# a .env name as people write them, in capitals. A line is a NAME=value line only with a value after "=" that is not
# padding: "dGVzdA==", "JBSWY3DPEHPK3PX=" (base64, base32 with their padding) are tokens, not names
ENV_SHOWN = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
PADDING = re.compile(r"^=*$")
ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t"}


def post(port, timeout=400):
    """Send the request to the page process (same entry as the page actions: localhost + X-Hub)."""
    def send(body, path="/api/secrets/fill", timeout=timeout):
        # the page's password (core/pagepass.py): the token of this start of the page lets the hub's own command in,
        # on this path only; the old key's cookie for a hub in the state "key" (and a page of an earlier release)
        from core import pagekey, pagepass, runtime
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "X-Hub": "1", "Host": f"127.0.0.1:{port}",
                                      **pagekey.cookie_header(), **pagepass.local_header(runtime.state_dir())})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                try:
                    return e.code, json.load(e)
                except ValueError:
                    return e.code, {"error": str(e)}
        except OSError as e:
            if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                return 504, engine.answer("cli.secrets.page_slow")   # it may still finish: not "it did not happen"
            return 503, engine.answer("cli.secrets.page_down")
    return send


def said(out, field):
    """The page's text in this process's language: by the key it sent, otherwise its text as is."""
    params = out.get("params") if isinstance(out.get("params"), dict) else {}
    return i18n.t(out["key"], **params) if isinstance(out.get("key"), str) else out.get(field)


def row(m):
    what = i18n.t("cli.secrets.what_login" if m["login"] else "cli.secrets.what_value")
    where = [i18n.t("cli.secrets.where_sites", sites=", ".join(m["sites"]))] if m["sites"] else []
    where += [i18n.t("cli.secrets.where_commands")] if m["commands"] else []
    line = f"{m['name']}: {what} — " + ("; ".join(where) or i18n.t("cli.secrets.where_nowhere"))
    return line + (f" — «{m['note']}»" if m["note"] else "") + ("" if m["value"] else " " + i18n.t("cli.secrets.no_value"))


def main(argv, cfg, session, send, state=None, put=None, run=None):
    from modules.secrets import vault
    cmd = argv[0] if argv else ""
    if cmd in ("list", "fill", "exec", "add") and not engine.enabled(cfg):
        print(i18n.t("cli.secrets.off"))
        return 0 if cmd == "list" else 2
    if cmd == "list":
        try:
            rows = [row(m) for _, m in sorted(vault.items(state).items())]
        except vault.Error as e:
            print(i18n.t(e.key, **e.params))
            return 2
        print("\n".join(rows) or i18n.t("cli.secrets.no_items"))
        return 0
    if cmd == "fill" and len(argv) in (2, 3) and (len(argv) == 2 or argv[2] in engine.FIELDS):
        code, out = send({"session": session or "", "item": argv[1], "field": argv[2] if len(argv) == 3 else "password"})
        print(said(out, "text") if code == 200 else i18n.t("cli.secrets.not_filled", error=said(out, "error") or code))
        return 0 if code == 200 else 2
    if cmd == "exec":
        return run_with(argv[1:], state, session, put or (lambda *a, **k: None), run)
    if cmd == "add":
        return add(argv[1:], session, send)
    print(i18n.t("cli.secrets.usage"))
    return 2


def parse(argv):
    """[(VAR, name, field)], command — or None when the line is not `--env VAR=name[:login] … -- command …`."""
    pairs, i = [], 0
    while i < len(argv) and argv[i] == "--env" and i + 1 < len(argv):
        var, sep, ref = argv[i + 1].partition("=")
        name, colon, part = ref.partition(":")
        if not (sep and ENV_NAME.match(var) and engine.NAME.match(name) and part in ("", "login") and (part or not colon)):
            return None
        pairs.append((var, name, "username" if part else "password"))
        i += 2
    if not pairs or i >= len(argv) or argv[i] != "--" or i + 1 >= len(argv):
        return None
    return pairs, argv[i + 1:]


def forms(value):
    """The byte forms of a value the mask looks for: as is, JSON-escaped, in the console's code page (Windows)."""
    out = {value.encode("utf-8"), json.dumps(value)[1:-1].encode("utf-8"),
           json.dumps(value, ensure_ascii=False)[1:-1].encode("utf-8")}
    import platforms
    for enc in {platforms.current.console_encoding(), locale.getpreferredencoding(False)}:
        try:
            out.add(value.encode(enc))
        except (LookupError, UnicodeError):
            pass
    return out


class Mask:
    """Replaces every form of every value in a byte stream with `[secret:<name>]`, also across the boundary of two
    pieces: the raw tail no form can still complete stays until the next piece."""

    def __init__(self, values):
        self.forms = sorted(((f, f"[secret:{n}]".encode()) for n, v in values for f in forms(v) if f),
                            key=lambda x: -len(x[0]))
        self.keep = max((len(f) for f, _ in self.forms), default=1) - 1
        self.firsts = {f[0] for f, _ in self.forms}
        self.buf = b""

    def _replace(self, out, final):
        while True:
            hits = [(self.buf.find(f), -len(f), f, m) for f, m in self.forms if f in self.buf]
            if not hits:
                return
            pos, _, f, m = min(hits)
            # a longer form may begin here or before and end in the next piece: wait for it rather than cut it in two
            if not final and any(self.buf[p] in self.firsts and any(len(g) > len(self.buf) - p
                                                                    and g.startswith(self.buf[p:]) for g, _ in self.forms)
                                 for p in range(max(0, len(self.buf) - self.keep), pos + 1)):
                return
            out += self.buf[:pos] + m
            self.buf = self.buf[pos + len(f):]

    def feed(self, data):
        self.buf += data
        out = bytearray()
        self._replace(out, False)
        # hold back only a tail that can still begin a form (a prompt waiting for input goes out now); only the places
        # holding a form's first byte are tried, so a long value costs no more than its first bytes in the tail
        start = max(0, len(self.buf) - self.keep)
        cut = next((i for i in range(start, len(self.buf)) if self.buf[i] in self.firsts
                    and any(f.startswith(self.buf[i:]) for f, _ in self.forms)), len(self.buf))
        out += self.buf[:cut]
        self.buf = self.buf[cut:]
        return bytes(out)

    def close(self):
        out = bytearray()
        self._replace(out, True)
        out += self.buf
        self.buf = b""
        return bytes(out)


def _pump(src, dst, mask):
    while True:
        data = os.read(src.fileno(), CHUNK)
        if not data:
            break
        dst.write(mask.feed(data))
        dst.flush()
    dst.write(mask.close())
    dst.flush()


def run_with(argv, state, session, put, run=None):
    """`hub secret exec`: the command's exit code; 2 — refused (wrong line, a secret not allowed into commands)."""
    from modules.secrets import vault
    got = parse(argv)
    if not got:
        print(i18n.t("cli.secrets.usage"))
        return 2
    pairs, command = got
    program = os.path.basename(command[0])
    asked = False   # `secret.requested` is written: a failure from here on is a fact too
    try:
        shown = vault.items(state)
        for var, name, field in pairs:
            m = shown.get(name)
            if not m or not m["value"]:
                raise vault.Error("cli.secrets.not_listed")
            if not m["commands"]:
                raise vault.Error("cli.secrets.not_for_commands", name=name,
                                  sites=", ".join(m["sites"]) or i18n.t("cli.secrets.where_nowhere"))
        for var, name, field in pairs:
            put("secret.requested", session, {"item": name, "field": field, "env": var, "program": program}, quiet=True)
        asked = True
        values = [(var, name, field, vault.value(state, name, field)) for var, name, field in pairs]
    except vault.Error as e:
        for var, name, field in pairs if asked else ():
            put("secret.failed", session, {"item": name, "field": field, "env": var, "program": program,
                                           "why": e.key}, quiet=True)
        print(i18n.t("cli.secrets.not_given", error=i18n.t(e.key, **e.params)), file=sys.stderr)
        return 2
    env = {**os.environ, **{var: v for var, _, _, v in values}}
    # a login is no secret (the card shows it): masking a short one would cut the command's ordinary output
    mask = Mask([(name, v) for _, name, field, v in values if field == "password"])
    del values
    # a bare program name through PATH (and PATHEXT: npm.cmd, az.cmd on Windows) as the session's shell finds it
    found = shutil.which(command[0]) if os.path.basename(command[0]) == command[0] else None
    code = None
    try:
        try:
            proc = (run or subprocess.Popen)([found or command[0], *command[1:]], env=env, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE)
        except OSError as e:
            print(i18n.t("cli.secrets.exec_failed", program=program, error=type(e).__name__), file=sys.stderr)
            code = 127
            return code
        try:
            err_mask = Mask([])
            err_mask.forms, err_mask.keep, err_mask.firsts = mask.forms, mask.keep, mask.firsts
            t = threading.Thread(target=_pump, args=(proc.stderr, sys.stderr.buffer, err_mask), daemon=True)
            t.start()
            _pump(proc.stdout, sys.stdout.buffer, mask)
            t.join()
        except BaseException:   # the reader of our output went away (`| head`), Ctrl+C: the command does not go on alone
            proc.kill()
            raise
        finally:
            code = proc.wait()
        return code
    finally:   # the outcome of the hand-out, whatever ended the run
        for var, name, field in pairs:
            put("secret.used", session, {"item": name, "field": field, "env": var, "program": program,
                                         "code": code if isinstance(code, int) else -1}, quiet=True)


# --- `hub secret add` (spec d2441132) -----------------------------------------------------------------------------------

class Refused(Exception):
    """A refusal of `add` with a catalog key — its parameters are names, keys and paths, never a line of the file."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key, self.params = key, params


def parse_add(argv):
    """{name, file, key, login_key, login, sites, commands, note} — or None when the line is not `add` as the help has it."""
    if not argv or not engine.NAME.match(argv[0]):
        return None
    got, i = {"name": argv[0], "sites": [], "commands": False}, 1
    one = {"--from-file": "file", "--key": "key", "--login-key": "login_key", "--login": "login", "--note": "note"}
    while i < len(argv):
        flag = argv[i]
        if flag == "--commands":
            got["commands"], i = True, i + 1
        elif flag in one and i + 1 < len(argv) and one[flag] not in got:
            got[one[flag]], i = argv[i + 1], i + 2
        elif flag == "--site" and i + 1 < len(argv):
            got["sites"].append(argv[i + 1])
            i += 2
        else:
            return None
    if "file" not in got or ("login" in got and "login_key" in got):
        return None
    return got


def read_input(where):
    """The text of a file or of stdin ("-"): UTF-8, at most FILE_MAX bytes; only a regular file (not a folder, a device
    or a pipe that would never end). It is read by this process: whatever the session itself may read."""
    if where == "-":
        data = sys.stdin.buffer.read(FILE_MAX + 1)
    else:
        import stat
        try:
            if not stat.S_ISREG(os.stat(where).st_mode):
                raise Refused("cli.secrets.not_a_file", file=where)
            with open(where, "rb") as f:
                data = f.read(FILE_MAX + 1)
        except OSError as e:
            raise Refused("cli.secrets.read_failed", file=where, error=type(e).__name__) from None
    if len(data) > FILE_MAX:
        raise Refused("cli.secrets.too_big", file=where, max="1 MB")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("cli.secrets.not_utf8", file=where) from None


def _dotenv_value(raw):
    """The value of one .env line as dotenv and a shell read it; None — it goes on past the line (not one line)."""
    if raw[:1] == "'":
        end = raw.find("'", 1)
        return None if end < 0 else raw[1:end]
    if raw[:1] == '"':
        out, i = [], 1
        while i < len(raw):
            c = raw[i]
            if c == '"':
                value = "".join(out)
                return None if "\n" in value else value
            if c == "\\" and i + 1 < len(raw):
                out.append(ESCAPES.get(raw[i + 1], c + raw[i + 1]))
                i += 2
                continue
            out.append(c)
            i += 1
        return None
    cut = re.search(r"\s#", raw)   # a comment after the value
    return (raw[:cut.start()] if cut else raw).rstrip()


def is_json(text):
    return text.lstrip().startswith("{")


def keyed(text):
    """{key: value or None} of a JSON object or a .env (None — not a one-line string); None when the text is neither."""
    if is_json(text):
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return {k: v if isinstance(v, str) and "\n" not in v else None for k, v in data.items()} \
            if isinstance(data, dict) else None
    out = {}
    for line in text.split("\n"):
        line = line.strip()
        m = DOTENV.match(line) if line and not line.startswith("#") else None
        if m and not PADDING.match(m.group(2)):
            out[m.group(1)] = _dotenv_value(m.group(2))   # a key named twice: the last line, as a shell has it
    return out or None


def _keys(found, text):
    """The keys an answer may name: JSON keys of a plain form, .env names in capitals — never a piece of a value. ""
    when there is none to name."""
    shown_re = KEY_SHOWN if is_json(text) else ENV_SHOWN
    shown = [k for k in found if shown_re.match(k)][:30]
    rest = len(found) - len(shown)
    return ", ".join(shown) + (f" (+{rest})" if rest > 0 else "") if shown else ""


def pick(text, where, key=None):
    """The value: without a key — the one line of the file as it is; with one — a .env line or a JSON field. -> (value,
    whether the line looks like NAME=value — the name is not said: it may be the start of a token)."""
    if key is None:
        body = text.rstrip("\r\n")
        if "\n" in body:
            found = keyed(text)
            keys = _keys(found or {}, text)
            raise Refused("cli.secrets.one_line_keys" if keys else "cli.secrets.one_line", file=where, keys=keys)
        m = DOTENV.match(body.strip())
        return body, bool(m and ENV_SHOWN.match(m.group(1)) and not PADDING.match(m.group(2)))
    found = keyed(text)
    if not found:
        raise Refused("cli.secrets.no_keys", file=where)
    if key not in found:
        raise Refused("cli.secrets.no_key", entry=key, file=where, keys=_keys(found, text) or "—")
    if found[key] is None:
        raise Refused("cli.secrets.not_one_line", entry=key, file=where)
    return found[key], False


def whose(was):
    who = was.get("who") or ""
    return i18n.t("cli.secrets.whose_owner") if not who else i18n.t("cli.secrets.whose_terminal") \
        if who == ":terminal" else i18n.t("cli.secrets.whose_session", session=who)


def add(argv, session, send):
    """`hub secret add`: 0 — added, changed or already so; 2 — refused (the line, the file, the page)."""
    got = parse_add(argv)
    if not got:
        print(i18n.t("cli.secrets.usage"))
        return 2
    where = got["file"]
    try:
        text = read_input(where)
        value, looks = pick(text, "stdin" if where == "-" else where, got.get("key"))
        login = pick(text, "stdin" if where == "-" else where, got["login_key"])[0] if "login_key" in got \
            else got.get("login")
    except Refused as e:
        print(i18n.t("cli.secrets.not_added", error=i18n.t(e.key, **e.params)), file=sys.stderr)
        return 2
    del text
    body = {"session": session or "", "name": got["name"], "password": value,
            "source": "stdin" if where == "-" else os.path.realpath(where), **({"key": got["key"]} if got.get("key") else {})}
    if login is not None:
        body["login"] = login
    if got["sites"]:
        body["sites"] = got["sites"]
    if got["commands"]:
        body["commands"] = True
    if "note" in got:
        body["note"] = got["note"]
    code, out = send(body, path="/api/secrets/add", timeout=ADD_TIMEOUT)
    del value, body
    if code != 200:
        # a refusal of this page has its key; a page of an earlier release knows neither the path nor the token
        error = said(out, "error") if isinstance(out.get("key"), str) else i18n.t("cli.secrets.page_old") \
            if code in (401, 403, 404) else out.get("error") or code
        print(i18n.t("cli.secrets.not_added", error=error), file=sys.stderr)
        return 2
    shown = got.get("key") and i18n.t("cli.secrets.source_key", entry=got["key"], file=where) or \
        ("stdin" if where == "-" else where)
    name, changed = got["name"], [c for c in out.get("changed") or [] if isinstance(c, str)]
    if out.get("new"):
        where_to = [i18n.t("cli.secrets.where_sites", sites=", ".join(got["sites"]))] if got["sites"] else []
        where_to += [i18n.t("cli.secrets.where_commands")] if got["commands"] else []
        print(i18n.t("cli.secrets.added", name=name, source=shown,
                     where="; ".join(where_to) or i18n.t("cli.secrets.where_nowhere")))
    elif not changed:
        print(i18n.t("cli.secrets.unchanged", name=name))
    elif isinstance(out.get("was"), dict):
        was = out["was"]
        print(i18n.t("cli.secrets.changed_kept", name=name, parts=", ".join(changed), source=shown, whose=whose(was),
                     when=str(was.get("changed") or "").replace("T", " ")[:16]))
    else:
        print(i18n.t("cli.secrets.changed", name=name, parts=", ".join(changed), source=shown))
    if where != "-":   # the copy the session was given stays in the clear whatever the vault now holds
        print(i18n.t("cli.secrets.source_stays", file=where))
    if looks:
        print(i18n.t("cli.secrets.looks_env"))
    return 0
