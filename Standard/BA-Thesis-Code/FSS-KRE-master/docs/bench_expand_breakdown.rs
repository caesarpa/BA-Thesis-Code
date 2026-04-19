use fss::prg::{self, PrgSeed, AES_BLOCK_SIZE};
use std::time::Instant;

use aes::cipher::{KeyInit, BlockEncrypt};
use aes::cipher::generic_array::GenericArray;
use aes::Aes128;

const ITERATIONS: usize = 1_000_000;
const WARMUP: usize = 100_000;
const AES_KEY_SIZE: usize = 16;

fn main() {
    let seeds: Vec<PrgSeed> = (0..ITERATIONS).map(|_| PrgSeed::random()).collect();

    println!("HT expand() breakdown benchmark ({ITERATIONS} iterations)\n");
    println!("{:<40} {:>12} {:>12}", "Step", "Total (ms)", "Per-call (ns)");
    println!("{}", "-".repeat(66));

    // --- 1. Thread-local access only (measure TLS overhead) ---
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box(prg::half_tree_hs_tls(&s.key));
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box(prg::half_tree_hs_tls(&s.key));
    }
    let ns_hs_tls = start.elapsed().as_nanos();
    print_row("H_S via TLS (sigma + AES + MMO)", ns_hs_tls);

    // --- 2. sigma only ---
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box(prg::half_tree_sigma(&s.key));
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box(prg::half_tree_sigma(&s.key));
    }
    let ns_sigma = start.elapsed().as_nanos();
    print_row("sigma only", ns_sigma);

    // --- 3. AES encrypt_block only (no sigma, no MMO xor) ---
    let aes_key = GenericArray::from_slice(b"HT-IDPF-HALFTREE");
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
    let ns_aes = start.elapsed().as_nanos();
    print_row("AES encrypt_block only", ns_aes);

    // --- 4. MMO XOR only (16-byte XOR) ---
    let dummy_a: Vec<[u8; 16]> = seeds.iter().map(|s| s.key).collect();
    let dummy_b: Vec<[u8; 16]> = seeds.iter().map(|s| {
        let mut k = s.key;
        k[0] ^= 0xFF;
        k
    }).collect();
    for i in 0..WARMUP {
        let mut out = [0u8; 16];
        for j in 0..16 { out[j] = dummy_a[i][j] ^ dummy_b[i][j]; }
        std::hint::black_box(out);
    }
    let start = Instant::now();
    for i in 0..ITERATIONS {
        let mut out = [0u8; 16];
        for j in 0..16 { out[j] = dummy_a[i][j] ^ dummy_b[i][j]; }
        std::hint::black_box(out);
    }
    let ns_xor16 = start.elapsed().as_nanos();
    print_row("16-byte XOR (MMO finalize)", ns_xor16);

    // --- 5. PrgOutput struct construction (2x PrgSeed::zero) ---
    for _ in 0..WARMUP {
        let out = prg::PrgOutput {
            bits: (true, true),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        std::hint::black_box(out);
    }
    let start = Instant::now();
    for _ in 0..ITERATIONS {
        let out = prg::PrgOutput {
            bits: (true, true),
            seeds: (PrgSeed::zero(), PrgSeed::zero()),
        };
        std::hint::black_box(out);
    }
    let ns_struct = start.elapsed().as_nanos();
    print_row("PrgOutput struct init (2x zero)", ns_struct);

    // --- 6. copy_from_slice (left child assignment) ---
    let h_vals: Vec<[u8; 16]> = seeds.iter().map(|s| prg::half_tree_hs_tls(&s.key)).collect();
    let start = Instant::now();
    for i in 0..ITERATIONS {
        let mut seed = PrgSeed::zero();
        seed.key.copy_from_slice(&h_vals[i]);
        std::hint::black_box(seed);
    }
    let ns_copy = start.elapsed().as_nanos();
    print_row("copy_from_slice (left child)", ns_copy);

    // --- 7. XOR loop (right child derivation) ---
    let start = Instant::now();
    for i in 0..ITERATIONS {
        let mut key = [0u8; AES_KEY_SIZE];
        for j in 0..AES_KEY_SIZE {
            key[j] = seeds[i].key[j] ^ h_vals[i][j];
        }
        std::hint::black_box(key);
    }
    let ns_right = start.elapsed().as_nanos();
    print_row("XOR loop (right child)", ns_right);

    // --- 8. Full expand() for reference ---
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box(s.expand());
    }
    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box(s.expand());
    }
    let ns_full = start.elapsed().as_nanos();
    print_row("Full expand() [reference]", ns_full);

    // --- Summary ---
    println!("\n--- Estimated breakdown ---");
    let sum_parts = ns_sigma + ns_aes + ns_xor16 + ns_struct + ns_copy + ns_right;
    println!("Sum of parts (sigma+AES+xor+struct+copy+right): {:.3} ms ({:.1} ns/call)",
        sum_parts as f64 / 1_000_000.0, sum_parts as f64 / ITERATIONS as f64);
    println!("Full expand() measured:                         {:.3} ms ({:.1} ns/call)",
        ns_full as f64 / 1_000_000.0, ns_full as f64 / ITERATIONS as f64);
    println!("TLS + overhead delta:                           {:.1} ns/call",
        ns_full as f64 / ITERATIONS as f64 - sum_parts as f64 / ITERATIONS as f64);
}

fn print_row(label: &str, ns: u128) {
    println!(
        "{:<40} {:>12.3} {:>12.1}",
        label,
        ns as f64 / 1_000_000.0,
        ns as f64 / ITERATIONS as f64,
    );
}
