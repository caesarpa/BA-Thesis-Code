# bench_compare

Alternating, paired benchmark runner for **HT** vs **Standard** FSS-KRE.

## Why this exists

The per-project `bench_runner/run.py` inside `HT/` and `Standard/` works
great when the system is quiet, but any external disturbance that happens
during just one of the two batches (a CPU spike, an AV scan, a Windows
Update worker, a browser tab waking up) attaches entirely to one side and
shows up as a fake speedup or slowdown. This runner instead interleaves the
two projects:

    round 1:  HT  ->  STD
    round 2:  HT  ->  STD
    round 3:  HT  ->  STD
    ...

so that any such disturbance tends to land on *two consecutive rows* — one
HT, one STD — and cancels out when you pair them.

## Layout

    Code/
    ├── HT/
    │   └── BA-Thesis-Code/FSS-KRE-master/frontend/target/release/frontend.exe
    ├── Standard/
    │   └── BA-Thesis-Code/FSS-KRE-master/frontend/target/release/frontend.exe
    └── bench_compare/
        ├── run.py
        ├── bench_expand_compare.py
        ├── README.md
        └── results.csv    (produced by run.py)

Each project's frontend is launched with `cwd` set to its own
`FSS-KRE-master/frontend/` directory, so the offline artifacts (`test/x*.bin`)
are written into each project's own `test/` folder and never clobber each
other.

## Requirements

- Python ≥ 3.9
- Prebuilt release binaries in both `HT/.../target/release/` and
  `Standard/.../target/release/`, or pass `--build` to build both.

## Usage

From `C:\Users\Paul\Desktop\Thesis\Code\`:

```powershell
# Default: 100 alternating rounds per project (=> 200 (server,client) pairs).
python bench_compare\run.py --runs 100 --out bench_compare\results.csv

# Build both projects first:
python bench_compare\run.py --runs 100 --build

# Echo each party's stdout (useful for the first smoke run):
python bench_compare\run.py --runs 2 --warmup 0 --echo

# Turn off per-stage timing if you only want outer totals:
python bench_compare\run.py --runs 100 --no-offline-timing --no-online-timing

# Re-print the aggregated summary from an existing CSV without running
# any new rounds (useful for re-inspecting old runs):
python bench_compare\run.py --summarize --out bench_compare\results.csv
```

### Expand microbenchmark (libfss)

`bench_expand_compare.py` runs the two **Rust example** binaries
`bench_expand` (Standard CTR PRG expansion) and `bench_ht_expand` (HT
half-tree expansion), then prints a small table of **paired** rows
(full expand with and without TLS, and a few isolated primitives).
Paths are resolved from this folder’s parent (`Code/`), so the command
works from any working directory.

```powershell
python bench_compare\bench_expand_compare.py
python bench_compare\bench_expand_compare.py --build
python bench_compare\bench_expand_compare.py --echo --list-rows
```

The **HT faster %** column is `(STD - HT) / STD * 100` when latency is
lower-is-better (same convention as the `run.py` summary).
Requires `cargo` on PATH. Use `--list-rows` to see parsed benchmark labels
you can add to `COMPARISON_ROWS` inside the script.

Per-stage timing (`[offline] B1a α PRG draw ...`, `[online] O4a ...`, etc.)
is on by default — the runner sets `OFFLINE_TIMING=1` and `ONLINE_TIMING=1`
in each child process's environment. The per-stage columns match those of
the single-project `bench_runner/run.py`.

## CSV schema

One row per (round, project, party):

| column | notes |
|---|---|
| `run_idx` | 1-based round number |
| `project` | `HT` or `STD` |
| `party` | `server` or `client` |
| `warmup` | `True` if `run_idx <= --warmup`, excluded from summary |
| `offline_ns`, `offline_ms` | from `"Offline key generation time: ..."` |
| `online_ns`, `online_ms` | from `"Computation time: ..."` |
| `online_rounds` | from `"Online rounds: ..."` |
| `comm_bytes` | from `"Communication volume: ..."` |
| `wall_ns` | Python-side wall clock for this project's pair |
| `offline_b1a_alpha_prg_draw_ns` … `offline_m1m2_zc_dpf_ns` | per-stage offline (ns) |
| `online_o1_init_mask_prep_ns` … `online_o6_vidpf_verify_ns` | per-stage online (ns) |

## Summary

At the end the runner prints a side-by-side summary per party, per stage,
showing `n / mean / sd / min / max` for each project and the percentage
change of `STD` relative to `HT` for both `mean` and `min`. Stages that
never produced data are omitted.

## Analysis tips

- **Pair by `run_idx`.** With interleaved data you can (and should) do a
  paired analysis in pandas: join HT and STD rows on `(run_idx, party)`,
  compute per-round diffs, then summarize. That cancels most global noise.
- **Prefer `min` or a trimmed mean** when reporting stage-level results;
  mean/sd are sensitive to tail outliers, and you want to know the steady-
  state cost, not the OS-jitter tail.
- **Watch `wall_ns` vs `offline_ns + online_ns`.** A large gap is a run
  where the OS stole time between phases; flag those rounds as suspect on
  *both* projects (they're adjacent rows, so it's easy).
