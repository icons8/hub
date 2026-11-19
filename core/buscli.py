"""`hub bus …` — the commands of the hub bus (spec hubbus): the owner's (slice S0) — switch it on and off, the hub's
card, the contact table, links to people of the book; the liaison's (slice S1) — send, pass, decline, done, note; a session's
request to the liaison (`hub send liaison --to <contact> "…"`). The owner's commands are a command outside sessions; the
core checks them again (strict guards.from_owner, core/busstate.py) and decides itself (core/bus.decide, core/busd.py).

  hub bus [status]
  hub bus name ["<hub name>"]            this hub's name for the other hubs (required before a link or a join);
                                         without an argument — shows it
  hub bus on [--install] [--label "<hub name>"]   (--label sets the same name)
  hub bus off
  hub bus card
  hub bus contact add <card> --as <name> [--code <check code>]
  hub bus contact set <name> [--status known|muted|blocked|pending] [--code <check code>]
  hub bus contact rename <name> "<name for the list>"   (your own name for the hub, only here; "" — as it calls itself)
  hub bus contact remove <name>
  hub bus contact ls                     (the liaison — a short view: no keys, no values of the paths)
  hub bus wire <folder>                  the stand's transport: envelopes through a shared folder (no network)
  hub bus relay [status]                 the relay: its address, the transport's state, our invitations
  hub bus relay join - | --file <path>   register on the relay the invitation (stdin or a file, like `join -`) names: its
                                         address and key come only from there; the core has no relay address of its own
  hub bus relay invite                   a new one-time invitation to our relay, signed by the hub's key (7 days)
  hub bus relay leave                    leave the relay
  hub bus link [--ttl 1h|24h|7d] [--label "<name>"] [--yes]
                                         A: make the bus work (the library, keys, relay) and print one link line;
                                         --label — the hub's name in this link alone
  hub bus join [-] [--yes]               B: a link (or a relay invitation) from stdin — never an argument: what it
                                         will do, a question [y/N], then everything up to «linked»
  hub bus offer <file>|-                 a session was given another hub's invitation: bring it to the owner as a card
                                         (page, Telegram) — «Link» there is the same join; not for pasting it to him
  hub bus chat <contact> [<text>|-]      tell that hub (your words to its liaison, no card), or the last messages
  hub bus contact verify <name> [--code <pair code>]   «Matches»: the pair code is the same on both screens
  hub bus reset [--yes]                  drop the bus identity: keys, contacts, links; another fingerprint
  hub contacts link "<person>" <hub> / hub contacts unlink <hub>
  hub contacts rename "<person>" "<new name>" / hub contacts remove "<person>"   the owner's book (a name, an alias or an id)
the liaison:
  hub bus send <contact> [--agent <hint>] [--thread <T>] (--text-from mN | --text "…")
  hub bus pass bN <session> [--note "…"]
  hub bus decline <bN|mN> [--why "…"]
  hub bus done bN [--why "…"]            read, nothing to do («thank you», «got it»): closed, nothing goes out
  hub bus note <contact> "…" [--on bN]
  hub bus rule [ls [<contact>]] | add <contact|*> "<rule>" | rm <rN>   rules about hubs (the liaison's memory)
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

import platforms
from core import bus, busd, busflow, busrelay, busstate, catalog, coretext, i18n, journal, langdata, liaison

WAIT = 20   # seconds for the core's decision (keys are made, the card is checked)


def _opt(args, flag):
    """(value, the rest) of `--flag value`."""
    if flag not in args:
        return None, args
    i = args.index(flag)
    if i + 1 >= len(args):
        raise SystemExit(i18n.t("cli.bus.flag_value", flag=flag))
    return args[i + 1], args[:i] + args[i + 2:]


def _texts():
    """The words for the core's refusal codes (literal keys: the i18n checks find them by text)."""
    t = i18n.t
    return {"code_mismatch": t("cli.bus.code.code_mismatch"), "code_needed": t("cli.bus.code.code_needed"),
            "known_hub": t("cli.bus.code.known_hub"), "name_taken": t("cli.bus.code.name_taken"),
            "own_card": t("cli.bus.code.own_card"), "bad_signature": t("cli.bus.code.bad_signature"),
            "stale": t("cli.bus.code.stale"), "bad_format": t("cli.bus.code.bad_format"),
            "no_contact": t("cli.bus.code.no_contact"), "no_lib": t("cli.bus.code.no_lib"),
            "old_lib": t("cli.bus.code.no_lib"), "no_hpke": t("cli.bus.code.no_lib"), "no_ssh": t("cli.bus.code.no_ssh"),
            "selfcheck": t("cli.bus.code.selfcheck"), "key_changed": t("cli.bus.code.key_changed"),
            "keys_broken": t("cli.bus.code.keys_broken"), "keys_exist": t("cli.bus.code.keys_broken"), "keygen": t("cli.bus.code.keygen"),
            "error": t("cli.bus.code.error")}


DETAILED = ("error", "keygen", "selfcheck", "keys_broken", "key_changed", "no_lib", "old_lib", "no_hpke", "no_ssh",
            "known_hub", "no_contact")


def _why(code, reason=""):
    text = _texts().get(code, code)
    return f"{text} ({reason})" if reason and code in DETAILED else text


def _confirm(question):
    """A yes from the terminal itself (stdin may carry the link): None without a terminal. Windows has two console
    devices, one to read and one to write."""
    names = platforms.current.TERMINAL
    try:
        with open(names[0], encoding="utf-8") as tin, open(names[1], "w", encoding="utf-8") as tout:
            tout.write(f"{question} [y/N] ")
            tout.flush()
            answer = tin.readline().strip().casefold()
    except (OSError, ValueError):
        return None
    return answer in ("y", "yes", *langdata.words("words", "yes"))   # and "yes" of every language with data


class Cli:
    def __init__(self, put, current, state_dir, me, outsider, rejected, note=None, idle=None):
        self.put, self.current, self.state_dir, self.me, self.outsider = put, current, state_dir, me, outsider
        self.rejected = rejected   # {seq: reason} of requests the core refused (bin/hub put)
        self.quiet = note          # a progress fact nobody waits for (bin/hub: put quiet)
        self.confirm = _confirm
        self.stdin = sys.stdin
        self.idle = idle           # between two looks at the core's view (a stand runs its cores there)

    def owner(self, what, hint=None):
        if self.me or self.outsider:
            more = i18n.t(hint) if hint and self.me and not self.outsider and not self.is_liaison() else ""
            raise SystemExit(i18n.t("cli.bus.owner_only", what=what) + ("\n" + more if more else ""))

    def is_liaison(self):
        return self.me == liaison.NAME and not self.outsider

    def liaison_only(self, what):
        if not self.is_liaison():
            raise SystemExit(i18n.t("cli.bus.not_liaison", what=what))

    def bus(self):
        return self.current().get("bus") or {}

    def ask(self, type_, data):
        """The owner's request and the core's decision on it: (code, reason) — "ok", a refusal code, or None (no answer;
        core/busflow.ask)."""
        code, reason, _ = busflow.ask(self.put, self.state_dir, self.rejected, type_, data, WAIT)
        if code is None:
            print(i18n.t("cli.bus.waiting"))
        return code, reason

    def refused(self, code, reason):
        if code == "rejected":   # the core's phrases of the reason in the reader's language (core/coretext.py)
            print(i18n.t("cli.bus.rejected", reason=coretext.human(reason)))
        elif code is not None:
            print(i18n.t("cli.bus.refused", reason=_why(code, reason)))
        return 1

    def status(self):
        b = self.bus()
        if not b.get("enabled"):
            print(i18n.t("cli.bus.status_off"))
            return 0
        me = b.get("me") or {}
        print(i18n.t("cli.bus.status_on", label=me.get("label") or "-", fp=me.get("fp"), check=bus.code(me["fp"]),
                     count=len(b.get("contacts") or {})))
        if b.get("crypto_ready") is False:
            f = b.get("failed") or {}
            print(i18n.t("cli.bus.status_broken", reason=_why(f.get("code"), f.get("reason") or "")))
        return 0

    def on(self, args):
        self.owner("hub bus on")
        label, args = _opt(args, "--label")
        install = "--install" in args
        if not shutil.which("ssh-keygen"):
            print(i18n.t("cli.bus.code.no_ssh"))
            return 1
        try:
            bus.crypto(self.state_dir)
        except bus.Unavailable as e:
            cmd = bus.install_command(self.state_dir)
            line = platforms.current.join_command(cmd)
            if not install:
                print(i18n.t("cli.bus.no_lib", why=str(e), target=bus.pylib(self.state_dir), command=line))
                return 1
            print(i18n.t("cli.bus.installing", command=line), flush=True)
            r = subprocess.run(cmd)
            if r.returncode != 0:
                print(i18n.t("cli.bus.install_failed", rc=r.returncode, command=line))
                return 1
            bus._LIB = None
            try:
                bus.crypto(self.state_dir)
            except bus.Unavailable as e2:
                print(i18n.t("cli.bus.no_lib", why=str(e2), target=bus.pylib(self.state_dir), command=line))
                return 1
        why = bus.selfcheck(bus.crypto(self.state_dir))
        if why:
            print(i18n.t("cli.bus.refused", reason=_why("selfcheck", why)))
            return 1
        if label:   # the same one setting as `hub bus name`; only once the bus can come on
            code, reason = self.ask("bus.name_requested", {"name": label})
            if code != "ok":
                return self.refused(code, reason)
        code, reason = self.ask("bus.on_requested", {})
        if code != "ok":
            return self.refused(code, reason)
        me = self.bus().get("me") or {}
        print(i18n.t("cli.bus.on", label=me.get("label") or "-", fp=me.get("fp"), check=bus.code(me["fp"])))
        return 0

    def name(self, args):
        """`hub bus name ["<name>"]`: this hub's name for the other hubs — shown, or set (the owner's one required
        setting: no link, no join without it)."""
        if len(args) > 1:
            raise SystemExit(i18n.t("cli.bus.usage"))
        if not args:
            name = self.bus().get("name")
            print(i18n.t("cli.bus.name_is", name=name) if name else i18n.t("cli.bus.name_none"))
            return 0
        self.owner("hub bus name")
        code, reason = self.ask("bus.name_requested", {"name": args[0]})
        if code != "ok":
            return self.refused(code, reason)
        print(i18n.t("cli.bus.name_set", name=args[0].strip()))
        return 0

    def mode(self, args):
        """`hub bus mode [secretary|remote]`: the liaison's mode — shown, or set by the owner. remote: nobody sits at
        this hub's page, the linked hubs with the pair code checked are its owners (spec d2505378, part D, remote)."""
        if len(args) > 1 or (args and args[0] not in busstate.MODES):
            raise SystemExit(i18n.t("cli.bus.mode_usage"))
        if not args:
            remote = self.bus().get("mode") == "remote"
            print(i18n.t("cli.bus.mode_remote" if remote else "cli.bus.mode_secretary"))
            return 0
        self.owner("hub bus mode")
        code, reason = self.ask("bus.mode_requested", {"mode": args[0]})
        if code != "ok":
            return self.refused(code, reason)
        print(i18n.t("cli.bus.mode_remote" if args[0] == "remote" else "cli.bus.mode_secretary"))
        return 0

    def off(self):
        self.owner("hub bus off")
        code, reason = self.ask("bus.off_requested", {})
        if code != "ok":
            return self.refused(code, reason)
        print(i18n.t("cli.bus.off"))
        return 0

    def card(self):
        self.owner("hub bus card")
        b = self.bus()
        me = b.get("me") or {}
        if not b.get("enabled") or not me.get("fp"):
            print(i18n.t("cli.bus.not_on"))
            return 1
        keys = bus.load(bus.key_dir(), self.state_dir)
        if keys.fp != me["fp"]:
            print(i18n.t("cli.bus.refused", reason=_why("key_changed", keys.fp)))
            return 1
        from core import catalog
        agents = busd.exposed(catalog.config_or_last(self.state_dir))   # the same catalog as the introduction's
        print(bus.make_card(keys, bus.hub_label(me, catalog.config_or_last(self.state_dir)), time.time(), relays=busd.relays_of(b), agents=agents))
        print(i18n.t("cli.bus.card_hint", check=bus.code(keys.fp)), file=sys.stderr)
        return 0

    def contact(self, args):
        sub, args = (args[0], args[1:]) if args else ("ls", [])
        if sub == "ls" and self.is_liaison():
            return self.ls_short()
        self.owner(f"hub bus contact {sub}")
        if sub == "ls":
            return self.ls()
        code_given, args = _opt(args, "--code")
        if sub == "verify":
            if len(args) != 1:
                raise SystemExit(i18n.t("cli.bus.usage"))
            done = self.flow().verify(args[0], code_given)
            if not done.ok:
                return self.fail(done)
            print(i18n.t("cli.bus.verified", name=args[0]))
            return 0
        if sub == "add":
            name, args = _opt(args, "--as")
            if len(args) != 1 or not name:
                raise SystemExit(i18n.t("cli.bus.usage"))
            data = {"op": "add", "name": name, "card": args[0], **({"code": code_given} if code_given else {})}
        elif sub == "set":
            status, args = _opt(args, "--status")
            may, args = _opt(args, "--may-write")     # the rights are gone (the liaison decides): taken, nothing is done
            allow, args = _opt(args, "--allow-from")
            if len(args) != 1 or not (status or may is not None or allow is not None):
                raise SystemExit(i18n.t("cli.bus.usage"))
            name = args[0]
            if not status:
                print(i18n.t("cli.bus.rights_gone"))
                return 0
            data = {"op": "set", "name": name, "status": status, **({"code": code_given} if code_given else {})}
        elif sub == "rename" and len(args) == 2:
            name, alias = args
            code, reason = self.ask("bus.contact_requested", {"op": "rename", "name": name, "alias": alias})
            if code != "ok":
                return self.refused(code, reason)
            c = (self.bus().get("contacts") or {}).get(name) or {}
            print(i18n.t("cli.bus.renamed", name=name, label=c.get("alias")) if c.get("alias")
                  else i18n.t("cli.bus.rename_cleared", name=name, label=c.get("label")))
            return 0
        elif sub == "remove" and len(args) == 1:
            name = args[0]
            data = {"op": "remove", "name": name}
        else:
            raise SystemExit(i18n.t("cli.bus.usage"))
        code, reason = self.ask("bus.contact_requested", data)
        if code != "ok":
            return self.refused(code, reason)
        c = (self.bus().get("contacts") or {}).get(name)
        if sub == "remove" or c is None:
            print(i18n.t("cli.bus.contact_removed", name=name))
        elif c["status"] == "pending":
            print(i18n.t("cli.bus.contact_pending", name=name, label=busstate.shown(c), fp=c["fp"], check=bus.code(c["fp"])))
        else:
            print(i18n.t("cli.bus.contact_status", name=name, label=busstate.shown(c), fp=c["fp"], status=c["status"]))
        return 0

    def ls(self):
        b = self.bus()
        contacts = b.get("contacts") or {}
        if not contacts:
            print(i18n.t("cli.bus.contacts_none"))
            return 0
        for name, c in sorted(contacts.items()):
            row = i18n.t("cli.bus.contact_row", name=name, label=busstate.shown(c), status=c.get("status"), fp=c.get("fp"),
                         check=bus.code(c["fp"]))
            print(row + (i18n.t("cli.bus.quarantine") if c.get("quarantine") else ""))
            extra = ([i18n.t("cli.bus.stated", label=liaison.safe_label(c.get("label")))] if c.get("alias") else []) \
                + ([i18n.t("cli.bus.person", name=(c.get("person") or {}).get("name"))]
                   if c.get("person") else [])
            pair = bus.contact_code(c)
            if pair:   # linked by a link: the pair code both pages show, and whether it is checked
                extra.append(i18n.t("cli.bus.pair_checked", code=pair) if c.get("verified")
                             else i18n.t("cli.bus.pair_unchecked", code=pair, name=name))
            if extra:
                print("    " + " · ".join(extra))
        return 0

    def ls_short(self):
        """The liaison's view (§5.5.1, p. 7): the local name, the status, the stated label, the visible sessions; of a linked person — the name and the kinds of their paths,
        never their values; no keys, no fingerprints."""
        from modules.contacts import contacts as book
        b = self.bus()
        cs = b.get("contacts") or {}
        if not b.get("enabled"):
            print(i18n.t("cli.bus.not_on"))
            return 1
        if not cs:
            print(i18n.t("cli.bus.contacts_none"))
            return 0
        try:
            rows = book.people(book.default_path())
        except (OSError, ValueError):
            rows = []
        for name, c in sorted(cs.items()):
            parts = [name, c.get("status") or "-", i18n.t("cli.bus.stated", label=liaison.safe_label(c.get("label")))]
            if c.get("agents"):
                parts.append(i18n.t("cli.bus.agents", names=", ".join(sorted(c["agents"]))))
            state, row = book.person_of(rows, c.get("person"))
            if state == "ok":
                kinds = [k for k, has in (("Telegram", row.get("telegram")), ("Teams", row.get("teams") or row.get("teams_chat")),
                                          ("email", row.get("email"))) if has]
                parts.append(i18n.t("cli.bus.person_kinds", name=row["name"], kinds=", ".join(kinds) or "-"))
            print(" · ".join(parts))
        return 0

    # --- the liaison ---------------------------------------------------------------------------------------------

    def decided(self, type_, data, done):
        """Put the liaison's request and say the core's decision: the first core.bus_* fact on it (`of` — its number)."""
        path = os.path.join(self.state_dir, "journal.jsonl")
        start = journal.mark(path)
        seq = self.put(type_, None, data, say=False)
        if seq is None:
            print(i18n.t("cli.bus.waiting"))
            return 1
        if seq in self.rejected:
            print(i18n.t("cli.bus.rejected", reason=coretext.human(self.rejected[seq])))
            return 1
        if not done:
            print(i18n.t("cli.bus.taken"))
            return 0
        hit = journal.waited(path, start, lambda e: e.get("type") in done and (e.get("data") or {}).get("of") == seq, WAIT)
        if hit is None:
            print(i18n.t("cli.bus.waiting"))
            return 0
        refusal = journal.waited(path, start, lambda e: e.get("type") == "core.rejected"
                                 and (e.get("data") or {}).get("of") == hit.get("seq"), 0)
        if refusal is not None:
            print(i18n.t("cli.bus.rejected", reason=coretext.human(coretext.why_of(refusal["data"]))))
            return 1
        d = hit.get("data") or {}
        if hit["type"] == "core.bus_send_held":
            print(i18n.t("cli.bus.sent_held", out=d.get("out")))
        elif hit["type"] == "core.bus_out":
            print(i18n.t("cli.bus.sent", out=d.get("out"), id=d.get("id")))
        elif hit["type"] == "core.bus_passed":
            print(i18n.t("cli.bus.passed", inbox=d.get("inbox"), to=d.get("to")))
        else:
            print(i18n.t("cli.bus.refused", reason=" ".join([d.get("code") or "-", busstate.REFUSED_HINT.get(d.get("code"), "")]).strip()))
            return 1
        return 0

    def send(self, args):
        self.liaison_only("hub bus send")
        agent, args = _opt(args, "--agent")
        thread, args = _opt(args, "--thread")
        basis, args = _opt(args, "--text-from")
        text, args = _opt(args, "--text")
        if len(args) != 1 or (basis is None) == (text is None):
            raise SystemExit(i18n.t("cli.bus.usage"))
        data = {"contact": args[0], **({"agent": agent} if agent else {}), **({"thread": thread} if thread else {}),
                **({"text_from": basis} if basis else {}), **({"text": text} if text is not None else {})}
        return self.decided("bus.send_requested", data, ("core.bus_send_held", "core.bus_out", "core.bus_send_refused"))

    def pass_on(self, args):
        self.liaison_only("hub bus pass")
        note, args = _opt(args, "--note")
        if len(args) != 2:
            raise SystemExit(i18n.t("cli.bus.usage"))
        return self.decided("bus.pass_requested", {"inbox": args[0], "session": args[1], **({"note": note} if note else {})},
                            ("core.bus_passed",))

    def decline(self, args, what="decline"):
        """hub bus decline / done: an outcome for an item (`why` — the liaison's reason); the fold answers at once."""
        self.liaison_only(f"hub bus {what}")
        why, args = _opt(args, "--why")
        if len(args) != 1:
            raise SystemExit(i18n.t("cli.bus.usage"))
        return self.decided(f"bus.{what}_requested", {"inbox": args[0], **({"why": why} if why else {})}, ())

    def done(self, args):
        return self.decline(args, "done")

    def note(self, args):
        self.liaison_only("hub bus note")
        on, args = _opt(args, "--on")
        if len(args) < 2:
            raise SystemExit(i18n.t("cli.bus.usage"))
        return self.decided("bus.note", {"contact": args[0], "text": " ".join(args[1:]), **({"on": on} if on else {})}, ())

    def sessions(self):
        """hub bus sessions: the live sessions of this hub the liaison may pass a message to — the name and what the
        session does (not the liaison itself)."""
        if self.outsider or self.me and not self.is_liaison():
            raise SystemExit(i18n.t("cli.bus.not_liaison", what="hub bus sessions"))
        rows = busstate.liaison_sessions(self.current())
        print("\n".join(f"{n} — {about or '-'}" for n, about in sorted(rows.items())) if rows else i18n.t("cli.bus.sessions_none"))
        return 0

    def rule(self, args):
        """hub bus rule ls [<contact>] | add <contact|*> "<text>" | rm <id>: the liaison's rules about hubs — the liaison
        writes them down on the owner's or a session's word (the core refuses them from another hub's turn) and reads
        them with each message of that hub; the owner reads and removes them too."""
        if self.me and not self.is_liaison() or self.outsider:
            raise SystemExit(i18n.t("cli.bus.not_liaison", what="hub bus rule"))
        sub, rest = (args[0], args[1:]) if args else ("ls", [])
        if sub == "ls":
            rows = [r for r in self.bus().get("rules") or [] if not rest or r.get("contact") in (rest[0], "*")]
            print("\n".join(i18n.t("cli.bus.rule_row", id=r.get("id"), contact=r.get("contact"), text=r.get("text"),
                                    by=r.get("by") or "-") for r in rows) if rows else i18n.t("cli.bus.rules_none"))
            return 0
        if sub == "add" and len(rest) == 2:
            data = {"op": "add", "contact": rest[0], "text": rest[1]}
        elif sub in ("rm", "remove") and len(rest) == 1:
            data = {"op": "remove", "id": rest[0]}
        else:
            raise SystemExit(i18n.t("cli.bus.usage"))
        code, reason = self.ask("bus.rule_requested", data)
        if code != "ok":
            return self.refused(code, reason)
        print(i18n.t("cli.bus.rule_saved" if sub == "add" else "cli.bus.rule_removed"))
        return 0

    # --- a session's request to the liaison, links to people ----------------------------------------------------

    def request(self, args):
        """hub send liaison --to <contact> "…": the name is resolved by the one resolver (§5.5.1, p. 5) — the local name
        of a contact or a person of the book; the canonical local name and how it was named go into the message."""
        from modules.contacts import contacts as book
        to, args = _opt(args, "--to")
        thread, args = _opt(args, "--thread")
        args = [a for a in args if a not in ("--now", "--fyi")]
        if not args or args[0] != liaison.NAME or not to or len(args) < 2:
            raise SystemExit(i18n.t("cli.bus.request_usage"))
        if self.is_liaison():
            raise SystemExit(i18n.t("cli.bus.liaison_no_send"))
        try:
            rows = book.people(book.default_path())
        except (OSError, ValueError):
            rows = []
        name, via, err = book.resolve(busd.read_view(self.state_dir), rows, to)
        if err:
            code, names = err
            print(i18n.t("cli.bus.no_hub_path", who=to) if code == "no_hub_path"
                  else i18n.t("cli.bus.ambiguous_path", who=to, names=", ".join(names) or "-"))
            return 1
        text = " ".join(args[1:])
        if ((busd.read_view(self.state_dir).get("contacts") or {}).get(name) or {}).get("direct"):
            # the owner's own hub: the core sends the words itself, the liaison is not woken (spec d2505378, part A, outgoing)
            seq = self.put("bus.direct_requested", None, {"contact": name, "text": text, "via": via,
                                                          **({"thread": thread} if thread else {})}, say=False)
            why = self.rejected.get(seq) if seq is not None else None
            if why is None:
                if seq is not None:
                    print(i18n.t("cli.bus.direct_sent", contact=name, seq=seq))
                return 0   # not decided in time: put said it is not lost, a later refusal comes to the session
            if not str(why).startswith("not_direct"):
                print(i18n.t("cli.bus.rejected", reason=coretext.human(why)))
                return 1
            # the view was older than the state: the liaison sends it, as for any other hub
        seq = self.put("message.received", None, {"to": liaison.NAME, "text": text, "bus_to": name, "bus_to_via": via,
                                                  **({"bus_thread": thread} if thread else {})})
        return 0 if seq is not None and seq not in self.rejected else 1

    def link(self, args):
        """hub contacts link "<person>" <hub> / unlink <hub>: the owner links a bus contact to a record of the book —
        the record gets an `id` (under the book's lock), the core records the link; it gives no trust (§5.5.1, p. 4)."""
        from modules.contacts import contacts as book
        sub = args[0]
        self.owner(f"hub contacts {sub}")
        if sub == "unlink":
            if len(args) != 2:
                raise SystemExit(i18n.t("cli.bus.usage"))
            data = {"op": "link", "name": args[1], "person": None}
        else:
            if len(args) != 3:
                raise SystemExit(i18n.t("cli.bus.usage"))
            who, hub_name = args[1], args[2]
            if hub_name not in (self.bus().get("contacts") or {}):
                print(i18n.t("cli.bus.refused", reason=_why("no_contact", hub_name)))
                return 1
            q = who.strip().casefold()

            def keys(r):
                al = r.get("aliases") if isinstance(r.get("aliases"), list) else []
                return [r["name"], *[a for a in al if isinstance(a, str)]]

            def change(rows):
                hits = [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)
                        and any(k.casefold() == q for k in keys(r))]
                if len(hits) != 1:
                    return None, len(hits)
                r = hits[0]
                if not r.get("id") or sum(1 for x in rows if isinstance(x, dict) and x.get("id") == r["id"]) > 1:
                    r["id"] = book.new_id(rows)   # a record picked out of copies gets an id of its own
                return {"id": r["id"], "name": r["name"]}, 1
            person, n = book.update(book.default_path(), change)
            if person is None:
                print(i18n.t("cli.bus.person_none", who=who) if n == 0 else i18n.t("cli.bus.person_many", who=who, n=n))
                return 1
            data = {"op": "link", "name": hub_name, "person": person}
        code, reason = self.ask("bus.contact_requested", data)
        if code != "ok":
            return self.refused(code, reason)
        print(i18n.t("cli.bus.linked", name=data["name"], person=(data["person"] or {}).get("name") or "-"))
        return 0

    def book_edit(self, args):
        """hub contacts rename "<person>" "<new name>" / remove "<person>": the owner corrects the book. A rename keeps the
        record's id, so its hubs stay linked; their stored name of the person is renewed. A record with a hub linked to it is
        not removed — unlink the hub first."""
        from modules.contacts import contacts as book
        sub = args[0]
        self.owner(f"hub contacts {sub}")
        if len(args) != (3 if sub == "rename" else 2):
            raise SystemExit(i18n.t("cli.bus.usage"))
        hubs = self.bus().get("contacts") or {}
        try:
            if sub == "remove":
                row = book.remove(book.default_path(), args[1], hubs)
                print(i18n.t("contacts.removed", name=row["name"]))
                return 0
            row, old = book.rename(book.default_path(), args[1], args[2])
        except ValueError as e:
            print(e)
            return 1
        print(i18n.t("contacts.renamed", old=old, row=book.line(row)))
        failed = 0
        for hub_name in book.hubs_of(hubs, row):   # every hub is told, one refusal does not stop the others
            code, reason = self.ask("bus.contact_requested", {"op": "link", "name": hub_name,
                                                              "person": {"id": row["id"], "name": row["name"]}})
            failed += code != "ok" and self.refused(code, reason)
        return 1 if failed else 0   # the book is renamed: the same command again renews the hubs

    # --- the relay -------------------------------------------------------------------------------------------------

    def relay(self, args):
        sub, args = (args[0], args[1:]) if args else ("status", [])
        self.owner(f"hub bus relay {sub}")
        b = self.bus()
        if not b.get("enabled") or not (b.get("me") or {}).get("fp"):
            print(i18n.t("cli.bus.not_on"))
            return 1
        if sub == "join" and args[:1] == ["--file"] and len(args) != 2:
            raise SystemExit(i18n.t("cli.text.file_usage"))
        if sub == "join" and len(args) in (1, 2) and (len(args) == 1 or args[0] == "--file"):
            text = self.invite_text(args)
            return 1 if text is None else self.relay_join(text, b)
        if sub in ("invite", "leave", "status") and not args:
            if not b.get("relay"):
                print(i18n.t("cli.bus.relay_none"))
                return 1 if sub != "status" else 0
            return getattr(self, f"relay_{sub}")(b)
        raise SystemExit(i18n.t("cli.bus.usage"))

    def _keys(self, b):
        keys = bus.load(bus.key_dir(), self.state_dir)
        if keys.fp != b["me"]["fp"]:
            raise bus.Error("key_changed", keys.fp)
        return keys

    def _client(self, b, keys_fp):
        found = busrelay.transport_key()
        if found is None:
            raise busrelay.Error("no_transport_key", i18n.t("cli.bus.relay_no_key"))
        relay = b["relay"]
        return busrelay.Client(relay["url"], relay["key"], keys_fp, found[0])

    def invite_text(self, args):
        """The invitation of `hub bus relay join`: `-` (or `--file -`) is stdin, `--file PATH` a file — like `hub bus join -`,
        out of the shell history and `ps`; a single word is the line itself, as before. None — nothing to join (said)."""
        if args in (["-"], ["--file", "-"]):
            what = i18n.t("cli.text.stdin")
            if self.stdin.isatty():
                print(i18n.t("cli.bus.join_prompt"), file=sys.stderr, flush=True)
            try:
                raw = self.stdin.readline() if self.stdin.isatty() else self.stdin.read(4 * bus.LINK_MAX)
            except (OSError, ValueError) as e:   # ValueError: not UTF-8
                print(i18n.t("cli.text.file_unreadable", path=what, why=e))
                return None
        elif args[0] == "--file":
            what = args[1]
            try:
                with open(os.path.expanduser(what), encoding="utf-8-sig") as f:
                    raw = f.read(4 * bus.LINK_MAX)
            except (OSError, ValueError) as e:   # ValueError: not UTF-8
                print(i18n.t("cli.text.file_unreadable", path=what, why=(e.strerror if isinstance(e, OSError) else None) or e))
                return None
        else:
            return args[0]
        if not raw.strip():
            print(i18n.t("cli.text.empty_input", what=what))
            return None
        kind, token = busflow.find(raw)
        return token if kind == "invite" else raw.strip()

    def relay_join(self, text, b):
        """The registration first, then the core records the relay (core/busflow.Flow.relay_join)."""
        done = self.flow(quiet=True).relay_join(text, b, self._keys(b))
        if not done.ok:
            return self.fail(done)
        print(i18n.t("cli.bus.relay_joined", url=done.data["url"], by=done.data.get("by") or "-"))
        return 0

    # --- the owner's path in two commands (core/busflow.py, spec hubbus/ux §3.5) ---------------------------------

    def flow(self, quiet=False):
        return busflow.Flow(self.put, self.current, self.state_dir, rejected=self.rejected, note=self.quiet,
                            say=None if quiet else self.show, idle=self.idle)

    def show(self, step, state, text):
        """A step of the path, as it goes — to stderr: stdout carries only the result (the link)."""
        t = i18n.t
        if step == "join":   # literal keys: the i18n checks find them by text
            line = {"joining": t("cli.bus.join_step.joining"), "joined": t("cli.bus.join_step.joined"),
                    "contact": t("cli.bus.join_step.contact"), "intro_sent": t("cli.bus.join_step.intro_sent"),
                    "linked": t("cli.bus.join_step.linked"),
                    "run": t("cli.bus.join_start", label=liaison.safe_label(text))}.get(state)
            if line:
                print(f"  ✓ {line}", file=sys.stderr, flush=True)
            return
        if step == "link":
            return
        name = {"ssh": t("cli.bus.step.ssh"), "lib": t("cli.bus.step.lib"), "selfcheck": t("cli.bus.step.selfcheck"),
                "keys": t("cli.bus.step.keys"), "card": t("cli.bus.step.card"), "relay": t("cli.bus.step.relay")}.get(step,
                                                                                                                  step)
        if state == "run":
            print(f"  … {name}" + (f": {text}" if text else ""), file=sys.stderr, flush=True)
        elif state == "ok":
            print(f"  ✓ {name}", file=sys.stderr, flush=True)
        elif state == "skip":
            print(f"  – {name}: {i18n.t('cli.bus.step_skip')}", file=sys.stderr, flush=True)
        else:
            print(f"  ✗ {name}: {text}", file=sys.stderr, flush=True)

    def fail(self, done):
        print(i18n.t("cli.bus.refused", reason=done.text or done.code))
        if done.command:
            print(i18n.t("cli.bus.command", command=done.command))
        return 1

    def yes(self, args, question):
        """--yes, or the owner's answer from the terminal; no terminal — a refusal that names --yes."""
        if "--yes" in args:
            return True
        got = self.confirm(question)
        if got is None:
            print(i18n.t("cli.bus.no_tty"))
            return False
        if not got:
            print(i18n.t("cli.bus.declined"))
        return got

    def link_cmd(self, args):
        self.owner("hub bus link")
        ttl, args = _opt(args, "--ttl")
        label, args = _opt(args, "--label")
        ttls = {"1h": 3600, "24h": 86400, "7d": 604800}
        if [a for a in args if a != "--yes"] or (ttl or "24h") not in ttls:
            raise SystemExit(i18n.t("cli.bus.usage"))
        if not self.bus().get("name"):   # before any question: nothing is made without the hub's name
            return self.fail(busflow.no_name())
        # no relay yet: the link takes the public one this hub was installed with — said before the question
        # (a process without the library leaves the relay to a paste, as before: nothing to name then)
        public = None if self.bus().get("relay") or not self.flow(quiet=True).has_library() \
            else busflow.public_relay(self.state_dir)
        if not self.bus().get("enabled"):
            print(i18n.t("cli.bus.consent"), file=sys.stderr)
            if public:
                print(i18n.t("cli.bus.public_relay", url=public), file=sys.stderr)
            if not self.yes(args, i18n.t("cli.bus.consent_q")):
                return 1
        elif public:
            print(i18n.t("cli.bus.public_relay", url=public), file=sys.stderr)
        done = self.flow().link(ttl=ttls[ttl or "24h"], label=label)
        if not done.ok:
            return self.fail(done)
        print(done.value)
        hint = i18n.t("cli.bus.link_hint", when=busflow._when(done.data.get("exp")))
        print(hint if done.data.get("posted") else hint + "\n" + i18n.t("cli.bus.link_unposted"), file=sys.stderr)
        return 0

    def join_cmd(self, args):
        self.owner("hub bus join", hint="cli.bus.join_offer_hint")
        rest = [a for a in args if a != "--yes"]
        if rest not in ([], ["-"]):
            print(i18n.t("cli.bus.join_not_arg"))
            return 2
        if not self.bus().get("name"):
            return self.fail(busflow.no_name())
        if not rest and self.stdin.isatty():
            print(i18n.t("cli.bus.join_prompt"), file=sys.stderr, flush=True)
            text = self.stdin.readline()
        else:
            text = self.stdin.read(4 * bus.LINK_MAX)
        kind, token = busflow.find(text)
        b = self.bus()
        if kind == "link":
            try:
                info = busflow.preview(token, b, time.time())
            except bus.Error as e:
                print(i18n.t("cli.bus.refused", reason=busflow.why(e.code, str(e) if e.code == "other_relay" else "")))
                return 1
            print(i18n.t("cli.bus.join_preview", label=liaison.safe_label(info["label"]), code=info["code"],
                         relay=info["relay"] or "-", when=busflow._when(info["exp"])), file=sys.stderr)
            if info["needs"]:
                print(i18n.t("cli.bus.consent"), file=sys.stderr)
            if not self.yes(args, i18n.t("cli.bus.join_q", label=liaison.safe_label(info["label"]))):
                return 1
        elif kind == "invite":
            try:
                inv = busrelay.read_invite(token)[0]
            except busrelay.Error as e:
                print(i18n.t("cli.bus.refused", reason=busflow.why("bad_invite", str(e))))
                return 1
            print(i18n.t("cli.bus.invite_preview", url=inv["relay"], by=inv["by"]), file=sys.stderr)
            if not b.get("enabled"):
                print(i18n.t("cli.bus.consent"), file=sys.stderr)
            if not self.yes(args, i18n.t("cli.bus.invite_q", url=inv["relay"])):
                return 1
        done = self.flow().join(text)
        if done.code in ("waiting", "queued"):
            print(done.text)
            return 0
        if not done.ok:
            return self.fail(done)
        if kind == "invite":
            print(i18n.t("cli.bus.relay_joined", url=done.data.get("url") or "-", by=done.data.get("by") or "-"))
            print(i18n.t("cli.bus.invite_next"))
            return 0
        print(i18n.t("cli.bus.linked_pair", label=liaison.safe_label(done.data.get("label")),
                     code=done.data.get("pair_code") or "-",
                     name=done.data.get("name") or "<name>"))
        return 0

    def offer_cmd(self, args):
        """`hub bus offer <file>|-`: a session was given another hub's invitation (a message, a file) — it brings it to the
        owner as a card (the page, Telegram): «Link» is the join of a pasted one. Nothing is joined here; the invitation
        goes as a file in the core's folder, never an argument or the journal."""
        if self.outsider or not self.me:
            raise SystemExit(i18n.t("cli.bus.offer_not_here"))
        if len(args) != 1 or (args[0].startswith("-") and args[0] != "-"):
            raise SystemExit(i18n.t("cli.bus.offer_usage"))
        try:
            if args[0] == "-":
                text = self.stdin.read(4 * bus.LINK_MAX)
            else:
                with open(args[0], encoding="utf-8", errors="replace") as f:
                    text = f.read(4 * bus.LINK_MAX)
        except OSError as e:
            print(i18n.t("cli.bus.offer_no_file", file=args[0], why=e.strerror or type(e).__name__))
            return 1
        kind, token = busflow.find(text)
        if kind != "link":
            print(i18n.t("cli.bus.offer_not_link"))
            return 1
        b = self.bus()
        if not b.get("enabled"):
            print(i18n.t("cli.bus.offer_off"))
            return 1
        try:
            info = busflow.preview(token, b, time.time())
        except bus.Error as e:
            print(i18n.t("cli.bus.refused", reason=busflow.why(e.code, str(e) if e.code == "other_relay" else "")))
            return 1
        label = liaison.safe_label(info["label"])
        if ((b.get("offers") or {}).get(info["id"]) or {}).get("state") == "open":
            print(i18n.t("cli.bus.offer_again", label=label))
            return 0
        raw = token.encode("ascii")
        digest = hashlib.sha256(raw).hexdigest()
        if busflow.waiting_request(self.state_dir, "bus.offer_requested", digest):   # the first one waits in the mailbox
            print(i18n.t("cli.bus.offer_queued", label=label))
            return 0
        folder = busd.offer_dir(self.state_dir)
        platforms.current.makedirs_private(folder)
        path = os.path.join(folder, digest + ".link")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        code, reason, _ = busflow.ask(self.put, self.state_dir, self.rejected, "bus.offer_requested",
                                      {"path": path, "sha256": digest}, WAIT)
        if code == "ok":
            print(i18n.t("cli.bus.offer_done", label=label))
            return 0
        if code == "offered":
            print(i18n.t("cli.bus.offer_again", label=label))
            return 0
        if code is None:   # the core has not answered: it takes the file when it gets to the mailbox
            print(i18n.t("cli.bus.offer_queued", label=label))
            return 0
        if code == "rejected":
            try:
                os.unlink(path)   # the fold refused before the core read the file: ours to remove
            except OSError:
                pass
            print(i18n.t("cli.bus.rejected", reason=coretext.human(reason)))
            return 1
        print(i18n.t("cli.bus.refused", reason=busflow.why(code, reason)))
        return 1

    def reset_cmd(self, args):
        self.owner("hub bus reset")
        if [a for a in args if a != "--yes"]:
            raise SystemExit(i18n.t("cli.bus.usage"))
        b = self.bus()
        print(i18n.t("cli.bus.reset_warn", n=len(b.get("contacts") or {}), fp=(b.get("me") or {}).get("fp") or "-"),
              file=sys.stderr)
        if not self.yes(args, i18n.t("cli.bus.reset_q")):
            return 1
        done = self.flow(quiet=True).reset()
        if not done.ok:
            return self.fail(done)
        print(i18n.t("cli.bus.reset_done"))
        if done.data.get("left") is False:
            print(i18n.t("cli.bus.reset_relay_stays"))
        return 0

    def chat_cmd(self, args):
        self.owner("hub bus chat")
        if not args:
            raise SystemExit(i18n.t("cli.bus.usage"))
        name, words = args[0], args[1:]
        yes = "--yes" in (words[:1] + words[-1:])   # first or last: inside, it is a word of the text
        words = words[1:] if words[:1] == ["--yes"] else words[:-1] if yes else words
        if not words:
            return self.chat_show(name)
        text = self.stdin.read(65536) if words == ["-"] else " ".join(words)
        flow = self.flow(quiet=True)
        done = flow.chat(name, text)
        if done.code == "secret":   # a key-like text: the owner sees it and says yes (the page asks the same in its box)
            print(busflow.why("secret"), file=sys.stderr)
            if not self.yes(["--yes"] if yes else [], i18n.t("cli.bus.chat_secret_q")):
                return 1
            done = flow.chat(name, text, confirm=True)
        if not done.ok:
            return self.fail(done)
        print(i18n.t("cli.bus.chat_sent", name=name))
        return 0

    def chat_show(self, name):
        b = self.bus()
        if name not in (b.get("contacts") or {}):
            print(i18n.t("cli.bus.refused", reason=_why("no_contact", name)))
            return 1
        # a delivered message keeps no text in the state: it is the owner's request in the journal (its tail here)
        said = _said(os.path.join(self.state_dir, "journal.jsonl"))
        rows = [(r["ts"], "→" if r["way"] == "out" else "←",
                 (f"{r['agent']}: " if r["way"] == "in" and r.get("agent") else "")
                 + (r["text"] or i18n.t("cli.bus.chat_archived")), _fate(r))
                for r in busflow.chat_rows({"bus": b}, name, said=said)]   # a key-like text the core held back is not there
        if not rows:
            print(i18n.t("cli.bus.chat_none", name=name))
        for ts, way, text, status in sorted(rows)[-20:]:
            print(f"{_when(ts)} {way} {text}" + (f"  [{status}]" if status else ""))
        return 0

    def relay_invite(self, b):
        keys = self._keys(b)
        text, _, exp = busrelay.make_invite(keys, b["relay"], time.time())
        self._client(b, keys.fp).invite(text)
        print(text)
        print(i18n.t("cli.bus.relay_invite_hint", when=time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))),
              file=sys.stderr)
        return 0

    def relay_leave(self, b):
        try:
            self._client(b, b["me"]["fp"]).leave()
        except busrelay.Error as e:
            if e.code not in ("unknown_hub", "removed", "no_transport_key"):   # already gone there: the core forgets it
                raise
        code, reason = self.ask("bus.relay_requested", {"op": "leave"})
        if code != "ok":
            return self.refused(code, reason)
        print(i18n.t("cli.bus.relay_left", url=b["relay"]["url"]))
        return 0

    def relay_status(self, b):
        relay = b["relay"]
        print(i18n.t("cli.bus.relay_status", url=relay["url"], by=relay.get("by") or "-"))
        part = busd.read_relay_status(self.state_dir)
        if part:
            print(i18n.t("cli.bus.relay_part", ok=_when(part.get("ok")), sent=part.get("sent", 0), got=part.get("got", 0)))
            err = part.get("error") or {}
            if err:
                print(i18n.t("cli.bus.relay_part_error", code=err.get("code"), text=err.get("text") or "",
                             when=_when(err.get("ts"))))
            for w in part.get("waiting") or []:
                print(i18n.t("cli.bus.relay_waiting", out=w.get("out"), code=w.get("code"), n=w.get("tries", 0)))
        else:
            print(i18n.t("cli.bus.relay_part_none"))
        try:
            invites = self._client(b, b["me"]["fp"]).invites()
        except busrelay.Error as e:
            print(i18n.t("cli.bus.relay_invites_failed", code=e.code, text=str(e)))
            return 0
        for inv in invites:
            state = (i18n.t("cli.bus.relay_used", fp=inv["used_by"]) if inv.get("used_by")
                     else i18n.t("cli.bus.relay_open", when=_when(inv.get("exp"))))
            print(i18n.t("cli.bus.relay_invite_row", id=str(inv.get("id"))[:8], made=_when(inv.get("made")), state=state))
        return 0

    # --- the stand's transport -----------------------------------------------------------------------------------

    def wire(self, args):
        """One pass of the folder transport (the S1 stand, no network): every signed envelope of this hub goes to
        `<folder>/<receiver's fingerprint>/`, and what lies in `<folder>/<our fingerprint>/` goes to the core."""
        self.owner("hub bus wire")
        if len(args) != 1:
            raise SystemExit(i18n.t("cli.bus.usage"))
        folder = args[0]
        b = self.bus()
        me = (b.get("me") or {}).get("fp")
        if not b.get("enabled") or not me:
            print(i18n.t("cli.bus.not_on"))
            return 1
        sent = got = 0
        for oid, o in sorted((b.get("out") or {}).items()):
            if o.get("status") != "signed" or not o.get("envelope") or not o.get("fp"):
                continue
            box = os.path.join(folder, o["fp"])
            os.makedirs(box, exist_ok=True)
            tmp = os.path.join(box, f".{oid}.tmp")
            with open(tmp, "wb") as f:
                f.write(base64.b64decode(o["envelope"]))
            os.replace(tmp, os.path.join(box, f"{oid}-{o.get('id')}.env"))
            self.put("bus.handed", None, {"out": oid, "via": "folder"}, say=False)
            sent += 1
        mine = os.path.join(folder, me)
        inbox = busd.in_dir(self.state_dir)
        os.makedirs(inbox, exist_ok=True)
        for name in sorted(os.listdir(mine)) if os.path.isdir(mine) else []:
            if not name.endswith(".env"):
                continue
            with open(os.path.join(mine, name), "rb") as f:
                raw = f.read()
            digest = hashlib.sha256(raw).hexdigest()
            path = os.path.join(inbox, digest + ".env")
            with open(path, "wb") as f:
                f.write(raw)
            os.unlink(os.path.join(mine, name))
            self.put("bus.offered", None, {"path": path, "sha256": digest, "size": len(raw)}, say=False)
            got += 1
        print(i18n.t("cli.bus.wired", sent=sent, got=got))
        return 0


_when = busflow._when


def _fate(r):
    """The mark after a message in the list: where ours is (queued, sent, delivered, …); what became of the other hub's
    words — held or unverified, else what the liaison did with them (passed, declined, answered, or nothing yet)."""
    if r["way"] == "out":
        return r["status"] or ""
    return r["status"] if r["status"] not in ("ok", None) else r["outcome"] or ""


_said = busflow.said_in_journal   # one reader of the owner's chat words for the terminal and the page


def main(argv, put, current, state_dir, me=None, outsider=None, rejected=None, note=None, idle=None):
    cli = Cli(put, current, state_dir, me, outsider, rejected if rejected is not None else {}, note=note, idle=idle)
    cmd, rest = (argv[0], list(argv[1:])) if argv else ("status", [])
    liaison_cmds = {"send": cli.send, "pass": cli.pass_on, "decline": cli.decline, "done": cli.done, "note": cli.note}
    try:
        if cmd in liaison_cmds:
            return liaison_cmds[cmd](rest)
        if cmd == "rule":
            return cli.rule(rest)
        if cmd == "sessions" and not rest:
            return cli.sessions()
        if cmd == "request":
            return cli.request(rest)
        if cmd == "contacts" and rest[:1] in (["link"], ["unlink"]):
            return cli.link(rest)
        if cmd == "contacts" and rest[:1] in (["rename"], ["remove"]):
            return cli.book_edit(rest)
        if cmd == "wire":
            return cli.wire(rest)
        if cmd == "relay":
            return cli.relay(rest)
        if cmd == "status" and not rest:
            return cli.status()
        if cmd == "on":
            return cli.on(rest)
        if cmd == "name":
            return cli.name(rest)
        if cmd == "mode":
            return cli.mode(rest)
        if cmd == "off" and not rest:
            return cli.off()
        if cmd == "card" and not rest:
            return cli.card()
        if cmd == "contact":
            return cli.contact(rest)
        if cmd == "link":
            return cli.link_cmd(rest)
        if cmd == "join":
            return cli.join_cmd(rest)
        if cmd == "offer":
            return cli.offer_cmd(rest)
        if cmd == "reset":
            return cli.reset_cmd(rest)
        if cmd == "chat":
            return cli.chat_cmd(rest)
    except bus.Error as e:
        print(i18n.t("cli.bus.refused", reason=_why(e.code, str(e))))
        return 1
    except busrelay.Error as e:
        print(i18n.t("cli.bus.relay_refused", code=e.code, text=str(e)))
        return 1
    except bus.Unavailable as e:   # the terminal's Python has no library (the core's may have): say how to get it
        line = platforms.current.join_command(bus.install_command(state_dir))
        print(i18n.t("cli.bus.no_lib", why=str(e), target=bus.pylib(state_dir), command=line))
        return 1
    print(i18n.t("cli.bus.usage"))
    return 2
