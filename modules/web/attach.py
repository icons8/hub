"""Owner attachments on disk: <state>/attachments/<id>/<name>. One naming rule for the page (base64 and raw
stream) and Telegram."""
import os
import shutil
import uuid

RAW_MAX_MB = 200   # attachment size limit, MB; hub.json web.upload_max_mb
NAME_CHARS, NAME_BYTES = 120, 200   # name length; in UTF-8 bytes — file systems allow up to 255
# Windows device names: a file with such a name (with any extension) cannot be created there
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def root(state_dir):
    return os.path.join(state_dir, "attachments")


def clean_name(name):
    """Attachment name: no folders (nor Windows "\\"), only letters, digits and "._- ", not "." or "..", no trailing
    dot or space, not a Windows device name; a long name is truncated, keeping the extension."""
    name = os.path.basename(str(name or "").replace("\\", "/")).strip()
    name = "".join(c if c.isalnum() or c in "._- " else "_" for c in name).rstrip(". ")
    if len(name) > NAME_CHARS or len(name.encode()) > NAME_BYTES:
        stem, ext = os.path.splitext(name)
        ext = ext if len(ext) <= 20 else ""
        while stem and (len(stem + ext) > NAME_CHARS or len((stem + ext).encode()) > NAME_BYTES):
            stem = stem[:-1]
        name = (stem + ext).rstrip(". ")
    if name.split(".")[0].strip().upper() in RESERVED:
        name = "_" + name
    return name if name.strip(". ") else "file"


def new_path(state_dir, name):
    """Path of a new attachment: attachments/<id>/<clean name>, folder created."""
    d = os.path.join(root(state_dir), uuid.uuid4().hex[:12])
    os.makedirs(d)
    return os.path.join(d, clean_name(name))


def max_bytes(cfg):
    """Attachment size limit in bytes: hub.json web.upload_max_mb, RAW_MAX_MB by default."""
    web = cfg.get("web")
    mb = web.get("upload_max_mb") if isinstance(web, dict) else None
    ok = isinstance(mb, (int, float)) and not isinstance(mb, bool) and 0 < mb < 1 << 20   # not inf from 1e999
    return int((mb if ok else RAW_MAX_MB) * (1 << 20))


def inside(state_dir, path):
    """Real path of a file among the attachments, or None."""
    real = os.path.realpath(path or "")
    ok = real.startswith(os.path.realpath(root(state_dir)) + os.sep) and os.path.isfile(real)
    return real if ok else None


def keep(state_dir, path, limit):
    """A file sent to the owner (hub send-file) is copied into the attachments: the session may delete the original,
    while the feed and Telegram keep the copy. Already an attachment — the same path; over the attachment limit or
    not copied — None (the original stays)."""
    if inside(state_dir, path):
        return path
    dst = None
    try:
        if os.path.getsize(path) > limit:
            return None
        dst = new_path(state_dir, os.path.basename(path))
        shutil.copyfile(path, dst)
    except OSError:
        if dst:
            shutil.rmtree(os.path.dirname(dst), ignore_errors=True)
        return None
    return dst
