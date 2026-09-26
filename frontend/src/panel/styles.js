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
