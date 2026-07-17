"""Scheduler cron facts shared by ops/main.py (job registration) and the
dashboard forecast (ops/dashboard/forecast.py). One source of truth so the
prediction can never drift from what actually fires. All times are
America/New_York (the BackgroundScheduler timezone)."""

# Orchestrator daily-cycle ticks: :00/:30, 09:00-15:30 ET, weekdays.
TICK_MINUTES = (0, 30)
TICK_HOUR_START = 9
TICK_HOUR_END = 15  # inclusive: last tick fires 15:30

TICK_CRON_MINUTE = ",".join(str(m) for m in TICK_MINUTES)
TICK_CRON_HOUR = f"{TICK_HOUR_START}-{TICK_HOUR_END}"
TICK_CRON_DOW = "mon-fri"

# The live-first background memo queue wakes frequently, then its policy gates
# work out of paper-trading hours. Once awake, it processes items sequentially
# until empty, paused, or at the pre-market deadline.
BACKGROUND_QUEUE_CRON_MINUTE = "*/5"
# Backward-compatible name for dashboard/tests that still describe this as the
# overnight queue.
OVERNIGHT_CRON_MINUTE = BACKGROUND_QUEUE_CRON_MINUTE
