#!/bin/bash
# Inspect an iOS wheel: tags, file list, and Mach-O details of a few extension modules.
#
# Usage: inspect_ios_wheel.sh <wheel> [module path inside the wheel ...]
# Without module paths it inspects the first three .so files in the wheel.

set -uo pipefail
WHEEL="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
shift
DEST="$(mktemp -d -t inspect-wheel)"
unzip -q "$WHEEL" -d "$DEST"

echo "=== wheel: $(basename "$WHEEL")"
echo "--- WHEEL metadata"
cat "$DEST"/*.dist-info/WHEEL
echo
echo "--- file list ($(unzip -Z1 "$WHEEL" | wc -l | tr -d ' ') files; extension modules shown in full)"
unzip -Z1 "$WHEEL" | grep -E '\.so$'
echo "--- non-extension files: $(unzip -Z1 "$WHEEL" | grep -vcE '\.so$')"

if [ $# -eq 0 ]; then
    set -- $(unzip -Z1 "$WHEEL" | grep -E '\.so$' | head -n 3)
fi

for mod in "$@"; do
    f="$DEST/$mod"
    echo
    echo "=== $mod"
    echo "--- file / lipo"
    file "$f"
    lipo -archs "$f"
    echo "--- LC_BUILD_VERSION (platform 2 = iOS device, 7 = iOS simulator)"
    otool -l "$f" | grep -A5 LC_BUILD_VERSION
    echo "--- LC_ENCRYPTION_INFO_64"
    otool -l "$f" | grep -A4 LC_ENCRYPTION_INFO_64 || echo "(none)"
    echo "--- dependent libraries"
    otool -L "$f"
    echo "--- LC_RPATH"
    otool -l "$f" | grep -A2 LC_RPATH || echo "(none)"
done
