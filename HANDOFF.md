# pocketnode — handoff brief

This brief carries over a planning conversation (Nov 2, 2026) so a local Claude Code session on the Mac can build the project. The local session has Xcode, the real Mac Python environments and the physical iPhone, none of which the cloud session had. Read all of it before writing code.

**Status:** design agreed, research partly in. No code written yet. Research threads that were still running get appended to `RESEARCH-NOTES.md` in this folder as they finish.

---

## 1. Goal

Turn an iPhone 18 Pro (A20 Pro, 12 GB, iOS 26.x) into a USB-connected compute node for a MacBook Air M4 (32 GB). The gold standard is arbitrary Python code on arbitrary data, with the phone behaving like just another processor node. It should cover about 95% of real data-science workloads.

The app is sideloaded with the developer's own signing and Developer Mode enabled. **App Store rules are irrelevant.** Only real technical OS limits count.

## 2. The user's principles (these override convenience)

1. **Push up against actual platform limits only.** Add no restriction the platform doesn't impose. If something can possibly work, implement it. Effort is not the constraint.
2. **Idiomatic, and correct by default.** The user should not have to remember restrictions. They write ordinary Python, NumPy, pandas, scikit-learn and PyTorch code, and the system works out where it can run.
   - Never silently wrong.
   - Self-teaching: results say where they ran and why, and point at the exact line and change that would unlock a faster path. That's a suggestion, never a requirement.
3. **Importing packages must just work.** Don't limit what can be imported; design around signing instead (§5).
4. **The user decides on costly actions,** with measured estimates. Don't silently kick work to the Mac when the user might prefer to install the missing package now.
5. **Keep working when the user switches apps on the phone.** At minimum pause and resume cleanly; ideally keep computing (§4).
6. **Research before design decisions.** Don't present designs as settled before the facts are in.
7. **Scope rule:** build everything except the "blue sky" items (§8). Pull a blue-sky item in only if it becomes obviously straightforward or clearly worth it.

## 3. Real platform limits (as understood so far)

| Limit | Status | Consequence |
|---|---|---|
| No JIT (no writable+executable memory) | Firm (re-check iOS 26 debugger-attach JIT; research pending) | No numba, JAX, `torch.compile` on device. CPython interprets bytecode, so it's fine. |
| No child processes | Firm | One process, threads only. No `multiprocessing` on the phone. |
| Native code must be code-signed | Firm | Compiled packages need iOS builds, signed. Python extensions go in `.framework` bundles per CPython's iOS docs. |
| Loading dylibs signed *after* install | **UNKNOWN, decisive** | Decides whether adding a compiled package takes seconds (push and load) or needs a rebuild and reinstall. Test on device early (§9). |
| Background execution | Research pending | `BGContinuedProcessingTask` (iOS 26), audio background mode, etc. Decides how long compute continues after leaving the app. |
| Memory ceiling | Research pending | Increased-memory-limit entitlement; `os_proc_available_memory()`. |
| Thermal throttling | Real | Scheduler must adapt. |
| GIL | Check | Free-threaded CPython for iOS? If none, pure-Python loops use one core. NumPy/BLAS/sklearn release the GIL. |
| Python minor version must match between Mac and phone | Firm for bytecode shipping | Pick one (3.13 or 3.14) for both. |

## 4. App switching and lifecycle (agreed design)

Correctness must not depend on the background research. The research only decides how long the phone keeps computing.

- When the app is about to suspend, the worker tells the Mac to stop assigning new work.
- Results finished while disconnected are held on the phone and delivered on reconnect. The Mac reconnects automatically.
- Speculative re-execution: when the queue drains, the Mac re-runs anything stuck on a paused or throttled phone using its idle cores. First result wins.
- Thermal state is reported; the scheduler gives a hot phone less work.
- If the app was killed, the Mac relaunches it over USB (`xcrun devicectl device process launch`) and re-queues lost tasks.
- Then use whatever background keep-alive the research shows actually works for a sideloaded app (continued-processing task, audio mode, …).

## 5. Packages: importing anything

- **Pure-Python packages:** always work. The phone imports them on demand from the Mac's environment (an import hook fetches source from the Mac, versions match, cached on the phone). No selection needed.
- **Compiled packages** need an iOS-targeted, signed build. The provisioning order:
  1. An official iOS wheel on PyPI, or BeeWare's iOS wheels (`https://pypi.anaconda.org/beeware/simple/`): download and sign, seconds.
  2. **Retag the Mac's own macOS arm64 build** (rewrite the Mach-O `LC_BUILD_VERSION` platform macOS→iOS, wrap as `.framework`, re-sign). Unproven; research pending. Predict load failures statically by checking imported symbols against the iOS SDK `.tbd` stubs before trying.
  3. Cross-compile on the Mac (`cibuildwheel` iOS support, Xcode's iOS toolchain; Rust targets iOS). Cache per package and version.
  4. Truly impossible on iOS (JIT-based, macOS-only APIs): runs on the Mac, and the note explains why.
- **If post-install loading works:** provisioning is invisible ("add now" takes seconds, no restart). Keep the selector as an optional pre-bundle step.

### The install/selector interface (agreed)

- **One interface,** usable any time: the first install and every later change.
- **On first use** it scans a chosen Mac Python environment (venv or conda):
  - A sensible scientific default stack is pre-checked.
  - Every other installed package is optional.
  - Each compiled package shows its status: iOS build exists / retaggable / needs cross-compile / impossible (with reason).
- **After first use** it reflects the phone's **current state**, read from the phone's own report, not a Mac-side list:
  - Checked means installed.
  - Edits show as a diff, with estimates for just those changes.
  - Recently needed but missing packages are flagged, **not** pre-checked.
- **Reinstalling isn't free.** The app restarts: in-flight tasks are re-queued, and pinned and cached data on the phone is lost and must be re-sent. Build time can be minutes. So a reinstall shows a warning with what would be disrupted, plus options to wait until idle or proceed now. It never reinstalls mid-job on its own.
- **When a task needs a missing compiled package:** prompt with measured estimates, e.g. "lightgbm isn't on the phone. [Add it now — ~4 min (build ~3 min, install/restart ~40 s); then this runs on the phone] [Run on Mac this time] [Always use Mac for lightgbm]".
  - "Add now" opens the same selector with the package pre-checked.
  - Other tasks keep running meanwhile.
- **Scripts that can't prompt** use a one-time setting. The default is run on the Mac and log the miss.
- **Estimates are measured.** Time every build and install and store the history per package. First-time estimates come from the source type (download vs cross-compile, package size) and are labelled as such.
- **Free Apple ID accounts** have a 7-day profile expiry (verify), so offer to fold pending package changes into the required reinstall.

## 6. The user-facing interface (agreed direction)

Existing idioms, no new vocabulary:

- `pn.connect()` returns a standard `concurrent.futures.Executor` that pools Mac cores and the phone. `submit`/`map` behave as usual.
- **joblib backend:** `with pn.parallel(): GridSearchCV(..., n_jobs=-1)`.
- **Dask:** `dask.compute(..., scheduler=executor)`.
- **pandas helpers:** partition and group-wise apply that never splits a group.
- **Jupyter:** a `%%phone` cell magic that ships the variables the cell uses and returns the ones it assigns.
- **Arbitrary functions:** lambdas, closures and notebook-defined functions, via cloudpickle.
- **Data movement:**
  - Arrays and DataFrames travel as raw buffers (pickle protocol 5 out-of-band).
  - Buffers are content-addressed, so the same `X` used in 200 fits crosses USB once.
  - Data can be explicitly pinned on the phone, with tasks routed to where it lives.
- **Transport:** talk to usbmuxd directly from Python (no `iproxy` needed). Wi-Fi via Bonjour is optional. A pairing token is required, since this is remote code execution by design.

### pandas

- **Inside any task:** pandas runs on the phone (BeeWare publishes iOS pandas wheels), so all ordinary pandas code works there.
- **A single pandas call on one big DataFrame** (`df.groupby(...).agg(...)` run at the Mac prompt) is not intercepted by the executor itself. To split one call across Mac and phone, use a partitioned DataFrame with the same API on top of the executor:
  - Dask DataFrame first (it works through the Dask integration).
  - Modin is worth evaluating too.
  - Group-wise helpers never split a group.
- **The router should be cost-aware for pandas.** Memory-bound ops (filters, simple column math) are often faster left on the Mac than moved over USB. Heavy `apply`/`transform`, group-apply, rolling/UDF work and feature engineering benefit from offloading.
  - Measure transfer cost versus compute and decide from the measurements.
  - Surface the decision in the "where it ran" note.

### Placement router (makes correctness the default)

- The phone advertises its capabilities: Python version, packages and versions, GPU/ML paths, memory, thermal state.
- Before sending, the Mac inspects each task: the modules its code references and the types of its arguments. It routes to the phone only what the phone can run.
- At runtime, an ImportError for a compiled module, a JIT attempt and similar failures are caught. The task is re-run on the Mac and the function is remembered as Mac-only (or the missing-package prompt fires).
- Default: correct results plus a short "where it ran and why" note. `strict=True` raises instead, for debugging placement.

## 7. PyTorch and ML on the phone (research findings, Nov 2 2026)

Findings (sources in RESEARCH-NOTES.md):

- **No iOS build of the real `torch` Python package exists.**
  - PyPI has no iOS wheels, and neither does BeeWare.
  - In pytorch `CMakeLists.txt`, IOS sets `INTERN_BUILD_MOBILE`, which forces `BUILD_PYTHON OFF`.
  - `scripts/build_ios.sh` was removed in 2.9.
  - MPS is macOS-only.
  - Eager torch needs no JIT, so a port is theoretically possible: multi-week, unsupported, CPU-only.
- **ExecuTorch v1.5.1 is the supported route.**
  - iOS 17+, SwiftPM.
  - Backends: XNNPACK (CPU), Core ML (ANE/GPU, the most mature), MLX (GPU; experimental, ~90 ops, inference-focused, not CI-tested on devices).
  - Workflow: `torch.export` on the Mac → `.pte` → load on the phone.
  - Training is experimental: joint forward+backward graph, CPU only, C++ API on iOS.
  - Python pybindings exist, but no iOS build is published.
- **MLX Swift runs on iPhone.**
  - No MLX Python wheels for iOS. The issue was opened 2026-06-12; a maintainer says the build system "appears able", and CI is the missing piece.
  - MLX enables GPU neural-accelerator (NAX) matmul paths for phone GPUs of generation ≥18 on iOS 26.2+ (inferred to include the A20 Pro).
  - Metal kernels are precompiled in the metallib.
- **Core ML:**
  - coremltools converts `torch.export` models (beta, ~70% op coverage) or traced models.
  - `MLModel.compileModel(at:)` works on device for models sent at runtime.
  - On-device training is limited to the legacy updatable NeuralNetwork format.
- **torch→MLX converters** are immature, apart from ExecuTorch's MLX delegate.

**Agreed plan (non-blue-sky):**
- Torch inference on the phone via ExecuTorch (XNNPACK / Core ML / MLX) and via Core ML directly.
- Model capture on the Mac, with automatic fallback when export fails.
- **The first run of any new path is numerically checked against the Mac on a sample, within a tolerance,** before it's trusted. The decision is cached.
- Build MLX Python for iOS ourselves (feasible build work), so the same `mlx` code runs on Mac and phone GPU.
- Build ExecuTorch Python runtime bindings for iOS (no torch dependency at runtime).

## 8. Blue sky (parked unless it proves straightforward or clearly worth it)

- Full eager PyTorch port (libtorch + `torch._C`) to iOS, CPU-only.
- Eager torch→MLX translator: a PrivateUse1 "mlx" device in the ported torch, with per-op CPU fallback and autograd. Alternative: a Python `__torch_dispatch__` wrapper over MLX arrays.
  - Semantic gaps: no float64 on the MLX GPU, no in-place ops, views and strides differ.
- Graph translator: a `torch.compile`/AOTAutograd backend that emits MLX programs. Mainly valuable for training on the phone GPU.
- The payoff is capped by how much the phone adds over the Mac's own GPU after USB transfer cost. Decide with measured numbers from the real device.

## 9. Build order

1. **Core system (pure Python, testable with a local process standing in for the phone):**
   - wire format and buffer cache
   - worker runtime
   - transport (TCP + direct usbmuxd)
   - executor/scheduler with pause/resume/reconnect, speculative re-execution and thermal weighting
   - placement router
   - on-demand pure-Python imports
   - integrations: joblib, Dask, pandas, IPython magic
2. **iOS host app (thin Swift):**
   - embed CPython (BeeWare Python-Apple-support or CPython's `Apple/` build)
   - expose thermal, memory and lifecycle state to Python
   - background-mode handling
   - **first on-device experiments:**
     - (a) dlopen a dylib signed after install
     - (b) background keep-alive duration
     - (c) usbmux throughput
     - (d) retagged macOS `.so` loading
3. **Installer/provisioning:** env scan, classification, iOS wheel lookup, retag, cross-compile, signing, measured estimates, the selector UI, the missing-package prompt.
4. **ML paths:** ExecuTorch / Core ML inference with numeric verification, MLX Python iOS build.
5. **Docs:** RESEARCH.md (sourced), DESIGN.md, README with dev-mode setup.

## 10. Things only the real device can settle

- Post-install dylib loading.
- Background execution duration and CPU limits.
- Actual usbmux throughput.
- Whether retagged macOS binaries load.
- Sustained thermal behaviour.
- Real speedups versus the Mac alone.

Measure these first; several design branches depend on them.
