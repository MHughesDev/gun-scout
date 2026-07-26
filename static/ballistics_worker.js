/* Gun Scout ballistics Web Worker — wraps ballistics_engine.js.
   Protocol: main thread posts {type, id, gen, payload}; worker replies with the same
   id/gen. A bumped `gen` on the main thread makes stale replies droppable there;
   the worker also drops queued MC jobs whose gen is superseded. */
'use strict';
importScripts('ballistics_engine.js');

var currentGen = 0;

self.onmessage = function (e) {
  var msg = e.data;
  if (msg.gen != null && msg.gen > currentGen) currentGen = msg.gen;
  try {
    switch (msg.type) {
      case 'init':
        BX.init(msg.payload.dragmodels);
        self.postMessage({ type: 'ready', id: msg.id, gen: msg.gen });
        break;

      case 'solve': { // deterministic solve + derived metrics for one loadout
        var p = msg.payload.params;
        var det = BX.solve(p);
        var derived = { supersonic12Yd: det.supersonic12Yd, subsonicYd: det.subsonicYd };
        if (msg.payload.vitalD) {
          try { derived.mpbr = BX.mpbr(p, msg.payload.vitalD); } catch (err) { derived.mpbr = null; }
          derived.danger = BX.dangerSpace(det.rows, msg.payload.pinnedYd || 300, msg.payload.vitalD);
        }
        derived.energy1000Yd = BX.crossingYd(det.rows, 'energy', 1000, true);
        derived.energy1500Yd = BX.crossingYd(det.rows, 'energy', 1500, true);
        if (msg.payload.minExpansionFps)
          derived.expansionYd = BX.crossingYd(det.rows, 'v', msg.payload.minExpansionFps, true);
        self.postMessage({ type: 'solved', id: msg.id, gen: msg.gen,
                           rows: det.rows, angle: det.angle, derived: derived });
        break;
      }

      case 'mc': { // Monte Carlo for one loadout (streamed per loadout by the caller)
        if (msg.gen < currentGen) break; // superseded before it ran
        var mc = BX.monteCarlo(msg.payload.params, msg.payload.extras,
                               msg.payload.N || 400, msg.payload.seed || 1234567);
        if (msg.gen < currentGen) break; // superseded while running
        var hit = BX.hitCurve(mc, msg.payload.detRows, msg.payload.precisionMoa || 1.5,
                              msg.payload.vitalD || 6, !!msg.payload.rangeEstimated);
        self.postMessage({ type: 'mcDone', id: msg.id, gen: msg.gen, mc: mc, hit: hit });
        break;
      }

      case 'trueMv': {
        var res = BX.trueMv(msg.payload.params, msg.payload.observations);
        if (res.ok && msg.payload.tryBc) {
          var det2 = BX.solve(msg.payload.params);
          var bcRes = BX.trueBc(Object.assign({}, msg.payload.params, { mv: res.mv }),
                                msg.payload.observations, det2.supersonic12Yd);
          if (bcRes.ok) res.bcScale = bcRes.bcScale;
        }
        self.postMessage({ type: 'trued', id: msg.id, gen: msg.gen, result: res });
        break;
      }

      case 'selftest':
        self.postMessage({ type: 'selftestDone', id: msg.id, results: BX.selftest() });
        break;
    }
  } catch (err) {
    self.postMessage({ type: 'error', id: msg.id, gen: msg.gen,
                       message: String(err && err.message || err), stack: String(err && err.stack || '') });
  }
};
