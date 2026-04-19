## Notation
We denote the private $n$-bit string $\alpha$ and its bit decomposition as  
$\alpha_1, \ldots, \alpha_n \in \{0,1\}^n$.

## Primitives
- **PRG:** $\{0,1\}^k \rightarrow \{0,1\}^{2k+2}$ is a pseudorandom generator.  
- $H_1 : \{0,1\}^* \times \{0,1\}^k \rightarrow \{0,1\}^{2k}$ and  
- $H_2 : \{0,1\}^{2k} \rightarrow \{0,1\}^{2k}$ are random oracles.

---

# Algorithm: `Gen(1^k, 1^n, α, (β₁, β₂, …, βₙ), G)`
Generate DPF keys.

1. Sample $s_b^{(0)} \leftarrow \{0,1\}^k$ for $b \in \{0,1\}$.
2. Let $t_0^{(0)} := 0$ and $t_1^{(0)} := 1$.
3. For $i = 1$ to $n$ do
4. $s_b^L \;||\; t_b^L \;||\; s_b^R \;||\; t_b^R := \text{PRG}(s_b^{(i-1)})$ for $b \in \{0,1\}$  
   Parse the output of PRG as a sequence of $(k \;||\; 1 \;||\; k \;||\; 1)$ bits.
5. If $\alpha_i = 0$ then  
   - Diff := L  
   - Same := R
6. Else  
   - Diff := R  
   - Same := L
7. $s_{cw} := s_0^{Same} \oplus s_1^{Same}$
8. $t_{cw}^L := t_0^L \oplus t_1^L \oplus \alpha_i \oplus 1$
9. $t_{cw}^R := t_0^R \oplus t_1^R \oplus \alpha_i$
10. $\tilde{s}_b^{(i)}$ := s_b^{Diff} \oplus t_b^{(i-1)} \cdot s_{cw}$ for $b \in \{0,1\}$
11. $t_b^{(i)} := t_b^{Diff} \oplus t_b^{(i-1)} \cdot t_{cw}^{Diff}$ for $b \in \{0,1\}$
12. $s_b^{(i)} \;||\; W_b^{(i)} := \text{Convert}(\tilde{s}_b^{(i)})$ for $b \in \{0,1\}$
13. $W_{cw}^{(i)} := (-1)^{t_1^{(i)}} \cdot [\beta_i - W_0^{(i)} + W_1^{(i)}]$
14. $cw^{(i)} := s_{cw} \;||\; t_{cw}^L \;||\; t_{cw}^R \;||\; W_{cw}^{(i)}$
15. $\tilde{\pi}_b^{(i)} := H_1(\alpha_{\le i} \;||\; s_b^{(i)})$
16. $cs^{(i)} := \tilde{\pi}_0^{(i)} \oplus \tilde{\pi}_1^{(i)}$
17. $key_b := (s_b^{(0)} \;||\; cw^{(1)} \;||\; \cdots \;||\; cw^{(n)} \;||\; cs^{(1)} \;||\; \cdots \;||\; cs^{(n)})$ for $b \in \{0,1\}$
18. **Return** $key_b$ for $b \in \{0,1\}$

---

# Function: `Convert_G(s)`

1. Let $u \leftarrow |G|$.
2. If $u = 2^m$ for an integer $m$ then
3. Return the group element represented by $\text{PRG}'(s) \bmod u$,
4. where $\text{PRG}' : \{0,1\}^k \rightarrow \{0,1\}^m$.
5. Else
6. Let $n = \lceil \log_2 u \rceil + k$.
7. Return the group element represented by $\text{PRG}''(s) \bmod u$,
8. where $\text{PRG}'' : \{0,1\}^k \rightarrow \{0,1\}^n$.

# EvalNext

**EvalNext**$(b, i, st^{(i-1)}, cw^{(i)}, cs^{(i)}, x_{\le i}, \pi)$  
Evaluate $x_i$.

1. Parse $st^{(i-1)}$ as $(s^{(i-1)} \;||\; t^{(i-1)})$.

2. $s_{cw} \;||\; t_{cw}^L \;||\; t_{cw}^R \;||\; W_{cw}^{(i)} := cw^{(i)}$

3. $\tilde{s}^L \;||\; \tilde{t}^L \;||\; \tilde{s}^R \;||\; \tilde{t}^R := PRG(s^{(i-1)})$

   Parse the output of PRG as a sequence of $(k \;||\; 1 \;||\; k \;||\; 1)$ bits.

4. $\tau^{(i)} := (\tilde{s}^L \;||\; \tilde{t}^L \;||\; \tilde{s}^R \;||\; \tilde{t}^R)
\oplus \big(t^{(i-1)} \cdot [\, s_{cw} \;||\; t_{cw}^L \;||\; s_{cw} \;||\; t_{cw}^R \,]\big)$

5. $s^L \;||\; t^L \;||\; s^R \;||\; t^R := \tau^{(i)}$

6. if $x_i = 0$ then $\tilde{s}^{(i)} := s^L,\quad t^{(i)} := t^L$

7. else $\tilde{s}^{(i)} := s^R,\quad t^{(i)} := t^R$

8. $s^{(i)} \;||\; W^{(i)} := \mathrm{Convert}(\tilde{s}^{(i)})$

9. $st^{(i)} := s^{(i)} \;||\; t^{(i)}$

10. $y^{(i)} := (-1)^b \cdot [W^{(i)} + t^{(i)} \cdot W_{cw}]$

11. $\tilde{\pi}^{(i)} := H_1(x^{\le i} \;||\; s^{(i)})$

12. $\pi := \pi \oplus H_2\!\left(\pi \oplus (\tilde{\pi}^{(i)} \oplus t^{(i)} \cdot cs^{(i)})\right)$

13. **return** $(st^{(i)}, y^{(i)}, \pi)$



---

# EvalPref

**EvalPref**$(b, key, x \in \{0,1\}^n, st^{(d-1)}, d, \pi)$  
Evaluate one public bitstring $x$ on all its bits $x_i$ for $i \in [n]$.

1. Parse $key$ as  

   $$
   s^{(0)} \;||\; cw^{(1)} \;||\; \dots \;||\; cw^{(n)} \;||\; cs^{(1)} \;||\; \dots \;||\; cs^{(n)}
   $$

2. If $d \ne 1$ then parse $st^{(d-1)}$ as $(s^{(d-1)} \;||\; t^{(d-1)})$

3. Else  
   $t^{(0)} := b$,  
   $st^{(0)} := s^{(0)} \;||\; t^{(0)}$

4. For $i = d$ to $n$ do

5. $(st^{(i)}, y^{(i)}, \pi) :=
   EvalNext(b, i, st^{(i-1)}, cw^{(i)}, cs^{(i)}, x^{\le i}, \pi)$

6. **return** $(st^{(n)}, y^{(n)}, \pi)$