# FSS-KRE Thesis Code

> **Based on** the FSS-KRE implementation by Nan Cheng et al., available at
> [github.com/nann-cheng/FSS-KRE](https://github.com/nann-cheng/FSS-KRE), accompanying the paper
> *Efficient Two-Party Secure Aggregation via Incremental Distributed Point Function*.
> The `HT/` and `Standard/` directories contain modified forks of that codebase;
> the `bench_compare/` tooling is original work added for this thesis.

Benchmark infrastructure for a Bachelor's thesis comparing three variants of the
FSS-KRE (Function Secret Sharing – Key Ranking Extension) protocol:

| Variant | Description |
|---------|-------------|
| **ORIG** | Baseline FSS-KRE checkout (`C:\Users\Paul\Desktop\FSS-KRE-master`). Plain IDPF, no verifiability. |
| **VRF** | Fork that adds VIDPF tag machinery for verifiability (Standard variant). |
| **HT** | Fork that applies the Half-Tree optimisation on top of VRF, halving the AES call count inside `expand_dir`. |

ORIG and VRF/HT are compared along two orthogonal axes:

1. **Verifiability cost** (ORIG → VRF): overhead of adding the VIDPF tag mechanism.
2. **Half-Tree speedup** (VRF → HT): savings from the reduced-AES expansion.

---

## Repository Layout

```
Code/
├── HT/                         # Half-Tree fork
│   └── BA-Thesis-Code/
│       └── FSS-KRE-master/
│           ├── frontend/       # Binary entry point (Rust)
│           ├── libfss/         # FSS primitives incl. HT expand_dir
│           └── libmpc/         # MPC protocol logic
│
├── Standard/                   # VRF fork (verifiable, no HT)
│   └── BA-Thesis-Code/
│       └── FSS-KRE-master/
│           ├── frontend/
│           ├── libfss/
│           └── libmpc/
│
├── bench_compare/              # All benchmark runners and scripts
│   ├── run.py                      # Main HT vs VRF protocol benchmark
│   ├── bench_expand_compare.py     # Seed-expansion microbenchmark (HT vs VRF)
│   ├── bench_expand_fraction.py    # Expand sub-step fraction analysis (HT only)
│   ├── run_external_fss.py         # ORIG baseline benchmark
│   ├── generate_report.py          # Assembles report.md from all result files
│   └── README.md                   # bench_compare-specific usage reference
│
├── report.md                   # Auto-generated benchmark report (from generate_report.py)
├── .gitignore
└── README.md                   # This file
```

---

## Prerequisites

- Python 3.9+
- Rust / `cargo` on PATH
- Prebuilt release binaries in each project's `frontend/target/release/` (or pass `--build`)

Build both frontends:

```powershell
cargo build --release --manifest-path HT\BA-Thesis-Code\FSS-KRE-master\frontend\Cargo.toml
cargo build --release --manifest-path Standard\BA-Thesis-Code\FSS-KRE-master\frontend\Cargo.toml
```

---

## Benchmarks

All commands are run from the repo root (`Code\`).

### 1. Main protocol benchmark — HT vs VRF (`run.py`)

Runs the full offline + online protocol for HT and VRF in an alternating paired
fashion (HT → VRF → HT → VRF …) to cancel machine noise. Records per-stage
timing for every B-step and O-step.

```powershell
# Run 4000 rounds and save results
python bench_compare\run.py --runs 4000 --out bench_compare\results.csv

# Re-print summary from saved results (no re-run)
python bench_compare\run.py --summarize --out bench_compare\results.csv
```

**Output:** `bench_compare\results.csv`

**Timing stages recorded:**

| Stage | Description |
|-------|-------------|
| B1a | α PRG draw |
| B2a | IDPF key generation (in memory) |
| B2b | IDPF key write to disk |
| B1b | α secret sharing |
| B3–B5 | Boolean / arithmetic correlated randomness, Beaver triples |
| M1+M2 | ZC-DPF material |
| O1–O6 | Online protocol rounds (mask exchange, IDPF eval, VIDPF verify, …) |

---

### 2. Seed-expansion microbenchmark — HT vs VRF (`bench_expand_compare.py`)

Measures the isolated `expand_dir` operation (one PRG seed → two child seeds)
for both variants using their respective `libfss` example binaries. Alternates
rounds the same way as the main benchmark.

```powershell
# Run and save
python bench_compare\bench_expand_compare.py --runs 100 --out bench_compare\expand_results.csv

# Re-print summary from saved results
python bench_compare\bench_expand_compare.py --summarize --out bench_compare\expand_results.csv
```

**Output:** `bench_compare\expand_results.csv`

**Metrics compared:**

| Metric | What it measures |
|--------|-----------------|
| Full expand (no TLS) | Complete expand call with stack-local AES state |
| Full expand (TLS) | Complete expand call using production thread-local AES state |
| Isolated: mask / sigma | STD: 2-bit seed mask; HT: σ orthomorphism |
| Isolated: AES block | Single raw AES-128 `encrypt_block` call |
| Isolated: 16-byte XOR | MMO finalisation XOR (HT) / CTR-mode output XOR (VRF) |
| Isolated: left child copy | 16-byte `copy_from_slice` for the left child seed |

---

### 3. Expand sub-step fraction analysis — HT only (`bench_expand_fraction.py`)

Runs the HT frontend binary with sub-step timing enabled (`OFFLINE_TIMING=1`,
`ONLINE_TIMING=1`) to measure what fraction of each IDPF stage is spent in
`expand_dir` vs. `convert+CW` vs. tag hashing.

```powershell
# Run 50 rounds and save
python bench_compare\bench_expand_fraction.py --runs 50

# Re-print summary from saved JSON (no re-run)
python bench_compare\bench_expand_fraction.py --summarize
```

**Output:** `bench_compare\expand_fraction_results.json`

**Sub-steps analysed:**

| Step | Sub-steps |
|------|-----------|
| B2a (offline IDPF gen) | B2a1 expand, B2a2 convert+CW, B2a3 tag hash |
| O3 (online round 0) | O3a expand, O3b convert+word, O3c tag update |
| O4a (online middle IDPF) | O4a1 expand, O4a2 convert+word, O4a3 tag update |

---

### 4. ORIG baseline benchmark (`run_external_fss.py`)

Benchmarks the unmodified external FSS-KRE checkout at
`C:\Users\Paul\Desktop\FSS-KRE-master`. Produces top-level offline/online
timings only (no sub-step breakdown). Used to quantify the verifiability
overhead (ORIG → VRF).

```powershell
python bench_compare\run_external_fss.py --runs 30
python bench_compare\run_external_fss.py --runs 30 --out-xlsx bench_compare\external_fss_results.xlsx
```

**Output:** `bench_compare\external_fss_results.csv`, `bench_compare\external_fss_results.xlsx`

---

### 5. Report generation (`generate_report.py`)

Reads all four result files and writes a combined Markdown report to `report.md`.

```powershell
python bench_compare\generate_report.py
```

Requires all result files to exist. Run the four benchmarks above first.

---

## Result Files

Result files are listed in `.gitignore` and are not committed to the repository.

| File | Produced by | Format |
|------|-------------|--------|
| `bench_compare\results.csv` | `run.py` | CSV, one row per (run, project, party) |
| `bench_compare\expand_results.csv` | `bench_expand_compare.py` | CSV, one row per (run, project, metric) |
| `bench_compare\expand_fraction_results.json` | `bench_expand_fraction.py` | JSON, mean/sd/min/max per sub-step |
| `bench_compare\external_fss_results.csv` | `run_external_fss.py` | CSV, one row per (run, party) |
| `bench_compare\external_fss_results.xlsx` | `run_external_fss.py` | Excel workbook with raw + summary sheets |
| `report.md` | `generate_report.py` | Markdown benchmark report |
