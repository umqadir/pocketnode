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
- [C] Static check against the iOS 27.1 SDK `.tbd` stubs (`scratchpad/retag/check_ios.py`): 981 of 1,047 Mach-O files and 62 of 88 packages in the Mac's global env are clean.
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
