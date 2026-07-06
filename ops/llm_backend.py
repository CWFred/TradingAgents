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
    def shutdown(self) -> None: ...


class NullManagedBackend:
    """Inert backend used when management is disabled."""

    def ensure_up(self) -> None:  # noqa: D401 - trivial
        return None

    def shutdown(self) -> None:
        return None


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

    @property
    def _binary(self) -> str:
        return os.path.join(self.config.ds4_dir, "ds4-server")

    def ensure_up(self) -> None:
        if not self.config.enabled:
            return
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
    return raw.strip().lower() not in ("0", "false", "no", "off")


def load_managed_backend_config() -> ManagedBackendConfig:
    kind = (os.environ.get("OPS_LLM_MANAGED_BACKEND") or "none").strip().lower()
    if kind in ("", "none"):
        return ManagedBackendConfig(kind="none")
    if kind != "ds4":
        raise ManagedBackendError(
            f"unknown OPS_LLM_MANAGED_BACKEND {kind!r} (supported: ds4)"
        )
    kwargs: dict = {"kind": "ds4"}
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


def build_managed_backend(config: ManagedBackendConfig) -> ManagedBackend:
    if not config.enabled:
        return NullManagedBackend()
    return Ds4ManagedBackend(config)
