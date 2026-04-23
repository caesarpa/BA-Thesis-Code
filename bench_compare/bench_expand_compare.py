"""
Paired, alternating benchmark runner for the libfss seed-expansion examples.

Unlike the old single-shot comparison, this runner executes the Standard and HT
expand examples repeatedly, alternates them round by round, writes normalized
rows to CSV, and prints aggregated mean / stddev summaries.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path


THIS_FILE = Path(__file__).resolve()
CODE_ROOT = THIS_FILE.parent.parent

PROJECTS: list[tuple[str, str, str]] = [
    ("HT", "HT", "bench_ht_expand"),
    ("STD", "Standard", "bench_expand"),
]

# (display name, Standard row label, HT row label)
COMPARISON_ROWS: list[tuple[str, str, str]] = [
    ("Full expand (no TLS)", "Full expansion (stack-local)", "Full expand: sigma + AES + MMO + children"),
    ("Full expand (TLS)", "Full expansion (TLS, s.expand())", "Full expand (TLS): H_S_tls + children"),
    ("Isolated: mask / sigma", "I1: mask seed", "I1: sigma"),
    ("Isolated: AES block", "I4: AES encrypt_block (one block)", "I2: AES encrypt_block"),
    ("Isolated: 16-byte XOR / MMO", "I5: 16-byte XOR (MMO finalize in refill)", "I3: MMO XOR (16-byte XOR)"),
    ("Isolated: left child copy", "I6: copy_from_slice (one 16-byte block)", "I4: left child (copy_from_slice)"),
]

_ROW_NUMS_RE = re.compile(
    r"\s+(?P<total>\d+\.\d{3})\s+(?P<pcall>\d+\.\d)(?:\s+(?P<delta>[+-]\d+\.\d))?\s*$"
)


def _libfss_manifest(project_folder: str) -> Path:
    return (
        CODE_ROOT
        / project_folder
        / "BA-Thesis-Code"
        / "FSS-KRE-master"
        / "libfss"
        / "Cargo.toml"
    )


def _parse_table(text: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        raw = line.rstrip("\r\n")
        if not raw.strip():
            continue
        if raw.strip().startswith("---") or raw.strip().startswith("==="):
            continue
        if "Total (ms)" in raw and "Per-call" in raw:
            continue
        if set(raw.strip()) == {"-"}:
            continue
        match = _ROW_NUMS_RE.search(raw)
        if not match:
            continue
        label = raw[: match.start()].rstrip()
        row: dict[str, float] = {
            "total_ms": float(match.group("total")),
            "per_ns": float(match.group("pcall")),
        }
        if match.group("delta") is not None:
            row["delta_ns"] = float(match.group("delta"))
        out[label] = row
    return out


def _run_example(manifest: Path, example: str) -> str:
    cargo = shutil.which("cargo")
    if not cargo:
        print("error: `cargo` not found on PATH", file=sys.stderr)
        sys.exit(1)

    cmd = [
        cargo,
        "run",
        "--release",
        "--example",
        example,
        "--manifest-path",
        str(manifest),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(manifest.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        print(proc.stdout, end="", file=sys.stderr)
        print(proc.stderr, end="", file=sys.stderr)
        print(f"error: {' '.join(cmd)} failed with exit {proc.returncode}", file=sys.stderr)
        sys.exit(proc.returncode)
    return proc.stdout


def _build_examples(manifest: Path, examples: list[str]) -> None:
    cargo = shutil.which("cargo")
    if not cargo:
        print("error: `cargo` not found on PATH", file=sys.stderr)
        sys.exit(1)

    for example in examples:
        subprocess.check_call(
            [
                cargo,
                "build",
                "--release",
                "--example",
                example,
                "--manifest-path",
                str(manifest),
            ],
            cwd=str(manifest.parent),
        )


def _rows_from_output(
    run_idx: int,
    is_warmup: bool,
    project_tag: str,
    parsed_rows: dict[str, dict[str, float]],
) -> tuple[list[dict[str, object]], list[str]]:
    rows: list[dict[str, object]] = []
    missing: list[str] = []

    for metric, std_label, ht_label in COMPARISON_ROWS:
        raw_label = ht_label if project_tag == "HT" else std_label
        parsed = parsed_rows.get(raw_label)
        if not parsed:
            missing.append(f"{project_tag}:{metric} -> {raw_label}")
            continue

        rows.append(
            {
                "run_idx": run_idx,
                "warmup": is_warmup,
                "project": project_tag,
                "metric": metric,
                "raw_label": raw_label,
                "total_ms": f"{parsed['total_ms']:.3f}",
                "per_ns": f"{parsed['per_ns']:.1f}",
                "delta_ns": f"{parsed['delta_ns']:.1f}" if "delta_ns" in parsed else "",
            }
        )

    return rows, missing


def _truthy(s: str) -> bool:
    return s.strip().lower() in ("1", "true", "yes", "y", "t")


def load_rows_from_csv(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Cannot summarize: no results CSV at {path}. Run the benchmark first."
        )

    rows: list[dict[str, object]] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["warmup"] = _truthy(row.get("warmup", "False"))
            rows.append(row)
    return rows


def _stats(values: list[float]) -> tuple[int, float, float, float, float]:
    count = len(values)
    if count == 0:
        return (0, 0.0, 0.0, 0.0, 0.0)

    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / count
    sd = math.sqrt(variance)
    return (count, mean, sd, min(values), max(values))


def _fmt_stats(values: list[float]) -> str:
    count, mean, sd, lo, hi = _stats(values)
    if count == 0:
        return "n=0"
    return (
        f"n={count:<3} mean={mean:>7.2f} ns  "
        f"sd={sd:>6.2f} ns  "
        f"min={lo:>7.2f} ns  "
        f"max={hi:>7.2f} ns"
    )


def _vals(rows: list[dict[str, object]], metric: str, project: str) -> list[float]:
    return [
        float(row["per_ns"])
        for row in rows
        if row["metric"] == metric and row["project"] == project
    ]


def _paired_diffs(
    rows: list[dict[str, object]], metric: str, faster: str, slower: str
) -> list[float]:
    by_run: dict[int, dict[str, float]] = {}
    for row in rows:
        if row["metric"] != metric:
            continue
        run_idx = int(row["run_idx"])
        by_run.setdefault(run_idx, {})[str(row["project"])] = float(row["per_ns"])

    diffs: list[float] = []
    for run_values in by_run.values():
        if faster in run_values and slower in run_values:
            diffs.append(run_values[slower] - run_values[faster])
    return diffs


def print_summary(rows: list[dict[str, object]]) -> None:
    measured = [row for row in rows if row["warmup"] is False]
    if not measured:
        print("[expand] No non-warmup measurements.")
        return

    print("\n[expand] ===== Paired summary (warmup excluded) =====")
    print("[expand] Positive paired delta means HT is faster than Standard.")

    for metric, _, _ in COMPARISON_ROWS:
        ht_vals = _vals(measured, metric, "HT")
        std_vals = _vals(measured, metric, "STD")
        paired = _paired_diffs(measured, metric, "HT", "STD")

        print(f"\n  {metric}:")
        print(f"    HT   {_fmt_stats(ht_vals)}")
        print(f"    STD  {_fmt_stats(std_vals)}")

        pair_n, pair_mean, pair_sd, pair_lo, pair_hi = _stats(paired)
        _, ht_mean, _, _, _ = _stats(ht_vals)
        _, std_mean, _, _, _ = _stats(std_vals)
        pct = ((std_mean - ht_mean) / std_mean * 100.0) if std_mean > 0 else None
        pct_s = f"{pct:+.2f}%" if pct is not None else "n/a"

        if pair_n == 0:
            print("    delta n=0")
        else:
            print(
                "    delta "
                f"n={pair_n:<3} mean={pair_mean:+7.2f} ns  "
                f"sd={pair_sd:>6.2f} ns  "
                f"min={pair_lo:+7.2f} ns  "
                f"max={pair_hi:+7.2f} ns  "
                f"HT faster={pct_s}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repeat and compare Standard vs HT libfss expand benchmarks."
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=30,
        help="Measured rounds per project. Default: 30",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Warmup rounds per project written to CSV but excluded from summary. Default: 1",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Run `cargo build --release` for both examples before benchmarking.",
    )
    parser.add_argument(
        "--echo",
        action="store_true",
        help="Print each program's full stdout during the run.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=THIS_FILE.parent / "expand_results.csv",
        help="Output CSV path. Default: bench_compare/expand_results.csv",
    )
    parser.add_argument(
        "--list-rows",
        action="store_true",
        help="Print parsed raw labels seen in the last round for each project.",
    )
    parser.add_argument(
        "--summarize",
        action="store_true",
        help="Do not run benchmarks; summarize the CSV at --out.",
    )
    args = parser.parse_args()

    std_manifest = _libfss_manifest("Standard")
    ht_manifest = _libfss_manifest("HT")
    manifests = {"STD": std_manifest, "HT": ht_manifest}

    for path, name in ((std_manifest, "Standard libfss"), (ht_manifest, "HT libfss")):
        if not path.is_file():
            print(f"error: missing {name} Cargo.toml: {path}", file=sys.stderr)
            sys.exit(1)

    if args.summarize:
        rows = load_rows_from_csv(args.out)
        print(f"[expand] Loaded {len(rows)} rows from {args.out}")
        print_summary(rows)
        return

    if args.build:
        _build_examples(std_manifest, ["bench_expand"])
        _build_examples(ht_manifest, ["bench_ht_expand"])

    args.out.parent.mkdir(parents=True, exist_ok=True)

    headers = [
        "run_idx",
        "warmup",
        "project",
        "metric",
        "raw_label",
        "total_ms",
        "per_ns",
        "delta_ns",
    ]

    all_rows: list[dict[str, object]] = []
    last_seen_labels: dict[str, list[str]] = {}
    total_rounds = args.warmup + args.runs

    print(f"[expand] Writing results to {args.out}")
    print("[expand] Interleave order per round: HT -> STD")
    print(
        f"[expand] Total rounds per project: {total_rounds} "
        f"({args.warmup} warmup + {args.runs} measured)"
    )

    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()

        for run_idx in range(1, total_rounds + 1):
            is_warmup = run_idx <= args.warmup
            warmup_tag = " (warmup)" if is_warmup else ""
            print(f"[expand] === round {run_idx}/{total_rounds}{warmup_tag} ===")

            for project_tag, _, example in PROJECTS:
                manifest = manifests[project_tag]
                stdout = _run_example(manifest, example)

                if args.echo:
                    print(f"======== {project_tag} / {example} / round {run_idx} ========")
                    print(stdout, end="" if stdout.endswith("\n") else "\n")

                parsed_rows = _parse_table(stdout)
                last_seen_labels[project_tag] = sorted(parsed_rows)
                normalized_rows, missing = _rows_from_output(
                    run_idx=run_idx,
                    is_warmup=is_warmup,
                    project_tag=project_tag,
                    parsed_rows=parsed_rows,
                )

                for row in normalized_rows:
                    writer.writerow(row)
                    all_rows.append(row)
                f.flush()

                if missing:
                    print(
                        f"[expand] warning: skipped {len(missing)} rows for {project_tag}: "
                        + "; ".join(missing),
                        file=sys.stderr,
                    )

                full_rows = [
                    row for row in normalized_rows if row["metric"] == "Full expand (no TLS)"
                ]
                if full_rows:
                    print(
                        f"           [{project_tag}] Full expand (no TLS): "
                        f"{full_rows[0]['per_ns']} ns/call"
                    )

    print_summary(all_rows)

    if args.list_rows:
        print("\n[expand] Parsed labels from the last run:")
        for project_tag in ("HT", "STD"):
            print(f"  {project_tag}:")
            for label in last_seen_labels.get(project_tag, []):
                print(f"    {label!r}")


if __name__ == "__main__":
    main()
