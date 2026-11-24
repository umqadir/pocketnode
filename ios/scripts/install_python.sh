#!/bin/bash
# Xcode build phase: stdlib + extension frameworks, staged app packages, token.
set -euo pipefail
XCF="Python.xcframework"
source "$PROJECT_DIR/$XCF/build/utils.sh"
install_stdlib "$XCF"
PYVER=$(ls -1 "$CODESIGNING_FOLDER_PATH/python/lib" | grep -E "^python3\.[0-9]+$")
process_dylibs "$XCF" "python/lib/$PYVER/lib-dynload"

rsync -a --delete "$PROJECT_DIR/App/python/" "$CODESIGNING_FOLDER_PATH/app/"

# Packages staged by `pocketnode build-app` for this SDK (pure files + retagged binaries).
STAGE="${POCKETNODE_STAGE:-$HOME/.pocketnode/build}/$PLATFORM_NAME"
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
  for fw in "$STAGE/Frameworks/"*.framework; do
    name=$(basename "$fw")
    stamp="$(cat "$fw"/* | cksum) $EXPANDED_CODE_SIGN_IDENTITY"
    if [ "$(cat "$DEST/$name/.pn-stamp" 2>/dev/null)" != "$stamp" ] || [ ! -d "$DEST/$name/_CodeSignature" ]; then
      rsync -a --delete "$fw/" "$DEST/$name/"
      echo "$stamp" > "$DEST/$name/.pn-stamp"
      echo "$name" >> "$TODO"
    fi
  done
  # Frameworks staged by an earlier build and since removed from the selection.
  if [ -f "$STAGED_LIST" ]; then
    while read -r name; do
      [ -d "$STAGE/Frameworks/$name" ] || rm -rf "$DEST/$name"
    done < "$STAGED_LIST"
  fi
  (cd "$STAGE/Frameworks" && ls -d *.framework) > "$STAGED_LIST"
  xargs -P 8 -I{} /usr/bin/codesign --force --sign "$EXPANDED_CODE_SIGN_IDENTITY" ${OTHER_CODE_SIGN_FLAGS:-} \
      -o runtime --timestamp=none --preserve-metadata=identifier,entitlements,flags --generate-entitlement-der \
      "$DEST/{}" < "$TODO"
  rm -f "$TODO"
fi
cp "${POCKETNODE_HOME:-$HOME/.pocketnode}/token" "$CODESIGNING_FOLDER_PATH/pocketnode_token"
if [ -f "${POCKETNODE_HOME:-$HOME/.pocketnode}/app-settings.json" ]; then
  cp "${POCKETNODE_HOME:-$HOME/.pocketnode}/app-settings.json" "$CODESIGNING_FOLDER_PATH/pocketnode_settings.json"
fi
