"""Paths of one machine moved to another (`hub import`, core/hubexport.py; the fold of core.imported): a table of
[old prefix, new prefix] pairs, the longest first. Paths are compared as paths, not as text: split by the rules of the
machine they came from (`/Users/x` and `C:\\Users\\x`), joined by the rules of the new one; `/Users/xy` is not under
`/Users/x`. A path outside every pair stays as it is. No imports of the hub: the reducer and the importer use it."""
import ntpath
import posixpath


def flavor(windows):
    return ntpath if windows else posixpath


def _parts(path, windows):
    """(root, [parts]) of an absolute path of that machine, or None (relative, not a string)."""
    if not isinstance(path, str) or not path:
        return None
    mod = flavor(windows)
    if not mod.isabs(path) or (windows and not ntpath.splitdrive(path)[0]):
        return None
    drive, rest = ntpath.splitdrive(path) if windows else ("", path)
    rest = rest.replace("\\", "/") if windows else rest
    parts = [p for p in rest.split("/") if p and p != "."]
    if ".." in parts:
        return None
    return (drive.upper() if windows else ""), parts


class Table:
    """The pairs of one move: old paths are of the machine `src_windows`, new ones of `dst_windows`."""

    def __init__(self, pairs, src_windows=False, dst_windows=False):
        self.src, self.dst = bool(src_windows), bool(dst_windows)
        rows = []
        for old, new in pairs or ():
            o, n = _parts(old, self.src), _parts(new, self.dst)
            if o is None or n is None or not o[1]:   # the root itself is never moved
                continue
            rows.append((o, new, n))
        # the longest old prefix first: the state folder inside the home folder goes by its own pair
        self.rows = sorted(rows, key=lambda r: len(r[0][1]), reverse=True)

    def __bool__(self):
        return bool(self.rows)

    def _same(self, a, b):
        return a.lower() == b.lower() if self.src else a == b

    def move(self, path):
        """The path on the new machine, or None when it is under no pair (or is not an absolute path)."""
        got = _parts(path, self.src)
        if got is None:
            return None
        root, parts = got
        for (oroot, oparts), new, (nroot, nparts) in self.rows:
            if (root.lower() if self.src else root) != (oroot.lower() if self.src else oroot) or len(parts) < len(oparts):
                continue
            if all(self._same(a, b) for a, b in zip(parts, oparts)):
                tail = parts[len(oparts):]
                if not tail:
                    return new
                return flavor(self.dst).join(new, *tail)
        return None

    def keep(self, path):
        """move(path), or the path as it was."""
        moved = self.move(path)
        return path if moved is None else moved

    def words(self, command):
        """A shell command with the words that are whole paths moved (quoted or not); the rest of the text as it was.
        -> (command, changed)."""
        if not isinstance(command, str) or not command:
            return command, False
        out, changed, i, n = [], False, 0, len(command)
        while i < n:
            if command[i].isspace():
                out.append(command[i])
                i += 1
                continue
            j, quote, close = i, command[i] if command[i] in "'\"" else "", ""
            if quote:
                k = command.find(quote, i + 1)
                j, close = (n, "") if k < 0 else (k + 1, quote)
                inner = command[i + 1:(k if k >= 0 else n)]
            else:
                while j < n and not command[j].isspace():
                    j += 1
                inner = command[i:j]
            moved = self.move(inner)
            if moved is not None and moved != inner:
                if not quote and any(c.isspace() or c in "&|;<>()$`\"'" for c in moved):
                    quote = close = '"'   # the new home has a space: one word still (sh, cmd and PowerShell alike)
                out.append(quote + moved + close)
                changed = True
            else:
                out.append(command[i:j])
            i = j
        return "".join(out), changed

    def tree(self, value, pathlists=False):
        """A JSON value with every string that is a whole path moved; pathlists — a string of paths joined by the
        old machine's separator (PATH) is moved element by element. -> (value, count)."""
        count = 0

        def walk(v):
            nonlocal count
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, str):
                moved = self.move(v)
                if moved is not None:
                    count += moved != v
                    return moved
                sep = ";" if self.src else ":"
                if pathlists and sep in v:
                    items = v.split(sep)
                    got = [self.move(x) for x in items]
                    if any(g is not None for g in got) and all(g is not None or _parts(x, self.src) is not None
                                                                for g, x in zip(got, items)):
                        count += 1
                        return (";" if self.dst else ":").join(g if g is not None else x for g, x in zip(got, items))
            return v
        return walk(value), count


# the fields of the core's state that hold a path of the machine (inventory of a real state, spec d2789272 §7); the
# texts of messages are history and stay as they were
SESSION_PATHS = (("cwd",), ("info", "cwd"), ("worktree", "repo"), ("worktree", "path"), ("remote", "top"))


def _get(d, keys):
    for k in keys[:-1]:
        d = d.get(k) if isinstance(d, dict) else None
    return d if isinstance(d, dict) else None


def move_state(st, table):
    """The core state's paths through the table, in place -> how many values changed."""
    if not table:
        return 0
    n = 0

    def put(holder, key):
        nonlocal n
        if isinstance(holder, dict) and isinstance(holder.get(key), str):
            moved = table.move(holder[key])
            if moved is not None and moved != holder[key]:
                holder[key] = moved
                n += 1
    for sess in (st.get("sessions") or {}).values():
        if not isinstance(sess, dict):
            continue
        for keys in SESSION_PATHS:
            put(_get(sess, keys), keys[-1])
        for skill in ((sess.get("kit") or {}).get("skills") or []) if isinstance(sess.get("kit"), dict) else []:
            put(skill, "path")
    for job in (st.get("schedule") or {}).values():
        if not isinstance(job, dict):
            continue
        for key in ("command", "check", "cwd"):
            if isinstance(job.get(key), str):
                moved, changed = table.words(job[key]) if key != "cwd" else (table.keep(job[key]), True)
                if changed and moved != job[key]:
                    job[key] = moved
                    n += 1
    for d in (st.get("delegations") or {}).values():
        for a in (d.get("attachments") or []) if isinstance(d, dict) else []:
            put(a, "path")
    for m in (st.get("messages") or {}).values():
        if isinstance(m, dict):
            put(m.get("forwarded"), "path")
    return n
