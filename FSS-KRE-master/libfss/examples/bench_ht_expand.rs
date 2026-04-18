use fss::prg::{self, PrgSeed};
use std::time::Instant;

use aes::cipher::{KeyInit, BlockEncrypt};
use aes::cipher::generic_array::GenericArray;
use aes::Aes128;

const ITERATIONS: usize = 1_000_000;
const WARMUP: usize = 100_000;
const KEY_SIZE: usize = 16;

fn main() {
    let seeds: Vec<PrgSeed> = (0..ITERATIONS).map(|_| PrgSeed::random()).collect();
    let aes = Aes128::new(GenericArray::from_slice(b"HT-IDPF-HALFTREE"));

    println!("Half-Tree expand() benchmark ({ITERATIONS} iterations)\n");

    // =====================================================================
    // SECTION 1: Full expansion (copied from expand_dir, no TLS)
    // =====================================================================
    println!("=== Full expansion (exact production code, stack-local AES) ===\n");
    println!("{:<55} {:>12} {:>12}", "Step", "Total (ms)", "Per-call (ns)");
    println!("{}", "-".repeat(81));

    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box({
            let sigma = sigma(&s.key);
            let mut block = GenericArray::clone_from_slice(&sigma);
            aes.encrypt_block(&mut block);
            let mut h = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { h[i] = block[i] ^ sigma[i]; }
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            let mut right = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { right[i] = s.key[i] ^ h[i]; }
            (left, right)
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box({
            let sigma = sigma(&s.key);
            let mut block = GenericArray::clone_from_slice(&sigma);
            aes.encrypt_block(&mut block);
            let mut h = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { h[i] = block[i] ^ sigma[i]; }
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            let mut right = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { right[i] = s.key[i] ^ h[i]; }
            (left, right)
        });
    }
    let ns_full = start.elapsed().as_nanos();
    print_row("Full expand: sigma + AES + MMO + children", ns_full);

    // =====================================================================
    // SECTION 2: Cumulative measurements (each builds on the previous)
    // =====================================================================
    println!("\n=== Cumulative measurements (each step adds to previous) ===\n");
    println!("{:<55} {:>12} {:>12} {:>12}", "Step", "Total (ms)", "Per-call (ns)", "Delta (ns)");
    println!("{}", "-".repeat(93));

    // Cumulative 1: sigma only
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box({
            sigma(&s.key)
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box({
            sigma(&s.key)
        });
    }
    let ns_c1 = start.elapsed().as_nanos();
    print_row_delta("C1: sigma", ns_c1, ns_c1 as i128);

    // Cumulative 2: sigma + AES encrypt
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box({
            let sigma = sigma(&s.key);
            let mut block = GenericArray::clone_from_slice(&sigma);
            aes.encrypt_block(&mut block);
            block
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box({
            let sigma = sigma(&s.key);
            let mut block = GenericArray::clone_from_slice(&sigma);
            aes.encrypt_block(&mut block);
            block
        });
    }
    let ns_c2 = start.elapsed().as_nanos();
    print_row_delta("C2: sigma + AES", ns_c2, ns_c2 as i128 - ns_c1 as i128);

    // Cumulative 3: sigma + AES + MMO XOR (= full H_S)
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box({
            let sigma = sigma(&s.key);
            let mut block = GenericArray::clone_from_slice(&sigma);
            aes.encrypt_block(&mut block);
            let mut h = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { h[i] = block[i] ^ sigma[i]; }
            h
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box({
            let sigma = sigma(&s.key);
            let mut block = GenericArray::clone_from_slice(&sigma);
            aes.encrypt_block(&mut block);
            let mut h = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { h[i] = block[i] ^ sigma[i]; }
            h
        });
    }
    let ns_c3 = start.elapsed().as_nanos();
    print_row_delta("C3: sigma + AES + MMO XOR  (= H_S)", ns_c3, ns_c3 as i128 - ns_c2 as i128);

    // Cumulative 4: H_S + left child (copy)
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box({
            let sigma = sigma(&s.key);
            let mut block = GenericArray::clone_from_slice(&sigma);
            aes.encrypt_block(&mut block);
            let mut h = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { h[i] = block[i] ^ sigma[i]; }
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            left
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box({
            let sigma = sigma(&s.key);
            let mut block = GenericArray::clone_from_slice(&sigma);
            aes.encrypt_block(&mut block);
            let mut h = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { h[i] = block[i] ^ sigma[i]; }
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            left
        });
    }
    let ns_c4 = start.elapsed().as_nanos();
    print_row_delta("C4: H_S + left child (copy)", ns_c4, ns_c4 as i128 - ns_c3 as i128);

    // Cumulative 5: H_S + left + right child (= full expand)
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box({
            let sigma = sigma(&s.key);
            let mut block = GenericArray::clone_from_slice(&sigma);
            aes.encrypt_block(&mut block);
            let mut h = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { h[i] = block[i] ^ sigma[i]; }
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            let mut right = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { right[i] = s.key[i] ^ h[i]; }
            (left, right)
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box({
            let sigma = sigma(&s.key);
            let mut block = GenericArray::clone_from_slice(&sigma);
            aes.encrypt_block(&mut block);
            let mut h = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { h[i] = block[i] ^ sigma[i]; }
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            let mut right = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { right[i] = s.key[i] ^ h[i]; }
            (left, right)
        });
    }
    let ns_c5 = start.elapsed().as_nanos();
    print_row_delta("C5: H_S + left + right  (= full expand)", ns_c5, ns_c5 as i128 - ns_c4 as i128);

    // =====================================================================
    // SECTION 3: Isolated measurements (each piece independently)
    // =====================================================================
    println!("\n=== Isolated measurements (each piece in its own loop) ===\n");
    println!("{:<55} {:>12} {:>12}", "Step", "Total (ms)", "Per-call (ns)");
    println!("{}", "-".repeat(81));

    // Isolated: sigma only
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box(sigma(&s.key));
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box(sigma(&s.key));
    }
    let ns_i_sigma = start.elapsed().as_nanos();
    print_row("I1: sigma", ns_i_sigma);

    // Isolated: AES encrypt_block only
    let sigmas: Vec<[u8; KEY_SIZE]> = seeds.iter().map(|s| sigma(&s.key)).collect();
    for s in sigmas.iter().take(WARMUP) {
        let mut block = GenericArray::clone_from_slice(s);
        aes.encrypt_block(&mut block);
        std::hint::black_box(block);
    }
    let start = Instant::now();
    for s in sigmas.iter() {
        let mut block = GenericArray::clone_from_slice(s);
        aes.encrypt_block(&mut block);
        std::hint::black_box(block);
    }
    let ns_i_aes = start.elapsed().as_nanos();
    print_row("I2: AES encrypt_block", ns_i_aes);

    // Isolated: MMO XOR (16-byte XOR)
    let enc_blocks: Vec<[u8; KEY_SIZE]> = sigmas.iter().map(|s| {
        let mut block = GenericArray::clone_from_slice(s);
        aes.encrypt_block(&mut block);
        let mut out = [0u8; KEY_SIZE];
        out.copy_from_slice(block.as_slice());
        out
    }).collect();
    for i in 0..WARMUP {
        let mut h = [0u8; KEY_SIZE];
        for j in 0..KEY_SIZE { h[j] = enc_blocks[i][j] ^ sigmas[i][j]; }
        std::hint::black_box(h);
    }
    let start = Instant::now();
    for i in 0..ITERATIONS {
        let mut h = [0u8; KEY_SIZE];
        for j in 0..KEY_SIZE { h[j] = enc_blocks[i][j] ^ sigmas[i][j]; }
        std::hint::black_box(h);
    }
    let ns_i_mmo = start.elapsed().as_nanos();
    print_row("I3: MMO XOR (16-byte XOR)", ns_i_mmo);

    // Isolated: left child copy
    let h_vals: Vec<[u8; KEY_SIZE]> = seeds.iter().map(|s| {
        let sigma = sigma(&s.key);
        let mut block = GenericArray::clone_from_slice(&sigma);
        aes.encrypt_block(&mut block);
        let mut h = [0u8; KEY_SIZE];
        for i in 0..KEY_SIZE { h[i] = block[i] ^ sigma[i]; }
        h
    }).collect();
    for i in 0..WARMUP {
        let mut left = [0u8; KEY_SIZE];
        left.copy_from_slice(&h_vals[i]);
        std::hint::black_box(left);
    }
    let start = Instant::now();
    for i in 0..ITERATIONS {
        let mut left = [0u8; KEY_SIZE];
        left.copy_from_slice(&h_vals[i]);
        std::hint::black_box(left);
    }
    let ns_i_left = start.elapsed().as_nanos();
    print_row("I4: left child (copy_from_slice)", ns_i_left);

    // Isolated: right child XOR
    for i in 0..WARMUP {
        let mut right = [0u8; KEY_SIZE];
        for j in 0..KEY_SIZE { right[j] = seeds[i].key[j] ^ h_vals[i][j]; }
        std::hint::black_box(right);
    }
    let start = Instant::now();
    for i in 0..ITERATIONS {
        let mut right = [0u8; KEY_SIZE];
        for j in 0..KEY_SIZE { right[j] = seeds[i].key[j] ^ h_vals[i][j]; }
        std::hint::black_box(right);
    }
    let ns_i_right = start.elapsed().as_nanos();
    print_row("I5: right child (parent XOR H_S)", ns_i_right);

    // =====================================================================
    // SECTION 4: Full expansion with TLS
    // =====================================================================
    println!("\n=== Full expansion (with TLS, as used in production expand_dir) ===\n");
    println!("{:<55} {:>12} {:>12}", "Step", "Total (ms)", "Per-call (ns)");
    println!("{}", "-".repeat(81));

    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box({
            let h = prg::half_tree_hs_tls(&s.key);
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            let mut right = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { right[i] = s.key[i] ^ h[i]; }
            (left, right)
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box({
            let h = prg::half_tree_hs_tls(&s.key);
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            let mut right = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { right[i] = s.key[i] ^ h[i]; }
            (left, right)
        });
    }
    let ns_full_tls = start.elapsed().as_nanos();
    print_row("Full expand (TLS): H_S_tls + children", ns_full_tls);

    // =====================================================================
    // SECTION 5: Cumulative measurements with TLS
    // =====================================================================
    println!("\n=== Cumulative measurements with TLS ===\n");
    println!("{:<55} {:>12} {:>12} {:>12}", "Step", "Total (ms)", "Per-call (ns)", "Delta (ns)");
    println!("{}", "-".repeat(93));

    // TLS Cumulative 1: sigma only (same as without TLS — no AES involved)
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box(sigma(&s.key));
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box(sigma(&s.key));
    }
    let ns_t1 = start.elapsed().as_nanos();
    print_row_delta("T1: sigma", ns_t1, ns_t1 as i128);

    // TLS Cumulative 2: H_S via TLS (sigma + AES + MMO, all inside TLS)
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box(prg::half_tree_hs_tls(&s.key));
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box(prg::half_tree_hs_tls(&s.key));
    }
    let ns_t2 = start.elapsed().as_nanos();
    print_row_delta("T2: H_S via TLS (sigma + AES + MMO)", ns_t2, ns_t2 as i128 - ns_t1 as i128);

    // TLS Cumulative 3: H_S via TLS + left child
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box({
            let h = prg::half_tree_hs_tls(&s.key);
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            left
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box({
            let h = prg::half_tree_hs_tls(&s.key);
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            left
        });
    }
    let ns_t3 = start.elapsed().as_nanos();
    print_row_delta("T3: H_S TLS + left child (copy)", ns_t3, ns_t3 as i128 - ns_t2 as i128);

    // TLS Cumulative 4: H_S via TLS + left + right (= full expand with TLS)
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box({
            let h = prg::half_tree_hs_tls(&s.key);
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            let mut right = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { right[i] = s.key[i] ^ h[i]; }
            (left, right)
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box({
            let h = prg::half_tree_hs_tls(&s.key);
            let mut left = [0u8; KEY_SIZE];
            left.copy_from_slice(&h);
            let mut right = [0u8; KEY_SIZE];
            for i in 0..KEY_SIZE { right[i] = s.key[i] ^ h[i]; }
            (left, right)
        });
    }
    let ns_t4 = start.elapsed().as_nanos();
    print_row_delta("T4: H_S TLS + left + right (= full)", ns_t4, ns_t4 as i128 - ns_t3 as i128);

    // =====================================================================
    // SECTION 6: Summary comparison
    // =====================================================================
    let sum_isolated = ns_i_sigma + ns_i_aes + ns_i_mmo + ns_i_left + ns_i_right;
    println!("\n=== Summary ===\n");
    println!("{:<50} {:>8.1} ns/call", "Full expansion (stack-local AES):", ns_full as f64 / ITERATIONS as f64);
    println!("{:<50} {:>8.1} ns/call", "Full expansion (TLS):", ns_full_tls as f64 / ITERATIONS as f64);
    println!("{:<50} {:>+8.1} ns/call", "TLS overhead (TLS - stack):", (ns_full_tls as f64 - ns_full as f64) / ITERATIONS as f64);
    println!("{:<50} {:>8.1} ns/call", "Cumulative total without TLS (C5):", ns_c5 as f64 / ITERATIONS as f64);
    println!("{:<50} {:>8.1} ns/call", "Cumulative total with TLS (T4):", ns_t4 as f64 / ITERATIONS as f64);
    println!("{:<50} {:>8.1} ns/call", "Sum of isolated parts:", sum_isolated as f64 / ITERATIONS as f64);
    println!("{:<50} {:>8.1} ns/call", "Dependency overhead (full - isolated):", (ns_full as f64 - sum_isolated as f64) / ITERATIONS as f64);
}

/// σ(x_L ∥ x_R) = (x_L ⊕ x_R) ∥ x_L — copied from prg.rs
#[inline]
fn sigma(parent: &[u8; KEY_SIZE]) -> [u8; KEY_SIZE] {
    let mut s = [0u8; KEY_SIZE];
    for i in 0..8 {
        s[i] = parent[i] ^ parent[i + 8];
    }
    s[8..KEY_SIZE].copy_from_slice(&parent[0..8]);
    s
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
