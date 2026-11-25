# pocketnode

Use your iPhone as a second processor for your Mac. Plug it in and your Python (pandas, scikit-learn, PyTorch, your own functions) runs across both the Mac's cores and the phone's.

```python
import pocketnode as pn

with pn.parallel():                                   # scikit-learn's n_jobs now includes the phone
    GridSearchCV(model, grid, n_jobs=-1).fit(X, y)

df.pn.groupby("customer").apply(features)             # pandas, split across Mac and phone
```

## Overview

| | |
|---|---|
| **APIs** | a `concurrent.futures` executor (`submit`, `map`), scikit-learn and joblib (`n_jobs`), pandas (`groupby().apply`, `map`, `apply`), Dask, and a `%%phone` cell in Jupyter |
| **Libraries** | numpy, pandas, scipy, scikit-learn, PyTorch, MLX, polars, pyarrow, duckdb, matplotlib, and any pure-Python package. Others get added the first time your code needs them. |
| **Phone hardware** | all six CPU cores, the GPU via PyTorch (`"mps"`) and MLX, and about 6 GB of memory |
| **Package versions** | the phone runs the same builds installed on your Mac, so there is no second environment to keep in sync |
| **Correctness** | phone results are checked against the Mac, and tasks the phone can't run fall back to the Mac |
| **Connection** | USB cable, or Wi-Fi if you turn it on. Code and data stay between your Mac and your phone. |

## Benchmarks

Measured on an iPhone 18 Pro:

| | |
|---|---|
| Pure Python | 38 million loop iterations a second per core, on six cores |
| NumPy matrix multiply | 2,100 GFLOPS in float32, 510 in float64 |
| PyTorch, small CNN, batch of 64 | 20 ms a training step on the GPU, 70 ms on the CPU |
| MLX, half-precision matrix multiply | 6 to 10 TFLOPS on the GPU |

## Install

```
uv pip install "pocketnode[all] @ git+https://github.com/umqadir/pocketnode"
pocketnode setup
```

Install it into the Python 3.13 environment your code runs in, since the phone runs that environment's packages. `pocketnode setup` checks Xcode, builds and signs the app with your Apple ID, installs it, and runs a full check against the phone. When something needs you, it tells you what to tap and waits.

Apple makes you do four things once:

1. Sign in to Xcode: Xcode › Settings › Apple Accounts.
2. Plug the phone in, unlock it, and tap Trust.
3. Turn on Developer Mode: Settings › Privacy & Security › Developer Mode.
4. With a free Apple ID, trust yourself as a developer after the app is installed: Settings › General › VPN & Device Management.

The first build takes about five minutes; later updates run from the command line.

| Requirements | |
|---|---|
| Mac | Apple silicon |
| Xcode | 26 or later, with the iOS platform |
| iPhone | iOS 26 or later, and a cable |
| Python | 3.13 |
| Apple ID | free or paid. A free one signs the app for a week at a time; `pocketnode app install` renews it. |

## Usage

Connect once and submit work:

```python
import pocketnode as pn

ex = pn.connect()                       # Mac cores + the phone
fut = ex.submit(fit_model, X, y)        # lambdas, closures and notebook functions all work
fut.result()
fut.placement                           # where it ran, and why
```

**Slow `.map` over a column.** Cleaning two million addresses with a Python function:

```python
df["address"] = df["address"].pn.map(clean_address)
```

**Features per customer.** Any `groupby().apply()`; groups are never split.

```python
features = transactions.pn.groupby("customer").apply(customer_features)
```

**One model per store.**

```python
coefficients = sales.pn.groupby("store").apply(fit_store)
```

**Cross-validation and grid search.** Anything in scikit-learn that takes `n_jobs`.

```python
with pn.parallel():
    scores = cross_val_score(model, X, y, cv=10, n_jobs=-1)
```

**Your own loop.** Fuzzy-matching names or bootstrapping a statistic:

```python
matches = list(ex.map(best_match, names))
```

**Training on the phone's GPU.** Code that checks for `"mps"` picks up the phone's GPU too.

```python
def train(config):
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    ...

results = list(ex.map(train, configs))  # trials run on both GPUs
```

**MLX.** `pocketnode packages add mlx`, then the same `mlx` code runs on either GPU.

**Dask.** `dask.compute(graph, scheduler=ex)`.

**Jupyter.** `%load_ext pocketnode`, then start a cell with `%%phone`. Variables the cell reads go to the phone, and ones it assigns come back.

Manual placement: `ex.on("phone").submit(...)`, `@pn.runs_on("mac")`, or `with pn.placement("phone"):`.

## Correctness

- **First call is checked.** The Mac runs each function's first task too and compares outputs within floating-point tolerance. If they differ, you get the Mac's answer and that function stays on the Mac.
- **Errors are confirmed.** Exceptions on the phone are re-run on the Mac before you see them.
- **Unsupported tasks stay on the Mac.** Tasks that read files on your Mac or start processes run locally, along with anything needing a library the phone can't load.
- **Disconnects don't drop work.** Unplug the phone, lock it, or let it get hot and its work finishes on the Mac.
- **Forced placement fails loudly.** `ex.on("phone")` raises `PlacementError` if the phone can't run the task.

## How it works

**The app.** `pocketnode setup` puts a small Python 3.13 app on the phone. Open it and leave it up: it prints a status line on a black screen and keeps the phone awake while in front.

**The cable.** `pn.connect()` finds the phone over USB on its own. Round trips take about a millisecond, and a USB 2 cable moves 34 MB a second. A pairing token built into your copy of the app keeps other Macs out.

**Wi-Fi.** `pocketnode app install --wifi` lets the Mac reach the phone on your local network via `pn.connect("phone-address:7151")`. Round trips take about 20 ms, so use the cable when you can.

**Backgrounding.** Switch to another app and the phone keeps computing, slower, until you come back. If iOS pauses the app (for example, when the phone locks), unfinished work moves to the Mac and the phone picks up again when you return.

**Cable pulled or thermal throttling.** Pull the cable and the phone's tasks finish on the Mac. When the phone reports it's getting hot, it gets less work.

**Data caching.** Arrays and DataFrames ship as raw buffers and cache on the phone by content, so an `X` used across 200 fits crosses the cable once. `ex.pin(X)` keeps it there.

**Disk caching.** Inputs of 8 MB or more, and anything pinned, are also kept on the phone's disk by content, so model weights and large tables survive an app restart and aren't sent again. The disk cache uses up to a fifth of the phone's free space (at most 50 GB) and drops the least recently used first. A file iOS cleared or that no longer matches its content is sent again.

**Scheduling.** pocketnode measures each function on each device, and the link's speed, then sends tasks where they'll finish soonest. Memory-bound operations like filtering or merging one large table stay on the Mac. Compute-heavy work spreads out.

**Subinterpreters and threads.** iOS blocks child processes, so pure-Python tasks run in one subinterpreter per core, each with its own GIL. NumPy, scikit-learn and PyTorch release the GIL and run on threads.

**Packages.** Your Mac's compiled packages are converted for iOS and signed with your developer certificate, which is why the phone has your exact versions and can add new packages while running.

**Multiple clients.** Notebooks, scripts, and `pocketnode status` can share the phone at the same time.

## Packages

Pure-Python packages need no setup; the phone imports them from your Mac as needed.

Compiled packages install while the app is running without a rebuild or restart. The first time a task needs one, that task runs on the Mac while the package goes to the phone in the background, and later tasks use the phone. duckdb takes about ten seconds.

```
pocketnode packages              # what's on the phone, and what can be
pocketnode packages add lightgbm
pocketnode packages select       # pick from a checklist
```

A few packages depend on libraries iOS doesn't have. `pocketnode packages` lists them, and tasks needing them run on the Mac.

## Commands

```
pocketnode setup        first run: checks, build, install, full check
pocketnode check        run every feature against the phone, pass or fail per item
pocketnode status       what's connected
pocketnode bench        speed of the link and of the phone
pocketnode app install  rebuild and reinstall (renews a free Apple ID's week)
```

## Troubleshooting

<details>
<summary>Common errors and fixes</summary>

- **"sign in to Xcode"**: Xcode › Settings › Apple Accounts › Sign In. A free Apple ID works.
- **"Xcode has your Apple ID but hasn't loaded its team yet"**: open Xcode › Settings › Apple Accounts and click the account once.
- **"several Apple development teams are available"**: `pocketnode setup --team TEAMID`. The message lists them.
- **Developer Mode is off**: Settings › Privacy & Security › Developer Mode, restart, confirm with your passcode. If the menu isn't there, connect the phone to the Mac and open Xcode once.
- **The app won't open, or "profile has not been explicitly trusted"**: Settings › General › VPN & Device Management › your Apple ID › Trust, then open the pocketnode app.
- **"the phone app's signing has expired"**: a free Apple ID signs for seven days. Run `pocketnode app install`.
- **"phone attached, app not answering"**: unlock the phone. pocketnode starts the app itself.
- **"the app on ... was built from a different version of pocketnode"**: you updated pocketnode. Run `pocketnode app install`.
- **"the bundle identifier ... is taken"**: pick your own with `--bundle-id com.yourname.pocketnode`.
- **"no iOS GPU shaders"**: `xcodebuild -downloadComponent MetalToolchain`, then `pocketnode app install`.
- **"Python 3.x" at the first setup step**: create a 3.13 environment (`uv venv --python 3.13`) and install pocketnode there.
- **A task seems stuck**: `ex.stacks("phone")` shows what the phone is doing.

</details>

## App Store edition

The app `pocketnode setup` builds is signed by you and runs anything. The project also builds an App Store edition with a fixed set of packages, on-phone pairing, and a screen showing what code is running. [RESEARCH-NOTES.md](RESEARCH-NOTES.md) covers the rules it was built to.

<details>
<summary>Differences and build steps</summary>

| | The app you build | App Store edition |
|---|---|---|
| Compiled packages | your Mac's own | a fixed set built for iOS: numpy, pandas, pillow and a few more |
| Adding a compiled package | seconds, while the app runs | not possible |
| PyTorch and MLX | yes, CPU and GPU | no |
| Pure-Python packages | imported from the Mac as needed | the same |
| Pairing | built into your copy of the app | on the phone: tap Pair a Mac, type the code on the Mac, tap Allow |
| On the phone's screen | a status line | who is connected, what is running, and the source of every function sent |
| Without a Mac | nothing to do | a Python console on the phone |

The phone runs a compiled package when your Mac has the same release (`uv pip install numpy==2.5.2 pandas==3.1.1` for the default set). Other tasks run on the Mac.

```
pocketnode app wheel pandas 3.1.1          # once: build pandas for iOS, about 4 minutes
pocketnode app install --store --sim       # run it in the simulator (port 7161)
pocketnode pair --address 127.0.0.1:7161   # type the code the app shows
pocketnode check --address 127.0.0.1:7161
pocketnode app archive                     # archive for submission
pocketnode app validate                    # the checks Apple's upload runs
pocketnode app build --dev                 # back to the app you sign yourself
```

On a phone, `pocketnode pair` works over the cable. "Ask before running new code" in the app holds each new function until you read it and tap Allow.

</details>

## Development

To work on pocketnode itself:

```
uv sync
uv venv --python 3.13 .phone-sim
uv pip install --python .phone-sim/bin/python cloudpickle "numpy==$(uv run python -c 'import numpy; print(numpy.__version__)')"
uv run pytest
```

Tests run against a local stand-in for the phone, so you don't need a phone or Apple ID. [RESEARCH-NOTES.md](RESEARCH-NOTES.md) has the design details and measurements.

Licensed under the AGPL-3.0.
