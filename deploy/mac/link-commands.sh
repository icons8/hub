#!/bin/sh
# Hub commands in the terminal: hub and hub-telegram are wrappers in ~/.local/bin that run them with the same Python
# as the hub (reported 2026-10-02: "hub: command not found after install"); another program with that name is left as it
# is (deploy/commands.py). If ~/.local/bin is not in PATH, append it to ~/.zshrc once — bash: to the login file it reads
# (the first of ~/.bash_profile, ~/.bash_login, ~/.profile; a new ~/.bash_profile would leave ~/.profile unread).
#   link-commands.sh REPO PYTHON [PATH_FIRST]   PATH_FIRST — folders the commands put first in PATH (the app's programs)
set -eu
REPO=$1 PY=$2 FIRST=${3:-}
BIN="$HOME/.local/bin"
"$PY" "$REPO/deploy/commands.py" "$REPO" "$PY" "$FIRST"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    RC="$HOME/.zshrc"
    if [ "${SHELL##*/}" = bash ]; then
      RC="$HOME/.bash_profile"
      for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
        [ -e "$f" ] && { RC=$f; break; }
      done
    fi
    if ! grep -qs "hub-core: ~/.local/bin" "$RC"; then
      printf '\n# hub-core: ~/.local/bin in PATH — the commands hub and hub-telegram\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
    fi
    echo "The hub command works in a new terminal window (or now: source $RC)"
    ;;
esac
