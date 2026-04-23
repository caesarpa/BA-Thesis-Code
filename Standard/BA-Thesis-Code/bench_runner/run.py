"""
Benchmark runner for the FSS-KRE frontend protocol.

Repeatedly launches a (server, client) pair of the `frontend` binary, parses the
offline / online timings from each party's stdout, and appends one row per
(run, party) to a CSV file.

Usage (PowerShell, from the workspace root):

    python bench_runner\run.py --runs 100 --out bench_runner\results.csv

The scope (which protocol / input_size is benchmarked) is whatever
`FSS-KRE-master/frontend/src/main.rs` currently selects; we do not override it.

Requirements: Python >= 3.9, a Rust toolchain on PATH (only if you pass
`--build`; by default we expect a prebuilt release binary to already exist).
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
from typing import Optional


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

THIS_FILE = Path(__file__).resolve()
WORKSPACE_ROOT = THIS_FILE.parent.parent
PROJECT_ROOT = WORKSPACE_ROOT / "FSS-KRE-master"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
RELEASE_BIN = FRONTEND_DIR / "target" / "release" / (
    "frontend.exe" if sys.platform.startswith("win") else "frontend"
)


# --------------------------------------------------------------------------- #
# Duration parsing (matches the output of Rust's `Duration` Debug formatter)
# --------------------------------------------------------------------------- #

# Examples:  "123ns", "12.345µs", "12.345us", "12.345ms", "1.234567890s"
# Also handles composite forms like "1s 234ms" defensively by taking the
# first token, which Rust uses only for very large durations.
_DUR_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ns|us|µs|ms|s)\b"
)

_UNIT_TO_NS = {
    "ns": 1.0,
    "us": 1e3,
    "\u00b5s": 1e3,   # µs
    "ms": 1e6,
    "s":  1e9,
}


def parse_rust_duration_ns(text: str) -> Optional[float]:
    """Parse the first Rust Duration literal found in `text` and return it in ns.

    Returns None if nothing parsable is found.
    """
    m = _DUR_RE.search(text)
    if not m:
        return None
    value = float(m.group("value"))
    unit = m.group("unit")
    return value * _UNIT_TO_NS[unit]


# --------------------------------------------------------------------------- #
# Per-stage timing ordering and CSV column keys
# --------------------------------------------------------------------------- #
#
# These mirror the `offline_step` / `online_step` calls in
# `libmpc/src/offline_data*.rs` and `libmpc/src/protocols/bitwise_max.rs`.
# Emitted only when the child process has `OFFLINE_TIMING=1` /
# `ONLINE_TIMING=1` in its environment (the runner sets both by default).

OFFLINE_STEPS: list[tuple[str, str]] = [
    ("B1a α PRG draw",        "offline_b1a_alpha_prg_draw_ns"),
    ("B2a IDPF gen (mem)",    "offline_b2a_idpf_gen_mem_ns"),
    ("B2a1 IDPF expand",      "offline_b2a1_idpf_expand_ns"),
    ("B2a2 IDPF convert+CW",  "offline_b2a2_idpf_convert_cw_ns"),
    ("B2a3 IDPF tag hash",    "offline_b2a3_idpf_tag_hash_ns"),
    ("B2b IDPF write (disk)", "offline_b2b_idpf_write_disk_ns"),
    ("B1b α shares",          "offline_b1b_alpha_shares_ns"),
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

_STEP_LABEL_TO_KEY: dict[str, str] = {label: key for label, key in
                                      OFFLINE_STEPS + ONLINE_STEPS}


# --------------------------------------------------------------------------- #
# Per-party output parsing
# --------------------------------------------------------------------------- #

@dataclass
class PartyResult:
    party: str                     # "server" or "client"
    offline_ns: Optional[float] = None
    online_ns: Optional[float] = None
    online_rounds: Optional[int] = None
    comm_bytes: Optional[int] = None
    steps_ns: dict[str, float] = field(default_factory=dict)  # csv_key -> ns
    raw_lines: list[str] = field(default_factory=list)


_RE_OFFLINE = re.compile(r"Offline key generation time:\s*(.+)$")
_RE_ROUNDS = re.compile(r"Online rounds:\s*(\d+)")
_RE_COMM = re.compile(r"Communication volume:\s*(\d+)\s*bytes")
_RE_COMP = re.compile(r"Computation time:\s*(.+?)\s*$")

# Matches lines produced by `offline_step` / `online_step` / `online_report`:
#   "  [offline] B1a α PRG draw             11.135ms"
#   "  [online]  O4a middle: IDPF eval      323.456ms"
# The label can contain spaces/punctuation; we non-greedily capture up to the
# last whitespace-separated duration token on the line.
_RE_STEP = re.compile(
    r"^\s*\[(?P<kind>offline|online)\]\s+"
    r"(?P<label>.+?)\s+"
    r"(?P<dur>\d+(?:\.\d+)?\s*(?:ns|us|µs|ms|s))\s*$"
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
                # First occurrence wins (defensive; steps should be unique per run).
                result.steps_ns.setdefault(key, dur)


# --------------------------------------------------------------------------- #
# Process orchestration
# --------------------------------------------------------------------------- #

_LISTEN_MARKER = "Start Listening"
_CONNECTED_MARKER = "Connect to"


def _pump_stdout(proc: subprocess.Popen, result: PartyResult,
                 echo_prefix: Optional[str], ready_q: "queue.Queue[str]") -> None:
    """Read lines from `proc.stdout` until EOF, update `result`, and push
    sentinel strings into `ready_q` when we see key markers (so the parent
    thread can synchronize client startup with server readiness)."""
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


def run_one(run_idx: int, binary: Path, echo: bool,
            startup_timeout_s: float,
            child_env: Optional[dict[str, str]] = None
            ) -> tuple[PartyResult, PartyResult]:
    """Launch one (server, client) pair and return both parsed results."""
    server_res = PartyResult(party="server")
    client_res = PartyResult(party="client")
    ready_q: "queue.Queue[str]" = queue.Queue()

    # Start server first.
    # NOTE: `encoding="utf-8"` is required on Windows — the frontend prints
    # UTF-8 bytes (α in "B1a α PRG draw", µ in "µs" durations). With the
    # default locale-based decoder (CP1252 on Windows) those bytes decode to
    # mojibake and silently break the per-stage regex parsing.
    server = subprocess.Popen(
        [str(binary), "0"],
        cwd=str(FRONTEND_DIR),
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
        args=(server, server_res, (f"[run {run_idx:>4} S] " if echo else None), ready_q),
        daemon=True,
    )
    server_thread.start()

    # Wait until the server is actually listening on the TCP port.
    t0 = time.time()
    while True:
        try:
            _ = ready_q.get(timeout=0.25)
            # Any signal is fine here; we only require "listening" first, but
            # in practice the server prints it long before anything else.
            break
        except queue.Empty:
            if server.poll() is not None:
                raise RuntimeError(
                    f"Server exited before listening (run {run_idx}). "
                    f"Last lines:\n" + "\n".join(server_res.raw_lines[-20:])
                )
            if time.time() - t0 > startup_timeout_s:
                server.kill()
                raise TimeoutError(
                    f"Server did not start listening within {startup_timeout_s}s "
                    f"(run {run_idx})"
                )

    # Now start the client.
    client = subprocess.Popen(
        [str(binary), "1"],
        cwd=str(FRONTEND_DIR),
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
        args=(client, client_res, (f"[run {run_idx:>4} C] " if echo else None), ready_q),
        daemon=True,
    )
    client_thread.start()

    # Wait for both to finish.
    server.wait()
    client.wait()
    server_thread.join(timeout=2.0)
    client_thread.join(timeout=2.0)

    if server.returncode != 0 or client.returncode != 0:
        raise RuntimeError(
            f"Non-zero exit (run {run_idx}): server={server.returncode}, "
            f"client={client.returncode}"
        )

    return server_res, client_res


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_release() -> None:
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("`cargo` not found on PATH; cannot --build.")
    print("[runner] Building frontend in release mode ...")
    subprocess.check_call(
        [cargo, "build", "--release", "--manifest-path",
         str(FRONTEND_DIR / "Cargo.toml")]
    )


def main() -> None:
    # Labels like "B1a α PRG draw" and µs durations are UTF-8; on Windows
    # Python's stdout defaults to the console codepage (often CP1252) which
    # would blow up when we print the summary. Make our own stdout/stderr
    # tolerant of non-ASCII.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=100,
                    help="Number of (server, client) pairs to launch. Default: 100")
    ap.add_argument("--warmup", type=int, default=1,
                    help="Number of initial runs to discard (cold-cache warmup). "
                         "Still written to the CSV with warmup=True. Default: 1")
    ap.add_argument("--out", type=Path, default=THIS_FILE.parent / "results.csv",
                    help="Output CSV path. Default: bench_runner/results.csv")
    ap.add_argument("--build", action="store_true",
                    help="Run `cargo build --release` before starting.")
    ap.add_argument("--binary", type=Path, default=RELEASE_BIN,
                    help=f"Path to the frontend binary. Default: {RELEASE_BIN}")
    ap.add_argument("--echo", action="store_true",
                    help="Echo each party's stdout line to the terminal.")
    ap.add_argument("--startup-timeout", type=float, default=30.0,
                    help="Seconds to wait for the server to bind the port. Default: 30")
    ap.add_argument("--inter-run-sleep", type=float, default=0.3,
                    help="Seconds to sleep between runs so the OS can free the "
                         "TCP port. Default: 0.3")
    ap.add_argument("--no-offline-timing", action="store_true",
                    help="Do NOT set OFFLINE_TIMING=1 in the child env "
                         "(suppresses per-stage offline prints and CSV cols).")
    ap.add_argument("--no-online-timing", action="store_true",
                    help="Do NOT set ONLINE_TIMING=1 in the child env "
                         "(suppresses per-stage online prints and CSV cols).")
    args = ap.parse_args()

    if args.build:
        build_release()

    if not args.binary.exists():
        print(f"[runner] Binary not found at {args.binary}.", file=sys.stderr)
        print("         Pass --build, or build manually with:", file=sys.stderr)
        print(f"         cargo build --release --manifest-path "
              f"{FRONTEND_DIR / 'Cargo.toml'}", file=sys.stderr)
        sys.exit(1)

    # Ensure the `test/` directory exists (main.rs writes ../test/x*.bin).
    (PROJECT_ROOT / "test").mkdir(exist_ok=True)

    # Build the env the children will inherit. We always start from the runner's
    # current environment, then opt into the per-stage timing prints unless the
    # user explicitly disables them.
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
    print(f"[runner] Child env: OFFLINE_TIMING={child_env.get('OFFLINE_TIMING', '<unset>')}, "
          f"ONLINE_TIMING={child_env.get('ONLINE_TIMING', '<unset>')}")

    # Canonical CSV column order: base metrics first, then per-step ns columns
    # in the same order as the offline/online pipelines.
    step_cols = [key for _, key in OFFLINE_STEPS] + [key for _, key in ONLINE_STEPS]
    headers = [
        "run_idx", "party", "warmup",
        "offline_ns", "offline_ms",
        "online_ns", "online_ms",
        "online_rounds", "comm_bytes",
        "wall_ns",
        *step_cols,
    ]

    rows: list[dict] = []
    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()

        for run_idx in range(1, args.runs + 1):
            is_warmup = run_idx <= args.warmup
            tag = " (warmup)" if is_warmup else ""
            print(f"[runner] === run {run_idx}/{args.runs}{tag} ===")

            t_wall = time.perf_counter_ns()
            try:
                srv, cli = run_one(
                    run_idx=run_idx,
                    binary=args.binary,
                    echo=args.echo,
                    startup_timeout_s=args.startup_timeout,
                    child_env=child_env,
                )
            except Exception as e:
                print(f"[runner] run {run_idx} failed: {e}", file=sys.stderr)
                # Try to keep going with the next run.
                time.sleep(max(1.0, args.inter_run_sleep))
                continue
            wall_ns = time.perf_counter_ns() - t_wall

            for res in (srv, cli):
                row = {
                    "run_idx": run_idx,
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
                f"           server: offline={_fmt_ms(srv.offline_ns)} "
                f"online={_fmt_ms(srv.online_ns)} "
                f"rounds={srv.online_rounds} bytes={srv.comm_bytes}"
            )
            print(
                f"           client: offline={_fmt_ms(cli.offline_ns)} "
                f"online={_fmt_ms(cli.online_ns)} "
                f"rounds={cli.online_rounds} bytes={cli.comm_bytes}"
            )

            if run_idx < args.runs:
                time.sleep(args.inter_run_sleep)

    print_summary(rows)


def _fmt_ms(ns: Optional[float]) -> str:
    return "—" if ns is None else f"{ns/1e6:.3f} ms"


def print_summary(rows: list[dict]) -> None:
    """Print mean/min/max/stddev per (party, metric), ignoring warmup rows."""
    import math
    measured = [r for r in rows if r["warmup"] is False]
    if not measured:
        print("[runner] No non-warmup measurements.")
        return

    def stats(vals: list[float]) -> str:
        n = len(vals)
        if n == 0:
            return "n=0"
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        sd = math.sqrt(var)
        return (f"n={n}  mean={mean/1e6:.3f} ms  "
                f"sd={sd/1e6:.3f} ms  "
                f"min={min(vals)/1e6:.3f} ms  max={max(vals)/1e6:.3f} ms")

    print("\n[runner] ===== Summary (warmup excluded) =====")
    for party in ("server", "client"):
        # Totals.
        for key in ("offline_ns", "online_ns"):
            vals = [float(r[key]) for r in measured
                    if r["party"] == party and r[key] != ""]
            print(f"  {party:<6} {key[:-3]:<28}  {stats(vals)}")

        # Per-stage offline then online, in pipeline order. Only emit rows
        # for stages that actually produced at least one measurement, so
        # disabling a timing scope doesn't clutter the summary.
        for label, csv_key in OFFLINE_STEPS + ONLINE_STEPS:
            vals = [float(r[csv_key]) for r in measured
                    if r["party"] == party and r.get(csv_key, "") != ""]
            if not vals:
                continue
            print(f"  {party:<6} {label:<28}  {stats(vals)}")


if __name__ == "__main__":
    main()
