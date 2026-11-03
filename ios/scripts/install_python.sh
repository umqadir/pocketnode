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
  mkdir -p "$CODESIGNING_FOLDER_PATH/Frameworks"
  for fw in "$STAGE/Frameworks/"*.framework; do
    name=$(basename "$fw")
    rsync -a "$fw/" "$CODESIGNING_FOLDER_PATH/Frameworks/$name/"
    /usr/bin/codesign --force --sign "$EXPANDED_CODE_SIGN_IDENTITY" ${OTHER_CODE_SIGN_FLAGS:-} -o runtime \
      --timestamp=none --preserve-metadata=identifier,entitlements,flags --generate-entitlement-der \
      "$CODESIGNING_FOLDER_PATH/Frameworks/$name"
  done
fi
cp "${POCKETNODE_HOME:-$HOME/.pocketnode}/token" "$CODESIGNING_FOLDER_PATH/pocketnode_token"
