"""Jira links (handle Jira links properly): a task key of a known project (ABC-1234) in a
session's text becomes a link to the task, and a bare task URL is shortened to its key. UTF-8, SHA-256 and keys of
other projects are left alone; so are code, pre and ready-made links."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from modules.jira import links
from modules.telegram import fmt

CFG = {"jira": {"url": "https://acme.atlassian.net/", "projects": ["ABC", "XYZ"]}}
LINK = '<a href="https://acme.atlassian.net/browse/ABC-1747">ABC-1747</a>'


class JiraLinks(unittest.TestCase):
    def test_settings(self):
        self.assertEqual(links.settings(CFG), {"url": "https://acme.atlassian.net", "projects": ["ABC", "XYZ"]})
        self.assertIsNone(links.settings({}))
        self.assertIsNone(links.settings({"jira": {"url": "https://acme.atlassian.net"}}))  # no projects: off

    def test_keys_become_links(self):
        html_in = fmt.to_messages("Done ABC-1747 and XYZ-5; UTF-8, SHA-256, DEF-1, feat/ABC-1747")[0]
        out = links.link_html(html_in, CFG)
        self.assertIn(f"Done {LINK} and ", out)
        self.assertIn('<a href="https://acme.atlassian.net/browse/XYZ-5">XYZ-5</a>', out)
        self.assertIn("UTF-8, SHA-256, DEF-1, feat/ABC-1747", out)  # foreign keys and a part of a branch path stay as they were

    def test_code_pre_and_links_untouched_except_bare_key(self):
        md = "`ABC-1747` and `git log ABC-1747`\n\n```\nABC-1747\n```\n[task](https://x.org/ABC-1747)"
        out = links.link_html(fmt.to_messages(md)[0], CFG)
        self.assertIn(f'<a href="https://acme.atlassian.net/browse/ABC-1747"><code>ABC-1747</code></a>', out)
        self.assertIn("<code>git log ABC-1747</code>", out)
        self.assertIn("<pre>ABC-1747</pre>", out)
        self.assertIn('<a href="https://x.org/ABC-1747">task</a>', out)

    def test_bare_url_shortened(self):
        out = links.link_html(fmt.to_messages("see https://acme.atlassian.net/browse/ABC-1747.")[0], CFG)
        self.assertEqual(out, f"see {LINK}.")
        out = links.link_html(fmt.to_messages("https://acme.atlassian.net/browse/ABC-1747?focusedCommentId=1")[0], CFG)
        self.assertIn('href="https://acme.atlassian.net/browse/ABC-1747?focusedCommentId=1">ABC-1747</a>', out)

    def test_off_without_settings(self):
        html_in = fmt.to_messages("ABC-1747")[0]
        self.assertEqual(links.link_html(html_in, {}), html_in)


if __name__ == "__main__":
    unittest.main()
