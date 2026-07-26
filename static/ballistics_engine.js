/* Gun Scout ballistics engine — pure compute, no DOM.
   Implements ballistics_spec.md §3–§9: muzzle-state estimator, Miller stability +
   Litz empirical spin corrections, RK4 modified point-mass solver, derived metrics,
   Monte Carlo + hit probability, truing solves, recoil.
   Loaded by ballistics_worker.js (importScripts) and by ?selftest=1 on the page.
   Units: imperial internally (ft, ft/s, lb, grain, inch, °F, inHg). */
'use strict';

var BX = (function () {
  // ---- constants (spec §2) ----
  var G = 32.174, RHO0 = 0.0764742, K0 = 2.0855e-4,
      OMEGA = 7.292115e-5, MPH_TO_FPS = 1.46667;

  var DRAG = null; // set via init: {models: {G1:[[M,Cd]..], G7:...}, cdm:{}}

  function init(dragmodels) { DRAG = dragmodels; }

  function cdLookup(table, mach) {
    if (mach <= table[0][0]) return table[0][1];
    var n = table.length;
    if (mach >= table[n - 1][0]) return table[n - 1][1];
    var lo = 0, hi = n - 1;
    while (hi - lo > 1) {
      var mid = (lo + hi) >> 1;
      if (table[mid][0] <= mach) lo = mid; else hi = mid;
    }
    var a = table[lo], b = table[hi];
    return a[1] + (b[1] - a[1]) * (mach - a[0]) / (b[0] - a[0]);
  }

  // ---- atmosphere (spec §5) ----
  function atmosphere(c) {
    var tF = c.temp_f != null ? c.temp_f : 59;
    var tR = tF + 459.67;
    var P;
    if (c.alt_mode === 'pressure' && c.station_inhg) P = c.station_inhg;
    else P = (c.altimeter_inhg || 29.92) * Math.pow(1 - 6.8756e-6 * (c.altitude_ft || 0), 5.2559);
    var tC = (tF - 32) / 1.8;
    var e = ((c.humidity_pct || 0) / 100) * 0.18036 * Math.exp(17.27 * tC / (tC + 237.3));
    var rho = RHO0 * ((P - 0.3783 * e) / 29.92) * (518.67 / tR);
    var mach1 = 49.0223 * Math.sqrt(tR);
    var da = 145442 * (1 - Math.pow(rho / RHO0, 0.234957));
    return { rho: rho, mach1: mach1, da: da, pressure: P, vapor: e };
  }

  function leduc(a, b, L) { return a * L / (L + b); }

  // ---- bullet geometry (spec §4.2) ----
  function bulletGeom(load, cart) {
    var d = load.bullet.diameter_in != null ? load.bullet.diameter_in
          : (cart ? cart.bullet_diameter_in : null);
    var len = load.bullet.length_in, est = false;
    if (len == null && d) { len = load.bullet.weight_gr / (1470 * d * d); est = true; }
    return { d: d, len: len, est: est };
  }

  function sdClassDefault(cls) {
    return { match: 12, defensive: 20, hunting: 20, bulk: 30, rimfire: 25 }[cls] || 30;
  }

  // ---- Module A: muzzle state (spec §3) ----
  // loadout: {chrono:{mv,sd,n,t_f}|null, truing:[...], temp_sens_override}
  function muzzleState(gun, load, cart, cond, loadout) {
    var flags = [], mv, sigmaSys, quality;
    var sdShot = load.sd_fps != null ? load.sd_fps : sdClassDefault(load['class']);
    var sT = (loadout && loadout.temp_sens_override != null) ? loadout.temp_sens_override
           : (cart ? cart.temp_sens_fps_per_f : 0.3);
    var measuredSens = loadout && loadout.temp_sens_override != null;
    var tPow = cond.powder_temp_f != null ? cond.powder_temp_f : (cond.temp_f != null ? cond.temp_f : 59);
    var truing = null;
    if (loadout && loadout.truing && loadout.truing.length)
      truing = loadout.truing[loadout.truing.length - 1];

    if (loadout && loadout.chrono && loadout.chrono.mv) {          // tier 1
      mv = loadout.chrono.mv;
      sigmaSys = Math.max(5, (loadout.chrono.sd || sdShot) / Math.sqrt(loadout.chrono.n || 1));
      quality = 'Measured';
      var tRef1 = loadout.chrono.t_f != null ? loadout.chrono.t_f : 59;
      mv += sT * (tPow - tRef1);
      if (Math.abs(tPow - tRef1) > 25) flags.push('conditions differ from chrono session (ΔT ' + Math.round(tPow - tRef1) + '°F) — re-chrono advised');
    } else if (truing && truing.solved && truing.solved.mv_fps) {  // tier 2
      mv = truing.solved.mv_fps;
      sigmaSys = Math.max(15, truing.solved.rms_fps || 15);
      quality = 'Trued';
      var tRef2 = (truing.conditions && truing.conditions.temp_f != null) ? truing.conditions.temp_f : 59;
      mv += sT * (tPow - tRef2);
      if (Math.abs(tPow - tRef2) > 25) flags.push('conditions differ from truing session — stale truing');
    } else {                                                        // tiers 3/4
      mv = load.mv.fps;
      var curve = cart && cart.barrel_curve;
      var testB = load.mv.test_barrel_in != null ? load.mv.test_barrel_in
                : (cart ? cart.saami_test_barrel_in : null);
      if (curve && testB && gun.barrel_in) {
        mv = load.mv.fps * leduc(curve.a, curve.b, gun.barrel_in) / leduc(curve.a, curve.b, testB);
        sigmaSys = Math.sqrt(30 * 30 + (curve.rms_fps || 15) * (curve.rms_fps || 15));
        quality = 'Estimated';
        var fr = curve.fit_range_in || [0, 99];
        if (gun.barrel_in < fr[0] - 2 || gun.barrel_in > fr[1] + 2) {
          sigmaSys += 20;
          flags.push('barrel outside the fitted chop-test range — extrapolated');
        }
      } else {
        sigmaSys = 75; quality = 'Nominal';
        if (!curve && testB && gun.barrel_in && Math.abs(gun.barrel_in - testB) > 1)
          flags.push('no chop-test curve for this cartridge — factory velocity used as-is');
      }
      var dT = tPow - 59;
      mv += sT * dT;
      var infl = (5 * Math.abs(dT) / 20) * (measuredSens ? 0.5 : 1);
      sigmaSys = Math.sqrt(sigmaSys * sigmaSys + infl * infl);
      if (gun.type === 'revolver') {                                // §3.4
        var f = gun.gap_loss_pct != null ? gun.gap_loss_pct / 100 : 0.03;
        mv *= (1 - f);
        sigmaSys = Math.sqrt(sigmaSys * sigmaSys + 15 * 15);
        flags.push('cylinder-gap loss ≈' + Math.round(f * 100) + '% applied');
      }
    }
    return { mv: mv, sd: sdShot, sigmaSys: sigmaSys, quality: quality, flags: flags };
  }

  // ---- Module B: spin & stability (spec §4) ----
  function stability(gun, load, cart, mv, cond) {
    if (gun.twist_in == null) return { smoothbore: true, spinChannels: false };
    var g = bulletGeom(load, cart);
    if (!g.d || !g.len) return { unknown: true, spinChannels: false };
    var m = load.bullet.weight_gr, d = g.d, l = g.len / d, t = gun.twist_in / d;
    var sg = 30 * m / (t * t * d * d * d * l * (1 + l * l));
    sg *= Math.pow(Math.max(mv, 300) / 2800, 1 / 3);
    var atm = atmosphere(cond);
    sg *= ((cond.temp_f != null ? cond.temp_f : 59) + 459.67) / 518.67 * (29.92 / atm.pressure);
    var rpm = mv * 12 / gun.twist_in * 60;
    var state = 'stable', bcPenalty = 1;
    if (sg < 1.0) state = 'unstable';
    else if (sg < 1.5) { state = 'marginal'; bcPenalty = Math.max(0.75, 1 - 0.03 * (1.5 - sg) / 0.1); }
    // Litz spin-drift / aero-jump validity domain: rifle-class cartridges only (§4.5)
    var spinChannels = !!cart && cart['class'] === 'rifle' && state !== 'unstable';
    return { sg: sg, sgFit: Math.min(sg, 2.5), rpm: rpm, state: state, bcPenalty: bcPenalty,
             lengthEstimated: g.est, lenCal: l, d: d, spinChannels: spinChannels };
  }

  // ---- Module C: flight solver (spec §6) ----
  // p: { mv, bc, dragTable, weightGr, sightHIn, zeroYd, maxYd, stepYd,
  //      windMph, windDeg (0=headwind, clockwise from 12), atm {rho,mach1},
  //      coriolis {on,lat_deg,az_deg} | null,
  //      spin {on, sgFit, dir} | null, aeroJumpMoaPerMph, jumpSign,
  //      dt (s), noDrag (test hook) }
  function integrate(p, angle, withWind, withCor) {
    var dt = p.dt || 0.0005;
    var maxX = p.maxYd * 3, stepFt = p.stepYd * 3;
    var wRad = (p.windDeg || 0) * Math.PI / 180;
    var ws = withWind ? (p.windMph || 0) * MPH_TO_FPS : 0;
    var wx = -ws * Math.cos(wRad), wz = -ws * Math.sin(wRad); // wind FROM windDeg
    var kBase = p.noDrag ? 0 : K0 * (p.atm.rho / RHO0) / p.bc;
    var cor = null;
    if (withCor && p.coriolis && p.coriolis.on) {
      var la = (p.coriolis.lat_deg || 45) * Math.PI / 180,
          az = (p.coriolis.az_deg || 0) * Math.PI / 180;
      cor = [OMEGA * Math.cos(la) * Math.cos(az), OMEGA * Math.sin(la), -OMEGA * Math.cos(la) * Math.sin(az)];
    }
    var table = p.dragTable, mach1 = p.atm.mach1;
    function accel(vx, vy, vz) {
      var rx = vx - wx, ry = vy, rz = vz - wz;
      var vr = Math.sqrt(rx * rx + ry * ry + rz * rz);
      var k = kBase ? kBase * cdLookup(table, vr / mach1) * vr : 0;
      var ax = -k * rx, ay = -k * ry - G, az2 = -k * rz;
      if (cor) {
        ax += -2 * (cor[1] * vz - cor[2] * vy);
        ay += -2 * (cor[2] * vx - cor[0] * vz);
        az2 += -2 * (cor[0] * vy - cor[1] * vx);
      }
      return [ax, ay, az2];
    }
    var x = 0, y = -p.sightHIn / 12, z = 0, t = 0;
    var vx = p.mv * Math.cos(angle), vy = p.mv * Math.sin(angle), vz = 0;
    var out = [], nextX = 0, maxOrd = -1e9;
    var m12 = null, m10 = null; // transonic crossing ranges (ft)
    var px = x, py = y, pz = z, pvx = vx, pvy = vy, pvz = vz, pt = t;
    for (var i = 0; i < 500000; i++) {
      // sample crossings by linear interpolation between steps
      while (nextX <= x && nextX <= maxX + 1e-9) {
        var f = (x - px) > 1e-12 ? (nextX - px) / (x - px) : 0;
        var sy = py + f * (y - py), sz = pz + f * (z - pz),
            svx = pvx + f * (vx - pvx), svy = pvy + f * (vy - pvy), svz = pvz + f * (vz - pvz),
            st = pt + f * (t - pt);
        var sv = Math.sqrt(svx * svx + svy * svy + svz * svz);
        out.push({ ft: nextX, y: sy, z: sz, v: sv, t: st, mach: sv / mach1 });
        nextX += stepFt;
      }
      if (x > maxX) break;
      var vAbs = Math.sqrt(vx * vx + vy * vy + vz * vz);
      var mach = vAbs / mach1;
      if (m12 === null && mach < 1.2 && i > 0) m12 = x;
      if (m10 === null && mach < 1.0 && i > 0) m10 = x;
      if (vAbs < 200 || t > 10 || y < -2000) break;
      if (y > maxOrd) maxOrd = y;
      px = x; py = y; pz = z; pvx = vx; pvy = vy; pvz = vz; pt = t;
      // RK4
      var k1 = accel(vx, vy, vz);
      var k2 = accel(vx + 0.5 * dt * k1[0], vy + 0.5 * dt * k1[1], vz + 0.5 * dt * k1[2]);
      var k3 = accel(vx + 0.5 * dt * k2[0], vy + 0.5 * dt * k2[1], vz + 0.5 * dt * k2[2]);
      var k4 = accel(vx + dt * k3[0], vy + dt * k3[1], vz + dt * k3[2]);
      var nvx = vx + dt / 6 * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0]),
          nvy = vy + dt / 6 * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1]),
          nvz = vz + dt / 6 * (k1[2] + 2 * k2[2] + 2 * k3[2] + k4[2]);
      x += dt / 2 * (vx + nvx); y += dt / 2 * (vy + nvy); z += dt / 2 * (vz + nvz);
      t += dt; vx = nvx; vy = nvy; vz = nvz;
    }
    return { samples: out, maxOrdFt: maxOrd, m12ft: m12, m10ft: m10 };
  }

  function zeroAngle(p) {
    var angle = 0, zeroFt = p.zeroYd * 3;
    var pz = Object.assign({}, p, { maxYd: p.zeroYd, stepYd: p.zeroYd });
    for (var i = 0; i < 3; i++) {
      var r = integrate(pz, angle, false, false);
      var at = null;
      for (var j = 0; j < r.samples.length; j++)
        if (Math.abs(r.samples[j].ft - zeroFt) < 1e-6) at = r.samples[j];
      if (!at) break;
      angle += Math.atan2(-at.y, zeroFt);
    }
    return angle;
  }

  // Full deterministic solve → channel rows (spec §6.5)
  function solve(p) {
    var angle = zeroAngle(p);
    var r = integrate(p, angle, true, true);
    var wRad = (p.windDeg || 0) * Math.PI / 180;
    var crossMph = -(p.windMph || 0) * Math.sin(wRad); // + = air moving right (from left)
    var jump = p.aeroJumpMoaPerMph || 0;               // MOA per mph, §4.6
    var spinOn = p.spin && p.spin.on, sgF = spinOn ? p.spin.sgFit : 0,
        spinSign = (p.spin && p.spin.dir === 'L') ? -1 : 1;
    var jumpSign = spinSign; // RH twist, wind from left (air →right, crossMph>0) ⇒ POI down
    var rows = r.samples.map(function (s) {
      var yd = s.ft / 3;
      var spinIn = spinOn ? spinSign * 1.25 * (sgF + 1.2) * Math.pow(s.t, 1.83) : 0;
      var jumpMoa = spinOn ? -jumpSign * jump * crossMph : 0;
      var jumpIn = jumpMoa * 1.047 * yd / 100;
      var dropIn = s.y * 12 + jumpIn;
      var windIn = s.z * 12 + spinIn;
      return { yd: yd, dropIn: dropIn, windIn: windIn, lagIn: s.z * 12,
               spinIn: spinIn, jumpIn: jumpIn, v: s.v, mach: s.mach,
               energy: p.weightGr * s.v * s.v / 450435, tof: s.t };
    });
    return { rows: rows, angle: angle,
             supersonic12Yd: r.m12ft != null ? r.m12ft / 3 : null,
             subsonicYd: r.m10ft != null ? r.m10ft / 3 : null };
  }

  // ---- derived metrics (spec §6.6) ----
  function mpbr(p, vitalD) {
    // find zero range whose max ordinate = vitalD/2, then far range where path = -vitalD/2
    var half = vitalD / 2 / 12; // ft
    var lo = 10, hi = Math.max(p.maxYd, 300);
    var pc = Object.assign({}, p, { dt: 0.002, stepYd: 10 });
    for (var i = 0; i < 16; i++) {
      var mid = (lo + hi) / 2;
      var ang = zeroAngle(Object.assign({}, pc, { zeroYd: mid }));
      var r = integrate(Object.assign({}, pc, { maxYd: hi }), ang, false, false);
      if (r.maxOrdFt > half) hi = mid; else lo = mid;
    }
    var zr = (lo + hi) / 2;
    var ang2 = zeroAngle(Object.assign({}, pc, { zeroYd: zr }));
    var rr = integrate(Object.assign({}, pc, { maxYd: Math.max(p.maxYd, 500) }), ang2, false, false);
    var far = null, s = rr.samples;
    for (var j = 1; j < s.length; j++) {
      if (s[j].y <= -half && s[j - 1].y > -half) {
        var f = (-half - s[j - 1].y) / (s[j].y - s[j - 1].y);
        far = (s[j - 1].ft + f * (s[j].ft - s[j - 1].ft)) / 3;
        break;
      }
    }
    return { zeroYd: zr, mpbrYd: far };
  }

  function crossingYd(rows, key, threshold, descending) {
    for (var i = 1; i < rows.length; i++) {
      var a = rows[i - 1][key], b = rows[i][key];
      if (descending ? (a >= threshold && b < threshold) : (a <= threshold && b > threshold)) {
        var f = (threshold - a) / (b - a);
        return rows[i - 1].yd + f * (rows[i].yd - rows[i - 1].yd);
      }
    }
    return null;
  }

  function interpRow(rows, yd) {
    if (!rows.length) return null;
    if (yd <= rows[0].yd) return rows[0];
    for (var i = 1; i < rows.length; i++) {
      if (rows[i].yd >= yd) {
        var a = rows[i - 1], b = rows[i], f = (yd - a.yd) / (b.yd - a.yd || 1);
        var o = {};
        for (var k in a) if (typeof a[k] === 'number') o[k] = a[k] + f * (b[k] - a[k]);
        return o;
      }
    }
    return rows[rows.length - 1];
  }

  // danger space (§6.6): hold so POI=POA at pinnedYd; span where |adjusted drop| <= D/2
  function dangerSpace(rows, pinnedYd, vitalD) {
    var at = interpRow(rows, pinnedYd);
    if (!at) return null;
    var holdMoaIn = at.dropIn / pinnedYd; // in per yd (angular hold)
    var half = vitalD / 2, lo = null, hi = null;
    for (var i = 0; i < rows.length; i++) {
      var adj = rows[i].dropIn - holdMoaIn * rows[i].yd;
      var inWin = Math.abs(adj) <= half;
      if (inWin && lo === null) lo = rows[i].yd;
      if (inWin) hi = rows[i].yd;
      if (!inWin && lo !== null && rows[i].yd > pinnedYd) break;
    }
    return lo != null ? { fromYd: lo, toYd: hi, spanYd: hi - lo } : null;
  }

  // ---- recoil (spec §8) ----
  function recoil(gun, load, cart, mv) {
    var wb = load.bullet.weight_gr, wp = cart ? (cart.typ_charge_gr || 0) : 0;
    var vGas = cart && cart['class'] === 'shotgun' ? 4000
             : (cart && (cart['class'] === 'handgun' || cart['class'] === 'rimfire'))
               ? Math.min(1.5 * mv, 4000) : 4700;
    var vGun = (wb * mv + wp * vGas) / (7000 * (gun.weight_lb || 7));
    return { energyFtLb: (gun.weight_lb || 7) * vGun * vGun / (2 * G), estimated: true };
  }

  // ---- Monte Carlo (spec §7) ----
  function mulberry32(seed) {
    var a = seed >>> 0;
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function gauss(rnd) {
    var u = 0, v = 0;
    while (u === 0) u = rnd();
    while (v === 0) v = rnd();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }
  function percentile(sorted, q) {
    var idx = q * (sorted.length - 1), lo = Math.floor(idx), f = idx - lo;
    return sorted[lo] + f * ((sorted[Math.min(lo + 1, sorted.length - 1)]) - sorted[lo]);
  }

  // mc: p = deterministic solve params; extras = {sd, sigmaSys, bcTolPct, gustSd, matchClass, cond}
  function monteCarlo(p, extras, N, seed) {
    N = N || 400;
    var rnd = mulberry32(seed || 1234567);
    var mcP = Object.assign({}, p, { dt: 0.002 });
    var nSteps = Math.floor(p.maxYd / p.stepYd) + 1;
    var drops = [], winds = [];
    for (var s = 0; s < nSteps; s++) { drops.push([]); winds.push([]); }
    var mvSigma = Math.sqrt(extras.sd * extras.sd + extras.sigmaSys * extras.sigmaSys);
    var gustSd = extras.gustSd != null ? extras.gustSd : Math.max(1, 0.35 * (p.windMph || 0));
    for (var n = 0; n < N; n++) {
      var d = Object.assign({}, mcP);
      d.mv = p.mv + gauss(rnd) * mvSigma;
      d.bc = p.bc * (1 + gauss(rnd) * (extras.bcTolPct || 0.02));
      d.windMph = Math.max(0, (p.windMph || 0) + gauss(rnd) * gustSd);
      d.windDeg = (p.windDeg || 0) + gauss(rnd) * 10;
      var tempD = (extras.cond && extras.cond.temp_f != null ? extras.cond.temp_f : 59) + gauss(rnd) * 3;
      d.atm = atmosphere(Object.assign({}, extras.cond || {}, { temp_f: tempD }));
      var rows = solve(d).rows;
      for (var i = 0; i < nSteps && i < rows.length; i++) {
        drops[i].push(rows[i].dropIn);
        winds[i].push(rows[i].windIn);
      }
    }
    var out = [];
    for (var i2 = 0; i2 < nSteps; i2++) {
      var ds = drops[i2].slice().sort(function (a, b) { return a - b; });
      var ws = winds[i2].slice().sort(function (a, b) { return a - b; });
      if (!ds.length) { out.push(null); continue; }
      var meanD = ds.reduce(function (a, b) { return a + b; }, 0) / ds.length;
      var meanW = ws.reduce(function (a, b) { return a + b; }, 0) / ws.length;
      var varD = 0, varW = 0;
      for (var j2 = 0; j2 < ds.length; j2++) { varD += (ds[j2] - meanD) * (ds[j2] - meanD); varW += (ws[j2] - meanW) * (ws[j2] - meanW); }
      out.push({
        yd: i2 * p.stepYd,
        dropP10: percentile(ds, 0.1), dropP50: percentile(ds, 0.5), dropP90: percentile(ds, 0.9),
        windP10: percentile(ws, 0.1), windP50: percentile(ws, 0.5), windP90: percentile(ws, 0.9),
        sdV: Math.sqrt(varD / ds.length), sdH: Math.sqrt(varW / ws.length)
      });
    }
    return out;
  }

  // ---- hit probability (spec §7.3) ----
  function erf(x) { // Abramowitz–Stegun 7.1.26
    var s = x < 0 ? -1 : 1; x = Math.abs(x);
    var t = 1 / (1 + 0.3275911 * x);
    var y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
    return s * y;
  }
  var SQRT2 = Math.sqrt(2), SQRT2PI = Math.sqrt(2 * Math.PI);
  function normCdf(x) { return 0.5 * (1 + erf(x / SQRT2)); }

  // sigma in inches; vitalD inches. 1-D reduction, Simpson (integrand is smooth).
  function hitProb(sigV, sigH, vitalD) {
    var r = vitalD / 2;
    if (sigV <= 0 || sigH <= 0) return 1;
    var ratio = sigV / sigH;
    if (ratio > 0.9 && ratio < 1.1) {
      var s2 = (sigV * sigV + sigH * sigH) / 2;
      return 1 - Math.exp(-r * r / (2 * s2));
    }
    var n = 96, h = 2 * r / n, sum = 0;
    for (var i = 0; i <= n; i++) {
      var x = -r + i * h;
      var half = Math.sqrt(Math.max(r * r - x * x, 0));
      var fx = Math.exp(-x * x / (2 * sigH * sigH)) / (sigH * SQRT2PI)
             * (normCdf(half / sigV) - normCdf(-half / sigV));
      sum += fx * (i === 0 || i === n ? 1 : (i % 2 ? 4 : 2));
    }
    return Math.min(1, sum * h / 3);
  }

  // hit-prob curve per loadout; slope-based extra vertical sigma when range estimated
  function hitCurve(mcRows, detRows, precisionMoa, vitalD, rangeEstimated) {
    var out = [];
    for (var i = 0; i < mcRows.length; i++) {
      var m = mcRows[i];
      if (!m || m.yd === 0) { out.push({ yd: m ? m.yd : 0, p: 1 }); continue; }
      var prec = (precisionMoa / 3) * 1.047 * m.yd / 100; // §7.3 convention (X/3 per axis)
      var sv2 = m.sdV * m.sdV + prec * prec, sh2 = m.sdH * m.sdH + prec * prec;
      if (rangeEstimated && detRows) {
        var a = interpRow(detRows, Math.max(m.yd - 25, 0)), b = interpRow(detRows, m.yd + 25);
        if (a && b) {
          var slope = (b.dropIn - a.dropIn) / 50; // in/yd
          var rngSig = Math.abs(slope) * 0.03 * m.yd;
          sv2 += rngSig * rngSig;
        }
      }
      out.push({ yd: m.yd, p: hitProb(Math.sqrt(sv2), Math.sqrt(sh2), vitalD) });
    }
    return out;
  }

  // ---- truing (spec §9) ----
  function goldenMin(f, lo, hi, iters) {
    var gr = (Math.sqrt(5) - 1) / 2;
    var a = lo, b = hi, c = b - gr * (b - a), d = a + gr * (b - a);
    var fc = f(c), fd = f(d);
    for (var i = 0; i < (iters || 40); i++) {
      if (fc < fd) { b = d; d = c; fd = fc; c = b - gr * (b - a); fc = f(c); }
      else { a = c; c = d; fc = fd; d = a + gr * (b - a); fd = f(d); }
    }
    return (a + b) / 2;
  }

  function dropAt(p, yd) {
    var r = solve(Object.assign({}, p, { maxYd: Math.max(yd, p.zeroYd) + p.stepYd, dt: 0.001 }));
    var row = interpRow(r.rows, yd);
    return row ? row.dropIn : null;
  }

  function truingSensitivity(p, yd) { // ∂drop/∂MV per 10 fps
    var d1 = dropAt(Object.assign({}, p, { mv: p.mv - 10 }), yd);
    var d2 = dropAt(Object.assign({}, p, { mv: p.mv + 10 }), yd);
    return Math.abs((d2 - d1) / 2);
  }

  function trueMv(p, observations) {
    var eligible = observations.filter(function (o) { return truingSensitivity(p, o.range_yd) >= 0.1; });
    if (!eligible.length) return { ok: false, reason: 'No observation is far enough to resolve MV (needs ≥0.1 in of drop change per 10 fps — shoot farther).' };
    function cost(mv) {
      var s = 0;
      for (var i = 0; i < eligible.length; i++) {
        var o = eligible[i];
        var w = o.group_moa ? 1 / Math.pow((o.group_moa / 3) * 1.047 * o.range_yd / 100, 2) : 1;
        var pred = dropAt(Object.assign({}, p, { mv: mv }), o.range_yd);
        s += w * Math.pow(pred - o.observed_drop_in, 2);
      }
      return s;
    }
    var best = goldenMin(cost, p.mv - 150, p.mv + 150, 36);
    var rss = 0;
    for (var i = 0; i < eligible.length; i++)
      rss += Math.pow(dropAt(Object.assign({}, p, { mv: best }), eligible[i].range_yd) - eligible[i].observed_drop_in, 2);
    var rmsIn = Math.sqrt(rss / eligible.length);
    var sens = truingSensitivity(Object.assign({}, p, { mv: best }), eligible[eligible.length - 1].range_yd);
    var rmsFps = sens > 0 ? (rmsIn / sens) * 10 : 30;
    return { ok: true, mv: best, rmsFps: Math.max(5, rmsFps), nUsed: eligible.length };
  }

  function trueBc(p, observations, supersonicYd) {
    var far;
    if (supersonicYd) {
      far = observations.filter(function (o) { return o.range_yd >= 0.8 * supersonicYd; });
      if (!far.length) return { ok: false, reason: 'BC truing needs an observation at ≥' + Math.round(0.8 * supersonicYd) + ' yd.' };
    } else { // always-subsonic: need 2 well-separated observations (§9)
      var sorted = observations.slice().sort(function (a, b) { return a.range_yd - b.range_yd; });
      if (sorted.length < 2 || sorted[sorted.length - 1].range_yd < 2 * sorted[0].range_yd)
        return { ok: false, reason: 'Subsonic BC truing needs ≥2 observations at well-separated ranges.' };
      far = sorted;
    }
    function cost(scale) {
      var s = 0;
      for (var i = 0; i < far.length; i++)
        s += Math.pow(dropAt(Object.assign({}, p, { bc: p.bc * scale }), far[i].range_yd) - far[i].observed_drop_in, 2);
      return s;
    }
    var scale = goldenMin(cost, 0.8, 1.2, 30);
    return { ok: true, bcScale: scale };
  }

  // ---- self test (spec §11 subset, runnable offline) ----
  function selftest() {
    var t = [];
    function check(name, got, want, tol) {
      t.push({ name: name, got: got, want: want, pass: Math.abs(got - want) <= tol });
    }
    // atmosphere
    var std = atmosphere({ temp_f: 59, alt_mode: 'pressure', station_inhg: 29.92, humidity_pct: 0 });
    check('rho @ ICAO std', std.rho, 0.0764742, 1e-4);
    check('mach1 @ 59F', std.mach1, 1116.45, 0.1);
    var denver = atmosphere({ temp_f: 59, altitude_ft: 5280, altimeter_inhg: 29.92, humidity_pct: 0 });
    check('rho @ Denver 5280ft/59F', denver.rho, 0.0630, 0.0007); // 59F held (not ISA lapse)
    var humid = atmosphere({ temp_f: 95, alt_mode: 'pressure', station_inhg: 29.92, humidity_pct: 90 });
    check('vapor e_s95F*0.9', humid.vapor, 1.494, 0.05);
    check('rho @ 95F/90%RH', humid.rho, 0.0699, 0.0007);
    // vacuum parabola: 45deg-ish small angle check via noDrag
    var pv = { mv: 1000, bc: 1, dragTable: DRAG.models.G1, weightGr: 100, sightHIn: 0,
               zeroYd: 100, maxYd: 300, stepYd: 100, windMph: 0, windDeg: 0,
               atm: std, coriolis: null, spin: null, aeroJumpMoaPerMph: 0, noDrag: true, dt: 0.0005 };
    var ang = zeroAngle(pv);
    var want = Math.asin(G * 300 / (1000 * 1000)) / 2; // vacuum zero angle for R=300ft
    check('vacuum zero angle', ang, want, want * 0.001 + 1e-7);
    // .308 175 SMK G1 sanity vs v1-validated numbers (100yd zero)
    var p308 = { mv: 2600, bc: 0.505, dragTable: DRAG.models.G1, weightGr: 175, sightHIn: 1.5,
                 zeroYd: 100, maxYd: 500, stepYd: 100, windMph: 10, windDeg: 90,
                 atm: std, coriolis: null, spin: null, aeroJumpMoaPerMph: 0, dt: 0.0005 };
    var r308 = solve(p308);
    check('.308 drop @300', interpRow(r308.rows, 300).dropIn, -15.7, 0.9);
    check('.308 drop @500', interpRow(r308.rows, 500).dropIn, -63.4, 2.5);
    check('.308 vel @500', interpRow(r308.rows, 500).v, 1792, 25);
    check('.308 10mph drift @500', Math.abs(interpRow(r308.rows, 500).windIn), 20.9, 2.0);
    // Sg anchor: Miller's own paper example — 168gr Sierra (L=1.226) in 1:12 ≈ 1.7 std,
    // ≈1.65 after the (2600/2800)^(1/3) velocity correction
    var sgR = stability({ twist_in: 12, type: 'rifle' },
      { bullet: { weight_gr: 168, diameter_in: 0.308, length_in: 1.226 }, 'class': 'match', sd_fps: 12, mv: { fps: 2600 } },
      { 'class': 'rifle', bullet_diameter_in: 0.308 }, 2600, { temp_f: 59 });
    check('Sg 168 Sierra 1:12 (Miller)', sgR.sg, 1.65, 0.15);
    // hit prob Rayleigh
    check('P_hit sigma=1,r=1', hitProb(1, 1, 2), 1 - Math.exp(-0.5), 1e-3);
    // asymmetric vs Rayleigh cross-check
    check('P_hit 1x1 via Simpson', hitProb(1, 1.15, 2.0), 0.36, 0.05);
    return t;
  }

  return {
    init: init, atmosphere: atmosphere, leduc: leduc, muzzleState: muzzleState,
    stability: stability, solve: solve, mpbr: mpbr, dangerSpace: dangerSpace,
    crossingYd: crossingYd, interpRow: interpRow, recoil: recoil,
    monteCarlo: monteCarlo, hitCurve: hitCurve, hitProb: hitProb,
    trueMv: trueMv, trueBc: trueBc, truingSensitivity: truingSensitivity,
    bulletGeom: bulletGeom, selftest: selftest,
    CONST: { G: G, RHO0: RHO0, K0: K0, OMEGA: OMEGA, MPH_TO_FPS: MPH_TO_FPS }
  };
})();
if (typeof self !== 'undefined') self.BX = BX;
