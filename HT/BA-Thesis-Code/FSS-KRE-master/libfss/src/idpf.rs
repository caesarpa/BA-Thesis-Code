use crate::prg;
use crate::Group;
use serde::Deserialize;
use serde::Serialize;
use std::mem;
use std::ops::{BitXor, BitXorAssign};

use aes::cipher::{KeyInit, BlockEncrypt};
use aes::cipher::generic_array::GenericArray;
use aes::Aes128;

use crate::TupleExt;
use crate::TupleMapToExt;

const TAG_SIZE: usize = prg::AES_BLOCK_SIZE;
const VIDPF_AES_KEY: [u8; 16] = *b"VIDPF-AES-HASH\x00\x00";

thread_local! {
    static VIDPF_AES: Aes128 = {
        let key = GenericArray::from_slice(&VIDPF_AES_KEY);
        Aes128::new(key)
    };
}

#[derive(Clone, Copy, Debug, Default)]
pub struct KeygenTimingBreakdown {
    pub expand_children: std::time::Duration,
    pub convert_and_cw: std::time::Duration,
    pub tag_hash: std::time::Duration,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct EvalTimingBreakdown {
    pub expand_dir: std::time::Duration,
    pub convert_and_word: std::time::Duration,
    pub tag_update: std::time::Duration,
}

fn keygen_timing_enabled() -> bool {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ON.get_or_init(|| std::env::var("OFFLINE_TIMING").ok().as_deref() == Some("1"))
}

fn eval_timing_enabled() -> bool {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ON.get_or_init(|| std::env::var("ONLINE_TIMING").ok().as_deref() == Some("1"))
}

fn keygen_timing_accum() -> &'static std::sync::Mutex<KeygenTimingBreakdown> {
    static ACC: std::sync::OnceLock<std::sync::Mutex<KeygenTimingBreakdown>> =
        std::sync::OnceLock::new();
    ACC.get_or_init(|| std::sync::Mutex::new(KeygenTimingBreakdown::default()))
}

fn eval_timing_accum() -> &'static std::sync::Mutex<EvalTimingBreakdown> {
    static ACC: std::sync::OnceLock<std::sync::Mutex<EvalTimingBreakdown>> =
        std::sync::OnceLock::new();
    ACC.get_or_init(|| std::sync::Mutex::new(EvalTimingBreakdown::default()))
}

fn add_keygen_timing(local: KeygenTimingBreakdown) {
    if keygen_timing_enabled() {
        let mut acc = keygen_timing_accum().lock().unwrap();
        acc.expand_children += local.expand_children;
        acc.convert_and_cw += local.convert_and_cw;
        acc.tag_hash += local.tag_hash;
    }
}

fn add_eval_timing(local: EvalTimingBreakdown) {
    if eval_timing_enabled() {
        let mut acc = eval_timing_accum().lock().unwrap();
        acc.expand_dir += local.expand_dir;
        acc.convert_and_word += local.convert_and_word;
        acc.tag_update += local.tag_update;
    }
}

pub fn reset_keygen_timing_breakdown() {
    if keygen_timing_enabled() {
        *keygen_timing_accum().lock().unwrap() = KeygenTimingBreakdown::default();
    }
}

pub fn take_keygen_timing_breakdown() -> KeygenTimingBreakdown {
    if keygen_timing_enabled() {
        std::mem::take(&mut *keygen_timing_accum().lock().unwrap())
    } else {
        KeygenTimingBreakdown::default()
    }
}

pub fn reset_eval_timing_breakdown() {
    if eval_timing_enabled() {
        *eval_timing_accum().lock().unwrap() = EvalTimingBreakdown::default();
    }
}

pub fn take_eval_timing_breakdown() -> EvalTimingBreakdown {
    if eval_timing_enabled() {
        std::mem::take(&mut *eval_timing_accum().lock().unwrap())
    } else {
        EvalTimingBreakdown::default()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Tag {
    bytes: [u8; TAG_SIZE],
}

impl Tag {
    pub fn zero() -> Self {
        Tag {
            bytes: [0; TAG_SIZE],
        }
    }

    pub fn to_bytes(&self) -> Vec<u8> {
        self.bytes.to_vec()
    }
}

impl BitXor for Tag {
    type Output = Tag;

    fn bitxor(self, rhs: Self) -> Self::Output {
        let mut out = self;
        out ^= rhs;
        out
    }
}

impl BitXorAssign for Tag {
    fn bitxor_assign(&mut self, rhs: Self) {
        for i in 0..TAG_SIZE {
            self.bytes[i] ^= rhs.bytes[i];
        }
    }
}

/// AES-MMO (Matyas-Meyer-Oseas) one-block compression: AES_K(x) ⊕ x
#[inline(always)]
fn aes_mmo(aes: &Aes128, input: &[u8; 16]) -> [u8; 16] {
    let mut block = GenericArray::clone_from_slice(input);
    aes.encrypt_block(&mut block);
    let mut out = [0u8; 16];
    for i in 0..16 {
        out[i] = block[i] ^ input[i];
    }
    out
}

/// h1(level, path_bits, seed) — 2 AES calls via Merkle-Damgård + MMO
fn h1(level: usize, path_bits: u32, seed: &prg::PrgSeed) -> Tag {
    VIDPF_AES.with(|aes| {
        let mut block0 = [0u8; 16];
        block0[0] = 0x01;
        block0[1..5].copy_from_slice(&(level as u32).to_be_bytes());
        block0[5..9].copy_from_slice(&path_bits.to_be_bytes());

        let mid = aes_mmo(aes, &block0);

        let mut block1 = [0u8; 16];
        for i in 0..16 {
            block1[i] = mid[i] ^ seed.key[i];
        }
        Tag { bytes: aes_mmo(aes, &block1) }
    })
}

/// h2(tag) — 1 AES call via MMO with domain separation
fn h2(tag: Tag) -> Tag {
    VIDPF_AES.with(|aes| {
        let mut input = tag.bytes;
        input[0] ^= 0x02;
        Tag { bytes: aes_mmo(aes, &input) }
    })
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct CorWord<T> {
    seed: prg::PrgSeed,
    bits: (bool, bool),
    word: T,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct IDPFKey<T> {
    key_idx: bool,
    root_seed: prg::PrgSeed,
    cor_words: Vec<CorWord<T>>,
    cor_tags: Vec<Tag>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EvalState {
    level: usize,
    seed: prg::PrgSeed,
    bit: bool,
    pi: Tag,
    path_bits: u32,
}

fn gen_cor_word<W>(
    bit: bool,
    value: W,
    level: usize,
    path_bits: u32,
    bits: &mut (bool, bool),
    seeds: &mut (prg::PrgSeed, prg::PrgSeed),
) -> (CorWord<W>, Tag)
    where W: prg::FromRng + Clone + Group + std::fmt::Debug
{
    let timing_on = keygen_timing_enabled();
    let mut timing = KeygenTimingBreakdown::default();
    let data = if timing_on {
        let t0 = std::time::Instant::now();
        let data = seeds.map(|s| s.expand());
        timing.expand_children += t0.elapsed();
        data
    } else {
        seeds.map(|s| s.expand())
    };

    // If alpha[i] = 0:
    //   Keep = L,  Lose = R
    // Else
    //   Keep = R,  Lose = L
    let keep = bit;
    let lose = !keep;


    let mut cw = CorWord {
        seed: data.0.seeds.get(lose) ^ data.1.seeds.get(lose),
        bits: (
            data.0.bits.0 ^ data.1.bits.0 ^ bit ^ true,
            data.0.bits.1 ^ data.1.bits.1 ^ bit,
        ),
        word: W::zero(),
    };
    let converted = if timing_on {
        let t0 = std::time::Instant::now();
        for (b, seed) in seeds.iter_mut() {
            *seed = data.get(b).seeds.get(keep).clone();

            if *bits.get(b) {
                *seed = &*seed ^ &cw.seed;
            }

            let mut newbit = *data.get(b).bits.get(keep);
            if *bits.get(b) {
                newbit ^= cw.bits.get(keep);
            }

            *bits.get_mut(b) = newbit;
        }
        let converted = seeds.map(|s| s.convert());
        cw.word = value;

        cw.word.sub(&converted.0.word);
        cw.word.add(&converted.1.word);

        if bits.1 {
            cw.word.negate();
        }
        timing.convert_and_cw += t0.elapsed();
        converted
    } else {
        for (b, seed) in seeds.iter_mut() {
            *seed = data.get(b).seeds.get(keep).clone();

            if *bits.get(b) {
                *seed = &*seed ^ &cw.seed;
            }

            let mut newbit = *data.get(b).bits.get(keep);
            if *bits.get(b) {
                newbit ^= cw.bits.get(keep);
            }

            *bits.get_mut(b) = newbit;
        }
        let converted = seeds.map(|s| s.convert());
        cw.word = value;

        cw.word.sub(&converted.0.word);
        cw.word.add(&converted.1.word);

        if bits.1 {
            cw.word.negate();
        }
        converted
    };

    let cor_tag = if timing_on {
        let t0 = std::time::Instant::now();
        let cor_tag =
            h1(level, path_bits, &converted.0.seed) ^ h1(level, path_bits, &converted.1.seed);
        timing.tag_hash += t0.elapsed();
        cor_tag
    } else {
        h1(level, path_bits, &converted.0.seed) ^ h1(level, path_bits, &converted.1.seed)
    };

    seeds.0 = converted.0.seed;
    seeds.1 = converted.1.seed;

    if timing_on {
        add_keygen_timing(timing);
    }

    (cw, cor_tag)
}


impl<T> IDPFKey<T> where T: prg::FromRng + Clone + Group + std::fmt::Debug
{
    pub fn gen(alpha_bits: &[bool], values: &[T]) -> (IDPFKey<T>, IDPFKey<T>) {
        debug_assert!(alpha_bits.len() == values.len() );

        let root_seeds = (prg::PrgSeed::random(), prg::PrgSeed::random());
        let root_bits = (false, true);

        let mut seeds = root_seeds.clone();
        let mut bits = root_bits;
        let mut cor_words: Vec<CorWord<T>> = Vec::new();
        let mut cor_tags: Vec<Tag> = Vec::new();
        let mut path_bits: u32 = 0;

        for (i, &bit) in alpha_bits.iter().enumerate() {
            path_bits |= (bit as u32) << i;
            let (cw, cor_tag) = gen_cor_word::<T>(bit, values[i].clone(), i + 1, path_bits, &mut bits, &mut seeds);
            cor_words.push(cw);
            cor_tags.push(cor_tag);
        }

        (
            IDPFKey::<T> {
                key_idx: false,
                root_seed: root_seeds.0,
                cor_words: cor_words.clone(),
                cor_tags: cor_tags.clone(),
            },
            IDPFKey::<T> {
                key_idx: true,
                root_seed: root_seeds.1,
                cor_words,
                cor_tags,
            },
        )
    }

    pub fn eval_bit(&self, state: &EvalState, dir: bool) -> (EvalState, T) {
        let timing_on = eval_timing_enabled();
        let mut timing = EvalTimingBreakdown::default();
        let tau = if timing_on {
            let t0 = std::time::Instant::now();
            let tau = state.seed.expand_dir(!dir, dir);
            timing.expand_dir += t0.elapsed();
            tau
        } else {
            state.seed.expand_dir(!dir, dir)
        };
        let mut seed = tau.seeds.get(dir).clone();
        let mut new_bit = *tau.bits.get(dir);

        let mut word = if timing_on {
            let t0 = std::time::Instant::now();
            if state.bit {
                seed = &seed ^ &self.cor_words[state.level].seed;
                new_bit ^= self.cor_words[state.level].bits.get(dir);
            }

            let converted = seed.convert::<T>();
            seed = converted.seed;

            let mut word = converted.word;
            if new_bit {
                word.add(&self.cor_words[state.level].word);
            }

            if self.key_idx {
                word.negate()
            }
            timing.convert_and_word += t0.elapsed();
            word
        } else {
            if state.bit {
                seed = &seed ^ &self.cor_words[state.level].seed;
                new_bit ^= self.cor_words[state.level].bits.get(dir);
            }

            let converted = seed.convert::<T>();
            seed = converted.seed;

            let mut word = converted.word;
            if new_bit {
                word.add(&self.cor_words[state.level].word);
            }

            if self.key_idx {
                word.negate()
            }
            word
        };

        let next_level = state.level + 1;
        let next_path_bits = state.path_bits | ((dir as u32) << state.level);

        let pi_next = if timing_on {
            let t0 = std::time::Instant::now();
            let mut tilde_pi = h1(next_level, next_path_bits, &seed);
            if new_bit {
                tilde_pi ^= self.cor_tags[state.level];
            }
            let pi_next = state.pi ^ h2(state.pi ^ tilde_pi);
            timing.tag_update += t0.elapsed();
            pi_next
        } else {
            let mut tilde_pi = h1(next_level, next_path_bits, &seed);
            if new_bit {
                tilde_pi ^= self.cor_tags[state.level];
            }
            state.pi ^ h2(state.pi ^ tilde_pi)
        };

        if timing_on {
            add_eval_timing(timing);
        }

        (
            EvalState {
                level: next_level,
                seed,
                bit: new_bit,
                pi: pi_next,
                path_bits: next_path_bits,
            },
            word,
        )
    }

    pub fn eval_init(&self) -> EvalState {
        EvalState {
            level: 0,
            seed: self.root_seed.clone(),
            bit: self.key_idx,
            pi: Tag::zero(),
            path_bits: 0,
        }
    }

    pub fn eval(&self, idx: &Vec<bool>) -> T {
        self.eval_with_tag(idx).0
    }

    pub fn eval_with_tag(&self, idx: &[bool]) -> (T, Tag) {
        debug_assert!(idx.len() <= self.domain_size());
        debug_assert!(!idx.is_empty());
        let mut state = self.eval_init();
        let mut last_word = T::zero();

        for bit in idx {
            let (state_new, word) = self.eval_bit(&state, *bit);
            last_word = word;
            state = state_new;
        }

        (last_word, state.pi)
    }

    pub fn gen_from_str(s: &str) -> (Self, Self) {
        let bits = crate::string_to_bits(s);
        let values = vec![T::one(); bits.len()-1];

        IDPFKey::gen(&bits, &values)
    }

    pub fn domain_size(&self) -> usize {
        self.cor_words.len()
    }

    pub fn key_size(&self) -> usize {
        let mut keySize = 0usize;

        keySize += mem::size_of_val(&self.key_idx);
        // println!("key_idx is {}",mem::size_of_val(&self.key_idx));


        keySize += mem::size_of_val(&self.root_seed);
        // println!("root_seed is {}",mem::size_of_val(&self.root_seed));


        keySize += mem::size_of_val(&*self.cor_words);
        // println!("cor_words is {}",mem::size_of_val(&*self.cor_words));

        keySize += mem::size_of_val(&*self.cor_tags);
        keySize
    }

}

impl EvalState {
    pub fn tag(&self) -> Tag {
        self.pi
    }
}


#[cfg(test)]
mod tests {
    use super::*;
    use crate::ring::*;
    use crate::Group;

    #[test]
    fn evalCheck() {
        let nbits = 3usize;
        let alpha = crate::u32_to_bits(nbits, 7);

        let values = RingElm::from(1u32).to_vec(nbits);

        let (dpf_key0, dpf_key1) = IDPFKey::gen(&alpha, &values);

        let mut state0 = dpf_key0.eval_init();
        let mut state1 = dpf_key1.eval_init();

        let testNumber = crate::u32_to_bits(nbits, 7);

        //Prefix trial test
        for i in 0..nbits{
            let bit = testNumber[i];
            let (state_new0, word0) = dpf_key0.eval_bit(&state0, bit);
            state0 = state_new0;

            let (state_new1, word1) = dpf_key1.eval_bit(&state1, bit);
            state1 = state_new1;

            let mut sum = RingElm::zero();
            sum.add(&word0);
            sum.add(&word1);

            assert_eq!(sum, values[i]);
        }
    }

    #[test]
    fn eval_with_tag_matches_eval_bit_path() {
        let nbits = 4usize;
        let alpha = crate::u32_to_bits(nbits, 9);
        let values = RingElm::from(1u32).to_vec(nbits);
        let test_number = crate::u32_to_bits(nbits, 9);

        let (key0, _) = IDPFKey::gen(&alpha, &values);

        let mut state = key0.eval_init();
        let mut last = RingElm::zero();
        for bit in &test_number {
            let (new_state, word) = key0.eval_bit(&state, *bit);
            state = new_state;
            last = word;
        }

        let (eval_word, eval_tag) = key0.eval_with_tag(&test_number);
        assert_eq!(eval_word, last);
        assert_eq!(eval_tag, state.tag());
    }

    #[test]
    fn two_party_tags_match_on_alpha() {
        for &nbits in &[3usize, 5, 8, 16] {
            for val in 0..std::cmp::min(8, 1 << nbits) {
                let alpha = crate::u32_to_bits(nbits, val);
                let values = RingElm::from(1u32).to_vec(nbits);
                let (key0, key1) = IDPFKey::gen(&alpha, &values);

                let mut s0 = key0.eval_init();
                let mut s1 = key1.eval_init();
                for &bit in &alpha {
                    let (ns0, _) = key0.eval_bit(&s0, bit);
                    let (ns1, _) = key1.eval_bit(&s1, bit);
                    assert_eq!(
                        ns0.tag(), ns1.tag(),
                        "Tag mismatch at level {} for nbits={} val={}",
                        ns0.level, nbits, val
                    );
                    s0 = ns0;
                    s1 = ns1;
                }
            }
        }
    }

    #[test]
    fn two_party_tags_match_off_alpha() {
        for &nbits in &[3usize, 5, 8, 16] {
            let alpha = crate::u32_to_bits(nbits, 5);
            let wrong = crate::u32_to_bits(nbits, 2);
            let values = RingElm::from(1u32).to_vec(nbits);
            let (key0, key1) = IDPFKey::gen(&alpha, &values);

            let mut s0 = key0.eval_init();
            let mut s1 = key1.eval_init();
            for &bit in &wrong {
                let (ns0, _) = key0.eval_bit(&s0, bit);
                let (ns1, _) = key1.eval_bit(&s1, bit);
                assert_eq!(
                    ns0.tag(), ns1.tag(),
                    "Off-alpha tag mismatch at level {} for nbits={}",
                    ns0.level, nbits
                );
                s0 = ns0;
                s1 = ns1;
            }
        }
    }

    #[test]
    fn aggregate_tags_match_multiple_keys() {
        for &nbits in &[8usize, 16, 31] {
            let m = 50usize;
            let prefix = crate::u32_to_bits(nbits, 42);

            let mut agg0 = Tag::zero();
            let mut agg1 = Tag::zero();

            for j in 0..m {
                let alpha = crate::u32_to_bits(nbits, (j * 7 + 3) as u32);
                let values = RingElm::from(1u32).to_vec(nbits);
                let (key0, key1) = IDPFKey::gen(&alpha, &values);

                let mut s0 = key0.eval_init();
                let mut s1 = key1.eval_init();
                for &bit in &prefix {
                    let (ns0, _) = key0.eval_bit(&s0, bit);
                    let (ns1, _) = key1.eval_bit(&s1, bit);
                    s0 = ns0;
                    s1 = ns1;
                }
                agg0 ^= s0.tag();
                agg1 ^= s1.tag();
            }

            assert_eq!(agg0, agg1, "Aggregate tags should match for nbits={}", nbits);
        }
    }

    #[test]
    fn two_party_tags_mixed_directions_31bits() {
        let nbits = 31usize;
        let m = 10;

        for j in 0..m {
            let alpha = crate::u32_to_bits(nbits, (j * 1337 + 17) as u32);
            let values = RingElm::from(1u32).to_vec(nbits);
            let (key0, key1) = IDPFKey::gen(&alpha, &values);

            let mut s0 = key0.eval_init();
            let mut s1 = key1.eval_init();

            for level in 0..nbits {
                let dir = (level % 3 != 0) ^ (j % 2 == 0);
                let (ns0, _) = key0.eval_bit(&s0, dir);
                let (ns1, _) = key1.eval_bit(&s1, dir);
                assert_eq!(
                    ns0.tag(), ns1.tag(),
                    "Tag mismatch at level {} for key {}", ns0.level, j
                );
                s0 = ns0;
                s1 = ns1;
            }
        }
    }
}
