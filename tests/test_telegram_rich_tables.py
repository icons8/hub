"""telegram.rich_results (off by default): a session's result goes as one rich message (sendRichMessage: a real table, a
long text as a gist and <details>); buttons, too wide/long, a refusal — the ordinary HTML path, without a duplicate."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import unittest
import unittest.mock

from modules.telegram import fmt, rich
from tests import test_telegram as base
from tests import test_telegram_group as group

TABLE = "| Name | Size |\n|---|---|\n| **a** | 1 |\n| [b](https://example.com) | 2 |"
TEXT = "**Done**\n\n" + TABLE + "\n\nend"


class RichResults(unittest.TestCase):
    setUp, tearDown = base.TelegramTest.setUp, base.TelegramTest.tearDown
    msg, spooled, ingest = base.TelegramTest.msg, base.TelegramTest.spooled, base.TelegramTest.ingest

    def result(self, text, on=True, **cfg):
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True, **cfg}} if on else {}
        self.fake.updates = [self.msg("hello", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("turn.result", "hub", {"text": text})
        self.bot.tick()

    def test_a_table_goes_as_one_rich_message_with_reply_and_without_html(self):
        self.result(TEXT)
        self.assertEqual(self.fake.sent(), [])
        (rich,) = self.fake.sent("sendRichMessage")
        self.assertIn("| **a** | 1 |", rich["rich_message"]["markdown"])
        self.assertIn("[b](https://example.com)", rich["rich_message"]["markdown"])
        self.assertIn("\nend", rich["rich_message"]["markdown"])
        self.assertEqual(rich["reply_parameters"]["message_id"], 5)
        self.assertEqual(rich["chat_id"], base.OWNER)

    def test_the_head_is_kept_as_markdown(self):
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True}}
        self.bot.send(TEXT, head="<b>w</b>:", fold=True)
        self.assertTrue(self.fake.sent("sendRichMessage")[0]["rich_message"]["markdown"].startswith("**w**:\n"))

    def test_without_the_option_it_is_as_before(self):
        self.result(TEXT, on=False)
        self.assertEqual(self.fake.sent("sendRichMessage"), [])
        self.assertIn("<pre>", self.fake.sent()[0]["text"])
        self.fake.calls.clear()

    def test_a_refusal_falls_back_once_without_a_duplicate(self):
        self.fake.fail["sendRichMessage"] = [400]
        self.result(TEXT)
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertIn("<pre>", self.fake.sent()[0]["text"])
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), 1)

    def test_too_wide_or_too_long_goes_the_old_way(self):
        wide = "|" + "a|" * 21 + "\n|" + "-|" * 21 + "\n|" + "1|" * 21
        self.result(wide)
        self.assertEqual(self.fake.sent("sendRichMessage"), [])
        self.assertEqual(len(self.fake.sent()), 1)
        self.fake.calls.clear()
        self.result(TABLE + "\n\n" + "x" * 33000)
        self.assertEqual(self.fake.sent("sendRichMessage"), [])
        self.assertTrue(self.fake.sent())

    def test_buttons_keep_the_ordinary_path(self):
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True}}
        self.bot.send(TABLE, fold=True, buttons=[[{"text": "ok", "callback_data": "x"}]])
        self.assertEqual(self.fake.sent("sendRichMessage"), [])
        self.assertEqual(len(self.fake.sent()), 1)

    def test_a_table_in_a_code_block_is_not_a_table(self):
        self.assertEqual(fmt.tables("```\n" + TABLE + "\n```"), [])
        self.assertEqual(fmt.tables(TEXT), [2])

    def test_file_links_in_code_become_markdown_links(self):
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True}}
        self.bot.files_url = lambda: "http://hub"
        import os
        p = os.path.join(self.dir, "spec.md")
        open(p, "w").close()
        self.bot.send("see `spec.md`\n\n" + TABLE, cwd=self.dir, fold=True)
        md = self.fake.sent("sendRichMessage")[0]["rich_message"]["markdown"]
        self.assertIn("[`spec.md`](http://hub", md)
        self.assertNotIn("<a", md)

    def test_a_plain_result_goes_rich_too_and_a_system_message_does_not(self):
        self.result("**Done**: finished")
        self.assertEqual(self.fake.sent(), [])
        (rich,) = self.fake.sent("sendRichMessage")
        self.assertEqual(rich["rich_message"]["markdown"], "**Done**: finished")
        self.fake.calls.clear()
        self.bot.send("status")   # not the session's words (fold unset): HTML as before
        self.assertEqual(self.fake.sent("sendRichMessage"), [])
        self.assertEqual(self.fake.sent()[0]["text"], "status")

    def test_a_long_result_keeps_the_gist_and_folds_the_rest_in_details(self):
        body = "\n".join(f"line {i} of the report" for i in range(12))
        self.result("Gist line\n" + body + "\n\n" + TABLE, fold_long=True)
        md = self.fake.sent("sendRichMessage")[0]["rich_message"]["markdown"]
        self.assertTrue(md.startswith("Gist line\n\n<details><summary>More</summary>\n\nline 0"))
        self.assertTrue(md.endswith("| [b](https://example.com) | 2 |\n\n</details>"))

    def test_a_long_result_is_not_folded_without_fold_long(self):
        # folding is telegram.fold_long's, as in the HTML parts: rich_results alone does not fold
        body = "\n".join(f"line {i} of the report" for i in range(12))
        self.result("Gist line\n" + body)
        md = self.fake.sent("sendRichMessage")[0]["rich_message"]["markdown"]
        self.assertNotIn("<details>", md)
        self.assertTrue(md.startswith("Gist line  \nline 0 of the report  \n"))

    def test_over_500_blocks_goes_the_old_way(self):
        self.result("\n\n".join(f"p{i}" for i in range(600)))
        self.assertEqual(self.fake.sent("sendRichMessage"), [])
        self.assertTrue(self.fake.sent())

    def test_a_refused_plain_result_falls_back_too(self):
        self.fake.fail["sendRichMessage"] = [400]
        self.result("**Done**")
        self.assertEqual([m["text"] for m in self.fake.sent()], ["<b>Done</b>"])

    def test_lines_stay_lines_and_the_head_is_a_paragraph_of_its_own(self):
        # Markdown joins single line breaks into one paragraph: the HTML parts show every line on its own
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True}}
        self.bot.send("Done:\nline one\nline two\n\n- a\n- b\n\n> q1\n> q2", head="<b>maps</b>:", fold=True)
        md = self.fake.sent("sendRichMessage")[0]["rich_message"]["markdown"]
        self.assertEqual(md, "**maps**:\n\nDone:  \nline one  \nline two\n\n- a\n- b\n\n> q1  \n> q2")

    def test_a_table_is_set_apart_from_the_lines_around_it(self):
        # a line right after a table would be its row; one right before it — its header
        self.result("intro\n| a | b |\n|---|---|\n| 1 | 2 |\nafter")
        md = self.fake.sent("sendRichMessage")[0]["rich_message"]["markdown"]
        self.assertEqual(md, "intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nafter")

    def test_what_only_rich_markdown_reads_as_markup_stays_text(self):
        # $…$ is a formula, ==…== marked, <tag> HTML in rich Markdown; the HTML parts show them as typed
        self.result("costs $5 and $10, a == b, put <path> here; `$x <y>` and\n```\n$z <w>\n```")
        md = self.fake.sent("sendRichMessage")[0]["rich_message"]["markdown"]
        self.assertEqual(md, "costs \\$5 and \\$10, a \\=\\= b, put \\<path> here; `$x <y>` and\n```\n$z <w>\n```")

    def test_the_head_is_escaped(self):
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True}}
        self.bot.send("ok", head="<b>maps_1</b> · *x* &lt;y&gt;:", fold=True)
        md = self.fake.sent("sendRichMessage")[0]["rich_message"]["markdown"]
        self.assertTrue(md.startswith("**maps\\_1** · \\*x\\* \\<y\\>:\n\n"), md)

    def test_plain_paths_and_task_keys_are_linked_as_in_the_html_parts(self):
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True},
                                       "jira": {"url": "https://jira.example.test", "projects": ["ABC"]}}
        self.bot.files_url = lambda: "http://hub"
        open(os.path.join(self.dir, "plan.md"), "w").close()
        self.bot.send("see ./plan.md for ABC-123 and `ABC-7`", cwd=self.dir, fold=True)
        md = self.fake.sent("sendRichMessage")[0]["rich_message"]["markdown"]
        self.assertIn("[./plan.md](http://hub/open?path=", md)
        self.assertIn("[ABC-123](https://jira.example.test/browse/ABC-123)", md)
        self.assertIn("[`ABC-7`](https://jira.example.test/browse/ABC-7)", md)

    def test_a_repeat_of_the_event_does_not_send_it_again(self):
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True}}
        self.bot.sending = 1
        first = self.bot.send(TEXT, fold=True, reply_to=5)
        self.bot.attempt = {}   # the event fails further on and goes again (tick does the same)
        again = self.bot.send(TEXT, fold=True, reply_to=5)
        self.bot.sending = None
        self.assertEqual((first, again, len(self.fake.sent("sendRichMessage")), self.fake.sent()), (first, first, 1, []))

    def test_a_flood_wait_repeats_the_event_without_html(self):
        self.fake.fail["sendRichMessage"] = [429]
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True}}
        with self.assertRaises(base.tg.TgError):
            self.bot.send(TEXT, fold=True)
        self.assertEqual(self.fake.sent(), [])
        self.bot.send(TEXT, fold=True)
        self.assertEqual((len(self.fake.sent("sendRichMessage")), self.fake.sent()), (1, []))

    def test_a_reply_to_a_rich_message_quotes_its_text(self):
        # a rich message comes back without text: its blocks are the text of the quote
        m = {"reply_to_message": {"message_id": 9, "rich_message": {"blocks": [
            {"type": "paragraph", "text": [{"type": "bold", "text": "maps"}, ":"]},
            {"type": "paragraph", "text": "Done"},
            {"type": "table", "cells": [[{"text": "a"}, {"text": "1"}]]}]}}}
        self.assertEqual(self.bot.quote(m, {"session": "maps", "head": "maps:"}), "Done\na | 1")
        m["quote"] = {"text": "", "rich_texts": ["Do", {"type": "bold", "text": "ne"}]}
        self.assertEqual(self.bot.quote(m, {"session": "maps"}), "Done")

    def test_plain_text_of_a_rich_message(self):
        self.assertEqual(rich.plain({"blocks": [
            {"type": "details", "summary": "More", "blocks": [{"type": "list", "items": [
                {"blocks": [{"type": "paragraph", "text": "x"}]}, {"blocks": [{"type": "paragraph", "text": "y"}]}]}]}]}),
            "More\nx\ny")


    def test_escapes_of_the_session_and_web_addresses_stay_as_written(self):
        self.assertEqual(rich.words("costs \\$5, see https://x.example/a?sig=ab== and $y"),
                         "costs \\$5, see https://x.example/a?sig=ab== and \\$y")

    def test_a_line_that_markdown_would_take_into_the_paragraph_keeps_its_own_line(self):
        # a numbered list interrupts a paragraph only from 1; a "|" line without a table is text; "---" under text a heading
        self.assertEqual(rich.words("Status\n2024. was good\n| no table\ntext\n---\n1. a\n2. b"),
                         "Status  \n2024. was good  \n| no table  \ntext\n\n---\n1. a  \n2. b")

    def test_a_dash_or_equals_line_under_text_stays_a_line(self):
        # under a line of text "=" or "-" would make it a heading; alone after a blank line "-" is an empty list item
        self.assertEqual(rich.words("Total\n=\nvalue\n-\nftp://h/$a$"), "Total  \n\\=  \nvalue  \n\\-  \nftp://h/\\$a\\$")

    def test_a_tilde_fence_at_the_start_is_not_folded_as_a_gist(self):
        r, _ = rich.result("~~~\n" + "\n".join(f"l{i}" for i in range(20)) + "\n~~~", more="More")
        self.assertNotIn("<details>", r.text)

    def test_a_text_the_conversion_breaks_on_goes_as_html(self):
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True}}
        with unittest.mock.patch.object(rich, "words", side_effect=ValueError("odd")):
            self.bot.send(TEXT, fold=True)
        self.assertEqual((self.fake.sent("sendRichMessage"), len(self.fake.sent())), ([], 1))


class RichInTopics(unittest.TestCase):
    T, G = base.TelegramTest, group.Group
    setUp, tearDown, spooled, ingest = G.setUp, T.tearDown, T.spooled, T.ingest

    def test_a_deleted_topic_sends_it_to_general_and_a_reply_goes_there(self):
        self.bot.hub_config = lambda: {"telegram": {"rich_results": True}}
        self.bot.st["topics"] = {"9": "maps"}
        self.fake.fail["sendRichMessage"] = [(400, "Bad Request: message thread not found")]
        (mid,) = self.bot.send(TEXT, route={"session": "maps"}, fold=True)
        (rich_sent,) = self.fake.sent("sendRichMessage")
        self.assertNotIn("message_thread_id", rich_sent)
        self.assertEqual(self.fake.texts(), [base.tg.i18n.t("telegram.group.topic_gone", name="maps")])
        self.assertIsNone(self.bot.st["topic_of"][str(mid)])
