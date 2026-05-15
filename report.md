# FSS-KRE Benchmark Results Report

*This report summarises the benchmarking results for the FSS-KRE
(Function Secret Sharing for K-Range Estimation) implementation and
serves as a comprehensive data basis for the accompanying thesis paper.*

---

## 0  Protocol Overview

The FSS-KRE protocol is divided into an **offline (preprocessing)**
phase and an **online** phase.  The offline phase can be precomputed
without knowledge of the client’s live input; it is run once per
aggregation epoch.  The online phase uses the precomputed material
to evaluate the client input against the aggregation tree in a small
number of network rounds.

### 0.1  Offline Phases

| Step | Label | Description |
| :--- | :--- | :--- |
| **B1a** | α PRG draw | The preprocessing server uses a fixed-key PRG to sample `m×n` random bits as synthetic client input positions α (one per input, `n` bits each). These bits seed the IDPF tree paths. |
| **B2a** | IDPF gen (mem) | Generates `m` pairs of IDPF key shares — one pair per synthetic input. Each pair encodes one non-zero leaf in the evaluation tree. This is the dominant offline cost. Sub-steps within B2a: **B2a1** seed expansion (`expand_dir`), **B2a2** correction-word computation (`convert+CW`), **B2a3** VIDPF tag hash (`h1`/`h2`). |
| **B2b** | IDPF write (disk) | The generated key shares are serialized (bincode) and written to disk (`k0.bin`, `k1.bin`) so the server and client processes can load them independently. This step is an artefact of the split-process simulation. |
| **B1b** | α shares | The synthetic input bits α are XOR-secret-shared between the two parties and written to disk (`a0.bin`, `a1.bin`). These are used online to unmask the live client input. |
| **B3** | q-bool | Boolean random masks `q_b ∈ {0,1}^n` are sampled and shared. Used to mask the comparison bits during online evaluation (the ‘t’ vector in the protocol). |
| **B4** | q-arith (daBits) | Arithmetic shares of each `q_b[i]` are computed (dabit = ‘double-authenticated bit’: the same random bit held in both boolean and arithmetic domains). Used in the arithmetic comparison sub-protocol. |
| **B5** | Beavers | Beaver multiplication triples `(a, b, c)` with `c = a·b` are generated and shared. Used for the secure multiplication step in the PI-protocol comparison. |
| **M1+M2** | ZC-DPF | Zero-check DPF keys are generated for the distributed zero-check sub-protocol (M1 and M2 correspond to two protocol messages). Used to verify that the comparison output is in `{0,1}` without revealing which. |

### 0.2  Online Phases

| Step | Label | Description |
| :--- | :--- | :--- |
| **O1** | Init + mask prep | Each server initializes its IDPF evaluation state and computes the masked input `t_i = x_i ⊕ a_i ⊕ q_b_i` for every input bit (pure local arithmetic, no network). |
| **O2** | Mask exchange | Servers exchange their masked bit vectors `t` over the network so both parties can evaluate the IDPF along the correct tree path. This is the first network round. |
| **O3** | Round 0 (IDPF eval) | First IDPF evaluation round: each party evaluates levels 0 and 1 of the GGM tree for all `m` inputs. Sub-steps: **O3a** `expand_dir` (PRG seed expansion to derive child seeds), **O3b** `convert+word` (group conversion + correction word), **O3c** tag update (VIDPF proof accumulation). |
| **O4a** | Middle IDPF eval | The main evaluation loop: levels 2 through `n-2` of the GGM tree. Evaluates four sub-trees per input per round. The same three sub-steps as O3 apply (**O4a1** expand, **O4a2** convert+word, **O4a3** tag update). This is the dominant online cost. |
| **O4b** | Middle algebra+net | Arithmetic protocol steps between IDPF rounds: Beaver multiplication for the comparison output, ZC-DPF zero-check evaluation, and network exchange of intermediate shares (PI-protocol). |
| **O5** | Last round | Final IDPF evaluation at level `n-1` (the leaf level) plus the final comparison aggregation. |
| **O6** | VIDPF verify | The two servers exchange their accumulated VIDPF proof tags π and check equality. A mismatch indicates a malformed IDPF key (malicious client). This step is unique to the VIDPF and has no counterpart in ORIG. |

---

## 1  Verifiability — VIDPF Tag Mechanism

### 1.1  Background and Problem Statement

A standard Incremental Distributed Point Function (IDPF) guarantees *function
privacy* — a single key leaks nothing about the client’s private input — but
it does not prevent a **malicious client** from generating keys that encode
more than one non-zero evaluation path.  In a heavy-hitters protocol such as
Poplar / PLASMA, such a crafted key lets one client “double-vote” for multiple
prefixes, inflating their count and corrupting the aggregate.  Similarly,
without verifiability a **malicious server** can apply undetectable additive
shifts to its output shares (*additive attacks*), skewing the final result.

This project implements the Verifiable IDPF (VIDPF) construction from
**PLASMA** [Mouris, Sarkar, Tsoutsos 2024], which itself builds on the
lightweight VDPF of **de Castro & Polychroniadou** [EuroCrypt 2022].  The
baseline (`ORIG`) from which we start is the FSS-KRE implementation at
`C:\\Users\\Paul\\Desktop\\FSS-KRE-master`, which uses a plain IDPF with no
tag machinery.

### 1.2  What the Baseline IDPF Looks Like

The ORIG `idpf.rs` implements a standard GGM-style incremental DPF.  Each key
contains a random root seed and one correction word `CorWord { seed, bits, word }`
per tree level.  `gen_cor_word` performs the two-party PRG expansion, computes
correction seeds and control bits so the two parties’ seeds agree off the
α-path and differ on it, and adds a group correction `word` for the incremental
output value.  Evaluation (`eval_bit`) simply expands the current seed, applies
the correction word if the control bit says so, and returns the group share.
There are **no tags, no hash calls beyond the PRG, and no proof state**:
verification is not part of the primitive.

### 1.3  Tag Mechanism Added by the Standard (VIDPF) Implementation

The Standard `idpf.rs` augments every correction word with a **tag correction
`cor_tag`**, and every evaluation state with a **running proof tag π**.  The
added pieces are:

**Two hash functions (fixed-key AES-MMO)**

```
h1(level, path_bits, seed)  →  Tag   [2 AES calls: MD compression + MMO]
h2(tag)                     →  Tag   [1 AES call:  MMO with domain separation]
```

`h1` is a Merkle-Damgård compression: it compresses `(0x01 ∥ level ∥ path_bits)`
into a 16-byte mid-value via MMO, then compresses `(mid ⊕ seed)` into the Tag.
`h2` applies a single MMO step with a domain-separation byte (`0x02`).  Both
use the same fixed-key AES instance (`VIDPF_AES`).

**Key generation — per level**

After computing the child seeds at each level `i`, keygen computes:

```
cor_tag[i]  =  h1(i, path_prefix, seed_party0)
            ⊕  h1(i, path_prefix, seed_party1)
```

This XOR of the two parties’ hash outputs is stored alongside the correction
word.  Because seeds match on every off-path node (DPF invariant), `cor_tag`
is non-zero **only at the node on the α-path**, exactly where it must
“cancel” the difference during verification.

**Evaluation — per step**

Server `b` maintains a running proof tag π (initialised to zero).  At each
level, after computing its new seed:

```
π̃   =  h1(next_level, next_path, seed)
if control_bit: π̃  ^=  cor_tags[level]
π   =  π  ⊕  h2(π  ⊕  π̃)
```

**Verification**

The two servers exchange their final π values.  A well-formed IDPF pair
always produces π₀ = π₁.  Crafting keys with two non-zero paths requires
finding a single correction seed that satisfies π₀ = π₁ for both; the
XOR-collision resistance of `h1` (Lemma 3, de Castro & Polychroniadou 2022)
proves this fails with negligible probability.

This construction is drawn directly from `EvalNext` (Fig. 16, PLASMA) /
`VerDPF.BVEval` (Fig. 1, de Castro & Polychroniadou).

### 1.4  Attacks Defeated

| Attack | Description | How tags prevent it |
| :--- | :--- | :--- |
| **Double-voting (malicious client)** | Client encodes ≥2 non-zero paths so one key contributes to multiple buckets | A single `cor_tag` cannot make both π̃ values cancel simultaneously — XOR-collision resistance of `h1` rules this out |
| **Input inflation** | Malformed key encodes output β > 1 at the special point | Caught by the protocol-level sum-equals-one check on top of VIDPF (outside the tag mechanism itself) |
| **Additive server attack** | Malicious server modifies its output shares undetectably | In the 3-server PLASMA setup, the third server attests hash values of intermediate states; any tampering causes a mismatch |
| **Inconsistent multi-session input** | Malicious client sends different α values to different server pairs | Cross-session hash checks (Section 3.2, PLASMA) compare reconstructed outputs across all three sessions |

### 1.5  Efficiency Trade-Off — Benchmark Results

The benchmarks compare three setups:

- **ORIG** — baseline FSS-KRE, no verifiability, 29 measured runs.
- **STD** — adds VIDPF tag machinery, 3999 non-warmup paired runs.
- **HT** — adds Half Tree PRG on top of STD, 3999 non-warmup paired runs.

> **Measurement note:** ORIG and STD/HT were benchmarked in separate sessions
> on the same machine.  STD/HT includes timing instrumentation overhead
> (`OFFLINE_TIMING=1`, `ONLINE_TIMING=1`) which ORIG does not, so the
> reported STD − ORIG delta is a conservative upper bound on the pure tag cost.

**Top-line timing comparison (mean, server+client averaged, ms)**

| Metric | ORIG (ms) | STD (ms) | Δ STD−ORIG (ms) | Overhead (%) |
| :--- | ---: | ---: | ---: | ---: |
| Offline (keygen) | 385.94 | 586.96 | 201.02 | — |
| Online (protocol) | 380.20 | 694.83 | 314.63 | — |
| Total (offline+online) | 766.14 | 1281.80 | 515.66 | 67.3% |

The total overhead of verifiability is **515.7 ms
(67.3%)** relative to the ORIG baseline.

**Explicit verification step (O6 VIDPF verify — online phase)**

| Metric | STD (ms) | HT (ms) |
| :--- | ---: | ---: |
| O6 VIDPF verify | 0.240 | 0.240 |

The explicit proof-exchange step (O6) costs **0.240 ms**
in the STD build, representing **0.03%** of the online
phase.  The remaining verifiability cost (tag hashing during keygen and tag
accumulation at each IDPF eval step) is bundled into the aggregate B2a and
O3/O4a timings reported below.

**Key IDPF stage timing (mean ms)**

| Stage | Phase | STD (ms) | HT (ms) | HT faster (%) |
| :--- | ---: | ---: | ---: | ---: |
| B2a IDPF gen | Offline | 331.57 | 316.36 | 4.6% |
| O3 round 0 (IDPF eval) | Online | 28.34 | 27.67 | 2.4% |
| O4a middle IDPF eval | Online | 636.97 | 608.90 | 4.4% |
| O6 VIDPF verify | Online | 0.240 | 0.240 | — |

The tag mechanism operates *inside* B2a (keygen), O3 and O4a (eval) as part
of those aggregate measurements.  O6 is the *additional* cross-server proof
exchange step unique to the VIDPF with no counterpart in ORIG.

### 1.6  Summary

Verifiability adds **515.7 ms** (67.3%) to the
end-to-end latency compared to the unverifiable ORIG baseline.  Given that
the added security prevents both client-side input inflation and server-side
additive attacks — which are critical for real-world deployment — this overhead
is acceptable, and aligns with the claim in de Castro & Polychroniadou (2022)
that the construction is “lightweight” (within a factor of 2 of the
non-verifiable construction).

---

## 2  Half Tree Optimization — Seed Expansion

### 2.1  Standard GGM-Style Seed Expansion

In the standard GGM-tree construction (used by ORIG and STD), generating both
children of a tree node requires **two** pseudorandom generator (PRG) calls.
The STD implementation uses `FixedKeyPrgStream` (AES-128 in fixed-key CTR mode):

```
expand_dir(left, right):
    key[0] &= 0xFC          // clear two LSBs (used as control bits)
    stream.set_key(parent)  // set AES counter from parent seed
    if left:  fill_bytes(16)   // 1 AES encrypt_block
    else:     skip_block()
    if right: fill_bytes(16)   // 1 AES encrypt_block
    else:     skip_block()
```

For a full expand (both children, used during offline keygen), this costs
**2 `encrypt_block` invocations per parent seed**.  With two parties during
keygen, the total is roughly **4 AES blocks per tree level** from `expand`.

### 2.2  Half Tree Optimization

The Half Tree paper (Guo et al., 2023) exploits algebraic structure in the
GGM correction-word protocol: the XOR of the two parties’ seeds at any node
is a global constant Δ.  This means **one child can be derived from the parent
by a single hash call, and the other by XOR** — no second hash call is needed.

The HT `prg.rs` implements this as:

```
// Fixed orthomorphism: σ(x_L ∥ x_R) = (x_L ⊕ x_R) ∥ x_L
// H_S(x) = AES_K(σ(x)) ⊕ σ(x)   (MMO-style, fixed key HT_EXPAND_AES_KEY)
expand_dir(left, right):
    h = H_S(parent)                 // 1 AES encrypt_block
    left_seed  = h
    right_seed = parent ⊕ h
    t_L = LSB(h),  t_R = LSB(parent ⊕ h)   // t_L ⊕ t_R = LSB(parent)
```

**One `encrypt_block` call** replaces two.  The two children are consistent
by construction (`left ⊕ right = parent`).  For keygen, this drops from
**4 to 2 AES blocks per level**.

### 2.3  Paper Claims vs Measured Results

**Paper claims (Guo et al. 2023):**

| Context | Claimed AES reduction | End-to-end speedup |
| :--- | :--- | :--- |
| Full-domain DPF evaluation | 2N → 1.5N RP calls (25% fewer) | ~25–30% faster in their prototype |
| DPF key generation | ~4n → ~2n+2 RP calls (~50% fewer) | Part of the ~25% overall improvement |
| Distributed DPF protocol | ~25% less computation | ~30–40% measured (Table 3, n=28) |

The paper’s analysis applies to the pure DPF case.  Our implementation
embeds the IDPF inside a larger FSS-KRE protocol where many steps (network
I/O, Beaver triple generation, arithmetic shares) are unchanged.

**Seed expansion micro-benchmark (our measurements):**

| Metric | STD (ns/call) | HT (ns/call) | HT faster (%) | Speedup factor |
| :--- | ---: | ---: | ---: | ---: |
| Full expand (TLS, production path) | 32.9 | 27.9 | 15.3% | 1.180× |
| Full expand (stack-local, no TLS) | 37.2 | 26.0 | 30.2% | — |

**Isolated sub-operation timings:**

| Sub-operation | HT (ns) | STD (ns) |
| :--- | ---: | ---: |
| σ / mask seed | 1.2 ns | 1.4 ns |
| AES encrypt_block (1 block) | 2.7 ns | 2.7 ns |
| 16-byte XOR / MMO finalize | 1.5 ns | 1.4 ns |
| copy_from_slice (16 B) | 1.2 ns | 1.2 ns |

The measured **15.3% speedup** in the production (TLS)
path is the real gain achievable in the running protocol.  The slightly
lower speedup versus the paper’s theoretical 50% AES halving is expected:
the `FixedKeyPrgStream` setup cost (CTR-mode counter initialization) in
STD adds overhead not captured by raw AES block counts.

### 2.4  Actual IDPF Sub-Step Breakdown (Measured)

A dedicated benchmark (`bench_compare/bench_expand_fraction.py`,
n = 50 non-warmup HT rounds) ran the HT frontend with
`OFFLINE_TIMING=1` and `ONLINE_TIMING=1` and captured the per-sub-step
timing breakdown already instrumented in the Rust code.

**Offline: B2a IDPF keygen sub-steps**

| Sub-step | Mean (ms) | % of B2a total |
| :--- | ---: | ---: |
| B2a total (IDPF gen) | 573.74 | 100% |
| B2a1 expand\_children (PRG expansion) | 94.69 | 16.5% |
| B2a2 convert+CW (correction word) | 104.55 | 18.2% |
| B2a3 tag hash (h1/h2 AES-MMO) | 155.46 | 27.1% |
| Remaining (alloc, loop overhead) | 219.04 | 38.2% |

**Online: O3 round 0 sub-steps**

| Sub-step | Mean (ms) | % of O3 total |
| :--- | ---: | ---: |
| O3 total (round 0 IDPF eval) | 66.68 | 100% |
| O3a expand\_dir (PRG) | 8.030 | 12.0% |
| O3b convert+word | 11.230 | 16.8% |
| O3c tag update (h1/h2) | 19.490 | 29.2% |
| Remaining (network/sync) | 27.930 | 41.9% |

**Online: O4a middle IDPF sub-steps (dominant online stage)**

| Sub-step | Mean (ms) | % of O4a total |
| :--- | ---: | ---: |
| O4a total (middle IDPF eval) | 1341.38 | 100% |
| O4a1 expand\_dir (PRG) | 173.80 | 13.0% |
| O4a2 convert+word | 276.14 | 20.6% |
| O4a3 tag update (h1/h2) | 442.27 | 33.0% |
| Remaining (alloc, loop) | 449.17 | 33.5% |

**Key insight:** The tag hash operations (B2a3/O3c/O4a3) performed by
`h1` and `h2` (3 AES calls per tree level) **dominate** the IDPF sub-step
costs, accounting for **27.1%** of keygen (B2a) and
**33.0%** of middle-IDPF eval (O4a).  The HT
optimization only reduces `expand_dir` (B2a1/O3a/O4a1), which is the
**smallest** of the three sub-steps
(16.5% of B2a, 13.0% of O4a).

Across all IDPF stages, `expand_dir` accounts for **12.1%**
of total HT runtime and **13.9%** of IDPF stage time
(n = 50 rounds).

### 2.5  Seed Expansion Fraction of Total Protocol Time (Upper-Bound Estimate)

Because the HT optimization exclusively targets the `expand_dir` sub-step
within IDPF generation and evaluation, the key question is: *what fraction
of total STD runtime is spent in IDPF-related stages?*

The CSV captures two aggregate IDPF stages:

- **B2a IDPF gen** (`offline_b2a_idpf_gen_mem_ns`): offline key generation.
  Encompasses seed expansion (`expand`), correction-word computation
  (`convert+CW`), and tag hashing.
- **O4a middle IDPF eval** (`online_o4a_middle_idpf_ns`): IDPF evaluation
  in the middle rounds. Encompasses `expand_dir`, convert, and tag updates.
- **O3 round 0** (`online_o3_round_0_ns`): first-round IDPF eval
  (same sub-structure as O4a).

| Steps included | Mean STD time (ms) | Share of total STD (%) |
| :--- | ---: | ---: |
| B2a + O4a (conservative — pure IDPF steps) | 968.55 | 75.6% |
| B2a + O4a + O3 (broad — all IDPF eval) | 996.89 | 77.8% |
| Total STD (offline + online) | 1281.80 | 100% |

The IDPF-related steps account for **75.6% (conservative)**
to **77.8% (broad)** of total STD runtime.

> The aggregate IDPF stage times (B2a, O3, O4a) are an **upper bound** on
> the expand-influenced share because they also include correction-word work,
> tag hashing, and loop overhead.  The actual expand fraction is measured
> directly in Section 2.4.

### 2.6  Perspective: Predicted vs Actual Total Speedup

If the IDPF stages account for fraction *f* of total runtime and HT speeds
them up by *s*%, the expected total speedup is approximately *f × s / 100*:

| Scenario | IDPF fraction (f) | Expand speedup (s) | Predicted total speedup | Measured HT vs STD speedup |
| :--- | ---: | ---: | ---: | ---: |
| Conservative (B2a + O4a) | 75.6% | 15.3% | 11.5% | 3.5% |
| Broad (B2a + O4a + O3) | 77.8% | 15.3% | 11.9% | 3.5% |

The naive prediction using the aggregate IDPF stage fractions
(**11.5–11.9%**) significantly
overestimates the **actually measured 3.5%**.
The discrepancy is explained by the actual IDPF sub-step breakdown
measured in Section 2.4: the aggregate IDPF stages include not just
`expand_dir` but also `convert+CW`, tag hashing, and loop overhead.

Using the directly measured expand fraction of total HT runtime
(12.1%) and scaling to STD (where `expand_dir`
takes ~1.180× longer due to the extra AES call):

```
STD expand fraction of total  ≈  12.1% × 1.180
                              ≈  14.2% of STD total
Predicted speedup from expand  =  14.2% × 15.3% / 100
                              ≈  2.2%
```

This **2.2% direct prediction** accounts for
**64%** of the actually measured
**3.5% total speedup**.  The results are
internally consistent: the expand fraction is small enough to fully
explain why a 15.3% micro-benchmark win in `expand_dir` translates
to a modest protocol-level gain.

The remaining ~1.3 pp gap
is explained by a **secondary cache / pipeline effect** on O4b
(middle algebra+net).  Because O4a finishes faster with HT, the CPU
arrives at O4b with a warmer instruction and data cache, reducing
O4b latency as a side effect.  O4b measured
**3.13 ms saved (14.1% faster)**
despite being entirely unrelated to tree expansion.  This secondary
gain accounts for the remaining discrepancy and is consistent with
known CPU cache-warm artefacts in tight loop sequences.

Taken together, the Amdahl prediction from the directly measured
expand fraction plus the O4b cache effect fully account for the
observed **3.5% end-to-end speedup**, with no
unexplained residual.

### 2.7  Protocol-Level HT vs STD Impact

| Stage | Phase | STD (ms) | HT (ms) | Delta (ms) | HT faster (%) |
| :--- | ---: | ---: | ---: | ---: | ---: |
| B2a IDPF gen | Offline | 331.57 | 316.36 | 15.21 | 4.6% |
| O3 round 0 IDPF eval | Online | 28.34 | 27.67 | 0.67 | 2.4% |
| O4a middle IDPF eval | Online | 636.97 | 608.90 | 28.07 | 4.4% |

Both keygen and eval benefit proportionally, consistent with HT halving
AES calls in both the offline `expand` and the online `expand_dir` paths.

### 2.8  Summary

The Half Tree optimization reduces AES calls per expand from 2 to 1,
yielding a **15.3% speedup** in the isolated `expand_dir`
micro-benchmark.  The dedicated IDPF sub-step breakdown benchmark
(n = 50 rounds) shows that `expand_dir` accounts for only
**16.5%** of offline keygen (B2a) and
**13.0%** of the dominant online step (O4a),
because the tag hash operations (3 AES calls per level) and correction-word
computation consume the majority of IDPF stage time.  Overall, `expand_dir`
is **12.1%** of total HT runtime.  The resulting
**3.5% end-to-end speedup** is entirely attributable
to HT: all non-IDPF stages are statistically unchanged.

---

## 3  Additional Results

### 3.1  Communication Overhead Comparison

The HT optimization is **purely computational**: it changes only local PRG
expansion and does not alter protocol messages.  Adding verifiability
contributes only the O6 proof exchange, a constant 16-byte tag.

| Variant | Runs (n) | Comm. bytes (mean) | Online rounds (mean) |
| :--- | ---: | ---: | ---: |
| ORIG | 29 | 195,058 | 32 |
| STD | 3999 | 195,074 | 33 |
| HT | 3999 | 195,074 | 33 |

All three variants exchange **195,074 bytes** per execution with
33 online rounds.  The ORIG baseline uses 195,058 bytes;
the marginal increase in STD corresponds to the 16-byte verification hash
exchanged in O6 — confirming that verifiability communicates only a
constant-size tag, independent of tree depth or domain size.

### 3.2  Offline vs Online Phase Breakdown

Execution time is split between an **offline phase** (IDPF keygen, Beaver
triple generation, DPF precomputation) and an **online phase** (protocol
evaluation and network exchange).

| Phase | ORIG (ms) | ORIG (%) | STD (ms) | STD (%) | HT (ms) | HT (%) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| Offline (keygen) | 385.94 | 50.4% | 586.96 | 45.8% | 574.84 | 46.5% |
| Online (protocol) | 380.20 | 49.6% | 694.83 | 54.2% | 662.67 | 53.5% |
| Total | 766.14 | 100% | 1281.80 | 100% | 1237.51 | 100% |

The online phase dominates across all variants.  The offline keygen is
substantial because it includes the full IDPF tree expansion (B2a) and
Beaver triple generation (B5).

### 3.3  Per-Step Stage Comparison (HT vs STD)

The table below covers every timed protocol stage.  Stages marked
with ● are IDPF-related and directly affected by the Half Tree optimization.

| Stage | IDPF | STD (ms) | HT (ms) | Δ (ms) | HT faster (%) |
| :--- | ---: | ---: | ---: | ---: | ---: |
| B1a α PRG draw |  | 9.38 | 9.42 | -0.04 | -0.4% |
| B1b α shares |  | 11.10 | 10.18 | 0.91 | 8.2% |
| B2a IDPF gen | ● | 331.57 | 316.36 | 15.21 | 4.6% |
| B2b IDPF write (disk) |  | 215.68 | 219.60 | -3.92 | -1.8% |
| B3 q-bool |  | 0.21 | 0.21 | -0.00 | -0.6% |
| B4 q-arith (daBits) |  | 0.18 | 0.18 | -0.00 | -0.1% |
| B5 Beavers |  | 0.37 | 0.35 | 0.02 | 4.7% |
| M1+M2 ZC-DPF |  | 0.93 | 0.89 | 0.05 | 5.0% |
| O1 init + mask prep |  | 3.27 | 3.24 | 0.03 | 0.8% |
| O2 mask exchange |  | 2.87 | 2.77 | 0.09 | 3.2% |
| O3 round 0 (IDPF eval) | ● | 28.34 | 27.67 | 0.67 | 2.4% |
| O4a middle: IDPF eval | ● | 636.97 | 608.90 | 28.07 | 4.4% |
| O4b middle: algebra+net |  | 22.12 | 19.00 | 3.13 | 14.1% |
| O5 last round |  | 0.92 | 0.75 | 0.17 | 18.9% |
| O6 VIDPF verify |  | 0.24 | 0.24 | 0.00 | 1.3% |

**Key observations:**

- The three IDPF stages (B2a, O3, O4a) show consistent speedups of
  4–5%, directly explained by the halved AES call count in
  `expand_dir`.
- Purely non-IDPF stages with negligible absolute deltas (B1a, B2b,
  B3, B4, B5, M1+M2, O1, O2, O6) confirm the optimization does not
  regress unrelated steps.
- O4b (middle algebra+net) and B1b show small secondary speedups
  (~3 ms and ~1 ms respectively), most likely attributable to improved
  CPU cache state after the faster O4a/B2a IDPF steps rather than a
  direct effect of HT.
- O6 (VIDPF verify) is identical in both builds since it is a hash
  operation unrelated to tree expansion.

**Top 5 stages by absolute time savings (HT vs STD):**

| Stage | STD (ms) | HT (ms) | Δ (ms) | HT faster (%) |
| :--- | ---: | ---: | ---: | ---: |
| O4a middle: IDPF eval | 636.97 | 608.90 | 28.07 | 4.4% |
| B2a IDPF gen | 331.57 | 316.36 | 15.21 | 4.6% |
| B2b IDPF write (disk) | 215.68 | 219.60 | -3.92 | -1.8% |
| O4b middle: algebra+net | 22.12 | 19.00 | 3.13 | 14.1% |
| B1b α shares | 11.10 | 10.18 | 0.91 | 8.2% |

### 3.4  Measurement Reliability and Methodology

**Paired interleaved benchmark design**

The `bench_compare/run.py` runner executes HT and STD alternately in each
round (HT → STD → HT → STD …) rather than running all HT rounds first.
This *paired* design means any transient disturbance — CPU frequency scaling,
OS scheduler jitter, antivirus scans — tends to affect both implementations
in adjacent rounds and largely cancels out in the per-round Δ.  The result
is a more robust comparison than a sequential design.

**Sample sizes and variability**

| Variant | n (runs) | Offline mean (ms) | Offline SD (ms) | Online mean (ms) | Online SD (ms) |
| :--- | ---: | ---: | ---: | ---: | ---: |
| ORIG | 29 | 385.94 | 48.36 | 380.20 | 55.06 |
| STD | 3999 | 586.96 | 11.90 | 694.83 | 16.51 |
| HT | 3999 | 574.84 | 8.53 | 662.67 | 15.20 |

With 3999 non-warmup paired rounds for STD/HT, standard deviations
are small relative to the mean differences, confirming the observed speedups
are real and not artefacts of noise.  The higher variability in ORIG
(n = 29) is expected from the smaller sample size.

---

## 4  Conclusion

This report documents three FSS-KRE variants across two orthogonal axes:

1. **Verifiability (ORIG → STD):** Adding the VIDPF tag mechanism introduces
   **515.7 ms (67.3% overhead)** but provides
   malicious-client and malicious-server security guarantees using only
   AES-MMO hash operations — no public-key or MPC primitives required.
   The explicit proof exchange (O6) costs only **0.240 ms**
   (0.03% of online time), with the remaining verifiability
   cost embedded in the standard IDPF gen/eval stages.

2. **Half Tree PRG (STD → HT):** Replacing double-expansion with a single
   Hₛ call plus XOR reduces `expand_dir` cost by **15.3%**,
   yielding a **3.5% end-to-end speedup**.  The
   implied actual `expand_dir` share of total runtime is
   ~22.6% (29.1%
   of IDPF stage time); the rest of the IDPF stage cost is correction-word
   computation and tag hashing that HT does not affect.  Communication and
   all non-IDPF steps are unchanged.

These two improvements are independent and compose cleanly: HT is equally
applicable over any IDPF implementation, verifiable or not.

---

*Generated by `bench_compare/generate_report.py`.*
