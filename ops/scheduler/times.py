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

# The overnight research job fires every half hour all day; the deadline
# hour (config.research_drain_deadline_hour) bounds the actual window.
OVERNIGHT_CRON_MINUTE = TICK_CRON_MINUTE
