"""`hub chats` (spec d2108458, core/chats.py): the liaison passes a person's message on or closes it, the liaison, the
hub or the session it was passed to answers the person in the owner's name — by his rules, as in his other messengers.
Without arguments — the connections and the open messages.

  hub chats                                 the connections and the messages without an outcome
  hub chats pass <cN> <session> [--note "…"]
  hub chats done <cN> [--why "…"]
  hub chats send <cN> "<text>" | -        (-: the text from stdin)
"""
import sys

from core import chats, coretext, i18n


def _opt(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 >= len(args):
            raise SystemExit(i18n.t("cli.chats.usage"))
        return args[i + 1], args[:i] + args[i + 2:]
    return None, args


def _request(put, rejected, type_, data):
    seq = put(type_, None, data, say=False)
    if seq is None:
        print(i18n.t("cli.chats.waiting"))
        return None
    if seq in rejected:
        print(i18n.t("cli.chats.rejected", reason=coretext.human(rejected[seq])))
        return None
    return seq


def status(st):
    c = (st or {}).get("chats") or {}
    conns = c.get("connections") or {}
    if not conns:
        print(i18n.t("cli.chats.none"))
        return 0
    for svc, conn in sorted(conns.items()):
        print(i18n.t("cli.chats.on" if conn.get("on") else "cli.chats.off",
                     service=chats.SERVICE_NAMES.get(svc, svc),
                     reply=i18n.t("cli.chats.reply_yes" if conn.get("reply") else "cli.chats.reply_no")))
    open_ = [(cid, i) for cid, i in (c.get("inbox") or {}).items() if i.get("outcome") is None]
    for cid, item in open_[-20:]:
        text = " ".join(str(item.get("text") or "").split())
        print(f"  {cid}  {(item.get('from') or {}).get('name') or '—'}: {text[:80]}{'…' if len(text) > 80 else ''}")
    return 0


def main(argv, put, current, rejected):
    cmd, rest = (argv[0], list(argv[1:])) if argv else ("", [])
    if not cmd or cmd == "status" and not rest:
        return status(current())
    if cmd == "pass":
        note, rest = _opt(rest, "--note")
        if len(rest) != 2:
            raise SystemExit(i18n.t("cli.chats.usage"))
        if _request(put, rejected, "chat.pass_requested", {"inbox": rest[0], "session": rest[1],
                                                      **({"note": note} if note else {})}) is None:
            return 1
        print(i18n.t("cli.chats.passed", inbox=rest[0], to=rest[1]))
        return 0
    if cmd == "done":
        why, rest = _opt(rest, "--why")
        if len(rest) != 1:
            raise SystemExit(i18n.t("cli.chats.usage"))
        if _request(put, rejected, "chat.done_requested", {"inbox": rest[0], **({"why": why} if why else {})}) is None:
            return 1
        print(i18n.t("cli.chats.done", inbox=rest[0]))
        return 0
    if cmd == "send":
        if len(rest) < 2:
            raise SystemExit(i18n.t("cli.chats.usage"))
        text = sys.stdin.read() if rest[1:] == ["-"] else " ".join(rest[1:])   # -: the text from stdin (backticks)
        seq = _request(put, rejected, "chat.send_requested", {"inbox": rest[0], "text": text})
        if seq is None:
            return 1
        print(i18n.t("cli.chats.going", inbox=rest[0], out=f"o{seq}"))
        return 0
    raise SystemExit(i18n.t("cli.chats.usage"))
