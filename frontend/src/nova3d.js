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

