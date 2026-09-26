# Residence 3D model: developer tools

The NOVA3D engine behind the panel's **Residence** tab lives in
[`../src/nova3d.js`](../src/nova3d.js). That file is the only copy: it is
built into `custom_components/nova/frontend/nova-panel.js` by
`scripts/build_panel.py`, and the tools here load it directly.

It is a pure geometry axonometric projection rendered to SVG, with no build
step and no CDN. API: `build(opts)`, `renderSVG(opts)`, `fixedBox(opts)`, where
`opts = { theta, floor: 'all'|'1f'|'2f'|'b', lit: { 'room name': 'on'|'dom' }, box }`.

## Files
- **`nova_house3d.html`**: standalone interactive viewer (drag to rotate, floor
  selector, view presets).
- **`render3d.js`**: writes an SVG for a given angle and floor:
  `node frontend/dev/render3d.js <theta> <out.svg> <floor>`.

## Changing the model
1. Edit `frontend/src/nova3d.js` and check it in the viewer or with `render3d.js`.
2. Run `python scripts/build_panel.py`.
3. Run the usual gate: `node --check`, `scripts/audit.py`, `pytest tests/ -q`
   and `scripts/smoke_panel.js`. The smoke test pins a hash of the engine's
   SVG output, so an intended geometry change also updates that hash.

## Opening the viewer locally
Browsers may block `file://` script loads, so serve the `frontend` folder:

```
cd frontend
python3 -m http.server 8099   # then open http://localhost:8099/dev/nova_house3d.html
```
