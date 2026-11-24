"""Source patches pandas needs for an iOS cross build. Each is idempotent.

1. numpy include lookup. pandas/meson.build asks the build interpreter to
   `import numpy` and call numpy.get_include(). In an iOS cross build the numpy
   in the build environment is BeeWare's iOS wheel, whose extension modules
   carry an iOS suffix (.cpython-313-iphoneos.so / -iphonesimulator.so) and do
   not load in the macOS interpreter, so the import fails. The headers are
   plain files inside the wheel and are the ones we want (they carry the iOS
   _numpyconfig.h), so the patch locates them with importlib's find_spec,
   which finds the package without executing it.

2. The `nil` macro. BeeWare's iOS numpy was configured with
   NPY_HAVE_ENDIAN_H, so numpy/npy_endian.h includes <endian.h>. In the iOS
   SDK that header pulls in MacTypes.h, which in C++ does
   `#define nil nullptr`. pandas' skiplist.h (compiled as C++ in
   window/aggregations) has a struct member called `nil`, which the macro
   breaks. pandas never uses the Objective-C `nil`, so skiplist.h drops the
   macro before declaring its own.

Usage: python -I patch_pandas.py <path to pandas source tree>
"""

import sys
from pathlib import Path

NUMPY_OLD = """import os
import numpy as np
try:
    # Check if include directory is inside the pandas dir
    # e.g. a venv created inside the pandas dir
    # If so, convert it to a relative path
    incdir = os.path.relpath(np.get_include())
except Exception:
    incdir = np.get_include()
print(incdir)"""

NUMPY_NEW = """import os
import importlib.util
spec = importlib.util.find_spec('numpy')
inc = os.path.join(os.path.dirname(spec.origin), '_core', 'include')
try:
    # Check if include directory is inside the pandas dir
    # e.g. a venv created inside the pandas dir
    # If so, convert it to a relative path
    incdir = os.path.relpath(inc)
except Exception:
    incdir = inc
print(incdir)"""

SKIPLIST_OLD = """#include <string.h>

typedef struct node_t node_t;"""

SKIPLIST_NEW = """#include <string.h>

// iOS cross build: Apple's MacTypes.h (reached through numpy's <endian.h>
// include) defines `nil` as a macro in C++, which clashes with the member below.
#ifdef nil
#  undef nil
#endif

typedef struct node_t node_t;"""


def patch(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if new in text:
        print(f"already patched: {path}")
        return
    if old not in text:
        raise SystemExit(f"expected snippet not found in {path}; the patch needs updating for this pandas version")
    path.write_text(text.replace(old, new))
    print(f"patched: {path}")


def main() -> None:
    root = Path(sys.argv[1])
    patch(root / "pandas" / "meson.build", NUMPY_OLD, NUMPY_NEW)
    patch(root / "pandas" / "_libs" / "include" / "pandas" / "skiplist.h", SKIPLIST_OLD, SKIPLIST_NEW)


if __name__ == "__main__":
    main()
