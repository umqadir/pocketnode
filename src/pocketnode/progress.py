"""Progress lines for the steps that take a while (staging, building, installing, sending packages).

    with progress.step("Building the app") as p:
        p.detail("compiling sources")
        p.update(212, 328, detail="signing frameworks")

On a terminal the step redraws one line in place,
`Building the app  signing frameworks 212/328  1:42`, ticking the elapsed time
every second even when nothing reports, and ends with
`Building the app  done in 4:51` (or `failed after 0:12`). Anywhere else (a
pipe, a log file, a notebook) it prints a line when the detail changes and
otherwise at most every 15 seconds. It writes to stderr, never raises, and is
silenced by `progress.quiet(True)` or POCKETNODE_QUIET=1.
"""

from __future__ import annotations

import os
import sys
import threading
import time

REDRAW_EVERY = 0.2  # terminal: at most 5 redraws a second
LOG_EVERY = 15.0  # elsewhere: a line at most this often unless the detail changes
TICK_EVERY = 1.0  # the elapsed time moves even when nothing reports
_clock = time.monotonic
_quiet = False


def quiet(flag: bool = True) -> None:
    """Silence (or restore) every progress line in this process."""
    global _quiet
    _quiet = flag


def is_quiet() -> bool:
    return _quiet or os.environ.get("POCKETNODE_QUIET", "") not in ("", "0")


def elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _num(x) -> str:
    if isinstance(x, float):
        return f"{x:.0f}" if x >= 10 or x == int(x) else f"{x:.1f}"
    return str(x)


def _live(stream) -> bool:
    """Redraw in place: a terminal, and not a Jupyter kernel (whose output isn't one)."""
    if "ipykernel" in sys.modules:
        return False
    try:
        return stream.isatty()
    except Exception:  # noqa: BLE001
        return False


def _width(stream) -> int:
    try:
        cols = os.get_terminal_size(stream.fileno()).columns
    except Exception:  # noqa: BLE001
        cols = 0
    return cols if cols > 0 else 80  # a terminal that doesn't say how wide it is


class Step:
    """One step's progress line. Thread-safe; nothing it does raises."""

    def __init__(self, label: str, stream=None, silent: bool = False, live: bool | None = None):
        self.label = label
        self.silent = silent
        self.stream = stream if stream is not None else sys.stderr
        self.live = _live(self.stream) if live is None else live
        self._lock = threading.RLock()
        self._detail, self._done, self._total, self._unit = "", None, None, ""
        self._t0 = _clock()
        self._drawn = self._logged = 0.0
        self._logged_detail: str | None = None
        self._shown = False  # a live line is on screen
        self._closed = False
        self._stop = threading.Event()

    # ------------------------------------------------------------ reporting

    def update(self, done=None, total=None, detail: str | None = None, unit: str | None = None) -> None:
        """Set the count (and optionally the detail and the count's unit, e.g. "MB")."""
        if self.silent:
            return
        with self._lock:
            changed = detail is not None and detail != self._detail
            if detail is not None:
                self._detail = detail
            if unit is not None or changed:
                self._unit = unit or ""
            self._done, self._total = done, total
            self._show(force=changed)

    def detail(self, text: str) -> None:
        """A new phase without a count."""
        self.update(None, None, detail=text)

    def echo(self, text: str, out=None) -> None:
        """Print a line of other output without scribbling over the progress line.
        out: the function that prints it (default: print)."""
        out = out or print
        if self.silent:
            out(text)
            return
        with self._lock:
            if self.live and self._shown:
                self._write("\r\x1b[K")
                self._shown = False
            try:
                out(text)
            except Exception:  # noqa: BLE001
                pass
            self._show(force=True)

    # ------------------------------------------------------------- the line

    def _body(self) -> str:
        count = ""
        if self._done is not None and self._unit == "%":
            count = f"{_num(self._done)}%"
        elif self._done is not None:
            count = _num(self._done) + (f"/{_num(self._total)}" if self._total is not None else "")
            if self._unit:
                count += f" {self._unit}"
        return " ".join(x for x in (self._detail, count) if x)

    def _line(self) -> str:
        t = elapsed(_clock() - self._t0)
        body = self._body()
        if self.live:
            return "  ".join(x for x in (self.label, body, t) if x)
        return f"{self.label}: {body} ({t})" if body else f"{self.label} ({t})"

    def _write(self, s: str) -> None:
        try:
            sys.stdout.flush()  # keep the order right when stdout and stderr share a file
        except Exception:  # noqa: BLE001
            pass
        try:
            self.stream.write(s)
            self.stream.flush()
        except Exception:  # noqa: BLE001
            pass

    def _show(self, force: bool = False, tick: bool = False) -> None:
        if self._closed:
            return
        now = _clock()
        if self.live:
            if not (force or tick) and now - self._drawn < REDRAW_EVERY:
                return
            line = self._line()
            w = _width(self.stream) - 1
            if len(line) > w:
                line = line[: max(w - 1, 0)] + "…"
            self._write("\r" + line + "\x1b[K")
            self._shown, self._drawn = True, now
        else:
            if self._detail != self._logged_detail or now - self._logged >= LOG_EVERY:
                self._write(self._line() + "\n")
                self._logged, self._logged_detail = now, self._detail

    def _ticker(self) -> None:
        while not self._stop.wait(TICK_EVERY):
            with self._lock:
                self._show(tick=self.live)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> Step:
        if self.silent:
            return self
        with self._lock:
            self._t0 = _clock()
            if self.live:
                self._show(force=True)
            else:
                self._write(self.label + "\n")
                self._logged, self._logged_detail = self._t0, ""
        threading.Thread(target=self._ticker, daemon=True, name="pn-progress").start()
        return self

    def finish(self, outcome: str = "done") -> None:
        """outcome: "done", "failed" or "stopped"."""
        if self.silent:
            return
        self._stop.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            t = elapsed(_clock() - self._t0)
            end = f"done in {t}" if outcome == "done" else f"{outcome} after {t}"
            if self.live:
                self._write("\r" + f"{self.label}  {end}"[: max(_width(self.stream) - 1, 1)] + "\x1b[K\n")
            else:
                self._write(f"{self.label}: {end}\n")

    def __enter__(self) -> Step:
        return self.start()

    def __exit__(self, kind, exc, tb) -> bool:
        self.finish("done" if kind is None else "stopped" if issubclass(kind, KeyboardInterrupt) else "failed")
        return False


def step(label: str, quiet: bool = False, stream=None) -> Step:
    """A progress line for one step; use it as a context manager."""
    return Step(label, stream=stream, silent=quiet or is_quiet())


NULL = Step("", silent=True)  # reports nothing: the default where a reporter is optional


def counter(p, total: int, detail: str, unit: str | None = None):
    """A function to call once per finished item (from any thread): reports k/total under `detail`."""
    lock, n = threading.Lock(), [0]
    p.update(0, total, detail=detail, unit=unit)

    def tick() -> None:
        with lock:
            n[0] += 1
            k = n[0]
            p.update(k, total, detail=detail, unit=unit)
    return tick


class Prefixed:
    """A reporter that puts `prefix: ` before every detail (e.g. "preparing lightgbm: retagging")."""

    def __init__(self, p, prefix: str):
        self.p, self.prefix = p, prefix

    def update(self, done=None, total=None, detail: str | None = None, unit: str | None = None) -> None:
        self.p.update(done, total, detail=None if detail is None else f"{self.prefix}: {detail}", unit=unit)

    def detail(self, text: str) -> None:
        self.update(None, None, detail=text)
