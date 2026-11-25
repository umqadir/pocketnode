#!/bin/bash
# Xcode build phase: stdlib + extension frameworks, staged app packages, token.
# Lines starting with POCKETNODE_PROGRESS are progress markers (`phase <name>`, or
# `<what> <done> <total>`). xcodebuild passes a script's output on in large
# chunks, so `pocketnode app build` also has them appended to the file it names
# in POCKETNODE_PROGRESS_FILE, which it follows as they're written.
# With POCKETNODE_FLAVOUR=store (project.store.yml) it builds the App Store
# edition instead: see store_install below. The rest of this file is the
# sideloaded (dev) edition.
set -euo pipefail
progress() {
  echo "POCKETNODE_PROGRESS $*"
  if [ -n "${POCKETNODE_PROGRESS_FILE:-}" ]; then
    echo "POCKETNODE_PROGRESS $*" >> "$POCKETNODE_PROGRESS_FILE"
  fi
}
XCF="Python.xcframework"
source "$PROJECT_DIR/$XCF/build/utils.sh"

# --------------------------------------------------------------- store edition
# Only real iOS binaries (staged from iOS wheels by `pocketnode app build --store`),
# no pairing token, no test modules, a privacy manifest in every framework that
# uses a required-reason API, and nothing written into the bundle that App Store
# validation rejects (stamps, dotfiles).

# Required-reason API symbols -> category and reason (Apple's "Describing use of
# required reason API"). Checked with nm rather than a Python helper: nm comes
# with Xcode, and a stock Mac's /usr/bin/python3 is only a stub until the command
# line tools are installed.
REASON_FILE='^_(stat|fstat|lstat|fstatat|getattrlist|fgetattrlist|getattrlistbulk|getattrlistat)$'
REASON_BOOT='^_mach_absolute_time$'
REASON_DISK='^_(statfs|fstatfs|statvfs|fstatvfs)$'
TEST_MODULES="_ctypes_test _testbuffer _testcapi _testclinic _testclinic_limited _testexternalinspection
  _testimportmultiple _testinternalcapi _testlimitedcapi _testmultiphase _testsinglephase _xxtestfuzz
  xxlimited xxlimited_35 xxsubtype"

privacy_entry() {  # category reason
  printf '\t\t<dict>\n\t\t\t<key>NSPrivacyAccessedAPIType</key><string>%s</string>\n' "$1"
  printf '\t\t\t<key>NSPrivacyAccessedAPITypeReasons</key><array><string>%s</string></array>\n\t\t</dict>\n' "$2"
}

write_framework_privacy() {  # framework folder
  local fw="$1" bin syms entries=""
  bin="$fw/$(basename "$fw" .framework)"
  rm -f "$fw/PrivacyInfo.xcprivacy"
  [ -f "$bin" ] || return 0
  syms=$(xcrun nm -u "$bin" 2>/dev/null || true)
  if grep -qE "$REASON_FILE" <<< "$syms"; then
    entries+=$(privacy_entry NSPrivacyAccessedAPICategoryFileTimestamp C617.1)$'\n'
  fi
  if grep -qE "$REASON_BOOT" <<< "$syms"; then
    entries+=$(privacy_entry NSPrivacyAccessedAPICategorySystemBootTime 35F9.1)$'\n'
  fi
  if grep -qE "$REASON_DISK" <<< "$syms"; then
    entries+=$(privacy_entry NSPrivacyAccessedAPICategoryDiskSpace E174.1)$'\n'
  fi
  [ -n "$entries" ] || return 0
  {
    printf '<?xml version="1.0" encoding="UTF-8"?>\n'
    printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    printf '<plist version="1.0">\n<dict>\n\t<key>NSPrivacyTracking</key><false/>\n'
    printf '\t<key>NSPrivacyTrackingDomains</key><array/>\n\t<key>NSPrivacyCollectedDataTypes</key><array/>\n'
    printf '\t<key>NSPrivacyAccessedAPITypes</key>\n\t<array>\n%s\t</array>\n</dict>\n</plist>\n' "$entries"
  } > "$fw/PrivacyInfo.xcprivacy"
}

store_wrap_extension() {  # lib-dynload base (relative to the bundle), full path of one .so
  # CPython's install_dylib (utils.sh), without its signing (done once at the end,
  # or not at all for an unsigned build) and with the framework typed FMWK.
  local base="$1" so="$2" rel mod fwdir
  rel=${so#$CODESIGNING_FOLDER_PATH/}
  mod=$(echo "${rel/$base/}" | cut -d "." -f 1 | tr "/" ".")
  fwdir="$CODESIGNING_FOLDER_PATH/Frameworks/$mod.framework"
  mkdir -p "$fwdir"
  cp "$PROJECT_DIR/$XCF/build/$PLATFORM_FAMILY_NAME-dylib-Info-template.plist" "$fwdir/Info.plist"
  plutil -replace CFBundleExecutable -string "$mod" "$fwdir/Info.plist"
  plutil -replace CFBundleIdentifier -string "$(echo "$PRODUCT_BUNDLE_IDENTIFIER.$mod" | tr "_" "-")" "$fwdir/Info.plist"
  plutil -replace CFBundlePackageType -string FMWK "$fwdir/Info.plist"
  if [ "$PLATFORM_NAME" = "iphonesimulator" ]; then
    plutil -replace CFBundleSupportedPlatforms -json '["iPhoneSimulator"]' "$fwdir/Info.plist"
  fi
  mv "$so" "$fwdir/$mod"
  echo "Frameworks/$mod.framework/$mod" > "${so%.so}.fwork"
  echo "${rel%.so}.fwork" > "$fwdir/$mod.origin"
}

store_install() {
  local PN_HOME="${POCKETNODE_HOME:-$HOME/.pocketnode}"
  local STAGE="${POCKETNODE_STAGE:-$PN_HOME/build}/store/$PLATFORM_NAME"
  local APP="$CODESIGNING_FOLDER_PATH"
  local DEST="$APP/Frameworks"
  local SIGN=1
  if [ "${CODE_SIGNING_ALLOWED:-YES}" = "NO" ] || [ -z "${EXPANDED_CODE_SIGN_IDENTITY:-}" ]; then
    SIGN=0
  fi

  # Settings: the store edition's, with "debug" true only in Debug builds.
  local SETTINGS="$APP/pocketnode_settings.json"
  if [ -f "$PN_HOME/store-settings.json" ]; then
    cp "$PN_HOME/store-settings.json" "$SETTINGS"
  else
    echo '{"edition":"store","tls":true,"privacy_url":null,"debug":false}' > "$SETTINGS"
  fi
  if [ "${CONFIGURATION:-}" = "Debug" ]; then
    plutil -replace debug -bool true "$SETTINGS"
  else
    plutil -replace debug -bool false "$SETTINGS"
  fi
  local TLS
  TLS=$(plutil -extract tls raw -o - "$SETTINGS" 2>/dev/null || echo true)
  rm -f "$APP/pocketnode_token"

  # The standard library, without the test suite (test.support stays: it is
  # small and holds the interpreters API) and the modules iOS can't use.
  progress phase copying-python
  install_stdlib "$XCF"
  local PYVER LIB
  PYVER=$(ls -1 "$APP/python/lib" | grep -E "^python3\.[0-9]+$")
  LIB="$APP/python/lib/$PYVER"
  if [ -d "$LIB/test" ]; then
    find "$LIB/test" -mindepth 1 -maxdepth 1 ! -name support ! -name __init__.py -exec rm -rf {} +
  fi
  for d in idlelib turtledemo tkinter ensurepip lib2to3; do
    rm -rf "${LIB:?}/$d"
  done
  local drop="$TEST_MODULES"
  if [ "$TLS" != "true" ]; then
    drop="$drop _ssl _hashlib"
  fi
  for m in $drop; do
    rm -f "$LIB/lib-dynload/$m".cpython-*.so "$LIB/lib-dynload/$m".cpython-*.fwork
    rm -rf "${DEST:?}/$m.framework"
  done
  find "$LIB/lib-dynload" -name "*.so" | while read -r so; do
    store_wrap_extension "python/lib/$PYVER/lib-dynload/" "$so"
  done

  progress phase copying-packages
  rsync -a --delete "$PROJECT_DIR/App/python/" "$APP/app/"

  # The fixed package set, staged from iOS wheels by `pocketnode app build --store`.
  if [ ! -d "$STAGE/app_packages" ]; then
    echo "error: no staged packages in $STAGE (run \`pocketnode app build --store\`)" >&2
    exit 1
  fi
  rsync -a --delete "$STAGE/app_packages/" "$APP/app_packages/"
  mkdir -p "$DEST"
  # The list of staged frameworks lives outside the bundle, so a framework
  # dropped from the package list is removed on the next build.
  local STAGED_LIST="${TARGET_TEMP_DIR:-$PROJECT_DIR/build}/pocketnode-store-staged-$PLATFORM_NAME.txt"
  mkdir -p "$(dirname "$STAGED_LIST")"
  if [ -f "$STAGED_LIST" ]; then
    while read -r name; do
      [ -d "$STAGE/Frameworks/$name" ] || rm -rf "${DEST:?}/$name"
    done < "$STAGED_LIST"
  fi
  : > "$STAGED_LIST"
  local fw i=0 n=0
  for fw in "$STAGE/Frameworks/"*.framework; do
    [ -d "$fw" ] && n=$((n + 1))
  done
  for fw in "$STAGE/Frameworks/"*.framework; do
    [ -d "$fw" ] || continue
    rsync -a --delete "$fw/" "$DEST/$(basename "$fw")/"
    basename "$fw" >> "$STAGED_LIST"
    i=$((i + 1))
    progress frameworks "$i" "$n"
  done

  # Privacy manifests, then signing (every framework, every build: no stamps in the bundle).
  progress phase writing-privacy-manifests
  for fw in "$DEST/"*.framework; do
    write_framework_privacy "$fw"
  done
  find "$APP" \( -name ".DS_Store" -o -name ".pn-stamp" \) -delete

  if [ "$TLS" = "true" ]; then ENC=true; else ENC=false; fi
  /usr/libexec/PlistBuddy -c "Delete :ITSAppUsesNonExemptEncryption" "$APP/Info.plist" >/dev/null 2>&1 || true
  /usr/libexec/PlistBuddy -c "Add :ITSAppUsesNonExemptEncryption bool $ENC" "$APP/Info.plist"

  if [ "$SIGN" = 1 ]; then
    # Each finished signature prints a line, counted into a progress marker (as in the dev edition).
    local N
    N=$(cd "$DEST" && ls -d *.framework | wc -l | tr -d ' ')
    (cd "$DEST" && ls -d *.framework | xargs -P 8 -n 1 /bin/sh -c '"$@" && echo signed' sh \
        /usr/bin/codesign --force \
        --sign "$EXPANDED_CODE_SIGN_IDENTITY" ${OTHER_CODE_SIGN_FLAGS:-} -o runtime --timestamp=none \
        --preserve-metadata=identifier,entitlements,flags --generate-entitlement-der) |
      { k=0; while read -r _; do k=$((k + 1)); progress sign "$k" "$N"; done; }
  else
    echo "Code signing is off for this build: frameworks left unsigned"
  fi
}

if [ "${POCKETNODE_FLAVOUR:-dev}" = "store" ]; then
  store_install
  exit 0
fi

# ------------------------------------------------------------ sideloaded (dev)
progress phase copying-python
install_stdlib "$XCF"
PYVER=$(ls -1 "$CODESIGNING_FOLDER_PATH/python/lib" | grep -E "^python3\.[0-9]+$")
process_dylibs "$XCF" "python/lib/$PYVER/lib-dynload"

progress phase copying-packages
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
    progress frameworks "$i" "${#FWS[@]}"
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
      { k=0; while read -r _; do k=$((k + 1)); progress sign "$k" "$N"; done; }
  fi
  rm -f "$TODO"
fi
cp "$PN_HOME/token" "$CODESIGNING_FOLDER_PATH/pocketnode_token"
if [ -f "$PN_HOME/app-settings.json" ]; then
  cp "$PN_HOME/app-settings.json" "$CODESIGNING_FOLDER_PATH/pocketnode_settings.json"
fi
