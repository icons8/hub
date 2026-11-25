"""A report as one rich Telegram message (hub send-rich, option telegram.rich): an .md or .html file with local pictures
becomes an InputRichMessage field (markdown | html) and a media list. A picture by a path on disk becomes a link
tg://photo?id=mN, and its file is uploaded in the same request (attach://mN); web addresses stay as they are. Bot API
limits (core.telegram.org/bots/api#rich-message-formatting-options): 32768 characters, 50 media, 500 blocks; too many
characters or media is seen before sending (too_big), the rest Telegram names itself by a refusal. The fallback is plain
Markdown and the pictures apart."""
import html as htmllib
import os
import re
import urllib.parse

from modules.telegram import fmt

MAX_CHARS = 32768
MAX_MEDIA = 50
MAX_COLUMNS = 20   # a table's columns
MAX_BLOCKS = 500   # list items and table rows count one each, nested blocks too
PHOTO_MAX = 10 * 1024 * 1024   # bigger: Telegram does not take it as a photo, it goes as a document

PHOTOS = (".png", ".jpg", ".jpeg", ".webp")
VIDEOS = (".mp4", ".mov", ".m4v", ".webm")
ANIMATIONS = (".gif",)
AUDIOS = (".mp3", ".m4a", ".ogg", ".wav")

REPORTS = (".md", ".markdown", ".html", ".htm")

# ![alt](path "caption"): the path may be in <…> or have balanced parentheses (map(1).png)
MD_MEDIA = re.compile(r'!\[([^\]]*)\]\(\s*(<[^>]+>|(?:[^()\s]|\([^()\s]*\))+)(\s+"[^"]*")?\s*\)')
# a fenced code block (``` or ~~~): its text is not a report's picture
FENCE = re.compile(r"^(?P<f>`{3,}|~{3,})[^\n]*\n.*?(?:^(?P=f)[`~]*[ \t]*$|\Z)", re.S | re.M)
# <img|video|audio|tg-document … src="path">
HTML_MEDIA = re.compile(r'(<(?:img|video|audio|tg-document)\b[^>]*?(?<![-\w])src\s*=\s*)(["\'])(.*?)\2', re.I | re.S)


class Rich:
    def __init__(self, field, text, files, base):
        self.field, self.text, self.files, self.base = field, text, files, base   # files: [(id, path, kind)]

    def media(self):
        """InputRichMessageMedia: kind is the InputMedia type, the file is part of the same request (attach://id)."""
        return [{"id": mid, "media": {"type": kind, "media": f"attach://{mid}"}} for mid, _, kind in self.files]

    def message(self):
        return {self.field: self.text, **({"media": self.media()} if self.files else {})}

    def too_big(self):
        """What will not fit one message, in words for the log; None: it fits."""
        if len(self.text) > MAX_CHARS:
            return f"{len(self.text)} characters, more than {MAX_CHARS}"   # i18n-ok: operator log
        if len(self.files) > MAX_MEDIA:
            return f"{len(self.files)} files, more than {MAX_MEDIA}"   # i18n-ok: operator log
        return None


def is_report(path):
    """Only these are read as a report; any other file goes as a file."""
    return path.lower().endswith(REPORTS)


def local(ref, base):
    """A path on disk from a report's link (relative: from the report's folder; file://, ~); a web address, tg:, data: give None."""
    ref = ref.strip()
    if ref.startswith("<") and ref.endswith(">"):
        ref = ref[1:-1]
    low = ref.lower()
    if low.startswith("file://"):
        ref = urllib.parse.unquote(urllib.parse.urlsplit(ref).path)
        if re.match(r"^/[A-Za-z]:/", ref):   # file:///C:/… a Windows path
            ref = ref[1:]
    elif re.match(r"^[a-z][a-z0-9+.-]+:", low) and not re.match(r"^[a-z]:[\\/]", low):
        return None   # http(s):, tg:, data:, mailto: are not a file
    ref = os.path.expanduser(ref)
    return os.path.normpath(ref if os.path.isabs(ref) else os.path.join(base, urllib.parse.unquote(ref)))


def kind_of(path):
    low = path.lower()
    if low.endswith(PHOTOS) and os.path.getsize(path) <= PHOTO_MAX:
        return "photo", "photo"
    if low.endswith(VIDEOS):
        return "video", "video"
    if low.endswith(ANIMATIONS):
        return "video", "animation"
    if low.endswith(AUDIOS):
        return "audio", "audio"
    return "document", "document"


def build(path, title=None, base=None, media=None, sender=None):
    """A report file → Rich. .html/.htm is the html field (only <body>, no <style>/<script>), the rest is markdown.
    base: the folder pictures are named from (the report's own by default; the source's when `path` is a copy);
    media: {source picture: its kept copy}; sender: whose report it is (as a file's caption has it)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    base = base or os.path.dirname(os.path.abspath(path))
    media = media or {}
    files, ids = [], {}

    def attach(ref):
        """A tg://…?id= link for a local file (one upload per file), or None: not a file; "": the file is missing."""
        p = local(ref, base)
        if p is None:
            return None
        p = media.get(p, p)
        if not os.path.isfile(p):
            return ""
        if p not in ids:
            link, kind = kind_of(p)
            ids[p] = (f"m{len(files) + 1}", link)
            files.append((ids[p][0], p, kind))
        mid, link = ids[p]
        return f"tg://{link}?id={mid}"

    if path.lower().endswith((".html", ".htm")):
        body = re.search(r"<body\b[^>]*>(.*?)(?:</body>|\Z)", text, re.I | re.S)
        text = body.group(1) if body else re.sub(r"<head\b.*?</head>", "", text, flags=re.I | re.S)
        text = re.sub(r"<(script|style|template)\b.*?</\1\s*>", "", text, flags=re.I | re.S)
        text = re.sub(r"<!--.*?-->|<!doctype[^>]*>|</?html\b[^>]*>", "", text, flags=re.I | re.S).strip()

        def html_sub(m):
            link = attach(htmllib.unescape(m.group(3)))
            if link is None:
                return m.group(0)
            if link == "":
                return f"{m.group(1)}{m.group(2)}{m.group(2)} alt=\"missing file: {htmllib.escape(os.path.basename(m.group(3)))}\""   # i18n-ok: report text
            return f"{m.group(1)}{m.group(2)}{link}{m.group(2)}"
        text = HTML_MEDIA.sub(html_sub, text)
        if title:
            text = f"<h1>{htmllib.escape(f'{sender}: {title}' if sender else title)}</h1>\n{text}"
        elif sender:
            text = f"<p>{htmllib.escape(sender)}</p>\n{text}"
        return Rich("html", text, files, base)

    def md_sub(m):
        link = attach(m.group(2))
        if link is None:
            return m.group(0)
        if link == "":
            return f"_🖼 missing file: {os.path.basename(m.group(2).strip('<>'))}_"   # i18n-ok: report text
        return f"![{m.group(1)}]({link}{m.group(3) or ''})"
    out, at = [], 0
    for m in FENCE.finditer(text):   # the pictures only outside fenced code
        out.append(MD_MEDIA.sub(md_sub, text[at:m.start()]))
        out.append(m.group(0))
        at = m.end()
    text = "".join(out) + MD_MEDIA.sub(md_sub, text[at:])
    if title:
        text = f"# {f'{sender}: {title}' if sender else title}\n\n{text}"
    elif sender:
        text = f"{sender}\n\n{text}"
    return Rich("markdown", text, files, base)


def fallback(r):
    """The fallback (it did not go as one message): the text as plain Markdown without media and HTML blocks, plus the files in order."""
    text = r.text
    if r.field == "html":
        text = re.sub(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", lambda m: f"\n**{m.group(1).strip()}**\n", text, flags=re.I | re.S)
        text = re.sub(r"<(br|/p|/li|/tr|/div|/summary|/figcaption|hr)\b[^>]*>", "\n", text, flags=re.I)
        text = re.sub(r"<li\b[^>]*>", "• ", text, flags=re.I)
        text = re.sub(r"</t[dh]>", " | ", text, flags=re.I)
        text = re.sub(r"<(img|video|audio|tg-document)\b[^>]*>", "", text, flags=re.I)
        text = htmllib.unescape(re.sub(r"<[^>]+>", "", text))
    else:
        text = re.sub(r'!\[[^\]]*\]\(tg://[^)]*\)', "", text)
        text = re.sub(r"</?(details|tg-collage|tg-slideshow|figure|figcaption|aside|u|ins|sub|sup|mark)\b[^>]*>", "",
                      text, flags=re.I)
        text = re.sub(r"<summary\b[^>]*>(.*?)</summary>", r"**\1**", text, flags=re.I | re.S)
        text = re.sub(r"==(.+?)==", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, [p for _, p, _ in r.files]


# A session's words (telegram.rich_results): the bot's Markdown as rich Markdown, looking as the HTML path (fmt) shows it
TOKEN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)|!?\[[^\]\n]*\]\([^)\s]*\)")   # `code` (any run of ticks), [a](b)
A_TAG = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.S)
FENCE_LINE = re.compile(r"\s*(`{3,}|~{3,})")
TABLE_DELIM = re.compile(r"^\s*\|?[\s:|-]+\|")
# a line that starts a block of its own under a paragraph line (a numbered list only from 1): no line break before it
BLOCK_START = re.compile(r"\s*(?:[-*+]\s|1[.)]\s|#{1,6}\s|(?:\*{3,}|_{3,})\s*$)")
SETEXT = re.compile(r" {0,3}(-+|=+)\s*$")   # under a line of text it would make that line a heading
BARE_URL = re.compile(r"(https?://[^\s<>]+)")


def _text(s):
    """Plain text: what only rich Markdown reads as markup ($formula$, ==marked==, <tag>) stays text, as fmt shows it;
    a mark the session escaped itself (\\$ shows as $) and a bare web address (an autolink: no escapes in it) stay."""
    def one(part):
        part = re.sub(r"(?<!\\)([$<])", r"\\\1", part)
        return re.sub(r"(?<!\\)={2,}", lambda m: "\\=" * len(m.group(0)), part)
    return "".join(p if i % 2 else one(p) for i, p in enumerate(BARE_URL.split(s)))


def _url(u):
    return u.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


def _from_html(h):
    """What the HTML link makers gave (escaped text and <a href>) → Markdown: a link around `code` is [`code`](url)."""
    out, at = [], 0
    for m in A_TAG.finditer(h):
        out.append(_text(htmllib.unescape(h[at:m.start()])))
        code = re.fullmatch(r"<code>(.*)</code>", m.group(2), re.S)
        shown = f"`{htmllib.unescape(code.group(1))}`" if code else \
            re.sub(r"([\[\]])", r"\\\1", _text(htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(2)))))
        out.append(f"[{shown}]({_url(htmllib.unescape(m.group(1)))})")
        at = m.end()
    return "".join(out) + _text(htmllib.unescape(h[at:]))


def _inline(line, link):
    out, at = [], 0
    for m in TOKEN.finditer(line):
        out.append(_from_html(link(htmllib.escape(line[at:m.start()], quote=False))))
        if m.group(1) == "`":   # one tick: the HTML path links it (a path, a session, a task key)
            h = link(f"<code>{htmllib.escape(m.group(2), quote=False)}</code>")
            a = A_TAG.fullmatch(h)
            if a and "`" not in m.group(2):   # [`path`](url): it still looks like code
                out.append(f"[{m.group(0)}]({_url(htmllib.unescape(a.group(1)))})")
            else:
                out.append(_from_html(h) if "<a " in h else m.group(0))
        else:
            out.append(m.group(0))   # a ``longer`` code span and a [link](url) of the session's own stay as written
        at = m.end()
    return "".join(out) + _from_html(link(htmllib.escape(line[at:], quote=False)))


def words(md, link=None):
    """A session's Markdown → rich Markdown. link(html) → html: the HTML link makers of the bot (paths into "Files",
    session names, task keys); their <a> become Markdown links. Fenced code stays as it is; a line break stays one (a
    single one joins lines into one paragraph in Markdown) and a table is set apart by blank lines (a line right after
    it would be its row) — as fmt shows them."""
    link = link or (lambda h: h)
    lines, rows, fence, i = (md or "").replace("\r\n", "\n").split("\n"), [], None, 0   # rows: [(kind, line)]
    while i < len(lines):
        line, f = lines[i], FENCE_LINE.match(lines[i])
        if fence:
            rows.append(("code", line))
            if f and f.group(1)[0] == fence[0] and len(f.group(1)) >= len(fence) and not line.strip().strip(fence[0]):
                fence = None
        elif f:
            fence = f.group(1)
            rows.append(("code", line))
        elif line.strip().startswith("|") and i + 1 < len(lines) and TABLE_DELIM.match(lines[i + 1]):
            if rows and rows[-1][0]:
                rows.append(("", ""))
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(("table", _inline(lines[i], link)))
                i += 1
            if i < len(lines) and lines[i].strip():
                rows.append(("", ""))
            continue
        else:
            under = SETEXT.match(line) if rows and rows[-1][0] == "text" else None
            if under and len(under.group(1)) >= 3 and under.group(1)[0] == "-":
                rows.append(("", ""))   # a rule, as fmt's "—"
            elif under:
                line = "\\" + line.lstrip()   # "-" or "=" as typed: not a heading, not an empty list item
            rows.append(("text", _inline(line, link)) if line.strip() else ("", ""))
        i += 1
    out = [line for _, line in rows]
    for k in range(len(rows) - 1):   # a hard line break (two spaces) where the next line would go on in the same paragraph
        if rows[k][0] == "text" == rows[k + 1][0] and not BLOCK_START.match(out[k + 1]) \
                and not re.match(r"\s*#{1,6}\s", out[k]) and not out[k].endswith("  "):
            out[k] += "  "
    return "\n".join(out)


def head_md(head):
    """The bot's HTML heading line (<b>name</b>: …) → Markdown: <b> is bold, the rest is text, every mark escaped."""
    return "".join("**" if p in ("<b>", "</b>") else "" if p.startswith("<") else
                   re.sub(r"([\\`*_\[\]<>$=~|#!])", r"\\\1", htmllib.unescape(p))
                   for p in re.split(r"(<[^>]+>)", head or "") if p)


def result(md, head="", link=None, more=None):
    """A session's words (the bot's send()) as one rich message → (Rich, None), or (None, why): it does not fit the
    Bot API limits (table columns, blocks, characters) — the ordinary HTML path. head — the bot's HTML heading line;
    more — the label of the fold (telegram.fold_long): a long text keeps its gist, the rest goes under <details>."""
    size = len((md or "").replace("\r\n", "\n"))
    if size > MAX_CHARS:   # the conversion only adds: no need to build it
        return None, f"{size} characters, more than {MAX_CHARS}"   # i18n-ok: operator log
    if max(fmt.tables(md), default=0) > MAX_COLUMNS:
        return None, f"a table wider than {MAX_COLUMNS} columns"   # i18n-ok: operator log
    split = fmt.gist_split(md) if more else None
    if split and FENCE_LINE.match(split[0]):   # a ~~~ fence (fmt knows ``` only) is not a gist
        split = None
    if split:   # the gist stays visible; blank lines around: Markdown is read inside <details>
        text = (f"{words(split[0], link)}\n\n<details><summary>{htmllib.escape(more)}</summary>\n\n"
                f"{words(split[1], link)}\n\n</details>")
    else:
        text = words(md, link)
    if head:
        text = f"{head_md(head)}\n\n{text}"
    blocks = sum(1 for ln in text.split("\n") if ln.strip())
    if blocks > MAX_BLOCKS:
        return None, f"about {blocks} blocks, more than {MAX_BLOCKS}"   # i18n-ok: operator log
    r = Rich("markdown", text, [], None)
    why = r.too_big()
    return (None, why) if why else (r, None)


def plain(node):
    """The text of a received rich message (Message.rich_message) or of a quote from one (TextQuote.rich_texts): its
    blocks one per line, a table's cells by " | "."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(plain(n) for n in node)
    if not isinstance(node, dict):
        return ""
    parts = [plain(node[k]) for k in ("summary", "text") if k in node]
    parts += ["\n".join(filter(None, (plain(b) for b in node.get(k) or []))) for k in ("blocks", "items")]
    parts.append("\n".join(" | ".join(plain(c) for c in row) for row in node.get("cells") or []))
    parts += [plain(node[k]) for k in ("caption", "credit") if k in node]
    return "\n".join(p for p in parts if p)
