"""
Paired, alternating benchmark runner for HT vs Standard FSS-KRE.

Launches one (server, client) pair of the HT frontend, then one pair of the
Standard frontend, then HT again, then Standard again, etc. — for a total of
`--runs` rounds per project. Each iteration's rows are written immediately to
a single shared CSV, tagged with the `project` column, so any external
disturbance (CPU spike, AV scan, scheduler glitch) tends to hit both projects
in consecutive rows and largely cancels when you pair the results.

Each project's frontend binary is launched with its own `cwd`, so each writes
its own `../test/x*.bin` offline artifacts (no clobbering between HT and STD).

The CSV schema and per-stage parsing mirror `bench_runner/run.py` inside each
project (same OFFLINE_STEPS / ONLINE_STEPS). We simply add a leading
`project` column.

Usage (PowerShell, from C:\\Users\\Paul\\Desktop\\Thesis\\Code\\):

    python bench_compare\\run.py --runs 100 --out bench_compare\\results.csv

Requirements:
- Python >= 3.9.
- Prebuilt release binaries already in place (or pass --build).
"""

from __future__ import annotations

import argparse
import csv
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Paths — absolute, so the runner works no matter where it's launched from.
# --------------------------------------------------------------------------- #

THIS_FILE = Path(__file__).resolve()
CODE_ROOT = THIS_FILE.parent.parent  # .../Desktop/Thesis/Code

_EXE_NAME = "frontend.exe" if sys.platform.startswith("win") else "frontend"


def _frontend_dir(project: str) -> Path:
    return (CODE_ROOT / project / "BA-Thesis-Code" / "FSS-KRE-master" / "frontend")


def _frontend_bin(project: str) -> Path:
    return _frontend_dir(project) / "target" / "release" / _EXE_NAME


def _project_root(project: str) -> Path:
    return CODE_ROOT / project / "BA-Thesis-Code" / "FSS-KRE-master"


# Ordered list of (tag, project_folder_name). The runner loops over this list
# in order inside each round, so changing this list changes the interleave
# order. With two entries the pattern is ABABAB... as the user requested.
PROJECTS: list[tuple[str, str]] = [
    ("HT",  "HT"),
    ("STD", "Standard"),
]


# --------------------------------------------------------------------------- #
# Duration parsing — matches Rust's `Duration` Debug formatter.
# --------------------------------------------------------------------------- #

_DUR_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ns|us|\u00b5s|ms|s)\b"
)

_UNIT_TO_NS = {
    "ns": 1.0,
    "us": 1e3,
    "\u00b5s": 1e3,
    "ms": 1e6,
    "s":  1e9,
}


def parse_rust_duration_ns(text: str) -> Optional[float]:
    m = _DUR_RE.search(text)
    if not m:
        return None
    return float(m.group("value")) * _UNIT_TO_NS[m.group("unit")]


# --------------------------------------------------------------------------- #
# Per-stage timing labels (must match the Rust-side `offline_step` /
# `online_step` calls in libmpc/src/offline_data*.rs and
# libmpc/src/protocols/bitwise_max.rs).
# --------------------------------------------------------------------------- #

OFFLINE_STEPS: list[tuple[str, str]] = [
    ("B1a \u03b1 PRG draw",   "offline_b1a_alpha_prg_draw_ns"),
    ("B2a IDPF gen (mem)",    "offline_b2a_idpf_gen_mem_ns"),
    ("B2a1 IDPF expand",      "offline_b2a1_idpf_expand_ns"),
    ("B2a2 IDPF convert+CW",  "offline_b2a2_idpf_convert_cw_ns"),
    ("B2a3 IDPF tag hash",    "offline_b2a3_idpf_tag_hash_ns"),
    ("B2b IDPF write (disk)", "offline_b2b_idpf_write_disk_ns"),
    ("B1b \u03b1 shares",     "offline_b1b_alpha_shares_ns"),
    ("B3 q-bool",             "offline_b3_q_bool_ns"),
    ("B4 q-arith (daBits)",   "offline_b4_q_arith_ns"),
    ("B5 Beavers",            "offline_b5_beavers_ns"),
    ("M1+M2 ZC-DPF",          "offline_m1m2_zc_dpf_ns"),
]

ONLINE_STEPS: list[tuple[str, str]] = [
    ("O1 init + mask prep",     "online_o1_init_mask_prep_ns"),
    ("O2 mask exchange",        "online_o2_mask_exchange_ns"),
    ("O3 round 0",              "online_o3_round_0_ns"),
    ("O3a round0: expand_dir",  "online_o3a_round0_expand_dir_ns"),
    ("O3b round0: convert+word", "online_o3b_round0_convert_word_ns"),
    ("O3c round0: tag update",  "online_o3c_round0_tag_update_ns"),
    ("O4a middle: IDPF eval",   "online_o4a_middle_idpf_ns"),
    ("O4a1 middle: expand_dir", "online_o4a1_middle_expand_dir_ns"),
    ("O4a2 middle: convert+word", "online_o4a2_middle_convert_word_ns"),
    ("O4a3 middle: tag update", "online_o4a3_middle_tag_update_ns"),
    ("O4b middle: algebra+net", "online_o4b_middle_algebra_net_ns"),
    ("O5 last round",           "online_o5_last_round_ns"),
    ("O6 VIDPF verify",         "online_o6_vidpf_verify_ns"),
]

_STEP_LABEL_TO_KEY: dict[str, str] = {
    label: key for label, key in OFFLINE_STEPS + ONLINE_STEPS
}

# Offline B-steps B1..B5 with B2a removed (B1a + B1b + B2b + B3 + B4 + B5); excludes M1+M2.
_OFFLINE_B_SUM_EX_B2A_KEYS: list[str] = [
    k
    for _, k in OFFLINE_STEPS
    if k not in {
        "offline_b2a_idpf_gen_mem_ns",
        "offline_b2a1_idpf_expand_ns",
        "offline_b2a2_idpf_convert_cw_ns",
        "offline_b2a3_idpf_tag_hash_ns",
        "offline_m1m2_zc_dpf_ns",
    }
]

# Online O1..O6 with O4a removed (parts of the protocol HT does not change).
_ONLINE_SUM_EX_O4A_KEYS: list[str] = [
    k
    for _, k in ONLINE_STEPS
    if k not in {
        "online_o3a_round0_expand_dir_ns",
        "online_o3b_round0_convert_word_ns",
        "online_o3c_round0_tag_update_ns",
        "online_o4a_middle_idpf_ns",
        "online_o4a1_middle_expand_dir_ns",
        "online_o4a2_middle_convert_word_ns",
        "online_o4a3_middle_tag_update_ns",
    }
]

# Reported top-line totals from the binary (same columns as written to CSV).
_REPORTED_OFFLINE_PLUS_ONLINE_KEYS: List[str] = ["offline_ns", "online_ns"]


# --------------------------------------------------------------------------- #
# Per-party output parsing
# --------------------------------------------------------------------------- #

@dataclass
class PartyResult:
    party: str
    offline_ns: Optional[float] = None
    online_ns: Optional[float] = None
    online_rounds: Optional[int] = None
    comm_bytes: Optional[int] = None
    steps_ns: dict[str, float] = field(default_factory=dict)
    raw_lines: list[str] = field(default_factory=list)


_RE_OFFLINE = re.compile(r"Offline key generation time:\s*(.+)$")
_RE_ROUNDS  = re.compile(r"Online rounds:\s*(\d+)")
_RE_COMM    = re.compile(r"Communication volume:\s*(\d+)\s*bytes")
_RE_COMP    = re.compile(r"Computation time:\s*(.+?)\s*$")

_RE_STEP = re.compile(
    r"^\s*\[(?P<kind>offline|online)\]\s+"
    r"(?P<label>.+?)\s+"
    r"(?P<dur>\d+(?:\.\d+)?\s*(?:ns|us|\u00b5s|ms|s))\s*$"
)


def update_party_from_line(result: PartyResult, line: str) -> None:
    result.raw_lines.append(line)

    if (m := _RE_OFFLINE.search(line)) and result.offline_ns is None:
        dur = parse_rust_duration_ns(m.group(1))
        if dur is not None:
            result.offline_ns = dur

    if (m := _RE_ROUNDS.search(line)) and result.online_rounds is None:
        result.online_rounds = int(m.group(1))

    if (m := _RE_COMM.search(line)) and result.comm_bytes is None:
        result.comm_bytes = int(m.group(1))

    if (m := _RE_COMP.search(line)) and result.online_ns is None:
        dur = parse_rust_duration_ns(m.group(1))
        if dur is not None:
            result.online_ns = dur

    if (m := _RE_STEP.match(line)):
        label = m.group("label").strip()
        key = _STEP_LABEL_TO_KEY.get(label)
        if key is not None:
            dur = parse_rust_duration_ns(m.group("dur"))
            if dur is not None:
                result.steps_ns.setdefault(key, dur)


# --------------------------------------------------------------------------- #
# Process orchestration (one (server, client) pair for one project)
# --------------------------------------------------------------------------- #

_LISTEN_MARKER    = "Start Listening"
_CONNECTED_MARKER = "Connect to"


def _pump_stdout(proc: subprocess.Popen, result: PartyResult,
                 echo_prefix: Optional[str], ready_q: "queue.Queue[str]") -> None:
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if echo_prefix is not None:
            print(f"{echo_prefix}{line}")
        update_party_from_line(result, line)
        if _LISTEN_MARKER in line:
            ready_q.put("listening")
        if _CONNECTED_MARKER in line:
            ready_q.put("connected")


def run_one(run_idx: int, project_tag: str, binary: Path, frontend_dir: Path,
            echo: bool, startup_timeout_s: float,
            child_env: dict[str, str]
            ) -> tuple[PartyResult, PartyResult]:
    """Launch one (server, client) pair for a single project."""
    server_res = PartyResult(party="server")
    client_res = PartyResult(party="client")
    ready_q: "queue.Queue[str]" = queue.Queue()

    server = subprocess.Popen(
        [str(binary), "0"],
        cwd=str(frontend_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_env,
    )
    server_thread = threading.Thread(
        target=_pump_stdout,
        args=(server, server_res,
              (f"[{project_tag} r{run_idx:>4} S] " if echo else None), ready_q),
        daemon=True,
    )
    server_thread.start()

    t0 = time.time()
    while True:
        try:
            _ = ready_q.get(timeout=0.25)
            break
        except queue.Empty:
            if server.poll() is not None:
                raise RuntimeError(
                    f"[{project_tag}] Server exited before listening "
                    f"(run {run_idx}). Last lines:\n"
                    + "\n".join(server_res.raw_lines[-20:])
                )
            if time.time() - t0 > startup_timeout_s:
                server.kill()
                raise TimeoutError(
                    f"[{project_tag}] Server did not start listening within "
                    f"{startup_timeout_s}s (run {run_idx})"
                )

    client = subprocess.Popen(
        [str(binary), "1"],
        cwd=str(frontend_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_env,
    )
    client_thread = threading.Thread(
        target=_pump_stdout,
        args=(client, client_res,
              (f"[{project_tag} r{run_idx:>4} C] " if echo else None), ready_q),
        daemon=True,
    )
    client_thread.start()

    server.wait()
    client.wait()
    server_thread.join(timeout=2.0)
    client_thread.join(timeout=2.0)

    if server.returncode != 0 or client.returncode != 0:
        raise RuntimeError(
            f"[{project_tag}] Non-zero exit (run {run_idx}): "
            f"server={server.returncode}, client={client.returncode}"
        )

    return server_res, client_res


# --------------------------------------------------------------------------- #
# Build helpers
# --------------------------------------------------------------------------- #

def build_release(project_tag: str, frontend_dir: Path) -> None:
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("`cargo` not found on PATH; cannot --build.")
    print(f"[runner] Building {project_tag} frontend in release mode ...")
    subprocess.check_call(
        [cargo, "build", "--release", "--manifest-path",
         str(frontend_dir / "Cargo.toml")]
    )


# --------------------------------------------------------------------------- #
# CSV + summary
# --------------------------------------------------------------------------- #

def _fmt_ms(ns: Optional[float]) -> str:
    return "\u2014" if ns is None else f"{ns/1e6:.3f} ms"


def _stats(vals: list[float]) -> tuple[int, float, float, float, float]:
    import math
    n = len(vals)
    if n == 0:
        return (0, 0.0, 0.0, 0.0, 0.0)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    sd = math.sqrt(var)
    return (n, mean, sd, min(vals), max(vals))


def _fmt_stats_line(vals: list[float]) -> str:
    n, mean, sd, lo, hi = _stats(vals)
    if n == 0:
        return "n=0"
    return (f"n={n:<3} mean={mean/1e6:>8.3f} ms  "
            f"sd={sd/1e6:>7.3f} ms  "
            f"min={lo/1e6:>8.3f} ms  "
            f"max={hi/1e6:>8.3f} ms")


def _delta_line(a_vals: list[float], b_vals: list[float],
                a_tag: str, b_tag: str) -> str:
    """Return a Δ line showing absolute (ms) savings of `a` vs `b` plus a
    `% reduction vs b` for both `mean` and `min`.

    Sign convention: positive = `a` is faster than `b` (i.e. `a` achieves
    that much reduction relative to the `b` baseline). Negative = `a` is
    slower than `b` (anti-reduction).

    % reduction = (b − a) / b * 100
    Absolute Δ  = b − a  (ns; rendered in ms)
    """
    na, ma, _, loa, _ = _stats(a_vals)
    nb, mb, _, lob, _ = _stats(b_vals)
    if na == 0 or nb == 0 or mb == 0 or lob == 0:
        return ""
    d_mean_ns  = mb  - ma
    d_min_ns   = lob - loa
    d_mean_pct = d_mean_ns / mb  * 100.0
    d_min_pct  = d_min_ns  / lob * 100.0
    return (f"{'':<8}\u0394 ({a_tag} reduction vs {b_tag})  "
            f"mean {d_mean_ns/1e6:+8.3f} ms ({d_mean_pct:+6.2f}%)   "
            f"min {d_min_ns/1e6:+8.3f} ms ({d_min_pct:+6.2f}%)")


def _vals_for_row_sum(
    measured: list[dict], tag: str, party: str, keys: list[str],
) -> list[float]:
    """Per-row sum of `keys` (ns); row skipped if any key missing or empty."""
    out: list[float] = []
    for r in measured:
        if r["project"] != tag or r["party"] != party:
            continue
        acc = 0.0
        for k in keys:
            s = r.get(k, "")
            if s == "":
                break
            acc += float(s)
        else:
            out.append(acc)
    return out


def _print_key_metrics_focus(measured: list[dict], project_tags: list[str]) -> None:
    """Short final block: totals, B2a/B2b, offline sum excl. B2a, O4a, online sum excl. O4a, offline+online."""

    def vals_for(tag: str, party: str, key: str) -> list[float]:
        return [
            float(r[key])
            for r in measured
            if r["project"] == tag and r["party"] == party and r.get(key, "") != ""
        ]

    b2a_label = next(l for l, k in OFFLINE_STEPS if k == "offline_b2a_idpf_gen_mem_ns")
    b2b_label = next(l for l, k in OFFLINE_STEPS if k == "offline_b2b_idpf_write_disk_ns")
    o4a_label = next(l for l, k in ONLINE_STEPS if k == "online_o4a_middle_idpf_ns")

    print("\n[runner] ===== Key metrics (warmup excluded) =====")
    print(
        "[runner] Focus: full offline/online; B2a & B2b; offline sum of B-steps "
        "excluding B2a; O4a; online sum of O-steps excluding O4a; "
        "last row = offline+online reported totals (overall improvement)."
    )
    if len(project_tags) >= 2:
        a_tag, b_tag = project_tags[0], project_tags[1]
        print(
            f"[runner] \u0394 rows: + = {a_tag} faster vs {b_tag}; "
            f"% = ({b_tag} \u2212 {a_tag}) / {b_tag} \u00d7 100."
        )

    rows_spec: List[Tuple[str, str, Optional[List[str]]]] = [
        ("Offline total (reported)", "offline_ns", None),
        ("Online total (reported)", "online_ns", None),
        (b2a_label, "offline_b2a_idpf_gen_mem_ns", None),
        (b2b_label, "offline_b2b_idpf_write_disk_ns", None),
        (
            "Offline sum B1..B5 excl. B2a (B1a+B1b+B2b+B3+B4+B5)",
            "",
            _OFFLINE_B_SUM_EX_B2A_KEYS,
        ),
        (o4a_label, "online_o4a_middle_idpf_ns", None),
        (
            "Online sum O1..O6 excl. O4a (O1+O2+O3+O4b+O5+O6)",
            "",
            _ONLINE_SUM_EX_O4A_KEYS,
        ),
        (
            "Offline + online total (reported; full run)",
            "",
            _REPORTED_OFFLINE_PLUS_ONLINE_KEYS,
        ),
    ]

    for party in ("server", "client"):
        print(f"\n  --- {party} (key metrics) ---")
        for title, single_key, sum_keys in rows_spec:
            print(f"  {title}:")
            for tag in project_tags:
                if sum_keys is not None:
                    vals = _vals_for_row_sum(measured, tag, party, sum_keys)
                else:
                    vals = vals_for(tag, party, single_key)
                print(f"    {tag:<4} {_fmt_stats_line(vals)}")
            if len(project_tags) >= 2:
                if sum_keys is not None:
                    va = _vals_for_row_sum(measured, project_tags[0], party, sum_keys)
                    vb = _vals_for_row_sum(measured, project_tags[1], party, sum_keys)
                else:
                    va = vals_for(project_tags[0], party, single_key)
                    vb = vals_for(project_tags[1], party, single_key)
                d = _delta_line(va, vb, project_tags[0], project_tags[1])
                if d:
                    print(f"    {d}")


def print_summary(rows: list[dict]) -> None:
    measured = [r for r in rows if r["warmup"] is False]
    if not measured:
        print("[runner] No non-warmup measurements.")
        return

    project_tags = [tag for tag, _ in PROJECTS]

    def vals_for(tag: str, party: str, key: str) -> list[float]:
        return [float(r[key]) for r in measured
                if r["project"] == tag and r["party"] == party and r.get(key, "") != ""]

    print("\n[runner] ===== Paired summary (warmup excluded) =====")
    print("[runner] Compared projects, in order: " + "  vs  ".join(project_tags))
    if len(project_tags) >= 2:
        a_tag, b_tag = project_tags[0], project_tags[1]
        print(f"[runner] \u0394 rows show {a_tag} vs {b_tag}: absolute ms "
              f"savings (+ = {a_tag} faster) and "
              f"`% reduction` = ({b_tag} \u2212 {a_tag}) / {b_tag} \u00d7 100  "
              f"(+ = {a_tag} faster vs {b_tag}).")
    for party in ("server", "client"):
        print(f"\n  --- {party} ---")

        # Top-line totals.
        for key, human in (("offline_ns", "offline"),
                           ("online_ns",  "online")):
            print(f"  {human}:")
            for tag in project_tags:
                vals = vals_for(tag, party, key)
                print(f"    {tag:<4} {_fmt_stats_line(vals)}")
            if len(project_tags) >= 2:
                d = _delta_line(vals_for(project_tags[0], party, key),
                                vals_for(project_tags[1], party, key),
                                project_tags[0], project_tags[1])
                if d:
                    print(f"    {d}")

        # Per-stage offline then online.
        for label, csv_key in OFFLINE_STEPS + ONLINE_STEPS:
            # Skip stages that never produced data for ANY project.
            any_data = any(vals_for(tag, party, csv_key) for tag in project_tags)
            if not any_data:
                continue
            print(f"  {label}:")
            for tag in project_tags:
                vals = vals_for(tag, party, csv_key)
                print(f"    {tag:<4} {_fmt_stats_line(vals)}")
            if len(project_tags) >= 2:
                d = _delta_line(vals_for(project_tags[0], party, csv_key),
                                vals_for(project_tags[1], party, csv_key),
                                project_tags[0], project_tags[1])
                if d:
                    print(f"    {d}")

    _print_key_metrics_focus(measured, project_tags)


# --------------------------------------------------------------------------- #
# Offline-only summarization: re-render the end-of-run summary from a CSV.
# --------------------------------------------------------------------------- #

def _truthy(s: str) -> bool:
    """Parse the `warmup` column (written as Python's str(bool)) back to bool."""
    return s.strip().lower() in ("1", "true", "yes", "y", "t")


def load_rows_from_csv(path: Path) -> list[dict]:
    """Load a results.csv produced by this runner back into the in-memory
    row shape that `print_summary` consumes. Only coerces the fields the
    summary actually reads:
      - `warmup` -> bool
      - numeric `*_ns` / top-line columns are kept as strings and parsed
        lazily inside `vals_for` (same path as the live run), so we don't
        need to know ahead of time which columns are present.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Cannot summarize: no results CSV at {path}. "
            f"Run the benchmark first, or pass --out pointing at an existing CSV."
        )
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows: list[dict] = []
        for r in reader:
            r["warmup"] = _truthy(r.get("warmup", "False"))
            rows.append(r)
    return rows


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=100,
                    help="Rounds PER PROJECT (HT and STD). Each round runs one "
                         "(server, client) pair of each project in the order "
                         "defined by PROJECTS. Default: 100 (=> 200 total runs).")
    ap.add_argument("--warmup", type=int, default=1,
                    help="Warmup rounds (still written to CSV with "
                         "warmup=True). Default: 1")
    ap.add_argument("--out", type=Path,
                    default=THIS_FILE.parent / "results.csv",
                    help="Output CSV path. Default: bench_compare/results.csv")
    ap.add_argument("--build", action="store_true",
                    help="Run `cargo build --release` for BOTH projects first.")
    ap.add_argument("--echo", action="store_true",
                    help="Echo each party's stdout line to the terminal.")
    ap.add_argument("--startup-timeout", type=float, default=30.0,
                    help="Seconds to wait for each server to bind. Default: 30")
    ap.add_argument("--inter-run-sleep", type=float, default=0.5,
                    help="Seconds to sleep between any two runs (same or "
                         "different project) so the OS can free the TCP port. "
                         "Default: 0.5 (slightly higher than single-project "
                         "runner because HT and STD share one port).")
    ap.add_argument("--no-offline-timing", action="store_true",
                    help="Do NOT set OFFLINE_TIMING=1 in the child env.")
    ap.add_argument("--no-online-timing", action="store_true",
                    help="Do NOT set ONLINE_TIMING=1 in the child env.")
    ap.add_argument("--summarize", action="store_true",
                    help="Do NOT run any benchmarks; load the CSV at --out "
                         "and re-print the aggregated end-of-run summary.")
    args = ap.parse_args()

    # --summarize: skip all the benchmarking machinery, just re-render the
    # summary from the existing CSV. Useful for re-inspecting old runs.
    if args.summarize:
        try:
            rows = load_rows_from_csv(args.out)
        except FileNotFoundError as e:
            print(f"[runner] {e}", file=sys.stderr)
            sys.exit(1)
        n_total = len(rows)
        n_warm  = sum(1 for r in rows if r["warmup"] is True)
        print(f"[runner] Loaded {n_total} rows from {args.out} "
              f"({n_warm} warmup, {n_total - n_warm} measured).")
        print_summary(rows)
        return

    # Resolve binaries / cwds per project and fail fast if anything's missing.
    project_info: list[dict] = []
    for tag, folder in PROJECTS:
        fdir = _frontend_dir(folder)
        fbin = _frontend_bin(folder)
        proot = _project_root(folder)
        if args.build:
            build_release(tag, fdir)
        if not fbin.exists():
            print(f"[runner] {tag} binary not found at {fbin}.", file=sys.stderr)
            print(f"         Pass --build, or run `cargo build --release "
                  f"--manifest-path {fdir / 'Cargo.toml'}`.", file=sys.stderr)
            sys.exit(1)
        proot.joinpath("test").mkdir(exist_ok=True)
        project_info.append({"tag": tag, "folder": folder,
                             "bin": fbin, "fdir": fdir, "proot": proot})

    # Child environment with timing toggles.
    child_env: dict[str, str] = os.environ.copy()
    if not args.no_offline_timing:
        child_env["OFFLINE_TIMING"] = "1"
    else:
        child_env.pop("OFFLINE_TIMING", None)
    if not args.no_online_timing:
        child_env["ONLINE_TIMING"] = "1"
    else:
        child_env.pop("ONLINE_TIMING", None)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[runner] Writing results to {args.out}")
    print(f"[runner] Interleave order per round: "
          + " \u2192 ".join(p["tag"] for p in project_info))
    print(f"[runner] Child env: OFFLINE_TIMING={child_env.get('OFFLINE_TIMING', '<unset>')}, "
          f"ONLINE_TIMING={child_env.get('ONLINE_TIMING', '<unset>')}")
    for p in project_info:
        print(f"[runner]   {p['tag']:<4} -> {p['bin']}")

    step_cols = [k for _, k in OFFLINE_STEPS] + [k for _, k in ONLINE_STEPS]
    headers = [
        "run_idx", "project", "party", "warmup",
        "offline_ns", "offline_ms",
        "online_ns", "online_ms",
        "online_rounds", "comm_bytes",
        "wall_ns",
        *step_cols,
    ]

    # Cumulative round-by-round scoreboards for the top-line totals only
    # (offline_ns and online_ns). These only count non-warmup rounds where
    # every project produced a valid measurement.
    project_tags = [p["tag"] for p in project_info]
    score_wins: dict[str, dict[str, int]] = {
        "offline": {t: 0 for t in project_tags},
        "online":  {t: 0 for t in project_tags},
    }
    score_ties: dict[str, int] = {"offline": 0, "online": 0}

    rows: list[dict] = []
    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()

        total_projects = len(project_info)
        for run_idx in range(1, args.runs + 1):
            is_warmup = run_idx <= args.warmup
            tag_warmup = " (warmup)" if is_warmup else ""
            print(f"[runner] === round {run_idx}/{args.runs}{tag_warmup} ===")

            # Per-round, per-project aggregate (averaged across server + client
            # for an apples-to-apples single number). Populated only for
            # projects that ran successfully this round.
            round_totals: dict[str, dict[str, float]] = {}

            for p_idx, p in enumerate(project_info):
                t_wall = time.perf_counter_ns()
                try:
                    srv, cli = run_one(
                        run_idx=run_idx,
                        project_tag=p["tag"],
                        binary=p["bin"],
                        frontend_dir=p["fdir"],
                        echo=args.echo,
                        startup_timeout_s=args.startup_timeout,
                        child_env=child_env,
                    )
                except Exception as e:
                    print(f"[runner] {p['tag']} round {run_idx} failed: {e}",
                          file=sys.stderr)
                    time.sleep(max(1.0, args.inter_run_sleep))
                    continue
                wall_ns = time.perf_counter_ns() - t_wall

                for res in (srv, cli):
                    row = {
                        "run_idx": run_idx,
                        "project": p["tag"],
                        "party": res.party,
                        "warmup": is_warmup,
                        "offline_ns": f"{res.offline_ns:.0f}" if res.offline_ns is not None else "",
                        "offline_ms": f"{res.offline_ns/1e6:.3f}" if res.offline_ns is not None else "",
                        "online_ns":  f"{res.online_ns:.0f}"  if res.online_ns  is not None else "",
                        "online_ms":  f"{res.online_ns/1e6:.3f}"  if res.online_ns  is not None else "",
                        "online_rounds": res.online_rounds if res.online_rounds is not None else "",
                        "comm_bytes": res.comm_bytes if res.comm_bytes is not None else "",
                        "wall_ns": wall_ns,
                    }
                    for col in step_cols:
                        ns = res.steps_ns.get(col)
                        row[col] = f"{ns:.0f}" if ns is not None else ""
                    writer.writerow(row)
                    rows.append(row)
                f.flush()

                print(
                    f"           [{p['tag']:<4}] server: offline={_fmt_ms(srv.offline_ns)} "
                    f"online={_fmt_ms(srv.online_ns)} "
                    f"rounds={srv.online_rounds} bytes={srv.comm_bytes}"
                )
                print(
                    f"           [{p['tag']:<4}] client: offline={_fmt_ms(cli.offline_ns)} "
                    f"online={_fmt_ms(cli.online_ns)} "
                    f"rounds={cli.online_rounds} bytes={cli.comm_bytes}"
                )

                # Remember per-round (server+client)-averaged totals for this
                # project so we can print a Δ / scoreboard after the round.
                if (srv.offline_ns is not None and cli.offline_ns is not None
                        and srv.online_ns is not None and cli.online_ns is not None):
                    round_totals[p["tag"]] = {
                        "offline": (srv.offline_ns + cli.offline_ns) / 2.0,
                        "online":  (srv.online_ns  + cli.online_ns)  / 2.0,
                    }

                # Sleep between any two runs (including the final one of a
                # round and the first of the next round) so the shared TCP
                # port is released cleanly before the next Popen.
                is_last_of_everything = (
                    run_idx == args.runs and p_idx == total_projects - 1
                )
                if not is_last_of_everything:
                    time.sleep(args.inter_run_sleep)

            # ------------------------------------------------------------
            # Per-round Δ + running scoreboard. Only computed when every
            # project produced a valid measurement this round (which is the
            # normal case; we just don't want to crash on a failed run).
            # ------------------------------------------------------------
            if len(round_totals) == len(project_info) and len(project_info) >= 2:
                a_tag, b_tag = project_tags[0], project_tags[1]
                a, b = round_totals[a_tag], round_totals[b_tag]

                def _fmt_delta(metric: str) -> tuple[str, Optional[str]]:
                    va, vb = a[metric], b[metric]
                    if va <= 0 or vb <= 0:
                        return ("", None)
                    # `a` reduction vs `b` baseline (positive => a faster).
                    # pct = (b − a) / b * 100
                    pct = (vb - va) / vb * 100.0
                    faster = a_tag if va < vb else (b_tag if vb < va else None)
                    faster_str = (f"({faster} faster)" if faster
                                  else "(tie)")
                    line = (f"\u0394 {metric:<7}: "
                            f"{a_tag} {va/1e6:>7.3f} ms  vs  "
                            f"{b_tag} {vb/1e6:>7.3f} ms   "
                            f"{a_tag} reduction {pct:+6.2f}% vs {b_tag}   "
                            f"{faster_str}")
                    return (line, faster)

                off_line, off_faster = _fmt_delta("offline")
                on_line,  on_faster  = _fmt_delta("online")
                if off_line:
                    print(f"           {off_line}")
                if on_line:
                    print(f"           {on_line}")

                # Score only non-warmup rounds.
                if not is_warmup:
                    if off_faster is not None:
                        score_wins["offline"][off_faster] += 1
                    else:
                        score_ties["offline"] += 1
                    if on_faster is not None:
                        score_wins["online"][on_faster] += 1
                    else:
                        score_ties["online"] += 1

                # Compact running scoreboard in the user-requested format:
                #   HT: 56 | STD: 20
                def _score_str(metric: str) -> str:
                    parts = [f"{t}: {score_wins[metric][t]}" for t in project_tags]
                    s = "  |  ".join(parts)
                    if score_ties[metric]:
                        s += f"  |  tie: {score_ties[metric]}"
                    return s

                print(f"           Score: offline  {_score_str('offline')}"
                      f"     online  {_score_str('online')}")

    print_summary(rows)


if __name__ == "__main__":
    main()
