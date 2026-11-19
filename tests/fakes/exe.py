"""A fake Python program that can be launched by path, on any OS."""
import os
import stat
import sys


def script(path, body):
    """Write body as the program at path; return the path to launch. macOS and Linux: a script with a shebang;
    Windows ignores shebangs, so path.py plus a path.cmd wrapper, like npm programs."""
    if os.name != "nt":
        with open(path, "w") as f:
            f.write(f"#!{sys.executable}\n{body}")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path
    with open(path + ".py", "w", encoding="utf-8") as f:
        f.write(body)
    with open(path + ".cmd", "w") as f:
        f.write(f'@"{sys.executable}" "%~dp0{os.path.basename(path)}.py" %*\n')
    return path + ".cmd"
