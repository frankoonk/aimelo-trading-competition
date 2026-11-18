#!/bin/zsh
# Dubbelklik om de Grid Bot te starten. De browser opent vanzelf.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Het hulpprogramma 'uv' is niet gevonden. Installeer het via https://docs.astral.sh/uv/ en probeer opnieuw."
  read -r "?Druk op Enter om te sluiten."
  exit 1
fi
exec uv run --quiet --no-project --python 3.13 --with-requirements requirements.txt python server.py "$@"
