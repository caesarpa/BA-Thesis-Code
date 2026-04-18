use fss::prg::PrgSeed;
use std::time::Instant;

const ITERATIONS: usize = 1_000_000;
const WARMUP: usize = 100_000;

fn bench_expand(seeds: &[PrgSeed]) -> u128 {
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box(s.expand());
    }

    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box(s.expand());
    }
    start.elapsed().as_nanos()
}

fn bench_expand_dir(seeds: &[PrgSeed]) -> u128 {
    for s in seeds.iter().take(WARMUP) {
        std::hint::black_box(s.expand_dir(false, true));
    }

    let start = Instant::now();
    for s in seeds.iter() {
        std::hint::black_box(s.expand_dir(false, true));
    }
    start.elapsed().as_nanos()
}

fn main() {
    let seeds: Vec<PrgSeed> = (0..ITERATIONS).map(|_| PrgSeed::random()).collect();

    println!("HT seed expansion micro-benchmark ({ITERATIONS} iterations)\n");
    println!("{:<30} {:>12} {:>12}", "Method", "Total (ms)", "Per-call (ns)");
    println!("{}", "-".repeat(56));

    let ns_both = bench_expand(&seeds);
    println!(
        "{:<30} {:>12.3} {:>12.1}",
        "expand (both children)",
        ns_both as f64 / 1_000_000.0,
        ns_both as f64 / ITERATIONS as f64,
    );

    let ns_one = bench_expand_dir(&seeds);
    println!(
        "{:<30} {:>12.3} {:>12.1}",
        "expand_dir (one child)",
        ns_one as f64 / 1_000_000.0,
        ns_one as f64 / ITERATIONS as f64,
    );
}
