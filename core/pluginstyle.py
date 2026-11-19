"""The `css` field of a page plugin's record (EXTENDING.md, API 1.37). A file of its own: the page server, the install and
the catalog check use one rule, and a process that is still running the previous release (the update's self-check) has
the old core.catalog in memory — a new name there would not exist for it, a new file is read from the disk."""
import re

CSS_FILE = re.compile(r"[A-Za-z0-9_./-]{1,200}\.css")
CSS_MAX = 8


def css_files(raw):
    """The `css` field of a plugin record (EXTENDING.md, API 1.37): the plugin's own stylesheets, relative to its folder,
    that the page puts into its <head>. Only vetted paths stay (a .css, Latin letters, digits and _ . / -, no .. or empty
    part, no leading /), without repeats, at most CSS_MAX: the rest is dropped, so a bad entry costs the plugin that file,
    not the page or its install. One rule for the page, the install and the catalog check."""
    out = []
    for f in raw if isinstance(raw, list) else []:
        if (isinstance(f, str) and CSS_FILE.fullmatch(f) and all(p not in ("", ".", "..") for p in f.split("/"))
                and f not in out):
            out.append(f)
    return out[:CSS_MAX]
