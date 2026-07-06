"""Tests for the config-gated managed LLM backend (ds4/DwarfStar lifecycle).

The backend is off unless OPS_LLM_MANAGED_BACKEND=ds4. When on, it brings a
local ds4-server up on first use and tears it down again, but it must never
kill a server it did not start (an externally-launched one).

All external effects (HTTP health check, `lms unload`, `make`, launching the
server) are injected so these tests never spawn a real process or touch RAM.
"""
from __future__ import annotations

import pytest

from ops.llm_backend import (
    Ds4ManagedBackend,
    ManagedBackendConfig,
    ManagedBackendError,
    NullManagedBackend,
    build_managed_backend,
    load_managed_backend_config,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class FakeProc:
    """Stand-in for a Popen handle."""

    def __init__(self) -> None:
        self._exit: int | None = None  # None = still running
        self.terminated = False
        self.killed = False

    def set_exited(self, code: int) -> None:
        self._exit = code

    def poll(self) -> int | None:
        return self._exit

    def terminate(self) -> None:
        self.terminated = True
        self._exit = -15

    def kill(self) -> None:
        self.killed = True
        self._exit = -9

    def wait(self, timeout=None) -> int:
        if self._exit is None:
            self._exit = 0
        return self._exit


class Deps:
    """Records injected calls and lets a test script the health sequence."""

    def __init__(self, health_sequence: list[bool]) -> None:
        self._health = list(health_sequence)
        self.run_calls: list[tuple[list[str], str | None]] = []
        self.run_returns: dict[str, int] = {}  # keyed by argv[0]
        self.spawned: list[tuple[list[str], str]] = []
        self.proc = FakeProc()
        self.existing_paths: set[str] = set()
        self.now = 0.0

    # health_check(base_url) -> bool ; pops the scripted sequence, then holds last
    def health_check(self, base_url: str) -> bool:
        if len(self._health) > 1:
            return self._health.pop(0)
        return self._health[0] if self._health else False

    def run(self, cmd, cwd=None) -> int:
        self.run_calls.append((cmd, cwd))
        return self.run_returns.get(cmd[0], 0)

    def spawn(self, cmd, cwd, log_path) -> FakeProc:
        self.spawned.append((cmd, cwd))
        return self.proc

    def exists(self, path: str) -> bool:
        return path in self.existing_paths

    def sleep(self, _seconds: float) -> None:
        self.now += 1.0

    def monotonic(self) -> float:
        return self.now


def make_backend(deps: Deps, **overrides) -> Ds4ManagedBackend:
    cfg = ManagedBackendConfig(
        kind="ds4",
        ds4_dir="/repo/ds4",
        model="ds4flash.gguf",
        host="127.0.0.1",
        port=8000,
        ctx=100000,
        kv_dir="/kv",
        kv_mb=8192,
        lms_path="/bin/lms",
        build_if_missing=True,
        startup_timeout_s=5.0,
    )
    # By default pretend both the binary and lms exist unless a test says otherwise.
    deps.existing_paths |= {"/repo/ds4/ds4-server", "/bin/lms"}
    for k, v in overrides.items():
        cfg = cfg.__class__(**{**cfg.__dict__, k: v})
    return Ds4ManagedBackend(
        cfg,
        health_check=deps.health_check,
        run=deps.run,
        spawn=deps.spawn,
        exists=deps.exists,
        sleep=deps.sleep,
        monotonic=deps.monotonic,
    )


# --------------------------------------------------------------------------- #
# Config gating
# --------------------------------------------------------------------------- #
def test_config_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OPS_LLM_MANAGED_BACKEND", raising=False)
    cfg = load_managed_backend_config()
    assert cfg.enabled is False
    assert cfg.kind == "none"


def test_config_enabled_for_ds4_with_env_overrides(monkeypatch):
    monkeypatch.setenv("OPS_LLM_MANAGED_BACKEND", "ds4")
    monkeypatch.setenv("DS4_PORT", "9001")
    monkeypatch.setenv("DS4_CTX", "32768")
    cfg = load_managed_backend_config()
    assert cfg.enabled is True
    assert cfg.kind == "ds4"
    assert cfg.port == 9001
    assert cfg.ctx == 32768
    assert cfg.base_url == "http://127.0.0.1:9001/v1"


def test_build_returns_null_when_disabled():
    backend = build_managed_backend(ManagedBackendConfig(kind="none"))
    assert isinstance(backend, NullManagedBackend)
    # Inert: these must not raise or do anything.
    backend.ensure_up()
    backend.shutdown()


def test_build_returns_ds4_when_enabled():
    backend = build_managed_backend(ManagedBackendConfig(kind="ds4"))
    assert isinstance(backend, Ds4ManagedBackend)


# --------------------------------------------------------------------------- #
# Ds4ManagedBackend.ensure_up
# --------------------------------------------------------------------------- #
def test_ensure_up_launches_and_unloads_lms_when_not_healthy():
    # unhealthy on first poll, healthy after launch
    deps = Deps(health_sequence=[False, True])
    backend = make_backend(deps)

    backend.ensure_up()

    # LM Studio was unloaded before launch
    assert (["/bin/lms", "unload", "--all"], None) in deps.run_calls
    # server launched with the expected argv, from the ds4 dir
    assert len(deps.spawned) == 1
    argv, cwd = deps.spawned[0]
    assert argv[0] == "/repo/ds4/ds4-server"
    assert "--port" in argv and "8000" in argv
    assert "-m" in argv and "ds4flash.gguf" in argv
    assert cwd == "/repo/ds4"


def test_ensure_up_skips_launch_when_already_healthy():
    deps = Deps(health_sequence=[True])
    backend = make_backend(deps)

    backend.ensure_up()

    assert deps.spawned == []          # did not launch
    assert deps.run_calls == []        # did not unload LM Studio


def test_ensure_up_does_not_unload_or_launch_when_disabled():
    deps = Deps(health_sequence=[False])
    cfg = ManagedBackendConfig(kind="none")
    backend = Ds4ManagedBackend(
        cfg, health_check=deps.health_check, run=deps.run,
        spawn=deps.spawn, exists=deps.exists, sleep=deps.sleep,
        monotonic=deps.monotonic,
    )
    backend.ensure_up()
    assert deps.spawned == []
    assert deps.run_calls == []


def test_ensure_up_builds_when_binary_missing():
    deps = Deps(health_sequence=[False, True])
    backend = make_backend(deps)
    deps.existing_paths.discard("/repo/ds4/ds4-server")  # binary absent

    backend.ensure_up()

    make_calls = [c for c in deps.run_calls if c[0][0] == "make"]
    assert make_calls, "expected a make build before launch"
    assert make_calls[0][1] == "/repo/ds4"  # built in the ds4 dir


def test_ensure_up_raises_when_binary_missing_and_build_disabled():
    deps = Deps(health_sequence=[False])
    backend = make_backend(deps, build_if_missing=False)
    deps.existing_paths.discard("/repo/ds4/ds4-server")

    with pytest.raises(ManagedBackendError):
        backend.ensure_up()
    assert deps.spawned == []


def test_ensure_up_raises_when_build_fails():
    deps = Deps(health_sequence=[False])
    deps.run_returns["make"] = 2
    backend = make_backend(deps)
    deps.existing_paths.discard("/repo/ds4/ds4-server")

    with pytest.raises(ManagedBackendError):
        backend.ensure_up()
    assert deps.spawned == []


def test_ensure_up_raises_when_server_exits_during_startup():
    deps = Deps(health_sequence=[False, False])
    cfg = make_backend(deps).config  # reuse the fully-populated config

    def spawn(cmd, cwd, log_path):
        deps.spawned.append((cmd, cwd))
        deps.proc.set_exited(1)  # dies immediately
        return deps.proc

    backend = Ds4ManagedBackend(
        cfg, health_check=deps.health_check,
        run=deps.run, spawn=spawn, exists=deps.exists,
        sleep=deps.sleep, monotonic=deps.monotonic,
    )
    with pytest.raises(ManagedBackendError):
        backend.ensure_up()


def test_ensure_up_times_out_and_kills_process():
    # never becomes healthy; process stays alive
    deps = Deps(health_sequence=[False])
    backend = make_backend(deps, startup_timeout_s=3.0)

    with pytest.raises(ManagedBackendError):
        backend.ensure_up()
    assert deps.proc.killed is True


# --------------------------------------------------------------------------- #
# Ds4ManagedBackend.shutdown — ownership rules
# --------------------------------------------------------------------------- #
def test_shutdown_terminates_owned_process():
    deps = Deps(health_sequence=[False, True])
    backend = make_backend(deps)
    backend.ensure_up()

    backend.shutdown()

    assert deps.proc.terminated is True


def test_shutdown_does_not_kill_external_server():
    # already healthy => external; we never started it
    deps = Deps(health_sequence=[True])
    backend = make_backend(deps)
    backend.ensure_up()

    backend.shutdown()

    assert deps.proc.terminated is False
    assert deps.proc.killed is False


def test_shutdown_is_noop_when_never_started():
    deps = Deps(health_sequence=[True])
    backend = make_backend(deps)
    # No ensure_up call at all.
    backend.shutdown()  # must not raise
    assert deps.proc.terminated is False


def test_null_backend_is_inert():
    backend = NullManagedBackend()
    backend.ensure_up()
    backend.shutdown()  # both no-ops, no raise
