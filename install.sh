#!/usr/bin/env bash
# rf-survey one-command installer -- macOS and Debian/Raspberry Pi OS.
#
#   curl -fsSL https://raw.githubusercontent.com/Chicago-Offline/rf-survey/main/install.sh | bash
#
# Installs the RTL-SDR userland, puts the \`survey\` command on your PATH, and
# runs the setup wizard.  Re-running it is safe: every step is idempotent.
#
# Environment knobs:
#   RFS_REPO=<git url>   source repo            (default: Chicago-Offline/rf-survey)
#   RFS_REF=<branch|tag> ref to install         (default: main)
#   RFS_SETUP=0          install only, no wizard
#   RFS_NO_SUDO=1        never call sudo; print the apt command instead

set -euo pipefail

REPO_URL="${RFS_REPO:-https://github.com/Chicago-Offline/rf-survey.git}"
REF="${RFS_REF:-main}"
RUN_SETUP="${RFS_SETUP:-1}"
NO_SUDO="${RFS_NO_SUDO:-0}"
# Deliberately NOT under ~/.local/share/rf-survey -- that directory is the
# observation data dir (observations.db lives there). Keep code out of it.
VENV_HOME="$HOME/.local/opt/rf-survey/venv"

if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; Z=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; Z=""
fi

say()  { printf '%s==>%s %s\n' "$B" "$Z" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$G" "$Z" "$*"; }
warn() { printf '%s  !!%s %s\n' "$Y" "$Z" "$*" >&2; }
die()  { printf '%s error:%s %s\n' "$R" "$Z" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

SUDO=""
if [ "$(id -u)" -ne 0 ] && [ "$NO_SUDO" != "1" ] && have sudo; then
  SUDO="sudo"
fi

# --------------------------------------------------------------- platform ---
OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM="macos" ;;
  Linux)
    if [ -r /etc/os-release ]; then
      . /etc/os-release
      case "${ID:-}:${ID_LIKE:-}" in
        *debian*|*raspbian*|*ubuntu*) PLATFORM="debian" ;;
        *) PLATFORM="linux-other" ;;
      esac
    else
      PLATFORM="linux-other"
    fi
    ;;
  *) die "unsupported OS: $OS (this installer covers macOS and Debian/Raspberry Pi OS)" ;;
esac
say "Platform: $PLATFORM"

# ------------------------------------------------------------ system deps ---
install_macos() {
  have brew || die "Homebrew is required. Install it from https://brew.sh then re-run this script."
  local want=() b
  for b in rtl-sdr coreutils; do
    brew list --formula "$b" >/dev/null 2>&1 || want+=("$b")
  done
  have pipx || want+=(pipx)
  if [ ${#want[@]} -gt 0 ]; then
    say "brew install ${want[*]}"
    brew install "${want[@]}"
  else
    ok "rtl-sdr, coreutils, pipx already present"
  fi
}

install_debian() {
  local pkgs=(rtl-sdr coreutils procps python3-venv python3-pip)
  # pipx is packaged from bookworm on; older releases fall back to a plain venv.
  apt-cache show pipx >/dev/null 2>&1 && pkgs+=(pipx)
  local missing=() p
  for p in "${pkgs[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    ok "apt packages already present"
    return
  fi
  if [ -z "$SUDO" ] && [ "$(id -u)" -ne 0 ]; then
    die "need root to install: ${missing[*]}
    run: sudo apt-get update && sudo apt-get install -y ${missing[*]}"
  fi
  say "apt-get install ${missing[*]}"
  $SUDO apt-get update
  $SUDO apt-get install -y "${missing[@]}"
}

case "$PLATFORM" in
  macos)  install_macos ;;
  debian) install_debian ;;
  linux-other)
    warn "unknown Linux flavour -- install rtl-sdr, python3-venv and procps yourself"
    ;;
esac

# --------------------------------------------- DVB driver conflict (Linux) ---
# The kernel DVB-T driver grabs the dongle and rtl_test reports "usb_claim_interface
# error -6".  Only add the blacklist if nothing already covers it, and only ever
# append our own file -- never rewrite someone else's modprobe config.
if [ "$OS" = "Linux" ]; then
  BL_FILE=/etc/modprobe.d/rtl-sdr-blacklist.conf
  if grep -rqs '^[[:space:]]*blacklist[[:space:]]\+dvb_usb_rtl28xxu' /etc/modprobe.d/ 2>/dev/null; then
    ok "DVB driver already blacklisted"
  elif [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
    say "blacklisting kernel DVB driver -> $BL_FILE"
    printf '# added by rf-survey install.sh\nblacklist dvb_usb_rtl28xxu\n' \
      | $SUDO tee -a "$BL_FILE" >/dev/null
    $SUDO modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
    warn "reboot (or replug the dongle) if rtl_test still reports 'usb_claim_interface error -6'"
  else
    warn "cannot blacklist dvb_usb_rtl28xxu without root; do it manually if rtl_test fails"
  fi
fi

# ------------------------------------------------------------ rf-survey ------
SPEC="git+${REPO_URL}@${REF}#egg=rf-survey[submit]"

if have pipx; then
  say "pipx install rf-survey ($REF)"
  pipx install --force "$SPEC"
  pipx ensurepath >/dev/null 2>&1 || true
  BIN="$HOME/.local/bin/survey"
else
  say "no pipx -- installing into $VENV_HOME"
  mkdir -p "$(dirname "$VENV_HOME")"
  [ -d "$VENV_HOME" ] || python3 -m venv "$VENV_HOME"
  "$VENV_HOME/bin/pip" install --upgrade pip >/dev/null
  "$VENV_HOME/bin/pip" install --upgrade "$SPEC"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$VENV_HOME/bin/survey" "$HOME/.local/bin/survey"
  BIN="$HOME/.local/bin/survey"
fi

[ -x "$BIN" ] || die "install finished but $BIN is missing"
ok "installed $("$BIN" --version 2>/dev/null || echo rf-survey)"

# PATH nudge -- append to the shell rc only if ~/.local/bin is not already on PATH.
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *)
    case "${SHELL:-}" in
      */zsh) RC="$HOME/.zshrc" ;;
      */bash) RC="$HOME/.bashrc" ;;
      *) RC="" ;;
    esac
    LINE='export PATH="$HOME/.local/bin:$PATH"'
    if [ -n "$RC" ] && ! grep -qsF '.local/bin' "$RC"; then
      printf '\n# added by rf-survey install.sh\n%s\n' "$LINE" >> "$RC"
      ok "added ~/.local/bin to PATH in $RC (open a new shell to pick it up)"
    else
      warn "add this to your shell profile:  $LINE"
    fi
    export PATH="$HOME/.local/bin:$PATH"
    ;;
esac

# ---------------------------------------------------------------- wizard -----
if [ "$RUN_SETUP" = "1" ] && [ -t 0 ]; then
  echo
  say "Starting setup -- it asks where the receiver is and what to listen to."
  exec "$BIN" setup
else
  echo
  ok "Done. Next:  survey setup"
  [ "$RUN_SETUP" = "1" ] && warn "(not a terminal -- skipping the wizard)"
fi
