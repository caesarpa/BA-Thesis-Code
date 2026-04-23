# bench_compare

Alternating, paired benchmark runners for comparing **HT** vs **Standard**
inside `C:\Users\Paul\Desktop\Thesis\Code`.

## Why this exists

Running all HT repetitions first and all Standard repetitions second makes the
comparison vulnerable to unrelated machine noise. These runners interleave the
two projects round by round:

    round 1: HT -> STD
    round 2: HT -> STD
    round 3: HT -> STD

That keeps disturbances much more symmetric and makes the per-round comparison
more trustworthy.

## Files

`bench_compare\run.py`
Protocol benchmark runner for the frontend binaries. Writes `results.csv`.

`bench_compare\bench_expand_compare.py`
Seed-expansion microbenchmark runner for the libfss example binaries. Writes
`expand_results.csv`.

`bench_compare\run_external_fss.py`
Single-project runner for the external checkout at
`C:\Users\Paul\Desktop\FSS-KRE-master`. Writes both CSV and XLSX output.

## Requirements

- Python 3.9+
- `cargo` on PATH
- Built release binaries, or pass `--build`

## Frontend Benchmark

```powershell
python bench_compare\run.py --runs 100 --out bench_compare\results.csv
python bench_compare\run.py --runs 100 --build
python bench_compare\run.py --runs 2 --warmup 0 --echo
python bench_compare\run.py --summarize --out bench_compare\results.csv
```

The frontend runner writes one CSV row per `(round, project, party)` and prints
`n / mean / sd / min / max` summaries at the end.

## Expand Benchmark

`bench_expand_compare.py` repeatedly runs:

- `Standard/.../libfss/examples/bench_expand.rs`
- `HT/.../libfss/examples/bench_ht_expand.rs`

It normalizes the matched benchmark rows across the two example outputs, writes
one CSV row per `(round, project, metric)`, and prints aggregate statistics for
each matched metric.

```powershell
python bench_compare\bench_expand_compare.py
python bench_compare\bench_expand_compare.py --build
python bench_compare\bench_expand_compare.py --runs 30 --warmup 1 --out bench_compare\expand_results.csv
python bench_compare\bench_expand_compare.py --summarize --out bench_compare\expand_results.csv
python bench_compare\bench_expand_compare.py --echo --list-rows
```

The summary reports:

- `HT` stats and `STD` stats for each metric
- a paired delta over matched rounds
- `HT faster % = (STD - HT) / STD * 100`

Use `--list-rows` to print the raw labels parsed from the last round if you
want to extend the comparison map in the script.

## External Project Benchmark

`run_external_fss.py` benchmarks the separate project at
`C:\Users\Paul\Desktop\FSS-KRE-master` using whatever protocol and input size
that checkout currently selects in `frontend/src/main.rs`. It launches one
fresh `(server, client)` pair per run, saves raw rows to CSV, and writes an
Excel workbook with a `Runs` sheet and a `Summary` sheet containing mean and
standard deviation.

```powershell
python bench_compare\run_external_fss.py --runs 30
python bench_compare\run_external_fss.py --runs 30 --build
python bench_compare\run_external_fss.py --runs 30 --out-xlsx bench_compare\external_fss_results.xlsx
```
