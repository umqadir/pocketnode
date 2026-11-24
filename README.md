# pocketnode

pocketnode turns an iPhone into a compute node for your Mac: you write ordinary Python (NumPy, pandas, scikit-learn, SciPy, PyTorch, polars, your own modules) and it decides where each piece runs, on the Mac's cores or on the phone over USB. A small iOS app embeds CPython, and the phone runs the Mac's own installed packages, so results are the same either way.

## What you need

| | Tested with | Required |
|---|---|---|
| Mac | Apple silicon (M4) | Apple silicon. The phone runs the Mac's own arm64 binaries, so Intel Macs can't work. |
| Xcode | 27.1 | 26 or later (the app targets iOS 26), with the iOS platform installed. |
| iPhone | iPhone 18 Pro on iOS 27.2 | iOS 26 or later, Developer Mode on, a USB cable. |
| Python | uv-managed CPython 3.13 | 3.13 exactly: the phone runs Python 3.13 and the Mac's minor version must match. |
| Apple ID | a free one | Free or paid. Free: the app is signed for 7 days at a time (`pocketnode app install` renews it), iOS asks you once to trust the developer, and you can register 10 app identifiers per 7 days. Paid (Apple Developer Program): signed for a year, and no trust step. |

All of this has been run on one setup only: the phone, Mac, Xcode and Python above, with a free Apple ID. Other phones, iOS 26, other Xcode versions, conda or Homebrew Pythons and paid accounts are untested. The GPU paths (PyTorch's Metal backend and MLX) work on the phone only: the simulator's GPU has no usable memory budget, so torch's Metal backend is turned off there, and MLX crashes in the simulator.

## Install and set up

Install pocketnode into the Python 3.13 environment you work in, then run `pocketnode setup`:

```
uv venv --python 3.13 && source .venv/bin/activate     # or use an existing 3.13 environment
uv pip install "pocketnode[all] @ git+https://github.com/umqadir/pocketnode"
pocketnode setup
```

Use the environment your code runs in, not `uv tool install`: the phone runs that environment's packages. `[all]` adds the libraries the integrations use (joblib, pandas, Dask, IPython) and a scientific stack; plain `pocketnode` works too.

`pocketnode setup` checks one thing per line. When something needs doing it says exactly what, then waits and checks again every few seconds, so you can fix it and watch it continue. Running it again is safe. What to expect:

- It checks the Mac, Python, Xcode (selected, licence accepted, first launch done) and the iOS platform, and downloads what the build needs into `~/.pocketnode/cache`: XcodeGen (6 MB, if it isn't installed) and BeeWare's embedded Python 3.13 for iOS (35 MB), each a pinned release checked against its checksum.
- It offers to download Xcode's Metal toolchain (about 840 MB). It's needed only to compile PyTorch's and MLX's GPU shaders for iOS; without it everything else works.
- Apple requires a few steps that only you can do, once each:
  1. Sign in to Xcode: Xcode › Settings › Apple Accounts › Sign In, then click the account once so Xcode loads its team.
  2. Plug the phone in with a cable, unlock it, and tap Trust when it asks whether to trust this computer.
  3. Turn on Developer Mode: on the phone, Settings › Privacy & Security › Developer Mode, restart when asked, then confirm with your passcode. (The menu appears once the phone has been connected to a Mac with Xcode.)
  4. With a free Apple ID, after the app is installed: Settings › General › VPN & Device Management, pick your Apple ID under Developer App, tap Trust, then open the pocketnode app. Setup tries to start the app once, tells you this if iOS refuses, and waits for the app to answer.
- It builds, signs and installs the app. The first build takes several minutes (about 5 on the test Mac, mostly signing a few hundred frameworks); later builds take about a minute, and installing about 30 to 40 seconds.
- It ends with the quick acceptance check: one PASS, FAIL or SKIP line per feature, run on the phone and compared with the Mac. An item is skipped when a library it needs isn't installed.

`pocketnode setup --sim` does the same with the iOS simulator instead of a phone (no Apple ID needed).

The app is built with numpy (when the environment has it). Other compiled packages go onto the phone while it runs, automatically when a task needs one (see Packages).

## A first example

```python
import numpy as np
import pocketnode as pn

ex = pn.connect()                         # Mac cores + the phone over USB

def top_singular_value(seed):
    a = np.random.default_rng(seed).normal(size=(1500, 1500))
    return float(np.linalg.svd(a, compute_uv=False)[0])

futs = [ex.submit(top_singular_value, s) for s in range(12)]
print([round(f.result(), 3) for f in futs])
print([f.placement.kind for f in futs])  # where each one ran: "phone" or "mac"
```

More of the interface:

```python
ex = pn.connect()                       # a concurrent.futures.Executor: Mac cores + phone
fut = ex.submit(fit_model, X, y)        # lambdas, closures and notebook functions all work
fut.result()
fut.placement                           # where it ran and why

with pn.parallel():                     # joblib backend: scikit-learn's n_jobs just works
    GridSearchCV(model, grid, n_jobs=-1).fit(X, y)

df.pn.groupby("user").apply(features)   # pandas, split across nodes, same result as df.groupby(...)
dask.compute(graph, scheduler=ex)       # Dask
```

In Jupyter, `%load_ext pocketnode` adds two cell magics. `%%phone` runs a cell on the phone; `%%pocket` runs it wherever fits. The cell's inputs are sent over, the names it assigns come back, and its output prints in the notebook.

## How it stays correct

- **The phone runs the Mac's own packages.** Compiled packages are not rebuilt or downloaded from somewhere else. pocketnode takes the Mac's installed arm64 binaries and retags them as iOS binaries: it rewrites the platform load command, fixes system framework paths, and signs them with your development certificate. The phone therefore runs exactly the versions the Mac runs.
  - Checked on an iPhone 18 Pro (iOS 27.2) and in the simulator: numpy, pandas, scipy, scikit-learn, pyarrow, polars, duckdb, lxml, matplotlib and eager PyTorch 2.11 (autograd and optimizers). Results match the Mac.
- **Only what the phone can run goes to it.** Each task's modules are checked against what the phone reports having. At run time, the phone sends a task back to the Mac if it:
  - imports a compiled module the phone lacks;
  - reads or writes a Mac file (for example `/Users/...`, or a relative path);
  - starts a process.

  It sends it back even if your code catches the error.
- **An error from the phone is confirmed on the Mac before you see it.** If the Mac runs the task fine, you get the Mac's result and that function stays on the Mac from then on.
- **The first result from the phone is checked against the Mac.** The first time a function runs on the phone, the Mac also runs it, ahead of the queue, and the results are compared field by field, with tolerance for floating point.
  - If they differ, you get the Mac's answer and that function stays on the Mac.
  - Fields that vary from run to run on the Mac itself (timings, unseeded randomness) are left out of the comparison; everything else still has to match.
  - A function is identified by its code and the kinds of arguments it gets. A joblib batch or a `map` chunk counts as the function inside it, so one estimator's verdict never applies to another.
- **Forcing placement raises instead of falling back.** `ex.on("phone")`, `pn.placement("phone")`, `@pn.runs_on("phone")` and `pn.connect(placement=...)` all force where code runs. If the forced node can't run it, you get a clear `PlacementError` rather than a silent fallback. `strict=True` does the same for automatic placement.
- **The phone can go away at any time.** It can get hot, go to the background, be unplugged, or crash. Its work moves to the Mac (speculative re-execution: the first result wins), and it rejoins when it's back.
  - A task that crashes the app is re-run on the Mac. The next time the app starts it reports what crashed (`ex.nodes[...].hello["last_crash"]`).
  - Inputs too big for the phone's free memory are never sent there.
- **Several sessions can share the phone.** A notebook, a script and `pocketnode status` can be connected at once, as long as they use the same Python environment. Each gets its own results.

## How it's fast

- **Large inputs cross the link once.** Arrays and DataFrames travel as raw buffers (pickle protocol 5) and are cached on the phone by content hash. The phone checks each hash on arrival, so an array you change between two submits is never mistaken for its old self. Data you'll reuse can be kept resident with `ex.pin(X)`.
- **The scheduler uses measurements.** It measures each function's compute time per node and the link's bandwidth. Memory-bound work stays on the Mac; heavy work spreads out.
- **Busy nodes get less work.** Thermal state and background state reduce how much work the phone is given.
- **Leaving the app doesn't stop it.** When work arrives, the app asks iOS for continued processing, so if you switch apps it keeps computing (iOS shows it as a live activity you can cancel). iOS throttles background work to roughly a fifteenth of full speed, so the scheduler sends it little until the app is in front again. If iOS suspends the app anyway, its tasks resume where they were when you come back, and anything urgent has been re-run on the Mac meanwhile.
- **The phone's GPU runs PyTorch.** `torch.backends.mps.is_available()` is true on the phone, so code that picks `"mps"` on the Mac picks it there too. torch's Metal shaders are compiled for iOS from the sources of the exact torch version on the Mac and swapped into the library when it's staged. On the test phone, 15 cases (elementwise maths, reductions, indexing, linear algebra, convolution with batch norm, attention, an LSTM, and training with SGD, Adam and AdamW) agree with the phone's CPU to about 1e-7. `pocketnode app install --no-torch-mps` turns it off.
- **The phone's GPU runs MLX.** `pocketnode packages add mlx` puts MLX on the phone with its Metal kernels compiled for iOS; the same `mlx` code then runs on the Mac's GPU or the phone's. On the test phone, results match numpy, a small network trains to convergence, and half-precision matrix multiply reaches 6 to 10 TFLOPS.
- **Pure-Python work uses all the phone's cores.** iOS has no child processes and no free-threaded Python build. So tasks that touch only the stdlib and pure-Python packages run in per-core subinterpreters, each with its own GIL. Work that releases the GIL (NumPy, scikit-learn, PyTorch) runs on threads.

## Packages

- **Pure-Python packages need nothing.** The phone imports them from the Mac on demand, at the Mac's version.
- **Compiled packages are added while the app runs.** No rebuild, no reinstall, no restart. When a task needs one the phone lacks, that task runs on the Mac and pocketnode puts the package on the phone in the background; later tasks use the phone. On the test phone, duckdb (38 MB) took 9.5 s and matplotlib with three dependencies took 48 s, most of it preparing the files on the Mac.
  - `pn.connect(auto_add=False)` turns the automatic part off; you then get a one-time note with the size and time instead.
  - Packages over 1.5 GB are never added automatically.
  - Replacing a package that is already loaded on the phone restarts the app, since a loaded native library can't be swapped.
- **Each package is checked against the iOS SDK before it ships.**
  - Functions the phone lacks but whose API defines failure (`OSStatus` returns) get a shim that reports "unimplemented".
  - Frameworks iOS doesn't have at all (AppKit, for matplotlib's macOS window backend or polars' clipboard support) get stand-ins, so the rest of the package loads.
  - Anything else is left out and listed with the reason; tasks that need it run on the Mac.

```
pocketnode packages              # Mac environment vs phone, with an iOS verdict for each compiled package
pocketnode packages select       # checklist: tick what should be on the phone, see the cost, apply
pocketnode packages add lightgbm # put it on the phone now (seconds)
pocketnode packages remove lightgbm
```

The checklist starts from what the phone has now (a default scientific stack on first use) and marks packages your tasks recently needed and didn't find. `pn.packages.add("lightgbm")` does the same from Python. Adding `--bundle` bakes a package into the app at the next build instead, which is how the default stack ships; app rebuilds sign only the frameworks that changed.

How adding works: iOS loads native code from an app's data folder if it is signed with the app's development certificate and its signing identifier equals the app's bundle ID. So the package is retagged in place, signed that way on the Mac, sent over USB and imported from the phone's own site directory.

## Day to day

- `pocketnode status` shows what's connected; `pocketnode check` runs the full acceptance check (`--quick` for the short one setup runs); `pocketnode bench` measures the link and compute.
- `pocketnode app install` rebuilds and reinstalls the app; `pocketnode app launch` starts it. Setup uses these too. Options: `--team TEAMID` when Xcode has several teams, `--bundle-id` to choose the app's identifier (by default it's `dev.pocketnode.<your team ID>`, saved at the first build), `--wifi`, `--no-torch-mps`.
- The app keeps the screen awake (and black) while it's in front, because a locked phone suspends it. If the app isn't running, the Mac can start it over USB, but only while the phone is unlocked.
- A free Apple ID signs the app for 7 days. pocketnode tells you when that's about to run out; `pocketnode app install` renews it.
- The app listens on USB only. `pocketnode app install --wifi` also accepts the Mac over the local network (`pn.connect("phone-address:7151")`); the pairing token is required either way. iOS asks once for local-network permission. On the test network Wi-Fi had a 20 ms round trip against 1 ms over USB, so USB is the better link when the cable is in.
- After updating pocketnode on the Mac, run `pocketnode app install` again; pocketnode tells you when the app on the phone is from a different version.
- If a task seems stuck on the phone, `ex.stacks("phone")` shows what every thread there is doing.
- For the simulator, `pn.connect("127.0.0.1:7151")`.
- pocketnode keeps its settings, pairing token, caches and build folder in `~/.pocketnode` (`POCKETNODE_HOME` moves it).

Not built: ExecuTorch and Core ML (PyTorch itself runs on the phone, CPU and GPU, which covers what they were for). Not measured: behaviour under sustained heat, and speed over a USB 3 cable. `RESEARCH-NOTES.md` has what was measured.

## Troubleshooting

These are the failures seen while building pocketnode, and the messages it prints for them.

- **"sign in to Xcode"**: Xcode has no Apple ID. Xcode › Settings › Apple Accounts › Sign In; a free Apple ID works.
- **"Xcode has your Apple ID but hasn't loaded its team yet"**: the team ID only becomes visible to command-line tools after Xcode has shown the account once. Open Xcode › Settings › Apple Accounts and click the account.
- **"several Apple development teams are available"**: pass the one to sign with, `pocketnode setup --team TEAMID` (the message lists them).
- **"Your team has no devices"** from xcodebuild: a new free team can only get a provisioning profile once a phone is registered with it, which happens when the build names a connected phone. Plug the phone in, unlock it and build again.
- **Developer Mode is off**: Settings › Privacy & Security › Developer Mode, restart, confirm with your passcode. The Mac can't turn it on for a phone with a passcode (`devmodectl` refuses). If the menu isn't there, connect the phone to the Mac and open Xcode once.
- **The app won't open, or "profile has not been explicitly trusted by the user"**: a free Apple ID's apps need trusting once per phone. Settings › General › VPN & Device Management › your Apple ID under Developer App › Trust, then open the pocketnode app.
- **The phone is locked after a crash**: if the app crashes, the phone locks itself after its usual delay, and the Mac can't start an app on a locked phone. Unlock it and pocketnode relaunches the app by itself. The task that crashed it is re-run on the Mac.
- **"the phone app's signing has expired"**: with a free Apple ID the app stops launching 7 days after it was built. Run `pocketnode app install` (or `pocketnode setup`).
- **"the bundle identifier ... is taken"** (xcodebuild: "cannot be registered to your development team because it is not available"): an app identifier belongs to the first team that registers it. pocketnode derives one from your team, so this only happens with a `--bundle-id` someone else has used; pick another, such as `--bundle-id com.yourname.pocketnode`. A free account can register 10 identifiers per 7 days, so reuse one rather than trying many.
- **"phone attached, app not answering"**: the phone is there but the app isn't listening. Open the app on the phone, or unlock the phone and let pocketnode start it. If the app is running but doesn't answer after two launches, pocketnode restarts it.
- **"the phone app was built with a different pairing token"**: the app was built under another `~/.pocketnode` (or `POCKETNODE_HOME`). Run `pocketnode app install`.
- **"the app on ... was built from a different version of pocketnode"**: pocketnode on the Mac was updated or edited since the app was built. Run `pocketnode app install`.
- **A package can't run on iOS**: some compiled packages link libraries iOS doesn't have (for example pysam and pyBigWig need libcurl, cffi's backend needs libffi). `pocketnode packages` shows a verdict for each compiled package; modules that can't load are left out with the reason, and tasks that need them run on the Mac.
- **"no iOS GPU shaders" or "no iOS GPU kernels"** while building: the Metal toolchain is missing. `xcodebuild -downloadComponent MetalToolchain`, then `pocketnode app install`.
- **"Python 3.x"** at the first setup step: the Mac's Python must be 3.13 to match the phone's. Create a 3.13 environment (`uv venv --python 3.13`) and install pocketnode there.
- A build that fails for another reason prints xcodebuild's errors and the path of the full log (in `~/.pocketnode/ios/build`).

## Store edition

Everything above describes the sideloaded app, which you build and sign yourself. The same project also builds a second flavour meant for the App Store. It has not been submitted: that needs a paid Apple Developer Program membership. `RESEARCH-NOTES.md` ("App Store edition: feasibility") has the rules it was built to, with sources.

What differs, and why:

| | Sideloaded | Store edition |
|---|---|---|
| Compiled packages | the Mac's own, retagged for iOS | a fixed set of real iOS builds inside the app |
| Adding a compiled package later | seconds, while the app runs | not possible: the rules forbid downloading native code, and it needs your development certificate |
| PyTorch, and torch or MLX on the GPU | yes | no: neither has an iOS build of its Python package |
| Pure-Python packages | imported from the Mac on demand | the same |
| Pairing | a token baked into your build | on the phone: tap Pair a Mac, type the code on the Mac, tap Allow |
| What the phone shows | a status line | who is connected, what is running, the source of every function a Mac has sent, every Python file fetched from the Mac |
| Stopping | quit the app | one switch (Accept work) and a Stop button |
| Background computing | asked for when work arrives | only when you tap "Keep running in the background" |
| Without a Mac | nothing to do | "Try it on this iPhone" runs Python you type |

Retagged macOS binaries can't ship in it. They lack a load command Apple's linker only writes when it links for iOS, and App Store Connect rejects uploads without it. So the store edition takes compiled packages only from iOS wheels: published ones (numpy, pillow, kiwisolver, markupsafe, contourpy, cffi), and ones cross-compiled here (pandas). The list is `src/pocketnode/ios/Store/packages.txt`; `pocketnode app wheel NAME VERSION` cross-compiles a C or Cython package into the wheelhouse so it can be added. scipy, scikit-learn, polars and pyarrow aren't in it yet: each needs its own cross-build (the notes say what that takes).

Because the set is fixed, the phone is used for a compiled package only when your Mac has the same release of it (`uv pip install numpy==2.5.2 pandas==3.1.1` for the default set). Otherwise those tasks run on the Mac and pocketnode says which version would match. Tasks that need a package the app doesn't have run on the Mac too.

Building and trying it:

```
pocketnode app wheel pandas 3.1.1          # once: cross-compile pandas for iOS (about 4 minutes)
pocketnode app install --store --sim       # build it and run it in the simulator (port 7161: add --port 7161)
pocketnode pair --address 127.0.0.1:7161   # type the code the app shows after you tap Pair a Mac
pocketnode check --address 127.0.0.1:7161
pocketnode app archive                     # Release archive for submission (unsigned unless --team is given)
pocketnode app validate                    # checks the archive the way upload validation would
pocketnode app build --dev                 # back to the sideloaded flavour
```

On a real phone, `pocketnode pair` works over the cable with no address. "Ask before running new code" in the app makes each new function of yours wait until you have read it on the phone and tapped Allow; it is off by default, and the `store_review` setting in `~/.pocketnode/app.json` makes it the default for a build. `store_tls: false` leaves Python's `ssl` module out of the app, which removes the export-compliance paperwork for bundled OpenSSL at the price of tasks not being able to use HTTPS on the phone.

## Layout

- `src/pocketnode/`: Mac side (executor, router, transport, integrations) and the worker that runs on the phone.
- `src/pocketnode/provision/`: environment scan, retagging, shims, app build and install, `pocketnode setup`.
- `src/pocketnode/ios/`: the thin Objective-C host app that embeds CPython. It ships inside the package; builds copy it to `~/.pocketnode/ios` and generate the Xcode project there.
- `src/pocketnode/check.py`: the acceptance check behind `pocketnode check`.
- `tests/`: runs against a local stand-in for the phone (`.phone-sim`, a venv holding only numpy, at the project's version, and cloudpickle). They don't need a phone or an Apple ID:

  ```
  uv sync
  uv venv --python 3.13 .phone-sim
  uv pip install --python .phone-sim/bin/python cloudpickle "numpy==$(uv run python -c 'import numpy; print(numpy.__version__)')"
  uv run pytest
  ```

`HANDOFF.md` is the original design brief. `RESEARCH-NOTES.md` holds sourced findings and measured results.
