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

set -eu

HOME_DIR="${CODEORBIT_HOME:-$HOME/.codeorbit}"
BIN_DIR="${CODEORBIT_BIN:-$HOME/.local/bin}"
REPO="${CODEORBIT_REPO:-https://github.com/Anilll-coder/codeorbit.git}"
REF="${CODEORBIT_REF:-main}"
MODEL="phi4-mini"
MIN_PY_MINOR=10

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

# ---------- uninstall -------------------------------------------------------
if [ "${1:-}" = "--uninstall" ]; then
  step "Removing CodeOrbit"
  [ -d "$HOME_DIR" ] && rm -rf "$HOME_DIR" && say "  removed $HOME_DIR"
  [ -f "$BIN_DIR/codeorbit" ] && rm -f "$BIN_DIR/codeorbit" && say "  removed $BIN_DIR/codeorbit"
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
  step "Installing from this checkout"
  say "  $SRC"
else
  command -v git >/dev/null 2>&1 || die "git is required to fetch the source."
  SRC=$(mktemp -d 2>/dev/null || mktemp -d -t codeorbit)
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
"$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || true
"$VPY" -m pip install --upgrade "$SRC" >/dev/null 2>&1 \
  || die "Installation failed. Re-run with:
  $VPY -m pip install --upgrade '$SRC'"
say "  ${G}ok${N}"

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
  *":$BIN_DIR:"*) ON_PATH=1 ;;
  *) ON_PATH=0 ;;
esac

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
  warn "$BIN_DIR is not on your PATH."
  say ""
  say "  Add it with one of:"
  say "    ${DIM}echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc && . ~/.bashrc${N}"
  say "    ${DIM}echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc  && . ~/.zshrc${N}"
  say ""
fi

say "  ${B}cd your-project${N}"
say "  ${B}codeorbit index .${N}                 build the graph"
say "  ${B}codeorbit ask \"how does X work?\"${N}   ask the local model"
say ""
say "  ${DIM}codeorbit --help          all commands${N}"
say "  ${DIM}./install.sh --uninstall  remove it${N}"
