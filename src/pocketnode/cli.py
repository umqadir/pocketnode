"""`pocketnode` command line.

    pocketnode status                 nodes, phone state, link
    pocketnode packages               what's on the phone vs this Mac's environment
    pocketnode packages add NAME...   put compiled packages on the phone
    pocketnode app build|install      build/install the iOS app (--sim for the simulator)
    pocketnode bench                  link + compute, phone vs Mac
    pocketnode experiments            device experiments that decide design branches
    pocketnode worker ...             run a worker here (what the phone app runs)
"""

from __future__ import annotations

import argparse
import json
import sys


def _connect(args, mac_workers=0):
    import pocketnode as pn
    addrs = [args.address] if getattr(args, "address", None) else []
    ex = pn.connect(*addrs, mac_workers=mac_workers, phone=not addrs, notes=False)
    if not ex.wait_for("phone", args.wait):
        src = next((s for s in ex.sources if hasattr(s, "status")), None)
        raise SystemExit(f"no phone after {args.wait:.0f} s ({src.status if src else 'not reachable'})")
    return ex


def cmd_status(args):
    ex = _connect(args)
    print(ex.describe())
    for n in ex.nodes.values():
        h = n.hello
        print(f"  {h['model']} · Python {'.'.join(map(str, h['python']))} · {h['cores']} · "
              f"{len(h['packages'])} packages · cache {h['store']['bytes'] / 2**20:.0f} MB")
    ex.shutdown(wait=False)


def cmd_packages(args):
    from .provision.packages import packages
    if args.action == "add":
        packages.add(*args.names, yes=args.yes, when_idle=args.when_idle, sdk=_sdk(args))
    elif args.action == "remove":
        packages.remove(*args.names, yes=args.yes, sdk=_sdk(args))
    elif args.action == "always-mac":
        packages.always_mac(*args.names)
    else:
        packages(check=not args.fast)


def _sdk(args):
    return "iphonesimulator" if getattr(args, "sim", False) else None


def cmd_app(args):
    from .provision import app
    sdk = "iphonesimulator" if args.sim else "iphoneos"
    app.save_config(sdk=sdk, **({"team": args.team} if args.team else {}),
                    **({"bundle_id": args.bundle_id} if args.bundle_id else {}))
    if args.action in ("build", "install"):
        p = app.build(sdk, restage=not args.no_stage)
        print(p)
        if args.action == "install":
            (app.install_sim if args.sim else app.install_device)(p)
            print("installed and launched")
    elif args.action == "launch":
        from .sources import launch_app
        launch_app(app.device_udid())


def cmd_bench(args):
    from . import bench
    ex = _connect(args, mac_workers=1)
    ex.wait_for("mac", 30)
    out = {"phone": ex.on("phone").submit(bench._cpu_info).result(), "link": bench.link(ex),
           "compute": bench.compute(ex)}
    print(json.dumps(out, indent=1))
    print("saved", bench.save("bench", out))
    ex.shutdown(wait=False)


def cmd_experiments(args):
    from . import bench
    from .provision import app
    ex = _connect(args, mac_workers=1)
    ex.wait_for("mac", 30)
    out = {"phone": ex.on("phone").submit(bench._cpu_info).result()}
    which = set(args.only or ["dlopen", "link", "memory", "background", "thermal"])
    if "dlopen" in which:
        out["dlopen_after_install"] = bench.dlopen_after_install(ex)
    if "link" in which:
        out["link"] = bench.link(ex, mb=256)
    if "memory" in which:
        out["memory"] = bench.memory(ex)
    if "background" in which:
        out["background"] = bench.background(ex, app.device_udid(), seconds=args.background_s)
    if "thermal" in which:
        out["thermal"] = bench.thermal(ex, seconds=args.thermal_s)
    print(json.dumps({k: v for k, v in out.items() if k != "thermal"}, indent=1, default=str))
    if "thermal" in out:
        t = out["thermal"]
        print(f"thermal: {t['gflops_start']} -> {t['gflops_end']} GFLOPS, stability {t['stability_pct']}%, "
              f"states {t['states']}")
    print("saved", bench.save("experiments", out))
    ex.shutdown(wait=False)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pocketnode", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def conn(p):
        p.add_argument("--address", help="host:port instead of USB (e.g. 127.0.0.1:7151 for the simulator)")
        p.add_argument("--wait", type=float, default=20.0, help="seconds to wait for the phone")

    p = sub.add_parser("status")
    conn(p)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("packages")
    p.add_argument("action", nargs="?", choices=["list", "add", "remove", "always-mac"], default="list")
    p.add_argument("names", nargs="*")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--when-idle", action="store_true")
    p.add_argument("--sim", action="store_true")
    p.add_argument("--fast", action="store_true", help="skip the static iOS check")
    p.set_defaults(fn=cmd_packages)

    p = sub.add_parser("app")
    p.add_argument("action", choices=["build", "install", "launch"])
    p.add_argument("--sim", action="store_true", help="build for the iOS simulator")
    p.add_argument("--team", help="Apple development team ID")
    p.add_argument("--bundle-id")
    p.add_argument("--no-stage", action="store_true", help="reuse staged packages")
    p.set_defaults(fn=cmd_app)

    p = sub.add_parser("bench")
    conn(p)
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("experiments")
    conn(p)
    p.add_argument("--only", nargs="*", choices=["dlopen", "link", "memory", "background", "thermal"])
    p.add_argument("--background-s", type=float, default=120)
    p.add_argument("--thermal-s", type=float, default=600)
    p.set_defaults(fn=cmd_experiments)

    p = sub.add_parser("worker", add_help=False)
    p.set_defaults(fn=lambda a: __import__("pocketnode.worker", fromlist=["main"]).main(a.rest))
    p.add_argument("rest", nargs=argparse.REMAINDER)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main(sys.argv[1:])
