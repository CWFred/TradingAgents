# ds4 / DwarfStar backend (DeepSeek V4 Flash)

Run the TradingAgents pipeline against a local [ds4/DwarfStar](https://github.com/) server
serving **DeepSeek V4 Flash**. `ds4-server` speaks the OpenAI-compatible API, so the
pipeline needs **no code changes** — only configuration — plus an optional ops
integration that starts/stops the server automatically around each analysis.

- [Prerequisites](#prerequisites)
- [Build the server](#build-the-server)
- [Manual use (one-off runs)](#manual-use-one-off-runs)
- [Managed backend (automatic in the ops service)](#managed-backend-automatic-in-the-ops-service)
- [Gotchas](#gotchas)

---

## Prerequisites

- The ds4 checkout at `~/Code/ds4` (override with `DS4_DIR`), containing the
  `ds4flash.gguf` model (DeepSeek V4 Flash IQ2XXS, ~86 GB) and its source.
- A Mac with enough unified memory to hold the model resident (validated on a
  128 GB M5 Max). See the [RAM gotcha](#gotchas) — do **not** run it alongside
  another large local model.
- Xcode command-line tools (for the Metal build).

## Build the server

```sh
cd ~/Code/ds4 && make -j8 ds4-server
```

Produces an arm64 `ds4-server` binary linked against Metal. The managed backend
can do this for you automatically (see `DS4_BUILD_IF_MISSING`).

## Manual use (one-off runs)

Launch the server (loads the model into residency; takes ~10–20 s):

```sh
cd ~/Code/ds4 && ./ds4-server -m ds4flash.gguf --metal --ctx 100000 \
  --kv-disk-dir ~/.ds4/server-kv --kv-disk-space-mb 8192 \
  --host 127.0.0.1 --port 8000
```

Point TradingAgents at it (`.env`):

```bash
TRADINGAGENTS_LLM_PROVIDER=openai_compatible
TRADINGAGENTS_LLM_BACKEND_URL=http://127.0.0.1:8000/v1
OPENAI_COMPATIBLE_API_KEY=ds4          # keyless server; any placeholder works
TRADINGAGENTS_DEEP_THINK_LLM=deepseek-v4-flash
TRADINGAGENTS_QUICK_THINK_LLM=deepseek-v4-flash
```

Verify and run:

```sh
curl http://127.0.0.1:8000/v1/models          # -> deepseek-v4-flash, deepseek-v4-pro
.venv/bin/python main.py                       # full NFLX pipeline
```

Both `deepseek-v4-flash` and `deepseek-v4-pro` are aliases for whichever GGUF was
loaded with `-m`; the endpoint name does not select a different model.

## Managed backend (automatic in the ops service)

The ops service can bring ds4 up when an analysis is about to run and tear it
down afterwards, so its ~86 GB is only resident while it's actually needed. This
is **off by default** — hosted-API and LM Studio setups are unaffected.

Enable it and configure via environment variables:

| Env var | Default | Meaning |
|---|---|---|
| `OPS_LLM_MANAGED_BACKEND` | *(unset)* | Set to `ds4` to enable. Anything else / unset = disabled (no-op). |
| `DS4_DIR` | `~/Code/ds4` | ds4 checkout directory. |
| `DS4_MODEL` | `ds4flash.gguf` | GGUF passed to `-m`. |
| `DS4_HOST` | `127.0.0.1` | Bind address. |
| `DS4_PORT` | `8000` | Bind port (must match `TRADINGAGENTS_LLM_BACKEND_URL`). |
| `DS4_CTX` | `100000` | Context tokens. DeepSeek's compressed KV makes this cheap (~1.9 GB). |
| `DS4_KV_DIR` | `~/.ds4/server-kv` | Disk KV checkpoint directory. |
| `DS4_KV_MB` | `8192` | Disk KV budget (MB). |
| `DS4_LMS_PATH` | `~/.lmstudio/bin/lms` | LM Studio CLI, used to unload its models before launch. |
| `DS4_BUILD_IF_MISSING` | `true` | Build `ds4-server` via `make -j8` if the binary is absent. |
| `DS4_STARTUP_TIMEOUT_S` | `180` | Seconds to wait for `/v1/models` to answer before failing. |

You still set the `TRADINGAGENTS_*` `.env` values above so the pipeline connects
to the right endpoint; the managed backend only handles the server's lifecycle.

### What it does, and when

On the first `propagate()` of an orchestrator tick (i.e. the first time an
analysis actually runs), `ensure_up()`:

1. Runs `lms unload --all` to free LM Studio (best effort — a missing CLI or
   "nothing loaded" is fine).
2. Builds `ds4-server` if the binary is missing (unless `DS4_BUILD_IF_MISSING`
   is false).
3. Launches the server and polls `/v1/models` until it answers (or times out).

When the tick's analysis batch ends, the pipeline's `session()` context manager
calls `shutdown()`, stopping the server and freeing its memory. `ops run` also
calls `shutdown()` on service exit as an idempotent safety net.

**Ownership rule:** if `ensure_up()` finds the port already serving — because you
started ds4 by hand — it uses that server and **never stops it** on shutdown. It
only tears down a server it started itself.

**Error posture:** if the server can't be built/launched/reached, `ensure_up()`
raises; the orchestrator's tick handler journals `orchestrator_tick_error` and
skips that tick — the service stays alive and teardown still runs.

### Lifecycle boundary

Teardown is **per analysis batch (per tick)**: the model is loaded when a tick
has candidates to analyze and unloaded when that tick finishes, so it does not
sit resident between the scheduler's :00/:30 ticks. The cost is a ~10–20 s model
load each time a tick actually analyzes.

## Gotchas

1. **RAM — this has crashed the machine.** Full residency loads all ~86 GB. Do
   not run ds4 alongside another large local model; LM Studio holding a ~60 GB
   model plus ds4 exceeded 128 GB and caused a kernel panic. The managed backend
   runs `lms unload --all` before launching to avoid exactly this. For tight
   memory, ds4 also supports `--ssd-streaming` (not used by the managed backend
   by default).
2. **Context is not the bottleneck.** DeepSeek's compressed KV cache means a
   100k-token window costs only ~1.9 GB — unlike LM Studio, where a small
   default context window truncates the pipeline mid-run.
3. **Slow and single-threaded.** `ds4-server` serializes all requests through one
   graph worker (no batching), and DeepSeek V4 Flash is a heavy reasoner
   (~30 tok/s with long "thinking" traces per node), so a full pipeline run takes
   tens of minutes. The Python pipeline log only writes when a node *finishes*;
   to confirm it's working rather than hung, watch `~/Code/ds4/ds4-server.log`
   for live `gen=` token progress.
