"""Config-gated lifecycle manager for a local LLM inference server.

The trading pipeline talks to whatever OpenAI-compatible endpoint
``TRADINGAGENTS_LLM_BACKEND_URL`` points at. When that endpoint is a local
server we can start on demand — currently ds4/DwarfStar serving DeepSeek V4
Flash — this module brings it up when an analysis is about to run and tears it
down afterwards, freeing the ~86 GB it holds resident.

It is **off unless** ``OPS_LLM_MANAGED_BACKEND=ds4``. When off,
``build_managed_backend`` returns an inert :class:`NullManagedBackend` so
hosted-API and manually-run-server setups are completely unaffected.

Ownership rule: :class:`Ds4ManagedBackend` only ever stops a server it started
itself. If ``ensure_up`` finds the port already serving (you launched ds4 by
hand), it leaves it alone on ``shutdown``.

All external effects (HTTP health check, ``lms unload``, ``make``, launching
the server) are injected, so the behavior is unit-testable without spawning a
real process.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class ManagedBackendError(RuntimeError):
    """Raised when a managed backend cannot be brought up."""


class ManagedBackendPaused(ManagedBackendError):
    """Raised when the operator resource-pause lease forbids model startup."""


def _expand(path: str) -> str:
    return os.path.expanduser(path)


@dataclass(frozen=True)
class ManagedBackendConfig:
    kind: str = "none"  # "none" | "ds4"
    ds4_dir: str = field(default_factory=lambda: _expand("~/Code/ds4"))
    model: str = "ds4flash.gguf"
    host: str = "127.0.0.1"
    port: int = 8000
    ctx: int = 100000
    kv_dir: str = field(default_factory=lambda: _expand("~/.ds4/server-kv"))
    kv_mb: int = 8192
    lms_path: str = field(default_factory=lambda: _expand("~/.lmstudio/bin/lms"))
    build_if_missing: bool = True
    startup_timeout_s: float = 180.0
    pause_flag_path: str | None = None

    @property
    def enabled(self) -> bool:
        return self.kind == "ds4"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"


# --------------------------------------------------------------------------- #
# Managed backend interface + implementations
# --------------------------------------------------------------------------- #
class ManagedBackend(Protocol):
    def ensure_up(self) -> None: ...
    def interrupt(self) -> None: ...
    def shutdown(self) -> None: ...


class NullManagedBackend:
    """Inert backend used when management is disabled."""

    def ensure_up(self) -> None:  # noqa: D401 - trivial
        return None

    def interrupt(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


# Active TradingAgents model sessions register here so the operator pause can
# preempt them without stopping the broker guardian or the daemon itself.
_model_targets: dict[int, tuple[ManagedBackend, int]] = {}
_model_targets_lock = threading.Lock()


def register_model_backend(backend: ManagedBackend) -> None:
    """Expose an active model backend to the daemon's pause handler."""
    key = id(backend)
    with _model_targets_lock:
        current = _model_targets.get(key)
        count = current[1] + 1 if current is not None else 1
        _model_targets[key] = (backend, count)


def unregister_model_backend(backend: ManagedBackend) -> None:
    key = id(backend)
    with _model_targets_lock:
        current = _model_targets.get(key)
        if current is None:
            return
        if current[1] <= 1:
            _model_targets.pop(key, None)
        else:
            _model_targets[key] = (current[0], current[1] - 1)


def interrupt_model_backends() -> int:
    """Interrupt active TradingAgents inference, returning the target count."""
    with _model_targets_lock:
        targets = [entry[0] for entry in _model_targets.values()]
    for backend in targets:
        interrupt = getattr(backend, "interrupt", None)
        if callable(interrupt):
            interrupt()
    return len(targets)


# Compatibility names for callers introduced with the background-only pause.
register_background_backend = register_model_backend
unregister_background_backend = unregister_model_backend
interrupt_background_backends = interrupt_model_backends


# Injected-dependency signatures (defaults below wire up the real ones).
HealthCheck = Callable[[str], bool]
Runner = Callable[[list[str], "str | None"], int]
Spawner = Callable[[list[str], str, str], object]
Exists = Callable[[str], bool]


def _default_health_check(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=5) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


def _default_run(cmd: list[str], cwd: str | None) -> int:
    return subprocess.run(cmd, cwd=cwd, capture_output=True).returncode


def _default_spawn(cmd: list[str], cwd: str, log_path: str) -> subprocess.Popen:
    log = open(log_path, "ab", buffering=0)  # noqa: SIM115 - lifetime tied to server
    return subprocess.Popen(
        cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )


class Ds4ManagedBackend:
    """Starts/stops a ds4-server on demand, only killing what it started."""

    def __init__(
        self,
        config: ManagedBackendConfig,
        *,
        health_check: HealthCheck = _default_health_check,
        run: Runner = _default_run,
        spawn: Spawner = _default_spawn,
        exists: Exists = os.path.exists,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        log_path: str | None = None,
    ) -> None:
        self.config = config
        self._health = health_check
        self._run = run
        self._spawn = spawn
        self._exists = exists
        self._sleep = sleep
        self._monotonic = monotonic
        self._log_path = log_path or os.path.join(config.ds4_dir, "ds4-server.log")
        self._proc: object | None = None  # set only when we launch it
        self._lock = threading.Lock()
        self._interrupt_requested = threading.Event()

    @property
    def _binary(self) -> str:
        return os.path.join(self.config.ds4_dir, "ds4-server")

    def ensure_up(self) -> None:
        if not self.config.enabled:
            return
        if self.config.pause_flag_path:
            from ops.work_pause import pause_state

            if pause_state(
                self.config.pause_flag_path, cleanup_expired=True,
            ).paused:
                raise ManagedBackendPaused("model startup blocked by operator pause")
        with self._lock:
            if self._health(self.config.base_url):
                return  # already serving (ours from a prior call, or external)
            self._free_lm_studio()
            self._build_if_needed()
            self._launch_and_wait()

    def _free_lm_studio(self) -> None:
        # Best effort: unload any LM Studio model so two big models can't
        # stack in RAM (that combination has crashed the machine). A missing
        # lms CLI or "nothing loaded" is fine; ignore the exit code.
        if self._exists(self.config.lms_path):
            self._run([self.config.lms_path, "unload", "--all"], None)

    def _build_if_needed(self) -> None:
        if self._exists(self._binary):
            return
        if not self.config.build_if_missing:
            raise ManagedBackendError(
                f"{self._binary} is missing and build_if_missing is disabled"
            )
        rc = self._run(["make", "-j8", "ds4-server"], self.config.ds4_dir)
        if rc != 0:
            raise ManagedBackendError(f"building ds4-server failed (make exited {rc})")

    def _launch_and_wait(self) -> None:
        cfg = self.config
        argv = [
            self._binary, "-m", cfg.model, "--metal",
            "--ctx", str(cfg.ctx),
            "--kv-disk-dir", cfg.kv_dir, "--kv-disk-space-mb", str(cfg.kv_mb),
            "--host", cfg.host, "--port", str(cfg.port),
        ]
        proc = self._spawn(argv, cfg.ds4_dir, self._log_path)
        self._proc = proc
        deadline = self._monotonic() + cfg.startup_timeout_s
        while self._monotonic() < deadline:
            if self._interrupt_requested.is_set():
                proc.terminate()
                raise ManagedBackendError("ds4-server startup interrupted")
            if proc.poll() is not None:  # exited before becoming healthy
                self._proc = None
                raise ManagedBackendError(
                    f"ds4-server exited during startup (code {proc.poll()})"
                )
            if self._health(cfg.base_url):
                return
            self._sleep(1.0)
        # Timed out — kill what we started so we don't leak a half-loaded server.
        try:
            proc.kill()
        finally:
            self._proc = None
        raise ManagedBackendError(
            f"ds4-server did not become healthy within {cfg.startup_timeout_s:.0f}s"
        )

    def interrupt(self) -> None:
        """Stop owned inference promptly without waiting for process teardown.

        This is intentionally narrower than ``shutdown``: it is safe to call
        from the daemon's control-signal handler, never touches an externally
        started server, and leaves the Popen handle for the worker's normal
        ``shutdown`` path to reap.
        """
        self._interrupt_requested.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def shutdown(self) -> None:
        if not self.config.enabled:
            return
        with self._lock:
            proc = self._proc
            self._proc = None
            if proc is None:  # never started by us (disabled path, or external)
                return
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()


# --------------------------------------------------------------------------- #
# Config loading + factory
# --------------------------------------------------------------------------- #
def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ManagedBackendError(f"invalid int for {name}: {raw!r}") from exc


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    # Never guess: a denylist would parse 'disable'/typos as True, the
    # opposite of the notify config's allowlist semantics.
    raise ManagedBackendError(
        f"invalid bool for {name}: {raw!r} (use 1/0, true/false, yes/no, on/off)"
    )


def load_managed_backend_config() -> ManagedBackendConfig:
    from ops.work_pause import default_pause_path

    kind = (os.environ.get("OPS_LLM_MANAGED_BACKEND") or "none").strip().lower()
    if kind in ("", "none"):
        return ManagedBackendConfig(
            kind="none",
            pause_flag_path=os.environ.get("OPS_RESEARCH_PAUSE_FLAG_PATH")
            or default_pause_path(),
        )
    if kind != "ds4":
        raise ManagedBackendError(
            f"unknown OPS_LLM_MANAGED_BACKEND {kind!r} (supported: ds4)"
        )
    kwargs: dict = {
        "kind": "ds4",
        "pause_flag_path": os.environ.get("OPS_RESEARCH_PAUSE_FLAG_PATH")
        or default_pause_path(),
    }
    for env_name, key in (
        ("DS4_DIR", "ds4_dir"), ("DS4_MODEL", "model"), ("DS4_HOST", "host"),
        ("DS4_KV_DIR", "kv_dir"), ("DS4_LMS_PATH", "lms_path"),
    ):
        val = os.environ.get(env_name)
        if val:
            kwargs[key] = _expand(val) if key in ("ds4_dir", "kv_dir", "lms_path") else val
    for env_name, key in (("DS4_PORT", "port"), ("DS4_CTX", "ctx"), ("DS4_KV_MB", "kv_mb")):
        val = _env_int(env_name)
        if val is not None:
            kwargs[key] = val
    timeout = os.environ.get("DS4_STARTUP_TIMEOUT_S")
    if timeout:
        kwargs["startup_timeout_s"] = float(timeout)
    build = _env_bool("DS4_BUILD_IF_MISSING")
    if build is not None:
        kwargs["build_if_missing"] = build
    return ManagedBackendConfig(**kwargs)


def _listener_pids(port: int) -> list[int]:
    """Best-effort local listener discovery used by the operator kill switch."""
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for raw in result.stdout.splitlines():
        try:
            pids.append(int(raw.strip()))
        except ValueError:
            continue
    return pids


def _process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def terminate_configured_ds4(
    config: ManagedBackendConfig,
    *,
    listener_pids: Callable[[int], list[int]] = _listener_pids,
    process_command: Callable[[int], str] = _process_command,
    send_signal: Callable[[int, int], None] = os.kill,
) -> int:
    """Terminate verified ds4 listeners, including orphaned server processes.

    Port ownership alone is not enough: the command's executable must resolve
    to the configured ds4 binary.  This makes the hard resource cutoff useful
    without ever killing an unrelated service that later reused port 8000.
    """
    binary = os.path.realpath(os.path.join(config.ds4_dir, "ds4-server"))
    stopped = 0
    for pid in listener_pids(config.port):
        command = process_command(pid)
        try:
            executable = os.path.realpath(shlex.split(command)[0])
        except (ValueError, IndexError):
            continue
        if executable != binary:
            continue
        try:
            send_signal(pid, signal.SIGTERM)
        except (OSError, ValueError):
            continue
        stopped += 1
    return stopped


def build_managed_backend(config: ManagedBackendConfig) -> ManagedBackend:
    if not config.enabled:
        return NullManagedBackend()
    return Ds4ManagedBackend(config)
