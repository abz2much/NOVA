  _css() {
    return `
      :host{
        ${this._themeTokens()}
        --font-display:'Plus Jakarta Sans',system-ui,-apple-system,'Segoe UI',sans-serif;
        --font-body:'Plus Jakarta Sans',system-ui,-apple-system,'Segoe UI',sans-serif;
        --font-mono:'IBM Plex Mono',ui-monospace,'SF Mono',monospace;
        /* One size scale and three corner sizes (8.75.0); the smoke test holds every rule to them. */
        --t-cap:12px; --t-sm:14px; --t-body:16px; --t-title:20px; --t-h:24px; --t-fig:32px; --t-hero:48px;
        --r-ctl:8px; --r-tile:12px; --r-card:16px;
      }
      *{box-sizing:border-box}
      /* Keyboard focus is always visible; a pointer click shows no ring. */
      :focus-visible{outline:2px solid var(--gold);outline-offset:2px}
      button:disabled,select:disabled,input:disabled,textarea:disabled{opacity:.45;cursor:not-allowed}
      /* Targets: 24px everywhere (WCAG 2.2), 44px on a touch screen. A link
         inside a sentence keeps the sentence's height. */
      button{min-height:24px}
      .vp-link{min-height:0}
      .new-pl-chip > button,.new-mem-forget,.new-doclib-del,.fpn-ent-del{min-width:24px}
      @media (pointer:coarse){
        button,select,input,textarea,.nav-tab{min-height:44px}
        .new-pl-chip > button,.new-mem-forget,.new-doclib-del,.fpn-ent-del{min-width:44px}
        .vp-link{min-height:0}
      }
      .wrap{background:var(--bg);color:var(--ink);font-family:var(--font-body);
        padding:20px 16px 40px;min-height:100vh;
        background-image:radial-gradient(ellipse 900px 500px at 50% -8%, color-mix(in srgb,var(--glow-top) 50%,transparent) 0%, transparent 60%);}
      .topbar{display:flex;align-items:center;justify-content:space-between;gap:12px 24px;margin-bottom:22px;flex-wrap:wrap;max-width:1100px;margin-inline:auto}
      .brand{display:flex;align-items:center;gap:11px}
      .brand-mark{width:26px;height:26px;border-radius:50%;flex:none;
        background:radial-gradient(circle at 34% 30%, var(--gold-pale), var(--gold) 42%, var(--ember) 78%, var(--orb-deep) 100%);
        box-shadow:0 0 14px 1px color-mix(in srgb,var(--ember) 33%,transparent);}
      .brand-name{font-family:var(--font-display);font-size:var(--t-title);font-weight:600}
      .brand-tag{font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint);letter-spacing:.06em;text-transform:uppercase}
      .hero{max-width:1100px;margin:0 auto;background:linear-gradient(180deg,var(--surface),color-mix(in srgb,var(--hero-end) 87%,transparent));
        border:1px solid var(--line-soft);border-radius:var(--r-card);overflow:hidden;container-type:inline-size;
        display:flex;flex-direction:column;align-items:stretch}
      .hero-stage{position:relative;height:clamp(520px,54cqw,620px)}
      /* The orbit (8.76.0): arcs behind the head, five cards round it. */
      canvas.hero-arcs{position:absolute;inset:0;width:100%;height:100%;display:block;pointer-events:none}
      .orbit{position:absolute;inset:0;pointer-events:none}
      .orbit-tile{position:absolute;width:200px;pointer-events:auto;display:flex;flex-direction:column;align-items:flex-start;gap:2px;
        padding:14px 16px;border-radius:var(--r-tile);border:1px solid var(--line-soft);background:color-mix(in srgb,var(--surface) 82%,transparent);
        color:var(--ink);font-family:var(--font-body);text-align:left;cursor:pointer;transition:border-color .6s,box-shadow .6s;animation:orbit-drift 9s ease-in-out infinite}
      .orbit-tile:nth-child(even){animation-duration:11s;animation-delay:-3s}
      .orbit-tile[data-orbit="security"]{left:3%;top:6%}.orbit-tile[data-orbit="home"]{left:1%;top:37%}.orbit-tile[data-orbit="energy"]{left:4%;top:68%}
      .orbit-tile[data-orbit="climate"]{right:3%;top:6%}.orbit-tile[data-orbit="weather"]{right:1%;top:68%}
      .orbit-cap{font-size:var(--t-cap);font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-faint)}
      .orbit-v{font-size:var(--t-h);font-weight:700;line-height:1.15}
      .orbit-sub{font-size:var(--t-sm);color:var(--ink-dim);min-height:1.2em}
      .orbit-tile.orbit-flash{border-color:color-mix(in srgb,var(--gold) 60%,transparent);box-shadow:0 0 28px color-mix(in srgb,var(--gold) 25%,transparent)}
      @keyframes orbit-drift{50%{transform:translateY(-6px)}}
      .present-card.present-ghost{position:absolute;display:flex;right:auto;bottom:auto;pointer-events:none;z-index:3;transform-origin:center;
        transition:transform .6s ease-in,opacity .6s ease-in}
      @keyframes pc-in{from{opacity:0;transform:scale(.85) translateX(-30px)}}
      .hero-latest{display:flex;justify-content:center;align-items:baseline;gap:8px;font-size:var(--t-sm);color:var(--ink-dim);min-height:1.4em}
      .hero-latest b{font-weight:600;color:var(--ink)}
      .hero-latest .dot{width:8px;height:8px;border-radius:50%;background:var(--gold);flex:none;align-self:center}
      .hero-latest-in{animation:latest-in .5s ease-out}
      @keyframes latest-in{from{opacity:0;transform:translateY(12px)}}
      .present-card{position:absolute;right:3%;top:30%;width:270px;display:none;flex-direction:column;gap:8px;z-index:2;
        padding:14px 18px;border:1px solid var(--line-soft);border-radius:var(--r-card);background:var(--surface-2);cursor:pointer;text-align:left}
      .hero-stage.presenting .present-card{display:flex;animation:pc-in .45s cubic-bezier(.2,.8,.2,1)}
      .present-card .pc-fig{font-size:clamp(40px,4.6cqw,56px)}
      .pc-cap{font-family:var(--font-body);font-size:var(--t-sm);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-dim)}
      .pc-big{font-family:var(--font-display);font-weight:500;line-height:1}
      .pc-fig{font-size:clamp(44px,8cqw,84px);letter-spacing:-.02em;
        background:linear-gradient(100deg,var(--gold-pale),var(--gold) 55%,var(--ember));-webkit-background-clip:text;background-clip:text;color:transparent}
      .pc-fig::after{content:" " attr(data-unit);font-size:clamp(16px,2.4cqw,22px);-webkit-text-fill-color:var(--gold-pale)}
      .pc-now{font-family:var(--font-body);font-size:var(--t-sm);color:var(--ink-dim)}
      .pc-curve{width:100%;height:28px;display:block}
      .pc-rows{display:flex;flex-direction:column;gap:4px;border-top:1px solid var(--line-soft);padding-top:8px}
      .pc-row{display:flex;justify-content:space-between;gap:12px;font-family:var(--font-body);font-size:var(--t-sm);color:var(--ink-dim)}
      .pc-row b{font-weight:500;color:var(--gold-pale)}
      canvas.core{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab;touch-action:pan-y}
      canvas.core:active{cursor:grabbing}
      .hero-copy{position:absolute;left:50%;bottom:5%;width:min(48%,540px);transform:translateX(-50%);pointer-events:none;
        display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center}
      .hero-word{font-family:var(--font-display);font-weight:700;font-size:clamp(36px,5cqw,56px);line-height:1.05;letter-spacing:-.02em;min-height:1.05em;
        background:linear-gradient(100deg,var(--gold-pale),var(--gold) 55%,var(--ember));-webkit-background-clip:text;background-clip:text;color:transparent;transition:filter .6s}
      .hero-word.dim{filter:brightness(.62)}
      .hero-caret{display:inline-block;width:.06em;height:.8em;margin-left:.06em;background:var(--gold);vertical-align:-.04em;animation:hero-caret 1s steps(1) infinite}
      @keyframes hero-caret{50%{opacity:0}}
      .state-line{font-family:var(--font-display);font-size:clamp(17px,2cqw,21px);font-weight:500;margin:0;text-wrap:balance}
      .state-sub{font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint)}
      @container (max-width:819px){
        .hero-stage{height:auto;display:flex;flex-direction:column}
        canvas.hero-arcs{display:none}
        canvas.core{position:relative;height:260px}
        .present-card{position:relative;left:auto;right:auto;top:auto;width:auto;transform:none;margin:0 16px 14px;align-items:center;text-align:center}
        .pc-row{width:100%}
        .hero-copy{position:relative;left:auto;right:auto;bottom:auto;width:auto;transform:none;align-items:center;text-align:center;padding:0 16px 16px}
        .orbit{position:relative;inset:auto;display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:0 16px 16px;pointer-events:auto}
        .orbit-tile,.orbit-tile[data-orbit]{position:relative;left:auto;right:auto;top:auto;width:auto;animation:none}
      }
      @media (prefers-reduced-motion: reduce){.hero-caret,.orbit-tile,.hero-latest-in,.hero-stage.presenting .present-card{animation:none}}
      /* The dock (8.77.0): five buttons at the bottom, panels slide up over the hero */
      .dock-wrap{position:sticky;bottom:0;z-index:6;max-width:1100px;margin:16px auto 0;padding-bottom:8px}
      .dock{display:flex;gap:4px;padding:6px;background:var(--surface);
        border:1px solid var(--line-soft);border-radius:var(--r-card);box-shadow:0 -8px 32px color-mix(in srgb,var(--bg) 70%,transparent)}
      .dock-btn{flex:1;min-width:0;min-height:48px;position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
        padding:6px 2px;background:none;border:0;border-radius:var(--r-tile);color:var(--ink-dim);font:inherit;font-size:var(--t-cap);font-weight:600;cursor:pointer;
        transition:color .15s,background .15s}
      .dock-btn svg{width:24px;height:24px;flex:none}
      .dock-btn:hover{color:var(--ink)}
      .dock-btn:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
      .dock-btn[aria-expanded="true"]{color:var(--gold);background:color-mix(in srgb,var(--gold) 12%,transparent)}
      .dock-label{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .dock-badge{position:absolute;top:2px;left:calc(50% + 6px);min-width:18px;padding:0 5px;border-radius:999px;background:var(--bad);
        color:var(--on-accent);font-size:var(--t-cap);font-weight:700;line-height:18px;text-align:center}
      .dock-sheet{position:absolute;left:0;right:0;bottom:calc(100% + 8px);max-height:min(55vh,560px);overflow-y:auto;overscroll-behavior:contain;
        padding:12px 16px 16px;background:var(--surface);border:1px solid var(--line-soft);border-radius:var(--r-card);
        box-shadow:0 -12px 40px color-mix(in srgb,var(--bg) 80%,transparent);
        opacity:0;transform:translateY(24px);transition:transform .3s cubic-bezier(.2,.8,.2,1),opacity .2s ease}
      .dock-sheet.dock-open{opacity:1;transform:none}
      .dock-sheet:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
      .dock-sheet-head{display:flex;align-items:center;gap:12px;margin-bottom:12px}
      .dock-sheet-head .panel-title{margin-right:auto}
      .dock-sheet .panel{margin:12px 0 0;padding:0;border:0;background:none}
      .nova-row{display:flex;align-items:flex-start;gap:10px;padding:9px 0;border-bottom:1px solid var(--line-soft);font-size:var(--t-sm)}
      .nova-time{flex:none;min-width:44px;color:var(--ink-faint);font-size:var(--t-cap);padding-top:2px}
      .nova-dot{flex:none;width:8px;height:8px;border-radius:50%;margin-top:6px;background:var(--ink-faint)}
      .nova-dot-spoken,.nova-dot-pushed{background:var(--gold)}
      .nova-dot-verify,.nova-dot-held{background:var(--warn)}
      .nova-dot-goal,.nova-dot-followup{background:var(--good)}
      .nova-dot-passed{background:var(--line-soft)}
      .nova-quiet .nova-text{color:var(--ink-faint)}
      .ask-nova{display:flex;gap:8px;margin-top:12px}
      .ask-nova .cfg-field{flex:1;min-width:0}
      .ask-reply:not(:empty){margin-top:8px;padding:10px 12px;background:var(--surface-2);border-radius:var(--r-tile);font-size:var(--t-sm)}
      .ask-q{color:var(--ink-dim);margin-bottom:4px}
      .ask-reply .mode-chip{margin-top:8px}
      .nova-foot{margin-top:12px}
      .more-item{display:flex;align-items:center;justify-content:space-between;width:100%;min-height:48px;padding:12px 4px;background:none;border:0;
        border-bottom:1px solid var(--line-soft);color:var(--ink);font:inherit;font-size:var(--t-body);text-align:left;cursor:pointer}
      .more-item::after{content:"›";color:var(--ink-faint);font-size:var(--t-title)}
      .more-item:focus-visible{outline:2px solid var(--gold);outline-offset:-2px}
      @media (prefers-reduced-motion: reduce){.dock-sheet{transition:none}.dock-btn{transition:none}}
      .chips{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;padding:16px 20px 20px;border-top:1px solid var(--line-soft);width:100%}
      .chip{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:var(--r-card);background:var(--surface-2);
        font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-dim);border:1px solid var(--line-soft)}
      .chip .dot{width:6px;height:6px;border-radius:50%;background:var(--good)}
      .chip.warn .dot{background:var(--warn)}
      .chip b{color:var(--ink);font-weight:600}
      .grid{max-width:1100px;margin:16px auto 0;display:grid;grid-template-columns:1fr;gap:16px}
      .panel{background:var(--surface);border:1px solid var(--line-soft);border-radius:var(--r-card);padding:16px 16px 14px}
      .panel-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px}
      .panel-title{font-family:var(--font-display);font-size:var(--t-body);font-weight:600}
      .panel-meta{font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint)}
      .lockdown-control{margin-left:auto;order:2;font-family:var(--font-body);font-size:var(--t-sm);font-weight:600;
        padding:8px 12px;border-radius:var(--r-ctl);border:1px solid var(--line-soft);background:var(--surface);color:var(--ink-faint);cursor:pointer}
      .lockdown-control.active{color:var(--alarm-ink);background:var(--alarm-bg);border-color:var(--alarm-line);box-shadow:0 0 16px color-mix(in srgb,var(--alarm-line) 20%,transparent)}
      .onboarding-card{max-width:1100px;margin:16px auto 0;background:linear-gradient(135deg,color-mix(in srgb,var(--gold) 7%,transparent),var(--surface));border:1px solid color-mix(in srgb,var(--gold) 40%,transparent);border-radius:var(--r-card);padding:16px}
      .onboarding-progress{position:relative;height:20px;background:var(--surface-2);border-radius:var(--r-ctl);overflow:hidden;margin:12px 0}
      .onboarding-progress i{position:absolute;inset:0 auto 0 0;background:color-mix(in srgb,var(--gold) 20%,transparent)}.onboarding-progress span{position:relative;z-index:1;display:block;padding:4px 8px;font-family:var(--font-body);font-size:var(--t-cap)}
      .onboarding-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:7px;margin-bottom:10px}
      .onboarding-step{display:grid;grid-template-columns:18px 1fr auto;align-items:center;gap:7px;background:var(--surface-2);border:1px solid var(--line-soft);border-radius:var(--r-ctl);padding:8px;color:var(--ink-dim)}
      .onboarding-step.done{opacity:.62}.onboarding-step b{display:block;font-size:var(--t-cap)}.onboarding-step small{display:block;font-size:var(--t-cap);color:var(--ink-faint);margin-top:2px}
      .welcome-checks,.welcome-hello{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:var(--r-ctl);padding:8px;margin-bottom:10px;color:var(--ink-dim)}
      .welcome-checks > b{display:block;font-size:var(--t-cap)}.welcome-checks small,.welcome-hello small{display:block;font-size:var(--t-cap);color:var(--ink-faint);margin-top:2px}
      .welcome-hello{display:grid;grid-template-columns:auto 1fr;align-items:center;gap:10px}.welcome-hello .welcome-reply{color:var(--ink-dim);font-size:var(--t-cap)}.welcome-hello .welcome-error{color:var(--bad)}
      .dashboard-pair{max-width:1100px;margin:16px auto 0;display:grid;grid-template-columns:1fr 1fr;gap:16px}
      @media (max-width:760px){.dashboard-pair{grid-template-columns:1fr}}
      .metric-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
      .metric{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:var(--r-ctl);padding:9px;display:flex;flex-direction:column;gap:2px}
      .metric b{font-family:var(--font-body);font-size:var(--t-body)}.metric span{font-size:var(--t-cap);color:var(--ink-faint)}
      .energy-live{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:0}
      .energy-live[hidden]{display:none}
      @media (max-width:760px){.energy-live{grid-template-columns:repeat(2,1fr)}}
      .energy-tile{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:var(--r-ctl);padding:9px;display:flex;flex-direction:column;gap:2px;min-width:0}
      .energy-tile dt{font-size:var(--t-cap);color:var(--ink-faint)}
      .energy-tile dd{margin:0}
      .energy-tile .energy-tile-w{font-family:var(--font-body);font-size:var(--t-body)}
      .energy-tile .energy-tile-state{font-size:var(--t-cap);color:var(--ink-dim);min-height:1em}
      .energy-flow-wrap{margin:0 0 10px;display:flex;justify-content:center}
      .energy-flow{display:block;width:100%;height:auto;max-height:clamp(220px,38vh,320px);margin:0 auto}
      .energy-flow .flow[data-flow="solar"]{--flow-c:var(--c-solar)}
      .energy-flow .flow[data-flow="grid"]{--flow-c:var(--c-grid)}
      .energy-flow .flow[data-flow="battery"]{--flow-c:var(--c-battery)}
      .energy-flow .flow-glow{fill:none;stroke:var(--flow-c);stroke-width:calc(var(--flow-w,2) * 3);stroke-linecap:round;opacity:.12}
      .energy-flow .flow-line{fill:none;stroke:var(--flow-c);stroke-width:var(--flow-w,2);stroke-linecap:round;stroke-dasharray:1 12;
        animation:nova-flow var(--flow-dur,6s) linear infinite}
      .energy-flow .flow[data-dir="out"] .flow-line{animation-direction:reverse}
      .energy-flow .flow-arrow{fill:var(--flow-c)}
      .energy-flow .flow[data-dir="out"] .flow-arrow{transform:rotate(180deg)}
      .energy-flow .flow[data-state="idle"] .flow-line{animation:none;opacity:.25}
      .energy-flow .flow[data-state="idle"] .flow-glow{opacity:.05}
      .energy-flow .flow[data-state="idle"] .flow-arrow{visibility:hidden}
      @keyframes nova-flow{to{stroke-dashoffset:-26}}
      .energy-flow .node-ring{fill:var(--surface-2);stroke-width:2.5}
      .energy-flow .node-icon{fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
      .energy-flow .flow-node[data-node="solar"] .node-ring,.energy-flow .flow-node[data-node="solar"] .node-icon{stroke:var(--c-solar)}
      .energy-flow .flow-node[data-node="grid"] .node-ring,.energy-flow .flow-node[data-node="grid"] .node-icon{stroke:var(--c-grid)}
      .energy-flow .flow-node[data-node="house"] .node-ring,.energy-flow .flow-node[data-node="house"] .node-icon{stroke:var(--c-house)}
      .energy-flow .flow-node[data-node="battery"] .node-ring,.energy-flow .flow-node[data-node="battery"] .node-icon{stroke:var(--c-battery)}
      .energy-flow .flow-label{font-family:var(--font-body);font-size:var(--t-title);fill:var(--ink-dim)}
      .energy-flow .flow-value{font-family:var(--font-body);font-size:var(--t-h);fill:var(--ink)}
      .energy-flow .flow-state{font-family:var(--font-body);font-size:var(--t-title);fill:var(--ink-dim)}
      .energy-flow .battery-track{fill:none;stroke:var(--line-soft);stroke-width:4}
      .energy-flow .battery-arc{fill:none;stroke:var(--c-battery);stroke-width:4;stroke-linecap:round;
        stroke-dasharray:var(--batt-pct,0) 100;transition:stroke-dasharray .6s ease}
      .energy-flow .battery-arc[data-pct="none"]{opacity:0}
      .energy-flow-summary{margin:0 0 10px}
      @media (min-width:900px){
        .energy-live-grid{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;align-items:center}
        .energy-live-grid:has(> .energy-live-main > .energy-flow-wrap[hidden]){display:block}
        .energy-live-side .energy-live{grid-template-columns:repeat(2,1fr)}
        .energy-live-main .energy-flow-wrap{margin:0}
      }
      .energy-flow-summary[hidden],.energy-flow-wrap[hidden]{display:none}
      #energyTodayPanel[hidden],#energyBatteryPanel[hidden],.energy-today[hidden],.battery-line[hidden]{display:none}
      .energy-today{grid-template-columns:repeat(auto-fit,minmax(130px,1fr))}
      .energy-battery{display:flex;align-items:center;gap:18px}
      .battery-tank{width:72px;height:auto;flex:none}
      .battery-tank .tank-outline{fill:var(--surface-2);stroke:var(--c-battery);stroke-width:3}
      .battery-tank .tank-cap{fill:var(--c-battery)}
      .battery-tank .tank-fill{fill:var(--c-battery);opacity:.85;transform-box:fill-box;transform-origin:50% 100%;
        transform:scaleY(calc(var(--tank-pct,0) / 100));transition:transform .6s ease}
      .battery-copy{display:flex;flex-direction:column;gap:4px;min-width:0}
      .battery-pct{font-family:var(--font-body);font-size:var(--t-h);color:var(--ink)}
      .battery-state{font-size:var(--t-sm);color:var(--ink-dim)}
      .battery-line{font-size:var(--t-sm);color:var(--ink-dim)}
      #energyOutlookPanel[hidden],.outlook-strip-wrap[hidden],.outlook-advice[hidden],.outlook-line[hidden],.outlook-learned[hidden]{display:none}
      .outlook-key{display:flex;gap:14px;font-size:var(--t-cap);color:var(--ink-dim);margin-bottom:4px}
      .outlook-key span::before{content:"";display:inline-block;width:10px;height:3px;border-radius:var(--r-ctl);margin-right:5px;vertical-align:middle}
      .outlook-key .key-sun::before{background:var(--c-solar)}.outlook-key .key-soc::before{background:var(--c-battery)}.outlook-key .key-price::before{background:var(--c-grid);height:8px;opacity:.6}
      .outlook-chart{position:relative}
      .outlook-strip{display:block;width:100%;height:116px}
      .outlook-strip .outlook-band{fill:var(--c-grid);opacity:.18}
      .outlook-strip .outlook-band[data-label="mid"]{opacity:.4}
      .outlook-strip .outlook-band[data-label="high"]{opacity:.6}
      .outlook-strip .outlook-solar{fill:var(--c-solar);fill-opacity:.2;stroke:var(--c-solar);stroke-opacity:.7;stroke-width:1.5;vector-effect:non-scaling-stroke}
      .outlook-strip .outlook-soc{fill:none;stroke:var(--c-battery);stroke-width:2.5;stroke-linejoin:round;vector-effect:non-scaling-stroke}
      .outlook-strip .outlook-now{stroke:var(--ink-dim);stroke-width:1;stroke-dasharray:3 4;vector-effect:non-scaling-stroke}
      .outlook-band-labels{position:absolute;left:0;right:0;bottom:0;height:26%;pointer-events:none}
      .outlook-band-label{position:absolute;top:0;bottom:0;display:flex;align-items:center;justify-content:center;gap:4px;overflow:hidden;white-space:nowrap;font-size:var(--t-cap);color:var(--ink)}
      .outlook-band-label b{font-family:var(--font-body);font-weight:500;color:var(--ink)}
      .outlook-ticks{position:relative;height:16px;font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint)}
      .outlook-ticks span{position:absolute;top:2px;transform:translateX(-50%);white-space:nowrap}
      .outlook-ticks .tick-start{transform:none}
      .outlook-ticks .tick-now{top:auto;bottom:-14px;color:var(--ink-dim)}
      .outlook-rates{display:grid;grid-template-columns:1fr;gap:3px 16px;margin:20px 0 0;font-size:var(--t-cap)}
      .outlook-rate{display:flex;gap:8px;min-width:0}
      .outlook-rate dt{font-family:var(--font-body);color:var(--ink);white-space:nowrap}
      .outlook-rate dd{margin:0;color:var(--ink-dim);min-width:0}
      .outlook-rate .rate-swatch{display:inline-block;width:9px;height:9px;border-radius:var(--r-ctl);margin-right:6px;vertical-align:-1px;background:var(--c-grid);opacity:.18}
      .outlook-rate[data-label="mid"] .rate-swatch{opacity:.4}.outlook-rate[data-label="high"] .rate-swatch{opacity:.6}
      @media (min-width:600px){.outlook-rates{grid-template-columns:repeat(2,minmax(0,1fr))}}
      @media (max-width:599px){.outlook-band-labels{display:none}}
      .outlook-advice{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-direction:column;gap:8px}
      .outlook-item{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:var(--r-ctl);padding:9px 10px}
      .outlook-item-title{font-size:var(--t-sm);color:var(--ink);margin-bottom:2px}
      .outlook-saving{font-family:var(--font-body);font-size:var(--t-cap);color:var(--gold-pale);margin-top:3px}
      .outlook-line{font-size:var(--t-sm);color:var(--ink-dim);margin-top:10px}
      .outlook-learned{font-size:var(--t-cap);color:var(--ink-faint);margin-top:6px}
      @media (prefers-reduced-motion: reduce){.energy-flow .flow-line{animation:none}.energy-flow .battery-arc{transition:none}.battery-tank .tank-fill{transition:none}}
      .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
      .goal-list{display:flex;flex-direction:column;gap:7px;max-height:300px;overflow:auto}
      .goal-row{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--surface-2);border:1px solid var(--line-soft);border-radius:var(--r-ctl);padding:9px}
      .goal-copy{min-width:0;display:flex;flex-direction:column;gap:2px}.goal-copy b{font-size:var(--t-sm)}.goal-copy span{font-size:var(--t-sm);color:var(--ink-dim);overflow-wrap:anywhere}
      .goal-copy small{font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint)}
      .goal-create{display:flex;gap:8px;margin-top:10px}.goal-create .cfg-field{flex:1;min-width:0}.empty-state{font-size:var(--t-sm);color:var(--ink-faint);padding:12px 0}
      .feed{max-height:420px;overflow-y:auto}
      .feed::-webkit-scrollbar{width:3px}
      .feed::-webkit-scrollbar-track{background:var(--surface-2)}
      .feed::-webkit-scrollbar-thumb{background:var(--line-soft);border-radius:var(--r-ctl)}
      .feed-row{padding:9px 0;border-bottom:1px solid var(--line-soft);display:flex;justify-content:space-between;gap:10px}
      .feed-row:last-child{border-bottom:none}
      .feed-text{font-size:var(--t-sm);line-height:1.4}
      .feed-text .dim{color:var(--ink-dim)}
      .feed-time{font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint);white-space:nowrap}
      .areas-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:10px}
      @media (max-width:1100px){.areas-grid{grid-template-columns:repeat(4,1fr)}}
      @media (max-width:560px){.areas-grid{grid-template-columns:repeat(2,1fr)}}
      .area-tile{position:relative;background:var(--surface-2);border:1px solid var(--line-soft);border-radius:var(--r-tile);
        padding:12px 12px 10px;overflow:hidden;transition:border-color .25s,box-shadow .25s}
      .area-tile::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;
        background:linear-gradient(90deg,var(--info),var(--gold) 55%,var(--ember));opacity:.55}
      .area-tile.no-temp::before{display:none}
      .area-tile.active{border-color:color-mix(in srgb,var(--ember) 44%,transparent);box-shadow:inset 0 0 14px color-mix(in srgb,var(--ember) 8%,transparent),0 0 14px color-mix(in srgb,var(--ember) 7%,transparent)}
      .area-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
      .area-name{font-family:var(--font-display);font-size:var(--t-body);font-weight:600;display:flex;align-items:center;gap:6px}
      .live-dot{width:6px;height:6px;border-radius:50%;background:var(--good);box-shadow:0 0 6px 1px color-mix(in srgb,var(--good) 60%,transparent);
        animation:novaLivePulse 2.4s ease-in-out infinite;flex:none}
      @keyframes novaLivePulse{0%,100%{opacity:1}50%{opacity:.45}}
      @media (prefers-reduced-motion: reduce){.live-dot{animation:none}}
      .area-caps{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:9px;min-height:20px}
      .area-cap{width:21px;height:21px;border-radius:var(--r-ctl);background:var(--surface);border:1px solid var(--line-soft);
        display:flex;align-items:center;justify-content:center;font-size:var(--t-cap);opacity:.85}
      .area-cap svg{width:13px;height:13px;fill:none;stroke:var(--ink-dim);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
      .new-decision-row{cursor:pointer}
      .area-climate{display:flex;gap:10px;margin-bottom:9px}
      .area-climate-item{flex:1;min-width:0}
      .area-climate-num{font-family:var(--font-body);font-size:var(--t-sm);font-weight:500;display:flex;align-items:baseline;gap:3px}
      .area-climate-num .unit{font-size:var(--t-cap);color:var(--ink-faint)}
      .area-climate svg{display:block;width:100%;height:16px;margin-top:2px}
      .area-bottom{display:flex;align-items:center;justify-content:space-between;padding-top:8px;border-top:1px solid var(--line-soft)}
      .area-stat{font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-dim)}
      .area-stat b{color:var(--ink);font-weight:600}
      .area-light-toggle{font-family:var(--font-body);font-size:var(--t-sm);font-weight:600;
        padding:3px 9px;border-radius:var(--r-ctl);border:1px solid var(--line-soft);background:var(--surface);
        color:var(--ink-faint);cursor:pointer}
      .area-light-toggle.on{background:color-mix(in srgb,var(--gold) 16%,transparent);border-color:color-mix(in srgb,var(--gold) 44%,transparent);color:var(--gold-pale)}
      .camera-panel{grid-column:1/-1}
      .camera-head-row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
      .camera-note{font-size:var(--t-sm);color:var(--ink-dim);max-width:46ch}
      .camera-toggle{font-family:var(--font-body);font-size:var(--t-sm);color:var(--ink-faint);background:var(--surface-2);
        border:1px solid var(--line-soft);border-radius:var(--r-ctl);padding:6px 10px;cursor:pointer}
      .camera-strip{display:none;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;margin-top:12px}
      .camera-strip.open{display:grid}
      .camera-slot{border-radius:var(--r-tile);background:var(--surface-2);border:1px solid var(--line-soft);overflow:hidden;padding-bottom:9px}
      .camera-slot img,.camera-empty{width:100%;aspect-ratio:16/9;object-fit:cover;display:flex;align-items:center;justify-content:center;background:var(--media-bg);color:var(--ink-faint);font-family:var(--font-body);font-size:var(--t-cap)}
      .camera-caption{padding:8px 9px 4px;display:flex;flex-direction:column;gap:2px}.camera-caption b{font-size:var(--t-sm)}.camera-caption span{font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint)}
      .camera-actions{display:flex;flex-wrap:wrap;gap:5px;padding:4px 9px}.camera-actions .mode-chip{padding:4px 7px;font-size:var(--t-cap)}
      .camera-diagnostic{font-size:var(--t-cap);color:var(--ink-dim);line-height:1.35;padding:5px 9px 0;overflow-wrap:anywhere}
      .nova-notice{position:fixed;left:16px;right:16px;bottom:16px;margin:0 auto;width:fit-content;z-index:20;max-width:560px;
        padding:10px 16px;border-radius:var(--r-tile);background:var(--surface-2);border:1px solid var(--gold);color:var(--ink);
        font-size:var(--t-sm);line-height:1.4;box-shadow:0 6px 24px #0008;cursor:pointer}
      .nova-notice.error{border-color:var(--bad);color:var(--alarm-ink)}
      .nova-notice[hidden]{display:none}
      .danger{color:var(--bad-ink);border-color:color-mix(in srgb,var(--bad-strong) 40%,transparent)}
      .danger:hover{border-color:var(--bad);background:color-mix(in srgb,var(--bad-strong) 8%,transparent)}
      .footnote{max-width:1100px;margin:20px auto 0;text-align:center;font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint)}
      .new-log-entries{max-height:65vh;overflow-y:auto;display:flex;flex-direction:column;gap:1px;margin-top:8px}
      .intr-snap{margin-bottom:10px}
      .intr-img{width:100%;max-width:320px;border-radius:var(--r-tile);border:1px solid var(--line-soft);display:block;margin-bottom:6px}
      .new-ilog-item{padding:10px 0;border-top:1px solid var(--line-soft)}
      .new-ilog-item:first-of-type{border-top:none}
      .new-log-entry{display:grid;grid-template-columns:70px 110px 1fr;gap:10px;padding:7px 8px;
        font-family:var(--font-body);font-size:var(--t-cap);border-bottom:1px solid var(--line-soft);align-items:baseline}
      .new-log-entry-error{background:color-mix(in srgb,var(--bad-strong) 8%,transparent)}
      .new-log-ts{color:var(--ink-faint)}
      .new-log-cat{white-space:nowrap;font-weight:600}
      .new-log-msg{color:var(--ink-dim);word-break:break-word}
      @media (max-width:560px){.new-log-entry{grid-template-columns:1fr;gap:2px}}
      .new-decision-row{grid-template-columns:150px minmax(150px,230px) minmax(0,1fr) auto;align-items:start}
      .new-decision-row .new-log-cat{white-space:normal;overflow-wrap:anywhere}
      @media (max-width:820px){
        .new-decision-row{grid-template-columns:minmax(0,1fr) auto;gap:2px 10px}
        .new-decision-row .new-log-ts{grid-column:1 / -1;grid-row:1}
        .new-decision-row .new-log-cat{grid-column:1;grid-row:2}
        .new-decision-row > span:last-child{grid-column:2;grid-row:2;justify-self:end}
        .new-decision-row .new-log-msg{grid-column:1 / -1;grid-row:3}
      }

      /* Top nav (v7.94.0) */
      .top-nav{display:flex;flex-wrap:wrap;gap:4px;background:var(--surface);border:1px solid var(--line-soft);border-radius:var(--r-tile);padding:4px;flex-basis:100%;order:3}
      .nav-tab{font-family:var(--font-body);font-size:var(--t-sm);font-weight:600;padding:7px 14px;border-radius:var(--r-ctl);
        border:none;background:transparent;color:var(--ink-dim);cursor:pointer}
      .nav-tab.active{background:var(--ember);color:var(--on-accent)}
      .nav-tab:not(.active):hover{color:var(--ink)}
      /* A phone shows the tabs as one row that scrolls sideways; the open tab
         is scrolled into view on every draw. */
      @media (max-width:640px){
        .top-nav{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
        .top-nav::-webkit-scrollbar{display:none}
        .nav-tab{flex:none;white-space:nowrap}
      }

      /* Settings (v7.94.0) */
      .settings-toolbar{max-width:1100px;margin:0 auto 16px;display:flex;flex-direction:column;gap:10px}
      .settings-search{width:100%;background:var(--surface);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:var(--t-sm);padding:10px 14px;border-radius:var(--r-tile)}
      .settings-search::placeholder{color:var(--ink-faint)}
      .settings-nav{display:flex;flex-wrap:wrap;gap:6px}
      .settings-nav-btn{font-family:var(--font-body);font-size:var(--t-sm);font-weight:600;padding:6px 12px;border-radius:var(--r-card);
        border:1px solid var(--line-soft);background:var(--surface);color:var(--ink-dim);cursor:pointer}
      .settings-nav-btn.active{background:var(--ember);border-color:var(--ember);color:var(--on-accent)}
      .settings-grid{max-width:1100px;margin:0 auto;column-count:2;column-gap:14px}
      @media (max-width:720px){.settings-grid{column-count:1}}
      .settings-card{break-inside:avoid;margin-bottom:14px;display:inline-block;width:100%}
      .settings-card[hidden]{display:none}
      /* The Cameras group is only two cards, so lay it out as a plain two
         column grid. Column breaks inside CSS columns are not supported in
         Safari, so Doorbell Training could not be pushed beside Cameras
         that way. */
      @media (min-width:721px){
        .settings-grid[data-section="cameras"]{column-count:auto;display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}
        .settings-grid[data-section="cameras"] .settings-card{margin-bottom:0}
      }
      .new-cam-collapse{background:none;border:none;padding:0;cursor:pointer;font:inherit;font-size:var(--t-sm);color:var(--ink-dim);text-align:left}
      .new-cam-collapse:hover{color:var(--ink)}
      .new-cam-caret{display:inline-block;width:1em}
      .stub-tag{font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint);
        background:var(--surface-2);border:1px solid var(--line-soft);border-radius:var(--r-card);padding:2px 8px;margin-left:8px;vertical-align:middle}
      .stub-body{font-size:var(--t-sm);color:var(--ink-dim);line-height:1.5}
      .stub-where{display:block;margin-top:6px;font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint)}
      .cfg-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
      .cfg-row-wrap{flex-wrap:wrap;justify-content:flex-start}
      .cfg-row label{font-size:var(--t-sm);color:var(--ink-dim)}
      select.cfg-field,input.cfg-field{background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:var(--t-sm);padding:6px 9px;border-radius:var(--r-ctl)}
      input.cfg-field:hover,input.cfg-field:focus,select.cfg-field:hover,select.cfg-field:focus{border-color:var(--gold)}
      .cfg-num{width:84px;min-width:0;text-align:right}
      .door-map-sel-new{flex:1;min-width:0;max-width:220px}
      textarea.cfg-field.vp-json{flex:1;min-width:0;background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:var(--t-sm);padding:6px 9px;border-radius:var(--r-ctl);resize:vertical}
      textarea.cfg-field.vp-json:focus{border-color:var(--gold)}
      .vp-row{flex-wrap:wrap}.vp-row .toggle-desc{flex:1;min-width:0;overflow-wrap:anywhere}
      .vp-editor select.cfg-field{flex:1;min-width:0;max-width:100%}
      .vp-note{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:-4px 0 8px}
      .vid-ok{color:var(--gold)}.vid-bad{color:var(--warn)}
      .vid-token-row{display:flex;gap:6px;align-items:center;min-width:0}.vid-token{flex:1;min-width:0}
      .vid-person{flex-wrap:wrap}.vid-actions{display:flex;flex-wrap:wrap;gap:6px}
      .vid-progress{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0}
      .vid-danger{color:var(--warn)}
      .vp-link{background:none;border:none;padding:0;color:var(--gold);font:inherit;cursor:pointer;text-decoration:underline}
      .mode-grid{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
      .mode-chip{font-family:var(--font-body);font-size:var(--t-sm);
        padding:6px 12px;border-radius:var(--r-ctl);border:1px solid var(--line-soft);background:var(--surface-2);color:var(--ink-dim);cursor:pointer}
      .mode-chip[data-mode], .mode-chip[data-agency]{text-transform:capitalize}
      .mode-chip:hover{border-color:var(--gold)}
      .mode-chip-on{background:var(--ember);border-color:var(--ember);color:var(--on-accent)}
      .mode-bindings>summary{cursor:pointer;list-style:none}
      .mode-bindings>summary::-webkit-details-marker{display:none}
      .mode-bindings>summary::before{content:"▸ ";color:var(--ink-faint)}
      .mode-bindings[open]>summary::before{content:"▾ "}
      .mode-bind-head{font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint);letter-spacing:.06em;
        text-transform:uppercase;margin:12px 0 8px;padding-top:12px;border-top:1px solid var(--line-soft)}
      .diag-ok{color:var(--good)} .diag-warn{color:var(--warn)} .diag-idle{color:var(--ink-dim)}
      .diag-down{color:var(--bad)} .diag-off{color:var(--ink-faint)}
      .new-model-list{display:flex;flex-direction:column;gap:10px}
      .new-model-row{display:grid;grid-template-columns:88px 1fr 1.3fr auto;gap:6px;align-items:center}
      .model-label{font-family:var(--font-body);font-size:var(--t-cap);letter-spacing:.06em;color:var(--ink-faint);text-transform:uppercase}
      .new-model-row .new-prov-select,.new-model-row .new-model-select{width:100%;min-width:0;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:var(--t-sm);padding:6px 8px;border-radius:var(--r-ctl)}
      .new-model-custom{grid-column:2/4;width:100%;box-sizing:border-box;padding:6px 9px;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--gold);
        font-family:var(--font-body);font-size:var(--t-cap);border-radius:var(--r-ctl)}
      .new-model-custom:focus{border-color:var(--gold)}
      .new-model-refresh{padding:5px 9px;font-size:var(--t-sm);line-height:1}
      .new-model-warning{grid-column:1/-1;font-size:var(--t-cap);color:var(--warn);margin-top:2px}
      .new-model-row .stub-body{grid-column:1/-1;font-size:var(--t-cap);margin-top:2px}
      .cred-row{display:grid;grid-template-columns:88px 74px 1fr auto auto;gap:6px;align-items:center}
      .cred-status{font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint);text-transform:uppercase}
      .cred-status.cred-configured{color:var(--gold)}
      .cred-input{width:100%;min-width:0;box-sizing:border-box;padding:6px 8px;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:var(--t-sm);border-radius:var(--r-ctl)}
      .cred-input:focus{border-color:var(--gold)}
      .new-appliance-list{display:flex;flex-direction:column;gap:8px;margin-bottom:10px}
      .new-appliance-row{display:grid;grid-template-columns:1.1fr .9fr 1.3fr 72px auto;gap:6px;align-items:end}
      /* After the rule above, so a phone gets two columns: the name on its own row. */
      @media (max-width:640px){
        .new-appliance-row{grid-template-columns:1fr 1fr}
        .new-appliance-row > .field:first-child{grid-column:1 / -1}
      }
      .new-appliance-row input,.new-appliance-row select{background:var(--surface-2);border:1px solid var(--line-soft);
        color:var(--ink);font-family:var(--font-body);font-size:var(--t-cap);padding:5px 7px;border-radius:var(--r-ctl);min-width:0;width:100%;box-sizing:border-box}
      .new-appliance-row input:focus,.new-appliance-row select:focus{border-color:var(--gold)}
      .new-appliance-remove{flex:none;width:26px;height:26px;padding:0;font-size:var(--t-cap);color:var(--bad-ink);
        border:1px solid color-mix(in srgb,var(--bad-strong) 30%,transparent);background:transparent;border-radius:var(--r-ctl);cursor:pointer}
      .new-appliance-remove:hover{border-color:var(--bad-strong);background:color-mix(in srgb,var(--bad-strong) 8%,transparent)}
      .new-pl-chip{display:inline-flex;align-items:center;gap:5px;font-family:var(--font-body);font-size:var(--t-cap);
        padding:5px 8px;border-radius:var(--r-ctl);border:1px solid var(--line-soft);background:var(--surface-2);color:var(--ink-dim)}
      .new-pl-del,.new-excl-ent-del,.new-excl-dom-del,.new-excl-lab-del,.new-dep-cal-del,.new-mem-forget{background:none;border:none;color:var(--ink-faint);cursor:pointer;font-size:var(--t-sm);padding:0}
      .new-pl-del:hover,.new-excl-ent-del:hover,.new-excl-dom-del:hover,.new-excl-lab-del:hover,.new-dep-cal-del:hover,.new-mem-forget:hover{color:var(--bad-strong)}
      .new-camset-row{padding:10px 0;border-top:1px solid var(--line-soft)}
      .new-camset-row:first-of-type{border-top:none}
      .toggle-list{display:flex;flex-direction:column;gap:2px}
      .toggle-row{display:grid;grid-template-columns:1fr auto;grid-template-rows:auto auto;gap:2px 10px;
        padding:9px 0;border-top:1px solid var(--line-soft)}
      .toggle-row:first-child{border-top:none}
      .toggle-label{font-size:var(--t-sm);font-weight:600;grid-column:1;grid-row:1}
      .toggle-desc{font-size:var(--t-sm);color:var(--ink-faint);grid-column:1;grid-row:2}
      .toggle-btn{grid-column:2;grid-row:1/3;align-self:center;font-family:var(--font-body);font-size:var(--t-sm);font-weight:600;
        padding:6px 12px;border-radius:var(--r-ctl);border:1px solid var(--line-soft);background:var(--surface-2);
        color:var(--ink-faint);cursor:pointer;min-width:44px}
      .toggle-btn.on{background:color-mix(in srgb,var(--good) 16%,transparent);border-color:color-mix(in srgb,var(--good) 44%,transparent);color:var(--good-ink)}
      .toggle-row select.cfg-field{grid-column:2;grid-row:1/3;align-self:center}
      .pairing-list{display:flex;flex-direction:column;gap:8px;margin-bottom:8px}
      .pairing-row{display:flex;align-items:center;justify-content:space-between;gap:10px}
      .pairing-label{font-size:var(--t-sm);color:var(--ink-dim)}
      .pairing-row select{background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:var(--t-sm);padding:6px 9px;border-radius:var(--r-ctl);max-width:56%}
      .person-honorific-row{flex-wrap:wrap}
      .person-honorific-custom{background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:var(--t-sm);padding:6px 9px;border-radius:var(--r-ctl);width:100%;margin-top:6px}
      .fpn-toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px}
      .fpn-floor-tabs,.fpn-actions{display:flex;gap:6px;flex-wrap:wrap}
      .fpn-hint{font-size:var(--t-cap);color:var(--ink-faint);font-family:var(--font-body);margin-bottom:6px}
      .fpn-canvas{min-height:520px;margin-bottom:10px}
      .fpn-actions{margin-top:4px}
      .fpn-op-marker.op-glow{opacity:1;stroke:var(--ink);stroke-width:2.5;filter:drop-shadow(0 0 6px var(--gold-pale))}
      .fpn-svg{touch-action:pan-x pan-y;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
      .fpn-drag-room,.fpn-zone,.fpn-prop-vtx,.fpn-prop-mid,.fpn-cam,.fpn-ent{touch-action:none}
      .fpn-zoom{display:inline-flex;gap:6px}
      .fpn-selbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:8px;min-height:32px}
      .fpn-sel-name{font-family:var(--font-display);color:var(--gold);margin-right:4px}
      .fpn-sel-tools{display:inline-flex;flex-wrap:wrap;gap:6px}
      .fpn-keys{flex-basis:100%}
      .fpn-hint-touch{display:none}
      .fpn-unsaved{color:var(--warn);align-self:center}
      .fpn-unsaved[hidden]{display:none}
      @media (pointer:coarse){
        .fpn-hint-mouse{display:none}
        .fpn-hint-touch{display:block}
        .fpn-keys{display:none}
        .fpn-zone-vtx,.fpn-prop-vtx,.fpn-zone-mid,.fpn-prop-mid,.fpn-cam-dot,.fpn-ent-dot,.fpn-resize-handle{stroke:transparent;stroke-width:var(--fpn-hit,44px)}
        .fpn-zone-mid,.fpn-prop-mid{fill:var(--gold);fill-opacity:0.45}
      }
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
      .chat-log{max-height:55vh;overflow-y:auto;margin-bottom:10px}
      .chat-turn{padding:8px 0;border-bottom:1px solid var(--line-soft);display:flex;gap:10px;font-size:var(--t-sm);line-height:1.5}
      .chat-turn:last-child{border-bottom:none}
      .chat-who{flex:0 0 52px;font-family:var(--font-body);font-size:var(--t-cap);color:var(--ink-faint);padding-top:2px}
      .chat-turn.nova .chat-who{color:var(--gold)}
      .chat-text{flex:1;min-width:0;white-space:pre-wrap;overflow-wrap:anywhere}
      .chat-error .chat-text{color:var(--warn)}
      .chat-compose{display:flex;gap:8px}.chat-compose .cfg-field{flex:1;min-width:0}
      .fpn-inline-lbl{display:inline-flex;align-items:center;gap:4px;font-size:var(--t-cap);color:var(--ink-dim)}
      .cfg-help{margin:-4px 0 10px}
      .field{display:flex;flex-direction:column;gap:4px;flex:1 1 150px;min-width:0}
      .cfg-row-wrap,.goal-create,.cfg-row:has(> .field){align-items:flex-end;flex-wrap:wrap}
      .field > span{font-size:var(--t-sm);color:var(--ink-dim)}
      .field > input,.field > select{width:100%}
    `;
  }
