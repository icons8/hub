"""Opus's safety filter stopped an answer and Claude Code went on with another model: the core brings the session back.

Claude Code (2.1.30x) retries a refused answer once on the same model; refused again, it answers on the fallback model
(Opus 5.5 -> Opus 4.8) and says so on its output: `system/model_refusal_fallback` with `original_model`,
`fallback_model` and `refused_user_message_uuid` - the prompt of the turn the filter stopped. It stays on the fallback
model to the end of the process, and a plain `--resume` keeps it there: on resume Claude Code restores the model of the
last answer and latches the fallback while that notice is in the conversation (checked against a fake API, 17.11). The
owner never saw it: one hub session ran on Opus 4.8 for a month. A refused turn sent again is also what the filter counts:
a session that keeps hitting it puts the account at risk.

So the host reports the fact (`session.model_refused`: asked, got, `at` - the entry just before that turn's prompt), and
the core, once the session is free (its background done, no card open), raises it again with the same conversation cut
at `at` (`claude --resume <id> --resume-session-at <at>`): the stopped turn and what Opus 4.8 did after it do not go back
into the context, and the session is on the asked model again. What that turn changed on disk stays; the session gets a
note "for the record" with its next message. One automatic return per trigger: a session that trips the filter again
within WINDOW of its return is only shown to the owner - its next raise (the owner's reload, a wake, a hub restart) still
gets the cut. Codex sessions and those not on a fallback model are not touched.

A pure function of the state and the event time: a replay of the journal decides the same."""

import re

from core import forms
from core.coretext import T

WINDOW = 24 * 3600   # s: a repeat within it after a return is not returned again; the account's count of the day
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\[\]-]{0,79}$")   # a model id as Claude Code names it (claude-opus-5-5[1m])
UUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")                       # a transcript entry (--resume-session-at)
LIVE = ("starting", "idle", "busy", "dialog")
CATEGORY = 40   # the filter's category ("cyber"): a word, cut

NOTE = T("core.msg.refused_cut",
         "Opus's safety filter stopped one of your answers in the turn on {turn}, and Claude Code finished that turn on "
         "{got}. The hub removed that turn from your conversation and brought you back on {asked}. What the turn changed "
         "on disk stays, and its result was delivered as usual. What that turn was asked: {read}. If its work is not "
         "finished, read that and the files and go on yourself, without repeating the step the filter stopped: there is "
         "nothing to ask anyone.")
NOTE_KEPT = T("core.msg.refused_kept",
              "Opus's safety filter stopped one of your answers in the turn on {turn}, and Claude Code finished that turn "
              "on {got}. The hub restarted you on {asked} with the same conversation. If that turn's work is not finished, "
              "go on without repeating the step the filter stopped.")
THIS_TURN = T("core.msg.refused_this_turn", "your previous turn")


def _model(v):
    return v if isinstance(v, str) and MODEL.match(v) else None


def bare(m):
    """A full model id without the [1m] context mark and a date tail ("" for an alias or no model)."""
    return re.sub(r"-\d{8}$", "", re.sub(r"\[1m\]$", "", m.lower())) if isinstance(m, str) and m.startswith("claude-") \
        else ""


def other_version(asked, got):
    """Another version of the same family (Opus 5.5 -> Opus 4.8): both full ids (an alias is not compared), the [1m]
    context mark and a date tail aside. Another family is someone's choice (`/model sonnet`), not the filter; Claude
    Code's synthetic answers are no model. The host reads the answers by it, the core the model after a return."""
    a, g = bare(asked), bare(got)
    fa, fg = (re.match(r"^claude-([a-z]+)-\d", m) for m in (a, g))
    return bool(fa and fg and fa.group(1) == fg.group(1) and a != g)


def fact(st, ev, data, engine):
    """session.model_refused (the host: by the answer's model, then by Claude Code's notice with the cut): what was
    asked, what answered, where to cut. Kept on the session until a raise applies it; a free live session is raised when
    free (dirty), a repeat within WINDOW of the last return is only shown. The notice after the answer of the same turn
    adds its cut to the same trigger. engine — the session's engine (a Codex one is not touched)."""
    sess = st["sessions"].get(ev.get("entity"))
    asked, got = _model(data.get("asked")), _model(data.get("got"))
    if not sess or sess["state"] == "removed" or engine == "codex" or not asked or not got or asked == got:
        return
    now = ev["ts"]
    at = data.get("at") if isinstance(data.get("at"), str) and UUID.match(data["at"]) else None
    cat = data.get("category") if isinstance(data.get("category"), str) else None
    was = sess.get("refused") or {}
    if was.get("via") == "answer" and data.get("via") == "notice" and not was.get("done") \
            and was.get("turn") == sess.get("turn_head"):
        was.update(via="notice", at=at)   # the same trigger: Claude Code's notice came after the answer that told it
        if cat:
            was["category"] = cat[:CATEGORY]
        return
    st["refusals"] = [r for r in st.get("refusals") or [] if now - r["ts"] < WINDOW] + [
        {"ts": now, "session": sess["name"], "account": sess.get("account")}]
    done = was.get("done")
    again = now - done < WINDOW if isinstance(done, (int, float)) else bool(was.get("again"))
    task = (st["messages"].get(sess.get("turn_head") or "") or {}).get("for_delegation")
    sess["refused"] = {"ts": now, "asked": asked, "got": got, "at": at, "turn": sess.get("turn_head"),
                       **({"task": task} if isinstance(task, str) else {}),
                       **({"via": data["via"]} if data.get("via") in ("answer", "notice") else {}),
                       **({"category": cat[:CATEGORY]} if cat else {}), **({"again": True} if again else {})}
    if not again and sess["state"] in LIVE:
        sess["dirty"] = True   # release + --resume in a free minute (reducer._pump): its background is not cut


def turn(sess, data):
    """turn.started: a turn on a message after the stopped one, before any raise (a repeat the core does not raise, a
    word to it while it waits for its background) - the session works on, so a cut there would take that work too: only
    the model comes back."""
    ref = sess.get("refused")
    if ref and not ref.get("done") and ref.get("at") and data.get("marker") and data["marker"] != ref.get("turn"):
        ref["at"] = None


def reload(sess, data):
    """`hub session reload` by the owner or the hub (the chip's button): a repeat comes back with it. Not the session's
    own: one that keeps tripping the filter would bring itself back in a loop."""
    ref = sess.get("refused")
    if ref and not ref.get("done") and ref.get("again") and data.get("sender") != sess["name"]:
        ref["go"] = True


def info(sess, data, ts):
    """The first session.info after a return: the model the new process runs on. Still another version of the asked
    family (Claude Code kept the fallback) - not done: a repeat, shown, the next return only by someone's hand and with
    no cut (the conversation is cut already)."""
    ref = sess.get("refused")
    if not ref or not ref.get("done") or "model" in ref or not isinstance(data.get("model"), str):
        return
    ref["model"] = data["model"][:80]
    if other_version(ref["asked"], data["model"]):
        sess["refused"] = {k: v for k, v in ref.items() if k not in ("done", "cut", "model", "go", "died")} | {
            "again": True, "at": None, "ts": ts}


def exited(sess):
    """A process left while the raise that carries the cut had not started. Twice (a Claude Code that does not know the
    flag fails every launch) - the next raise brings only the model back, so no launch loop on it. Once may be the old
    process the reload killed: `sent` stays, the new one's start is the return."""
    ref = sess.get("refused")
    if ref and not ref.get("done") and ref.get("sent"):
        ref["died"] = ref.get("died", 0) + 1
        if ref["died"] >= 2:
            ref["at"] = None


def spawned(sess, data):
    """host.done of the raise that carried it: whether the host cut the conversation (it cuts only at an entry it
    finds on disk) - the note and the page say what was done."""
    ref = sess.get("refused")
    if ref and not ref.get("done") and ref.get("sent"):
        ref["cut"] = bool(data.get("cut"))


def lost(sess):
    """host.done of a raise that found no conversation on disk and started a new one (`conversation: lost`): the
    stopped turn is gone with the rest and the fallback is not latched in a new conversation - nothing to cut, nothing
    to bring back, and no note that the same conversation went on. A mark already returned (`done`) stays: the window of
    a repeat holds, the filter again within it is shown and not returned by itself."""
    ref = sess.get("refused")
    if ref and not ref.get("done"):
        sess.pop("refused")


def spawn(sess, action):
    """The raise that applies the cut: `resume_at` (the host checks the entry is in the conversation) and `back_model` -
    the asked model, the host passes it when it cannot cut and the session has no model of its own (a plain --resume
    would stay on the fallback). Marked sent: session.started of this raise is the return."""
    ref = sess.get("refused")
    if not ref or ref.get("done"):
        return
    if not action.get("resume"):   # a fresh conversation (none on disk yet): nothing to cut, nothing latched
        sess.pop("refused")
        return
    if ref.get("again") and not ref.get("go"):
        return   # a repeat waits for someone's hand (reload): a crash or a wake raises it as it is
    if ref.get("at"):
        action["resume_at"] = ref["at"]
    action["back_model"] = ref["asked"]
    ref["sent"] = True


def started(st, sess, data, ts, new_message, mid):
    """session.started of a raise that carried the cut: the return is done, the session gets the note with its next
    message. A new conversation (/clear, another id) drops it: there is nothing to cut in it."""
    ref = sess.get("refused")
    if not ref or ref.get("done"):
        return
    if data.get("source") == "clear" or (data.get("session_id") and data["session_id"] != sess.get("session_id")):
        sess.pop("refused")
        return
    if not ref.pop("sent", None) or data.get("source") not in (None, "startup", "resume"):
        return
    ref["done"] = ts
    turn = ref.get("turn") or THIS_TURN
    # where the words of the removed turn are: its task, or the message itself, or the session's recent lines
    read = (f"`hub task show {ref['task']} --full`" if ref.get("task") else f"`hub show {ref['turn']}`"
            if ref.get("turn") else f"`hub show {sess['name']} -n 40`")
    text = (NOTE if ref.get("cut") is True else NOTE_KEPT).format(turn=turn, asked=ref["asked"], got=ref["got"],
                                                                   read=read)
    new_message(st, mid, sess["name"], text, ts, origin=forms.ORIGIN_CORE, fyi=True)


def view(st, sess, now):
    """What the page shows of it: {state, asked, got, ts, day, cut, again?} or None. state: waits — on the fallback model, comes back
    when free; again — tripped again soon after a return, stays until a raise (the owner's hand); back — returned, within
    WINDOW. day — how many times the filter moved this account's sessions in the last WINDOW; cut — the stopped turn is
    (to be) cut out of the conversation, not only the model brought back."""
    ref = sess.get("refused")
    if not isinstance(ref, dict):
        return None
    if ref.get("done"):
        if now - ref["done"] >= WINDOW:
            return None
        state = "back"
    else:
        state = "again" if ref.get("again") else "waits"
    day = sum(1 for r in st.get("refusals") or [] if now - r["ts"] < WINDOW and r.get("account") == sess.get("account"))
    return {"state": state, "asked": ref["asked"], "got": ref["got"], "ts": ref["ts"], "day": day,
            "cut": ref.get("cut") is True, **({"again": True} if ref.get("again") else {})}
