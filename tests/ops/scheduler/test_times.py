"""One source of truth for scheduler cron facts (main.py + forecast)."""
from ops.scheduler import times


def test_constants():
    assert times.TICK_MINUTES == (0, 30)
    assert times.TICK_HOUR_START == 9
    assert times.TICK_HOUR_END == 15
    assert times.TICK_CRON_MINUTE == "0,30"
    assert times.TICK_CRON_HOUR == "9-15"
    assert times.TICK_CRON_DOW == "mon-fri"


def test_cron_strings_match_tuples():
    assert times.TICK_CRON_MINUTE == ",".join(str(m) for m in times.TICK_MINUTES)
    assert times.TICK_CRON_HOUR == f"{times.TICK_HOUR_START}-{times.TICK_HOUR_END}"


def test_main_uses_the_constants():
    import inspect
    from ops import main
    src = inspect.getsource(main._start_full_scheduler)
    assert "times.TICK_CRON_MINUTE" in src
    assert "times.TICK_CRON_HOUR" in src
