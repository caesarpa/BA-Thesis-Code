//! Comprehensive benchmark of the CTR-mode seed expansion.
//!
//! Production expansion is `PrgSeed::expand()` -> `expand_dir(true, true)` in
//! `libfss/src/prg.rs`. It has five sequential steps:
//!   A. Mask parent seed (`key_short[0] &= 0xFC`)
//!   B. `set_key(&key_short)` on the fixed-key AES-CTR stream (loads counter)
//!   C. Build `PrgOutput` (extract the two control bits + 2x `PrgSeed::zero`)
//!   D. `fill_bytes` into `out.seeds.0.key`   (left child,  1 AES-CTR block)
//!   E. `fill_bytes` into `out.seeds.1.key`   (right child, 1 AES-CTR block)
//!
//! Section 1 measures the full pipeline with a stack-local stream (no TLS).
//! Section 2 measures levels L1..L5 cumulatively (no TLS).
//! Section 3 measures each primitive in isolation (no TLS).
//! Section 4 measures the full pipeline via the production TLS wrapper.
//! Section 5 measures levels L1..L5 cumulatively inside the TLS wrapper.
//! Section 6 prints a summary.

use fss::prg::{self, PrgSeed, FixedKeyPrgStream};
use std::cell::RefCell;
use std::time::Instant;

use aes::cipher::{KeyInit, BlockEncrypt};
use aes::cipher::generic_array::GenericArray;
use aes::Aes128;

use rand_core::RngCore;

const ITERATIONS: usize = 1_000_000;
const WARMUP: usize = 100_000;
const AES_KEY_SIZE: usize = 16;

thread_local!(static BENCH_PRG_STREAM: RefCell<FixedKeyPrgStream> = RefCell::new(FixedKeyPrgStream::new()));

fn main() {
    let seeds: Vec<PrgSeed> = (0..ITERATIONS).map(|_| PrgSeed::random()).collect();

    println!("CTR-mode seed expansion benchmark ({ITERATIONS} iterations, {WARMUP} warmup)\n");

    // ============================================================
    // SECTION 1 — Full expansion (no TLS, stack-local stream)
    // ============================================================
    println!("--- Section 1: Full expansion (stack-local, no TLS) ---");
    println!("{:<55} {:>12} {:>12}", "Step", "Total (ms)", "Per-call (ns)");
    println!("{}", "-".repeat(81));

    let mut stream = FixedKeyPrgStream::new();

    for s in seeds.iter().take(WARMUP) {
        // EXACT copy of expand_dir(true, true), but using `stream` instead of TLS.
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        stream.set_key(&key_short);
        let mut out = prg::PrgOutput {
            bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        stream.fill_bytes(&mut out.seeds.0.key);
        stream.fill_bytes(&mut out.seeds.1.key);
        std::hint::black_box(out);
    }
    let start = Instant::now();
    for s in seeds.iter() {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        stream.set_key(&key_short);
        let mut out = prg::PrgOutput {
            bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        stream.fill_bytes(&mut out.seeds.0.key);
        stream.fill_bytes(&mut out.seeds.1.key);
        std::hint::black_box(out);
    }
    let ns_full_notls = start.elapsed().as_nanos();
    print_row("Full expansion (stack-local)", ns_full_notls);

    // ============================================================
    // SECTION 2 — Cumulative (no TLS)
    // ============================================================
    println!("\n--- Section 2: Cumulative breakdown (stack-local, no TLS) ---");
    println!(
        "{:<55} {:>12} {:>12} {:>12}",
        "Step (cumulative)", "Total (ms)", "Per-call (ns)", "Delta (ns)"
    );
    println!("{}", "-".repeat(94));

    // L1: mask parent seed (key_short[0] &= 0xFC)
    for s in seeds.iter().take(WARMUP) {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        std::hint::black_box(key_short);
    }
    let start = Instant::now();
    for s in seeds.iter() {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        std::hint::black_box(key_short);
    }
    let ns_l1 = start.elapsed().as_nanos();
    print_row_delta("L1: mask seed", ns_l1, ns_l1 as i128);

    // L2: L1 + set_key on local stream
    for s in seeds.iter().take(WARMUP) {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        stream.set_key(&key_short);
        std::hint::black_box(&stream);
    }
    let start = Instant::now();
    for s in seeds.iter() {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        stream.set_key(&key_short);
        std::hint::black_box(&stream);
    }
    let ns_l2 = start.elapsed().as_nanos();
    print_row_delta("L2: + set_key", ns_l2, ns_l2 as i128 - ns_l1 as i128);

    // L3: L2 + PrgOutput struct init (bits + 2x PrgSeed::zero)
    for s in seeds.iter().take(WARMUP) {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        stream.set_key(&key_short);
        let out = prg::PrgOutput {
            bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        std::hint::black_box(out);
    }
    let start = Instant::now();
    for s in seeds.iter() {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        stream.set_key(&key_short);
        let out = prg::PrgOutput {
            bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        std::hint::black_box(out);
    }
    let ns_l3 = start.elapsed().as_nanos();
    print_row_delta("L3: + PrgOutput init (bits + 2x zero)", ns_l3, ns_l3 as i128 - ns_l2 as i128);

    // L4: L3 + 1st fill_bytes (left child)
    for s in seeds.iter().take(WARMUP) {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        stream.set_key(&key_short);
        let mut out = prg::PrgOutput {
            bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        stream.fill_bytes(&mut out.seeds.0.key);
        std::hint::black_box(out);
    }
    let start = Instant::now();
    for s in seeds.iter() {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        stream.set_key(&key_short);
        let mut out = prg::PrgOutput {
            bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        stream.fill_bytes(&mut out.seeds.0.key);
        std::hint::black_box(out);
    }
    let ns_l4 = start.elapsed().as_nanos();
    print_row_delta("L4: + 1st fill_bytes (left child)", ns_l4, ns_l4 as i128 - ns_l3 as i128);

    // L5: L4 + 2nd fill_bytes (right child)  [= full expansion]
    for s in seeds.iter().take(WARMUP) {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        stream.set_key(&key_short);
        let mut out = prg::PrgOutput {
            bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        stream.fill_bytes(&mut out.seeds.0.key);
        stream.fill_bytes(&mut out.seeds.1.key);
        std::hint::black_box(out);
    }
    let start = Instant::now();
    for s in seeds.iter() {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        stream.set_key(&key_short);
        let mut out = prg::PrgOutput {
            bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        stream.fill_bytes(&mut out.seeds.0.key);
        stream.fill_bytes(&mut out.seeds.1.key);
        std::hint::black_box(out);
    }
    let ns_l5 = start.elapsed().as_nanos();
    print_row_delta("L5: + 2nd fill_bytes (right child) [full]", ns_l5, ns_l5 as i128 - ns_l4 as i128);

    // ============================================================
    // SECTION 3 — Isolated primitives (no TLS)
    // ============================================================
    println!("\n--- Section 3: Isolated primitives (stack-local, no TLS) ---");
    println!("{:<55} {:>12} {:>12}", "Primitive", "Total (ms)", "Per-call (ns)");
    println!("{}", "-".repeat(81));

    // I1: mask parent seed
    for s in seeds.iter().take(WARMUP) {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        std::hint::black_box(key_short);
    }
    let start = Instant::now();
    for s in seeds.iter() {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        std::hint::black_box(key_short);
    }
    let ns_i_mask = start.elapsed().as_nanos();
    print_row("I1: mask seed", ns_i_mask);

    // I2: set_key only (pre-masked keys)
    let masked_keys: Vec<[u8; AES_KEY_SIZE]> = seeds
        .iter()
        .map(|s| {
            let mut k = s.key;
            k[0] &= 0xFC;
            k
        })
        .collect();
    for k in masked_keys.iter().take(WARMUP) {
        stream.set_key(k);
        std::hint::black_box(&stream);
    }
    let start = Instant::now();
    for k in masked_keys.iter() {
        stream.set_key(k);
        std::hint::black_box(&stream);
    }
    let ns_i_setkey = start.elapsed().as_nanos();
    print_row("I2: set_key (pre-masked)", ns_i_setkey);

    // I3: PrgOutput struct init (pre-masked keys for bit extraction)
    for k in masked_keys.iter().take(WARMUP) {
        let out = prg::PrgOutput {
            bits: ((k[0] & 0x1) == 0, (k[0] & 0x2) == 0),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        std::hint::black_box(out);
    }
    let start = Instant::now();
    for k in masked_keys.iter() {
        let out = prg::PrgOutput {
            bits: ((k[0] & 0x1) == 0, (k[0] & 0x2) == 0),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        std::hint::black_box(out);
    }
    let ns_i_struct = start.elapsed().as_nanos();
    print_row("I3: PrgOutput init", ns_i_struct);

    // I4: raw AES encrypt_block (the primitive underlying each CTR refill)
    let aes_key = GenericArray::from_slice(&[0u8; AES_KEY_SIZE]);
    let aes = Aes128::new(aes_key);
    for s in seeds.iter().take(WARMUP) {
        let mut block = GenericArray::clone_from_slice(&s.key);
        aes.encrypt_block(&mut block);
        std::hint::black_box(block);
    }
    let start = Instant::now();
    for s in seeds.iter() {
        let mut block = GenericArray::clone_from_slice(&s.key);
        aes.encrypt_block(&mut block);
        std::hint::black_box(block);
    }
    let ns_i_aes = start.elapsed().as_nanos();
    print_row("I4: AES encrypt_block (one block)", ns_i_aes);

    // I5: 16-byte XOR (inside refill(): AES(ctr) XOR ctr)
    let dummy_a: Vec<[u8; 16]> = seeds.iter().map(|s| s.key).collect();
    let dummy_b: Vec<[u8; 16]> = seeds
        .iter()
        .map(|s| {
            let mut k = s.key;
            k[0] ^= 0xFF;
            k
        })
        .collect();
    for i in 0..WARMUP {
        let mut out = [0u8; 16];
        for j in 0..16 {
            out[j] = dummy_a[i][j] ^ dummy_b[i][j];
        }
        std::hint::black_box(out);
    }
    let start = Instant::now();
    for i in 0..ITERATIONS {
        let mut out = [0u8; 16];
        for j in 0..16 {
            out[j] = dummy_a[i][j] ^ dummy_b[i][j];
        }
        std::hint::black_box(out);
    }
    let ns_i_xor = start.elapsed().as_nanos();
    print_row("I5: 16-byte XOR (MMO finalize in refill)", ns_i_xor);

    // I6: copy_from_slice (the final step of fill_bytes, buf -> dest)
    let ct_blocks: Vec<[u8; 16]> = seeds
        .iter()
        .map(|s| {
            let mut b = [0u8; 16];
            stream.set_key(&s.key);
            stream.fill_bytes(&mut b);
            b
        })
        .collect();
    for i in 0..WARMUP {
        let mut seed = PrgSeed::zero();
        seed.key.copy_from_slice(&ct_blocks[i]);
        std::hint::black_box(seed);
    }
    let start = Instant::now();
    for i in 0..ITERATIONS {
        let mut seed = PrgSeed::zero();
        seed.key.copy_from_slice(&ct_blocks[i]);
        std::hint::black_box(seed);
    }
    let ns_i_copy = start.elapsed().as_nanos();
    print_row("I6: copy_from_slice (one 16-byte block)", ns_i_copy);

    // ============================================================
    // SECTION 4 — Full expansion (with TLS, production path)
    // ============================================================
    println!("\n--- Section 4: Full expansion (TLS, production path) ---");
    println!("{:<55} {:>12} {:>12}", "Step", "Total (ms)", "Per-call (ns)");
    println!("{}", "-".repeat(81));

    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box(s.expand());
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box(s.expand());
    }
    let ns_full_tls = start.elapsed().as_nanos();
    print_row("Full expansion (TLS, s.expand())", ns_full_tls);

    // ============================================================
    // SECTION 5 — Cumulative (with TLS)
    // ============================================================
    println!("\n--- Section 5: Cumulative breakdown (TLS) ---");
    println!(
        "{:<55} {:>12} {:>12} {:>12}",
        "Step (cumulative)", "Total (ms)", "Per-call (ns)", "Delta (ns)"
    );
    println!("{}", "-".repeat(94));

    // L1 (TLS): mask only — no TLS needed (same as no-TLS L1, but reported here for completeness)
    for s in seeds.iter().take(WARMUP) {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        std::hint::black_box(key_short);
    }
    let start = Instant::now();
    for s in seeds.iter() {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        std::hint::black_box(key_short);
    }
    let ns_t_l1 = start.elapsed().as_nanos();
    print_row_delta("L1 (TLS): mask seed", ns_t_l1, ns_t_l1 as i128);

    // L2 (TLS): mask + set_key inside .with()
    for s in seeds.iter().take(WARMUP) {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut s_stream = s_in.borrow_mut();
            let mut key_short = s.key;
            key_short[0] &= 0xFC;
            s_stream.set_key(&key_short);
            std::hint::black_box(&*s_stream);
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut s_stream = s_in.borrow_mut();
            let mut key_short = s.key;
            key_short[0] &= 0xFC;
            s_stream.set_key(&key_short);
            std::hint::black_box(&*s_stream);
        });
    }
    let ns_t_l2 = start.elapsed().as_nanos();
    print_row_delta("L2 (TLS): + .with + borrow_mut + set_key", ns_t_l2, ns_t_l2 as i128 - ns_t_l1 as i128);

    // L3 (TLS): + struct init
    for s in seeds.iter().take(WARMUP) {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut s_stream = s_in.borrow_mut();
            let mut key_short = s.key;
            key_short[0] &= 0xFC;
            s_stream.set_key(&key_short);
            let out = prg::PrgOutput {
                bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
                seeds: (PrgSeed::zero(), PrgSeed::zero()),
            };
            std::hint::black_box(out);
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut s_stream = s_in.borrow_mut();
            let mut key_short = s.key;
            key_short[0] &= 0xFC;
            s_stream.set_key(&key_short);
            let out = prg::PrgOutput {
                bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
                seeds: (PrgSeed::zero(), PrgSeed::zero()),
            };
            std::hint::black_box(out);
        });
    }
    let ns_t_l3 = start.elapsed().as_nanos();
    print_row_delta("L3 (TLS): + PrgOutput init", ns_t_l3, ns_t_l3 as i128 - ns_t_l2 as i128);

    // L4 (TLS): + 1st fill_bytes
    for s in seeds.iter().take(WARMUP) {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut s_stream = s_in.borrow_mut();
            let mut key_short = s.key;
            key_short[0] &= 0xFC;
            s_stream.set_key(&key_short);
            let mut out = prg::PrgOutput {
                bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
                seeds: (PrgSeed::zero(), PrgSeed::zero()),
            };
            s_stream.fill_bytes(&mut out.seeds.0.key);
            std::hint::black_box(out);
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut s_stream = s_in.borrow_mut();
            let mut key_short = s.key;
            key_short[0] &= 0xFC;
            s_stream.set_key(&key_short);
            let mut out = prg::PrgOutput {
                bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
                seeds: (PrgSeed::zero(), PrgSeed::zero()),
            };
            s_stream.fill_bytes(&mut out.seeds.0.key);
            std::hint::black_box(out);
        });
    }
    let ns_t_l4 = start.elapsed().as_nanos();
    print_row_delta("L4 (TLS): + 1st fill_bytes", ns_t_l4, ns_t_l4 as i128 - ns_t_l3 as i128);

    // L5 (TLS): + 2nd fill_bytes  [= full TLS expand]
    for s in seeds.iter().take(WARMUP) {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut s_stream = s_in.borrow_mut();
            let mut key_short = s.key;
            key_short[0] &= 0xFC;
            s_stream.set_key(&key_short);
            let mut out = prg::PrgOutput {
                bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
                seeds: (PrgSeed::zero(), PrgSeed::zero()),
            };
            s_stream.fill_bytes(&mut out.seeds.0.key);
            s_stream.fill_bytes(&mut out.seeds.1.key);
            std::hint::black_box(out);
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut s_stream = s_in.borrow_mut();
            let mut key_short = s.key;
            key_short[0] &= 0xFC;
            s_stream.set_key(&key_short);
            let mut out = prg::PrgOutput {
                bits: ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0),
                seeds: (PrgSeed::zero(), PrgSeed::zero()),
            };
            s_stream.fill_bytes(&mut out.seeds.0.key);
            s_stream.fill_bytes(&mut out.seeds.1.key);
            std::hint::black_box(out);
        });
    }
    let ns_t_l5 = start.elapsed().as_nanos();
    print_row_delta("L5 (TLS): + 2nd fill_bytes [full]", ns_t_l5, ns_t_l5 as i128 - ns_t_l4 as i128);

    // ============================================================
    // SECTION 6 — Summary
    // ============================================================
    println!("\n--- Section 6: Summary ---");

    let full_notls_ns = ns_full_notls as f64 / ITERATIONS as f64;
    let full_tls_ns = ns_full_tls as f64 / ITERATIONS as f64;
    let cum_notls_ns = ns_l5 as f64 / ITERATIONS as f64;
    let cum_tls_ns = ns_t_l5 as f64 / ITERATIONS as f64;

    // Sum of isolated primitives: the full expansion produces 2 blocks, so we
    // include (AES + XOR + copy) twice (one per block), plus one each of the
    // non-repeated per-expansion steps (mask, set_key, struct init).
    let sum_iso_ns = (ns_i_mask
        + ns_i_setkey
        + ns_i_struct
        + 2 * (ns_i_aes + ns_i_xor + ns_i_copy)) as f64
        / ITERATIONS as f64;

    println!("Full expansion (stack-local, no TLS):   {:>7.1} ns/call", full_notls_ns);
    println!("Full expansion (TLS, production):       {:>7.1} ns/call", full_tls_ns);
    println!(
        "TLS overhead (TLS - stack):             {:>+7.1} ns/call",
        full_tls_ns - full_notls_ns
    );
    println!();
    println!("Cumulative total (Section 2, no TLS):   {:>7.1} ns/call", cum_notls_ns);
    println!("Cumulative total (Section 5, TLS):      {:>7.1} ns/call", cum_tls_ns);
    println!();
    println!("Sum of isolated parts (Section 3):      {:>7.1} ns/call", sum_iso_ns);
    println!("  = I1(mask) + I2(set_key) + I3(struct) + 2 * (I4(AES) + I5(XOR) + I6(copy))");
    println!();
    println!(
        "Dependency overhead (full - isolated):  {:>+7.1} ns/call",
        full_notls_ns - sum_iso_ns
    );
    println!("  Positive => the sequential chain (counter bookkeeping, refill control flow,");
    println!("  buffer pointer logic) costs more than the sum of the isolated primitives.");
}

fn print_row(label: &str, ns: u128) {
    println!(
        "{:<55} {:>12.3} {:>12.1}",
        label,
        ns as f64 / 1_000_000.0,
        ns as f64 / ITERATIONS as f64,
    );
}

fn print_row_delta(label: &str, ns: u128, delta: i128) {
    println!(
        "{:<55} {:>12.3} {:>12.1} {:>+12.1}",
        label,
        ns as f64 / 1_000_000.0,
        ns as f64 / ITERATIONS as f64,
        delta as f64 / ITERATIONS as f64,
    );
}
