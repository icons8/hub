"""Sign a Windows program of the app (Setup.exe) on the signing pipeline and check what comes back (app spec §6.3,
slice A8). The key is a hardware token on the signing machine, behind a GitLab project whose pipeline signs the files
given to it by an https address and a sha256; nothing of the key is here.

  python3 tools/package/sign.py <unsigned.exe> [--out <signed.exe>] [--settings F] [--require] [--again]
  python3 tools/package/sign.py --verify <unsigned.exe> <signed.exe> [--settings F]

The steps of one file:
1. the unsigned file goes into the signing project's generic packages under a version of its own (the pipeline's fetch
   takes it with its job token: the file never leaves that GitLab);
2. ONE pipeline is started for it with the trigger token — never a second one by itself: a failed signing stops here
   with its reason (a wrong input may lock the token of the people the signing machine belongs to). The file's sha256
   is written down (the ledger) with the pipeline: the same file again waits for that pipeline or takes its result,
   and after a failure it is refused until --again;
3. the wait for the signing job, then its artifact: signed/<name> and signed/SHA256SUMS.txt; the package is deleted as
   soon as the pipeline has taken it;
4. the checks, all of them before the signed file is written: the sum in SHA256SUMS; the signed file is the given one
   plus one signature over it, byte for byte (authenticode.py: the claimed digest is the given file's Authenticode
   hash); `osslsigncode verify` against the code signing root of the settings: valid, timestamped, the publisher's
   organization as in the settings. Anything off — nothing is released.

The settings (default ~/.config/hub-core/sign-windows.json; none — the file stays unsigned, said in one line, exit 0;
with --require that is code 2):

  {"gitlab": "https://<host>", "project": <id>, "ref": "main", "job": "<signing job>", "package": "<package name>",
   "publisher": "<the certificate's O=>", "cafile": "<PEM of the code signing root>",
   "tsa_cafile": "<PEM of the timestamp roots>" (default: the system's), "osslsigncode": "<path>",
   "trigger_env": "<file with HUB_SIGNING_TRIGGER_TOKEN=…>", "api_token_command": ["<prints a GitLab API token>"],
   "ledger": "<file>", "wait_minutes": 60}

The trigger token may also come from HUB_SIGNING_TRIGGER_TOKEN, the API token (packages, jobs, artifacts) from
HUB_SIGNING_API_TOKEN. Tokens are never printed. Codes: 0 — signed (or no settings), 2 — wrong input or no settings
with --require, 3 — refused (the signing failed or its result did not pass), 4 — GitLab, the network or a wait: run
again (another signing running here, a pipeline still waiting, an artifact cut short).
The stdlib only."""
import argparse
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import authenticode  # noqa: E402
from fetch import sha256  # noqa: E402

SETTINGS = os.path.join("~", ".config", "hub-core", "sign-windows.json")
LEDGER = os.path.join("~", ".cache", "hub-package", "signed.json")
UA = "agents-hub-sign"
NAME = re.compile(r"[A-Za-z0-9._-]{1,120}\.exe")     # what the pipeline's fetch takes as a file name
POLL = 10
WAITING = {"created", "pending", "running", "waiting_for_resource", "preparing", "scheduled", "manual"}
TOKEN_VAR = "HUB_SIGNING_TRIGGER_TOKEN"
API_VAR = "HUB_SIGNING_API_TOKEN"


class Stop(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


class Off(Exception):
    """No signing settings here: the file stays unsigned."""


def expand(path):
    return os.path.expanduser(str(path)) if path else path


def settings(path=None, environ=os.environ, run=subprocess.run, tokens=True):
    """The settings with both tokens (tokens=False: the checks' part only), or Off (no settings, no token) / Stop
    (settings that are wrong)."""
    path = expand(path or SETTINGS)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise Off(f"no signing settings ({path})") from None
    except (OSError, ValueError) as e:
        raise Stop(2, f"{path}: {e}") from None
    if not isinstance(cfg, dict):
        raise Stop(2, f"{path}: not an object")
    need = {"gitlab": str, "project": (int, str), "job": str, "package": str, "publisher": str, "cafile": str}
    for key, kind in need.items():
        if not isinstance(cfg.get(key), kind) or cfg[key] in ("", None):
            raise Stop(2, f"{path}: no {key!r}")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", cfg["gitlab"].rstrip("/")):
        raise Stop(2, f"{path}: 'gitlab' is https://<host>")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", cfg["package"]):
        raise Stop(2, f"{path}: 'package' is a name of letters, digits and ._-")
    cfg = {"ref": "main", "osslsigncode": "osslsigncode", "tsa_cafile": "/etc/ssl/certs/ca-certificates.crt",
           "ledger": LEDGER, "wait_minutes": 60, **cfg}
    cfg["gitlab"] = cfg["gitlab"].rstrip("/")
    for key in ("cafile", "tsa_cafile", "ledger", "trigger_env", "osslsigncode"):
        if isinstance(cfg.get(key), str):
            cfg[key] = expand(cfg[key])
    if not tokens:
        return cfg
    trigger = environ.get(TOKEN_VAR) or env_file(cfg.get("trigger_env")).get(TOKEN_VAR)
    if not trigger:
        raise Off(f"no trigger token ({TOKEN_VAR}, or 'trigger_env' in {path})")
    api = environ.get(API_VAR)
    if not api and cfg.get("api_token_command"):
        try:
            r = run(list(cfg["api_token_command"]), capture_output=True, text=True, timeout=60,
                    stdin=subprocess.DEVNULL)
            api = r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError, TypeError):
            api = ""
    if not api:
        raise Off(f"no GitLab API token ({API_VAR}, or 'api_token_command' in {path})")
    return {**cfg, "trigger_token": trigger, "api_token": api}


def env_file(path):
    """KEY=VALUE lines of a file (no file — nothing)."""
    out = {}
    try:
        with open(expand(path), encoding="utf-8") as f:
            for line in f:
                key, eq, value = line.strip().removeprefix("export ").partition("=")
                if eq and key and not key.startswith("#"):
                    out[key.strip()] = value.strip().strip("'\"")
    except (OSError, TypeError):
        pass
    return out


class GitLab:
    """The few calls of the GitLab API the signing needs (PRIVATE-TOKEN: the API token)."""

    def __init__(self, cfg):
        self.base = f"{cfg['gitlab']}/api/v4/projects/{urllib.parse.quote(str(cfg['project']), safe='')}"
        self.token = cfg["api_token"]

    def call(self, method, path, body=None, headers=None, token=True, raw=False, timeout=120):
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"User-Agent": UA, **({"PRIVATE-TOKEN": self.token} if token else {}),
                                              **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            text = e.read()[:300].decode("utf-8", "replace").strip()
            raise Stop(4, f"GitLab {method} {path.split('?')[0]}: {e.code} {text}") from None
        except (urllib.error.URLError, OSError) as e:
            raise Stop(4, f"GitLab {method} {path.split('?')[0]}: {e}") from None
        if raw:
            return data
        try:
            return json.loads(data or b"null")
        except ValueError:
            raise Stop(4, f"GitLab {method} {path.split('?')[0]}: not JSON") from None

    def file_url(self, package, version, name):
        return f"{self.base}/packages/generic/{package}/{version}/{name}"

    def upload(self, package, version, name, path):
        """The package's id."""
        with open(path, "rb") as f:
            got = self.call("PUT", f"/packages/generic/{package}/{version}/{name}?select=package_file", body=f,
                            headers={"Content-Type": "application/octet-stream",
                                     "Content-Length": str(os.path.getsize(path))}, timeout=1800)
        if not isinstance(got, dict) or not isinstance(got.get("package_id"), int):
            raise Stop(4, "GitLab: the upload's answer has no package_id")
        return got["package_id"]

    def trigger(self, token, ref, variables):
        form = {"token": token, "ref": ref, **{f"variables[{k}]": v for k, v in variables.items()}}
        got = self.call("POST", "/trigger/pipeline", body=urllib.parse.urlencode(form).encode(), token=False,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        if not isinstance(got, dict) or not isinstance(got.get("id"), int):
            raise Stop(4, "GitLab: the trigger's answer has no pipeline id")
        return got

    def jobs(self, pipeline):
        got = self.call("GET", f"/pipelines/{pipeline}/jobs?per_page=100")
        return got if isinstance(got, list) else []

    def trace(self, job):
        try:
            return self.call("GET", f"/jobs/{job}/trace", raw=True).decode("utf-8", "replace")
        except Stop:
            return ""

    def artifacts(self, job):
        return self.call("GET", f"/jobs/{job}/artifacts", raw=True, timeout=1800)

    def delete_package(self, package_id):
        self.call("DELETE", f"/packages/{package_id}", raw=True)


# --- the ledger: one pipeline per file ---------------------------------------------------------------------------

def ledger(cfg):
    """{sha256: entry}; a ledger that cannot be read is an error, never "nothing was signed"."""
    try:
        with open(cfg["ledger"], encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise Stop(2, f"the signing ledger {cfg['ledger']} is not readable ({e}): it is what keeps a file from being "
                      f"signed twice — mend it by hand") from None
    if not isinstance(data, dict):
        raise Stop(2, f"the signing ledger {cfg['ledger']} is not an object — mend it by hand")
    return data


class Lock:
    """One signing at a time on this machine: <ledger>.lock, made exclusively and removed at the end. One left by a
    run that was killed is removed by hand (the text says so) — never taken over by itself."""

    def __init__(self, cfg):
        self.path = cfg["ledger"] + ".lock"

    def __enter__(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise Stop(4, f"another signing runs on this machine ({self.path}); if none does, remove that file") from None
        self.mine = f"{os.getpid()} {secrets.token_hex(8)}"
        with os.fdopen(fd, "w") as f:
            f.write(self.mine)
        return self

    def __exit__(self, *exc):
        try:   # only this run's own lock: one removed by hand and made again by another run stays
            with open(self.path, encoding="utf-8") as f:
                mine = f.read() == self.mine
            if mine:   # after the file is closed: Windows does not delete an open one
                os.unlink(self.path)
        except OSError:
            pass


def note(cfg, digest, **entry):
    """The file's line in the ledger, written before the step it records goes further."""
    data = ledger(cfg)
    data[digest] = {**data.get(digest, {}), **entry, "at": round(time.time())}
    os.makedirs(os.path.dirname(os.path.abspath(cfg["ledger"])), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".signed-", dir=os.path.dirname(os.path.abspath(cfg["ledger"])))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, cfg["ledger"])


# --- the checks ----------------------------------------------------------------------------------------------------

def subject_field(subject, field):
    """The value of field (O, CN) in an osslsigncode subject line: "CN=…,O=…" (RFC 2253, "\\," inside a value) or
    "/C=…/O=…" (the older one-line form)."""
    subject = subject.strip()
    if subject.startswith("/"):
        parts = re.split(r"(?<!\\)/", subject[1:])
    else:
        parts = re.split(r"(?<!\\),", subject)
    for part in parts:
        key, eq, value = part.strip().partition("=")
        if eq and key == field:
            return re.sub(r"\\(.)", r"\1", value.strip())
    return None


def os_verify(cfg, path, run=subprocess.run):
    """osslsigncode's word on the signature: valid, timestamped, the publisher of the settings; else Stop(3)."""
    args = [cfg["osslsigncode"], "verify", "-CAfile", cfg["cafile"], "-TSA-CAfile", cfg["tsa_cafile"], "-in", path]
    try:
        r = run(args, capture_output=True, text=True, errors="replace", timeout=300, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Stop(4, "osslsigncode verify did not end in 300 s — nothing is released; run again") from None
    except (OSError, subprocess.SubprocessError) as e:
        raise Stop(2, f"osslsigncode: {e} — the signature is not checked, nothing is released") from None
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode or "Signature verification: ok" not in out:
        tail = " / ".join(x.strip() for x in out.splitlines() if re.search(r"(?i)error|failed", x))[-400:]
        raise Stop(3, f"osslsigncode verify: not valid (exit code {r.returncode}): {tail or out.strip()[-300:]}")
    signer = out.split("Signer's certificate:", 1)[1] if "Signer's certificate:" in out else ""
    m = re.search(r"Subject:\s*(.+)", signer)
    org = subject_field(m.group(1), "O") if m else None
    if org != cfg["publisher"]:
        raise Stop(3, f"the signer is {m.group(1).strip() if m else 'not named'}, not {cfg['publisher']!r}")
    if "Timestamp time:" not in out:
        raise Stop(3, "the signature has no timestamp: it would stop being valid when the certificate expires")
    return m.group(1).strip()


def check(cfg, unsigned, signed, sums=None, run=subprocess.run):
    """All the checks of a signed file against the given one; Stop(3) at the first that fails. -> the signer."""
    digest = sha256(signed)
    if sums is not None:
        name = os.path.basename(unsigned)
        listed = {}
        for line in sums.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                listed[parts[1].lstrip("*")] = parts[0].lower()
        if listed.get(name) != digest:
            raise Stop(3, f"SHA256SUMS.txt says {listed.get(name)} for {name}, the file is {digest}")
    try:
        authenticode.same_program(unsigned, signed)
    except authenticode.Bad as e:
        raise Stop(3, f"the signed file is not the given one plus a signature: {e}") from None
    return os_verify(cfg, signed, run)


# --- one file ------------------------------------------------------------------------------------------------------

def say(text):
    print(f"sign.py: {text}", flush=True)


def wait(api, cfg, digest, pipeline, package_id, sleep=time.sleep, clock=time.monotonic):
    """The signing job of the pipeline once it ended well; Stop when it did not or the wait ran out."""
    end = clock() + 60 * float(cfg["wait_minutes"])
    seen = None
    while True:
        jobs = api.jobs(pipeline)
        sign = next((j for j in jobs if j.get("name") == cfg["job"]), None)
        # deleted once the signing job has ended: before that its fetch may still be to come
        if package_id and (sign and sign.get("status") not in WAITING
                           or any(j.get("status") == "failed" for j in jobs)):
            with_reason(lambda: api.delete_package(package_id))   # the pipeline has taken the file (or will not)
            note(cfg, digest, package=None)
            package_id = None
        failed = next((j for j in jobs if j.get("status") in ("failed", "canceled", "skipped")
                       and (j.get("name") == cfg["job"] or j.get("status") == "failed")), None)
        if failed:
            tail = [x for x in api.trace(failed["id"]).splitlines() if x.strip()][-12:]
            why = f"job {failed.get('name')} {failed.get('status')} (pipeline {pipeline})" + (
                ": " + " / ".join(x.strip()[:200] for x in tail) if tail else "")
            note(cfg, digest, status="failed", why=why[:1500])
            raise Stop(3, f"{why}\nnot signed; no second attempt by itself (the signing token counts wrong inputs): "
                          f"find the cause, then --again")
        if sign and sign.get("status") == "success":
            return sign, package_id
        status = sign.get("status") if sign else "no job yet"
        if status != seen:
            say(f"pipeline {pipeline}: {cfg['job']} {status}")
            seen = status
        if clock() >= end:
            raise Stop(4, f"pipeline {pipeline}: {cfg['job']} still {status} after {cfg['wait_minutes']} min — run "
                          f"the same command again to keep waiting for it (no new signing is started)")
        sleep(POLL)


def with_reason(step):
    try:
        step()
    except Stop as e:
        say(f"the package was not deleted: {e}")


def take(api, job, unsigned, out, cfg, run):
    """The job's artifact checked and written to out. -> the signer."""
    name = os.path.basename(unsigned)
    data = api.artifacts(job)
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:   # a download cut short or replaced on the way: taken again next time
        raise Stop(4, f"the artifact of job {job} is not a whole zip ({len(data)} bytes): {e}") from None
    try:
        body, sums = z.read(f"signed/{name}"), z.read("signed/SHA256SUMS.txt").decode("ascii", "replace")
    except (zipfile.BadZipFile, KeyError) as e:
        raise Stop(3, f"the artifact of job {job} has no signed/{name} and signed/SHA256SUMS.txt: {e}") from None
    folder = os.path.dirname(os.path.abspath(out))
    os.makedirs(folder, exist_ok=True)
    fd, part = tempfile.mkstemp(prefix=".signed-", suffix=".exe", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        signer = check(cfg, unsigned, part, sums, run)
        os.chmod(part, os.stat(unsigned).st_mode & 0o777)   # the given file's mode, not mkstemp's 0600
        os.replace(part, out)
    finally:
        if os.path.exists(part):
            os.unlink(part)
    return signer


def preflight(cfg, run=subprocess.run):
    """What the checks need is here before anything is signed: a missing tool must not cost a signing."""
    for key in ("cafile", "tsa_cafile"):
        if not os.path.isfile(cfg[key]):
            raise Stop(2, f"no {key} {cfg[key]} — the signature could not be checked")
    try:
        r = run([cfg["osslsigncode"], "--version"], capture_output=True, text=True, errors="replace", timeout=60,
                stdin=subprocess.DEVNULL)
        ok = "osslsigncode" in (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        raise Stop(2, f"{cfg['osslsigncode']} does not run — the signature could not be checked")


def sign(path, cfg, out=None, again=False, api=None, run=subprocess.run, sleep=time.sleep, clock=time.monotonic):
    """The signed copy of path at out (default: path itself, replaced only after every check). -> out."""
    out = out or path
    name = os.path.basename(path)
    if not NAME.fullmatch(name):
        raise Stop(2, f"{name}: the pipeline takes a file name of letters, digits and ._- ending in .exe")
    try:
        if authenticode.layout(path)[4]:
            raise Stop(2, f"{name} is already signed")
    except authenticode.Bad as e:
        raise Stop(2, str(e)) from None
    preflight(cfg, run)
    with Lock(cfg):
        return _sign(path, name, sha256(path), cfg, out, again, api or GitLab(cfg), run, sleep, clock)


def _sign(path, name, digest, cfg, out, again, api, run, sleep, clock):
    had = ledger(cfg).get(digest) or {}
    status = had.get("status")
    if status not in (None, "signed", "triggered") and not again:
        raise Stop(3, f"{name} (sha256 {digest}) was already given to the signing and it {status}: "
                      f"{had.get('why') or ''}\nno second attempt by itself — find the cause, then --again")
    if status == "signed" and had.get("job") and not again:
        say(f"{name} was signed by job {had['job']} — taking its result, no new signing")
        try:
            return taken(api, had["job"], path, digest, out, cfg, run)
        except Stop as e:
            if e.code == 4:
                raise Stop(4, f"{e}\nif the job's artifact is gone (it is kept for days), a new signing: --again") from None
            raise
    if status == "triggered" and had.get("pipeline") and not again:
        pipeline, package_id = had["pipeline"], had.get("package")
        say(f"{name} is already in pipeline {pipeline} — waiting for it, no new signing")
    else:
        version = f"{digest[:16]}-{secrets.token_hex(4)}"
        say(f"{name}: sha256 {digest}, {os.path.getsize(path)} bytes — into the signing project's packages")
        package_id = api.upload(cfg["package"], version, name, path)
        # from here until the answer is read a pipeline may have started: a run that dies leaves "unknown"
        note(cfg, digest, status="unknown", package=package_id, name=name, pipeline=None, job=None,
             why="the trigger was sent and its answer was not read")
        try:
            got = api.trigger(cfg["trigger_token"], cfg["ref"], {"ARTIFACT_URL": api.file_url(cfg["package"], version, name),
                                                                 "ARTIFACT_SHA256": digest})
        except Stop as e:
            note(cfg, digest, why=f"the trigger did not answer: {e}")
            with_reason(lambda: api.delete_package(package_id))
            note(cfg, digest, package=None)
            raise Stop(4, f"{e}\nwhether a pipeline started is not known — look at the signing project's pipelines; "
                          f"a new attempt only with --again") from None
        pipeline = got["id"]
        note(cfg, digest, status="triggered", pipeline=pipeline, why=None)
        say(f"pipeline {pipeline} started{(': ' + got['web_url']) if got.get('web_url') else ''}")
    job, package_id = wait(api, cfg, digest, pipeline, package_id, sleep, clock)
    note(cfg, digest, status="signed", job=job["id"])
    return taken(api, job["id"], path, digest, out, cfg, run)


def taken(api, job, path, digest, out, cfg, run):
    """The signing job's result checked and written, the ledger told how it went. -> out."""
    try:
        signer = take(api, job, path, out, cfg, run)
    except Stop as e:
        if e.code == 3:   # the result itself did not pass; a download that broke is taken again next time
            note(cfg, digest, status="rejected", why=f"the result did not pass: {e}"[:1500])
        raise
    note(cfg, digest, signed=sha256(out))
    say(f"signed: {out} ({signer})")
    return out


def step(path, out=None, settings_path=None, require=False, again=False, **kw):
    """The build's signing step: signed when this machine has the settings, else one line and the file as it is.
    -> True signed, False not."""
    try:
        cfg = settings(settings_path)
    except Off as e:
        if require:
            raise Stop(2, f"{e} — signing is required") from None
        say(f"Windows signing is off here ({e}): {os.path.basename(path)} stays unsigned")
        return False
    sign(path, cfg, out=out, again=again, **kw)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sign.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--settings")
    ap.add_argument("--require", action="store_true", help="no settings here is an error (a release)")
    ap.add_argument("--again", action="store_true", help="a new signing of a file whose signing failed")
    ap.add_argument("--verify", nargs=2, metavar=("UNSIGNED", "SIGNED"))
    a = ap.parse_args(argv)
    try:
        if a.verify:
            try:
                cfg = settings(a.settings, tokens=False)
            except Off as e:
                raise Stop(2, str(e)) from None
            say(f"ok: {check(cfg, *a.verify)}")
            return 0
        if not a.file or not os.path.isfile(a.file):
            raise Stop(2, f"no file {a.file}")
        step(a.file, a.out, a.settings, a.require, a.again)
        return 0
    except Stop as e:
        print(f"sign.py: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
