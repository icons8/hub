#!/bin/bash
# The one-line install of the hub on macOS and Linux (one-line spec sections 3-5):
#   curl -fsSL https://<delivery server>/i/<build key>/install.sh | bash [-s -- <flags>]
#   curl -fsSL https://<pages of the open build>/install.sh | bash [-s -- <flags>]      (the open build: no key)
# The delivery server writes the build key into this file as it hands it out; the open build's copy (tools/package/
# pages.py: the open flavor written in) needs none and sends none - its pages are open to anyone, its environments are
# assets of its GitHub releases, downloaded through their redirects (every one https to HUB_INSTALL_ASSET_HOSTS). The script picks the environment build of
# this machine (one of six), checks the machine, reads releases.json, downloads the environment with resume, checks its
# size and sha256, unpacks it into <data>/agents-hub/shells/<runtime>-<flavor>/ and checks every file against the
# archive, then hands over to the environment's own Python: install.py (the code layer, the settings, the service,
# the page - the same on three OSes). Nothing before that point changes the hub; a refusal says why in one line.
#
# Flags (also from HUB_INSTALL="--channel main --move", the command line wins):
#   --channel stable|main     taken and ignored: a hub installed by the line has one channel (stable)
#   --move | --stay           the answer to "found a hub from git" without the question
#   --instance <name>         a second hub on this machine
#   --yes                     no questions: every answer is the safe default (a move only with --move)
#   --no-open                 do not open the browser
#   --server [--user <name>]  a server (a cloud VM, as root or a user with sudo without a password): the user the hub
#                             runs under (hub, no sudo), git, Chrome, swap, linger, a login message - then the hub under
#                             that user as above; the page stays on 127.0.0.1, an SSH tunnel brings it to a computer
#   --keys-from <user>        with --server: that user's SSH keys reach the hub's user too; without it, only the keys
#                             of the one who ran it (the user sudo came from, else root)
#   --server --remove         takes off what --server put on (written down in /var/lib/agents-hub/server.json)
# Environment: HUB_DELIVER_URL (another delivery server: https, or http on this machine only), HUB_DELIVER_KEY (the
# build key when the server did not write one in), HUB_STATE_DIR (the hub's state folder), HTTPS_PROXY, SSL_CERT_FILE.
#
# The hand-over to install.py (contract 1; deploy/oneline/README.md):
#   <env>/runtime/python/bin/python3 -I -B <env>/install.py --contract 1 --env <env> --server <url> --key-file <file 0600>
#       --platform <p> --flavor <f> --runtime <n> --channel stable|nightly [--state <dir>] [--move|--stay]
#       [--instance <name>] [--yes] [--no-open] [--no-input]
#   stdin is /dev/tty when there is a terminal, else /dev/null with --no-input; HUB_LANG is the system's language when
#   it is not set; HUB_INSTALL_LOG names the install log. Its exit code is this script's.
# Exit codes: 0 done; 1 refused or failed (nothing of the hub changed); 3 install.py's: nobody to ask about a hub from
# git (no terminal: run again with --move or --stay) or about a second one with no main hub; 64 a wrong flag.
# The whole body is in functions and runs from the last line: a download cut short runs nothing.
# ASCII only: the words a person reads after the unpacking are install.py's, in the system's language.

HUB_INSTALL_CONTRACT=1
HUB_INSTALL_KEY_IN='@@HUB_DELIVER_KEY@@'
HUB_INSTALL_SERVER='@@HUB_DELIVER_URL@@'   # the release writes in the server it puts the script on (upload.py script)
HUB_INSTALL_FLAVOR=team
# the open build's environments: an address its releases.json names off its own pages, and every redirect of that
# download, is https to one of these exactly (core/fetch.py ASSET_HOSTS holds the same list)
HUB_INSTALL_ASSET_HOSTS='github.com objects.githubusercontent.com release-assets.githubusercontent.com'
HUB_INSTALL_NEED_KB=2097152           # about 2 GB free: the environment, Chrome for the agents, Claude Code, downloads
HUB_INSTALL_LOCK_WAIT=600             # s a second install waits for the first
HUB_INSTALL_MAC_MIN=13.5              # Node 24 of the environment needs macOS 13.5
HUB_INSTALL_GLIBC_MIN=2.28            # Node 24 and ffmpeg of the environment need glibc 2.28
HUB_INSTALL_SERVER_USER=hub           # --server: the user the hub runs under
HUB_INSTALL_SERVER_RAM_KB=3670016     # --server: 3.5 GB of memory at least (the core ~1.2 GB, Chrome, a few sessions)
HUB_INSTALL_SERVER_SWAP_BELOW_KB=7340032   # --server: a swap file on a machine of less than 7 GB with no swap
HUB_INSTALL_SERVER_SWAP_MB=4096
HUB_INSTALL_SERVER_DISK_KB=10485760   # --server: 10 GB free for the home folders
HUB_INSTALL_CHROME_DEB='https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb'
HUB_INSTALL_SERVER_FIELDS='user user_created accounts linger swap motd chrome_conf auto_conf status code tunnel'
# what the script itself is, handed on to the run under the hub's user or through sudo (a piped script has no file)
HUB_INSTALL_CONSTANTS='HUB_INSTALL_CONTRACT HUB_INSTALL_KEY_IN HUB_INSTALL_SERVER HUB_INSTALL_FLAVOR
  HUB_INSTALL_ASSET_HOSTS HUB_INSTALL_NEED_KB HUB_INSTALL_LOCK_WAIT HUB_INSTALL_MAC_MIN HUB_INSTALL_GLIBC_MIN
  HUB_INSTALL_SERVER_USER HUB_INSTALL_SERVER_RAM_KB HUB_INSTALL_SERVER_SWAP_BELOW_KB HUB_INSTALL_SERVER_SWAP_MB
  HUB_INSTALL_SERVER_DISK_KB HUB_INSTALL_CHROME_DEB HUB_INSTALL_SERVER_FIELDS HUB_INSTALL_CONSTANTS'

say() {
  if [ -n "${UI_ON:-}" ]; then ui_say "$*"; else printf '%s\n' "$*" >&2; fi
  [ -n "${HUB_INSTALL_LOGFILE:-}" ] && printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$HUB_INSTALL_LOGFILE" 2>/dev/null
  return 0
}
note() { [ -n "${HUB_INSTALL_LOGFILE:-}" ] && printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$HUB_INSTALL_LOGFILE" 2>/dev/null; return 0; }
refuse() {
  if [ -n "${UI_ON:-}" ]; then ui_fail "$*"; else printf '%s\n' "Hub by Icons8 install: $*" >&2; fi
  note "refused: $*"
  return 1
}

# --- the look: what a terminal shows (deploy/oneline/README.md, "The look"; look.py draws what comes after the hand-over,
# the same way, told by HUB_INSTALL_UI). Five steps: the first three are this script's, the last two are setup.py's.
# The live part of the screen - "the tail": the hub's sign with its words (its dots glow in a wave while the work goes on;
# with 256 colours and 20 rows at least) and the line of the step in progress - is taken off and drawn again whenever
# something is said above it. Nothing of this runs unless stderr is a terminal with a TERM, 60 columns wide at least, in a
# UTF-8 locale, with NO_COLOR unset: anything else gets the lines as they were. HUB_INSTALL_LOOK=plain asks for them too.
# logo:begin (tools/dev/logo.py writes this; a test holds it equal to look.py's)
ui_logo_rgb() {   # a letter of the sign -> UI_RGB (24 bits) and UI_X256 (256 colours)
  case $1 in
    T) UI_RGB='224;80;16'; UI_X256=166 ;;
    a) UI_RGB='229;109;56'; UI_X256=167 ;;
    b) UI_RGB='234;138;96'; UI_X256=173 ;;
    c) UI_RGB='240;168;136'; UI_X256=216 ;;
    d) UI_RGB='245;197;175'; UI_X256=223 ;;
    e) UI_RGB='250;226;215'; UI_X256=224 ;;
    W) UI_RGB='255;255;255'; UI_X256=231 ;;
    X) UI_RGB='164;92;60'; UI_X256=131 ;;
    g) UI_RGB='191;141;118'; UI_X256=138 ;;
    K) UI_RGB='40;32;36'; UI_X256=235 ;;
  esac
}
ui_logo() {   # the sign: UI_FRAMES (the wave of the work), UI_PIC_DONE and UI_PIC_FAIL, UI_W columns of UI_H rows of pixels
  UI_W=14; UI_H=14; UI_NFRAMES=8
  UI_FRAMES=(
    .TTTTTTTTTTTT.TTTTTbTTbTTTTTTTTTTTTTTTTTTTTTTbTTccTTbTTTTTTTcTddTcTTTTTbTTTdTTdTTTbTTTTcdTTTTdcTTTTTTcdTTTTdcTTTTbTTTdTTdTTTbTTTTTcTddTcTTTTTTTbTTccTTbTTTTTTTTTTTTTTTTTTTTTTbTTbTTTTT.TTTTTTTTTTTT.
    .TTTTTTTTTTTT.TTTTTbTTbTTTTTTTTTTTTTTTTTTTTTTbTTccTTbTTTTTTTcTeeTcTTTTTbTTTeTTeTTTbTTTTceTTTTecTTTTTTceTTTTecTTTTbTTTeTTeTTTbTTTTTcTeeTcTTTTTTTbTTccTTbTTTTTTTTTTTTTTTTTTTTTTbTTbTTTTT.TTTTTTTTTTTT.
    .TTTTTTTTTTTT.TTTTTbTTbTTTTTTTTTTTTTTTTTTTTTTbTTccTTbTTTTTTTcTWWTcTTTTTbTTTWTTWTTTbTTTTcWTTTTWcTTTTTTcWTTTTWcTTTTbTTTWTTWTTTbTTTTTcTWWTcTTTTTTTbTTccTTbTTTTTTTTTTTTTTTTTTTTTTbTTbTTTTT.TTTTTTTTTTTT.
    .TTTTTTTTTTTT.TTTTTbTTbTTTTTTTTTTTTTTTTTTTTTTbTTddTTbTTTTTTTdTeeTdTTTTTbTTTeTTeTTTbTTTTdeTTTTedTTTTTTdeTTTTedTTTTbTTTeTTeTTTbTTTTTdTeeTdTTTTTTTbTTddTTbTTTTTTTTTTTTTTTTTTTTTTbTTbTTTTT.TTTTTTTTTTTT.
    .TTTTTTTTTTTT.TTTTTcTTcTTTTTTTTTTTTTTTTTTTTTTcTTeeTTcTTTTTTTeTddTeTTTTTcTTTdTTdTTTcTTTTedTTTTdeTTTTTTedTTTTdeTTTTcTTTdTTdTTTcTTTTTeTddTeTTTTTTTcTTeeTTcTTTTTTTTTTTTTTTTTTTTTTcTTcTTTTT.TTTTTTTTTTTT.
    .TTTTTTTTTTTT.TTTTTdTTdTTTTTTTTTTTTTTTTTTTTTTdTTeeTTdTTTTTTTeTddTeTTTTTdTTTdTTdTTTdTTTTedTTTTdeTTTTTTedTTTTdeTTTTdTTTdTTdTTTdTTTTTeTddTeTTTTTTTdTTeeTTdTTTTTTTTTTTTTTTTTTTTTTdTTdTTTTT.TTTTTTTTTTTT.
    .TTTTTTTTTTTT.TTTTTeTTeTTTTTTTTTTTTTTTTTTTTTTeTTddTTeTTTTTTTdTddTdTTTTTeTTTdTTdTTTeTTTTddTTTTddTTTTTTddTTTTddTTTTeTTTdTTdTTTeTTTTTdTddTdTTTTTTTeTTddTTeTTTTTTTTTTTTTTTTTTTTTTeTTeTTTTT.TTTTTTTTTTTT.
    .TTTTTTTTTTTT.TTTTTdTTdTTTTTTTTTTTTTTTTTTTTTTdTTccTTdTTTTTTTcTddTcTTTTTdTTTdTTdTTTdTTTTcdTTTTdcTTTTTTcdTTTTdcTTTTdTTTdTTdTTTdTTTTTcTddTcTTTTTTTdTTccTTdTTTTTTTTTTTTTTTTTTTTTTdTTdTTTTT.TTTTTTTTTTTT.
  )
  UI_PIC_DONE=.TTTTTTTTTTTT.TTTTTeTTeTTTTTTTTTTTTTTTTTTTTTTeTTWWTTeTTTTTTTWTWWTWTTTTTeTTTWTTWTTTeTTTTWWTTTTWWTTTTTTWWTTTTWWTTTTeTTTWTTWTTTeTTTTTWTWWTWTTTTTTTeTTWWTTeTTTTTTTTTTTTTTTTTTTTTTeTTeTTTTT.TTTTTTTTTTTT.
  UI_PIC_FAIL=.XXXXXXXXXXXX.XXXXXKXXKXXXXXXXXXXXXXXXXXXXXXXgXXKKXXgXXXXXXXgXKKXgXXXXXgXXXgXXgXXXgXXXXggXXXXggXXXXXXggXXXXggXXXXgXXXgXXgXXXgXXXXXgXggXgXXXXXXXgXXggXXgXXXXXXXXXXXXXXXXXXXXXXgXXgXXXXX.XXXXXXXXXXXX.
}
# logo:end

ui_init() {   # -> UI_ON and its settings, or nothing: a plain run, the lines as they always were
  local loc size
  UI_ON=; UI_COL=1; UI_ROWS=24; UI_COLS=80; UI_STEPS=5; UI_STEP=0; UI_NAME=; UI_DETAIL=; UI_T0=0
  UI_TAIL=0; UI_MOOD=work; UI_ART_ON=; UI_PAUSED=; UI_IDLE=; UI_S=; UI_CPID=; UI_ESC=; UI_TOOK=; UI_LEN=0
  UI_LIVE=; UI_FRAME=0; UI_NFRAMES=1; UI_CACHE=(); UI_BUF=; UI_NLINES=0
  [ -t 2 ] || return 0                              # a pipe, a log, user data of a cloud machine
  case ${TERM:-} in ''|dumb) return 0 ;; esac
  [ -z "${NO_COLOR:-}" ] || return 0                # no colour asked for: the plain lines
  [ "${HUB_INSTALL_LOOK:-}" != plain ] || return 0
  loc=${LC_ALL:-${LC_CTYPE:-${LANG:-}}}
  case $loc in *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) ;; *) return 0 ;; esac   # the marks are UTF-8
  size=$(stty size <&2 2>/dev/null)
  rows=${size%% *}; UI_COLS=${size#* }
  case $UI_COLS in ''|*[!0-9]*) UI_COLS=${COLUMNS:-80} ;; esac
  case $UI_COLS in ''|*[!0-9]*) UI_COLS=80 ;; esac
  UI_ROWS=$rows
  case $UI_ROWS in ''|*[!0-9]*) UI_ROWS=24 ;; esac
  [ "$UI_COLS" -ge 60 ] || return 0                 # a narrow window: the plain lines wrap by themselves
  [ "$UI_COLS" -gt 80 ] && UI_COLS=80
  case ${COLORTERM:-} in
    truecolor|24bit) UI_COL=3 ;;
    *) case ${TERM:-} in *256color*) UI_COL=2 ;; esac ;;
  esac
  UI_ESC=$(printf '\033')
  # the marks as the bytes of UTF-8: this file stays ASCII
  printf -v UI_OK '\342\234\223'; printf -v UI_BAD '\342\234\227'; printf -v UI_NOW '\342\200\272'
  printf -v UI_SKIP '\342\200\223'; printf -v UI_DOT '\302\267'; printf -v UI_ELL '\342\200\246'
  printf -v UI_BAR '\342\224\201'; printf -v UI_REST '\342\224\200'
  printf -v UI_HI '\342\226\200'; printf -v UI_LO '\342\226\204'
  if [ "$UI_COL" -ge 2 ] && [ "$UI_ROWS" -ge 20 ]; then UI_ART_ON=1; fi
  ui_logo
  # a drawing that fails here (a bash or a tool that does something else than expected) is no reason for a screen of
  # errors: the lines as they were
  if ! ( ui_art work && [ -n "${UI_LINES[0]}" ] && ui_art done && ui_art fail && ui_line accent "$UI_NOW" 1 Check "$UI_DOT x" 1 && ui_p dim x ) >/dev/null 2>&1; then
    UI_ART_ON=; return 0
  fi
  UI_ON=1
}

ui_p() {   # ui_p <class> <text> -> UI_S: the text in a colour (accent good bad dim; accentb goodb badb bold)
  local c=
  if [ "$UI_COL" -gt 0 ]; then
    case $1:$UI_COL in
      accent*:3) c='38;2;240;104;43' ;; accent*:2) c='38;5;202' ;; accent*:1) c=33 ;;
      good*:3) c='38;2;40;167;90' ;; good*:2) c='38;5;35' ;; good*:1) c=32 ;;
      bad*:3) c='38;2;232;76;76' ;; bad*:2) c='38;5;167' ;; bad*:1) c=31 ;;
      dim:*) c=2 ;;   # the terminal's own text colour, fainter: readable on a dark and on a light background
      bold:*) c=1 ;;
    esac
    case $1 in accentb|goodb|badb) c="1;$c" ;; esac
  fi
  if [ -n "$c" ]; then UI_S="${UI_ESC}[${c}m$2${UI_ESC}[0m"; else UI_S=$2; fi
}

ui_color() {   # ui_color <letter> <38|48> -> UI_S: the escape of a colour of the picture
  ui_logo_rgb "$1"
  if [ "$UI_COL" = 3 ]; then UI_S="${UI_ESC}[$2;2;${UI_RGB}m"; else UI_S="${UI_ESC}[$2;5;${UI_X256}m"; fi
}

ui_art() {   # ui_art <mood> -> UI_LINES: the picture, one line of text for two rows of pixels
  local pic y x top bot want last line cell slot i n=$((UI_H / 2))
  case $1 in   # the frames of the wave while it works, drawn once each (the cache), then as they are
    done) slot=$UI_NFRAMES; pic=$UI_PIC_DONE ;;
    fail) slot=$((UI_NFRAMES + 1)); pic=$UI_PIC_FAIL ;;
    *) slot=$UI_FRAME; pic=${UI_FRAMES[$UI_FRAME]} ;;
  esac
  if [ -n "${UI_CACHE[$((slot * n))]:-}" ]; then   # (one flat array: the n lines of slot k at k*n; bash 3.2 has no other)
    UI_LINES=(); i=0
    while [ "$i" -lt "$n" ]; do UI_LINES[$i]=${UI_CACHE[$((slot * n + i))]}; i=$((i + 1)); done
    return 0
  fi
  UI_LINES=(); y=0
  while [ "$y" -lt "$UI_H" ]; do
    line=; last=; x=0
    while [ "$x" -lt "$UI_W" ]; do
      top=${pic:$((y * UI_W + x)):1}; bot=${pic:$(((y + 1) * UI_W + x)):1}
      if [ "$top" = . ] && [ "$bot" = . ]; then
        cell=' '; want=; [ -n "$last" ] && want="${UI_ESC}[0m"
      elif [ "$top" = . ]; then
        cell=$UI_LO; ui_color "$bot" 38; want="$UI_S${UI_ESC}[49m"
      else
        cell=$UI_HI; ui_color "$top" 38; want=$UI_S
        if [ "$bot" = . ]; then want="$want${UI_ESC}[49m"; else ui_color "$bot" 48; want="$want$UI_S"; fi
      fi
      if [ -n "$want" ] && [ "$want" != "$last" ]; then line="$line$want"; last=$want; fi
      line="$line$cell"; x=$((x + 1))
    done
    UI_LINES[$((y / 2))]="$line${UI_ESC}[0m"
    y=$((y + 2))
  done
  i=0; while [ "$i" -lt "$n" ]; do UI_CACHE[$((slot * n + i))]=${UI_LINES[$i]}; i=$((i + 1)); done
}

ui_words() {   # the words beside the sign: UI_WORDS (painted lines; the first one at the second line of the picture)
  UI_WORDS=()
  ui_p accentb "Hub by Icons8"; UI_WORDS[0]=$UI_S
  UI_WORDS[1]=
  ui_p dim "Claude Code and Codex, in one place"; UI_WORDS[2]=$UI_S
  ui_p dim "installing for this computer"; UI_WORDS[3]=$UI_S
}

ui_face_buf() {   # the sign in UI_MOOD beside UI_WORDS -> UI_BUF (the lines, each ended), UI_NLINES
  local i=0 n w pad
  ui_art "$UI_MOOD"
  n=$((UI_H / 2))
  printf -v pad '%*s' $((2 + UI_W + 3)) ''
  UI_BUF=; UI_NLINES=0
  while [ "$i" -lt "$n" ] || [ "$i" -le "${#UI_WORDS[@]}" ]; do
    w=; [ "$i" -ge 1 ] && w=${UI_WORDS[$((i - 1))]:-}
    if [ "$i" -lt "$n" ]; then UI_BUF="$UI_BUF  ${UI_LINES[$i]}${w:+   }$w"$'\n'
    else UI_BUF="$UI_BUF$pad$w"$'\n'
    fi
    i=$((i + 1)); UI_NLINES=$((UI_NLINES + 1))
  done
}

ui_face() {   # print the sign in UI_MOOD beside UI_WORDS; UI_TAIL counts the lines
  ui_face_buf
  printf '%s' "$UI_BUF" >&2
  UI_TAIL=$((UI_TAIL + UI_NLINES))
}

ui_took() {   # the seconds since UI_T0 -> UI_TOOK
  local n=$((SECONDS - UI_T0))
  if [ "$n" -lt 1 ]; then UI_TOOK='<1 s'; elif [ "$n" -lt 60 ]; then UI_TOOK="$n s"; else UI_TOOK="$((n / 60)) min $((n % 60)) s"; fi
}

ui_len() {   # ui_len <text> -> UI_LEN: its columns (a mark is one, whatever the locale makes of its bytes)
  local t=${1//"$UI_DOT"/.}
  t=${t//"$UI_ELL"/.}
  UI_LEN=${#t}
}

ui_line() {   # ui_line <class> <mark> <n> <name> <detail> [took] -> UI_S: the line of a step (20 columns, then the detail)
  local room text took=${6:-} pad a b c name line
  printf -v name '%-10s' "$4"
  room=$((UI_COLS - 2 - 20 - ${#took}))
  [ -n "$took" ] && room=$((room - 2))
  text=$5
  ui_len "$text"
  [ "$UI_LEN" -gt "$room" ] && { text="${text:0:$((room - 1))}$UI_ELL"; ui_len "$text"; }
  ui_p "$1" "$2"; a=$UI_S
  ui_p dim "$3/$UI_STEPS"; b=$UI_S
  ui_p dim "$text"; c=$UI_S
  line="  $a $b  $name $c"
  if [ -n "$took" ]; then
    pad=$((UI_COLS - 2 - 20 - UI_LEN - ${#took}))
    [ "$pad" -lt 1 ] && pad=1
    ui_p dim "$took"
    printf -v pad '%*s' "$pad" ''
    line="$line$pad$UI_S"
  fi
  UI_S=$line
}

ui_tail_clear() { [ "$UI_TAIL" -gt 0 ] && printf '\033[%dA\r\033[J' "$UI_TAIL" >&2; UI_TAIL=0; return 0; }

ui_tail_buf() {   # the live part: the sign with its words, and the line of the step in progress -> UI_BUF, UI_NLINES
  local lines=0 art=
  if [ -n "$UI_ART_ON" ]; then
    ui_words; ui_face_buf; art=$UI_BUF; lines=$((UI_NLINES + 1))
    art="$art"$'\n'
  fi
  ui_live_line
  UI_BUF="$art$UI_S${UI_ESC}[K"$'\n'; UI_NLINES=$((lines + 1))
}

ui_live_line() {   # -> UI_S: the line of the step in progress (the bar of a download, when there is one)
  if [ -n "$UI_LIVE" ]; then UI_S=$UI_LIVE; else ui_line accent "$UI_NOW" "$UI_STEP" "$UI_NAME" "${UI_DETAIL:-$UI_ELL}"; fi
}

ui_tail_draw() {
  [ -n "$UI_PAUSED$UI_IDLE" ] && return 0
  ui_tail_buf
  printf '%s' "$UI_BUF" >&2
  UI_TAIL=$UI_NLINES
}

ui_tick() {   # the live part again, in place, in one write: the next frame of the sign, the live line as it is now
  [ -n "$UI_PAUSED$UI_IDLE" ] && return 0
  [ "$UI_TAIL" -ge 1 ] || return 0
  if [ -n "$UI_ART_ON" ]; then
    UI_FRAME=$(((UI_FRAME + 1) % UI_NFRAMES))
    ui_tail_buf
    printf '\033[%dA%s' "$UI_TAIL" "$UI_BUF" >&2
  else
    ui_live_line
    printf '\033[1A\r%s\033[K\n' "$UI_S" >&2
  fi
}

ui_say() {   # a line of this script's own words, above the live part
  local line
  ui_tail_clear
  while IFS= read -r line; do
    ui_p dim "$line"; printf '    %s\n' "$UI_S" >&2
  done <<< "$(printf '%s\n' "$1" | fold -s -w $((UI_COLS - 6)))"
  ui_tail_draw
}

ui_begin() {   # ui_begin <n> <name> [detail]: the step n starts; it is the live line
  ui_tail_clear
  UI_STEP=$1; UI_NAME=$2; UI_DETAIL=${3:-}; UI_T0=$SECONDS; UI_MOOD=work; UI_IDLE=; UI_LIVE=
  ui_tail_draw
}

ui_progress() {   # ui_progress <detail>: only the live line changes
  UI_DETAIL=$1
  ui_tick
}

ui_done() {   # ui_done [detail]: the step in progress is finished; its line stays
  ui_tail_clear
  ui_took
  ui_line good "$UI_OK" "$UI_STEP" "$UI_NAME" "${1:-}" "$UI_TOOK"
  printf '%s\n' "$UI_S" >&2
  UI_IDLE=1
}

ui_skip() {   # ui_skip <n> <name> <why>: a step this run does not need
  ui_tail_clear
  ui_line dim "$UI_SKIP" "$1" "$2" "$3"
  printf '%s\n' "$UI_S" >&2
  UI_IDLE=1
}

ui_interrupted() {   # Ctrl-C or a TERM: the live part off the screen, the colours back, so the prompt is a prompt
  if [ -n "${UI_ON:-}" ]; then ui_tail_clear; printf '\033[0m' >&2; fi
  return 0
}

ui_pause() { if [ -n "${UI_ON:-}" ]; then ui_tail_clear; UI_PAUSED=1; fi; return 0; }   # sudo asks for a password
ui_resume() { if [ -n "${UI_ON:-}" ]; then UI_PAUSED=; ui_tail_draw; fi; return 0; }

ui_fail() {   # ui_fail <words>: the step in progress failed - its line, then the words beside the sign
  local line i width
  ui_tail_clear
  if [ -z "$UI_IDLE" ] && [ "$UI_STEP" -gt 0 ]; then   # a step that is done keeps its line: it did not fail
    ui_took
    ui_line bad "$UI_BAD" "$UI_STEP" "$UI_NAME" "" "$UI_TOOK"
    printf '%s\n' "$UI_S" >&2
  fi
  printf '\n' >&2
  width=$((UI_COLS - 4))
  [ -n "$UI_ART_ON" ] && width=$((UI_COLS - 2 - UI_W - 3))
  UI_WORDS=()
  ui_p badb "The install stopped"; UI_WORDS[0]=$UI_S
  UI_WORDS[1]=
  i=2
  while IFS= read -r line; do UI_WORDS[$i]=$line; i=$((i + 1)); done <<< "$(printf '%s\n' "$1" | fold -s -w "$width")"
  if [ -n "${HUB_INSTALL_LOGFILE:-}" ]; then
    UI_WORDS[$i]=; ui_p dim "The log  $HUB_INSTALL_LOGFILE"; UI_WORDS[$((i + 1))]=$UI_S
  fi
  if [ -n "$UI_ART_ON" ]; then
    UI_MOOD=fail; UI_TAIL=0; ui_face; UI_TAIL=0
  else
    for line in "${UI_WORDS[@]}"; do printf '    %s\n' "$line" >&2; done
  fi
  UI_ON=
}

ui_env() {   # ui_env <step>: the words of HUB_INSTALL_UI for setup.py (look.py parse): the screen is its from here
  printf '%s.%s.%s.%s.0' "$UI_COL" "$UI_COLS" "$UI_ROWS" "$1"
}

ui_header() {   # the title, once, when there is no sign: with it, the title is beside it
  [ -n "$UI_ART_ON" ] && return 0
  ui_p bold "Hub by Icons8"; printf '\n  %s\n' "$UI_S" >&2
  ui_p dim "Claude Code and Codex, in one place"; printf '  %s\n\n' "$UI_S" >&2
}

ui_download_bar() {   # ui_download_bar <curl's pid> <the part file> <bytes it had>: the bar of the live line (and the sign), 8 times a second
  local pid=$1 part=$2 had=$3 got pct n filled i bar rest speed t0=$SECONDS el mb k=0 nap=0.12 width=$((UI_COLS - 54))   # the line is 20 + the bar + 2 + 30 columns at most
  [ "$width" -gt 20 ] && width=20; [ "$width" -lt 4 ] && width=4
  [ -n "$UI_ART_ON" ] || nap=0.3   # with the sign: 8 frames of it a second, and the bar itself is made each third time
  while kill -0 "$pid" 2>/dev/null; do
    k=$((k + 1))
    if [ -n "$UI_ART_ON" ] && [ $((k % 3)) -ne 1 ]; then ui_tick; sleep "$nap" 2>/dev/null || sleep 1; continue; fi
    got=0
    if [ -f "$part" ]; then got=$(wc -c < "$part" | tr -d ' '); fi
    pct=$((got * 100 / SHELL_SIZE)); [ "$pct" -gt 100 ] && pct=100
    filled=$((pct * width / 100))
    bar=; rest=; i=0
    while [ "$i" -lt "$width" ]; do
      if [ "$i" -lt "$filled" ]; then bar="$bar$UI_BAR"; else rest="$rest$UI_REST"; fi
      i=$((i + 1))
    done
    el=$((SECONDS - t0)); speed=
    if [ "$el" -ge 2 ] && [ "$got" -gt "$had" ]; then
      n=$(((got - had) / el / 100000))   # tenths of MB/s
      speed=" $UI_DOT $((n / 10)).$((n % 10)) MB/s"
    fi
    mb="$((got / 1000000)) of $((SHELL_SIZE / 1000000)) MB"
    ui_bar_line "$bar" "$rest" "$pct% $mb$speed"
    ui_tick
    sleep "$nap" 2>/dev/null || sleep 1
  done
  UI_LIVE=
}

ui_bar_line() {   # ui_bar_line <filled> <rest> <text> -> UI_LIVE: the line of the step with a bar in place of the detail
  local name a b c d e
  printf -v name '%-10s' "$UI_NAME"
  ui_p accent "$UI_NOW"; a=$UI_S
  ui_p dim "$UI_STEP/$UI_STEPS"; b=$UI_S
  ui_p accent "$1"; c=$UI_S
  ui_p dim "$2"; d=$UI_S
  ui_p dim "$3"; e=$UI_S
  UI_LIVE="  $a $b  $name $c$d  $e"
}
# --- end of the look

usage() {
  cat >&2 <<'EOF'
Usage: curl -fsSL <install line from the team chat> | bash -s -- [flags]
  --channel stable|main   ignored: a hub installed by the line has one channel
  --move | --stay         a hub from git is here: move it to this install, or keep it on the clone
  --instance <name>       a second hub on this machine
  --yes                   no questions (safe answers; a move only with --move)
  --no-open               do not open the browser
  --server [--user <name>]  a server, as root: a user for the hub (no sudo), what it needs, then the hub
  --keys-from <user>      with --server: that user's SSH keys reach the hub's user too (besides yours)
  --server --remove       take off what --server put on
The same flags may come in HUB_INSTALL="...".
EOF
}

# --- the machine

version_at_least() {   # version_at_least 13.4.1 13.5 -> false
  local IFS=.
  local -a a=($1) b=($2)
  local i x y
  for i in 0 1 2; do
    x=${a[$i]:-0}; y=${b[$i]:-0}
    case $x in *[!0-9]*|'') x=0 ;; esac
    case $y in *[!0-9]*|'') y=0 ;; esac
    [ "$x" -gt "$y" ] && return 0
    [ "$x" -lt "$y" ] && return 1
  done
  return 0
}

pick_platform() {   # -> PLATFORM, or a refusal
  local os arch arm v libc
  os=$(uname -s 2>/dev/null)
  arch=$(uname -m 2>/dev/null)
  case $os in
    Darwin)
      v=$(sw_vers -productVersion 2>/dev/null)
      if ! version_at_least "${v:-0}" "$HUB_INSTALL_MAC_MIN"; then
        refuse "macOS ${v:-of an unknown version} is older than $HUB_INSTALL_MAC_MIN, which the hub's environment needs. Install the hub from its git repository instead (README, "Installation and configuration")"
        return 1
      fi
      case $arch in
        arm64) PLATFORM=mac-arm64 ;;
        x86_64)
          # a terminal under Rosetta on Apple Silicon says x86_64; the real Intel Mac has no such key ("unknown oid",
          # a non-zero code) - that is an answer too, not a failure
          arm=$(sysctl -n hw.optional.arm64 2>/dev/null)
          if [ "$arm" = 1 ]; then PLATFORM=mac-arm64; else PLATFORM=mac-x64; fi ;;
        *) refuse "this Mac's processor ($arch) is not one the hub is built for"; return 1 ;;
      esac ;;
    Linux)
      case $arch in
        x86_64|amd64) PLATFORM=linux-x64 ;;
        aarch64|arm64) PLATFORM=linux-arm64 ;;
        *) refuse "this processor ($arch) is not one the hub is built for (x86_64 and arm64 are)"; return 1 ;;
      esac
      libc=$(getconf GNU_LIBC_VERSION 2>/dev/null)
      case $libc in
        "glibc "*)
          v=${libc#glibc }
          if ! version_at_least "$v" "$HUB_INSTALL_GLIBC_MIN"; then
            refuse "this Linux has glibc $v, and the hub's environment needs $HUB_INSTALL_GLIBC_MIN or newer (Ubuntu 20.04+, Debian 10+). Install the hub from its git repository instead (README, "Installation and configuration")"
            return 1
          fi ;;
        *)
          if ls /lib/ld-musl-* >/dev/null 2>&1 || { ldd --version 2>&1 | grep -qi musl; }; then
            refuse "this Linux is built on musl (Alpine and the like); the hub's environment needs glibc $HUB_INSTALL_GLIBC_MIN or newer"
          else
            refuse "could not tell this Linux's C library (getconf GNU_LIBC_VERSION gave nothing); the hub's environment needs glibc $HUB_INSTALL_GLIBC_MIN or newer"
          fi
          return 1 ;;
      esac ;;
    *) refuse "this script is for macOS and Linux; on Windows run in PowerShell: irm <install line>/install.ps1 | iex"; return 1 ;;
  esac
  return 0
}

find_programs() {   # -> FETCH (curl|wget), SUM (the sha256 command), MISSING (what the machine lacks)
  local p
  MISSING=
  if command -v curl >/dev/null 2>&1; then FETCH=curl
  elif command -v wget >/dev/null 2>&1; then FETCH=wget
  else MISSING="curl"
  fi
  for p in tar gzip; do command -v $p >/dev/null 2>&1 || MISSING="$MISSING $p"; done
  if command -v sha256sum >/dev/null 2>&1; then SUM="sha256sum"
  elif command -v shasum >/dev/null 2>&1; then SUM="shasum -a 256"
  else MISSING="$MISSING sha256sum"
  fi
  case $PLATFORM in linux-*) command -v git >/dev/null 2>&1 || MISSING="$MISSING git" ;; esac
  MISSING=${MISSING# }
  PACKAGES=$(printf '%s' "$MISSING" | sed 's/sha256sum/coreutils/')   # the package a program comes in
}

package_manager() {   # PKG_MANAGER: the system's package manager of a Linux; PKG_INSTALL: how a person types the install
  PKG_MANAGER=; PKG_INSTALL=
  if command -v apt-get >/dev/null 2>&1; then PKG_MANAGER=apt-get; PKG_INSTALL="apt install"
  elif command -v dnf >/dev/null 2>&1; then PKG_MANAGER=dnf; PKG_INSTALL="dnf install"
  elif command -v yum >/dev/null 2>&1; then PKG_MANAGER=yum; PKG_INSTALL="yum install"
  elif command -v zypper >/dev/null 2>&1; then PKG_MANAGER=zypper; PKG_INSTALL="zypper install"
  elif command -v pacman >/dev/null 2>&1; then PKG_MANAGER=pacman; PKG_INSTALL="pacman -S"
  fi
}

install_missing() {   # PACKAGES through sudo; 1 - nothing was installed (a Mac, no sudo, no right to it, no password to
  # type, the manager said no): the caller refuses with the command to run by hand
  case $PLATFORM in linux-*) ;; *) return 1 ;; esac
  [ -n "$PKG_MANAGER" ] && command -v sudo >/dev/null 2>&1 || return 1
  if sudo -n true >/dev/null 2>&1; then
    say "installing $PACKAGES ($PKG_MANAGER, through sudo)"
  elif (: < /dev/tty) 2>/dev/null; then   # a piped line: stdin is the script; sudo asks for the password on the terminal
    say "installing $PACKAGES: sudo asks for your password"
  else
    return 1
  fi
  # a word of a package is a word of this script, never typed by anybody
  # shellcheck disable=SC2086
  {
    case $PKG_MANAGER in
      apt-get) # a fresh image's index is empty: update first (a failed update is no reason to stop)
        sudo env DEBIAN_FRONTEND=noninteractive sh -c 'apt-get -q update >/dev/null 2>&1; exec apt-get -q -y install "$@"' sh $PACKAGES < /dev/null >/dev/null ;;
      dnf|yum) sudo "$PKG_MANAGER" install -y -q $PACKAGES < /dev/null >/dev/null ;;
      zypper) sudo zypper --non-interactive -q install $PACKAGES < /dev/null >/dev/null ;;
      pacman) sudo pacman -S --noconfirm --needed $PACKAGES < /dev/null >/dev/null ;;
    esac
  } || { say "$PKG_MANAGER did not install $PACKAGES"; return 1; }
}

need_programs() {   # -> FETCH (curl|wget), SUM (the sha256 command)
  find_programs
  [ -z "$MISSING" ] && return 0
  package_manager
  ui_pause   # sudo may ask for the password on the terminal
  install_missing
  local installed=$?
  ui_resume
  if [ "$installed" -eq 0 ]; then
    find_programs
    [ -n "$MISSING" ] || return 0
  fi
  if [ -n "$PKG_INSTALL" ]; then
    refuse "this machine has no $MISSING; install it (sudo $PKG_INSTALL $PACKAGES) and run the line again"
  else
    refuse "this machine has no $MISSING; install it and run the line again"
  fi
  return 1
}

data_root() {   # where the environments live, shared by the hubs of this user
  case $PLATFORM in
    mac-*) printf '%s\n' "$HOME/.local/share/agents-hub" ;;
    *) printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/agents-hub" ;;
  esac
}

state_dir() {   # the hub's state folder as the core finds it: HUB_STATE_DIR, the installers' mark, the default
  local f got
  if [ -n "${HUB_STATE_DIR:-}" ]; then printf '%s\n' "$HUB_STATE_DIR"; return; fi
  for f in "${XDG_CONFIG_HOME:-$HOME/.config}/hub-core/state-dir" "$HOME/.config/hub-core/state-dir"; do
    got=$(cat "$f" 2>/dev/null | head -n 1)
    if [ -n "$got" ]; then printf '%s\n' "$got"; return; fi
  done
  printf '%s\n' "$HOME/.hub-core"
}

free_kb() {   # free KB of the file system of a folder
  df -Pk "$1" 2>/dev/null | awk 'NR == 2 { print $4 }'
}

system_language() {   # the language tag of the system for install.py's words: ru-RU, en-US, ...
  local tag=
  case $PLATFORM in
    # ( "ru-RU", "en-US" ) on lines of their own, the first is the one the system speaks
    mac-*) tag=$(defaults read -g AppleLanguages 2>/dev/null | tr -d ' "()' | tr ',' '\n' | grep . | head -n 1) ;;
  esac
  [ -n "$tag" ] || tag=${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}
  tag=${tag%%.*}; tag=${tag%%@*}
  case $tag in ''|C|POSIX) return 0 ;; esac
  printf '%s\n' "$tag" | tr '_' '-'
}

# --- the delivery server

server_url() {   # HUB_DELIVER_URL or the one the release wrote in; https, or http to this machine only (a stand)
  local u=${HUB_DELIVER_URL:-$HUB_INSTALL_SERVER}
  case $u in @@*@@) refuse "this copy of the script names no delivery server: take the install line from the team chat (or set HUB_DELIVER_URL)"; return 1 ;; esac
  u=${u%/}
  case $u in
    https://*) ;;
    http://127.0.0.1|http://127.0.0.1:*|http://127.0.0.1/*|http://localhost|http://localhost:*|http://localhost/*|http://\[::1\]|http://\[::1\]:*|http://\[::1\]/*) ;;
    *) refuse "HUB_DELIVER_URL must be an https address (http only to this machine): $u"; return 1 ;;
  esac
  case $u in *[!A-Za-z0-9.:/\[\]_-]*) refuse "HUB_DELIVER_URL has characters an address does not: $u"; return 1 ;; esac
  printf '%s\n' "$u"
}

build_key() {   # the key the server wrote in, else HUB_DELIVER_KEY
  local k=$HUB_INSTALL_KEY_IN
  case $k in @@*@@) k=${HUB_DELIVER_KEY:-} ;; esac
  case $k in
    '') refuse "no build key: take the install line from the team chat"; return 1 ;;
    *[!A-Za-z0-9_-]*) refuse "the build key has characters a key does not: take the install line from the team chat"; return 1 ;;
  esac
  printf '%s\n' "$k"
}

key_files() {   # the key as a header file for curl, a config for wget and a plain file for install.py, all 0600; their
  # words for curl and wget (CURL_KEY, WGET_KEY) - none for the open build, which has no key
  CURL_KEY=(); WGET_KEY=()
  [ -n "$KEY" ] || return 0
  ( umask 077
    printf 'Authorization: Bearer %s\n' "$KEY" > "$WORK/header"
    printf 'header = Authorization: Bearer %s\n' "$KEY" > "$WORK/wgetrc"
    printf '%s\n' "$KEY" > "$WORK/key" ) || return 1
  CURL_KEY=(-H @"$WORK/header"); WGET_KEY=(--config="$WORK/wgetrc")
}

asset_ok() {   # asset_ok <address>: the open build may download from it - https to the pages' own host or to one of
  # HUB_INSTALL_ASSET_HOSTS, the host as it is (no port, no login); a stand (SERVER http to this machine) - http to this
  # machine as well (core/fetch.py asset_ok answers the same)
  local u=$1 auth h
  case $u in ''|*[!A-Za-z0-9._~:/?\&=%+\[\]-]*) return 1 ;; esac
  case $u in
    https://*)
      auth=${u#https://}; auth=${auth%%[/?]*}
      [ "$auth" = "$(server_host)" ] && return 0
      for h in $HUB_INSTALL_ASSET_HOSTS; do [ "$auth" = "$h" ] && return 0; done
      return 1 ;;
    http://*)
      auth=${u#http://}; auth=${auth%%[/?]*}
      case $SERVER in http://*) ;; *) return 1 ;; esac
      case ${auth#*]} in *:*[!0-9]*|*:) return 1 ;; esac   # a port is digits
      case $auth in 127.0.0.1|127.0.0.1:*|localhost|localhost:*|\[::1\]|\[::1\]:*) return 0 ;; esac
      return 1 ;;
  esac
  return 1
}

server_host() {   # the host (and port) of SERVER
  local a=${SERVER#*://}
  printf '%s\n' "${a%%[/?]*}"
}

resolve() {   # resolve <address>: the address the file is at once its redirects are followed, each one checked by
  # asset_ok before it is asked (curl -L would ask first); nothing and 1 - one of them is not to be asked
  local u=$1 n out code loc
  for n in 1 2 3 4 5 6 7 8; do
    asset_ok "$u" || { note "not an address to download from: $u"; echo "not an address to download from: $u" >> "$WORK/fetch.err"; return 1; }
    loc=
    if [ "$FETCH" = curl ]; then
      out=$(curl -sS --connect-timeout 20 --max-time 60 -r 0-0 -o /dev/null -w '%{http_code} %{redirect_url}' "$u" 2>>"$WORK/fetch.err")
      code=${out%% *}; loc=${out#"$code"}; loc=${loc# }
    else
      wget -q -S --max-redirect=0 --header='Range: bytes=0-0' --tries=1 --timeout=60 -O /dev/null "$u" 2>"$WORK/resolve.err"
      code=$(sed -n 's/^ *HTTP\/[0-9.]* \([0-9][0-9][0-9]\).*/\1/p' "$WORK/resolve.err" | tail -n 1)
      loc=$(sed -n 's/^ *[Ll]ocation: *//p' "$WORK/resolve.err" | tail -n 1 | tr -d '\r')
      case $loc in /*) loc="${u%%://*}://$(a=${u#*://}; printf '%s' "${a%%[/?]*}")$loc" ;; esac
    fi
    case $code in
      301|302|303|307|308) [ -n "$loc" ] || return 1; u=$loc ;;
      *) printf '%s\n' "$u"; return 0 ;;
    esac
  done
  echo "too many redirects: $1" >> "$WORK/fetch.err"
  return 1
}

fetch_text() {   # fetch_text <path> <file> -> the HTTP code on stdout (000: no answer)
  local code
  if [ "$FETCH" = curl ]; then
    code=$(curl -sS --connect-timeout 20 --max-time 120 --retry 3 --retry-delay 3 "${CURL_KEY[@]}" \
      -o "$2" -w '%{http_code}' "$SERVER$1" 2>"$WORK/fetch.err")
    printf '%s\n' "${code:-000}"
  else
    # -S: the server's answer goes to stderr, where its status is read (wget's own code says only "server error")
    if wget -q -S --max-redirect=0 "${WGET_KEY[@]}" --tries=3 --timeout=60 -O "$2" "$SERVER$1" 2>"$WORK/fetch.err"; then
      echo 200
    else
      code=$(sed -n 's/^ *HTTP\/[0-9.]* \([0-9][0-9][0-9]\).*/\1/p' "$WORK/fetch.err" | tail -n 1)
      printf '%s\n' "${code:-000}"
    fi
  fi
}

fetch_hint() {   # what a failed fetch means, for a person
  # the lines wget -S indents are the server's headers, not its own words
  if grep -v '^  ' "$WORK/fetch.err" 2>/dev/null | grep -qi 'certificate'; then
    echo "the server's certificate is not trusted here; if your network inspects TLS, point SSL_CERT_FILE at your organisation's root certificate and run the line again"
  else
    echo "no answer from the delivery server ($SERVER): check the connection (and HTTPS_PROXY) and run the line again"
  fi
}

flat_json() { tr -d '\r\n' < "$1"; }

shell_entry() {   # the runtime.shells entry of a platform in releases.json, as {"url":...} text, or nothing
  # releases.json is written by the server (json.dumps): the shells object holds flat objects only, its strings have
  # no braces (addresses of stored names); a string never holds an unescaped quote, so "shells": is the key itself
  flat_json "$1" | LC_ALL=C sed -n 's/.*"shells"[[:space:]]*:[[:space:]]*{\(\([^{}]*{[^{}]*}\)*[^{}]*\)}.*/\1/p' |
    LC_ALL=C sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*{\([^{}]*\)}.*/\1/p"
}

field() {   # field <entry text> <name> -> a string's or a number's value
  local v
  v=$(printf '%s\n' "$1" | LC_ALL=C sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p")
  [ -n "$v" ] || v=$(printf '%s\n' "$1" | LC_ALL=C sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p")
  printf '%s\n' "$v"
}

read_release() {   # -> SHELL_URL, SHELL_SHA, SHELL_SIZE, SHELL_RUNTIME, ARCHIVE
  local code entry
  code=$(fetch_text /v1/releases.json "$WORK/releases.json")
  case $code in
    200) ;;
    401|403)
      if [ -n "$KEY" ]; then refuse "the delivery server does not take this build key: take the install line from the team chat"
      else refuse "the server refused releases.json (HTTP $code): $SERVER"; fi
      return 1 ;;
    404) refuse "nothing is published on the delivery server yet ($SERVER)"; return 1 ;;
    000) refuse "$(fetch_hint)"; return 1 ;;
    *) refuse "the delivery server answered $code to releases.json; run the line again later"; return 1 ;;
  esac
  entry=$(shell_entry "$WORK/releases.json" "$PLATFORM")
  if [ -z "$entry" ]; then
    refuse "the delivery server has no environment for $PLATFORM yet"
    return 1
  fi
  SHELL_URL=$(field "$entry" url)
  SHELL_SHA=$(field "$entry" sha256)
  SHELL_SIZE=$(field "$entry" size)
  SHELL_RUNTIME=$(field "$entry" runtime)
  ARCHIVE=${SHELL_URL##*/}
  case $SHELL_URL in
    /v1/app/*) [ "${SHELL_URL#/v1/app/}" = "$ARCHIVE" ] || SHELL_URL= ;;   # a plain name: no "..", no folder
    https://*|http://*)   # the open build's: an address of its releases' files (its query signs nothing here)
      [ "$HUB_INSTALL_FLAVOR" = open ] && asset_ok "$SHELL_URL" || SHELL_URL=
      ARCHIVE=${SHELL_URL%%\?*}; ARCHIVE=${ARCHIVE##*/} ;;
    *) SHELL_URL= ;;
  esac
  case $ARCHIVE in ''|*[!A-Za-z0-9._+-]*|.*) SHELL_URL= ;; *.tar.gz) ;; *) SHELL_URL= ;; esac
  case $SHELL_SHA in *[!0-9a-f]*) SHELL_URL= ;; esac
  case $SHELL_SIZE in ''|*[!0-9]*) SHELL_URL= ;; esac
  case $SHELL_RUNTIME in ''|*[!0-9]*) SHELL_URL= ;; esac
  if [ -z "$SHELL_URL" ] || [ ${#SHELL_SHA} -ne 64 ]; then
    refuse "the delivery server's entry for $PLATFORM is not of the form this script knows: $entry"
    return 1
  fi
  return 0
}

# --- the environment

download() {   # the archive into downloads/<name>.part, continued if a run before was cut short; size and sum checked
  # (an address of the open build: its redirects followed again each try - a release's file storage signs its
  # addresses for minutes)
  local part="$ROOT/downloads/$ARCHIVE.part" have try got from
  mkdir -p "$ROOT/downloads" || { refuse "cannot create $ROOT/downloads"; return 1; }
  for try in 1 2 3 4; do
    have=0
    [ -f "$part" ] && have=$(wc -c < "$part" | tr -d ' ')
    if [ "$have" -gt "$SHELL_SIZE" ]; then rm -f "$part"; have=0; fi
    [ "$have" -eq "$SHELL_SIZE" ] && break
    [ "$try" -eq 1 ] && [ -z "${UI_ON:-}" ] && say "Downloading the hub's environment for $PLATFORM ($((SHELL_SIZE / 1000000)) MB)..."
    [ "$have" -gt 0 ] && note "continuing $ARCHIVE from $have"
    : > "$WORK/fetch.err"
    case $SHELL_URL in
      /*) from=$SERVER$SHELL_URL ;;
      *) from=$(resolve "$SHELL_URL") || { got=1; note "download try $try: $(tail -n 1 "$WORK/fetch.err" 2>/dev/null)"; break; } ;;
    esac
    if [ "$FETCH" = curl ] && [ -n "${UI_ON:-}" ]; then   # the look's own bar: the file's size, asked while curl runs
      curl -f -sS --connect-timeout 20 --retry 3 --retry-delay 3 -C - "${CURL_KEY[@]}" \
        -o "$part" "$from" 2>"$WORK/fetch.err" &
      UI_CPID=$!
      ui_download_bar "$UI_CPID" "$part" "$have"
      wait "$UI_CPID"
      got=$?
      UI_CPID=
    elif [ "$FETCH" = curl ] && [ -t 2 ]; then     # a terminal sees the progress bar (and curl's own errors)
      curl -f --progress-bar --connect-timeout 20 --retry 3 --retry-delay 3 -C - "${CURL_KEY[@]}" \
        -o "$part" "$from"
      got=$?
    elif [ "$FETCH" = curl ]; then
      curl -f -sS --connect-timeout 20 --retry 3 --retry-delay 3 -C - "${CURL_KEY[@]}" \
        -o "$part" "$from" 2>"$WORK/fetch.err"
      got=$?
    else
      wget -q -c --max-redirect=0 "${WGET_KEY[@]}" --tries=3 --timeout=60 -O "$part" "$from" 2>"$WORK/fetch.err"
      got=$?
    fi
    [ "$FETCH" = curl ] && [ "$got" -eq 33 ] && rm -f "$part"     # the server would not continue: start over
    [ "$got" -ne 0 ] && note "download try $try: code $got $(tail -n 1 "$WORK/fetch.err" 2>/dev/null)"
    [ "$try" -lt 4 ] && [ "$got" -ne 0 ] && sleep 3
  done
  have=0
  [ -f "$part" ] && have=$(wc -c < "$part" | tr -d ' ')
  if [ "${have:-0}" -ne "$SHELL_SIZE" ]; then
    refuse "the download of the environment did not complete ($((${have:-0} / 1000000)) of $((SHELL_SIZE / 1000000)) MB): $(fetch_hint) - the line continues where it stopped"
    return 1
  fi
  got=$($SUM "$part" 2>/dev/null | awk '{ print $1 }')
  if [ "$got" != "$SHELL_SHA" ]; then
    rm -f "$part"
    refuse "the downloaded environment does not match its sha256 (got ${got:-nothing}); it was removed - run the line again"
    return 1
  fi
  PART_FILE=$part
  return 0
}

shell_ok() {   # an environment folder this script finished: its mark, its platform, its installer, its Python starts,
  # and every file it unpacked is there as unpacked (its sheet)
  local out
  [ -f "$1/.agents-hub-shell.json" ] && [ -f "$1/install.py" ] || return 1
  grep -q "\"platform\": *\"$PLATFORM\"" "$1/.agents-hub-shell.json" 2>/dev/null || return 1
  "$1/runtime/python/bin/python3" -I -B -c 'import ssl, sqlite3' >/dev/null 2>&1 || { note "$1: its Python does not start"; return 1; }
  out=$("$1/runtime/python/bin/python3" -I -B "$WORK/unpack.py" check "$1" 2>&1)
  case $out in
    "ok "*) return 0 ;;
    *) note "$1 is broken: $(printf '%s' "$out" | tail -n 1)"; return 1 ;;
  esac
}

pick_env() {   # ENV: the environment of this release's runtime the hub runs on, or the default folder; a broken one is
  # left as it is (a running hub holds it) and a new one goes next to it - the pointer moves, the old one is removed later
  local base="$ROOT/shells/$SHELL_RUNTIME-$HUB_INSTALL_FLAVOR" shells cur n
  shells=$(cd "$ROOT/shells" && pwd -P) || shells=
  cur=$(cd "$1/app" 2>/dev/null && pwd -P) || cur=
  [ -n "$shells" ] && [ "${cur%/*}" = "$shells" ] || cur=
  case ${cur##*/} in
    "$SHELL_RUNTIME-$HUB_INSTALL_FLAVOR"-[0-9]*)
      ENV="$ROOT/shells/${cur##*/}"
      shell_ok "$ENV" && return 0 ;;
  esac
  ENV=$base
  shell_ok "$ENV" && return 0
  if [ -e "$base" ] || [ -L "$base" ]; then
    n=$(date +%s)
    while [ -e "$base-$n" ] || [ -e "$base-$n.part" ]; do n=$((n + 1)); done
    ENV="$base-$n"
  fi
  return 1
}

write_helper() {   # the unpack/check program for the environment's Python (deploy/oneline/install.ps1 holds the same)
  cat > "$WORK/unpack.py" <<'HUB_UNPACK_PY'
# --- unpack.py (the same in install.sh and install.ps1) ---
"""unpack|verify <archive> <part folder> [<final folder>]: unpack the archive into the part folder (Windows: through
the long-path prefix, without which tar.exe and Python lose files) or take what tar unpacked there; check that
every member of the archive is there as itself (a folder, a file of its size, a link to its target), one top folder of
the archive dropped; write the sheet of the tree (SHEET: every file's size and sha256, every link's target) and the
mark (HUB_SHELL_MARK, JSON) with the count; rename the part to the final folder when given.
check <folder>: the folder against its sheet: every file of it there with its size and sum, every link to its target
(what the environment adds itself, such as the bytecode (its Python's too: own_bytecode) and Windows' links, is not in
the sheet and not looked at).
Prints "ok <files>" or the reason, exits 0 or 1."""
import hashlib
import json
import os
import stat
import sys
import tarfile

WIN = os.name == "nt"
SHEET = ".agents-hub-shell.files"


def long(path):
    path = os.path.abspath(path)
    return "\\\\?\\" + path if WIN and not path.startswith("\\\\?\\") else path


def rel_of(name):
    parts = name.replace("\\", "/").split("/")
    while len(parts) > 1 and parts[0] == ".":
        parts = parts[1:]
    if parts[0] in ("", ".", ".."):
        return None, parts[0]
    rest = [p for p in parts[1:] if p not in ("", ".")]
    if not rest:
        return "", parts[0]
    if any(p == ".." or ":" in p for p in rest):
        raise ValueError("a member reaches outside its folder: " + name)
    return "/".join(rest), parts[0]


def inside(rel, target):
    base = os.path.dirname(rel)
    joined = os.path.normpath(os.path.join(base, target)).replace("\\", "/")
    return not (target.startswith(("/", "\\")) or joined == ".." or joined.startswith("../"))


def own_bytecode(rel):
    """The bytecode of the environment's Python: the Python comes with .pyc older than their sources (a build the
    factory's machine cannot start keeps them) and the environment's installer compiles them again (install.py
    prepare). Not written into the sheet; one an earlier sheet holds is not looked at."""
    return rel.startswith("runtime/python/") and rel.endswith(".pyc")


def place(root, rel):
    return os.path.join(root, *rel.split("/")) if rel else root


def copy(src, path, umask, mode):
    """src's bytes into a new file at path -> their sha256."""
    h = hashlib.sha256()
    with open(path, "wb") as out:
        while True:
            block = src.read(1 << 20)
            if not block:
                break
            h.update(block)
            out.write(block)
    os.chmod(path, (mode & 0o777) & ~umask)
    return h.hexdigest()


def sum_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def main(argv):
    mode, archive, part = argv[1], argv[2], argv[3]
    final = argv[4] if len(argv) > 4 else None
    root = long(part)
    top = None
    files = 0
    sizes = {}
    sheet = {"files": {}, "links": {}}
    umask = os.umask(0o077)
    os.umask(umask)
    with tarfile.open(archive, "r:gz") as t:
        for m in t:
            rel, first = rel_of(m.name)
            if rel is None:
                return "a member outside the archive's top folder: " + m.name
            if top is None:
                top = first
            elif first != top:
                return "the archive has more than one top folder: " + first
            if rel == "":
                continue
            target = place(root, rel)
            got = None
            if mode == "unpack":
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                elif m.isreg():
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    got = copy(t.extractfile(m), target, umask, m.mode)
                elif m.issym() and not WIN:
                    if not inside(rel, m.linkname):
                        return "a link reaches outside its folder: " + m.name
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    os.symlink(m.linkname, target)
                elif m.islnk():
                    src_rel, _ = rel_of(m.linkname)
                    if not src_rel:
                        return "a hard link outside the archive's folder: " + m.name
                    with open(place(root, src_rel), "rb") as a:
                        got = copy(a, target, umask, m.mode)
                else:
                    return "a member of a kind the environment never has: " + m.name
            try:
                st = os.lstat(target)
            except OSError:
                return "missing after unpacking: " + rel
            if m.isdir():
                ok = stat.S_ISDIR(st.st_mode)
            elif m.isreg():
                ok, sizes[rel] = stat.S_ISREG(st.st_mode) and st.st_size == m.size, m.size
                files += 1
            elif m.issym():
                ok = stat.S_ISLNK(st.st_mode) and os.readlink(target) == m.linkname and inside(rel, m.linkname)
                sheet["links"][rel] = m.linkname
                files += 1
            elif m.islnk():
                src_rel, _ = rel_of(m.linkname)
                ok = stat.S_ISREG(st.st_mode) and st.st_size == sizes.get(src_rel, -1)
                files += 1
            else:
                ok = False
            if not ok:
                return "not as in the archive: " + rel
            if (m.isreg() or m.islnk()) and not own_bytecode(rel):
                sheet["files"][rel] = [st.st_size, got or sum_of(target)]
    if top is None:
        return "the archive is empty"
    with open(os.path.join(root, SHEET), "w", encoding="utf-8") as f:
        json.dump(sheet, f, separators=(",", ":"), sort_keys=True)
    mark = json.loads(os.environ.get("HUB_SHELL_MARK") or "{}")
    mark.update(files=files, top=top)
    with open(os.path.join(root, ".agents-hub-shell.json"), "w", encoding="utf-8") as f:
        json.dump(mark, f, indent=1, sort_keys=True)
    if final:
        os.replace(root, long(final))
    print("ok", files)
    return None


def check(folder):
    root = long(folder)
    try:
        with open(os.path.join(root, SHEET), encoding="utf-8") as f:
            sheet = json.load(f)
        items, links = sheet["files"], sheet["links"]
        if not (isinstance(items, dict) and isinstance(links, dict)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        return "no sheet of the environment's files (" + SHEET + ")"
    for rel, target in links.items():
        path = place(root, rel)
        if not os.path.islink(path) or os.readlink(path) != target:
            return "not as unpacked: " + rel
    items = {rel: entry for rel, entry in items.items() if not own_bytecode(rel)}
    for rel, entry in items.items():
        path = place(root, rel)
        try:
            st = os.lstat(path)
        except OSError:
            return "missing: " + rel
        if not stat.S_ISREG(st.st_mode) or st.st_size != entry[0] or sum_of(path) != entry[1]:
            return "not as unpacked: " + rel
    print("ok", len(items) + len(links))
    return None


if __name__ == "__main__":
    try:
        why = check(sys.argv[2]) if sys.argv[1:2] == ["check"] else main(sys.argv)
    except (OSError, ValueError, tarfile.TarError, EOFError, IndexError) as e:
        why = f"{type(e).__name__}: {e}"
    if why:
        print(why)
        sys.exit(1)
# --- end of unpack.py ---
HUB_UNPACK_PY
}

mark_json() {
  printf '{"platform": "%s", "flavor": "%s", "runtime": %s, "archive": "%s", "sha256": "%s", "at": "%s"}' \
    "$PLATFORM" "$HUB_INSTALL_FLAVOR" "$SHELL_RUNTIME" "$ARCHIVE" "$SHELL_SHA" "$(date -u +%FT%TZ)"
}

unpack() {   # the archive into shells/<runtime>-<flavor>.part with tar, every file checked, renamed into place
  local part="$ENV.part" out
  rm -rf "$part" || { refuse "cannot remove the earlier unfinished $part"; return 1; }
  mkdir -p "$part" || { refuse "cannot create $part"; return 1; }
  [ -z "${UI_ON:-}" ] && say "Unpacking..."
  if ! tar -xzf "$PART_FILE" -C "$part" --strip-components=1 2>"$WORK/tar.err"; then
    rm -rf "$part"
    refuse "unpacking the environment failed: $(tail -n 1 "$WORK/tar.err")"
    return 1
  fi
  if [ ! -x "$part/runtime/python/bin/python3" ]; then
    rm -rf "$part"
    refuse "the environment has no Python at runtime/python/bin/python3"
    return 1
  fi
  out=$(HUB_SHELL_MARK=$(mark_json) "$part/runtime/python/bin/python3" -I -B "$WORK/unpack.py" verify "$PART_FILE" "$part" 2>&1)
  case $out in
    "ok "*) note "checked: $out" ;;
    *) rm -rf "$part"
       refuse "the environment did not unpack whole or its Python does not start here: $(printf '%s' "$out" | tail -n 1)"
       return 1 ;;
  esac
  mv "$part" "$ENV" || { rm -rf "$part"; refuse "cannot put the environment in place at $ENV"; return 1; }
  rm -f "$PART_FILE"
  return 0
}

# --- one install at a time

take_lock() {
  local lock="$ROOT/install.lock" waited=0 owner pid said=
  while :; do
    if mkdir "$lock" 2>/dev/null; then
      printf 'pid=%s host=%s since=%s\n' "$$" "$(hostname 2>/dev/null)" "$(date -u +%FT%TZ)" > "$lock/owner"
      LOCK=$lock
      return 0
    fi
    owner=$(cat "$lock/owner" 2>/dev/null)
    pid=${owner#pid=}; pid=${pid%% *}
    case $pid in ''|*[!0-9]*) pid= ;; esac
    if [ -n "$owner" ] && [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      # its holder is gone: take the stale lock aside by a rename (one taker wins), make sure it is the stale one
      if mv "$lock" "$lock.stale.$$" 2>/dev/null; then
        if [ "$(cat "$lock.stale.$$/owner" 2>/dev/null)" = "$owner" ]; then
          rm -rf "$lock.stale.$$"
        else
          mv "$lock.stale.$$" "$lock" 2>/dev/null || rm -rf "$lock.stale.$$"
        fi
      fi
      continue
    fi
    if [ -z "$said" ]; then
      say "Another install of the hub is running (${owner:-starting}); waiting for it, up to $((HUB_INSTALL_LOCK_WAIT / 60)) min..."
      said=1
    fi
    if [ "$waited" -ge "$HUB_INSTALL_LOCK_WAIT" ]; then
      refuse "another install still runs (${owner:-unknown}); run the line again when it is over"
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
}

cleanup() {
  [ -n "${UI_CPID:-}" ] && kill "$UI_CPID" 2>/dev/null
  [ -n "${LOCK:-}" ] && [ "$(cat "$LOCK/owner" 2>/dev/null | cut -d' ' -f1)" = "pid=$$" ] && rm -rf "$LOCK"
  [ -n "${WORK:-}" ] && rm -rf "$WORK"
  LOCK=; WORK=
}

# --- the flags

keys_from() {   # keys_from <name>: one more --keys-from user - a name (no number, no pattern), added to KEYS_FROM
  case $1 in
    [ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_]*) ;;
    *) refuse "--keys-from takes a user name: $1"; return 1 ;;
  esac
  case $1 in
    *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-]*) refuse "--keys-from takes a user name: $1"; return 1 ;;
  esac
  KEYS_FROM="${KEYS_FROM:+$KEYS_FROM }$1"
}

parse_flags() {   # parse_flags <where> <words>: one source -> CHANNEL MOVE STAY INSTANCE YES NO_OPEN SERVE SUSER REMOVE
  # KEYS_FROM
  local from=$1
  shift
  CHANNEL=; MOVE=; STAY=; INSTANCE=; YES=; NO_OPEN=; SERVE=; SUSER=; REMOVE=; KEYS_FROM=
  while [ $# -gt 0 ]; do
    case $1 in
      --channel) [ $# -ge 2 ] || { refuse "--channel needs a value: stable or main"; usage; return 64; }
                 CHANNEL=$2; shift ;;
      --channel=*) CHANNEL=${1#--channel=} ;;
      --move) MOVE=1 ;;
      --stay) STAY=1 ;;
      --instance) [ $# -ge 2 ] || { refuse "--instance needs a name"; usage; return 64; }
                  INSTANCE=$2; shift ;;
      --instance=*) INSTANCE=${1#--instance=} ;;
      --yes|-y) YES=1 ;;
      --no-open) NO_OPEN=1 ;;
      --server) SERVE=1 ;;
      --user) [ $# -ge 2 ] || { refuse "--user needs a name"; usage; return 64; }
              SUSER=$2; shift ;;
      --user=*) SUSER=${1#--user=} ;;
      --remove) REMOVE=1 ;;
      --keys-from) [ $# -ge 2 ] || { refuse "--keys-from needs a user name"; usage; return 64; }
                   keys_from "$2" || return 64; shift ;;
      --keys-from=*) keys_from "${1#--keys-from=}" || return 64 ;;
      -h|--help) usage; return 2 ;;
      *) refuse "unknown flag${from}: $1"; [ -z "$from" ] && usage; return 64 ;;
    esac
    shift
  done
  case $CHANNEL in
    ''|stable) ;;
    main|nightly) CHANNEL=nightly ;;
    *) refuse "--channel is stable or main, not $CHANNEL"; return 64 ;;
  esac
  if [ -n "$MOVE" ] && [ -n "$STAY" ]; then refuse "--move and --stay are two answers to one question: give one"; return 64; fi
  if [ -n "$INSTANCE" ]; then
    case $INSTANCE in
      *[!a-z0-9-]*|-*|'') refuse "--instance takes a name of small letters, digits and dashes: $INSTANCE"; return 64 ;;
    esac
    [ ${#INSTANCE} -le 32 ] || { refuse "--instance takes a name of up to 32 characters"; return 64; }
  fi
  return 0
}

# --- a server: --server (a cloud VM's user data runs it as root; by SSH - root or a user with sudo without a password)
# Only this part runs as root, and only the steps below; the hub itself is installed by this same script run again under
# the hub's user (runuser -l), from a copy root wrote and that user cannot change. The hub's sessions run without
# asking, so that user has no sudo and no group that is root in disguise (docker, lxd, ...). What it put on is written
# down in server.json, so a run again leaves it as it is and --remove takes off that and nothing else.
# HUB_INSTALL_SYSROOT (tests only) is put before every system path.

sys_path() { printf '%s%s' "${HUB_INSTALL_SYSROOT:-}" "$1"; }

self_text() {   # self_text flags|hub: this script as it runs now - its settings, the person's environment that sudo
  # and runuser drop (in the file, never on a command line every account sees: a key, a proxy's password), its
  # functions, its last line. "flags": HUB_INSTALL too (the run through sudo); "hub": the hub's own run (its flags are
  # words)
  local v flags=
  [ "${1:-}" = flags ] && flags=HUB_INSTALL
  printf '#!/bin/bash\n'
  for v in $HUB_INSTALL_CONSTANTS; do declare -p "$v"; done
  for v in HUB_DELIVER_URL HUB_DELIVER_KEY $flags HUB_INSTALL_SYSROOT HUB_INSTALL_METADATA HUB_LANG LANG \
           LC_ALL HTTPS_PROXY https_proxy HTTP_PROXY http_proxy NO_PROXY no_proxy SSL_CERT_FILE SSH_CONNECTION; do
    [ -n "${!v:-}" ] && printf 'export %s=%q\n' "$v" "${!v}"
  done
  declare -f
  printf 'main "$@"\n'
}

as_server() {   # --server: root does it; a user with sudo without a password hands it to root
  local f code
  if [ "$(id -u)" != 0 ]; then
    if ! sudo -n true 2>/dev/null; then
      refuse "--server sets up the machine: run it as root, or as a user with sudo without a password"
      return 1
    fi
    f=$(mktemp "${TMPDIR:-/tmp}/agents-hub-server.XXXXXX") || { refuse "cannot make a temporary file"; return 1; }
    self_text flags > "$f"
    sudo -n bash "$f" "$@" < /dev/null
    code=$?
    rm -f "$f"
    return "$code"
  fi
  umask 022   # what root writes here is read by every user (apt's configuration, the login message)
  server_lock || return 1
  if [ -n "$REMOVE" ]; then
    server_remove
  else
    server_check && server_install   # a refusal of the checks leaves nothing on the disk (the lock is in /run)
  fi
}

server_lock() {   # one --server at a time: the user data's run and a person's line by SSH may meet. In /run: gone at
  # a reboot, never removed by a run (a removed lock file lets a waiting run and a new one in together)
  local f
  command -v flock >/dev/null 2>&1 || { refuse "--server needs flock (util-linux)"; return 1; }
  f=$(sys_path /run)/agents-hub-server.lock
  exec 9> "$f" || { refuse "cannot open $f"; return 1; }
  if ! flock -n 9; then
    say "another --server run is going (the machine's user data?): waiting for it, up to $((HUB_INSTALL_LOCK_WAIT / 60)) minutes"
    flock -w "$HUB_INSTALL_LOCK_WAIT" 9 || { refuse "another --server run still holds $f"; return 1; }
  fi
}

# server.json: one line of "name": "value" pairs (read without jq), HUB_INSTALL_SERVER_FIELDS; SV_<name> hold it

server_file() { sys_path /var/lib/agents-hub/server.json; }

server_load() {
  local f n line
  f=$(server_file)
  line=$(cat "$f" 2>/dev/null)
  for n in $HUB_INSTALL_SERVER_FIELDS; do
    printf -v "SV_$n" '%s' "$(printf '%s' "$line" | sed -n "s/.*\"$n\": *\"\([^\"]*\)\".*/\1/p")"
  done
}

server_save() {
  local f n v out=
  f=$(server_file)
  mkdir -p "$(dirname "$f")" && chmod 755 "$(dirname "$f")" || return 1
  for n in $HUB_INSTALL_SERVER_FIELDS; do
    v=SV_$n
    v=${!v//\\/\\\\}; v=${v//\"/}
    out="$out${out:+, }\"$n\": \"$v\""
  done
  printf '{%s}\n' "$out" > "$f.part" && chmod 644 "$f.part" && mv -f "$f.part" "$f"
}

server_log() { sys_path /var/log/agents-hub-server.log; }

server_check() {   # -> MEM_KB, or a refusal before anything is changed
  local id free need have u
  [ "$(uname -s 2>/dev/null)" = Linux ] || { refuse "--server is for a Linux server"; return 1; }
  id=$(. "$(sys_path /etc/os-release)" 2>/dev/null; printf '%s %s' "${ID:-}" "${ID_LIKE:-}")
  case " $id " in
    *" ubuntu "*|*" debian "*) ;;
    *) refuse "--server sets up Ubuntu or Debian; on another Linux run the line as an ordinary user, without --server"
       return 1 ;;
  esac
  command -v apt-get >/dev/null 2>&1 || { refuse "--server needs apt-get"; return 1; }
  [ -d "$(sys_path /run/systemd/system)" ] || { refuse "--server needs systemd: the hub is a service that starts with the machine"; return 1; }
  case $(uname -m 2>/dev/null) in
    x86_64|amd64|aarch64|arm64) ;;
    *) refuse "--server is for x86_64 or arm64, not $(uname -m 2>/dev/null)"; return 1 ;;
  esac
  MEM_KB=$(awk '/^MemTotal:/ {print $2}' "$(sys_path /proc/meminfo)" 2>/dev/null)
  case $MEM_KB in ''|*[!0-9]*) MEM_KB=0 ;; esac
  if [ "$MEM_KB" -lt "$HUB_INSTALL_SERVER_RAM_KB" ]; then
    # GB by awk -v in the C locale: escaped quotes inside $( ) inside a quoted string break in bash 3.2 (macOS), and a
    # locale with a decimal comma would print 3,5
    need=$(LC_ALL=C awk -v k="$HUB_INSTALL_SERVER_RAM_KB" 'BEGIN { printf "%.1f", k / 1048576 }')
    have=$(LC_ALL=C awk -v k="$MEM_KB" 'BEGIN { printf "%.1f", k / 1048576 }')
    refuse "the hub needs at least $need GB of memory, this server has $have GB"
    return 1
  fi
  free=$(free_kb "$(sys_path /home)")
  if [ -n "$free" ] && [ "$free" -lt "$HUB_INSTALL_SERVER_DISK_KB" ]; then
    refuse "the hub needs $((HUB_INSTALL_SERVER_DISK_KB / 1048576)) GB free for the home folders, there are $((free / 1048576)) GB"
    return 1
  fi
  for u in $KEYS_FROM; do
    [ "$u" = "$SUSER" ] && { refuse "--keys-from takes the keys of another user, not of the hub's $SUSER"; return 1; }
    getent passwd "$u" >/dev/null 2>&1 || { refuse "--keys-from: there is no user $u here"; return 1; }
  done
  return 0
}

server_user() {   # the hub's user: made here, or the one made here before; never somebody else's
  local u=$SUSER groups
  if [ -n "$SV_user" ] && [ "$SV_user" != "$u" ]; then
    refuse "the hub of this server runs under the user $SV_user (made by --server): give --user $SV_user"
    return 1
  fi
  if id -u "$u" >/dev/null 2>&1; then
    if [ "$SV_user" != "$u" ]; then
      refuse "a user $u is here already, not made by --server: remove it, or give --user <another name>"
      return 1
    fi
  else
    SV_accounts=$(getent passwd | cut -d: -f1 | tr '\n' ' ')   # the person's: what was here before the hub's user
    useradd --create-home --user-group --shell /bin/bash --comment "Hub by Icons8" "$u" ||
      { refuse "cannot add the user $u"; return 1; }
    usermod -p '*' "$u" || { refuse "cannot set up the user $u"; return 1; }   # no password, its SSH keys still work
    SV_user=$u; SV_user_created=1
    server_save || { refuse "cannot write $(server_file)"; return 1; }
    say "made the user $u for the hub (no password, no sudo)"
  fi
  groups=$(id -nG "$u" 2>/dev/null)
  if [ "$groups" != "$u" ]; then
    refuse "the user $u is in the groups $groups: the hub's sessions run without asking and must not get root through sudo, docker or lxd"
    return 1
  fi
  return 0
}

server_key_re() {   # a key of an authorized_keys line: its type and its base64
  printf '%s' '(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) [A-Za-z0-9+/]+=*'
}

server_keys_unlimited() {   # the key lines of an authorized_keys on stdin that have no options: a limited line (from=,
  # command=, restrict) must not get a shell as the hub's user
  grep -E "^[[:space:]]*$(server_key_re)([[:space:]].*)?\$" | sed 's/^[[:space:]]*//'
}

server_keys_file() {   # server_keys_file <user>: that user's authorized_keys; another user's is read as that user, so a
  # link there reaches nothing that user cannot read
  local home
  if [ "$1" = root ]; then cat "$(sys_path /root/.ssh/authorized_keys)" 2>/dev/null; return 0; fi
  home=$(getent passwd "$1" | cut -d: -f6)
  [ -n "$home" ] && runuser -u "$1" -- cat "$home/.ssh/authorized_keys" 2>/dev/null
  return 0
}

server_keys_of() {   # server_keys_of <user>: the unlimited keys of that user. Root's line that a cloud wrote to send
  # you to its default user (command="echo 'Please login as the user \"ubuntu\"...') is the key of the one who made
  # the machine: it counts when that user has the same key unlimited
  local raw line u blob
  raw=$(server_keys_file "$1")
  printf '%s\n' "$raw" | server_keys_unlimited
  [ "$1" = root ] || return 0
  printf '%s\n' "$raw" | grep -F 'Please login as the user' | while IFS= read -r line; do
    u=$(printf '%s\n' "$line" | sed -n 's/.*Please login as the user \\\{0,1\}"\([a-z_][a-z0-9_.-]*\)\\\{0,1\}" rather than the user \\\{0,1\}"root\\\{0,1\}".*/\1/p')
    blob=$(printf '%s\n' "$line" | grep -oE "$(server_key_re)" | tail -1 | cut -d' ' -f2)
    [ -n "$u" ] && [ -n "$blob" ] && [ "$u" != root ] && [ "$u" != "$SUSER" ] || continue
    server_keys_file "$u" | server_keys_unlimited | awk -v b="$blob" '$2 == b'
  done
}

server_keys_who() {   # the one who ran it: the user that sudo came from, else root (a cloud's user data, root by SSH)
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "$SUSER" ] && getent passwd "$SUDO_USER" >/dev/null 2>&1; then
    printf '%s' "$SUDO_USER"
  else
    printf root
  fi
}

server_keys() {   # the keys that reach this server for the one who ran it reach the hub's user too: the tunnel goes as
  # <user>@<server>. A file that is there already is left as it is; only --keys-from adds to it, and only keys it lacks.
  # The hub's home is its user's and its sessions': looked at and written as that user, never by root, and a link
  # there is never followed
  local home have= keys= k u users from= seen=' ' code
  home=$(getent passwd "$SUSER" | cut -d: -f6)
  [ -n "$home" ] || { refuse "the user $SUSER has no home folder"; return 1; }
  KEYS_WHO=$(server_keys_who)
  users="$KEYS_WHO $KEYS_FROM"   # names only (keys_from): no pattern to expand
  if runuser -u "$SUSER" -- sh -c '[ -e "$1/.ssh/authorized_keys" ] || [ -L "$1/.ssh/authorized_keys" ]' sh "$home"; then
    [ -n "$KEYS_FROM" ] || return 0
    users=$KEYS_FROM
    have=$(runuser -u "$SUSER" -- sh -c '[ -L "$1/.ssh" ] || [ -L "$1/.ssh/authorized_keys" ] || cat "$1/.ssh/authorized_keys"' sh "$home" |
      server_keys_unlimited)
  fi
  for u in $users; do
    case $seen in *" $u "*) continue ;; esac
    seen="$seen$u "
    # only the keys not taken yet: a user whose keys are all there already is not named
    k=$(server_keys_of "$u" | HAVE="$have$keys" awk 'BEGIN { n = split(ENVIRON["HAVE"], l, "\n")
        for (i = 1; i <= n; i++) { split(l[i], f, " "); if (f[2] != "") seen[f[1] " " f[2]] = 1 } }
      NF && !seen[$1 " " $2]++')
    [ -n "$k" ] || continue
    keys="$keys$k"$'\n'; from="$from${from:+, }$u"
  done
  keys=${keys%$'\n'}
  if [ -z "$keys" ]; then
    [ -n "$have" ] || NOKEYS=1
    return 0
  fi
  printf '%s\n' "$keys" | runuser -u "$SUSER" -- sh -c 'umask 077
    [ -L "$1/.ssh" ] && exit 3
    [ -d "$1/.ssh" ] || mkdir "$1/.ssh" || exit 1
    f=$1/.ssh/authorized_keys
    [ -L "$f" ] && exit 3
    if [ -e "$f" ]; then   # its last line may have no newline: the next key goes on a line of its own
      [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ] && echo >> "$f"
      cat >> "$f"
    else
      set -C; cat > "$f"
    fi && chmod 700 "$1/.ssh" && chmod 600 "$f"' sh "$home"
  code=$?
  case $code in
    0) say "the SSH keys of $from now reach $SUSER too: $(printf '%s\n' "$keys" | wc -l | tr -d ' ')" ;;
    3) NOKEYS=link ;;
    *) refuse "cannot write $home/.ssh/authorized_keys"; return 1 ;;
  esac
  return 0
}

installed() { dpkg-query -W -f '${Status}' "$1" 2>/dev/null | grep -q 'ok installed'; }

server_packages() {   # git and curl for the hub, the system's unattended updates, Google Chrome for the agents (x86_64)
  local need= chrome= p d deb apt f
  apt="apt-get -o DPkg::Lock::Timeout=600 -q -y"   # cloud-init's own first upgrade may hold the lock
  for p in git curl ca-certificates unattended-upgrades; do installed "$p" || need="$need $p"; done
  case $(uname -m) in x86_64|amd64) installed google-chrome-stable || chrome=1 ;; esac
  if [ -n "$need$chrome" ]; then   # a cloud image's index is old: its dependencies' versions are gone (404)
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 -q update >/dev/null 2>&1 || :
  fi
  if [ -n "$need" ]; then
    say "installing:$need"
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive $apt install $need >/dev/null || { refuse "apt-get could not install:$need"; return 1; }
  fi
  f=$(sys_path /etc/apt/apt.conf.d/20auto-upgrades)
  if [ ! -e "$f" ]; then   # Debian's cloud images: unattended-upgrades is there, turned off
    printf '%s\n' 'APT::Periodic::Update-Package-Lists "1";' 'APT::Periodic::Unattended-Upgrade "1";' > "$f" &&
      chmod 644 "$f" && SV_auto_conf=$f   # apt reads its configuration as every user
  fi
  case $(uname -m) in x86_64|amd64) ;; *) NOCHROME=arch; return 0 ;; esac
  if [ -n "$chrome" ]; then
    say "installing Google Chrome for the hub's agents"
    deb=google-chrome-stable_current_amd64.deb
    # root runs nothing the hub's user can write: the package is fetched by root into its own new folder
    d=$(mktemp -d) && chmod 755 "$d" && curl -fsSL -o "$d/$deb" "$HUB_INSTALL_CHROME_DEB" &&
      DEBIAN_FRONTEND=noninteractive $apt install "$d/$deb" >/dev/null || NOCHROME=failed
    [ -n "$d" ] && rm -rf "$d"
    [ -n "${NOCHROME:-}" ] && { say "Google Chrome did not install: the hub runs, its agents have no browser yet"; return 0; }
  fi
  f=$(sys_path /etc/apt/apt.conf.d/51agents-hub-chrome)
  if [ ! -e "$f" ]; then   # unattended upgrades take the system's security updates only: Chrome's own repository too
    printf '%s\n' '// Hub by Icons8 (install.sh --server): the Chrome its agents browse with is kept up to date' \
      'Unattended-Upgrade::Origins-Pattern { "origin=Google LLC,codename=stable"; };' > "$f" && chmod 644 "$f" &&
      SV_chrome_conf=$f
  fi
  return 0
}

server_swap() {   # a swap file on a small machine with none: a burst of sessions slows down instead of being killed
  local f
  f=$(sys_path /swapfile)
  [ "$MEM_KB" -lt "$HUB_INSTALL_SERVER_SWAP_BELOW_KB" ] || return 0
  [ -n "$(swapon --show --noheadings 2>/dev/null)" ] && return 0
  [ -e "$f" ] && return 0
  say "adding a swap file of $((HUB_INSTALL_SERVER_SWAP_MB / 1024)) GB"
  { fallocate -l "${HUB_INSTALL_SERVER_SWAP_MB}M" "$f" 2>/dev/null ||
      dd if=/dev/zero of="$f" bs=1M count="$HUB_INSTALL_SERVER_SWAP_MB" status=none; } &&
    chmod 600 "$f" && mkswap "$f" >/dev/null && swapon "$f" || { rm -f "$f"; say "no swap file: the hub runs without one"; return 0; }
  printf '%s none swap sw 0 0  # agents-hub\n' /swapfile >> "$(sys_path /etc/fstab)"
  SV_swap=/swapfile
}

server_linger() {   # the hub's service starts with the machine, nobody signed in
  [ -e "$(sys_path /var/lib/systemd/linger)/$SUSER" ] && return 0
  loginctl enable-linger "$SUSER" || { refuse "loginctl enable-linger $SUSER failed"; return 1; }
  SV_linger=1
}

server_motd() {   # what a person sees when they sign in by SSH: the hub's state and the way to its page
  local f
  f=$(sys_path /etc/update-motd.d/90-agents-hub)
  [ -d "$(dirname "$f")" ] || return 0
  cat > "$f" <<'HUB_MOTD'
#!/bin/sh
# Hub by Icons8 (install.sh --server): the hub's state and the way to its page. Reads one file, nothing else.
f=/var/lib/agents-hub/server.json
[ -r "$f" ] || exit 0
get() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$f"; }
port=$(get tunnel | sed -n 's/^ssh -N -L \([0-9]*\):.*/\1/p')
case $(get status) in
  done) echo
        echo "Hub by Icons8 runs here under the user $(get user). Its page, from your computer:"
        echo "  $(get tunnel)"
        echo "  and keep it running; then open http://localhost:${port:-8787} there." ;;
  installing) echo; echo "Hub by Icons8 is being installed: /var/log/agents-hub-server.log" ;;
  *) echo; echo "Hub by Icons8: the install did not finish (code $(get code)): /var/log/agents-hub-server.log; run the line again." ;;
esac
HUB_MOTD
  chmod 755 "$f" && SV_motd=$f
}

server_run_hub() {   # this script again under the hub's user: the ordinary install, nobody to ask
  local d f home code log
  log=$(server_log)
  home=$(getent passwd "$SUSER" | cut -d: -f6)
  d=$(mktemp -d "${TMPDIR:-/tmp}/agents-hub-server.XXXXXX") || { refuse "cannot make a temporary folder"; return 1; }
  f=$d/install.sh
  self_text hub > "$f" && chgrp "$SUSER" "$d" "$f" && chmod 750 "$d" && chmod 640 "$f" ||
    { rm -rf "$d"; refuse "cannot write $f"; return 1; }
  set -- --yes --no-open
  [ "$CHANNEL" = nightly ] && set -- "$@" --channel main
  say "installing the hub under the user $SUSER"
  runuser -l "$SUSER" -c "cd $(printf %q "$home") && bash $(printf %q "$f") $*" < /dev/null 2>&1 |
    tee -a "$log"
  code=${PIPESTATUS[0]}
  rm -rf "$d"
  return "$code"
}

server_tunnel() {   # the hub's own words on how to reach its page (setup.py says them), else the default ports
  local line
  line=$(grep -o 'ssh -N -L .*' "$(server_log)" 2>/dev/null | tail -1)
  [ -n "$line" ] || line="ssh -N -L 8787:127.0.0.1:8787 -L 8788:127.0.0.1:8788 $SUSER@<server address>"
  printf '%s' "$line"
}

server_role() {   # a cloud role on the machine: the sessions can take its rights through the metadata address
  local base t
  base=${HUB_INSTALL_METADATA:-http://169.254.169.254}
  t=$(curl -fsS -m 1 --noproxy '*' -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' "$base/latest/api/token" 2>/dev/null) || return 1
  curl -fsS -m 1 --noproxy '*' -H "X-aws-ec2-metadata-token: $t" "$base/latest/meta-data/iam/info" 2>/dev/null | grep -q InstanceProfileArn
}

server_install() {
  local code log
  log=$(server_log)
  server_load
  server_user || return 1
  mkdir -p "$(dirname "$log")" && : >> "$log" && chmod 600 "$log"
  HUB_INSTALL_LOGFILE=$log
  note "--server start: user=$SUSER channel=$CHANNEL"
  SV_status=installing; SV_code=
  server_motd
  server_save || { refuse "cannot write $(server_file)"; return 1; }
  server_keys && server_packages && server_swap && server_linger || { SV_status=failed; SV_code=1; server_save; return 1; }
  server_save
  HUB_INSTALL_LOGFILE=
  server_run_hub
  code=$?
  HUB_INSTALL_LOGFILE=$log
  SV_code=$code
  if [ "$code" -eq 0 ]; then SV_status=done; else SV_status=failed; fi
  SV_tunnel=$(server_tunnel)
  server_save
  if [ "$code" -ne 0 ]; then
    say "the hub's install under $SUSER ended with code $code: $log; the line again goes on from where it stopped"
    return "$code"
  fi
  say ""
  say "Hub by Icons8 runs on this server under the user $SUSER; nothing of it listens outside this machine."
  say "Its page, from your computer: keep this running and open http://localhost:$(printf '%s' "$SV_tunnel" | sed -n 's/^ssh -N -L \([0-9]*\):.*/\1/p') there:"
  say "  $SV_tunnel"
  [ "${NOKEYS:-}" = link ] && say "~$SUSER/.ssh (or its authorized_keys) is a link: no SSH key was put there. Remove the link and run the line again."
  [ "${NOKEYS:-}" = 1 ] && say "No SSH key of $KEYS_WHO without limits (from=, command=, restrict) was given to $SUSER: put yours into ~$SUSER/.ssh/authorized_keys (or run the line again with --keys-from <user>), or tunnel as the user you sign in with."
  case ${NOCHROME:-} in
    arch) say "No browser for the agents on this machine: Google Chrome is for x86_64 only." ;;
    failed) say "No browser for the agents yet: Google Chrome did not install (apt's words above); the line again tries once more." ;;
  esac
  server_role && say "Warning: this machine has a cloud role (IAM): the hub's sessions can take its rights. Detach it if the hub does not need it."
  return 0
}

server_remove() {   # --server --remove: only what server.json says --server put on
  local u uid f
  server_load
  u=$SV_user
  [ -n "$u" ] || { refuse "--server put nothing on this machine ($(server_file) is not there)"; return 1; }
  [ "$u" = "$SUSER" ] || { refuse "the hub of this server runs under the user $u: give --user $u"; return 1; }
  if [ -n "$SV_user_created" ] && id -u "$u" >/dev/null 2>&1; then
    uid=$(id -u "$u")
    [ -n "$SV_linger" ] && loginctl disable-linger "$u" 2>/dev/null
    loginctl terminate-user "$u" 2>/dev/null
    systemctl stop "user@$uid.service" 2>/dev/null
    userdel -r "$u" 2>/dev/null || { loginctl kill-user "$u" -s KILL 2>/dev/null; sleep 2; userdel -r "$u"; } ||
      { refuse "cannot remove the user $u (its processes?): run --server --remove again"; return 1; }
    say "removed the user $u and its home folder (the hub, its data)"
  fi
  if [ -n "$SV_swap" ]; then
    swapoff "$(sys_path "$SV_swap")" 2>/dev/null
    f=$(sys_path /etc/fstab)
    [ -f "$f" ] && grep -vxF "$SV_swap none swap sw 0 0  # agents-hub" "$f" > "$f.agents-hub" 2>/dev/null
    [ -f "$f.agents-hub" ] && cat "$f.agents-hub" > "$f" && rm -f "$f.agents-hub"   # its owner and rights stay
    rm -f "$(sys_path "$SV_swap")"
  fi
  for f in "$SV_motd" "$SV_chrome_conf" "$SV_auto_conf"; do [ -n "$f" ] && rm -f "$f"; done
  rm -f "$(server_file)" "$(server_log)"
  rmdir "$(dirname "$(server_file)")" 2>/dev/null
  say "--server is taken off; the packages it installed (git, Google Chrome, unattended-upgrades) stay."
  return 0
}

# --- the whole run

main() {
  local code state tty py
  umask 077
  # HUB_INSTALL first, then the command line, which wins: its --channel, --instance, and its --move or --stay
  set -f                                  # HUB_INSTALL is split into words, never into file names
  # shellcheck disable=SC2086
  parse_flags " in HUB_INSTALL" ${HUB_INSTALL:-}
  code=$?
  local e_channel=$CHANNEL e_move=$MOVE e_stay=$STAY e_instance=$INSTANCE e_yes=$YES e_no_open=$NO_OPEN
  local e_serve=$SERVE e_suser=$SUSER e_remove=$REMOVE e_keys_from=$KEYS_FROM
  [ "$code" -eq 0 ] && { parse_flags "" "$@"; code=$?; }
  set +f
  [ "$code" -eq 2 ] && return 0
  [ "$code" -ne 0 ] && return "$code"
  CHANNEL=${CHANNEL:-${e_channel:-stable}}
  INSTANCE=${INSTANCE:-$e_instance}
  if [ -z "$MOVE$STAY" ]; then MOVE=$e_move; STAY=$e_stay; fi
  YES=$YES$e_yes; NO_OPEN=$NO_OPEN$e_no_open
  SERVE=$SERVE$e_serve; SUSER=${SUSER:-$e_suser}; REMOVE=$REMOVE$e_remove
  KEYS_FROM="$e_keys_from${e_keys_from:+${KEYS_FROM:+ }}$KEYS_FROM"   # every --keys-from: HUB_INSTALL's and the line's
  if [ -z "$SERVE" ] && [ -n "$SUSER$REMOVE$KEYS_FROM" ]; then
    refuse "--user, --keys-from and --remove go with --server"; return 64
  fi
  if [ -n "$SERVE" ]; then
    if [ -n "$INSTANCE" ]; then refuse "--server sets up one hub on a server: no --instance"; return 64; fi
    if [ -n "$MOVE$STAY" ]; then refuse "--server sets up a new hub: no --move or --stay"; return 64; fi
    SUSER=${SUSER:-$HUB_INSTALL_SERVER_USER}
    # the letters spelled out: a range like a-z follows the locale in bash 3.2 (macOS) and takes capitals too
    case $SUSER in
      [abcdefghijklmnopqrstuvwxyz_]*) ;;
      *) refuse "--user takes a name of small letters, digits, dashes and underscores: $SUSER"; return 64 ;;
    esac
    case $SUSER in
      *[!abcdefghijklmnopqrstuvwxyz0123456789_-]*) refuse "--user takes a name of small letters, digits, dashes and underscores: $SUSER"; return 64 ;;
    esac
    [ ${#SUSER} -le 32 ] || { refuse "--user takes a name of up to 32 characters"; return 64; }
    as_server "$@"
    return
  fi
  if [ "$(id -u)" = 0 ]; then
    refuse "run it as yourself, not as root: the hub is a user's, it lives in the home folder (a server: add --server, it makes a user for the hub)"
    return 1
  fi
  [ -n "${HOME:-}" ] && [ -d "$HOME" ] || { refuse "the home folder (HOME) is not there"; return 1; }
  ui_init
  if [ -n "$UI_ON" ]; then ui_header; ui_begin 1 Check "this computer"; fi
  pick_platform || return 1
  need_programs || return 1
  SERVER=$(server_url) || return 1
  KEY=
  if [ "$HUB_INSTALL_FLAVOR" != open ]; then KEY=$(build_key) || return 1; fi   # the open build has no key
  ROOT=$(data_root)
  mkdir -p "$ROOT" || { refuse "cannot create $ROOT"; return 1; }
  HUB_INSTALL_LOGFILE="$ROOT/install.log"
  note "start: $PLATFORM channel=$CHANNEL server=$SERVER"
  local free
  free=$(free_kb "$ROOT")
  if [ -n "$free" ] && [ "$free" -lt "$HUB_INSTALL_NEED_KB" ]; then
    refuse "the hub needs about $((HUB_INSTALL_NEED_KB / 1048576)) GB free in $ROOT, and there are $((free / 1024)) MB"
    return 1
  fi
  state=$(state_dir)
  tty=
  if [ -z "$YES" ] && ( exec < /dev/tty ) 2>/dev/null; then tty=1; fi
  # what the state folder holds is install.py's to read, with or without a terminal: a hub from git, a hub's data nothing
  # runs, another install's - each gets its own words there (and the line of a second hub from git); the script cannot tell them apart
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/agents-hub-install.XXXXXX") || { refuse "cannot make a temporary folder"; return 1; }
  trap cleanup EXIT
  trap 'ui_interrupted; cleanup; exit 130' INT TERM
  key_files || { refuse "cannot write the key files in $WORK"; return 1; }
  take_lock || return 1
  read_release || return 1
  if [ -n "$UI_ON" ]; then ui_done "$PLATFORM $UI_DOT $((${free:-0} / 1048576)) GB free"; fi
  mkdir -p "$ROOT/shells" || { refuse "cannot create $ROOT/shells"; return 1; }
  write_helper
  if pick_env "$state"; then
    note "environment $ENV is in place"
    if [ -n "$UI_ON" ]; then ui_skip 2 Download "not needed $UI_DOT the environment is here"; ui_skip 3 Unpack "not needed"; fi
  else
    if [ -n "$UI_ON" ]; then ui_begin 2 Download "$((SHELL_SIZE / 1000000)) MB"; fi
    download || return 1
    if [ -n "$UI_ON" ]; then ui_done "$((SHELL_SIZE / 1000000)) MB $UI_DOT verified"; ui_begin 3 Unpack "every file is checked"; fi
    unpack || return 1
    if [ -n "$UI_ON" ]; then ui_done "every file checked"; fi
  fi
  if [ ! -f "$ENV/install.py" ]; then
    refuse "this build of the environment has no installer (install.py): the delivery server has an older build"
    return 1
  fi
  py="$ENV/runtime/python/bin/python3"
  set -- --contract "$HUB_INSTALL_CONTRACT" --env "$ENV" --server "$SERVER"
  [ -n "$KEY" ] && set -- "$@" --key-file "$WORK/key"
  set -- "$@" --platform "$PLATFORM" --flavor "$HUB_INSTALL_FLAVOR" --runtime "$SHELL_RUNTIME" --channel "$CHANNEL"
  [ -n "${HUB_STATE_DIR:-}" ] && set -- "$@" --state "$HUB_STATE_DIR"
  [ -n "$MOVE" ] && set -- "$@" --move
  [ -n "$STAY" ] && set -- "$@" --stay
  [ -n "$INSTANCE" ] && set -- "$@" --instance "$INSTANCE"
  [ -n "$YES" ] && set -- "$@" --yes
  [ -n "$NO_OPEN" ] && set -- "$@" --no-open
  [ -z "$tty" ] && set -- "$@" --no-input
  local lang
  lang=$(system_language)
  note "install.py $*"
  local uienv=   # the screen goes to setup.py from the step 4 on (look.py); an older setup.py ignores it and says its lines
  if [ -n "$UI_ON" ]; then ui_tail_clear; UI_IDLE=1; uienv=$(ui_env 4); fi
  if [ -n "$tty" ]; then
    HUB_INSTALL_UI=$uienv HUB_LANG=${HUB_LANG:-$lang} HUB_INSTALL_LOG=$HUB_INSTALL_LOGFILE "$py" -I -B "$ENV/install.py" "$@" < /dev/tty
  else
    HUB_INSTALL_UI=$uienv HUB_LANG=${HUB_LANG:-$lang} HUB_INSTALL_LOG=$HUB_INSTALL_LOGFILE "$py" -I -B "$ENV/install.py" "$@" < /dev/null
  fi
  code=$?
  note "install.py exit $code"
  return "$code"
}

main "$@"
