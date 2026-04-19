"""
Run the libfss seed-expansion microbenchmarks for Standard (CTR PRG) and HT
(half-tree expand), then print a side-by-side comparison of matching rows.

The Rust programs print fixed-width tables; this script parses the numeric
columns from the end of each data line (same layout in both examples).

Usage (from the parent of bench_compare/, or any cwd):

    python bench_compare\\bench_expand_compare.py
    python bench_compare\\bench_expand_compare.py --build
    python bench_compare\\bench_expand_compare.py --echo --list-rows

Requirements: Python >= 3.9, `cargo` on PATH.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


THIS_FILE = Path(__file__).resolve()
CODE_ROOT = THIS_FILE.parent.parent

# (display name, Standard bench_expand label, HT bench_ht_expand label).
# Labels must match the string literals passed to print_row / print_row_delta.
COMPARISON_ROWS: list[tuple[str, str, str]] = [
    ("Full expand (no TLS)", "Full expansion (stack-local)", "Full expand: sigma + AES + MMO + children"),
    ("Full expand (TLS)", "Full expansion (TLS, s.expand())", "Full expand (TLS): H_S_tls + children"),
    ("Isolated: mask / sigma", "I1: mask seed", "I1: sigma"),
    ("Isolated: AES block", "I4: AES encrypt_block (one block)", "I2: AES encrypt_block"),
    ("Isolated: 16-byte XOR / MMO", "I5: 16-byte XOR (MMO finalize in refill)", "I3: MMO XOR (16-byte XOR)"),
    ("Isolated: left child copy", "I6: copy_from_slice (one 16-byte block)", "I4: left child (copy_from_slice)"),
]

# Trailing data columns: total ms (3 decimals), per-call ns (1 decimal), optional delta (+/- 1 decimal).
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
    """Map row label -> {total_ms, per_ns, delta_ns?}."""
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
        m = _ROW_NUMS_RE.search(raw)
        if not m:
            continue
        label = raw[: m.start()].rstrip()
        row: dict[str, float] = {
            "total_ms": float(m.group("total")),
            "per_ns": float(m.group("pcall")),
        }
        if m.group("delta") is not None:
            row["delta_ns"] = float(m.group("delta"))
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
    for ex in examples:
        subprocess.check_call(
            [
                cargo,
                "build",
                "--release",
                "--example",
                ex,
                "--manifest-path",
                str(manifest),
            ],
            cwd=str(manifest.parent),
        )


def _pct_ht_faster(std_ns: float, ht_ns: float) -> Optional[float]:
    """Percent lower latency for HT vs STD: (STD - HT) / STD * 100."""
    if std_ns <= 0:
        return None
    return (std_ns - ht_ns) / std_ns * 100.0


def main() -> None:
    p = argparse.ArgumentParser(description="Compare Standard vs HT libfss expand benchmarks.")
    p.add_argument(
        "--build",
        action="store_true",
        help="Run `cargo build --release` for both examples before benchmarking.",
    )
    p.add_argument(
        "--echo",
        action="store_true",
        help="Print each program's full stdout before the comparison table.",
    )
    p.add_argument(
        "--list-rows",
        action="store_true",
        help="After the table, list parsed row labels that are not part of the fixed comparison map.",
    )
    args = p.parse_args()

    std_manifest = _libfss_manifest("Standard")
    ht_manifest = _libfss_manifest("HT")
    for path, name in [(std_manifest, "Standard libfss"), (ht_manifest, "HT libfss")]:
        if not path.is_file():
            print(f"error: missing {name} Cargo.toml: {path}", file=sys.stderr)
            sys.exit(1)

    if args.build:
        _build_examples(std_manifest, ["bench_expand"])
        _build_examples(ht_manifest, ["bench_ht_expand"])

    std_out = _run_example(std_manifest, "bench_expand")
    ht_out = _run_example(ht_manifest, "bench_ht_expand")

    if args.echo:
        print("======== Standard (bench_expand) ========")
        print(std_out, end="" if std_out.endswith("\n") else "\n")
        print("======== HT (bench_ht_expand) ===========")
        print(ht_out, end="" if ht_out.endswith("\n") else "\n")
        print()

    std_rows = _parse_table(std_out)
    ht_rows = _parse_table(ht_out)

    w = 42
    print(f"{'Metric':<{w}} {'STD ns/call':>14} {'HT ns/call':>14} {'HT-STD (ns)':>14} {'HT faster %':>14}")
    print("-" * (w + 14 * 3 + 14 + 3))

    missing: list[str] = []
    for display, std_lbl, ht_lbl in COMPARISON_ROWS:
        sr = std_rows.get(std_lbl)
        hr = ht_rows.get(ht_lbl)
        if not sr or not hr:
            missing.append(f"{display}: STD={bool(sr)} HT={bool(hr)}")
            continue
        s_ns = sr["per_ns"]
        h_ns = hr["per_ns"]
        delta = h_ns - s_ns
        pct = _pct_ht_faster(s_ns, h_ns)
        pct_s = f"{pct:+.1f}" if pct is not None else "n/a"
        print(f"{display:<{w}} {s_ns:>14.1f} {h_ns:>14.1f} {delta:>+14.1f} {pct_s:>14}")

    if missing:
        print("\n(warning) Some comparison rows were skipped (label mismatch or parse miss):")
        for m in missing:
            print(f"  - {m}")

    # Labels present in one run but not used in the pairing table (debug aid).
    if args.list_rows:
        std_only = set(std_rows) - {t[1] for t in COMPARISON_ROWS}
        ht_only = set(ht_rows) - {t[2] for t in COMPARISON_ROWS}
        if std_only or ht_only:
            print("\n(parsed labels not in COMPARISON_ROWS; add tuples to extend the table)")
            for lbl in sorted(std_only):
                print(f"  [STD] {lbl!r}")
            for lbl in sorted(ht_only):
                print(f"  [HT]  {lbl!r}")


if __name__ == "__main__":
    main()
