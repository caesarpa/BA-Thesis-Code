use fss::*;
use fss::idpf::*;
use fss::RingElm;
use crate::mpc_platform::NetInterface;

/// Whether online per-stage timing is enabled (env var `ONLINE_TIMING=1`).
/// Read once per process via `OnceLock`.
fn online_timing_enabled() -> bool {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ON.get_or_init(|| std::env::var("ONLINE_TIMING").ok().as_deref() == Some("1"))
}

/// Per-stage online timing helper. Prints elapsed since the previous
/// checkpoint and resets the clock. No-op unless `ONLINE_TIMING=1`.
pub(crate) fn online_step(label: &str, t: &mut std::time::Instant) {
    if online_timing_enabled() {
        println!("  [online]  {:<28} {:>10.3?}", label, t.elapsed());
    }
    *t = std::time::Instant::now();
}

/// Report an accumulated `Duration` (for sub-timings summed across many
/// loop iterations). No-op unless `ONLINE_TIMING=1`.
pub(crate) fn online_report(label: &str, d: std::time::Duration) {
    if online_timing_enabled() {
        println!("  [online]  {:<28} {:>10.3?}", label, d);
    }
}

pub mod bitwise_max;
pub mod bitwise_kre;
pub mod batch_max_proto;
pub mod batch_kre_proto;
pub mod max_ic_proto;

pub fn tree_eval_of_idpf(idpf: &IDPFKey<RingElm>, old_state: &EvalState, t_batch: &Vec<bool>, batch_size: usize, 
    tree_ind: usize, msk: bool, new_state: &mut Vec<EvalState>, beta: &mut Vec<RingElm>) {
    
    let mut vec_msk = vec![false; batch_size];
    vec_msk[tree_ind] = msk;
    let x_batch = bits_Xor(&vec_msk, t_batch);

    let (tmp_state, y_eval) =  idpf.eval_bit(&old_state, x_batch[tree_ind]);
    if tree_ind < (batch_size-1){
        tree_eval_of_idpf(idpf, &tmp_state, t_batch, batch_size, 
            tree_ind+1, true, new_state, beta);
        tree_eval_of_idpf(idpf, &tmp_state, t_batch, batch_size, 
            tree_ind+1, false, new_state, beta);
    }
    else{
        new_state.push(tmp_state);
        beta.push(y_eval);
    }
}

pub async fn verify_vidpf_tags(netlayer: &mut NetInterface, states: &[EvalState]) -> bool {
    let mut aggregate_tag = Tag::zero();
    for state in states {
        aggregate_tag ^= state.tag();
    }

    let local = aggregate_tag.to_bytes();
    let remote = netlayer.exchange_byte_vec(&local).await;
    let verified = local == remote;
    println!(
        "VIDPF tag verification: {}",
        if verified { "SUCCESS" } else { "FAILED" }
    );
    verified
}