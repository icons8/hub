"""Which secrets stay out of the journal.

- `mask` / `mask_data`: known forms of keys and tokens in tool input and output, background commands and permission
  requests become `[masked]` (journal.event, host, hubd, watchdog, cleanup of old journals — core/scrub.py);
- `public_catalog`: the tool catalog in the journal (`catalog.loaded`, snapshots, state.json, archives, the page, `hub
  status --json`) — without the `env` and `headers` of MCP servers: they hold keys and tokens. Sessions get the whole
  server from the catalog that hubd keeps in memory (daemon.Hub.tools), in mcp.json only for their own user.
"""
import re
import unicodedata

MASK = "[masked]"

# The mask takes only the characters of the key itself, never what the shell executes or substitutes: the owner approves
# a permission request by its masked copy, while the session executes the input as is.
# Private key: base64 lines up to the END line (real newlines, escaped \n or spaces); a block without END (truncated
# output) is the BEGIN line and the whole base64 lines right after it. Text after the key stays. A whole body line is
# 40+ characters (64 or 70 in keys): a short line of base64 characters (`/usr/local/bin/x`) can be a command, and after
# a BEGIN line without END it stays visible. Headers of an old encrypted key (Proc-Type, DEK-Info) are part of the
# block.
_PEM_SEP = r"(?:(?:[ \t]*(?:\r?\n|(?:\\r)?\\n))+[ \t]*|[ \t]+)"
_PEM_HEAD = rf"(?:{_PEM_SEP}(?:Proc-Type|DEK-Info): [A-Za-z0-9,\-]{{1,80}})*"
_PEM = (rf"-----BEGIN [A-Z ]{{0,40}}PRIVATE KEY-----{_PEM_HEAD}(?:(?:{_PEM_SEP}[A-Za-z0-9+/=]{{40,}})*"
        rf"(?:{_PEM_SEP}[A-Za-z0-9+/=]{{1,39}})?{_PEM_SEP}-----END [A-Z ]{{0,40}}PRIVATE KEY-----"
        rf"|(?:{_PEM_SEP}[A-Za-z0-9+/=]{{40,}})*)")
# A link of the hub bus is masked in every event of the journal (core/journal.py), not only the tools' mirror: it holds
# a one-time key for linking two hubs. A wrapped link (a mail client at 76 columns) is read whole by the core, so the
# mask goes on across a line break only after a line of wrapping width (16 to 200 characters: mail clients wrap at 72-78,
# narrow chats at 30-40); a link on one line is longer than that (a signed card and a key are in it, 600 and more), so
# the line after it is text, even when its first word is a 16-character run.
_B64 = "[A-Za-z0-9_-]"
_SEP = r"[ \t]*\r?\n[ \t]*"
_WRAPPED = rf"(?:{_B64}{{16,200}}(?!{_B64}){_SEP})*"   # whole wrapped lines, each followed by a break
_LINK = rf"\bhub-link-1:\s*{_WRAPPED}{_B64}{{16,}}"
# Only high-confidence forms: a false positive hides text from the page, a miss leaves a key in the journal.
_TOKENS = [
    r"\bsk-ant-[A-Za-z0-9_\-]{20,}",                       # Anthropic
    r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}",  # OpenAI
    r"\bgh[pousr]_[A-Za-z0-9]{36,}", r"\bgithub_pat_[A-Za-z0-9_]{22,}",   # GitHub
    r"\bglpat-[A-Za-z0-9_\-]{20,}",                        # GitLab
    r"\bxox[abposr]-[A-Za-z0-9\-]{10,}",                   # Slack
    r"\bxapp-\d-[A-Za-z0-9\-]{20,}",                        # Slack, app token (Socket Mode)
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",                    # AWS, access key ID
    r"\bAIza[0-9A-Za-z_\-]{35}",                           # Google API key
    r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b",                  # Telegram bot
    r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",           # Stripe
    r"\bnpm_[A-Za-z0-9]{36}\b",                          # npm
    r"\bhf_[A-Za-z0-9]{30,}",                            # Hugging Face
    r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",   # JWT
    r"\beyJhIjoi[A-Za-z0-9+/_\-]{40,}={0,2}",                 # Cloudflare Tunnel token (base64 of {"a":…,"t":…,"s":…})
    r"\bEAA[A-Za-z0-9]{60,}",                            # Meta, access token (WhatsApp Cloud API)
    r"\b[A-Za-z0-9_~.\-]{3}\dQ~[A-Za-z0-9_~.\-]{31,34}",   # Entra, app secret
    r"\bya29\.[0-9A-Za-z_\-]{20,}",                        # Google OAuth, access token
    r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b",     # SendGrid
    r"\bSK[0-9a-fA-F]{32}\b",                            # Twilio, API key
    _LINK,                                               # a link of the hub bus (a one-time key inside)
    _PEM,
]
LINK = re.compile(_LINK)
LINK_MARK = "hub-link-1:"
# A relay invitation of the hub bus is a permit: its signature is what the relay takes, so the signature is masked; the
# fields before it (the relay's address and key, the permit's id, who made it) stay — the core reads them from the
# masked request (core/busstate._relay). A wrapped invitation is read whole, as a link.
_RUN = r"[A-Za-z0-9_-]+(?:[ \t]*\r?\n[ \t]*[A-Za-z0-9_-]+)*"
INVITE = re.compile(rf"\b(hub-relay-invite-1:\s*{_RUN}\.)(?:{_B64}{{1,15}}(?!{_B64}){_SEP})?{_WRAPPED}{_B64}{{16,}}")
INVITE_MARK = "hub-relay-invite-1:"


def mask_links(value):
    """A copy of a JSON value with every bus link replaced by MASK and every relay invitation's signature too (the rest of
    the text as is)."""
    if isinstance(value, str):
        if INVITE_MARK in value:
            value = INVITE.sub(lambda m: m.group(1) + MASK, value)
        return LINK.sub(MASK, value) if LINK_MARK in value else value
    if isinstance(value, dict):
        return {k: mask_links(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_links(v) for v in value]
    return value
_TOKEN = re.compile("|".join(f"(?:{p})" for p in _TOKENS))
# the value after the Authorization scheme: the header name and the scheme stay readable
_AUTH = re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+)[A-Za-z0-9._~+/=\-]{8,}")
# the password in the user:password@host of an address; the user and host stay readable. The scheme starts a word (the
# lookbehind keeps parsing of long dotted words linear)
_URL_PASSWORD = re.compile(r"""(?<![A-Za-z0-9+.\-])(\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]+:)"""
                           r"""([^\s@/$`()\[\]{};|&<>"'\\]+)(?=@)""")
# NAME=value (shell, .env, address query, --flag=), name: value (YAML, JSON, header) — whether it is a key is decided by
# _secret_key. Not taken, as code and text: `NAME = "…"` with spaces (a constant), a name with a dot (translation
# catalog), a value with spaces (a phrase), ==, :=, ::. The name starts a word (the lookbehind keeps parsing linear).
# The match is the name only: _assignments reads the value, and a value of a non-key name (https://…?token=…) is
# searched further
_ASSIGN = re.compile(r"""(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_\-]{0,63})["']?([ \t]*:(?![:=])[ \t]*|=(?!=))""")
# the value is the characters of the key itself: a quote, a space or what the shell executes or substitutes ($, `, \, ;,
# |, a bracket) ends a bare value; a quoted value with such a character inside is not a simple value
_BARE = re.compile(r"""[^\s"'`,;&|(){}\[\]<>$\\]+""")
# name = value as a whole line (INI, TOML, ~/.aws/credentials): an unquoted value of 16+ characters up to the end of the
# line — code is not written like that (`password = hash(password)`, `token = None`, a quoted constant is not this
# form)
_INI = re.compile(r"""(?m)^([ \t]*([A-Za-z_][A-Za-z0-9_\-]{0,63})[ \t]+=[ \t]+)([^\s"'`,;&|(){}\[\]<>$\\]{16,})"""
                  r"""(?=[ \t]*\r?$)""")
_QUOTED = re.compile(r"""(["'])([^"'\s`$\\]{6,}?)\1""")
_LOWER_SECRETS = ("password", "passwd", "secret", "token", "apikey", "accesskey", "privatekey", "secretkey",
                  "credential", "credentials")
# what stands next to a key name but is not a key: a number, a path, an address (its password is _URL_PASSWORD's job) and
# code, a word or a placeholder (self.token, os.environ, "same-origin", your_api_key_here) — except a shell or .env
# assignment (NAME=value), where the word is the value
_PLAIN = re.compile(r"\d+$|[/~]|\.\.?/|[A-Za-z][A-Za-z0-9+.\-]*://")   # .match inside the value
# a word or a list of words in any language (`same-origin`, `letters,digits`, a Russian word) and a code path through a
# dot from a lowercase word (`process.env.S3_KEY`, `user.apiHash2`; not `dp.st.…`, not `Ab3x9K.pQ7z`)
_CODE = re.compile(r"(?:[^\W\d]|[.,\-])+|[a-z_]{3,}(?:\.[A-Za-z_]\w*)+")
# page cursors — the whole name (not uppercase: FB_PAGE_TOKEN is a key): Graph $skiptoken, AWS NextToken,
# nextPageToken, page_token, nextSyncToken
_CURSORS = ("skiptoken", "deltatoken", "nexttoken", "nextpagetoken", "pagetoken", "continuationtoken", "synctoken",
            "nextsynctoken")
_SECRET_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS", "PAT")


def secret_name(name):
    """Whether an environment variable name looks like a key: a part of the name (split by `_`) is KEY, TOKEN, SECRET,
    PASSWORD… or ends with one of them (APIKEY, AUTHTOKEN, PGPASSWORD). TOKENIZERS, KEYRING, KEYBOARD and MAX_…_TOKENS
    are not."""
    for part in str(name).upper().split("_"):
        if part in _SECRET_PARTS or part.endswith(("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")):
            return True
    return False


# headers of every claude request (core/modlaunch.HEADERS): in a session they carry the account gateway's secret
_SECRET_ENV = ("ANTHROPIC_CUSTOM_HEADERS",)


def child_env(env, keep=()):
    """A copy of env for a program that needs none of the hub's keys (Chrome, chrome-devtools-mcp): without the
    variables secret_name takes for keys and the claude request headers; names in keep pass as they are."""
    keep = set(keep or ())
    return {k: v for k, v in env.items() if k in keep or not (secret_name(k) or k.upper() in _SECRET_ENV)}


def _secret_key(name):
    """A name in UPPERCASE is an environment variable: secret_name decides. Any other (password, api_key, clientSecret,
    x-api-key) is a key when it ends with a key word; a bare key is not (sort key, primary key)."""
    flat = name.lower().replace("_", "").replace("-", "")
    if name.upper() != name and flat in _CURSORS or flat.endswith("sitekey"):
        return False   # a page cursor; the public site key of a captcha (RECAPTCHA_SITE_KEY, turnstileSitekey)
    if name.upper() == name:
        return name != "KEY" and secret_name(name.replace("-", "_"))   # KEY=value in documentation is a placeholder
    return name.lower().replace("_", "").replace("-", "").endswith(_LOWER_SECRETS)


def secret_key(name):
    """Whether a settings key or variable names a secret (the same rule as the mask of assignments): `hub
    support-bundle` masks such values of hub.json."""
    return _secret_key(name)


def _assignments(text):
    """Values of assignments with a key name become MASK, quotes stay in place. Linear over the text: a bare value ends
    where the run of value characters ends, and that end is looked up once per run, however many names stand in it."""
    out, pos, run = [], 0, (0, 0)
    for m in _ASSIGN.finditer(text):
        name, sep = m.groups()
        if m.start() < pos or not _secret_key(name) or m.start() >= 2 and text.startswith("${", m.start() - 2) \
                and text.startswith("?", m.end()):
            continue   # inside an already masked value; not a key name; ${NAME:?error text} (not ${NAME:-value})
        p = m.end()
        quoted = _QUOTED.match(text, p)
        if quoted:
            start, end = p + 1, quoted.end() - 1
        elif text.startswith(("'", '"'), p):
            continue   # a quoted value with a space or a substitution inside is not a simple value
        else:
            if not run[0] <= p < run[1]:
                bare = _BARE.match(text, p)
                run = (p, bare.end() if bare else p)
            start, end = p, max(run[1], p)
        shell = sep == "=" and name.upper() == name
        if (end - start < 6 or text.startswith(("%", "{{"), start) or quoted and MASK in quoted.group(2)
                or _PLAIN.match(text, start, end) or not shell and _CODE.fullmatch(text, start, end)):
            continue   # a variable, a template, a path, code — not a value
        out += [text[pos:start], MASK]
        pos = end
    return "".join(out) + text[pos:] if out else text


def _ini(m):
    value = m.group(3)
    if not _secret_key(m.group(2)) or _PLAIN.match(value) or _CODE.fullmatch(value):
        return m.group(0)
    return m.group(1) + MASK


# A label in words, the way a person writes a password in a message: "Password: ...", "**Token:** ...", "Password (for
# the admin page): ...", and in every language with data (core/langdata.py `secrets.labels`) also with up to three words
# before the separator ("the password of the mail: ..."), with "=" or a dash for one and with the value on the next
# line. The label is a whole word in any case (a word written without spaces, CJK and kana, has no boundary). The value
# is one run of characters right after the separator: a value that is a word, a name, a number, a time, a path, a
# file:line reference or an assignment is not a key (prose such as "the token - path/to/file" around such words in the
# owner's journal). A tool's output that is JSON with escaped letters (\\uXXXX) holds the label escaped, and that form
# is matched too. English labels take only ":" and a bracket, on one line: "password:" and "token:" alone are already
# assignments (_ASSIGN), and English prose and code ("Reset password - Site", `password = "..."`, "the token expired:
# ...", "if token:" and the next line) hold such words with a dash, words around them or a value on the next line. The
# labels are matched in the lowercased text as one tree of their common beginnings: a long alternation is tried at every
# position of the text, ten times slower.
# A private key is a PEM block (_PEM): "private key: base64 ..." in English is how code describes one.
_EN_LABELS = ("password", "passwd", "passphrase", "secret", "token", "api key", "access key", "secret key",
              "client secret", "access token", "refresh token", "auth token", "bot token")
_UNSPACED = re.compile("[%s-%s%s-%s%s-%s%s-%s]" % tuple(chr(c) for c in (0x3040, 0x30FF, 0x3400, 0x4DBF, 0x4E00, 0x9FFF,
                                                                       0xF900, 0xFAFF)))   # kana, CJK
# a space; French typography puts a no-break one before ":" (in escaped JSON too)
_SP = r"(?:[ \t\N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}]|\\u00a0|\\u202f)"
_GAP = r"[ \t\N{NO-BREAK SPACE}_-]+"      # between the words of a label: "api key", "api-key", "API_KEY"
_GAP_ESCAPED = r"(?:[ \t_-]|\\u00a0)+"
# a word between a label and its separator: anything but a space, a separator, a bracket or a quote (a vowel sign of
# Devanagari is not \w; an escaped letter is \uXXXX)
_WORD = (r"[^\s:=()\"'\-\N{EN DASH}\N{EM DASH}\N{FULLWIDTH COLON}\N{FULLWIDTH LEFT PARENTHESIS}"
         r"\N{FULLWIDTH RIGHT PARENTHESIS}][^\s:=()\"'\N{FULLWIDTH COLON}\N{FULLWIDTH LEFT PARENTHESIS}"
         r"\N{FULLWIDTH RIGHT PARENTHESIS}]{0,59}")
_COLONS = (":", "\N{FULLWIDTH COLON}", "\\uff1a")
_AFTER = (
    r"(?:[*_]{1,2})?"   # the end of an emphasis: **Password**: ...
    rf"(?P<words>(?:{_SP}+{_WORD}){{1,3}})?"
    rf"(?:{_SP}*[(\N{{FULLWIDTH LEFT PARENTHESIS}}]"
    r"[^()\n\N{FULLWIDTH LEFT PARENTHESIS}\N{FULLWIDTH RIGHT PARENTHESIS}]{1,60}[)\N{FULLWIDTH RIGHT PARENTHESIS}])?"
    r"""(?:\\?["']|[*_]{1,2})?"""
    rf"(?P<sep>{_SP}*(?::|\N{{FULLWIDTH COLON}}|\\uff1a)(?![:=])|{_SP}*=(?!=)"
    rf"|{_SP}+(?:-|\N{{EN DASH}}|\N{{EM DASH}}|\\u201[34])(?={_SP}))"
    rf"{_SP}*(?P<line>\r?\n{_SP}*)?(?:[*_]{{1,2}}{_SP}*)?"
    r"(?:\\?[\"'`\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}\N{LEFT DOUBLE QUOTATION MARK}"
    r"\N{DOUBLE LOW-9 QUOTATION MARK}\N{LEFT SINGLE QUOTATION MARK}])?")
# what ends a sentence or an emphasis after the value, not the value itself
_TAIL = ".:*\N{HORIZONTAL ELLIPSIS}\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}" \
        "\N{RIGHT SINGLE QUOTATION MARK}"
_FILE_LINE = re.compile(r"[\w./\-]+\.\w{1,8}:\d+(?::\d+)?")   # core/redact.py:42
_NAMED = re.compile(r"[A-Za-z_][\w\-]*=[^=]")   # NAME=value (not the "=" padding of base64): the assignment's own rule
_SHORT_SHA = re.compile(r"[0-9a-f]{7,8}|[0-9a-f]{12}")   # a commit, a hash prefix (sha256[:8])
_NOT_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+"                  # an issue: ABC-123
                      r"|\d{1,2}(?::\d\d){1,2}(?:\.\d+)?Z?"   # a time: 14:57:48Z
                      r"|\d{4}-\d\d-\d\d(?:T\S*)?"            # a date
                      r"|\d+(?:[.,]\d+)?(?i:ms|s|m|h|d|min|k|kb|mb|gb|tb|px|x)"   # a number with a unit: 1500ms
                      r"|0x[0-9a-fA-F]+"                      # a number in hex
                      r"|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"       # the name of a variable: S3_BUCKET2
                      r"|[^@\s]+@[\w\-]+(?:\.[\w\-]+)+"       # an email address
                      r"|[\w\-.]+\.[a-z]{2,4}\d?")            # a file: notes-1.0.zip
# a lowercase slug of words, each maybe with a number (shop-landing, max-5x, tok-corp-2), two words at least: one word
# and a number is a password (qwerty-123); not a part of a UUID (3f2a9c1e)
_SLUG_PART = r"(?:[^\W\d_]+\d*|\d+[^\W\d_]?|\d+)"
_SLUG = re.compile(rf"{_SLUG_PART}(?:[-_.]{_SLUG_PART})+")
# a part of a path, a package (@scope/name) or a cut address (host…/v1/x?a=b): a lowercase or a Capitalized word, not
# base64
_PATH_PART = re.compile(r"[a-z0-9._~@?=&%+\-\N{HORIZONTAL ELLIPSIS}]*|[A-Z][a-z0-9._\-]*")
_WORDISH = "-.,:/@~#'_"   # what joins words into a name, a handle, a path or a reference


def _letter(c):
    """A letter, or a mark that is a part of one (the vowel signs of Devanagari are not \\w)."""
    return c.isalpha() or unicodedata.category(c)[0] == "M"


def _key_value(value):
    """Whether a run after a label in words can be a key: not a number, a time or a date (no letters), a word, a name,
    a handle or a path of words (no digits), a path of lowercase or Capitalized words, a lowercase slug, a file:line
    reference, an assignment, a commit or a hash prefix, an issue key, a number with a unit, an email address, a
    variable's name, a file name or words of a non-Latin language joined to a number (prose around a label in the
    owner's journal held all of these)."""
    if value[-1:] in "!?" and not _key_value(value[:-1]):
        return False   # a word and a sign of the sentence: "rotated?"; a password may end with one: "Summer2024!"
    if not any(c.isalpha() for c in value) or _PLAIN.match(value) or _CODE.fullmatch(value):
        return False
    if not any(c.isdigit() for c in value) and all(_letter(c) or c in _WORDISH for c in value):
        return False
    if not any(c.isascii() and c.isalpha() for c in value) and any(c in _WORDISH for c in value):
        return False   # words of a language without Latin letters joined to a number: "1-<week>", "8:<app>"
    if "/" in value and all(_PATH_PART.fullmatch(p) for p in value.split("/")):
        return False
    if value == value.lower() and _SLUG.fullmatch(value) and sum(
            any(c.isalpha() for c in part) for part in re.split(r"[-_.]", value)) >= 2:
        return False
    return not (_FILE_LINE.fullmatch(value) or _NAMED.match(value) or _SHORT_SHA.fullmatch(value)
                or _NOT_KEY.fullmatch(value))


def _tree(phrases, escaped):
    """One regex of the phrases, factored by their common beginnings, over the lowercased text. A space or a hyphen is
    a gap; with escaped, a letter past ASCII is its JSON escape (of the lowercase or the uppercase letter)."""
    root = {}
    for phrase in phrases:
        node = root
        for ch in phrase:
            node = node.setdefault(" " if ch in " -" else "'" if ch in "'\N{RIGHT SINGLE QUOTATION MARK}" else ch, {})
        node[""] = {}

    def piece(ch):
        if ch == " ":
            return _GAP_ESCAPED if escaped else _GAP
        if ch == "'":
            return r"(?:'|\\u2019)" if escaped else "['\N{RIGHT SINGLE QUOTATION MARK}]"
        # a letter whose uppercase lowercases to another one (Turkish dotless i: PAROLASI -> parolasi) and the Turkish
        # dotted capital I, which lowercases to two characters and stays as it is (_said)
        forms = {ch} | ({ch.upper().lower()} if len(ch.upper().lower()) == 1 else set())
        forms |= {"\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"} if ch == "i" else set()
        if escaped:   # ASCII stays a letter in escaped JSON (lowercased here), the rest is \uXXXX of either case
            forms |= {ch.upper()} if len(ch.upper()) == 1 else set()
            alts = sorted(c for c in forms if c.isascii() and c == c.lower())
            alts += [r"\\u%04x" % ord(c) for c in sorted(forms) if not c.isascii()]
            return alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        return re.escape(ch) if len(forms) == 1 else "[" + "".join(sorted(forms)) + "]"

    def emit(node, last):
        alts = [piece(ch) + emit(sub, ch) for ch, sub in sorted(node.items()) if ch]
        if "" in node:   # a phrase ends here, and so does its word (one written without spaces has no end)
            alts.append("" if _UNSPACED.match(last) else r"(?!\w)")
        return alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
    return emit(root, "")


def _labels():
    """The regexes of a label in words: over plain text, and over JSON with escaped letters (only the labels with a
    letter past ASCII: the rest reads the same there)."""
    from core import langdata   # stdlib only
    phrases = []
    for phrase in list(_EN_LABELS) + [x for x in langdata.words("secrets", "labels") if isinstance(x, str)]:
        phrase = " ".join(phrase.lower().split())
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    out = []
    for escaped in (False, True):
        group = [p for p in phrases if not (escaped and p.isascii())]
        spaced = [p for p in group if not _UNSPACED.match(p)]
        unspaced = [p for p in group if _UNSPACED.match(p)]
        # a word: nothing of a word before it, or a line break escaped in JSON
        alts = [r"(?:(?<!\w)|(?<=\\[nrt]))" + _tree(spaced, escaped)] if spaced else []
        alts += [_tree(unspaced, escaped)] if unspaced else []
        out.append(re.compile("(?P<label>" + "|".join(alts or ["(?!)"]) + ")" + _AFTER))
    return tuple(out)


_SAID = {}   # the loaded language data -> (plain, escaped): compiled on the first mask, not at import (every hook
# imports the journal); other data loaded (a test's) — other labels, as langdata.fold


def _said(text, said):
    """Values after a label in words become MASK; what fails the checks stays as is. A label that is refused is passed
    by itself only: another one may stand in its qualifier ("API key and <a password in words>: ...")."""
    low = text.lower()
    if len(low) != len(text):   # a letter that lowercases to two (U+0130): the positions must stay the same
        low = "".join(c.lower() if len(c.lower()) == 1 else c for c in text)
    out, pos, at = [], 0, 0
    while m := said.search(low, at):
        at = m.end("label")
        if " ".join(re.split(_GAP, m.group("label"))) in _EN_LABELS and (
                m.group("sep").strip(" \t\N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}") not in _COLONS
                or m.group("words") or m.group("line")):
            continue   # an English label: only "label: value" and "label (...): value" on one line
        p = m.end()
        bare = _BARE.match(text, p)
        end = bare.end() if bare else p
        while end > p and text[end - 1] in _TAIL:
            end -= 1
        if end - p < 6 or not _key_value(text[p:end]):
            continue
        out += [text[pos:p], MASK]
        pos = at = end
    return "".join(out) + text[pos:] if out else text


def mask(text):
    """The text with known forms of keys replaced by MASK. A second pass changes nothing."""
    if not isinstance(text, str) or not text:
        return text
    text = _TOKEN.sub(MASK, text)
    text = _AUTH.sub(lambda m: m.group(1) + MASK, text)
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + MASK, text)
    from core import langdata
    key = tuple(id(langdata._cache.get(x)) for x in langdata.LANGS)
    if key not in _SAID:
        _SAID.clear()
        _SAID[key] = _labels()
    plain, escaped = _SAID[key]
    text = _said(_assignments(_INI.sub(_ini, text)), plain)
    return _said(text, escaped) if "\\u" in text else text


SPAN = 8192   # the mask sees this many characters past the cut: a key on the boundary (PEM ~3 KB) is recognized whole


def mask_head(text, limit):
    """The first limit characters of the text, masked: the mask runs before truncation (a truncated key tail is shorter
    than the pattern and cannot be recognized afterwards) and over a slightly longer piece, not megabytes of output."""
    return mask(text[:limit + SPAN])[:limit]


def mask_data(value):
    """A copy of a JSON value with every string passed through mask; the original is not changed."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {k: mask_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_data(v) for v in value]
    return value


PRIVATE_SERVER_KEYS = ("env", "headers")


def public_server(server):
    """An MCP server description without what may hold keys: env and headers are dropped, the rest (arguments, address)
    goes through the mask."""
    if not isinstance(server, dict):
        return server
    return mask_data({k: v for k, v in server.items() if k not in PRIVATE_SERVER_KEYS})


def public_catalog(tools):
    """The tool catalog as the journal keeps it: MCP servers go through public_server, the rest as is."""
    out = {}
    for tid, t in (tools or {}).items():
        out[tid] = {**t, "server": public_server(t["server"])} if isinstance(t, dict) and "server" in t else t
    return out
