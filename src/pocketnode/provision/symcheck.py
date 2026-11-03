"""Static check: do a Mach-O's imports exist on iOS? (against the iOS SDK .tbd stubs)

Predicts load failures of retagged macOS binaries before they reach the phone.
Missing libraries or non-weak symbols mean dyld would refuse the file.
"""
import functools
import json
import os
import re
import subprocess
import sys

import yaml

@functools.lru_cache(None)
def _sdk() -> str:
    return subprocess.check_output(["xcrun", "--show-sdk-path", "--sdk", "iphoneos"], text=True).strip()

def ios_path(install):
    # macOS deep framework path -> iOS shallow path
    m = re.match(r"(.*/(\w[\w+-]*)\.framework)/Versions/[^/]+/(.*)$", install)
    if m:
        return m.group(1) + "/" + m.group(3)
    return install

def tbd_file_for(install):
    p = ios_path(install)
    base = os.path.splitext(p)[0] if p.endswith(".dylib") else p
    for cand in (_sdk() + base + ".tbd", _sdk() + p + ".tbd"):
        if os.path.exists(cand):
            return cand
    return None

class Loader(yaml.SafeLoader): pass
Loader.add_multi_constructor("!", lambda l, s, n: l.construct_mapping(n, deep=True) if isinstance(n, yaml.MappingNode) else None)

@functools.lru_cache(None)
def load_docs(path):
    txt = open(path).read()
    if txt.lstrip().startswith("{"):
        return [("json", json.loads(txt))]
    return [("yaml", d) for d in yaml.load_all(txt, Loader=Loader) if d]

ARM = ("arm64-ios", "arm64e-ios", "arm64e.x1-ios")
def ok_targets(t): return any(x in ARM for x in t)

@functools.lru_cache(None)
def exports_of(install):
    """Return set of exported symbols for install name incl. re-exports, or None if not in SDK."""
    f = tbd_file_for(install)
    if f is None:
        return None
    syms, reexp = set(), []
    for kind, d in load_docs(f):
        if kind == "yaml":
            if d.get("install-name") not in (ios_path(install), install) and d.get("install-name") != ios_path(install):
                # inline document for another library; index it separately
                continue
            for sect in ("exports", "reexports"):
                for e in d.get(sect, []) or []:
                    if not ok_targets(e.get("targets", [])): continue
                    for s in (e.get("symbols") or []) + (e.get("weak-symbols") or []) + (e.get("thread-local-symbols") or []):
                        syms.add(s)
                    for c in e.get("objc-classes") or []:
                        syms.update({f"_OBJC_CLASS_$_{c}", f"_OBJC_METACLASS_$_{c}"})
                    for c in e.get("objc-eh-types") or []:
                        syms.add(f"_OBJC_EHTYPE_$_{c}")
                    for c in e.get("objc-ivars") or []:
                        syms.add(f"_OBJC_IVAR_$_{c}")
            for r in d.get("reexported-libraries", []) or []:
                if ok_targets(r.get("targets", [])):
                    reexp += r.get("libraries", [])
        else:
            # tbd v5 JSON
            for doc in [d["main_library"]] + d.get("libraries", []):
                if doc["install_names"][0]["name"] != ios_path(install): continue
                for sect in ("exported_symbols", "reexported_symbols"):
                    for e in doc.get(sect, []):
                        for k in ("global", "weak", "thread_local"):
                            g = e.get("data", {}).get(k, []) + e.get("text", {}).get(k, [])
                            syms.update(g)
                        for c in e.get("data", {}).get("objc_class", []):
                            syms.update({f"_OBJC_CLASS_$_{c}", f"_OBJC_METACLASS_$_{c}"})
                for r in doc.get("reexported_libraries", []):
                    reexp += r.get("names", [])
    # inline sub-documents in same file
    for r in reexp:
        sub = inline_exports(f, r)
        if sub is None:
            sub = exports_of(r) or set()
        syms |= sub
    return frozenset(syms)

@functools.lru_cache(None)
def inline_exports(f, install):
    for kind, d in load_docs(f):
        if kind == "yaml" and d.get("install-name") == install:
            break
    else:
        return None
    # reuse exports_of logic by temporarily faking: simple re-implementation
    syms = set()
    for sect in ("exports", "reexports"):
        for e in d.get(sect, []) or []:
            if not ok_targets(e.get("targets", [])): continue
            for s in (e.get("symbols") or []) + (e.get("weak-symbols") or []) + (e.get("thread-local-symbols") or []):
                syms.add(s)
            for c in e.get("objc-classes") or []:
                syms.update({f"_OBJC_CLASS_$_{c}", f"_OBJC_METACLASS_$_{c}"})
    for r in d.get("reexported-libraries", []) or []:
        if ok_targets(r.get("targets", [])):
            for lib in r.get("libraries", []):
                syms |= inline_exports(f, lib) or exports_of(lib) or set()
    return syms

def leaf(install):
    b = os.path.basename(install)
    return b.split(".")[0]

def analyze(path):
    libs = []
    out = subprocess.run(["otool", "-arch", "arm64", "-L", path], capture_output=True, text=True).stdout.splitlines()[1:]
    own = subprocess.run(["otool", "-arch", "arm64", "-D", path], capture_output=True, text=True).stdout.splitlines()[1:]
    if own and out and out[0].strip().startswith(own[0].strip()):
        out = out[1:]
    for l in out:
        l = l.strip()
        if l: libs.append(l.split(" (compatibility")[0])
    imps = subprocess.run(["dyld_info", "-arch", "arm64", "-imports", path], capture_output=True, text=True).stdout
    res = {"missing_libs": [], "missing_syms": [], "flat_nonpy": [], "flat_py": 0, "rpath_libs": []}
    leafmap = {leaf(l): l for l in libs}
    for l in libs:
        if l.startswith("@"):
            res["rpath_libs"].append(l)
        elif exports_of(l) is None:
            res["missing_libs"].append(l)
    for line in imps.splitlines():
        m = re.match(r"\s+(\S+)\s+(\[weak-import\]\s+)?\(from (.+)\)", line)
        if not m: continue
        sym, weak, src = m.group(1), m.group(2), m.group(3)
        if src == "<flat-namespace>":
            if re.match(r"__?Py|_PyInit|__Py", sym) or sym.startswith("_Py") or sym.startswith("__Py"):
                res["flat_py"] += 1
            else:
                res["flat_nonpy"].append(sym)
            continue
        lib = leafmap.get(src)
        if lib is None or lib.startswith("@"):
            continue
        ex = exports_of(lib)
        if ex is None: continue
        if sym not in ex and not weak:
            res["missing_syms"].append((sym, lib))
    return res

if __name__ == "__main__":
    for p in sys.argv[1:]:
        r = analyze(p)
        print(json.dumps({"file": p, **r}))
