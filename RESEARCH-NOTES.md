# Research notes (appended as research threads finish)

Labels: [C] confirmed from a primary source or the source code; [I] inferred.

## PyTorch / ML on iOS — finished Nov 2, 2026

### Full eager `torch` on iOS
- [C] No iOS wheels on PyPI; the tags are macOS, manylinux and Windows only. https://pypi.org/simple/torch/
- [C] BeeWare's iOS index has numpy, pandas, pillow and others, but no torch. https://pypi.anaconda.org/beeware/simple/
- [C] Chaquopy's Android torch stopped at 1.8.1.
- [C] I found no Pyto, a-Shell or Pythonista torch. Code signing of every compiled module is cited as the blocker: https://omz-software.com/forum/archive/7038_understanding-the-workload-to-have-pytonista-can-support-torch-and-cv2.html
- [C] In pytorch main `CMakeLists.txt`, an IOS build sets `INTERN_BUILD_MOBILE`. That forces `BUILD_PYTHON OFF`, `NO_API ON`, and turns autograd off unless `BUILD_MOBILE_AUTOGRAD` is set. https://raw.githubusercontent.com/pytorch/pytorch/main/CMakeLists.txt
- [C] `scripts/build_ios.sh` and `cmake/iOS.cmake` exist at v2.8.0 and are gone at v2.9.0 and main.
- [C] PyTorch Mobile / LibTorch-Lite is in maintenance mode, and ExecuTorch is its successor. https://discuss.pytorch.org/t/pytorch-mobile-current-status/192040
- [C] MPS is macOS-only (it needs macOS 15+). https://docs.pytorch.org/docs/2.15/notes/mps.html
- [I] Eager torch needs no JIT: ATen is ahead-of-time compiled, and the TorchScript interpreter is just an interpreter. `torch.compile`/Inductor needs a C++ compiler, so it is out. A custom cross-build means patching around `INTERN_BUILD_MOBILE`: a multi-week effort, CPU only.

### ExecuTorch
- [C] Latest is v1.5.1 (23 Sep 2026). https://github.com/pytorch/executorch/releases
- [C] iOS 17+, SwiftPM/xcframework. https://docs.pytorch.org/executorch/stable/using-executorch-ios.html
  - Products: `executorch`, `executorch_llm`, `backend_xnnpack`, `backend_coreml`, `backend_mlx`, `kernels_optimized`, `kernels_quantized`, `kernels_torchao`.
- [C] The MPS delegate is deprecated (since 1.2). The Metal backend is experimental.
- [C] The MLX delegate is experimental: about 90 ATen ops, transformer-inference focused, dynamic shapes supported.
  - Blog: https://pytorch.org/blog/running-pytorch-models-on-apple-silicon-gpus-with-the-executorch-mlx-delegate/
  - Docs (shipped for iOS, but device runs aren't covered by CI): https://docs.pytorch.org/executorch/stable/backends/mlx/mlx-overview.html
- [C] Training is experimental. https://raw.githubusercontent.com/pytorch/executorch/main/extension/training/README.md
  - `_export_forward_backward` captures the joint graph; the loss must be the first output.
  - SGD and AdamW only. Delegate integration is work in progress, so training is CPU only.
  - On iOS it goes through the C++ `extension/training` API.
- [C] Python bindings: `executorch.extension.pybindings._C`, plus `extension.training`. They can run without torch (`EXECUTORCH_PYBINDINGS_TENSOR_OUTPUT=executorch`). [I] An iOS build of the bindings is realistic; none is published.
- [C/I] What `torch.export` can capture:
  - Static graphs; dynamic dims via `torch.export.Dim`.
  - A Python `if` on tensor values must become `torch.cond`.
  - Data-dependent shapes need unbacked symints or `torch._check`.

### torch → MLX translation
- [C] The ExecuTorch MLX delegate is the only maintained converter, inference only. https://raw.githubusercontent.com/pytorch/executorch/main/backends/mlx/README.md
- [C] SynapticSage/torch2mlx: early stage, about 80 ops. https://github.com/SynapticSage/torch2mlx
- [C] LucasSte/MlxTorch: an experimental fork; many ops unimplemented; not iOS. https://github.com/LucasSte/MlxTorch

### MLX on iOS
- [C] MLX Swift runs on iPhone. The official examples include MNIST training, LLMs and Stable Diffusion, and they use the increased-memory-limit entitlement. https://github.com/ml-explore/mlx-swift
- [C] mlx-c is the C API. https://github.com/ml-explore/mlx-c
- [C] No MLX Python iOS wheels. The issue was opened 2026-06-12; a maintainer says the build system "appears able". https://upd.dev/ml-explore/mlx/issues/3665
- [C] Latest MLX is v0.32.4.
- [C] `is_nax_available()` requires iOS/macOS 26.2+ and phone GPU generation ≥18. https://raw.githubusercontent.com/ml-explore/mlx/main/mlx/backend/metal/device.cpp
- [I] The A20 Pro gets NAX matmul paths.
- [C] Metal kernels are precompiled in the metallib. Runtime Metal source compilation (custom kernels, `mx.compile`) is allowed on iOS. CPU `compile` needs a C++ compiler, so it falls back.

### Core ML
- [C] coremltools 9.0 converts from `torch.jit.trace`, or from `torch.export` (beta, about 70% op coverage). https://apple.github.io/coremltools/docs-guides/source/model-exporting.html
- [C] `MLModel.compileModel(at:)` works on iOS 11+. https://developer.apple.com/documentation/coreml/mlmodel/compilemodel(at:)-6442s.md
  - Alternative: compile on the Mac with `xcrun coremlcompiler`.
- [C] On-device training only via `MLUpdateTask` on legacy updatable NeuralNetwork models. https://apple.github.io/coremltools/docs-guides/source/updatable-model-examples.html

## iOS execution model — finished Nov 2, 2026 (local session)

Device facts read from this Mac: the phone is iPhone19,2 on **iOS 27.2** (24B119), paired; Xcode 27.1 with the iPhoneOS 27.1 SDK.

### Loading code signed after install (decides HANDOFF §3's unknown)
- [C] LiveContainer does it on stock iOS 15–26.x and on iOS 27 since Sept/Oct 2026. It writes a dylib to its container, signs it on the device (zsign with the .p12 of the certificate that signed the host), checks it with `fcntl(F_ADDFILESIGS_RETURN)` / `F_CHECK_LV`, then `dlopen`s it. Requirements in its error strings: a development-signed host, and the same certificate and team ID. Free and paid certificates both work. https://github.com/LiveContainer/LiveContainer (`LCUtils.m`, `LCMachOUtils.m`)
- [C] InjectionNext signs with Mac `codesign` and `dlopen`s from tmp, and gets `code signature invalid (errno=1)` on iOS 26.3 (A15, TXM). Unresolved. https://github.com/johnno1962/InjectionNext/issues/81
- [C] Apple DTS: `dlopen` only supports libraries built into the OS or embedded in the app. https://developer.apple.com/forums/thread/773210
- [I] It probably works with the right signer. The device test must try Mac `codesign`, `rcodesign`, and on-device signing with the .p12, each with `F_ADDFILESIGS_RETURN` + `F_CHECK_LV` logging.

### JIT
- [C] On TXM devices JIT is possible only through a debugger handshake (`brk #0xf00d`, debugger maps RX). StikDebug-style, works on iOS 26/27. [I] Not idiomatic for numba/llvmlite; route JIT code to the Mac.

### Background execution
- [C] BGContinuedProcessingTask (iOS 26): must start from a user action in the foreground. A Live Activity shows progress and the user can cancel. CPU-heavy work is allowed. Progress must update roughly every 30 s or the task is marked stalled. No documented duration. The GPU entitlement isn't available on iPhone. https://developer.apple.com/documentation/backgroundtasks/performing-long-running-tasks-on-ios-and-ipados, https://developer.apple.com/forums/thread/805554, https://developer.apple.com/forums/thread/807957
- [C] Audio background mode keeps an app alive, but the CPU monitor kills background apps over about 80% CPU over 60 s (EXC_RESOURCE CPU_FATAL). So silent audio plus multi-core compute will be killed.
- [C] An attached debugger prevents suspension and disables most watchdogs (DTS). https://developer.apple.com/forums/thread/796528
- [C] `beginBackgroundTask` gives about 30 s in total. Use it only to checkpoint and tell the Mac.
- [I] Plan: while cabled, the Mac attaches a debugger (no suspension); otherwise run a BGContinuedProcessingTask started from a tap; otherwise pause/resume.

### Memory
- [C] `increased-memory-limit`: about 6144 MB measured on a 12 GB iPhone 17 Pro Max; `increased-debugging-memory-limit` adds about 0.5 GB (one forum report). Free accounts appear able to get increased-memory-limit, but not extended-virtual-addressing or the debugging limit. https://developer.apple.com/forums/thread/834805, https://developer.apple.com/help/account/reference/supported-capabilities-ios
- The default limit without the entitlement is unpublished; measure it.

### Processes and threads
- [C] No fork/spawn. [C] LiveContainer starts extra processes as app extensions through private NSExtension calls. [I] Worth testing later as a way to get more GILs (each costs an App ID).
- [I] The thread limit (1024–2560) is not a real constraint.

### USB transport
- [C] usbmux protocol confirmed (pymobiledevice3 `usbmux.py`). The pure-Python client in `src/pocketnode/usbmux.py` lists devices against this Mac's usbmuxd.
- [C] No published raw usbmux throughput figure. AFC through pymobiledevice3 measured about 85 MB/s down and 25 MB/s up. iPhone over USB 3 peaks at about 215 MB/s. **The cable in the box is USB 2 (480 Mb/s).**
- [C] Backburner reaches the app's own TCP listener directly over the USB network interface (CDC-NCM, link-local 169.254/16), not through usbmux: about 1 ms round trip. [I] Probably faster than usbmux; benchmark both.
- [C] `devicectl device process launch` (with `-e` env JSON, `--console`, `--start-stopped`), `devicectl device install app` and `devicectl device copy to --domain-type appDataContainer` all work headlessly.

### Free Apple ID signing
- [C] Free accounts: 7-day profiles, 10 App IDs per 7 days, 3 apps per device. They get Background Modes and App Groups. `xcodebuild -allowProvisioningUpdates -allowProvisioningDeviceRegistration` works once the account is in Xcode. https://developer.apple.com/support/compare-memberships/
- [I] Reinstalling over an existing app keeps its data container, so cached data survives a reinstall; only the process restarts.

## Python-on-iOS ecosystem — finished Nov 2, 2026

- [C] CPython iOS support: 3.13 (`iOS/`), 3.14 (`Apple/`), 3.15+ (`Platforms/Apple/`). Official python.org iOS XCframeworks start at **3.15**. BeeWare Python-Apple-support ships 3.13-b18 (3.13.18) and 3.14-b14. Layout: `Python.xcframework/{ios-arm64, ios-arm64_x86_64-simulator}`, plus `build/utils.sh` with `install_python`, which moves every `.so` into `Frameworks/<dotted.name>.framework` and leaves a `.fwork` pointer behind. https://docs.python.org/3.15/using/ios.html
- [C] **No free-threaded iOS build exists** (cibuildwheel: "Free threading builds aren't available for iOS (yet)").
- [C] **iOS wheels exist only for numpy, pillow and cryptography.** BeeWare has numpy 2.5.2.post1, 2.5.0.post1 and 2.3.5.post1 for cp313/cp314 (2.5.x also cp315), and pandas only 1.5.3 (≤cp312). Nothing on PyPI or BeeWare for scipy, scikit-learn, pyarrow, polars, statsmodels, lightgbm, xgboost, msgpack, psutil, numexpr or bottleneck.
- [C] The BeeWare numpy uses Accelerate. The `ios_17_0` wheel uses NEWLAPACK ILP64 symbols and is the one iOS 27 picks.
- [C] scipy 1.18 is almost Fortran-free (6 ODRPACK files; experimental `_without-fortran`); main has none. scipy 1.16 (the Mac's version) still has much more Fortran.
- [C] cibuildwheel 4.3.5 builds `cp313/314/315-ios_*`, runs tests in the simulator testbed, and rejects uv as the build frontend. maturin has initial iOS support.
- [C] rubicon-objc 0.5.7 works on iOS.
- [C] Briefcase notes that stray `.a` files in wheels must be removed.

## Retagging macOS binaries, hardware, prior art — finished Nov 2, 2026

### Retagging (HANDOFF §5 option 2)
- [C] iOS dyld refuses a macOS-platform Mach-O: `incompatible platform (have 'macOS', need 'iOS')` (dyld-1378 `UnsafeHeader.cpp`). dyld does **not** enforce `minos`.
- [C] A plain-arm64 iOS process accepts arm64 and arm64e dylibs. **The host app must stay plain arm64** (no arm64e / Enhanced Security), because macOS wheels are arm64.
- [C] Pipeline, tested on numpy and the 216 MB `libtorch_cpu.dylib`:
  1. `install_name_tool -change` system framework paths from `X.framework/Versions/A/X` to `X.framework/X`.
  2. `xcrun vtool -set-build-version ios 17.0 27.1 -replace`.
  3. Re-sign.
  4. Rename `cpython-313-darwin.so` to `cpython-313-iphoneos.so`.
- [C] Static check against the iOS 27.1 SDK `.tbd` stubs (a prototype of what became `provision/symcheck.py`): 981 of 1,047 Mach-O files and 62 of 88 packages in the Mac's global env are clean.
  - Clean: **numpy, pandas, scikit-learn, statsmodels, torch, skimage, duckdb, pydantic_core, tokenizers, lxml, cryptography, mlx, numba, llvmlite**.
  - Failures:
    - scipy: its bundled `libgcc_s` imports `pthread_jit_write_protect_np`, and `cython_blas` imports `dcabs1_` from Accelerate.
    - pyarrow: Keychain APIs.
    - polars: AppKit.
    - cffi: `/usr/lib/libffi.dylib`.
- [C] macOS extensions link with `-undefined dynamic_lookup`. dyld's flat lookup searches every loaded image, so `Py*` symbols resolve against the embedded Python.framework if the minor version matches.
- [C] Known Python-level breakage on iOS: torch's `_load_global_deps` picks `.so` vs `.dylib` from `platform.system()=="Darwin"`; psutil raises on non-darwin.
- [I] MLX's metallib is built for macOS and probably needs an iOS one.

### Hardware

| | iPhone 18 Pro (A20 Pro) | MacBook Air M4 |
|---|---|---|
| CPU | 2 super + 4 efficiency cores, ~4.93 GHz | 4P + 6E |
| GB6 single / multi | ~4,720 / ~12,700 | ~3,750 / ~15,000 |
| GPU cores | 7 | 10 |
| GB6 Metal | ~64,000 | ~54,000 |
| Neural Engine | 32-core | 16-core |
| Memory bandwidth | ~115 GB/s [I] | 120 GB/s |

- 3DMark stress stability: 18 Pro 62.8% vs 18 Pro Max 79.4%.
- USB 3 at 10 Gb/s only with a USB 3 cable.

### Prior art
- [C] Backburner (iPhone + Mac LLM split): raw TCP over the USB network interface (link-local), Noise tunnel over Wi-Fi, Guided Access to stay in the foreground, increased-memory-limit (~6 GB), AltStore 7-day refresh.
- [C] TVM `ios_rpc`: new modules can't be uploaded without a custom dyld that needs JIT (debugger), so they were bundled at build time.
- [C] exo: the $1,000 bounty for the Python node on iOS was closed unsolved (Pyto dependency problems; BeeWare judged buggy).


## Local session results — Nov 2, 2026 (Mac + iOS 27.1 simulator; device runs pending)

[M] = measured or verified here.

### Retagging works, and replaces most cross-compiling
- [M] With the pipeline below, the Mac's own installed binaries load and run in the iOS 27.1 simulator. Results match the Mac.

  | Package | What was checked |
  |---|---|
  | numpy 2.3.4 | Accelerate BLAS; SVD |
  | scipy 1.16.3 | Fortran-backed modules: odeint, L-BFGS-B, ARPACK |
  | scikit-learn 1.8.0 | random forest, logistic regression, GridSearchCV |
  | pandas 2.3.3 | groupby, timezones |
  | statsmodels 0.14.6 | OLS |
  | pyarrow 22 | Parquet round trip |
  | polars 1.40 | eager and lazy |
  | torch 2.11.0 | eager autograd, Adam, Conv2d; loss equals the Mac's to ~1e-8 |

  The full eager PyTorch port (HANDOFF §8) is therefore no longer blue sky.
- [M] Pipeline:
  1. Thin to arm64.
  2. `install_name_tool`: shallow framework paths, rewrite bundled-dylib references to `@rpath/<name>.framework`, add rpath `@executable_path/Frameworks`.
  3. `vtool -set-build-version ios|iossim 17.0 <sdk>`.
  4. Two binary patches, needed because iOS dyld is stricter than macOS for old binaries:
     - zero the file offset of zero-fill sections (`__thread_bss`): error "zero-fill section type, but offset field is not zero";
     - pad the LC_SYMTAB string pool to 8-byte alignment: error "mis-aligned LINKEDIT string pool" (sklearn's libomp).
  5. Wrap every Mach-O as `Frameworks/<dotted>.framework` with a `.fwork` pointer.
  6. Xcode signs.
- [M] Symbols iOS lacks are handled per kind:
  - scipy `libgcc_s` → `pthread_jit_write_protect_np`: shim (no-op).
  - `cython_blas` → `dcabs1_`: shim (implemented).
  - pyarrow/polars Security keychain functions: auto-stubbed to `errSecUnimplemented`, because the macOS headers declare them as returning OSStatus.
  - Frameworks iOS lacks entirely (polars → AppKit `NSPasteboardTypeString`): a generated stand-in defining exactly the imported symbols. Weak-linking the dylib alone is not enough: dyld still demanded the individual symbol.
- [M] Python-level gaps on iOS, fixed in `compat.py`:
  - no `_multiprocessing` or `_posixshmem`: an in-process SemLock built on thread semaphores, so multiprocessing's thread pools, locks and queues work;
  - joblib's default backend → threading at the real core count;
  - torch ctypes-loads its relocated dylibs: redirected through a map written at staging;
  - no USER/LOGNAME env (`getpass`);
  - threadpoolctl goes down the Linux path on `ios`.
- [M] Executables (`torch_shm_manager`) are replaced by placeholders. They can't run on iOS anyway.
- [M] The iOS 27 SDK requires the UIScene lifecycle, otherwise the app is refused at launch. Its CPython 3.13 `PyConfig` has no `use_system_logger`.
- [M] Static check over the Mac's 280-package environment: most compiled packages are clean or fixable.
  - Truly blocked, because they link plain dylibs iOS lacks: pysam and pyBigWig (libcurl), cffi's backend (libffi).
  - Partially blocked: pyproj and rasterio (libcurl modules only), pyobjc's AppKit parts.

### Consequences for the design
- Provisioning order changes from HANDOFF §5:
  1. **Retag the Mac's own build.** This guarantees identical versions, so the version check always passes.
  2. A published iOS wheel.
  3. Cross-compile.
  4. Mac-only.

  A published wheel is now second because only numpy, pillow and cryptography have one, at versions different from the Mac's.
- The router's version policy (`minor` by default) matters only for packages that weren't retagged.

### Plumbing measured in the simulator (not phone numbers)
- [M] The simulator shares the Mac's network stack and CPU. The link reaches 4.3 GB/s with a 0.23 ms round trip, so throughput and compute figures must come from the device.
- [M] GridSearchCV (24 fits × 3 folds) through `pn.parallel()` with 4 Mac workers plus the simulator app: identical best params and scores, 33.8 s → 7.5 s.

### Still to settle on the device
`pocketnode experiments` runs each of these:
- post-install `dlopen` (adhoc vs team signature);
- usbmux throughput with the cable;
- background survival after switching apps;
- the `os_proc_available_memory` ceiling with increased-memory-limit;
- sustained thermal behaviour;
- device dyld accepting retagged binaries (expected, since the simulator applies the same loader checks, but signature enforcement is device-only).

### Later in the same session
- [M] Parallel pure Python without processes: Python 3.13's isolated subinterpreters (`_interpreters`, each with its own GIL) exist in the iOS build. On the stand-in, six pure-Python tasks take 3.1× the time of one, i.e. they run in parallel, and the same holds in the iOS simulator. numpy can't load in subinterpreters, so tasks that use it stay in the main interpreter on threads (BLAS releases the GIL). In 3.13 the low-level queue module needs the test-only `test.support.interpreters`, and `exec`/`call` can't return values, so results come back over a pipe.
- [M] MLX:
  - The macOS metallib can't serve iOS, so the iOS metallib is compiled from the v0.31.1 kernel sources with the Metal toolchain (a separate 839 MB Xcode component), NAX kernels included: 125 MB.
  - `libmlx` imports `MTLCopyAllDevicesWithObserver` and `MTLRemoveDeviceObserver`, which iOS lacks; they're shimmed with the single-GPU meaning.
  - In the simulator, MLX segfaults because the virtual GPU has no `architecture.name`. GPU numbers wait for the device.
- [M] A bug in the static check: with chained-fixup binaries, `dyld_info` prints an index before each import (`0x0014 _sym (from Lib)`), which the original regex skipped. Those binaries passed unchecked until this was fixed.
- [M] Verification of each function's first phone result against the Mac (tolerance-aware, with a second Mac run to tell nondeterminism from real differences) is on by default for automatically placed work.

## Device results — Nov 24, 2026 (iPhone 18 Pro, iOS 27.2.1, free Apple ID, Xcode 27.1)

[M] = measured on the phone in this session. The Mac was busy with other jobs all session, so Mac-side timings from today are not a fair reference and are left out.

### Getting onto the device
- [M] A free personal team works end to end: `xcodebuild -allowProvisioningUpdates -allowProvisioningDeviceRegistration` creates the certificate, registers the phone and issues the profile, but only when the destination names the phone (`id=<udid>`); with a generic destination it stops at "Your team has no devices".
- [M] The team ID isn't visible in Xcode's UI or to the command line until Xcode has loaded the account's teams once (Settings › Apple Accounts › click the account). After that it is in Xcode's preferences (`IDEProvisioningTeamByIdentifier`), which `pocketnode app install` reads.
- [M] Developer Mode can't be armed from the Mac when the phone has a passcode (`devmodectl`: "Device has a passcode set"). It is a one-time toggle on the phone. The first launch also needs the developer trusted once in Settings › General › VPN & Device Management.
- [M] `com.apple.developer.kernel.increased-memory-limit` is granted to a free account. The app sees about 6.1 GB available (`os_proc_available_memory`) on the 12 GB phone, and a probe allocated 5.6 GB before stopping at its safety margin.
- [M] `devicectl device process launch` fails while the phone is locked. The app keeps the phone awake while it runs, but after a crash the phone locks itself and the Mac can't relaunch the app until it's unlocked.
- [M] usbmux reaches a listener bound to loopback only, so the app doesn't need to listen on any network interface for USB.
- [M] Build and install times: first signed build 279 s (signing 328 frameworks one at a time), install 41 s. With per-framework stamps and parallel signing a rebuild that only changes pocketnode itself takes 53–130 s and the install about 30 s.

### Retagged macOS binaries on the device
- [M] The Mac's own arm64 builds load and run on the phone after retagging, with results matching the Mac: numpy 2.3.4, pandas 2.3.3, scipy 1.16.3, scikit-learn 1.8.0, torch 2.11.0 (autograd, Adam), pyarrow 22, polars 1.40. The simulator had predicted this; device code-signing enforcement didn't change it.
- [M] One device-only failure: torch's Metal backend calls `-[MTLDevice isLowPower]` while choosing a GPU. That selector exists on macOS only, and on the phone the unrecognized selector aborts the app the first time autograd runs. (The simulator's Metal device answers it, so it never showed there.) The host app now adds `isLowPower`, `isHeadless` and `isRemovable` to the device class at startup.
- [M] torch's MPS backend is compiled in (`mps.is_built()` is true). It reports unavailable because the app answers "low power"; whether it works on the phone GPU when allowed is untested.

### Code signed after install loads (HANDOFF §3's decisive unknown)
- [M] A dylib written to the app's data container loads with `dlopen` if it is signed with the app's development certificate **and its signing identifier equals the app's bundle identifier**:
  `codesign -f -s "Apple Development: …" --identifier dev.pocketnode.app --timestamp=none lib.dylib`
- [M] What doesn't work: ad-hoc signing; the same certificate with the default identifier (the file name), with any other identifier (including the bundle ID plus a suffix), with SHA-1+SHA-256 digests, with the hardened-runtime flag, or with the app's entitlements attached. Entitlements make no difference either way. The error is always `code signature invalid … (errno=1)`. This is very likely what InjectionNext hit on iOS 26.3.
- [M] No on-device signing and no copy of the private key on the phone are needed (LiveContainer's approach), and no debugger.
- [M] A real package confirmed it: kiwisolver's C++ extension, retagged in place, signed that way, unzipped into `Library/Application Support/pocketnode/site` and imported as a plain `.so`. iOS CPython's import system accepts `.cpython-313-iphoneos.so`, `.abi3.so` and `.so` outside frameworks; wrapping extensions as frameworks is an App Store rule, not a loader requirement.
- Consequence: adding a compiled package doesn't need a rebuild or a restart. Packages added this way keep their normal layout, so the libraries they bundle keep finding each other.
- [M] Built and run on the phone (`pocketnode packages add`, and automatically when a task needs a missing package):
  - duckdb 1.5.0: 38 MB, 9.5 s in total, of which 1.5 s was sending. `select 6*7` then ran on the phone.
  - matplotlib 3.10.7 with pillow, fonttools and contourpy: 37 MB, 48 s in total (3.2 s sending; the rest was the static check, an AppKit stand-in for the macOS backend, and signing, on a busy Mac).
  - regex, added automatically: the first six tasks ran on the Mac at once, the package was on the phone 4.3 s later, and 25 of the next 40 tasks ran on the phone.
  - lxml also loads. Each result matched the Mac's.
- [M] The app's data container path changed between two installs of the app in this session, and packages added before still imported afterwards, so added files should never record absolute paths.

### Subinterpreters and PyTorch
- [M] Creating any subinterpreter makes `PyGILState_Check()` return true for the rest of the process (CPython sets `gilstate.check_enabled = 0`). PyTorch's autograd engine asserts that it is *not* true, so every `backward()` afterwards fails with "The autograd engine was called while holding the GIL".
- [M] Fix: subinterpreter work runs on its own threads that never do anything else, and the host app turns the check back on (it is accurate on every other thread). torch autograd and subinterpreter tasks then coexist.
- [M] numpy must never be imported in a subinterpreter. It doesn't refuse with ImportError: it raises `RuntimeError: CPU dispatcher tracer already initlized`, and after that numpy fails to import in the main interpreter too until the app restarts. Subinterpreters now refuse every third-party extension module before its loader runs, and such tasks go to the main interpreter.
- [M] Tasks in subinterpreters can be cancelled: a watcher thread inside the subinterpreter injects an exception into the task's thread (ctypes works there). Isolated subinterpreters refuse daemon threads, so the watcher is woken and joined at the end of every task.
- [M] Each new subinterpreter spent about 0.45 s importing pocketnode. The package now imports lazily (0.05 s) and the pool is created ahead of the first task.
- [M] On the phone (2 performance + 4 efficiency cores), six 20M-iteration pure-Python loops finish in 1.15 s against 0.59 s for one: 3.1× the throughput of a single core.

### Speed (phone only)
- [M] Pure Python: 25–36 M simple loop iterations per second on one core.
- [M] `float32` matrix multiply through Accelerate (numpy): about 2,000 GFLOPS; `float64`: about 500 GFLOPS. These are far above what NEON cores deliver, so Accelerate is using the chip's matrix extension. Limiting BLAS to one thread doesn't change the figure.
- [M] scikit-learn random forest (5,000 × 40, 100 trees, `n_jobs=-1`): 0.26 s. torch MLP training step (256 × 512 → 1024 → 10): 0.55–0.65 ms.
- [M] USB with the cable in use (480 Mb/s): 34 MB/s Mac to phone, round-trip latency 1.1–1.3 ms median. A 320 MB array took 9.0 s the first time and 0.05 s from the phone's cache after that.

### Leaving the app
- [M] Without a background task: the app is reported in the background within 2 s and throughput drops at once (923 → 53 GFLOPS on the same load). After about 33 s it is suspended. When the app comes back to the front, the same tasks continue and the USB connection is still open; nothing has to be re-sent.
- [M] With a continued-processing task (`BGContinuedProcessingTask`) the app is not suspended: it kept computing for the whole 100 s it was in the background, at 108 GFLOPS against 1,649 in front (about 1/15). The request can be submitted by the app itself with no tap, as long as the app is in front at that moment, so the worker asks when work arrives and gives it back after 30 idle seconds.
- [M] The handoff's "Keep computing in the background" button crashed the app: it submitted a continued-processing request whose handler had been registered for the wildcard identifier, which iOS never matches (`_handleSubmissionWithoutRegistrationForTaskRequest` throws). Handlers have to be registered for the exact identifier, just before submitting.

### GPU on the phone
- [M] MLX 0.31.1 works on the phone GPU. Added to the running app (150 MB with the iOS metallib; 139 s in total on a busy Mac, 10 s of it sending). `mx.metal.is_available()` is true, the device is "Apple A20 Pro GPU" (`applegpu_g19p`), recommended working set 8.6 GB. A 256×256 matmul matches numpy (max abs error 1.8e-4 in float32), and a small MLP trained with Adam goes from loss 28.5 to 0.0003. Half-precision matmul: 6.2 TFLOPS at 1024², 10.4 TFLOPS at 4096², so the neural-accelerator matmul kernels are in use.
- [M] The two macOS-only Metal calls MLX imports (`MTLCopyAllDevicesWithObserver`, `MTLRemoveDeviceObserver`) work through the generated shim on the device.
- [M] torch MPS works on the phone GPU. Three things were in the way:
  1. `-[MTLDevice isLowPower]` doesn't exist on iOS (the abort described above), and torch also requires `[device supportsFamily:MTLGPUFamilyMac2]`. The host app answers both for the phone's GPU.
  2. torch finds its own Metal shaders by walking the loaded images for a path containing `/libtorch_cpu.dylib` and reading that image's `__TEXT,metal_basic` section; if none matches it reads the main executable instead and fails with "Can't find metal library section metal_basic". In the app bundle the library is therefore wrapped as `libtorch_cpu.dylib.framework/libtorch_cpu.dylib`, the one framework whose name isn't derived from its module path.
  3. That section holds a metallib built for macOS (32.3 MB). The 33 shader files (`aten/src/ATen/native/mps` and `native/sparse/mps` at the matching tag, with `c10/metal`) are compiled for iOS the way torch's own build does (`-std=metal3.1`), linked, and written over the section in place. The section can't grow: other sections follow it in `__TEXT`. Built for iOS 17 the library is 258 KB too big with the current Metal compiler, so it is built for iOS 26 and compressed (`metal-strip --compress-sections=MODULE_LIST`): 30.7 MB, all 5,867 functions.
  - Before the shaders were replaced, 13 of 15 test cases failed; only cross-entropy and an embedding+LSTM pass, which go through MPSGraph alone, worked.
  - After: all 15 agree with the phone's CPU, relative error between 0 and 1e-6: elementwise maths, activations, reductions, cross-entropy, indexing and gather, sort/topk/cumsum, Cholesky and inverse, conv + batch norm + pooling, layer norm + multi-head attention, bilinear upsampling and grid_sample, embedding + LSTM, and training with SGD, Adam and AdamW plus a small conv net.
  - torch's Metal allocator takes its ceiling from the GPU's recommended working set (8.6 GB), above the app's memory allowance (about 6 GB), so the worker sets `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.5`. The low mark has to be set below it too, or torch refuses to allocate ("invalid low watermark ratio 1.4").
  - Not measured: speed against the phone's CPU on large models. The test cases are small, where the CPU wins.

### Wi-Fi
- [M] With the app built with `wifi` on, the phone accepts the Mac at its LAN address: tasks give correct results, 16 MB transferred in 0.7–1.2 s, round trip 20 ms median (USB: about 1 ms). iOS shows its local-network permission prompt the first time.

### Acceptance check and a listener fault
- [M] `pocketnode check` on the phone with the final build: 29 passed, 0 failed, 0 skipped (libraries against the Mac's results, guards, caching and pinning, placement overrides, verification, GridSearchCV split 10 phone / 4 Mac tasks, pandas accessor, Dask, the `%%phone` magic, torch and MLX on the GPU, two sessions sharing the phone).
- [M] Once during the session the app was running but refused every connection until it was restarted. The listener loop called `accept()` with no error handling, and a client that connects and resets immediately makes `accept()` fail (the Wi-Fi test had just ended that way), which would end the loop for good. Not reproduced on purpose. The loop now survives accept errors, the app restarts its listener if it ever returns, and the Mac starts the app afresh when two launches haven't made it answer.

### Not done on the device
- Sustained heat: the 10-minute load run was skipped to free the phone. The scheduler reacts to the reported thermal state, but how quickly the phone throttles is unmeasured.
- A USB 3 cable. All USB numbers above are from a 480 Mb/s cable.
