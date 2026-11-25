"""Download a package's source distribution from PyPI and check its hash.

Usage: python -I fetch_sdist.py <name> <version> <dest dir>
Prints the path of the downloaded archive.
"""

import hashlib
import json
import sys
import urllib.request
from pathlib import Path


def main() -> None:
    name, version, dest = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json") as resp:
        info = json.load(resp)
    sdists = [u for u in info["urls"] if u["packagetype"] == "sdist"]
    if not sdists:
        raise SystemExit(f"{name} {version} has no sdist on PyPI")
    sdist = sdists[0]
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / sdist["filename"]
    if not target.exists():
        urllib.request.urlretrieve(sdist["url"], target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != sdist["digests"]["sha256"]:
        raise SystemExit(f"sha256 mismatch for {target}: {digest}")
    print(target)


if __name__ == "__main__":
    main()
