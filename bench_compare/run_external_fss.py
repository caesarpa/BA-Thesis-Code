"""
Benchmark runner for the external FSS-KRE checkout at
`C:\\Users\\Paul\\Desktop\\FSS-KRE-master`.

The scope of the benchmark is whatever `frontend/src/main.rs` in that checkout
currently selects. At the time this script was added, that file was configured
for `BITWISE_MAX` with `input_size = 50000`.

This runner:
- launches one fresh (server, client) pair per run
- parses the reported offline and online timings from stdout
- writes one CSV row per (run, party)
- writes an XLSX workbook with raw rows and a summary sheet

Usage:

    python bench_compare\\run_external_fss.py --runs 30
    python bench_compare\\run_external_fss.py --runs 30 --build
    python bench_compare\\run_external_fss.py --runs 30 --out-xlsx bench_compare\\external_results.xlsx
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


DEFAULT_PROJECT_ROOT = Path(r"C:\Users\Paul\Desktop\FSS-KRE-master")
THIS_FILE = Path(__file__).resolve()


def _frontend_dir(project_root: Path) -> Path:
    return project_root / "frontend"


def _release_bin(project_root: Path) -> Path:
    exe_name = "frontend.exe" if sys.platform.startswith("win") else "frontend"
    return _frontend_dir(project_root) / "target" / "release" / exe_name


_DUR_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ns|us|\u00b5s|ms|s)\b")
_UNIT_TO_NS = {
    "ns": 1.0,
    "us": 1e3,
    "\u00b5s": 1e3,
    "ms": 1e6,
    "s": 1e9,
}

_RE_OFFLINE = re.compile(r"Offline key generation time:\s*(.+)$")
_RE_ONLINE = re.compile(r"Computation time:\s*(.+?)\s*$")
_RE_ROUNDS = re.compile(r"Online rounds:\s*(\d+)")
_RE_COMM = re.compile(r"Communication volume:\s*(\d+)\s*bytes")

_LISTEN_MARKER = "Start Listening"


@dataclass
class PartyResult:
    party: str
    offline_ns: float | None = None
    online_ns: float | None = None
    online_rounds: int | None = None
    comm_bytes: int | None = None
    raw_lines: list[str] | None = None

    def __post_init__(self) -> None:
        if self.raw_lines is None:
            self.raw_lines = []


def parse_rust_duration_ns(text: str) -> float | None:
    match = _DUR_RE.search(text)
    if not match:
        return None
    return float(match.group("value")) * _UNIT_TO_NS[match.group("unit")]


def update_party_from_line(result: PartyResult, line: str) -> None:
    assert result.raw_lines is not None
    result.raw_lines.append(line)

    if (match := _RE_OFFLINE.search(line)) and result.offline_ns is None:
        result.offline_ns = parse_rust_duration_ns(match.group(1))
    if (match := _RE_ONLINE.search(line)) and result.online_ns is None:
        result.online_ns = parse_rust_duration_ns(match.group(1))
    if (match := _RE_ROUNDS.search(line)) and result.online_rounds is None:
        result.online_rounds = int(match.group(1))
    if (match := _RE_COMM.search(line)) and result.comm_bytes is None:
        result.comm_bytes = int(match.group(1))


def _pump_stdout(
    proc: subprocess.Popen[str],
    result: PartyResult,
    echo_prefix: str | None,
    ready_q: "queue.Queue[str]",
) -> None:
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if echo_prefix is not None:
            print(f"{echo_prefix}{line}")
        update_party_from_line(result, line)
        if _LISTEN_MARKER in line:
            ready_q.put("listening")


def run_one(
    run_idx: int,
    binary: Path,
    frontend_dir: Path,
    echo: bool,
    startup_timeout_s: float,
) -> tuple[PartyResult, PartyResult, int]:
    server_res = PartyResult("server")
    client_res = PartyResult("client")
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
    )
    server_thread = threading.Thread(
        target=_pump_stdout,
        args=(server, server_res, f"[run {run_idx:>4} S] " if echo else None, ready_q),
        daemon=True,
    )
    server_thread.start()

    start_wait = time.time()
    while True:
        try:
            ready_q.get(timeout=0.25)
            break
        except queue.Empty:
            if server.poll() is not None:
                tail = "\n".join((server_res.raw_lines or [])[-20:])
                raise RuntimeError(
                    f"Server exited before listening (run {run_idx}). Last lines:\n{tail}"
                )
            if time.time() - start_wait > startup_timeout_s:
                server.kill()
                raise TimeoutError(
                    f"Server did not start listening within {startup_timeout_s}s "
                    f"(run {run_idx})"
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
    )
    client_thread = threading.Thread(
        target=_pump_stdout,
        args=(client, client_res, f"[run {run_idx:>4} C] " if echo else None, ready_q),
        daemon=True,
    )
    client_thread.start()

    wall_start = time.perf_counter_ns()
    server.wait()
    client.wait()
    wall_ns = time.perf_counter_ns() - wall_start

    server_thread.join(timeout=2.0)
    client_thread.join(timeout=2.0)

    if server.returncode != 0 or client.returncode != 0:
        raise RuntimeError(
            f"Non-zero exit (run {run_idx}): server={server.returncode}, "
            f"client={client.returncode}"
        )

    return server_res, client_res, wall_ns


def build_release(project_root: Path) -> None:
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("`cargo` not found on PATH; cannot --build.")
    frontend_dir = _frontend_dir(project_root)
    print(f"[runner] Building external frontend in release mode at {frontend_dir}")
    subprocess.check_call(
        [cargo, "build", "--release", "--manifest-path", str(frontend_dir / "Cargo.toml")]
    )


def _stats(values: list[float]) -> tuple[int, float, float, float, float]:
    count = len(values)
    if count == 0:
        return (0, 0.0, 0.0, 0.0, 0.0)
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / count
    sd = math.sqrt(variance)
    return (count, mean, sd, min(values), max(values))


def _fmt_ms(ns: float | None) -> str:
    return "n/a" if ns is None else f"{ns / 1e6:.3f} ms"


def _summary_rows(rows: list[dict[str, object]], project_root: Path) -> list[list[object]]:
    measured = [row for row in rows if row["warmup"] is False]
    out: list[list[object]] = [
        ["Project root", str(project_root)],
        ["Scope", "Uses whatever protocol/input is currently selected in frontend/src/main.rs"],
        ["Stats", "Mean and stddev are computed over non-warmup rows only"],
        [],
        ["party", "metric", "n", "mean_ms", "stddev_ms", "min_ms", "max_ms"],
    ]

    for party in ("server", "client"):
        for key, label in (("offline_ns", "offline"), ("online_ns", "online")):
            values = [
                float(row[key])
                for row in measured
                if row["party"] == party and row.get(key, "") != ""
            ]
            count, mean, sd, lo, hi = _stats(values)
            out.append([
                party,
                label,
                count,
                mean / 1e6 if count else "",
                sd / 1e6 if count else "",
                lo / 1e6 if count else "",
                hi / 1e6 if count else "",
            ])

    return out


def print_summary(rows: list[dict[str, object]]) -> None:
    measured = [row for row in rows if row["warmup"] is False]
    if not measured:
        print("[runner] No non-warmup measurements.")
        return

    print("\n[runner] ===== Summary (warmup excluded) =====")
    for party in ("server", "client"):
        for key, label in (("offline_ns", "offline"), ("online_ns", "online")):
            values = [
                float(row[key])
                for row in measured
                if row["party"] == party and row.get(key, "") != ""
            ]
            count, mean, sd, lo, hi = _stats(values)
            print(
                f"  {party:<6} {label:<7} "
                f"n={count:<3} mean={mean/1e6:.3f} ms  sd={sd/1e6:.3f} ms  "
                f"min={lo/1e6:.3f} ms  max={hi/1e6:.3f} ms"
            )


def _col_name(index: int) -> str:
    name = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def _xlsx_cell(cell_ref: str, value: object) -> str:
    if value is None or value == "":
        return f'<c r="{cell_ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{cell_ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{cell_ref}"><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{cell_ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _sheet_xml(rows: list[list[object]]) -> str:
    xml_rows: list[str] = []
    for row_idx, row in enumerate(rows, start=1):
        cells = []
        for col_idx, value in enumerate(row, start=1):
            cell_ref = f"{_col_name(col_idx)}{row_idx}"
            cells.append(_xlsx_cell(cell_ref, value))
        xml_rows.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        + "".join(xml_rows)
        + "</sheetData></worksheet>"
    )


def write_xlsx(path: Path, runs_rows: list[list[object]], summary_rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/worksheets/sheet2.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            '<sheet name="Runs" sheetId="1" r:id="rId1"/>'
            '<sheet name="Summary" sheetId="2" r:id="rId2"/>'
            "</sheets></workbook>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet2.xml"/>'
            '<Relationship Id="rId3" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>",
        )
        zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(runs_rows))
        zf.writestr("xl/worksheets/sheet2.xml", _sheet_xml(summary_rows))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=30, help="Number of runs. Default: 30")
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Number of initial runs to mark as warmup and exclude from summary. Default: 1",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=DEFAULT_PROJECT_ROOT,
        help=f"External project root. Default: {DEFAULT_PROJECT_ROOT}",
    )
    parser.add_argument(
        "--out-csv",
        type=Path,
        default=THIS_FILE.parent / "external_fss_results.csv",
        help="CSV output path. Default: bench_compare/external_fss_results.csv",
    )
    parser.add_argument(
        "--out-xlsx",
        type=Path,
        default=THIS_FILE.parent / "external_fss_results.xlsx",
        help="XLSX output path. Default: bench_compare/external_fss_results.xlsx",
    )
    parser.add_argument("--build", action="store_true", help="Build the external frontend first.")
    parser.add_argument("--echo", action="store_true", help="Echo child stdout live.")
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the server to bind. Default: 30",
    )
    parser.add_argument(
        "--inter-run-sleep",
        type=float,
        default=0.5,
        help="Seconds to sleep between runs. Default: 0.5",
    )
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    frontend_dir = _frontend_dir(project_root)
    binary = _release_bin(project_root)

    if args.build:
        build_release(project_root)

    if not binary.exists():
        print(f"[runner] Binary not found at {binary}", file=sys.stderr)
        sys.exit(1)

    (project_root / "test").mkdir(exist_ok=True)
    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    args.out_xlsx.parent.mkdir(parents=True, exist_ok=True)

    print(f"[runner] Project root: {project_root}")
    print(f"[runner] Binary: {binary}")
    print(f"[runner] Writing CSV to {args.out_csv}")
    print(f"[runner] Writing XLSX to {args.out_xlsx}")

    headers = [
        "run_idx",
        "party",
        "warmup",
        "offline_ns",
        "offline_ms",
        "online_ns",
        "online_ms",
        "online_rounds",
        "comm_bytes",
        "wall_ns",
        "wall_ms",
    ]

    rows: list[dict[str, object]] = []
    with args.out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()

        for run_idx in range(1, args.runs + 1):
            is_warmup = run_idx <= args.warmup
            warmup_tag = " (warmup)" if is_warmup else ""
            print(f"[runner] === run {run_idx}/{args.runs}{warmup_tag} ===")

            try:
                server_res, client_res, wall_ns = run_one(
                    run_idx=run_idx,
                    binary=binary,
                    frontend_dir=frontend_dir,
                    echo=args.echo,
                    startup_timeout_s=args.startup_timeout,
                )
            except Exception as exc:
                print(f"[runner] run {run_idx} failed: {exc}", file=sys.stderr)
                if run_idx < args.runs:
                    time.sleep(max(1.0, args.inter_run_sleep))
                continue

            for result in (server_res, client_res):
                row = {
                    "run_idx": run_idx,
                    "party": result.party,
                    "warmup": is_warmup,
                    "offline_ns": f"{result.offline_ns:.0f}" if result.offline_ns is not None else "",
                    "offline_ms": f"{result.offline_ns / 1e6:.3f}" if result.offline_ns is not None else "",
                    "online_ns": f"{result.online_ns:.0f}" if result.online_ns is not None else "",
                    "online_ms": f"{result.online_ns / 1e6:.3f}" if result.online_ns is not None else "",
                    "online_rounds": result.online_rounds if result.online_rounds is not None else "",
                    "comm_bytes": result.comm_bytes if result.comm_bytes is not None else "",
                    "wall_ns": wall_ns,
                    "wall_ms": f"{wall_ns / 1e6:.3f}",
                }
                writer.writerow(row)
                rows.append(row)
            f.flush()

            print(
                f"           server: offline={_fmt_ms(server_res.offline_ns)} "
                f"online={_fmt_ms(server_res.online_ns)} "
                f"rounds={server_res.online_rounds} bytes={server_res.comm_bytes}"
            )
            print(
                f"           client: offline={_fmt_ms(client_res.offline_ns)} "
                f"online={_fmt_ms(client_res.online_ns)} "
                f"rounds={client_res.online_rounds} bytes={client_res.comm_bytes}"
            )

            if run_idx < args.runs:
                time.sleep(args.inter_run_sleep)

    runs_sheet: list[list[object]] = [headers]
    for row in rows:
        runs_sheet.append([row.get(header, "") for header in headers])
    summary_sheet = _summary_rows(rows, project_root)
    write_xlsx(args.out_xlsx, runs_sheet, summary_sheet)

    print_summary(rows)


if __name__ == "__main__":
    main()
