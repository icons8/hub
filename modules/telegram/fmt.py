"""Markdown of session replies → Telegram HTML (parse_mode=HTML) and splitting into messages of up to 4096 characters.

Telegram understands only b, i, u, s, code, pre, a, blockquote; tables and lists go as text: a table goes into <pre>
so the columns do not drift apart. We cut along Markdown blocks before converting to HTML — tags are not torn in the middle.
"""
import html
import os
import re
from urllib.parse import quote

from core import i18n
from modules.files import paths

LIMIT = 3800  # headroom below 4096 for tags and escaping


def _inline(text):
    # code is replaced by a marker during markup: bold and italic around code (**Folder `x`**) are marked up as a whole (asterisks
    # showed up in Telegram), while asterisks inside code stay as they are
    codes = []

    def hide(m):
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"
    # a NUL in the text itself would be read as a marker (an IndexError crashed the bot)
    s = html.escape(re.sub(r"`([^`\n]+)`", hide, text.replace("\x00", "")), quote=False)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)


def blocks(md):
    """Markdown → a list of blocks: ('pre', text) for code and tables, ('text', lines) for the rest."""
    out, buf, lines = [], [], (md or "").replace("\r\n", "\n").split("\n")
    i = 0

    def flush():
        if buf:
            out.append(("text", "\n".join(buf)))
            buf.clear()
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush()
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            out.append(("pre", "\n".join(body)))
            i += 1
            continue
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1]):
            flush()
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i]):
                    body.append(" │ ".join(c.strip() for c in lines[i].strip().strip("|").split("|")))
                i += 1
            out.append(("pre", "\n".join(body)))
            continue
        buf.append(line)
        i += 1
    flush()
    return out


def render_text(block, nested=False):
    rows = []
    for line in block.split("\n"):
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if m:
            rows.append(f"<b>{_inline(m.group(1))}</b>")
            continue
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if m:
            rows.append(f"{'  ' * (len(m.group(1)) // 2)}• {_inline(m.group(2))}")
            continue
        m = re.match(r"^\s*>\s?(.*)$", line)
        if m:   # a quote is not nested inside a folded blockquote (Telegram: no blockquote inside blockquote)
            rows.append(f"▎ {_inline(m.group(1))}" if nested else f"<blockquote>{_inline(m.group(1))}</blockquote>")
            continue
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            rows.append("—")
            continue
        rows.append(_inline(line))
    return "\n".join(rows)


def render(kind, body, nested=False):
    return f"<pre>{html.escape(body)}</pre>" if kind == "pre" else render_text(body, nested)


def _pack(md, cur, limit, first=None, nested=False):
    """md blocks → HTML messages no longer than limit (the first one — first); cur — already placed at the start of the first."""
    msgs, first = [], first or limit
    for kind, body in blocks(md):
        pieces = [body]
        step = min(first, limit)
        if len(body) > step:  # one huge block — by lines
            pieces, part = [], ""
            lines = [ln[k:k + step] for ln in body.split("\n") for k in range(0, max(len(ln), 1), step)]
            for line in lines:
                if len(part) + len(line) + 1 > step and part:
                    pieces.append(part)
                    part = ""
                part += ("\n" if part else "") + line
            pieces.append(part)
        for piece in pieces:
            chunk = render(kind, piece, nested)
            if cur and len(cur) + len(chunk) + 2 > (limit if msgs else first):
                msgs.append(cur)
                cur = ""
            cur = (cur + "\n" if cur else "") + chunk
    if cur.strip():
        msgs.append(cur)
    return msgs


# Folded view (telegram.fold_long): the first line is the gist, as plain text; everything else is in
# <blockquote expandable>, expanded by a tap. Long means: after the first line more than FOLD_CHARS characters or more than
# FOLD_LINES non-empty lines; a gist longer than FOLD_GIST characters does not count as a gist — then it is shown as usual.
FOLD_CHARS, FOLD_LINES, FOLD_GIST = 300, 3, 1000
FOLDED = "<blockquote expandable>{}</blockquote>"


def gist_split(md):
    """(gist, rest) of a long text, otherwise None: it is short or starts with code or a table."""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    k = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if k is None or lines[k].strip().startswith(("```", "|")) or len(lines[k]) > FOLD_GIST:
        return None
    rest = "\n".join(lines[k + 1:]).strip("\n")
    if len(rest.strip()) <= FOLD_CHARS and sum(1 for ln in rest.split("\n") if ln.strip()) <= FOLD_LINES:
        return None
    return lines[k], rest


def to_messages(md, head="", fold=False):
    """Ready HTML messages. head — a heading line (already HTML) at the start of the first. fold — a long text is folded: the gist only
    in the first message, the rest in each one — entirely in its own blockquote."""
    split = gist_split(md) if fold else None
    top = split and (head + "\n" if head else "") + render_text(split[0])
    room = LIMIT - len(FOLDED.format(""))
    if not split or len(top) > room // 2:   # a gist that grew from escaping would leave no room for the body — as usual
        return _pack(md, head, LIMIT) or [head or i18n.t("telegram.empty")]
    chunks = _pack(split[1], "", room, room - len(top) - 1, nested=True)
    return [(top + "\n" if not i else "") + FOLDED.format(c) for i, c in enumerate(chunks)] or [top]


def plain(html_text):
    """An HTML heading line as Telegram shows it (and returns in reply_to_message): without tags."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text or "")).strip()


CODE = re.compile(r"<code>([^<]{1,300})</code>")
PATHLIKE = re.compile(r"[\w.~/-]*[\w-]\.[A-Za-z0-9]{1,8}|[\w.~-]*/[\w./-]+")
# folders with a space in an absolute path (`My Projects`): the word after the space starts with a capital letter or a digit, otherwise it is a phrase or
# a command (`/usr/bin/ls -la`); the same rule is in modules/web/static/js/markdown.js
SPACED = r"[\w.@+-]+(?: [A-Z0-9][\w.@+-]*)*"
ABS_CODE = re.compile(r"~?(?:/" + SPACED + r"){2,}")


def _from(session):
    """Which session the link is from — file comments in "Files" will go to it."""
    return f"&session={quote(session)}" if session else ""


def link_paths(text, base, cwd, exists=os.path.exists, session=None):
    """`path` → a link into the "Files" module. Only if the file exists; the path is from the session
    folder, `file:line` — to the file. base — the "Files" address in the tailnet; without it — the text as it was."""
    if not base:
        return text

    def one(m):
        raw = html.unescape(m.group(1))
        path = re.sub(r":\d+(:\d+)?$", "", raw)
        if "://" in path or not (" " not in path and PATHLIKE.fullmatch(path) or ABS_CODE.fullmatch(path)):
            return m.group(0)
        full = paths.find(path, cwd, exists)  # from the session folder and its parents; without duplication
        if not full:
            return m.group(0)
        line = re.search(r":(\d+)(?::\d+)?$", raw)  # `file:89` — to line 89
        href = f"{base}/open?path={quote(full)}" + (f"&line={line.group(1)}" if line else "") + _from(session)
        return f'<a href="{html.escape(href)}">{m.group(1)}</a>'
    return CODE.sub(one, text)


def link_sessions(text, base, names):
    """`name` of an existing session → a link to its tab on the hub page. Only code as a whole from the name;
    base — the page address in the tailnet, without it — as it was."""
    if not base or not names:
        return text

    def one(m):
        name = html.unescape(m.group(1))
        if name not in names:
            return m.group(0)
        return f'<a href="{html.escape(base)}/#/s/{quote(name, safe="")}">{m.group(0)}</a>'
    return CODE.sub(one, text)


# a bare file name (without a folder) — only with a "document" extension (a name like "(inventory.md)" must be highlighted
# so one can look at what spec it is); no js/ts — "Node.js" is not a file
BARE_EXT = "md|markdown|txt|json|jsonl|yaml|yml|toml|csv|tsv|log|pdf|html|sql|sh|py|ini|env|xml|png|jpg|jpeg|svg|webp"
TEXT_PATH = re.compile(r"(^|[\s(«\"'])(~?(?:/" + SPACED + r")+(?=/)/[\w.@-]+\.[A-Za-z0-9]{1,8}"
                       r"|(?:~|\.{1,2})?/?[\w.@-]+(?:/[\w.@-]+)+\.[A-Za-z0-9]{1,8}"
                       r"|[\w@-][\w.@-]*\.(?:" + BARE_EXT + r"))(?::(\d+))?(?![\w/])")
TAG = re.compile(r"(<[^>]+>)")


def link_text(text, base, cwd, exists=os.path.exists, session=None):
    """A path in plain text (a session question, options): with a slash and an extension, only if the file exists;
    inside a, code and pre we do not touch."""
    if not base:
        return text
    out, depth = [], 0
    for part in TAG.split(text):
        if TAG.fullmatch(part):
            name = re.match(r"</?(\w+)", part)
            if name and name.group(1).lower() in ("a", "code", "pre"):
                depth += -1 if part.startswith("</") else 1
            out.append(part)
            continue
        if depth:
            out.append(part)
            continue

        def one(m):
            path = html.unescape(m.group(2))
            full = paths.find(path, cwd, exists) if re.search(r"[a-z]", re.sub(r"\.\w+$", "", path)) else None
            if not full:
                return m.group(0)
            href = f"{base}/open?path={quote(full)}" + (f"&line={m.group(3)}" if m.group(3) else "") + _from(session)
            shown = m.group(2) + (f":{m.group(3)}" if m.group(3) else "")
            return f'{m.group(1)}<a href="{html.escape(href)}">{shown}</a>'
        out.append(TEXT_PATH.sub(one, part))
    return "".join(out)
