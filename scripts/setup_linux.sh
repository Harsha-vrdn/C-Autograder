#!/usr/bin/env bash
# One-time setup on Linux (Debian/Ubuntu, Fedora/RHEL, Arch).
#   ./scripts/setup_linux.sh            # toolchain + Python env + database
#   WITH_VALGRIND=1 ./scripts/setup_linux.sh   # also install valgrind (used instead of LeakSanitizer)
set -euo pipefail
cd "$(dirname "$0")/.."

pkgs_apt="gcc libc6-dev python3 python3-venv ca-certificates"
pkgs_dnf="gcc glibc-devel libasan libubsan python3"
pkgs_pacman="gcc python"
extra=""
[ "${WITH_VALGRIND:-0}" = "1" ] && extra="valgrind"

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

if command -v apt-get >/dev/null; then
  $SUDO apt-get update
  $SUDO apt-get install -y --no-install-recommends $pkgs_apt $extra
elif command -v dnf >/dev/null; then
  $SUDO dnf install -y $pkgs_dnf $extra
elif command -v pacman >/dev/null; then
  $SUDO pacman -S --needed --noconfirm $pkgs_pacman $extra
else
  echo "Unknown package manager: install gcc (with libasan/libubsan), python3 and python3-venv yourself." >&2
fi

# Sanitizers must work, otherwise memory_safety cannot be graded.
tmp="$(mktemp -d)"
printf 'int main(void){return 0;}\n' > "$tmp/t.c"
if ! "${AUTOGRADER_CC:-cc}" -fsanitize=address,undefined "$tmp/t.c" -o "$tmp/t" 2>/dev/null || ! "$tmp/t"; then
  echo "WARNING: -fsanitize=address,undefined does not work with ${AUTOGRADER_CC:-cc}; install libasan/libubsan." >&2
fi
rm -rf "$tmp"

python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python manage.py migrate -v0
[ -f .env ] || cp .env.example .env

cat <<'MSG'

Done. Next:
  1. put GROQ_API_KEY=... in .env
  2. optional local LLM:  curl -fsSL https://ollama.com/install.sh | sh && ollama pull qwen3:4b
  3. run:  .venv/bin/python manage.py runserver 0.0.0.0:8000
MSG
