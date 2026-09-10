#!/usr/bin/env sh
#
# CodeOrbit installer (Linux / macOS / Git Bash on Windows).
#
#   curl -fsSL https://raw.githubusercontent.com/USER/codeorbit/main/install.sh | sh
#   ./install.sh                 # from a clone
#   ./install.sh --uninstall
#
# Installs into an isolated virtualenv so it can never break the system Python,
# then drops a small launcher on PATH. Re-running upgrades in place.
#
# Knobs:
#   CODEORBIT_HOME=DIR   where the venv lives      (default: ~/.codeorbit)
#   CODEORBIT_BIN=DIR    where the launcher goes   (default: ~/.local/bin)
#   CODEORBIT_REPO=URL   source to clone when not run from a checkout
#   CODEORBIT_REF=REF    branch/tag to install     (default: main)
#   CODEORBIT_NO_MODEL=1 skip pulling the Ollama model
#   CODEORBIT_NO_PATH=1  do not edit shell rc files to extend PATH

set -eu

HOME_DIR="${CODEORBIT_HOME:-$HOME/.codeorbit}"
BIN_DIR="${CODEORBIT_BIN:-$HOME/.local/bin}"
REPO="${CODEORBIT_REPO:-https://github.com/Anilll-coder/codeorbit.git}"
REF="${CODEORBIT_REF:-main}"
MODEL="phi4-mini"
MIN_PY_MINOR=10

# Fences around the block this installer appends to a shell rc file, so it
# can recognise its own work on an upgrade and take it back out on uninstall.
MARK_BEGIN='# >>> codeorbit >>>'
MARK_END='# <<< codeorbit <<<'

# ---------- output ----------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$(printf '\033[1m'); DIM=$(printf '\033[2m'); R=$(printf '\033[31m')
  G=$(printf '\033[32m'); Y=$(printf '\033[33m'); N=$(printf '\033[0m')
else
  B=''; DIM=''; R=''; G=''; Y=''; N=''
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
warn() { printf '%s warn%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

# ---------- shell rc files --------------------------------------------------
# Every file this installer might have written a PATH block into. Used only
# by --uninstall; the install path picks exactly one.
rc_candidates() {
  printf '%s\n' \
    "$HOME/.bashrc" \
    "$HOME/.bash_profile" \
    "$HOME/.zshrc" \
    "${ZDOTDIR:-$HOME}/.zshrc" \
    "$HOME/.profile" \
    "$HOME/.config/fish/config.fish"
}

# Delete our fenced block, leaving the rest of the file alone. Written as
# tmp + copy-back rather than `sed -i` because that flag takes a different
# argument on BSD/macOS than it does on GNU.
strip_path_block() {
  _f="$1"
  [ -f "$_f" ] || return 0
  grep -Fq "$MARK_BEGIN" "$_f" 2>/dev/null || return 0
  _tmp="$_f.codeorbit.$$"
  if sed "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" "$_f" > "$_tmp" 2>/dev/null; then
    if cat "$_tmp" > "$_f" 2>/dev/null; then
      say "  removed PATH entry from $_f"
    fi
  fi
  rm -f "$_tmp"
}

# ---------- uninstall -------------------------------------------------------
if [ "${1:-}" = "--uninstall" ]; then
  step "Removing CodeOrbit"
  [ -d "$HOME_DIR" ] && rm -rf "$HOME_DIR" && say "  removed $HOME_DIR"
  [ -f "$BIN_DIR/codeorbit" ] && rm -f "$BIN_DIR/codeorbit" && say "  removed $BIN_DIR/codeorbit"
  rc_candidates | sort -u | while IFS= read -r f; do strip_path_block "$f"; done
  say "${G}Done.${N} Per-project .codeorbit/ indexes were left alone."
  exit 0
fi

# ---------- python ----------------------------------------------------------
step "Checking Python"
PY=''
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,'"$MIN_PY_MINOR"') else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
[ -n "$PY" ] || die "Python 3.$MIN_PY_MINOR+ is required but was not found.
  Install it from https://python.org and re-run this script."
say "  $($PY --version 2>&1) ${DIM}($(command -v "$PY"))${N}"

"$PY" -c 'import venv' 2>/dev/null || die "Python is missing the venv module.
  On Debian/Ubuntu: sudo apt install python3-venv"

# ---------- source ----------------------------------------------------------
SRC=''
# Piped through `curl ... | sh` there is no script file: $0 is "sh" or "-", and
# dirname would yield "." - which would make this treat whatever directory the
# user happens to be standing in as a CodeOrbit checkout. Only trust $0 when it
# actually names a file.
if [ -f "$0" ]; then
  SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
else
  SELF_DIR=""
fi
if [ -f "$SELF_DIR/pyproject.toml" ] && [ -d "$SELF_DIR/codeorbit" ]; then
  SRC="$SELF_DIR"
  SRC_RECORD="$SELF_DIR"
  step "Installing from this checkout"
  say "  $SRC"
else
  command -v git >/dev/null 2>&1 || die "git is required to fetch the source."
  SRC=$(mktemp -d 2>/dev/null || mktemp -d -t codeorbit)
  # Record the REPO, never this temp clone: it is deleted on exit, and
  # recording it left `codeorbit upgrade` pointing at a path that no
  # longer exists.
  SRC_RECORD="git+$REPO@$REF"
  # shellcheck disable=SC2064
  trap "rm -rf '$SRC'" EXIT INT TERM
  step "Fetching source"
  say "  $REPO ${DIM}($REF)${N}"
  git clone --depth 1 --branch "$REF" "$REPO" "$SRC" >/dev/null 2>&1 \
    || die "Could not clone $REPO (ref: $REF)."
fi

# ---------- venv ------------------------------------------------------------
step "Creating isolated environment"
say "  $HOME_DIR"
if [ -d "$HOME_DIR" ]; then
  say "  ${DIM}existing install found - upgrading${N}"
else
  "$PY" -m venv "$HOME_DIR" || die "Could not create a virtualenv at $HOME_DIR"
fi

if [ -x "$HOME_DIR/bin/python" ]; then
  VPY="$HOME_DIR/bin/python"; VBIN="$HOME_DIR/bin"
elif [ -x "$HOME_DIR/Scripts/python.exe" ]; then
  VPY="$HOME_DIR/Scripts/python.exe"; VBIN="$HOME_DIR/Scripts"   # Git Bash
else
  die "The virtualenv at $HOME_DIR looks broken. Remove it and re-run."
fi

step "Installing CodeOrbit and its dependencies"
# Deliberately NOT upgrading pip: the venv ships a working one, and an
# upgrade pulled a release that breaks console-script generation on
# Windows badly enough that pip could not reinstall itself.
"$VPY" -m pip install --upgrade "$SRC" >/dev/null 2>&1 \
  || die "Installation failed. Re-run with:
  $VPY -m pip install --upgrade '$SRC'"
say "  ${G}ok${N}"

# Record where this came from. pip does not keep the source of a
# non-editable install, so `codeorbit upgrade` would otherwise have to
# guess at the public repo even when installed from a local checkout.
echo "$SRC_RECORD" > "$HOME_DIR/.codeorbit-source" 2>/dev/null || true

# ---------- launcher --------------------------------------------------------
step "Putting codeorbit on your PATH"
mkdir -p "$BIN_DIR" || die "Could not create $BIN_DIR"

if [ -x "$VBIN/codeorbit" ]; then
  TARGET="$VBIN/codeorbit"
elif [ -x "$VBIN/codeorbit.exe" ]; then
  TARGET="$VBIN/codeorbit.exe"
else
  die "The codeorbit entry point was not installed. This is a bug."
fi

cat > "$BIN_DIR/codeorbit" <<LAUNCHER
#!/usr/bin/env sh
exec "$TARGET" "\$@"
LAUNCHER
chmod +x "$BIN_DIR/codeorbit"
say "  $BIN_DIR/codeorbit"

case ":${PATH}:" in
  *":$BIN_DIR:"*)  ON_PATH=1 ;;
  *":$BIN_DIR/:"*) ON_PATH=1 ;;
  *)               ON_PATH=0 ;;
esac

# Actually put BIN_DIR on PATH, the way install.ps1 edits the user PATH on
# Windows. Printing advice was not enough: the install reported success and
# then every command was "codeorbit: command not found". The Debian/Ubuntu
# snippet that adds ~/.local/bin runs from ~/.profile at *login* and only if
# the directory already exists - neither holds right after a fresh install.
PATH_FILE=''
if [ "$ON_PATH" = "0" ] && [ "${CODEORBIT_NO_PATH:-}" != "1" ]; then
  PATH_LINE="export PATH=\"$BIN_DIR:\$PATH\""
  case "$(basename -- "${SHELL:-sh}")" in
    zsh)
      PATH_FILE="${ZDOTDIR:-$HOME}/.zshrc"
      ;;
    fish)
      PATH_FILE="$HOME/.config/fish/config.fish"
      PATH_LINE="set -gx PATH $BIN_DIR \$PATH"
      ;;
    bash)
      # macOS Terminal starts bash as a *login* shell, which reads
      # .bash_profile and never .bashrc unless that file sources it.
      if [ "$(uname -s 2>/dev/null || echo)" = "Darwin" ] && [ -f "$HOME/.bash_profile" ]; then
        PATH_FILE="$HOME/.bash_profile"
      else
        PATH_FILE="$HOME/.bashrc"
      fi
      ;;
    *)
      PATH_FILE="$HOME/.profile"
      ;;
  esac

  if [ -f "$PATH_FILE" ] && grep -Fq "$MARK_BEGIN" "$PATH_FILE" 2>/dev/null; then
    # Ours already, from an earlier run. Appending a second block would stack
    # another copy of BIN_DIR onto PATH on every upgrade.
    say "  already configured in $PATH_FILE"
    PATH_FILE=''
  elif mkdir -p "$(dirname -- "$PATH_FILE")" 2>/dev/null &&
       printf '\n%s\n%s\n%s\n' "$MARK_BEGIN" "$PATH_LINE" "$MARK_END" >> "$PATH_FILE" 2>/dev/null; then
    say "  added to PATH in $PATH_FILE"
  else
    warn "Could not write $PATH_FILE - you will have to add $BIN_DIR to PATH yourself."
    PATH_FILE=''
  fi
fi

# Also for the remainder of this script, so the version check below and
# anything else that shells out can find the launcher.
PATH="$BIN_DIR:$PATH"
export PATH

# ---------- ollama ----------------------------------------------------------
step "Checking Ollama (needed for 'codeorbit ask')"
if command -v ollama >/dev/null 2>&1; then
  if ollama list 2>/dev/null | grep -q "^${MODEL}"; then
    say "  ${G}ollama ok${N}, model ${B}${MODEL}${N} present"
  elif [ "${CODEORBIT_NO_MODEL:-}" = "1" ]; then
    say "  ${DIM}skipping model download (CODEORBIT_NO_MODEL=1)${N}"
  else
    say "  pulling ${B}${MODEL}${N} (~2.5 GB, one time)"
    ollama pull "$MODEL" || warn "Model pull failed. Run later: ollama pull $MODEL"
  fi
else
  warn "Ollama not found - graph commands work, but 'codeorbit ask' will not.
       Install it from https://ollama.com, then: ollama pull $MODEL"
fi

# ---------- done ------------------------------------------------------------
VERSION=$("$VPY" -c 'import importlib.metadata as m; print(m.version("codeorbit"))' 2>/dev/null || echo '?')
say ""
say "${G}CodeOrbit ${VERSION} installed.${N}"
say ""

if [ "$ON_PATH" = "0" ]; then
  if [ -n "$PATH_FILE" ]; then
    warn "PATH was updated - open a NEW terminal before using codeorbit, or
       bring it into this one with:  . $PATH_FILE"
  else
    warn "$BIN_DIR is not on your PATH."
    say ""
    say "  Add it with one of:"
    say "    ${DIM}echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc && . ~/.bashrc${N}"
    say "    ${DIM}echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc  && . ~/.zshrc${N}"
  fi
  say ""
fi

say "  ${B}cd your-project${N}"
say "  ${B}codeorbit index .${N}                 build the graph"
say "  ${B}codeorbit ask \"how does X work?\"${N}   ask the local model"
say ""
say "  ${DIM}codeorbit --help          all commands${N}"
say "  ${DIM}./install.sh --uninstall  remove it${N}"
