#!/bin/bash
# Build real iOS wheels (device and arm64 simulator) of a PyPI package from its sdist.
# `pocketnode app wheel NAME VERSION` runs this with the wheelhouse as the output directory.
#
# Usage: build_ios_wheel.sh <package> <version> <output dir>
#
# Optional environment variables:
#   WORK_DIR     scratch directory for sources, tools and caches (default: a new temporary directory)
#   IOS_TARGETS  space-separated cibuildwheel identifiers to build
#                (default: "cp313-ios_arm64_iphoneos cp313-ios_arm64_iphonesimulator")
#
# It needs macOS on Apple Silicon with Xcode and its iOS SDK, uv, tar, and network access to
# PyPI, GitHub (the Python-Apple-support download) and pypi.anaconda.org/beeware.
# It uses these helpers from its own directory: cibw_local.py, fetch_sdist.py, patch_pandas.py.
# inspect_ios_wheel.sh and audit_ios_wheel.sh check the result.
# The wheels are tagged ios_13_0_arm64_iphoneos and ios_13_0_arm64_iphonesimulator and are
# compiled against BeeWare's Python-Apple-support 3.13-b18, which is what cibuildwheel 4.3.5
# downloads for cp313 iOS builds and what the app embeds.

set -euo pipefail

if [ $# -ne 3 ]; then
    echo "usage: $0 <package> <version> <output dir>" >&2
    exit 2
fi
PKG="$1"
VERSION="$2"
OUT_DIR="$(mkdir -p "$3" && cd "$3" && pwd)"
RECIPE_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="${WORK_DIR:-$(mktemp -d -t "ios-wheel-$PKG")}"
mkdir -p "$WORK_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
IOS_TARGETS="${IOS_TARGETS:-cp313-ios_arm64_iphoneos cp313-ios_arm64_iphonesimulator}"

# Pinned tool versions. Changing any of them changes the build.
# cibuildwheel 4.3.5 maps cp313 iOS builds to Python-Apple-support 3.13-b18.
CIBUILDWHEEL_VERSION=4.3.5
# The macOS interpreter the cross environment is based on. Only its minor version has to
# match the target; uv supplies a standalone build of it.
BUILD_PYTHON_VERSION=3.13.7
NINJA_VERSION=1.13.2
# BeeWare publishes iOS wheels of numpy and a few other binary packages here. pip in the
# cross environment accepts only iOS wheels or pure-Python wheels, so build requirements
# with C extensions have to come from this index.
BEEWARE_INDEX=https://pypi.anaconda.org/beeware/simple

echo "work dir: $WORK_DIR"
SRC_DIR="$WORK_DIR/src"        # downloaded and extracted sources only
TOOLS_DIR="$WORK_DIR/tools"    # build tools installed for the build (ninja)
CACHE_DIR="$WORK_DIR/cibw-cache"
mkdir -p "$SRC_DIR" "$CACHE_DIR"

# Build requirements are pinned through a pip constraints file. It applies to the isolated
# build environment that `python -m build` creates, and only constrains packages the project
# asks for. numpy is pinned to the BeeWare iOS build the app ships (2.5.2.post1); without the
# pin pip picks the newest numpy sdist from PyPI and tries to cross-compile numpy itself.
# Cython resolves to its pure-Python wheel (the only kind pip accepts for an iOS target), which
# works but makes cythonizing slower than usual.
CONSTRAINTS="$WORK_DIR/build-constraints.txt"
cat > "$CONSTRAINTS" <<'EOF'
cython==3.3.1
meson==1.12.1
meson-python==0.22.1
numpy==2.5.2.post1
wheel==0.48.0
versioneer==0.29
setuptools==84.2.1
EOF

# Step 1: a macOS CPython of the right minor version, through uv. cibuildwheel would
# otherwise demand the python.org framework installer, which needs sudo.
uv python install "$BUILD_PYTHON_VERSION"
BUILD_PYTHON="$(uv python find --no-project "$BUILD_PYTHON_VERSION")"
echo "build python: $BUILD_PYTHON"

# Step 2: ninja in a private tools directory. cibuildwheel cuts PATH down to system
# directories inside the cross environment and passes through only the tools named in
# CIBW_XBUILD_TOOLS; meson needs ninja to run the compile.
if [ ! -x "$TOOLS_DIR/bin/ninja" ]; then
    uv venv --quiet --python "$BUILD_PYTHON" "$TOOLS_DIR"
    uv pip install --quiet --python "$TOOLS_DIR/bin/python" "ninja==$NINJA_VERSION"
fi
export PATH="$TOOLS_DIR/bin:$PATH"

# Step 3: download the sdist from PyPI and check it against PyPI's sha256.
SDIST="$(python3 -I "$RECIPE_DIR/fetch_sdist.py" "$PKG" "$VERSION" "$SRC_DIR/download")"
echo "sdist: $SDIST"

# Step 4: clear compiler and linker settings inherited from the shell. A shell set up for
# Homebrew typically exports CPPFLAGS and LDFLAGS pointing at /opt/homebrew, and cibuildwheel
# passes the environment through to the compiler, so macOS headers and libraries would leak
# into the iOS build.
unset CFLAGS CXXFLAGS CPPFLAGS LDFLAGS OBJCFLAGS ARCHFLAGS \
      CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH \
      PKG_CONFIG_PATH PKG_CONFIG_LIBDIR PKG_CONFIG_SYSROOT_DIR \
      DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH DYLD_FRAMEWORK_PATH \
      SDKROOT MACOSX_DEPLOYMENT_TARGET CC CXX LD AR

# Step 5: cibuildwheel settings that hold for any package.
export CIBW_IOS_BUILD_PYTHON="$BUILD_PYTHON"  # read by cibw_local.py
export CIBW_CACHE_PATH="$CACHE_DIR"
export CIBW_BUILD_FRONTEND=build              # an isolated build environment, created from the cross venv so it is cross too
export CIBW_XBUILD_TOOLS=ninja
export CIBW_TEST_SKIP="*"                     # no test run in a simulator; the wheels are checked by inspection
export CIBW_BUILD_VERBOSITY=1
export PIP_EXTRA_INDEX_URL="$BEEWARE_INDEX"
export PIP_CONSTRAINT="$CONSTRAINTS"

# Step 6: per-package adjustments, applied to each fresh copy of the source tree.
prepare_source() {
    local tree="$1"
    case "$PKG" in
        pandas)
            # Two source patches (explained in patch_pandas.py): find numpy's headers without
            # importing the iOS numpy in the macOS build interpreter, and drop Apple's C++
            # `nil` macro before skiplist.h declares a struct member of that name.
            python3 -I "$RECIPE_DIR/patch_pandas.py" "$tree"
            # pandas' own [tool.cibuildwheel] configuration sets LDFLAGS=-Wl,--strip-all for
            # every platform (meant for its Linux builds). Clearing it leaves the iOS link with
            # only the flags the Python-Apple-support compiler wrappers and meson supply.
            export CIBW_ENVIRONMENT_IOS=""
            ;;
        *)
            ;;
    esac
}

# Step 7: build each target from its own freshly extracted source tree, because meson-python
# builds inside the tree and the two targets must not share state.
for ident in $IOS_TARGETS; do
    tree_parent="$SRC_DIR/$ident"
    mkdir -p "$tree_parent"
    tar -xzf "$SDIST" -C "$tree_parent"
    tree="$(find "$tree_parent" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    prepare_source "$tree"

    start=$(date +%s)
    # cibw_local.py is cibuildwheel's normal entry point with the build Python swapped for
    # the uv one (see that file).
    uvx --from "cibuildwheel==$CIBUILDWHEEL_VERSION" python -I "$RECIPE_DIR/cibw_local.py" \
        --only "$ident" --output-dir "$WORK_DIR/wheels" "$tree"
    echo "built $ident in $(( $(date +%s) - start ))s"
done

# Step 8: copy the finished wheels to the output directory.
cp "$WORK_DIR"/wheels/*.whl "$OUT_DIR"/
ls -l "$OUT_DIR"
