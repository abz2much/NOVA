  // ─── Floor plan maths: walls, sight lines and camera coverage ──────────
  // Every result here is pinned in scripts/smoke_panel.js, so keep the
  // arithmetic in the same order when changing anything below.

  // The crossing point of segment A (x1,y1 to x2,y2) and segment B
  // (x3,y3 to x4,y4), or null when they are parallel or do not meet.
  _segIntersect(x1, y1, x2, y2, x3, y3, x4, y4) {
    const ax = x1 - x2, ay = y1 - y2, bx = x3 - x4, by = y3 - y4, cx = x1 - x3, cy = y1 - y3;
    const cross = ax * by - ay * bx;
    if (Math.abs(cross) < 1e-9) return null;
    const along = (cx * by - cy * bx) / cross;
    const onB = (cx * ay - cy * ax) / cross;
    if (along < 0 || along > 1 || onB < 0 || onB > 1) return null;
    return { x: x1 + along * (x2 - x1), y: y1 + along * (y2 - y1) };
  }

  // Solid walls (the outline of every walled room) and the gaps doors and
  // windows leave in them. Stairs, door rooms and outdoor zones add no walls.
  _planGeometry(floor) {
    const rooms = (this._getEditingPlan()[floor] || {}).rooms || [];
    const walls = [];
    for (const r of rooms) {
      if (r.type === "stairs" || r.type === "door" || r.type === "outdoor") continue;
      const corners = this._zonePoints(r);
      corners.forEach((a, k) => {
        const b = corners[(k + 1) % corners.length];
        walls.push({ x0: a[0], y0: a[1], x1: b[0], y1: b[1] });
      });
    }
    // Outside openings sit on the whole floor's outline, inside ones on their room.
    const outline = rooms.length ? this._fpBounds(rooms) : null;
    const span = (lo, hi, along, half) => { const mid = lo + along * (hi - lo); return [mid - half, mid + half]; };
    const gaps = [];
    for (const e of this._elemsFor(floor) || []) {
      if (e.type !== "door" && e.type !== "window") continue;
      let box = outline;
      if ((e.kind === "interior" || e.kind === "cased") && e.room) {
        const own = rooms.find(r => r.name === e.room);
        if (own) box = this._fpRoomBox(own);
      }
      if (!box) continue;
      const half = (e.w || 20) / 2, along = e.pos != null ? e.pos : 0.5;
      if (e.wall === "front" || e.wall === "back") {
        const [from, to] = span(box.x0, box.x1, along, half), y = e.wall === "front" ? box.y0 : box.y1;
        gaps.push({ x0: from, y0: y, x1: to, y1: y });
      } else {
        const [from, to] = span(box.y0, box.y1, along, half), x = e.wall === "left" ? box.x0 : box.x1;
        gaps.push({ x0: x, y0: from, x1: x, y1: to });
      }
    }
    return { walls, gaps };
  }

  // True when (x, y) lies in a door or window gap, give or take 2.5 units.
  _inGap(x, y, gaps) {
    const within = (v, a, b) => v >= Math.min(a, b) - 2.5 && v <= Math.max(a, b) + 2.5;
    return gaps.some(g => within(x, g.x0, g.x1) && within(y, g.y0, g.y1));
  }

  // True when no solid wall stands between the two points.
  _losClear(x1, y1, x2, y2, geo) {
    return geo.walls.every(w => {
      const hit = this._segIntersect(x1, y1, x2, y2, w.x0, w.y0, w.x1, w.y1);
      return !hit || this._inGap(hit.x, hit.y, geo.gaps);
    });
  }

  // True when the point is in range, inside the cone and in plain sight.
  _pointCovered(px, py, cx, cy, ang, half, rng, geo) {
    const offX = px - cx, offY = py - cy;
    if (Math.hypot(offX, offY) > rng) return false;
    const bearing = Math.atan2(offY, offX) * 180 / Math.PI;
    if (Math.abs(((bearing - ang + 540) % 360) - 180) > half) return false;
    return this._losClear(cx, cy, px, py, geo);
  }

  // A camera's aim with the editor's defaults: facing up the plan, 90° wide,
  // reaching 55 units and never less than 5.
  _camAim(cam) {
    const or = (v, fallback) => (v != null ? v : fallback);
    return { x: cam.x, y: cam.y, ang: or(cam.angle, 270), fov: or(cam.fov, 90), rng: Math.max(or(cam.range, 55), 5) };
  }

  // Calls visit(x, y) at the centre of each cell of an n by n grid over a box.
  _fpGrid(x0, y0, w, h, n, visit) {
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) visit(x0 + (i + 0.5) / n * w, y0 + (j + 0.5) / n * h);
    }
  }

  // The share of each room one camera sees, to two places: a 4 by 4 sample
  // for a plain room, 6 by 6 (inside the outline only) for a shaped room or
  // zone. Rooms it cannot see at all are left out.
  _computeCoverage(floor, cam, geo) {
    const rooms = (this._getEditingPlan()[floor] || {}).rooms || [];
    if (!rooms.length) return {};
    const sight = geo || this._planGeometry(floor), aim = this._camAim(cam), half = aim.fov / 2;
    const share = {};
    for (const r of rooms) {
      if (r.type === "door" || r.type === "stairs") continue;
      let seen = 0, tried = 0;
      const tally = (px, py) => {
        tried++;
        if (this._pointCovered(px, py, aim.x, aim.y, aim.ang, half, aim.rng, sight)) seen++;
      };
      if (r.type === "outdoor" || (r.points && r.points.length >= 3)) {
        const outline = this._zonePoints(r), box = this._fpPointBounds(outline);
        this._fpGrid(box.x0, box.y0, box.x1 - box.x0, box.y1 - box.y0, 6, (px, py) => {
          if (this._pointInPoly(px, py, outline)) tally(px, py);
        });
      } else {
        this._fpGrid(r.x, r.y, r.w, r.h, 4, tally);
      }
      if (seen > 0 && tried > 0) share[r.name] = Math.round(seen / tried * 100) / 100;
    }
    return share;
  }

  // Even-odd test: a ray from the point crosses the outline an odd number of times.
  _pointInPoly(x, y, pts) {
    let inside = false;
    pts.forEach((p, i) => {
      const q = pts[(i || pts.length) - 1];
      if ((p[1] > y) !== (q[1] > y) && x < (q[0] - p[0]) * (y - p[1]) / (q[1] - p[1]) + p[0]) inside = !inside;
    });
    return inside;
  }

  // The full cone as an SVG path: two edges and the arc at full range.
  _coneD(cam) {
    const aim = this._camAim(cam);
    const rim = deg => {
      const rad = deg * Math.PI / 180;
      return `${(aim.x + aim.rng * Math.cos(rad)).toFixed(1)} ${(aim.y + aim.rng * Math.sin(rad)).toFixed(1)}`;
    };
    const wide = aim.fov > 180 ? 1 : 0;
    return `M ${aim.x} ${aim.y} L ${rim(aim.ang - aim.fov / 2)} A ${aim.rng} ${aim.rng} 0 ${wide} 1 ${rim(aim.ang + aim.fov / 2)} Z`;
  }

  // How far a ray from (cx, cy) at angle ang (radians) gets before a solid wall.
  _rayCast(cx, cy, ang, range, geo) {
    const endX = cx + range * Math.cos(ang), endY = cy + range * Math.sin(ang);
    let nearest = range;
    for (const w of geo.walls) {
      const hit = this._segIntersect(cx, cy, endX, endY, w.x0, w.y0, w.x1, w.y1);
      if (!hit || this._inGap(hit.x, hit.y, geo.gaps)) continue;
      const dist = Math.hypot(hit.x - cx, hit.y - cy);
      if (dist < nearest) nearest = dist;
    }
    return nearest;
  }

  // The cone cut short by walls: one ray at least every 3 degrees, 24 at least.
  _clippedCone(cam, geo) {
    if (!geo || !geo.walls || !geo.walls.length) return this._coneD(cam);
    const aim = this._camAim(cam);
    const rays = Math.max(24, Math.round(aim.fov / 3));
    const start = (aim.ang - aim.fov / 2) * Math.PI / 180, step = (aim.fov * Math.PI / 180) / rays;
    const path = [`M ${aim.x} ${aim.y}`];
    for (let k = 0; k <= rays; k++) {
      const a = start + k * step, reach = this._rayCast(aim.x, aim.y, a, aim.rng, geo);
      path.push(`L ${(aim.x + reach * Math.cos(a)).toFixed(1)} ${(aim.y + reach * Math.sin(a)).toFixed(1)}`);
    }
    return path.join(" ") + " Z";
  }

  // The room a point is in, or else the room whose centre is nearest.
  _roomAt(floor, x, y) {
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    const holder = rooms.find(r => r.type !== "stairs" && r.type !== "door"
      && x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h);
    if (holder) return holder.name;
    let nearest = null, best = 1e18;
    for (const r of rooms) {
      const dx = r.x + r.w / 2 - x, dy = r.y + r.h / 2 - y, d2 = dx * dx + dy * dy;
      if (d2 < best) { best = d2; nearest = r.name; }
    }
    return nearest || "the area";
  }

  // Plain notes on inside openings and stairs, sent with a coverage request.
  _openingDescriptions(floor) {
    const lead = { cased: "cased opening at ", interior: "interior door at " };
    const notes = (this._elemsFor(floor) || [])
      .filter(e => e.type === "door" && (e.kind === "cased" || e.kind === "interior") && e.room)
      .map(e => lead[e.kind] + e.room);
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    if (rooms.some(r => r.type === "stairs")) notes.push("open staircase");
    return notes;
  }
