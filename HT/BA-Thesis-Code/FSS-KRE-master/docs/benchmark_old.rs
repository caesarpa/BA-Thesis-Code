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

    println!("Old (counter-mode) expand() breakdown benchmark ({ITERATIONS} iterations)\n");
    println!("{:<45} {:>12} {:>12}", "Step", "Total (ms)", "Per-call (ns)");
    println!("{}", "-".repeat(71));

    // --- 1. Bit masking + control bit extraction ---
    for s in seeds.iter().take(WARMUP) {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        let bits = ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0);
        std::hint::black_box((key_short, bits));
    }
    let start = Instant::now();
    for s in seeds.iter() {
        let mut key_short = s.key;
        key_short[0] &= 0xFC;
        let bits = ((key_short[0] & 0x1) == 0, (key_short[0] & 0x2) == 0);
        std::hint::black_box((key_short, bits));
    }
    let ns_mask = start.elapsed().as_nanos();
    print_row("Bit mask + control bit extract", ns_mask);

    // --- 2. TLS access + set_key only ---
    for s in seeds.iter().take(WARMUP) {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut stream = s_in.borrow_mut();
            let mut key_short = s.key;
            key_short[0] &= 0xFC;
            stream.set_key(&key_short);
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut stream = s_in.borrow_mut();
            let mut key_short = s.key;
            key_short[0] &= 0xFC;
            stream.set_key(&key_short);
        });
    }
    let ns_set_key = start.elapsed().as_nanos();
    print_row("TLS + borrow_mut + set_key", ns_set_key);

    // --- 3. TLS + set_key + 1x fill_bytes (one child) ---
    for s in seeds.iter().take(WARMUP) {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut stream = s_in.borrow_mut();
            stream.set_key(&s.key);
            let mut block = [0u8; 16];
            stream.fill_bytes(&mut block);
            std::hint::black_box(block);
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut stream = s_in.borrow_mut();
            stream.set_key(&s.key);
            let mut block = [0u8; 16];
            stream.fill_bytes(&mut block);
            std::hint::black_box(block);
        });
    }
    let ns_one_block = start.elapsed().as_nanos();
    print_row("TLS + set_key + 1x fill_bytes", ns_one_block);

    // --- 4. TLS + set_key + 2x fill_bytes (both children) ---
    for s in seeds.iter().take(WARMUP) {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut stream = s_in.borrow_mut();
            stream.set_key(&s.key);
            let mut block1 = [0u8; 16];
            let mut block2 = [0u8; 16];
            stream.fill_bytes(&mut block1);
            stream.fill_bytes(&mut block2);
            std::hint::black_box((block1, block2));
        });
    }
    let start = Instant::now();
    for s in seeds.iter() {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut stream = s_in.borrow_mut();
            stream.set_key(&s.key);
            let mut block1 = [0u8; 16];
            let mut block2 = [0u8; 16];
            stream.fill_bytes(&mut block1);
            stream.fill_bytes(&mut block2);
            std::hint::black_box((block1, block2));
        });
    }
    let ns_two_blocks = start.elapsed().as_nanos();
    print_row("TLS + set_key + 2x fill_bytes", ns_two_blocks);

    // --- 5. Raw AES encrypt_block only (no TLS, no stream) ---
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
    let ns_aes = start.elapsed().as_nanos();
    print_row("AES encrypt_block only (raw)", ns_aes);

    // --- 6. 16-byte XOR (MMO finalize, per block) ---
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

    // --- 7. PrgOutput struct construction (2x PrgSeed::zero) ---
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

    // --- 8. 2x copy_from_slice (writing both children) ---
    let blocks: Vec<([u8; 16], [u8; 16])> = seeds.iter().map(|s| {
        BENCH_PRG_STREAM.with(|s_in| {
            let mut stream = s_in.borrow_mut();
            stream.set_key(&s.key);
            let mut b1 = [0u8; 16];
            let mut b2 = [0u8; 16];
            stream.fill_bytes(&mut b1);
            stream.fill_bytes(&mut b2);
            (b1, b2)
        })
    }).collect();
    let start = Instant::now();
    for i in 0..ITERATIONS {
        let mut seed0 = PrgSeed::zero();
        let mut seed1 = PrgSeed::zero();
        seed0.key.copy_from_slice(&blocks[i].0);
        seed1.key.copy_from_slice(&blocks[i].1);
        std::hint::black_box((seed0, seed1));
    }
    let ns_copy2 = start.elapsed().as_nanos();
    print_row("2x copy_from_slice (both children)", ns_copy2);

    // --- 9. Full expand() for reference ---
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
    let sum_parts = ns_mask + (2 * ns_aes) + (2 * ns_xor16) + ns_struct + ns_copy2;
    println!("Sum of parts (mask+2*AES+2*xor+struct+2*copy):  {:.3} ms ({:.1} ns/call)",
        sum_parts as f64 / 1_000_000.0, sum_parts as f64 / ITERATIONS as f64);
    println!("Full expand() measured:                          {:.3} ms ({:.1} ns/call)",
        ns_full as f64 / 1_000_000.0, ns_full as f64 / ITERATIONS as f64);
    println!("TLS + overhead delta:                            {:.1} ns/call",
        ns_full as f64 / ITERATIONS as f64 - sum_parts as f64 / ITERATIONS as f64);
    println!();
    println!("2nd fill_bytes cost (2x - 1x blocks):            {:.1} ns/call",
        (ns_two_blocks as f64 - ns_one_block as f64) / ITERATIONS as f64);
}

fn print_row(label: &str, ns: u128) {
    println!(
        "{:<45} {:>12.3} {:>12.1}",
        label,
        ns as f64 / 1_000_000.0,
        ns as f64 / ITERATIONS as f64,
    );
}
