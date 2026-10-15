  // ─── Hologram: the hero's WebGL bust ────────────────────────────────────
  // A head, neck and shoulders drawn as glowing contour lines of gold
  // points, with a soft bloom. It needs WebGL 2; without it _initCore()
  // falls back to the canvas 2D ember head in core-animation.js.
  //
  // States: idle, reasoning (thinking), asleep are driven by the panel
  // today. listening, speaking and presenting are ready for when Nova sends
  // the live voice state and data cards; until then nothing selects them.

  _holoTargets(state) {
    // glow, warm core, eyes open, lift (thinking sparks), scan, flow speed,
    // speech waveform, pose bias, breathing, bloom
    const T = {
      idle:       { glow: 0.85, warm: 0.45, open: 1,    lift: 0, scan: 0,   flow: 0.35, wave: 0, yaw: 0,   pitch: 0,     roll: 0,    breath: 1,   bloom: 1.0 },
      listening:  { glow: 1.0,  warm: 0.55, open: 1.12, lift: 0, scan: 0.7, flow: 0.55, wave: 0, yaw: 0,   pitch: -0.04, roll: 0.07, breath: 0.7, bloom: 1.15 },
      reasoning:  { glow: 1.1,  warm: 1.0,  open: 1,    lift: 1, scan: 1.5, flow: 1.4,  wave: 0, yaw: 0,   pitch: 0.16,  roll: 0,    breath: 0.5, bloom: 1.35 },
      speaking:   { glow: 1.0,  warm: 0.75, open: 1,    lift: 0, scan: 0,   flow: 0.7,  wave: 1, yaw: 0,   pitch: 0,     roll: 0,    breath: 0.6, bloom: 1.2 },
      presenting: { glow: 0.9,  warm: 0.5,  open: 1,    lift: 0, scan: 0,   flow: 0.45, wave: 0, yaw: 0.3, pitch: 0.04,  roll: 0,    breath: 1,   bloom: 1.0 },
      asleep:     { glow: 0.38, warm: 0.15, open: 0,    lift: 0, scan: 0,   flow: 0.08, wave: 0, yaw: 0,   pitch: 0.3,   roll: 0.05, breath: 1.6, bloom: 0.75 },
    };
    const key = T[state] ? state : "idle";
    return { key, target: T[key], rate: { asleep: 0.5, listening: 3, speaking: 3, presenting: 2.2, reasoning: 2.6 }[key] || 2.4 };
  }

  // The particles, eight floats each: type, three shape values, seed, size,
  // contour line, flag. Types: 0 head, 1 neck, 2 shoulders, 3 eyes, 4 lips,
  // 5 drifting dust, 6 speech waveform.
  _holoParticles(narrow) {
    const out = [], rnd = Math.random, k = narrow ? 0.55 : 1;
    const add = (type, a, b, c, size, line, flag) => out.push(type, a, b, c, rnd(), size, line, flag);
    for (let i = 0; i < 160; i++) add(5, rnd(), rnd(), 0.3 + rnd(), 0.5 + rnd() * 1.5, 0, 0);
    const waveN = Math.round(520 * k);
    for (let i = 0; i < waveN; i++) add(6, i / waveN, 0, 0, 1.1, 0, 0);
    for (const side of [-1, 1]) {
      for (let i = 0; i < 170; i++) {
        const r = Math.sqrt(rnd()), a = rnd() * 2 * Math.PI;
        add(3, Math.cos(a) * r, Math.sin(a) * r, side, 0.8 + rnd() * 0.5, 0, 0);
      }
    }
    for (let i = 0; i < 300; i++) add(4, rnd(), (rnd() + rnd() + rnd()) / 1.5 - 1, 0, 0.8 + rnd() * 0.4, 0, 0);
    const headLines = 64, headPer = Math.round(270 * k);
    for (let j = 0; j < headLines; j++) {
      const ph = -1.35 + 2.75 * j / (headLines - 1);
      for (let i = 0; i < headPer; i++) {
        const th = -Math.PI + 2 * Math.PI * (i + rnd() * 0.5) / headPer;
        add(0, th, ph, 0, 1, j, (ph > 0.55 && rnd() < 0.14) ? 1 : 0);
      }
    }
    for (let i = 0, n = Math.round(4500 * k); i < n; i++) {
      let th = (rnd() * 2 - 1) * Math.PI;
      if (rnd() < 0.6) th *= 0.45;
      add(0, th, Math.asin(rnd() * 2 - 1) * 0.985, 0, 0.7, -1, 0);
    }
    const neckPer = Math.round(120 * k);
    for (let j = 0; j < 16; j++) for (let i = 0; i < neckPer; i++) add(1, 2 * Math.PI * (i + rnd() * 0.5) / neckPer, j / 15, 0, 1, j, 0);
    const bodyLines = 46, bodyPer = Math.round(380 * k);
    for (let j = 0; j < bodyLines; j++) for (let i = 0; i < bodyPer; i++) add(2, 2 * Math.PI * (i + rnd() * 0.5) / bodyPer, j / (bodyLines - 1), 0, 1, j, 0);
    for (let i = 0, n = Math.round(3500 * k); i < n; i++) add(2, rnd() * 2 * Math.PI, rnd(), 0, 0.7, -1, 0);
    return { data: new Float32Array(out), count: out.length / 8 };
  }

  _holoShaders() {
    const points = `#version 300 es
    precision highp float;
    in vec4 aA; in vec4 aB;
    uniform vec2 uRes, uC; uniform float uS, uDpr, uT, uFlow, uGlow, uWarm, uOpen, uMouth, uLift, uWave, uVoice, uBreathY, uScanY, uScanK, uSizeK, uDensity;
    uniform vec3 uHead, uNeck, uBody, cA, cB, cC, cW;
    out vec4 vCol; out float vSoft;
    const vec3 L = normalize(vec3(-0.55, 0.55, 0.62));
    float bump(float v, float m, float s) { float d = v - m; return exp(-d * d / (2. * s * s)); }
    float lipLine(float x) { float q = x / 0.17; return -0.46 + 0.007 * min(1., q * q); }
    // how far the face stands out from a plain egg: nose, eye sockets, brows, cheeks, lips, chin, jaw
    float relief(float x, float y) {
      float ax = abs(x), lip = lipLine(x), mo = uMouth;
      return 0.27 * bump(x, 0., 0.055) * bump(y, -0.1, 0.14) + 0.07 * bump(x, 0., 0.08) * bump(y, -0.24, 0.055)
        - 0.14 * bump(ax, 0.29, 0.1) * bump(y, 0.1, 0.075) + 0.11 * bump(y, 0.25, 0.05) * bump(ax, 0.28, 0.2)
        + 0.05 * bump(ax, 0.38, 0.12) * bump(y, -0.12, 0.1) + 0.045 * bump(y, lip + 0.035, 0.03) * bump(x, 0., 0.14)
        + 0.05 * bump(y, lip - 0.045 - mo * 0.08, 0.035) * bump(x, 0., 0.13)
        - 0.1 * mo * bump(y, lip - mo * 0.04, 0.012 + mo * 0.03) * bump(x, 0., 0.11)
        + 0.07 * bump(y, -0.8, 0.11) * bump(x, 0., 0.24) + 0.04 * bump(ax, 0.5, 0.09) * bump(y, -0.55, 0.13);
    }
    vec3 headAt(float th, float ph) {
      float cp = cos(ph), dx = sin(th) * cp, dy = sin(ph), dz = cos(th) * cp;
      float x = dx * 0.76, y = dy, z = dz * 0.86;
      if (y < 0.) { float k = pow(-y, 1.6); x *= 1. - 0.22 * k; z *= 1. - 0.1 * k; }
      if (z < 0.) z *= 1.1;
      if (y > 0.) x *= 1. + 0.05 * y;
      if (dz > 0.) z += pow(dz, 1.2) * relief(x, y);
      return vec3(x, y, z);
    }
    float frontZ(float x, float y) {
      float k = y < 0. ? pow(-y, 1.6) : 0.; float x0 = x / (1. - 0.22 * k);
      float z0 = 0.86 * sqrt(max(0., 1. - (x0 / 0.76) * (x0 / 0.76) - y * y)) * (1. - 0.1 * k);
      return z0 + pow(max(z0, 1e-4) / 0.86, 1.2) * relief(x, y);
    }
    vec3 turn(vec3 p, vec3 pose) {
      float cy = cos(pose.x), sy = sin(pose.x), cp = cos(pose.y), sp = sin(pose.y), cr = cos(pose.z), sr = sin(pose.z);
      float x = p.x * cy + p.z * sy, z = -p.x * sy + p.z * cy;
      float y2 = p.y * cp - z * sp, z2 = p.y * sp + z * cp;
      return vec3(x * cr - y2 * sr, x * sr + y2 * cr, z2);
    }
    vec3 turnAbout(vec3 p, vec3 pose, vec3 pivot) { return turn(p - pivot, pose) + pivot; }
    void main() {
      int type = int(aA.x + 0.5); float seed = aB.x, size = aB.y, soft = 0., al = 0.; vec3 col = cA; vec2 px;
      float twinkle = 0.8 + 0.2 * sin(uT * 2.3 + seed * 6.283);
      if (type == 5) {
        px = vec2(fract(aA.y + uFlow * aA.w * 0.03) * uRes.x, aA.z * uRes.y + sin(uT * 0.5 + seed * 6.283) * 8.);
        al = (0.08 + 0.25 * uGlow) * (0.6 + 0.4 * sin(uT * 1.7 + seed * 6.283)) * 0.7; col = mix(cA, cC, 0.4); soft = 0.6; size *= 1.2;
      } else {
        vec3 r, rn = vec3(0., 0., 1.), p = vec3(0.); bool body = false;
        float warmK = 0., fade = 1., feature = 0., line = aB.z;
        if (type == 0) {
          float th = aA.y, ph = aA.z, e = 0.004;
          p = headAt(th, ph);
          vec3 n = normalize(cross(headAt(th + e, ph) - p, headAt(th, ph + e) - p));
          r = turnAbout(p, uHead, vec3(0., -0.65, 0.)); rn = turn(n, uHead); body = true;
          // light the features: where the surface bends away from the plain egg
          if (cos(th) > 0.) feature = clamp((1. - dot(n, normalize(vec3(p.x / 0.58, p.y, p.z / 0.74)))) * 9., 0., 1.);
          warmK = exp(-dot(p - vec3(0., -0.12, 0.72), p - vec3(0., -0.12, 0.72)) * 14.) * 0.6;
          if (aB.w > 0.5 && uLift > 0.01) {
            float k = fract(uT * (0.14 + 0.2 * fract(seed * 3.7)) + seed);
            r.y += k * 1.6 * uLift; r.x += sin(k * 5. + seed * 20.) * 0.18 * k * uLift; fade = mix(1., (1. - k) * 1.4, uLift);
          }
        } else if (type == 1) {
          float u = aA.y, v = aA.z;
          p = vec3(0.33 * cos(u), -0.62 - v * 0.55, -0.06 + 0.3 * sin(u));
          r = turnAbout(p, uNeck, vec3(0., -1., 0.)); rn = turn(normalize(vec3(cos(u), 0., sin(u))), uNeck); body = true;
          warmK = exp(-pow(p.y + 0.95, 2.) * 20.) * 0.35 * step(0., sin(u));
        } else if (type == 2) {
          float u = aA.y, v = aA.z, cu = cos(u), su = sin(u);
          float w = 0.32 + 1.0 * smoothstep(0.0, 0.6, v) * (1. - 0.05 * v), d = 0.27 + 0.22 * smoothstep(0.0, 0.5, v);
          float x = w * sign(cu) * pow(abs(cu), 0.8), z = -0.1 + d * sign(su) * pow(abs(su), 0.8);
          z += 0.05 * bump(abs(x), 0.35, 0.18) * bump(v, 0.55, 0.15) * step(0., su);
          float drop = 0.32 * pow(clamp(abs(x) / 1.3, 0., 1.), 1.6) * (1. - smoothstep(0.35, 0.85, v));
          p = vec3(x * (1. + uBreathY * 0.4), -1.08 - v * 1.25 - drop, z);
          float ny = 0.9 * (1. - smoothstep(0.05, 0.45, v)) * abs(x) / w;
          vec3 n = normalize(vec3(x / (w * w), ny, (z + 0.1) / (d * d)));
          r = turnAbout(p, uBody, vec3(0., -1.4, 0.)); rn = turn(n, uBody); body = true;
          fade = 1. - smoothstep(0.55, 1., v);
          warmK = exp(-dot(p - vec3(0., -1.55, 0.3), p - vec3(0., -1.55, 0.3)) * 6.) * 0.45;
        } else if (type == 3) {
          float side = aA.w, u = aA.y, v = aA.z, op = uOpen;
          float rx = 0.062, ry = rx * (0.08 + 0.92 * min(op, 1.) * 0.75) * (0.9 + 0.2 * max(0., op - 1.) * 5.);
          float x = side * 0.285 + u * rx, y = 0.1 + v * ry - (1. - min(op, 1.)) * (1. - u * u) * 0.2 * rx;
          p = vec3(x, y, frontZ(x, y) + 0.03);
          r = turnAbout(p, uHead, vec3(0., -0.65, 0.));
          float rr = length(vec2(u, v));
          col = mix(cA, cC, clamp(1. - rr, 0., 1.));
          al = (0.35 + 0.5 * uGlow) * (1. - 0.5 * rr) * 0.5 * mix(0.05, 1., min(op, 1.));
        } else if (type == 4) {
          float x = -0.15 + 0.3 * aA.y, y = lipLine(x) - uMouth * 0.04 + aA.z * (0.005 + uMouth * 0.04);
          p = vec3(x, y, frontZ(x, y) + 0.02);
          r = turnAbout(p, uHead, vec3(0., -0.65, 0.));
          col = mix(cC, cA, 0.5);
          al = (0.45 + uMouth * 0.9) * 0.4 * uGlow * smoothstep(0., 0.15, aA.y) * smoothstep(1., 0.85, aA.y);
        } else {
          float s = aA.y, env = sin(s * 3.14159);
          float y = -2.12 + uWave * env * (0.13 * uVoice * sin(s * 34. + uT * 11.) + 0.05 * sin(s * 13. - uT * 6.));
          r = vec3(-1.75 + 3.5 * s, y, 0.55);
          col = mix(cA, cW, 0.35 + 0.4 * uVoice * env); al = uWave * 0.5 * env; size *= 1. + uVoice * env * 0.6;
        }
        if (body) {
          float facing = clamp((r.z + 0.9) / 1.8, 0., 1.), diff = max(0., dot(rn, L));
          float rim = pow(1. - max(0., rn.z), 2.5) * smoothstep(-0.3, 0.2, r.z);
          float bands = 0.65 + 0.35 * sin(p.y * 24. - uFlow * 3.2 + line * 0.15);
          float scan = exp(-pow(p.y - uScanY, 2.) * 35.) * uScanK;
          al = (0.07 + 0.62 * facing * diff * diff + 0.8 * rim + 0.9 * feature * facing) * bands * uGlow + scan * 0.55 * facing;
          if (line < -0.5) al *= 0.55;
          if (r.z < -0.12) al *= 0.28;
          al *= uDensity * twinkle * fade;
          col = mix(mix(cB, cA, 0.35 + 0.65 * clamp(diff * 0.6 + rim, 0., 1.)), cC, clamp(rim * 0.45 + scan * 0.6, 0., 1.));
          float wk = clamp(warmK * uWarm, 0., 0.95);
          col = mix(col, cW, wk); al += wk * 0.85 * uDensity * fade;
          float blur = clamp(0.05 - r.z, 0., 1.); soft = blur; size *= 1. + blur * 1.3; al *= 1. - 0.55 * blur;
        }
        float persp = 5. / (5. - r.z);
        px = uC + vec2(r.x, -(r.y + uBreathY)) * persp * uS;
        size *= persp;
      }
      vCol = vec4(col, al); vSoft = soft;
      gl_Position = vec4(px.x / uRes.x * 2. - 1., 1. - px.y / uRes.y * 2., 0., 1.);
      gl_PointSize = max(1., size * uSizeK * 2.2 * uDpr);
    }`;
    const dot = `#version 300 es
    precision mediump float; in vec4 vCol; in float vSoft; out vec4 o;
    void main() {
      vec2 d = gl_PointCoord * 2. - 1.; float r2 = dot(d, d); if (r2 > 1.) discard;
      float a = exp(-r2 * mix(5., 2.2, vSoft)) * vCol.a; o = vec4(vCol.rgb * a, a);
    }`;
    const quad = `#version 300 es
    in vec2 p; out vec2 uv; void main() { uv = p * 0.5 + 0.5; gl_Position = vec4(p, 0., 1.); }`;
    const blur = `#version 300 es
    precision mediump float; in vec2 uv; out vec4 o; uniform sampler2D t; uniform vec2 dir;
    void main() {
      vec3 c = texture(t, uv).rgb * 0.227027;
      c += (texture(t, uv + dir).rgb + texture(t, uv - dir).rgb) * 0.1945946;
      c += (texture(t, uv + dir * 2.).rgb + texture(t, uv - dir * 2.).rgb) * 0.1216216;
      c += (texture(t, uv + dir * 3.).rgb + texture(t, uv - dir * 3.).rgb) * 0.054054;
      c += (texture(t, uv + dir * 4.).rgb + texture(t, uv - dir * 4.).rgb) * 0.016216;
      o = vec4(c, 1.);
    }`;
    // The canvas is transparent, so the hero's own background
    // shows through; the light is written premultiplied.
    const compose = `#version 300 es
    precision highp float; in vec2 uv; out vec4 o;
    uniform sampler2D scene, bloom; uniform vec2 uRes, uC, uF; uniform float uDpr, uR, uGlow, uWarm, uBloomK, uS; uniform vec3 cA, cW;
    void main() {
      vec2 css = vec2(gl_FragCoord.x, uRes.y * uDpr - gl_FragCoord.y) / uDpr;
      vec3 c = cA * 0.10 * uGlow * pow(max(0., 1. - distance(css, uC) / uR), 1.8);
      c += cW * 0.08 * uWarm * pow(max(0., 1. - distance(css, uF) / (uS * 0.6)), 2.);
      c = min(c + texture(scene, uv).rgb + texture(bloom, uv).rgb * uBloomK, vec3(1.));
      o = vec4(c, max(c.r, max(c.g, c.b)));
    }`;
    return { points, dot, quad, blur, compose };
  }

  _holoProgram(gl, vs, fs) {
    const shader = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || "shader");
      return s;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, shader(gl.VERTEX_SHADER, vs));
    gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) || "link");
    const u = {};
    for (let i = 0, n = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS); i < n; i++) {
      const name = gl.getActiveUniform(prog, i).name;
      u[name] = gl.getUniformLocation(prog, name);
    }
    return { prog, u };
  }

  _holoTarget(gl, w, h) {
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
      [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
    const fb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    return { tex, fb, w, h };
  }

  // Compile and link everything on the hologram's context. Throws when the
  // browser cannot.
  _holoSetupGL(h) {
    const gl = h.canvas.getContext("webgl2", { antialias: false, alpha: true, premultipliedAlpha: true });
    if (!gl) throw new Error("WebGL 2 is not available");
    const src = this._holoShaders();
    h.gl = gl;
    h.points = this._holoProgram(gl, src.points, src.dot);
    h.blur = this._holoProgram(gl, src.quad, src.blur);
    h.compose = this._holoProgram(gl, src.quad, src.compose);
    h.quad = gl.createVertexArray();
    gl.bindVertexArray(h.quad);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    for (const p of [h.blur.prog, h.compose.prog]) {
      const loc = gl.getAttribLocation(p, "p");
      gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    }
    h.vao = null; h.built = null; h.fbo = {};
  }

  _holoUpload(h) {
    const gl = h.gl;
    h.built = this._holoParticles(h.narrow);
    if (h.vao) gl.deleteVertexArray(h.vao);
    h.vao = gl.createVertexArray();
    gl.bindVertexArray(h.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, h.built.data, gl.STATIC_DRAW);
    const la = gl.getAttribLocation(h.points.prog, "aA"), lb = gl.getAttribLocation(h.points.prog, "aB");
    gl.enableVertexAttribArray(la); gl.vertexAttribPointer(la, 4, gl.FLOAT, false, 32, 0);
    gl.enableVertexAttribArray(lb); gl.vertexAttribPointer(lb, 4, gl.FLOAT, false, 32, 16);
  }

  _holoResize() {
    const h = this._holo;
    if (!h || !h.gl || h.stopped) return;
    const rect = h.canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const wasNarrow = h.narrow;
    h.W = rect.width; h.H = rect.height;
    h.dpr = Math.min(window.devicePixelRatio || 1, 2);
    h.narrow = h.W < 640;
    h.canvas.width = Math.round(h.W * h.dpr); h.canvas.height = Math.round(h.H * h.dpr);
    if (!h.built || wasNarrow !== h.narrow) this._holoUpload(h);
    const gl = h.gl;
    for (const k of Object.keys(h.fbo)) { gl.deleteTexture(h.fbo[k].tex); gl.deleteFramebuffer(h.fbo[k].fb); }
    h.fbo.scene = this._holoTarget(gl, h.canvas.width, h.canvas.height);
    const bw = Math.max(1, h.canvas.width >> 1), bh = Math.max(1, h.canvas.height >> 1);
    h.fbo.a = this._holoTarget(gl, bw, bh); h.fbo.b = this._holoTarget(gl, bw, bh);
    if (this._reduceMotion) this._holoFrame(performance.now());
  }

  _holoLayout(h) {
    if (h.narrow) { const S = h.H * 0.25; return { S, hx: h.W / 2, hy: h.H * 0.33 }; }
    const S = h.H * 0.27;
    return { S, hx: Math.max(h.W * 0.27, S * 1.9), hy: h.H * 0.3 };
  }

  // Try to start the hologram on this canvas. False when WebGL 2 is missing
  // or fails, so the caller can fall back to the 2D head.
  _initHologram(canvas) {
    this._holoStop();
    const h = {
      canvas, narrow: false, W: 0, H: 0, dpr: 1, stopped: false, visible: true,
      cur: Object.assign({}, this._holoTargets(this._coreStateKey).target),
      face: { t: 0, blink: 1, blinkT: -1, nextBlink: 2, gx: 0, gy: 0, gazeT: 0, gTarget: [0, 0],
        yaw: 0, pitch: 0, roll: 0, voice: 0, mouth: 0, lookText: 0, dragYaw: 0, dragging: false, dragX: 0 },
      pointer: { inside: false, x: 0, y: 0 },
      flow: 0, scanY: 1.2, clock: 0, last: 0,
    };
    try { this._holoSetupGL(h); } catch (_) {
      // A canvas that gave a WebGL context can never give a 2D one, so the
      // fallback head gets a fresh copy of it.
      if (h.gl) canvas.replaceWith(canvas.cloneNode(false));
      return false;
    }
    this._holo = h;
    const fine = typeof window.matchMedia === "function" && window.matchMedia("(pointer: fine)").matches;
    h.fine = fine;
    canvas.addEventListener("pointermove", (e) => {
      const r = canvas.getBoundingClientRect(), f = h.face;
      h.pointer.x = e.clientX - r.left; h.pointer.y = e.clientY - r.top; h.pointer.inside = true;
      if (f.dragging) {
        f.dragYaw = Math.max(-1.2, Math.min(1.2, f.dragYaw + (e.clientX - f.dragX) * 0.012));
        f.dragX = e.clientX;
      }
    });
    canvas.addEventListener("pointerleave", () => { h.pointer.inside = false; });
    canvas.addEventListener("pointerdown", (e) => {
      h.face.dragging = true; h.face.dragX = e.clientX;
      try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* not all browsers */ }
    });
    const endDrag = () => { h.face.dragging = false; };
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    // A wall tablet can drop the graphics context; rebuild it when it comes back.
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      if (h.stopped || this._holo !== h) return;
      if (this._animHandle) { cancelAnimationFrame(this._animHandle); this._animHandle = null; }
    });
    canvas.addEventListener("webglcontextrestored", () => {
      if (h.stopped || this._holo !== h) return;
      try { this._holoSetupGL(h); } catch (_) { return; }
      this._holoResize();
      this._holoKick();
    });
    if (typeof ResizeObserver === "function") {
      h.resizeObs = new ResizeObserver(() => this._holoResize());
      h.resizeObs.observe(canvas);
    } else {
      h.onResize = () => this._holoResize();
      window.addEventListener("resize", h.onResize);
    }
    if (typeof IntersectionObserver === "function") {
      h.seenObs = new IntersectionObserver((entries) => {
        h.visible = entries[entries.length - 1].isIntersecting;
        this._holoKick();
      });
      h.seenObs.observe(canvas);
    }
    h.onVisibility = () => this._holoKick();
    document.addEventListener("visibilitychange", h.onVisibility);
    this._holoResize();
    if (this._reduceMotion) this._holoFrame(performance.now()); else this._holoKick();
    return true;
  }

  // Run the animation loop while the hologram is on screen and the tab is
  // visible; it stops by itself otherwise and this starts it again.
  _holoKick() {
    const h = this._holo;
    if (!h || h.stopped || this._reduceMotion || this._animHandle) return;
    if (!h.visible || document.hidden) return;
    h.last = 0;
    const loop = (now) => {
      this._animHandle = null;
      if (this._holo !== h || h.stopped) return;
      this._holoFrame(now);
      if (h.visible && !document.hidden) this._animHandle = requestAnimationFrame(loop);
    };
    this._animHandle = requestAnimationFrame(loop);
  }

  // Release everything the hologram holds: observers, listeners and the
  // graphics context (browsers allow only a few live ones).
  _holoStop() {
    const h = this._holo;
    if (!h) return;
    h.stopped = true;
    this._holo = null;
    if (this._animHandle) { cancelAnimationFrame(this._animHandle); this._animHandle = null; }
    if (h.resizeObs) h.resizeObs.disconnect();
    if (h.seenObs) h.seenObs.disconnect();
    if (h.onVisibility) document.removeEventListener("visibilitychange", h.onVisibility);
    if (h.onResize) window.removeEventListener("resize", h.onResize);
    try { h.gl?.getExtension("WEBGL_lose_context")?.loseContext(); } catch (_) { /* already gone */ }
  }

  _holoSetState(state) {
    const h = this._holo;
    if (!h) return;
    if (this._reduceMotion) {
      Object.assign(h.cur, this._holoTargets(state).target);
      this._holoFrame(performance.now());
    }
  }

  _holoUpdateFace(h, dt, key) {
    const f = h.face, cur = h.cur, lay = this._holoLayout(h);
    f.t += dt;
    if (f.blinkT < 0 && f.t > f.nextBlink) f.blinkT = 0;
    if (f.blinkT >= 0) {
      f.blinkT += dt;
      const p = f.blinkT / 0.16;
      f.blink = p < 1 ? Math.abs(1 - 2 * p) : 1;
      if (p >= 1) { f.blinkT = -1; f.nextBlink = f.t + 2.8 + Math.random() * 3.5; }
    }
    f.lookText = Math.max(0, f.lookText - dt);
    let tx = 0, ty = 0;
    if (key === "asleep" || key === "presenting") { tx = 0; ty = 0; }
    else if (f.lookText > 0 && !h.narrow) { tx = 0.95; ty = 0.05; }               // glance at the new word
    else if (h.pointer.inside && h.fine && !f.dragging) {
      tx = Math.max(-1, Math.min(1, (h.pointer.x - lay.hx) / (h.W * 0.4)));
      ty = Math.max(-0.8, Math.min(0.8, (h.pointer.y - lay.hy) / (h.H * 0.6)));
    } else if (key === "reasoning") { tx = Math.sin(f.t * 0.5) * 0.35; }
    else if (key === "idle") {
      if (f.t > f.gazeT) { f.gTarget = [(Math.random() * 2 - 1) * 0.6, (Math.random() * 2 - 1) * 0.25]; f.gazeT = f.t + 2.2 + Math.random() * 3; }
      [tx, ty] = f.gTarget;
    }
    const k = Math.min(1, dt * 2.5);
    f.gx += (tx - f.gx) * k; f.gy += (ty - f.gy) * k;
    if (!f.dragging) f.dragYaw *= Math.pow(0.08, dt);
    f.yaw = cur.yaw + f.gx * 0.5 + Math.sin(f.t * 0.3) * 0.05 * (1 - cur.lift) + f.dragYaw;
    f.pitch = cur.pitch + f.gy * 0.25 + Math.sin(f.t * 0.7) * 0.01;
    f.roll = cur.roll + Math.sin(f.t * 0.23) * 0.015;
    // A made up voice envelope moves the mouth while speaking, until real
    // audio levels are wired in.
    const v = Math.max(0, Math.sin(f.t * 8.7) * Math.sin(f.t * 3.3 + 1) * 0.8 + 0.25 * Math.sin(f.t * 17) + 0.1);
    const speaking = this._holoTargets(key).target.wave;
    f.voice += (v * speaking - f.voice) * Math.min(1, dt * 14);
    f.mouth += ((speaking ? Math.min(0.9, v * 0.9) : 0) - f.mouth) * Math.min(1, dt * 16);
  }

  _holoFrame(now) {
    const h = this._holo;
    if (!h || h.stopped || !h.gl || !h.W || !h.fbo.scene) return;
    const gl = h.gl, reduce = this._reduceMotion, f = h.face, cur = h.cur;
    const dt = reduce ? 0 : Math.min(0.05, (now - (h.last || now)) / 1000);
    h.last = now; h.clock += dt;
    const { key, target, rate } = this._holoTargets(this._coreStateKey);
    if (h.key !== key) { if (h.key) f.lookText = 1.6; h.key = key; }
    const kk = reduce ? 1 : Math.min(1, dt * rate);
    for (const name of Object.keys(target)) cur[name] += (target[name] - cur[name]) * kk;
    if (reduce) { f.yaw = cur.yaw + f.dragYaw; f.pitch = cur.pitch; f.roll = cur.roll; f.blink = 1; }
    else this._holoUpdateFace(h, dt, key);
    h.flow += dt * cur.flow;
    h.scanY -= dt * cur.scan * 0.9; if (h.scanY < -2.4) h.scanY = 1.2;
    const breathY = reduce ? 0 : Math.sin(h.clock * 2 * Math.PI / (key === "asleep" ? 7 : 5)) * 0.018 * cur.breath;
    const lay = this._holoLayout(h), t = reduce ? 0 : h.clock;
    const gold = [0.957, 0.722, 0.376], deep = [0.62, 0.27, 0.12], pale = [1.0, 0.89, 0.68], ember = [0.886, 0.33, 0.18];

    gl.bindFramebuffer(gl.FRAMEBUFFER, h.fbo.scene.fb); gl.viewport(0, 0, h.fbo.scene.w, h.fbo.scene.h);
    gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE);
    const P = h.points.u;
    gl.useProgram(h.points.prog); gl.bindVertexArray(h.vao);
    gl.uniform2f(P.uRes, h.W, h.H); gl.uniform2f(P.uC, lay.hx, lay.hy); gl.uniform1f(P.uS, lay.S); gl.uniform1f(P.uDpr, h.dpr);
    gl.uniform1f(P.uT, t); gl.uniform1f(P.uFlow, h.flow); gl.uniform1f(P.uGlow, cur.glow);
    gl.uniform1f(P.uWarm, cur.warm * (1 + f.voice * 0.6)); gl.uniform1f(P.uOpen, Math.max(0, cur.open * f.blink));
    gl.uniform1f(P.uMouth, f.mouth); gl.uniform1f(P.uLift, cur.lift); gl.uniform1f(P.uWave, cur.wave); gl.uniform1f(P.uVoice, f.voice);
    gl.uniform1f(P.uBreathY, breathY); gl.uniform1f(P.uScanY, h.scanY); gl.uniform1f(P.uScanK, Math.min(1, cur.scan));
    gl.uniform1f(P.uSizeK, Math.max(0.75, lay.S / 125)); gl.uniform1f(P.uDensity, h.narrow ? 0.6 : 0.42);
    gl.uniform3f(P.uHead, f.yaw, f.pitch, f.roll);
    gl.uniform3f(P.uNeck, f.yaw * 0.55, f.pitch * 0.4, f.roll * 0.5);
    gl.uniform3f(P.uBody, f.yaw * 0.25, 0, f.roll * 0.2);
    gl.uniform3f(P.cA, ...gold); gl.uniform3f(P.cB, ...deep); gl.uniform3f(P.cC, ...pale); gl.uniform3f(P.cW, ...ember);
    gl.drawArrays(gl.POINTS, 0, h.built.count);
    gl.disable(gl.BLEND);

    // bloom: two blurs of the scene at half size
    gl.useProgram(h.blur.prog); gl.bindVertexArray(h.quad); gl.activeTexture(gl.TEXTURE0); gl.uniform1i(h.blur.u.t, 0);
    const pass = (from, to, dx, dy) => {
      gl.bindFramebuffer(gl.FRAMEBUFFER, to.fb); gl.viewport(0, 0, to.w, to.h);
      gl.bindTexture(gl.TEXTURE_2D, from.tex); gl.uniform2f(h.blur.u.dir, dx / to.w, dy / to.h);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    };
    pass(h.fbo.scene, h.fbo.a, 1, 0); pass(h.fbo.a, h.fbo.b, 0, 1); pass(h.fbo.b, h.fbo.a, 2.5, 0); pass(h.fbo.a, h.fbo.b, 0, 2.5);

    const C = h.compose.u;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.viewport(0, 0, h.canvas.width, h.canvas.height);
    gl.useProgram(h.compose.prog);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, h.fbo.scene.tex); gl.uniform1i(C.scene, 0);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, h.fbo.b.tex); gl.uniform1i(C.bloom, 1);
    gl.uniform2f(C.uRes, h.W, h.H); gl.uniform2f(C.uC, lay.hx, lay.hy + lay.S * 0.6);
    gl.uniform2f(C.uF, lay.hx + f.yaw * lay.S * 0.55, lay.hy + lay.S * 0.05); gl.uniform1f(C.uDpr, h.dpr);
    gl.uniform1f(C.uR, Math.max(h.W, h.H) * 0.6); gl.uniform1f(C.uS, lay.S); gl.uniform1f(C.uGlow, cur.glow); gl.uniform1f(C.uWarm, cur.warm);
    gl.uniform1f(C.uBloomK, cur.bloom * 1.5); gl.uniform3f(C.cA, ...gold); gl.uniform3f(C.cW, ...ember);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
