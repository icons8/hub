import os
import shutil
import time


def cleanup(host, state_dir):
    """Kill the test tmux server, let the SessionEnd hooks finish writing, remove the directory and the socket."""
    host.kill_server()
    time.sleep(0.4)
    shutil.rmtree(state_dir, ignore_errors=True)
    sock = os.path.join(os.environ.get("TMUX_TMPDIR", "/tmp"), f"tmux-{os.getuid()}", host.socket)
    if os.path.exists(sock):
        os.unlink(sock)
