# Dynamic memo and backtest queue

The always-on service coordinates background DS4 work as one live-first queue.
The sleeve databases remain the durable queue sources; no memo payload is copied
into a second scheduler database.

## Priority and safety

Work runs sequentially in this order:

1. research vetting and research screen-hit drain;
2. short vetting and short screen-hit drain;
3. insider memo-lite work;
4. explicitly enqueued backtest memo generation.

The queue wakes every five minutes. On weekdays it may use DS4 before the
configured pre-market deadline and after 16:45 America/New_York. It does no
background model work from the pre-market deadline through 16:45, protecting
the market-hours momentum cycle and the 16:20-16:35 sleeve/overview train.
Weekends remain available until Monday's pre-market deadline.

Guardian polling, monitoring, trading, exits, and notifications are never part
of this pause/queue boundary.

## Enqueueing backtests

Plan only; nothing will run automatically:

```bash
.venv/bin/python -m ops.cli backtest generate \
  --sleeve research --start 2023-01-01 --end 2025-06-01 --cases 40
```

Opt missing jobs into automatic backfill processing:

```bash
.venv/bin/python -m ops.cli backtest generate \
  --sleeve research --start 2023-01-01 --end 2025-06-01 --cases 40 --enqueue
```

`--execute` remains an immediate foreground run and cannot be combined with
`--enqueue`. Queue intent is persisted with each generation job, so daemon and
machine restarts do not lose it. Only one backtest memo is attempted per queue
pass, and live work is checked again before the next pass.

## Pausing and automatic resume

Indefinite pause:

```bash
.venv/bin/python -m ops.cli research pause
```

Three-hour pause with automatic resume:

```bash
.venv/bin/python -m ops.cli research pause --hours 3
```

Manual resume:

```bash
.venv/bin/python -m ops.cli research resume
```

The current name is allowed to finish before DS4 is released. Timed pause
leases survive daemon restarts and are removed by the worker after expiration.
Legacy empty pause files remain valid indefinite pauses.

## Learning boundary

Queueing increases corpus throughput but does not automatically change paper
sleeve prompts, sizing, exits, or lesson conditioning. Research backtests remain
control/treated experiments, and promotion into a paper sleeve is a separate
operator decision. The current backtest adapter supports the research sleeve;
short and insider work participate in live queue priority but require their own
PIT replay adapters before their memos can be backtested mechanically.

