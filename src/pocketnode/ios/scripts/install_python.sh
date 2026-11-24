#!/bin/bash
# Xcode build phase: stdlib + extension frameworks, staged app packages, token.
# Lines starting with POCKETNODE_PROGRESS are progress markers that `pocketnode
# app build` reads from the build output (`phase <name>`, or `<what> <done> <total>`).
set -euo pipefail
XCF="Python.xcframework"
source "$PROJECT_DIR/$XCF/build/utils.sh"
echo "POCKETNODE_PROGRESS phase copying-python"
install_stdlib "$XCF"
PYVER=$(ls -1 "$CODESIGNING_FOLDER_PATH/python/lib" | grep -E "^python3\.[0-9]+$")
process_dylibs "$XCF" "python/lib/$PYVER/lib-dynload"

echo "POCKETNODE_PROGRESS phase copying-packages"
rsync -a --delete "$PROJECT_DIR/App/python/" "$CODESIGNING_FOLDER_PATH/app/"

# Packages staged by `pocketnode app build` for this SDK (pure files + retagged binaries).
PN_HOME="${POCKETNODE_HOME:-$HOME/.pocketnode}"
STAGE="${POCKETNODE_STAGE:-$PN_HOME/build}/$PLATFORM_NAME"
if [ -d "$STAGE/app_packages" ]; then
  rsync -a --delete "$STAGE/app_packages/" "$CODESIGNING_FOLDER_PATH/app_packages/"
fi
if [ -d "$STAGE/Frameworks" ]; then
  DEST="$CODESIGNING_FOLDER_PATH/Frameworks"
  mkdir -p "$DEST"
  # Signing rewrites each binary, so the copies in the app never compare equal
  # to the staged ones. A stamp (a checksum of the staged files, plus the
  # signing identity) says whether a framework needs copying and signing again;
  # a rebuild that changes nothing signs nothing, and the rest sign eight at a time.
  TODO=$(mktemp)
  STAGED_LIST="${TARGET_TEMP_DIR:-$PROJECT_DIR/build}/pocketnode-staged-$PLATFORM_NAME.txt"
  FWS=("$STAGE/Frameworks/"*.framework)
  i=0
  for fw in "${FWS[@]}"; do
    name=$(basename "$fw")
    stamp="$(cat "$fw"/* | cksum) $EXPANDED_CODE_SIGN_IDENTITY"
    if [ "$(cat "$DEST/$name/.pn-stamp" 2>/dev/null)" != "$stamp" ] || [ ! -d "$DEST/$name/_CodeSignature" ]; then
      rsync -a --delete "$fw/" "$DEST/$name/"
      echo "$stamp" > "$DEST/$name/.pn-stamp"
      echo "$name" >> "$TODO"
    fi
    i=$((i + 1))
    echo "POCKETNODE_PROGRESS frameworks $i ${#FWS[@]}"
  done
  # Frameworks staged by an earlier build and since removed from the selection.
  if [ -f "$STAGED_LIST" ]; then
    while read -r name; do
      [ -d "$STAGE/Frameworks/$name" ] || rm -rf "$DEST/$name"
    done < "$STAGED_LIST"
  fi
  (cd "$STAGE/Frameworks" && ls -d *.framework) > "$STAGED_LIST"
  # Names relative to $DEST, appended by xargs: `xargs -I` caps each substituted
  # argument at 255 bytes, which a long home folder path exceeds. Each finished
  # signature prints a line, which this shell counts into a progress marker.
  N=$(wc -l < "$TODO" | tr -d ' ')
  if [ "$N" -gt 0 ]; then
    (cd "$DEST" && xargs -P 8 -n 1 /bin/sh -c '"$@" && echo signed' sh \
        /usr/bin/codesign --force --sign "$EXPANDED_CODE_SIGN_IDENTITY" \
        ${OTHER_CODE_SIGN_FLAGS:-} -o runtime --timestamp=none \
        --preserve-metadata=identifier,entitlements,flags --generate-entitlement-der < "$TODO") |
      { k=0; while read -r _; do k=$((k + 1)); echo "POCKETNODE_PROGRESS sign $k $N"; done; }
  fi
  rm -f "$TODO"
fi
cp "$PN_HOME/token" "$CODESIGNING_FOLDER_PATH/pocketnode_token"
if [ -f "$PN_HOME/app-settings.json" ]; then
  cp "$PN_HOME/app-settings.json" "$CODESIGNING_FOLDER_PATH/pocketnode_settings.json"
fi
