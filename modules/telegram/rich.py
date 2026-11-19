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

MAX_CHARS = 32768
MAX_MEDIA = 50
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
