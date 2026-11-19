#!/bin/sh
# Publishing a snapshot of the repository: the working tree of the last commit minus the private parts, as a single "Sync"
# commit into a separate, clean history of the remote repository. The local history (commit messages) never leaves.
#   tools/publish.sh <git-url> [--check] [--skip-compat] [--nightly [--stands]] [--tag vX.Y.Z | --release] [--overwrite-remote]
# Channels: the snapshot goes to main, which every hub from git follows (nightly; owner 2026-11-18: no installing by tags
# from git); a release is a vX.Y.Z tag on an already published snapshot (tools/release.py) — the mark of a packaged release.
#   --check — build and verify, do not push.
#   --nightly — a snapshot into main without the test benches (compat, own checks): those run when a tag is released.
#     Every hub from git installs main within the hour (its own self-check runs, the benches do not; protection — the rollback).
#   --nightly --stands — the same benches before pushing to main: the hubs from git get a snapshot that will become
#     a release only after them (the tag runs the benches once more).
#   --tag vX.Y.Z | --release (the number is picked automatically: minor — the first release of the day, patch — later ones)
#     — a tag on the last snapshot of main; HEAD must be what is published (published.json), otherwise refused: run --nightly first.
#   --overwrite-remote — the remote main has commits after the last publish that did not come from publish.sh (a PR merged there):
#     without the flag — refused (code 6) with their list, the snapshot would revert them (tools/publish_remote.py); with it — on top.
# The Claude Code of the release (owner decision 2026-11-11): every snapshot carries the version Anthropic's stable channel
# names at the moment, as core/version.py CLAUDE_TESTED (tools/compat.py --pin; the working copy keeps its own value): the
# benches test it, a hub whose claude is older installs it while it updates; unknown — nothing is published, code 11.
# Benches: tools/compat.py — the core against that Claude Code version on this machine (and a second one, HUB_COMPAT_SSH);
# if it fails, nothing is published, --skip-compat is for emergencies only, with an explicit line in the output. Last
# come the hub's own checks, if the settings folder has an executable publish-checks (it receives the commit; non-zero —
# no release; tools/publish_checks.py).
# Private modules: before pushing (both in --check and for a tag) their tests run against the snapshot — tools/private_check.py,
# code 8; --skip-compat does not lift it.
# The language of the tree: tools/public_language.py (the ratchet of Cyrillic lines) runs on every snapshot, code 9.
# The translations: every ready language is whole and fresh (tools/i18n.py check --release), code 10.
# Not published: docs/, evals/locale/ru/ (the eval cases of a Russian-speaking owner) and everything in .gitignore. Before pushing, the snapshot is checked for forbidden words: patterns (one
# per line, Python regular expressions, matched against the text of each line) — .publish-deny (case-insensitive) and
# .publish-deny-case (case-sensitive) in the root of the main copy and of the copy being published from (they are in
# .gitignore: they hold exactly what must not appear), and the shared forms tools/publish-deny.txt (case-sensitive); then gitleaks, if installed.
# The check is closed by default: no patterns in the main copy or no shared forms, or a check error — nothing is published.
# The open repositories (tools/oss_open.py OPEN_TARGETS: icons8/hub and the open catalog) are refused by their address in any
# form (https, ssh, .git, case), code 12: they are filled only by tools/oss_open.py (tools/publish_guard.py).
set -eu
if ! (set -o pipefail) 2>/dev/null; then
  # sh without pipefail (dash before 0.5.13 — /bin/sh on Ubuntu 24.04): the script is handed to bash if there is one
  command -v bash >/dev/null 2>&1 && exec bash "$0" "$@"
  echo "this shell has no pipefail — run it with bash: bash tools/publish.sh …" >&2; exit 2
fi
set -o pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
USAGE="usage: tools/publish.sh <git-url> [--check] [--skip-compat] [--nightly [--stands]] [--tag vX.Y.Z | --release] [--overwrite-remote]"
URL=${1:?$USAGE}
shift
CHECK=""; SKIP_COMPAT=""; NIGHTLY=""; STANDS=""; TAG=""; RELEASE=""; OVERWRITE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=--check ;;
    --skip-compat) SKIP_COMPAT=1 ;;
    --nightly) NIGHTLY=1 ;;
    --stands) STANDS=1 ;;
    --tag) TAG=${2:-}; [ $# -gt 0 ] && shift
           printf '%s' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "--tag: a number vX.Y.Z is needed" >&2; exit 2; } ;;
    --release) RELEASE=1 ;;
    --overwrite-remote) OVERWRITE=1 ;;
    *) echo "$USAGE" >&2; exit 2 ;;
  esac
  shift
done
if [ -n "$TAG$RELEASE" ] && [ -n "$NIGHTLY" ]; then echo "--nightly is a snapshot into main, --tag/--release is a release: one of them" >&2; exit 2; fi
if [ -n "$STANDS" ] && [ -z "$NIGHTLY" ]; then echo "--stands goes only with --nightly (a release runs the stands itself)" >&2; exit 2; fi
if [ -n "$TAG" ] && [ -n "$RELEASE" ]; then echo "--tag or --release" >&2; exit 2; fi
# the open repositories get only the open snapshot, from tools/oss_open.py (its OPEN_TARGETS): never the full tree
set +e; OPEN=$(python3 "$REPO/tools/publish_guard.py" "$URL"); code=$?; set -e
if [ "$code" -eq 17 ]; then
  echo "$URL is the open repository $OPEN: publish.sh pushes the full tree (with the company's parts) and goes only to the team's own repository; the open one is filled by tools/oss_open.py — not publishing" >&2; exit 12
elif [ "$code" -ne 0 ]; then echo "tools/publish_guard.py failed — not publishing" >&2; exit 3; fi
NAME=${PUBLISH_NAME:?PUBLISH_NAME — the author name of the public commits}
MAIL=${PUBLISH_EMAIL:?PUBLISH_EMAIL — the author email of the public commits (noreply)}
cd "$REPO"
if [ -n "$(git status --porcelain --untracked-files=no -- . ':!docs' ':!evals/locale/ru')" ]; then  # these are not published
  echo "there are uncommitted changes — commit first" >&2; exit 2
fi
if [ -n "$TAG$RELEASE" ]; then   # a stable release — a tag on an already published snapshot, the snapshot is not rebuilt
  exec python3 "$REPO/tools/release.py" "$URL" ${TAG:+--tag "$TAG"} ${RELEASE:+--release} $CHECK ${SKIP_COMPAT:+--skip-compat} \
    ${OVERWRITE:+--overwrite-remote}
fi
# deny patterns live in the main copy (they are in .gitignore, worktrees and clones do not have them): no patterns — no publish
COMMON=$(cd "$(git rev-parse --git-common-dir)" && pwd)
MAIN=$(dirname "$COMMON")
DENY=$MAIN/.publish-deny
DENY_CASE=$MAIN/.publish-deny-case
if [ ! -s "$DENY" ]; then
  echo "no deny patterns: $DENY is missing or empty — not publishing" >&2; exit 3
fi
# a worktree may have its own .publish-deny / .publish-deny-case (also in .gitignore) — additional to the main copy's patterns
OWN_DENY=""; OWN_DENY_CASE=""
if [ "$REPO" != "$MAIN" ]; then OWN_DENY=$REPO/.publish-deny; OWN_DENY_CASE=$REPO/.publish-deny-case; fi
# forms that no hub publishes (home paths, private network addresses, lookalikes of real keys) — in the repo
SHARED=$REPO/tools/publish-deny.txt
if [ ! -s "$SHARED" ]; then
  echo "no shared deny patterns: $SHARED is missing or empty — not publishing" >&2; exit 3
fi
# the state folder describes one address (tools/publish_state.py): publishing to another one (a rehearsal) does not rewrite it
STATE_OF=$(python3 "$REPO/tools/publish_state.py" own "$URL") || { echo "tools/publish_state.py failed — not publishing" >&2; exit 3; }
record() {  # what is published in main (the "Updates" page of the shared hub; the release tag goes on this snapshot): $1 — the Sync commit
  if [ -z "$STATE_OF" ]; then
    python3 "$REPO/tools/publish_state.py" record "$(git -C "$REPO" rev-parse HEAD)" "$1" "$URL"
  else
    echo "the publishing state of this hub stays with $STATE_OF; to move it here: git config hub.release-url $URL" >&2
  fi
}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FIRST=1   # the remote repo has no main yet: the first publish
FOREIGN=""   # the remote main has foreign commits after the last publish (list above): pushing only with --overwrite-remote
if git ls-remote "$URL" >/dev/null 2>&1 && [ -n "$(git ls-remote --heads "$URL" main)" ]; then
  FIRST=""
  # main history without old files (the comparison with the last publish reads commits only); a local path does not take the filter
  case "$URL" in *://*|*@*:*) FILTER=--filter=blob:none ;; *) FILTER="" ;; esac
  git clone -q $FILTER --single-branch --branch main "$URL" "$TMP/pub"
  set +e
  python3 "$REPO/tools/publish_remote.py" "$TMP/pub" "$URL"
  code=$?
  set -e
  if [ "$code" -eq 6 ]; then FOREIGN=1
  elif [ "$code" -ne 0 ]; then echo "the check against the remote main failed — not publishing" >&2; exit 3; fi
  find "$TMP/pub" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
else
  git init -q -b main "$TMP/pub"
  git -C "$TMP/pub" remote add origin "$URL"
fi
git archive HEAD | tar -x -C "$TMP/pub"
rm -rf "$TMP/pub/docs" "$TMP/pub/evals/locale/ru"   # the Russian owner's eval cases stay at home (publish_allow.SKIP)
# the credits (owner decision 2026-11-17): the "## Contributors" section of README.md and CONTRIBUTORS.md, built from the
# whole CHANGES.md under the README's author line; a README without that line — refused (tools/contributors.py); the
# checks below spare the credits of these two files only (tools/publish_allow.py credit_free)
python3 "$REPO/tools/contributors.py" write "$TMP/pub" || { echo "the contributors are not built (above) — not publishing" >&2; exit 3; }
PIN=""   # the Claude Code of this snapshot (above); a tree without core/version.py has none
if [ -f "$TMP/pub/core/version.py" ]; then
  python3 "$REPO/tools/compat.py" --pin "$TMP/pub" || { echo "the Claude Code version for CLAUDE_TESTED is unknown (above) — not publishing" >&2; exit 11; }
  PIN=$(sed -n 's/^CLAUDE_TESTED *= *"\([0-9.]*\)".*/\1/p' "$TMP/pub/core/version.py")
fi
deny() {  # $1 — the patterns file, $2 — -i: case-insensitive. 0 — forbidden found, 1 — clean; an error — exit
  [ -n "$1" ] && [ -f "$1" ] || return 1
  # tools/publish_allow.py reads every snapshot file itself, binary ones too, the same on all OSes (GNU and BSD grep flags
  # differ): it matches the patterns against the text of each line, skips paths that are not checked (the library in vendor,
  # the shared forms themselves), and cuts out the lines of tools/publish-allow.txt (public project addresses)
  if ! python3 "$REPO/tools/publish_allow.py" "$1" "$2" "$REPO/tools/publish-allow.txt" "$TMP/pub" > "$TMP/denied"; then
    echo "the check filter crashed — not publishing" >&2; exit 3
  fi
  [ -s "$TMP/denied" ] || return 1
  cut -c1-200 "$TMP/denied" | head -40 >&2
  return 0
}
# unresolved merge conflict (the <<<<<<< markers once reached CHANGES.md on GitHub — the "Updates" page of colleagues)
set +e
grep -r -n -E '^(<<<<<<< |>>>>>>> )' "$TMP/pub" --exclude-dir=.git --exclude-dir=vendor --exclude-dir=node_modules >&2
code=$?
set -e
if [ "$code" -eq 0 ]; then echo "the snapshot has an unresolved merge conflict (see above) — not publishing" >&2; exit 3; fi
if [ "$code" -ge 2 ]; then echo "the merge conflict check failed (grep: $code) — not publishing" >&2; exit 3; fi
# .publish-deny — case-insensitive; .publish-deny-case — case-sensitive (task keys like ABC-12, but not "devops-2")
if deny "$DENY" -i || deny "$DENY_CASE" "" || deny "$SHARED" "" || deny "$OWN_DENY" -i || deny "$OWN_DENY_CASE" ""; then
  echo "the snapshot has something forbidden (see above) — not publishing" >&2; exit 3
fi
# the language of the tree (tools/public_language.py): the same ratchet as in the full test run, as its own step of seconds and its
# own exit code, so that a snapshot without a full run still cannot carry new Russian. The tree is the committed one (the
# uncommitted state was refused above), so the snapshot and the working copy count the same.
python3 "$REPO/tools/public_language.py" >&2 || { echo "the language of the tree is not held (see above): translate, or lower the baseline with tools/public_language.py --prune - not publishing" >&2; exit 9; }
# the translations (tools/i18n.py check --release): every language the hub offers is whole and fresh, so the new phrases of
# a release reach every language at once instead of showing English until someone remembers; tools/translate.py fills them
python3 "$REPO/tools/i18n.py" check --release >&2 || { echo "the translations of the ready languages are not whole (see above): tools/translate.py --all, commit - not publishing" >&2; exit 10; }
# gitleaks knows many more key formats than the shared forms; if installed on the machine, it checks the snapshot
leaks() {   # the repo's .gitleaks.toml (gitleaks rules and what is known here not to be a key), if present
  if [ -f "$REPO/.gitleaks.toml" ]; then gitleaks dir --config "$REPO/.gitleaks.toml" "$@"; else gitleaks dir "$@"; fi
}
# gitleaks before 8.19 (apt on Ubuntu 24.04) does not know "dir" — skipped with a line; any other failure — refusal, like a finding
OLD_LEAKS=""
if command -v gitleaks >/dev/null 2>&1; then
  case "$(gitleaks dir --help 2>&1 || true)" in *"unknown command"*) OLD_LEAKS=1 ;; esac
fi
if [ -n "$OLD_LEAKS" ]; then
  echo "gitleaks is older than 8.19 (no \"gitleaks dir\"): the snapshot is checked by the deny patterns only — update gitleaks" >&2
elif command -v gitleaks >/dev/null 2>&1; then
  if ! (cd "$TMP/pub" && leaks --no-banner --redact --verbose --log-level warn --exit-code 1 . >&2); then
    echo "gitleaks found keys in the snapshot or failed (see above) — not publishing" >&2; exit 3
  fi
else
  echo "gitleaks is not installed: the snapshot is checked by the deny patterns only (https://github.com/gitleaks/gitleaks)" >&2
fi
cd "$TMP/pub"
git add -A
# a nightly where only the Claude Code moved (Anthropic's stable went on, the code did not): no snapshot for that alone —
# the hubs on main would restart onto an untested claude for nothing; the snapshot keeps its own (a publication with
# the stands may move it alone: it is tested)
if [ -n "$NIGHTLY" ] && [ -z "$STANDS" ] && [ -n "$PIN" ] && [ "$(git diff --cached --name-only)" = "core/version.py" ] && \
   [ -z "$(git diff --cached -U0 -- core/version.py | grep -E '^[-+]' | grep -vE '^(\+\+\+|---) ' | grep -vE '^[-+]CLAUDE_TESTED = "')" ]; then
  git reset -q -- core/version.py && git checkout -q -- core/version.py
  PIN=$(sed -n 's/^CLAUDE_TESTED *= *"\([0-9.]*\)".*/\1/p' core/version.py)
fi
if git diff --cached --quiet; then
  echo "nothing new"
  # the HEAD snapshot is already in main — remember it, so that the release tag knows HEAD is published
  [ -z "$CHECK" ] && git rev-parse -q --verify HEAD >/dev/null && record "$(git rev-parse HEAD)"
  exit 0
fi
# test of the private modules against this snapshot (extraction spec §2.2, tools/private_check.py): a module reads the journal outside the
# Module API contract, and a core change breaks it silently. For both channels (nightly reaches hubs on main at once), without benches,
# --skip-compat does not lift it; no access to the modules repo — refused (the owner publishes, and has it). In --check too.
# After "nothing new": an already published snapshot was checked when it went out
python3 "$REPO/tools/private_check.py" "$TMP/pub" || { code=$?; echo "the private modules' tests against the snapshot did not pass (see above) — not publishing" >&2; exit "$code"; }
# the snapshot would revert foreign commits of the remote main; the check runs to the end and does not pass
if [ -n "$FOREIGN" ] && [ -n "$OVERWRITE" ]; then
  echo "WARNING: the snapshot goes over the commits above (--overwrite-remote)" >&2
elif [ -n "$FOREIGN" ] && [ -z "$CHECK" ]; then
  echo "the snapshot would roll back commits of the remote main (listed above) — not publishing" >&2; exit 6
fi
# new CHANGES.md entries go under today's date (owner decision 2026-10-04: yesterday's date kept being appended to); the first
# publish into a new repo takes CHANGES.md whole, whatever its last date is
TODAY=$(date +%Y-%m-%d)
if [ -z "$FIRST" ] && ! git diff --cached --quiet -- CHANGES.md && [ "$(grep -m1 '^## ' CHANGES.md)" != "## $TODAY" ]; then
  echo "CHANGES.md: new entries are not under \"## ${TODAY}\" — add the heading of the day" >&2; exit 4
fi
# the message is the fresh CHANGES.md entry (colleagues' hubs show it on the "Updates" page)
# (a file, not -m: a release day's entry may pass the 128 KB of one command-line argument)
{ printf 'Sync %s\n\n' "$(date +%Y-%m-%d)"; awk '/^## /{n++} n==1' "$REPO/CHANGES.md" 2>/dev/null || true; } > "$TMP/message"
# Co-authored-by: whoever CHANGES.md thanks more often than the last snapshot did (the first one — all of them), and
# whoever it thanks and no commit of the remote main credits yet (--history: a snapshot made before the credits, or one
# whose id was unknown, is caught up by the next one; a credited login is not repeated): GitHub counts them as
# contributors of the repository; the author of the commit stays the publisher
if [ -f CHANGES.md ]; then
  OLD=""; HISTORY=""
  if [ -z "$FIRST" ]; then
    HISTORY=.
    if git show HEAD:CHANGES.md > "$TMP/old-changes" 2>/dev/null; then OLD=$TMP/old-changes; fi
  fi
  python3 "$REPO/tools/contributors.py" coauthors . ${OLD:+--old "$OLD"} ${HISTORY:+--history "$HISTORY"} > "$TMP/coauthors" \
    || { echo "the co-authors are not known (above) — not publishing" >&2; exit 3; }
  if [ -s "$TMP/coauthors" ]; then printf '\n' >> "$TMP/message"; cat "$TMP/coauthors" >> "$TMP/message"; fi
fi
git -c user.name="$NAME" -c user.email="$MAIL" commit -q -F "$TMP/message"
echo "snapshot: $(git ls-files | wc -l | tr -d ' ') files"
if [ "$CHECK" = "--check" ]; then
  [ -n "$FOREIGN" ] && [ -z "$OVERWRITE" ] && { echo "check: the snapshot would roll back the commits above — it will not go without --overwrite-remote" >&2; exit 6; }
  grep '^Co-authored-by:' "$TMP/message" || true   # what the commit credits besides its author
  echo "the check passed, not sending"; exit 0
fi
# compatibility with Claude Code (owner decision 2026-10-06: on Windows too; 2026-11-11: only the snapshot's CLAUDE_TESTED);
# --nightly — without benches: they run before the release tag; --nightly --stands — before pushing (hubs on main take the
# snapshot at once)
if [ -n "$NIGHTLY" ] && [ -z "$STANDS" ]; then
  echo "nightly: the stands (compatibility, own checks) run when a tag is released"
elif [ -n "$SKIP_COMPAT" ]; then
  echo "WARNING: the Claude Code compatibility check is skipped (--skip-compat)" >&2
elif ! python3 "$REPO/tools/compat.py" ${PIN:+--versions "$PIN"}; then
  echo "compatibility with Claude Code is not confirmed (table above) — not publishing; in an emergency — --skip-compat" >&2; exit 5
fi
# the hub's own checks before pushing (optional): an executable publish-checks in the settings folder, tools/publish_checks.py
if { [ -z "$NIGHTLY" ] || [ -n "$STANDS" ]; } && ! python3 "$REPO/tools/publish_checks.py" "$(git -C "$REPO" rev-parse HEAD)"; then
  echo "the own pre-release checks did not pass (see above) — not publishing" >&2; exit 7
fi
git push -q origin main
echo "published: $URL"
# what is published — for the "Updates" page of the shared hub: what has not come out yet
record "$(git rev-parse HEAD)"
