"""`pocketnode` command line.

    pocketnode setup [--sim]          guided first run: checks, builds, installs, acceptance check
    pocketnode status                 nodes, phone state, link
    pocketnode pair [--code CODE]     pair this Mac with an iPhone running the App Store edition (USB)
    pocketnode unpair [--all|PHONE]   forget an iPhone's pairing on this Mac
    pocketnode packages               what's on the phone vs this Mac's environment
    pocketnode packages select        checklist: tick what should be on the phone, see what it costs
    pocketnode packages add NAME...   put compiled packages on the running phone app (seconds, no restart)
    pocketnode packages remove NAME...  take them off again
                                      (--bundle: rebuild the app with the change baked in instead)
    pocketnode app build|install      build/install the iOS app (--sim for the simulator;
                                      --store / --dev switch between the App Store edition and the sideloaded one)
    pocketnode app archive            App Store archive of the store edition (unsigned without --team)
    pocketnode app validate [PATH]    check a store archive or app against App Store rules, PASS/FAIL per item
    pocketnode app wheel NAME VERSION build iOS wheels of a package into the wheelhouse (for the App Store edition)
    pocketnode bench                  link + compute, phone vs Mac
    pocketnode check [--quick]        release acceptance check: every feature, PASS/FAIL per item
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
              f"{len(h['packages'])} packages · cache {h['store']['bytes'] / 2**20:.0f} MB"
              + (" · App Store edition" if h.get("edition") == "store" else ""))
    ex.shutdown(wait=False)


def cmd_pair(args):
    from .pairing import PairingFailed, pair_phone
    try:
        r = pair_phone(address=args.address, code=args.code)
    except PairingFailed as e:
        raise SystemExit(f"not paired: {e}")
    except KeyboardInterrupt:
        raise SystemExit("not paired")
    print(f"paired with {r['phone_name']}; it connects whenever the pocketnode app is open "
          "(`pocketnode status` shows it)")


def cmd_unpair(args):
    from . import pairing
    creds = pairing.load()
    if args.all:
        targets = list(creds)
    elif args.phone:
        want = args.phone.strip().lower()
        targets = [pid for pid, c in creds.items()
                   if want in (pid.lower(), str(c.get("phone_name", "")).lower())]
        if not targets:
            raise SystemExit(f"this Mac isn't paired with {args.phone!r}"
                             + (f" (paired: {', '.join(_phone_names(creds))})" if creds else ""))
    elif len(creds) == 1:
        targets = list(creds)
    elif not creds:
        raise SystemExit("this Mac isn't paired with any iPhone")
    else:
        raise SystemExit(f"paired with {', '.join(_phone_names(creds))}: name one, or use --all")
    for pid in targets:
        name = creds[pid].get("phone_name") or pid
        pairing.remove(pid)
        print(f"forgot {name} on this Mac. To remove this Mac from the iPhone too, open pocketnode "
              "there and forget it under Paired Macs.")


def _phone_names(creds: dict) -> list[str]:
    return [f"{c.get('phone_name') or '?'} ({pid})" for pid, c in creds.items()]


def cmd_packages(args):
    from .provision.hot import CantAdd
    from .provision.packages import packages
    try:
        if args.action == "add":
            packages.add(*args.names, yes=args.yes, when_idle=args.when_idle, sdk=_sdk(args), bundle=args.bundle)
        elif args.action == "remove":
            packages.remove(*args.names, yes=args.yes, sdk=_sdk(args), bundle=args.bundle)
        elif args.action == "select":
            packages.select(sdk=_sdk(args), bundle=args.bundle)
        elif args.action == "always-mac":
            packages.always_mac(*args.names)
        else:
            packages(check=not args.fast)
    except CantAdd as e:
        raise SystemExit(str(e))


def _sdk(args):
    return "iphonesimulator" if getattr(args, "sim", False) else None


def cmd_app(args):
    from .provision import app
    if args.action == "validate":
        from .provision import validate
        raise SystemExit(validate.main(args.path, sdk="iphonesimulator" if args.sim else None))
    if args.action == "wheel":
        from .provision import wheels
        if not args.path or not args.version:
            raise SystemExit("usage: pocketnode app wheel NAME VERSION (builds iOS wheels for the App Store edition)")
        try:
            built = wheels.build_wheel(args.path, args.version, house=wheels.wheelhouse(app.config()))
        except RuntimeError as e:
            raise SystemExit(str(e))
        print("built:")
        for w in built:
            print(f"  {w}")
        print(f"to put it in the App Store edition, add this line to {wheels.packages_file(app.config())}:\n"
              f"  {args.path}=={args.version}")
        return
    if args.action == "archive":
        if args.dev:
            raise SystemExit("`pocketnode app archive` builds the App Store edition; the sideloaded one isn't archived")
        try:
            arc, signed = app.archive(team=args.team, restage=not args.no_stage)
        except RuntimeError as e:
            raise SystemExit(str(e))
        print(arc)
        if not signed:
            print("built without code signing (no --team given): `pocketnode app validate` checks its "
                  "structure, but exporting it for upload needs the owner's distribution signing (--team TEAMID)")
        return
    chosen = "store" if args.store else "dev" if args.dev else None
    store = (chosen or app.current_flavour()) == "store"
    sdk = "iphonesimulator" if args.sim else "iphoneos"
    app.save_config(sdk=sdk, **({"team": args.team} if args.team else {}),
                    **({"store_bundle_id" if store else "bundle_id": args.bundle_id} if args.bundle_id else {}),
                    **({"wifi": args.wifi} if args.wifi is not None else {}),
                    **({"torch_mps": args.torch_mps} if args.torch_mps is not None else {}),
                    **({"flavour": chosen} if chosen else {}))
    if args.action in ("build", "install") and store:
        print("building the App Store edition (store flavour; `--dev` switches back to the sideloaded app)")
        try:
            p = app.build(sdk, restage=not args.no_stage, flavour="store")
            print(p)
            if args.action == "install":
                if args.sim:
                    app.install_sim(p, device=args.device, flavour="store", port=args.port)
                else:
                    app.install_device(p, udid=args.device, flavour="store")
                print("installed and launched")
        except RuntimeError as e:
            raise SystemExit(str(e))
    elif args.action in ("build", "install"):
        p = app.build(sdk, restage=not args.no_stage)
        print(p)
        if args.action == "install":
            try:
                if args.sim and (args.device or args.port):
                    app.install_sim(p, device=args.device, port=args.port)
                elif args.device and not args.sim:
                    app.install_device(p, udid=args.device)
                else:
                    (app.install_sim if args.sim else app.install_device)(p)
            except RuntimeError as e:
                raise SystemExit(str(e))
            print("installed and launched")
    elif args.action == "launch":
        from .sources import launch_app
        why = launch_app(app.device_udid())
        if why:
            raise SystemExit(why)


def cmd_setup(args):
    from .provision import app, setup
    if args.bundle_id:
        app.save_config(bundle_id=args.bundle_id)
    app.save_config(sdk="iphonesimulator" if args.sim else "iphoneos")
    raise SystemExit(setup.main(sim=args.sim, yes=args.yes, team=args.team))


def cmd_bench(args):
    from . import bench
    ex = _connect(args, mac_workers=1)
    ex.wait_for("mac", 30)
    out = {"phone": ex.on("phone").submit(bench._cpu_info).result(), "link": bench.link(ex),
           "compute": bench.compute(ex)}
    print(json.dumps(out, indent=1))
    print("saved", bench.save("bench", out))
    ex.shutdown(wait=False)


def cmd_check(args):
    from . import check
    ex = _connect(args, mac_workers=2)
    ex.wait_for("mac", 30)
    code = check.main(ex, quick=args.quick)
    ex.shutdown(wait=False)
    raise SystemExit(code)


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

    p = sub.add_parser("setup", help="guided first run, from a fresh install to a passing check")
    p.add_argument("--sim", action="store_true", help="use the iOS simulator instead of an iPhone")
    p.add_argument("-y", "--yes", action="store_true", help="download what's needed without asking")
    p.add_argument("--team", help="Apple development team ID (when Xcode has several)")
    p.add_argument("--bundle-id", help="the app's bundle identifier (default: derived from the team)")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("status")
    conn(p)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("pair", help="pair this Mac with an iPhone running the App Store edition",
                       description="In the pocketnode app on the iPhone tap Pair a Mac, then run this "
                                   "with the iPhone connected by USB and type the code it shows.")
    p.add_argument("--address", help="host:port instead of USB (the simulator, or a stand-in on this Mac)")
    p.add_argument("--code", help="the six-digit code shown on the iPhone (asked for when not given)")
    p.set_defaults(fn=cmd_pair)

    p = sub.add_parser("unpair", help="forget an iPhone's pairing on this Mac")
    p.add_argument("phone", nargs="?", help="the iPhone's name or id (see `pocketnode unpair` with no argument)")
    p.add_argument("--all", action="store_true", help="forget every paired iPhone")
    p.set_defaults(fn=cmd_unpair)

    p = sub.add_parser("packages")
    p.add_argument("action", nargs="?", choices=["list", "select", "add", "remove", "always-mac"], default="list")
    p.add_argument("names", nargs="*")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--when-idle", action="store_true")
    p.add_argument("--bundle", action="store_true",
                   help="rebuild the app with the change baked in instead of changing the running app")
    p.add_argument("--sim", action="store_true")
    p.add_argument("--fast", action="store_true", help="skip the static iOS check")
    p.set_defaults(fn=cmd_packages)

    p = sub.add_parser("app")
    p.add_argument("action", choices=["build", "install", "launch", "archive", "validate", "wheel"])
    p.add_argument("path", nargs="?", help="validate: an .xcarchive or .app (default: the last store archive, "
                                           "else the last store build); wheel: the package name")
    p.add_argument("version", nargs="?", help="wheel: the package version to build")
    p.add_argument("--sim", action="store_true",
                   help="build for the iOS simulator (validate: the last store simulator build)")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--store", action="store_true",
                       help="build the App Store edition from now on (fixed packages from iOS wheels, pairing at run time)")
    which.add_argument("--dev", action="store_true", help="build the sideloaded edition from now on (the default)")
    p.add_argument("--team", help="Apple development team ID")
    p.add_argument("--bundle-id")
    p.add_argument("--device", help="install: the simulator (name or UDID) or iPhone (UDID) to install on")
    p.add_argument("--port", type=int, help="install on a simulator: the port the app listens on")
    p.add_argument("--no-stage", action="store_true", help="reuse staged packages")
    p.add_argument("--wifi", action=argparse.BooleanOptionalAction, default=None,
                   help="also accept the Mac over the local network (default: USB only)")
    p.add_argument("--torch-mps", action=argparse.BooleanOptionalAction, default=None,
                   help="let PyTorch's Metal backend (MPS) use the phone's GPU (default: on)")
    p.set_defaults(fn=cmd_app)

    p = sub.add_parser("bench")
    conn(p)
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("check")
    conn(p)
    p.add_argument("--quick", action="store_true", help="smaller sizes; skip torch and GridSearchCV")
    p.set_defaults(fn=cmd_check)

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
