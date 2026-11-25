#!/bin/bash
# Summarise every extension module in an iOS wheel, then list one module's undefined symbols.
#
# Usage: audit_ios_wheel.sh <wheel> <module path for the nm -u listing>
# Prints: the set of platforms, minimum OS versions, architectures, encryption commands and
# dependent libraries across all .so files; then the undefined symbols of the chosen module,
# split into Python C-API names (Py*/_Py*) and everything else.

set -uo pipefail
WHEEL="$1"
MOD="$2"
DEST="$(mktemp -d -t audit-wheel)"
unzip -q "$WHEEL" -d "$DEST"
SOS=$(cd "$DEST" && find . -name '*.so' | sort)
N=$(echo "$SOS" | wc -l | tr -d ' ')

echo "=== $(basename "$WHEEL"): $N extension modules"
echo "--- platform / minos across all modules (count value)"
for f in $SOS; do otool -l "$DEST/$f" | awk '/LC_BUILD_VERSION/{b=1} b&&/platform/{p=$2} b&&/minos/{print "platform " p " minos " $2; b=0}'; done | sort | uniq -c
echo "--- architectures across all modules"
for f in $SOS; do lipo -archs "$DEST/$f"; done | sort | uniq -c
echo "--- modules with LC_ENCRYPTION_INFO_64"
for f in $SOS; do otool -l "$DEST/$f" | grep -q LC_ENCRYPTION_INFO_64 && echo x; done | wc -l | tr -d ' '
echo "--- dependent libraries across all modules (count library)"
for f in $SOS; do otool -L "$DEST/$f" | tail -n +3 | awk '{print $1}'; done | sort | uniq -c

echo
echo "=== undefined symbols of $MOD"
ALL=$(nm -u "$DEST/$MOD" | sort -u)
echo "total: $(echo "$ALL" | wc -l | tr -d ' ')"
echo "--- Python C-API (_Py*/__Py*): $(echo "$ALL" | grep -cE '^__?Py')"
echo "$ALL" | grep -E '^__?Py' | tr '\n' ' ' | fold -w 160
echo
echo "--- everything else (should be libSystem / libc++):"
echo "$ALL" | grep -vE '^__?Py' | tr '\n' ' ' | fold -w 160
echo
echo "--- where 'everything else' resolves (dyld bind info, library per symbol)"
dyld_info -imports "$DEST/$MOD" 2>/dev/null | awk 'NR>2 && $0 !~ /Python/ {print $NF}' | sort | uniq -c | head
