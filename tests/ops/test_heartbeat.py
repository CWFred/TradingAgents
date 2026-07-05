"""A1.3 dead-man's switch: the heartbeat job pings an external
healthchecks.io-style URL only while the guardian loop is demonstrably
alive, so the EXTERNAL service alarms when pings stop — the one alert
path that works when the process cannot speak for itself."""
from types import SimpleNamespace

from ops.journal import Journal
from ops.main import (
    _build_heartbeat_job,
    _make_heartbeat_job,
    _start_full_scheduler,
    _start_guardian_only,
)

URL = "https://hc-ping.com/abc123"


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _Pinger:
    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self._fail = fail

    def __call__(self, url: str) -> None:
        self.calls.append(url)
        if self._fail:
            raise RuntimeError("monitoring outage: token=secret host=hc-ping.internal")


def _guardian(last_pass):
    return SimpleNamespace(last_pass_started_at=last_pass)


def test_fresh_guardian_pass_pings(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    clock = _Clock()
    pinger = _Pinger()
    job = _make_heartbeat_job(
        guardian=_guardian(clock() - 10), journal=j, url=URL,
        http_get=pinger, clock=clock,
    )
    job()
    assert pinger.calls == [URL]


def test_wedged_guardian_does_not_ping(tmp_path):
    """A stale last_pass_started_at (>= 180s) means the safety loop is
    wedged or dead — the ping MUST stop so the external check alarms."""
    j = Journal(str(tmp_path / "j.sqlite"))
    clock = _Clock()
    pinger = _Pinger()
    job = _make_heartbeat_job(
        guardian=_guardian(clock() - 200), journal=j, url=URL,
        http_get=pinger, clock=clock,
    )
    job()
    assert pinger.calls == []


def test_guardian_never_ran_does_not_ping(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    clock = _Clock()
    pinger = _Pinger()
    job = _make_heartbeat_job(
        guardian=_guardian(None), journal=j, url=URL,
        http_get=pinger, clock=clock,
    )
    job()
    assert pinger.calls == []


def test_ping_failure_swallowed_and_journaled_once_per_cooldown(tmp_path):
    """A monitoring outage must never disturb trading: the exception is
    swallowed, journaled as heartbeat_error at most once per 10 minutes,
    and the payload carries only the exception TYPE (URLs/tokens in
    requests exception text must not reach the journal)."""
    j = Journal(str(tmp_path / "j.sqlite"))
    clock = _Clock()
    pinger = _Pinger(fail=True)
    guardian = _guardian(clock())
    job = _make_heartbeat_job(
        guardian=guardian, journal=j, url=URL, http_get=pinger, clock=clock,
    )
    job()                                   # fails -> journaled
    clock.advance(60)
    guardian.last_pass_started_at = clock()
    job()                                   # fails again, within cooldown
    errors = [e for e in j.read_events() if e["kind"] == "heartbeat_error"]
    assert len(errors) == 1
    assert errors[0]["payload"] == {"error_type": "RuntimeError"}
    assert "secret" not in str(errors[0]["payload"])

    clock.advance(600)                      # past the 10-minute cooldown
    guardian.last_pass_started_at = clock()
    job()
    errors = [e for e in j.read_events() if e["kind"] == "heartbeat_error"]
    assert len(errors) == 2


def test_build_heartbeat_job_none_when_url_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("OPS_HEARTBEAT_URL", raising=False)
    j = Journal(str(tmp_path / "j.sqlite"))
    assert _build_heartbeat_job(_guardian(None), j) is None


def test_build_heartbeat_job_callable_when_url_set(monkeypatch, tmp_path):
    monkeypatch.setenv("OPS_HEARTBEAT_URL", URL)
    j = Journal(str(tmp_path / "j.sqlite"))
    assert callable(_build_heartbeat_job(_guardian(None), j))


def _scheduler_stubs(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    orchestrator = SimpleNamespace(tick=lambda: None)
    guardian = SimpleNamespace(check_stops_once=lambda: None)
    dispatcher = SimpleNamespace(dispatch_once=lambda: 0)
    broker = SimpleNamespace()
    return j, orchestrator, guardian, dispatcher, broker


def test_full_scheduler_registers_heartbeat_job_when_configured(tmp_path):
    j, orch, guardian, dispatcher, broker = _scheduler_stubs(tmp_path)
    sched = _start_full_scheduler(
        orch, guardian, dispatcher, j, broker, heartbeat_job=lambda: None,
    )
    try:
        assert sched.get_job("heartbeat") is not None
    finally:
        sched.shutdown(wait=False)


def test_full_scheduler_no_heartbeat_job_by_default(tmp_path):
    j, orch, guardian, dispatcher, broker = _scheduler_stubs(tmp_path)
    sched = _start_full_scheduler(orch, guardian, dispatcher, j, broker)
    try:
        assert sched.get_job("heartbeat") is None
    finally:
        sched.shutdown(wait=False)


def test_guardian_only_scheduler_registers_heartbeat_job_when_configured(tmp_path):
    """The reconcile-halt (guardian-only) mode is exactly when external
    liveness matters most — the heartbeat must be wired there too."""
    j, orch, guardian, dispatcher, broker = _scheduler_stubs(tmp_path)
    sched = _start_guardian_only(guardian, dispatcher, heartbeat_job=lambda: None)
    try:
        assert sched.get_job("heartbeat") is not None
    finally:
        sched.shutdown(wait=False)


def test_guardian_only_scheduler_no_heartbeat_job_by_default(tmp_path):
    j, orch, guardian, dispatcher, broker = _scheduler_stubs(tmp_path)
    sched = _start_guardian_only(guardian, dispatcher)
    try:
        assert sched.get_job("heartbeat") is None
    finally:
        sched.shutdown(wait=False)


def test_heartbeat_error_policy_email_throttled():
    from ops.notify.policy import POLICY

    entry = POLICY["heartbeat_error"]
    assert entry.channels == ("email",)
    assert entry.cooldown_seconds == 600
