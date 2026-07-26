# Gun Scout Ballistics Engine — Engineering Specification

**Status:** Draft v1.0 (2026-07-22)
**Scope:** Replaces the current `static/ballistics.html` single-file calculator with a
measured-anchored, physics-forward, uncertainty-quantified ballistics simulation system.
**Audience:** This document is the build reference. Every module, equation, data schema,
and UI surface below is normative unless marked *(optional)* or *(phase N)*.

---

## 1. Purpose, design principles, and non-goals

### 1.1 Purpose

Simulate the full behavior of a specific **loadout** — a particular gun (barrel length,
twist rate, sights, weight) firing a particular ammunition load (bullet, muzzle velocity,
drag profile) under particular conditions (atmosphere, wind, latitude) — and present
drop, drift, velocity, energy, stability, and **hit probability** as functions of range,
with honest uncertainty bounds.

### 1.2 Design principles

1. **Measured-anchored.** Muzzle velocity is fundamentally a measured quantity. The
   engine consumes MV from a hierarchy of sources (chronograph > barrel-curve-scaled
   factory spec > factory nominal), each carrying an explicit uncertainty. The engine
   never pretends to compute MV from powder physics for factory ammunition, because the
   required inputs (powder identity, charge weight) are proprietary and unpublished.
2. **Physics-forward.** Everything downstream of the muzzle is simulated from physics:
   a modified point-mass flight model with standard drag functions (G1/G7) or custom
   Cd(M) curves, spin/stability physics from the Miller rule and Litz empirical
   corrections, full atmosphere, 3-D wind, and Coriolis.
3. **Uncertainty-quantified.** Every input has a distribution, not a value. A Monte
   Carlo layer propagates MV spread, BC tolerance, wind gusting, and rifle precision
   into percentile bands and hit-probability curves. Determinism is presented as the
   median of an honest distribution.
4. **Provenance everywhere.** Every number in the data libraries carries a `source`
   field. The UI can always answer "where did this number come from?"
5. **No dependencies.** Vanilla JS, canvas rendering, static JSON data files, Web
   Worker for compute. Consistent with the rest of Gun Scout.

### 1.3 Non-goals (explicit, with rationale)

- **No interior-ballistics simulation of factory ammunition.** Factory powder blends
  are non-canister, unpublished, and changed without notice; simulating them would be
  false precision. (A GRT-class interior module for *handloaders* — who know their
  powder and charge — is sketched as Phase 4, off by default.)
- **No 6-DOF rigid-body solver.** 6-DOF requires per-bullet aerodynamic coefficient
  sets (pitch damping, Magnus, overturning moment vs Mach) measured in spark ranges;
  these are not publicly available. Modified point-mass + Litz empirical spin
  corrections is the same architecture class as Hornady 4DOF and is the accuracy
  ceiling for public data.
- **No load-recipe publication.** The app never displays powder charge recommendations.
  This is a trajectory tool, not reloading guidance.
- **No slide/action cycling model.** Slide and gun mass have negligible effect on MV
  (the bullet exits before recoil displacement is meaningful). Gun mass is used for the
  recoil estimate only (§8).

---

## 2. System architecture

```
                ┌────────────────────────────────────────────────────────┐
                │                      DATA LIBRARIES                    │
                │  loads.json  cartridges.json  dragmodels.json guns.json│
                └──────────────┬─────────────────────────────────────────┘
                               │
   USER INPUTS                 ▼
   gun, load, conditions ──► [A] MUZZLE-STATE ESTIMATOR
                               │  MV μ±σ, powder-temp adj, cylinder-gap adj
                               ▼
                             [B] SPIN & STABILITY
                               │  spin rate, Sg, BC_eff, spin-drift & aero-jump params
                               ▼
                             [C] FLIGHT SOLVER  (RK4 modified point-mass)
                               │  drop/drift/vel/energy/Mach vs range
                               ▼
                             [D] MONTE CARLO ENGINE  (Web Worker)
                               │  percentile bands, hit probability vs range
                               ▼
                             OUTPUTS: charts, tables, stat cards, DOPE export
                               ▲
                             [E] TRUING LOOP
                                observed impacts → posterior MV/BC → tightened σ
```

Modules are pure functions over plain data: `estimateMuzzleState(gun, load, cond) →
muzzle`, `stability(gun, load, cond) → spin`, `solve(muzzle, spin, load, cond, opts) →
trajectory`, `monteCarlo(inputs, N) → bands`. All compute runs in a Web Worker;
the UI thread only renders.

**Units policy.** Internal computation is imperial (ft, ft/s, lb, grain, inch, °F,
inHg) because every published data source (BCs in lb/in², velocities in fps, SAAMI
specs) is imperial; conversions happen at the UI boundary only. Displayed units are
user-selectable (§12.8).

Constants (single `constants.js`):

| Symbol | Value | Meaning |
|---|---|---|
| `g` | 32.174 ft/s² | standard gravity |
| `RHO0` | 0.0764742 lb/ft³ | ICAO sea-level air density (59 °F, 29.92 inHg, dry) |
| `A0` | 1116.45 ft/s | speed of sound at 59 °F |
| `K0` | 2.0855×10⁻⁴ | drag constant (§6.2 derivation) |
| `OMEGA` | 7.292115×10⁻⁵ rad/s | Earth rotation rate |
| `GRAIN` | 1/7000 lb | grain→pound |
| `MPH_TO_FPS` | 1.46667 | mph→ft/s (5280/3600) |

---

## 3. Module A — Muzzle-State Estimator

Produces `{ mv: μ fps, sd: σ_shot fps, sigma_sys: σ_systematic fps, quality: tier }`.

Two distinct uncertainties are tracked and never conflated:

- `sd` — **shot-to-shot spread** of the load itself (physical, irreducible).
- `sigma_sys` — **systematic uncertainty of the estimate of μ** (reducible by
  chronograph or truing).

### 3.1 MV source hierarchy

| Tier | Source | μ | σ_sys | Quality label |
|---|---|---|---|---|
| 1 | User chronograph (n shots) | measured mean | SD/√n (min 5 fps) | "Measured" |
| 2 | Truing solution (§9) | back-solved | solver residual, floor 15 fps | "Trued" |
| 3 | Factory MV × barrel-length curve | scaled (§3.2) | √(30² + rms²) fps (quadrature, §3.2) | "Estimated" |
| 4 | Factory MV, barrel length unknown/no curve | nominal | 75 fps | "Nominal" |

`sd` comes from the load record (`sd_fps`) or class defaults: match 12 fps,
defensive/hunting 20 fps, bulk/FMJ 30 fps, rimfire 25 fps.

### 3.2 Barrel-length velocity scaling

Factory MV is published for a **test barrel** (`mv.test_barrel_in`, defaulting to the
cartridge's SAAMI test length, e.g. 4″ for 9mm Luger, 24″ for most rifle cartridges).
Per-cartridge scaling curves are fit to published chop-test data (BBTI for handgun
cartridges; Rifleshooter.com / AccurateShooter cut-down series for rifle cartridges)
using the **Le Duc form**, which matches interior-ballistics velocity-vs-travel curves
well with two parameters:

```
v(L) = a · L / (L + b)          L = barrel length, inches
```

`a` (asymptotic velocity) and `b` (half-velocity length) are fit per cartridge by
least squares over the chop-test points, stored in `cartridges.json` with the fit RMS
and source. Adjusted MV for the user's barrel:

```
MV_adj = MV_factory · v(L_user) / v(L_test)
```

The *ratio* form transfers the curve **shape** across loads of the same cartridge even
though absolute velocities differ, which is the empirically supported claim (relative
per-inch change is far more load-independent than absolute change). σ_sys tier 3 adds
the fit RMS in quadrature: `σ = √(30² + rms²)`.

Extrapolation guard: if `L_user` is outside the fitted data range by more than 2″, add
20 fps to σ_sys and set a UI warning flag `extrapolated: true`.

### 3.3 Powder temperature sensitivity

```
MV_T = MV_adj + s_T · (T_powder − T_ref)
```

`T_ref` depends on the MV tier: **tier 1** uses the chronograph session temperature
(`chrono.t_f`, §10.5 — a chronographed mean already embeds that day's temperature);
**tier 2** uses the truing session's snapshot temperature (§9); **tiers 3–4** use
the 59 °F factory/SAAMI baseline. `s_T` (fps/°F) by powder class stored per
cartridge (a class prior — factory powder is unknown): modern temp-stable rifle
≈ 0.3, legacy double-base ≈ 1.0, handgun ≈ 0.2, rimfire ≈ 0.4. A per-loadout
`temp_sens_override` (§10.5) replaces the class prior when the user has measured
data. Applying the class-prior adjustment adds 5 fps to σ_sys per 20 °F of |ΔT|
(prior uncertainty); with a user-measured override this inflation is halved.
`T_powder` defaults to ambient temperature.

### 3.4 Revolver cylinder gap

If `gun.type == "revolver"`: `MV_gap = MV_T · (1 − f_gap)` with `f_gap` default 0.03
(≈3%, BBTI "real guns vs test barrel" data; range 1–6% depending on gap size), and
σ_sys += 15 fps. User-overridable (`gun.gap_loss_pct`). Tier-1 (chronographed) MV
skips this correction — the chronograph already saw the real gun.

### 3.5 What is deliberately absent

Powder identity/charge (unknowable for factory ammo), slide mass (no MV effect),
bore friction/wear (folded into truing), suppressor back-pressure (typically +10–30 fps;
Phase 3 optional flat offset input).

---

## 4. Module B — Spin & Stability

Consumes `gun.twist_in` (inches/turn, + `twist_dir` R/L), bullet geometry, muzzle
state, atmosphere. Produces `{ spin_rps, Sg, bc_penalty, spindrift_params,
aerojump_params, warnings[] }`.

### 4.1 Spin rate

```
p = 12 · MV / twist_in        [rev/s]     ω = 2π·p [rad/s]
```

Shown as RPM (`p·60`) in the Sg badge popover (§12.4). Spin decays slower than
forward velocity in flight; for the empirical corrections below no spin-decay model
is needed (they are parameterized on launch Sg and time of flight).

**Smoothbore** (`twist_in: null` — non-rifled shotgun barrels, §10.3): all of
Module B is skipped — no spin rate, Sg, BC penalty, spin drift, or aerodynamic jump
(Foster slugs are drag-stabilized). The UI shows a neutral "smoothbore" chip
instead of an Sg badge and never suppresses the trajectory (§12.10's UNSTABLE
state does not apply).

### 4.2 Bullet length

Litz/Miller need bullet length. Library field `bullet.length_in` when known (measured
values exist for all match bullets). When absent, estimate:

```
L_est = w_gr / (1470 · d²)     [inches]   (empirical jacketed-bullet fit)
```

(Checks: 175 gr .308 → 1.25″ vs actual 1.24″; 124 gr 9mm → 0.67″ vs actual ~0.60″ —
pistol bullets run stubbier; the estimate is flagged `length_estimated: true` and Sg
gets ±0.15 uncertainty, surfaced as a hedged badge, never a hard warning.)

### 4.3 Miller stability factor

With `m` = weight (grains), `d` = diameter (in), `l = L/d` (calibers), `t = twist_in/d`
(calibers/turn):

```
Sg_std = 30·m / (t² · d³ · l · (1 + l²))
```

Corrections to actual conditions (Miller 2005):

```
f_v = (MV / 2800)^(1/3)                      velocity correction
f_ρ = (T_R / 518.67) · (29.92 / P_inHg)      air density correction (T_R = °F+459.67)
Sg  = Sg_std · f_v · f_ρ
```

Interpretation thresholds:

| Sg | State | UI badge |
|---|---|---|
| ≥ 1.5 | Fully stable | green "Sg 1.8" |
| 1.0–1.5 | Marginal — BC degraded | amber "Sg 1.3 · BC −6%" |
| < 1.0 | Unstable — will tumble | red "UNSTABLE" + trajectory suppressed |

Applicability note (shown in methodology): Miller's rule assumes flat-base/boat-tail
lead-core bullets; it reads conservative (low) for plastic-tipped and long copper
monolithic bullets. Phase 3 may add the Courtney plastic-tip correction; until then
tipped bullets get a footnote, not a formula change.

### 4.4 Marginal-stability BC penalty (Litz)

For 1.0 ≤ Sg < 1.5, effective BC is degraded (Litz measured relationship, as
implemented in the Berger stability calculator):

```
BC_eff = BC · (1 − 0.03 · (1.5 − Sg)/0.1 )     i.e. ≈3% BC loss per 0.1 Sg below 1.5
```

Clamped at 25% total loss. Applied inside Module C wherever BC is used. For custom
Cd(M) curves the same multiplier is applied to the curve's effective inverse-drag
scale.

### 4.5 Spin drift (gyroscopic drift) — Litz empirical

Horizontal drift in the direction of twist (right for RH twist):

```
drift_spin(t) = 1.25 · (Sg + 1.2) · t^1.83     [inches], t = time of flight (s)
```

Added to the wind-drift lateral component in Module C output, signed by `twist_dir`.

**Validity domain (applies to §4.5 and §4.6):** the Litz fits are empirical
regressions over spin-stabilized rifle bullets, Sg ≈ 1.0–2.5. The engine clamps
`Sg_fit = min(Sg, 2.5)` before evaluating either formula, and for cartridge `class`
handgun, rimfire, or shotgun (§10.1) both channels are **suppressed entirely** —
handgun twists yield Sg ≈ 10–20+, where the fits extrapolate to nonsense
(multi-inch "spin drift" at 100 yd), while real spin drift inside handgun ranges is
sub-half-inch. The methodology page (§13) documents the domain limit.

### 4.6 Aerodynamic jump (crosswind-induced vertical deflection) — Litz empirical

A crosswind at the muzzle pitches the bullet and produces a *vertical* POI shift that
is constant in angle (MOA), not growing with range:

```
AJ = (0.01·Sg − 0.0024·l + 0.032)   [MOA per mph of crosswind]
```

Sign convention: RH twist, wind left→right ⇒ POI down; wind right→left ⇒ POI up
(reversed for LH twist). Applied as a constant angular offset to the drop channel.
Uses the crosswind component at the muzzle.

### 4.7 Transonic stability flag

Module C records where Mach crosses 1.2 and 1.0. If the trajectory's requested max
range extends past M=1.2, the UI marks the transonic band and the methodology note
explains the model's fidelity degrades there (point-mass drag tables remain defined
through subsonic — the flag is about *dispersion*, not solver failure).

---

## 5. Atmosphere model

Inputs: temperature `T` (°F), station pressure `P` (inHg) **or** altitude + altimeter
setting, relative humidity `RH` (%), powder temperature (defaults to `T`).

- If altitude `h` (ft) + altimeter `P_alt` given, station pressure:
  `P = P_alt · (1 − 6.8756×10⁻⁶·h)^5.2559`
- Vapor pressure (Tetens, over water), `T_C = (T−32)/1.8`:
  `e = RH/100 · 0.18036 · exp(17.27·T_C/(T_C+237.3))` [inHg]
  (0.18036 inHg = 6.1078 hPa, the Tetens saturation pressure at 0 °C; sanity
  anchors: e_s(59 °F) ≈ 0.50 inHg, e_s(212 °F) ≈ 29.9 inHg)
- Air density (imperial form of CIPM-lite):
  `ρ = 0.0764742 · (P − 0.3783·e)/29.92 · 518.67/T_R` [lb/ft³], `T_R = T + 459.67`
- Speed of sound (dry-air approximation, humidity effect <0.3% ignored):
  `a = 49.0223 · √T_R` [ft/s]
- **Density altitude** (displayed, not used internally):
  `DA = 145442 · (1 − (ρ/0.0764742)^0.234957)` [ft]

Defaults: 59 °F, 29.92 inHg, 0% RH, sea level (ICAO standard — matches the current
page's hard-coded assumption, now merely the default).

---

## 6. Module C — Flight Solver (modified point-mass)

### 6.1 State, frame, and integration

Right-handed ground frame: **x** downrange (along the horizontal projection of the
bore-to-target line), **y** up, **z** right. State `s = [x,y,z,vx,vy,vz]`, plus time.

Integrator: **RK4**, fixed `dt = 0.5 ms` (rifle trajectories ≈ 2–4 k steps; a full
solve is < 2 ms in JS — measured on the current Euler code, RK4 ≈ 4× cost, still
trivial). Termination: `x ≥ maxRange`, or `v < 200 fps`, or `t > 10 s`, or `y < −2000
ft` (safety). Sampling: linear interpolation onto user step grid, as current code.

### 6.2 Forces

**Drag** (dominant). Air-relative velocity `v_air = v − w` (w = wind vector, §6.3);
`M = |v_air| / a_local`.

```
a_drag = −K0 · (ρ/RHO0) · Cd_ref(M) · |v_air| · v_air / BC_eff
```

with `K0 = 2.0855×10⁻⁴` — derivation: drag force
`F = ½·ρ_m·|v_air|²·(i·Cd_ref)·(πd²/4)`, `a = F/(m/g)`, and imperial
`BC = m_lb/(i·d_in²)` gives `a = (π·g·ρ_m,0/1152)·(ρ/ρ0)·Cd_ref(M)·v²/BC` with
`ρ_m,0 = 0.0023769 slug/ft³` ⇒ constant 2.0855×10⁻⁴ (v in ft/s, BC in lb/in²,
a in ft/s²). This constant was already validated in the v1 implementation (matches
factory trajectory tables within ~1%); §11 item 1 formalizes the gate.

`Cd_ref(M)`: binary-search + linear interpolation over the selected drag table —
`G1`, `G7`, or a per-bullet custom `CDM` (§10.4). For custom CDMs the "BC" is the
sectional density `m/(7000·d²)` with form factor 1 (the curve *is* the bullet).

**Gravity**: `a_g = (0, −g, 0)`.

**Coriolis** *(toggleable, on when latitude provided)*:
`a_cor = −2·Ω × v` with Ω expressed in the local frame from latitude `φ` and firing
azimuth `Az` (degrees from true north):

```
Ω_local = OMEGA · ( cosφ·cosAz,  sinφ,  −cosφ·sinAz )      (x,y,z components)
a_cor   = −2 · Ω_local × v
```

This captures both horizontal deflection (2Ω·sinφ·v, rightward in the northern
hemisphere) and the Eötvös vertical effect for east/west fire. Defaults: φ=45°,
Az=0, feature off until the user opens the Advanced panel.

**Spin corrections** are *not* integrated forces (that would require Magnus/pitch
coefficients we don't have). Per Litz's method they are post-hoc channel offsets:
spin drift (§4.5) added to `z`-deflection as a function of sampled `t`; aerodynamic
jump (§4.6) added to the drop channel as a constant angle.

### 6.3 Wind

Wind is a horizontal vector: speed (mph) + direction (clock position relative to
shooter-target line, or degrees; 12 o'clock = headwind). Internally
`w = MPH_TO_FPS · (−w_head_mph, 0, w_cross_mph)` ft/s (same constant converts the
§7.1 sampled wind draws). Both components enter the drag term through
`v_air` — a headwind steepens drop by raising air-relative speed; a crosswind
produces drift through the lateral drag component (the classic "lag rule" emerges
naturally from the point-mass equations rather than being imposed). *(Phase 3)*:
multiple wind zones (per-range-band vectors).

### 6.4 Sight line and zeroing

Muzzle at `y = −sight_height_in/12`; optional `zero_offset` (POA-POI at a second
distance) *(phase 3)*. Launch angle solved so the trajectory crosses `y=0` at
`zero_range`, by Newton iteration on the angle (2–3 passes; residual < 0.01″), using
the **nominal** MV, the *current* atmosphere (§5), zero wind, and Coriolis off
(zeroing is a calm-day act; separate zero-day atmosphere inputs are phase 3).
Cant *(phase 3)*.

### 6.5 Output channels (per sampled range)

`x_yd, drop_in, drop_moa, drop_mil, wind_in (wind lag + spin drift), wind_moa,
wind_mil, v_fps, mach, energy_ftlb (= w_gr·v²/450435), tof_s, spin_component_in,
jump_component_in`. The drop channels **include** the aerodynamic-jump offset of
§4.6 (a constant angle, signed per §4.6); `jump_component_in` is its breakout for
decomposition displays and is **not** part of `wind_in` — aero jump is vertical. Angular conversions: `MOA = in/(1.04720·yd/100)`,
`mil = in/(3.6·yd/100)`.

### 6.6 Derived scalar metrics (per loadout)

- **Supersonic range**: interpolated range where M=1.2 ("transonic onset") and M=1.0.
- **MPBR** (maximum point-blank range) for vital-zone diameter `D` (default 6″):
  find zero range such that max ordinate = D/2, then MPBR = far range where path =
  −D/2. Solved by bisection on zero range (≤ 20 solver calls, cached).
- **Energy thresholds**: ranges where energy falls below configurable marks
  (defaults 1000 ft·lb and 1500 ft·lb) and where velocity falls below the bullet's
  `min_expansion_fps` (library field, default absent → threshold not drawn).
- **Danger space**: with the hold placed so POI = POA at the loadout's pinned range
  (§12.4), the span of ranges over which the path stays within ±D/2 of the line of
  sight (D = the §12.3 vital-zone Ø). Surfaced on the stat card (§12.4) and as the
  trajectory-window shading (§12.5).
- **Sectional density**: `SD = w_gr/7000/d²` (display stat).
- **Recoil estimate** (§8 — gun-level, not trajectory).

### 6.7 Vacuum/degenerate behavior

`BC → ∞` (or drag disabled in dev/test mode) must reproduce the analytic parabola
within 0.1% (validation gate §11). Subsonic loads (M<1 at muzzle, e.g. .300 BLK 220gr)
run through the same tables — G1/G7 are defined to M=0.

---

## 7. Module D — Monte Carlo engine

### 7.1 Sampled variables (per shot draw)

| Variable | Distribution | Source of σ |
|---|---|---|
| MV | Normal(μ_MV, √(sd² + σ_sys²)) | Module A |
| BC (or CDM scale) | Normal(BC, 2% match / 3.5% non-match) | bullet class |
| Wind speed | Normal(w, gust_sd) — gust_sd default 0.35·w, floor 1 mph | user-adjustable |
| Wind direction | Normal(dir, 10°) | fixed prior |
| Temperature | Normal(T, 3 °F) | fixed prior |
| Range (only when the §12.3 "range is estimated" toggle is on) | Normal(R, 3% of R) | rangefinder class |

Rifle/shooter precision enters *after* simulation as an angular quadrature term
(§7.3), not as a sampled trajectory input.

### 7.2 Execution

Default **N = 400** draws **per loadout**, run in the Web Worker and streamed per
loadout. MC draws integrate RK4 at `dt = 2 ms` — 4× coarser than the deterministic
center-line solve; §11 item 6 gates the resulting band error — giving ≈2 ms per
draw, ≈1 s per loadout. Deterministic seeded PRNG (mulberry32) so results are
reproducible per input hash; seed shown in methodology popover. Per sampled range
the worker records P10/P50/P90 for drop and windage **and the sample standard
deviations of the vertical and horizontal impact coordinates across draws**
(inches at target, about the sample mean) — these are the σ_v,MC(R) and σ_h,MC(R)
consumed by §7.3, sample SDs of the draws, not values derived from the percentile
band. Bands render on charts, `±` columns in the table. The deterministic solve
(§6) is drawn as the center line; P50 should straddle it (sanity check, §11).

### 7.3 Hit probability vs range

At each range R, per loadout: impact dispersion is approximated bivariate normal with

```
σ_v² = σ_v,MC²(R) + (precision_moa/3 · 1.047 · R/100)²        [inches²]
σ_h² = σ_h,MC²(R) + (precision_moa/3 · 1.047 · R/100)²
```

`precision_moa` is a **per-loadout** property (§10.5) — the rifle+shooter group
size ("my rifle shoots X-MOA groups"; defaults by gun type: rifle 1.5,
pistol/revolver 4.0; editable on the stat card, §12.4). **Normative convention:**
a quoted X-MOA group diameter is treated as ~3σ of the radial dispersion, so
per-axis σ_angular = X/3 MOA — hence the /3 in the formulas; the convention is
stated on the methodology page. Hit probability on a circular vital zone of
diameter D, aim-centered, via the exact 1-D reduction of the disk integral:

```
P_hit(R) = ∫₋ᵣʳ (1/σ_h)·φ(x/σ_h) · [Φ(√(r²−x²)/σ_v) − Φ(−√(r²−x²)/σ_v)] dx,    r = D/2
```

(φ/Φ = standard normal pdf/cdf), evaluated with fixed 32-point Gauss–Legendre on
[−r, r] — accurate to <10⁻⁵. (Tensor Gauss–Hermite must **not** be used: the disk
indicator is discontinuous, so GH converges badly and produces a jumpy P_hit
curve.) When σ_v ≈ σ_h within 10%, use the Rayleigh closed form
`P = 1 − exp(−r²/(2σ²))` with `σ² = (σ_v²+σ_h²)/2`. Output: P_hit vs range curve
per loadout + "90% effective range" scalar (largest R with P_hit ≥ 0.90).

Truing state (§9) reduces σ_sys, visibly tightening bands — the UI's reward for
calibration.

---

## 8. Recoil estimate (gun-level output)

Free-recoil energy via SAAMI method; powder charge `w_p` is *estimated* from a
per-cartridge class table (`cartridges.json.typ_charge_gr`, e.g. 9mm ≈ 6, .308 ≈ 44,
12ga ≈ 30) since factory charges are unpublished — displayed as "≈" with ±20%:

```
V_gun = (w_b·MV + w_p·v_gas) / (7000 · W_gun)      v_gas = 4700 fps (rifle), 4000 (shotgun), 1.5·MV clamped ≤4000 (handgun)
E_recoil = W_gun · V_gun² / (2·g)                   [ft·lb]
```

Displayed per loadout stat card; comparison is its whole purpose (absolute felt
recoil also depends on stock fit, muzzle devices, action type — footnoted).

---

## 9. Module E — Truing loop

**Goal:** reconcile prediction with observed reality, the industry-standard
calibration (AB/4DOF both ship it).

Input: one or more observations `{range_yd, observed_drop_in (POI vs POA),
group_moa (optional), conditions snapshot}` — record shape defined in §10.5.
Procedure (two-stage, standard practice):

1. **MV truing** — 1-D solve for MV minimizing Σ(predicted−observed)² (Brent's
   method over MV ± 150 fps). **Eligibility is the sensitivity guard, not a fixed
   range or Mach cut**: an observation qualifies iff ∂drop/∂MV ≥ 0.1 in per 10 fps
   at its range (typically satisfied from ~300 yd for supersonic rifle loads and
   from ~100 yd for handgun loads, whose curved trajectories pass easily). If no
   observation qualifies, the wizard refuses with an explanation.
2. **BC truing** — after MV truing. For loads with a finite supersonic range:
   requires ≥1 observation at ≥ 0.8·supersonic-range; 1-D solve on a BC scale
   factor (±20% clamp). For always-subsonic loads (e.g. .300 BLK 220 gr) that gate
   is degenerate — instead run a joint MV+BC solve when ≥2 observations exist at
   ranges separated by ≥2× the nearer range; otherwise MV-only.

When `group_moa` is supplied it weights the observation in the least-squares
(σ_obs = group_moa/3 in MOA, converted to inches at that range — the §7.3
convention) and sets a data-driven floor on the posterior σ_sys; the wizard also
offers to update the loadout's `precision_moa` from it.

Posterior: trued values become the tier-2 MV source; `σ_sys ← max(15,
rms_residual)`. Truing sessions persist per loadout (schema §10.5) with date + a
snapshot of the Conditions rail at save time; a stale-truing warning appears if
current conditions differ materially (|ΔDA| > 2000 ft or |ΔT| > 25 °F). The same
staleness rule applies to tier-1 chronograph data via its stored session
temperature (`chrono.t_f`, §10.5).

---

## 10. Data architecture

All static JSON under `static/data/`, loaded once, cached. Every record carries
`src` (short citation) and optional `url`. Schema version key `"v"` at file root;
loaders reject unknown major versions.

### 10.1 `cartridges.json`

```jsonc
{ "v": 1, "cartridges": [ {
  "id": "9mm-luger",
  "name": "9mm Luger",                 // must equal clients/calibers.py canonical name
  "class": "handgun",                  // handgun | rifle | rimfire | shotgun
  "bullet_diameter_in": 0.355,         // groove/bullet Ø; §4.2/§4.3/§6.2/§6.6 fall
                                       // back to this when a load carries no diameter
                                       // (custom loads); load-level bullet.diameter_in
                                       // wins when both exist
  "saami_test_barrel_in": 4.0,
  "barrel_curve": {                    // §3.2; null when no chop data exists
    "model": "leduc", "a": 1580, "b": 1.9,
    "fit_range_in": [2, 18], "rms_fps": 11,
    "src": "BBTI 9mm chop test (2 loads avg)", "url": "..." },
  "temp_sens_fps_per_f": 0.2,          // §3.3 class prior
  "typ_charge_gr": 6,                  // §8 recoil estimate only
  "typ_twists": ["1:10", "1:16"]       // UI suggestions
} ] }
```

### 10.2 `loads.json`

```jsonc
{ "v": 1, "loads": [ {
  "id": "fgmm-308-175smk",
  "cartridge": "308-win",
  "brand": "Federal", "line": "Gold Medal Match", "name": "175gr Sierra MatchKing",
  "bullet": {
    "model": "Sierra MatchKing", "type": "BTHP-match",
    "weight_gr": 175, "diameter_in": 0.308,
    "length_in": 1.240,               // optional; null → §4.2 estimate
    "min_expansion_fps": null },      // hunting bullets: expansion floor
  "bc": [ { "model": "G7", "value": 0.243, "src": "Litz measured (AB 3rd ed)" },
          { "model": "G1", "value": 0.505, "src": "Sierra published" } ],
  "cdm": null,                        // optional custom drag model id (§10.4)
  "mv": { "fps": 2600, "test_barrel_in": 24, "src": "Federal spec sheet" },
  "sd_fps": 12,                       // null → class default (§3.1)
  "class": "match"                    // match | defensive | hunting | bulk | rimfire
} ] }
```

Preference order when both BCs exist: **G7 for `class: match`/boat-tail bullets, G1
otherwise**, overridable per loadout (`bc_model`, §10.5). Library target: ~120
loads at launch (every v1 preset migrated + filled), every row sourced.

### 10.3 `guns.json` (preset guns; users can also define custom)

```jsonc
{ "v": 1, "guns": [ {
  "id": "glock-34-gen5", "name": "Glock 34 (Gen5)", "type": "pistol",
  "cartridge": "9mm-luger", "barrel_in": 5.31, "twist_in": 9.84, "twist_dir": "R",
  "weight_lb": 1.63, "sight_height_in": 0.55,
  "gap_loss_pct": null,                // revolvers only; null → §3.4 default 3%
  "src": "Glock spec" } ] }
```

`twist_in` is **nullable** — `null` marks a smoothbore barrel and routes Module B
to its §4.1 smoothbore branch. ~30 launch presets (common Glocks/S&W/SIG, AR-15 profiles 10.5/14.5/16/20″ with
1:7/1:8/1:9, common bolt guns 16–26″, lever guns, a revolver or two). Custom gun
form mirrors the schema; stored in localStorage.

### 10.4 `dragmodels.json`

`{ "v":1, "models": { "G1": [[M,Cd],...], "G7": [[M,Cd],...] }, "cdm": { "<id>":
{ "src": "...", "points": [[M,Cd],...] } } }` — G1 table is the one already in
production; G7 from the public-domain McCoy/BRL tabulation (JBM-hosted). Custom CDMs
(phase 3) hold per-bullet Doppler-derived curves where published.

### 10.5 localStorage (versioned wrapper `{v:2, ...}`)

```jsonc
gs_ballistics_v2 = {
  loadouts: [ {
    gun_id | custom_gun, load_id | custom_load, color,
    bc_model: "G1"|"G7"|null,      // §10.2 preference; null → class default; a load's
                                   // cdm still takes precedence (§6.2)
    precision_moa: 1.5,            // §7.3; defaults by gun type: rifle 1.5, pistol/revolver 4.0
    pinned_range_yd: null,         // §12.4 stat-card pin; null → 300 yd rifle / 25 yd handgun
    temp_sens_override: null,      // §3.3; null → cartridge class prior
    chrono: { mv, sd, n, t_f } | null,   // t_f = chrono-session temp, the §3.3 tier-1 baseline
    truing: [ { date,
      observations: [ { range_yd, observed_drop_in, group_moa|null } ],
      conditions: { temp_f, da_ft, pressure_inhg, wind_mph },   // §9 staleness check
      solved: { mv_fps, bc_scale|null, rms_fps } } ]            // tier-2 μ and σ_sys source
  } ],
  conditions: { /* atmosphere + wind + latitude rail values (§12.3) */ },
  target: { zero_yd, vital_d_in, max_yd, step_yd, range_estimated: false },  // §12.3
  ui: { units, tab, metric, bands_on }
}
```

**v1 migration** (one-time, from key `gs_ballistics`; v1 records are `{name,
weight, vel, bc}`): each becomes a **custom load** — `name → name`,
`weight → bullet.weight_gr`, `bc → bc: [{model: "G1", value, src: "v1 user
entry"}]` (v1 was G1-only), `mv: {fps: vel, test_barrel_in: null, src: "v1 user
entry"}`, `sd_fps: null`, `class: "bulk"`, `cartridge: null`. No `quality` field
is ever stored — quality is derived by §3.1 (these rows land at tier 4,
"Nominal"). Migrated loads are **library entries, not loadouts**: they appear in
the §12.2 ammo picker under a "Migrated (v1)" group visible for any gun, with an
inline prompt to assign a canonical cartridge (which unlocks barrel scaling, the
temp-sens class, and bullet diameter). Until a cartridge is assigned: Module B is
skipped entirely (neutral badge "spin effects off — cartridge unknown"), barrel
scaling and temp sensitivity are skipped (tier-4 MV), and recoil uses the bullet
term only. The §12.10 no-loadout empty state covers the interim.

### 10.6 Provenance rules

No numeric field without `src`. Chop-test-derived curves cite the specific test.
UI: every stat card and library row exposes source on hover/tap (§12.7). The
methodology page lists all sources (§13).

---

## 11. Validation plan (build gate for each phase)

1. **Oracle tests vs JBM** (the public reference solver), standard atmosphere,
   drop within **0.1 mil** and drift within **0.15 mil**, over per-load spans:
   rifle loads (.308 175 G7, 6.5CM 140 G7, 5.56 55 G1, .338 300 G7) 100–1000 yd
   evaluated out to each load's M=1.2 range; 9mm 124 G1 over 25–300 yd;
   .300 BLK 220 subsonic G1 over 100–500 yd (fully subsonic — validates the M<1
   region of the drag tables, §6.7).
2. **Vacuum test**: drag off → parabola analytic match < 0.1%.
3. **Sg tests**: reproduce Berger stability calculator outputs ±0.05 for 5 bullets.
4. **Spin drift**: Litz published example (.308 175 @ 1000 yd ≈ 7–9″) within 15%.
5. **Barrel curves**: refit residual vs source chop data RMS < 15 fps (handgun),
   < 20 fps (rifle); Glock 34/19/43 sanity triple within published real-gun deltas.
6. **Monte Carlo**: P50 vs deterministic solve < 0.05 mil through the supersonic
   range; N=400 band stability (re-seed variance of P90 drop < 5% of band width);
   coarse-dt draws (§7.2, dt = 2 ms) vs full-dt draws: band-edge shift < 10% of
   band width.
7. **Atmosphere**: ρ within 0.5% of CIPM reference at BOTH a Denver standard day
   (dry) AND 95 °F / 90% RH / 29.92 inHg (humid case anchors the vapor term:
   e_s(95 °F) ≈ 1.66 inHg); DA round-trip within 200 ft.
8. **Cross-check page**: hidden `?selftest=1` mode runs the suite in-browser and
   renders a pass/fail table (no test framework dependency).

---

## 12. UI specification

The page keeps Gun Scout's dark theme, tokens, and no-dependency stance. Layout is a
**left rail + main stage**, like Search — familiarity over novelty. Everything below
the rail recomputes reactively (debounced 150 ms; Monte Carlo re-runs are cancellable
in the worker).

### 12.1 Information architecture

```
/ballistics
├── Left rail (320px)
│   ├── LOADOUTS (list of gun+ammo pairings, the core object)
│   │     [+ Add loadout] → picker flow (§12.2)
│   ├── CONDITIONS (atmosphere, wind, latitude — shared by all loadouts)
│   └── TARGET (zero, vital zone size, max range, step, precision MOA)
└── Main stage
    ├── Stat cards row (per loadout, §12.4)
    ├── Tabs: Trajectory · Windage · Velocity/Energy · Hit % · Table · Truing
    └── Methodology footer link (§13)
```

**Loadout** replaces the v1 "load" as the atomic compared object: *a gun firing an
ammo*. Same ammo in two guns = two loadouts = two curves. Up to 6, colored from the
existing palette.

### 12.2 Add-loadout flow (two-step picker, one panel)

1. **Gun**: searchable combo (reuse Search-page combo component) over `guns.json`
   grouped by type, + "Custom gun…" expander: type, cartridge (canonical combo),
   barrel length, twist (combo pre-filled from `typ_twists`; blank = smoothbore for
   shotguns), sight height, weight, twist direction (R default), and — revolvers
   only — gap loss % (pre-filled 3%, §3.4). Cartridge choice filters step 2.
2. **Ammo**: searchable combo over `loads.json` filtered to the gun's cartridge,
   grouped by class (Match/Defensive/Hunting/Bulk), each row showing
   `brand line · gr · fps · BC`, + "Custom load…" expander (the v1 form + new
   optional fields: bullet length, SD, test barrel, G7/G1 value+model; bullet
   diameter is inherited from the gun's cartridge record (§10.1) unless entered).
   - **Chronograph override** inline: "I chronographed this: [μ fps] [SD]
     [n shots] [session temp °F]" (temp pre-filled from the current Conditions
     rail) → tier-1 quality immediately; the session temp becomes the §3.3 tier-1
     baseline (`chrono.t_f`).
   - The panel also sets the loadout's `precision_moa` (pre-filled by gun type,
     editable — §7.3).

The panel footer previews the computed muzzle state before adding:
`MV est. 1,152 fps ±34 · Sg 1.42 (marginal) · quality: Estimated` — the estimator
running live *is* the education.

### 12.3 Conditions & target rail groups

- **Conditions**: temperature, altitude *or* station pressure (toggle), humidity,
  a read-only **density altitude** line (§5), wind speed + direction **dial**
  (12-o'clock clock widget, drag or type), powder temp (defaults=ambient,
  expander), Advanced accordion: latitude, azimuth, Coriolis toggle, gust σ.
- **Target**: zero range, vital zone Ø (default 6″), max range, table step, and a
  "range is estimated, not lased" toggle (enables §7.1 range-error sampling).
  Precision is per-loadout, not here (§12.2 / §12.4).
- Every group header has a reset-to-default icon. All values persist (§10.5
  `conditions` + `target`).

### 12.4 Stat cards (the comparison verdict, one card per loadout)

Card contents (top→bottom): color swatch + loadout name; quality chip
(Measured/Trued/Estimated/Nominal with tooltip explaining tier); **MV ± σ**;
Sg badge (§4.3 colors; its popover lists spin RPM (§4.1), sectional density
(§6.6), and bullet length — hedged when estimated per §4.2); supersonic range;
MPBR (for current vital zone); danger space @ the pinned range (§6.6); energy @
threshold ("1,000 ft·lb to 410 yd"); drop & drift @ the pinned range
(user-pinnable, persisted per loadout via `pinned_range_yd`, default 300 yd
rifle / 25 yd handgun); editable **precision MOA** (helper: "typical: factory
rifle 1.5, match 0.75, pistol 4" — feeds §7.3); recoil ≈ ft·lb; 90%-hit range.
Cards scroll horizontally ≥4 loadouts. Clicking any stat highlights that
metric's tab/curve.

### 12.5 Charts (shared component, all tabs)

Canvas, existing axis/grid style, plus:

- **Hover crosshair**: vertical rule + tooltip listing every loadout's value at the
  hovered range (the single biggest v1 readability gap). Touch: tap-hold.
- **Uncertainty bands**: P10–P90 shaded fill per loadout (~18% opacity of line
  color), toggle in chart corner ("Show spread"). Off = deterministic line only.
- **Markers**: transonic band (hatched region M1.2→M1.0 per loadout, subtle),
  energy-threshold crossings (tick + label on the energy chart), zero range (dashed
  vertical). When a vital zone is set, the trajectory chart shades the ±D/2 window
  about the sight line and brackets each curve's in-window span — this is the §6.6
  danger space evaluated under the **user's** zero, a true property of the plotted
  curve. The optimal-zero MPBR number lives on the stat card only (it assumes a
  re-solved zero different from the plotted one).
- **Windage tab** shows total drift with stacked decomposition on hover (wind lag /
  spin drift). Aerodynamic jump is vertical (§4.6) — it appears as a labeled
  component in the **trajectory tab's** hover tooltip instead, from
  `jump_component_in`.
- **Hit % tab**: P_hit vs range curves + horizontal 90% guide; the "90% effective
  range" of each loadout labeled on-curve.

### 12.6 Table tab & DOPE export

Column set = v1 metrics + `±` sub-columns (from bands, shown when bands on) +
Mach + P_hit. Metric-select dropdown retained for compactness; "All columns"
expander for the full matrix. **Export**: CSV (all channels) and **DOPE card** —
print-stylesheet view: loadout header (gun, ammo, MV source+value, zero, conditions,
date) + drop/drift in the user's angular unit at their step, sized for a stock-side
card. `window.print()` — no dependencies.

### 12.7 Provenance & uncertainty surfaces

- Every number that came from a library shows a faint `ⓘ` on hover → source popover
  (`src`, url, retrieved date).
- Quality chips (§12.4) are the MV-tier surface; clicking opens a "how to improve
  this estimate" popover → points to chronograph entry or Truing tab.
- Estimated bullet length, extrapolated barrel curve, revolver gap correction each
  add one amber line to the loadout card's tooltip — never modal nags.

### 12.8 Units & accessibility

Global toggles in the page header: **yd/m**, **in/cm**, **MOA/mil**, **°F/°C**.
Angular unit applies to charts, table, DOPE. All interactive elements keyboard-
reachable; canvas charts get an offscreen data table (`aria-label` summary + the
Table tab is the accessible equivalent); color pairs pass WCAG AA on the dark bg;
loadout colors distinguishable for deuteranopia (current palette verified: blue/
green/amber/red/purple/teal — swap red→pink if flagged in review).

### 12.9 Truing tab (wizard, §9)

Three steps with plain-language framing: **1** "Shoot a group at a known distance,
calm conditions" (inputs: range, POI vs POA inches up/down, group size — group
size weights the solve and can update the loadout's `precision_moa`, §9); the
current Conditions rail is snapshotted into the session and shown read-only for
confirmation → **2** engine checks the §9 sensitivity guard, solves, shows
before/after (`MV 2,600 → 2,571 · bands tighten 38%`) → **3** save truing session
to loadout (schema §10.5). List of past sessions with conditions + stale-warning
badges. Copy explicitly says what truing can and cannot
fix (it calibrates *this* loadout in *these* conditions).

### 12.10 Empty/error/edge states

- No loadouts: explainer + 2 one-click demo loadouts ("Glock 19 · Federal 124gr",
  "16″ AR · M855") so the first render is never blank.
- Sg < 1.0: card shows red UNSTABLE, curves for that loadout suppressed with inline
  explanation ("predicted to tumble — trajectory not meaningful").
- Worker busy: thin progress bar under the tabs; stale charts dimmed 40% until
  fresh.
- Missing barrel curve for a cartridge: quality drops to Nominal with tooltip
  ("no chop-test data for this cartridge — using factory number as-is").
- localStorage full/corrupt: fall back to session memory + toast.
- Data-library states: skeleton treatment for the rail and stat-card row until the
  §10 JSON fetch resolves; on fetch failure or major-version rejection, an inline
  rail error panel names the affected file(s) with a retry action. Degraded modes:
  custom-gun/custom-load forms and saved custom loadouts still work without
  guns/loads/cartridges.json; `dragmodels.json` is required for any solve — its
  failure blocks solving and the panel says so explicitly.

### 12.11 Performance budget

First render < 150 ms after data fetch (~80 KB gzipped JSON). Deterministic solve
(RK4, dt = 0.5 ms) < 10 ms per loadout. Monte Carlo (N = 400 per loadout at the
§7.2 coarse dt): < 1.2 s per loadout, < 6 s total worker time for 6 loadouts, UI
never blocked; band results stream per loadout as they finish (first loadout's
bands < 1.5 s).

---

## 13. Methodology page (`/ballistics#methodology`)

A rendered section (same file, anchor-linked): model class statement (modified
point-mass + Litz empirical spin corrections — same class as commercial solvers),
every equation from §3–§9 in readable form (including recoil and the truing
procedure), the MV-tier table, all data sources with
links (BBTI, Rifleshooter/AccurateShooter chop tests, SAAMI, JBM/Litz BCs,
manufacturer spec sheets, Lucky Gunner/Buffalo Bore cross-checks), known limits
(transonic fidelity, Miller applicability, factory-powder unknowability — stated
plainly), and the validation results table (§11) rendered from the self-test.
This page is the "research-based" claim, in public.

---

## 14. Implementation phases

| Phase | Scope | Exit gate |
|---|---|---|
| **1. Core engine + data** | JSON libraries (§10) + migrator; RK4 solver; G7; atmosphere; wind vector; zeroing; derived metrics (§6.6); loadout picker; stat cards; hover crosshair; table; units | Validation §11 items 1,2,7; all v1 features preserved |
| **2. Gun physics + uncertainty** | Barrel curves; temp sensitivity; cylinder gap; Miller Sg + BC penalty; spin drift; aero jump; Coriolis; Monte Carlo + bands + Hit %; precision input; recoil card | §11 items 3,4,5,6 |
| **3. Calibration + polish** | Truing wizard; DOPE/CSV export; demo loadouts; methodology page; custom CDM support; suppressor offset; multi-zone wind; second-distance zero offset; zero-day atmosphere; cant | Self-test page green; doc complete |
| **4. (Optional) Handloader interior module** | GRT-class lumped-parameter burn model, community powder DB, pressure display with SAAMI MAP guardrails; **never** ships load recipes | Separate spec required |

Phase 1 replaces `ballistics.html` wholesale; the URL and nav don't change.

---

## 15. Source register

| Domain | Source |
|---|---|
| Handgun barrel curves | BallisticsByTheInch chop tests (archived 2020) |
| Rifle barrel curves | Rifleshooter.com / AccurateShooter cut-down series (.223, .308, 6.5CM, 7RM, .300WM, .224 Valk) |
| Factory test barrels | ANSI/SAAMI standards |
| BCs (match) | Litz measured G7 set (Applied Ballistics, via JBM bullet library) |
| BCs (general) + MVs | Manufacturer spec sheets (per-row citation) |
| Drag tables | G1/G7 standard projectile tabulations (McCoy/BRL, JBM-hosted) |
| Stability | Miller, *A New Rule for Estimating Rifling Twist* (2005); Courtney (tipped correction, phase 3) |
| Spin drift / aero jump / BC-vs-Sg | Litz, *Applied Ballistics for Long Range Shooting* / *Modern Advancements* |
| Real-gun cross-validation | Lucky Gunner Labs chrono data; Buffalo Bore real-firearm velocities; Ammoland G43/19/34 test |
| Recoil method | SAAMI free-recoil computation |
| Solver-class reference | Hornady 4DOF technical paper (modified point-mass); JBM solver as validation oracle |
```
