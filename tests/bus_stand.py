"""A stand hub for the page's e2e of the hub bus (tests/test_hubbus_l3.py): one process — a core (core.daemon.Hub over a
fake host: sessions are only names), its page server and its relay transport (modules/bus_relay) — so two hubs in one
test never share os.environ (the bus reads its folders from there). It prints the page's port, then runs the core's
loop until its stdin closes: the mailbox, the bus's tick and view, the transport's hand-over and take.

    python3 tests/bus_stand.py <root> <label> [--unnamed]

The hub has its own name (the label) unless `--unnamed`: a link and a join need it."""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(root, label, named=True):
    state, config = os.path.join(root, "state"), os.path.join(root, "config")
    os.makedirs(state, exist_ok=True)
    os.makedirs(config, exist_ok=True)
    sys.path.insert(0, ROOT)
    import tests  # noqa: F401 — the machine's settings stay out (it sets its own folders: this hub's go after it)
    os.environ.update(HUB_STATE_DIR=state, HUB_CONFIG_DIR=config, HUB_CONTACTS=os.path.join(config, "contacts.json"),
                      HUB_CONFIG=os.path.join(config, "hub.json"))   # the language: the tests' (tests/__init__.py)
    os.environ.pop("HUB_NO_SYSTEM_AGENTS", None)
    from tests.test_hubbus import LIB  # noqa: F401 — the library of the tests on sys.path
    from tests.test_hubbus_s1 import FakeHost
    from core import busd
    from core.daemon import Hub
    from modules.bus_relay.run import Transport
    from modules.web.server import Web
    if not os.path.exists(os.path.join(config, "hub.json")):   # a stand of snapshots brings its own (theme plugins)
        with open(os.path.join(config, "hub.json"), "w") as f:
            f.write('{"title": "%s"}' % label.replace('"', ""))
    hub = Hub(state, FakeHost())
    if named:
        hub.record("bus.name_requested", None, {"name": label, "channel": "cli", "owner": True})
    lock = threading.Lock()   # the core is one: the page's requests come through the mailbox, the transport's here

    def put(kind, data):
        with lock:
            hub.record(kind, None, data, src="bus-relay")
    t = Transport(state, config, put=put, log=lambda text: None)
    t.stop.wait = lambda s: None
    web = Web(state)
    web.bus.config_dir = config
    web.bus.idle = lambda: time.sleep(0.2)
    server = web.serve(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=web.tail.run, args=(web.stop,), daemon=True).start()
    print(server.server_address[1], flush=True)
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
    while not stop.is_set():
        try:
            with lock:
                hub.ingest()
                hub.__dict__.pop("bus_link_tried", None)
                busd.tick(hub)
                busd.write_view(hub)
            v = t.view()
            if v is not None:
                t.job_tries.clear()
                t.hand(v)
                t.take(v, wait=0)
        except Exception as e:  # noqa: BLE001 — the stand keeps going; the test sees the result on the page
            print(f"stand: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        stop.wait(0.5)   # the relay limits how often a hub asks
    web.stop.set()
    server.shutdown()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], "--unnamed" not in sys.argv[3:])
