/* ===================================================================
 * NOVA3D — rotatable axonometric 3D residence model (SVG).
 * Self-contained, no build/CDN. The DEFAULT HOUSE spec (dimensions,
 * room layout, garage doors, dormers) lives at the top of this IIFE;
 * edit it for a different home. Occupancy is data-driven from HA areas.
 * =================================================================== */
/* Nova Residence — 3D house core (v3 rebuild)
 * Real dimensions from the architect's ApexSketch; labels/layout from the Nova editor.
 * Pure-geometry axonometric projection rendered to SVG so it is (a) rotatable in the
 * browser and (b) rasterizable here via cairosvg for verification. Same math both places.
 * Works under Node (module.exports) and in the browser (window.NOVA3D).
 */
const NOVA3D = (function () {
  'use strict';

  // ---------- real dimensions (feet) ----------
  var GW = 30, HW = 33, D = 24;                 // garage W, house W, depth
  var XG0 = 0, XGH = GW, XHE = GW + HW;         // garage 0..30, house 30..63
  var WALL = 9;                                 // 1st-floor wall height = main eave
  var BASE_RISE = 11, GBASE_RISE = 5;           // roof rises at pitch 1.0
  var RISE = BASE_RISE, RIDGE = WALL + RISE;    // main roof: eave 9 -> ridge 20
  var GWALL = 9, GRISE = GBASE_RISE, GRIDGE = GWALL + GRISE; // garage roof: eave 9 -> ridge 14
  var RY = D / 2;                               // ridge centerline (depth) = 12
  var OVH = 1.2;                                // roof overhang
  var SCALE = 8.6;                              // feet -> px
  var PITCH = 30 * Math.PI / 180;               // camera elevation
  var CENTER = [(XG0 + XHE) / 2, RY, WALL * 0.5]; // rotate about model center

  // ---------- per-render home spec (type/specs); fields left unset = approved default ----------
  // garageBays, dormersFront, dormersRear: counts · chimney: 'right'|'left'|'none' · pitch: roof-rise scale
  var SPEC = {};
  function applySpec(s) {
    SPEC = s || {};
    var p = SPEC.pitch > 0 ? SPEC.pitch : 1;
    RISE = BASE_RISE * p; RIDGE = WALL + RISE;
    GRISE = GBASE_RISE * p; GRIDGE = GWALL + GRISE;
  }

  // ---------- palette (Nova dark-cyan HUD) ----------
  // Warm ember/gold palette matching Command Center's own theme tokens
  // (--ember:#e2542f, --gold:#f4b860, --gold-pale:#ffe3ad) — this engine
  // used to be Classic's own cyan (#00f2fe), reused as-is when Command
  // Center's Residence tab first adopted it (v7.101.24). Retheme keeps the
  // exact same alpha/opacity structure per state (off/on/dominant), only
  // the hue changes, so occupancy contrast logic is untouched. "dom" (the
  // currently-dominant/focused area) stays green — consistent with the
  // rest of the app's own status-dot language (RUNNING/ONLINE are green).
  var C = {
    wallF: 'rgba(42,33,25,0.34)', wallS: 'rgba(244,184,96,0.5)',
    wallDk:'rgba(21,17,13,0.40)', wallSdk:'rgba(244,184,96,0.34)',
    roofF: 'rgba(21,17,13,0.94)', roofS: 'rgba(226,84,47,0.5)',
    roofDk:'rgba(15,12,9,0.96)',  roofSdk:'rgba(226,84,47,0.3)',
    gableF:'rgba(38,30,22,0.6)',  gableS:'rgba(244,184,96,0.46)',
    chimF: 'rgba(24,19,14,0.97)', chimS:'rgba(244,184,96,0.42)',
    doorOff:'rgba(244,184,96,0.10)', doorOn:'rgba(244,184,96,0.30)', doorS:'rgba(244,184,96,0.6)',
    doorOpen:'rgba(255,170,40,0.32)', doorOpenS:'rgba(255,190,72,0.95)', doorOpenGlow:'rgba(255,170,40,0.42)',
    winOff:'rgba(244,184,96,0.07)', winOn:'rgba(244,184,96,0.72)', winDom:'rgba(0,245,160,0.82)',
    glassOff:'rgba(244,184,96,0.32)', glassOn:'rgba(255,227,173,0.92)', glassDom:'rgba(150,255,210,0.95)',
    edge:'rgba(244,184,96,0.5)', dim:'rgba(244,184,96,0.26)', faint:'rgba(244,184,96,0.13)',
    glowOn:'rgba(244,184,96,0.5)', glowDom:'rgba(0,245,160,0.55)'
  };

  // ---------- projection (turntable axonometric, orthographic) ----------
  function rot(p, t) {
    var x = p[0] - CENTER[0], y = p[1] - CENTER[1], z = p[2] - CENTER[2];
    var c = Math.cos(t), s = Math.sin(t);
    return [x * c + y * s, -x * s + y * c, z]; // rotated (rx, ry, z)
  }
  function project(p, thetaDeg) {
    var r = rot(p, thetaDeg * Math.PI / 180);
    return [r[0] * SCALE, -(r[1] * Math.sin(PITCH) + r[2] * Math.cos(PITCH)) * SCALE];
  }
  function faceDepth(face, thetaDeg) {
    var t = thetaDeg * Math.PI / 180, cy = 0, cz = 0, n = face.p.length, i, r;
    for (i = 0; i < n; i++) { r = rot(face.p[i], t); cy += r[1]; cz += r[2]; }
    cy /= n; cz /= n;
    return cy * Math.cos(PITCH) - cz * Math.sin(PITCH); // larger = farther from camera
  }

  // ---------- face helpers ----------
  function F(list, pts, fill, stroke, sw, extra) {
    var o = { p: pts, f: fill, s: stroke || C.edge, w: (sw == null ? 0.9 : sw) };
    if (extra) for (var k in extra) o[k] = extra[k];
    list.push(o);
  }
  // quad on a vertical plane y=const (a wall facing front/back)
  function wallY(L, y, x0, x1, z0, z1, f, s, w) { F(L, [[x0,y,z0],[x1,y,z0],[x1,y,z1],[x0,y,z1]], f, s, w); }
  // gable end on plane x=const: wall rect + triangle to ridge
  function gableEnd(L, x, yA, yB, zWall, yPk, zPk, f, s, w) {
    F(L, [[x,yA,0],[x,yB,0],[x,yB,zWall],[x,yPk,zPk],[x,yA,zWall]], f, s, w);
  }

  // ---------- a lit window on the front/back plane (y=const) ----------
  function winY(L, GL, y, x0, x1, z0, z1, state, mull, faceOut, cols) {
    var f = state === 'dom' ? C.winDom : state === 'on' ? C.winOn : C.winOff;
    var st = state === 'dom' ? C.glassDom : state === 'on' ? C.glassOn : C.glassOff;
    var n = faceOut == null ? -0.06 : faceOut;
    var yy = y + n;
    F(L, [[x0,yy,z0],[x1,yy,z0],[x1,yy,z1],[x0,yy,z1]], f, st, 0.7);
    if (mull) {
      F(L, [[x0,yy,(z0+z1)/2],[x1,yy,(z0+z1)/2]], 'none', st, 0.4);
      var nc = cols || 2, k;
      for (k = 1; k < nc; k++) { var xm = x0 + (x1 - x0) * k / nc; F(L, [[xm,yy,z0],[xm,yy,z1]], 'none', st, 0.4); }
    }
    if (state !== 'off' && GL) {
      var g = state === 'dom' ? C.glowDom : C.glowOn;
      GL.push({ p: [[x0-1.4,yy,z0-1.4],[x1+1.4,yy,z0-1.4],[x1+1.4,yy,z1+1.4],[x0-1.4,yy,z1+1.4]], f: g });
    }
  }
  // window on the end plane (x=const)
  function winX(L, GL, x, y0, y1, z0, z1, state, mull, faceOut) {
    var f = state === 'dom' ? C.winDom : state === 'on' ? C.winOn : C.winOff;
    var st = state === 'dom' ? C.glassDom : state === 'on' ? C.glassOn : C.glassOff;
    var n = faceOut == null ? 0.06 : faceOut;
    var xx = x + n;
    F(L, [[xx,y0,z0],[xx,y1,z0],[xx,y1,z1],[xx,y0,z1]], f, st, 0.7);
    if (mull) {
      F(L, [[xx,y0,(z0+z1)/2],[xx,y1,(z0+z1)/2]], 'none', st, 0.4);
      F(L, [[xx,(y0+y1)/2,z0],[xx,(y0+y1)/2,z1]], 'none', st, 0.4);
    }
    if (state !== 'off' && GL) {
      var g = state === 'dom' ? C.glowDom : C.glowOn;
      GL.push({ p: [[xx,y0-1.4,z0-1.4],[xx,y1+1.4,z0-1.4],[xx,y1+1.4,z1+1.4],[xx,y0-1.4,z1+1.4]], f: g });
    }
  }

  // ---------- a door on a front/back plane (y=const). hinge 'left'|'right'; ----------
  // state 'open' → swings out (amber + glow), else flush cyan-dim. faceOut sets the side.
  function doorY(L, GL, y, x0, x1, z0, z1, faceOut, hinge, state) {
    var n = faceOut, yy = y + n;
    if (state !== 'open') {
      F(L, [[x0,yy,z0],[x1,yy,z0],[x1,yy,z1],[x0,yy,z1]], C.doorOff, C.doorS, 0.8, { cls: 'door' });
      return;
    }
    var w = x1 - x0, ang = 66 * Math.PI / 180, dir = n >= 0 ? 1 : -1;
    var dx = w * Math.cos(ang), dy = dir * w * Math.sin(ang);
    var hx = hinge === 'right' ? x1 : x0;
    var fx = hinge === 'right' ? x1 - dx : x0 + dx;
    var fy = yy + dy;
    if (GL) GL.push({ p: [[hx,yy,z0],[fx,fy,z0],[fx,fy,z1],[hx,yy,z1]], f: C.doorOpenGlow });
    F(L, [[x0,yy,z0],[x1,yy,z0],[x1,yy,z1],[x0,yy,z1]], 'rgba(9,7,5,0.92)', C.doorOpenS, 0.45);   // dark opening
    F(L, [[hx,yy,z0],[fx,fy,z0],[fx,fy,z1],[hx,yy,z1]], C.doorOpen, C.doorOpenS, 0.9, { cls: 'door door-open' }); // swung leaf
  }
  // ---------- a slanted cellar bulkhead at the base of the rear wall ----------
  function bulkhead(L, GL, x0, x1, state) {
    var open = state === 'open', yTop = D, zTop = 3.0, yBot = D + 2.6, xm = (x0 + x1) / 2;
    F(L, [[x0,yTop,0],[x0,yTop,zTop],[x0,yBot,0]], 'rgba(15,12,9,0.92)', C.dim, 0.5);   // left cheek
    F(L, [[x1,yTop,0],[x1,yTop,zTop],[x1,yBot,0]], 'rgba(15,12,9,0.92)', C.dim, 0.5);   // right cheek
    if (!open) {
      F(L, [[x0,yTop,zTop],[x1,yTop,zTop],[x1,yBot,0],[x0,yBot,0]], 'rgba(20,16,12,0.95)', C.doorS, 0.8, { cls: 'door' });
      F(L, [[xm,yTop,zTop],[xm,yBot,0]], 'none', C.doorS, 0.4);   // center seam
    } else {
      if (GL) GL.push({ p: [[x0,yTop,zTop],[x1,yTop,zTop],[x1,yTop,zTop+3.4],[x0,yTop,zTop+3.4]], f: C.doorOpenGlow });
      F(L, [[x0,yTop,zTop],[x1,yTop,zTop],[x1,yBot,0],[x0,yBot,0]], 'rgba(9,7,5,0.95)', C.doorOpenS, 0.5);  // hole into ground
      F(L, [[x0,yTop,zTop],[x1,yTop,zTop],[x1,yTop,zTop+3.4],[x0,yTop,zTop+3.4]], C.doorOpen, C.doorOpenS, 0.85, { cls: 'door door-open' }); // raised leaves
    }
  }

  function dormerFront(L, GL, cx, state) {
    var w = 6, yF = 1.6, zSill = WALL + 2.2, zHead = WALL + 6.2, zPk = WALL + 8.2, yBack = 6.2;
    var wf = 'rgba(24,19,14,0.96)', rf = 'rgba(15,12,9,0.97)', es = C.roofSdk;
    // side walls (triang│ following slope back into roof)
    F(L, [[cx-w/2,yF,zSill],[cx-w/2,yF,zHead],[cx-w/2,yBack,WALL+RISE*(1-(yBack)/RY)]], wf, es, 0.55);
    F(L, [[cx+w/2,yF,zSill],[cx+w/2,yF,zHead],[cx+w/2,yBack,WALL+RISE*(1-(yBack)/RY)]], wf, es, 0.55);
    // little gable roof (two slopes from the dormer peak back to the main slope)
    F(L, [[cx-w/2,yF,zHead],[cx,yF,zPk],[cx,yBack,WALL+RISE*(1-(yBack)/RY)+1.2],[cx-w/2,yBack,WALL+RISE*(1-(yBack)/RY)]], rf, es, 0.55);
    F(L, [[cx+w/2,yF,zHead],[cx,yF,zPk],[cx,yBack,WALL+RISE*(1-(yBack)/RY)+1.2],[cx+w/2,yBack,WALL+RISE*(1-(yBack)/RY)]], rf, es, 0.55);
    // front face (the bit that holds the window)
    F(L, [[cx-w/2,yF,zSill],[cx+w/2,yF,zSill],[cx+w/2,yF,zHead],[cx-w/2,yF,zHead]], wf, C.wallS, 0.7);
    F(L, [[cx-w/2,yF,zHead],[cx+w/2,yF,zHead],[cx,yF,zPk]], wf, C.wallS, 0.7);
    // window
    winY(L, GL, yF, cx-1.95, cx+1.95, zSill+0.4, zHead-0.4, state, true, -0.05);
  }
  // ---------- the rear dormer with a ROUND window (the upstairs bath) ----------
  function dormerRearRound(L, GL, cx, state) {
    var w = 7, yB = D - 1.6, zSill = WALL + 2.0, zHead = WALL + 6.6, zPk = WALL + 8.4, yFwd = D - 6.2;
    var wf = 'rgba(24,19,14,0.96)', rf = 'rgba(15,12,9,0.97)', es = C.roofSdk;
    var zSlope = function (yy) { return WALL + RISE * (1 - (D - yy) / RY); };
    F(L, [[cx-w/2,yB,zSill],[cx-w/2,yB,zHead],[cx-w/2,yFwd,zSlope(yFwd)]], wf, es, 0.55);
    F(L, [[cx+w/2,yB,zSill],[cx+w/2,yB,zHead],[cx+w/2,yFwd,zSlope(yFwd)]], wf, es, 0.55);
    F(L, [[cx-w/2,yB,zHead],[cx,yB,zPk],[cx,yFwd,zSlope(yFwd)+1.2],[cx-w/2,yFwd,zSlope(yFwd)]], rf, es, 0.55);
    F(L, [[cx+w/2,yB,zHead],[cx,yB,zPk],[cx,yFwd,zSlope(yFwd)+1.2],[cx+w/2,yFwd,zSlope(yFwd)]], rf, es, 0.55);
    F(L, [[cx-w/2,yB,zSill],[cx+w/2,yB,zSill],[cx+w/2,yB,zHead],[cx-w/2,yB,zHead]], wf, C.wallS, 0.7);
    F(L, [[cx-w/2,yB,zHead],[cx+w/2,yB,zHead],[cx,yB,zPk]], wf, C.wallS, 0.7);
    // round window approximated by an octagon on the y=yB plane
    var cz = (zSill + zHead) / 2 + 0.3, r = 1.7, pts = [], i, a;
    var f = state === 'dom' ? C.winDom : state === 'on' ? C.winOn : C.winOff;
    var stk = state === 'dom' ? C.glassDom : state === 'on' ? C.glassOn : C.glassOff;
    for (i = 0; i < 8; i++) { a = Math.PI / 8 + i * Math.PI / 4; pts.push([cx + r * Math.cos(a), yB - 0.05, cz + r * Math.sin(a)]); }
    F(L, pts, f, stk, 0.7);
    if (state !== 'off' && GL) GL.push({ p: [[cx-r-1.2,yB-0.05,cz-r-1.2],[cx+r+1.2,yB-0.05,cz-r-1.2],[cx+r+1.2,yB-0.05,cz+r+1.2],[cx-r-1.2,yB-0.05,cz+r+1.2]], f: state==='dom'?C.glowDom:C.glowOn });
  }

  // ---------- garage doors (count = SPEC.garageBays, default 3; fill the garage front) ----------
  function garageDoors(L, GL, state, openState) {
    var bays = SPEC.garageBays > 0 ? SPEC.garageBays : 3, gap = 1.8;
    var dw = (GW - gap * (bays + 1)) / bays, z0 = 0.4, z1 = 7.4, i, x0;
    var open = openState === 'open', lit = state === 'on' || state === 'dom';
    var f = open ? C.doorOpen : lit ? C.doorOn : C.doorOff;
    var s = open ? C.doorOpenS : lit ? C.glassOn : C.doorS;
    var cls = open ? 'gdoor door-open' : 'gdoor';
    for (i = 0; i < bays; i++) {
      x0 = gap + i * (dw + gap);
      F(L, [[x0,-0.06,z0],[x0+dw,-0.06,z0],[x0+dw,-0.06,z1],[x0,-0.06,z1]], f, s, 1.0, { cls: cls });
      for (var k = 1; k < 4; k++) { var zz = z0 + (z1 - z0) * k / 4; F(L, [[x0,-0.06,zz],[x0+dw,-0.06,zz]], 'none', s, 0.45); }
      if ((open || lit) && GL) GL.push({ p: [[x0-1.2,-0.06,z0],[x0+dw+1.2,-0.06,z0],[x0+dw+1.2,-0.06,z1+1.2],[x0-1.2,-0.06,z1+1.2]], f: open ? C.doorOpenGlow : C.glowOn });
    }
  }

  // ---------- BUILD: exterior shell + roof ----------
  // A roof slope drawn as fill strips (so a protruding dormer in front sorts correctly
  // per-strip instead of being swallowed by one big quad) plus a single crisp outline.
  function roofPlane(L, xL, xR, yE, zE, yR, zR, fill, stroke, sw) {
    var N = 12, i, xa, xb;
    for (i = 0; i < N; i++) {
      xa = xL + (xR - xL) * i / N; xb = xL + (xR - xL) * (i + 1) / N;
      F(L, [[xa,yE,zE],[xb,yE,zE],[xb,yR,zR],[xa,yR,zR]], fill, 'none', 0);
    }
    F(L, [[xL,yE,zE],[xR,yE,zE],[xR,yR,zR],[xL,yR,zR]], 'none', stroke, sw);
  }

  function buildShell(L, GL) {
    // garage walls
    wallY(L, 0, XG0, XGH, 0, GWALL, C.wallF, C.wallS, 0.85);            // garage front
    wallY(L, D, XG0, XGH, 0, GWALL, C.wallDk, C.wallSdk, 0.7);          // garage back
    gableEnd(L, XG0, 0, D, GWALL, RY, GRIDGE, C.gableF, C.gableS, 0.8); // garage left gable
    // house walls
    wallY(L, 0, XGH, XHE, 0, WALL, C.wallF, C.wallS, 0.85);             // house front
    wallY(L, D, XGH, XHE, 0, WALL, C.wallDk, C.wallSdk, 0.7);           // house back
    gableEnd(L, XHE, 0, D, WALL, RY, RIDGE, C.gableF, C.gableS, 0.85);  // house right gable (chimney end)
    gableEnd(L, XGH, 0, D, WALL, RY, RIDGE, C.gableF, C.gableSdk || C.gableS, 0.7); // house left gable (above garage)

    // garage roof (ridge ∥ house, lower)
    F(L, [[XG0-OVH,-OVH,GWALL],[XGH,-OVH,GWALL],[XGH,RY,GRIDGE],[XG0-OVH,RY,GRIDGE]], C.roofF, C.roofS, 0.85);  // front slope
    F(L, [[XG0-OVH,D+OVH,GWALL],[XGH,D+OVH,GWALL],[XGH,RY,GRIDGE],[XG0-OVH,RY,GRIDGE]], C.roofDk, C.roofSdk, 0.7); // back slope

    // main roof (strip-split so dormers in front sort correctly)
    roofPlane(L, XGH - OVH, XHE + OVH, -OVH, WALL, RY, RIDGE, C.roofF, C.roofS, 0.9);   // front slope
    roofPlane(L, XGH - OVH, XHE + OVH, D + OVH, WALL, RY, RIDGE, C.roofDk, C.roofSdk, 0.7); // back slope
  }

  function chimney(L, side) {
    if (side === 'none') return;
    var ya = 9.6, yb = 13.2, zt, x0, x1;
    if (side === 'left') { x0 = XG0; x1 = XG0 - 2.2; zt = GRIDGE + 4; }   // west gable (garage end)
    else { x0 = XHE; x1 = XHE + 2.2; zt = RIDGE + 4; }                    // default: east gable
    F(L, [[x1,ya,0],[x1,yb,0],[x1,yb,zt],[x1,ya,zt]], C.chimF, C.chimS, 0.7);      // outer
    F(L, [[x0,ya,0],[x1,ya,0],[x1,ya,zt],[x0,ya,zt]], 'rgba(15,12,9,0.97)', C.chimS, 0.6); // front side
    F(L, [[x0,yb,0],[x1,yb,0],[x1,yb,zt],[x0,yb,zt]], 'rgba(15,12,9,0.97)', C.dim, 0.5);    // back side
    F(L, [[x0,ya,zt],[x1,ya,zt],[x1,yb,zt],[x0,yb,zt]], 'rgba(244,184,96,0.08)', C.chimS, 0.5); // cap
  }

  // ---------- interior rooms (labels/layout from Nova editor; sizes from the plan) ----------
  // [x0, y0, w, d, label, occupancy-key]   (front y=0 .. rear y=24; garage 0..30, house 30..63)
  var ROOMS = {
    '1f': [
      [0, 0, 30, 24, 'GARAGE', 'garage'],
      [30, 0, 13, 11, 'DINING', 'dining room'],
      [43, 0, 20, 11, 'LIVING ROOM', 'living room'],
      [30, 13, 14, 11, 'KITCHEN', 'kitchen'],
      [49, 13, 14, 11, 'GUEST RM', 'guest room'],
      [44, 16.5, 5, 7.5, 'BATH', 'bath'],
      [43, 11, 20, 2, 'HALL', 'downstairs hallway'],
      [44, 3, 4, 8, 'STAIRS', 'stairs']
    ],
    '2f': [
      [31, 2, 15, 20, "BEDROOM 2", "bedroom 2"],
      [48, 2, 14, 20, 'MASTER', 'master bedroom'],
      [44, 16, 7, 8, 'BATH', 'bath'],
      [45, 11, 6, 5, 'U.HALL', 'upstairs hallway'],
      [45.5, 7, 4, 4, 'STAIRS', 'stairs']
    ],
    'b': [
      [30, 0, 33, 24, 'BASEMENT', 'basement']
    ]
  };
  var BSMT_ITEMS = [[34, 4, 'SUMP'], [34, 9.5, 'DEHUM'], [58, 9.5, 'ENERGY'], [58, 18, 'WASHER'], [46, 12, 'STAIRS']];
  var FLOOR_Z = { '1f': [0.4, 8.6], '2f': [9.0, 13.8], 'b': [-7, -0.6] };

  function roomBox(L, LBL, x0, y0, w, d, z0, z1, name, state) {
    var x1 = x0 + w, y1 = y0 + d, occ = state !== 'off';
    var mm = state === 'mmwave';
    // dom → mint; mmwave (active sensor) → punchy aqua-green, brighter than a
    // bare area flag so live detection reads at a glance; plain occ → cyan
    var ff = state === 'dom' ? 'rgba(0,245,160,0.15)' : mm ? 'rgba(30,255,180,0.22)' : occ ? 'rgba(244,184,96,0.13)' : 'rgba(244,184,96,0.035)';
    var ss = state === 'dom' ? 'rgba(130,255,205,0.9)' : mm ? 'rgba(70,255,195,1)' : occ ? 'rgba(244,184,96,0.62)' : 'rgba(244,184,96,0.24)';
    var sw = mm ? 1.3 : occ ? 1.0 : 0.6;
    var wf = state === 'dom' ? 'rgba(0,245,160,0.06)' : mm ? 'rgba(20,255,170,0.1)' : occ ? 'rgba(244,184,96,0.05)' : 'rgba(244,184,96,0.018)';
    F(L, [[x0,y0,z0],[x1,y0,z0],[x1,y1,z0],[x0,y1,z0]], ff, ss, sw * 0.7);            // floor
    F(L, [[x0,y0,z0],[x1,y0,z0],[x1,y0,z1],[x0,y0,z1]], wf, ss, sw * 0.5);
    F(L, [[x0,y1,z0],[x1,y1,z0],[x1,y1,z1],[x0,y1,z1]], wf, ss, sw * 0.5);
    F(L, [[x0,y0,z0],[x0,y1,z0],[x0,y1,z1],[x0,y0,z1]], wf, ss, sw * 0.5);
    F(L, [[x1,y0,z0],[x1,y1,z0],[x1,y1,z1],[x1,y0,z1]], wf, ss, sw * 0.5);
    LBL.push({ x: (x0 + x1) / 2, y: (y0 + y1) / 2, z: z0 + 0.2, t: name, st: state, big: w > 14 });
    if (occ) LBL.push({ x: (x0 + x1) / 2, y: (y0 + y1) / 2, z: z1 - 0.5, st: state, dot: true });
  }

  // Extrude a room from its actual polygon (3b-1): floor = the polygon, one wall
  // quad per edge. Same occupancy styling as roomBox.
  function roomPrism(L, LBL, pts, z0, z1, name, state) {
    var occ = state !== 'off', mm = state === 'mmwave';
    var ff = state === 'dom' ? 'rgba(0,245,160,0.15)' : mm ? 'rgba(30,255,180,0.22)' : occ ? 'rgba(244,184,96,0.13)' : 'rgba(244,184,96,0.035)';
    var ss = state === 'dom' ? 'rgba(130,255,205,0.9)' : mm ? 'rgba(70,255,195,1)' : occ ? 'rgba(244,184,96,0.62)' : 'rgba(244,184,96,0.24)';
    var sw = mm ? 1.3 : occ ? 1.0 : 0.6;
    var wf = state === 'dom' ? 'rgba(0,245,160,0.06)' : mm ? 'rgba(20,255,170,0.1)' : occ ? 'rgba(244,184,96,0.05)' : 'rgba(244,184,96,0.018)';
    F(L, pts.map(function (p) { return [p[0], p[1], z0]; }), ff, ss, sw * 0.7);   // floor polygon
    for (var i = 0; i < pts.length; i++) {                                        // walls: one quad per edge
      var a = pts[i], b = pts[(i + 1) % pts.length];
      F(L, [[a[0], a[1], z0], [b[0], b[1], z0], [b[0], b[1], z1], [a[0], a[1], z1]], wf, ss, sw * 0.5);
    }
    var cx = 0, cy = 0, mnx = 1e9, mxx = -1e9;
    pts.forEach(function (p) { cx += p[0]; cy += p[1]; mnx = Math.min(mnx, p[0]); mxx = Math.max(mxx, p[0]); });
    cx /= pts.length; cy /= pts.length;
    LBL.push({ x: cx, y: cy, z: z0 + 0.2, t: name, st: state, big: (mxx - mnx) > 14 });
    if (occ) LBL.push({ x: cx, y: cy, z: z1 - 0.5, st: state, dot: true });
  }

  // interior door on an x=const wall (shown on the floor-plan views)
  function intDoorX(L, x, y0, y1, z0, z1, state) {
    if (state === 'open') {
      var w = y1 - y0, ang = 58 * Math.PI / 180;
      var fy = y0 + w * Math.cos(ang), fx = x + w * Math.sin(ang);   // swing into the kitchen (+x)
      F(L, [[x,y0,z0],[fx,fy,z0],[fx,fy,z1],[x,y0,z1]], C.doorOpen, C.doorOpenS, 0.8, { cls: 'door door-open' });
    } else {
      F(L, [[x,y0,z0],[x,y1,z0],[x,y1,z1],[x,y0,z1]], 'rgba(244,184,96,0.14)', C.doorS, 0.7, { cls: 'door' });
    }
  }
  // interior door on a y=const wall (e.g. the basement door in the rear foundation wall)
  function intDoorY(L, y, x0, x1, z0, z1, faceOut, hinge, state) {
    var n = faceOut == null ? -0.06 : faceOut, yy = y + n;
    if (state !== 'open') {
      F(L, [[x0,yy,z0],[x1,yy,z0],[x1,yy,z1],[x0,yy,z1]], 'rgba(244,184,96,0.14)', C.doorS, 0.7, { cls: 'door' });
      return;
    }
    var w = x1 - x0, ang = 58 * Math.PI / 180, dir = n >= 0 ? 1 : -1;
    var dx = w * Math.cos(ang), dy = dir * w * Math.sin(ang);
    var hx = hinge === 'right' ? x1 : x0, fx = hinge === 'right' ? x1 - dx : x0 + dx, fy = yy + dy;
    F(L, [[hx,yy,z0],[fx,fy,z0],[fx,fy,z1],[hx,yy,z1]], C.doorOpen, C.doorOpenS, 0.8, { cls: 'door door-open' });
  }

  function buildRooms(floor, lit, doors, L, LBL) {
    var stOf = function (n) { var s = lit[String(n).toLowerCase()]; return s === 'dom' ? 'dom' : s === 'mmwave' ? 'mmwave' : s ? 'on' : 'off'; };
    var dOf = function (k) { return doors && doors[k] === 'open' ? 'open' : 'closed'; };
    var z = FLOOR_Z[floor] || FLOOR_Z['1f'];
    (ROOMS[floor] || []).forEach(function (r) { roomBox(L, LBL, r[0], r[1], r[2], r[3], z[0], z[1], r[4], stOf(r[5])); });
    if (floor === '1f') intDoorX(L, XGH, 19.5, 22.5, z[0], z[0] + 6.5, dOf('kitchen_garage')); // kitchen ↔ garage
    if (floor === 'b') {
      intDoorY(L, D, 33.5, 38.5, z[0] + 0.3, z[1] - 0.1, -0.06, 'left', dOf('basement'));      // basement door (foot of the cellar stairs, inline w/ the bulkhead above)
      BSMT_ITEMS.forEach(function (it) { LBL.push({ x: it[0], y: it[1], z: z[1] - 0.3, t: it[2], st: 'off', small: true }); });
    }
  }

  function buildContext(L, floor) {
    var fe = 'rgba(244,184,96,0.13)';
    F(L, [[XG0,0,0],[XHE,0,0],[XHE,D,0],[XG0,D,0]], 'none', fe, 0.5);   // footprint
    F(L, [[XGH,0,0],[XGH,D,0]], 'none', fe, 0.4);                       // garage/house split
    if (floor === '2f') {
      var rw = 'rgba(244,184,96,0.10)';
      F(L, [[XGH,-OVH,WALL],[XHE,-OVH,WALL],[XHE,RY,RIDGE],[XGH,RY,RIDGE]], 'none', rw, 0.4);
      F(L, [[XGH,D+OVH,WALL],[XHE,D+OVH,WALL],[XHE,RY,RIDGE],[XGH,RY,RIDGE]], 'none', rw, 0.4);
      F(L, [[XGH,RY,RIDGE],[XHE,RY,RIDGE]], 'none', 'rgba(244,184,96,0.16)', 0.5);
    }
  }

  // ---------- data-driven build: geometry from the editor's rooms (feet) (v7.101.28) ----------
  var DEFAULT_CENTER = [(XG0 + XHE) / 2, RY, WALL * 0.5];
  function _planZ(fk) { var A = { bsmt: 'b', basement: 'b' }; return FLOOR_Z[fk] || FLOOR_Z[A[fk]] || FLOOR_Z['1f']; }
  function _planFloorKey(plan, floor) { if (plan[floor]) return floor; var A = { b: 'bsmt', bsmt: 'b' }; return plan[A[floor]] ? A[floor] : floor; }
  function extWalls(L, x0, y0, x1, y1, z0, z1) {
    F(L, [[x0,y0,z0],[x1,y0,z0],[x1,y0,z1],[x0,y0,z1]], C.wallF,  C.wallS,   0.7); // front  (y=y0)
    F(L, [[x0,y1,z0],[x1,y1,z0],[x1,y1,z1],[x0,y1,z1]], C.wallDk, C.wallSdk, 0.7); // back   (y=y1)
    F(L, [[x0,y0,z0],[x0,y1,z0],[x0,y1,z1],[x0,y0,z1]], C.wallDk, C.wallSdk, 0.7); // left   (x=x0)
    F(L, [[x1,y0,z0],[x1,y1,z0],[x1,y1,z1],[x1,y0,z1]], C.wallF,  C.wallS,   0.7); // right  (x=x1)
  }
  function _roomCorners(r) {
    if (r.points && r.points.length >= 3) return r.points;
    var x = r.x || 0, y = r.y || 0, w = r.w || 0, d = r.d || 0;
    return [[x, y], [x + w, y], [x + w, y + d], [x, y + d]];
  }
  function _ptInPoly(px, py, pts) {
    var inside = false;
    for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      var xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
      if (((yi > py) !== (yj > py)) && (px < (xj - xi) * (py - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }
  // Exterior walls from the real outline (3b-2a): an edge is an outside wall unless the
  // point just past it lands inside another enclosed room (i.e. it's a shared wall).
  // Decompose an orthogonal footprint (axis-aligned room bboxes) into rectangular masses.
  // One rectangle for a rectangular footprint; several for an L/T. (v7.101.28)
  // x-intervals of an orthogonal room polygon at scanline y (bay-aware decomposition, v7.101.28).
  function _polyScanX(pts, ym) {
    var xs = [];
    for (var i = 0; i < pts.length; i++) {
      var a = pts[i], b = pts[(i + 1) % pts.length];
      if ((a[1] <= ym && b[1] > ym) || (b[1] <= ym && a[1] > ym)) xs.push(a[0] + (ym - a[1]) / (b[1] - a[1]) * (b[0] - a[0]));
    }
    xs.sort(function (p, q) { return p - q; });
    var ivs = [];
    for (var k = 0; k + 1 < xs.length; k += 2) ivs.push([xs[k], xs[k + 1]]);
    return ivs;
  }
  function _footprintMasses(rooms) {
    // Stairs stay IN the enclosed footprint here (only outdoor/door are excluded):
    // a stairwell is an open floor void, not a walled room, but it still sits inside
    // the building envelope. Dropping it (as this used to) left a notch wherever no
    // other room's rectangle covered that same slice, splitting a single rectangular
    // house into several offset gable masses -- stacked, jagged rooflines instead of
    // one clean ridge (caught live: a real house with a fully-interior stairwell).
    var encl = (rooms || []).filter(function (r) { return r.type !== 'outdoor' && r.type !== 'door'; });
    if (!encl.length) return [];
    var ys = [];
    encl.forEach(function (r) { _roomCorners(r).forEach(function (p) { ys.push(p[1]); }); });
    ys = ys.sort(function (a, b) { return a - b; }).filter(function (v, i, a) { return i === 0 || Math.abs(v - a[i - 1]) > 0.01; });
    var rects = [];
    for (var i = 0; i < ys.length - 1; i++) {
      var y0 = ys[i], y1 = ys[i + 1], ym = (y0 + y1) / 2, ivs = [];
      encl.forEach(function (r) { _polyScanX(_roomCorners(r), ym).forEach(function (iv) { ivs.push(iv); }); });
      ivs.sort(function (a, b) { return a[0] - b[0]; });
      var merged = [];
      ivs.forEach(function (iv) { var last = merged[merged.length - 1]; if (last && iv[0] <= last[1] + 0.01) last[1] = Math.max(last[1], iv[1]); else merged.push([iv[0], iv[1]]); });
      merged.forEach(function (iv) { rects.push({ x0: iv[0], y0: y0, x1: iv[1], y1: y1 }); });
    }
    var changed = true;
    while (changed) {
      changed = false;
      for (var a = 0; a < rects.length; a++) {
        if (!rects[a]) continue;
        for (var b = a + 1; b < rects.length; b++) {
          if (!rects[b]) continue;
          var A = rects[a], B = rects[b];
          if (Math.abs(A.x0 - B.x0) < 0.01 && Math.abs(A.x1 - B.x1) < 0.01 && (Math.abs(A.y1 - B.y0) < 0.01 || Math.abs(B.y1 - A.y0) < 0.01)) {
            A.y0 = Math.min(A.y0, B.y0); A.y1 = Math.max(A.y1, B.y1); rects[b] = null; changed = true;
          }
        }
      }
      rects = rects.filter(Boolean);
    }
    return rects;
  }
  function extWallsPoly(L, rooms, z0, z1) {
    // Same reasoning as _footprintMasses above: a stairs room still counts toward
    // the enclosed footprint for exterior-wall tracing, or an interior stairwell
    // with nothing else covering its slice reads as a notch cut into the outline.
    var encl = (rooms || []).filter(function (r) { return r.type !== 'outdoor' && r.type !== 'door'; });
    var corners = [];
    encl.forEach(function (r) { _roomCorners(r).forEach(function (c) { corners.push(c); }); });
    function inAny(px, py, skip) { return encl.some(function (rr) { return rr !== skip && _ptInPoly(px, py, _roomCorners(rr)); }); }
    var segs = [];
    encl.forEach(function (r) {
      var pts = _roomCorners(r), cx = 0, cy = 0;
      pts.forEach(function (p) { cx += p[0]; cy += p[1]; }); cx /= pts.length; cy /= pts.length;
      for (var i = 0; i < pts.length; i++) {
        var a = pts[i], b = pts[(i + 1) % pts.length];
        var dx = b[0] - a[0], dy = b[1] - a[1], L2 = dx * dx + dy * dy;
        if (L2 < 1e-6) continue;
        var ts = [0, 1];
        corners.forEach(function (c) {
          var t = ((c[0] - a[0]) * dx + (c[1] - a[1]) * dy) / L2;
          if (t <= 0.001 || t >= 0.999) return;
          if (Math.hypot(c[0] - (a[0] + dx * t), c[1] - (a[1] + dy * t)) < 0.05) ts.push(t);
        });
        ts.sort(function (p, q) { return p - q; });
        var nx = -dy, ny = dx, len = Math.hypot(nx, ny) || 1; nx /= len; ny /= len;
        var mmx = (a[0] + b[0]) / 2, mmy = (a[1] + b[1]) / 2;
        if ((mmx + nx - cx) * (mmx + nx - cx) + (mmy + ny - cy) * (mmy + ny - cy) < (mmx - nx - cx) * (mmx - nx - cx) + (mmy - ny - cy) * (mmy - ny - cy)) { nx = -nx; ny = -ny; }
        var front = ny < -0.3;
        for (var k = 0; k < ts.length - 1; k++) {
          var t0 = ts[k], t1 = ts[k + 1]; if (t1 - t0 < 0.001) continue;
          var tm = (t0 + t1) / 2, smx = a[0] + dx * tm, smy = a[1] + dy * tm;
          if (inAny(smx + nx * 1.5, smy + ny * 1.5, r)) continue;
          segs.push({ a: [a[0] + dx * t0, a[1] + dy * t0], b: [a[0] + dx * t1, a[1] + dy * t1], front: front });
        }
      }
    });
    // Merge collinear + adjacent same-shade segments into continuous runs so each wall is
    // ONE stroked quad, not one per room edge (that seam was the "break") (v7.101.28).
    function nr(p, q) { return Math.abs(p[0] - q[0]) < 0.06 && Math.abs(p[1] - q[1]) < 0.06; }
    function coll(a, b, c) { var LL = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1; return Math.abs(((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / LL) < 0.06; }
    var used = new Array(segs.length).fill(false);
    for (var s = 0; s < segs.length; s++) {
      if (used[s]) continue;
      used[s] = true;
      var A = segs[s].a, B = segs[s].b, fr = segs[s].front, grew = true;
      while (grew) {
        grew = false;
        for (var j = 0; j < segs.length; j++) {
          if (used[j] || segs[j].front !== fr) continue;
          var ta = segs[j].a, tb = segs[j].b;
          if (!coll(A, B, ta) || !coll(A, B, tb)) continue;
          if (nr(tb, A)) { A = ta; used[j] = true; grew = true; }
          else if (nr(ta, A)) { A = tb; used[j] = true; grew = true; }
          else if (nr(ta, B)) { B = tb; used[j] = true; grew = true; }
          else if (nr(tb, B)) { B = ta; used[j] = true; grew = true; }
        }
      }
      F(L, [[A[0], A[1], z0], [B[0], B[1], z0], [B[0], B[1], z1], [A[0], A[1], z1]], fr ? C.wallF : C.wallDk, fr ? C.wallS : C.wallSdk, 0.7);
    }
  }
  // ----- generalized roofs over the derived footprint (v7.101.28) -----
  function _oneFloorTop() { return FLOOR_Z['1f'][1]; }                                  // 1st-floor eave
  function _roofRise(spanShort) { var p = SPEC.pitch != null ? SPEC.pitch : 1; return (spanShort / 2) * 0.9 * Math.max(p, 0.08); }

  function gableRoofOver(L, GL, x0, y0, x1, y1, zE, minRise, pitchOv) {
    var W = x1 - x0, Dd = y1 - y0, ov = OVH, alongX = W >= Dd, span = alongX ? Dd : W;
    var p = pitchOv != null ? pitchOv : (SPEC.pitch != null ? SPEC.pitch : 1);
    var rise = Math.max((span / 2) * 0.9 * Math.max(p, 0.08), minRise || 0), zR = zE + rise;
    if (alongX) {
      var yC = (y0 + y1) / 2;
      roofPlane(L, x0 - ov, x1 + ov, y0 - ov, zE, yC, zR, C.roofF, C.roofS, 0.85);                       // front
      F(L, [[x0-ov,y1+ov,zE],[x1+ov,y1+ov,zE],[x1+ov,yC,zR],[x0-ov,yC,zR]], C.roofDk, C.roofSdk, 0.7);   // back
      gableEnd(L, x0, y0, y1, zE, yC, zR, C.gableF, C.gableS, 0.8);
      gableEnd(L, x1, y0, y1, zE, yC, zR, C.gableF, C.gableS, 0.8);
      return { axis: 'x', front: y0, back: y1, eave: zE, ridge: zR, mid: yC, a: x0, b: x1 };
    }
    var xC = (x0 + x1) / 2;
    F(L, [[x0-ov,y0-ov,zE],[x0-ov,y1+ov,zE],[xC,y1+ov,zR],[xC,y0-ov,zR]], C.roofF, C.roofS, 0.85);
    F(L, [[x1+ov,y0-ov,zE],[x1+ov,y1+ov,zE],[xC,y1+ov,zR],[xC,y0-ov,zR]], C.roofDk, C.roofSdk, 0.7);
    F(L, [[x0,y0,zE],[x1,y0,zE],[xC,y0,zR]], C.gableF, C.gableS, 0.8);
    F(L, [[x0,y1,zE],[x1,y1,zE],[xC,y1,zR]], C.gableF, C.gableS, 0.8);
    return { axis: 'y', eave: zE, ridge: zR };
  }

  // ---------- gambrel (Dutch Colonial "barn") gable end: wall + steep lower + shallow upper ----------
  function gambrelEnd(L, x, yA, yB, zWall, yKa, yKb, zK, yPk, zPk, f, s) {
    F(L, [[x,yA,0],[x,yB,0],[x,yB,zWall],[x,yKb,zK],[x,yPk,zPk],[x,yKa,zK],[x,yA,zWall]], f, s, 0.8);
  }
  function gambrelEndY(L, y, xA, xB, zWall, xKa, xKb, zK, xPk, zPk, f, s) {
    F(L, [[xA,y,0],[xB,y,0],[xB,y,zWall],[xKb,y,zK],[xPk,y,zPk],[xKa,y,zK],[xA,y,zWall]], f, s, 0.8);
  }
  // gambrel roof: two slopes per face (steep lower ~60deg, shallow upper). Knuckle at KH of the
  // half-span in / KV of the rise up — 2nd-floor windows live in the steep lower slope, high off
  // the wall. Returns a gable-compatible object plus knuckle data so dormers ride the lower slope.
  function gambrelRoofOver(L, GL, x0, y0, x1, y1, zE, minRise, pitchOv) {
    var W = x1 - x0, Dd = y1 - y0, ov = OVH, alongX = W >= Dd, span = alongX ? Dd : W;
    var p = pitchOv != null ? pitchOv : (SPEC.pitch != null ? SPEC.pitch : 1);
    var rise = Math.max((span / 2) * 0.9 * Math.max(p, 0.08), minRise || 0), zR = zE + rise;
    var KH = 0.42, KV = 0.64, zK = zE + rise * KV;
    if (alongX) {
      var yC = (y0 + y1) / 2, yKa = y0 + (yC - y0) * KH, yKb = y1 - (y1 - yC) * KH;
      roofPlane(L, x0 - ov, x1 + ov, y0 - ov, zE, yKa, zK, C.roofF, C.roofS, 0.85);                 // front lower (steep)
      roofPlane(L, x0 - ov, x1 + ov, yKa, zK, yC, zR, C.roofF, C.roofS, 0.85);                       // front upper (shallow)
      F(L, [[x0-ov,y1+ov,zE],[x1+ov,y1+ov,zE],[x1+ov,yKb,zK],[x0-ov,yKb,zK]], C.roofDk, C.roofSdk, 0.7);   // back lower
      F(L, [[x0-ov,yKb,zK],[x1+ov,yKb,zK],[x1+ov,yC,zR],[x0-ov,yC,zR]], C.roofDk, C.roofSdk, 0.7);          // back upper
      gambrelEnd(L, x0, y0, y1, zE, yKa, yKb, zK, yC, zR, C.gableF, C.gableS);
      gambrelEnd(L, x1, y0, y1, zE, yKa, yKb, zK, yC, zR, C.gableF, C.gableS);
      return { axis: 'x', front: y0, back: y1, eave: zE, ridge: zR, mid: yC, a: x0, b: x1, knuckleZ: zK, knuckleFront: yKa, knuckleBack: yKb };
    }
    var xC = (x0 + x1) / 2, xKa = x0 + (xC - x0) * KH, xKb = x1 - (x1 - xC) * KH;
    F(L, [[x0-ov,y0-ov,zE],[x0-ov,y1+ov,zE],[xKa,y1+ov,zK],[xKa,y0-ov,zK]], C.roofF, C.roofS, 0.85);       // left lower
    F(L, [[xKa,y0-ov,zK],[xKa,y1+ov,zK],[xC,y1+ov,zR],[xC,y0-ov,zR]], C.roofF, C.roofS, 0.85);             // left upper
    F(L, [[x1+ov,y0-ov,zE],[x1+ov,y1+ov,zE],[xKb,y1+ov,zK],[xKb,y0-ov,zK]], C.roofDk, C.roofSdk, 0.7);     // right lower
    F(L, [[xKb,y0-ov,zK],[xKb,y1+ov,zK],[xC,y1+ov,zR],[xC,y0-ov,zR]], C.roofDk, C.roofSdk, 0.7);           // right upper
    gambrelEndY(L, y0, x0, x1, zE, xKa, xKb, zK, xC, zR, C.gableF, C.gableS);
    gambrelEndY(L, y1, x0, x1, zE, xKa, xKb, zK, xC, zR, C.gableF, C.gableS);
    return { axis: 'y', eave: zE, ridge: zR };
  }

  function hipRoofOver(L, GL, x0, y0, x1, y1, zE, minRise) {
    var W = x1 - x0, Dd = y1 - y0, ov = OVH, span = Math.min(W, Dd);
    var rise = Math.max(_roofRise(span), minRise || 0), zR = zE + rise, yC = (y0 + y1) / 2, inset = span / 2;
    var a = x0 + inset, b = x1 - inset; if (b < a) { a = b = (x0 + x1) / 2; }
    F(L, [[x0-ov,y0-ov,zE],[x1+ov,y0-ov,zE],[b,yC,zR],[a,yC,zR]], C.roofF, C.roofS, 0.85);   // front
    F(L, [[x0-ov,y1+ov,zE],[x1+ov,y1+ov,zE],[b,yC,zR],[a,yC,zR]], C.roofDk, C.roofSdk, 0.7); // back
    F(L, [[x0-ov,y0-ov,zE],[x0-ov,y1+ov,zE],[a,yC,zR]], C.roofF, C.roofS, 0.8);              // left hip
    F(L, [[x1+ov,y0-ov,zE],[x1+ov,y1+ov,zE],[b,yC,zR]], C.roofDk, C.roofSdk, 0.7);           // right hip
    return { axis: 'x', front: y0, eave: zE, ridge: zR, mid: yC, a: a, b: b };
  }

  function flatRoofOver(L, GL, x0, y0, x1, y1, zTop) {
    var ov = OVH * 0.5;
    F(L, [[x0-ov,y0-ov,zTop],[x1+ov,y0-ov,zTop],[x1+ov,y1+ov,zTop],[x0-ov,y1+ov,zTop]], C.roofF, C.roofS, 0.85);
    extWalls(L, x0 - ov, y0 - ov, x1 + ov, y1 + ov, zTop, zTop + 1.0);   // parapet
    return { axis: 'flat', eave: zTop, ridge: zTop + 1.0 };
  }

  function dormersOn(L, GL, roof, count, wSt, rear, positions, states) {
    wSt = wSt || 'off';
    var nD = positions && positions.length ? positions.length : count;
    if (!nD || nD < 1 || !roof || roof.axis !== 'x') return;
    var a = roof.a, b = roof.b, eave = roof.eave, ridge = roof.ridge, mid = roof.mid;
    if (roof.knuckleZ != null) { ridge = roof.knuckleZ; mid = rear ? roof.knuckleBack : roof.knuckleFront; }  // gambrel: dormers ride the steep lower slope
    var edge = rear ? (roof.back != null ? roof.back : (2 * mid - roof.front)) : roof.front;
    var sgn = rear ? -1 : 1;
    var slope = (mid - edge) !== 0 ? (ridge - eave) / Math.abs(mid - edge) : 0;
    var dw = 5, proj = 5.5, i;
    for (i = 0; i < nD; i++) {
      var frac = positions && positions.length ? positions[i] : (i + 1) / (nD + 1);
      var cx = a + (b - a) * frac;
      var yFace = edge + sgn * 1.4, yBack = edge + sgn * proj;
      var zBack = eave + Math.abs(yBack - edge) * slope;
      var zSill = eave + 0.9, zHead = zSill + 3.4, zPk = zHead + 1.5, out = sgn * -0.06;
      F(L, [[cx-dw/2,yFace,zSill],[cx+dw/2,yFace,zSill],[cx+dw/2,yFace,zHead],[cx-dw/2,yFace,zHead]], 'rgba(24,19,14,0.97)', C.wallS, 0.7);
      F(L, [[cx-dw/2,yFace,zHead],[cx+dw/2,yFace,zHead],[cx,yFace,zPk]], 'rgba(24,19,14,0.97)', C.wallS, 0.7);
      F(L, [[cx-dw/2,yFace,zSill],[cx-dw/2,yFace,zHead],[cx-dw/2,yBack,zBack]], 'rgba(17,13,10,0.94)', C.roofSdk, 0.55);   // cheek L
      F(L, [[cx+dw/2,yFace,zSill],[cx+dw/2,yFace,zHead],[cx+dw/2,yBack,zBack]], 'rgba(17,13,10,0.94)', C.roofSdk, 0.55);   // cheek R
      F(L, [[cx-dw/2,yFace,zHead],[cx,yFace,zPk],[cx,yBack,zBack],[cx-dw/2,yBack,zBack]], C.roofF, C.roofS, 0.6);         // gable slope L
      F(L, [[cx+dw/2,yFace,zHead],[cx,yFace,zPk],[cx,yBack,zBack],[cx+dw/2,yBack,zBack]], C.roofF, C.roofS, 0.6);         // gable slope R
      var _dst = (states && states[i] != null) ? states[i] : wSt;
      winY(L, GL, yFace, cx - 1.7, cx + 1.7, zSill + 0.3, zHead - 0.3, _dst, true, out);
    }
  }

  function garageDoorsOn(L, GL, x0, x1, yF, state, baysState) {
    var bays = SPEC.garageBays > 0 ? SPEC.garageBays : 3, gap = 1.8;
    var W = x1 - x0, dw = (W - gap * (bays + 1)) / bays, z0 = 0.4, z1 = 7.2, n = -0.06, i, gx, k;
    if (dw <= 1) { gap = 0.6; dw = (W - gap * (bays + 1)) / bays; }
    if (dw <= 0) return;
    var litD = state === 'on' || state === 'dom';
    var f = litD ? C.doorOn : C.doorOff, s = litD ? C.glassOn : C.doorS;
    for (i = 0; i < bays; i++) {
      gx = x0 + gap + i * (dw + gap);
      var bayOpen = baysState && baysState[i] && baysState[i].open;
      if (bayOpen) {
        // door rolled up: amber opening + a raised panel at the top
        F(L, [[gx,yF+n-0.35,z0],[gx+dw,yF+n-0.35,z0],[gx+dw,yF+n-0.35,z1-1.4],[gx,yF+n-0.35,z1-1.4]], 'rgba(255,170,40,0.12)', C.doorOpenS, 0.6, { cls: 'door-open' });
        F(L, [[gx,yF+n,z1-1.4],[gx+dw,yF+n,z1-1.4],[gx+dw,yF+n,z1],[gx,yF+n,z1]], C.doorOpen, C.doorOpenS, 1.0, { cls: 'gdoor door-open' });
        if (GL) GL.push({ p: [[gx-1.2,yF+n,z0],[gx+dw+1.2,yF+n,z0],[gx+dw+1.2,yF+n,z1+1.2],[gx-1.2,yF+n,z1+1.2]], f: C.doorOpenGlow });
      } else {
        F(L, [[gx,yF+n,z0],[gx+dw,yF+n,z0],[gx+dw,yF+n,z1],[gx,yF+n,z1]], f, s, 1.0, { cls: 'gdoor' });
        for (k = 1; k < 4; k++) { var zz = z0 + (z1 - z0) * k / 4; F(L, [[gx,yF+n,zz],[gx+dw,yF+n,zz]], 'none', s, 0.45); }
        if (litD && GL) GL.push({ p: [[gx-1.2,yF+n,z0],[gx+dw+1.2,yF+n,z0],[gx+dw+1.2,yF+n,z1+1.2],[gx-1.2,yF+n,z1+1.2]], f: C.glowOn });
      }
    }
  }

  function chimneyAt(L, xEdge, yc, zTop, outward) {
    var ya = yc - 1.8, yb = yc + 1.8, x0 = xEdge, x1 = xEdge + outward * 2.2;
    F(L, [[x1,ya,0],[x1,yb,0],[x1,yb,zTop],[x1,ya,zTop]], C.chimF, C.chimS, 0.7);
    F(L, [[x0,ya,0],[x1,ya,0],[x1,ya,zTop],[x0,ya,zTop]], 'rgba(15,12,9,0.97)', C.chimS, 0.6);
    F(L, [[x0,yb,0],[x1,yb,0],[x1,yb,zTop],[x0,yb,zTop]], 'rgba(15,12,9,0.97)', C.dim, 0.5);
    F(L, [[x0,ya,zTop],[x1,ya,zTop],[x1,yb,zTop],[x0,yb,zTop]], 'rgba(244,184,96,0.08)', C.chimS, 0.5);
  }

  // A Bilco-style bulkhead cellar door: a sloped wedge against the wall, high at
  // the house and low at the outer edge, split into two door panels (v7.101.28).
  function bulkheadDoor(L, GL, wall, cx, cy, w, open) {
    var depth = Math.max(w, 5.5), zHigh = 3.2, zLow = 0.2;   // ~28\u00b0 slope, low enough to clear windows
    var f = open ? C.doorOpen : C.doorOff, s = open ? C.doorOpenS : C.doorS, cls = open ? 'cellar-door door-open' : 'cellar-door';
    var dk = 'rgba(17,13,10,0.94)';
    if (wall === 'front' || wall === 'back') {
      var sgn = wall === 'front' ? -1 : 1, yw = cy, yo = cy + sgn * depth;
      F(L, [[cx-w/2,yw,zHigh],[cx,yw,zHigh],[cx,yo,zLow],[cx-w/2,yo,zLow]], f, s, 0.9, { cls: cls });      // left panel
      F(L, [[cx,yw,zHigh],[cx+w/2,yw,zHigh],[cx+w/2,yo,zLow],[cx,yo,zLow]], f, s, 0.9, { cls: cls });      // right panel
      F(L, [[cx-w/2,yw,0],[cx-w/2,yw,zHigh],[cx-w/2,yo,zLow],[cx-w/2,yo,0]], dk, s, 0.6);                  // left side
      F(L, [[cx+w/2,yw,0],[cx+w/2,yw,zHigh],[cx+w/2,yo,zLow],[cx+w/2,yo,0]], dk, s, 0.6);                  // right side
      F(L, [[cx-w/2,yo,0],[cx+w/2,yo,0],[cx+w/2,yo,zLow],[cx-w/2,yo,zLow]], dk, s, 0.6);                   // outer end
    } else {
      var sgnx = wall === 'left' ? -1 : 1, xw = cx, xo = cx + sgnx * depth;
      F(L, [[xw,cy-w/2,zHigh],[xw,cy,zHigh],[xo,cy,zLow],[xo,cy-w/2,zLow]], f, s, 0.9, { cls: cls });
      F(L, [[xw,cy,zHigh],[xw,cy+w/2,zHigh],[xo,cy+w/2,zLow],[xo,cy,zLow]], f, s, 0.9, { cls: cls });
      F(L, [[xw,cy-w/2,0],[xw,cy-w/2,zHigh],[xo,cy-w/2,zLow],[xo,cy-w/2,0]], dk, s, 0.6);
      F(L, [[xw,cy+w/2,0],[xw,cy+w/2,zHigh],[xo,cy+w/2,zLow],[xo,cy+w/2,0]], dk, s, 0.6);
      F(L, [[xo,cy-w/2,0],[xo,cy+w/2,0],[xo,cy+w/2,zLow],[xo,cy-w/2,zLow]], dk, s, 0.6);
    }
    if (open && GL) GL.push({ p: [[cx-w/2,cy,zHigh],[cx+w/2,cy,zHigh],[cx+w/2,cy,zHigh+1.5],[cx-w/2,cy,zHigh+1.5]], f: C.doorOpenGlow });
  }

  // A door on a footprint wall, open (swung) or closed, for placed exterior/cellar doors (v7.101.28).
  // A cased opening (open doorway / pass-through): a doorway frame with no leaf —
  // you see straight through it. Always open; marks a visual + flow connection
  // between the two rooms the wall separates (v7.101.28).
  function casedOnWall(L, GL, wall, cx, cy, w, z0, z1) {
    var horiz = (wall === 'front' || wall === 'back');
    var f = 'rgba(120,100,70,0.12)', s = 'rgba(210,185,140,0.8)';
    if (horiz) {
      var yy = cy + (wall === 'front' ? -0.06 : 0.06);
      F(L, [[cx-w/2,yy,z0],[cx+w/2,yy,z0],[cx+w/2,yy,z1],[cx-w/2,yy,z1]], f, s, 0.7, { cls: 'cased' });
    } else {
      var xx = cx + (wall === 'left' ? -0.06 : 0.06);
      F(L, [[xx,cy-w/2,z0],[xx,cy+w/2,z0],[xx,cy+w/2,z1],[xx,cy-w/2,z1]], f, s, 0.7, { cls: 'cased' });
    }
  }
  function doorOnWall(L, GL, wall, cx, cy, w, z0, z1, open) {
    var horiz = (wall === 'front' || wall === 'back');
    var f = open ? C.doorOpen : C.doorOff, s = open ? C.doorOpenS : C.doorS, cls = open ? 'door door-open' : 'door';
    var ang = 55 * Math.PI / 180;
    if (horiz) {
      var n = wall === 'front' ? -0.06 : 0.06, yy = cy + n;
      if (!open) { F(L, [[cx-w/2,yy,z0],[cx+w/2,yy,z0],[cx+w/2,yy,z1],[cx-w/2,yy,z1]], f, s, 0.9, { cls: cls }); }
      else {
        var dir = wall === 'front' ? -1 : 1, dx = w * Math.cos(ang), dy = dir * w * Math.sin(ang);
        F(L, [[cx-w/2,yy,z0],[cx-w/2+dx,yy+dy,z0],[cx-w/2+dx,yy+dy,z1],[cx-w/2,yy,z1]], f, s, 0.9, { cls: cls });
        if (GL) GL.push({ p: [[cx-w/2-1,yy,z0],[cx+w/2+1,yy,z0],[cx+w/2+1,yy+dy,z1+1],[cx-w/2-1,yy+dy,z1+1]], f: C.doorOpenGlow });
      }
    } else {
      var nx = wall === 'left' ? -0.06 : 0.06, xx = cx + nx;
      if (!open) { F(L, [[xx,cy-w/2,z0],[xx,cy+w/2,z0],[xx,cy+w/2,z1],[xx,cy-w/2,z1]], f, s, 0.9, { cls: cls }); }
      else {
        var dir2 = wall === 'left' ? -1 : 1, dx2 = dir2 * w * Math.sin(ang), dy2 = w * Math.cos(ang);
        F(L, [[xx,cy-w/2,z0],[xx+dx2,cy-w/2+dy2,z0],[xx+dx2,cy-w/2+dy2,z1],[xx,cy-w/2,z1]], f, s, 0.9, { cls: cls });
        if (GL) GL.push({ p: [[xx,cy-w/2-1,z0],[xx+dx2,cy+w/2+1,z0],[xx+dx2,cy+w/2+1,z1+1],[xx,cy-w/2-1,z1+1]], f: C.doorOpenGlow });
      }
    }
  }

  // Clean exterior shell for the whole-house view — presence shows as lit
  // windows, exactly like the original approved model, but built from the
  // editor's footprint + rooms + home type (v7.101.28).
  function buildExteriorFromPlan(opts, plan, minx, miny, maxx, maxy, ztop) {
    var lit = opts.lit || {}, L = [], GL = [], LBL = [];
    var wOf = function (n) { var s = lit[String(n).toLowerCase()]; return s === 'dom' ? 'dom' : s ? 'on' : 'off'; };
    var stories = SPEC.stories != null ? SPEC.stories : 1.5;
    var has2f = !!(plan['2f'] && plan['2f'].length);
    var rt = SPEC.roof || 'gable';
    var eave = (rt !== 'flat' && stories < 2 && has2f) ? _oneFloorTop() : ztop;
    var minRise = (rt !== 'flat' && stories < 2 && has2f) ? (ztop - eave) + 2.5 : 0;

    if ((plan['1f'] || []).length) extWallsPoly(L, plan['1f'], 0, eave);
    else extWalls(L, minx, miny, maxx, maxy, 0, eave);

    var TOL = 1.5, EDGE = 3, z0 = 3, z1 = 7, garageRoom = null;
    (plan['1f'] || []).forEach(function (r) { if (String(r.name).toLowerCase().indexOf('garage') >= 0 && !garageRoom) garageRoom = r; });

    var winOcc = function (cx, cy) {
      var best = 'off';
      (plan['1f'] || []).forEach(function (r) {
        if (cx >= r.x - 1 && cx <= r.x + r.w + 1 && cy >= r.y - 1 && cy <= r.y + r.d + 1) {
          var s = wOf(r.name); if (s === 'dom') best = 'dom'; else if (s === 'on' && best !== 'dom') best = 'on';
        }
      });
      return best;
    };
    var winOcc2f = function (cx, cy) {
      var best = 'off';
      (plan['2f'] || []).forEach(function (r) {
        if (cx >= r.x - 1 && cx <= r.x + r.w + 1 && cy >= r.y - 1 && cy <= r.y + r.d + 1) {
          var s = wOf(r.name); if (s === 'dom') best = 'dom'; else if (s === 'on' && best !== 'dom') best = 'on';
        }
      });
      return best;
    };
    var wallXY = function (wall, p) {
      if (wall === 'front') return [minx + p * (maxx - minx), miny];
      if (wall === 'back') return [minx + p * (maxx - minx), maxy];
      if (wall === 'left') return [minx, miny + p * (maxy - miny)];
      return [maxx, miny + p * (maxy - miny)];
    };
    var placed = ((opts.elements && opts.elements['1f']) || []).filter(function (e) { return e.type === 'window' || (e.type === 'door' && e.kind !== 'interior' && e.kind !== 'cased'); });

    if (placed.length) {
      // user-placed exterior openings — windows lit by the room they front, doors open/closed from their sensor
      placed.forEach(function (e) {
        var p = e.pos != null ? e.pos : 0.5, w = e.w || 4, horiz = (e.wall === 'front' || e.wall === 'back');
        var xy = wallXY(e.wall, p), cx = xy[0], cy = xy[1];
        if (e.type === 'window') {
          var st = winOcc(cx, cy);
          if (horiz) winY(L, GL, cy, cx - w / 2, cx + w / 2, z0, z1, st, true, e.wall === 'front' ? -0.06 : 0.06);
          else winX(L, GL, cx, cy - w / 2, cy + w / 2, z0, z1, st, true, e.wall === 'left' ? -0.06 : 0.06);
        } else if (e.kind === 'cellar') {
          bulkheadDoor(L, GL, e.wall, cx, cy, Math.max(w, 5), e.open);
        } else {
          doorOnWall(L, GL, e.wall, cx, cy, Math.max(w, 3), 0.4, 6.8, e.open);
        }
      });
    } else {
      // auto: a window on each room's exterior-facing wall (fallback when nothing placed)
      (plan['1f'] || []).forEach(function (r) {
        if (String(r.name).toLowerCase().indexOf('garage') >= 0) return;
        if (r.type && r.type !== 'room' && r.type !== 'bath') return;
        var x0r = r.x, x1r = r.x + r.w, y0r = r.y, y1r = r.y + r.d, st = wOf(r.name);
        if (x1r - x0r < 6 || y1r - y0r < 6) return;
        if (Math.abs(y0r - miny) < TOL) winY(L, GL, miny, x0r + EDGE, x1r - EDGE, z0, z1, st, true, -0.06);
        if (Math.abs(y1r - maxy) < TOL) winY(L, GL, maxy, x0r + EDGE, x1r - EDGE, z0, z1, st, true, 0.06);
        if (Math.abs(x0r - minx) < TOL) winX(L, GL, minx, y0r + EDGE, y1r - EDGE, z0, z1, st, true, -0.06);
        if (Math.abs(x1r - maxx) < TOL) winX(L, GL, maxx, y0r + EDGE, y1r - EDGE, z0, z1, st, true, 0.06);
      });
    }

    if (garageRoom) {
      var g = garageRoom;
      var gy = Math.abs(g.y - miny) < TOL ? miny : (Math.abs(g.y + g.d - maxy) < TOL ? maxy : g.y);
      garageDoorsOn(L, GL, g.x + 0.5, g.x + g.w - 0.5, gy, wOf(g.name), opts.garage);
    }

    var roof, gLow = ((rt === 'gable' || rt === 'gambrel') && garageRoom && garageRoom.w > 8);
    if (rt === 'flat') roof = flatRoofOver(L, GL, minx, miny, maxx, maxy, ztop);
    else if (rt === 'hip') roof = hipRoofOver(L, GL, minx, miny, maxx, maxy, eave, minRise);
    else if (gLow) {
      // House = non-garage 1f rooms. If they nearly fill their bbox it's a rectangle -> one
      // gable (un-roomed interior gaps don't fragment it); a real notch (<85%) -> per mass.
      var houseRooms = (plan['1f'] || []).filter(function (r) { return r !== garageRoom && r.type !== 'outdoor' && r.type !== 'door'; });
      var hb = null, harea = 0;
      houseRooms.forEach(function (r) { harea += r.w * r.d; if (!hb) hb = { x0: r.x, y0: r.y, x1: r.x + r.w, y1: r.y + r.d }; else { hb.x0 = Math.min(hb.x0, r.x); hb.y0 = Math.min(hb.y0, r.y); hb.x1 = Math.max(hb.x1, r.x + r.w); hb.y1 = Math.max(hb.y1, r.y + r.d); } });
      var hbArea = hb ? (hb.x1 - hb.x0) * (hb.y1 - hb.y0) : 0;
      var houseRect = !hb || hbArea <= 0 || (harea / hbArea) >= 0.85;
      if (!houseRect) {
        var hmasses = _footprintMasses(houseRooms);
        hmasses.sort(function (a, b) { return (b.x1 - b.x0) * (b.y1 - b.y0) - (a.x1 - a.x0) * (a.y1 - a.y0); });
        hmasses.forEach(function (m, mi) { var _r = (rt === 'gambrel') ? gambrelRoofOver(L, GL, m.x0, m.y0, m.x1, m.y1, eave, minRise) : gableRoofOver(L, GL, m.x0, m.y0, m.x1, m.y1, eave, minRise); if (mi === 0) roof = _r; });
      } else {
        roof = (rt === 'gambrel') ? gambrelRoofOver(L, GL, hb.x0, hb.y0, hb.x1, hb.y1, eave, minRise) : gableRoofOver(L, GL, hb.x0, hb.y0, hb.x1, hb.y1, eave, minRise);
      }
      // attached garage: low, shallow-pitch roof over its ACTUAL bbox (spans its real depth)
      gableRoofOver(L, GL, garageRoom.x, garageRoom.y, garageRoom.x + garageRoom.w, garageRoom.y + garageRoom.d, _oneFloorTop(), 0, 0.20);
    } else {
      var _masses = _footprintMasses(plan['1f'] || []);
      if (_masses.length > 1) {   // irregular footprint (L/T) -> a gable per rectangular mass
        _masses.sort(function (a, b) { return (b.x1 - b.x0) * (b.y1 - b.y0) - (a.x1 - a.x0) * (a.y1 - a.y0); });
        _masses.forEach(function (m, mi) {
          var _r = (rt === 'gambrel') ? gambrelRoofOver(L, GL, m.x0, m.y0, m.x1, m.y1, eave, minRise) : gableRoofOver(L, GL, m.x0, m.y0, m.x1, m.y1, eave, minRise);
          if (mi === 0) roof = _r;   // largest mass is the dormer reference
        });
      } else {
        roof = (rt === 'gambrel') ? gambrelRoofOver(L, GL, minx, miny, maxx, maxy, eave, minRise) : gableRoofOver(L, GL, minx, miny, maxx, maxy, eave, minRise);
      }
    }

    var d2 = 'off';
    (plan['2f'] || []).forEach(function (r) { var s = wOf(r.name); if (s === 'dom') d2 = 'dom'; else if (s === 'on' && d2 !== 'dom') d2 = 'on'; });
    if (rt !== 'flat') {
      var dormF = ((opts.elements && opts.elements['2f']) || []).filter(function (e) { return e.type === 'dormer' && e.slope !== 'rear'; });
      var dormR = ((opts.elements && opts.elements['2f']) || []).filter(function (e) { return e.type === 'dormer' && e.slope === 'rear'; });
      // occupancy of the 2f room sitting under a dormer at fraction `frac` along the
      // roof — so each dormer lights for ITS room, not the whole floor (v7.101.28).
      var _dSt = function (frac) { var cx = roof.a + (roof.b - roof.a) * frac; var rm = (plan['2f'] || []).filter(function (r) { return cx >= r.x && cx <= r.x + r.w; })[0]; return rm ? wOf(rm.name) : 'off'; };
      var _autoPos = function (n) { var ps = []; for (var k = 0; k < n; k++) ps.push((k + 1) / (n + 1)); return ps; };
      if (dormF.length) { var _pf = dormF.map(function (e) { return e.pos != null ? e.pos : 0.5; }); dormersOn(L, GL, roof, 0, d2, false, _pf, _pf.map(_dSt)); }
      else if (SPEC.dormersFront) { var _pfa = _autoPos(SPEC.dormersFront); dormersOn(L, GL, roof, 0, d2, false, _pfa, _pfa.map(_dSt)); }
      if (dormR.length) { var _pr = dormR.map(function (e) { return e.pos != null ? e.pos : 0.5; }); dormersOn(L, GL, roof, 0, d2, true, _pr, _pr.map(_dSt)); }
      else if (SPEC.dormersRear) { var _pra = _autoPos(SPEC.dormersRear); dormersOn(L, GL, roof, 0, d2, true, _pra, _pra.map(_dSt)); }
    }

    // 2nd-floor placed windows: side walls land on the gable ends, front/back on the
    // roof slope (front-facing 2F windows are the dormers). Lit by upstairs occupancy.
    var h2 = null;
    (plan['2f'] || []).forEach(function (r) {
      if (!h2) h2 = { x0: r.x, y0: r.y, x1: r.x + r.w, y1: r.y + r.d };
      else { h2.x0 = Math.min(h2.x0, r.x); h2.y0 = Math.min(h2.y0, r.y); h2.x1 = Math.max(h2.x1, r.x + r.w); h2.y1 = Math.max(h2.y1, r.y + r.d); }
    });
    if (!h2) h2 = { x0: minx, y0: miny, x1: maxx, y1: maxy };
    // 2nd-floor gable-end windows ride high on the gable, but clamped under the actual
    // roofline at their position — so they clear the low garage roof yet never poke through
    // the slope (v7.101.28). Front/back 2F glazing stays lower: that face is roof, so it reads
    // as dormers. ridgeApprox = eave + minRise is the gable peak height (story-driven).
    var gyC = (h2.y0 + h2.y1) / 2, gHalf = Math.max((h2.y1 - h2.y0) / 2, 0.1), ridgeApprox = eave + (minRise || 0);
    ((opts.elements && opts.elements['2f']) || []).filter(function (e) { return e.type === 'window'; }).forEach(function (e) {
      var p = e.pos != null ? e.pos : 0.5, w = e.w || 4;
      if (e.wall === 'left' || e.wall === 'right') {
        var xg = e.wall === 'left' ? ((h2.x0 - minx < 3) ? minx : h2.x0) : ((maxx - h2.x1 < 3) ? maxx : h2.x1), yc = h2.y0 + p * (h2.y1 - h2.y0);
        var roofAtYc = eave + (1 - Math.min(1, Math.abs(yc - gyC) / gHalf)) * (ridgeApprox - eave);
        var zHi = Math.min(15.0, roofAtYc - 0.5), zLo = Math.max(zHi - 2.8, 10.2);
        winX(L, GL, xg, yc - w / 2, yc + w / 2, zLo, zHi, winOcc2f(xg, yc), true, e.wall === 'left' ? -0.06 : 0.06);
      } else {
        var cx2 = h2.x0 + p * (h2.x1 - h2.x0), yf = e.wall === 'front' ? h2.y0 + 3.5 : h2.y1 - 3.5;
        winY(L, GL, yf, cx2 - w / 2, cx2 + w / 2, 10.8, 13.8, winOcc2f(cx2, yf), true, e.wall === 'front' ? -0.3 : 0.3);
      }
    });

    // basement exterior openings — walkout doors / egress windows at grade (v7.101.28)
    var hb = null;
    (plan['bsmt'] || []).forEach(function (r) {
      if (!hb) hb = { x0: r.x, y0: r.y, x1: r.x + r.w, y1: r.y + r.d };
      else { hb.x0 = Math.min(hb.x0, r.x); hb.y0 = Math.min(hb.y0, r.y); hb.x1 = Math.max(hb.x1, r.x + r.w); hb.y1 = Math.max(hb.y1, r.y + r.d); }
    });
    var placedB = ((opts.elements && opts.elements['bsmt']) || []).filter(function (e) { return e.type === 'window' || (e.type === 'door' && e.kind !== 'interior' && e.kind !== 'cased'); });
    if (hb && placedB.length) {
      placedB.forEach(function (e) {
        var p = e.pos != null ? e.pos : 0.5, w = e.w || 4, horiz = (e.wall === 'front' || e.wall === 'back'), cx, cy;
        if (e.wall === 'front') { cx = hb.x0 + p * (hb.x1 - hb.x0); cy = miny; }
        else if (e.wall === 'back') { cx = hb.x0 + p * (hb.x1 - hb.x0); cy = maxy; }
        else if (e.wall === 'left') { cx = (hb.x0 - minx < 3) ? minx : hb.x0; cy = hb.y0 + p * (hb.y1 - hb.y0); }
        else { cx = (maxx - hb.x1 < 3) ? maxx : hb.x1; cy = hb.y0 + p * (hb.y1 - hb.y0); }
        if (e.type === 'window') {
          if (horiz) winY(L, GL, cy, cx - w / 2, cx + w / 2, 0.6, 3.4, 'off', true, e.wall === 'front' ? -0.06 : 0.06);
          else winX(L, GL, cx, cy - w / 2, cy + w / 2, 0.6, 3.4, 'off', true, e.wall === 'left' ? -0.06 : 0.06);
        } else if (e.kind === 'cellar') {
          bulkheadDoor(L, GL, e.wall, cx, cy, Math.max(w, 5), e.open);
        } else {
          doorOnWall(L, GL, e.wall, cx, cy, Math.max(w, 3), 0.3, 6.4, e.open);
        }
      });
    }
    // auto bulkhead only when nothing is placed on the 1st floor or basement
    if (!placed.length && !placedB.length && plan['bsmt'] && plan['bsmt'].length) {
      bulkheadDoor(L, GL, 'back', (minx + maxx) / 2, maxy, 6, false);
    }

    var side = SPEC.chimney, yc = (miny + maxy) / 2;
    if (side === 'left' && roof.ridge) chimneyAt(L, minx, yc, roof.ridge + 4, -1);
    else if (side === 'right' && roof.ridge) chimneyAt(L, maxx, yc, roof.ridge + 4, 1);

    return { faces: L, glow: GL, labels: LBL };
  }

  function buildFromPlan(opts) {
    var plan = opts.plan || {}, lit = opts.lit || {}, floor = opts.floor || 'all';
    var L = [], GL = [], LBL = [];
    var stOf = function (n) { var s = lit[String(n).toLowerCase()]; return s === 'dom' ? 'dom' : s === 'mmwave' ? 'mmwave' : s ? 'on' : 'off'; };
    // footprint bbox across all floors -> center + exterior walls
    var minx = 1e9, miny = 1e9, maxx = -1e9, maxy = -1e9, ztop = -1e9;
    Object.keys(plan).forEach(function (fk) {
      var z = _planZ(fk);
      if (fk !== 'b' && fk !== 'bsmt' && z[1] > ztop) ztop = z[1];
      (plan[fk] || []).forEach(function (r) {
        if (r.x < minx) minx = r.x; if (r.y < miny) miny = r.y;
        if (r.x + r.w > maxx) maxx = r.x + r.w; if (r.y + r.d > maxy) maxy = r.y + r.d;
      });
    });
    if (minx > maxx) { minx = XG0; miny = 0; maxx = XHE; maxy = D; }
    if (ztop < 0) ztop = RIDGE;
    CENTER = [(minx + maxx) / 2, (miny + maxy) / 2, WALL * 0.5];
    var draw = function (fk) {
      var z = _planZ(fk);
      (plan[fk] || []).forEach(function (r) {
        if (r.points && r.points.length >= 3) roomPrism(L, LBL, r.points, z[0], z[1], r.label || r.name, stOf(r.name));
        else roomBox(L, LBL, r.x, r.y, r.w, r.d, z[0], z[1], r.label || r.name, stOf(r.name));
      });
    };
    if (floor !== 'all') {
      var pf = _planFloorKey(plan, floor);
      draw(pf);
      // interior doors on this floor's rooms, open/closed from their sensor (v7.101.28)
      var zf = _planZ(pf);
      ((opts.elements && opts.elements[pf]) || []).filter(function (e) { return e.type === 'door' && e.kind === 'interior'; }).forEach(function (e) {
        var rname = String(e.room || '').toLowerCase();
        var rr = (plan[pf] || []).filter(function (r) { return String(r.name).toLowerCase() === rname; })[0];
        if (!rr) return;
        var p = e.pos != null ? e.pos : 0.5, w = Math.max(e.w || 3, 3);
        var rx0 = rr.x, ry0 = rr.y, rx1 = rr.x + rr.w, ry1 = rr.y + rr.d, cx, cy;
        if (e.wall === 'front') { cx = rx0 + p * (rx1 - rx0); cy = ry0; }
        else if (e.wall === 'back') { cx = rx0 + p * (rx1 - rx0); cy = ry1; }
        else if (e.wall === 'left') { cx = rx0; cy = ry0 + p * (ry1 - ry0); }
        else { cx = rx1; cy = ry0 + p * (ry1 - ry0); }
        doorOnWall(L, GL, e.wall, cx, cy, w, zf[0] + 0.3, Math.min(zf[0] + 7, zf[1] - 0.2), e.open);
      });
      ((opts.elements && opts.elements[pf]) || []).filter(function (e) { return e.type === 'door' && e.kind === 'cased'; }).forEach(function (e) {
        var rname = String(e.room || '').toLowerCase();
        var rr = (plan[pf] || []).filter(function (r) { return String(r.name).toLowerCase() === rname; })[0];
        if (!rr) return;
        var p = e.pos != null ? e.pos : 0.5, w = Math.max(e.w || 3, 3);
        var rx0 = rr.x, ry0 = rr.y, rx1 = rr.x + rr.w, ry1 = rr.y + rr.d, cx, cy;
        if (e.wall === 'front') { cx = rx0 + p * (rx1 - rx0); cy = ry0; }
        else if (e.wall === 'back') { cx = rx0 + p * (rx1 - rx0); cy = ry1; }
        else if (e.wall === 'left') { cx = rx0; cy = ry0 + p * (ry1 - ry0); }
        else { cx = rx1; cy = ry0 + p * (ry1 - ry0); }
        casedOnWall(L, GL, e.wall, cx, cy, w, zf[0] + 0.3, Math.min(zf[0] + 7, zf[1] - 0.2));
      });
      return { faces: L, glow: GL, labels: LBL };
    }
    // Whole-house view: clean exterior shell with presence as lit windows.
    return buildExteriorFromPlan(opts, plan, minx, miny, maxx, maxy, ztop);
  }

  // ---------- assemble a frame for given options ----------
  function build(opts) {
    opts = opts || {};
    applySpec(opts.spec);                           // home type/specs (empty = default)
    if (opts.plan) return buildFromPlan(opts);      // data-driven: geometry from the editor's rooms
    CENTER = DEFAULT_CENTER;                         // hardcoded path uses the fixed model center
    var lit = opts.lit || {};                       // { 'master bedroom':'on'|'mmwave'|'dom', ... }
    var doors = opts.doors || {};                   // { front:'open'|'closed', garage:..., cellar:..., ... }
    var floor = opts.floor || 'all';
    var stOf = function (name) { var s = lit[String(name).toLowerCase()]; return s === 'dom' ? 'dom' : s === 'mmwave' ? 'mmwave' : s ? 'on' : 'off'; };
    var dOf = function (k) { return doors[k] === 'open' ? 'open' : 'closed'; };
    var L = [], GL = [], LBL = [];

    if (floor !== 'all') {
      // floor isolation: faint shell context + translucent labeled rooms for this level
      buildContext(L, floor);
      buildRooms(floor, lit, doors, L, LBL);
      return { faces: L, glow: GL, labels: LBL };
    }

    buildShell(L, GL);
    chimney(L, SPEC.chimney);
    garageDoors(L, GL, stOf('garage'), dOf('garage'));

    // dormers — counts configurable; unset = the approved default layout
    if (SPEC.dormersFront == null) {
      dormerFront(L, GL, XGH + 9, stOf("bedroom 2"));
      dormerFront(L, GL, XGH + 24, stOf('master bedroom'));
    } else {
      var fKeys = ["bedroom 2", 'master bedroom'], df;
      for (df = 0; df < SPEC.dormersFront; df++)
        dormerFront(L, GL, XGH + HW * (df + 1) / (SPEC.dormersFront + 1), stOf(fKeys[df] || fKeys[fKeys.length - 1]));
    }
    if (SPEC.dormersRear == null) {
      dormerRearRound(L, GL, XGH + 16, stOf('bath'));
    } else {
      var dr;
      for (dr = 0; dr < SPEC.dormersRear; dr++)
        dormerRearRound(L, GL, XGH + HW * (dr + 1) / (SPEC.dormersRear + 1), stOf('bath'));
    }

    // front facade: Dining (one window, L) · front door (centered) · Living Room (one window, R) — matching pair
    winY(L, GL, 0, XGH + 4, XGH + 8.5, 3, 7, stOf('dining room'), true);              // dining window
    doorY(L, GL, 0, XGH + 14.5, XGH + 17.5, 0, 7, -0.06, 'left', dOf('front'));        // front entry (centered)
    winY(L, GL, 0, XGH + 23.5, XGH + 28, 3, 7, stOf('living room'), true);            // living-room window (matches dining)
    // right (east) gable corners: Living Rm front (SE), Guest Rm rear (NE) — flank the chimney
    winX(L, GL, XHE, 3.5, 7.5, 3, 7, stOf('living room'), true);
    winX(L, GL, XHE, 16.5, 20.5, 3, 7, stOf('guest room'), true);
    // rear (north) facade: Kitchen (NW) · Guest (NE) windows · garage man-door · cellar bulkhead
    winY(L, GL, D, XGH + 3, XGH + 9, 3, 7, stOf('kitchen'), true, 0.06);
    winY(L, GL, D, XGH + 22, XGH + 28, 3, 7, stOf('guest room'), true, 0.06);
    doorY(L, GL, D, 25.2, 28.2, 0, 6.8, 0.06, 'right', dOf('garage_rear'));            // garage rear man-door (~3ft W of junction)
    bulkhead(L, GL, 33.5, 38.5, dOf('cellar'));                                        // cellar door under the kitchen window

    return { faces: L, glow: GL, labels: LBL };
  }

  // ---------- render to SVG ----------
  function renderSVG(opts) {
    opts = opts || {};
    var theta = opts.theta == null ? 35 : opts.theta;
    var built = build(opts);
    var faces = built.faces, glow = built.glow, labels = built.labels || [];
    // depth sort: farthest first
    faces.sort(function (a, b) { return faceDepth(b, theta) - faceDepth(a, theta); });

    // bounds
    var mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9, all = faces.concat(glow), i, j, q;
    for (i = 0; i < all.length; i++) for (j = 0; j < all[i].p.length; j++) {
      q = project(all[i].p[j], theta);
      if (q[0] < mnx) mnx = q[0]; if (q[0] > mxx) mxx = q[0];
      if (q[1] < mny) mny = q[1]; if (q[1] > mxy) mxy = q[1];
    }
    var pad = 40, X0, Y0, W, H;
    if (opts.box) { X0 = opts.box[0]; Y0 = opts.box[1]; W = opts.box[2]; H = opts.box[3]; }
    else { X0 = mnx - pad; Y0 = mny - pad; W = (mxx - mnx) + 2 * pad; H = (mxy - mny) + 2 * pad; }
    var vb = X0.toFixed(1) + ' ' + Y0.toFixed(1) + ' ' + W.toFixed(1) + ' ' + H.toFixed(1);
    var pp = function (pts) { return pts.map(function (p) { var s = project(p, theta); return s[0].toFixed(1) + ',' + s[1].toFixed(1); }).join(' '); };

    var body = '';
    body += '<defs><filter id="g3" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3"/></filter>'
         + '<radialGradient id="bg3" cx="50%" cy="40%" r="65%"><stop offset="0%" stop-color="rgba(90,45,20,0.20)"/><stop offset="100%" stop-color="rgba(0,0,0,0)"/></radialGradient>'
         + '<radialGradient id="sh3" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="rgba(0,0,0,0.55)"/><stop offset="100%" stop-color="rgba(0,0,0,0)"/></radialGradient></defs>';
    body += '<rect x="' + X0.toFixed(1) + '" y="' + Y0.toFixed(1) + '" width="' + W.toFixed(1) + '" height="' + H.toFixed(1) + '" fill="url(#bg3)"/>';
    // ground shadow
    var gc = project([CENTER[0], CENTER[1], 0], theta);
    body += '<ellipse cx="' + gc[0].toFixed(1) + '" cy="' + (mxy + pad * 0.2).toFixed(1) + '" rx="' + ((mxx - mnx) * 0.42).toFixed(1) + '" ry="20" fill="url(#sh3)"/>';
    // glow
    for (i = 0; i < glow.length; i++) body += '<polygon points="' + pp(glow[i].p) + '" fill="' + glow[i].f + '" filter="url(#g3)"/>';
    // faces
    for (i = 0; i < faces.length; i++) {
      var fc = faces[i], closed = fc.f !== 'none';
      body += '<poly' + (closed ? 'gon' : 'line') + ' points="' + pp(fc.p) + '"' + (fc.cls ? ' class="' + fc.cls + '"' : '')
            + ' fill="' + (closed ? fc.f : 'none') + '" stroke="' + fc.s + '" stroke-width="' + fc.w + '" stroke-linejoin="round" stroke-linecap="round"/>';
    }
    // room labels + occupancy pulses (upright, drawn on top)
    for (i = 0; i < labels.length; i++) {
      var lb = labels[i], sp = project([lb.x, lb.y, lb.z], theta);
      if (lb.dot) {
        var dc = lb.st === 'dom' ? '#7dffcd' : lb.st === 'mmwave' ? '#5affbe' : '#f4b860';
        body += '<circle cx="' + sp[0].toFixed(1) + '" cy="' + sp[1].toFixed(1) + '" r="2.4" fill="' + dc + '">'
              + '<animate attributeName="opacity" values="0.35;1;0.35" dur="2s" repeatCount="indefinite"/></circle>';
      } else {
        var tc = lb.st === 'dom' ? '#9effd0' : lb.st === 'mmwave' ? '#8fffd4' : lb.st === 'on' ? '#f4b860' : 'rgba(244,184,96,0.5)';
        var fs = lb.small ? 6 : (lb.big ? 9 : 7.5);
        body += '<text x="' + sp[0].toFixed(1) + '" y="' + sp[1].toFixed(1) + '" text-anchor="middle" dominant-baseline="middle"'
              + ' font-family="JetBrains Mono, ui-monospace, monospace" font-size="' + fs + '" font-weight="600" letter-spacing="0.8"'
              + ' paint-order="stroke" stroke="#04080c" stroke-width="0.8" stroke-linejoin="round" fill="' + tc + '">' + lb.t + '</text>';
      }
    }
    return '<svg xmlns="http://www.w3.org/2000/svg" width="' + W.toFixed(0) + '" height="' + H.toFixed(0) + '" viewBox="' + vb + '">' + body + '</svg>';
  }

  // ---------- a stable viewBox covering the model across all rotations ----------
  function fixedBox(opts) {
    var b = build(opts || {}), all = b.faces.concat(b.glow);
    var mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9, t, i, j, q;
    for (t = 0; t < 360; t += 15)
      for (i = 0; i < all.length; i++) for (j = 0; j < all[i].p.length; j++) {
        q = project(all[i].p[j], t);
        if (q[0] < mnx) mnx = q[0]; if (q[0] > mxx) mxx = q[0];
        if (q[1] < mny) mny = q[1]; if (q[1] > mxy) mxy = q[1];
      }
    var pad = 46;
    return [mnx - pad, mny - pad, (mxx - mnx) + 2 * pad, (mxy - mny) + 2 * pad];
  }

  return { build: build, renderSVG: renderSVG, fixedBox: fixedBox, project: project, dims: { GW: GW, HW: HW, D: D, WALL: WALL, RIDGE: RIDGE } };
})();

// window.NOVA3D lets the code below (this panel's own Residence 3D tab)
// reuse this engine as a plain global instead of re-deriving 1000+ lines
// of 3D geometry.
if (typeof window !== "undefined") window.NOVA3D = NOVA3D;

/*
 * Nova Command Center Panel.
 * v7.120.3
 *
 * Started life as "Command Center" — a genuinely separate implementation
 * from the original Classic UI, built with full creative freedom over
 * ongoing maintenance cost. Classic reached feature parity and was
 * deleted in v7.101.30; this is now Nova's one and only dashboard,
 * registered directly as "nova-panel" via panel_custom (no more style
 * switcher, no more dynamic import — this file loads on its own).
 *
 * Layout: Command Center + Settings, reorganized around what you're
 * trying to do rather than which subsystem it touches (General, Voice &
 * Speakers, Awareness & Safety, Learning & Memory, Cameras, Home &
 * Extras), plus a search box across every setting. All 27 Settings cards
 * are real. Residence, Intrusion, Suggestions, Logs, and Memory are all
 * full nav tabs here.
 *
 * Design: an animated "stellar core" (Nova = a star's sudden brightening)
 * replaces a camera feed as the dashboard's visual anchor — it works
 * identically whether someone has zero cameras or twelve, and doesn't
 * repeat the cyan sci-fi-HUD look this project's name already evokes.
 * Camera Watch becomes an optional, collapsed card instead, since not
 * every camera integration (e.g. Eufy) streams live into Home Assistant.
 * Approved from a static mockup (nova-command-center-mockup.html) before
 * this real, live-data build.
 */
class NovaPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this._liveData = null;
    this._activityData = null;
    this._renderedOnce = false;
    this._fetchInterval = null;
    this._lastActivitySig = null;
    this._flareUntil = 0;
    this._animHandle = null;
    this._particles = [];
    this._current = { speed: 0.20, count: 70, radiusMul: 1, glow: 0.55, hot: 0.35, flare: 0.05 };
    this._camOpen = false;
    this._cameraImages = {};
    this._cameraLoading = {};
    this._cameraDiagnostics = {};
    this._cameraInterval = null;
    this._cognitive = null;
    this._currentTab = "dashboard"; // "dashboard" | "settings" | "logs" | "memory" | "intrusion" | "suggestions"
    this._logFilter = "all";
    this._logSearch = "";
    this._settingsSection = "general";
    this._settingsSearch = "";
    this._uiStrings = null;
    this._uiLangLoaded = null;
    this._uiLangRequest = 0;
  }

  // ─── HA property contract — same shape as Classic's, see nova-panel.js ──
  set hass(hass) {
    const first = this._hass === null;
    this._hass = hass;
    if (first) {
      this._render();
      this._startIntervals();
      this._loadUiStrings();
    }
  }
  get hass() { return this._hass; }
  set panel(panel) { this._config = panel?.config || {}; }
  set narrow(narrow) { this._narrow = narrow; }
  set route(route) { this._route = route; }

  connectedCallback() {
    if (!window.__novaBannerLogged) {
      window.__novaBannerLogged = true;
      console.log("%c Nova Panel %c v7.120.3 ",
        "color: #f4b860; background: #1e0d06; padding: 2px 6px;",
        "color: #e2542f; background: #050403; padding: 2px 6px;");
    }
    if (!document.getElementById("nova-new-fonts")) {
      const l = document.createElement("link");
      l.id = "nova-new-fonts";
      l.rel = "stylesheet";
      l.href = "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Manrope:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap";
      document.head.appendChild(l);
    }
    if (this._hass && !this._renderedOnce) {
      this._render();
      this._startIntervals();
    }
  }

  disconnectedCallback() {
    if (this._fetchInterval) clearInterval(this._fetchInterval);
    if (this._sparklineInterval) clearInterval(this._sparklineInterval);
    if (this._cameraInterval) clearInterval(this._cameraInterval);
    if (this._animHandle) cancelAnimationFrame(this._animHandle);
    if (this._resizeListener) window.removeEventListener("resize", this._resizeListener);
  }

  _startIntervals() {
    this._fetchLiveData();
    if (!this._fetchInterval) {
      this._fetchInterval = setInterval(() => this._fetchLiveData(), 20000);
    }
    this._fetchAreaSparklines();
    if (!this._sparklineInterval) {
      // Trend history changes slowly — matches Classic's own 5-minute cadence
      // (nova-panel.js's _fetchAreaSparklines), no need to poll as often as
      // the main dashboard data.
      this._sparklineInterval = setInterval(() => this._fetchAreaSparklines(), 300000);
    }
  }

  async _fetchAreaSparklines() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/get_area_sparklines" });
      this._sparklines = res?.sparklines || {};
    } catch (err) {
      console.warn("Nova: sparkline fetch failed", err);
      return;
    }
    this._renderData();
  }

  // ─── Data ────────────────────────────────────────────────────────────────

  async _fetchLiveData() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "nova/get_panel_data" });
      this._liveData = result;
      try {
        const log = await this._hass.callWS({ type: "nova/get_activity_log", hours: 24, limit: 60 });
        this._activityData = log?.entries || [];
      } catch (_) { this._activityData = []; }
    } catch (err) {
      console.warn("Nova: panel data fetch failed", err);
    }
    try {
      this._solar = await this._hass.callWS({ type: "nova/solar", action: "status" });
    } catch (_) { this._solar = null; }
    try {
      this._mode = await this._hass.callWS({ type: "nova/mode", action: "status" });
    } catch (_) { this._mode = null; }
    if (this._currentTab === "dashboard") {
      try {
        this._cognitive = await this._hass.callWS({ type: "nova/get_cognitive_status" });
      } catch (_) { this._cognitive = null; }
    }
    if (this._currentTab === "logs") this._fetchDebugLog();
    this._loadUiStrings();
    this._detectFlare();
    this._renderData();
  }

  _detectFlare() {
    const top = (this._activityData || [])[0];
    const sig = top ? `${top.ts}|${top.tag}|${top.msg}` : null;
    if (sig && this._lastActivitySig && sig !== this._lastActivitySig) {
      this._flareUntil = Date.now() + 4000; // a real event just landed — brief flare
    }
    this._lastActivitySig = sig;
  }

  _coreState() {
    const sleepState = String(this._liveData?.status?.sleep?.state || "").toUpperCase();
    if (sleepState === "ASLEEP") return "asleep";
    if (Date.now() < this._flareUntil) return "reasoning";
    return "idle";
  }

  _data() {
    const live = this._liveData;
    if (!live) return null;
    return {
      status: live.status || {},
      areas: live.areas || [],
      doors: live.doors || {},
      cameras: live.config?.cameras || [],
      areasMonitored: live.meta?.areas_monitored ?? "—",
      occupied: (live.areas || []).filter(a => a.active).length,
      config: live.config || {},
      doorbellTraining: live.doorbell_training || {},
      suggestions: live.suggestions || [],
      goals: live.goals || [],
      lockdown: live.lockdown || live.config?.lockdown || {},
      onboarding: live.onboarding || live.config?.onboarding || null,
      available_labels: live.config?.available_labels || [],
    };
  }

  // ─── Render (structure once, patch data after) ──────────────────────────

  _render() {
    // Tearing down and rebuilding the DOM (now happening on every tab
    // switch, not just once) orphans the previous canvas — its
    // requestAnimationFrame loop and resize listener would otherwise keep
    // running forever on a detached, invisible canvas, compounding every
    // time someone switches tabs. Stop it before _initCore() starts a
    // fresh one.
    if (this._animHandle) { cancelAnimationFrame(this._animHandle); this._animHandle = null; }
    if (this._resizeListener) { window.removeEventListener("resize", this._resizeListener); this._resizeListener = null; }
    this._ctx = null;
    this._canvas = null;

    const root = this.shadowRoot;
    root.innerHTML = this._html();
    this._localizeDOM(root);
    this._renderedOnce = true;
    this._wire();
    this._initCore();
    this._renderData();
  }

  // Panel translations are keyed by exact English source strings. Dynamic
  // values (entity ids, model names, counts) therefore remain untouched, and
  // a missing key falls back to the English text already in the DOM.
  _resolveUiLang() {
    const override = this._liveData?.config?.ui_language;
    if (override && override !== "auto") return String(override);
    return String(this._hass?.language || "en");
  }

  async _loadUiStrings(force = false) {
    const full = (this._resolveUiLang() || "en").toLowerCase().replace(/_/g, "-");
    const base = full.split("-")[0];
    if (!force && this._uiLangLoaded === full) return;
    const request = ++this._uiLangRequest;
    this._uiLangLoaded = full;
    if (base === "en") {
      this._uiStrings = null;
      if (this._renderedOnce) this._render();
      return;
    }
    const grab = async (lang) => {
      const response = await fetch(`/nova_panel_static/i18n/${encodeURIComponent(lang)}.json`);
      if (!response.ok) return null;
      const value = await response.json();
      if (!value || Array.isArray(value) || typeof value !== "object") return null;
      return Object.fromEntries(Object.entries(value).filter(([k, v]) =>
        typeof k === "string" && typeof v === "string"));
    };
    try {
      let dict = await grab(full);
      if (!dict && full !== base) dict = await grab(base);
      if (request !== this._uiLangRequest) return;
      this._uiStrings = dict || null;
    } catch (_) {
      if (request !== this._uiLangRequest) return;
      this._uiStrings = null;
    }
    if (this._renderedOnce) this._render();
  }

  _localizeDOM(root) {
    const dict = this._uiStrings;
    if (!dict || !root) return;
    try {
      const walker = document.createTreeWalker(root, 4, null);
      const swaps = [];
      let node;
      while ((node = walker.nextNode())) {
        const raw = node.nodeValue;
        if (!raw) continue;
        const key = raw.trim();
        if (key && Object.prototype.hasOwnProperty.call(dict, key)) {
          swaps.push([node, raw.replace(key, dict[key])]);
        }
      }
      swaps.forEach(([textNode, value]) => { textNode.nodeValue = value; });
      root.querySelectorAll("[title],[placeholder]").forEach(el => {
        ["title", "placeholder"].forEach(attr => {
          const raw = el.getAttribute(attr);
          const key = raw?.trim();
          if (key && Object.prototype.hasOwnProperty.call(dict, key)) {
            el.setAttribute(attr, raw.replace(key, dict[key]));
          }
        });
      });
    } catch (_) { /* English DOM remains usable if localization fails. */ }
  }

  _html() {
    const tab = this._currentTab;
    return `
      <style>${this._css()}</style>
      <div class="wrap">
        <div class="topbar">
          <div class="brand">
            <div class="brand-mark"></div>
            <div>
              <div class="brand-name">Nova</div>
              <div class="brand-tag">${tab === "settings" ? "Settings" : tab === "logs" ? "Logs" : tab === "memory" ? "Memory" : tab === "intrusion" ? "Intrusion" : tab === "suggestions" ? "Suggestions" : tab === "residence" ? "Residence" : "Command Center"}</div>
            </div>
          </div>
          <nav class="top-nav">
            <button class="nav-tab${tab === "dashboard" ? " active" : ""}" data-tab="dashboard">Command Center</button>
            <button class="nav-tab${tab === "residence" ? " active" : ""}" data-tab="residence">Residence</button>
            <button class="nav-tab${tab === "intrusion" ? " active" : ""}" data-tab="intrusion">Intrusion</button>
            <button class="nav-tab${tab === "suggestions" ? " active" : ""}" data-tab="suggestions">Suggestions</button>
            <button class="nav-tab${tab === "settings" ? " active" : ""}" data-tab="settings">Settings</button>
            <button class="nav-tab${tab === "logs" ? " active" : ""}" data-tab="logs">Logs</button>
            <button class="nav-tab${tab === "memory" ? " active" : ""}" data-tab="memory">Memory</button>
          </nav>
          <button class="lockdown-control" id="lockdownControl" hidden></button>
        </div>

        ${tab === "settings" ? this._htmlSettings() : tab === "logs" ? this._htmlLogs() : tab === "memory" ? this._htmlMemory() : tab === "intrusion" ? this._htmlIntrusion() : tab === "suggestions" ? this._htmlSuggestions() : tab === "residence" ? this._htmlResidence() : this._htmlDashboard()}

        <div class="footnote">NOVA COMMAND CENTER</div>
      </div>
    `;
  }

  _htmlDashboard() {
    return `
        <div id="onboardingMount"></div>
        <div class="hero">
          <div class="core-wrap"><canvas class="core" id="core"></canvas></div>
          <div class="state-line" id="stateLine">Watching over the house.</div>
          <div class="state-sub" id="stateSub">—</div>
          <div class="chips" id="chips"></div>
        </div>
${this._htmlDashboardBody()}`;
  }

  _onboardingHtml(onboarding) {
    return onboarding?.show ? `
      <div class="onboarding-card" id="onboardingCard">
        <div class="panel-head"><div><div class="panel-title">Welcome — get Nova working for you</div>
          <div class="toggle-desc">These steps are optional. Nova can already answer you.</div></div>
          <button class="camera-toggle" id="onboardingDismiss" title="Dismiss">DISMISS</button></div>
        <div class="onboarding-progress"><span>${this._esc(onboarding.done_count || 0)}/${this._esc(onboarding.total || 0)} DONE</span><i style="width:${Math.round(((onboarding.done_count || 0) / Math.max(1, onboarding.total || 1)) * 100)}%"></i></div>
        <div class="onboarding-steps">${(onboarding.steps || []).map(step => `<div class="onboarding-step${step.done ? " done" : ""}">
          <span>${step.done ? "✓" : "○"}</span><div><b>${this._esc(step.label)}</b><small>${this._esc(step.hint)}</small></div>
          ${step.jump ? `<button class="mode-chip onboarding-jump" data-settings-title="${this._esc(step.jump)}">OPEN</button>` : ""}</div>`).join("")}</div>
        <button class="mode-chip onboarding-settings">OPEN SETTINGS</button>
      </div>` : "";
  }

  _htmlDashboardBody() {
    return `
        <div class="grid">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Activity</div>
              <div class="panel-meta" id="feedMeta">—</div>
            </div>
            <div class="feed" id="feed"></div>
          </div>
        </div>

        <div class="panel" style="max-width:1100px;margin:16px auto 0">
          <div class="panel-head">
            <div class="panel-title">Areas</div>
            <div class="panel-meta" id="areasMeta">—</div>
          </div>
          <div class="areas-grid" id="areasGrid"></div>
        </div>

        <div class="dashboard-pair">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Cognitive Core</div>
              <div class="panel-meta" id="cognitiveState">—</div>
            </div>
            <div class="metric-grid" id="cognitiveMetrics"></div>
            <div class="toggle-desc" id="cognitiveAnalysis"></div>
          </div>
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Goals</div>
              <div class="panel-meta" id="goalsMeta">—</div>
            </div>
            <div class="goal-list" id="goalList"></div>
            <div class="goal-create">
              <input class="cfg-field" id="goalOutcome" maxlength="500" placeholder="Outcome Nova should work toward">
              <button class="mode-chip" id="goalCreate">ADD GOAL</button>
            </div>
            <div class="toggle-desc" id="goalResult"></div>
          </div>
        </div>

        <div class="panel" id="solarPanel" style="max-width:1100px;margin:16px auto 0">
          <div class="panel-head">
            <div class="panel-title">Solar</div>
            <div class="panel-meta" id="solarSufficiency">—</div>
          </div>
          <div id="solarBody" class="stub-body">Loading…</div>
        </div>

        <div class="panel" style="max-width:1100px;margin:16px auto 0">
          <div class="panel-head">
            <div class="panel-title">Quick Actions</div>
            <div class="panel-meta">CMD</div>
          </div>
          <div class="mode-grid">
            <button class="mode-chip" data-svc="nova.briefing">Briefing</button>
            <button class="mode-chip" data-svc="nova.nap" data-svc-data='{"duration_minutes":30}'>Nap 30m</button>
            <button class="mode-chip" data-svc="nova.nap" data-svc-data='{"duration_minutes":60}'>Nap 60m</button>
            <button class="mode-chip" data-svc="nova.unshush">Unshush All</button>
            <button class="mode-chip" data-svc="nova.observer_status">Status Dump</button>
            <button class="mode-chip" id="qaRunAnalysis">Analyze Now</button>
          </div>
          <div class="toggle-desc" id="qaAnalysisResult" style="margin-top:8px"></div>
        </div>

        <div class="panel camera-panel" id="cameraPanel" style="max-width:1100px;margin:16px auto 0" hidden>
          <div class="camera-head-row">
            <div>
              <div class="panel-title" style="margin-bottom:5px">Camera Watch</div>
              <div class="camera-note">Authenticated snapshots from cameras available to Home Assistant.</div>
            </div>
            <button class="camera-toggle" id="camToggle">SHOW CAMERAS ▾</button>
          </div>
          <div class="camera-strip" id="camStrip"></div>
        </div>
    `;
  }

  // ─── Logs ─────────────────────────────────────────────────────────────
  // Ported from Classic's own _fetchDebugLog (nova-panel.js) — same single
  // nova/get_debug_log call, same client-side category+search filtering,
  // and the same escaping discipline: e.ts/e.cat/e.msg are log CONTENT
  // (entity names, states, model output can end up in them), so they are
  // attacker/LLM-influenced and go through this._esc() before innerHTML —
  // this is a real fixed-XSS surface in Classic, not decorative caution.
  // Kept in sync with every literal category string passed to nova_log()
  // across the backend (grep `nova_log("` to re-verify). ROUTE/REASON/TTS
  // removed 13 Sept 2026: nothing in the backend logs under those
  // categories any more, so their chips could never match a real entry —
  // caught live when a real "LEARN" entry (used, but missing from both
  // this list and the color map) fell through to a bare "•" bullet with
  // no way to filter for it.
  static LOG_FILTERS = ["all", "CONV", "REPLY", "LOCAL", "LEARN", "AGENT", "AUTO", "MODE", "CONFIG",
    "CLASSIFY", "CAMERA", "ENERGY", "BIO", "OFFER", "SAFETY", "ERROR", "WARNING", "GATE", "DEDUP", "OFFLINE"];
  static LOG_CATEGORIES = {
    CONV: { color: "#5fd0e0", icon: "💬" },
    REPLY: { color: "#4fb8ff", icon: "💭" },
    LOCAL: { color: "#5fbf7a", icon: "⚡" },
    LEARN: { color: "#8fd15c", icon: "🧠" },
    AGENT: { color: "var(--gold)", icon: "🤖" },
    AUTO: { color: "#ffb454", icon: "🔁" },
    MODE: { color: "#c9a0ff", icon: "🎚️" },
    CONFIG: { color: "#9d8cff", icon: "⚙️" },
    CLASSIFY: { color: "#9d8cff", icon: "🏷️" },
    CAMERA: { color: "#5fbf7a", icon: "📷" },
    ENERGY: { color: "#ffcf6a", icon: "🔌" },
    BIO: { color: "#ff8fc7", icon: "💓" },
    OFFER: { color: "#ffd27a", icon: "🙋" },
    SAFETY: { color: "#ff8a8a", icon: "🛡️" },
    ERROR: { color: "#ff6b81", icon: "❌" },
    WARNING: { color: "var(--warn)", icon: "⚠️" },
    GATE: { color: "var(--ink-faint)", icon: "🚧" },
    DEDUP: { color: "var(--ink-faint)", icon: "🔇" },
    OFFLINE: { color: "var(--ink-faint)", icon: "📴" },
  };

  _htmlLogs() {
    const filterChips = NovaPanel.LOG_FILTERS.map(f =>
      `<button class="mode-chip new-log-filter${(this._logFilter || "all") === f ? " mode-chip-on" : ""}" data-filter="${f}">${f.toUpperCase()}</button>`).join("");
    const view = this._logView || "system";
    const title = view === "decisions" ? "Decisions" : view === "spoken_history" ? "Spoken History"
      : view === "actions" ? "Actions" : "System Log";
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">${title}</div>
            <div class="panel-meta">Nova internal</div>
          </div>
          <div class="mode-grid">
            <button class="mode-chip new-logview${view === "system" ? " mode-chip-on" : ""}" data-view="system">SYSTEM LOG</button>
            <button class="mode-chip new-logview${view === "decisions" ? " mode-chip-on" : ""}" data-view="decisions">DECISIONS</button>
            <button class="mode-chip new-logview${view === "spoken_history" ? " mode-chip-on" : ""}" data-view="spoken_history">SPOKEN HISTORY</button>
            <button class="mode-chip new-logview${view === "actions" ? " mode-chip-on" : ""}" data-view="actions">ACTIONS</button>
          </div>
          ${view === "decisions" ? this._htmlDecisionsView() : view === "spoken_history" ? this._htmlSpokenHistoryView()
            : view === "actions" ? this._htmlActionsView() : `
          <div class="cfg-row">
            <input id="newLogSearch" class="cfg-field" style="flex:1" type="text" placeholder="search…" autocomplete="off" value="${this._esc(this._logSearch || "")}">
          </div>
          <div class="mode-grid">${filterChips}</div>
          <div class="toggle-desc" id="newLogCount" style="margin:8px 0"></div>
          <div id="newLogEntries" class="new-log-entries">
            <div class="stub-body">Loading…</div>
          </div>`}
        </div>
    `;
  }

  // ─── Spoken History (v7.104.0) ──────────────────────────────────────────
  // The last things Nova actually sent to a speaker — welcome-home,
  // reminders, alerts, briefings, manual tests, confirmed Assist replies,
  // and repeats. Text only, bounded to the last 100, newest first. Always
  // renders its heading; a distinct empty vs. error state, same pattern as
  // Provider Activity/Installed Automations.

  _spokenSourceLabel(source) {
    return {
      welcome: "Welcome", reminder: "Reminder", alert: "Alert",
      briefing: "Briefing", camera: "Camera", manual: "Manual",
      reply: "Reply", repeat: "Repeat", routine: "Routine", scene: "Scene",
      confirm: "Confirmation", followup: "Follow-up",
    }[source] || (source ? source[0].toUpperCase() + source.slice(1) : "Other");
  }

  _speakerLabel(eid) {
    const st = this._hass?.states?.[eid];
    return (st && st.attributes && st.attributes.friendly_name) || eid;
  }

  _htmlSpokenHistoryView() {
    // Static shell only — _fetchSpokenHistory()/_renderSpokenHistoryRows()
    // update #spokenHistoryEntries directly, the same way _fetchDecisions()/
    // _renderDecisionRows() do, so a fetch never has to go through a full
    // _render() (which would re-trigger _wire() and re-fetch, looping).
    return `<div id="spokenHistoryEntries"><div class="stub-body">Loading…</div></div>`;
  }

  async _fetchSpokenHistory() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "nova/get_spoken_history" });
      this._spokenHistory = result.entries || [];
    } catch (_) { this._spokenHistory = null; }
    this._renderSpokenHistoryRows();
  }

  _renderSpokenHistoryRows() {
    const container = this.shadowRoot?.getElementById("spokenHistoryEntries");
    if (!container) return;
    const entries = this._spokenHistory;
    if (entries === null) {
      container.innerHTML = `<div class="stub-body">Couldn't load spoken history.</div>`;
      return;
    }
    if (!entries || !entries.length) {
      container.innerHTML = `<div class="stub-body">No spoken messages recorded yet.</div>`;
      return;
    }
    container.innerHTML = entries.map(e => {
      const when = e.timestamp ? new Date(e.timestamp * 1000).toLocaleString() : "";
      const speakerNames = (e.speakers || []).map(s => this._speakerLabel(s)).join(", ") || "—";
      return `
        <div class="cfg-row">
          <label>${this._esc(this._spokenSourceLabel(e.source))} · ${this._esc(when)}</label>
          <span class="toggle-desc">${this._esc((e.delivery_state || "sent").toUpperCase())}</span>
        </div>
        <div class="stub-body" style="margin:-6px 0 4px">${this._esc(e.text)}</div>
        <div class="cfg-row">
          <span class="toggle-desc">${this._esc(speakerNames)}</span>
          <button class="mode-chip new-spoken-repeat" data-spoken-id="${e.id}">REPEAT</button>
        </div>`;
    }).join("");
    container.querySelectorAll(".new-spoken-repeat").forEach(btn => {
      btn.addEventListener("click", () => {
        const id = parseInt(btn.getAttribute("data-spoken-id"), 10);
        if (!isNaN(id)) this._repeatSpoken(id);
      });
    });
  }

  async _repeatSpoken(spokenId) {
    if (!this._hass) return;
    try {
      await this._hass.callWS({ type: "nova/repeat_spoken", spoken_id: spokenId });
      this._fetchSpokenHistory();
    } catch (err) {
      console.error("Nova: repeat spoken failed", err);
    }
  }

  // ─── Actions (Action Audit Log) ─────────────────────────────────────────
  // Actions Nova genuinely attempted or performed — device controls, bulk
  // controls, scene/script/automation execution, safety routines, suggested-
  // automation installation, notifications. Request-level, keyset-paginated:
  // one page is a set of COMPLETE request groups (a bulk action's targets
  // are never split across pages). Strictly read-only — no retry/replay/
  // approve/reject control here, matching Spoken History and Decisions.

  _actionStatusClass(status) {
    return {
      success: "diag-ok", verified: "diag-ok",
      partial: "diag-warn", awaiting: "diag-warn",
      failed: "diag-down", blocked: "diag-down",
    }[status] || "diag-idle";
  }

  _htmlActionsView() {
    // Static shell only — _fetchActions()/_renderActionRows() update
    // #actionEntries directly, same pattern as Spoken History/Decisions, so
    // a fetch never re-triggers a full _render().
    return `
      <div id="actionEntries" class="new-log-entries">
        <div class="stub-body">Loading…</div>
      </div>
      <div class="cfg-row" id="actionLoadMoreRow" hidden>
        <button class="mode-chip" id="newActionLoadMore">LOAD MORE</button>
      </div>
    `;
  }

  async _fetchActions(reset = true) {
    if (!this._hass) return;
    if (reset) { this._actions = []; this._actionsCursor = null; }
    const container = this.shadowRoot?.getElementById("actionEntries");
    if (container && reset) container.innerHTML = `<div class="stub-body">Loading…</div>`;
    try {
      const args = { type: "nova/list_actions", limit: 20 };
      if (!reset && this._actionsCursor) {
        args.cursor_ts = this._actionsCursor.ts;
        args.cursor_request_id = this._actionsCursor.request_id;
      }
      const result = await this._hass.callWS(args);
      const page = result.requests || [];
      this._actions = reset ? page : (this._actions || []).concat(page);
      this._actionsCursor = result.next_cursor || null;
      this._renderActionRows();
    } catch (err) {
      this._actions = null;
      this._renderActionRows();
    }
  }

  _actionLabel(a) {
    return (a.action || "").replace(/_/g, " ");
  }

  _renderActionRows() {
    const container = this.shadowRoot?.getElementById("actionEntries");
    if (!container) return;
    const requests = this._actions;
    if (requests === null) {
      container.innerHTML = `<div class="new-log-entry-error" style="padding:12px">Couldn't load actions.</div>`;
      const row = this.shadowRoot?.getElementById("actionLoadMoreRow");
      if (row) row.hidden = true;
      return;
    }
    if (!requests || !requests.length) {
      container.innerHTML = `<div class="stub-body">No actions recorded yet.</div>`;
      const row = this.shadowRoot?.getElementById("actionLoadMoreRow");
      if (row) row.hidden = true;
      return;
    }
    container.innerHTML = requests.map(r => {
      const when = r.ts_created ? new Date(r.ts_created * 1000).toLocaleString() : "";
      const statusCls = this._actionStatusClass(r.status);
      const requester = r.requested_by_name || r.requested_by_user_id || r.request_device_id || "";
      const targets = r.targets || [];
      const spokenNote = (r.spoken_history_id !== null && r.spoken_history_id !== undefined)
        ? `<span class="toggle-desc" title="Linked Spoken History entry">🔊 spoken</span>` : "";
      const targetRows = targets.map(t => {
        const targetName = t.entity_id || [t.domain, t.service].filter(Boolean).join(".") || "—";
        return `
          <div class="cfg-row">
            <label>${this._esc(targetName)}</label>
            <span class="toggle-desc">approval: ${this._esc(t.approval_result)} · execution: ${this._esc(t.execution_result)}</span>
          </div>
          ${t.reason_text ? `<div class="stub-body" style="margin:-4px 0 6px;font-size:11px">${this._esc(t.reason_text)}</div>` : ""}`;
      }).join("");
      return `
        <details class="new-log-entry" style="display:block">
          <summary style="cursor:pointer;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span class="new-log-ts">${this._esc(when)}</span>
            <span class="new-log-cat">${this._esc((r.source || "").toUpperCase())}</span>
            <span class="new-log-msg">${this._esc(this._actionLabel(r))}${requester ? " · " + this._esc(requester) : ""}</span>
            <span class="${statusCls}">${this._esc((r.status || "").toUpperCase())}</span>
            ${spokenNote}
          </summary>
          <div style="margin-top:8px">${targetRows || '<div class="stub-body">No target detail.</div>'}</div>
        </details>`;
    }).join("");
    const loadMoreRow = this.shadowRoot?.getElementById("actionLoadMoreRow");
    if (loadMoreRow) loadMoreRow.hidden = !this._actionsCursor;
  }

  // ─── Decisions (Phase 1: decision explanations + feedback) ─────────────
  // A bounded, cursor-paginated browser over Nova's Decision Record store —
  // separate from the System Log above (nova/get_debug_log): these are the
  // structured observation/interpretation/evidence/outcome rows behind
  // nova/get_calibration's aggregate stats, not free-text log lines.

  _htmlDecisionsView() {
    return `
      <div class="cfg-row">
        <button class="mode-chip${this._decisionsUnjudgedOnly ? " mode-chip-on" : ""}" id="newDecUnjudged">UNJUDGED ONLY</button>
      </div>
      <div class="toggle-desc" id="newDecCount" style="margin:8px 0"></div>
      <div id="decisionEntries" class="new-log-entries">
        <div class="stub-body">Loading…</div>
      </div>
      <div class="cfg-row" id="decisionLoadMoreRow" hidden>
        <button class="mode-chip" id="newDecLoadMore">LOAD MORE</button>
      </div>
      <div id="decisionDrawer" class="new-decision-drawer" hidden></div>
    `;
  }

  async _fetchDecisions(reset = true) {
    if (!this._hass) return;
    if (reset) { this._decisions = []; this._decisionsCursor = null; }
    const container = this.shadowRoot?.getElementById("decisionEntries");
    if (container && reset) container.innerHTML = `<div class="stub-body">Loading…</div>`;
    try {
      const args = { type: "nova/list_decisions", limit: 50, only_unjudged: !!this._decisionsUnjudgedOnly };
      if (!reset && this._decisionsCursor) {
        args.cursor_ts = this._decisionsCursor.ts;
        args.cursor_id = this._decisionsCursor.id;
      }
      const result = await this._hass.callWS(args);
      this._decisions = reset ? (result.decisions || []) : (this._decisions || []).concat(result.decisions || []);
      this._decisionsCursor = result.next_cursor || null;
      this._renderDecisionRows();
    } catch (err) {
      if (container) container.innerHTML = `<div class="new-log-entry-error" style="padding:12px">Error loading decisions: ${this._esc(err)}</div>`;
    }
  }

  _renderDecisionRows() {
    const container = this.shadowRoot?.getElementById("decisionEntries");
    if (!container) return;
    const entries = this._decisions || [];
    const countEl = this.shadowRoot?.getElementById("newDecCount");
    if (countEl) countEl.textContent = `${entries.length} decision(s) loaded`;
    container.innerHTML = entries.length ? entries.map(d => {
      const outcomeCls = d.outcome === "good" ? "diag-ok" : d.outcome === "wrong" ? "diag-down"
        : d.outcome === "unnecessary" ? "diag-warn" : "diag-idle";
      const outcomeLabel = d.outcome ? d.outcome.toUpperCase() : "UNJUDGED";
      const when = d.ts ? new Date(d.ts * 1000).toLocaleString() : "";
      return `<div class="new-log-entry new-decision-row" data-id="${this._esc(d.id)}">
          <span class="new-log-ts">${this._esc(when)}</span>
          <span class="new-log-cat">${this._esc((d.kind || "").toUpperCase())}</span>
          <span class="new-log-msg">${this._esc(d.decision || "")}</span>
          <span class="${outcomeCls}">${this._esc(outcomeLabel)}</span>
        </div>`;
    }).join("") : `<div class="stub-body">No decisions recorded yet.</div>`;
    const loadMoreRow = this.shadowRoot?.getElementById("decisionLoadMoreRow");
    if (loadMoreRow) loadMoreRow.hidden = !this._decisionsCursor;
    container.querySelectorAll(".new-decision-row").forEach(row => {
      row.addEventListener("click", () => this._openDecisionDetail(parseInt(row.getAttribute("data-id"), 10)));
    });
  }

  _decisionBlockHtml(label, value) {
    const text = value === null || value === undefined || value === ""
      ? "—" : (typeof value === "object" ? JSON.stringify(value, null, 2) : String(value));
    return `<div class="mode-bind-head">${this._esc(label)}</div>
      <pre class="new-sug-yaml" style="white-space:pre-wrap;font-family:var(--font-mono);font-size:10.5px;color:var(--ink-dim);background:var(--surface-2);border:1px solid var(--line-soft);border-radius:8px;padding:10px;margin:0 0 8px">${this._esc(text)}</pre>`;
  }

  async _openDecisionDetail(id) {
    const drawer = this.shadowRoot?.getElementById("decisionDrawer");
    if (!drawer) return;
    drawer.hidden = false;
    drawer.innerHTML = `<div class="stub-body">Loading…</div>`;
    try {
      const result = await this._hass.callWS({ type: "nova/get_decision", decision_id: id });
      const d = result.decision || {};
      const judged = !!d.outcome;
      const row = (label, value) => `<div class="cfg-row"><label>${this._esc(label)}</label><span>${this._esc(
        value === null || value === undefined || value === "" ? "—" : String(value))}</span></div>`;
      drawer.innerHTML = `
        <div class="panel-head"><div class="panel-title">Decision #${this._esc(d.id)}</div>
          <button class="mode-chip" id="newDecCloseDrawer">CLOSE</button></div>
        ${row("Route", d.kind)}
        ${row("Decision", d.decision)}
        ${row("Reason", d.reason)}
        ${row("Confidence", d.confidence)}
        ${row("Model", d.model)}
        ${row("Tokens", d.tokens)}
        ${row("Latency (ms)", d.latency_ms)}
        ${row("Outcome", d.outcome)}
        ${this._decisionBlockHtml("Observation", d.observation)}
        ${this._decisionBlockHtml("Interpretation", d.interpretation)}
        ${this._decisionBlockHtml("Evidence", d.evidence)}
        <div class="mode-bind-head">Feedback</div>
        <div class="mode-grid">
          <button class="mode-chip new-dec-fb" data-verdict="good" data-id="${this._esc(d.id)}" ${judged ? "disabled" : ""}>HELPFUL</button>
          <button class="mode-chip new-dec-fb" data-verdict="unnecessary" data-id="${this._esc(d.id)}" ${judged ? "disabled" : ""}>UNNECESSARY</button>
          <button class="mode-chip new-dec-fb" data-verdict="wrong" data-id="${this._esc(d.id)}" ${judged ? "disabled" : ""}>WRONG</button>
        </div>
        <div class="toggle-desc" id="newDecFbStatus">${judged ? `Already judged: ${this._esc(d.outcome)}` : ""}</div>
        <div class="mode-bind-head">Decision Lab</div>
        <div class="cfg-row"><button class="mode-chip" id="newDecReplay" data-id="${this._esc(d.id)}">REPLAY</button></div>
        <div id="newDecReplayResult"></div>
      `;
      drawer.querySelector("#newDecCloseDrawer")?.addEventListener("click", () => {
        drawer.hidden = true; drawer.innerHTML = "";
      });
      drawer.querySelectorAll(".new-dec-fb").forEach(btn => {
        btn.addEventListener("click", () => this._submitDecisionOutcome(
          parseInt(btn.getAttribute("data-id"), 10), btn.getAttribute("data-verdict")));
      });
      drawer.querySelector("#newDecReplay")?.addEventListener("click", () => this._replayDecision(d.id));
    } catch (err) {
      drawer.innerHTML = `<div class="new-log-entry-error" style="padding:12px">Error loading decision: ${this._esc(err)}</div>`;
    }
  }

  async _replayDecision(id) {
    const resultEl = this.shadowRoot?.getElementById("newDecReplayResult");
    if (resultEl) resultEl.innerHTML = `<div class="stub-body">Replaying…</div>`;
    try {
      const r = await this._hass.callWS({ type: "nova/replay_decision", decision_id: id });
      if (!resultEl) return;
      const row = (label, value) => `<div class="cfg-row"><label>${this._esc(label)}</label><span>${this._esc(String(value))}</span></div>`;
      if (!r.supported) {
        resultEl.innerHTML = `
          <div class="toggle-desc" style="margin-top:8px"><b>${this._esc(r.label)}</b></div>
          <div class="stub-body">${this._esc(r.reason || "Not supported for this decision kind.")}</div>`;
        return;
      }
      resultEl.innerHTML = `
        <div class="toggle-desc" style="margin-top:8px"><b>${this._esc(r.label)}</b></div>
        ${row("Current suggestion threshold", r.current_threshold)}
        ${row("Would pass current threshold", r.would_pass_current_threshold ? "Yes" : "No")}
        ${row("Within 0.05 of threshold", r.within_0_05_of_threshold ? "Yes" : "No")}
      `;
    } catch (err) {
      if (resultEl) resultEl.innerHTML = `<div class="new-log-entry-error" style="padding:12px">Error running replay: ${this._esc(err)}</div>`;
    }
  }

  async _submitDecisionOutcome(id, verdict) {
    const statusEl = this.shadowRoot?.getElementById("newDecFbStatus");
    try {
      const result = await this._hass.callWS({ type: "nova/set_decision_outcome", decision_id: id, verdict });
      const disableButtons = () => this.shadowRoot?.querySelectorAll(".new-dec-fb")
        .forEach(b => b.setAttribute("disabled", "disabled"));
      if (result.status === "ok") {
        if (statusEl) statusEl.textContent = `Recorded: ${verdict}`;
        disableButtons();
        this._fetchDecisions(true);
      } else if (result.status === "already_judged") {
        if (statusEl) statusEl.textContent = "This decision was already judged.";
        disableButtons();
      } else {
        if (statusEl) statusEl.textContent = "Decision not found.";
      }
    } catch (err) {
      if (statusEl) statusEl.textContent = `Error: ${this._esc(err)}`;
    }
  }

  // Pure DOM-render step, given an already-fetched entries array — no
  // network I/O. Called both synchronously from cache (_wire(), on
  // re-entering the System Log view) and from _fetchDebugLog()'s network
  // result, so a cached view renders immediately and a background refresh
  // reuses the exact same render path. Keeps the existing signature-based
  // skip (avoids flicker/scroll-jump on the shared 20s poll) — safe now
  // that the container is never left holding a stale "Loading…" shell by
  // the time this runs, cache-rendered or freshly fetched alike.
  _renderDebugLogEntries(entries) {
    const container = this.shadowRoot?.getElementById("newLogEntries");
    if (!container) return;
    if (!entries || !entries.length) {
      container.innerHTML = `<div class="stub-body">No entries yet. Talk to Nova to generate log entries.</div>`;
      return;
    }
    const cc = NovaPanel.LOG_CATEGORIES;
    const activeFilter = this._logFilter || "all";
    const categoryFiltered = activeFilter === "all" ? entries : entries.filter(e => e.cat === activeFilter);
    const search = (this._logSearch || "").trim().toLowerCase();
    const filtered = search
      ? categoryFiltered.filter(e => (e.msg || "").toLowerCase().includes(search) || (e.cat || "").toLowerCase().includes(search))
      : categoryFiltered;

    const countEl = this.shadowRoot?.getElementById("newLogCount");
    if (countEl) {
      countEl.textContent = search || activeFilter !== "all"
        ? `${filtered.length} of ${entries.length}`
        : `${entries.length} entries`;
    }

    const ordered = filtered.slice().reverse();

    // Skip the rebuild when nothing changed (same signature trick as
    // Classic) — avoids flicker/scroll-jump on the shared 20s poll. The
    // signature is stamped on the CONTAINER ELEMENT itself (dataset), not
    // kept as component-instance state: _render() tears down and rebuilds
    // #newLogEntries from scratch on every tab/view switch, so a fresh
    // container's dataset is naturally unstamped and this always proceeds
    // to render — instance-level state would instead persist a stale
    // "already rendered" signature across the rebuild and skip the render
    // that was needed to replace the shell's "Loading…" placeholder (the
    // exact bug this replaced).
    const first = ordered[0];
    const last = ordered[ordered.length - 1];
    const sig = ordered.length + "|" + (first ? first.ts + first.msg : "") + "|" + (last ? last.ts + last.msg : "");
    const renderSig = sig + " " + activeFilter + " " + search;
    if (renderSig === container.dataset.renderSig) {
      return;
    }
    const filterChanged = activeFilter !== container.dataset.renderFilter || search !== container.dataset.renderSearch;
    const nearTop = container.scrollTop < 40;
    const prevTop = container.scrollTop;

    container.innerHTML = ordered.length ? ordered.map(e => {
      const cat = cc[e.cat] || { color: "var(--ink-dim)", icon: "•" };
      const isError = e.cat === "ERROR" || (e.msg || "").toLowerCase().includes("error") || (e.msg || "").toLowerCase().includes("failed");
      const safeCat = this._esc(e.cat);
      return `<div class="new-log-entry${isError ? " new-log-entry-error" : ""}">
        <span class="new-log-ts">${this._esc(e.ts)}</span>
        <span class="new-log-cat" style="color:${cat.color}">${cat.icon} ${safeCat}</span>
        <span class="new-log-msg">${this._esc(e.msg)}</span>
      </div>`;
    }).join("") : `<div class="stub-body">No entries match${search ? ` "${this._esc(search)}"` : ""}${activeFilter !== "all" ? ` in ${activeFilter}` : ""}.</div>`;

    container.dataset.renderSig = renderSig;
    container.dataset.renderFilter = activeFilter;
    container.dataset.renderSearch = search;

    container.scrollTop = (filterChanged || nearTop) ? 0 : prevTop;
  }

  async _fetchDebugLog() {
    if (!this._hass) return;
    // Repeated navigation back into System Log must not pile up concurrent
    // duplicate requests — the in-flight one will render whatever it finds
    // in #newLogEntries when it resolves, same as any other stale-view
    // guard here (container lookup by id, below).
    if (this._debugLogFetchInFlight) return;
    this._debugLogFetchInFlight = true;
    try {
      const result = await this._hass.callWS({ type: "nova/get_debug_log" });
      const entries = result?.entries || [];
      this._debugLogEntries = entries;
      this._renderDebugLogEntries(entries);
    } catch (err) {
      // Fail-open: a background refresh failure must never discard rows
      // already on screen. Only show the error state when there's nothing
      // usable cached to fall back on (a genuine first-load failure).
      if (!this._debugLogEntries || !this._debugLogEntries.length) {
        const c = this.shadowRoot?.getElementById("newLogEntries");
        if (c) c.innerHTML = `<div class="new-log-entry-error" style="padding:12px">Error loading logs: ${this._esc(err)}</div>`;
      } else {
        console.warn("Nova: System Log refresh failed, keeping cached entries", err);
      }
    } finally {
      this._debugLogFetchInFlight = false;
    }
  }

  _wireLogs() {
    const root = this.shadowRoot;
    root.querySelectorAll(".new-logview").forEach(btn => {
      btn.addEventListener("click", () => {
        this._logView = btn.getAttribute("data-view");
        this._render();
      });
    });
    if ((this._logView || "system") === "decisions") {
      const unjudgedBtn = root.getElementById("newDecUnjudged");
      if (unjudgedBtn) {
        unjudgedBtn.addEventListener("click", () => {
          this._decisionsUnjudgedOnly = !this._decisionsUnjudgedOnly;
          this._render();
        });
      }
      const loadMoreBtn = root.getElementById("newDecLoadMore");
      if (loadMoreBtn) loadMoreBtn.addEventListener("click", () => this._fetchDecisions(false));
      return;
    }
    if ((this._logView || "system") === "spoken_history") return;
    if ((this._logView || "system") === "actions") {
      const loadMoreBtn = root.getElementById("newActionLoadMore");
      if (loadMoreBtn) loadMoreBtn.addEventListener("click", () => this._fetchActions(false));
      return;
    }
    root.querySelectorAll(".new-log-filter").forEach(btn => {
      btn.addEventListener("click", () => {
        this._logFilter = btn.getAttribute("data-filter");
        root.querySelectorAll(".new-log-filter").forEach(b => b.classList.toggle("mode-chip-on", b === btn));
        this._fetchDebugLog();
      });
    });
    const logSearch = root.getElementById("newLogSearch");
    if (logSearch) {
      logSearch.addEventListener("input", (e) => {
        clearTimeout(this._logSearchDebounce);
        const val = e.currentTarget.value;
        this._logSearchDebounce = setTimeout(() => {
          this._logSearch = val;
          this._fetchDebugLog();
        }, 200);
      });
    }
  }

  // ─── Memory ───────────────────────────────────────────────────────────
  // Ported from Classic's own Memory tab (nova-panel.js): curated facts
  // ("What Nova Knows" + TEACH form), a Pending Confirmation queue for
  // facts staged via "remember that…" but not yet approved, and Person
  // Routines (habits confidently attributed to one person). Each section
  // patches its own container after a fetch/action rather than doing a
  // full _render() — the TEACH inputs are free text the user may be
  // mid-typing, and a full re-render would wipe them the same way it
  // would for AI Models/Cameras/Appliances.
  _htmlMemory() {
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">What Nova Knows</div>
            <div class="panel-meta" id="newMemCount">—</div>
          </div>
          <div class="stub-body">Durable facts &amp; preferences Nova recalls in conversation. Teach it something, or forget anything with ✕.</div>
          <div class="cfg-row cfg-row-wrap">
            <input id="newMemKey" class="cfg-field" style="flex:1" placeholder="what (e.g. trash day)" autocomplete="off">
            <input id="newMemVal" class="cfg-field" style="flex:1" placeholder="is (e.g. Tuesday)" autocomplete="off">
            <select id="newMemSubject" class="cfg-field">
              <option value="household">Household</option>
              <option value="primary">About me</option>
            </select>
            <button class="mode-chip" id="newMemAdd">TEACH</button>
          </div>
          <div id="newMemList" class="mem-body"><div class="stub-body">Loading…</div></div>
        </div>

        <div class="panel" id="newPendingPanel" hidden>
          <div class="panel-head">
            <div class="panel-title">Pending Confirmation</div>
            <div class="panel-meta" id="newPendingCount">—</div>
          </div>
          <div class="stub-body">Nova proposed these while talking with you — from "remember that…" — but nobody confirmed them yet, so they aren't trusted or used in conversation until you approve, edit, or reject them here.</div>
          <div id="newPendingList" class="mem-body"></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Person Routines</div>
            <div class="panel-meta">Learned</div>
          </div>
          <div class="stub-body">Habits Nova has confidently attributed to one person, from 30 days of sole-occupant activity — separate from household-wide facts above.</div>
          <div id="newProutineList" class="mem-body"><div class="stub-body">Loading…</div></div>
        </div>
    `;
  }

  async _fetchKnowledge() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/get_knowledge" });
      this._knowledge = { facts: res?.facts || [], pending: res?.pending || [], stats: res?.stats || {} };
    } catch (err) {
      this._knowledge = { facts: [], pending: [], stats: {}, error: String(err) };
    }
    this._knowledgeLoaded = true;
    this._renderKnowledgeList();
    this._renderPendingFacts();
  }

  async _fetchPersonRoutines() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/get_person_routines" });
      this._personRoutines = { groups: res?.routines || {} };
    } catch (err) {
      this._personRoutines = { groups: {}, error: String(err) };
    }
    this._personRoutinesLoaded = true;
    this._renderPersonRoutines();
  }

  _renderPersonRoutines() {
    const list = this.shadowRoot?.getElementById("newProutineList");
    if (!list) return;
    const groups = this._personRoutines?.groups || {};
    const people = Object.keys(groups).sort();
    if (this._personRoutines?.error) {
      list.innerHTML = `<div class="stub-body">Couldn't load routines — ${this._esc(this._personRoutines.error)}</div>`;
      return;
    }
    if (!people.length) {
      list.innerHTML = this._personRoutinesLoaded
        ? `<div class="stub-body">Nothing person-specific learned yet — Nova needs a few weeks of sole-occupant data before routines are confidently individual.</div>`
        : `<div class="stub-body">Loading…</div>`;
      return;
    }
    list.innerHTML = people.map(person => {
      const items = groups[person]
        .slice().sort((a, b) => (b.confidence || 0) - (a.confidence || 0))
        .map(r => {
          const pct = Math.round((r.confidence || 0) * 100);
          return `
            <div class="cfg-row">
              <label>${this._esc(r.description)}</label>
              <span class="toggle-desc">${pct}% · ×${r.occurrences || "?"}</span>
            </div>`;
        }).join("");
      const label = this._esc(person.replace(/_/g, " ")).replace(/\b\w/g, c => c.toUpperCase());
      return `<div class="mode-bind-head">${label}</div>${items}`;
    }).join("");
  }

  _renderKnowledgeList() {
    const list = this.shadowRoot?.getElementById("newMemList");
    if (!list) return;
    const facts = this._knowledge?.facts || [];
    const count = this.shadowRoot?.getElementById("newMemCount");
    if (count) count.textContent = facts.length + (facts.length === 1 ? " fact" : " facts");
    if (this._knowledge?.error) {
      list.innerHTML = `<div class="stub-body">Couldn't load memory — ${this._esc(this._knowledge.error)}</div>`;
      return;
    }
    if (!facts.length) {
      list.innerHTML = this._knowledgeLoaded
        ? `<div class="stub-body">Nothing yet. Say "remember that…" to Nova, or teach it above.</div>`
        : `<div class="stub-body">Loading…</div>`;
      return;
    }
    const groups = {};
    facts.forEach(f => { (groups[f.subject] = groups[f.subject] || []).push(f); });
    const labels = { household: "Household", primary: "About me" };
    const order = Object.keys(groups).sort(
      (a, b) => (a === "household" ? -1 : b === "household" ? 1 : a.localeCompare(b)));
    list.innerHTML = order.map(subj => {
      const items = groups[subj].map(f => {
        const soft = (f.source !== "stated" || (f.confidence ?? 1) < 0.9);
        const hedge = soft
          ? `<span title="${this._esc(f.source)} · ${Math.round((f.confidence ?? 1) * 100)}% sure">~</span>`
          : "";
        const exp = f.expires_at ? `<span title="expires">⌛</span>` : "";
        return `
          <div class="cfg-row" data-id="${f.id}">
            <label>${this._esc(f.key)}</label>
            <div style="display:flex;align-items:center;gap:6px">
              <span class="toggle-desc">${this._esc(f.value)}${hedge}${exp}</span>
              <button class="new-mem-forget" data-id="${f.id}" title="Forget this" aria-label="Forget">✕</button>
            </div>
          </div>`;
      }).join("");
      const label = labels[subj] || this._esc(subj.replace(/_/g, " "));
      return `<div class="mode-bind-head">${label}</div>${items}`;
    }).join("");
    list.querySelectorAll(".new-mem-forget").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const id = parseInt(e.currentTarget.getAttribute("data-id"), 10);
        if (!isNaN(id)) this._forgetKnowledge(id);
      });
    });
  }

  async _teachKnowledge() {
    const root = this.shadowRoot;
    if (!root || !this._hass) return;
    const keyEl = root.getElementById("newMemKey");
    const valEl = root.getElementById("newMemVal");
    const subjEl = root.getElementById("newMemSubject");
    const key = (keyEl?.value || "").trim();
    const value = (valEl?.value || "").trim();
    const subject = subjEl?.value || "household";
    if (!key || !value) return;
    try {
      const res = await this._hass.callWS({
        type: "nova/add_knowledge", key, value, subject,
        kind: subject === "primary" ? "preference" : "fact",
      });
      this._knowledge = { facts: res?.facts || [], pending: this._knowledge.pending, stats: this._knowledge.stats };
      if (keyEl) keyEl.value = "";
      if (valEl) valEl.value = "";
      if (keyEl) keyEl.focus();
    } catch (err) { console.error("Nova: teach failed", err); }
    this._knowledgeLoaded = true;
    this._renderKnowledgeList();
  }

  async _forgetKnowledge(id) {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/forget_knowledge", fact_id: id });
      this._knowledge = { facts: res?.facts || [], pending: this._knowledge.pending, stats: this._knowledge.stats };
    } catch (err) { console.error("Nova: forget failed", err); }
    this._renderKnowledgeList();
  }

  async _pendingFactAction(id, action) {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/pending_fact_action", fact_id: id, action });
      this._knowledge = { facts: res?.facts || this._knowledge.facts, pending: res?.pending || [], stats: this._knowledge.stats };
    } catch (err) { console.error(`Nova: ${action} failed`, err); }
    this._renderKnowledgeList();
    this._renderPendingFacts();
  }

  async _editPendingFact(id, value) {
    if (!this._hass || !value) return;
    try {
      const res = await this._hass.callWS({ type: "nova/edit_pending_fact", fact_id: id, value });
      this._knowledge = { facts: this._knowledge.facts, pending: res?.pending || [], stats: this._knowledge.stats };
    } catch (err) { console.error("Nova: edit pending fact failed", err); }
    this._renderPendingFacts();
  }

  _renderPendingFacts() {
    const root = this.shadowRoot;
    const panel = root?.getElementById("newPendingPanel");
    const list = root?.getElementById("newPendingList");
    const countEl = root?.getElementById("newPendingCount");
    if (!panel || !list) return;
    const pending = this._knowledge?.pending || [];
    panel.hidden = pending.length === 0;
    if (!pending.length) { list.innerHTML = ""; return; }
    if (countEl) countEl.textContent = pending.length + (pending.length === 1 ? " waiting" : " waiting");
    list.innerHTML = pending.map(f => `
      <div class="cfg-row" data-id="${f.id}">
        <label>${this._esc(f.key)}</label>
        <input class="cfg-field new-pending-edit-val" style="flex:1" data-id="${f.id}" value="${this._esc(f.value)}">
      </div>
      <div class="mode-grid" style="margin-bottom:10px">
        <button class="mode-chip new-pending-confirm" data-id="${f.id}">✓ Confirm</button>
        <button class="mode-chip new-pending-reject" data-id="${f.id}">✕ Reject</button>
        <button class="mode-chip new-pending-save-edit" data-id="${f.id}">💾 Save edit</button>
      </div>`).join("");
    list.querySelectorAll(".new-pending-confirm").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const id = parseInt(e.currentTarget.getAttribute("data-id"), 10);
        if (!isNaN(id)) this._pendingFactAction(id, "confirm");
      });
    });
    list.querySelectorAll(".new-pending-reject").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const id = parseInt(e.currentTarget.getAttribute("data-id"), 10);
        if (!isNaN(id)) this._pendingFactAction(id, "reject");
      });
    });
    list.querySelectorAll(".new-pending-save-edit").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const id = parseInt(e.currentTarget.getAttribute("data-id"), 10);
        const input = list.querySelector(`.new-pending-edit-val[data-id="${id}"]`);
        const value = (input?.value || "").trim();
        if (!isNaN(id) && value) this._editPendingFact(id, value);
      });
    });
  }

  _wireMemory() {
    const root = this.shadowRoot;
    const memAdd = root.getElementById("newMemAdd");
    if (memAdd) {
      memAdd.addEventListener("click", () => this._teachKnowledge());
      ["newMemKey", "newMemVal"].forEach(id => {
        const el = root.getElementById(id);
        if (el) el.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); this._teachKnowledge(); }
        });
      });
    }
    this._renderKnowledgeList();
    this._renderPendingFacts();
    this._renderPersonRoutines();
  }

  // ─── Intrusion ────────────────────────────────────────────────────────
  // Ported from Classic's own Intrusion tab (nova-panel.js). Safety-relevant
  // (call-off/acknowledge affect real escalation), so this is a straight
  // port of Classic's exact websocket calls and semantics — no new
  // behavior invented here. The timeout select and vision-confirm toggle
  // reuse the generic .cfg-field/.toggle-btn autosave already wired for
  // Settings (see _saveSetting's dashboard-only render guard above).
  _htmlIntrusion() {
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Intrusion</div>
            <div class="panel-meta" id="newIntrStatus">—</div>
          </div>
          <div class="stub-body">Last intrusion snapshot and false-alarm call-off. When Nova confirms an intruder on camera it grabs a still; if it's not real, call it off here or say "it's a false alarm".</div>
          <div id="newIntrBody"><div class="stub-body">Loading…</div></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Intrusion Log</div>
            <div class="panel-meta" id="newIlogLearn">—</div>
          </div>
          <div class="stub-body">Every intrusion event with its snapshot. Mark each one <b>real</b> or <b>false alarm</b> — Nova learns from your labels and stops firing the low-confidence alerts for patterns you keep calling false. A confirmed intrusion always alerts, no matter what it has learned.</div>
          <div class="cfg-row"><button class="mode-chip" id="newIlogRefresh">⟳ REFRESH</button></div>
          <div id="newIlogBody"><div class="stub-body">Loading…</div></div>
        </div>
    `;
  }

  async _fetchIntrusion() {
    if (!this._hass) return;
    try {
      const cfg = this._liveData?.config || {};
      if (cfg.intrusion_response_timeout != null) this._intrTimeout = cfg.intrusion_response_timeout;
    } catch (_) {}
    try {
      this._intr = await this._hass.callWS({ type: "nova/intrusion", action: "status" });
    } catch (_) {
      this._intr = { error: true };
    }
    this._renderIntrusionStatus();
  }

  _renderIntrusionStatus() {
    const root = this.shadowRoot;
    const body = root?.getElementById("newIntrBody");
    const statusEl = root?.getElementById("newIntrStatus");
    if (!body) return;
    const s = this._intr || {};
    if (s.error) {
      body.innerHTML = `<div class="stub-body">Couldn't load — restart Home Assistant after updating.</div>`;
      if (statusEl) statusEl.textContent = "—";
      return;
    }
    if (statusEl) {
      statusEl.innerHTML = s.called_off
        ? `<span class="diag-warn">CALLED OFF · ${s.suppressed_for}s</span>`
        : `<span class="diag-ok">ARMED</span>`;
    }
    const snap = s.last_snapshot;
    let html = "";
    if (snap && snap.image_b64) {
      const when = snap.ts ? new Date(snap.ts * 1000).toLocaleString() : "";
      html += `<div class="intr-snap">
        <img src="data:image/jpeg;base64,${snap.image_b64}" alt="intrusion snapshot" class="intr-img">
        <div class="toggle-desc">${this._esc((snap.camera || "").replace("camera.", "").replace(/_/g, " "))} · ${this._esc(when)}</div>
      </div>`;
    } else {
      html += `<div class="stub-body">No intrusion snapshots captured. This stays empty unless Nova confirms an intruder on camera.</div>`;
    }
    if (s.false_alarms_24h) {
      html += `<div class="stub-body">${s.false_alarms_24h} false alarm${s.false_alarms_24h === 1 ? "" : "s"} called off in the last 24h</div>`;
    }
    if (s.acknowledged) {
      html += `<div class="stub-body">✓ Acknowledged — automatic escalation held (you're handling it)</div>`;
    }
    html += `
      <div class="cfg-row">
        <label>Auto-escalate if no response after</label>
        <select class="cfg-field" data-cfg-key="intrusion_response_timeout">${this._optSelect([["60", "1 min"], ["120", "2 min"], ["180", "3 min"], ["300", "5 min"], ["600", "10 min"]], String(this._intrTimeout || 120))}</select>
      </div>
      <div class="cfg-row">
        <label>Confirm Frigate person with Nova vision before alarming</label>
        <button class="toggle-btn ${this._liveData?.config?.intrusion_vision_confirm !== false ? "on" : "off"}" data-cfg-key="intrusion_vision_confirm" data-cfg-val="${this._liveData?.config?.intrusion_vision_confirm !== false ? "false" : "true"}">${this._liveData?.config?.intrusion_vision_confirm !== false ? "ON" : "OFF"}</button>
      </div>
      <div class="mode-grid">
        <button class="mode-chip new-intr-ack">✓ I'M LOOKING (HOLD)</button>
        <button class="mode-chip new-intr-dismiss">✕ CALL OFF (FALSE ALARM)</button>
      </div>`;
    body.innerHTML = html;
    body.querySelectorAll(".toggle-btn[data-cfg-key], select.cfg-field[data-cfg-key]").forEach(el => {
      if (el.tagName === "BUTTON") {
        el.addEventListener("click", () => this._saveSetting(el.getAttribute("data-cfg-key"), el.getAttribute("data-cfg-val") === "true"));
      } else {
        el.addEventListener("change", () => this._saveSetting(el.getAttribute("data-cfg-key"), el.value));
      }
    });
    const dismissBtn = body.querySelector(".new-intr-dismiss");
    dismissBtn?.addEventListener("click", async () => {
      if (!this._hass) return;
      dismissBtn.disabled = true;
      try {
        await this._hass.callWS({ type: "nova/intrusion", action: "dismiss", reason: "panel" });
        await this._fetchIntrusion();
      } catch (err) {
        console.error("Nova: intrusion dismiss failed", err);
        dismissBtn.disabled = false;
      }
    });
    const ackBtn = body.querySelector(".new-intr-ack");
    ackBtn?.addEventListener("click", async () => {
      if (!this._hass) return;
      ackBtn.disabled = true;
      try {
        await this._hass.callWS({ type: "nova/intrusion", action: "acknowledge", reason: "panel" });
        await this._fetchIntrusion();
      } catch (err) {
        console.error("Nova: intrusion acknowledge failed", err);
        ackBtn.disabled = false;
      }
    });
  }

  async _fetchIntrusionLog() {
    const root = this.shadowRoot;
    const body = root?.getElementById("newIlogBody");
    const side = root?.getElementById("newIlogLearn");
    if (!this._hass || !body) return;
    try {
      const res = await this._hass.callWS({ type: "nova/intrusion", action: "log", limit: 40 });
      this._ilog = res;
      const L = res?.learning || {};
      if (side) side.textContent = `${L.labeled || 0}/${L.events || 0} labelled`;
      body.innerHTML = this._renderIntrusionLogHtml(res);
      this._wireIntrusionLabels();
    } catch (err) {
      body.innerHTML = `<div class="stub-body">Could not load the log.</div>`;
    }
  }

  _renderIntrusionLogHtml(res) {
    const evs = (res && res.events) || [];
    if (!evs.length) {
      return `<div class="stub-body">No intrusion events recorded yet.</div>`;
    }
    const damped = ((res.learning || {}).damped_patterns || []).length;
    let html = "";
    if (damped) {
      html += `<div class="stub-body">Nova has learned ${damped} benign pattern${damped === 1 ? "" : "s"} — low-confidence alerts for these stay quiet.</div>`;
    }
    for (const e of evs) {
      const when = new Date((e.ts || 0) * 1000).toLocaleString();
      const kindCls = { confirmed: "diag-down", unresolved: "diag-warn", investigating: "diag-off" }[e.kind] || "diag-off";
      const label = e.label || "";
      html += `<div class="new-ilog-item" data-ev="${this._esc(e.id)}">
        <div class="cfg-row">
          <span class="${kindCls}">${this._esc((e.kind || "").toUpperCase())}</span>
          <span class="toggle-desc">${this._esc(when)}</span>
        </div>
        <div class="toggle-desc">${this._esc(e.breach || e.camera || "activity")}${e.reason ? " — " + this._esc(e.reason) : ""}</div>
        ${e.image_b64 ? `<img class="intr-img" src="data:image/jpeg;base64,${e.image_b64}" alt="snapshot">` : ""}
        <div class="mode-grid">
          <button class="mode-chip new-ilog-btn${label === "real" ? " mode-chip-on" : ""}" data-label="real">REAL</button>
          <button class="mode-chip new-ilog-btn${label === "false" ? " mode-chip-on" : ""}" data-label="false">FALSE ALARM</button>
          ${label ? `<button class="mode-chip new-ilog-btn" data-label="">CLEAR</button>` : ""}
        </div>
      </div>`;
    }
    return html;
  }

  _wireIntrusionLabels() {
    const body = this.shadowRoot?.getElementById("newIlogBody");
    body?.querySelectorAll(".new-ilog-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const item = btn.closest(".new-ilog-item");
        const id = item?.getAttribute("data-ev");
        if (!id || !this._hass) return;
        try {
          await this._hass.callWS({ type: "nova/intrusion", action: "label", event_id: id, label: btn.getAttribute("data-label") });
          await this._fetchIntrusionLog();
        } catch (err) { console.error("Nova: intrusion label failed", err); }
      });
    });
  }

  _wireIntrusion() {
    const root = this.shadowRoot;
    this._fetchIntrusion();
    const refreshBtn = root.getElementById("newIlogRefresh");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", async () => {
        refreshBtn.disabled = true;
        const orig = refreshBtn.textContent;
        refreshBtn.textContent = "⟳ LOADING…";
        try { await this._fetchIntrusionLog(); }
        finally { refreshBtn.disabled = false; refreshBtn.textContent = orig; }
      });
    } else {
      this._fetchIntrusionLog();
    }
  }

  // ─── Suggestions ──────────────────────────────────────────────────────
  // Ported from Classic's own Suggestions tab. Data rides on the same
  // nova/get_panel_data payload the dashboard already polls (d.suggestions)
  // — no separate fetch. Approve/dismiss dim the card in place rather than
  // removing it or re-fetching, matching Classic's own lightweight pattern.
  static SUGGESTION_TYPE_LABEL = {
    time_routine: "Daily routine", sequence: "Action sequence",
    repeated_command: "Repeated command", temp_pref: "Temperature",
    presence: "Presence", numeric_trigger: "Sensor threshold",
  };

  _htmlSuggestions() {
    const sugs = this._data()?.suggestions || [];
    if (!sugs.length) {
      return `
        ${this._htmlAutomationInventory()}
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Learned Opportunities</div></div>
          <div class="stub-body">No suggestions right now. Nova proposes automations as it notices routines repeat — a light you turn on each evening, a scene after a button press, the heat when it's cold. As patterns build up, they'll appear here for you to review and approve. Nothing is ever created without your say-so.</div>
          <div class="mode-grid"><button class="mode-chip" id="sugRunAnalysis">Analyze Now</button></div>
          <div class="toggle-desc" id="sugAnalysisResult" style="margin-top:8px">See why nothing has qualified yet, or force a fresh pass over your history.</div>
        </div>
        ${this._htmlAutomationTrials()}`;
    }
    const rows = sugs.map(s => {
      const pct = Math.round((s.confidence || 0) * 100);
      const confColor = pct >= 80 ? "#5fbf7a" : pct >= 55 ? "var(--warn)" : "var(--ink-faint)";
      const label = NovaPanel.SUGGESTION_TYPE_LABEL[s.pattern_type] || "Learned pattern";
      const evidence = (s.evidence || []).map(e => `<li>${this._esc(e)}</li>`).join("");
      const entities = (s.entities || []).length
        ? `<div class="mode-grid">${(s.entities || []).map(e => `<span class="area-cap" style="width:auto;padding:3px 8px;font-family:var(--font-mono);font-size:10px">${this._esc(e)}</span>`).join("")}</div>`
        : "";
      const match = s.automation_match || {};
      // The backend names the automations it matched in match.matches.
      const matched = (match.matches || [])[0] || {};
      const matchName = matched.name || matched.entity_id || "an existing automation";
      let overlap = "";
      if (match.status === "possible_overlap") {
        overlap = `<div class="stub-body" style="color:var(--warn)">⚠ Possible overlap with <b>${this._esc(matchName)}</b>. Review both before creating this automation.</div>`;
      } else if (match.status === "unknown_overlap") {
        overlap = `<div class="stub-body" style="color:var(--warn)">⚠ ${(match.matches || []).length ? `<b>${this._esc(matchName)}</b> may control the same device, but Nova cannot fully compare its blueprint or template.` : "Nova cannot fully compare this automation's template or blueprint with existing automations."}</div>`;
      } else if (match.status === "inventory_unavailable") {
        overlap = `<div class="stub-body" style="color:var(--warn)">⚠ Nova could not check existing automations. Review Home Assistant before creating this one.</div>`;
      }
      return `
        <div class="panel new-sug" data-sug-id="${s.id}">
          <div class="panel-head">
            <div class="panel-title">${this._esc(label)}</div>
            <div class="panel-meta" style="color:${confColor}">${pct}% confident</div>
          </div>
          ${s.why_headline ? `<div class="stub-body"><b>${this._esc(s.why_headline)}</b></div>` : ""}
          <div class="stub-body">${this._esc(s.description)}</div>
          ${overlap}
          ${evidence ? `<div class="mode-bind-head">What Nova observed</div><ul style="margin:0 0 10px;padding-left:18px;font-size:12px;color:var(--ink-dim);line-height:1.6">${evidence}</ul>` : ""}
          ${entities}
          <div class="cfg-row"><span class="toggle-desc">seen ${s.count || "?"}× in 30 days</span></div>
          <div class="mode-grid">
            <button class="mode-chip new-sug-approve">✓ Create automation</button>
            <button class="mode-chip new-sug-dismiss">✕ Dismiss</button>
            <button class="mode-chip new-sug-yaml-btn">⌄ See the automation</button>
          </div>
          <pre class="new-sug-yaml" hidden style="white-space:pre-wrap;font-family:var(--font-mono);font-size:10.5px;color:var(--ink-dim);background:var(--surface-2);border:1px solid var(--line-soft);border-radius:8px;padding:10px;margin-top:8px">${this._esc(s.yaml || "")}</pre>
        </div>`;
    }).join("");
    return `
      ${this._htmlAutomationInventory()}
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">Learned Opportunities</div>
          <div class="panel-meta">${sugs.length} suggestion${sugs.length === 1 ? "" : "s"} to review</div>
        </div>
        <div class="stub-body">Automations Nova has learned from watching your routines. Review each — approve to create it in Home Assistant, or dismiss it. Nothing runs until you approve, and you can see the exact automation before deciding.</div>
      </div>
      ${rows}
      ${this._htmlAutomationTrials()}`;
  }

  _htmlAutomationInventory() {
    const result = this._automationInventory;
    if (result === null) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div></div>
          <div class="stub-body">Couldn't load Home Assistant automations. Nova will not assume the list is empty.</div>
        </div>`;
    }
    if (result === undefined) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div></div>
          <div class="stub-body">Loading the automation inventory…</div>
        </div>`;
    }
    const automations = result.automations || [];
    if (!result.available) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div></div>
          <div class="stub-body">Nova's automation inventory is unavailable. Suggestions will be marked for manual review instead of assuming nothing exists.</div>
        </div>`;
    }
    if (!automations.length) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div><div class="panel-meta">0 found</div></div>
          <div class="stub-body">Home Assistant currently reports no loaded automations.</div>
        </div>`;
    }
    const rows = automations.map(a => {
      const status = a.enabled ? "enabled" : "disabled";
      const scope = a.understanding === "full" ? "fully understood"
        : a.understanding === "partial" ? "blueprint · partial comparison"
        : "metadata only";
      const triggered = a.last_triggered
        ? new Date(a.last_triggered).toLocaleString() : "never";
      const origin = a.origin === "nova" ? "created by Nova" : "existing";
      return `
        <div class="cfg-row">
          <label>${this._esc(a.name || a.entity_id)}</label>
          <span class="toggle-desc">${this._esc(status)} · ${this._esc(origin)} · ${this._esc(scope)} · last triggered ${this._esc(triggered)}</span>
        </div>`;
    }).join("");
    return `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">Existing Home Assistant Automations</div>
          <div class="panel-meta">${automations.length} loaded</div>
        </div>
        <div class="stub-body">Nova uses this read-only inventory to avoid relearning routines Home Assistant already handles. It refreshes at startup and whenever automations are reloaded.</div>
        ${rows}
      </div>`;
  }

  // ─── Automation probation (Phase 3) ──────────────────────────────────────
  // Installing a suggestion only means it was ACCEPTED — this section shows
  // what's actually been observed running since, entirely separate from that
  // acceptance. Run counts come from Home Assistant's own automation_triggered
  // event; "Working"/"Needs adjustment" is manual feedback only, never inferred.

  _htmlAutomationTrials() {
    const trials = this._automationTrials;
    if (trials === null) {
      return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Created by Nova</div>
          </div>
          <div class="stub-body">Couldn't load installed automations.</div>
        </div>`;
    }
    if (!trials || !trials.length) {
      return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Created by Nova</div>
          </div>
          <div class="stub-body">No tracked Nova automations yet. Automations installed from new suggestions will appear here.</div>
        </div>`;
    }
    const rows = trials.map(t => {
      const when = t.last_run ? new Date(t.last_run * 1000).toLocaleString() : "never";
      const outcome = t.manual_outcome
        ? `<span class="toggle-desc">Feedback: ${this._esc(t.manual_outcome === "working" ? "Working" : "Needs adjustment")}</span>`
        : "";
      return `
        <div class="cfg-row">
          <label>${this._esc(t.automation_id)}</label>
          <span class="toggle-desc">ran ${t.run_count || 0}× · last ${this._esc(when)}</span>
        </div>
        <div class="mode-grid" data-trial-id="${t.id}">
          <button class="mode-chip new-trial-fb" data-verdict="working">WORKING</button>
          <button class="mode-chip new-trial-fb" data-verdict="needs_adjustment">NEEDS ADJUSTMENT</button>
        </div>
        <div class="cfg-row">${outcome}</div>`;
    }).join("");
    return `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">Created by Nova</div>
          <div class="panel-meta">${trials.length} tracked</div>
        </div>
        <div class="stub-body">Installing an automation means you accepted the suggestion — it isn't proof the automation works. This shows what's actually been observed running; "Working" and "Needs adjustment" are your own call, not Nova's.</div>
        ${rows}
      </div>`;
  }

  async _fetchAutomationTrials() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "nova/list_automation_trials" });
      this._automationTrials = result.trials || [];
    } catch (_) { this._automationTrials = null; }
    if (this._currentTab === "suggestions") this._render();
  }

  async _fetchAutomationInventory() {
    if (!this._hass) return;
    try {
      this._automationInventory = await this._hass.callWS({
        type: "nova/list_automation_inventory",
      });
    } catch (_) { this._automationInventory = null; }
    if (this._currentTab === "suggestions") this._render();
  }

  async _submitAutomationTrialFeedback(trialId, verdict) {
    if (!this._hass) return;
    try {
      await this._hass.callWS({ type: "nova/automation_trial_feedback", trial_id: trialId, verdict });
      this._fetchAutomationTrials();
    } catch (err) {
      console.error("Nova: automation trial feedback failed", err);
    }
  }

  _wireSuggestions() {
    const root = this.shadowRoot;
    root.querySelectorAll(".new-sug").forEach(card => {
      const sid = parseInt(card.getAttribute("data-sug-id"), 10);
      const act = async (action) => {
        if (!this._hass || isNaN(sid)) return;
        const buttons = card.querySelectorAll("button");
        buttons.forEach(b => b.disabled = true);
        let res = null;
        try {
          res = await this._hass.callWS({ type: "nova/suggestion_action", suggestion_id: sid, action });
        } catch (err) {
          console.error(`Nova: suggestion ${action} failed`, err);
        }
        // Only a settled suggestion (installed, acknowledged, covered or
        // dismissed) greys out. A failed install stays pending on the
        // backend, so its buttons come back with the reason shown.
        if (res && res.ok) {
          card.style.opacity = "0.35";
          return;
        }
        buttons.forEach(b => b.disabled = false);
        let note = card.querySelector(".new-sug-status");
        if (!note) {
          note = document.createElement("div");
          note.className = "stub-body new-sug-status";
          note.style.color = "var(--warn)";
          card.querySelector(".new-sug-approve")?.parentElement?.before(note);
        }
        note.textContent = `⚠ ${(res && res.reason) || `Could not ${action} this suggestion. Try again.`}`;
      };
      card.querySelector(".new-sug-approve")?.addEventListener("click", () => act("approve"));
      card.querySelector(".new-sug-dismiss")?.addEventListener("click", () => act("dismiss"));
      card.querySelector(".new-sug-yaml-btn")?.addEventListener("click", () => {
        const pre = card.querySelector(".new-sug-yaml");
        if (pre) pre.hidden = !pre.hidden;
      });
    });
    root.querySelectorAll(".new-trial-fb").forEach(btn => {
      btn.addEventListener("click", () => {
        const group = btn.closest("[data-trial-id]");
        const trialId = parseInt(group?.getAttribute("data-trial-id"), 10);
        if (!isNaN(trialId)) this._submitAutomationTrialFeedback(trialId, btn.getAttribute("data-verdict"));
      });
    });
  }

  // ─── Settings ─────────────────────────────────────────────────────────
  // Reorganized around what you're trying to do rather than which Nova
  // subsystem it touches — the old Classic split (System Diagnostics
  // under General, a separate Diagnostics under Cameras) is merged here
  // into one place. Every card is "real:true" — nothing here is a stub.
  // Mirrors const.py's HONORIFIC_OPTIONS — kept in sync by hand, same as
  // AREA_CAP_ORDER/AREA_CAP_ICON below mirror their own backend source.
  static HONORIFIC_OPTIONS = ["sir", "ma'am", "boss", "friend"];

  static SETTINGS_GROUPS = [
    { id: "general", label: "General" },
    { id: "voice", label: "Voice & Speakers" },
    { id: "safety", label: "Awareness & Safety" },
    { id: "learning", label: "Learning & Memory" },
    { id: "cameras", label: "Cameras" },
    { id: "home", label: "Home & Extras" },
  ];

  static SETTINGS_CARDS = [
    { id: "general", group: "general", title: "General", real: true,
      desc: "Language, sleep state, and the core proactive-speech switches." },
    { id: "person_honorifics", group: "general", title: "Person Honorifics", real: true,
      desc: "What Nova calls each person when they're home alone. Drops the address entirely the moment more than one person — or nobody — is home." },
    { id: "residence_home", group: "general", title: "Residence / Home", real: true,
      desc: "Home style, stories, and layout counts that feed the Residence 3D view." },
    { id: "operational_mode", group: "general", title: "Operational Mode", real: true,
      desc: "Party/movie/away modes and what each one changes while active." },
    { id: "diagnostics", group: "general", title: "Diagnostics", real: true,
      desc: "Service health checks and system status (merged from Classic's two separate diagnostics cards)." },
    { id: "room_speakers", group: "voice", title: "Room Speakers", real: true,
      desc: "Assign the one speaker Nova may use per room, plus a general fallback." },
    { id: "ai_models", group: "voice", title: "AI Models", real: true,
      desc: "Provider and model per tier — main agent, classifier, reasoning, review, vision." },
    { id: "briefings", group: "voice", title: "Briefings", real: true,
      desc: "Daily briefing schedule, content, and delivery speakers." },
    { id: "voice_confirmation", group: "voice", title: "Voice Confirmation", real: true,
      desc: "Whether risky actions need a spoken or phone confirmation before Nova acts." },
    { id: "satellite_speaker", group: "voice", title: "Satellite → Speaker", real: true,
      desc: "Per-satellite override, for a specific satellite that shouldn't use its room's assigned speaker." },
    { id: "announcement_speakers", group: "voice", title: "Announcement Speakers", real: true,
      desc: "Which speakers whole-house broadcasts (briefings, sentinel alerts) use." },
    { id: "notifications", group: "safety", title: "Notifications", real: true,
      desc: "Your phone's notify service, for alerts when nobody's home to hear a speaker." },
    { id: "security_alarm", group: "safety", title: "Security Alarm", real: true,
      desc: "Choose the one alarm Nova uses for security decisions. Automatic lockdown is always opt in." },
    { id: "sentinel_rules", group: "safety", title: "Sentinel Rules", real: true,
      desc: "Enable or disable individual door/lock/garage anomaly rules." },
    { id: "hazard_monitor", group: "safety", title: "Hazard Monitor", real: true,
      desc: "Earthquake, severe weather, and disaster feeds near your home." },
    { id: "energy_management", group: "safety", title: "Energy Management", real: true,
      desc: "Peak-draw threshold and how much say Nova has over high-draw appliances." },
    { id: "host_health", group: "safety", title: "Host Health", real: true,
      desc: "Home Assistant's own System Monitor readings for the machine Nova runs on — off by default." },
    { id: "appliances", group: "safety", title: "Appliances", real: true,
      desc: "Declared appliance profiles Nova fingerprints by wattage." },
    { id: "anticipation_memory", group: "learning", title: "Anticipation & Memory", real: true,
      desc: "Cross-session memory window, continued conversation, and multi-satellite follow." },
    { id: "memory_curated", group: "learning", title: "Memory", real: true,
      desc: "Memory backend and how many memories are stored. Full review/edit lives on the Memory tab." },
    { id: "observer_tuning", group: "learning", title: "Observer Tuning", real: true,
      desc: "How cautious or talkative the proactive Observer is." },
    { id: "routine_learning", group: "learning", title: "Routine Learning", real: true,
      desc: "What Nova is allowed to learn from — doors, presence, button presses." },
    { id: "excluded_entities", group: "learning", title: "Excluded Entities", real: true,
      desc: "Entities, domains, or labels Nova should ignore entirely." },
    { id: "cameras", group: "cameras", title: "Cameras", real: true,
      desc: "Camera names, indoor/outdoor designation, and location overrides." },
    { id: "doorbell_training", group: "cameras", title: "Doorbell Training", real: true,
      desc: "Teach Nova to recognize regular visitors at the door." },
    { id: "floor_plan_editor", group: "home", title: "Floor Plan Editor", real: true,
      desc: "Rooms, outdoor zones, property line, windows/doors/dormers, camera placement, a background image, and AI camera-coverage estimation." },
    { id: "wellbeing_context", group: "home", title: "Wellbeing Context", real: true,
      desc: "Whether wearable heart-rate/sleep data reaches Nova, and which providers." },
    { id: "character_research", group: "home", title: "Nova Character & Research", real: true,
      desc: "Banter level and the web-research backend (DuckDuckGo or self-hosted SearXNG)." },
    { id: "document_library", group: "home", title: "Document Library", real: true,
      desc: "Manuals and receipts Nova can search and cite from." },
  ];

  // ─── Residence 3D (Phase A) ──────────────────────────────────────────────
  // Reuses the NOVA3D engine defined at the top of this file (window.NOVA3D)
  // rather than re-deriving the ~1000 lines of isometric-projection geometry
  // — this tab only shapes the small options object (spec/plan/elements/
  // garage/lit/doors/theta) NOVA3D expects, using the same floor_plan_rooms/
  // floor_plan_elements data this panel already reads for its own Floor
  // Plan Editor.
  _resStyles() {
    return {
      cape_cod: { label: "Cape Cod", roof: "gable", pitch: 1.0 },
      colonial: { label: "Colonial", roof: "gable", pitch: 0.7 },
      dutch_colonial: { label: "Dutch Colonial", roof: "gambrel", pitch: 0.6 },
      ranch: { label: "Ranch", roof: "hip", pitch: 0.5 },
      two_story: { label: "Two-Story", roof: "gable", pitch: 0.65 },
      craftsman: { label: "Craftsman", roof: "hip", pitch: 0.6 },
      modern: { label: "Modern", roof: "flat", pitch: 0 },
      townhouse: { label: "Townhouse", roof: "gable", pitch: 0.85 },
      apartment: { label: "Apartment", roof: "flat", pitch: 0 },
      cabin: { label: "Cabin", roof: "gable", pitch: 1.25 },
    };
  }
  _residenceStyleOptions(d) {
    const styles = this._resStyles();
    const cur = (d.config && d.config.residence_style) || "cape_cod";
    return Object.keys(styles).map(k => `<option value="${k}"${k === cur ? " selected" : ""}>${styles[k].label}</option>`).join("");
  }
  _styleDefaults(style) {
    const T = {
      cape_cod: { roof: "gable", pitch: 1.0, dormersFront: 2, dormersRear: 1 },
      colonial: { roof: "gable", pitch: 0.7, dormersFront: 0, dormersRear: 0 },
      dutch_colonial: { roof: "gambrel", pitch: 0.6, dormersFront: 2, dormersRear: 1 },
      ranch: { roof: "hip", pitch: 0.5, dormersFront: 0, dormersRear: 0 },
      two_story: { roof: "gable", pitch: 0.65, dormersFront: 0, dormersRear: 0 },
      craftsman: { roof: "hip", pitch: 0.6, dormersFront: 1, dormersRear: 0 },
      modern: { roof: "flat", pitch: 0.12, dormersFront: 0, dormersRear: 0 },
      townhouse: { roof: "gable", pitch: 0.85, dormersFront: 0, dormersRear: 0 },
      apartment: { roof: "flat", pitch: 0.12, dormersFront: 0, dormersRear: 0 },
      cabin: { roof: "gable", pitch: 1.25, dormersFront: 2, dormersRear: 1 },
    };
    return T[style] || T.cape_cod;
  }
  _houseSpec() {
    const c = this._data()?.config || {};
    const style = c.residence_style || "cape_cod";
    const sd = this._styleDefaults(style);
    const num = v => (v === "" || v == null ? null : Number(v));
    const fEx = num(c.dormers_front), rEx = num(c.dormers_rear);
    const spec = {};
    spec.roof = sd.roof || "gable";
    spec.stories = num(c.home_stories) != null ? num(c.home_stories) : 1.5;
    if (sd.pitch != null) spec.pitch = sd.pitch;
    spec.dormersFront = fEx != null ? fEx : sd.dormersFront;
    spec.dormersRear = rEx != null ? rEx : sd.dormersRear;
    if (num(c.garage_bays) != null) spec.garageBays = num(c.garage_bays);
    if (c.chimney_side) spec.chimney = c.chimney_side;
    return spec;
  }
  // Panel floor key -> model floor key; convert editor rooms (SVG units) to
  // the model's real feet (FT_PER_UNIT = 0.2), same as Classic.
  _planToFeet(plan) {
    const FT = 0.2, out = {};
    Object.keys(plan || {}).forEach(fk => {
      out[fk] = (((plan[fk] || {}).rooms) || []).filter(r => r.type !== "outdoor").map(r => ({
        name: (r.name || "").toLowerCase(),
        label: (r.name || "").toUpperCase(),
        x: (r.x || 0) * FT, y: (r.y || 0) * FT, w: (r.w || 0) * FT, d: (r.h || 0) * FT,
        type: r.type,
        points: (Array.isArray(r.points) && r.points.length >= 3) ? r.points.map(p => [p[0] * FT, p[1] * FT]) : undefined,
      }));
    });
    return this._snapFeet(out);
  }
  _snapMap(vals, tol) {
    const s = vals.slice().sort((a, b) => a - b), reps = [];
    let cur = null;
    s.forEach(v => { if (cur && v - cur.start <= tol) { cur.vals.push(v); } else { cur = { vals: [v], start: v }; reps.push(cur); } });
    const means = reps.map(g => g.vals.reduce((a, b) => a + b, 0) / g.vals.length);
    return v => { let best = v, bd = tol + 1e-6; means.forEach(m => { const dd = Math.abs(v - m); if (dd < bd) { bd = dd; best = m; } }); return best; };
  }
  _snapFeet(out) {
    const TOL = 1.0;
    Object.keys(out || {}).forEach(fk => {
      const rooms = out[fk] || [];
      if (rooms.length < 2) return;
      const xs = [], ys = [];
      rooms.forEach(r => { xs.push(r.x, r.x + r.w); ys.push(r.y, r.y + r.d); if (r.points) r.points.forEach(p => { xs.push(p[0]); ys.push(p[1]); }); });
      const sx = this._snapMap(xs, TOL), sy = this._snapMap(ys, TOL);
      rooms.forEach(r => {
        const x0 = sx(r.x), x1 = sx(r.x + r.w), y0 = sy(r.y), y1 = sy(r.y + r.d);
        r.x = x0; r.w = x1 - x0; r.y = y0; r.d = y1 - y0;
        if (r.points) r.points = r.points.map(p => [sx(p[0]), sy(p[1])]);
      });
    });
    return out;
  }
  _house3dPlan() { return this._planToFeet(this._getFloorPlan()); }
  _elementsToFeet(raw) {
    const FT = 0.2, states = this._hass?.states || {}, out = {};
    Object.keys(raw || {}).forEach(fk => {
      out[fk] = (raw[fk] || []).map(e => {
        let open = false;
        if (e.entity && states[e.entity]) { const s = states[e.entity].state; open = (s === "on" || s === "open"); }
        return { type: e.type, kind: e.kind, wall: e.wall, room: e.room, slope: e.slope, pos: (e.pos != null ? e.pos : 0.5), w: (e.w || 20) * FT, open };
      });
    });
    return out;
  }
  _house3dElements() { return this._elementsToFeet(this._getFloorElements()); }
  _house3dGarage() {
    const cfg = this._data()?.config || {};
    const map = cfg.door_mapping || {};
    const bays = Math.max(0, Math.min(Number(cfg.garage_bays) || 0, 8));
    const states = this._hass?.states || {};
    const out = [];
    for (let i = 1; i <= bays; i++) {
      const eid = map["garage_" + i] || (i === 1 ? (map.garage || "") : "");
      let open = false;
      if (eid && states[eid]) { const s = states[eid].state; open = (s === "on" || s === "open"); }
      out.push({ open });
    }
    return out;
  }
  _house3dFloor() {
    const f = this._currentFloor || "all";
    return f === "bsmt" ? "b" : f;
  }
  _house3dLit() {
    const d = this._data() || {};
    const lit = {};
    (d.areas || []).forEach(a => { if (a.active) lit[String(a.name).toLowerCase()] = "on"; });
    const mm = this._mmwave && this._mmwave.rooms;
    if (Array.isArray(mm)) mm.forEach(r => { if (r.detecting_count > 0) lit[String(r.name).toLowerCase()] = "mmwave"; });
    return lit;
  }
  _house3dDoors() {
    const d = this._data() || {};
    return d.doors || {};
  }
  _doorSlots() {
    const bays = Math.max(0, Math.min(Number((this._data()?.config || {}).garage_bays) || 0, 8));
    const garage = [];
    for (let i = 1; i <= bays; i++) garage.push(["garage_" + i, "Garage Door " + i]);
    if (!bays) garage.push(["garage", "Garage Door"]);
    return [["front", "Front Door"], ...garage, ["garage_rear", "Garage Side / Rear"], ["kitchen_garage", "Kitchen ↔ Garage"], ["cellar", "Cellar / Bulkhead"], ["basement", "Basement"]];
  }
  _renderDoorMappingNew(d) {
    const map = (d.config && d.config.door_mapping) || {};
    const rows = this._doorSlots().map(([slot, label]) => `
      <div class="cfg-row">
        <label>${label}</label>
        <select class="door-map-sel-new" id="resDoorMap-${slot}" data-slot="${slot}">${this._doorEntityOptions(map[slot] || "")}</select>
      </div>`).join("");
    return rows;
  }
  _renderHouse3dNew() {
    const mount = this.shadowRoot?.getElementById("resIso");
    if (!mount || typeof window.NOVA3D === "undefined") return;
    const floor = this._house3dFloor();
    const spec = this._houseSpec();
    const plan = this._house3dPlan();
    const elements = this._house3dElements();
    const garage = this._house3dGarage();
    const key = floor + "|" + JSON.stringify(spec) + "|" + JSON.stringify(plan) + "|" + JSON.stringify(elements) + "|" + JSON.stringify(garage);
    if (this._house3dBoxKey !== key) {
      this._house3dBox = window.NOVA3D.fixedBox({ floor, spec, plan, elements, garage });
      this._house3dBoxKey = key;
    }
    const base = this._house3dBox, zoom = this._house3dZoom || 1;
    const cx = base[0] + base[2] / 2, cy = base[1] + base[3] / 2;
    const w = base[2] / zoom, h = base[3] / zoom;
    const box = [cx - w / 2, cy - h / 2, w, h];
    mount.innerHTML = window.NOVA3D.renderSVG({
      theta: this._house3dTheta || 35, floor, lit: this._house3dLit(), doors: this._house3dDoors(), box, spec, plan, elements, garage,
    });
  }
  _buildResidenceAnnotationsNew() {
    const d = this._data() || {};
    const cfg = d.config || {};
    const areas = d.areas || [];
    const cfgBeds = cfg.home_bedrooms, cfgBaths = cfg.home_bathrooms;
    const beds = (cfgBeds != null && cfgBeds !== "") ? Number(cfgBeds) : (areas.filter(a => a.bedroom).length || 0);
    const baths = (cfgBaths != null && cfgBaths !== "") ? Number(cfgBaths) : areas.filter(a => /bath/i.test(a.name || "")).length;
    const bbEl = this.shadowRoot?.getElementById("resBb");
    if (bbEl) bbEl.textContent = beds + " / " + (baths || "—");
    const sqEl = this.shadowRoot?.getElementById("resSqft");
    if (sqEl) {
      let sqft = cfg.floor_plan_sqft;
      if (!sqft) {
        const plan = this._getFloorPlan();
        let u = 0;
        Object.keys(plan).forEach(fk => (plan[fk] && plan[fk].rooms || []).forEach(r => {
          if (r.type === "door" || r.type === "stairs") return;
          u += (r.w || 0) * (r.h || 0);
        }));
        sqft = Math.min(5000, Math.max(600, Math.round(u * 0.032 / 50) * 50));
      }
      sqEl.textContent = sqft ? "~" + Number(sqft).toLocaleString() : "—";
    }
    const styleTag = this.shadowRoot?.getElementById("resStyleTag");
    if (styleTag) {
      const rs = this._resStyles()[(cfg.residence_style || "cape_cod")];
      styleTag.textContent = rs ? rs.label : "—";
    }
    const occEl = this.shadowRoot?.getElementById("resOcc");
    if (occEl) {
      const occ = areas.filter(a => a.active).length;
      occEl.textContent = occ + " / " + (areas.length || 0);
    }
  }
  _build3DHouseNew() {
    const mount = this.shadowRoot?.getElementById("resIso");
    if (!mount) return;
    this._renderHouse3dNew();
    this._buildResidenceAnnotationsNew();
    this._wire3DDragNew();
  }
  _wire3DDragNew() {
    const scene = this.shadowRoot?.getElementById("resScene");
    if (!scene || scene._house3dWired) return;
    scene._house3dWired = true;
    // Touch is ambiguous between "rotate the house" and "scroll the page
    // past it" — both start as a drag on the same element. Committing to
    // rotate on touchstart (and preventDefault-ing every touchmove) hijacked
    // every vertical scroll attempt that happened to start on the house,
    // which read as "the 3D view is sluggish" (real complaint: it wouldn't
    // let go of the touch to let the page scroll at all). Mouse drag has no
    // such ambiguity — only touch needs the direction check below.
    let dragging = false, lastX = 0, raf = null;
    let isTouch = false, startX = 0, startY = 0, decided = false;
    const DIR_THRESHOLD = 6; // px of movement before committing to a direction
    const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = null; this._renderHouse3dNew(); }); };
    const pt = e => (e.touches && e.touches[0] ? e.touches[0] : e);
    const move = (e) => {
      if (!dragging) return;
      const p = pt(e);
      if (isTouch && !decided) {
        const dx = Math.abs(p.clientX - startX), dy = Math.abs(p.clientY - startY);
        if (dx < DIR_THRESHOLD && dy < DIR_THRESHOLD) return; // not enough movement yet to tell
        if (dy > dx) { up(); return; } // vertical swipe — let the page scroll instead
        decided = true;
      }
      if (e.cancelable) e.preventDefault();
      this._house3dTheta = (this._house3dTheta || 35) + (p.clientX - lastX) * 0.5;
      lastX = p.clientX;
      schedule();
    };
    const up = () => {
      dragging = false; scene.classList.remove("dragging");
      window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up);
      window.removeEventListener("touchmove", move); window.removeEventListener("touchend", up);
    };
    const down = (e, touch) => {
      const p = pt(e);
      dragging = true; lastX = p.clientX;
      isTouch = !!touch; startX = p.clientX; startY = p.clientY; decided = !isTouch;
      scene.classList.add("dragging");
      window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
      window.addEventListener("touchmove", move, { passive: false }); window.addEventListener("touchend", up);
    };
    scene.addEventListener("mousedown", (e) => { down(e, false); e.preventDefault(); });
    scene.addEventListener("touchstart", (e) => down(e, true), { passive: true });
    scene.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      this._house3dZoom = Math.max(0.5, Math.min(4, (this._house3dZoom || 1) * factor));
      schedule();
    }, { passive: false });
  }
  async _fetchMmwaveNew() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/mmwave_overview" });
      this._mmwave = res || { rooms: [], summary: {} };
    } catch (_) {
      this._mmwave = { rooms: [], summary: {}, error: true };
    }
    this._renderMmwaveNew();
    if (this._currentTab === "residence") this._renderHouse3dNew();
  }
  _renderMmwaveNew() {
    const list = this.shadowRoot?.getElementById("resMmwaveList");
    const sumEl = this.shadowRoot?.getElementById("resMmwaveSummary");
    if (!list) return;
    const data = this._mmwave || { rooms: [], summary: {} };
    const s = data.summary || {};
    if (sumEl) sumEl.textContent = s.rooms_with_mmwave ? `◉ ${s.rooms_detecting || 0}/${s.rooms_with_mmwave} OCCUPIED` : "◉ NONE";
    if (data.error) { list.innerHTML = `<div class="toggle-desc">Couldn't read sensors — restart Home Assistant after updating, then reopen.</div>`; return; }
    const rooms = data.rooms || [];
    if (!rooms.length) { list.innerHTML = `<div class="toggle-desc">No presence, motion, or mmWave sensors found. Assign occupancy sensors to areas in Home Assistant and they'll appear here.</div>`; return; }
    list.innerHTML = rooms.map(r => {
      const on = r.detecting_count > 0;
      const sensorLine = r.sensor_count > 1 ? `${r.detecting_count}/${r.sensor_count} sensors` : `${r.sensor_count} sensor`;
      return `<div class="cfg-row">
        <label>${this._esc(r.name)}${r.outdoor ? " ▲" : ""}</label>
        <span class="toggle-desc">${on ? "OCCUPIED" : "clear"} · ${sensorLine} · ${on ? "now" : this._esc(r.freshest)}</span>
      </div>`;
    }).join("");
  }
  _wireResidenceControlsNew() {
    const root = this.shadowRoot;
    // Not part of the Settings tab, so it can't ride _wireSettings()'s
    // generic .cfg-field autosave (only wired while that tab is current) —
    // wire this select directly, same as Intrusion's own cfg-field controls.
    const styleSel = root.querySelector('select.cfg-field[data-cfg-key="residence_style"]');
    if (styleSel && !styleSel._wired) {
      styleSel._wired = true;
      styleSel.addEventListener("change", () => this._saveSetting("residence_style", styleSel.value));
    }
    root.querySelectorAll(".res-floor-tab").forEach(btn => {
      btn.addEventListener("click", () => {
        this._currentFloor = btn.getAttribute("data-res-floor");
        this._house3dBoxKey = null;
        root.querySelectorAll(".res-floor-tab").forEach(b => b.classList.toggle("active", b === btn));
        this._renderHouse3dNew();
      });
    });
    root.querySelectorAll(".res-view-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        this._house3dTheta = parseFloat(btn.getAttribute("data-res-theta"));
        this._renderHouse3dNew();
      });
    });
    this._doorSlots().forEach(([slot]) => {
      const ds = root.getElementById("resDoorMap-" + slot);
      if (ds && !ds._wired) {
        ds._wired = true;
        ds.addEventListener("change", async () => {
          const cfg = this._data()?.config || {};
          const map = { ...(cfg.door_mapping || {}) };
          if (ds.value) map[slot] = ds.value; else delete map[slot];
          if (this._liveData?.config) this._liveData.config.door_mapping = map;
          this._house3dBoxKey = null;
          this._renderHouse3dNew();
          try { await this._hass.callWS({ type: "nova/update_config", key: "door_mapping", value: JSON.stringify(map) }); } catch (_) {}
        });
      }
    });
  }

  _htmlResidence() {
    const d = this._data() || {};
    const cfg = d.config || {};
    if (!this._currentFloor) this._currentFloor = "all";
    const floors = [["all", "All"], ["1f", "1st Floor"]];
    if (String(cfg.home_stories ?? "1.5") !== "1") floors.push(["2f", "2nd Floor"]);
    if (cfg.has_basement !== false) floors.push(["bsmt", "Basement"]);
    return `
      <div class="res-tab-new">
        <div class="res-main-new">
          <div class="cfg-row">
            <label>Home style</label>
            <select class="cfg-field" data-cfg-key="residence_style">${this._residenceStyleOptions(d)}</select>
          </div>
          <div class="fpn-toolbar">
            <div class="fpn-floor-tabs">
              ${floors.map(([fk, lbl]) => `<button class="mode-chip res-floor-tab${this._currentFloor === fk ? " active" : ""}" data-res-floor="${fk}">${lbl}</button>`).join("")}
            </div>
            <div class="fpn-actions">
              ${[["FRONT", 0], ["RIGHT", 90], ["REAR", 180], ["LEFT", 270], ["ISO", 35]].map(([lbl, th]) => `<button class="mode-chip res-view-btn" data-res-theta="${th}">${lbl}</button>`).join("")}
            </div>
          </div>
          <div class="fpn-hint">Drag to rotate · scroll to zoom</div>
          <div class="res-scene-new" id="resScene"><div id="resIso"></div></div>
          <div class="mode-grid res-stats-new">
            <div class="cfg-row"><label>Est. sq ft</label><b id="resSqft">—</b></div>
            <div class="cfg-row"><label>Bed / Bath</label><b id="resBb">—</b></div>
            <div class="cfg-row"><label>Style</label><b id="resStyleTag">—</b></div>
            <div class="cfg-row"><label>Occupied</label><b id="resOcc">—</b></div>
          </div>
          <div class="mode-bind-head">Doors <span class="toggle-desc">map to your entities — blank = auto-detect by name</span></div>
          ${this._renderDoorMappingNew(d)}
        </div>
        <div class="res-side-new">
          <div class="mode-bind-head">mmWave Presence <span class="toggle-desc" id="resMmwaveSummary">◉ scan</span></div>
          <div class="toggle-desc">Live occupancy per room from presence/motion/mmWave sensors.</div>
          <div id="resMmwaveList"><div class="toggle-desc">Reading sensors…</div></div>
        </div>
      </div>`;
  }

  _htmlSettings() {
    const groupsNav = NovaPanel.SETTINGS_GROUPS.map(g =>
      `<button class="settings-nav-btn${this._settingsSection === g.id ? " active" : ""}" data-settings-section="${g.id}">${this._esc(g.label)}</button>`
    ).join("");
    const cards = NovaPanel.SETTINGS_CARDS.map(c => this._settingsCardHtml(c)).join("");
    return `
        <div class="settings-toolbar">
          <input type="search" id="settingsSearch" class="settings-search" placeholder="Search settings — try “camera” or “sleep”…" value="${this._esc(this._settingsSearch)}">
          <nav class="settings-nav">${groupsNav}</nav>
        </div>
        <div class="settings-grid" id="settingsGrid">${cards}</div>
    `;
  }

  _settingsCardHtml(c) {
    const body = c.real
      ? (c.id === "general" ? this._generalCardBody()
        : c.id === "person_honorifics" ? this._personHonorificsCardBody()
        : c.id === "room_speakers" ? this._roomSpeakersCardBody()
        : c.id === "residence_home" ? this._residenceHomeCardBody()
        : c.id === "operational_mode" ? this._operationalModeCardBody()
        : c.id === "diagnostics" ? this._diagnosticsCardBody()
        : c.id === "ai_models" ? this._aiModelsCardBody()
        : c.id === "briefings" ? this._briefingsCardBody()
        : c.id === "voice_confirmation" ? this._voiceConfirmationCardBody()
        : c.id === "satellite_speaker" ? this._satelliteSpeakerCardBody()
        : c.id === "announcement_speakers" ? this._announcementSpeakersCardBody()
        : c.id === "notifications" ? this._notificationsCardBody()
        : c.id === "security_alarm" ? this._securityAlarmCardBody()
        : c.id === "sentinel_rules" ? this._sentinelRulesCardBody()
        : c.id === "hazard_monitor" ? this._hazardMonitorCardBody()
        : c.id === "energy_management" ? this._energyManagementCardBody()
        : c.id === "host_health" ? this._hostHealthCardBody()
        : c.id === "appliances" ? this._appliancesCardBody()
        : c.id === "anticipation_memory" ? this._anticipationMemoryCardBody()
        : c.id === "memory_curated" ? this._memoryCardBody()
        : c.id === "observer_tuning" ? this._observerTuningCardBody()
        : c.id === "routine_learning" ? this._routineLearningCardBody()
        : c.id === "excluded_entities" ? this._excludedEntitiesCardBody()
        : c.id === "cameras" ? this._camerasCardBody()
        : c.id === "doorbell_training" ? this._doorbellTrainingCardBody()
        : c.id === "floor_plan_editor" ? this._floorPlanEditorCardBody()
        : c.id === "wellbeing_context" ? this._wellbeingContextCardBody()
        : c.id === "character_research" ? this._characterResearchCardBody()
        : c.id === "document_library" ? this._documentLibraryCardBody()
        : "")
      : `<div class="stub-body">${this._esc(c.desc)}<br><span class="stub-where">Not built here yet — see Settings → Devices &amp; Services → Nova → Configure.</span></div>`;
    return `
      <div class="panel settings-card" id="settings-card-${c.id}" data-settings-group="${c.group}" data-search="${this._esc((c.title + " " + c.desc).toLowerCase())}">
        <div class="panel-head">
          <div class="panel-title">${this._esc(c.title)}${c.real ? "" : '<span class="stub-tag">SOON</span>'}</div>
        </div>
        ${body}
      </div>`;
  }

  _generalCardBody() {
    const cfg = this._data()?.config || {};
    const onOff = (key, label, desc) => `
      <div class="toggle-row">
        <span class="toggle-label">${this._esc(label)}</span>
        <span class="toggle-desc">${this._esc(desc)}</span>
        <button class="toggle-btn ${cfg[key] ? "on" : "off"}" data-cfg-key="${key}" data-cfg-val="${cfg[key] ? "false" : "true"}">
          ${cfg[key] ? "ON" : "OFF"}
        </button>
      </div>`;
    return `
      <div class="cfg-row">
        <label>Language</label>
        <select id="uiLanguage" class="cfg-field" data-cfg-key="ui_language">
          ${this._optSelect([
            ["auto", "Auto (Home Assistant)"], ["en", "English"], ["cs", "Čeština"],
            ["da", "Dansk"], ["de", "Deutsch"], ["es", "Español"], ["fi", "Suomi"],
            ["fr", "Français"], ["it", "Italiano"], ["nb", "Norsk bokmål"],
            ["nl", "Nederlands"], ["pl", "Polski"], ["pt", "Português"],
            ["pt-br", "Português (Brasil)"], ["ro", "Română"], ["ru", "Русский"],
            ["sk", "Slovenčina"], ["sv", "Svenska"], ["tr", "Türkçe"],
            ["uk", "Українська"],
          ], cfg.ui_language || "auto")}
        </select>
      </div>
      <div class="cfg-row">
        <label>Sleep state</label>
        <select class="cfg-field" data-cfg-key="sleep_override">
          ${this._optSelect([["auto", "Auto (occupancy + quiet hours)"], ["awake", "Awake"], ["asleep", "Asleep"]], cfg.sleep_override || "auto")}
        </select>
      </div>
      <div class="toggle-list">
        ${onOff("announcements_enabled", "Announcements", "Master switch — all proactive speech")}
        ${onOff("sentinel_enabled", "Sentinel", "Door/garage/lock-left-open alerts")}
        ${onOff("observer_enabled", "Observer", "AI event awareness (uses API)")}
        ${onOff("cognition_enabled", "Cognition", "Local triage — sees telemetry and decides what deserves deeper reasoning")}
        ${onOff("rich_reasoning", "Rich Reasoning", "Use the configured reasoning model first for medium and high-priority events")}
        ${onOff("light_control_enabled", "Dashboard Light Control", "Allow room light toggles on the dashboard; status remains visible when off")}
      </div>`;
  }

  _personHonorificsCardBody() {
    const cfg = this._data()?.config || {};
    const people = cfg.all_people || [];
    const overrides = cfg.person_honorifics || {};
    const opts = NovaPanel.HONORIFIC_OPTIONS;
    if (!people.length) {
      return `<div class="stub-body">No <code>person.*</code> entities found yet — add one in Home Assistant to set a personal address here.</div>`;
    }
    const rows = people.map(p => {
      const current = overrides[p.entity_id] || "";
      const isCustom = current && !opts.includes(current);
      return `
        <div class="pairing-row person-honorific-row">
          <span class="pairing-label">${this._esc(p.name)}</span>
          <select class="person-honorific-select" data-person-id="${this._esc(p.entity_id)}">
            <option value="">— use default —</option>
            ${opts.map(o => `<option value="${this._esc(o)}"${!isCustom && o === current ? " selected" : ""}>${this._esc(o[0].toUpperCase() + o.slice(1))}</option>`).join("")}
            <option value="__custom__"${isCustom ? " selected" : ""}>Custom…</option>
          </select>
          <input type="text" class="person-honorific-custom" data-person-id="${this._esc(p.entity_id)}"
                 placeholder="Custom address" value="${isCustom ? this._esc(current) : ""}"
                 ${isCustom ? "" : "hidden"}>
        </div>`;
    }).join("");
    return `
      <div class="pairing-list">${rows}</div>
      <div class="camera-note">Only applies while that person is home alone. With nobody home, or more than one person home, Nova doesn't guess — it drops the address entirely. Anyone without an override here uses the global "Address me as" setting (Settings → Devices &amp; Services → Nova → Configure).</div>`;
  }

  _roomSpeakersCardBody() {
    const cfg = this._data()?.config || {};
    const areas = cfg.speaker_areas || [];
    const castDevs = cfg.cast_devices || [];
    const assigned = cfg.room_speakers || {};
    const rows = areas.length
      ? areas.map(a => `
        <div class="pairing-row">
          <span class="pairing-label">${this._esc(a.name)}</span>
          <select class="new-room-speaker-select" data-area-id="${this._esc(a.area_id)}">
            <option value="">— none —</option>
            ${castDevs.map(cd => `<option value="${this._esc(cd.entity_id)}"${cd.entity_id === assigned[a.area_id] ? " selected" : ""}>${this._esc(cd.name)}</option>`).join("")}
          </select>
        </div>`).join("")
      : `<div class="stub-body">No rooms found yet.</div>`;
    return `
      <div class="pairing-list">${rows}</div>
      <div class="pairing-row">
        <span class="pairing-label">General speaker (fallback)</span>
        <select class="new-general-speaker-select">
          <option value="">— none —</option>
          ${castDevs.map(cd => `<option value="${this._esc(cd.entity_id)}"${cd.entity_id === cfg.general_speaker ? " selected" : ""}>${this._esc(cd.name)}</option>`).join("")}
        </select>
      </div>`;
  }

  _residenceHomeCardBody() {
    const cfg = this._data()?.config || {};
    const styles = {
      cape_cod: "Cape Cod", colonial: "Colonial", dutch_colonial: "Dutch Colonial",
      ranch: "Ranch", two_story: "Two-Story", craftsman: "Craftsman",
      modern: "Modern", townhouse: "Townhouse", apartment: "Apartment", cabin: "Cabin",
    };
    return `
      <div class="cfg-row">
        <label>Home type</label>
        <select class="cfg-field" data-cfg-key="residence_style">
          ${this._optSelect(Object.entries(styles), cfg.residence_style || "cape_cod")}
        </select>
      </div>
      <div class="cfg-row">
        <label>Stories</label>
        <select class="cfg-field" data-cfg-key="home_stories">
          ${this._optSelect(["1", "1.5", "2", "3"].map(v => [v, v]), String(cfg.home_stories ?? "1.5"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Garage bays</label>
        <select class="cfg-field" data-cfg-key="garage_bays">
          ${this._optSelect(["0", "1", "2", "3", "4"].map(v => [v, v]), String(cfg.garage_bays ?? "3"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Front dormers</label>
        <select class="cfg-field" data-cfg-key="dormers_front">
          ${this._optSelect(["0", "1", "2", "3"].map(v => [v, v]), String(cfg.dormers_front ?? "2"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Rear dormers</label>
        <select class="cfg-field" data-cfg-key="dormers_rear">
          ${this._optSelect(["0", "1", "2"].map(v => [v, v]), String(cfg.dormers_rear ?? "1"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Chimney</label>
        <select class="cfg-field" data-cfg-key="chimney_side">
          ${this._optSelect([["right", "East / right"], ["left", "West / left"], ["none", "None"]], cfg.chimney_side || "right")}
        </select>
      </div>
      <div class="cfg-row">
        <label>Basement</label>
        <button class="toggle-btn ${(cfg.has_basement !== false) ? "on" : "off"}" data-cfg-key="has_basement" data-cfg-val="${(cfg.has_basement !== false) ? "false" : "true"}">
          ${(cfg.has_basement !== false) ? "YES" : "NO"}
        </button>
      </div>
      <div class="cfg-row">
        <label>Bedrooms</label>
        <input class="cfg-field cfg-num" type="number" min="0" max="12" data-cfg-key="home_bedrooms" value="${cfg.home_bedrooms ?? ""}" placeholder="3">
      </div>
      <div class="cfg-row">
        <label>Bathrooms</label>
        <input class="cfg-field cfg-num" type="number" min="0" max="12" step="0.5" data-cfg-key="home_bathrooms" value="${cfg.home_bathrooms ?? ""}" placeholder="2">
      </div>
      <div class="cfg-row">
        <label>Square feet</label>
        <input class="cfg-field cfg-num" type="number" min="0" max="20000" step="50" data-cfg-key="floor_plan_sqft" value="${cfg.floor_plan_sqft ?? ""}" placeholder="1800">
      </div>
      <div class="stub-body">Detailed room layout is edited in the Floor Plan Editor. This feeds the Residence 3D view.</div>`;
  }

  _operationalModeCardBody() {
    const cfg = this._data()?.config || {};
    const areas = this._data()?.areas || [];
    const m = this._mode || {};
    const active = m.active || "normal";
    const avail = m.available || [];
    const modeChips = avail.length
      ? avail.map(mo => `<button class="mode-chip ${mo.name === active ? "mode-chip-on" : ""}" data-mode="${this._esc(mo.name)}" title="${this._esc(mo.description || "")}">${this._esc(mo.name)}</button>`).join("")
      : `<div class="stub-body">Couldn't load modes — restart Home Assistant after updating.</div>`;
    const labAreas = Array.isArray(cfg.lab_areas) ? cfg.lab_areas : [];
    const labChips = areas.length
      ? areas.map(a => `<button class="mode-chip ${labAreas.includes(a.id) ? "mode-chip-on" : ""}" data-lab-area="${this._esc(a.id)}">${this._esc(a.name)}</button>`).join("")
      : `<span class="stub-body">No rooms detected yet.</span>`;
    const areaOpts = [["", "— none —"], ...areas.map(a => [a.id, a.name])];
    const mpOpts = this._mediaPlayerOptions(cfg.movie_media_player || "");
    return `
      <div class="cfg-row">
        <label>Auto (follow occupancy)</label>
        <button class="toggle-btn ${cfg.operational_mode_auto !== false ? "on" : "off"}" data-cfg-key="operational_mode_auto" data-cfg-val="${cfg.operational_mode_auto !== false ? "false" : "true"}">
          ${cfg.operational_mode_auto !== false ? "ON" : "OFF"}
        </button>
      </div>
      <div class="stub-body">Active: <strong>${this._esc(active.toUpperCase())}</strong>${m.description ? " — " + this._esc(m.description) : ""}. Safety always stays active.</div>
      <div class="mode-grid">${modeChips}</div>
      <div class="mode-bind-head">Mode bindings — scope Lab &amp; Movie to specific rooms</div>
      <div class="cfg-row"><label>Lab rooms (quiet only here)</label></div>
      <div class="mode-grid">${labChips}</div>
      <div class="cfg-row">
        <label>Movie room</label>
        <select class="cfg-field" data-cfg-key="movie_area">${this._optSelect(areaOpts, cfg.movie_area || "")}</select>
      </div>
      <div class="cfg-row">
        <label>Movie player <span class="toggle-desc">optional</span></label>
        <select class="cfg-field" data-cfg-key="movie_media_player">${this._optSelect(mpOpts, cfg.movie_media_player || "")}</select>
      </div>
      <div class="cfg-row">
        <label>Movie dim %</label>
        <input class="cfg-field cfg-num" type="number" min="0" max="100" step="5" data-cfg-key="movie_dim_pct" value="${cfg.movie_dim_pct ?? ""}" placeholder="15">
      </div>`;
  }

  // Merged from Classic's two separate diagnostics cards ("System
  // Diagnostics" under General, a per-service "Diagnostics" test panel
  // under Cameras) into the one place this section's own docstring already
  // says it should live. Fetched once per element lifetime (not on the 20s
  // live-data poll, and not on every settings re-render) — the health check
  // makes a real, if lightweight, LLM/TTS connectivity probe, matching
  // Classic's own on-demand-only behaviour.
  async _fetchDiagnosticsData() {
    if (!this._hass) return;
    try {
      this._diag = await this._hass.callWS({ type: "nova/diagnostics" });
    } catch (_) { this._diag = { error: true }; }
    try {
      this._calib = await this._hass.callWS({ type: "nova/get_calibration" });
    } catch (_) { this._calib = null; }
    try {
      this._setupHealth = await this._hass.callWS({ type: "nova/get_setup_health" });
    } catch (_) { this._setupHealth = { error: true }; }
    try {
      const activity = await this._hass.callWS({ type: "nova/get_provider_activity", days: 7 });
      this._providerActivity = activity.days || [];
    } catch (_) { this._providerActivity = null; }
    if (this._currentTab === "settings") this._render();
  }

  _diagStatusCls(st) {
    return { ok: "diag-ok", warn: "diag-warn", idle: "diag-idle", down: "diag-down", off: "diag-off" }[st] || "diag-off";
  }
  _diagStatusLabel(st) {
    return { ok: "OK", warn: "WARN", idle: "IDLE", down: "DOWN", off: "OFF" }[st] || "?";
  }

  // ── Setup Doctor (Phase 2): read-only configuration health, folded into
  // the same diagnostics card. Only the setup-specific checks are shown here
  // — the 8 core-service checks (llm/embeddings/tts/stt/cameras/routines/
  // database/scheduler) already render above under "Core services"; showing
  // them a second time from the same backend payload would just be noise.
  static SETUP_HEALTH_CORE_KEYS = new Set([
    "llm", "embeddings", "tts", "stt", "cameras", "routines", "database", "scheduler",
  ]);

  _setupHealthCardBody() {
    const sh = this._setupHealth || {};
    if (sh.error) {
      return `<div class="stub-body">Couldn't run Setup Doctor — restart Home Assistant after updating.</div>`;
    }
    const checks = (sh.checks || []).filter(c => !NovaPanel.SETUP_HEALTH_CORE_KEYS.has(c.key));
    if (!checks.length) {
      return `<div class="panel-head"><div class="panel-title">Setup Doctor</div></div><div class="stub-body">Loading…</div>`;
    }
    const rows = checks.map(c => `
        <div class="cfg-row">
          <label>${this._esc(c.name)}</label>
          <span class="${this._diagStatusCls(c.status)}">${this._diagStatusLabel(c.status)}</span>
        </div>
        <div class="stub-body" style="margin:-6px 0 8px">${this._esc(c.detail || "")}${
          c.suggested_fix ? ` — ${this._esc(c.suggested_fix)}` : ""}</div>`).join("");
    return `<div class="panel-head"><div class="panel-title">Setup Doctor</div></div>${rows}`;
  }

  // ── Provider activity (Phase 5): bounded daily aggregates only — never
  // prompts, responses, tool arguments, images, or credentials. Days with no
  // recorded activity are simply absent, not shown as zero rows.
  _providerActivityCardBody() {
    const days = this._providerActivity;
    if (days === null) {
      return `<div class="panel-head"><div class="panel-title">Provider Activity</div></div><div class="stub-body">Couldn't load provider activity.</div>`;
    }
    if (!days || !days.length) {
      return `<div class="panel-head"><div class="panel-title">Provider Activity</div></div><div class="stub-body">No provider activity recorded yet. Activity appears after Nova uses a supported conversation or classifier path.</div>`;
    }
    const rows = days.map(d => {
      const entries = (d.entries || []).map(e => {
        const tokens = (e.avg_input_tokens != null || e.avg_output_tokens != null)
          ? ` · avg tokens in/out ${e.avg_input_tokens ?? "—"}/${e.avg_output_tokens ?? "—"}`
          : "";
        return `<div class="stub-body" style="margin:2px 0">
            ${this._esc(e.provider)}/${this._esc(e.model)} (${this._esc(e.role)}, ${this._esc(e.location)}) —
            ${e.call_count} call${e.call_count === 1 ? "" : "s"},
            ${e.success_count} ok / ${e.failure_count} failed,
            avg ${e.avg_latency_ms ?? "—"}ms${tokens}
          </div>`;
      }).join("");
      return `<div class="cfg-row"><label>${this._esc(d.day)}</label></div>${entries}`;
    }).join("");
    return `<div class="panel-head"><div class="panel-title">Provider Activity</div></div>${rows}`;
  }

  _diagnosticsCardBody() {
    const cfg = this._data()?.config || {};
    const diag = this._diag || {};
    if (diag.error) {
      return `<div class="stub-body">Couldn't run diagnostics — restart Home Assistant after updating.</div>`;
    }
    const svcs = diag.services || [];
    const overall = svcs.length
      ? `<span class="${this._diagStatusCls(diag.overall)}">${this._esc((diag.summary || diag.overall || "").toUpperCase())}</span>`
      : "—";
    const rows = svcs.length
      ? svcs.map(s => `
        <div class="cfg-row">
          <label>${this._esc(s.name)}</label>
          <span class="${this._diagStatusCls(s.status)}">${this._diagStatusLabel(s.status)}</span>
        </div>
        <div class="stub-body" style="margin:-6px 0 8px">${this._esc(s.detail || "")}</div>`).join("")
      : `<div class="stub-body">Loading…</div>`;
    const svcTest = (svc, label) => `
      <div class="cfg-row">
        <label>${this._esc(label)}</label>
        <button class="mode-chip" data-svc="${this._esc(svc)}">RUN</button>
      </div>`;
    const camOpts = (cfg.cameras || []).filter(c => c.enabled !== false).map(c => [c.entity_id, c.name]);
    return `
      <div class="cfg-row"><label>Core services</label>${overall}</div>
      ${rows}
      <div class="cfg-row"><button class="mode-chip" id="newDiagRefresh">⟳ RUN CHECK</button></div>
      <div class="cfg-row"><label>HOMER — diagnostic sub-agent</label><span class="diag-ok">AVAILABLE</span></div>
      <div class="stub-body" style="margin:-6px 0 8px">Read-only. Nova can delegate a "why is this broken/slow" question to HOMER to investigate before answering — it can only read state, telemetry, and history, never control anything or change a setting. Always on; nothing to configure.</div>
      <div class="mode-bind-head"></div>
      ${this._setupHealthCardBody()}
      ${this._providerActivityCardBody()}
      <div class="panel-head"><div class="panel-title">Service tests</div></div>
      ${svcTest("nova.test_tts", "TTS — Nova voice test")}
      ${svcTest("nova.observer_status", "Observer — fire status event")}
      ${svcTest("nova.briefing", "Briefing — manual trigger")}
      ${svcTest("nova.diagnose_doorbell", "Doorbell — run diagnostics")}
      ${svcTest("nova.test_notify", "Notification — test phone push")}
      ${svcTest("nova.test_routing", "Routing — dump routing state to log")}
      <div class="cfg-row">
        <label>Camera — analyze now</label>
        <select class="cfg-field" id="newDiagCameraSelect">${camOpts.length ? this._optSelect(camOpts, camOpts[0][0]) : '<option value="">— no cameras —</option>'}</select>
      </div>
      <div class="cfg-row"><button class="mode-chip" id="newDiagCameraRun">RUN</button></div>`;
  }

  // ── AI Models — ported near-verbatim from Classic (see nova-panel.js's
  // own _modelRoles/_loadModelsFor/_pickHealModel). Deliberately NOT wired
  // through the generic .cfg-field autosave or _saveSetting: those trigger
  // a full _render(), which would wipe the just-populated live model
  // dropdown before the user ever sees it — the exact reason Classic's own
  // wiring comment gives for avoiding that here. ──
  _modelRoles() {
    return [
      { role: "llm", label: "Main Agent", provKey: "llm_provider", modelKey: "model" },
      { role: "classifier", label: "Classifier", provKey: "classifier_provider", modelKey: "classifier_model" },
      { role: "reasoning", label: "Reasoning", provKey: "reasoning_provider", modelKey: "reasoning_model" },
      { role: "vision", label: "Vision", provKey: "vision_provider", modelKey: "vision_model" },
      { role: "camrsn", label: "Camera Reasoning", provKey: "camera_reasoning_provider", modelKey: "camera_reasoning_model" },
    ];
  }

  _aiModelsCardBody() {
    const cfg = this._data()?.config || {};
    const PROVIDERS = ["groq", "openai", "gemini", "ollama", "anthropic", "custom"];
    const configuredProviders = this._modelRoles().map(r => cfg[r.provKey]);
    const legacyEndpoint = cfg.self_hosted_endpoints_migrated ? "" : cfg.llm_base_url;
    const ollamaEndpoint = cfg.ollama_base_url || (configuredProviders.includes("ollama") ? legacyEndpoint : "") || "";
    const customEndpoint = cfg.custom_base_url || (configuredProviders.includes("custom") ? legacyEndpoint : "") || "";
    const rows = this._modelRoles().map(r => {
      const curProv = cfg[r.provKey] || "groq";
      const curModel = cfg[r.modelKey] || "";
      const modelOpts =
        (curModel ? `<option value="${this._esc(curModel)}" selected>${this._esc(curModel)}</option>` : "") +
        `<option value="" disabled>loading…</option><option value="__custom__">✎ Custom…</option>`;
      return `
        <div class="new-model-row" data-role="${this._esc(r.role)}">
          <span class="model-label">${this._esc(r.label)}</span>
          <select class="new-prov-select" data-role="${this._esc(r.role)}" data-cfg-key="${r.provKey}">
            ${this._optSelect(PROVIDERS.map(p => [p, p]), curProv)}
          </select>
          <select class="new-model-select" data-role="${this._esc(r.role)}" data-cfg-key="${r.modelKey}" data-current="${this._esc(curModel)}">${modelOpts}</select>
          <input class="new-model-custom" data-role="${this._esc(r.role)}" data-cfg-key="${r.modelKey}"
                 type="text" placeholder="enter model id" value="${this._esc(curModel)}" style="display:none">
          <button class="mode-chip new-model-refresh" data-role="${this._esc(r.role)}" title="Refresh the live model list (bypasses the cache)">↻</button>
          <div class="new-model-warning" data-role-warning="${this._esc(r.role)}" hidden></div>
          ${r.role === "llm" ? `<div class="stub-body">Changes are staged until you press Apply. Nova reloads itself after a successful check and save.</div>` : ""}
          ${r.role === "vision" ? `<div class="stub-body">Vision needs a model whose provider reports image support. Camera Reasoning is text-only and does not.</div>` : ""}
        </div>`;
    }).join("");
    const credRows = ["groq", "openai", "anthropic", "gemini", "custom", "ollama"].map(p => `
      <div class="cred-row" data-cred-provider="${p}">
        <span class="model-label">${this._esc(p)}</span>
        <span class="cred-status" data-cred-status="${p}">…</span>
        <input class="cred-input" type="password" data-cred-provider="${p}" placeholder="enter to set or replace" autocomplete="off">
        <button class="mode-chip cred-save" data-cred-provider="${p}">SAVE</button>
        <button class="mode-chip cred-clear" data-cred-provider="${p}">CLEAR</button>
      </div>`).join("");
    return `
      <div class="stub-body">Choose a starting profile or configure each role yourself. Profiles only stage changes; nothing is saved until Apply.</div>
      <div class="cfg-row" data-ai-profiles>
        <label>Profile</label>
        <button class="mode-chip" data-ai-profile="hybrid">HYBRID</button>
        <button class="mode-chip" data-ai-profile="local">LOCAL TEXT</button>
        <button class="mode-chip" data-ai-profile="manual">MANUAL</button>
      </div>
      <div class="stub-body">Hybrid keeps the Main Agent and Vision choices, and moves background text work to Ollama. Local Text also moves the Main Agent. Vision only moves when Ollama reports a vision-capable model.</div>
      <div class="panel-head" style="margin-top:14px"><div class="panel-title">Self-hosted endpoints</div></div>
      <div class="cfg-row" data-endpoint-row="ollama">
        <label>Ollama</label>
        <input class="cfg-field ai-endpoint" data-endpoint-provider="ollama" type="text" value="${this._esc(ollamaEndpoint)}" placeholder="http://host:11434">
        <button class="mode-chip ai-endpoint-test" data-endpoint-provider="ollama">TEST</button>
      </div>
      <div class="stub-body ai-endpoint-status" data-endpoint-status="ollama"></div>
      <div class="cfg-row" data-endpoint-row="custom">
        <label>OpenAI-compatible</label>
        <input class="cfg-field ai-endpoint" data-endpoint-provider="custom" type="text" value="${this._esc(customEndpoint)}" placeholder="https://host/v1">
        <button class="mode-chip ai-endpoint-test" data-endpoint-provider="custom">TEST</button>
      </div>
      <div class="stub-body ai-endpoint-status" data-endpoint-status="custom"></div>
      <div class="cfg-row">
        <label>Ollama context length</label>
        <input class="cfg-field" id="aiOllamaNumCtx" type="number" min="512" max="262144" step="512" value="${this._esc(cfg.ollama_num_ctx || 8192)}">
      </div>
      <div class="cfg-row">
        <label>Prompt size <span class="toggle-desc">entity names per type; 0 = counts only</span></label>
        <input class="cfg-field" id="aiHomeContextMaxEntities" type="number" min="0" max="50" step="1" value="${this._esc(cfg.home_context_max_entities ?? 15)}">
      </div>
      <div class="new-model-list">${rows}</div>
      <div class="cfg-row" style="margin-top:14px">
        <button class="mode-chip" id="aiApply">APPLY</button>
        <span class="stub-body" id="aiApplyStatus">No unsaved changes.</span>
      </div>
      <div class="panel-head" style="margin-top:14px"><div class="panel-title">Provider Credentials</div></div>
      <div class="stub-body">Stored only in Home Assistant's secrets.yaml, one per provider. A saved credential is never shown here again — only whether one is set. Ollama's is optional, for a protected endpoint only.</div>
      <div class="new-model-list">${credRows}</div>`;
  }

  // Mismatch warnings (Phase 3, v7.108.0): flagged only on strong, specific
  // evidence — never inferred from an unrecognised name, never auto-applied.
  // Selecting the model is still the administrator's call either way.
  _looksOllamaTagged(model) {
    return /^[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$/i.test((model || "").trim());
  }

  _providerPrefixOwners() {
    return [
      { re: /^claude-/i, owner: "anthropic" },
      { re: /^gemini-/i, owner: "gemini" },
      { re: /^(gpt-|o[1-9](-|$))/i, owner: "openai" },
    ];
  }

  // Nova's own well-known text-only defaults (const.py DEFAULT_MODEL /
  // DEFAULT_CLASSIFIER_MODEL / etc, plus a few other common text-only cloud
  // models) — selecting one of these EXACT ids for vision/camera-reasoning
  // is strong evidence of a leftover default rather than a real choice.
  // Deliberately NOT a broad "doesn't look like a vision model" regex —
  // that would warn on every model Nova simply doesn't recognise yet.
  _knownTextOnlyModels() {
    return new Set([
      "openai/gpt-oss-120b", "openai/gpt-oss-20b",
      "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
      "mixtral-8x7b-32768", "deepseek-r1-distill-llama-70b",
    ]);
  }

  _modelMismatchWarning(role, provider, model) {
    const m = (model || "").trim();
    if (!m) return null;
    if (this._looksOllamaTagged(m) && provider !== "ollama") {
      return `"${m}" looks like an Ollama-tagged model (name:tag) — ${provider} is a cloud provider and won't recognise that format.`;
    }
    for (const { re, owner } of this._providerPrefixOwners()) {
      if (re.test(m) && provider !== owner) {
        return `"${m}" looks like a ${owner} model, but the selected provider is ${provider}.`;
      }
    }
    if (role === "vision" && this._knownTextOnlyModels().has(m)) {
      return `"${m}" is one of Nova's own text-only default models — it will reject image input.`;
    }
    const detail = ((this._modelCatalog || {})[provider] || []).find(item => item.id === m);
    const caps = new Set((detail && detail.capabilities) || []);
    if (detail && role === "vision" && !caps.has("vision")) {
      return `"${m}" does not report vision capability.`;
    }
    if (detail && role === "llm" && !caps.has("tools")) {
      return `"${m}" does not report tool-calling capability, which the Main Agent needs.`;
    }
    return null;
  }

  _updateRoleWarning(row) {
    const provSel = row.querySelector(".new-prov-select");
    const modelSel = row.querySelector(".new-model-select");
    const customInput = row.querySelector(".new-model-custom");
    const warnEl = row.querySelector("[data-role-warning]");
    if (!provSel || !modelSel || !warnEl) return;
    const role = row.getAttribute("data-role");
    const model = (customInput && customInput.style.display !== "none")
      ? customInput.value : modelSel.value;
    const warning = this._modelMismatchWarning(role, provSel.value, model);
    warnEl.textContent = warning || "";
    warnEl.hidden = !warning;
  }

  _populateModelSelect(provider, selectEl, res) {
    if (!selectEl) return;
    const cur = selectEl.getAttribute("data-current") || "";
    const models = (res && res.models) || [];
    this._modelCatalog = this._modelCatalog || {};
    this._modelCatalog[provider] = (res && res.model_details) || models.map(id => ({ id, capabilities: [] }));
    let opts = "";
    const label = model => {
      const detail = this._modelCatalog[provider].find(item => item.id === model);
      const caps = (detail && detail.capabilities) || [];
      return caps.length ? `${model} · ${caps.join(", ")}` : model;
    };
    if (models.length) {
      if (!cur) opts += `<option value="" selected disabled>choose a model…</option>`;
      if (cur && !models.includes(cur)) {
          // Never silently replace a saved model just because a live
          // discovery call didn't happen to list it — it may be private,
          // preview, newly released, or simply not returned by this
          // endpoint. Keep it selected and offer the live list alongside
          // it. (Previously this auto-picked and SAVED a different model
          // — often just the alphabetically-first one — on every render.)
        opts += `<option value="${this._esc(cur)}" selected>${this._esc(cur)} — not in the live list</option>`;
        opts += models.map(m => `<option value="${this._esc(m)}">${this._esc(label(m))}</option>`).join("");
      } else {
        opts += models.map(m => `<option value="${this._esc(m)}"${m === cur ? " selected" : ""}>${this._esc(label(m))}</option>`).join("");
      }
    } else {
      const err = res && res.error ? ` — ${String(res.error).slice(0, 48)}` : "";
      opts += (cur ? `<option value="${this._esc(cur)}" selected>${this._esc(cur)}</option>` : "");
      opts += `<option value="" disabled>no models found${this._esc(err)}</option>`;
    }
    opts += `<option value="__custom__">✎ Custom…</option>`;
    selectEl.innerHTML = opts;
    selectEl.title = (res && res.truncated)
      ? "The provider returned more models than fit in one page — list may be incomplete." : "";
    const row = selectEl.closest(".new-model-row");
    if (row) this._updateRoleWarning(row);
  }

  async _loadModelsFor(provider, selectEl, { refresh = false } = {}) {
    if (!this._hass || !selectEl) return;
    try {
      const res = await this._hass.callWS({ type: "nova/list_models", provider, refresh });
      this._populateModelSelect(provider, selectEl, res);
    } catch (_) { /* keep the saved selection and let endpoint Test explain failures */ }
  }

  async _rawSaveConfig(key, value) {
    if (!this._hass || !key) return;
    try {
      await this._hass.callWS({ type: "nova/update_config", key, value });
    } catch (err) {
      console.error(`Nova: failed to save ${key}`, err);
    }
  }

  _wireAiModels() {
    const root = this.shadowRoot;
    const markDirty = message => {
      const status = root.getElementById("aiApplyStatus");
      if (status) status.textContent = message || "Unsaved changes.";
    };
    root.querySelectorAll(".new-model-row").forEach(row => {
      const provSel = row.querySelector(".new-prov-select");
      const modelSel = row.querySelector(".new-model-select");
      const customInput = row.querySelector(".new-model-custom");
      const refreshBtn = row.querySelector(".new-model-refresh");
      if (!provSel || !modelSel) return;
      this._loadModelsFor(provSel.value, modelSel);
      provSel.addEventListener("change", async (e) => {
        const provider = e.target.value;
        modelSel.setAttribute("data-current", "");
        if (customInput) customInput.style.display = "none";
        await this._loadModelsFor(provider, modelSel);
        markDirty();
        this._updateRoleWarning(row);
      });
      modelSel.addEventListener("change", e => {
        if (e.target.value === "__custom__") {
          if (customInput) { customInput.style.display = ""; customInput.focus(); }
          this._updateRoleWarning(row);
          markDirty();
          return;
        }
        if (customInput) customInput.style.display = "none";
        modelSel.setAttribute("data-current", e.target.value);
        markDirty();
        this._updateRoleWarning(row);
      });
      if (customInput) {
        customInput.addEventListener("input", e => {
          const v = (e.target.value || "").trim();
          if (v) modelSel.setAttribute("data-current", v);
          markDirty();
          this._updateRoleWarning(row);
        });
      }
      if (refreshBtn) {
        refreshBtn.addEventListener("click", async () => {
          refreshBtn.disabled = true;
          try {
            await this._loadModelsFor(provSel.value, modelSel, { refresh: true });
          } finally {
            refreshBtn.disabled = false;
          }
        });
      }
    });

    root.querySelectorAll(".ai-endpoint").forEach(input => {
      input.addEventListener("input", () => {
        const provider = input.getAttribute("data-endpoint-provider");
        if (this._modelCatalog) delete this._modelCatalog[provider];
        const status = root.querySelector(`[data-endpoint-status="${provider}"]`);
        if (status) status.textContent = "Endpoint changed. Test it before choosing a profile.";
        markDirty();
      });
    });
    root.getElementById("aiOllamaNumCtx")?.addEventListener("input", () => markDirty());
    root.getElementById("aiHomeContextMaxEntities")?.addEventListener("input", () => markDirty());

    root.querySelectorAll(".ai-endpoint-test").forEach(button => {
      button.addEventListener("click", async () => {
        const provider = button.getAttribute("data-endpoint-provider");
        const input = root.querySelector(`.ai-endpoint[data-endpoint-provider="${provider}"]`);
        const status = root.querySelector(`[data-endpoint-status="${provider}"]`);
        button.disabled = true;
        if (status) status.textContent = "Testing…";
        try {
          const res = await this._hass.callWS({
            type: "nova/test_provider_endpoint", provider,
            endpoint: (input?.value || "").trim(),
          });
          if (!res || !res.ok) throw new Error((res && res.message) || "Endpoint test failed.");
          if (input) input.value = res.endpoint;
          this._modelCatalog = this._modelCatalog || {};
          this._modelCatalog[provider] = res.model_details ||
            res.models.map(id => ({ id, capabilities: [] }));
          root.querySelectorAll(`.new-model-row`).forEach(row => {
            const provSel = row.querySelector(".new-prov-select");
            if (provSel?.value === provider) {
              this._populateModelSelect(provider, row.querySelector(".new-model-select"), res);
            }
          });
          if (status) status.textContent = `Connected. ${res.models.length} model${res.models.length === 1 ? "" : "s"} found.`;
          markDirty("Endpoint tested. Changes are not saved yet.");
        } catch (err) {
          if (status) status.textContent = err?.message || "Could not test this endpoint.";
        } finally {
          button.disabled = false;
        }
      });
    });

    const selectProfileModel = (row, provider, requiredCapability) => {
      const catalog = (this._modelCatalog || {})[provider] || [];
      const match = catalog.find(item =>
        !requiredCapability || (item.capabilities || []).includes(requiredCapability));
      if (!match) return false;
      const provSel = row.querySelector(".new-prov-select");
      const modelSel = row.querySelector(".new-model-select");
      provSel.value = provider;
      modelSel.setAttribute("data-current", match.id);
      this._populateModelSelect(provider, modelSel, {
        models: catalog.map(item => item.id), model_details: catalog,
      });
      modelSel.value = match.id;
      this._updateRoleWarning(row);
      return true;
    };
    root.querySelectorAll("[data-ai-profile]").forEach(button => {
      button.addEventListener("click", () => {
        const profile = button.getAttribute("data-ai-profile");
        if (profile === "manual") {
          markDirty("Manual mode: choose each provider and model, then Apply.");
          return;
        }
        const textRoles = profile === "hybrid"
          ? ["classifier", "reasoning", "camrsn"]
          : ["llm", "classifier", "reasoning", "camrsn"];
        const missing = [];
        textRoles.forEach(role => {
          const row = root.querySelector(`.new-model-row[data-role="${role}"]`);
          const required = role === "llm" ? "tools" : "completion";
          if (!row || !selectProfileModel(row, "ollama", required)) missing.push(role);
        });
        if (profile === "local") {
          const visionRow = root.querySelector('.new-model-row[data-role="vision"]');
          if (visionRow) selectProfileModel(visionRow, "ollama", "vision");
        }
        markDirty(missing.length
          ? "Profile staged where compatible models were found. Test Ollama first to load capabilities for the remaining roles."
          : "Profile staged. Review the choices, then Apply.");
      });
    });

    root.getElementById("aiApply")?.addEventListener("click", async event => {
      const button = event.currentTarget;
      const status = root.getElementById("aiApplyStatus");
      const updates = {
        ollama_base_url: (root.querySelector('.ai-endpoint[data-endpoint-provider="ollama"]')?.value || "").trim(),
        custom_base_url: (root.querySelector('.ai-endpoint[data-endpoint-provider="custom"]')?.value || "").trim(),
        ollama_num_ctx: Number(root.getElementById("aiOllamaNumCtx")?.value || 8192),
        home_context_max_entities: Number(root.getElementById("aiHomeContextMaxEntities")?.value ?? 15),
      };
      root.querySelectorAll(".new-model-row").forEach(row => {
        const provSel = row.querySelector(".new-prov-select");
        const modelSel = row.querySelector(".new-model-select");
        const customInput = row.querySelector(".new-model-custom");
        updates[provSel.getAttribute("data-cfg-key")] = provSel.value;
        updates[modelSel.getAttribute("data-cfg-key")] =
          customInput && customInput.style.display !== "none"
            ? customInput.value.trim() : modelSel.value;
      });
      button.disabled = true;
      if (status) status.textContent = "Checking models and saving…";
      try {
        const res = await this._hass.callWS({ type: "nova/apply_ai_config", updates });
        if (!res || !res.ok) throw new Error((res && res.message) || "Could not apply AI settings.");
        if (status) status.textContent = res.message || "Saved. Nova is reloading.";
      } catch (err) {
        if (status) status.textContent = err?.message || "Could not apply AI settings.";
        button.disabled = false;
      }
    });

    this._wireCredentials();
  }

  // Provider availability (Phase 3, v7.108.0): labels each role's provider
  // <option> as "not configured" when unavailable, WITHOUT disabling it —
  // an administrator can still pick it and add the credential/endpoint
  // right after. Never a value, just a boolean-derived label; each
  // provider's own evidence only (see websocket.py's
  // _compute_provider_availability — this only renders what it returns).
  _markProviderAvailability(available) {
    if (!available) return;
    const root = this.shadowRoot;
    root.querySelectorAll(".new-prov-select").forEach(sel => {
      Array.from(sel.options).forEach(opt => {
        const base = opt.value;
        if (!(base in available)) return;
        opt.textContent = available[base] ? base : `${base} (not configured)`;
      });
    });
  }

  // Provider Credentials (Phase 2, v7.107.0): status is fetched once per
  // render (never cached across renders — a stale "configured" badge after
  // a clear elsewhere would be misleading) and a saved/cleared value is
  // never echoed back by the websocket commands, only `ok`.
  async _wireCredentials() {
    const root = this.shadowRoot;
    const rows = root.querySelectorAll("[data-cred-provider]");
    if (!rows.length || !this._hass) return;

    try {
      const res = await this._hass.callWS({ type: "nova/get_credential_status" });
      const status = (res && res.status) || {};
      root.querySelectorAll("[data-cred-status]").forEach(el => {
        const p = el.getAttribute("data-cred-status");
        const configured = !!status[p];
        el.textContent = configured ? "configured" : "not set";
        el.classList.toggle("cred-configured", configured);
      });
      this._markProviderAvailability(res && res.available);
    } catch (_) { /* leave the "…" placeholder on error */ }

    root.querySelectorAll(".cred-save").forEach(btn => {
      btn.addEventListener("click", async () => {
        const p = btn.getAttribute("data-cred-provider");
        const input = root.querySelector(`.cred-input[data-cred-provider="${p}"]`);
        const value = (input && input.value || "").trim();
        if (!value || !this._hass) return;
        try {
          const res = await this._hass.callWS({ type: "nova/set_credential", provider: p, value });
          if (res && res.ok) {
            input.value = "";
            const statusEl = root.querySelector(`[data-cred-status="${p}"]`);
            if (statusEl) { statusEl.textContent = "configured"; statusEl.classList.add("cred-configured"); }
            this._markProviderAvailability({ [p]: true });
          }
        } catch (err) { console.error(`Nova: failed to save credential for ${p}`, err); }
      });
    });
    root.querySelectorAll(".cred-clear").forEach(btn => {
      btn.addEventListener("click", async () => {
        const p = btn.getAttribute("data-cred-provider");
        if (!this._hass) return;
        if (!window.confirm(`Clear the stored ${p} credential? Any role still using it will stop working until a new key is set.`)) return;
        try {
          const res = await this._hass.callWS({ type: "nova/delete_credential", provider: p });
          if (res && res.ok) {
            const statusEl = root.querySelector(`[data-cred-status="${p}"]`);
            if (statusEl) { statusEl.textContent = "not set"; statusEl.classList.remove("cred-configured"); }
            // custom/ollama availability isn't credential-derived (endpoint
            // / always-on respectively) — only the four cloud providers'
            // availability tracks their own credential.
            if (["groq", "openai", "anthropic", "gemini"].includes(p)) {
              this._markProviderAvailability({ [p]: false });
            }
          }
        } catch (err) { console.error(`Nova: failed to clear credential for ${p}`, err); }
      });
    });
  }

  _briefingsCardBody() {
    const cfg = this._data()?.config || {};
    const onOff = (key, onLabel, offLabel, defaultOn) => {
      const on = defaultOn ? cfg[key] !== false : !!cfg[key];
      return `<button class="toggle-btn ${on ? "on" : "off"}" data-cfg-key="${key}" data-cfg-val="${on ? "false" : "true"}">${on ? onLabel : offLabel}</button>`;
    };
    const feedChip = (key, label) => {
      const on = cfg[key] !== false;
      return `<button class="mode-chip ${on ? "mode-chip-on" : ""}" data-cfg-key="${key}" data-cfg-val="${on ? "false" : "true"}">${label}</button>`;
    };
    return `
      <div class="stub-body">Nova speaks a summary at the times you set — weather and forecast, your calendar, what happened overnight, power draw, and any active hazards nearby.</div>
      <div class="cfg-row">
        <label>Morning</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="cfg-field cfg-num" style="width:64px;text-align:center" type="text" data-cfg-key="briefing_morning_time" value="${this._esc(cfg.briefing_morning_time || "07:30")}" placeholder="07:30">
          ${onOff("briefing_morning_enabled", "ON", "OFF", false)}
        </div>
      </div>
      <div class="cfg-row">
        <label>Evening</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="cfg-field cfg-num" style="width:64px;text-align:center" type="text" data-cfg-key="briefing_evening_time" value="${this._esc(cfg.briefing_evening_time || "19:30")}" placeholder="19:30">
          ${onOff("briefing_evening_enabled", "ON", "OFF", false)}
        </div>
      </div>
      <div class="cfg-row">
        <label>Only when someone's home</label>
        ${onOff("briefing_require_home", "YES", "NO", true)}
      </div>
      <div class="mode-bind-head">Include</div>
      <div class="mode-grid">
        ${feedChip("briefing_include_weather", "Weather")}
        ${feedChip("briefing_include_calendar", "Calendar")}
        ${feedChip("briefing_include_events", "Overnight")}
        ${feedChip("briefing_include_energy", "Energy")}
        ${feedChip("briefing_include_hazards", "Hazards")}
      </div>
      <div class="mode-bind-head">Arrival</div>
      <div class="stub-body">A welcome briefing fires when someone gets home — but only once this door actually opens, not the moment their phone shows them nearby (still in the driveway or car). Leave unset to keep arrival briefings off entirely.</div>
      <div class="cfg-row">
        <label>Front door</label>
        <select class="cfg-field" data-cfg-key="arrival_front_door_entity">${this._optSelect(this._frontDoorOptions(cfg.arrival_front_door_entity || ""), cfg.arrival_front_door_entity || "")}</select>
      </div>
      <div class="cfg-row"><button class="mode-chip" id="newBriefNow">▶ BRIEF ME NOW</button></div>`;
  }

  _voiceConfirmationCardBody() {
    const cfg = this._data()?.config || {};
    const on = !!cfg.voice_confirm_enabled;
    return `
      <div class="stub-body">Ask out loud before sensitive actions (unlock, garage, disarm) and listen for a spoken yes/no. Native mode uses the satellite's own audio; gated mode speaks through the room speaker — run the test to see which your setup supports.</div>
      <div class="cfg-row">
        <label>Voice confirmation</label>
        <button class="toggle-btn ${on ? "on" : "off"}" data-cfg-key="voice_confirm_enabled" data-cfg-val="${on ? "false" : "true"}">${on ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Mode</label>
        <select class="cfg-field" data-cfg-key="voice_confirm_mode">
          ${this._optSelect([["auto", "Auto (try native, fall back)"], ["native", "Native (satellite audio)"], ["gated", "Gated (room speaker)"]], cfg.voice_confirm_mode || "auto")}
        </select>
      </div>
      <div class="cfg-row"><button class="mode-chip" id="newVcTest">▶ TEST SATELLITE AUDIO</button></div>
      <div class="stub-body" id="newVcTestResult"></div>`;
  }

  _satelliteSpeakerCardBody() {
    const cfg = this._data()?.config || {};
    const satellites = cfg.satellites || [];
    const castDevs = cfg.cast_devices || [];
    const pairings = cfg.satellite_pairings || {};
    if (!satellites.length) return `<div class="stub-body">No satellites found.</div>`;
    const rows = satellites.map(sat => {
      const paired = pairings[sat.entity_id] || "";
      const label = sat.area || sat.name;
      return `
        <div class="pairing-row">
          <span class="pairing-label">${this._esc(label)}</span>
          <select class="new-sat-pair-select" data-sat-id="${this._esc(sat.entity_id)}">
            <option value="">— none —</option>
            ${castDevs.map(cd => `<option value="${this._esc(cd.entity_id)}"${cd.entity_id === paired ? " selected" : ""}>${this._esc(cd.name)}</option>`).join("")}
          </select>
        </div>`;
    }).join("");
    return `<div class="pairing-list">${rows}</div>`;
  }

  _announcementSpeakersCardBody() {
    const cfg = this._data()?.config || {};
    const castDevs = cfg.cast_devices || [];
    const selected = cfg.announcement_speakers || [];
    if (!castDevs.length) return `<div class="stub-body">No Cast devices found.</div>`;
    const rows = castDevs.map(cd => {
      const on = selected.includes(cd.entity_id);
      return `
        <div class="toggle-row">
          <span class="toggle-label">${this._esc(cd.name)}</span>
          <span class="toggle-desc">${this._esc(cd.entity_id)}</span>
          <button class="toggle-btn ${on ? "on" : "off"} new-ann-speaker-toggle" data-speaker-id="${this._esc(cd.entity_id)}">${on ? "ON" : "OFF"}</button>
        </div>`;
    }).join("");
    return `<div class="toggle-list">${rows}</div>`;
  }

  _notificationsCardBody() {
    const cfg = this._data()?.config || {};
    const svcs = cfg.notify_services_available || [];
    const selected = Array.isArray(cfg.notify_services)
      ? cfg.notify_services
      : (cfg.notify_service ? [cfg.notify_service] : []);
    if (!this._notifySavePending) this._notifyServicesDraft = [...selected];
    const displayed = this._notifySavePending ? this._notifyServicesDraft : selected;
    if (!svcs.length) return `<div class="stub-body">No notification services found.</div>`;
    const rows = svcs.map(service => {
      const on = displayed.includes(service);
      return `
        <div class="toggle-row">
          <span class="toggle-label">${this._esc(service.replace("notify.", ""))}</span>
          <span class="toggle-desc">${this._esc(service)}</span>
          <button class="toggle-btn ${on ? "on" : "off"} new-notify-service-toggle" data-notify-service="${this._esc(service)}">${on ? "ON" : "OFF"}</button>
        </div>`;
    }).join("");
    return `<div class="stub-body">Normal Nova alerts go to every selected device.</div><div class="toggle-list">${rows}</div>`;
  }

  _securityAlarmCardBody() {
    const cfg = this._data()?.config || {};
    const panels = cfg.alarm_panels || [];
    const selected = cfg.security_alarm_entity || "";
    const opts = [["", "Auto detect a single Alarmo panel"], ...panels.map(p => {
      const suffix = p.platform ? ` (${p.platform})` : "";
      return [p.entity_id, `${p.name}${suffix}`];
    })];
    const automatic = !!cfg.lockdown_auto_on_arm;
    return `
      <div class="stub-body">Nova ignores every other alarm panel for security alerts and lockdown decisions. If more than one Alarmo panel exists, choose the intended household alarm here.</div>
      <div class="cfg-row">
        <label>Security alarm</label>
        <select class="cfg-field" data-cfg-key="security_alarm_entity">${this._optSelect(opts, selected)}</select>
      </div>
      <div class="toggle-row">
        <span class="toggle-label">Automatic lockdown</span>
        <span class="toggle-desc">Allow the selected alarm and sleep mode to lock doors and close covers</span>
        <button class="toggle-btn ${automatic ? "on" : "off"}" data-cfg-key="lockdown_auto_on_arm" data-cfg-val="${automatic ? "false" : "true"}">${automatic ? "ON" : "OFF"}</button>
      </div>`;
  }

  _sentinelRulesCardBody() {
    const cfg = this._data()?.config || {};
    const rules = cfg.sentinel_rules || [];
    const disabled = cfg.disabled_sentinel_rules || [];
    if (!rules.length) return `<div class="stub-body">No sentinel rules found.</div>`;
    const rows = rules.map(r => {
      const isOff = disabled.includes(r.id);
      const name = r.id.replace(/_/g, " ");
      const desc = (r.desc || "").slice(0, 60);
      return `
        <div class="toggle-row">
          <span class="toggle-label">${this._esc(name)}</span>
          <span class="toggle-desc">${this._esc(desc)}</span>
          <button class="toggle-btn ${isOff ? "off" : "on"} new-rule-toggle" data-rule-id="${this._esc(r.id)}">${isOff ? "OFF" : "ON"}</button>
        </div>`;
    }).join("");
    return `<div class="toggle-list">${rows}</div>`;
  }

  // Hazard status is fetched once per element lifetime (same on-demand
  // pattern as Diagnostics) — a manual SCAN NOW re-checks USGS/NWS/EONET.
  async _fetchHazardStatus() {
    if (!this._hass) return;
    try {
      this._hazard = await this._hass.callWS({ type: "nova/hazard", action: "status" });
    } catch (_) { this._hazard = null; }
    if (this._currentTab === "settings") this._render();
  }

  _hazardMonitorCardBody() {
    const cfg = this._data()?.config || {};
    const hz = this._hazard || {};
    const loc = hz.center
      ? (hz.using_override ? `Location: override ${hz.center[0]}, ${hz.center[1]}.` : `Location: home ${hz.center[0]}, ${hz.center[1]}.`)
      : "Location: using home coordinates.";
    const feedChip = (key, label) => {
      const on = cfg[key] !== false;
      return `<button class="mode-chip ${on ? "mode-chip-on" : ""}" data-cfg-key="${key}" data-cfg-val="${on ? "false" : "true"}">${label}</button>`;
    };
    return `
      <div class="stub-body">Real-time nearby earthquakes (USGS), severe-weather warnings (NWS), and natural disasters like wildfires (NASA EONET). Alerts speak and push like any Nova alert.</div>
      <div class="cfg-row">
        <label>Monitor</label>
        <button class="toggle-btn ${cfg.hazard_monitor_enabled ? "on" : "off"}" data-cfg-key="hazard_monitor_enabled" data-cfg-val="${cfg.hazard_monitor_enabled ? "false" : "true"}">${cfg.hazard_monitor_enabled ? "ON" : "OFF"}</button>
      </div>
      <div class="mode-grid">
        ${feedChip("hazard_quakes_on", "Earthquakes")}
        ${feedChip("hazard_weather_on", "Weather")}
        ${feedChip("hazard_disasters_on", "Disasters")}
      </div>
      <div class="stub-body" style="font-family:var(--font-mono);font-size:10.5px">${this._esc(loc)}</div>
      <div class="cfg-row">
        <label>Override lat / lon <span class="toggle-desc">optional</span></label>
        <div style="display:flex;gap:6px">
          <input class="cfg-field cfg-num" style="width:76px" type="text" inputmode="decimal" data-cfg-key="hazard_lat" value="${this._esc(cfg.hazard_lat || "")}" placeholder="lat">
          <input class="cfg-field cfg-num" style="width:76px" type="text" inputmode="decimal" data-cfg-key="hazard_lon" value="${this._esc(cfg.hazard_lon || "")}" placeholder="lon">
        </div>
      </div>
      <div class="cfg-row">
        <label>Quake radius (km) / min mag</label>
        <div style="display:flex;gap:6px">
          <input class="cfg-field cfg-num" style="width:56px" type="text" inputmode="numeric" data-cfg-key="hazard_quake_radius_km" value="${this._esc(cfg.hazard_quake_radius_km ?? 300)}">
          <input class="cfg-field cfg-num" style="width:56px" type="text" inputmode="decimal" data-cfg-key="hazard_quake_min_mag" value="${this._esc(cfg.hazard_quake_min_mag ?? 2.5)}">
        </div>
      </div>
      <div class="cfg-row"><button class="mode-chip" id="newHazScan">⟳ SCAN NOW</button></div>
      <div id="newHazBody" class="stub-body"></div>`;
  }

  // Phase 10 (v7.112.0) — Host Health. Off by default; discovery/mapping
  // status is always shown (so an admin can see what's detected before
  // turning anything on), live readings only populate once enabled and the
  // periodic sampler has run at least once.
  _hostHealthCardBody() {
    const cfg = this._data()?.config || {};
    const status = cfg.host_health_status || {};
    const metrics = status.metrics || [];
    const snap = status.snapshot || {};
    const enabled = cfg.host_health_enabled === true;
    const alertsEnabled = cfg.host_health_alerts_enabled === true;
    const mappings = cfg.host_health_mappings || {};

    const STATUS_CLS = { mapped: "diag-ok", ambiguous: "diag-warn", missing: "diag-off", disabled: "diag-warn" };
    const STATUS_LABEL = { mapped: "OK", ambiguous: "PICK ONE", missing: "MISSING", disabled: "DISABLED" };

    const snapEntryFor = (key) =>
      (snap.available || []).find(a => a.key === key)
      || (snap.problems || []).find(a => a.key === key)
      || (snap.missing_or_stale || []).find(a => a.key === key);

    const metricRow = (m) => {
      const entry = snapEntryFor(m.key);
      const valueText = (entry && typeof entry.value === "number")
        ? `${entry.value.toFixed(entry.unit === "°C" ? 1 : 0)}${entry.unit ? (entry.unit === "%" ? "%" : " " + entry.unit) : ""}`
        : "—";
      const stale = entry && !("value" in entry) && entry.reason === "stale";
      const cands = m.candidates || [];
      const selectHtml = cands.length ? `
        <select class="host-health-map-select" data-metric-key="${this._esc(m.key)}">
          <option value="">${m.source === "auto" ? "— auto —" : "— none —"}</option>
          ${cands.map(c => `<option value="${this._esc(c.entity_id)}"${mappings[m.key] === c.entity_id ? " selected" : ""}>${this._esc(c.friendly_name)}${c.disabled ? " (disabled)" : ""}</option>`).join("")}
        </select>` : "";
      return `
        <div class="cfg-row">
          <label>${this._esc(m.label)}${m.recommended ? "" : ` <span class="toggle-desc">optional</span>`}</label>
          <span style="font-family:var(--font-mono);font-size:11px">${valueText}${stale ? " (stale)" : ""}</span>
          <span class="${STATUS_CLS[m.status] || "diag-off"}">${STATUS_LABEL[m.status] || (m.status || "").toUpperCase()}</span>
        </div>
        ${selectHtml ? `<div class="cfg-row">${selectHtml}</div>` : ""}`;
    };

    const setupNotes = metrics.filter(m => m.recommended && (m.status === "missing" || m.status === "disabled"));
    const setupGuidance = setupNotes.length ? `
      <div class="stub-body">Missing or disabled recommended readings: ${setupNotes.map(m => this._esc(m.label)).join(", ")}. In Home Assistant: Settings → Devices &amp; services → System Monitor → its entities → enable the ones you want (System Monitor disables several by default), then reopen this card.</div>` : "";

    return `
      <div class="stub-body">Reads Home Assistant's own System Monitor sensors for the machine Nova runs on — processor/memory/disk usage, memory &amp; I/O pressure, and (if your hardware exposes it) temperature. Off by default; nothing is read or reported until you turn it on. Disk usage measures capacity, not drive health; I/O pressure measures workload contention, not drive failure. Nova cannot warn you after this machine has completely frozen, since Nova runs on it too.</div>
      <div class="cfg-row">
        <label>Host health awareness</label>
        <button class="toggle-btn ${enabled ? "on" : "off"}" data-cfg-key="host_health_enabled" data-cfg-val="${enabled ? "false" : "true"}">${enabled ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Alerts <span class="toggle-desc">speak/push only once a problem persists — turn off to silence immediately</span></label>
        <button class="toggle-btn ${alertsEnabled ? "on" : "off"}" data-cfg-key="host_health_alerts_enabled" data-cfg-val="${alertsEnabled ? "false" : "true"}" ${enabled ? "" : "disabled"}>${alertsEnabled ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Announce recovery <span class="toggle-desc">bounded, optional</span></label>
        <button class="toggle-btn ${cfg.host_health_recovery_announce !== false ? "on" : "off"}" data-cfg-key="host_health_recovery_announce" data-cfg-val="${cfg.host_health_recovery_announce !== false ? "false" : "true"}" ${enabled && alertsEnabled ? "" : "disabled"}>${cfg.host_health_recovery_announce !== false ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Persistence (minutes) <span class="toggle-desc">how long a problem must persist before the first alert</span></label>
        <input class="cfg-field cfg-num" type="number" min="2" max="120" step="1" data-cfg-key="host_health_persistence_minutes" value="${cfg.host_health_persistence_minutes ?? 10}" ${enabled ? "" : "disabled"}>
      </div>
      <div class="cfg-row">
        <label>Cooldown (minutes) <span class="toggle-desc">minimum gap between repeat alerts on the same unresolved problem</span></label>
        <input class="cfg-field cfg-num" type="number" min="5" max="720" step="1" data-cfg-key="host_health_cooldown_minutes" value="${cfg.host_health_cooldown_minutes ?? 60}" ${enabled ? "" : "disabled"}>
      </div>
      ${setupGuidance}
      <div class="mode-bind-head">Readings</div>
      ${metrics.length ? metrics.map(metricRow).join("") : `<div class="stub-body">Loading detected readings…</div>`}`;
  }

  _renderHazardScan(res) {
    if (!res || res.ok === false) {
      return `<div class="stub-body">${this._esc(res?.error || "No location configured.")}</div>`;
    }
    const q = res.earthquakes || [], w = res.weather || [], d = res.disasters || [];
    if (!q.length && !w.length && !d.length) {
      return `<div class="stub-body">✓ All clear near ${res.center ? res.center[0] + ", " + res.center[1] : "home"} — no active earthquakes, severe weather, or disasters.</div>`;
    }
    let html = "";
    for (const e of q) {
      const mag = (typeof e.mag === "number") ? `M${e.mag.toFixed(1)}` : "M?";
      html += `<div class="stub-body"><b class="diag-warn">${mag}</b> ${this._esc(e.place)} — ${e.dist_km} km away</div>`;
    }
    for (const e of w) {
      html += `<div class="stub-body"><b class="diag-down">${this._esc(e.severity)}</b> ${this._esc(e.event)}${e.area ? " — " + this._esc(e.area) : ""}</div>`;
    }
    for (const e of d) {
      html += `<div class="stub-body"><b class="diag-warn">${this._esc(e.category)}</b> ${this._esc(e.title)} — ${e.dist_km} km away</div>`;
    }
    return html;
  }

  // Energy status is fetched once per element lifetime (same on-demand
  // pattern as Diagnostics/Hazard) — set_agency re-fetches immediately after.
  async _fetchEnergyStatus() {
    if (!this._hass) return;
    try {
      this._energy = await this._hass.callWS({ type: "nova/energy", action: "status" });
    } catch (_) { this._energy = { error: true }; }
    if (this._currentTab === "settings") this._render();
  }

  _energyManagementCardBody() {
    const e = this._energy || {};
    if (e.error) {
      return `<div class="stub-body">Couldn't load energy data — restart Home Assistant after updating.</div>`;
    }
    const draw = e.kw == null
      ? `<span class="diag-off">NO METER</span>`
      : `<span class="${e.over_peak ? "diag-warn" : "diag-ok"}">${e.kw} kW${e.over_peak ? " · OVER PEAK" : ""}</span>`;
    const agencies = ["advisory", "opt_in", "autonomous"];
    const agencyChips = agencies.map(a =>
      `<button class="mode-chip ${a === e.configured_agency ? "mode-chip-on" : ""}" data-agency="${a}">${a.replace("_", "-")}</button>`).join("");
    const advice = (e.advice || []).map(a => `<div class="stub-body">${this._esc(a)}</div>`).join("");
    const running = e.running || [];
    const runRows = running.length
      ? `<div class="mode-bind-head">Running now</div>` + running.map(r =>
          `<div class="cfg-row"><label>${this._esc(r.name || r.entity)}</label><span class="${r.shed_ok ? "" : "diag-warn"}">${r.watts} W${r.shed_ok ? "" : " · protected"}</span></div>`).join("")
      : "";
    const cfg = this._data()?.config || {};
    return `
      <div class="stub-body">Whole-home power, peak awareness, and load advice. Pick how much Nova may act — it never sheds critical loads (fridge, medical, network).</div>
      <div class="cfg-row"><label>Current draw</label>${draw}</div>
      <div class="mode-grid" id="newEnergyAgency">${agencyChips}</div>
      ${advice}
      ${runRows}
      <div class="stub-body">Daily solar report cost (optional): if you already track exact electricity cost, point Nova at your own sensor instead of its price × kWh estimate.</div>
      <div class="cfg-row">
        <label>Cost today entity</label>
        <input class="cfg-field" type="text" data-cfg-key="energy_cost_today_entity" value="${this._esc(cfg.energy_cost_today_entity || "")}" placeholder="sensor.electricity_cost_today">
      </div>
      <div class="cfg-row">
        <label>Net cost today entity (optional)</label>
        <input class="cfg-field" type="text" data-cfg-key="energy_cost_net_entity" value="${this._esc(cfg.energy_cost_net_entity || "")}" placeholder="sensor.net_electricity_cost_today">
      </div>`;
  }

  // Appliances — batch-edit-then-save, like Classic (see nova-panel.js's own
  // #appliance-save comment): rows are added/removed/edited locally and only
  // written on "Save appliances", so this deliberately does NOT go through
  // _saveSetting/_render on every keystroke — that would wipe an unsaved,
  // just-added row.
  _applianceTypes() {
    return ["washer", "dryer", "dishwasher", "oven", "microwave", "appliance"];
  }

  _applianceEntityOptions(selected) {
    const states = this._hass?.states || {};
    const cands = [];
    Object.keys(states).forEach(eid => {
      const s = states[eid];
      const dom = eid.split(".")[0];
      const dc = (s.attributes && s.attributes.device_class) || "";
      const unit = ((s.attributes && s.attributes.unit_of_measurement) || "").toLowerCase();
      const isPower = dc === "power" || dc === "energy" || unit === "w" || unit === "kw";
      const isStatus = (dom === "binary_sensor" || dom === "sensor") &&
        /(washer|dryer|dishwash|laundry|appliance|run_complete|cycle_complete|job_state|machine_state)/i.test(eid);
      if (isPower || isStatus) cands.push(eid);
    });
    cands.sort();
    if (selected && !cands.includes(selected)) cands.unshift(selected);
    return [["", "— no entity (use watts) —"], ...cands.map(eid => {
      const fn = (states[eid] && states[eid].attributes && states[eid].attributes.friendly_name) || eid;
      return [eid, fn];
    })];
  }

  _applianceRowHtml(a) {
    const t = a.type || "appliance";
    return `
      <div class="new-appliance-row">
        <input class="new-appliance-name cfg-field" type="text" placeholder="Name (e.g. Washer)" value="${this._esc(a.name || "")}">
        <select class="new-appliance-type cfg-field">${this._optSelect(this._applianceTypes().map(x => [x, x]), t)}</select>
        <select class="new-appliance-entity cfg-field">${this._optSelect(this._applianceEntityOptions(a.entity || ""), a.entity || "")}</select>
        <input class="new-appliance-watts cfg-field cfg-num" type="number" min="0" step="10" placeholder="watts" value="${a.watts || ""}">
        <button class="new-appliance-remove mode-chip" title="Remove appliance" aria-label="Remove appliance">✕</button>
      </div>`;
  }

  _appliancesCardBody() {
    const cfg = this._data()?.config || {};
    const prof = cfg.appliance_profile || [];
    const rows = prof.map(a => this._applianceRowHtml(a)).join("")
      || `<div class="stub-body">No appliances declared yet. Nova still tracks unidentified power sensors in the background, but only a declared or native appliance ever announces a finished cycle.</div>`;
    return `
      <div class="stub-body">Tell Nova which appliances exist so it names cycles correctly instead of guessing from the whole-home meter. Map a dedicated power or status entity when one exists; otherwise set typical running watts.</div>
      <div class="new-appliance-list" id="newApplianceList">${rows}</div>
      <div class="mode-grid">
        <button class="mode-chip" id="newApplianceAdd">+ Add appliance</button>
        <button class="mode-chip mode-chip-on" id="newApplianceSave">Save appliances</button>
      </div>`;
  }

  _wireAppliances() {
    const root = this.shadowRoot;
    const apList = root.getElementById("newApplianceList");
    const apAdd = root.getElementById("newApplianceAdd");
    const apSave = root.getElementById("newApplianceSave");
    if (apAdd && apList) {
      apAdd.addEventListener("click", () => {
        const empty = apList.querySelector(".stub-body");
        if (empty) empty.remove();
        const tmp = document.createElement("div");
        tmp.innerHTML = this._applianceRowHtml({ name: "", type: "appliance", entity: "", watts: "" });
        const row = tmp.firstElementChild;
        if (row) apList.appendChild(row);
      });
    }
    if (apList) {
      apList.addEventListener("click", (e) => {
        const rm = e.target.closest(".new-appliance-remove");
        if (rm) {
          e.preventDefault();
          rm.closest(".new-appliance-row")?.remove();
        }
      });
    }
    if (apSave) {
      apSave.addEventListener("click", async () => {
        const rows = Array.from(root.querySelectorAll(".new-appliance-row"));
        const out = [];
        rows.forEach(r => {
          const name = (r.querySelector(".new-appliance-name")?.value || "").trim();
          if (!name) return;
          out.push({
            name,
            type: r.querySelector(".new-appliance-type")?.value || "appliance",
            entity: r.querySelector(".new-appliance-entity")?.value || "",
            watts: parseFloat(r.querySelector(".new-appliance-watts")?.value || "0") || 0,
          });
        });
        await this._rawSaveConfig("appliance_profile", JSON.stringify(out));
        try { await this._hass.callWS({ type: "nova/reload_appliances" }); } catch (err) { console.error("Nova: appliance reload failed", err); }
        await this._fetchLiveData();
        if (this._currentTab === "settings") this._render();
      });
    }
  }

  _entName(eid) {
    const st = (this._hass && this._hass.states) ? this._hass.states[eid] : null;
    return (st && st.attributes && st.attributes.friendly_name) || eid;
  }

  _trackerOptions(selected) {
    const states = this._hass?.states || {};
    const cands = Object.keys(states).filter(eid => { const dom = eid.split(".")[0]; return dom === "person" || dom === "device_tracker"; }).sort();
    if (selected && !cands.includes(selected)) cands.unshift(selected);
    return [["", "— none —"], ...cands.map(eid => [eid, this._entName(eid)])];
  }

  _frontDoorOptions(selected) {
    const states = this._hass?.states || {};
    const OPEN_DC = ["door", "garage_door", "opening"];
    const OPEN_RE = /door|entry|front|contact/i;
    const cands = Object.keys(states).filter(eid => {
      if (eid.split(".")[0] !== "binary_sensor") return false;
      const a = states[eid].attributes || {};
      return OPEN_DC.includes(a.device_class || "") || OPEN_RE.test(eid) || OPEN_RE.test(a.friendly_name || "");
    }).sort();
    if (selected && !cands.includes(selected)) cands.unshift(selected);
    return [["", "— none (arrival briefing stays off) —"], ...cands.map(eid => [eid, this._entName(eid)])];
  }

  _travelSensorOptions(selected) {
    const states = this._hass?.states || {};
    const cands = Object.keys(states).filter(eid => {
      const dom = eid.split(".")[0]; if (dom !== "sensor") return false;
      const a = states[eid].attributes || {}, dc = a.device_class || "", unit = a.unit_of_measurement || "";
      return dc === "duration" || /^(min|minutes|h|hr|hrs|hours)$/i.test(unit) || /travel|commute|duration|eta|route|waze|maps|traffic|drive_time|driving|to_work|to_home/i.test(eid);
    }).sort();
    if (selected && !cands.includes(selected)) cands.unshift(selected);
    return [["", "— none —"], ...cands.map(eid => [eid, this._entName(eid)])];
  }

  _anticipationMemoryCardBody() {
    const cfg = this._data()?.config || {};
    const onOff = (key, defaultOn, hint) => {
      const on = defaultOn ? cfg[key] !== false : !!cfg[key];
      return `
        <div class="cfg-row">
          <label>${hint.label}${hint.sub ? `<span class="toggle-desc"> — ${this._esc(hint.sub)}</span>` : ""}</label>
          <button class="toggle-btn ${on ? "on" : "off"}" data-cfg-key="${key}" data-cfg-val="${on ? "false" : "true"}">${on ? "ON" : "OFF"}</button>
        </div>`;
    };
    const num = (key, label, placeholder, min, max, step) => `
      <div class="cfg-row">
        <label>${this._esc(label)}</label>
        <input class="cfg-field cfg-num" type="number" min="${min}" max="${max}" step="${step}" data-cfg-key="${key}" value="${cfg[key] ?? ""}" placeholder="${placeholder}">
      </div>`;
    return `
      ${onOff("departure_alerts_enabled", false, { label: "Departure alerts" })}
      ${onOff("routine_alerts_enabled", false, { label: "Routine alerts" })}
      ${onOff("memory_threading_enabled", false, { label: "Memory threading" })}
      ${onOff("pattern_learn_motion", false, { label: "Learn motion/presence triggers" })}
      ${onOff("adaptive_interruption_budget", false, { label: "Adaptive interruptions", sub: "speak less after alerts are repeatedly marked unhelpful" })}
      ${onOff("adaptive_suggestion_threshold", false, { label: "Adaptive suggestions", sub: "adjust the suggestion bar from past feedback" })}
      ${num("observer_group_debounce", "Sibling-burst coalescing (sec)", "90", 0, 600, 10)}
      ${onOff("continued_conversation_enabled", false, { label: "Continued conversation" })}
      ${onOff("continued_conversation_multi_satellite", false, { label: "Follow me between rooms", sub: "reopen the mic where you moved to (needs 2+ satellites)" })}
      ${onOff("continued_conversation_speaker_reopen", true, { label: "Follow-up mic reopen (speaker-aware)" })}
      ${onOff("tts_use_ha_voice", false, { label: "Use Home Assistant default voice" })}
      ${num("departure_lead_minutes", "Departure lead (min)", "30", 0, 240, 5)}
      ${num("memory_threading_hours", "Memory window (hrs)", "48", 1, 336, 1)}
      ${num("memory_threading_max", "Memory max turns", "12", 1, 50, 1)}
      <div class="cfg-row">
        <label>Origin tracker</label>
        <select class="cfg-field" data-cfg-key="departure_origin_entity">${this._optSelect(this._trackerOptions(cfg.departure_origin_entity || ""), cfg.departure_origin_entity || "")}</select>
      </div>
      <div class="cfg-row">
        <label>OSRM URL</label>
        <input class="cfg-field" type="text" data-cfg-key="departure_osrm_url" value="${this._esc(cfg.departure_osrm_url || "")}" placeholder="self-host (optional)">
      </div>
      <div class="cfg-row">
        <label>Travel sensor</label>
        <select class="cfg-field" data-cfg-key="departure_travel_sensor">${this._optSelect(this._travelSensorOptions(cfg.departure_travel_sensor || ""), cfg.departure_travel_sensor || "")}</select>
      </div>
      <div class="stub-body">Departure warns when to leave for calendar events using your device location + open-source routing. Routine alerts learn per-person timing over about a week. Continued conversation keeps the mic open after a question.</div>`;
  }

  _memoryCardBody() {
    const cfg = this._data()?.config || {};
    const stats = cfg.memory_stats || {};
    return `
      <div class="cfg-row"><label>Backend</label><span>${this._esc(stats.backend || "—")}</span></div>
      <div class="cfg-row"><label>Stored Memories</label><span>${this._esc(stats.total_memories ?? 0)}</span></div>
      <div class="stub-body">Full review, edit, and forget lives on the Memory tab.</div>`;
  }

  _observerTuningCardBody() {
    const s = this._data()?.config?.observer_stats || {};
    const row = (label, value, cls) => `<div class="cfg-row"><label>${this._esc(label)}</label><span class="${cls || ""}">${value}</span></div>`;
    const rateLimit = s.rate_limit ?? 30;
    const presenceRows = (s.presence || []).map(p =>
      row(`${p.name}${p.gps ? " 📍" : ""}`, `${this._esc(p.zone)}${p.distance_km != null ? " · " + p.distance_km + " km" : ""}`)).join("");
    const llmLabel = s.llm_breaker === "open" ? "LOCAL-ONLY" : s.llm_breaker === "half_open" ? "PROBING" : "ONLINE";
    const llmCls = s.llm_breaker === "open" ? "diag-down" : s.llm_breaker === "half_open" ? "diag-warn" : "diag-ok";
    return `
      ${row("Status", s.running ? "RUNNING" : "STOPPED", s.running ? "diag-ok" : "diag-off")}
      ${row("Calls / Hour", `${s.calls_last_hour || 0} / ${rateLimit <= 0 ? "∞" : rateLimit}`)}
      <div class="cfg-row">
        <label>Hourly Cap <span class="toggle-desc">0 = unlimited</span></label>
        <input class="cfg-field cfg-num" type="number" min="0" step="1" id="newObserverRateLimit" value="${rateLimit}">
      </div>
      ${row("Events 24h", s.events_24h || 0)}
      ${row("Flagged 24h", s.flagged_24h || 0)}
      ${row("Spoken 24h", s.spoken_24h || 0)}
      ${row("Cognition", s.cognition_enabled ? "ACTIVE" : "OFF", s.cognition_enabled ? "diag-ok" : "diag-off")}
      ${row("Tracked Entities", s.cog_entities || 0)}
      ${row("Predictable", s.cog_predictable || 0)}
      ${row("Routines Learned", s.cog_routines || 0)}
      ${row("Presence Routines", s.cog_presence || 0)}
      ${presenceRows}
      ${row("Cog Escalated", s.cog_escalated || 0)}
      ${row("Local Decisions", `${s.local_rate || 0}% (${s.local_decisions || 0} local / ${s.cloud_calls || 0} cloud)`)}
      ${row("Learned Patterns", s.learned_patterns || 0)}
      ${row("LLM Link", llmLabel, llmCls)}`;
  }

  _optInEntityDatalist() {
    const states = this._hass?.states || {};
    return Object.keys(states).filter(eid => {
      const dom = eid.split(".")[0];
      return dom === "binary_sensor" || dom === "device_tracker" || dom === "person" || dom === "sensor";
    }).sort().map(eid => `<option value="${this._esc(eid)}">${this._esc(this._entName(eid))}</option>`).join("");
  }

  _plList() {
    let incl = this._data()?.config?.pattern_include_entities || [];
    if (!Array.isArray(incl)) { try { incl = JSON.parse(incl) || []; } catch (_) { incl = []; } }
    return incl;
  }

  _routineLearningCardBody() {
    const cfg = this._data()?.config || {};
    const awarenessAvailable = cfg.observer_enabled !== false && cfg.cognition_enabled !== false && cfg.camera_event_learning !== false;
    const awarenessOn = awarenessAvailable && cfg.camera_historical_awareness !== false;
    const awarenessMinimum = Math.max(3, Math.min(12, Number(cfg.camera_awareness_min_observations ?? 3) || 3));
    const onOff = (key, label, desc) => `
      <div class="toggle-row">
        <span class="toggle-label">${this._esc(label)}</span>
        <span class="toggle-desc">${this._esc(desc)}</span>
        <button class="toggle-btn ${cfg[key] ? "on" : "off"}" data-cfg-key="${key}" data-cfg-val="${cfg[key] ? "false" : "true"}">${cfg[key] ? "ON" : "OFF"}</button>
      </div>`;
    const incl = this._plList();
    const chips = incl.length
      ? incl.map((e, i) => `<span class="new-pl-chip">${this._esc(e)}<button class="new-pl-del" data-i="${i}" title="Remove">×</button></span>`).join("")
      : `<span class="toggle-desc">No specific entities added.</span>`;
    return `
      <div class="stub-body">Nova learns routines from device activity (lights, locks, thermostats…) and skips noisy door/window and presence signals by default. Opt them in to build routines from them.</div>
      <div class="toggle-list">
        ${onOff("camera_event_learning", "Learn from camera detections", "Eufy, Frigate, Nest and Nova's own vision analysis — on by default, no images or faces stored")}
        <div class="toggle-row">
          <span class="toggle-label">What I've noticed lately</span>
          <span class="toggle-desc">Use repeated historical camera patterns in conversation, never as current state</span>
          <button class="toggle-btn ${awarenessOn ? "on" : "off"}" data-cfg-key="camera_historical_awareness" data-cfg-val="${awarenessOn ? "false" : "true"}" ${awarenessAvailable ? "" : "disabled"}>${awarenessOn ? "ON" : "OFF"}</button>
        </div>
        <div class="cfg-row">
          <label>Minimum observations <span class="toggle-desc">across more than one day</span></label>
          <input class="cfg-field cfg-num" type="number" min="3" max="12" step="1" data-cfg-key="camera_awareness_min_observations" value="${awarenessMinimum}" ${awarenessAvailable ? "" : "disabled"}>
        </div>
        ${onOff("pattern_learn_doors", "Learn doors & windows", "Door, window and garage contact sensors")}
        ${onOff("pattern_learn_presence", "Learn presence & arrivals", "People and device trackers (home / away)")}
        ${onOff("pattern_learn_buttons", "Learn button & remote presses", "Suggest “press → scene / action” automations")}
      </div>
      <div class="mode-bind-head">Also learn specific entities <span class="toggle-desc">e.g. a bay occupancy sensor</span></div>
      <div class="cfg-row">
        <input id="newPlEntityInput" list="newPlEntityList" class="cfg-field" style="flex:1" placeholder="type to find an entity…" autocomplete="off">
        <datalist id="newPlEntityList">${this._optInEntityDatalist()}</datalist>
        <button class="mode-chip" id="newPlAddEntity">+ Add</button>
      </div>
      <div class="mode-grid" id="newPlChips">${chips}</div>`;
  }

  _exclArr(v) {
    if (!v) return [];
    if (Array.isArray(v)) return v;
    try { const j = JSON.parse(v); return Array.isArray(j) ? j : []; } catch (_) { return []; }
  }
  _allEntityDatalist() {
    const states = this._hass?.states || {};
    return Object.keys(states).sort().map(eid => `<option value="${this._esc(eid)}">${this._esc(this._entName(eid))}</option>`).join("");
  }
  _domainDatalist() {
    const states = this._hass?.states || {};
    const doms = [...new Set(Object.keys(states).map(e => e.split(".")[0]))].sort();
    return doms.map(dm => `<option value="${this._esc(dm)}">${this._esc(dm)}</option>`).join("");
  }
  _labelDatalist() {
    const labels = this._data()?.available_labels || [];
    return labels.map(l => `<option value="${this._esc(l.name)}">${this._esc(l.name)}</option>`).join("");
  }
  async _exclSave(key, arr) {
    if (this._liveData && this._liveData.config) this._liveData.config[key] = arr;
    await this._saveSetting(key, JSON.stringify(arr));
  }

  _excludedEntitiesCardBody() {
    const cfg = this._data()?.config || {};
    const ents = this._exclArr(cfg.excluded_entities);
    const doms = this._exclArr(cfg.excluded_domains);
    const labs = this._exclArr(cfg.excluded_labels);
    const chipRow = (arr, cls) => arr.length
      ? arr.map((e, i) => `<span class="new-pl-chip">${this._esc(e)}<button class="${cls}" data-i="${i}" title="Remove">×</button></span>`).join("")
      : `<span class="toggle-desc">None.</span>`;
    return `
      <div class="stub-body">Entities you exclude are removed from Nova's awareness — presence detection, room routing, the observer and routine learning all skip them. Home Assistant still has the entity, and Nova can still control it if you ask by name.</div>
      <div class="mode-bind-head">Exclude specific entities</div>
      <div class="cfg-row">
        <input id="newExclEntInput" list="newExclEntList" class="cfg-field" style="flex:1" placeholder="type to find an entity…" autocomplete="off">
        <datalist id="newExclEntList">${this._allEntityDatalist()}</datalist>
        <button class="mode-chip" id="newExclEntAdd">+ Add</button>
      </div>
      <div class="mode-grid" id="newExclEntChips">${chipRow(ents, "new-excl-ent-del")}</div>
      <div class="mode-bind-head">Exclude whole domains <span class="toggle-desc">e.g. light, switch — every entity in the domain</span></div>
      <div class="cfg-row">
        <input id="newExclDomInput" list="newExclDomList" class="cfg-field" style="flex:1" placeholder="type a domain…" autocomplete="off">
        <datalist id="newExclDomList">${this._domainDatalist()}</datalist>
        <button class="mode-chip" id="newExclDomAdd">+ Add</button>
      </div>
      <div class="mode-grid" id="newExclDomChips">${chipRow(doms, "new-excl-dom-del")}</div>
      <div class="mode-bind-head">Exclude by label <span class="toggle-desc">every entity carrying a Home Assistant label</span></div>
      <div class="cfg-row">
        <input id="newExclLabInput" list="newExclLabList" class="cfg-field" style="flex:1" placeholder="type a label…" autocomplete="off">
        <datalist id="newExclLabList">${this._labelDatalist()}</datalist>
        <button class="mode-chip" id="newExclLabAdd">+ Add</button>
      </div>
      <div class="mode-grid" id="newExclLabChips">${chipRow(labs, "new-excl-lab-del")}</div>`;
  }

  // Cameras — enable/rename/location settings deliberately avoid the
  // generic _saveSetting/_render round-trip (see Classic's own
  // _rerenderCameraSettings comment): a full re-render would blow away
  // whatever a user is mid-typing in the rename input, so only the
  // #newCamsetBody sub-tree is patched, matching Classic's #camset-body.
  _renderCameraSettingsRows() {
    const cfg = this._data()?.config || {};
    const cams = cfg.cameras || [];
    if (!cams.length) return `<div class="stub-body">No camera entities in Home Assistant.</div>`;
    const names = cfg.camera_names || {};
    const nOn = cams.filter(c => c.enabled !== false).length;
    const head = `
      <div class="cfg-row">
        <label>${nOn} of ${cams.length} cameras in use</label>
        <div style="display:flex;gap:6px">
          <button class="mode-chip" id="newCamEnableAll">Enable all</button>
          <button class="mode-chip" id="newCamDisableAll">Disable all</button>
        </div>
      </div>`;
    const rows = cams.map(c => {
      const enabled = c.enabled !== false;
      const custom = names[c.entity_id] || "";
      const mode = c.location_mode || "auto";
      const resolved = c.outdoor ? "outdoor" : "indoor";
      const chip = (m, label) => `<button class="mode-chip new-cam-loc-chip ${mode === m ? "mode-chip-on" : ""}" data-loc="${m}" data-cam="${this._esc(c.entity_id)}">${label}</button>`;
      return `
        <div class="new-camset-row" data-cam="${this._esc(c.entity_id)}">
          <div class="cfg-row">
            <label>${this._esc(c.entity_id)}</label>
            <button class="toggle-btn ${enabled ? "on" : "off"} new-cam-enable-toggle" data-cam="${this._esc(c.entity_id)}">${enabled ? "ON" : "OFF"}</button>
          </div>
          <div class="cfg-row">
            <input class="cfg-field new-camset-name" style="flex:1" type="text" data-cam="${this._esc(c.entity_id)}" value="${this._esc(custom)}" placeholder="${this._esc(c.raw_name || c.entity_id)}" autocomplete="off">
          </div>
          <div class="mode-grid">
            ${chip("auto", `AUTO (${resolved})`)}
            ${chip("indoor", "⌂ INDOOR")}
            ${chip("outdoor", "▲ OUTDOOR")}
          </div>
        </div>`;
    }).join("");
    return head + rows;
  }

  _camerasCardBody() {
    const cfg = this._data()?.config || {};
    return `
      <div class="stub-body">Names are Nova-only (HA untouched; blank reverts). Location governs intrusion + outdoor-event filtering — AUTO shows what the heuristics resolve.</div>
      <div class="cfg-row">
        <label>Camera Watch — auto-analyze doorbell and person events</label>
        <button class="toggle-btn ${cfg.camera_auto_analyze !== false ? "on" : "off"}" data-cfg-key="camera_auto_analyze" data-cfg-val="${cfg.camera_auto_analyze !== false ? "false" : "true"}">${cfg.camera_auto_analyze !== false ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Also analyze motion events <span class="toggle-desc">noisier; off by default</span></label>
        <button class="toggle-btn ${cfg.camera_auto_analyze_motion === true ? "on" : "off"}" data-cfg-key="camera_auto_analyze_motion" data-cfg-val="${cfg.camera_auto_analyze_motion === true ? "false" : "true"}">${cfg.camera_auto_analyze_motion === true ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Package Watch — detect packages and mail at the door</label>
        <button class="toggle-btn ${cfg.package_detection !== false ? "on" : "off"}" data-cfg-key="package_detection" data-cfg-val="${cfg.package_detection !== false ? "false" : "true"}">${cfg.package_detection !== false ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Visitor Learning — silently log strangers seen at the door</label>
        <button class="toggle-btn ${cfg.visitor_learning !== false ? "on" : "off"}" data-cfg-key="visitor_learning" data-cfg-val="${cfg.visitor_learning !== false ? "false" : "true"}">${cfg.visitor_learning !== false ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Face recognition source</label>
        <select class="cfg-field" data-cfg-key="recognition_source">${this._optSelect([["both", "Both (Double Take + Frigate)"], ["frigate", "Frigate only (sub_label)"], ["doubletake", "Double Take only"]], cfg.recognition_source || "both")}</select>
      </div>
      <div class="cfg-row">
        <label>Recognition confidence</label>
        <input class="cfg-field cfg-num" type="number" min="0" max="1" step="0.05" data-cfg-key="identity_min_confidence" value="${cfg.identity_min_confidence ?? ""}" placeholder="0.45">
      </div>
      <div id="newCamsetBody">${this._renderCameraSettingsRows()}</div>`;
  }

  _rerenderCameraSettings() {
    const host = this.shadowRoot?.getElementById("newCamsetBody");
    if (!host) return;
    host.innerHTML = this._renderCameraSettingsRows();
    this._wireCameraSettings();
  }

  _wireCameraSettings() {
    const root = this.shadowRoot;
    const applyDisabled = async (next) => {
      try {
        await this._hass.callWS({ type: "nova/update_config", key: "disabled_cameras", value: JSON.stringify(next) });
        if (this._liveData?.config) {
          this._liveData.config.disabled_cameras = next;
          const off = new Set(next);
          (this._liveData.config.cameras || []).forEach(c => { c.enabled = !off.has(c.entity_id); });
        }
        this._rerenderCameraSettings();
      } catch (err) { console.error("Nova: camera enable/disable failed", err); }
    };
    const curDisabled = () => {
      const v = (this._data()?.config || {}).disabled_cameras;
      return Array.isArray(v) ? v.slice() : [];
    };
    root.querySelectorAll(".new-cam-enable-toggle[data-cam]").forEach(btn => {
      btn.addEventListener("click", () => {
        const cam = btn.getAttribute("data-cam"), cur = curDisabled(), isOff = cur.includes(cam);
        applyDisabled(isOff ? cur.filter(c => c !== cam) : [...cur, cam]);
      });
    });
    const enAll = root.getElementById("newCamEnableAll");
    if (enAll) enAll.addEventListener("click", () => applyDisabled([]));
    const disAll = root.getElementById("newCamDisableAll");
    if (disAll) disAll.addEventListener("click", () => applyDisabled(((this._data()?.config || {}).cameras || []).map(c => c.entity_id)));

    root.querySelectorAll(".new-camset-name").forEach(input => {
      input.dataset.saved = input.value;
      const save = async () => {
        const entity = input.getAttribute("data-cam");
        const name = input.value;
        if (name === input.dataset.saved) return;
        try {
          const res = await this._hass.callWS({ type: "nova/rename_camera", entity_id: entity, name });
          input.dataset.saved = name;
          if (this._liveData?.config) {
            this._liveData.config.camera_names = res?.camera_names || {};
            if (Array.isArray(res?.cameras)) this._liveData.config.cameras = res.cameras;
          }
        } catch (err) { console.error("Nova: camera rename failed", err); }
      };
      input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); input.blur(); } });
      input.addEventListener("blur", save);
    });

    root.querySelectorAll(".new-cam-loc-chip").forEach(chipEl => {
      chipEl.addEventListener("click", async () => {
        const entity = chipEl.getAttribute("data-cam");
        const m = chipEl.getAttribute("data-loc");
        chipEl.disabled = true;
        try {
          const res = await this._hass.callWS({ type: "nova/camera_location", entity_id: entity, mode: m });
          if (Array.isArray(res?.cameras) && this._liveData?.config) this._liveData.config.cameras = res.cameras;
          this._rerenderCameraSettings();
        } catch (err) {
          console.error("Nova: camera location failed", err);
          chipEl.disabled = false;
        }
      });
    });
  }

  _dbTrainRow(e) {
    const ts = String(e.ts || "").replace("T", " ").replace("Z", "").slice(5, 16);
    const src = String(e.image_source || "?");
    const cat = e.category || "";
    const desc = this._esc(e.summary || e.analysis || "");
    // "speak" is what Nova would actually say aloud for this event — logged
    // regardless of whether announcements_enabled let it through, so you can
    // see after the fact what a notable event would have sounded like.
    const speak = (e.speak || "").trim();
    const speakLine = speak ? `<div class="toggle-desc" style="margin-top:2px"><i>"${this._esc(speak)}"</i></div>` : "";
    return `
      <div class="cfg-row"${e.notable ? ' style="color:var(--gold)"' : ""}>
        <label>${this._esc(ts)} · ${this._esc(src)}${cat ? " · " + this._esc(cat) : ""}</label>
        <span class="toggle-desc">${desc}</span>
      </div>
      ${speakLine}`;
  }

  _doorbellTrainingCardBody() {
    const t = this._data()?.doorbellTraining || {};
    const stats = t.stats || {};
    const events = t.recent || [];
    const patterns = t.patterns || [];
    const total = stats.total || 0;
    const notable = stats.notable || 0;
    const bySource = stats.by_source || {};
    const srcLine = Object.keys(bySource).length
      ? Object.entries(bySource).map(([k, v]) => `${k} ${v}`).join(" · ")
      : "none yet";
    const rows = events.length
      ? events.slice().reverse().map(e => this._dbTrainRow(e)).join("")
      : `<div class="stub-body">No analysed doorbell events yet. Run a backlog scan, or wait for the next doorbell press.</div>`;
    // Patterns are timing-only — no names, no face matching. Nova has no
    // local face model; that needs Frigate or DoubleTake (recognition.py),
    // neither configured here. This just clusters the vision model's own
    // category label (delivery/mail/person/...) by camera and time of day.
    const patternsBlock = patterns.length
      ? `<div class="mode-bind-head">Recurring patterns (timing only — not face recognition)</div>
         <ul style="margin:0 0 10px;padding-left:18px;font-size:12px;color:var(--ink-dim);line-height:1.7">
           ${patterns.map(p => `<li>${this._esc(p.description)}</li>`).join("")}
         </ul>`
      : `<div class="stub-body">No recurring patterns yet — needs a few more days of data, or nothing repeats at a consistent time yet.</div>`;
    return `
      <div class="stub-body">Analysed doorbell events — Nova's visitor training data. Each press is logged automatically; run a backlog scan to mine the recorded-event history into the dataset.</div>
      <div class="cfg-row">
        <label>Scan limit</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input id="newDbtLimit" class="cfg-field cfg-num" type="number" min="1" max="500" value="40" title="Max events to analyse">
          <button class="mode-chip" id="newDbtScan">Scan backlog</button>
        </div>
      </div>
      <div class="stub-body">${total} analysed · ${notable} notable · ${this._esc(srcLine)}</div>
      ${patternsBlock}
      ${rows}`;
  }

  // Wellbeing status is fetched once per element lifetime (same on-demand
  // pattern as Diagnostics/Hazard/Energy).
  async _fetchBio() {
    if (!this._hass) return;
    try {
      this._bio = await this._hass.callWS({ type: "nova/biometrics", action: "status" });
    } catch (_) { this._bio = { error: true }; }
    if (this._currentTab === "settings") this._render();
  }

  _wellbeingContextCardBody() {
    const b = this._bio || {};
    if (b.error) {
      return `<div class="stub-body">Couldn't load — restart Home Assistant after updating.</div>`;
    }
    const status = b.enabled
      ? `<span class="diag-ok">ON · ${b.found || 0} sensor${b.found === 1 ? "" : "s"}</span>`
      : `<span class="diag-off">OFF</span>`;
    const ents = b.entities || [];
    let body;
    if (!b.enabled) {
      body = `<div class="stub-body">Off — enable to let Nova use wearable context. Health readings are never diagnosed or alarmed on.</div>`;
    } else if (!ents.length) {
      body = `<div class="stub-body">No wearable entities found. Connect a wearable integration (Withings, Google Fit, Oura, etc.) to Home Assistant.</div>`;
    } else {
      body = ents.map(e =>
        `<div class="cfg-row"><label>${this._esc((e.kind || "").replace(/_/g, " "))}</label><span>${this._esc(e.value)}${e.unit ? " " + this._esc(e.unit) : ""}</span></div>`).join("");
    }
    return `
      <div class="stub-body">Lets Nova read a connected wearable (heart rate, sleep, steps) so it can be quieter when you're resting. Context only — not medical. Off by default; health data stays private.</div>
      <div class="cfg-row">
        <label>Status</label>
        <div style="display:flex;align-items:center;gap:8px">${status}<button class="mode-chip" id="newBioToggle">${b.enabled ? "✕ DISABLE" : "◉ ENABLE"}</button></div>
      </div>
      ${body}`;
  }

  _characterResearchCardBody() {
    const cfg = this._data()?.config || {};
    return `
      <div class="cfg-row">
        <label>Banter level</label>
        <select class="cfg-field" data-cfg-key="banter_level">
          ${this._optSelect([["0", "Plain — no wit"], ["1", "Dry — occasional wit (default)"], ["2", "Full — expressive wit"]], String(cfg.banter_level ?? "1"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Web research backend</label>
        <select class="cfg-field" data-cfg-key="search_backend">
          ${this._optSelect([["duckduckgo", "DuckDuckGo (no key, default)"], ["searxng", "SearXNG (self-hosted)"]], cfg.search_backend || "duckduckgo")}
        </select>
      </div>
      <div class="cfg-row">
        <label>SearXNG URL</label>
        <input class="cfg-field" type="text" data-cfg-key="searxng_url" value="${this._esc(cfg.searxng_url || "")}" placeholder="http://searxng.local:8080" autocomplete="off">
      </div>
      <div class="cfg-row">
        <label>Calendar tight gap <span class="toggle-desc">minutes between events treated as back-to-back</span></label>
        <input class="cfg-field cfg-num" type="number" min="0" max="120" step="5" data-cfg-key="calendar_tight_gap_min" value="${this._esc(cfg.calendar_tight_gap_min ?? 15)}">
      </div>`;
  }

  // Document Library (RAG) — fetched once per element lifetime, like the
  // other on-demand cards (Diagnostics/Hazard/Energy/Wellbeing).
  async _fetchDocLibrary() {
    if (!this._hass) return;
    try {
      this._docLib = await this._hass.callWS({ type: "nova/documents", action: "status" });
    } catch (_) { this._docLib = { error: true }; }
    if (this._currentTab === "settings") this._render();
  }

  async _fetchVectorBackend() {
    if (!this._hass) return;
    try {
      this._vecbk = await this._hass.callWS({ type: "nova/semantic_search", action: "status" });
    } catch (_) { this._vecbk = { error: true }; }
    if (this._currentTab === "settings") this._render();
  }

  _renderDocLibraryList() {
    const d = this._docLib || {};
    if (d.error) return `<div class="stub-body">Couldn't reach the library — restart Home Assistant after updating, then reopen.</div>`;
    const sources = d.sources || [];
    if (!sources.length) {
      return `<div class="stub-body">No documents ingested yet. Add PDF/.txt/.md files to <code>/config/nova/documents</code> and press Ingest.${d.chroma ? "" : " (Vector search needs ChromaDB; keyword fallback is active.)"}</div>`;
    }
    return sources.map(s => `
      <div class="cfg-row">
        <label>${this._esc(s.source)}</label>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="toggle-desc">${s.chunks} chunks</span>
          <button class="new-doclib-del" data-src="${this._esc(s.source)}" title="Remove document">✕</button>
        </div>
      </div>`).join("");
  }

  _renderDocSearchResults(hits) {
    if (!hits || !hits.length) return `<div class="stub-body">No matches. Try different words, or ingest more documents.</div>`;
    return hits.map(h => {
      const score = (h.score != null) ? ` · ${Math.round(h.score * 100)}%` : "";
      const excerpt = (h.text || "").slice(0, 220);
      return `<div class="stub-body"><b>${this._esc(h.source || "?")}${score}</b><br>${this._esc(excerpt)}${h.text && h.text.length > 220 ? "…" : ""}</div>`;
    }).join("");
  }

  _renderVectorBackendBody() {
    const v = this._vecbk || {};
    if (v.error) return "";
    if (v.enabled) {
      return `
        <div class="cfg-row"><label>Search</label><span class="diag-ok">◉ SEMANTIC (Ollama)</span></div>
        <div class="stub-body">Meaning-based matching via Ollama ${this._esc(v.model || "nomic-embed-text")}${v.vector_count ? ` · ${v.vector_count} vectors` : " · re-ingest to embed your documents"}.</div>
        <div class="cfg-row"><button class="mode-chip" id="newVecbkToggle" data-mode="disable">✕ DISABLE SEMANTIC SEARCH</button></div>`;
    }
    if (!v.ollama_configured) {
      return `
        <div class="cfg-row"><label>Search</label><span class="diag-off">KEYWORD (FTS)</span></div>
        <div class="stub-body">Works everywhere with no setup. Semantic search needs an Ollama host — set the LLM base URL to your Ollama server and pull an embed model (ollama pull nomic-embed-text).</div>`;
    }
    return `
      <div class="cfg-row"><label>Search</label><span class="diag-off">KEYWORD (FTS)</span></div>
      <div class="stub-body">Enable semantic search to match on meaning, using your Ollama server (${this._esc(v.model || "nomic-embed-text")}). No install, no ChromaDB. Re-ingest afterward to embed existing docs.</div>
      <div class="cfg-row"><button class="mode-chip" id="newVecbkToggle" data-mode="enable">⬆ ENABLE SEMANTIC SEARCH</button></div>`;
  }

  _documentLibraryCardBody() {
    const d = this._docLib || {};
    const backend = d.chroma ? "VECTOR" : d.fts ? "KEYWORD" : "NONE";
    return `
      <div class="stub-body">Drop manuals &amp; receipts (PDF, .txt, .md) into <code>/config/nova/documents</code> or upload below, then ingest. Ask Nova "what's the furnace filter size?" and it answers from your paperwork.</div>
      <div class="cfg-row"><label>Backend</label><span>${this._esc(backend)} · ${d.chunk_count || 0} chunks</span></div>
      ${this._renderVectorBackendBody()}
      <div class="mode-bind-head">Library</div>
      <div class="cfg-row">
        <button class="mode-chip" id="newDoclibUpload">⬆ UPLOAD FILE</button>
        <input type="file" id="newDoclibFile" accept=".pdf,.txt,.md" style="display:none">
        <button class="mode-chip" id="newDoclibIngest">⟳ INGEST FOLDER</button>
      </div>
      <div class="cfg-row">
        <input id="newDoclibSearch" class="cfg-field" style="flex:1" type="text" placeholder="test a search — e.g. furnace filter size" autocomplete="off">
      </div>
      <div id="newDoclibBody">${this._renderDocLibraryList()}</div>
      <div class="mode-bind-head">Watch folders <span class="toggle-desc">one per line/comma, e.g. /media/downloads</span></div>
      <div class="cfg-row">
        <input id="newDoclibWatch" class="cfg-field" style="flex:1" type="text" value="${this._esc(this._docLibWatchValue())}" autocomplete="off">
        <button class="mode-chip" id="newDoclibScan">⟳ SCAN WATCH</button>
      </div>`;
  }

  _docLibWatchValue() {
    const wf = this._data()?.config?.document_watch_folders;
    if (!wf) return "";
    return Array.isArray(wf) ? wf.join("\n") : wf;
  }

  _rerenderDocLibraryBody() {
    const host = this.shadowRoot?.getElementById("newDoclibBody");
    if (!host) return;
    host.innerHTML = this._renderDocLibraryList();
    this._wireDocLibraryDeletes();
  }

  _wireDocLibraryDeletes() {
    this.shadowRoot?.querySelectorAll(".new-doclib-del").forEach(btn => {
      btn.addEventListener("click", async () => {
        const src = btn.getAttribute("data-src");
        if (!src || !this._hass) return;
        if (!window.confirm(`Remove "${src}" from the library? This deletes the file and its indexed chunks.`)) return;
        try {
          await this._hass.callWS({ type: "nova/documents", action: "delete", filename: src });
          await this._fetchDocLibrary(); // triggers a full _render() when in the settings tab
        } catch (err) { console.error("Nova: document delete failed", err); }
      });
    });
  }

  _wireDocLibrary() {
    const root = this.shadowRoot;
    this._wireDocLibraryDeletes();

    const ingestBtn = root.getElementById("newDoclibIngest");
    if (ingestBtn) {
      ingestBtn.addEventListener("click", async () => {
        if (!this._hass) return;
        ingestBtn.disabled = true;
        const orig = ingestBtn.textContent;
        ingestBtn.textContent = "⟳ INGESTING…";
        try {
          await this._hass.callWS({ type: "nova/documents", action: "ingest" });
          await this._fetchDocLibrary();
        } catch (err) {
          console.error("Nova: ingest failed", err);
        } finally {
          ingestBtn.disabled = false;
          ingestBtn.textContent = orig;
        }
      });
    }

    const q = root.getElementById("newDoclibSearch");
    if (q) {
      q.addEventListener("keydown", async (ev) => {
        if (ev.key !== "Enter") return;
        ev.preventDefault();
        const query = q.value.trim();
        if (!query || !this._hass) { this._rerenderDocLibraryBody(); return; }
        try {
          const res = await this._hass.callWS({ type: "nova/documents", action: "search", query });
          const host = root.getElementById("newDoclibBody");
          if (host) host.innerHTML = this._renderDocSearchResults(res?.results || []);
        } catch (err) { console.error("Nova: document search failed", err); }
      });
    }

    const upBtn = root.getElementById("newDoclibUpload");
    const fileInput = root.getElementById("newDoclibFile");
    if (upBtn && fileInput) {
      upBtn.addEventListener("click", () => fileInput.click());
      fileInput.addEventListener("change", async () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file || !this._hass) return;
        if (file.size > 25 * 1024 * 1024) { fileInput.value = ""; return; }
        upBtn.disabled = true;
        const orig = upBtn.textContent;
        upBtn.textContent = "⬆ UPLOADING…";
        try {
          const b64 = await new Promise((resolve, reject) => {
            const r = new FileReader();
            r.onload = () => resolve(String(r.result).split(",")[1] || "");
            r.onerror = () => reject(new Error("read failed"));
            r.readAsDataURL(file);
          });
          const res = await this._hass.callWS({ type: "nova/documents", action: "upload", filename: file.name, content: b64 });
          if (res.ok) {
            await this._fetchDocLibrary();
          }
        } catch (err) {
          console.error("Nova: document upload failed", err);
        } finally {
          upBtn.disabled = false;
          upBtn.textContent = orig;
          fileInput.value = "";
        }
      });
    }

    const watchField = root.getElementById("newDoclibWatch");
    const saveWatch = async () => {
      if (!this._hass || !watchField) return;
      try { await this._hass.callWS({ type: "nova/update_config", key: "document_watch_folders", value: watchField.value.trim() }); } catch (_) {}
    };
    if (watchField) watchField.addEventListener("blur", saveWatch);

    const scanBtn = root.getElementById("newDoclibScan");
    if (scanBtn) {
      scanBtn.addEventListener("click", async () => {
        if (!this._hass) return;
        await saveWatch();
        scanBtn.disabled = true;
        const orig = scanBtn.textContent;
        scanBtn.textContent = "⟳ SCANNING…";
        try {
          const res = await this._hass.callWS({ type: "nova/documents", action: "scan_watch" });
          if (res.watched > 0) {
            await this._fetchDocLibrary();
          }
        } catch (err) {
          console.error("Nova: watch scan failed", err);
        } finally {
          scanBtn.disabled = false;
          scanBtn.textContent = orig;
        }
      });
    }

    const vecbkToggle = root.getElementById("newVecbkToggle");
    if (vecbkToggle) {
      vecbkToggle.addEventListener("click", async () => {
        if (!this._hass) return;
        const mode = vecbkToggle.getAttribute("data-mode") || "enable";
        try {
          await this._hass.callWS({ type: "nova/semantic_search", action: mode });
        } catch (err) {
          console.error("Nova: semantic search toggle failed", err);
        }
        await this._fetchVectorBackend();
      });
    }
  }

  _mediaPlayerOptions(selected) {
    const states = this._hass?.states || {};
    const eids = Object.keys(states).filter(e => e.startsWith("media_player.")).sort();
    if (selected && !eids.includes(selected)) eids.unshift(selected);
    return [["", "— none —"], ...eids.map(e => {
      const st = states[e];
      const fn = (st && st.attributes && st.attributes.friendly_name) || e;
      return [e, fn];
    })];
  }

  _optSelect(pairs, current) {
    return pairs.map(([v, label]) => `<option value="${this._esc(v)}"${v === current ? " selected" : ""}>${this._esc(label)}</option>`).join("");
  }

  _esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  _renderData() {
    if (!this._renderedOnce) return;
    const d = this._data();
    const root = this.shadowRoot;
    if (!d) return;

    const onboardingMount = root.getElementById("onboardingMount");
    if (onboardingMount) {
      onboardingMount.innerHTML = this._onboardingHtml(d.onboarding);
      this._wireOnboarding();
    }

    // hero state line
    const state = this._coreState();
    const lineEl = root.getElementById("stateLine");
    const subEl = root.getElementById("stateSub");
    const lines = {
      idle: ["Watching over the house.", "ALL QUIET · NOTHING NEEDS YOU RIGHT NOW"],
      reasoning: ["Something just happened.", "CHECK THE ACTIVITY FEED BELOW"],
      asleep: ["Everyone's asleep. Staying quiet.", "A GROUND-FLOOR BREACH WOULD STILL WAKE ME"],
    };
    if (lineEl) lineEl.textContent = lines[state][0];
    if (subEl) subEl.textContent = lines[state][1];
    this._targetCoreState(state);

    // status chips
    const chipDefs = [
      ["Observer", d.status.observer], ["Sleep", d.status.sleep], ["Broadcast", d.status.broadcast],
      ["Notify", d.status.notify], ["Satellites", d.status.satellites],
    ];
    const chipsEl = root.getElementById("chips");
    if (chipsEl) {
      chipsEl.innerHTML = chipDefs.map(([label, s]) => {
        const warn = (s?.level === "warn") ? " warn" : "";
        return `<div class="chip${warn}"><span class="dot"></span> ${this._esc(label)} <b>${this._esc(s?.state ?? "—")}</b></div>`;
      }).join("");
    }

    // Formal lockdown is deliberately separate from the alarm controls. It
    // only calls Nova's guarded lockdown command and always asks for a human
    // confirmation before changing state.
    const lockdown = d.lockdown || {};
    const lockdownBtn = root.getElementById("lockdownControl");
    if (lockdownBtn) {
      lockdownBtn.hidden = false;
      lockdownBtn.classList.toggle("active", !!lockdown.active);
      lockdownBtn.textContent = lockdown.active ? "LOCKDOWN ACTIVE" : "LOCKDOWN OFF";
      lockdownBtn.title = lockdown.reason || "Nova formal lockdown";
    }

    // activity feed
    const entries = (this._activityData && this._activityData.length)
      ? this._activityData
      : [{ ts: "--:--", tag: "SYSTEM", msg: "No activity yet." }];
    const feedEl = root.getElementById("feed");
    if (feedEl) {
      feedEl.innerHTML = entries.map(e => `
        <div class="feed-row">
          <div class="feed-text"><b>${this._esc(e.tag || "")}</b> · <span class="dim">${this._esc(e.msg || "")}</span></div>
          <div class="feed-time">${this._esc(e.ts || "")}</div>
        </div>`).join("");
    }
    const feedMeta = root.getElementById("feedMeta");
    if (feedMeta) feedMeta.textContent = `LAST ${entries.length}`;

    // areas
    const areasGridEl = root.getElementById("areasGrid");
    if (areasGridEl) {
      areasGridEl.innerHTML = (d.areas || []).map(a => this._areaTileHtml(a)).join("");
      // Re-wire on every patch — innerHTML above just replaced these nodes,
      // so any listeners from a previous _renderData() are already gone.
      areasGridEl.querySelectorAll(".area-light-toggle[data-light-area]").forEach(btn => {
        btn.addEventListener("click", () => {
          const areaId = btn.getAttribute("data-light-area");
          const name = btn.getAttribute("data-area-name") || "Area";
          const isOn = btn.classList.contains("on");
          this._toggleAreaLights(areaId, name, isOn);
        });
      });
    }
    const areasMeta = root.getElementById("areasMeta");
    if (areasMeta) areasMeta.textContent = `${d.occupied} OCCUPIED · ${d.areasMonitored} MONITORED`;

    this._renderSolarPanel();

    const cog = this._cognitive || {};
    const learning = cog.learning || {};
    const cognitiveState = root.getElementById("cognitiveState");
    if (cognitiveState) cognitiveState.textContent = cog.running === false ? "STOPPED" : (cog.running ? "RUNNING" : "UNAVAILABLE");
    const cognitiveMetrics = root.getElementById("cognitiveMetrics");
    if (cognitiveMetrics) {
      const metrics = [
        ["Days learned", learning.days_of_data ?? 0],
        ["State changes", learning.state_changes ?? 0],
        ["Commands", learning.commands ?? 0],
        ["Suggestions", learning.suggestions ?? 0],
        ["Actions", cog.actions_taken ?? 0],
        ["Ignore rules", cog.ignore_rules ?? 0],
      ];
      cognitiveMetrics.innerHTML = metrics.map(([label, value]) =>
        `<div class="metric"><b>${this._esc(value)}</b><span>${this._esc(label)}</span></div>`).join("");
    }
    const cognitiveAnalysis = root.getElementById("cognitiveAnalysis");
    if (cognitiveAnalysis) {
      const analysis = cog.last_analysis || {};
      cognitiveAnalysis.textContent = analysis.summary || analysis.message || "Nova learns from household patterns locally.";
    }

    const goals = d.goals || [];
    const goalList = root.getElementById("goalList");
    const goalsMeta = root.getElementById("goalsMeta");
    if (goalsMeta) goalsMeta.textContent = `${goals.filter(g => g.status === "active").length} ACTIVE`;
    if (goalList) {
      goalList.innerHTML = goals.length ? goals.map(g => {
        const active = g.status === "active";
        const progress = g.steps_total ? `${g.steps_done || 0}/${g.steps_total} STEPS` : "OPEN OUTCOME";
        return `<div class="goal-row">
          <div class="goal-copy"><b>${this._esc(g.title || g.outcome || `Goal ${g.id}`)}</b>
            <span>${this._esc(g.outcome || "")}</span>
            <small>${this._esc(String(g.status || "active").toUpperCase())} · ${this._esc(progress)}</small></div>
          <button class="mode-chip goal-action" data-goal-id="${this._esc(g.id)}" data-goal-action="${active ? "cancel" : "delete"}">${active ? "CANCEL" : "DELETE"}</button>
        </div>`;
      }).join("") : `<div class="empty-state">No goals yet.</div>`;
      this._wireGoalActions();
    }

    // camera — collapsed, optional, honest
    const camPanel = root.getElementById("cameraPanel");
    const camStrip = root.getElementById("camStrip");
    if (camPanel && camStrip) {
      const cams = d.cameras || [];
      camPanel.hidden = cams.length === 0;
      const camToggle = root.getElementById("camToggle");
      if (camToggle) camToggle.textContent = this._camOpen ? "HIDE CAMERAS ▴" : `SHOW ${cams.length} CAMERA${cams.length === 1 ? "" : "S"} ▾`;
      camStrip.classList.toggle("open", this._camOpen);
      camStrip.innerHTML = cams.map(c => {
        const eid = c.entity_id;
        const image = this._cameraImages[eid];
        const diag = this._cameraDiagnostics[eid];
        const body = image
          ? `<img src="data:image/jpeg;base64,${image}" alt="${this._esc(c.name || eid)} snapshot">`
          : `<div class="camera-empty">${this._cameraLoading[eid] ? "LOADING…" : "NO SNAPSHOT"}</div>`;
        return `<div class="camera-slot" data-camera="${this._esc(eid)}">
          ${body}<div class="camera-caption"><b>${this._esc(c.name || eid)}</b><span>${this._esc(eid)}</span></div>
          <div class="camera-actions"><button class="mode-chip camera-refresh" data-camera="${this._esc(eid)}">REFRESH</button>
            <button class="mode-chip camera-analyze" data-camera="${this._esc(eid)}">ANALYZE</button>
            <button class="mode-chip camera-diagnose" data-camera="${this._esc(eid)}">DIAGNOSE</button></div>
          ${diag ? `<div class="camera-diagnostic">${this._esc(diag)}</div>` : ""}
        </div>`;
      }).join("");
      this._wireCameraActions();
    }
    this._localizeDOM(root);
  }

  async _refreshCameraSnapshot(entityId) {
    if (!this._hass || !entityId || this._cameraLoading[entityId]) return;
    this._cameraLoading[entityId] = true;
    this._renderData();
    try {
      const res = await this._hass.callWS({ type: "nova/camera_snapshot", entity_id: entityId });
      this._cameraImages[entityId] = res?.image || null;
    } catch (err) {
      this._cameraImages[entityId] = null;
      this._cameraDiagnostics[entityId] = err?.message || "Snapshot failed";
    } finally {
      this._cameraLoading[entityId] = false;
      this._renderData();
    }
  }

  _wireCameraActions() {
    const root = this.shadowRoot;
    root.querySelectorAll(".camera-refresh").forEach(btn => btn.addEventListener("click", () =>
      this._refreshCameraSnapshot(btn.getAttribute("data-camera"))));
    root.querySelectorAll(".camera-analyze").forEach(btn => btn.addEventListener("click", async () => {
      const entityId = btn.getAttribute("data-camera");
      btn.disabled = true;
      try {
        await this._hass.callService("nova", "analyze_camera", { entity_id: entityId, announce: false });
        this._cameraDiagnostics[entityId] = "Analysis requested. Results will appear in Activity.";
      } catch (err) { this._cameraDiagnostics[entityId] = err?.message || "Analysis failed"; }
      btn.disabled = false;
      this._renderData();
    }));
    root.querySelectorAll(".camera-diagnose").forEach(btn => btn.addEventListener("click", async () => {
      const entityId = btn.getAttribute("data-camera");
      btn.disabled = true;
      try {
        const res = await this._hass.callWS({ type: "nova/camera_diagnostics", entity_id: entityId });
        this._cameraDiagnostics[entityId] = res?.probe?.verdict || "No diagnostic result.";
      } catch (err) { this._cameraDiagnostics[entityId] = err?.message || "Diagnostics failed"; }
      btn.disabled = false;
      this._renderData();
    }));
  }

  _wireGoalActions() {
    this.shadowRoot.querySelectorAll(".goal-action").forEach(btn => btn.addEventListener("click", async () => {
      const action = btn.getAttribute("data-goal-action");
      if (!window.confirm(`${action === "cancel" ? "Cancel" : "Delete"} this goal?`)) return;
      await this._goalAction({ action, goal_id: Number(btn.getAttribute("data-goal-id")) });
    }));
  }

  async _goalAction(payload) {
    const out = this.shadowRoot.getElementById("goalResult");
    try {
      const res = await this._hass.callWS({ type: "nova/goal_action", ...payload });
      if (this._liveData && Array.isArray(res?.goals)) this._liveData.goals = res.goals;
      if (out) out.textContent = "Saved.";
      this._renderData();
    } catch (err) {
      if (out) out.textContent = err?.message || "Goal action failed.";
    }
  }

  // Canonical order + icon per capability, matching the backend's own
  // ordering (websocket.py's area-caps builder) so a room with many
  // capabilities always shows them in the same, sensible sequence.
  static AREA_CAP_ORDER = ["sat", "spkr", "mmwave", "cam", "light", "switch", "lock", "climate", "door", "leak", "alarm"];
  static AREA_CAP_ICON = {
    sat: "🛰️", spkr: "🔊", mmwave: "📡", cam: "📷", light: "💡", switch: "🔌",
    lock: "🔒", climate: "🌡️", door: "🚪", leak: "💧", alarm: "🔔",
  };

  _areaSparklineSvg(values, color) {
    if (!values || values.length < 2) return "";
    const w = 60, h = 16, pad = 1;
    const min = Math.min(...values), max = Math.max(...values), range = (max - min) || 1;
    const step = (w - pad * 2) / (values.length - 1);
    const pts = values.map((v, i) => {
      const x = pad + i * step;
      const y = h - pad - ((v - min) / range) * (h - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  }

  _areaTileHtml(a) {
    const caps = (a.caps || []).slice().sort((x, y) =>
      NovaPanel.AREA_CAP_ORDER.indexOf(x) - NovaPanel.AREA_CAP_ORDER.indexOf(y)).slice(0, 5);
    const capsRow = caps.length
      ? `<div class="area-caps">${caps.map(c => `<div class="area-cap" title="${this._esc(c)}">${NovaPanel.AREA_CAP_ICON[c] || "•"}</div>`).join("")}</div>`
      : "";
    const spark = this._sparklines?.[a.id] || {};
    const tempSpark = spark.temp ? this._areaSparklineSvg(spark.temp, "var(--gold)") : "";
    const humSpark = spark.humidity ? this._areaSparklineSvg(spark.humidity, "#6ea8ff") : "";
    const climateRow = (a.temp || a.humidity) ? `
      <div class="area-climate">
        ${a.temp ? `<div class="area-climate-item"><div class="area-climate-num">${this._esc(a.temp)}</div>${tempSpark}</div>` : ""}
        ${a.humidity ? `<div class="area-climate-item"><div class="area-climate-num">${this._esc(a.humidity)}</div>${humSpark}</div>` : ""}
      </div>` : "";
    const hasLights = (a.lights_total || 0) > 0;
    const lit = hasLights && (a.lights_on || 0) > 0;
    const ctlOn = (this._liveData?.config?.light_control_enabled) !== false;
    const lightCtl = hasLights
      ? `<button class="area-light-toggle${lit ? " on" : ""}"${ctlOn ? ` data-light-area="${this._esc(a.id || "")}" data-area-name="${this._esc(a.name)}"` : " disabled"} title="${a.lights_on}/${a.lights_total} lights on${ctlOn ? " — tap to toggle" : ""}">${lit ? "ON" : "OFF"}</button>`
      : "";
    return `
      <div class="area-tile${a.active ? " active" : ""}${(a.temp || a.humidity) ? "" : " no-temp"}">
        <div class="area-top">
          <div class="area-name">${this._esc(a.name)}${a.active ? '<span class="live-dot" title="Occupied now"></span>' : ""}</div>
        </div>
        ${capsRow}
        ${climateRow}
        <div class="area-bottom">
          <div class="area-stat">lights <b>${a.lights_on ?? 0}/${a.lights_total ?? 0}</b></div>
          ${lightCtl}
        </div>
      </div>`;
  }

  // Solar (ported from Classic's own Solar card — data was already fetched
  // into this._solar by _fetchLiveData but never rendered anywhere; the new
  // look never actually showed it despite pulling the data every poll).
  _renderSolarPanel() {
    const root = this.shadowRoot;
    const body = root.getElementById("solarBody");
    const sufficiencyEl = root.getElementById("solarSufficiency");
    if (!body) return;
    const s = this._solar || {};
    if (!s || s.error) {
      body.innerHTML = `<div class="stub-body">Couldn't load solar data — restart Home Assistant after updating.</div>`;
      if (sufficiencyEl) sufficiencyEl.textContent = "—";
      return;
    }
    if (!s.configured) {
      body.innerHTML = `<div class="stub-body">${this._esc((s.advice || [])[0] || "No solar source configured yet.")}</div>`;
      if (sufficiencyEl) sufficiencyEl.textContent = "—";
      return;
    }
    if (sufficiencyEl) {
      sufficiencyEl.textContent = s.self_sufficiency_pct != null
        ? `${s.self_sufficiency_pct}% self-sufficient` : "—";
    }
    const rows = [];
    if (s.solar_w != null) {
      rows.push(`<div class="feed-row"><span class="feed-text">Solar</span><span class="feed-time">${(s.solar_w / 1000).toFixed(2)} kW</span></div>`);
    }
    if (s.grid_w != null) {
      const dirLabel = s.grid_direction === "export" ? "Exporting" : s.grid_direction === "import" ? "Importing" : "Balanced";
      rows.push(`<div class="feed-row"><span class="feed-text">Grid</span><span class="feed-time">${dirLabel} ${(Math.abs(s.grid_w) / 1000).toFixed(2)} kW</span></div>`);
    }
    if (s.battery_w != null || s.battery_pct != null) {
      const pct = s.battery_pct != null ? `${s.battery_pct}%` : "no % available";
      rows.push(`<div class="feed-row"><span class="feed-text">Battery</span><span class="feed-time">${pct}${s.battery_w != null ? ` · ${(s.battery_w / 1000).toFixed(2)} kW` : ""}</span></div>`);
    }
    const advice = (s.advice || []).map(a => `<div class="toggle-desc" style="margin-bottom:6px">${this._esc(a)}</div>`).join("");
    body.innerHTML = advice + rows.join("");
  }

  // Shared by the dashboard's Quick Actions card and the Suggestions tab's
  // empty state — same "Analyze Now" behavior Classic exposes, wired to
  // whichever button/result-div ids the caller passes.
  _wireAnalyzeButton(btnId, resultId) {
    const root = this.shadowRoot;
    const btn = root.getElementById(btnId);
    if (!btn || btn._wired) return;
    btn._wired = true;
    btn.addEventListener("click", async () => {
      if (!this._hass) return;
      const out = root.getElementById(resultId);
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = "Analyzing…";
      if (out) out.textContent = "Running pattern analysis over your history…";
      try {
        const res = await this._hass.callWS({ type: "nova/run_analysis" });
        const bf = res.backfill || {};
        const bfNote = bf.imported ? `<br>Imported ${bf.imported} past event${bf.imported === 1 ? "" : "s"} from history for ${bf.entities} new entit${bf.entities === 1 ? "y" : "ies"}.` : "";
        if (out) {
          if (res.ran) {
            const nf = res.patterns_found ?? 0;
            const ns = res.new_suggestions ?? 0;
            const covered = res.already_automated ?? 0;
            let msg = `✓ Found ${nf} pattern${nf === 1 ? "" : "s"}, ${ns} new suggestion${ns === 1 ? "" : "s"}.`;
            if (covered > 0) {
              msg += ` ${covered} already handled by Home Assistant.`;
            }
            if (ns > 0) {
              msg += ` Check Suggestions.`;
            } else {
              msg += ` Nothing cleared the confidence bar this pass.`;
              const dg = res.diagnostic || {};
              const cand = (dg.candidates || [])[0];
              if (cand) {
                const hr = String(cand.hour).padStart(2, "0");
                const remaining = Math.max(0, (dg.min_days || 0) - (cand.days || 0));
                const progress = remaining > 0
                  ? `${remaining} more qualifying day${remaining === 1 ? "" : "s"} needed`
                  : "day coverage met; confidence or evidence is still below the threshold";
                msg += `<br>Closest routine: <b>${this._esc(cand.entity_id)}</b> → ${this._esc(cand.state)} ~${hr}:00, seen ${cand.days}/${dg.total_days} days (${progress}).`;
              }
              const src = (dg.top_sources || [])[0];
              if (src) msg += `<br>Busiest source: ${this._esc(src.entity_id)} (${src.changes} changes).`;
            }
            const nm = Array.isArray(res.near_misses) ? res.near_misses : [];
            if (nm.length) {
              msg += `<br>Building toward suggestions:`;
              msg += nm.slice(0, 5).map(m => {
                const prog = m.needed ? ` (${m.occurrences}/${m.needed})` : ` (${m.occurrences}×)`;
                return `<br>• ${this._esc(m.description || m.type)}${prog}`;
              }).join("");
            }
            out.innerHTML = msg + bfNote;
          } else {
            out.innerHTML = `✕ ${this._esc(res.reason || res.error || "Analysis did not run.")}` + bfNote;
          }
        }
        try { await this._fetchLiveData(); } catch (_) {}
      } catch (err) {
        if (out) out.innerHTML = `✕ ${this._esc(err?.message || String(err))}`;
      } finally {
        btn.disabled = false;
        btn.textContent = orig;
      }
    });
  }

  async _toggleAreaLights(areaId, roomName, isOn) {
    if (!this._hass || !areaId) return;
    const turnOn = !isOn;
    try {
      await this._hass.callService("light", turnOn ? "turn_on" : "turn_off", {}, { area_id: areaId });
      setTimeout(() => { try { this._fetchLiveData(); } catch (_) {} }, 500);
    } catch (err) {
      console.error(`Nova: ${roomName} lights toggle failed`, err);
    }
  }

  _wire() {
    const root = this.shadowRoot;
    const camToggle = root.getElementById("camToggle");
    if (camToggle) {
      camToggle.addEventListener("click", () => {
        this._camOpen = !this._camOpen;
        this._renderData();
        if (this._camOpen) {
          const refresh = () => (this._data()?.cameras || []).forEach(c =>
            this._refreshCameraSnapshot(c.entity_id));
          refresh();
          if (!this._cameraInterval) this._cameraInterval = setInterval(refresh, 15000);
        } else if (this._cameraInterval) {
          clearInterval(this._cameraInterval);
          this._cameraInterval = null;
        }
      });
    }
    const lockdownBtn = root.getElementById("lockdownControl");
    if (lockdownBtn) lockdownBtn.addEventListener("click", async () => {
      const active = !!this._data()?.lockdown?.active;
      if (!window.confirm(`${active ? "Lift" : "Engage"} Nova lockdown?`)) return;
      lockdownBtn.disabled = true;
      try {
        const res = await this._hass.callWS({ type: "nova/set_lockdown", on: !active });
        if (this._liveData && res?.lockdown) {
          this._liveData.lockdown = res.lockdown;
          if (this._liveData.config) this._liveData.config.lockdown = res.lockdown;
        }
      } catch (err) { console.error("Nova: lockdown change failed", err); }
      lockdownBtn.disabled = false;
      this._renderData();
    });
    // Top nav: Command Center / Settings
    root.querySelectorAll(".nav-tab").forEach(btn => {
      btn.addEventListener("click", () => {
        const tab = btn.getAttribute("data-tab");
        if (tab === this._currentTab) return;
        if (this._cameraInterval) { clearInterval(this._cameraInterval); this._cameraInterval = null; }
        this._camOpen = false;
        this._currentTab = tab;
        this._render();
      });
    });

    if (this._currentTab === "settings") this._wireSettings();
    if (this._currentTab === "logs") {
      this._wireLogs();
      const logView = this._logView || "system";
      if (logView === "decisions") this._fetchDecisions();
      else if (logView === "spoken_history") this._fetchSpokenHistory();
      else if (logView === "actions") this._fetchActions();
      else {
        // The shell always starts each render on "Loading…" (torn down and
        // rebuilt fresh on every tab/view switch), but the entries fetched
        // last time are still sitting in _debugLogEntries — render them
        // immediately so re-entering System Log shows the cached rows
        // instantly instead of a blocking spinner, then refresh in the
        // background exactly as a first visit would.
        if (this._debugLogEntries) this._renderDebugLogEntries(this._debugLogEntries);
        this._fetchDebugLog();
      }
    }
    if (this._currentTab === "memory") { this._wireMemory(); this._fetchKnowledge(); this._fetchPersonRoutines(); }
    if (this._currentTab === "intrusion") this._wireIntrusion();
    if (this._currentTab === "residence") {
      this._build3DHouseNew();
      this._wireResidenceControlsNew();
      this._fetchMmwaveNew();
    }
    if (this._currentTab === "suggestions") {
      this._wireSuggestions();
      this._wireAnalyzeButton("sugRunAnalysis", "sugAnalysisResult");
      if (this._automationInventory === undefined) this._fetchAutomationInventory();
      if (this._automationTrials === undefined) this._fetchAutomationTrials();
    }
    if (this._currentTab === "dashboard") {
      this._wireAnalyzeButton("qaRunAnalysis", "qaAnalysisResult");
      const createGoal = root.getElementById("goalCreate");
      const goalOutcome = root.getElementById("goalOutcome");
      if (createGoal && goalOutcome) createGoal.addEventListener("click", async () => {
        const outcome = goalOutcome.value.trim();
        if (!outcome) { goalOutcome.focus(); return; }
        createGoal.disabled = true;
        await this._goalAction({ action: "create", outcome });
        goalOutcome.value = "";
        createGoal.disabled = false;
      });
      root.querySelectorAll(".panel [data-svc]").forEach(btn => {
        if (btn._wired) return;
        btn._wired = true;
        btn.addEventListener("click", async () => {
          const svcAttr = btn.getAttribute("data-svc");
          if (!svcAttr || !this._hass) return;
          const [domain, service] = svcAttr.split(".");
          let data = {};
          const dataAttr = btn.getAttribute("data-svc-data");
          if (dataAttr) {
            try { data = JSON.parse(dataAttr); } catch (_) { data = {}; }
          }
          try {
            await this._hass.callService(domain, service, data);
          } catch (err) {
            console.error(`Nova: service ${svcAttr} failed`, err);
          }
        });
      });
    }
  }

  _wireOnboarding() {
    const root = this.shadowRoot;
    root.getElementById("onboardingDismiss")?.addEventListener("click", async () => {
      if (this._liveData?.onboarding) this._liveData.onboarding.show = false;
      if (this._liveData?.config?.onboarding) this._liveData.config.onboarding.show = false;
      this._renderData();
      try { await this._hass.callWS({ type: "nova/update_config", key: "onboarding_dismissed", value: true }); } catch (_) {}
    });
    root.querySelector(".onboarding-settings")?.addEventListener("click", () => {
      this._currentTab = "settings";
      this._render();
    });
    root.querySelectorAll(".onboarding-jump").forEach(btn => btn.addEventListener("click", () =>
      this._openSettingsCard(btn.getAttribute("data-settings-title"))));
  }

  _openSettingsCard(title) {
    this._currentTab = "settings";
    this._render();
    const cards = Array.from(this.shadowRoot.querySelectorAll(".settings-card"));
    const needle = String(title || "").toLowerCase();
    const card = cards.find(c => (c.getAttribute("data-search") || "").includes(needle));
    if (!card) return;
    this._settingsSection = card.getAttribute("data-settings-group") || "general";
    this._applySettingsFilter();
    this.shadowRoot.querySelectorAll(".settings-nav-btn").forEach(b =>
      b.classList.toggle("active", b.getAttribute("data-settings-section") === this._settingsSection));
    if (typeof card.scrollIntoView === "function") card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  _wireSettings() {
    const root = this.shadowRoot;

    root.querySelectorAll(".settings-nav-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        this._settingsSection = btn.getAttribute("data-settings-section");
        this._settingsSearch = "";
        this._applySettingsFilter();
        root.querySelectorAll(".settings-nav-btn").forEach(b =>
          b.classList.toggle("active", b === btn));
        const search = root.getElementById("settingsSearch");
        if (search) search.value = "";
      });
    });

    const search = root.getElementById("settingsSearch");
    if (search) {
      search.addEventListener("input", (e) => {
        this._settingsSearch = e.target.value;
        this._applySettingsFilter();
      });
    }

    // Generic settings autosave — same shape as Classic's own .cfg-field
    // handler: any toggle/select tagged data-cfg-key writes straight
    // through nova/update_config, then a live re-fetch refreshes state.
    root.querySelectorAll(".toggle-btn[data-cfg-key], .mode-chip[data-cfg-key]").forEach(btn => {
      btn.addEventListener("click", async () => {
        await this._saveSetting(btn.getAttribute("data-cfg-key"), btn.getAttribute("data-cfg-val") === "true");
      });
    });
    root.querySelectorAll("select.cfg-field[data-cfg-key]").forEach(sel => {
      sel.addEventListener("change", async () => {
        const key = sel.getAttribute("data-cfg-key");
        await this._saveSetting(key, sel.value);
        if (key === "ui_language") {
          this._uiLangLoaded = null;
          await this._loadUiStrings(true);
        }
      });
    });
    root.querySelectorAll("input.cfg-field[data-cfg-key]").forEach(inp => {
      inp.addEventListener("change", async () => {
        const key = inp.getAttribute("data-cfg-key");
        let value = inp.value;
        if (inp.type === "number") value = (value === "" ? null : Number(value));
        await this._saveSetting(key, value);
      });
    });

    root.querySelectorAll(".person-honorific-select").forEach(sel => {
      sel.addEventListener("change", async () => {
        const customInput = sel.closest(".person-honorific-row")?.querySelector(".person-honorific-custom");
        if (sel.value === "__custom__") {
          if (customInput) { customInput.hidden = false; customInput.focus(); }
          return;  // wait for an actual value before saving anything
        }
        if (customInput) { customInput.hidden = true; customInput.value = ""; }
        const personId = sel.getAttribute("data-person-id");
        const cfg = this._data()?.config || {};
        const overrides = { ...(cfg.person_honorifics || {}) };
        if (sel.value) overrides[personId] = sel.value; else delete overrides[personId];
        await this._saveSetting("person_honorifics", JSON.stringify(overrides));
      });
    });
    root.querySelectorAll(".person-honorific-custom").forEach(inp => {
      inp.addEventListener("change", async () => {
        const personId = inp.getAttribute("data-person-id");
        const cfg = this._data()?.config || {};
        const overrides = { ...(cfg.person_honorifics || {}) };
        const val = inp.value.trim();
        if (val) overrides[personId] = val; else delete overrides[personId];
        await this._saveSetting("person_honorifics", JSON.stringify(overrides));
      });
    });
    this._wireFloorPlanEditor();

    root.querySelectorAll(".new-room-speaker-select").forEach(sel => {
      sel.addEventListener("change", async () => {
        const cfg = this._data()?.config || {};
        const assigned = { ...(cfg.room_speakers || {}) };
        const areaId = sel.getAttribute("data-area-id");
        if (sel.value) assigned[areaId] = sel.value; else delete assigned[areaId];
        await this._saveSetting("room_speakers", JSON.stringify(assigned));
      });
    });
    root.querySelectorAll(".host-health-map-select").forEach(sel => {
      sel.addEventListener("change", async () => {
        const cfg = this._data()?.config || {};
        const mappings = { ...(cfg.host_health_mappings || {}) };
        const metricKey = sel.getAttribute("data-metric-key");
        if (sel.value) mappings[metricKey] = sel.value; else delete mappings[metricKey];
        await this._saveSetting("host_health_mappings", JSON.stringify(mappings));
      });
    });
    root.querySelectorAll(".new-sat-pair-select").forEach(sel => {
      sel.addEventListener("change", async () => {
        const cfg = this._data()?.config || {};
        const pairings = { ...(cfg.satellite_pairings || {}) };
        const satId = sel.getAttribute("data-sat-id");
        if (sel.value) pairings[satId] = sel.value; else delete pairings[satId];
        await this._saveSetting("satellite_pairings", JSON.stringify(pairings));
      });
    });
    root.querySelectorAll(".new-rule-toggle").forEach(btn => {
      btn.addEventListener("click", async () => {
        const ruleId = btn.getAttribute("data-rule-id");
        if (!ruleId) return;
        const current = this._data()?.config?.disabled_sentinel_rules || [];
        const isDisabled = current.includes(ruleId);
        const updated = isDisabled ? current.filter(id => id !== ruleId) : [...current, ruleId];
        await this._saveSetting("disabled_sentinel_rules", JSON.stringify(updated));
      });
    });
    root.querySelectorAll(".new-ann-speaker-toggle").forEach(btn => {
      btn.addEventListener("click", async () => {
        const spkId = btn.getAttribute("data-speaker-id");
        if (!spkId) return;
        const current = this._data()?.config?.announcement_speakers || [];
        const isOn = current.includes(spkId);
        const updated = isOn ? current.filter(id => id !== spkId) : [...current, spkId];
        await this._saveSetting("announcement_speakers", JSON.stringify(updated));
      });
    });
    root.querySelectorAll(".new-notify-service-toggle").forEach(btn => {
      btn.addEventListener("click", async () => {
        const service = btn.getAttribute("data-notify-service");
        if (!service) return;
        const cfg = this._data()?.config || {};
        const current = Array.isArray(this._notifyServicesDraft)
          ? this._notifyServicesDraft
          : (Array.isArray(cfg.notify_services)
            ? cfg.notify_services
            : (cfg.notify_service ? [cfg.notify_service] : []));
        const isOn = current.includes(service);
        const updated = isOn
          ? current.filter(item => item !== service)
          : [...current, service];
        this._notifyServicesDraft = updated;
        this._notifySavePending = (this._notifySavePending || 0) + 1;
        const previous = this._notifySaveQueue || Promise.resolve();
        this._notifySaveQueue = previous.then(() =>
          this._saveSetting("notify_services", JSON.stringify(updated)));
        try {
          await this._notifySaveQueue;
        } finally {
          this._notifySavePending -= 1;
        }
      });
    });
    const generalSpeakerSel = root.querySelector(".new-general-speaker-select");
    if (generalSpeakerSel) {
      generalSpeakerSel.addEventListener("change", async () => {
        await this._saveSetting("general_speaker", generalSpeakerSel.value);
      });
    }

    // Operational Mode: mode chips call nova/mode directly (not update_config —
    // same websocket contract Classic's own mode-grid already uses).
    root.querySelectorAll(".mode-grid .mode-chip[data-mode]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const mode = btn.getAttribute("data-mode");
        if (!this._hass || !mode || btn.classList.contains("mode-chip-on")) return;
        try {
          await this._hass.callWS({ type: "nova/mode", action: "set", mode });
        } catch (err) {
          console.error("Nova: failed to set mode", err);
        }
        await this._fetchLiveData();
        if (this._currentTab === "settings") this._render();
      });
    });
    root.querySelectorAll(".mode-grid [data-lab-area]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-lab-area");
        let cur = this._data()?.config?.lab_areas;
        cur = Array.isArray(cur) ? cur.slice() : [];
        const i = cur.indexOf(id);
        if (i >= 0) cur.splice(i, 1); else cur.push(id);
        await this._saveSetting("lab_areas", cur);
      });
    });

    this._wireAiModels();
    this._wireAppliances();
    this._wireCameraSettings();

    const dbtScan = root.getElementById("newDbtScan");
    if (dbtScan) {
      dbtScan.addEventListener("click", async () => {
        if (!this._hass) return;
        const limInput = root.getElementById("newDbtLimit");
        let limit = limInput ? parseInt(limInput.value, 10) : 40;
        if (isNaN(limit) || limit < 1) limit = 40;
        try {
          await this._hass.callService("nova", "train_doorbell_backlog", { limit });
          setTimeout(() => this._fetchLiveData(), 4000);
        } catch (err) {
          console.error("Nova: doorbell backlog scan failed", err);
        }
      });
    }

    const plAddBtn = root.getElementById("newPlAddEntity");
    if (plAddBtn) {
      plAddBtn.addEventListener("click", async () => {
        const inp = root.getElementById("newPlEntityInput");
        const eid = inp && inp.value.trim();
        if (!eid) return;
        if (this._hass && this._hass.states && this._hass.states[eid]) {
          const arr = this._plList();
          if (!arr.includes(eid)) {
            arr.push(eid);
            // Mirror Classic's own _plSave: write the array onto _liveData
            // directly, not just to the backend — otherwise the re-render
            // right after this still shows the pre-save list, since the
            // live-data refetch has no way to know the write landed.
            if (this._liveData && this._liveData.config) this._liveData.config.pattern_include_entities = arr;
            await this._saveSetting("pattern_include_entities", JSON.stringify(arr));
          }
        } else {
          console.warn(`Nova: "${eid}" is not a known entity id`);
        }
      });
    }
    root.querySelectorAll(".new-pl-del").forEach(b => {
      b.addEventListener("click", async () => {
        const arr = this._plList();
        arr.splice(parseInt(b.getAttribute("data-i"), 10), 1);
        if (this._liveData && this._liveData.config) this._liveData.config.pattern_include_entities = arr;
        await this._saveSetting("pattern_include_entities", JSON.stringify(arr));
      });
    });

    const exclAdd = (addId, inpId, key, validate) => {
      const btn = root.getElementById(addId);
      if (!btn) return;
      btn.addEventListener("click", async () => {
        const inp = root.getElementById(inpId);
        const val = inp && inp.value.trim();
        if (!val) return;
        if (validate && !validate(val)) return;
        const arr = this._exclArr((this._data()?.config || {})[key]);
        if (!arr.includes(val)) { arr.push(val); await this._exclSave(key, arr); }
      });
    };
    exclAdd("newExclEntAdd", "newExclEntInput", "excluded_entities", (v) => {
      if (this._hass && this._hass.states && this._hass.states[v]) return true;
      console.warn(`Nova: "${v}" is not a known entity id`);
      return false;
    });
    exclAdd("newExclDomAdd", "newExclDomInput", "excluded_domains", null);
    exclAdd("newExclLabAdd", "newExclLabInput", "excluded_labels", null);
    const exclDel = (cls, key) => root.querySelectorAll("." + cls).forEach(b => {
      b.addEventListener("click", async () => {
        const arr = this._exclArr((this._data()?.config || {})[key]);
        arr.splice(parseInt(b.getAttribute("data-i"), 10), 1);
        await this._exclSave(key, arr);
      });
    });
    exclDel("new-excl-ent-del", "excluded_entities");
    exclDel("new-excl-dom-del", "excluded_domains");
    exclDel("new-excl-lab-del", "excluded_labels");

    const rateLimitInput = root.getElementById("newObserverRateLimit");
    if (rateLimitInput) {
      rateLimitInput.addEventListener("change", async () => {
        let v = parseInt(rateLimitInput.value, 10);
        if (isNaN(v) || v < 0) v = 0;
        rateLimitInput.value = v;
        await this._rawSaveConfig("classifier_rate_limit", v);
        await this._fetchLiveData();
        if (this._currentTab === "settings") this._render();
      });
    }

    const vcTest = root.getElementById("newVcTest");
    if (vcTest) {
      vcTest.addEventListener("click", async () => {
        if (!this._hass) return;
        const out = root.getElementById("newVcTestResult");
        vcTest.disabled = true;
        const orig = vcTest.textContent;
        vcTest.textContent = "▶ PLAYING…";
        if (out) out.textContent = "Firing announce to your satellite — listen for it…";
        try {
          const res = await this._hass.callWS({ type: "nova/voice_confirm_test" });
          if (out) out.innerHTML = res.ok
            ? `<span class="diag-ok">✓</span> ${this._esc(res.note || "Announce fired.")} (${this._esc(res.satellite || "")})`
            : `<span class="diag-down">✕</span> ${this._esc(res.note || res.error || "Test failed.")}`;
        } catch (err) {
          if (out) out.innerHTML = `<span class="diag-down">✕</span> ${this._esc(err?.message || String(err))}`;
        } finally {
          vcTest.disabled = false;
          vcTest.textContent = orig;
        }
      });
    }

    const briefNow = root.getElementById("newBriefNow");
    if (briefNow) {
      briefNow.addEventListener("click", async () => {
        if (!this._hass) return;
        const cfg = this._data()?.config || {};
        const spk = cfg.announcement_speakers;
        const hasTargets = (Array.isArray(spk) && spk.length > 0) || !!cfg.broadcast_group;
        if (!hasTargets) {
          console.warn("Nova: no announcement speakers set — choose them in Settings → Announcement Speakers");
          return;
        }
        briefNow.disabled = true;
        const orig = briefNow.textContent;
        briefNow.textContent = "▶ BRIEFING…";
        try {
          await this._hass.callService("nova", "briefing", { announce: true });
        } catch (err) {
          console.error("Nova: briefing failed", err);
        } finally {
          briefNow.disabled = false;
          briefNow.textContent = orig;
        }
      });
    }

    // Diagnostics: fetch once per element lifetime (see _fetchDiagnosticsData
    // for why this isn't on the live-data poll), then RUN CHECK re-fetches
    // on demand and service-test buttons call the same HA services Classic's
    // own Diagnostics card does.
    if (!this._diagFetchedOnce) {
      this._diagFetchedOnce = true;
      this._fetchDiagnosticsData();
    }
    if (!this._hazFetchedOnce) {
      this._hazFetchedOnce = true;
      this._fetchHazardStatus();
    }
    if (!this._energyFetchedOnce) {
      this._energyFetchedOnce = true;
      this._fetchEnergyStatus();
    }
    if (!this._bioFetchedOnce) {
      this._bioFetchedOnce = true;
      this._fetchBio();
    }
    if (!this._docLibFetchedOnce) {
      this._docLibFetchedOnce = true;
      this._fetchDocLibrary();
      this._fetchVectorBackend();
    }
    this._wireDocLibrary();
    const bioToggle = root.getElementById("newBioToggle");
    if (bioToggle) {
      bioToggle.addEventListener("click", async () => {
        if (!this._hass) return;
        const enabling = !(this._bio && this._bio.enabled);
        bioToggle.disabled = true;
        try {
          await this._hass.callWS({ type: "nova/biometrics", action: enabling ? "enable" : "disable" });
        } catch (err) {
          console.error("Nova: wellbeing toggle failed", err);
        }
        await this._fetchBio();
      });
    }
    root.querySelectorAll("#newEnergyAgency .mode-chip[data-agency]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!this._hass) return;
        const agency = btn.getAttribute("data-agency");
        try {
          await this._hass.callWS({ type: "nova/energy", action: "set_agency", agency });
        } catch (err) {
          console.error("Nova: failed to set energy agency", err);
        }
        await this._fetchEnergyStatus();
      });
    });
    const hazScan = root.getElementById("newHazScan");
    if (hazScan) {
      hazScan.addEventListener("click", async () => {
        if (!this._hass) return;
        const body = root.getElementById("newHazBody");
        hazScan.disabled = true;
        const orig = hazScan.textContent;
        hazScan.textContent = "⟳ SCANNING…";
        if (body) body.innerHTML = `<div class="stub-body">Checking USGS, NWS, and NASA EONET…</div>`;
        try {
          const res = await this._hass.callWS({ type: "nova/hazard", action: "scan" });
          if (body) body.innerHTML = this._renderHazardScan(res);
        } catch (err) {
          if (body) body.innerHTML = `<div class="stub-body">Scan failed: ${this._esc(err?.message || String(err))}</div>`;
        } finally {
          hazScan.disabled = false;
          hazScan.textContent = orig;
        }
      });
    }
    const diagRefresh = root.getElementById("newDiagRefresh");
    if (diagRefresh) {
      diagRefresh.addEventListener("click", () => this._fetchDiagnosticsData());
    }
    root.querySelectorAll(".settings-card [data-svc]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const svcAttr = btn.getAttribute("data-svc");
        if (!svcAttr || !this._hass) return;
        const [domain, service] = svcAttr.split(".");
        try {
          await this._hass.callService(domain, service, {});
        } catch (err) {
          console.error(`Nova: service ${svcAttr} failed`, err);
        }
      });
    });
    const camRun = root.getElementById("newDiagCameraRun");
    if (camRun) {
      camRun.addEventListener("click", async () => {
        const sel = root.getElementById("newDiagCameraSelect");
        const entity_id = sel ? sel.value : "";
        if (!entity_id || !this._hass) return;
        try {
          await this._hass.callService("nova", "analyze_camera", { entity_id, announce: true });
        } catch (err) {
          console.error("Nova: camera analyze failed", err);
        }
      });
    }

    this._applySettingsFilter();
  }

  async _saveSetting(key, value) {
    if (!this._hass || !key) return;
    try {
      await this._hass.callWS({ type: "nova/update_config", key, value });
    } catch (err) {
      console.error(`Nova: failed to save ${key}`, err);
    }
    await this._fetchLiveData();
    // Re-render on any tab except the dashboard — a full _render() there
    // would tear down and restart the stellar-core canvas animation for no
    // reason, since the dashboard never calls this helper anyway. Broadened
    // from "settings" only so Intrusion's own cfg-field/toggle-btn fields
    // (which reuse this same generic autosave) actually refresh.
    if (this._currentTab !== "dashboard") this._render();
  }

  _applySettingsFilter() {
    const root = this.shadowRoot;
    const q = (this._settingsSearch || "").trim().toLowerCase();
    root.querySelectorAll(".settings-card").forEach(card => {
      const matchesGroup = !q && card.getAttribute("data-settings-group") === this._settingsSection;
      const matchesSearch = q && (card.getAttribute("data-search") || "").includes(q);
      card.hidden = !(matchesGroup || matchesSearch);
    });
  }

  // ─── Stellar core animation (from the approved mockup) ──────────────────

  _initCore() {
    const canvas = this.shadowRoot.getElementById("core");
    if (!canvas) return;
    this._canvas = canvas;
    // Defensive, not just a test accommodation: canvas 2D context creation
    // can fail (exotic embedded webviews, jsdom in tests) and matchMedia
    // isn't universally present — both degrade to a static hero instead of
    // throwing and blanking the whole dashboard.
    try { this._ctx = canvas.getContext("2d"); } catch (_) { this._ctx = null; }
    if (!this._ctx) return;
    this._reduceMotion = (typeof window.matchMedia === "function")
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this._makeParticles(this._current.count);
    this._resizeCore();
    if (!this._resizeListener) {
      this._resizeListener = () => this._resizeCore();
      window.addEventListener("resize", this._resizeListener);
    }
    this._t0 = performance.now();
    this._flareT = 0;
    const loop = (now) => {
      this._coreFrame(now);
      if (!this._reduceMotion) this._animHandle = requestAnimationFrame(loop);
    };
    this._animHandle = requestAnimationFrame(loop);
  }

  _resizeCore() {
    if (!this._canvas) return;
    const rect = this._canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this._canvas.width = rect.width * dpr;
    this._canvas.height = rect.height * dpr;
    this._ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._coreW = rect.width; this._coreH = rect.height;
  }

  _makeParticles(n) {
    const particles = [];
    for (let i = 0; i < n; i++) {
      particles.push({
        a: Math.random() * Math.PI * 2, r: 0.30 + Math.random() * 0.62,
        speedMul: 0.6 + Math.random() * 0.8, size: 0.9 + Math.random() * 1.8,
        phase: Math.random() * Math.PI * 2,
      });
    }
    this._particles = particles;
  }

  _targetCoreState(state) {
    const STATES = {
      idle: { speed: 0.20, count: 70, radiusMul: 1.00, glow: 0.55, hot: 0.35, flare: 0.05 },
      reasoning: { speed: 0.62, count: 110, radiusMul: 1.12, glow: 0.95, hot: 0.85, flare: 0.55 },
      asleep: { speed: 0.07, count: 40, radiusMul: 0.78, glow: 0.30, hot: 0.10, flare: 0.0 },
    };
    this._targetState = STATES[state] || STATES.idle;
    if (this._particles.length !== this._targetState.count) this._makeParticles(this._targetState.count);
  }

  _lerp(a, b, t) { return a + (b - a) * t; }

  _colorForHeat(t) {
    const stops = [[126, 36, 18], [226, 84, 47], [244, 184, 96], [255, 231, 189]];
    const seg = t * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(seg));
    const f = seg - i;
    const c0 = stops[i], c1 = stops[i + 1];
    return [Math.round(this._lerp(c0[0], c1[0], f)), Math.round(this._lerp(c0[1], c1[1], f)), Math.round(this._lerp(c0[2], c1[2], f))];
  }

  _coreFrame(now) {
    if (!this._ctx || !this._targetState) return;
    const dt = Math.min(0.05, (now - (this._t0 || now)) / 1000);
    this._t0 = now;
    const c = this._current, t = this._targetState;
    c.speed = this._lerp(c.speed, t.speed, dt * 1.4);
    c.radiusMul = this._lerp(c.radiusMul, t.radiusMul, dt * 1.4);
    c.glow = this._lerp(c.glow, t.glow, dt * 1.4);
    c.hot = this._lerp(c.hot, t.hot, dt * 1.4);
    c.flare = this._lerp(c.flare, t.flare, dt * 1.4);

    const ctx = this._ctx, W = this._coreW, H = this._coreH;
    if (!W || !H) { this._resizeCore(); return; }
    ctx.clearRect(0, 0, W, H);
    const cx = W / 2, cy = H / 2;
    const baseR = Math.min(W, H) * 0.30 * c.radiusMul;

    this._flareT += dt;
    const flareBoost = c.flare > 0.01 ? (0.5 + 0.5 * Math.sin(this._flareT * 3.1)) * c.flare : 0;

    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, baseR * 1.9);
    const hc = this._colorForHeat(Math.min(1, c.hot + flareBoost * 0.4));
    grad.addColorStop(0, `rgba(${hc[0]},${hc[1]},${hc[2]},${0.85 * c.glow + 0.15})`);
    grad.addColorStop(0.35, `rgba(${hc[0]},${hc[1]},${hc[2]},${0.35 * c.glow})`);
    grad.addColorStop(1, "rgba(20,12,8,0)");
    ctx.fillStyle = grad;
    ctx.beginPath(); ctx.arc(cx, cy, baseR * 1.9, 0, Math.PI * 2); ctx.fill();

    ctx.globalCompositeOperation = "lighter";
    for (const p of this._particles) {
      p.a += c.speed * p.speedMul * dt;
      const wobble = Math.sin(now / 1000 * 1.3 + p.phase) * 0.06;
      const rr = (p.r + wobble) * baseR * 1.55;
      const x = cx + Math.cos(p.a) * rr, y = cy + Math.sin(p.a) * rr * 0.86;
      const heat = Math.min(1, c.hot * 0.7 + p.r * 0.5 + flareBoost * 0.5);
      const pc = this._colorForHeat(heat);
      const alpha = 0.35 + 0.5 * c.glow;
      ctx.beginPath();
      ctx.fillStyle = `rgba(${pc[0]},${pc[1]},${pc[2]},${alpha})`;
      ctx.arc(x, y, p.size * (0.8 + 0.5 * c.glow), 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalCompositeOperation = "source-over";
  }

  // ─── Floor Plan Editor: rooms only (v7.101.16) ──────────────────────────
  // Ported from Classic's _renderFloorPlanEditor/_renderEditableSVG/
  // _wireFloorPlanDrag — same floor_plan_rooms config, same working-copy
  // pattern, room drag/resize/add/remove/save/reset. Property line, outdoor
  // zones, camera placement, and the AI camera-coverage feature stay
  // Classic-only for now ("Edit advanced layout in Classic" below) — ported
  // separately later if it turns out to matter.

  _defaultFloorPlan() {
    return {
      "1f": {
        label: "1st Floor", viewBox: "0 0 320 150",
        rooms: [
          { name: "Garage", x: 5, y: 5, w: 100, h: 88, type: "room" },
          { name: "Kitchen", x: 115, y: 5, w: 65, h: 40, type: "room" },
          { name: "Bath", x: 185, y: 5, w: 28, h: 22, type: "bath" },
          { name: "Guest Room", x: 218, y: 5, w: 95, h: 40, type: "room" },
          { name: "Dining Room", x: 115, y: 50, w: 65, h: 38, type: "room" },
          { name: "Stairs", x: 185, y: 32, w: 28, h: 32, type: "stairs" },
          { name: "Living Room", x: 218, y: 50, w: 95, h: 38, type: "room" },
          { name: "Downstairs Hallway", x: 115, y: 93, w: 198, h: 20, type: "room" },
          { name: "Front Door", x: 185, y: 117, w: 50, h: 12, type: "door" },
        ],
      },
      "2f": {
        label: "2nd Floor", viewBox: "0 0 320 140",
        rooms: [
          { name: "Bedroom 2", x: 50, y: 25, w: 95, h: 80, type: "room" },
          { name: "Bath", x: 150, y: 25, w: 30, h: 40, type: "bath" },
          { name: "Master Bedroom", x: 185, y: 25, w: 85, h: 80, type: "room" },
          { name: "Upstairs Hallway", x: 150, y: 70, w: 30, h: 35, type: "room" },
          { name: "Stairs", x: 150, y: 108, w: 25, h: 20, type: "stairs" },
        ],
      },
      "bsmt": {
        label: "Basement", viewBox: "0 0 320 130",
        rooms: [
          { name: "Basement", x: 50, y: 10, w: 220, h: 90, type: "room" },
          { name: "Stairs", x: 120, y: 20, w: 28, h: 35, type: "stairs" },
        ],
        labels: [
          { text: "SUMP PUMP", x: 95, y: 55 }, { text: "DEHUMIDIFIER", x: 95, y: 75 },
          { text: "HOME ENERGY", x: 235, y: 55 }, { text: "WASHER", x: 235, y: 75 },
        ],
      },
    };
  }

  _getFloorPlan() {
    try {
      const raw = this._data()?.config?.floor_plan_rooms;
      if (raw) {
        const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (parsed && typeof parsed === "object" && Object.keys(parsed).length) return parsed;
      }
    } catch (_) {}
    return this._defaultFloorPlan();
  }

  _getEditingPlan() {
    if (this._editingPlan) return this._editingPlan;
    this._editingPlan = JSON.parse(JSON.stringify(this._getFloorPlan()));
    return this._editingPlan;
  }

  _fpUnits() { return (this._data()?.config?.floor_plan_units === "metric") ? "metric" : "imperial"; }
  _fpUnitLabel() { return this._fpUnits() === "metric" ? "m" : "ft"; }
  _fpToReal(u) {
    const ft = (u || 0) * 0.2;
    return this._fpUnits() === "metric" ? Math.round(ft * 0.3048 * 10) / 10 : Math.round(ft * 10) / 10;
  }
  _fpDim(u) { return this._fpToReal(u) + (this._fpUnits() === "metric" ? "m" : "'"); }

  // Devices pinned on the floor plan (Floor Plan Editor Phase 2, v7.101.18) —
  // live-state markers, drag to move, tap to open HA's more-info.
  _getFloorEntities() {
    const raw = this._data()?.config?.floor_plan_entities;
    let e = {};
    try { e = typeof raw === "string" ? (raw ? JSON.parse(raw) : {}) : (raw || {}); } catch (_) { e = {}; }
    return e || {};
  }
  _getEditingEntities() {
    if (this._editingEntities) return this._editingEntities;
    this._editingEntities = JSON.parse(JSON.stringify(this._getFloorEntities()));
    return this._editingEntities;
  }
  _entsFor(floor) {
    const e = this._getEditingEntities();
    if (!Array.isArray(e[floor])) e[floor] = [];
    return e[floor];
  }
  _entMarkerStyle(eid) {
    const st = this._hass && this._hass.states ? this._hass.states[eid] : null;
    const dom = (eid.split(".")[0] || "");
    const dim = "var(--ink-faint)";
    if (!st) return { color: dim, name: (eid.split(".")[1] || eid), val: "—" };
    const s = st.state, dc = (st.attributes && st.attributes.device_class) || "";
    const name = (st.attributes && st.attributes.friendly_name) || eid;
    let color = dim, val = s;
    const offish = ["off", "unavailable", "unknown", "idle", "standby", "none"];
    if (dom === "sensor") {
      const u = (st.attributes && st.attributes.unit_of_measurement) || "";
      val = (s === "unknown" || s === "unavailable") ? "—" : (s + u);
      color = "var(--gold)";
    } else if (dom === "binary_sensor") {
      const on = s === "on";
      if (["door", "window", "garage_door", "opening"].indexOf(dc) >= 0) { color = on ? "var(--warn)" : dim; val = on ? "OPEN" : "SHUT"; }
      else if (["motion", "occupancy", "presence"].indexOf(dc) >= 0) { color = on ? "var(--gold)" : dim; val = on ? "DET" : "—"; }
      else { color = on ? "var(--gold)" : dim; val = on ? "ON" : "OFF"; }
    } else if (dom === "lock") { const locked = s === "locked"; color = locked ? dim : "#ff5a5a"; val = locked ? "LOCK" : "OPEN"; }
    else if (dom === "cover") { const open = s === "open" || s === "opening"; color = open ? "var(--warn)" : dim; val = open ? "OPEN" : "SHUT"; }
    else if (dom === "person" || dom === "device_tracker") { const home = s === "home"; color = home ? "var(--gold)" : dim; val = home ? "HOME" : "AWAY"; }
    else if (dom === "climate") { color = "var(--gold)"; const t = st.attributes && st.attributes.current_temperature; val = (t != null) ? (t + "°") : s; }
    else { const on = offish.indexOf(s) < 0; color = on ? "var(--gold)" : dim; val = on ? "ON" : "OFF"; }
    return { color, name, val };
  }
  _fpBgOpacity() {
    const op = parseFloat(this._data()?.config?.floor_plan_bg_opacity);
    return (isFinite(op) && op >= 0 && op <= 1) ? op : 0.2;
  }

  // Property line + outdoor zones (Phase 3a) — same geometry helpers as
  // Classic, same floor_plan_property config, same zone-as-polygon-room
  // representation in floor_plan_rooms.
  _zonePoints(r) {
    if (r && Array.isArray(r.points) && r.points.length >= 3) return r.points;
    const x = r.x || 0, y = r.y || 0, w = r.w || 40, h = r.h || 40;
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
  }
  _ensureZonePoints(rm) {
    if (!Array.isArray(rm.points) || rm.points.length < 3) rm.points = this._zonePoints(rm).map(p => [p[0], p[1]]);
    return rm.points;
  }
  _syncRoomBBox(rm) {
    if (!rm || !Array.isArray(rm.points) || rm.points.length < 3) return;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    rm.points.forEach(p => { x0 = Math.min(x0, p[0]); y0 = Math.min(y0, p[1]); x1 = Math.max(x1, p[0]); y1 = Math.max(y1, p[1]); });
    rm.x = Math.round(x0); rm.y = Math.round(y0); rm.w = Math.round(x1 - x0); rm.h = Math.round(y1 - y0);
  }
  _propPathD(pts) { return pts.map((p, k) => (k ? "L" : "M") + p[0] + " " + p[1]).join(" ") + " Z"; }
  _getProperty() {
    const raw = this._data()?.config?.floor_plan_property;
    let p = null;
    try { p = typeof raw === "string" ? (raw ? JSON.parse(raw) : null) : (raw || null); } catch (_) { p = null; }
    return (p && Array.isArray(p.points)) ? p.points : [];
  }
  _propertyPts() {
    if (!this._editingProperty) this._editingProperty = JSON.parse(JSON.stringify(this._getProperty()));
    return this._editingProperty;
  }
  _setProperty(pts) { this._editingProperty = pts; }
  _propertyArea(pts) {
    if (!pts || pts.length < 3) return "";
    let a = 0;
    for (let i = 0; i < pts.length; i++) { const p = pts[i], q = pts[(i + 1) % pts.length]; a += p[0] * q[1] - q[0] * p[1]; }
    const sqFt = Math.abs(a) / 2 * 0.04;
    if (this._fpUnits() === "metric") {
      const sqM = sqFt * 0.092903;
      return sqM >= 10000 ? (sqM / 10000).toFixed(2) + " ha" : Math.round(sqM).toLocaleString() + " m²";
    }
    return sqFt >= 43560 ? (sqFt / 43560).toFixed(2) + " acres" : Math.round(sqFt).toLocaleString() + " sq ft";
  }

  // Windows/doors/dormers ("openings", Phase 3c) — same floor_plan_elements
  // config and geometry as Classic. Feeds _planGeometry's wall gaps below,
  // so AI camera-coverage now accounts for doorways instead of treating
  // every wall as solid.
  _getFloorElements() {
    const raw = this._data()?.config?.floor_plan_elements;
    let el = {};
    try { el = typeof raw === "string" ? (raw ? JSON.parse(raw) : {}) : (raw || {}); } catch (_) { el = {}; }
    return el || {};
  }
  _getEditingElements() {
    if (this._editingElements) return this._editingElements;
    this._editingElements = JSON.parse(JSON.stringify(this._getFloorElements()));
    return this._editingElements;
  }
  _elemsFor(floor) {
    const el = this._getEditingElements();
    if (!Array.isArray(el[floor])) el[floor] = [];
    return el[floor];
  }
  _doorEntityOptions(selected) {
    const states = this._hass?.states || {};
    const cands = [];
    const OPEN_DC = ["door", "window", "garage_door", "opening"];
    const OPEN_RE = /door|garage|gate|cellar|bulkhead|hatch|window|contact|entry|slider|sash|casement|patio|french|skylight|opening|sliding/i;
    Object.keys(states).forEach(eid => {
      const dom = eid.split(".")[0];
      const at = states[eid].attributes || {};
      const dc = at.device_class || "";
      const fn = at.friendly_name || "";
      const ok = dom === "cover" || dom === "lock"
        || (dom === "binary_sensor" && (OPEN_DC.includes(dc) || OPEN_RE.test(eid) || OPEN_RE.test(fn)));
      if (ok) cands.push(eid);
    });
    cands.sort();
    if (selected && !cands.includes(selected)) cands.unshift(selected);
    const opts = cands.map(eid => `<option value="${this._esc(eid)}"${eid === selected ? " selected" : ""}>${this._esc(this._entName(eid))}</option>`).join("");
    return `<option value=""${selected ? "" : " selected"}>— auto-detect —</option>${opts}`;
  }

  // Cameras + AI coverage (Phase 3b) — same config/geometry as Classic.
  _getFloorCameras() {
    const raw = this._data()?.config?.floor_plan_cameras;
    let c = {};
    try { c = typeof raw === "string" ? (raw ? JSON.parse(raw) : {}) : (raw || {}); } catch (_) { c = {}; }
    return c || {};
  }
  _getEditingCameras() {
    if (this._editingCameras) return this._editingCameras;
    this._editingCameras = JSON.parse(JSON.stringify(this._getFloorCameras()));
    return this._editingCameras;
  }
  _camsFor(floor) {
    const c = this._getEditingCameras();
    if (!Array.isArray(c[floor])) c[floor] = [];
    return c[floor];
  }
  _cameraEntityOptions(selected) {
    const states = this._hass?.states || {};
    const eids = Object.keys(states).filter(e => e.startsWith("camera.")).sort();
    if (selected && !eids.includes(selected)) eids.unshift(selected);
    const opts = eids.map(e => {
      const st = states[e];
      const fn = (st && st.attributes && st.attributes.friendly_name) || e;
      return `<option value="${this._esc(e)}"${e === selected ? " selected" : ""}>${this._esc(fn)}</option>`;
    }).join("");
    return `<option value="">— camera —</option>${opts}`;
  }
  _segIntersect(x1, y1, x2, y2, x3, y3, x4, y4) {
    const den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4);
    if (Math.abs(den) < 1e-9) return null;
    const t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den;
    const u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / den;
    if (t < 0 || t > 1 || u < 0 || u > 1) return null;
    return { x: x1 + t * (x2 - x1), y: y1 + t * (y2 - y1) };
  }
  _planGeometry(floor) {
    const plan = this._getEditingPlan()[floor];
    const rooms = (plan && plan.rooms) || [];
    const walls = [], gaps = [];
    rooms.forEach(r => {
      if (r.type === "stairs" || r.type === "door" || r.type === "outdoor") return;
      const rp = this._zonePoints(r);
      for (let wi = 0; wi < rp.length; wi++) { const a = rp[wi], b = rp[(wi + 1) % rp.length]; walls.push({ x0: a[0], y0: a[1], x1: b[0], y1: b[1] }); }
    });
    let fb = null;
    if (rooms.length) {
      let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
      rooms.forEach(r => { mnx = Math.min(mnx, r.x); mny = Math.min(mny, r.y); mxx = Math.max(mxx, r.x + r.w); mxy = Math.max(mxy, r.y + r.h); });
      fb = { x0: mnx, y0: mny, x1: mxx, y1: mxy };
    }
    (this._elemsFor(floor) || []).forEach(e => {
      if (e.type !== "door" && e.type !== "window") return;
      const ow = e.w || 20;
      let bb = fb;
      if ((e.kind === "interior" || e.kind === "cased") && e.room) {
        const rr = rooms.filter(r => r.name === e.room)[0];
        if (rr) bb = { x0: rr.x, y0: rr.y, x1: rr.x + rr.w, y1: rr.y + rr.h };
      }
      if (!bb) return;
      const p = e.pos != null ? e.pos : 0.5;
      let gx0, gy0, gx1, gy1;
      if (e.wall === "front") { const c = bb.x0 + p * (bb.x1 - bb.x0); gx0 = c - ow / 2; gx1 = c + ow / 2; gy0 = gy1 = bb.y0; }
      else if (e.wall === "back") { const c = bb.x0 + p * (bb.x1 - bb.x0); gx0 = c - ow / 2; gx1 = c + ow / 2; gy0 = gy1 = bb.y1; }
      else if (e.wall === "left") { const c = bb.y0 + p * (bb.y1 - bb.y0); gy0 = c - ow / 2; gy1 = c + ow / 2; gx0 = gx1 = bb.x0; }
      else { const c = bb.y0 + p * (bb.y1 - bb.y0); gy0 = c - ow / 2; gy1 = c + ow / 2; gx0 = gx1 = bb.x1; }
      gaps.push({ x0: gx0, y0: gy0, x1: gx1, y1: gy1 });
    });
    return { walls, gaps };
  }
  _inGap(x, y, gaps) {
    const tol = 2.5;
    for (let i = 0; i < gaps.length; i++) {
      const g = gaps[i];
      if (x >= Math.min(g.x0, g.x1) - tol && x <= Math.max(g.x0, g.x1) + tol
        && y >= Math.min(g.y0, g.y1) - tol && y <= Math.max(g.y0, g.y1) + tol) return true;
    }
    return false;
  }
  _losClear(x1, y1, x2, y2, geo) {
    for (let i = 0; i < geo.walls.length; i++) {
      const w = geo.walls[i];
      const ip = this._segIntersect(x1, y1, x2, y2, w.x0, w.y0, w.x1, w.y1);
      if (ip && !this._inGap(ip.x, ip.y, geo.gaps)) return false;
    }
    return true;
  }
  _pointCovered(px, py, cx, cy, ang, half, rng, geo) {
    const dx = px - cx, dy = py - cy;
    if (Math.hypot(dx, dy) > rng) return false;
    const a = Math.atan2(dy, dx) * 180 / Math.PI;
    if (Math.abs(((a - ang + 540) % 360) - 180) > half) return false;
    return this._losClear(cx, cy, px, py, geo);
  }
  _computeCoverage(floor, cam, geo) {
    const plan = this._getEditingPlan()[floor];
    if (!plan || !plan.rooms || !plan.rooms.length) return {};
    geo = geo || this._planGeometry(floor);
    const cx = cam.x, cy = cam.y, ang = cam.angle != null ? cam.angle : 270,
      fov = cam.fov != null ? cam.fov : 90, rng = Math.max(cam.range != null ? cam.range : 55, 5), half = fov / 2;
    const cov = {};
    plan.rooms.forEach(r => {
      if (r.type === "door" || r.type === "stairs") return;
      let hit = 0, tot = 0;
      if (r.type === "outdoor" || (r.points && r.points.length >= 3)) {
        const pts = this._zonePoints(r);
        let bx0 = 1e9, by0 = 1e9, bx1 = -1e9, by1 = -1e9;
        pts.forEach(p => { bx0 = Math.min(bx0, p[0]); by0 = Math.min(by0, p[1]); bx1 = Math.max(bx1, p[0]); by1 = Math.max(by1, p[1]); });
        const nx = 6, ny = 6;
        for (let i = 0; i < nx; i++) for (let j = 0; j < ny; j++) {
          const px = bx0 + (i + 0.5) / nx * (bx1 - bx0), py = by0 + (j + 0.5) / ny * (by1 - by0);
          if (!this._pointInPoly(px, py, pts)) continue;
          tot++;
          if (this._pointCovered(px, py, cx, cy, ang, half, rng, geo)) hit++;
        }
      } else {
        const nx = 4, ny = 4;
        for (let i = 0; i < nx; i++) for (let j = 0; j < ny; j++) {
          const px = r.x + (i + 0.5) / nx * r.w, py = r.y + (j + 0.5) / ny * r.h;
          tot++;
          if (this._pointCovered(px, py, cx, cy, ang, half, rng, geo)) hit++;
        }
      }
      if (hit > 0 && tot > 0) cov[r.name] = Math.round(hit / tot * 100) / 100;
    });
    return cov;
  }
  _pointInPoly(x, y, pts) {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
      if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }
  _coneD(cam) {
    const cx = cam.x, cy = cam.y, ang = cam.angle != null ? cam.angle : 270,
      fov = cam.fov != null ? cam.fov : 90, rng = Math.max(cam.range != null ? cam.range : 55, 5);
    const a1 = (ang - fov / 2) * Math.PI / 180, a2 = (ang + fov / 2) * Math.PI / 180;
    const x1 = cx + rng * Math.cos(a1), y1 = cy + rng * Math.sin(a1);
    const x2 = cx + rng * Math.cos(a2), y2 = cy + rng * Math.sin(a2);
    const large = fov > 180 ? 1 : 0;
    return `M ${cx} ${cy} L ${x1.toFixed(1)} ${y1.toFixed(1)} A ${rng} ${rng} 0 ${large} 1 ${x2.toFixed(1)} ${y2.toFixed(1)} Z`;
  }
  _rayCast(cx, cy, ang, range, geo) {
    const ex = cx + range * Math.cos(ang), ey = cy + range * Math.sin(ang);
    let best = range;
    for (let i = 0; i < geo.walls.length; i++) {
      const w = geo.walls[i];
      const ip = this._segIntersect(cx, cy, ex, ey, w.x0, w.y0, w.x1, w.y1);
      if (!ip || this._inGap(ip.x, ip.y, geo.gaps)) continue;
      const d = Math.hypot(ip.x - cx, ip.y - cy);
      if (d < best) best = d;
    }
    return best;
  }
  _clippedCone(cam, geo) {
    if (!geo || !geo.walls || !geo.walls.length) return this._coneD(cam);
    const cx = cam.x, cy = cam.y, ang = cam.angle != null ? cam.angle : 270,
      fov = cam.fov != null ? cam.fov : 90, rng = Math.max(cam.range != null ? cam.range : 55, 5);
    const N = Math.max(24, Math.round(fov / 3)), a0 = (ang - fov / 2) * Math.PI / 180, step = (fov * Math.PI / 180) / N;
    let d = `M ${cx} ${cy}`;
    for (let i = 0; i <= N; i++) {
      const a = a0 + i * step, dist = this._rayCast(cx, cy, a, rng, geo);
      d += ` L ${(cx + dist * Math.cos(a)).toFixed(1)} ${(cy + dist * Math.sin(a)).toFixed(1)}`;
    }
    return d + " Z";
  }
  _roomAt(floor, x, y) {
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    for (let i = 0; i < rooms.length; i++) {
      const r = rooms[i];
      if (r.type === "stairs" || r.type === "door") continue;
      if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) return r.name;
    }
    let best = null, bd = 1e18;
    rooms.forEach(r => { const cx = r.x + r.w / 2, cy = r.y + r.h / 2, d = (cx - x) * (cx - x) + (cy - y) * (cy - y); if (d < bd) { bd = d; best = r.name; } });
    return best || "the area";
  }
  _openingDescriptions(floor) {
    const out = [];
    (this._elemsFor(floor) || []).forEach(e => {
      if (e.type === "door" && (e.kind === "cased" || e.kind === "interior") && e.room) {
        out.push((e.kind === "cased" ? "cased opening at " : "interior door at ") + e.room);
      }
    });
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    if (rooms.some(r => r.type === "stairs")) out.push("open staircase");
    return out;
  }

  _floorPlanEditorCardBody() {
    const plan = this._getEditingPlan();
    const floors = Object.keys(plan);
    if (!this._editorFloor || !plan[this._editorFloor]) this._editorFloor = floors[0] || "1f";
    const floor = this._editorFloor;
    return `
      <div class="fpn-toolbar">
        <div class="fpn-floor-tabs">
          ${floors.map(fk => `<button class="mode-chip fpn-floor-tab${fk === floor ? " active" : ""}" data-fpn-floor="${fk}">${this._esc(plan[fk].label || fk)}</button>`).join("")}
        </div>
        <div class="fpn-actions">
          <button class="mode-chip" id="fpnAddRoom">+ Add Room</button>
          <button class="mode-chip" id="fpnAddZone">+ Outdoor Zone</button>
          ${this._fpnAddPropertyButton()}
          <button class="mode-chip" id="fpnUnits">Units: ${this._fpUnits() === "metric" ? "Metric" : "Imperial"}</button>
          <button class="mode-chip" id="fpnZoomFit">⤢ Fit</button>
        </div>
      </div>
      <div class="fpn-hint">Drag to move · bottom-right handle to resize · right-click to delete · double-click an edge to add a corner · scroll to zoom · drag empty space to pan</div>
      <div class="fpn-canvas" id="fpnCanvas">${this._renderFloorPlanSVG(plan, floor)}</div>
      ${this._renderOpeningsNew(floor)}
      ${this._renderCamerasNew(floor)}
      ${this._renderPlanEntitiesNew(floor)}
      <div class="fpn-actions">
        <button class="mode-chip" id="fpnSave">Save Layout</button>
        <button class="mode-chip" id="fpnReset">Reset Default</button>
        <button class="mode-chip" id="fpnExport">⬇ Export</button>
        <button class="mode-chip" id="fpnImport">⬆ Import</button>
        <input type="file" id="fpnImportFile" accept=".json,application/json" style="display:none">
      </div>`;
  }

  _fpnAddPropertyButton() {
    const pp = this._propertyPts();
    const has = pp.length >= 3;
    return `<button class="mode-chip" id="fpnAddProperty">${has ? "Clear Property" : "+ Property Line"}</button>`
      + (has ? `<span class="toggle-desc">Lot: ${this._propertyArea(pp)}</span>` : "");
  }

  _renderPlanEntitiesNew(floor) {
    const ents = this._entsFor(floor);
    const op = this._fpBgOpacity();
    let hasBg = false;
    try {
      const b = this._data()?.config?.floor_plan_bg;
      const bd = typeof b === "string" ? JSON.parse(b || "{}") : (b || {});
      hasBg = !!bd[floor];
    } catch (_) {}
    const chips = ents.length
      ? ents.map((e, i) => `<span class="new-pl-chip">${this._esc(this._entMarkerStyle(e.e).name)}<button class="fpn-ent-del" data-ei="${i}" title="Remove">×</button></span>`).join("")
      : `<span class="toggle-desc">No devices placed on this floor yet.</span>`;
    return `
      <div class="mode-bind-head">Devices on plan <span class="toggle-desc">add a device, drag its pin on the canvas, tap it to open controls</span></div>
      <div class="cfg-row">
        <input id="fpnEntInput" list="fpnEntList" class="cfg-field" style="flex:1" placeholder="type to find an entity…" autocomplete="off">
        <datalist id="fpnEntList">${this._allEntityDatalist()}</datalist>
        <button class="mode-chip" id="fpnEntAdd">+ Add</button>
      </div>
      <div class="mode-grid">${chips}</div>
      <div class="mode-bind-head">Imported plan <span class="toggle-desc">${hasBg ? "opacity of the uploaded floor-plan image behind the rooms" : "upload a real floor-plan image to trace rooms over"}</span></div>
      <div class="cfg-row">
        <button class="mode-chip" id="fpnBgUpload">⬆ ${hasBg ? "Replace" : "Upload"} Image</button>
        <input type="file" id="fpnBgFile" accept="image/*" style="display:none">
        <label>opacity</label>
        <input id="fpnBgOp" type="range" min="0" max="1" step="0.05" value="${op}">
        <span id="fpnBgOpVal">${Math.round(op * 100)}%</span>
      </div>`;
  }

  _renderOpeningsNew(floor) {
    const els = this._elemsFor(floor), uL = this._fpUnitLabel();
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    const walls = [["front", "Front"], ["back", "Back"], ["left", "Left"], ["right", "Right"]];
    const wsel = (e, i) => `<select class="op-field-new" data-op="wall" data-i="${i}">${walls.map(w => `<option value="${w[0]}"${e.wall === w[0] ? " selected" : ""}>${w[1]}</option>`).join("")}</select>`;
    const rsel = (e, i) => `<select class="op-field-new" data-op="room" data-i="${i}"><option value="">— room —</option>${rooms.filter(r => r.type !== "outdoor").map(r => `<option value="${this._esc(r.name)}"${e.room === r.name ? " selected" : ""}>${this._esc(r.name)}</option>`).join("")}</select>`;
    const rows = els.map((e, i) => {
      const isD = e.type === "dormer";
      const t = isD ? (e.slope === "rear" ? "REAR DORMER" : "FRONT DORMER") : (e.type === "window" ? "WINDOW" : (e.kind === "interior" ? "INT DOOR" : (e.kind === "cellar" ? "CELLAR" : (e.kind === "cased" ? "CASED OPENING" : "EXT DOOR"))));
      const place = isD
        ? `<select class="op-field-new" data-op="slope" data-i="${i}"><option value="front"${e.slope !== "rear" ? " selected" : ""}>Front slope</option><option value="rear"${e.slope === "rear" ? " selected" : ""}>Rear slope</option></select>`
        : ((e.kind === "interior" || e.kind === "cased") ? (rsel(e, i) + " " + wsel(e, i)) : wsel(e, i));
      return `
        <div class="cfg-row op-row-new" data-i="${i}">
          <span class="new-pl-chip">${t}</span>
          ${place}
          <input class="op-field-new" data-op="pos" data-i="${i}" type="range" min="0" max="1" step="0.02" value="${e.pos != null ? e.pos : 0.5}" title="position along the wall">
          ${isD ? "" : `<input class="op-field-new op-num-new" data-op="w" data-i="${i}" type="number" min="1" step="0.5" value="${this._fpToReal(e.w || 20)}" style="width:56px"> ${uL}`}
          ${e.kind === "cased" ? `<span class="toggle-desc">open passage · no sensor</span>` : `<select class="op-field-new op-ent-new" data-op="entity" data-i="${i}">${this._doorEntityOptions(e.entity || "")}</select>`}
          <button class="fpn-ent-del op-del-new" data-i="${i}" title="Remove">×</button>
        </div>`;
    }).join("");
    const dBtns = floor === "2f" ? `<button class="mode-chip" id="opAddFdormer">+ Front Dormer</button><button class="mode-chip" id="opAddRdormer">+ Rear Dormer</button>` : "";
    return `
      <div class="mode-bind-head">Windows, doors &amp; dormers <span class="toggle-desc">interior doors &amp; cased openings attach to a room · a cased opening is a doorway with no door · dormers on the 2nd floor</span></div>
      <div class="cfg-row cfg-row-wrap">
        <button class="mode-chip" id="opAddWindow">+ Window</button>
        <button class="mode-chip" id="opAddExtdoor">+ Exterior Door</button>
        <button class="mode-chip" id="opAddCellar">+ Cellar Door</button>
        <button class="mode-chip" id="opAddIntdoor">+ Interior Door</button>
        <button class="mode-chip" id="opAddCased">+ Cased Opening</button>
        ${dBtns}
      </div>
      ${rows || `<div class="toggle-desc">No openings placed on this floor yet — add one above.</div>`}`;
  }

  _renderCamerasNew(floor) {
    const cams = this._camsFor(floor), uL = this._fpUnitLabel();
    const geo = cams.length ? this._planGeometry(floor) : null;
    const zoneNames = new Set((((this._getEditingPlan()[floor] || {}).rooms) || []).filter(r => r.type === "outdoor").map(r => r.name));
    const rows = cams.map((c, i) => {
      const cov = geo ? this._computeCoverage(floor, c, geo) : {};
      const order = Object.keys(cov).sort((a, b) => cov[b] - cov[a]);
      const covLine = order.length
        ? `<div class="toggle-desc">sees: ${order.map(rn => `${this._esc(rn)} ${Math.round(cov[rn] * 100)}%${zoneNames.has(rn) ? " (zone)" : ""}`).join(" · ")}</div>`
        : `<div class="toggle-desc">nothing in view — aim it, widen the FOV, or extend the range</div>`;
      const cvg = c.coverage;
      const llmLine = (cvg && cvg.reason)
        ? `<div class="toggle-desc">${(cvg.covered && cvg.covered.length) ? `✓ confirms ${cvg.covered.map(r => this._esc(r)).join(", ")} — ` : ""}${this._esc(cvg.reason)}</div>`
        : "";
      return `
        <div class="cfg-row cam-row-new" data-ci="${i}">
          <span class="new-pl-chip">CAM ${i + 1}</span>
          <select class="cam-field-new" data-cam="entity" data-ci="${i}">${this._cameraEntityOptions(c.entity || "")}</select>
          <label class="fpn-inline-lbl">aim <input class="cam-field-new" data-cam="angle" data-ci="${i}" type="range" min="0" max="359" step="1" value="${c.angle != null ? c.angle : 270}"></label>
          <label class="fpn-inline-lbl">FOV <input class="cam-field-new" data-cam="fov" data-ci="${i}" type="range" min="20" max="170" step="5" value="${c.fov != null ? c.fov : 90}"></label>
          <label class="fpn-inline-lbl">range <input class="cam-field-new cam-num-new" data-cam="range" data-ci="${i}" type="number" min="5" step="5" value="${this._fpToReal(c.range != null ? c.range : 55)}"> ${uL}</label>
          <button class="mode-chip cam-io-new" data-ci="${i}" title="indoor = bounded by walls, outdoor = by range">${c.indoor === false ? "OUTDOOR" : "INDOOR"}</button>
          <button class="fpn-ent-del cam-del-new" data-ci="${i}" title="Remove">×</button>
        </div>
        ${covLine}${llmLine}`;
    }).join("");
    return `
      <div class="mode-bind-head">Cameras · field of view <span class="toggle-desc">drop a camera, bind its entity, aim it — drag the dot on the plan to move, right-click to delete</span></div>
      <div class="cfg-row cfg-row-wrap">
        <button class="mode-chip" id="fpnCamAdd">+ Camera</button>
        ${cams.length ? `<button class="mode-chip" id="fpnCamCompute" title="AI: judge what each camera can confirm">Compute coverage</button>` : ""}
      </div>
      ${rows || `<div class="toggle-desc">No cameras placed on this floor yet — add one above.</div>`}`;
  }

  _renderFloorPlanSVG(plan, floor) {
    const floorData = plan[floor];
    if (!floorData) return "";
    const vb = this._editVB
      ? `${this._editVB.x} ${this._editVB.y} ${this._editVB.w} ${this._editVB.h}`
      : (floorData.viewBox || "0 0 320 150");
    let svg = `<svg viewBox="${vb}" class="fpn-svg" id="fpnSvg" style="width:100%;height:100%;min-height:520px;background:var(--bg);border:1px solid var(--line-soft);border-radius:10px;cursor:crosshair;">`;
    svg += '<defs>'
      + '<pattern id="fpn-grid-sm" width="10" height="10" patternUnits="userSpaceOnUse"><path d="M 10 0 L 0 0 0 10" fill="none" stroke="rgba(244,184,96,0.05)" stroke-width="0.2"/></pattern>'
      + '<pattern id="fpn-grid-lg" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M 50 0 L 0 0 0 50" fill="none" stroke="rgba(244,184,96,0.12)" stroke-width="0.3"/></pattern>'
      + '</defs>';
    const vbp = vb.split(" ").map(Number);
    const gx = vbp[0], gy = vbp[1], gw = vbp[2], gh = vbp[3];
    svg += `<rect class="fpn-grid-rect" x="${gx}" y="${gy}" width="${gw}" height="${gh}" fill="url(#fpn-grid-sm)"/>`;
    svg += `<rect class="fpn-grid-rect" x="${gx}" y="${gy}" width="${gw}" height="${gh}" fill="url(#fpn-grid-lg)"/>`;

    const bgs = this._data()?.config?.floor_plan_bg;
    if (bgs) {
      try {
        const bgData = typeof bgs === "string" ? JSON.parse(bgs) : bgs;
        if (bgData && bgData[floor]) {
          svg += `<image href="${bgData[floor]}" x="0" y="0" width="100%" height="100%" opacity="${this._fpBgOpacity()}" preserveAspectRatio="xMidYMid meet"/>`;
        }
      } catch (_) {}
    }

    // Property boundary (the lot) — draw behind rooms; vertices are draggable.
    let prop = [];
    try { prop = this._propertyPts() || []; } catch (_) { prop = []; }
    if (prop.length >= 2) {
      svg += `<path class="fpn-prop-path" d="${this._propPathD(prop)}" fill="rgba(244,184,96,0.03)" stroke="var(--gold)" stroke-width="1" stroke-dasharray="6 4" pointer-events="none"/>`;
      for (let vi = 0; vi < prop.length; vi++) {
        const a = prop[vi], b = prop[(vi + 1) % prop.length];
        svg += `<circle class="fpn-prop-mid" data-prop-edge="${vi}" cx="${(a[0] + b[0]) / 2}" cy="${(a[1] + b[1]) / 2}" r="2.2" fill="none" stroke="var(--gold)" stroke-width="0.7" opacity="0.5" style="cursor:copy"/>`;
      }
      for (let pi = 0; pi < prop.length; pi++) {
        svg += `<circle class="fpn-prop-vtx" data-prop-vtx="${pi}" cx="${prop[pi][0]}" cy="${prop[pi][1]}" r="3" fill="var(--gold)" stroke="var(--bg)" stroke-width="0.7" style="cursor:grab"/>`;
      }
    }

    for (let i = 0; i < (floorData.rooms || []).length; i++) {
      const rm = floorData.rooms[i];
      const colors = { room: "var(--gold)", bath: "var(--ink-faint)", stairs: "var(--ember)", door: "var(--warn)", outdoor: "#8fdba8" };
      const c = colors[rm.type] || "var(--gold)";
      const out = rm.type === "outdoor";
      const fs = rm.w > 80 ? 7 : (rm.w > 50 ? 5.5 : (rm.w > 25 ? 4 : 3));
      if (out || (rm.points && rm.points.length >= 3)) {
        const zpts = this._zonePoints(rm);
        let zcx = 0, zcy = 0; zpts.forEach(p => { zcx += p[0]; zcy += p[1]; }); zcx /= zpts.length; zcy /= zpts.length;
        svg += `<g class="fpn-zone" data-zone-idx="${i}">`;
        svg += `<path class="fpn-zone-path" data-zone-idx="${i}" d="${this._propPathD(zpts)}" fill="${c}" fill-opacity="0.06" stroke="${c}" stroke-width="1"${out ? ' stroke-dasharray="4 3"' : ""} style="cursor:move"/>`;
        svg += `<text x="${zcx.toFixed(1)}" y="${zcy.toFixed(1)}" text-anchor="middle" fill="${c}" font-size="${fs}" font-family="var(--font-display)" letter-spacing="0.3" pointer-events="none">${this._esc((rm.name || "").toUpperCase())}</text>`;
        for (let vi = 0; vi < zpts.length; vi++) { const a = zpts[vi], b = zpts[(vi + 1) % zpts.length]; svg += `<circle class="fpn-zone-mid" data-zone-idx="${i}" data-edge="${vi}" cx="${(a[0] + b[0]) / 2}" cy="${(a[1] + b[1]) / 2}" r="2" fill="none" stroke="${c}" stroke-width="0.6" opacity="0.5" style="cursor:copy"/>`; }
        for (let vi = 0; vi < zpts.length; vi++) { svg += `<circle class="fpn-zone-vtx" data-zone-idx="${i}" data-vtx="${vi}" cx="${zpts[vi][0]}" cy="${zpts[vi][1]}" r="2.8" fill="${c}" stroke="var(--bg)" stroke-width="0.6" style="cursor:grab"/>`; }
        svg += "</g>";
      } else {
        svg += `<g class="fpn-drag-room" data-idx="${i}" style="cursor:move">`;
        svg += `<rect x="${rm.x}" y="${rm.y}" width="${rm.w}" height="${rm.h}" rx="2" fill="${out ? "rgba(143,219,168,0.06)" : "rgba(244,184,96,0.08)"}" stroke="${c}" stroke-width="1" class="fpn-drag-rect"/>`;
        svg += `<text x="${rm.x + rm.w / 2}" y="${rm.y + rm.h / 2}" text-anchor="middle" fill="${c}" font-size="${fs}" font-family="var(--font-display)" letter-spacing="0.3" pointer-events="none">${this._esc((rm.name || "").toUpperCase())}</text>`;
        if (rm.w > 30 && rm.h > 24) svg += `<text x="${rm.x + rm.w / 2}" y="${rm.y + rm.h / 2 + fs + 2.5}" text-anchor="middle" fill="${c}" opacity="0.6" font-size="${(fs * 0.72).toFixed(1)}" font-family="var(--font-mono)" pointer-events="none">${this._fpDim(rm.w)} × ${this._fpDim(rm.h)}</text>`;
        svg += `<rect x="${rm.x + rm.w - 8}" y="${rm.y + rm.h - 8}" width="8" height="8" fill="${c}" opacity="0.35" rx="1" class="fpn-resize-handle" data-idx="${i}" style="cursor:nwse-resize"/>`;
        svg += "</g>";
      }
    }
    for (const lbl of (floorData.labels || [])) {
      svg += `<text x="${lbl.x}" y="${lbl.y}" text-anchor="middle" fill="var(--ink-faint)" font-size="4" font-family="var(--font-mono)">${this._esc(lbl.text)}</text>`;
    }

    // Placed openings as wall markers.
    const els = this._elemsFor(floor) || [];
    if (els.length && floorData.rooms && floorData.rooms.length) {
      let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
      floorData.rooms.forEach(r => { if (r.type === "outdoor") return; mnx = Math.min(mnx, r.x); mny = Math.min(mny, r.y); mxx = Math.max(mxx, r.x + r.w); mxy = Math.max(mxy, r.y + r.h); });
      els.forEach((e, i) => {
        if (e.type === "dormer") {
          const dp = e.pos != null ? e.pos : 0.5, dcx = mnx + dp * (mxx - mnx), dcy = e.slope === "rear" ? mxy : mny;
          svg += `<rect class="fpn-op-marker" data-op-marker="${i}" x="${dcx - 4}" y="${dcy - 3}" width="8" height="6" fill="#b06aff" opacity="0.9" rx="1.5" pointer-events="none"/>`;
          return;
        }
        const w = e.w || 20, p = e.pos != null ? e.pos : 0.5, horiz = (e.wall === "front" || e.wall === "back");
        let bx0 = mnx, by0 = mny, bx1 = mxx, by1 = mxy;
        if ((e.kind === "interior" || e.kind === "cased") && e.room) {
          const rr = floorData.rooms.filter(r => r.name === e.room)[0];
          if (rr) { bx0 = rr.x; by0 = rr.y; bx1 = rr.x + rr.w; by1 = rr.y + rr.h; }
        }
        let cx, cy;
        if (e.wall === "front") { cx = bx0 + p * (bx1 - bx0); cy = by0; }
        else if (e.wall === "back") { cx = bx0 + p * (bx1 - bx0); cy = by1; }
        else if (e.wall === "left") { cx = bx0; cy = by0 + p * (by1 - by0); }
        else { cx = bx1; cy = by0 + p * (by1 - by0); }
        const col = e.type === "window" ? "var(--gold)" : (e.kind === "interior" ? "#5a7a8a" : (e.kind === "cellar" ? "#c98a2a" : (e.kind === "cased" ? "#78b9d7" : "var(--warn)")));
        const ex = horiz ? cx - w / 2 : cx - 2, ey = horiz ? cy - 2 : cy - w / 2, ew = horiz ? w : 4, eh = horiz ? 4 : w;
        svg += `<rect class="fpn-op-marker" data-op-marker="${i}" x="${ex}" y="${ey}" width="${ew}" height="${eh}" fill="${col}" opacity="0.9" rx="1" pointer-events="none"/>`;
      });
    }

    // Cameras — icon + FOV cone, clipped to walls.
    const cams = this._camsFor(floor);
    const camGeo = cams.length ? this._planGeometry(floor) : null;
    for (let ci = 0; ci < cams.length; ci++) {
      const cam = cams[ci], out = cam.indoor === false;
      svg += `<g class="fpn-cam" data-cam-idx="${ci}">`
        + `<path class="fpn-cam-cone" d="${this._clippedCone(cam, camGeo)}" fill="${out ? "rgba(232,178,61,0.10)" : "rgba(226,84,47,0.10)"}" stroke="${out ? "rgba(232,178,61,0.6)" : "rgba(226,84,47,0.6)"}" stroke-width="0.7" pointer-events="none"/>`
        + `<circle class="fpn-cam-dot" cx="${cam.x}" cy="${cam.y}" r="3.2" fill="${out ? "var(--warn)" : "var(--ember)"}" stroke="var(--ink)" stroke-width="0.7" style="cursor:grab"/>`
        + `<text x="${cam.x}" y="${cam.y - 5}" text-anchor="middle" fill="${out ? "var(--warn)" : "var(--ember)"}" font-size="5" font-family="var(--font-mono)" pointer-events="none">${ci + 1}</text>`
        + "</g>";
    }

    const ents = this._entsFor(floor);
    for (let ei = 0; ei < ents.length; ei++) {
      const ent = ents[ei];
      if (!ent || !ent.e) continue;
      const ms = this._entMarkerStyle(ent.e);
      const nm = ms.name.length > 16 ? (ms.name.slice(0, 15) + "…") : ms.name;
      svg += `<g class="fpn-ent" data-ent-idx="${ei}" data-ent-id="${this._esc(ent.e)}" style="cursor:pointer">`
        + `<circle class="fpn-ent-dot" cx="${ent.x}" cy="${ent.y}" r="3" fill="${ms.color}" stroke="var(--bg)" stroke-width="0.7"/>`
        + `<text class="fpn-ent-nm" x="${ent.x}" y="${ent.y - 4}" text-anchor="middle" fill="${ms.color}" font-size="3.4" font-family="var(--font-mono)" pointer-events="none">${this._esc(nm)}</text>`
        + `<text class="fpn-ent-val" x="${ent.x}" y="${ent.y + 6.5}" text-anchor="middle" fill="var(--ink-dim)" font-size="3" font-family="var(--font-mono)" pointer-events="none">${this._esc(ms.val)}</text>`
        + "</g>";
    }

    svg += "</svg>";
    return svg;
  }

  // Re-renders just this one card (not the whole settings grid) so an
  // in-progress edit elsewhere on the page isn't disturbed and scroll
  // position is preserved — mirrors Classic's _rerenderFloorEditor().
  _rerenderFloorPlanCard() {
    const card = this.shadowRoot?.getElementById("settings-card-floor_plan_editor");
    if (!card) return;
    const c = NovaPanel.SETTINGS_CARDS.find(x => x.id === "floor_plan_editor");
    card.innerHTML = `
        <div class="panel-head"><div class="panel-title">${this._esc(c.title)}</div></div>
        ${this._floorPlanEditorCardBody()}`;
    this._wireFloorPlanEditor();
  }

  _wireFloorPlanEditor() {
    const root = this.shadowRoot;
    if (!root || !root.getElementById("fpnCanvas")) return;   // card not in the DOM right now

    root.querySelectorAll(".fpn-floor-tab").forEach(btn => {
      btn.addEventListener("click", () => {
        this._editorFloor = btn.getAttribute("data-fpn-floor");
        this._editVB = null;
        this._rerenderFloorPlanCard();
      });
    });
    const fit = root.getElementById("fpnZoomFit");
    if (fit) fit.addEventListener("click", () => { this._editVB = null; this._rerenderFloorPlanCard(); });

    const units = root.getElementById("fpnUnits");
    if (units) units.addEventListener("click", async () => {
      const next = this._fpUnits() === "metric" ? "imperial" : "metric";
      try {
        await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_units", value: next });
        if (this._liveData?.config) this._liveData.config.floor_plan_units = next;
      } catch (err) { console.error("Nova: floor plan units save failed", err); }
      this._rerenderFloorPlanCard();
    });

    const addRoom = root.getElementById("fpnAddRoom");
    if (addRoom) addRoom.addEventListener("click", () => {
      const plan = this._getEditingPlan();
      const floor = this._editorFloor;
      if (!plan[floor]) return;
      const name = window.prompt("Room name:");
      if (!name) return;
      const type = window.prompt("Type (room, bath, stairs, door):", "room") || "room";
      plan[floor].rooms = plan[floor].rooms || [];
      plan[floor].rooms.push({ name, x: 50, y: 50, w: 60, h: 40, type });
      this._rerenderFloorPlanCard();
    });

    const addZone = root.getElementById("fpnAddZone");
    if (addZone) addZone.addEventListener("click", () => {
      const plan = this._getEditingPlan();
      const floor = this._editorFloor;
      if (!plan[floor]) return;
      const name = window.prompt("Outdoor zone name (e.g. Front Yard, Driveway, Backyard):");
      if (!name) return;
      plan[floor].rooms = plan[floor].rooms || [];
      const house = plan[floor].rooms.filter(r => r.type !== "outdoor");
      let zx = 40, zy = 40, zw = 90, zh = 70;
      if (house.length) {
        let hx0 = 1e9, hx1 = -1e9, hy1 = -1e9;
        house.forEach(r => { hx0 = Math.min(hx0, r.x); hx1 = Math.max(hx1, r.x + r.w); hy1 = Math.max(hy1, r.y + r.h); });
        zx = Math.round(hx0); zy = Math.round(hy1 + 25); zw = Math.round(Math.max(hx1 - hx0, 90));
      }
      plan[floor].rooms.push({ name, x: zx, y: zy, w: zw, h: zh, type: "outdoor", points: [[zx, zy], [zx + zw, zy], [zx + zw, zy + zh], [zx, zy + zh]] });
      this._rerenderFloorPlanCard();
    });

    const addProperty = root.getElementById("fpnAddProperty");
    if (addProperty) addProperty.addEventListener("click", () => {
      const cur = this._propertyPts();
      if (cur.length >= 3) {
        if (window.confirm("Remove the property boundary?")) { this._setProperty([]); this._rerenderFloorPlanCard(); }
        return;
      }
      const floor = this._editorFloor;
      const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
      let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      rooms.forEach(r => { x0 = Math.min(x0, r.x); y0 = Math.min(y0, r.y); x1 = Math.max(x1, r.x + r.w); y1 = Math.max(y1, r.y + r.h); });
      if (!isFinite(x0)) { x0 = 20; y0 = 20; x1 = 220; y1 = 170; }
      const m = Math.max(150, Math.max(x1 - x0, y1 - y0) * 0.7);
      this._setProperty([[Math.round(x0 - m), Math.round(y0 - m)], [Math.round(x1 + m), Math.round(y0 - m)], [Math.round(x1 + m), Math.round(y1 + m)], [Math.round(x0 - m), Math.round(y1 + m)]]);
      this._rerenderFloorPlanCard();
    });

    const addElem = (type, kind) => {
      const floor = this._editorFloor;
      this._elemsFor(floor).push({ id: "e" + Date.now().toString(36), type, kind, wall: "front", pos: 0.5, w: 20, entity: "" });
      this._rerenderFloorPlanCard();
    };
    const opAddWindow = root.getElementById("opAddWindow"); if (opAddWindow) opAddWindow.addEventListener("click", () => addElem("window", null));
    const opAddExtdoor = root.getElementById("opAddExtdoor"); if (opAddExtdoor) opAddExtdoor.addEventListener("click", () => addElem("door", "exterior"));
    const opAddCellar = root.getElementById("opAddCellar"); if (opAddCellar) opAddCellar.addEventListener("click", () => addElem("door", "cellar"));
    const opAddIntdoor = root.getElementById("opAddIntdoor"); if (opAddIntdoor) opAddIntdoor.addEventListener("click", () => addElem("door", "interior"));
    const opAddCased = root.getElementById("opAddCased"); if (opAddCased) opAddCased.addEventListener("click", () => addElem("door", "cased"));
    const opAddFdormer = root.getElementById("opAddFdormer");
    if (opAddFdormer) opAddFdormer.addEventListener("click", () => {
      this._elemsFor(this._editorFloor).push({ id: "e" + Date.now().toString(36), type: "dormer", slope: "front", pos: 0.5, entity: "" });
      this._rerenderFloorPlanCard();
    });
    const opAddRdormer = root.getElementById("opAddRdormer");
    if (opAddRdormer) opAddRdormer.addEventListener("click", () => {
      this._elemsFor(this._editorFloor).push({ id: "e" + Date.now().toString(36), type: "dormer", slope: "rear", pos: 0.5, entity: "" });
      this._rerenderFloorPlanCard();
    });
    root.querySelectorAll(".op-field-new").forEach(f => {
      f.addEventListener("change", () => {
        const arr = this._elemsFor(this._editorFloor), e = arr[parseInt(f.getAttribute("data-i"))];
        if (!e) return;
        const op = f.getAttribute("data-op");
        if (op === "w") e.w = this._fpFromReal(parseFloat(f.value) || 4);
        else if (op === "pos") e.pos = parseFloat(f.value);
        else e[op] = f.value;
        this._rerenderFloorPlanCard();
      });
    });
    root.querySelectorAll(".op-del-new").forEach(b => b.addEventListener("click", () => {
      this._elemsFor(this._editorFloor).splice(parseInt(b.getAttribute("data-i")), 1);
      this._rerenderFloorPlanCard();
    }));
    const glowMarker = (i, on) => {
      const m = root.querySelector(`.fpn-op-marker[data-op-marker="${i}"]`);
      if (m) m.classList.toggle("op-glow", on);
    };
    root.querySelectorAll(".op-row-new").forEach(row => {
      const i = row.getAttribute("data-i");
      row.addEventListener("mouseenter", () => glowMarker(i, true));
      row.addEventListener("mouseleave", () => glowMarker(i, false));
      const ent = row.querySelector(".op-ent-new");
      if (ent) {
        ent.addEventListener("focus", () => glowMarker(i, true));
        ent.addEventListener("blur", () => glowMarker(i, false));
      }
    });

    const camAdd = root.getElementById("fpnCamAdd");
    if (camAdd) camAdd.addEventListener("click", () => {
      const floor = this._editorFloor;
      const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
      let ccx = 100, ccy = 80;
      if (rooms.length) {
        let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
        rooms.forEach(r => { mnx = Math.min(mnx, r.x); mny = Math.min(mny, r.y); mxx = Math.max(mxx, r.x + r.w); mxy = Math.max(mxy, r.y + r.h); });
        ccx = Math.round((mnx + mxx) / 2); ccy = Math.round((mny + mxy) / 2);
      }
      this._camsFor(floor).push({ id: "c" + Date.now().toString(36), x: ccx, y: ccy, angle: 270, fov: 90, range: 55, entity: "", indoor: true });
      this._rerenderFloorPlanCard();
    });
    const setCamField = (f) => {
      const cam = this._camsFor(this._editorFloor)[parseInt(f.getAttribute("data-ci"))];
      if (!cam) return null;
      const k = f.getAttribute("data-cam");
      if (k === "range") cam.range = this._fpFromReal(parseFloat(f.value) || 10);
      else if (k === "angle" || k === "fov") cam[k] = parseFloat(f.value);
      else cam[k] = f.value;
      return cam;
    };
    root.querySelectorAll(".cam-field-new").forEach(f => {
      f.addEventListener("input", () => {
        const cam = setCamField(f);
        if (!cam || f.getAttribute("data-cam") === "entity") return;
        const g = root.querySelector(`.fpn-cam[data-cam-idx="${f.getAttribute("data-ci")}"]`);
        if (g) { const cone = g.querySelector(".fpn-cam-cone"); if (cone) cone.setAttribute("d", this._clippedCone(cam, this._planGeometry(this._editorFloor))); }
      });
      f.addEventListener("change", () => { setCamField(f); this._rerenderFloorPlanCard(); });
    });
    root.querySelectorAll(".cam-io-new").forEach(b => b.addEventListener("click", () => {
      const cam = this._camsFor(this._editorFloor)[parseInt(b.getAttribute("data-ci"))];
      if (!cam) return; cam.indoor = (cam.indoor === false); this._rerenderFloorPlanCard();
    }));
    root.querySelectorAll(".cam-del-new").forEach(b => b.addEventListener("click", () => {
      this._camsFor(this._editorFloor).splice(parseInt(b.getAttribute("data-ci")), 1);
      this._rerenderFloorPlanCard();
    }));
    const camCompute = root.getElementById("fpnCamCompute");
    if (camCompute) camCompute.addEventListener("click", async () => {
      const floor = this._editorFloor;
      const cams = this._camsFor(floor);
      if (!cams.length) return;
      camCompute.disabled = true;
      const geo = this._planGeometry(floor), openings = this._openingDescriptions(floor);
      for (const cam of cams) {
        const cand = this._computeCoverage(floor, cam, geo);
        const ctx = { entity: cam.entity || "", room: this._roomAt(floor, cam.x, cam.y), fov: cam.fov || 90, range_ft: this._fpToReal(cam.range || 55), indoor: cam.indoor !== false, candidates: cand, openings };
        try { cam.coverage = await this._hass.callWS({ type: "nova/compute_camera_coverage", camera: ctx }); } catch (err) { /* keep going */ }
      }
      this._rerenderFloorPlanCard();
    });

    const save = root.getElementById("fpnSave");
    if (save) save.addEventListener("click", async () => {
      const hasProperty = this._editingProperty !== null && this._editingProperty !== undefined;
      if (!this._editingPlan && !this._editingEntities && !this._editingCameras && !this._editingElements && !hasProperty) return;
      const savedPlan = this._editingPlan, savedEnts = this._editingEntities,
        savedCams = this._editingCameras, savedEls = this._editingElements, savedProp = this._editingProperty;
      try {
        if (savedPlan) await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_rooms", value: JSON.stringify(savedPlan) });
        if (savedEnts) await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_entities", value: JSON.stringify(savedEnts) });
        if (savedCams) await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_cameras", value: JSON.stringify(savedCams) });
        if (savedEls) await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_elements", value: JSON.stringify(savedEls) });
        if (hasProperty) await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_property", value: JSON.stringify({ points: savedProp }) });
        if (this._liveData?.config) {
          if (savedPlan) this._liveData.config.floor_plan_rooms = savedPlan;
          if (savedEnts) this._liveData.config.floor_plan_entities = savedEnts;
          if (savedCams) this._liveData.config.floor_plan_cameras = savedCams;
          if (savedEls) this._liveData.config.floor_plan_elements = savedEls;
          if (hasProperty) this._liveData.config.floor_plan_property = { points: savedProp };
        }
        this._editingPlan = null;
        this._editingEntities = null;
        this._editingCameras = null;
        this._editingElements = null;
        this._editingProperty = null;
      } catch (err) { console.error("Nova: floor plan save failed", err); }
    });

    const reset = root.getElementById("fpnReset");
    if (reset) reset.addEventListener("click", async () => {
      try {
        await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_rooms", value: "" });
        if (this._liveData?.config) this._liveData.config.floor_plan_rooms = {};
      } catch (err) { console.error("Nova: floor plan reset failed", err); }
      this._editingPlan = null;
      this._editorFloor = null;
      this._rerenderFloorPlanCard();
    });

    // Export/Import the floor plan layout as JSON — a manual backup/restore,
    // or a way to copy a layout between installs.
    const fpExport = root.getElementById("fpnExport");
    if (fpExport) fpExport.addEventListener("click", () => {
      try {
        const data = JSON.stringify(this._getEditingPlan(), null, 2);
        const blob = new Blob([data], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = "nova-floor-plan.json";
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (err) { console.error("Nova: floor plan export failed", err); }
    });
    const fpImportBtn = root.getElementById("fpnImport");
    const fpImportFile = root.getElementById("fpnImportFile");
    if (fpImportBtn && fpImportFile) {
      fpImportBtn.addEventListener("click", () => fpImportFile.click());
      fpImportFile.addEventListener("change", () => {
        const file = fpImportFile.files && fpImportFile.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (ev) => {
          try {
            const parsed = JSON.parse(ev.target.result);
            if (!parsed || typeof parsed !== "object" || !Object.keys(parsed).length) throw new Error("empty");
            this._editingPlan = parsed;
            if (!this._editingPlan[this._editorFloor]) this._editorFloor = Object.keys(parsed)[0];
            this._rerenderFloorPlanCard();
          } catch (err) { console.error("Nova: floor plan import — invalid layout file", err); }
        };
        reader.readAsText(file);
        fpImportFile.value = "";
      });
    }

    const entAdd = root.getElementById("fpnEntAdd");
    if (entAdd) entAdd.addEventListener("click", () => {
      const inp = root.getElementById("fpnEntInput");
      const val = inp && inp.value.trim();
      if (!val) return;
      if (!(this._hass && this._hass.states && this._hass.states[val])) return;
      const floor = this._editorFloor;
      if (this._entsFor(floor).some(x => x.e === val)) return;
      const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
      let ecx = 100, ecy = 80;
      if (rooms.length) {
        let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
        rooms.forEach(r => { mnx = Math.min(mnx, r.x); mny = Math.min(mny, r.y); mxx = Math.max(mxx, r.x + r.w); mxy = Math.max(mxy, r.y + r.h); });
        ecx = Math.round((mnx + mxx) / 2); ecy = Math.round((mny + mxy) / 2);
      }
      this._entsFor(floor).push({ e: val, x: ecx, y: ecy });
      this._rerenderFloorPlanCard();
    });
    root.querySelectorAll(".fpn-ent-del").forEach(b => b.addEventListener("click", () => {
      this._entsFor(this._editorFloor).splice(parseInt(b.getAttribute("data-ei")), 1);
      this._rerenderFloorPlanCard();
    }));
    const bgUpBtn = root.getElementById("fpnBgUpload");
    const bgFileInput = root.getElementById("fpnBgFile");
    if (bgUpBtn && bgFileInput) {
      bgUpBtn.addEventListener("click", () => bgFileInput.click());
      bgFileInput.addEventListener("change", async () => {
        const file = bgFileInput.files && bgFileInput.files[0];
        if (!file) return;
        const floor = this._editorFloor;
        try {
          const dataUrl = await new Promise((resolve, reject) => {
            const r = new FileReader();
            r.onload = () => resolve(String(r.result));
            r.onerror = () => reject(new Error("read failed"));
            r.readAsDataURL(file);
          });
          let bgs = {};
          try {
            const raw = this._data()?.config?.floor_plan_bg;
            if (raw) bgs = typeof raw === "string" ? JSON.parse(raw) : raw;
          } catch (_) {}
          bgs[floor] = dataUrl;
          await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_bg", value: JSON.stringify(bgs) });
          if (this._liveData?.config) this._liveData.config.floor_plan_bg = JSON.stringify(bgs);
          this._rerenderFloorPlanCard();
        } catch (err) {
          console.error("Nova: floor plan background upload failed", err);
        } finally {
          bgFileInput.value = "";
        }
      });
    }

    const bgOp = root.getElementById("fpnBgOp");
    if (bgOp) {
      const bgVal = root.getElementById("fpnBgOpVal");
      bgOp.addEventListener("input", () => {
        if (bgVal) bgVal.textContent = Math.round(parseFloat(bgOp.value) * 100) + "%";
        const img = root.querySelector("#fpnSvg image");
        if (img) img.setAttribute("opacity", bgOp.value);
      });
      bgOp.addEventListener("change", async () => {
        const v = String(parseFloat(bgOp.value));
        try {
          await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_bg_opacity", value: v });
          if (this._liveData?.config) this._liveData.config.floor_plan_bg_opacity = v;
        } catch (err) { console.error("Nova: floor plan bg opacity save failed", err); }
      });
    }

    this._wireFloorPlanDrag();
  }

  _wireFloorPlanDrag() {
    const svgEl = this.shadowRoot.getElementById("fpnSvg");
    if (!svgEl) return;
    const self = this;
    const plan = this._getEditingPlan();
    const floor = this._editorFloor;
    const rooms = plan[floor]?.rooms;
    if (!rooms) return;

    let dragging = null, panning = null;

    function svgPoint(e) {
      const pt = svgEl.createSVGPoint();
      const ctm = svgEl.getScreenCTM().inverse();
      pt.x = e.clientX; pt.y = e.clientY;
      return pt.matrixTransform(ctm);
    }
    const vbFromAttr = () => {
      const p = (svgEl.getAttribute("viewBox") || "0 0 320 150").split(" ").map(Number);
      return { x: p[0], y: p[1], w: p[2], h: p[3] };
    };
    function applyVB() {
      const v = self._editVB; if (!v) return;
      svgEl.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
      svgEl.querySelectorAll(".fpn-grid-rect").forEach(r => {
        r.setAttribute("x", v.x); r.setAttribute("y", v.y); r.setAttribute("width", v.w); r.setAttribute("height", v.h);
      });
    }
    function redraw() {
      const canvas = self.shadowRoot.getElementById("fpnCanvas");
      if (canvas) {
        canvas.innerHTML = self._renderFloorPlanSVG(plan, floor);
        setTimeout(() => self._wireFloorPlanDrag(), 10);
      }
    }

    svgEl.querySelectorAll(".fpn-drag-room").forEach(g => {
      const rect = g.querySelector(".fpn-drag-rect");
      if (!rect) return;
      rect.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        const idx = parseInt(g.getAttribute("data-idx"));
        const rm = rooms[idx]; if (!rm) return;
        const pt = svgPoint(e);
        dragging = { idx, startX: pt.x, startY: pt.y, origX: rm.x, origY: rm.y, resize: false };
        rect.setAttribute("stroke-width", "2.5");
      });
      g.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const idx = parseInt(g.getAttribute("data-idx"));
        const rm = rooms[idx]; if (!rm) return;
        if (window.confirm(`Delete '${rm.name}' from floor plan?`)) { rooms.splice(idx, 1); redraw(); }
      });
    });

    svgEl.querySelectorAll(".fpn-resize-handle").forEach(handle => {
      handle.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const idx = parseInt(handle.getAttribute("data-idx"));
        const rm = rooms[idx]; if (!rm) return;
        const pt = svgPoint(e);
        dragging = { idx, startX: pt.x, startY: pt.y, origW: rm.w, origH: rm.h, resize: true };
      });
    });

    // Outdoor zone polygons: drag body (move), drag corner (reshape), add/remove corners
    svgEl.querySelectorAll(".fpn-zone-path").forEach(pth => {
      pth.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const zi = parseInt(pth.getAttribute("data-zone-idx"));
        const rm = rooms[zi]; if (!rm) return;
        self._ensureZonePoints(rm);
        const pt = svgPoint(e);
        dragging = { zoneBody: true, zi, startX: pt.x, startY: pt.y, ddx: 0, ddy: 0 };
      });
    });
    svgEl.querySelectorAll(".fpn-zone-vtx").forEach(v => {
      v.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const zi = parseInt(v.getAttribute("data-zone-idx")), vi = parseInt(v.getAttribute("data-vtx"));
        const rm = rooms[zi]; if (!rm) return;
        const pts = self._ensureZonePoints(rm); const p = pts[vi]; if (!p) return;
        const pt = svgPoint(e);
        dragging = { zoneVtx: true, zi, vi, startX: pt.x, startY: pt.y, origX: p[0], origY: p[1] };
      });
      v.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const zi = parseInt(v.getAttribute("data-zone-idx")), vi = parseInt(v.getAttribute("data-vtx"));
        const rm = rooms[zi]; if (!rm) return;
        const pts = self._ensureZonePoints(rm);
        if (pts.length <= 3) return;
        pts.splice(vi, 1); self._syncRoomBBox(rm); self._rerenderFloorPlanCard();
      });
    });
    svgEl.querySelectorAll(".fpn-zone-mid").forEach(m => {
      m.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const zi = parseInt(m.getAttribute("data-zone-idx")), ei = parseInt(m.getAttribute("data-edge"));
        const rm = rooms[zi]; if (!rm) return;
        const pts = self._ensureZonePoints(rm);
        const a = pts[ei], b = pts[(ei + 1) % pts.length]; if (!a || !b) return;
        pts.splice(ei + 1, 0, [Math.round((a[0] + b[0]) / 2), Math.round((a[1] + b[1]) / 2)]);
        self._rerenderFloorPlanCard();
      });
    });

    // Property boundary: drag a corner, add a corner (edge midpoint), remove (right-click)
    svgEl.querySelectorAll(".fpn-prop-vtx").forEach(v => {
      v.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const pi = parseInt(v.getAttribute("data-prop-vtx"));
        const p = (self._propertyPts() || [])[pi];
        if (!p) return;
        const pt = svgPoint(e);
        dragging = { propVtx: pi, startX: pt.x, startY: pt.y, origX: p[0], origY: p[1], property: true };
      });
      v.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const pi = parseInt(v.getAttribute("data-prop-vtx"));
        const pts = self._propertyPts();
        if (pts.length <= 3) return;
        pts.splice(pi, 1); self._rerenderFloorPlanCard();
      });
    });
    svgEl.querySelectorAll(".fpn-prop-mid").forEach(m => {
      m.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const ei = parseInt(m.getAttribute("data-prop-edge"));
        const pts = self._propertyPts();
        const a = pts[ei], b = pts[(ei + 1) % pts.length];
        if (!a || !b) return;
        pts.splice(ei + 1, 0, [Math.round((a[0] + b[0]) / 2), Math.round((a[1] + b[1]) / 2)]);
        self._rerenderFloorPlanCard();
      });
    });

    // Camera drag + right-click delete
    svgEl.querySelectorAll(".fpn-cam").forEach(g => {
      const dot = g.querySelector(".fpn-cam-dot");
      if (dot) dot.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const ci = parseInt(g.getAttribute("data-cam-idx"));
        const cam = (self._camsFor(floor) || [])[ci];
        if (!cam) return;
        const pt = svgPoint(e);
        dragging = { camIdx: ci, startX: pt.x, startY: pt.y, origX: cam.x, origY: cam.y, camera: true, geo: self._planGeometry(floor) };
      });
      g.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const ci = parseInt(g.getAttribute("data-cam-idx"));
        const arr = self._camsFor(floor);
        if (arr[ci] && window.confirm("Delete this camera?")) { arr.splice(ci, 1); redraw(); }
      });
    });

    // Device pins — drag to move (saved with the plan); a tap with no drag
    // opens the entity's controls; right-click removes it.
    svgEl.querySelectorAll(".fpn-ent").forEach(g => {
      g.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const ei = parseInt(g.getAttribute("data-ent-idx"));
        const ent = (self._entsFor(floor) || [])[ei];
        if (!ent) return;
        const pt = svgPoint(e);
        dragging = { entIdx: ei, startX: pt.x, startY: pt.y, origX: ent.x, origY: ent.y, entity: true, moved: false, entId: g.getAttribute("data-ent-id") };
      });
      g.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const ei = parseInt(g.getAttribute("data-ent-idx"));
        const arr = self._entsFor(floor);
        if (arr[ei] && window.confirm("Remove this device from the plan?")) { arr.splice(ei, 1); redraw(); }
      });
    });

    svgEl.addEventListener("mousemove", (e) => {
      if (panning) {
        const rect = svgEl.getBoundingClientRect();
        const sx = panning.w / rect.width, sy = panning.h / rect.height;
        self._editVB = {
          x: panning.vbX - (e.clientX - panning.sx) * sx, y: panning.vbY - (e.clientY - panning.sy) * sy,
          w: panning.w, h: panning.h,
        };
        applyVB();
        return;
      }
      if (!dragging) return;
      const pt = svgPoint(e);
      if (dragging.zoneVtx) {
        const rm = rooms[dragging.zi]; if (!rm || !rm.points) return;
        const p = rm.points[dragging.vi]; if (!p) return;
        p[0] = Math.round(dragging.origX + (pt.x - dragging.startX));
        p[1] = Math.round(dragging.origY + (pt.y - dragging.startY));
        const g = svgEl.querySelector(`.fpn-zone[data-zone-idx="${dragging.zi}"]`);
        if (g) {
          const path = g.querySelector(".fpn-zone-path"); if (path) path.setAttribute("d", self._propPathD(rm.points));
          const dot = g.querySelector(`.fpn-zone-vtx[data-vtx="${dragging.vi}"]`); if (dot) { dot.setAttribute("cx", p[0]); dot.setAttribute("cy", p[1]); }
        }
        return;
      }
      if (dragging.zoneBody) {
        dragging.ddx = Math.round(pt.x - dragging.startX); dragging.ddy = Math.round(pt.y - dragging.startY);
        const g = svgEl.querySelector(`.fpn-zone[data-zone-idx="${dragging.zi}"]`);
        if (g) g.setAttribute("transform", `translate(${dragging.ddx},${dragging.ddy})`);
        return;
      }
      if (dragging.property) {
        const p = (self._propertyPts() || [])[dragging.propVtx];
        if (!p) return;
        p[0] = Math.round(dragging.origX + (pt.x - dragging.startX));
        p[1] = Math.round(dragging.origY + (pt.y - dragging.startY));
        const dot = svgEl.querySelector(`.fpn-prop-vtx[data-prop-vtx="${dragging.propVtx}"]`);
        if (dot) { dot.setAttribute("cx", p[0]); dot.setAttribute("cy", p[1]); }
        const path = svgEl.querySelector(".fpn-prop-path");
        if (path) path.setAttribute("d", self._propPathD(self._propertyPts()));
        return;
      }
      if (dragging.camera) {
        const cam = (self._camsFor(floor) || [])[dragging.camIdx];
        if (!cam) return;
        cam.x = Math.round(dragging.origX + (pt.x - dragging.startX));
        cam.y = Math.round(dragging.origY + (pt.y - dragging.startY));
        const gc = svgEl.querySelector(`.fpn-cam[data-cam-idx="${dragging.camIdx}"]`);
        if (gc) {
          const dot = gc.querySelector(".fpn-cam-dot"); if (dot) { dot.setAttribute("cx", cam.x); dot.setAttribute("cy", cam.y); }
          const cone = gc.querySelector(".fpn-cam-cone"); if (cone) cone.setAttribute("d", self._clippedCone(cam, dragging.geo));
          const tx = gc.querySelector("text"); if (tx) { tx.setAttribute("x", cam.x); tx.setAttribute("y", cam.y - 5); }
        }
        return;
      }
      if (dragging.entity) {
        const ent = (self._entsFor(floor) || [])[dragging.entIdx];
        if (!ent) return;
        const nx = Math.round(dragging.origX + (pt.x - dragging.startX));
        const ny = Math.round(dragging.origY + (pt.y - dragging.startY));
        if (Math.abs(nx - dragging.origX) > 1 || Math.abs(ny - dragging.origY) > 1) dragging.moved = true;
        ent.x = nx; ent.y = ny;
        const ge = svgEl.querySelector(`.fpn-ent[data-ent-idx="${dragging.entIdx}"]`);
        if (ge) {
          const dot = ge.querySelector(".fpn-ent-dot"); if (dot) { dot.setAttribute("cx", nx); dot.setAttribute("cy", ny); }
          const nm = ge.querySelector(".fpn-ent-nm"); if (nm) { nm.setAttribute("x", nx); nm.setAttribute("y", ny - 4); }
          const vl = ge.querySelector(".fpn-ent-val"); if (vl) { vl.setAttribute("x", nx); vl.setAttribute("y", ny + 6.5); }
        }
        return;
      }
      const rm = rooms[dragging.idx];
      if (!rm) return;
      if (dragging.resize) {
        rm.w = Math.max(15, Math.round(dragging.origW + (pt.x - dragging.startX)));
        rm.h = Math.max(10, Math.round(dragging.origH + (pt.y - dragging.startY)));
      } else {
        rm.x = Math.round(dragging.origX + (pt.x - dragging.startX));
        rm.y = Math.round(dragging.origY + (pt.y - dragging.startY));
      }
      const g = svgEl.querySelector(`.fpn-drag-room[data-idx="${dragging.idx}"]`);
      if (g) {
        const r = g.querySelector(".fpn-drag-rect");
        if (r) { r.setAttribute("x", rm.x); r.setAttribute("y", rm.y); r.setAttribute("width", rm.w); r.setAttribute("height", rm.h); }
        const t = g.querySelector("text");
        if (t) { t.setAttribute("x", rm.x + rm.w / 2); t.setAttribute("y", rm.y + rm.h / 2); }
        const rh = g.querySelector(".fpn-resize-handle");
        if (rh) { rh.setAttribute("x", rm.x + rm.w - 8); rh.setAttribute("y", rm.y + rm.h - 8); }
      }
    });

    const endDrag = () => {
      if (panning) { panning = null; return; }
      if (!dragging) return;
      if (dragging.zoneBody) {
        const rm = rooms[dragging.zi];
        if (rm && rm.points && (dragging.ddx || dragging.ddy)) rm.points.forEach(p => { p[0] += dragging.ddx; p[1] += dragging.ddy; });
      }
      if ((dragging.zoneVtx || dragging.zoneBody) && rooms[dragging.zi]) self._syncRoomBBox(rooms[dragging.zi]);
      // A device pin clicked without dragging → open its HA more-info controls.
      if (dragging.entity && !dragging.moved && dragging.entId) {
        self.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: dragging.entId }, bubbles: true, composed: true }));
      }
      const heavy = dragging.camera || dragging.property || dragging.zoneVtx || dragging.zoneBody;
      dragging = null;
      if (heavy) self._rerenderFloorPlanCard(); else redraw();
    };
    svgEl.addEventListener("mouseup", endDrag);
    svgEl.addEventListener("mouseleave", endDrag);

    svgEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      const v = self._editVB || vbFromAttr();
      const p = svgPoint(e);
      const f = e.deltaY < 0 ? 0.85 : 1.18;
      const nw = Math.max(60, Math.min(8000, v.w * f)), nh = Math.max(42, Math.min(8000, v.h * f));
      const fx = nw / v.w, fy = nh / v.h;
      self._editVB = { x: p.x - (p.x - v.x) * fx, y: p.y - (p.y - v.y) * fy, w: nw, h: nh };
      applyVB();
    }, { passive: false });

    svgEl.addEventListener("mousedown", (e) => {
      if (dragging) return;
      const mid = e.button === 1;
      const bg = e.button === 0 && !e.target.closest(".fpn-drag-room, .fpn-resize-handle, .fpn-zone, .fpn-zone-vtx, .fpn-zone-mid, .fpn-zone-path, .fpn-prop-vtx, .fpn-prop-mid, .fpn-cam, .fpn-ent");
      if (!mid && !bg) return;
      e.preventDefault();
      const v = self._editVB || vbFromAttr();
      self._editVB = { x: v.x, y: v.y, w: v.w, h: v.h };
      panning = { sx: e.clientX, sy: e.clientY, vbX: v.x, vbY: v.y, w: v.w, h: v.h };
    });
  }

  _css() {
    return `
      :host{
        --bg:#15110d; --surface:#1e1712; --surface-2:#2a2119; --line-soft:#33291f;
        --ink:#f3ece1; --ink-dim:#a89a89; --ink-faint:#7a6d5e;
        --ember:#e2542f; --gold:#f4b860; --gold-pale:#ffe3ad; --warn:#e8b23d;
        --font-display:'Fraunces',ui-serif,Georgia,serif;
        --font-body:'Manrope',system-ui,-apple-system,'Segoe UI',sans-serif;
        --font-mono:'IBM Plex Mono',ui-monospace,'SF Mono',monospace;
      }
      *{box-sizing:border-box}
      .wrap{background:var(--bg);color:var(--ink);font-family:var(--font-body);
        padding:20px 16px 40px;min-height:100vh;
        background-image:radial-gradient(ellipse 900px 500px at 50% -8%, #2a1c1180 0%, transparent 60%);}
      .topbar{display:flex;align-items:center;justify-content:flex-start;gap:24px;margin-bottom:22px;flex-wrap:wrap;max-width:1100px;margin-inline:auto}
      .brand{display:flex;align-items:center;gap:11px}
      .brand-mark{width:26px;height:26px;border-radius:50%;flex:none;
        background:radial-gradient(circle at 34% 30%, var(--gold-pale), var(--gold) 42%, var(--ember) 78%, #7a2513 100%);
        box-shadow:0 0 14px 1px #e2542f55;}
      .brand-name{font-family:var(--font-display);font-size:18px;font-weight:600}
      .brand-tag{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);letter-spacing:.1em;text-transform:uppercase}
      .hero{max-width:1100px;margin:0 auto;background:linear-gradient(180deg,var(--surface),#19140fdd);
        border:1px solid var(--line-soft);border-radius:22px;padding:32px 20px 24px;
        display:flex;flex-direction:column;align-items:center;text-align:center}
      .core-wrap{width:min(70vw,280px);aspect-ratio:1/1;margin-bottom:4px}
      canvas.core{width:100%;height:100%;display:block}
      .state-line{font-family:var(--font-display);font-size:19px;font-weight:500;margin:4px 0 2px;text-wrap:balance}
      .state-sub{font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint);letter-spacing:.05em;margin-bottom:18px}
      .chips{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;padding-top:16px;border-top:1px solid var(--line-soft);width:100%}
      .chip{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:20px;background:var(--surface-2);
        font-family:var(--font-mono);font-size:10.5px;color:var(--ink-dim);border:1px solid var(--line-soft)}
      .chip .dot{width:6px;height:6px;border-radius:50%;background:#6fbf8a}
      .chip.warn .dot{background:var(--warn)}
      .chip b{color:var(--ink);font-weight:600}
      .grid{max-width:1100px;margin:16px auto 0;display:grid;grid-template-columns:1fr;gap:16px}
      .panel{background:var(--surface);border:1px solid var(--line-soft);border-radius:16px;padding:16px 16px 14px}
      .panel-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px}
      .panel-title{font-family:var(--font-display);font-size:15px;font-weight:600}
      .panel-meta{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);letter-spacing:.05em}
      .lockdown-control{margin-left:auto;font-family:var(--font-mono);font-size:10px;font-weight:600;letter-spacing:.06em;
        padding:8px 12px;border-radius:9px;border:1px solid var(--line-soft);background:var(--surface);color:var(--ink-faint);cursor:pointer}
      .lockdown-control.active{color:#ffd7d7;background:#7d2028;border-color:#d95b65;box-shadow:0 0 16px #d95b6533}
      .onboarding-card{max-width:1100px;margin:0 auto 16px;background:linear-gradient(135deg,#f4b86012,var(--surface));border:1px solid #f4b86066;border-radius:16px;padding:16px}
      .onboarding-progress{position:relative;height:20px;background:var(--surface-2);border-radius:8px;overflow:hidden;margin:12px 0}
      .onboarding-progress i{position:absolute;inset:0 auto 0 0;background:#f4b86033}.onboarding-progress span{position:relative;z-index:1;display:block;padding:4px 8px;font-family:var(--font-mono);font-size:9px}
      .onboarding-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:7px;margin-bottom:10px}
      .onboarding-step{display:grid;grid-template-columns:18px 1fr auto;align-items:center;gap:7px;background:var(--surface-2);border:1px solid var(--line-soft);border-radius:9px;padding:8px;color:var(--ink-dim)}
      .onboarding-step.done{opacity:.62}.onboarding-step b{display:block;font-size:11px}.onboarding-step small{display:block;font-size:9px;color:var(--ink-faint);margin-top:2px}
      .dashboard-pair{max-width:1100px;margin:16px auto 0;display:grid;grid-template-columns:1fr 1fr;gap:16px}
      @media (max-width:760px){.dashboard-pair{grid-template-columns:1fr}}
      .metric-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
      .metric{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:9px;padding:9px;display:flex;flex-direction:column;gap:2px}
      .metric b{font-family:var(--font-mono);font-size:14px}.metric span{font-size:10px;color:var(--ink-faint)}
      .goal-list{display:flex;flex-direction:column;gap:7px;max-height:300px;overflow:auto}
      .goal-row{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--surface-2);border:1px solid var(--line-soft);border-radius:9px;padding:9px}
      .goal-copy{min-width:0;display:flex;flex-direction:column;gap:2px}.goal-copy b{font-size:12px}.goal-copy span{font-size:11px;color:var(--ink-dim);overflow-wrap:anywhere}
      .goal-copy small{font-family:var(--font-mono);font-size:9px;color:var(--ink-faint)}
      .goal-create{display:flex;gap:8px;margin-top:10px}.goal-create .cfg-field{flex:1;min-width:0}.empty-state{font-size:12px;color:var(--ink-faint);padding:12px 0}
      .feed{max-height:420px;overflow-y:auto}
      .feed::-webkit-scrollbar{width:3px}
      .feed::-webkit-scrollbar-track{background:var(--surface-2)}
      .feed::-webkit-scrollbar-thumb{background:var(--line-soft);border-radius:3px}
      .feed-row{padding:9px 0;border-bottom:1px solid var(--line-soft);display:flex;justify-content:space-between;gap:10px}
      .feed-row:last-child{border-bottom:none}
      .feed-text{font-size:12.8px;line-height:1.4}
      .feed-text .dim{color:var(--ink-dim)}
      .feed-time{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);white-space:nowrap}
      .areas-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:10px}
      @media (max-width:1100px){.areas-grid{grid-template-columns:repeat(4,1fr)}}
      @media (max-width:560px){.areas-grid{grid-template-columns:repeat(2,1fr)}}
      .area-tile{position:relative;background:var(--surface-2);border:1px solid var(--line-soft);border-radius:13px;
        padding:12px 12px 10px;overflow:hidden;transition:border-color .25s,box-shadow .25s}
      .area-tile::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;
        background:linear-gradient(90deg,#6ea8ff,var(--gold) 55%,var(--ember));opacity:.55}
      .area-tile.no-temp::before{display:none}
      .area-tile.active{border-color:#e2542f70;box-shadow:inset 0 0 14px #e2542f14,0 0 14px #e2542f12}
      .area-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
      .area-name{font-family:var(--font-display);font-size:14.5px;font-weight:600;display:flex;align-items:center;gap:6px}
      .live-dot{width:6px;height:6px;border-radius:50%;background:#5fbf7a;box-shadow:0 0 6px 1px #5fbf7a99;
        animation:novaLivePulse 2.4s ease-in-out infinite;flex:none}
      @keyframes novaLivePulse{0%,100%{opacity:1}50%{opacity:.45}}
      @media (prefers-reduced-motion: reduce){.live-dot{animation:none}}
      .area-caps{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:9px;min-height:20px}
      .area-cap{width:21px;height:21px;border-radius:6px;background:var(--surface);border:1px solid var(--line-soft);
        display:flex;align-items:center;justify-content:center;font-size:10.5px;opacity:.85}
      .area-climate{display:flex;gap:10px;margin-bottom:9px}
      .area-climate-item{flex:1;min-width:0}
      .area-climate-num{font-family:var(--font-mono);font-size:12.5px;font-weight:500;display:flex;align-items:baseline;gap:3px}
      .area-climate-num .unit{font-size:9px;color:var(--ink-faint)}
      .area-climate svg{display:block;width:100%;height:16px;margin-top:2px}
      .area-bottom{display:flex;align-items:center;justify-content:space-between;padding-top:8px;border-top:1px solid var(--line-soft)}
      .area-stat{font-family:var(--font-mono);font-size:10px;color:var(--ink-dim)}
      .area-stat b{color:var(--ink);font-weight:600}
      .area-light-toggle{font-family:var(--font-mono);font-size:9px;font-weight:600;letter-spacing:.05em;
        padding:3px 9px;border-radius:7px;border:1px solid var(--line-soft);background:var(--surface);
        color:var(--ink-faint);cursor:pointer}
      .area-light-toggle.on{background:#f4b8602a;border-color:#f4b86070;color:var(--gold-pale)}
      .camera-panel{grid-column:1/-1}
      .camera-head-row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
      .camera-note{font-size:11.5px;color:var(--ink-dim);max-width:46ch}
      .camera-toggle{font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint);background:var(--surface-2);
        border:1px solid var(--line-soft);border-radius:8px;padding:6px 10px;cursor:pointer}
      .camera-strip{display:none;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;margin-top:12px}
      .camera-strip.open{display:grid}
      .camera-slot{border-radius:10px;background:var(--surface-2);border:1px solid var(--line-soft);overflow:hidden;padding-bottom:9px}
      .camera-slot img,.camera-empty{width:100%;aspect-ratio:16/9;object-fit:cover;display:flex;align-items:center;justify-content:center;background:#080706;color:var(--ink-faint);font-family:var(--font-mono);font-size:10px}
      .camera-caption{padding:8px 9px 4px;display:flex;flex-direction:column;gap:2px}.camera-caption b{font-size:12px}.camera-caption span{font-family:var(--font-mono);font-size:9px;color:var(--ink-faint)}
      .camera-actions{display:flex;flex-wrap:wrap;gap:5px;padding:4px 9px}.camera-actions .mode-chip{padding:4px 7px;font-size:9px}
      .camera-diagnostic{font-size:10px;color:var(--ink-dim);line-height:1.35;padding:5px 9px 0;overflow-wrap:anywhere}
      .footnote{max-width:1100px;margin:20px auto 0;text-align:center;font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);letter-spacing:.05em}
      .new-log-entries{max-height:65vh;overflow-y:auto;display:flex;flex-direction:column;gap:1px;margin-top:8px}
      .intr-snap{margin-bottom:10px}
      .intr-img{width:100%;max-width:320px;border-radius:10px;border:1px solid var(--line-soft);display:block;margin-bottom:6px}
      .new-ilog-item{padding:10px 0;border-top:1px solid var(--line-soft)}
      .new-ilog-item:first-of-type{border-top:none}
      .new-log-entry{display:grid;grid-template-columns:70px 110px 1fr;gap:10px;padding:7px 8px;
        font-family:var(--font-mono);font-size:11px;border-bottom:1px solid var(--line-soft);align-items:baseline}
      .new-log-entry-error{background:#ff5a5a14}
      .new-log-ts{color:var(--ink-faint)}
      .new-log-cat{white-space:nowrap;font-weight:600}
      .new-log-msg{color:var(--ink-dim);word-break:break-word}
      @media (max-width:560px){.new-log-entry{grid-template-columns:1fr;gap:2px}}

      /* Top nav (v7.94.0) */
      .top-nav{display:flex;flex-wrap:wrap;gap:4px;background:var(--surface);border:1px solid var(--line-soft);border-radius:11px;padding:4px}
      .nav-tab{font-family:var(--font-body);font-size:12.5px;font-weight:600;padding:7px 14px;border-radius:8px;
        border:none;background:transparent;color:var(--ink-dim);cursor:pointer}
      .nav-tab.active{background:var(--ember);color:#1e0d06}
      .nav-tab:not(.active):hover{color:var(--ink)}

      /* Settings (v7.94.0) */
      .settings-toolbar{max-width:1100px;margin:0 auto 16px;display:flex;flex-direction:column;gap:10px}
      .settings-search{width:100%;background:var(--surface);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:13px;padding:10px 14px;border-radius:10px}
      .settings-search::placeholder{color:var(--ink-faint)}
      .settings-nav{display:flex;flex-wrap:wrap;gap:6px}
      .settings-nav-btn{font-family:var(--font-body);font-size:11.5px;font-weight:600;padding:6px 12px;border-radius:20px;
        border:1px solid var(--line-soft);background:var(--surface);color:var(--ink-dim);cursor:pointer}
      .settings-nav-btn.active{background:var(--ember);border-color:var(--ember);color:#1e0d06}
      .settings-grid{max-width:1100px;margin:0 auto;column-count:2;column-gap:14px}
      @media (max-width:720px){.settings-grid{column-count:1}}
      .settings-card{break-inside:avoid;margin-bottom:14px;display:inline-block;width:100%}
      .settings-card[hidden]{display:none}
      .stub-tag{font-family:var(--font-mono);font-size:9px;letter-spacing:.08em;color:var(--ink-faint);
        background:var(--surface-2);border:1px solid var(--line-soft);border-radius:20px;padding:2px 8px;margin-left:8px;vertical-align:middle}
      .stub-body{font-size:12.5px;color:var(--ink-dim);line-height:1.5}
      .stub-where{display:block;margin-top:6px;font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint)}
      .cfg-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
      .cfg-row-wrap{flex-wrap:wrap;justify-content:flex-start}
      .cfg-row label{font-size:12.5px;color:var(--ink-dim)}
      select.cfg-field,input.cfg-field{background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:12px;padding:6px 9px;border-radius:8px}
      input.cfg-field:hover,input.cfg-field:focus,select.cfg-field:hover,select.cfg-field:focus{border-color:var(--gold);outline:none}
      .cfg-num{width:84px;min-width:0;text-align:right}
      .door-map-sel-new{flex:1;min-width:0;max-width:220px}
      .mode-grid{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
      .mode-chip{font-family:var(--font-mono);font-size:11px;text-transform:uppercase;letter-spacing:.04em;
        padding:6px 12px;border-radius:8px;border:1px solid var(--line-soft);background:var(--surface-2);color:var(--ink-dim);cursor:pointer}
      .mode-chip:hover{border-color:var(--gold)}
      .mode-chip-on{background:var(--ember);border-color:var(--ember);color:var(--gold-pale)}
      .mode-bind-head{font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint);letter-spacing:.05em;
        text-transform:uppercase;margin:12px 0 8px;padding-top:12px;border-top:1px solid var(--line-soft)}
      .diag-ok{color:#5fbf7a} .diag-warn{color:var(--warn)} .diag-idle{color:var(--ink-dim)}
      .diag-down{color:#ff6b81} .diag-off{color:var(--ink-faint)}
      .new-model-list{display:flex;flex-direction:column;gap:10px}
      .new-model-row{display:grid;grid-template-columns:88px 1fr 1.3fr auto;gap:6px;align-items:center}
      .model-label{font-family:var(--font-mono);font-size:10px;letter-spacing:.1em;color:var(--ink-faint);text-transform:uppercase}
      .new-model-row .new-prov-select,.new-model-row .new-model-select{width:100%;min-width:0;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:11.5px;padding:6px 8px;border-radius:8px}
      .new-model-custom{grid-column:2/4;width:100%;box-sizing:border-box;padding:6px 9px;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--gold);
        font-family:var(--font-mono);font-size:11px;border-radius:8px}
      .new-model-custom:focus{outline:none;border-color:var(--gold)}
      .new-model-refresh{padding:5px 9px;font-size:12px;line-height:1}
      .new-model-warning{grid-column:1/-1;font-size:10.5px;color:var(--warn);margin-top:2px}
      .new-model-row .stub-body{grid-column:1/-1;font-size:10.5px;margin-top:2px}
      .cred-row{display:grid;grid-template-columns:88px 74px 1fr auto auto;gap:6px;align-items:center}
      .cred-status{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);text-transform:uppercase}
      .cred-status.cred-configured{color:var(--gold)}
      .cred-input{width:100%;min-width:0;box-sizing:border-box;padding:6px 8px;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:11.5px;border-radius:8px}
      .cred-input:focus{outline:none;border-color:var(--gold)}
      .new-appliance-list{display:flex;flex-direction:column;gap:8px;margin-bottom:10px}
      .new-appliance-row{display:grid;grid-template-columns:1.1fr .9fr 1.3fr 64px 28px;gap:6px;align-items:center}
      .new-appliance-row input,.new-appliance-row select{background:var(--surface-2);border:1px solid var(--line-soft);
        color:var(--ink);font-family:var(--font-body);font-size:11px;padding:5px 7px;border-radius:7px;min-width:0;width:100%;box-sizing:border-box}
      .new-appliance-row input:focus,.new-appliance-row select:focus{outline:none;border-color:var(--gold)}
      .new-appliance-remove{flex:none;width:26px;height:26px;padding:0;font-size:11px;color:#ff8a8a;
        border:1px solid #ff5a5a4d;background:transparent;border-radius:7px;cursor:pointer}
      .new-appliance-remove:hover{border-color:#ff5a5a;background:#ff5a5a14}
      .new-pl-chip{display:inline-flex;align-items:center;gap:5px;font-family:var(--font-mono);font-size:10.5px;
        padding:5px 8px;border-radius:8px;border:1px solid var(--line-soft);background:var(--surface-2);color:var(--ink-dim)}
      .new-pl-del,.new-excl-ent-del,.new-excl-dom-del,.new-excl-lab-del,.new-mem-forget{background:none;border:none;color:var(--ink-faint);cursor:pointer;font-size:12px;padding:0}
      .new-pl-del:hover,.new-excl-ent-del:hover,.new-excl-dom-del:hover,.new-excl-lab-del:hover,.new-mem-forget:hover{color:#ff5a5a}
      .new-camset-row{padding:10px 0;border-top:1px solid var(--line-soft)}
      .new-camset-row:first-of-type{border-top:none}
      .toggle-list{display:flex;flex-direction:column;gap:2px}
      .toggle-row{display:grid;grid-template-columns:1fr auto;grid-template-rows:auto auto;gap:2px 10px;
        padding:9px 0;border-top:1px solid var(--line-soft)}
      .toggle-row:first-child{border-top:none}
      .toggle-label{font-size:12.5px;font-weight:600;grid-column:1;grid-row:1}
      .toggle-desc{font-size:11px;color:var(--ink-faint);grid-column:1;grid-row:2}
      .toggle-btn{grid-column:2;grid-row:1/3;align-self:center;font-family:var(--font-mono);font-size:10.5px;font-weight:600;
        letter-spacing:.05em;padding:6px 12px;border-radius:8px;border:1px solid var(--line-soft);background:var(--surface-2);
        color:var(--ink-faint);cursor:pointer;min-width:44px}
      .toggle-btn.on{background:#5fbf7a2a;border-color:#5fbf7a70;color:#8fdba8}
      .toggle-row select.cfg-field{grid-column:2;grid-row:1/3;align-self:center}
      .pairing-list{display:flex;flex-direction:column;gap:8px;margin-bottom:8px}
      .pairing-row{display:flex;align-items:center;justify-content:space-between;gap:10px}
      .pairing-label{font-size:12.5px;color:var(--ink-dim)}
      .pairing-row select{background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:12px;padding:6px 9px;border-radius:8px;max-width:56%}
      .person-honorific-row{flex-wrap:wrap}
      .person-honorific-custom{background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:12px;padding:6px 9px;border-radius:8px;width:100%;margin-top:6px}
      .fpn-toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px}
      .fpn-floor-tabs,.fpn-actions{display:flex;gap:6px;flex-wrap:wrap}
      .fpn-hint{font-size:10px;color:var(--ink-faint);font-family:var(--font-mono);letter-spacing:0.04em;margin-bottom:6px}
      .fpn-canvas{min-height:520px;margin-bottom:10px}
      .fpn-actions{margin-top:4px}
      .fpn-op-marker.op-glow{opacity:1;stroke:var(--ink);stroke-width:2.5;filter:drop-shadow(0 0 6px var(--gold-pale))}
      /* Openings/cameras rows pack more controls than a plain cfg-row (chip,
         wall/room select, slider, size, entity select, delete). flex-wrap
         alone isn't enough: a native <input type=range>/<select> has no
         intrinsic width limit, so two of them can already be wider than a
         settings-card's column before wrapping even has a reason to kick
         in -- the settings-grid uses CSS columns, which don't clip
         horizontal overflow, so a too-wide row bleeds into the next card
         over instead of being clipped. Give every control in these rows an
         explicit cap so the row actually has narrow enough pieces to wrap. */
      .op-row-new,.cam-row-new{flex-wrap:wrap;row-gap:6px;max-width:100%}
      .op-row-new select,.cam-row-new select{flex:0 1 auto;max-width:110px}
      .op-row-new input[type="range"],.cam-row-new input[type="range"]{flex:0 0 auto;width:70px}
      .op-row-new input[type="number"],.cam-row-new input[type="number"]{flex:0 0 auto;width:44px}
      .fpn-inline-lbl{display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--ink-dim)}
      .res-tab-new{display:grid;grid-template-columns:1fr 260px;gap:16px}
      .res-scene-new{min-height:360px;background:var(--surface-2);border:1px solid var(--line-soft);border-radius:10px;
        margin-bottom:10px;cursor:grab;touch-action:none;display:flex;align-items:center;justify-content:center;overflow:hidden}
      .res-scene-new.dragging{cursor:grabbing}
      .res-scene-new svg{max-width:100%;height:auto}
      .res-stats-new{margin-top:6px}
      .res-side-new{display:flex;flex-direction:column;gap:6px}
      @media (max-width:720px){.res-tab-new{grid-template-columns:1fr}}
    `;
  }
}

if (!customElements.get("nova-panel")) {
  customElements.define("nova-panel", NovaPanel);
}
