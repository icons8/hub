"""Repository presentation for going public: README (English), SECURITY, CONTRIBUTING, CODE_OF_CONDUCT and the
screenshots in media/. The tests keep the presentation honest: links and images are found in the snapshot (publishing
strips docs/), the commands and installers named exist in the code, the texts and screenshots hold nothing personal,
and SECURITY does not promise what the hub does not do."""
import glob
import hashlib
import importlib.util
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest

try:
    import yaml   # not in the standard library: the structure checks of the issue forms skip without it
except ImportError:   # pragma: no cover - depends on the machine
    yaml = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PR_TEMPLATE = ".github/PULL_REQUEST_TEMPLATE.md"   # GitHub fills a new pull request's description with it
FORMS = ".github/ISSUE_TEMPLATE"   # GitHub builds the "new issue" chooser from these: forms + config.yml
FORM_FILES = [FORMS + "/bug.yml", FORMS + "/feature.yml"]
DOCS = ["README.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "CONTRIBUTORS.md", PR_TEMPLATE]
PORTABILITY = "Portability: tests run on other machines"   # the CONTRIBUTING section the PR template names
MEDIA = os.path.join(ROOT, "media")
# the public repository and its GitHub Pages site (the one-line install): README, CONTRIBUTING, SECURITY and the issue
# chooser name them already, before the repository opens (the owner's decision) — no other address of the organization
REPO_SLUG = "icons8/hub"
PAGES = "https://icons8.github.io/hub/"
# what media/ must hold (name -> size) — anything else in the folder is extra
SHOTS = {"hero.png": (2560, 1600), "social-preview.png": (2560, 1280), "feed.png": (2560, 1600), "now.png": (2560, 1600),
         "telegram.png": (2560, 1600)}   # @2x frames, one size (16:10) except the GitHub social preview
CYRILLIC = re.compile(r"[\u0410-\u044f\u0401\u0451]")


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "tools", "dev", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def deny_forms():
    return load_tool("readme_demo").deny_forms()   # with the same loader as publish.sh


def public_out(line):
    """The line without the addresses of the public repository and its Pages site, and without the product's name."""
    line = re.sub(r"</?span>", "", line)   # the name may be split into colours in a template: Hub <span>by Icons8</span>
    return line.replace(PAGES.rstrip("/"), "").replace(REPO_SLUG, "").replace("Hub by Icons8", "")


def slug(heading):
    """A heading anchor as GitHub builds it: lowercase, no punctuation, spaces become hyphens."""
    s = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return s.replace(" ", "-")


def anchors(text):
    return {slug(m.group(2)) for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.M)}


def png_chunks(path):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    pos, out = 8, []
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        out.append((kind.decode(), data[pos + 8:pos + 8 + n]))
        pos += 12 + n
    return out


class Docs(unittest.TestCase):
    def test_all_files_exist(self):
        for name in DOCS:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, name)), f"no {name}")

    def test_readme_is_english_only(self):
        en = read("README.md")
        self.assertIsNone(CYRILLIC.search(en), "README.md is English, no Russian")
        self.assertFalse(os.path.exists(os.path.join(ROOT, "README.ru.md")), "there is no Russian README")

    def test_first_screen_order(self):
        # the name and the phrase come above the picture: a picture above the heading pushes the heading and badges
        # below the fold
        en = read("README.md")
        self.assertIn("Run a team of Claude Code or Codex agents from your browser and your phone.", en)
        head = en.index("Hub by Icons8")
        self.assertLess(head, en.index("media/hero.png"))
        self.assertLess(en.index("media/hero.png"), en.index("## Quick start"))
        self.assertLess(en.index("## Quick start"), en.index("## Security"))
        # the caveats banner comes before installation, the details after it
        note = en.index("Experimental")
        self.assertLess(note, en.index("## Quick start"))
        self.assertLess(len(en[note:en.index("## Quick start")].splitlines()), 12)

    def test_links_and_images_resolve_inside_the_snapshot(self):
        for name in DOCS:
            text = read(name)
            here = anchors(text)
            targets = re.findall(r"\]\(([^)\s]+)", text) + re.findall(r'(?:href|src)="([^"]+)"', text)
            for t in targets:
                if re.match(r"(https?:|mailto:)", t):
                    continue
                path, _, frag = t.partition("#")
                if not path:
                    self.assertIn(frag, here, f"{name}: no anchor #{frag}")
                    continue
                self.assertFalse(path.startswith("docs/"), f"{name}: {t} — publishing strips docs/")
                full = os.path.join(ROOT, path)
                self.assertTrue(os.path.exists(full), f"{name}: no {path}")
                if frag and path.endswith(".md"):
                    self.assertIn(frag, anchors(read(path)), f"{name}: no anchor {t}")

    def test_every_image_has_alt_text(self):
        for name in DOCS:
            text = read(name)
            for m in re.finditer(r"!\[([^\]]*)\]\(", text):
                self.assertTrue(m.group(1).strip(), f"{name}: image without alt")
            for m in re.finditer(r"<img\b[^>]*>", text):
                alt = re.search(r'alt="([^"]*)"', m.group(0))
                self.assertTrue(alt and alt.group(1).strip(), f"{name}: {m.group(0)[:60]} without alt")

    def test_no_private_forms_and_no_placeholders(self):
        forms = deny_forms()
        self.assertTrue(forms)
        for name in DOCS + ["tools/dev/readme_demo.py"] + [f"tools/dev/readme_media/{n}" for n in
                                                          sorted(os.listdir(os.path.join(ROOT, "tools", "dev", "readme_media")))
                                                          if n.endswith(".html")]:
            for n, line in enumerate(read(name).splitlines(), 1):
                for f in forms:
                    self.assertIsNone(f.search(line), f"{name}:{n}: forbidden form {f.pattern!r}")
                self.assertNotRegex(line, r"<set before|TODO|FIXME|XXX|lorem", f"{name}:{n}")
                self.assertNotRegex(public_out(line), r"(?i)icons8", f"{name}:{n}: organization name")

    def test_commands_installers_and_modules_named_in_readme_exist(self):
        hub = read("bin/hub") + read("core/hubhelp.txt")   # the help of every command is in core/hubhelp.txt
        for name in ("README.md",):
            text = read(name)
            for cmd in set(re.findall(r"`hub ([a-z]+)", text)):
                self.assertTrue(re.search(rf"(^|\s|\||\")hub {cmd}\b|cmd == \"{cmd}\"", hub), f"{name}: no command hub {cmd}")
            for path in set(re.findall(r"`((?:deploy|tools|bin|core|hosts)/[\w./\\-]+)`", text.replace("\\", "/"))):
                self.assertTrue(os.path.exists(os.path.join(ROOT, path)), f"{name}: no {path}")
            self.assertIn("127.0.0.1:8787", text)   # the page's default port — as in the code
            for mod in set(re.findall(r"`modules/([a-z_]+)`", text)):
                self.assertTrue(os.path.isdir(os.path.join(ROOT, "modules", mod)), f"{name}: no module {mod}")
        self.assertIn("8787", read("modules/web/server.py"))
        self.assertTrue(os.path.exists(os.path.join(ROOT, "bin", "hub-telegram")))

    def test_security_is_honest(self):
        text = read("SECURITY.md")
        low = text.lower()
        for need in ("bypasspermissions", "not a sandbox", "private vulnerability reporting", "same os user",
                     "no telemetry", "hub journal scrub"):
            self.assertIn(need, low)
        self.assertNotRegex(text, r"@[\w-]+\.(com|org|net|ru)", "vulnerabilities — only through private GitHub messages")
        # releases are not signed (decision 17.10): any "signed/signature" appears only with a negation
        for sentence in re.split(r"(?<=[.!?])\s+|\n\n", text):
            if re.search(r"(?i)\bsign(ed(?!-in)|ature)", sentence):
                self.assertRegex(sentence, r"(?i)\b(no|not|never|without|unsigned)\b", sentence[:80])
        # there is no page login by a one-time link (decision 16.10)
        self.assertNotRegex(low, r"one-time link|web-secret|release-signers")
        # leftovers of the OSS security review: a harmful release, the main channel, the key mask, external copies,
        # the sender is not a boundary
        for need in ("nightly", "time machine", "narrow", "hub_session"):
            self.assertIn(need, low)

    def test_security_says_what_is_on_from_the_start_and_what_it_trusts(self):
        """The external review before going public: SECURITY and README describe how the hub works now — what it
        reaches and listens on by itself, the channels it has, what it trusts — and each named setting is the code's."""
        sec, readme = read("SECURITY.md"), read("README.md")
        low = sec.lower()
        # Tailscale: the switch the docs name turns the module off without asking tailscale anything
        from modules.tailnet import access, run
        with tempfile.TemporaryDirectory() as d:
            run.Tailnet(d, config=lambda: {"tailnet": {"enabled": False}}, cli=["/nonexistent/tailscale"]).tick()
            self.assertFalse(access.status(d).get("ok"))
        off = '"tailnet": {"enabled": false}'
        self.assertIn(off, sec)
        self.assertIn(off, readme)
        self.assertNotIn("remote access and anything that sends data out are off", low)
        self.assertNotIn("Nothing of the hub listens outside the machine", readme)
        # the channels: Telegram's private chat, group and chats with people; the hub bus and its relay
        for need in ("telegram group", "chat automation", "hub bus on", "relay", "liaison", "hub bus mode remote"):
            self.assertIn(need, low)
        # the agents' Chrome: its DevTools port on 127.0.0.1 — the default and the macOS installer's, as the docs name them
        from modules.browser import browsers
        self.assertEqual(browsers.configured({"browsers": {"x": {"kind": "local"}}})["x"]["port"], 9230)
        self.assertIn("127.0.0.1:9230", sec)
        self.assertIn('"mac": {"kind": "local", "port": 9223', read("deploy/mac/install.sh"))
        self.assertIn("127.0.0.1:9223", sec)
        # packages trust the pages and the release files: no key beside them
        self.assertIn("github pages", low)
        # the session's folder is code that runs: its hooks and MCP servers
        for need in (".claude/settings.json", ".mcp.json", "trusted code"):
            self.assertIn(need, low)
        # an HTML report of "Files": its scripts run, may reach the network, and its mark opens its folder
        from modules.files import server as files
        self.assertIn("allow-scripts", files.RENDER_CSP)
        self.assertNotIn("connect-src", files.RENDER_CSP)
        self.assertIn("html report", low)
        # the vault: "Copy" hands the value to the browser — the page does show it then
        self.assertNotIn("The page never shows a value", sec)
        from modules.web import secretsweb
        self.assertEqual(secretsweb.VALUES_VIA, ("local", "tailnet"))   # "Copy" from this machine or the tailnet only
        self.assertRegex(low, r"\bcopy\b[^.]*\bvalue\b")
        # the catalogs a hub connects by itself are named where README says what leaves the machine
        leaves = next(ln for ln in readme.splitlines() if ln.startswith("| **What leaves the machine**"))
        self.assertIn("the project's own catalog", leaves)   # its slug names the organization (no_private_forms)
        # the sources of marketplaces.BUILTIN (empty here: the tests run with HUB_NO_OFFICIAL_MARKETPLACE)
        self.assertIn('"openai-curated": "openai/plugins"}', read("core/marketplaces.py"))
        for source in ("anthropics/claude-plugins-official", "openai/plugins"):
            self.assertIn(source, leaves)

    def test_tailscale_access_is_off_until_turned_on_as_the_docs_say(self):
        """SECURITY and README say Tailscale access is off until the owner turns it on: a hub with nothing in hub.json
        and no earlier record does not ask tailscale and stays off."""
        from modules.tailnet import access, run
        with tempfile.TemporaryDirectory() as d:
            run.Tailnet(d, config=lambda: {}, cli=["/nonexistent/tailscale"]).tick()
            self.assertIs(access.status(d).get("off"), True)
        self.assertIn("Remote access over Tailscale (off until you turn it on)", read("SECURITY.md"))
        self.assertIn('"tailnet": {"enabled": true}', read("README.md"))

    def test_tailscale_carry_over_works_as_the_docs_say(self):
        """SECURITY says how an update from an earlier release carries Tailscale access over: where it worked, the key is
        written once; installed but not connected, the choice waits; no Tailscale, off for good."""
        from modules.tailnet import run
        missing, stopped = ["/nonexistent/tailscale"], [sys.executable, "-c", 'print(\'{"BackendState": "Stopped"}\')']

        def updated(earlier, *clis):
            """An earlier release's record and an empty hub.json, then a tick per CLI -> (the last record, hub.json)."""
            with tempfile.TemporaryDirectory() as d:
                hub_json = os.path.join(d, "hub.json")
                with open(hub_json, "w") as f:
                    json.dump({}, f)
                with open(os.path.join(d, "tailnet.json"), "w") as f:
                    json.dump(earlier, f)

                def config():
                    with open(hub_json) as f:
                        return json.load(f)
                for cli in clis:
                    rec = run.Tailnet(d, config=config, cli=cli, settings=hub_json).tick()
                return rec, config()

        self.assertEqual(updated({"ok": True, "ts": 1.0}, missing)[1], {"tailnet": {"enabled": True}})   # it worked
        rec, cfg = updated({"ok": False, "why": "x", "ts": 1.0}, stopped)   # installed, not connected: not decided
        self.assertTrue(rec.get("down"))
        self.assertEqual(cfg, {})
        failing = [sys.executable, "-c", "raise SystemExit(1)"]   # installed, its command fails now: not decided either
        self.assertTrue(updated({"ok": False, "why": "x", "ts": 1.0}, failing)[0].get("down"))
        rec, cfg = updated({"ok": False, "why": "x", "ts": 1.0}, missing, stopped)   # no Tailscale: off, and it stays
        self.assertIs(rec.get("off"), True)
        self.assertEqual(cfg, {})
        sec = " ".join(read("SECURITY.md").split())
        self.assertIn("the choice waits until it connects", sec)
        self.assertIn("stays off and does not turn itself on again", sec)

    def test_scheduled_commands_and_new_groups_work_as_the_docs_say(self):
        """SECURITY says a scheduled command speaks as the session that wrote it, and that a bound bot takes another
        Telegram group only by the owner's press (a card, not a code)."""
        from core import schedule
        from modules.telegram import bot
        self.assertEqual(schedule.speaker_of({"command": "true", "by": "maps"}), ("session", "maps"))   # an older job
        self.assertEqual(schedule.speaker_of({"command": "true", "command_by": {"session": "maps"}}), ("session", "maps"))
        self.assertEqual(schedule.speaker_of({"command": "true", "command_by": {"plugin": "ext:x"}}), ("plugin", "ext:x"))
        self.assertEqual(schedule.speaker_of({"command": "true", "command_by": {}}), ("owner", None))
        self.assertTrue(callable(getattr(bot.Bot, "ask_group", None)))
        sec = read("SECURITY.md")
        self.assertIn("a job a session set speaks as that session", " ".join(sec.split()))
        self.assertIn("it comes to your chat as a card", " ".join(sec.split()))
        self.assertIn("not from a pipe, a hub session or a schedule job", " ".join(sec.split()))

    def test_contributing_says_no_external_pull_requests(self):
        text = read("CONTRIBUTING.md")
        self.assertRegex(text, r"(?i)not accepting (external )?pull requests")
        self.assertIn("SECURITY.md", text)
        self.assertIn("EXTENDING.md", text)
        self.assertNotIn("Signed-off-by", text)   # DCO is not needed without external PRs — no overpromising

    def test_contributing_portability_names_only_helpers_that_exist(self):
        # every hub runs the suite on its own machine when it updates (modules/update run_tests): a test red only on
        # Windows under a standard account holds back someone else's update. The section says how to write tests that
        # pass there; whatever helper it names must exist on every OS, or the advice is a broken promise
        text = read("CONTRIBUTING.md")
        self.assertIn(slug(PORTABILITY), anchors(text))
        start = text.index("## " + PORTABILITY)
        end = text.find("\n## ", start + 3)
        part = text[start:end if end != -1 else None]
        flat = " ".join(part.split())
        for need in ("Windows", "macOS", "Linux", "standard", "tools/test.py", "os.kill(pid, 0)", "shutil.rmtree",
                     'newline="\\n"', 'encoding="utf-8"', "sleep", "pull request template", "tests/fakes/procs.py"):
            self.assertIn(need, flat, f"CONTRIBUTING.md, {PORTABILITY}: no {need!r}")
        adapters = [read(os.path.join("platforms", n)) for n in ("posix.py", "windows.py")]
        named = set(re.findall(r"platforms\.current\.(\w+)", part))
        self.assertTrue({"rmtree", "alive"} <= named, named)   # the hub's code; tests ask procs.alive
        for name in named:
            for src in adapters:
                self.assertRegex(src, rf"(?m)^def {name}\(", f"platforms.current.{name}: missing in one of the adapters")
        from tests.fakes import can, procs
        # tests ask it, not platforms.current.alive: a zombie counts as dead
        self.assertTrue(callable(getattr(procs, "alive", None)), "tests/fakes/procs.py: no alive()")
        probes = set(re.findall(r"\bcan\.(\w+)\(\)", part))
        self.assertTrue({"symlink", "replace_open"} <= probes, probes)
        for name in probes:
            self.assertTrue(callable(getattr(can, name, None)), f"tests/fakes/can.py: no {name}()")
        from tests import winsafe
        links = set(re.findall(r"\bwinsafe\.(\w+)\(", part))
        self.assertTrue({"symlink", "try_symlink"} <= links, links)
        for name in links:
            self.assertTrue(callable(getattr(winsafe, name, None)), f"tests/winsafe.py: no {name}()")
        for path in set(re.findall(r"`((?:tests|tools|platforms|core|modules)/[\w./-]+)`", part)):
            self.assertTrue(os.path.exists(os.path.join(ROOT, path)), f"CONTRIBUTING.md: no {path}")

    def test_pull_request_template_asks_which_systems_ran_the_tests(self):
        text = read(PR_TEMPLATE)
        self.assertIn("CONTRIBUTING.md", text)
        self.assertIn(PORTABILITY, text)   # the section by its name: a relative link in a PR body does not resolve
        for system in ("Windows", "macOS", "Linux"):
            line = next((x for x in text.splitlines() if system in x and x.lstrip().startswith("- [ ]")), "")
            self.assertIn("not checked", line, f"{PR_TEMPLATE}: no item {system} or no 'not checked' in it")
            if system == "Windows":   # under a standard account: no right to make links, no replacing open files
                self.assertIn("standard account", line)
        self.assertNotRegex(text, r"(?i)signed[- ]off|git commit -s|\bDCO\b")   # as in CONTRIBUTING: no DCO

    def test_pull_request_template_points_outsiders_to_issues_and_lets_invited_people_carry_on(self):
        # the same .github reaches the repository that colleagues send pull requests to every day: the notice says
        # "not accepted" to a stranger and "carry on" to someone invited, and it sits in comments, so a rendered
        # pull request description stays clean
        text = read(PR_TEMPLATE)
        above = re.split(r"(?m)^## ", text, maxsplit=1)[0]   # everything above the first heading
        notice = " ".join(" ".join(re.findall(r"<!--(.*?)-->", above, re.S)).split())
        self.assertRegex(notice, r"(?i)not accepted for now")
        self.assertRegex(notice, r"(?i)open an issue instead")
        self.assertRegex(notice, r"(?i)invited")
        self.assertRegex(notice, r"(?i)carry on")
        head = re.sub(r"<!--.*?-->", "", above, flags=re.S)
        self.assertEqual(head.strip(), "", "the notice is in comments: nothing visible above the first heading")

    def test_contributing_names_the_forms_and_the_invited(self):
        text = " ".join(read("CONTRIBUTING.md").split())
        self.assertIn("bug form", text)
        self.assertIn("feature form", text)
        self.assertIn("A blank issue is possible too", text)
        self.assertIn("collaborators they invited", text)
        self.assertIn("The pull request template is for them", text)

    def test_code_of_conduct_reports_privately(self):
        text = read("CODE_OF_CONDUCT.md")
        self.assertIn("Contributor Covenant", text)
        self.assertIn("private", text.lower())
        self.assertNotRegex(text, r"\[INSERT")

    def test_public_addresses_are_the_repository_and_its_pages(self):
        readme = read("README.md")
        self.assertIn(f"curl -fsSL {PAGES}install.sh | bash", readme)
        self.assertIn(f"irm {PAGES}install.ps1 | iex", readme)
        self.assertIn(f"git clone https://github.com/{REPO_SLUG}.git", readme)
        self.assertIn(f"https://github.com/{REPO_SLUG}/security/advisories/new", read("SECURITY.md"))
        self.assertIn(f"https://github.com/{REPO_SLUG}/discussions", read("CONTRIBUTING.md"))
        for name in DOCS:
            for url in re.findall(r"https?://[^\s)>\]`\"']+", read(name)):
                if "icons8" in url.lower():
                    self.assertTrue(url.startswith((f"https://github.com/{REPO_SLUG}", PAGES)), f"{name}: {url}")

    def test_license_is_apache_and_readme_links_it(self):
        lic = read("LICENSE")
        self.assertTrue(lic.lstrip().startswith("Apache License\n"), "LICENSE is the full Apache-2.0 text, from its first line")
        for needle in ("Version 2.0, January 2004", "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
                       "END OF TERMS AND CONDITIONS", "limitations under the License."):
            self.assertIn(needle, lic)
        readme = read("README.md")
        self.assertIn("Apache License 2.0", readme)
        self.assertIn("](LICENSE)", readme)
        self.assertNotIn("added to the repository when it is opened", readme)
        notice = read("NOTICE")   # the copyright holder of the open repository (owner's decision: Apache-2.0, Icons8 LLC)
        self.assertTrue(notice.startswith("Hub by Icons8\nCopyright 2026 Icons8 LLC\n"), notice[:80])
        self.assertIn("THIRD-PARTY-NOTICES.md", notice)
        self.assertIn("](NOTICE)", readme)

    def test_third_party_notices_match_the_bundled_files(self):
        notices = read("THIRD-PARTY-NOTICES.md")
        self.assertIsNone(CYRILLIC.search(notices))
        vendor = os.path.join("modules", "web", "vendor")
        # versions come from the headers of the vendored files; the notices must name the same ones
        for fname, pattern in (("marked.min.js", r"marked v([\d.]+)"), ("highlight.min.js", r"Highlight\.js v([\d.]+)"),
                               ("purify.min.js", r"@license DOMPurify ([\d.]+)")):
            m = re.search(pattern, read(os.path.join(vendor, fname))[:600])
            self.assertTrue(m, f"{fname}: no version in the header")
            self.assertRegex(notices, rf"\| [^|]*\| {re.escape(m.group(1))} \|[^\n]*{re.escape(fname)}", fname)
        mermaid = read(os.path.join(vendor, "mermaid.min.js"))
        ver = re.search(r'\{version:"(\d+\.\d+\.\d+)"\},\w+=\w+\(\(\)=>\w+\.version,"getVersion"\)', mermaid)
        self.assertTrue(ver, "mermaid: build version not found")
        self.assertRegex(notices, rf"\| Mermaid \| {re.escape(ver.group(1))} \|")
        # every license the summary promises has a text or a pointer to it, every named file exists
        for needle in ("### MIT License", "### ISC License", "### BSD 3-Clause License", "fonts/OFL.txt"):
            self.assertIn(needle, notices)
        for path in re.findall(r"`((?:modules|media)/[^`]+)`", notices):
            self.assertTrue(glob.glob(os.path.join(ROOT, path)), f"no {path}")
        # the hub's own mark goes under the code's license (owner's decision): not in the list of the page's icons whose
        # license the Icons8 section names
        with open(os.path.join(ROOT, "modules", "web", "icons8.json"), encoding="utf-8") as f:
            self.assertNotIn("favicon", json.load(f)["icons"]["items"])
        self.assertRegex(notices, r"hub's mark[^#]*Apache License 2\.0[^#]*`modules/web/favicon\.svg`")

    def test_extending_describes_what_the_current_plugin_api_added(self):
        # EXTENDING is the contract for clients and plugins: everything declared in the PLUGIN_API line is named there
        from core import version
        text = read("EXTENDING.md")
        for name in ("`GET /api/marketplaces`", "`GET /api/marketplaces/icon?", "`POST /api/plugins/install`",
                     "409 `{error, wider:", "`page_access`", "`plugins.consented`", "`GET /api/voice/failed?id=",
                     "`POST /api/voice`", "`POST /api/accounts/use", "`state: \"archived\"`", "`sidebar.tiles`",
                     "`hub.banner(text)`", "`GET /api/backlog?", "`parked`", "`retired`", "503 with `Retry-After`",
                     "`lag` (1.31)", "`data-emoji`", "An external agent's letter (1.31)", "`hub.addSubgroups(",
                     "`hub.groupFold(", "`news: true` on a feed row `out` (API 1.32)", "`--jump-shadow`",
                     "`send.tasks` in `features`", "`p.hint.feed-loading`", "`session.header.start`",
                     "`files:state`", "`files:do`", "`chrome=hub`", "`hub.messageAt(node)`", "`POST /api/session/resume`",
                     "`session.resume` in `features`", "`hub.capabilities.themeOptions === 1`"):
            self.assertIn(name, text, f"EXTENDING.md: no {name} (Plugin API {version.PLUGIN_API})")
        for name in ("modules.model", "profile.use", "speech.say", "account.limits", '"stdin": true', '"bus.transport"',
                     "bus.offered", "codex.decide", "OPTIONAL_POINTS"):
            self.assertIn(name, text, f"EXTENDING.md: no {name} (Module API {version.MODULE_API})")
        self.assertIn(f"Module API {version.MODULE_API}", text)
        self.assertIn(f"API {version.PLUGIN_API}", text)

    def test_picture_templates_name_the_same_repository(self):
        self.assertIn(REPO_SLUG, read("tools/dev/readme_media/social-preview.html"))


FIELD_TYPES = {"markdown", "textarea", "input", "dropdown", "checkboxes"}   # what GitHub's issue forms know


def load_yaml(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


class IssueForms(unittest.TestCase):
    """The "new issue" chooser: two forms and config.yml. The same .github goes to the repository that colleagues use, so
    the forms must work for both; the statements in them must match CONTRIBUTING and SECURITY. The tests named
    *_requires_pyyaml skip where PyYAML (not in the standard library) is not installed."""

    def all_files(self):
        return FORM_FILES + [FORMS + "/config.yml"]

    def test_files_exist_and_hold_nothing_personal(self):
        forms = deny_forms()
        self.assertTrue(forms)
        for name in self.all_files():
            self.assertTrue(os.path.isfile(os.path.join(ROOT, name)), f"no {name}")
            text = read(name)
            self.assertIsNone(CYRILLIC.search(text), f"{name}: Russian")
            for n, line in enumerate(text.splitlines(), 1):
                for f in forms:
                    self.assertIsNone(f.search(line), f"{name}:{n}: forbidden form {f.pattern!r}")
                self.assertNotRegex(line, r"<set before|TODO|FIXME|XXX|lorem", f"{name}:{n}")
                self.assertNotRegex(public_out(line), r"(?i)icons8", f"{name}:{n}: organization name")

    def test_no_other_files_in_the_folder(self):
        # GitHub shows every .yml/.md here in the chooser: a stray file becomes a form
        shown = [n for n in os.listdir(os.path.join(ROOT, FORMS)) if n.lower().endswith((".yml", ".yaml", ".md"))]
        self.assertEqual(sorted(shown), ["bug.yml", "config.yml", "feature.yml"])

    @unittest.skipUnless(yaml, "PyYAML is not installed")
    def test_forms_follow_the_github_schema_requires_pyyaml(self):
        for name in FORM_FILES:
            doc = load_yaml(name)
            self.assertIsInstance(doc, dict, name)
            for key in ("name", "description"):
                self.assertTrue(isinstance(doc.get(key), str) and doc[key].strip(), f"{name}: no {key}")
            self.assertLessEqual(set(doc) - {"name", "description", "title", "labels", "assignees", "type", "projects", "body"},
                                 set(), f"{name}: keys GitHub does not know")
            self.assertTrue(isinstance(doc.get("body"), list) and doc["body"], f"{name}: no body")
            ids = []
            for i, item in enumerate(doc["body"]):
                where = f"{name}: body[{i}]"
                self.assertIn(item.get("type"), FIELD_TYPES, where)
                attrs = item.get("attributes")
                self.assertIsInstance(attrs, dict, where)
                if item["type"] == "markdown":
                    self.assertTrue(isinstance(attrs.get("value"), str) and attrs["value"].strip(), where)
                    self.assertNotIn("id", item, where)   # GitHub rejects an id on markdown
                    continue
                self.assertTrue(isinstance(attrs.get("label"), str) and attrs["label"].strip(), f"{where}: no label")
                self.assertRegex(item.get("id", ""), r"^[A-Za-z0-9_-]+$", f"{where}: id")
                ids.append(item["id"])
                if item["type"] == "dropdown":
                    options = attrs.get("options")
                    self.assertTrue(isinstance(options, list) and options and all(isinstance(o, str) for o in options), where)
                    self.assertEqual(len(options), len(set(options)), f"{where}: a repeat in options")
                if item["type"] == "checkboxes":
                    options = attrs.get("options")
                    self.assertTrue(isinstance(options, list) and options, where)
                    for o in options:
                        self.assertTrue(isinstance(o.get("label"), str) and o["label"].strip(), where)
                        self.assertIsInstance(o.get("required", False), bool, where)
                    self.assertNotIn("validations", item, f"{where}: in checkboxes, required is set in options")
                if "render" in attrs:
                    self.assertEqual(item["type"], "textarea", where)
                self.assertLessEqual(set(item.get("validations", {})), {"required"}, where)
            self.assertEqual(len(ids), len(set(ids)), f"{name}: a repeated id")
            self.assertTrue(ids, f"{name}: markdown only — GitHub does not accept such a form")
            self.assertTrue(any(i.get("validations", {}).get("required") for i in doc["body"]), f"{name}: no required fields")

    @unittest.skipUnless(yaml, "PyYAML is not installed")
    def test_config_keeps_blank_issues_and_links_the_public_repository_requires_pyyaml(self):
        # colleagues write free-form issues in the working repository; the forms are a choice. GitHub takes only absolute
        # addresses in contact_links: the public repository's advisory form and its Discussions
        doc = load_yaml(FORMS + "/config.yml")
        self.assertIs(doc["blank_issues_enabled"], True)
        self.assertEqual([c["url"] for c in doc["contact_links"]],
                         [f"https://github.com/{REPO_SLUG}/security/advisories/new", f"https://github.com/{REPO_SLUG}/discussions"])
        self.assertTrue(all(c.get("name") and c.get("about") for c in doc["contact_links"]))

    def test_config_names_only_the_public_repository(self):
        text = read(FORMS + "/config.yml")
        for url in re.findall(r"https?://\S+", text):
            self.assertTrue(url.startswith(f"https://github.com/{REPO_SLUG}/"), url)
        self.assertNotRegex(text, r"mailto:|@[\w-]+\.(com|org|net|ru)")   # SECURITY.md: no e-mail for vulnerabilities

    @unittest.skipUnless(yaml, "PyYAML is not installed")
    def test_bug_form_asks_for_what_contributing_asks_requires_pyyaml(self):
        # CONTRIBUTING: version, OS, what you did, what you expected, what happened, the output of `hub doctor`
        doc = load_yaml(FORMS + "/bug.yml")
        required = {i["id"] for i in doc["body"] if i.get("validations", {}).get("required")}
        self.assertTrue({"version", "os", "steps", "expected", "actual", "doctor"} <= required, required)
        doctor = next(i for i in doc["body"] if i.get("id") == "doctor")
        self.assertIn("hub doctor", doctor["attributes"]["label"])
        self.assertTrue(re.search(r"hub\s+doctor", read("CONTRIBUTING.md")))
        systems = next(i for i in doc["body"] if i.get("id") == "os")["attributes"]["options"]
        for system in ("macOS", "Linux", "Windows"):
            self.assertTrue(any(system in o for o in systems), system)
        checks = next(i for i in doc["body"] if i.get("id") == "checks")["attributes"]["options"]
        self.assertTrue(all(o.get("required") for o in checks))
        self.assertTrue(any("tokens" in o["label"] and "private paths" in o["label"] for o in checks))   # as in CONTRIBUTING
        self.assertTrue(any("vulnerabilit" in o["label"] for o in checks))

    def test_forms_say_what_contributing_and_security_say(self):
        security = read("SECURITY.md")
        self.assertIn("private vulnerability reporting", security.lower())   # the way the forms send a vulnerability
        self.assertIn("Report a vulnerability", security)   # the button the forms name
        for name in FORM_FILES:
            flat = " ".join(read(name).split())
            self.assertIn("not accepted for now", flat, name)   # CONTRIBUTING: "not accepting external pull requests"
            self.assertIn("CONTRIBUTING.md", flat, name)
            self.assertNotRegex(flat, r"(?i)\b(send|open|submit) (us )?a (pull request|PR)\b|\bPRs? (are )?welcome", name)
        bug, feature = (" ".join(read(n).split()) for n in FORM_FILES)
        self.assertIn("SECURITY.md", bug)
        self.assertIn("Report a vulnerability", bug)
        self.assertIn("EXTENDING.md", feature)   # a module or a plugin first: CONTRIBUTING "Where a change belongs"
        self.assertIn("Discussions", feature)
        for doc in ("SECURITY.md", "CONTRIBUTING.md", "EXTENDING.md"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, doc)), doc)


class Media(unittest.TestCase):
    def test_only_expected_pictures_with_sizes_and_budget(self):
        names = {n for n in os.listdir(MEDIA) if n.endswith(".png")}
        self.assertEqual(names - set(SHOTS), set(), "extra images not on the list")
        self.assertEqual(names, set(SHOTS), "a shot README needs is missing")
        with open(os.path.join(MEDIA, "logo.svg"), encoding="utf-8") as f, \
                open(os.path.join(ROOT, "modules", "web", "favicon.svg"), encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read(), "the README logo is the current hub icon")
        total = 0
        for name, size in SHOTS.items():
            path = os.path.join(MEDIA, name)
            chunks = png_chunks(path)
            w, h = struct.unpack(">II", chunks[0][1][:8])
            self.assertEqual((w, h), size, name)
            total += os.path.getsize(path)
            self.assertLess(os.path.getsize(path), 600_000, name)   # into the snapshot that every hub installs
        self.assertLess(total, 1_500_000)

    def test_pngs_carry_no_metadata(self):
        for name in os.listdir(MEDIA):
            if name.endswith(".png"):
                kinds = {k for k, _ in png_chunks(os.path.join(MEDIA, name))}
                self.assertEqual(kinds - {"IHDR", "IDAT", "IEND", "PLTE", "tRNS", "sRGB", "gAMA", "pHYs", "cHRM"}, set(),
                                 f"{name}: extra blocks in PNG (text, EXIF)")

    def test_manifest_matches_the_pictures_and_the_scan_passed(self):
        with open(os.path.join(MEDIA, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual(set(man["pictures"]), {n for n in os.listdir(MEDIA) if n.endswith(".png")})
        for name, info in man["pictures"].items():
            with open(os.path.join(MEDIA, name), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), info["sha256"], f"{name}: the image was replaced without a new shot")
            self.assertEqual(info["scan"], "clean", name)   # the page text under the screenshot was checked for personal data
            self.assertTrue(info["pages"] and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in info["pages"].values()), name)


class Demo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool("readme_demo")

    def test_seed_is_neutral(self):
        # an invented fleet: no real path, name or address — by the same forms as the publish check
        with tempfile.TemporaryDirectory() as d:
            self.tool.seed(d)
            with open(os.path.join(d, "journal.jsonl"), encoding="utf-8") as f:
                text = f.read()
        self.assertIn("AskUserQuestion", text)   # the "waiting for your reply" screenshot rests on this card
        self.assertEqual(self.tool.forbidden(text, []), [])
        for form in deny_forms():
            self.assertIsNone(form.search(text), form.pattern)

    def test_shots_refuse_a_folder_whose_frames_another_tool_made(self):
        # the README frames are composed by hand over this stand's pages: shooting over them brought the older look back
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"tool": "composed by hand", "pictures": {}}, f)
            with self.assertRaises(SystemExit) as got:
                self.tool.shots(d)   # before any browser starts
            self.assertIn("composed by hand", str(got.exception))
            with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"tool": self.tool.TOOL, "pictures": {}}, f)
            self.assertIsNone(self.tool.made_elsewhere(d))   # its own frames: shot over as before
            for bad in ('{"pictures": {}}', "{not json"):   # a manifest that does not say the tool made it: not its own
                with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
                    f.write(bad)
                self.assertTrue(self.tool.made_elsewhere(d), bad)
        self.assertIsNone(self.tool.made_elsewhere(os.path.join(d, "none")))
        self.assertTrue(self.tool.made_elsewhere(MEDIA))   # the frames in media/ are not this tool's

    def test_scan_flags_private_things(self):
        f = self.tool.forbidden
        # the samples are assembled on the fly: the publish check would reject them whole in the test file
        mac, win, lan = "/Us" + "ers/anna/work/x", "C:\\Us" + "ers\\anna\\x", "10." + "1.2.3"
        self.assertTrue(f(f"opened {mac}", []))
        self.assertTrue(f(win, []))
        self.assertTrue(f(f"ssh {lan}", []))
        self.assertTrue(f("welcome, borya-hub", ["borya-hub"]))   # a live hub's name / login / host — from the capture environment
        self.assertTrue(f("Welcome, Borya-Hub", ["borya-hub"]))
        self.assertEqual(f("/work/shop-api and the hub", ["borya-hub"]), [])

    def test_live_names_come_from_the_machine_not_from_a_list(self):
        from unittest import mock
        with mock.patch("getpass.getuser", return_value="Borya"), mock.patch("socket.gethostname", return_value="Ivy.local"):
            names = self.tool.live_names()
        self.assertIn("borya", names)
        self.assertIn("ivy", names)
        self.assertTrue(all(n == n.lower() and len(n) >= 3 and n not in self.tool.FLEET_WORDS for n in names))
        with mock.patch("getpass.getuser", return_value="docs"):   # a user named like a demo agent does not cancel the capture
            self.assertNotIn("docs", self.tool.live_names())

    def test_shots_refuse_to_run_over_a_live_config(self):
        # the capture runs with an empty HOME and an empty hub.json: another hub's environment in the process means
        # refusal, not a silent capture
        env = self.tool.clean_env({"HOME": "/x", "HUB_STATE_DIR": "/live", "HUB_CONFIG": "/live.json", "PATH": "/bin"}, "/tmp/h")
        self.assertEqual(env["HOME"], "/tmp/h")
        self.assertNotIn("HUB_STATE_DIR", env)
        self.assertNotIn("HUB_CONFIG", env)
        self.assertEqual(env["PATH"], "/bin")


if __name__ == "__main__":
    unittest.main()
