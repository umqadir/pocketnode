"""Run cibuildwheel's iOS build with a locally managed build Python.

cibuildwheel's iOS platform needs a macOS CPython of the same minor version to
act as the "build" interpreter that the cross virtual environment is based on.
Out of CI it insists on the python.org framework installer under
/Library/Frameworks, which needs sudo. Any macOS CPython of the right minor
version works for this purpose, so this shim swaps in the interpreter named by
the CIBW_IOS_BUILD_PYTHON environment variable (for example a uv-managed
python-build-standalone 3.13) and then runs cibuildwheel's normal entry point.
"""

import os
import sys
from pathlib import Path

import cibuildwheel.platforms.ios as ios_platform

_build_python = Path(os.environ["CIBW_IOS_BUILD_PYTHON"]).resolve()
if not _build_python.exists():
    raise SystemExit(f"CIBW_IOS_BUILD_PYTHON does not exist: {_build_python}")


def _use_local_build_python(*_args, **_kwargs):
    return _build_python


ios_platform.install_build_cpython = _use_local_build_python

from cibuildwheel.__main__ import main  # noqa: E402

sys.exit(main())
