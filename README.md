# pocketnode

Your iPhone as a compute node for your Mac. You write ordinary Python (NumPy, pandas, scikit-learn, SciPy, PyTorch, polars, your own modules) and pocketnode decides where each piece runs: on the Mac's cores or on the phone over USB. It never changes the result.

```python
import pocketnode as pn

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

## Setup

1. `uv pip install -e .` into the Python environment you work in. The phone's Python minor version must match it (3.13 today).
2. Sign in to Xcode with an Apple ID: Xcode › Settings › Apple Accounts › Sign In, then click the account once so Xcode loads its team. A free account works.
3. On the iPhone, turn on Developer Mode: Settings › Privacy & Security › Developer Mode, restart, confirm with your passcode.
4. Plug the phone in, unlock it and run `pocketnode app install`. It finds your team, registers the phone, builds, signs and installs.
5. The first time only, iOS asks you to trust the developer: Settings › General › VPN & Device Management › your Apple ID › Trust. Then open the app, or run `pocketnode app launch`.
6. Run `pocketnode check`. It exercises every feature against the connected phone (each library against the Mac's results, the guards, caching, placement, verification, joblib, pandas, Dask, the Jupyter magic, both GPU paths) and prints a pass or fail line for each. `pocketnode status` and `pocketnode bench` show the connection and its speed.

Things to know:

- The app keeps the screen awake (and black) while it's in front, because a locked phone suspends it. If the app isn't running, the Mac can start it over USB, but only while the phone is unlocked.
- A free Apple ID signs the app for 7 days. pocketnode tells you when that's about to run out; `pocketnode app install` renews it.
- The app listens on USB only. `pocketnode app install --wifi` also accepts the Mac over the local network (`pn.connect("phone-address:7151")`); the pairing token is required either way. iOS asks once for local-network permission. On the test network Wi-Fi had a 20 ms round trip against 1 ms over USB, so USB is the better link when the cable is in.
- After updating pocketnode on the Mac, run `pocketnode app install` again; pocketnode tells you when the app on the phone is from a different version.
- If a task seems stuck on the phone, `ex.stacks("phone")` shows what every thread there is doing.
- If the app crashes, the phone locks itself after its usual delay, and the Mac can't restart the app on a locked phone. Unlock it and pocketnode relaunches the app by itself.

Not built: ExecuTorch and Core ML (PyTorch itself runs on the phone, CPU and GPU, which covers what they were for). Not measured: behaviour under sustained heat, and speed over a USB 3 cable. `RESEARCH-NOTES.md` has what was measured.

For the simulator, run `pocketnode app install --sim`, then `pn.connect("127.0.0.1:7151")`.

## Layout

- `src/pocketnode/`: Mac side (executor, router, transport, integrations) and the worker that runs on the phone.
- `src/pocketnode/provision/`: environment scan, retagging, shims, app build and install.
- `ios/`: the thin Objective-C host app that embeds CPython.
- `src/pocketnode/check.py`: the acceptance check behind `pocketnode check`.
- `tests/`: runs against a local stand-in for the phone (`.phone-sim`, a venv holding only numpy and cloudpickle):

  ```
  uv venv .phone-sim
  uv pip install --python .phone-sim/bin/python numpy cloudpickle
  uv run pytest
  ```

`HANDOFF.md` is the original design brief. `RESEARCH-NOTES.md` holds sourced findings and measured results.
