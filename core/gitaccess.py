"""Why git could not reach GitHub, in a form the owner can act on: the kind of the failure from git's own message and a
short hint per kind (auth, ssh, missing, network). Used by the update check, the private modules step and the page; the
classification is pure text matching, so the same message gives the same kind everywhere (and in old journals).

Hints name only harmless commands (`gh auth login`, `gh auth setup-git`, `gh auth status`): none of them changes the
repository or removes anything."""
import re

from core import langdata   # the hub's own words for a git that did not answer or start (codestore.git), earlier forms

# The first match wins. ssh first: its message also says "Could not read from remote repository", which is no kind of its own.
KINDS = (
    ("ssh", re.compile(r"permission denied \(publickey", re.I)),
    ("missing", re.compile(r"repository .{0,200}?not found|returned error: 404", re.I)),
    ("auth", re.compile(r"authentication failed|could not read (?:username|password)|terminal prompts disabled"
                        r"|invalid username or (?:password|token)|password authentication is not supported"
                        r"|returned error: 40[13]", re.I)),
    ("network", re.compile(r"could not resolve host|could not resolve hostname|failed to connect|connection (?:timed out"
                           r"|refused|reset)|operation timed out|no route to host|could not resolve proxy"
                           r"|network is unreachable|temporary failure in name resolution"
                           # the hub's own timeout of git (codestore.git, modules/update Updater.git), earlier in Russian
                           r"|no answer within \d+ s|" + langdata.legacy("git_no_answer"), re.I)),
)
NOT_STARTING = "git does not start"   # codestore.git: git itself cannot be run

# string literals, not a computed key: the catalog checks (tools/i18n.py) find the keys the code asks for by them
HINTS = {
    "auth": "web.updates.git_hint_auth",
    "ssh": "web.updates.git_hint_ssh",
    "missing": "web.updates.git_hint_missing",
    "network": "web.updates.git_hint_network",
}


def kind(text):
    """The kind of the failure by git's message: auth | ssh | missing | network, or None (not recognised, or not a
    matter of access)."""
    s = str(text or "")
    return next((k for k, rx in KINDS if rx.search(s)), None)


# a short code of why access failed, for the stats report (no texts, no paths): git itself does not run in the hub's folder / no sign-in
# (a login or a key) / no network / otherwise no access (the repository is not visible to us, or git said nothing we recognise)
def code(text):
    """git | auth | network | no-access by git's message (the codestore.git words for a git that does not start, and
    their earlier form)."""
    s = str(text or "")
    if s.startswith((NOT_STARTING, langdata.legacy("git_not_starting"))):
        return "git"
    return {"auth": "auth", "ssh": "auth", "network": "network"}.get(kind(s), "no-access")


def hint(text):
    """The hint for the failure in this process's language, or "" when the kind is not recognised."""
    k = kind(text)
    if not k:
        return ""
    from core import i18n
    return i18n.t(HINTS[k])
