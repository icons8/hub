"""The owner's contacts (owner request 2026-10-01: keep a table of recipients so they are not confused, and ask the
first time before sending to Telegram; a session searching by surname had written to a namesake).
One table for all sessions: a person maps to Telegram (username + id) / Teams / email, plus a source and a confirmation date."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

from modules.contacts import contacts

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class Table(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")

    def test_add_find_update_and_private_file(self):
        contacts.add(self.path, "Anna Lee", telegram="@annalee:100200300", aliases=["Annie", "Anna L"],
                     source="owner, a message")
        contacts.add(self.path, "Mark Cole", aliases=["Marky"], teams_chat="19:abc@unq.gbl.spaces",
                     note="SEO Team Lead", source="hub journal")
        if os.name != "nt":   # file permissions are POSIX; on Windows chmod knows only read-only
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        (anna,) = contacts.find(self.path, "annie")
        self.assertEqual((anna["telegram"], anna["source"]), ({"username": "annalee", "id": 100200300}, "owner, a message"))
        self.assertTrue(anna["confirmed"])
        self.assertEqual(contacts.find(self.path, "100200300")[0]["name"], "Anna Lee")   # by id
        self.assertEqual(contacts.find(self.path, "@annalee")[0]["name"], "Anna Lee")     # by username
        self.assertEqual(contacts.find(self.path, "marky")[0]["name"], "Mark Cole")   # by alias
        self.assertEqual(contacts.find(self.path, "anna_other"), [])                      # a stranger is not found
        contacts.add(self.path, "Anna Lee", email="anna@x.com", source="owner")      # add to the record, do not duplicate
        (anna,) = contacts.find(self.path, "Anna Lee")
        self.assertEqual((anna["email"], anna["telegram"]["id"]), ("anna@x.com", 100200300))
        self.assertEqual(len(contacts.load(self.path)), 2)

    def test_bad_telegram_spec(self):
        with self.assertRaises(ValueError):
            contacts.add(self.path, "X", telegram="annalee", source="s")   # not allowed without an id: names get confused


class Cli(unittest.TestCase):
    def test_commands(self):
        path = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")
        env = {**os.environ, "HUB_CONTACTS": path}
        run = lambda *a: subprocess.run([sys.executable, HUB, "contacts", *a], env=env, capture_output=True,  # noqa: E731
                                        text=True)
        r = run("add", "Anna Lee", "--telegram", "@annalee:100200300", "--alias", "Annie", "--source", "owner")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run("find", "annie")
        self.assertIn("@annalee", r.stdout)
        self.assertIn("100200300", r.stdout)
        r = run("find", "no such name")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ask the owner", r.stdout + r.stderr)
        self.assertIn("Anna Lee", run().stdout)
        with open(path) as f:
            self.assertEqual(json.load(f)[0]["name"], "Anna Lee")


GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "contacts-cli.json")


class Golden(unittest.TestCase):
    """The output of `hub contacts` stays byte for byte what it was before the hub bus (spec hubbus §5.5.1, p. 8): agents
    read it as text, and the teams agent and colleagues' scripts take addresses from it."""

    def test_output_is_byte_for_byte_the_reference(self):
        import datetime
        with open(GOLDEN, encoding="utf-8") as f:
            golden = json.load(f)
        path = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")
        env = {**os.environ, "HUB_CONTACTS": path, "PYTHONIOENCODING": "utf-8"}
        today = datetime.date.today().isoformat()
        for run in golden["runs"]:
            r = subprocess.run([sys.executable, HUB, "contacts", *run["args"]], env=env, capture_output=True)
            text = lambda b: b.decode("utf-8").replace("\r\n", "\n").replace(today, "<DATE>")   # noqa: E731 — CRLF of Windows
            got = {"rc": r.returncode, "out": text(r.stdout), "err": text(r.stderr)}
            self.assertEqual(got, {k: run[k] for k in ("rc", "out", "err")}, run["args"])
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.loads(f.read().replace(today, "<DATE>")), golden["book"])


class People(unittest.TestCase):
    """One tolerant reader of the book (spec hubbus §5.5.1, p. 8): find used to crash on the {"people": […]} form (the
    "Modules" counter reads it), on a non-dict entry and on aliases: null."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def test_odd_forms_do_not_crash_find_list_or_add(self):
        anna = {"name": "Anna Lee", "aliases": None, "telegram": {"username": "annalee", "id": 100200300},
                "source": "s", "confirmed": "2026-01-02"}
        for label, data in (("people", {"people": [anna]}), ("not a dict", [anna, "x", 5, None, {"aliases": []}]),
                            ("aliases null", [anna])):
            with self.subTest(label):
                self.write(data)
                self.assertEqual([r["name"] for r in contacts.people(self.path)], ["Anna Lee"])
                (hit,) = contacts.find(self.path, "annalee")
                self.assertEqual(hit["name"], "Anna Lee")
                env = {**os.environ, "HUB_CONTACTS": self.path, "PYTHONIOENCODING": "utf-8"}
                for args in ([], ["find", "anna"]):
                    r = subprocess.run([sys.executable, HUB, "contacts", *args], env=env, capture_output=True, text=True,
                                       encoding="utf-8")
                    self.assertEqual((r.returncode, r.stderr), (0, ""), args)
                    self.assertIn("Telegram @annalee id 100200300", r.stdout)
                contacts.add(self.path, "Anna Lee", email="a@example.org", source="s2")
                (hit,) = contacts.find(self.path, "annalee")
                self.assertEqual(hit["email"], "a@example.org")
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f)
                self.assertEqual(type(raw), type(data))   # the form of the file is kept

    def test_the_modules_tab_counts_with_the_same_reader(self):
        from unittest import mock
        from modules.web import modinfo
        self.write({"people": [{"name": "Anna Lee", "aliases": None}, "x", {"name": "Mark Cole"}]})
        with mock.patch.dict(os.environ, {"HUB_CONTACTS": self.path}):
            (m,) = [m for m in modinfo.modules(tempfile.mkdtemp(prefix="mi-"), {}, {}, lang="en") if m["id"] == "contacts"]
        self.assertIn("2", m["detail"])

    def test_duplicate_names_are_all_seen(self):
        self.write([{"name": "Anna Lee", "aliases": []}, {"name": "anna lee", "aliases": ["A2"]}])
        self.assertEqual(len(contacts.people(self.path)), 2)
        self.assertEqual(len(contacts.find(self.path, "anna lee")), 2)

    def test_add_keeps_id_and_unknown_fields(self):
        self.write([{"name": "Anna Lee", "aliases": [], "id": "c1a2b3c4", "x_future": {"a": 1}}])
        contacts.add(self.path, "anna lee", email="a@example.org", source="s")
        (row,) = contacts.load(self.path)
        self.assertEqual((row["id"], row["x_future"], row["email"]), ("c1a2b3c4", {"a": 1}, "a@example.org"))
        self.assertNotIn("c1a2b3c4", contacts.line(row))


class SessionsFillTheBook(unittest.TestCase):
    """Sessions record the addresses they see in their work (owner decision 2026-11-06: many books were empty). One person
    stays one record: a new channel goes into the record found by its name, an alias or a known address; a session adds
    what is missing and never replaces an address already written (a namesake's id would take a confirmed person's place —
    the reason the book exists); its new record says it is a session's, not the owner's confirmation."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")
        contacts.add(self.path, "Anna Lee", telegram="@annalee:100200300", aliases=["Annie"], source="owner, m90")

    def test_a_session_fills_in_the_record_of_this_name_or_alias(self):
        contacts.add(self.path, "Annie", email="anna@example.org", source="chat 19:x", by="maps")
        contacts.add(self.path, "Anna Lee", teams="ANNA@example.org", note="legal", source="chat", by="maps")
        (row,) = contacts.load(self.path)
        self.assertEqual((row["email"], row["teams"], row["note"]), ("anna@example.org", "ANNA@example.org", "legal"))
        self.assertEqual(row["source"], "owner, m90; chat 19:x; chat")   # the owner's source is kept; "chat" is its own
        self.assertNotIn("by", row)                                         # the record is still the owner's...
        self.assertEqual(row["seen"], {"email": "maps", "teams": "maps"})   # ...but these addresses are not his word
        self.assertIn("maps", contacts.line(row).split("@example.org")[1])
        contacts.add(self.path, "Anna Lee", email="ANNA@EXAMPLE.ORG", source="mail", by="maps")
        self.assertEqual(contacts.load(self.path)[0]["email"], "anna@example.org")   # the same address: not rewritten
        contacts.add(self.path, "Anna Lee", email="anna@example.org", source="owner, m91")
        self.assertEqual(contacts.load(self.path)[0]["seen"], {"teams": "maps"})   # his word on the e-mail only
        contacts.add(self.path, "Anna Lee", telegram="@anna_l:100200300", source="chat", by="maps")
        (row,) = contacts.load(self.path)
        self.assertEqual(row["telegram"], {"username": "anna_l", "id": 100200300})   # the same id, a new @username
        self.assertEqual(row["seen"]["telegram"], "maps")
        row["seen"] = "maps"   # a hand-edited book
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([row], f)
        contacts.add(self.path, "Anna Lee", teams_chat="19:abc@thread.v2", source="chat", by="maps")
        self.assertEqual(contacts.load(self.path)[0]["seen"], {"teams_chat": "maps"})

    def test_an_address_of_another_record_is_not_merged_by_a_guess(self):
        before = contacts.load(self.path)
        with self.assertRaises(ValueError) as e:   # a shared address: the session names the person the book has
            contacts.add(self.path, "A. Lee", telegram="@annalee:100200300", source="chat", by="maps")
        self.assertIn("Anna Lee", str(e.exception))
        contacts.add(self.path, "Mark Cole", email="mark@example.org", source="owner")
        with self.assertRaises(ValueError):   # an exact name, but the address is the other one's
            contacts.add(self.path, "Mark Cole", telegram="@annalee:100200300", source="chat", by="maps")
        self.assertEqual(len(contacts.load(self.path)), 2)
        self.assertEqual(contacts.load(self.path)[:1], before)
        contacts.add(self.path, "Anna Lee", telegram="@annalee:100200300", aliases=["A. Lee"], source="chat", by="maps")
        self.assertIn("A. Lee", contacts.load(self.path)[0]["aliases"])

    def test_the_owner_still_goes_by_the_exact_name(self):
        contacts.add(self.path, "Annie", telegram="@annie2:222", source="owner, m92")   # a new colleague, not Anna's alias
        self.assertEqual([r["name"] for r in contacts.load(self.path)], ["Anna Lee", "Annie"])
        self.assertEqual(contacts.load(self.path)[0]["telegram"]["id"], 100200300)

    def test_a_session_does_not_replace_an_address(self):
        before = contacts.load(self.path)
        with self.assertRaises(ValueError) as e:
            contacts.add(self.path, "Anna Lee", telegram="@anna_l:999", source="chat", by="maps")
        self.assertIn("@annalee id 100200300", str(e.exception))
        self.assertEqual(contacts.load(self.path), before)
        contacts.add(self.path, "Anna Lee", telegram="@anna_l:999", source="owner, m95")   # the owner may
        self.assertEqual(contacts.load(self.path)[0]["telegram"]["id"], 999)

    def test_nothing_new_writes_nothing(self):
        before = contacts.load(self.path)
        row = contacts.add(self.path, "Annie", telegram="@annalee:100200300", source="chat", by="maps")
        self.assertEqual(row["name"], "Anna Lee")
        self.assertEqual(contacts.load(self.path), before)   # not even the source or the date

    def test_a_sessions_record_is_marked_and_the_owner_confirms_it(self):
        contacts.add(self.path, "Mark Cole", email="mark@example.org", source="ticket thread", by="maps")
        mark = contacts.find(self.path, "mark")[0]
        self.assertEqual(mark["by"], "maps")
        self.assertIn("maps", contacts.line(mark))
        self.assertNotEqual(contacts.line(mark), contacts.line({k: v for k, v in mark.items() if k != "by"}))
        contacts.add(self.path, "Mark Cole", source="owner, m99")
        self.assertNotIn("by", contacts.find(self.path, "mark")[0])

    def test_a_name_of_two_records_is_refused(self):
        contacts.add(self.path, "Anna Kim", aliases=["Annie"], source="owner")
        with self.assertRaises(ValueError):
            contacts.add(self.path, "Annie", email="a@example.org", source="x", by="maps")

    def test_a_hand_edited_telegram_id_as_text_is_the_same_id(self):
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        raw[0]["telegram"]["id"] = "100200300"
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        row, changed = contacts.add_new(self.path, "Anna Lee", telegram="@annalee:100200300", source="x", by="maps")
        self.assertFalse(changed)

    def test_the_command_records_the_session(self):
        env = {**os.environ, "HUB_CONTACTS": self.path, "HUB_SESSION": "maps", "PYTHONIOENCODING": "utf-8"}
        run = lambda *a: subprocess.run([sys.executable, HUB, "contacts", *a], env=env, capture_output=True,  # noqa: E731
                                        text=True, encoding="utf-8")
        r = run("add", "Mark Cole", "--email", "mark@example.org", "--source", "ticket thread")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(contacts.find(self.path, "mark")[0]["by"], "maps")
        r = run("add", "Anna Lee", "--telegram", "@anna_l:999", "--source", "chat")
        self.assertEqual(r.returncode, 2)
        r = run("find", "nobody")
        self.assertEqual(r.returncode, 1)
        self.assertIn("hub contacts add", r.stdout)
        self.assertIn("--source", r.stdout)


class RenameRemove(unittest.TestCase):
    """The owner corrects the book himself: `hub contacts rename "<name>" "<new>"` keeps the record (its id, so the hubs
    linked to it) and `hub contacts remove "<name>"` drops it; a session does neither (a bug of the page once made a record
    named by a cut piece of a pasted link, and there was no way to put it right)."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")
        contacts.add(self.path, "Anna Lee", telegram="@annalee:100200300", aliases=["Annie"], source="owner")
        contacts.add(self.path, "Mark Cole", email="mark@example.org", source="owner")
        raw = contacts.load(self.path)
        raw[0]["id"] = "c1a2b3c4"
        raw += [{"name": "Twin", "aliases": [], "id": "c0000001"}, {"name": "twin", "aliases": [], "id": "c0000002"}]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        self.env = {**os.environ, "HUB_CONTACTS": self.path, "PYTHONIOENCODING": "utf-8"}
        self.env.pop("HUB_SESSION", None)

    def run_hub(self, *a, **env):
        return subprocess.run([sys.executable, HUB, "contacts", *a], env={**self.env, **env}, capture_output=True,
                              text=True, encoding="utf-8")

    def names(self):
        return [r["name"] for r in contacts.load(self.path)]

    def test_rename_keeps_the_record_and_its_id(self):
        before = contacts.load(self.path)[0]
        r = self.run_hub("rename", "annie", "  Anna   Lee-Smith ")   # by an alias, the new name tidied
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
        self.assertIn("Anna Lee-Smith", r.stdout)
        row = contacts.load(self.path)[0]
        self.assertEqual(row, {**before, "name": "Anna Lee-Smith"})   # nothing else moved: id, address, source, aliases
        self.assertEqual(self.run_hub("rename", "c1a2b3c4", "Anna L").returncode, 0)   # by the id as well
        self.assertEqual(self.names(), ["Anna L", "Mark Cole", "Twin", "twin"])
        self.assertEqual(contacts.find(self.path, "annie")[0]["name"], "Anna L")

    def test_rename_to_an_own_alias_and_to_a_case_only_change(self):
        self.assertEqual(self.run_hub("rename", "Anna Lee", "Annie").returncode, 0)
        (row,) = contacts.find(self.path, "annie")
        self.assertEqual((row["name"], row["aliases"], row["id"]), ("Annie", [], "c1a2b3c4"))
        self.assertEqual(self.run_hub("rename", "Mark Cole", "MARK COLE").returncode, 0)
        self.assertEqual(self.names()[1], "MARK COLE")

    def test_rename_refusals_change_nothing(self):
        raw = open(self.path, "rb").read()
        link = "hub-link-1:" + "A" * 40
        for args, what in ((("rename", "Anna Lee", "Mark Cole"), "a name of another record"),
                           (("rename", "Anna Lee", "mark cole"), "the same in another case"),
                           (("rename", "Anna Lee", ""), "empty"), (("rename", "Anna Lee", "   "), "blank"),
                           (("rename", "Anna Lee", "Ann\x07a"), "a control character"),
                           (("rename", "Anna Lee", "N" * 101), "longer than a name of the bus"),
                           (("rename", "Anna Lee", link), "a link"),
                           (("rename", "Anna Lee", "Hub-Relay-Invite-1:abc.def"), "an invitation"),
                           (("rename", "Anna Lee", "x hub-bus-card-1:AAAA"), "a card"),
                           (("rename", "nobody", "New"), "no such record"),
                           (("rename", "twin", "Twin Two"), "two records"),
                           (("rename", "Anna Lee"), "no new name"), (("rename", "a", "b", "c"), "too many")):
            r = self.run_hub(*args)
            self.assertNotEqual(r.returncode, 0, what)
            self.assertEqual(open(self.path, "rb").read(), raw, what)
        r = self.run_hub("rename", "twin", "Twin Two")
        self.assertIn("c0000001", r.stdout + r.stderr)   # the list tells the records apart
        self.assertEqual(self.run_hub("rename", "c0000002", "Twin Two").returncode, 0)

    def test_remove(self):
        r = self.run_hub("remove", "Mark Cole")
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
        self.assertEqual(self.names(), ["Anna Lee", "Twin", "twin"])
        raw = open(self.path, "rb").read()
        for args, what in ((("remove", "twin"), "two records"), (("remove", "nobody"), "no such record"),
                           (("remove",), "no name"), (("remove", "a", "b"), "too many")):
            r = self.run_hub(*args)
            self.assertNotEqual(r.returncode, 0, what)
            self.assertEqual(open(self.path, "rb").read(), raw, what)
        self.assertIn("c0000001", self.run_hub("remove", "twin").stdout)
        self.assertEqual(self.run_hub("remove", "c0000001").returncode, 0)
        self.assertEqual(self.run_hub("remove", "annie").returncode, 0)   # by an alias, one record
        self.assertEqual(self.names(), ["twin"])

    def test_a_session_may_neither_rename_nor_remove(self):
        raw = open(self.path, "rb").read()
        for args in (("rename", "Anna Lee", "Anna X"), ("remove", "Anna Lee")):
            r = self.run_hub(*args, HUB_SESSION="maps")
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("owner", r.stdout + r.stderr)
            self.assertEqual(open(self.path, "rb").read(), raw, args)

    def test_a_form_of_the_file_with_people_is_kept(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"people": [{"name": "Anna Lee", "aliases": []}, {"name": "Mark Cole"}], "x": 1}, f)
        self.assertEqual(self.run_hub("rename", "Mark Cole", "Mark C").returncode, 0)
        self.assertEqual(self.run_hub("remove", "Anna Lee").returncode, 0)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"people": [{"name": "Mark C"}], "x": 1})


class LockedWrite(unittest.TestCase):
    """The book's writer (spec hubbus §5.5.1, p. 3): read → change → write under core.lock.exclusive, a temp file of its
    own. `add` used to write without a lock through a fixed `<path>.tmp`: of two writers at once one change was lost."""

    def race(self, first, second, lock=True):
        import contextlib
        import threading
        from unittest import mock
        path = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")
        contacts.add(path, "Anna Lee", source="s")
        read, go = threading.Event(), threading.Event()

        def slow(rows):   # the first writer has read the book and stops before writing
            first(rows)
            read.set()
            go.wait(5)
        patch = mock.patch("core.lock.exclusive", lambda p: contextlib.nullcontext()) if not lock else \
            contextlib.nullcontext()
        with patch:
            t = threading.Thread(target=contacts.update, args=(path, slow))
            t.start()
            read.wait(5)
            t2 = threading.Thread(target=contacts.update, args=(path, second))
            t2.start()
            t2.join(0.5)        # with the lock it waits for the first writer
            go.set()
            t.join(5)
            t2.join(5)
        return {r["name"]: r for r in contacts.load(path)}

    @staticmethod
    def give_id(rows):
        rows[0]["id"] = "c1a2b3c4"

    @staticmethod
    def add_mark(rows):
        rows.append({"name": "Mark Cole", "aliases": [], "email": "m@example.org"})

    def test_both_orders_keep_both_changes(self):
        for first, second in ((self.give_id, self.add_mark), (self.add_mark, self.give_id)):
            book = self.race(first, second)
            self.assertEqual(book["Anna Lee"].get("id"), "c1a2b3c4")
            self.assertEqual(book["Mark Cole"]["email"], "m@example.org")

    def test_without_the_lock_a_change_is_lost(self):
        # the scenario above does catch a lost write: with the lock taken away one of the two changes is gone
        book = self.race(self.give_id, self.add_mark, lock=False)
        self.assertFalse(book["Anna Lee"].get("id") == "c1a2b3c4" and "Mark Cole" in book)

    def test_each_writer_has_its_own_temp_file(self):
        path = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")
        os.makedirs(path + ".tmp")            # the old fixed temp name is taken: a writer of its own does not care
        contacts.add(path, "Anna Lee", source="s")
        self.assertEqual([r["name"] for r in contacts.load(path)], ["Anna Lee"])


if __name__ == "__main__":
    unittest.main()
