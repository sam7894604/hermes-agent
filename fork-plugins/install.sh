#!/usr/bin/env bash
# Symlink every fork plugin into $HERMES_HOME/plugins (idempotent). Platform overrides land under
# plugins/platforms/<name>/ so their key matches the bundled adapter they shadow.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
DEST="$HOME_DIR/plugins"
mkdir -p "$DEST/platforms"
names=()
for dir in "$HERE"/*/; do
  name="$(basename "$dir")"
  [ "$name" = "platforms" ] && continue
  ln -sfn "$dir" "$DEST/$name"
  names+=("$name")
  echo "linked $DEST/$name -> $dir"
done
if [ -d "$HERE/platforms" ]; then
  for dir in "$HERE"/platforms/*/; do
    name="$(basename "$dir")"
    ln -sfn "$dir" "$DEST/platforms/$name"
    names+=("platforms/$name")
    echo "linked $DEST/platforms/$name -> $dir"
  done
fi
echo
echo "Enable them once (config.yaml plugins.enabled):"
echo "  hermes plugins enable ${names[*]}"
