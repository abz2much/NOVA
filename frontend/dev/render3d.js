// Render the NOVA3D engine (frontend/src/nova3d.js) to an SVG file:
//   node frontend/dev/render3d.js <theta> <out.svg> <floor>
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'nova3d.js'), 'utf8');
const ctx = {};
vm.runInNewContext(src + '\nthis.NOVA3D = NOVA3D;', ctx);
const J = ctx.NOVA3D;
const theta = Number(process.argv[2] || 35);
const out = process.argv[3] || '/tmp/h3d.svg';
const floor = process.argv[4] || 'all';
const lit = { 'master bedroom':'dom', "bedroom 2":'on', 'garage':'on', 'living room':'on', 'kitchen':'on', 'dining room':'on', 'guest room':'on', 'bath':'on' };
const svg = J.renderSVG({ theta, lit, floor });
fs.writeFileSync(out, svg);
console.log('wrote', out, 'theta', theta, 'floor', floor);
