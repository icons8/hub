# Chrome for e2e tests

A hub update runs the tests, and the page's e2e tests start Chrome. On macOS even `--headless=new` Chrome blinks in the
Dock for a second — on every update. The person asks "set up chrome for tests" (or complains about the Dock) — set it up
like voice (`voice.cmd`): install `chrome-headless-shell` for their system and put the path into `testchrome.path` in
hub.json. With nothing set, the tests take `chrome-headless-shell` themselves if it sits in the hub's settings folder or
in the puppeteer/playwright cache, else the system Chrome (it blinks). So after step 2 the path can be skipped (step 4).

1. Find out the system: `uname -sm`. Linux and Windows usually don't blink — ask whether it's needed.
2. Install into the hub's settings folder: `npx @puppeteer/browsers install chrome-headless-shell@stable --path
   ~/.config/hub-core/chrome-headless-shell` (no node — download the `chrome-headless-shell` zip for your platform from
   https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json).
3. Find the file: `find ~/.config/hub-core/chrome-headless-shell -name chrome-headless-shell -type f`.
4. Set the path (needed only if the browser is not in those places): `"testchrome": {"path": "<path>"}` in
   ~/.config/hub-core/hub.json. No restart needed: the setting is read on every run by the update, tests from a session
   (`tools/test.py`, `unittest`), `hub shot`/`hub video` and mermaid diagrams. Check: `python3 -m unittest
   tests.test_web_e2e`, the Dock stays quiet.

The download is your action at the owner's request, not hub code. To remove: delete `testchrome` from hub.json.
