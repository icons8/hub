"""Run the watcher over an already recorded journal, to check that it behaves correctly on past incidents. The clock is
the time of the events: the watcher sees the journal as it would have seen it live. Writes nothing to the core mailbox:
decisions go to a JSONL file, one line per model question.

  python3 modules/watch/replay.py <journal.jsonl> <out.jsonl> [--from SEQ] [--dry]
  --dry — no model (counts the questions and the core facts); without it — Haiku via claude -p for each question
"""
import json
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from modules.watch import watch  # noqa: E402
from core import langdata  # noqa: E402

# the session in the head line of a question (Watcher), English and the Russian of earlier versions (language data)
HEAD_SESSION = re.compile(rf"^(?:{langdata.legacy('watch_session')}|Session) (.+?)\. "
                          rf"(?:{langdata.legacy('watch_state')}|State): .*")


def replay(events, ask, out, start=0):
    """Feed the events in order; between events the clock moves in steps, so the window and the silence rules fire."""
    asked = []

    def spy(text):
        try:
            got = ask(text)
        except Exception as e:  # noqa: BLE001
            got = {"failed": repr(e)[:200]}
        asked.append((text, got))
        if "failed" in got:
            raise OSError(got["failed"])
        return got

    w = watch.Watcher(tempfile.mkdtemp(prefix="watch-replay-"), {}, ask=spy, needs=None, urgent=None,
                      recap=lambda text: "")
    w.started = True
    prev = None
    for e in events:
        if prev is not None and e["seq"] > start:
            gap = e["ts"] - prev
            ticks = [prev + watch.WINDOW_SECONDS + 1] if gap > watch.WINDOW_SECONDS + 1 else []
            t = prev + watch.SILENT + 1
            while t < e["ts"] and len(ticks) < 8:  # a long silence: no more than eight questions in a row
                ticks.append(t)
                t += watch.SILENT
            for now in ticks:
                _decide(w, now, asked, out)
        w.feed(e, e["ts"])
        if e["seq"] <= start:
            w.fresh.clear()
        else:
            _decide(w, e["ts"], asked, out)
        prev = e["ts"]
    return w


def _decide(w, now, asked, out):
    asked.clear()
    w.decide(now)
    for text, got in asked:
        lines = text.split("\n")
        lines = lines[1:] if lines[0].startswith("Owner's language:") else lines   # the language line (Watcher.lang_line)
        head = lines[0]
        out.write(json.dumps({"ts": now, "session": HEAD_SESSION.sub(r"\1", head), "head": head,
                              "tail": lines[1:][-4:], **got}, ensure_ascii=False) + "\n")
    box = os.path.join(w.state_dir, "spool")  # what the watcher would have put into the core mailbox: alarms to the hub and messages to the owner
    for name in sorted(os.listdir(box)) if os.path.isdir(box) else []:
        with open(os.path.join(box, name), encoding="utf-8") as f:
            item = json.load(f)
        os.unlink(os.path.join(box, name))
        out.write(json.dumps({"alert": item["type"], "ts": now, "session": item.get("entity"), **item["data"]},
                             ensure_ascii=False) + "\n")
    out.flush()


def main(argv):
    src, dst = argv[0], argv[1]
    start = int(argv[argv.index("--from") + 1]) if "--from" in argv else 0
    with open(src, encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]
    ask = (lambda text: {"trouble": False, "kind": "ok"}) if "--dry" in argv else watch.ask
    with open(dst, "w", encoding="utf-8") as out:
        replay(events, ask, out, start)


if __name__ == "__main__":
    main(sys.argv[1:])
