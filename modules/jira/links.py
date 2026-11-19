"""Jira module: issue keys in session replies become links to the issues (owner request 2026-09-30). Settings are
`jira` in hub.json: {"url": "https://<site>.atlassian.net", "projects": ["ABC", …]}.
Only keys from the list: UTF-8, SHA-256, GPT-4 look like issue keys. No settings: the module is off.

Telegram: link_html here; the hub page: the same rule in modules/web/static/js/markdown.js (linkJira), its settings
arrive in the snapshot (`jira`).
"""
import html
import re


def settings(cfg):
    j = (cfg or {}).get("jira") or {}
    url, projects = str(j.get("url") or "").rstrip("/"), [str(p) for p in j.get("projects") or [] if p]
    return {"url": url, "projects": projects} if url and projects else None


def _patterns(s):
    keys = "|".join(re.escape(p) for p in sorted(s["projects"], key=len, reverse=True))
    key = rf"(?<![\w/-])((?:{keys})-\d+)(?![\w-])"
    url = rf"{re.escape(s['url'])}/browse/((?:{keys})-\d+)(?:[?#][^\s<\"']*)?"
    return re.compile(url), re.compile(key), re.compile(rf"^((?:{keys})-\d+)$")


# existing links, pre and code are left alone: the key in them is part of someone else's text
PROTECTED = re.compile(r"(<a\b.*?</a>|<pre>.*?</pre>|<code>.*?</code>|<[^>]+>)", re.S)


def link_html(text, cfg):
    """Telegram HTML -> the same HTML with issue keys turned into links; a bare issue URL is shortened to its key."""
    s = settings(cfg)
    if not s:
        return text
    url_re, key_re, whole = _patterns(s)
    browse = lambda k: html.escape(f"{s['url']}/browse/{k}")  # noqa: E731

    def plain(seg):
        seg = url_re.sub(lambda m: f'<a href="{html.escape(html.unescape(m.group(0)))}">{m.group(1)}</a>', seg)
        parts = re.split(r"(<a\b.*?</a>)", seg)  # links just made are not touched a second time
        return "".join(p if p.startswith("<a") else key_re.sub(lambda m: f'<a href="{browse(m.group(1))}">{m.group(1)}</a>', p)
                       for p in parts)

    out = []
    for seg in PROTECTED.split(text):
        if seg.startswith("<code>") and whole.match(html.unescape(seg[6:-7])):
            out.append(f'<a href="{browse(html.unescape(seg[6:-7]))}">{seg}</a>')
        elif seg.startswith("<"):
            out.append(seg)
        else:
            out.append(plain(seg))
    return "".join(out)
