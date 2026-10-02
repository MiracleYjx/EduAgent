(() => {
  if (window.__t160PassiveObserverInstalled) return;
  window.__t160PassiveObserverInstalled = true;
  let active = null;
  let mutationVersion = 0;
  let imageLoadVersion = 0;
  const completed = new Set();
  const q = (selector) => document.querySelector(selector);
  const cfg = () => {
    const node = q("#t160-observer-config [data-t160-config]");
    if (!node) return null;
    try { return JSON.parse(node.dataset.t160Config); } catch { return null; }
  };
  const value = (id) => {
    const root = q("#" + id);
    const input = root && root.querySelector("textarea,input:not([type=hidden])");
    return input ? input.value : "";
  };
  const visible = (node) => !!(node && node.getClientRects().length);
  const queueBusy = () => Array.from(document.querySelectorAll(
    '#t160-panel [aria-busy="true"],#t160-panel .generating,#t160-panel .pending,#t160-panel [data-testid="loading-status"]'
  )).some(visible);
  const snapshot = () => {
    const fields = {};
    for (const id of ["number","order","qtype","content","score","points","answer","rubric","analysis"]) {
      fields[id] = value("t160-" + id);
    }
    const page = q("#t160-page-preview img");
    const images = Array.from(document.querySelectorAll("#t160-panel img"));
    return {
      fields,
      page_alt: page ? page.alt : null,
      page_src: page ? page.getAttribute("src") : null,
      page_loaded: !!(page && page.complete && page.naturalWidth > 0),
      page_facts: q("#t160-page-facts")?.textContent?.trim() || "",
      images_count: images.length,
      images_loaded: images.every(img => img.complete && img.naturalWidth > 0),
      queue_busy: queueBusy(),
      field_interactive: ["number","content","score"].every(id => {
        const node = q("#t160-" + id + " textarea,#t160-" + id + " input");
        return !!(node && !node.disabled && !node.readOnly);
      }),
      panel_text: q("#t160-panel")?.textContent || ""
    };
  };
  const send = (record) => fetch("/t160-observer", {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(record),
    keepalive:true
  }).catch(() => { const n=q("#t160-observer-status"); if(n)n.textContent="Observer log transport failed; evidence incomplete."; });
  const safeSnapshot = (s) => ({
    fields:s.fields, page_alt:s.page_alt, page_facts:s.page_facts,
    page_loaded:s.page_loaded, images_count:s.images_count, images_loaded:s.images_loaded,
    queue_busy:s.queue_busy, field_interactive:s.field_interactive,
    visibility_state:document.visibilityState, document_has_focus:document.hasFocus(),
    mutation_version:mutationVersion, image_load_version:imageLoadVersion
  });
  const finish = (status, reason) => {
    if (!active) return;
    const item=active; active=null; completed.add(item.config.run_id);
    clearTimeout(item.timeout);
    const stop=performance.now();
    const seen=snapshot();
    const record={
      ...item.config.identity, run_id:item.config.run_id, event:"end", status, reason,
      started_at:item.startedAt, ended_at:new Date().toISOString(),
      start_event:item.startEvent, end_event:status==="success"?"target_page_and_fields_loaded_queue_idle_stable_double_raf":status,
      start_perf_ms:item.startPerf, end_perf_ms:stop, elapsed_ms:stop-item.startPerf,
      measurement_source:"browser_performance_now_passive_native_event_dom_image_double_raf",
      timeout_limit_ms:item.config.timeout_ms, target_ms:500,
      target_met:status==="success"?(stop-item.startPerf)<500:false,
      screenshot_ref:item.config.screenshot_ref, screenshot_status:"pending_capture",
      actual_snapshot:safeSnapshot(seen),
      console_error_observations:item.errors
    };
    send(record);
    const node=q("#t160-observer-status");
    if(node)node.textContent=record.run_id+": "+status+"; "+record.elapsed_ms.toFixed(2)+" ms; "+(reason||"");
  };
  const expected = (s, c) =>
    s.page_loaded && s.images_loaded && !s.queue_busy &&
    s.page_alt === c.expected.page_alt &&
    Object.entries(c.expected.fields).every(([k,v]) => s.fields[k] === v) &&
    (!c.expected.require_interactive || s.field_interactive);
  const tick = () => {
    if(!active)return;
    const s=snapshot(), c=active.config;
    const fingerprint=JSON.stringify({
      fields:s.fields,page_alt:s.page_alt,page_src:s.page_src,page_facts:s.page_facts,
      images_count:s.images_count,images_loaded:s.images_loaded,panel_text:s.panel_text,
      mutationVersion,imageLoadVersion
    });
    if(!expected(s,c)){active.stableKey=null;active.stableFrames=0;}
    else if(active.stableKey===fingerprint)active.stableFrames++;
    else {active.stableKey=fingerprint;active.stableFrames=0;}
    if(active.stableFrames>=2){finish("success",null);return;}
    requestAnimationFrame(tick);
  };
  const begin = (event, eventName, optionLabel) => {
    const c=cfg();
    if(!c || active || completed.has(c.run_id))return;
    if(optionLabel.trim()!==c.target_option_label.trim())return;
    active={
      config:c,startPerf:performance.now(),startedAt:new Date().toISOString(),
      startEvent:eventName,stableKey:null,stableFrames:0,errors:[],
      before:safeSnapshot(snapshot())
    };
    active.timeout=setTimeout(()=>finish("timeout","Target fields/page/image/queue never reached the required end state."),c.timeout_ms);
    send({...c.identity,run_id:c.run_id,event:"start",status:"measuring",started_at:active.startedAt,
      start_event:eventName,start_perf_ms:active.startPerf,measurement_source:"browser_performance_now",
      before_snapshot:active.before,screenshot_ref:c.screenshot_ref});
    const node=q("#t160-observer-status");if(node)node.textContent=c.run_id+": measuring";
    requestAnimationFrame(tick);
  };
  const targetOptionEvent = (event, eventName) => {
    const c=cfg();if(!c)return;
    const root=q("#"+c.selector_id);
    const option=event.target instanceof Element ? event.target.closest('[role="option"]') : null;
    const expanded=!!(root && (root.getAttribute("aria-expanded")==="true" ||
      root.querySelector('[aria-expanded="true"]')));
    // Gradio options can be portaled outside the component root.
    // The intended dropdown must be open and the selected label must match.
    if(option && expanded)begin(event,eventName,option.getAttribute("aria-label") || option.textContent || "");
  };
  document.addEventListener("pointerdown", event =>
    targetOptionEvent(event,"native_target_option_pointerdown"), true);
  document.addEventListener("click", event =>
    targetOptionEvent(event,"native_target_option_click"), true);
  document.addEventListener("keydown", event => {
    const c=cfg();if(!c || event.key!=="Enter")return;
    const root=q("#"+c.selector_id);
    if(!(event.target instanceof Element) || !root?.contains(event.target))return;
    const aid=event.target.getAttribute("aria-activedescendant");
    const option=aid?document.getElementById(aid):root.querySelector('[role="option"][aria-selected="true"]');
    if(option)begin(event,"native_target_option_enter",option.getAttribute("aria-label") || option.textContent || "");
  }, true);
  document.addEventListener("input", event => {
    const c=cfg();if(!c || !(event.target instanceof HTMLInputElement))return;
    const root=q("#"+c.selector_id);
    if(root?.contains(event.target))begin(event,"native_selected_input",event.target.value);
  }, true);
  document.addEventListener("load", event => { if(event.target instanceof HTMLImageElement && q("#t160-panel")?.contains(event.target))imageLoadVersion++; }, true);
  window.addEventListener("error", e => {
    if(active)active.errors.push({kind:"window_error",message:String(e.message||"").slice(0,300)});
  });
  window.addEventListener("unhandledrejection", e => {
    if(active)active.errors.push({kind:"unhandled_rejection",message:String(e.reason||"").slice(0,300)});
  });
  const observer=new MutationObserver(records => { if(records.some(r => q('#t160-panel')?.contains(r.target)))mutationVersion++; });
  observer.observe(document.documentElement,{subtree:true,childList:true,attributes:true,characterData:true});
  document.documentElement.dataset.t160Observer="installed";
})();