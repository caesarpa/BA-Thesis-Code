# bench_runner

External benchmark driver for the `FSS-KRE-master/frontend` protocol.

It spawns `frontend 0` (server) and `frontend 1` (client) as child processes,
parses their stdout for the `Offline key generation time`, `Computation time`,
`Online rounds`, and `Communication volume` lines, and appends one CSV row per
(run, party) to `results.csv`.

The scope of each run (which protocol, which `input_size`) is determined by
`FSS-KRE-master/frontend/src/main.rs`; the runner does not override it. Adjust
`BENCHMARK_PROTOCOL_TYPES` / `INPUT_PARAMETERS` in that file first if you want a
different scope.

## Prerequisites

- Python 3.9+
- A built release binary at
  `FSS-KRE-master/frontend/target/release/frontend.exe`
  (pass `--build` to the runner to build it for you).

## Usage

From the workspace root (PowerShell):

```powershell
# 100 runs, first one treated as warmup, CSV at bench_runner/results.csv
python bench_runner\run.py --runs 100

# Build the release binary first, then run 50 repetitions, echoing live output
python bench_runner\run.py --build --runs 50 --echo

# Custom output path
python bench_runner\run.py --runs 100 --out bench_runner\results_bitmax_50k.csv
```

## CSV schema

| column          | meaning |
|-----------------|---------|
| `run_idx`       | 1-based index of the (server, client) pair |
| `party`         | `"server"` or `"client"` |
| `warmup`        | `True` if this row is within the first `--warmup` runs |
| `offline_ns` / `offline_ms` | offline key generation time on this party |
| `online_ns` / `online_ms`   | online computation time reported by `NetInterface::print_benchmarking` |
| `online_rounds` | communication rounds in the online phase |
| `comm_bytes`    | bytes received by this party during the online phase |
| `wall_ns`       | wall-clock time of the full (offline + online) run measured by the runner |

## Notes

- Each run spawns a fresh pair of processes, so the measurements include no
  carry-over from previous iterations (fresh allocators, caches, etc.).
- Between runs we sleep briefly (`--inter-run-sleep`) so the OS can release the
  TCP port (the server rebinds `127.0.0.1:8088` on every run).
- If a run fails (non-zero exit, startup timeout, …) the runner logs it and
  continues with the next run; the failed run is simply absent from the CSV.
- A final summary (mean / stddev / min / max per party and metric) is printed
  to stdout once all runs have finished.
