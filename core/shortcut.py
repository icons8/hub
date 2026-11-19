"""The menu shortcut of a hub of the one-line install (spec of the one-line install §0 item 9, §8): "Hub by Icons8" in the
Start menu of Windows and in the applications menu of Linux (a .desktop file), made without administrator rights by
deploy/oneline/setup.py and taken away by `hub uninstall`. It runs `hub open` of its own hub — the page, with the hub
turned on first when it is off. A Mac has none; a hub from a git clone gets none. A second hub (an instance) has one
of its own, named for it. The stdlib only: the install and the uninstall both load it."""
import os

import platforms

# the first line of the .desktop file: the file is ours to rewrite and to remove (a file of a person's, with this name,
# is left alone); a .lnk cannot carry it
MARK = "# agents-hub: the shortcut of the one-line install"
ICON = os.path.join("modules", "web", "static", "icons", "icon-192.png")   # inside the code layer
ICO = "agents-hub.ico"   # the Windows icon, in the state folder (a shortcut takes .ico, not .png)


def former(name=""):
    """The name the .lnk had before: a run of the install puts its own in place of it, the uninstall takes it too."""
    return f"Agents Hub ({name})" if name else "Agents Hub"


def label(name=""):
    """The name a person reads in the menu: Hub by Icons8, or Hub by Icons8 (<name>) for a second hub — Linux's Name=
    line (the .desktop file keeps its file name), the name of the Windows .lnk (found again by the uninstall and by a
    run of the install over it)."""
    return f"Hub by Icons8 ({name})" if name else "Hub by Icons8"


def title(name=""):
    """label: the name the installers of earlier releases call (an older release's setup.py runs on this core)."""
    return label(name)


def file_name(os_name, name=""):
    if os_name == "windows":
        return label(name) + ".lnk"
    return f"agents-hub-{name}.desktop" if name else "agents-hub.desktop"


def path(os_name, name="", home=None, environ=None):
    """The shortcut file of the hub called name ("" — the main one) or None (a Mac)."""
    folder = platforms.current.shortcut_dir(home, environ)
    return os.path.join(folder, file_name(os_name, name)) if folder and os_name in ("linux", "windows") else None


def icon_path(state):
    return os.path.join(state, ICO)


def former_path(where, name=""):
    """The .lnk of the name before (former) next to the shortcut file where, or None (not a .lnk)."""
    return os.path.join(os.path.dirname(where), former(name) + ".lnk") if where and where.endswith(".lnk") else None


def candidates(os_name, name="", home=None):
    """Every file this hub's shortcut may lie in: where the environment of this process puts the menu folder, and the
    default one (an install run with XDG_DATA_HOME set, an uninstall without it); a .lnk under its former name too."""
    got = [path(os_name, name, home), path(os_name, name, home, {})]
    got += [former_path(p, name) for p in got]
    return [p for i, p in enumerate(got) if p and p not in got[:i]]


def _read(file):
    try:
        with open(file, "rb") as f:
            return f.read(65536)
    except OSError:
        return None


def names(file, hub_cmd=None):
    """The .lnk names a hub.cmd — the one given, or any — among its strings (UTF-16, the way the shell stores them;
    letters of any case)."""
    data = _read(file)
    what = (hub_cmd or os.path.join("bin", "hub.cmd")).encode("utf-16-le")
    return data is not None and what.lower() in data.lower()   # bytes.lower: the ASCII letters only, on both sides


def ours(file, hub_cmd=None):
    """The file is this install's: a .desktop file starts with MARK; a .lnk (it cannot carry one) names a hub.cmd
    (names()), or is too short to be a .lnk at all — what a PowerShell that stopped halfway left is nobody else's."""
    data = _read(file)
    if data is None:
        return False
    if file.endswith(".lnk"):
        return len(data) < 76 or names(file, hub_cmd)
    return data.lstrip(b"\xef\xbb\xbf").split(b"\n", 1)[0].rstrip(b"\r") == MARK.encode("utf-8")


def _quote(arg):
    """One argument of an Exec line (Desktop Entry spec, "The Exec key"): in double quotes when it has anything the
    line reads specially, with the four characters escaped, and the escaping of a string value on top."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`%'):
        return arg
    arg = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + arg.replace("%", "%%").replace("\\", "\\\\") + '"'


def desktop(name, argv, comment, icon=None):
    """The .desktop file: argv — the command it runs (a program and its arguments, absolute paths)."""
    lines = [MARK, "[Desktop Entry]", "Type=Application", f"Name={label(name)}", f"Comment={comment}",
             "Exec=" + " ".join(_quote(a) for a in argv)]
    if icon:
        lines.append(f"Icon={icon}")
    lines += ["Terminal=false", "Categories=Development;", "StartupNotify=false"]
    return "\n".join(lines) + "\n"


def ico(png):
    """An .ico of one PNG image (Windows Vista and later take PNG inside), or None when it is not a PNG up to 256 px."""
    if png[:8] != b"\x89PNG\r\n\x1a\n" or len(png) < 24:
        return None
    w, h = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    if not (0 < w <= 256 and 0 < h <= 256):
        return None
    return (b"\x00\x00\x01\x00\x01\x00" + bytes([w % 256, h % 256, 0, 0]) + (1).to_bytes(2, "little")
            + (32).to_bytes(2, "little") + len(png).to_bytes(4, "little") + (22).to_bytes(4, "little") + png)


def lnk_script():
    """The PowerShell that makes the .lnk: every value comes in an environment variable of its own (no quoting of a
    path with a quote or a non-Latin letter in it). A minimized window: cmd runs the hub's own hub.cmd and goes."""
    return ("$ErrorActionPreference = 'Stop'\n"
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:HUB_LNK)\n"
            "$s.TargetPath = $env:HUB_LNK_TARGET\n"
            "$s.Arguments = $env:HUB_LNK_ARGS\n"
            "$s.WorkingDirectory = $env:HUB_LNK_DIR\n"
            "$s.WindowStyle = 7\n"
            "$s.Description = $env:HUB_LNK_TEXT\n"
            "if ($env:HUB_LNK_ICON) { $s.IconLocation = $env:HUB_LNK_ICON }\n"
            "$s.Save()\n")
