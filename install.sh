#!/usr/bin/env bash
# PopUtil installer. PopUtil ships as a Flatpak with updates from GitHub, so this is a thin helper around flatpak.
#   ./install.sh                     install PopUtil (and clean up older non-Flatpak copies)
#   ./install.sh update              update to the newest version (the COSMIC Store does this too)
#   ./install.sh --check             check that PopUtil and what it needs are in place
#   ./install.sh --uninstall         remove PopUtil (keeps your settings and anything waiting in Removed)
#   ./install.sh --file PopUtil.flatpak   install from a downloaded file instead
#   ./install.sh dev                 run straight from this folder without installing (for working on it)
set -euo pipefail

APP_ID="io.github.Jayswansocool48.PopUtil"
REF_URL="https://jayswansocool48.github.io/poputil/poputil.flatpakref"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/poputil"

say() { printf '%s\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; }
note() { printf '  · %s\n' "$*"; }

if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  say "Run this as your normal user, without sudo. PopUtil asks for your password itself when a step needs it."
  exit 1
fi

need_flatpak() {
  command -v flatpak >/dev/null && return
  say "Flatpak isn't installed. Install it with:  sudo apt install flatpak"
  exit 1
}

installed() { flatpak info "$APP_ID" >/dev/null 2>&1; }

# Older versions were installed with this script into ~/.local. Remove them so only the Flatpak is left.
cleanup_old() {
  local removed=0 stuck=()
  local apps="$HOME/.local/share/applications"
  for f in "$apps"/*.desktop; do
    [[ -f "$f" ]] || continue
    if grep -q '^Name=PopUtil' "$f" && grep -q 'poputil.py' "$f"; then
      rm -f "$f" 2>/dev/null && removed=$((removed + 1)) || stuck+=("$f")
    fi
  done
  for name in poputil popclean; do
    local link="$HOME/.local/bin/$name"
    if [[ -L "$link" ]] && [[ "$(readlink "$link")" == *poputil* ]]; then
      rm -f "$link" 2>/dev/null && removed=$((removed + 1)) || stuck+=("$link")
    fi
  done
  if [[ -f "$HOME/.local/lib/poputil/poputil.py" ]]; then
    rm -rf "$HOME/.local/lib/poputil" 2>/dev/null && removed=$((removed + 1)) || stuck+=("$HOME/.local/lib/poputil")
  fi
  for f in poputil.py popclean.py organizer.py ui.html icon.svg apps.json apps.json.new; do
    if [[ -f "$DATA/$f" ]]; then rm -f "$DATA/$f" 2>/dev/null && removed=$((removed + 1)) || stuck+=("$DATA/$f"); fi
  done
  command -v update-desktop-database >/dev/null && update-desktop-database "$apps" 2>/dev/null || true
  if (( removed )); then ok "Removed $removed piece(s) of older PopUtil installs (your data stays)"; fi
  if (( ${#stuck[@]} )); then
    bad "Some old files are owned by root, probably from a sudo install. Remove them with:"
    printf '      sudo rm -rf'; printf ' %q' "${stuck[@]}"; printf '\n'
  fi
}

do_install() {
  need_flatpak
  cleanup_old
  if [[ -n "${1:-}" ]]; then
    [[ -f "$1" ]] || { say "No such file: $1"; exit 1; }
    flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak install --user -y "$1"
  else
    flatpak install --user -y "$REF_URL"
  fi
  say ""
  say "Done. Open PopUtil from the app library. Updates show up in the COSMIC Store."
}

do_update() {
  need_flatpak
  installed || { say "PopUtil isn't installed yet. Run ./install.sh"; exit 1; }
  flatpak update --user -y "$APP_ID"
}

do_check() {
  local fail=0
  say "Checking PopUtil…"
  if command -v flatpak >/dev/null; then ok "Flatpak $(flatpak --version | awk '{print $2}')"; else bad "Flatpak isn't installed (sudo apt install flatpak)"; fail=1; fi
  if command -v flatpak >/dev/null && installed; then
    ok "PopUtil $(flatpak info "$APP_ID" | awk -F': *' '/Version/ {print $2; exit}') is installed"
    local origin; origin="$(flatpak info "$APP_ID" | awk -F': *' '/Origin/ {print $2; exit}')"
    ok "Updates come from the '$origin' source"
  else
    note "PopUtil isn't installed yet. Run ./install.sh"
  fi
  command -v python3 >/dev/null && ok "Python 3 (PopUtil's engine runs on your system)" || { bad "python3 is missing (sudo apt install python3)"; fail=1; }
  command -v pkexec >/dev/null && ok "pkexec for password prompts" || { bad "pkexec is missing, so steps that need your password can't run"; fail=1; }
  command -v dpkg-query >/dev/null && ok "dpkg and apt" || { bad "dpkg-query is missing; this doesn't look like Pop!_OS or Ubuntu"; fail=1; }
  local old=0
  [[ -d "$HOME/.local/lib/poputil" ]] && old=1
  [[ -f "$DATA/poputil.py" ]] && old=1
  grep -l 'poputil.py' "$HOME"/.local/share/applications/*.desktop >/dev/null 2>&1 && old=1
  if (( old )); then note "An older non-Flatpak copy is still here. ./install.sh removes it."; else ok "No older copies left"; fi
  if [[ -d "$DATA/removed" ]] && [[ -n "$(ls -A "$DATA/removed" 2>/dev/null)" ]]; then
    note "Removed holding area: $(du -sh "$DATA/removed" | cut -f1) (empty it from Apps on this PC)"
  fi
  return $fail
}

do_uninstall() {
  need_flatpak
  if installed; then flatpak uninstall --user -y "$APP_ID" || flatpak uninstall -y "$APP_ID"; fi
  cleanup_old
  rm -rf "$DATA/runtime"
  ok "PopUtil is removed."
  if [[ -d "$DATA/removed" ]] && [[ -n "$(ls -A "$DATA/removed" 2>/dev/null)" ]]; then
    note "Apps you removed with PopUtil are still held in $DATA/removed (and /var/lib/poputil/removed for system ones)."
    note "Reinstall PopUtil to restore or empty them, or delete those folders yourself."
  fi
  note "Your settings stay in ~/.config/poputil. Delete that folder too for a clean slate."
}

case "${1:-install}" in
  install) do_install ;;
  --file) do_install "${2:?Give the path to PopUtil.flatpak}" ;;
  update|--update) do_update ;;
  --check|check) do_check ;;
  --uninstall|uninstall) do_uninstall ;;
  dev) exec python3 "$SRC/poputil.py" "${@:2}" ;;
  -h|--help|help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) say "Unknown option: $1"; sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
