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

