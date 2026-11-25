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

## App Store edition: feasibility — Nov 24, 2026

Question: can pocketnode be an app the App Store would accept? Rules were read from Apple's pages on this date, not from memory. Acceptance is only certain after Apple reviews a submission, and none was made (it needs a paid Apple Developer Program membership). "Possible" below means: no rule or technical fact was found that blocks it, and comparable apps are on the store now.

**Verdict: possible, as a reduced edition.** The store edition keeps the worker, the protocol and the Mac side. It loses everything that depends on native code the Mac prepares: running the Mac's own compiled packages, adding compiled packages after install, PyTorch (no iOS build of the Python package exists), and with it torch on the GPU. Compiled packages become a fixed set built for iOS and shipped in the reviewed bundle. The largest remaining risk is review of the core idea (code arriving from a computer), which Apple enforced more strictly in 2026.

### The rules
- [C] App Review Guideline 2.5.2: apps "may not ... download, install, or execute code which introduces or changes features or functionality of the app, including other apps." The exception is for "educational apps designed to teach, develop, or allow students to test executable code", which "must make the source code provided by the app completely viewable and editable by the user." https://developer.apple.com/app-store/review/guidelines/
- [C] Developer Program License Agreement 3.3.1(B): "an Application may not download or install executable code. Interpreted code may be downloaded to an Application but only so long as such code: (a) does not change the primary purpose of the Application by providing features or functionality that are inconsistent with the intended and advertised purpose of the Application (b) does not bypass signing, sandbox, or other security features of the OS; and (c) for Applications distributed on the App Store, does not create a store or storefront for other Applications." The clause was last revised on Oct 8, 2025. https://developer.apple.com/support/terms/apple-developer-program-license-agreement/ and https://developer.apple.com/news/?id=fnkpd51y
- [C] 3.3.1(A) and guideline 2.5.1: public (documented) APIs only.
- [C] 2.4.2: apps "should not rapidly drain battery, generate excessive heat, or put unnecessary strain on device resources", and may not run "unrelated background processes, such as cryptocurrency mining". 2.5.4: background services only for their intended purposes. 4.2.3(i): "Your app should work on its own without requiring installation of another app to function." 2.1 and "Before You Submit": reviewers need full access, including "any other hardware or resources that might be needed to review your app"; a built-in demo mode is the stated alternative to a demo account. 5.1.1(i): a privacy policy linked in the app and in the store listing.
- [C] Uploads must be "built with Xcode 26 or later using an SDK for iOS 26" (since April 28, 2026) and must declare approved reasons for required-reason APIs (since May 1, 2024). https://developer.apple.com/news/upcoming-requirements/
- [C] Required-reason API categories that CPython touches: file timestamps (`stat`, `fstat`, `lstat`, `getattrlist`; reason C617.1 for files in the app's container), system boot time (`mach_absolute_time`; 35F9.1, elapsed time may leave the device, boot time may not), disk space (`statfs`, `statvfs`; E174.1, "may not be sent off-device"). "For each executable or dynamic library in an app that uses a required reason API, the bundle that includes the executable or dynamic library needs to include a privacy manifest file." https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api
- [C] OpenSSL is on Apple's list of commonly used third-party SDKs that need a privacy manifest (and a signature when used as a binary dependency). The embedded Python links OpenSSL 3.5.9 statically into `_ssl` and `_hashlib`. https://developer.apple.com/support/third-party-SDK-requirements/
- [C] Export compliance: `ITSAppUsesNonExemptEncryption` is `NO` only if the app "including any third-party libraries it links against" uses no encryption or only exempt forms. Encryption "limited to that within the Apple operating system" needs no documentation; "an industry standard algorithm, not provided within the Apple operating system" needs a French encryption declaration if the app is sold in France. https://developer.apple.com/documentation/security/complying-with-encryption-export-regulations and https://developer.apple.com/help/app-store-connect/reference/export-compliance-documentation-for-encryption/
- [C] Continued processing: "your app needs to run the task only in response to someone's action, such as tapping a button." Progress must be reported; the system shows a Live Activity where the person can cancel. https://developer.apple.com/documentation/backgroundtasks/performing-long-running-tasks-on-ios-and-ipados
- [C] Local network privacy (TN3179): listening for and accepting incoming TCP connections does not need local-network permission; outgoing connections and Bonjour do. https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy
- [C] `com.apple.developer.kernel.increased-memory-limit` is an ordinary capability ("Increased Memory Limit") with no stated distribution restriction. https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.kernel.increased-memory-limit
- [C] CPython's own iOS documentation: all binary modules must be dynamic libraries in frameworks in `Frameworks/`, one binary per framework, with `.fwork`/`.origin` markers, and the standard library is patched because Apple's "automated validation rules" reject some of its code. https://docs.python.org/3.13/using/ios.html

### Enforcement in 2026
- [C] March 2026: Apple blocked updates to Replit and Vibecode and removed Anything under 2.5.2, for running generated software inside the app. Apple's position as reported: no rule against such apps as a category, but they have to follow the longstanding guidelines; Replit was expected to pass once generated apps opened in an external browser. Replit said in May that the matter was resolved after four months without updates. https://www.macrumors.com/2026/03/18/apple-blocks-updates-for-vibe-coding-apps/ and https://macrumors.com/2026/03/30/apple-pulls-vibe-coding-app/
- [C] Expo Go, the closest structural match to pocketnode (a store app that runs JavaScript pushed from the developer's computer), has had no update approved since SDK 54. On May 4, 2026 Expo wrote that the SDK 55 build "is still waiting for approval" with no timeline, and called Expo Go "first and foremost an educational tool". Its documentation now says the App Store build "stops at SDK 54". No reason from Apple is public. https://expo.dev/changelog/expo-go-and-app-store-may-2026 and https://docs.expo.dev/troubleshooting/expo-go-version-mismatch
- [C] In the same period, apps where the person can see and edit the code kept being approved, including ones driven from a computer:
  - Codea 3.20 (Nov 11, 2026). Its "Air Code" feature ("code live from your PC using WiFi") lets VS Code on a computer connect to the app by address and port, or over USB through usbmuxd, and edit and run code there. 3.17 (May 2026) added a built-in MCP server for coding agents. https://apps.apple.com/us/app/codea/id439571171 and https://twolivesleft.github.io/codea-air-code/
  - a-Shell 2.4.0 (Nov 2026): Python, pip, a C compiler targeting WebAssembly, ssh and curl. It and iSH were told in Nov 2020 they broke 2.5.2, and Apple withdrew that for iSH. https://apps.apple.com/us/app/a-shell/id1473805438 and https://ish.app/app-store-removal
  - Juno 4.4.1 (Oct 27, 2026): Python 3.13 with NumPy 2.4.6, pandas 3.0, SciPy 1.18, scikit-learn 1.9, Polars, DuckDB, Matplotlib, XGBoost, LightGBM, MLX and more, and it installs "any pure Python package". https://apps.apple.com/us/app/juno-python-and-jupyter/id1462586500
  - Pyto and Pythonista (not updated since 2024 and 2023) bundle numpy, pandas, scipy, scikit-learn and matplotlib. https://apps.apple.com/us/app/pyto-python-3/id1436650069
- [I] The line Apple drew in 2026 is between apps that present downloaded software as new apps or features, and programming tools whose user supplies, sees and controls the code. A store edition of pocketnode has to sit clearly on the second side.

### Retagged macOS binaries cannot ship
- [M] In the sideloaded build (`Debug-iphoneos`), 322 of the 397 frameworks are retagged macOS binaries and none of them has an `LC_ENCRYPTION_INFO_64` load command. The 75 built for iOS (Python and its extension modules) all have one. Apple's linker adds it only when it links for iOS, and a linked binary can't be relinked.
- [C] App Store Connect rejects such binaries at upload: ITMS-90125, "The binary is invalid. The encryption info in the LC_ENCRYPTION_INFO load command is either missing or invalid, or the binary is already encrypted. This binary does not seem to have been built with Apple's linker." https://developer.apple.com/forums/thread/730748
- [I] Passing would need a forged encryption load command on top of the rewritten platform and SDK fields, for binaries that were not built with the iOS SDK (which the upload rule requires) and that reference macOS-only selectors and frameworks through stand-ins. That is disguising binaries from the validator. It is not defensible and the store edition does not do it.
- [C] Adding compiled packages after install is impossible on two counts. The rules forbid downloading executable code (3.3.1(B), 2.5.2). And the mechanism needs the app's development certificate on the Mac; a store app is signed by Apple's distribution chain and the user has no such certificate.

### What real iOS builds exist (checked against the package indexes on this date)
- [M] cp313 iOS wheels: numpy 2.3.5, 2.5.0 and 2.5.2 (BeeWare's index), pillow 12–13, kiwisolver 1.5.1, markupsafe 3.0.4 and cffi 2.1 (PyPI), contourpy 1.3.3, cryptography 47, bcrypt and lru-dict (BeeWare). Nothing for pandas (BeeWare stops at 1.5.3 for cp312), scipy, scikit-learn, pyarrow, polars, duckdb, matplotlib, lxml, regex, torch or mlx. https://pypi.org/simple/ and https://pypi.anaconda.org/beeware/simple/
- [I] Juno shows that pandas, scipy 1.18, scikit-learn, polars, duckdb and MLX can be built for iOS on Python 3.13 and pass review. Its builds aren't published, so each is a cross-compile to do here: pandas (C and Cython, Meson) is the easy one and was done (below); scipy needs 1.18 or later (earlier versions need a Fortran compiler for iOS) linked against Accelerate; scikit-learn follows scipy; polars is a Rust cross-build; MLX needs its Python bindings built for iOS plus the iOS metallib this project already compiles. PyTorch has no iOS Python build anywhere and its build system turns the Python module off for iOS; that stays a multi-week port.

### Each capability
| Capability | Ruling | Notes |
|---|---|---|
| Run arbitrary Python sent by the user's own Mac | Allowed by the letter of 3.3.1(B); review outcome unknowable without submission | Interpreted code, consistent with the advertised purpose, no storefront, no sandbox bypass. The 2026 enforcement makes this the main risk. The app must show what is running, including its source, let the person stop it, and work as a small Python environment by itself. |
| Pure-Python packages fetched from the Mac at run time | Allowed | Interpreted code; Juno, Pyto and a-Shell install pure-Python packages from the network. |
| Compiled packages bundled at build time | Allowed, if built for iOS | As frameworks, one binary each (CPython's documented layout). |
| Compiled packages added after install | Not allowed | Rule and mechanism both fail (above). |
| Retagged macOS binaries | Not allowed | Fail upload validation (ITMS-90125) and the SDK requirement. |
| Stand-in and shim libraries for missing symbols | Not needed, not shipped | They exist only for retagged binaries. Stand-ins named after Apple frameworks would also invite a 2.5.1 question. |
| Methods added to Apple's Metal device class, patched shader section | Not shipped | Only torch needs them, and torch isn't in the store edition. |
| The CPython-internal poke in `pn_core.c` | Not shipped | It touches the app's own bundled interpreter, not an Apple API, but only torch needs it. |
| Subinterpreters, threads, the guards | Allowed | Ordinary use of the bundled interpreter. |
| Background computing (`BGContinuedProcessingTask`) | Allowed with a change | Only started by a tap on the phone, never by work arriving. |
| Keeping the screen awake | Allowed | `idleTimerDisabled` is public API; limited to while a Mac is connected, with a visible switch. |
| USB through usbmuxd to a loopback listener | Allowed | No entitlement; the pattern (PeerTalk) ships in Duet Display and is how Codea's Air Code works over USB. https://github.com/rsms/peertalk |
| Wi-Fi | Allowed | Off by default, a switch on the phone, usage string present. |
| Pairing token baked into the build | Must change | A store binary is the same for everyone. Replaced by pairing at run time (below). |
| `increased-memory-limit` | Allowed | A normal capability. The paid account's App ID has to enable it. |
| Mac-side companion from PyPI | Allowed, with a demo mode | Camo and Duet Display need desktop software from outside the App Store. 4.2.3(i) and reviewability are answered by the app doing something useful alone. https://apps.apple.com/us/app/camo-webcam-for-mac-and-pc/id1514199064 |
| TLS for tasks (bundled OpenSSL) | Owner's choice | Keeping it means declaring standard encryption (a French declaration if sold in France) and the OpenSSL privacy manifest. Dropping `_ssl` and `_hashlib` leaves hashing and HMAC only. |

### Pairing design for the store edition
- The phone holds one secret per paired Mac. Nothing is baked into the build.
- Pairing only happens over the cable (the listener only accepts a pairing request from a loopback peer, which is what a usbmuxd connection is), because the link isn't encrypted and the new secret crosses it once. Wi-Fi sessions afterwards prove knowledge of the secret without sending it, as today.
- Steps: the person taps "Pair a Mac" on the phone, which shows a six-digit code for two minutes; `pocketnode pair` on the Mac asks for the code and proves it knows it; the phone then asks "Allow <Mac name> to run Python code on this iPhone?"; on Allow it issues that Mac a random 256-bit secret. Five wrong codes end the attempt.
- The phone lists paired Macs and can forget any of them, shows who is connected and what is running, and has one switch that stops everything and refuses connections.
- What review will expect, and gets: a connection the person starts, state that is always visible, and a way to stop.

### Interaction model: how much the phone has to ask
The sideloaded app runs whatever its Mac sends, silently. That is the one thing the store edition can't keep as it is. Four designs were compared against the rule text and the 2026 cases.

- What the text requires. 3.3.1(B) puts no consent or display condition on interpreted code; all four designs meet it equally. 2.5.2's main sentence asks whether the code "introduces or changes features or functionality of the app"; no interaction design changes the answer, it only changes how plainly the app reads as a tool that runs its user's own code. Only the educational exception asks for source that is "completely viewable and editable", and pocketnode isn't an educational app (Expo repositioned Expo Go that way and its updates stayed unapproved).
- What the cases show. Approved in 2026: Codea runs code pushed from a computer at once, with no prompt per change, and that code is readable in the app; a-Shell and Juno run scripts and downloaded pure-Python packages without prompts, and the files are readable on the device. Not approved: apps that ran software the person couldn't see as code (generated apps in Replit, Vibecode and Anything; Expo Go's bundles). The dividing property is visibility and ownership of the code on the device, not a tap per run.

| Design | Meets the text | Fits the approved cases | Cost to the product |
|---|---|---|---|
| Session consent only (pair, running indicator, stop), code not shown | Yes | No: this is Expo Go's shape | None |
| Session consent, plus everything that runs is shown on the phone with its source | Yes | Yes: Codea's shape, minus editing | None at run time |
| The same, plus approval of each new piece of the user's own code before it runs | Yes | Yes, with the most margin | One tap per new function; a `%%phone` cell is new code every time it is edited |
| Source of everything editable on the phone | Yes | Only needed for the educational exception | Editing a function on the phone would make its result differ from the Mac's, which the project forbids |
| A tap on the phone for every run | Yes | Not asked for by any rule or case | Ends the product: an executor can't ask per task |

- Chosen: session consent with full visibility. Pairing is the consent ("Allow <Mac> to run Python code on this iPhone?"). The phone always shows who is connected and what is running, lists every function a Mac has sent with its source (a disassembly when no source exists), lets the person read every Python file fetched from the Mac, and has one switch that stops everything. The lighter design (no source shown) isn't defensible because nothing would distinguish the app from a loader of unseen code. The heavier ones aren't required by the text or by any approved case.
- Built in as an option, off by default: "Ask before running new code". With it on, the first task carrying a given piece of the user's own code waits until the person has read it on the phone and tapped Allow. Approval is per code (a hash of the text shown), not per call and not per argument type, so a 200-task map asks once. Code from installed packages is never asked about: scikit-learn submits its own `_fit_and_score`, not the user's function, and packages are either part of the reviewed app or pure-Python files mirrored from the Mac that are readable in the app. Until the person answers, automatically placed work runs on the Mac and work forced onto the phone waits (two minutes, then an error that says why). The build setting `store_review` makes it the default, which is the fallback if review asks for more than visibility.
- The app also runs code typed on the phone ("Try it on this iPhone"), so it is useful, and reviewable, with no Mac.

### Version policy
- The sideloaded phone runs the Mac's own binaries, so versions always match. The store phone has a fixed set, so they often won't. A compiled package is used on the store phone only when the Mac has the same release (major.minor.patch); otherwise the task runs on the Mac and the note says which version would match. The first-result check against the Mac still applies on top.

### What was built and measured (iOS 27 simulator and static checks; no device, no signing, no submission)
- [M] pandas 3.1.1 cross-compiles for iOS with cibuildwheel 4.3.5 (Python-Apple-support 3.13-b18, Cython 3.3.1, meson 1.12.1, Xcode 27.1): both wheels (device and arm64 simulator) in 249 s. Three things were in the way. cibuildwheel wants the python.org framework install; any CPython 3.13 works as the build-side interpreter. pandas' build imports numpy to find its headers, and an iOS numpy can't be imported on the Mac; the headers are located without importing it. BeeWare's iOS numpy headers pull in `MacTypes.h`, whose `nil` macro breaks a C++ struct field of that name in pandas. The recipe is `src/pocketnode/ios/Store/wheels/` (`pocketnode app wheel NAME VERSION`).
- [M] All 45 extension modules of the device wheel have platform iOS, `LC_ENCRYPTION_INFO_64`, arm64 only, and depend only on `@rpath/Python.framework/Python`, libSystem and (two of them) libc++.
- [M] The store archive (Release, unsigned) with numpy 2.5.2, pandas 3.1.1, pillow, kiwisolver, markupsafe, contourpy and cffi: 92 MB, 126 frameworks, 41 MB of `__TEXT` (the limit is 500 MB). `pocketnode app validate` passes all 33 checks: Info.plist keys, one binary per framework and none elsewhere, platform and encryption load commands, arm64 only, every dependent library in the iOS SDK or the bundle, all 14,519 imported symbols resolving against the SDK's stubs or bundled frameworks, no private frameworks, no shims or stand-ins, no Objective-C runtime patching in the executable, no `itms-services` string, privacy manifests at the app root and in the four frameworks that import required-reason symbols (Python, `_ssl`, `_hashlib`, `PIL._imaging`).
- [M] The same validator on the sideloaded device build fails 16 checks, for the reasons given above: 322 binaries without encryption info, shim and stand-in frameworks, `libtorch_cpu`, 232 symbols that only resolve through shims, the method-adding imports, no privacy manifests.
- [M] Things only an archive build showed: Xcode's default strip removes the executable's exported C functions, which Python finds through `dlsym`, so the store target strips non-global symbols only. CPython's framework template marks extension-module frameworks as `APPL`; the store build writes `FMWK`. CPython's own extension modules are `MH_DYLIB` files.
- [M] `pocketnode check` against the store app in the simulator, from a Mac environment with the same releases: 24 passed, 0 failed, 7 skipped (scipy, scikit-learn, pyarrow, polars, torch and MLX aren't in the app). numpy and pandas results match the Mac; of 40 automatically placed tasks 38 ran on the phone; pure-Python work ran in subinterpreters; adding a package is refused; the function a task ran was listed on the phone with its source. With "Ask before running new code" on, an automatically placed new function ran on the Mac and a forced one ran on the phone after Allow was tapped.
- [M] The sideloaded flavour after these changes, in another simulator with numpy and pandas: 22 passed, 0 failed, 9 skipped.
- [M] dask's scheduler function stays on the Mac for either flavour unless PyYAML's compiled build is on the phone: dask imports yaml, and the Mac's PyYAML is a compiled wheel.
- [I] From the pandas build: scipy 1.18 needs its experimental no-Fortran option, Accelerate's newer LAPACK interface (a deployment target of iOS 16.4 or later, so a different wheel tag) and pythran turned off; one to three days with real risk. scikit-learn compiles against scipy's Cython BLAS and LAPACK declarations and would be built without OpenMP; half a day to a day after scipy.
- Not verified, because each needs a paid account or a phone: signing and export of the archive, App Store Connect's own upload validation, App Review, the store app on a device (cable pairing through usbmuxd, the keychain on a device, background computing, memory limits).
