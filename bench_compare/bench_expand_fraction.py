#!/usr/bin/env python3
"""
bench_expand_fraction.py  --  Short benchmark to measure what fraction of
total IDPF operation time is consumed by the `expand_dir` sub-step in the
HT project.

Runs the HT frontend (server + client pair) for --runs rounds with
OFFLINE_TIMING=1 and ONLINE_TIMING=1, parses the per-step sub-breakdowns
that are already instrumented in the Rust code, and computes statistics.

Usage (from workspace root):
    python bench_compare/bench_expand_fraction.py --runs 50
    python bench_compare/bench_expand_fraction.py --summarize
    python bench_compare/bench_expand_fraction.py --summarize --out bench_compare/expand_fraction_results.json

Writes results to bench_compare/expand_fraction_results.json and prints
a human-readable summary.  Pass --summarize to re-print the summary from
a previously saved JSON without running any benchmarks.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE      = Path(__file__).resolve().parent
CODE_ROOT = HERE.parent

_EXE = "frontend.exe" if sys.platform.startswith("win") else "frontend"

HT_FRONTEND_DIR = (CODE_ROOT / "HT" / "BA-Thesis-Code" / "FSS-KRE-master"
                   / "frontend")
HT_BIN = HT_FRONTEND_DIR / "target" / "release" / _EXE

RESULTS_JSON = HERE / "expand_fraction_results.json"

# ---------------------------------------------------------------------------
# Parsing (mirrors bench_compare/run.py)
# ---------------------------------------------------------------------------

_DUR_RE = re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*(?P<u>ns|us|\u00b5s|ms|s)\b")
_UNIT_NS = {"ns": 1.0, "us": 1e3, "\u00b5s": 1e3, "ms": 1e6, "s": 1e9}
_RE_STEP = re.compile(
    r"^\s*\[(?P<kind>offline|online)\]\s+"
    r"(?P<label>.+?)\s+"
    r"(?P<dur>\d+(?:\.\d+)?\s*(?:ns|us|\u00b5s|ms|s))\s*$"
)
_RE_OFFLINE = re.compile(r"Offline key generation time:\s*(.+)$")
_RE_ONLINE  = re.compile(r"Computation time:\s*(.+?)$")

STEP_LABELS = {
    # offline aggregate + sub-steps
    "B2a IDPF gen (mem)":   "b2a_total",
    "B2a1 IDPF expand":     "b2a1_expand",
    "B2a2 IDPF convert+CW": "b2a2_convert",
    "B2a3 IDPF tag hash":   "b2a3_tag",
    # online round 0
    "O3 round 0":                "o3_total",
    "O3a round0: expand_dir":    "o3a_expand",
    "O3b round0: convert+word":  "o3b_convert",
    "O3c round0: tag update":    "o3c_tag",
    # online middle IDPF
    "O4a middle: IDPF eval":        "o4a_total",
    "O4a1 middle: expand_dir":      "o4a1_expand",
    "O4a2 middle: convert+word":    "o4a2_convert",
    "O4a3 middle: tag update":      "o4a3_tag",
}


def _parse_dur(text: str) -> Optional[float]:
    m = _DUR_RE.search(text)
    if not m:
        return None
    return float(m.group("v")) * _UNIT_NS[m.group("u")]


@dataclass
class RunResult:
    party: str
    offline_ns: Optional[float] = None
    online_ns:  Optional[float] = None
    steps: Dict[str, float] = field(default_factory=dict)


def _parse_line(line: str, res: RunResult) -> None:
    if (m := _RE_OFFLINE.search(line)) and res.offline_ns is None:
        res.offline_ns = _parse_dur(m.group(1))
    if (m := _RE_ONLINE.search(line)) and res.online_ns is None:
        res.online_ns = _parse_dur(m.group(1))
    if (m := _RE_STEP.match(line)):
        label = m.group("label").strip()
        key = STEP_LABELS.get(label)
        if key is not None:
            dur = _parse_dur(m.group("dur"))
            if dur is not None:
                res.steps.setdefault(key, dur)


def _pump(proc: subprocess.Popen, res: RunResult,
          ready_q: "queue.Queue[str]", echo: bool, prefix: str) -> None:
    for raw in proc.stdout:   # type: ignore[union-attr]
        line = raw.rstrip("\r\n")
        if echo:
            print(f"{prefix}{line}")
        _parse_line(line, res)
        if "Start Listening" in line:
            ready_q.put("listening")
        if "Connect to" in line:
            ready_q.put("connected")


def run_one(binary: Path, frontend_dir: Path, env: dict,
            startup_s: float, echo: bool, idx: int
            ) -> Tuple[RunResult, RunResult]:
    srv_res = RunResult("server")
    cli_res = RunResult("client")
    rq: "queue.Queue[str]" = queue.Queue()

    srv = subprocess.Popen([str(binary), "0"], cwd=str(frontend_dir),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace",
                           bufsize=1, env=env)
    th_s = threading.Thread(target=_pump,
                             args=(srv, srv_res, rq, echo, f"[S{idx:>3}] "),
                             daemon=True)
    th_s.start()

    t0 = time.time()
    while True:
        try:
            rq.get(timeout=0.25); break
        except queue.Empty:
            if srv.poll() is not None:
                raise RuntimeError(f"Server exited early (run {idx})")
            if time.time() - t0 > startup_s:
                srv.kill()
                raise TimeoutError(f"Server did not start within {startup_s}s")

    cli = subprocess.Popen([str(binary), "1"], cwd=str(frontend_dir),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace",
                           bufsize=1, env=env)
    th_c = threading.Thread(target=_pump,
                             args=(cli, cli_res, rq, echo, f"[C{idx:>3}] "),
                             daemon=True)
    th_c.start()

    srv.wait(); cli.wait()
    th_s.join(timeout=2.0); th_c.join(timeout=2.0)

    if srv.returncode != 0 or cli.returncode != 0:
        raise RuntimeError(f"Non-zero exit (run {idx}): "
                           f"srv={srv.returncode} cli={cli.returncode}")
    return srv_res, cli_res


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _stats(vals: List[float]) -> Dict:
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None}
    mean = sum(vals) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / n)
    return {"n": n, "mean": mean, "sd": sd, "min": min(vals), "max": max(vals)}


# ---------------------------------------------------------------------------
# Summary printer (shared by --summarize and live runs)
# ---------------------------------------------------------------------------

def print_summary(results: Dict) -> None:
    def pct(num_key: str, den_key: str) -> Optional[float]:
        n = results[num_key].get("mean")
        d = results[den_key].get("mean")
        if n is None or d is None or d == 0:
            return None
        return round(n / d * 100, 2)

    print("\n" + "=" * 60)
    print("  IDPF Expand Fraction Results (HT project)")
    print("=" * 60)

    b2a  = results["b2a_total"]["mean"]
    b2a1 = results["b2a1_expand"]["mean"]
    b2a2 = results["b2a2_convert"]["mean"]
    b2a3 = results["b2a3_tag"]["mean"]
    print("\n--- Offline: B2a IDPF gen breakdown ---")
    if b2a and b2a > 0:
        for lbl, val in [
            ("B2a  total",           b2a),
            ("B2a1 expand_children", b2a1),
            ("B2a2 convert+CW",      b2a2),
            ("B2a3 tag hash",        b2a3),
        ]:
            if val:
                print(f"  {lbl:<28}  {val/1e6:8.3f} ms  "
                      f"({val/b2a*100:5.1f}% of B2a)")
    else:
        print("  (no B2a timing data captured)")

    o3  = results["o3_total"]["mean"]
    o3a = results["o3a_expand"]["mean"]
    o3b = results["o3b_convert"]["mean"]
    o3c = results["o3c_tag"]["mean"]
    print("\n--- Online: O3 round 0 breakdown ---")
    if o3 and o3 > 0:
        for lbl, val in [
            ("O3  total",         o3),
            ("O3a expand_dir",    o3a),
            ("O3b convert+word",  o3b),
            ("O3c tag update",    o3c),
        ]:
            if val:
                print(f"  {lbl:<28}  {val/1e6:8.3f} ms  "
                      f"({val/o3*100:5.1f}% of O3)")
    else:
        print("  (no O3 timing data captured)")

    o4a  = results["o4a_total"]["mean"]
    o4a1 = results["o4a1_expand"]["mean"]
    o4a2 = results["o4a2_convert"]["mean"]
    o4a3 = results["o4a3_tag"]["mean"]
    print("\n--- Online: O4a middle IDPF breakdown ---")
    if o4a and o4a > 0:
        for lbl, val in [
            ("O4a  total",        o4a),
            ("O4a1 expand_dir",   o4a1),
            ("O4a2 convert+word", o4a2),
            ("O4a3 tag update",   o4a3),
        ]:
            if val:
                print(f"  {lbl:<28}  {val/1e6:8.3f} ms  "
                      f"({val/o4a*100:5.1f}% of O4a)")
    else:
        print("  (no O4a timing data captured)")

    off_ns = results["offline_ns"]["mean"]
    on_ns  = results["online_ns"]["mean"]
    tot_ns = (off_ns or 0) + (on_ns or 0)
    expand_total = (b2a1 or 0) + (o3a or 0) + (o4a1 or 0)
    idpf_total   = (b2a  or 0) + (o3  or 0) + (o4a  or 0)

    print("\n--- Summary ---")
    if tot_ns > 0:
        print(f"  Total (offline+online):      {tot_ns/1e6:.2f} ms")
        print(f"  IDPF stages (B2a+O3+O4a):   {idpf_total/1e6:.2f} ms "
              f"({idpf_total/tot_ns*100:.1f}% of total)")
        if expand_total > 0:
            print(f"  expand_dir (B2a1+O3a+O4a1): {expand_total/1e6:.2f} ms "
                  f"({expand_total/tot_ns*100:.1f}% of total, "
                  f"{expand_total/idpf_total*100:.1f}% of IDPF stages)")
        if b2a  and b2a1:
            print(f"  expand fraction within B2a:  {b2a1/b2a*100:.1f}%")
        if o3   and o3a:
            print(f"  expand fraction within O3:   {o3a/o3*100:.1f}%")
        if o4a  and o4a1:
            print(f"  expand fraction within O4a:  {o4a1/o4a*100:.1f}%")

    meta = results.get("_meta", {})
    if meta:
        print(f"\n  Source: {meta.get('binary', '?')}")
        print(f"  Runs:   {meta.get('runs', '?')} measured + "
              f"{meta.get('warmup', '?')} warmup  |  "
              f"Recorded: {meta.get('timestamp', '?')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--runs",    type=int, default=50,
                    help="Number of measurement rounds (default 50)")
    ap.add_argument("--warmup",  type=int, default=2,
                    help="Number of warmup rounds (default 2)")
    ap.add_argument("--startup", type=float, default=30.0,
                    help="Max seconds to wait for server to start (default 30)")
    ap.add_argument("--echo",    action="store_true",
                    help="Echo binary stdout to terminal")
    ap.add_argument("--summarize", action="store_true",
                    help="Do not run any benchmarks; load the JSON at --out "
                         "and re-print the summary.")
    ap.add_argument("--out", type=Path, default=RESULTS_JSON,
                    help=f"JSON output path (default: {RESULTS_JSON})")
    args = ap.parse_args()

    # --summarize: just reload the saved JSON and print, no benchmarking.
    if args.summarize:
        if not args.out.exists():
            sys.exit(f"[expand-fraction] ERROR: no results file at {args.out}\n"
                     "Run the benchmark first (without --summarize).")
        results = json.loads(args.out.read_text(encoding="utf-8"))
        print(f"[expand-fraction] Loaded results from {args.out}")
        print_summary(results)
        return

    if not HT_BIN.exists():
        sys.exit(f"ERROR: HT binary not found at {HT_BIN}\n"
                 "Run: cargo build --release  inside the HT frontend folder.")

    env = os.environ.copy()
    env["OFFLINE_TIMING"] = "1"
    env["ONLINE_TIMING"]  = "1"

    total = args.warmup + args.runs
    print(f"[expand-fraction] HT binary: {HT_BIN}")
    print(f"[expand-fraction] Running {args.warmup} warmup + "
          f"{args.runs} measurement rounds ...")

    accum: Dict[str, List[float]] = {k: [] for k in STEP_LABELS.values()}
    accum["offline_ns"] = []
    accum["online_ns"]  = []

    for run_idx in range(1, total + 1):
        is_warmup = run_idx <= args.warmup
        tag = " (warmup)" if is_warmup else ""
        print(f"[expand-fraction] round {run_idx}/{total}{tag}", end=" ... ",
              flush=True)
        try:
            srv, cli = run_one(HT_BIN, HT_FRONTEND_DIR, env,
                               args.startup, args.echo, run_idx)
        except Exception as exc:
            print(f"FAILED: {exc}")
            continue

        if is_warmup:
            print("ok (warmup, skipped)")
            continue

        for key in list(STEP_LABELS.values()) + ["offline_ns", "online_ns"]:
            vals = []
            for res in (srv, cli):
                v = res.steps.get(key) if key in STEP_LABELS.values() \
                    else (res.offline_ns if key == "offline_ns" else res.online_ns)
                if v is not None:
                    vals.append(v)
            if vals:
                accum[key].append(sum(vals) / len(vals))

        b2a_acc  = accum["b2a_total"]
        b2a1_acc = accum["b2a1_expand"]
        if b2a_acc and b2a1_acc:
            p = b2a1_acc[-1] / b2a_acc[-1] * 100
            print(f"ok  (expand={b2a1_acc[-1]/1e6:.1f}ms / "
                  f"b2a={b2a_acc[-1]/1e6:.1f}ms = {p:.1f}%)")
        else:
            print("ok")

    results: Dict = {k: _stats(v) for k, v in accum.items()}
    results["_meta"] = {
        "runs": args.runs, "warmup": args.warmup,
        "binary": str(HT_BIN),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    def pct(num_key: str, den_key: str) -> Optional[float]:
        n = results[num_key].get("mean")
        d = results[den_key].get("mean")
        if n is None or d is None or d == 0:
            return None
        return round(n / d * 100, 2)

    b2a1 = results["b2a1_expand"]["mean"] or 0
    o3a  = results["o3a_expand"]["mean"]  or 0
    o4a1 = results["o4a1_expand"]["mean"] or 0
    b2a  = results["b2a_total"]["mean"]   or 0
    o3   = results["o3_total"]["mean"]    or 0
    o4a  = results["o4a_total"]["mean"]   or 0
    off_ns = results["offline_ns"]["mean"] or 0
    on_ns  = results["online_ns"]["mean"]  or 0
    expand_total = b2a1 + o3a + o4a1
    idpf_total   = b2a  + o3  + o4a
    tot_ns       = off_ns + on_ns

    results["_derived"] = {
        "expand_pct_of_b2a":   pct("b2a1_expand", "b2a_total"),
        "convert_pct_of_b2a":  pct("b2a2_convert", "b2a_total"),
        "tag_pct_of_b2a":      pct("b2a3_tag",     "b2a_total"),
        "expand_pct_of_o3":    pct("o3a_expand",   "o3_total"),
        "convert_pct_of_o3":   pct("o3b_convert",  "o3_total"),
        "tag_pct_of_o3":       pct("o3c_tag",       "o3_total"),
        "expand_pct_of_o4a":   pct("o4a1_expand",  "o4a_total"),
        "convert_pct_of_o4a":  pct("o4a2_convert", "o4a_total"),
        "tag_pct_of_o4a":      pct("o4a3_tag",     "o4a_total"),
        "expand_total_ns":     expand_total or None,
        "idpf_total_ns":       idpf_total   or None,
        "total_ns":            tot_ns       or None,
        "expand_pct_of_total": round(expand_total / tot_ns * 100, 2) if tot_ns and expand_total else None,
        "expand_pct_of_idpf":  round(expand_total / idpf_total * 100, 2) if idpf_total and expand_total else None,
        "idpf_pct_of_total":   round(idpf_total   / tot_ns * 100, 2) if tot_ns and idpf_total else None,
    }

    args.out.write_text(json.dumps(results, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n[expand-fraction] Results written to {args.out}")
    print_summary(results)


if __name__ == "__main__":
    main()
