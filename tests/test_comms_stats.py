"""tools/comms_stats.py — a measurement of the owner's communication with the hub from the journal: messages by channel,
voice, bot messages in Telegram per owner message, time to reply, repeated "so what?" nudges, open question cards.
The journal is small, built right here; the numbers were counted by hand."""
import tests  # noqa: F401 — hide the machine's settings
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout

from core import langdata

from tools import comms_stats

T = 1_800_000_000.0   # start of the window


def _said(pattern):
    """A repeated question of the data's FOLLOWUP list written out: optional groups dropped, the first choice of a group
    taken, spaces for the space classes."""
    for regex, by in ((r"\([^()]*\)\?", ""), (r"\(([^|()]*)\|[^()]*\)", r"\1"), (r"\\s[+*]", " "), (r"\\b|\^", "")):
        pattern = re.sub(regex, by, pattern)
    return " ".join(pattern.split())


WHERE, WELL = (_said(p) for p in langdata.get("ru", "comms", "followup")[:2])   # Russian "where is the answer", "well, what"
CHAT = 42


def ev(seq, dt, type, entity=None, src="core", **data):
    return {"seq": seq, "ts": T + dt, "src": src, "type": type, "entity": entity, "data": data}


def owner_tg(seq, dt, text, tg_id, to=None):
    d = {"text": text, "attachments": [], "tg": {"chat": CHAT, "id": tg_id}, "channel": "telegram", "owner": True}
    if to:
        d["to"] = to
    return ev(seq, dt, "message.received", src="telegram", **d)


def owner_web(seq, dt, text, to=None):
    return ev(seq, dt, "message.received", src="web", text=text, channel="web", owner=True, **({"to": to} if to else {}))


def result(seq, dt, who, text):
    return ev(seq, dt, "turn.result", who, src="host", text=text, is_error=False)


def started(seq, dt, who, marker):
    return ev(seq, dt, "turn.started", who, src="hook", marker=marker)


def ask(seq, dt, who, rid, questions=None, tool="AskUserQuestion"):
    inp = {"questions": [{"question": q, "options": []} for q in questions]} if questions else {"command": "ls"}
    return ev(seq, dt, "permission.requested", who, src="host", request_id=rid, tool=tool, input=inp, interactive=True)


JOURNAL = [
    ev(1, -100, "session.requested", "main-hub", kind="hub"),
    owner_web(2, -50, "yesterday's — outside the window"),
    # Telegram: a task for the hub, the hub passes it to an executor, the executor replies after 311 s
    owner_tg(10, 0, "make a report", 100),
    started(11, 2, "main-hub", "m10"),
    result(12, 10, "main-hub", "Passed to worker"),
    ev(13, 11, "delegation.created", src="session", session="worker", **{"for": "m10"}),
    started(14, 12, "worker", "d13"),
    result(15, 311, "worker", "Report is ready"),
    ev(16, 400, "delegation.done", "d13", src="session"),
    # a repeated nudge by voice; between it and the previous one — three bot messages (id 101–103)
    owner_tg(20, 600, f"🎤 voice: {WELL}?", 104),
    started(21, 605, "main-hub", "m20"),
    result(22, 630, "main-hub", "All done, the report is above"),
    # a card with two questions, answered with a button from Telegram
    ask(30, 700, "worker", "r1", ["Where to put it?", "Which format?"]),
    ev(31, 760, "permission.answered", "worker", src="telegram", request_id="r1", behavior="allow",
       answers={"Where to put it?": "here", "Which format?": "csv"}, channel="telegram", owner=True),
    # a message without a reply (five bot messages before it: id 105–109)
    owner_tg(40, 800, "ok", 110),
    # from the page — straight to a session, a repeated nudge; reply after 61 s
    owner_web(50, 900, f"{WHERE}?", to="worker"),
    started(51, 901, "worker", "m50"),
    result(52, 961, "worker", "Answer: all done"),
    # from the page — to the hub; a silent "—" result is not a reply, reply after 100 s
    owner_web(60, 1000, "hello"),
    started(61, 1001, "main-hub", "m60"),
    result(62, 1005, "main-hub", "—"),
    result(63, 1100, "main-hub", "Hello!"),
    # a permission request without a reply and a question withdrawn by the session
    ask(70, 1200, "other", "r2", tool="Bash"),
    ask(71, 1210, "other", "r3", ["Continue?"]),
    ev(72, 1220, "question.withdrawn", "other", src="session", id="q71", sender="other", channel="session", owner=False),
    ev(73, 1300, "owner.notify", src="monitor", title="Monitor", text="the disk is almost full"),
    ev(74, 1310, "message.received", src="session", to="worker", text="from a session", channel="session", owner=False,
       sender="other"),
]


class CommsStats(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.main = os.path.join(self.dir, "journal.jsonl")
        self.archive = os.path.join(self.dir, "journal-1-40.jsonl")   # the archive repeats part of the journal
        with open(self.main, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in JOURNAL)
        with open(self.archive, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in JOURNAL if e["seq"] <= 40)
            f.write("{broken line\n")

    def stats(self):
        return comms_stats.collect([self.archive, self.main], T, T + 86400)

    def test_owner_messages_by_channel_and_voice(self):
        s = self.stats()["owner"]
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["by_channel"]["telegram"], {"messages": 3, "voice": 1, "to_hub": 3, "to_session": 0})
        self.assertEqual(s["by_channel"]["web"], {"messages": 2, "voice": 0, "to_hub": 1, "to_session": 1})

    def test_bot_messages_per_owner_telegram_message(self):
        s = self.stats()["telegram"]
        self.assertEqual(s["owner_messages"], 3)
        self.assertEqual(s["by_ids"], {"bot_messages": 8, "gaps": 2, "per_owner_message": 4.0, "median_gap": 4.0,
                                       "max_gap": 5})
        # hub result ×3 (m10, m20, m60), executor on a delegation ×1, question: 2 + 2 "answer", permission 1,
        # question 1 + 1 "withdrawn", monitor 1 — 12; a session's reply to the page does not go to Telegram
        self.assertEqual(s["by_events"]["sends"], 12)
        self.assertEqual(s["by_events"]["per_owner_message"], 4.0)
        self.assertEqual(s["by_events"]["by_kind"], {"hub_result": 3, "session_result": 1, "card": 4, "card_closed": 3,
                                                     "notify": 1})

    def test_time_to_first_reply(self):
        r = self.stats()["reply"]
        self.assertEqual(r["all"], {"n": 5, "answered": 4, "unanswered": 1, "median_s": 45.5, "p90_s": 100.0})
        self.assertEqual(r["by_channel"]["telegram"], {"n": 3, "answered": 2, "unanswered": 1, "median_s": 20.0,
                                                       "p90_s": 30.0})
        self.assertEqual(r["by_channel"]["web"], {"n": 2, "answered": 2, "unanswered": 0, "median_s": 80.5,
                                                  "p90_s": 100.0})
        self.assertEqual(r["pickup"], {"n": 4, "median_s": 1.5, "p90_s": 5.0})
        # an "ok" without a reply: the owner's next message to the hub (m60) arrived before any hub reply
        self.assertEqual(r["superseded"], 1)
        self.assertEqual(r["unanswered_seqs"], [40])

    def test_delegated_executor(self):
        d = self.stats()["delegation"]
        self.assertEqual(d["for_owner_messages"], 1)
        self.assertEqual(d["executor_reply"], {"n": 1, "answered": 1, "unanswered": 0, "median_s": 311.0,
                                               "p90_s": 311.0})
        self.assertEqual(d["created_to_done"], {"n": 1, "median_s": 389.0, "p90_s": 389.0})
        self.assertEqual(d["open"], 1 - 1)

    def test_followups(self):
        f = self.stats()["followups"]
        self.assertEqual(f["total"], 2)
        self.assertEqual(f["share"], 0.4)
        self.assertEqual(f["by_channel"], {"telegram": 1, "web": 1})
        self.assertEqual(sorted(x["seq"] for x in f["examples"]), [20, 50])

    def test_question_cards(self):
        c = self.stats()["cards"]
        self.assertEqual(c["requested"], 3)
        self.assertEqual(c["by_tool"], {"AskUserQuestion": 2, "other": 1})
        self.assertEqual(c["answered"], {"telegram": 1})
        self.assertEqual(c["withdrawn"], 1)
        self.assertEqual(c["dismissed"], 0)
        self.assertEqual(c["unanswered"], 1)
        self.assertEqual(c["answer_latency"], {"n": 1, "median_s": 60.0, "p90_s": 60.0})
        self.assertEqual(c["top_sessions"], [["other", 2], ["worker", 1]])
        self.assertEqual(c["top_unanswered"], [["other", 1]])

    def test_cli_json_and_text(self):
        out = io.StringIO()
        with redirect_stdout(out):
            comms_stats.main(["--json", "--since", "@%d" % T, "--until", "@%d" % (T + 86400), self.archive, self.main])
        self.assertEqual(json.loads(out.getvalue())["owner"]["total"], 5)
        out = io.StringIO()
        with redirect_stdout(out):
            comms_stats.main(["--since", "@%d" % T, "--until", "@%d" % (T + 86400), self.main])
        text = out.getvalue()
        self.assertIn("telegram 3", text)
        self.assertIn("Repeated asks: 2 (40%)", text)


if __name__ == "__main__":
    unittest.main()
