/* Browser replay of the event-based inspection simulator. No external runtime. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const palette = ['#087f83','#c17836','#7763a1','#3b6ca5','#b45365','#64874b','#a48b29','#478d9d'];
  const taskColors = {S:'#087f83', L:'#b96b29', M:'#7967a5'};
  const phases = {idle:'Available',ready_charge:'Available',ready_transit:'Departing',takeoff:'Taking off',transit:'Flying',ready_service:'At site',service:'Inspecting',service_ready:'At site',decision:'Selecting next task',arrived:'Arriving',continue:'At site',ready_return:'Returning',return:'Returning',landing_wait:'Holding',landing:'Landing',turnaround:'Turnaround',charge_wait:'Charge queue',charging:'Charging',ground_closed:'At base',failed:'Unavailable'};
  const flying = new Set(['takeoff','transit','return','service','ready_service','ready_return','ready_transit','continue','landing_wait','landing','service_ready','decision','arrived']);
  const activeEvents = new Set(['task_completed','inspection_completed','task_interrupted','unit_interrupted','service_interrupted','weather_changed','sortie_started','sortie_finished','departed','arrived','charge_started','charge_completed','aircraft_failed','unit_completed','phase_changed']);
  const state = {replay:null, geography:null, time:0, playing:false, speed:300, lastWall:0, current:null, frameIndex:0, selectedSite:null, view:null, autoFit:true, lastEventSignature:'', loading:false, trails:new Map(), completedBySite:new Map(), aircraftNodes:new Map()};
  const canvas = $('map'), ctx = canvas.getContext('2d');
  const chart = $('age-chart'), chartCtx = chart.getContext('2d');
  let width=0, height=0, dpr=1, chartWidth=0, chartHeight=0;
  let gesture=null, needsDraw=true, lastPanelUpdate=0;

  function finite(value, fallback=0) { return Number.isFinite(Number(value)) ? Number(value) : fallback; }
  function clamp(x,a,b) { return Math.max(a,Math.min(b,x)); }
  function escape(value) { return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function clock(seconds, short=false) { const n=Math.max(0,Math.floor(finite(seconds))),h=Math.floor(n/3600),m=Math.floor(n%3600/60),s=n%60; return [h,m,...(short?[]:[s])].map(x=>String(x).padStart(2,'0')).join(':'); }
  function project(lon,lat) { return [finite(lon)*Math.PI/180, Math.log(Math.tan(Math.PI/4+clamp(finite(lat),-85,85)*Math.PI/360))]; }
  function screen(lon,lat) { const p=project(lon,lat),v=state.view;return [width/2+(p[0]-v.cx)*v.scale,height/2-(p[1]-v.cy)*v.scale]; }
  function aircraftId(a) { return String(a.id ?? a.aircraft_id ?? ''); }
  function normalizedSiteId(value) { return String(value ?? '').replace(/-PER(?::.*)?$/,''); }
  function limit() { return finite(state.replay?.metadata?.duration_s, state.replay?.frames?.at(-1)?.t || 21600); }

  async function jsonFetch(url, options={}) {
    const response=await fetch(url,options);let body;
    try {body=await response.json();} catch (_) {throw new Error(`The server returned an invalid response (${response.status}).`);}
    if (!response.ok) throw new Error(body.error || body.message || `Request failed (${response.status}).`);
    return body;
  }
  function setLoading(on,message='Simulating inspection operations') {
    state.loading=on;$('loading-overlay').hidden=!on;$('loading-message').textContent=message;
    $('loading-hint').textContent=state.replay?'Updating the fleet, task and charging trajectories':'Preparing the operating scenario';
    $('run-button').disabled=on;$('run-button').querySelector('span').textContent=on?'Running…':'Run simulation';
    for (const el of $('config-form').querySelectorAll('select')) el.disabled=on;if(!on)$('recovery_policy').disabled=$('network_id').value==='legacy_24';
  }
  function loadReplay(replay) {
    if (!replay || !Array.isArray(replay.frames) || !replay.frames.length) throw new Error('This run contains no simulation frames.');
    state.replay=replay;state.time=0;state.playing=false;state.current=null;state.selectedSite=null;state.frameIndex=0;state.lastEventSignature='';state.trails.clear();state.completedBySite.clear();
    replay.frames.sort((a,b)=>a.t-b.t);replay.events=(replay.events||[]).slice().sort((a,b)=>a.t-b.t);
    for (const frame of replay.frames) for (const aircraft of frame.aircraft||[]) {
      const id=aircraftId(aircraft);if(!state.trails.has(id))state.trails.set(id,[]);
      state.trails.get(id).push({t:frame.t,lon:aircraft.lon,lat:aircraft.lat,phase:aircraft.phase});
    }
    for (const event of replay.events) if (isCompletion(event)) {
      const id=normalizedSiteId(event.site_id||event.asset_id);if(!id)continue;
      if(!state.completedBySite.has(id))state.completedBySite.set(id,[]);state.completedBySite.get(id).push(event.t);
    }
    const conf=replay.metadata.config||replay.metadata;
    $('network_id').value=conf.network_id||'pingtan_96'; updateNetworkOptions(conf.fleet_size);
    for (const key of ['network_id','fleet_size','targets_per_sortie','charge_ports','port_input_w','policy','weather','duration_hours','recovery_policy']) {
      const el=$(key),value=conf[key];if(value!==undefined && [...el.options].some(o=>o.value===String(value)))el.value=String(value);
    }
    $('timeline').max=limit();$('timeline').disabled=false;$('play-button').disabled=false;$('reset-button').disabled=false;$('download-button').disabled=false;
    $('clock-end').textContent=clock(limit());$('timeline-ticks').replaceChildren(...Array.from({length:5},(_,i)=>{const s=document.createElement('span');s.textContent=clock(limit()*i/4,true);return s;}));
    $('weather-credit').hidden=conf.weather!=='historical';
    $('map-network-name').textContent=`${replay.bases.length===1?'Single-base':'Three-base'} network`; $('wind-description').textContent=replay.metadata.wind_label; $('wind-scope').textContent=replay.bases.length===1?'regional wind':'regional min–max';
    $('map-site-count').textContent=`${(replay.sites||[]).length} sites`;
    $('run-status').textContent='Simulation complete';
    $('run-status').className='status-pass';$('connection-text').textContent='Ready';
    $('site-detail').hidden=true;buildFleet();fitMap();renderAll();updatePlayButton();
  }
  function frameAt(t) {
    const frames=state.replay.frames;let low=0,high=frames.length-1;
    while(low<high){const mid=Math.ceil((low+high)/2);if(frames[mid].t<=t)low=mid;else high=mid-1;}
    state.frameIndex=low;const a=frames[low]; let consumed=0;
    const aircraft=state.replay.aircraft.map(profile=>{
      const rows=state.replay.tracks[profile.id];let lo=0,hi=rows.length-1;
      while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(rows[mid].t<=t)lo=mid;else hi=mid-1;}
      const row=rows[lo],elapsed=clamp(t-row.t,0,row.end_t-row.t),f=row.end_t>row.t?elapsed/(row.end_t-row.t):0;
      consumed+=row.consumed_wh_start+elapsed*row.power_w/3600;
      return {...row,id:profile.id,lon:row.lon+f*(row.end_lon-row.lon),lat:row.lat+f*(row.end_lat-row.lat),
        energy_wh:row.energy_wh+f*(row.end_energy_wh-row.energy_wh),heading:Math.atan2(row.end_lon-row.lon,row.end_lat-row.lat)};
    });
    function information(history){const stamps=new Map(state.replay.sites.map(s=>[s.task_id||s.id,-s.initial_age_s]));let count=0;
      for(const event of history){if(event.t>t)break;stamps.set(event.task_id,Math.max(stamps.get(event.task_id),event.stamp));count++;}
      return {age:[...stamps.values()].reduce((sum,stamp)=>sum+t-stamp,0)/stamps.size/3600,count};}
    const c=information(state.replay.collection_history),p=information(state.replay.platform_history);
    return {...a,aircraft,completed:c.count,delivered:p.count,age_h:c.age,platform_age_h:p.age,energy_wh:consumed,
      charging:aircraft.filter(x=>x.phase==='charging').length,queue:aircraft.filter(x=>x.phase==='charge_wait').length};
  }
  function seek(t) {if(!state.replay)return;state.time=clamp(finite(t),0,limit());renderAll();}
  function play() {if(!state.replay)return;if(state.time>=limit())state.time=0;state.playing=true;state.lastWall=performance.now();updatePlayButton();}
  function pause() {state.playing=false;updatePlayButton();}
  function updatePlayButton() {
    $('play-icon').innerHTML=state.playing?'<path d="M5 3h4v14H5zm7 0h4v14h-4z"/>':'<path d="m6 3 11 7-11 7z"/>';
    $('play-button').setAttribute('aria-label',state.playing?'Pause simulation':'Play simulation');
    $('playback-label').textContent=periodLabel();
  }
  function periodLabel() {
    if(!state.replay)return 'Simulation time';
    const m=state.replay.metadata;
    if(state.time<finite(m.evaluation_start_s))return 'Warm-up period';
    return state.time<finite(m.evaluation_end_s,limit())?'Operating period':'Recovery period';
  }
  function resize() {
    const rect=canvas.getBoundingClientRect();dpr=Math.min(window.devicePixelRatio||1,2);width=rect.width;height=rect.height;
    canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);
    const c=chart.getBoundingClientRect();chartWidth=c.width;chartHeight=c.height;chart.width=Math.round(c.width*dpr);chart.height=Math.round(c.height*dpr);chartCtx.setTransform(dpr,0,0,dpr,0,0);
    if(state.replay && (!state.view||state.autoFit))fitMap();needsDraw=true;drawChart();
  }
  function fitMap() {
    if(!state.replay)return;
    const b=state.replay.metadata.map_bounds;
    let points=Array.isArray(b)&&b.length===4?[project(b[0],b[1]),project(b[2],b[3])]:[...(state.replay.sites||[]),state.replay.base].filter(Boolean).map(p=>project(p.lon,p.lat));
    if(!points.length)return;
    const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);const minx=Math.min(...xs),maxx=Math.max(...xs),miny=Math.min(...ys),maxy=Math.max(...ys);
    const scale=Math.min(Math.max(100,width-90)/Math.max(maxx-minx,1e-6),Math.max(100,height-115)/Math.max(maxy-miny,1e-6));
    state.view={cx:(minx+maxx)/2,cy:(miny+maxy)/2,scale,initialScale:scale};state.autoFit=true;needsDraw=true;
  }
  function zoom(factor,x=width/2,y=height/2) {
    if(!state.view)return;state.autoFit=false;const v=state.view,old=v.scale;v.scale=clamp(old*factor,v.initialScale*.65,v.initialScale*18);
    v.cx+=(x-width/2)/old-(x-width/2)/v.scale;v.cy-=(y-height/2)/old-(y-height/2)/v.scale;needsDraw=true;
  }
  function pathLine(coords,close=false) {if(!coords.length)return;coords.forEach((c,i)=>{const p=screen(c[0],c[1]);i?ctx.lineTo(...p):ctx.moveTo(...p);});if(close)ctx.closePath();}
  function geoPath(g) {
    if(!g)return;
    if(g.type==='Polygon')g.coordinates.forEach(r=>pathLine(r,true));
    else if(g.type==='MultiPolygon')g.coordinates.forEach(p=>p.forEach(r=>pathLine(r,true)));
    else if(g.type==='LineString')pathLine(g.coordinates);
    else if(g.type==='MultiLineString')g.coordinates.forEach(r=>pathLine(r));
    else if(g.type==='GeometryCollection')g.geometries.forEach(geoPath);
  }
  function drawGeography() {
    ctx.fillStyle='#dbe9ed';ctx.fillRect(0,0,width,height);
    const features=state.geography?.features||[];
    for (const layer of ['land','landuse','roads']) for (const f of features) {
      if(f.properties?.layer!==layer)continue;const type=f.properties.fclass||'',g=f.geometry;if(!g)continue;
      ctx.beginPath();geoPath(g);
      if(layer==='land'){ctx.fillStyle='#f2f1e9';ctx.fill('evenodd');ctx.strokeStyle='#b5cac9';ctx.lineWidth=1;ctx.stroke();}
      if(layer==='landuse'){ctx.fillStyle=/forest|park|grass|recreation|nature|orchard|farm|meadow|scrub|heath/.test(type)?'#d7e3d2':/industrial|commercial|retail/.test(type)?'#e0ded4':'#e7e6dc';ctx.fill('evenodd');}
      if(layer==='roads') {
        const major=/motorway|trunk|primary|secondary/.test(type);ctx.lineWidth=major?3.4:1.5;ctx.strokeStyle=major?'#d9c5a3':'#d1d5cc';ctx.stroke();ctx.lineWidth=major?2:0.7;ctx.strokeStyle=major?'#faf2dc':'#fafaf5';ctx.stroke();
      }
    }
    if(state.replay?.bases.length>1){
      for(const f of features.filter(f=>f.properties?.layer==='windzone')){ctx.beginPath();geoPath(f.geometry);ctx.fillStyle='#087f830b';ctx.fill('evenodd');ctx.strokeStyle='#4b778a70';ctx.lineWidth=1;ctx.setLineDash([4,4]);ctx.stroke();ctx.setLineDash([]);}
      for(const wind of state.current?.winds||[]){const[x,y]=screen(wind.lon,wind.lat),speed=Math.hypot(wind.east,wind.north);ctx.save();ctx.translate(x,y);ctx.rotate(Math.atan2(wind.east,wind.north));ctx.strokeStyle=speed>10?'#a6522c':'#336d8e';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(0,12);ctx.lineTo(0,-12);ctx.lineTo(-4,-6);ctx.moveTo(0,-12);ctx.lineTo(4,-6);ctx.stroke();ctx.restore();ctx.font='600 10px system-ui';ctx.fillStyle='#35596a';ctx.textAlign='left';ctx.fillText(`${wind.id} ${speed.toFixed(1)} m/s`,x+8,y+18);}
    }
    // A geographic name is a map label, independent of scenario facility assignments.
    if(state.replay?.metadata?.map_label!==false) {
      ctx.font='600 13px system-ui, sans-serif';ctx.fillStyle='#708e9290';ctx.textAlign='left';ctx.fillText('PINGTAN ISLAND',21,height-102);
    }
  }
  function drawTracks() {
    const from=state.time-1200;
    for(const [id,rows] of Object.entries(state.replay.tracks)) {
      const color=palette[state.replay.aircraft.findIndex(a=>a.id===id)%palette.length];ctx.beginPath();
      for(const r of rows){if(r.t>state.time)break;if(r.end_t<from||!['transit','return'].includes(r.phase)||r.end_t<=r.t)continue;
        const f=clamp((state.time-r.t)/(r.end_t-r.t),0,1);ctx.moveTo(...screen(r.lon,r.lat));ctx.lineTo(...screen(r.lon+f*(r.end_lon-r.lon),r.lat+f*(r.end_lat-r.lat)));}
      ctx.strokeStyle=color+'99';ctx.lineWidth=2;ctx.stroke();
    }
  }
  function lastSiteCompletion(id) {const times=state.completedBySite.get(normalizedSiteId(id))||[];let result=null;for(const t of times){if(t>state.time)break;result=t;}return result;}
  function drawSites() {
    const occupied=new Set((state.current?.aircraft||[]).filter(a=>a.phase==='service').map(a=>normalizedSiteId(a.site_id||a.task_id?.split('-PER')[0]||'')));
    for (const site of state.replay.sites||[]) {
      const [x,y]=screen(site.lon,site.lat);if(x<-20||y<-20||x>width+20||y>height+20)continue;
      const selected=state.selectedSite?.id===site.id,color=taskColors[site.template]||taskColors.S;
      if(selected||occupied.has(site.id)){ctx.beginPath();ctx.arc(x,y,selected?13:11,0,Math.PI*2);ctx.fillStyle=color+'22';ctx.fill();ctx.strokeStyle=color+'70';ctx.lineWidth=1;ctx.stroke();}
      ctx.beginPath();ctx.arc(x,y,selected?5.5:4,0,Math.PI*2);ctx.fillStyle=color;ctx.fill();ctx.strokeStyle='#fff';ctx.lineWidth=1.5;ctx.stroke();
      const label=String(site.id).match(/(\d+)$/)?.[1]||String(site.id);ctx.font=`${selected?'700':'600'} 9px system-ui,sans-serif`;ctx.textAlign='left';ctx.lineWidth=3;ctx.strokeStyle='#ffffffdd';ctx.strokeText(label,x+7,y-5);ctx.fillStyle='#49606a';ctx.fillText(label,x+7,y-5);
    }
    for(const b of state.replay.bases){const[x,y]=screen(b.lon,b.lat);ctx.save();ctx.translate(x,y);ctx.fillStyle='#fff';ctx.strokeStyle='#152b3c';ctx.lineWidth=1.3;ctx.beginPath();ctx.roundRect(-8,-8,16,16,3);ctx.fill();ctx.stroke();ctx.fillStyle='#152b3c';ctx.fillRect(-5,-4,10,8);ctx.fillStyle='#fff';ctx.fillRect(-1.5,-1,3,5);ctx.restore();ctx.font='700 10px system-ui,sans-serif';ctx.textAlign='left';ctx.lineWidth=4;ctx.strokeStyle='#ffffffed';ctx.strokeText(b.id,x+12,y+4);ctx.fillStyle='#203c4d';ctx.fillText(b.id,x+12,y+4);}
  }
  function drawAircraft() {
    const aircraft=state.current?.aircraft||[];const atBase=new Map();
    aircraft.forEach((a,index)=> {
      const color=palette[index%palette.length];let[x,y]=screen(a.lon,a.lat);const airborne=flying.has(a.phase);
      if(!airborne){const key=`${Math.round(x)},${Math.round(y)}`,n=atBase.get(key)||0;atBase.set(key,n+1);x+=18+(n%4)*14;y+=16+Math.floor(n/4)*15;}
      ctx.save();ctx.translate(x,y);if(airborne)ctx.rotate(a.heading||0);
      ctx.shadowColor='#163c4b35';ctx.shadowBlur=4;ctx.shadowOffsetY=2;
      ctx.lineWidth=4;ctx.strokeStyle='#fff';ctx.lineCap='round';ctx.beginPath();ctx.moveTo(-5,-5);ctx.lineTo(5,5);ctx.moveTo(5,-5);ctx.lineTo(-5,5);ctx.stroke();
      ctx.shadowBlur=0;ctx.shadowOffsetY=0;ctx.lineWidth=1.8;ctx.strokeStyle=color;ctx.beginPath();ctx.moveTo(-5,-5);ctx.lineTo(5,5);ctx.moveTo(5,-5);ctx.lineTo(-5,5);ctx.stroke();
      for(const[dx,dy]of[[-5,-5],[5,-5],[-5,5],[5,5]]){ctx.beginPath();ctx.arc(dx,dy,2.8,0,Math.PI*2);ctx.fillStyle='#fff';ctx.fill();ctx.lineWidth=1.4;ctx.stroke();}
      ctx.beginPath();ctx.moveTo(0,-5);ctx.lineTo(2.7,3);ctx.lineTo(-2.7,3);ctx.closePath();ctx.fillStyle=color;ctx.fill();ctx.restore();
      if(airborne){ctx.font='700 9px system-ui,sans-serif';ctx.textAlign='center';ctx.strokeStyle='#fffffff0';ctx.lineWidth=3;ctx.strokeText(aircraftId(a),x,y-13);ctx.fillStyle=color;ctx.fillText(aircraftId(a),x,y-13);}
    });
  }
  function drawCompass() {ctx.save();ctx.translate(width-32,height-87);ctx.font='700 9px system-ui,sans-serif';ctx.textAlign='center';ctx.fillStyle='#54717c';ctx.fillText('N',0,-13);ctx.beginPath();ctx.moveTo(0,-7);ctx.lineTo(-3.5,4);ctx.lineTo(0,1);ctx.lineTo(3.5,4);ctx.closePath();ctx.fill();ctx.restore();}
  function drawMap() {needsDraw=false;if(!width||!height)return;ctx.clearRect(0,0,width,height);if(!state.view||!state.replay){ctx.fillStyle='#dbe9ed';ctx.fillRect(0,0,width,height);return;}drawGeography();drawTracks();drawSites();drawAircraft();drawCompass();const metersPerPixel=6378137*Math.cos((state.replay.base?.lat||25.49)*Math.PI/180)/state.view.scale;const sizes=[50,100,200,500,1000,2000,5000,10000];const m=sizes.reduce((best,n)=>Math.abs(n/metersPerPixel-65)<Math.abs(best/metersPerPixel-65)?n:best,1000);$('scale-label').textContent=m>=1000?`${m/1000} km`:`${m} m`;$('scale-bar').style.width=`${m/metersPerPixel}px`;$('scale-label').style.width=`${m/metersPerPixel}px`;}
  function buildFleet() {
    const container=$('fleet-list');container.replaceChildren();state.aircraftNodes.clear();
    (state.replay.aircraft||[]).forEach((a,i)=>{const id=aircraftId(a),div=document.createElement('div');div.className='aircraft-card';div.style.setProperty('--aircraft-color',palette[i%palette.length]);div.innerHTML=`<div class="aircraft-top"><span class="aircraft-name"><i></i>${escape(id)}</span><span class="phase">Available</span></div><div class="aircraft-spec">${escape((a.capabilities||[]).join(' + ').toUpperCase() || a.profile_id || 'Inspection UAV')}</div><div class="battery-row"><span class="battery-bar"><i></i></span><span class="battery-value">—</span></div><div class="aircraft-task">At base</div>`;container.append(div);state.aircraftNodes.set(id,{el:div,profile:a});});
  }
  function updateFleet() {
    let count=0;
    for(const a of state.current.aircraft||[]) {
      const id=aircraftId(a),n=state.aircraftNodes.get(id);if(!n)continue;if(flying.has(a.phase))count++;
      const phase=n.el.querySelector('.phase');phase.className=`phase ${a.phase}`;phase.textContent=phases[a.phase]||String(a.phase).replaceAll('_',' ');
      const capacity=finite(n.profile.capacity_wh||n.profile.battery_usable_wh,1),energy=finite(a.energy_wh);n.el.querySelector('.battery-bar>i').style.width=`${clamp(100*energy/capacity,0,100)}%`;
      n.el.querySelector('.battery-value').textContent=`${Math.round(energy)} Wh`;
      const task=a.task_id||a.site_id;n.el.querySelector('.aircraft-task').textContent=task?`Task · ${String(task).replace('-PER', '')}`:(flying.has(a.phase)?'Recovering':`At ${a.base_id||'base'}`);
    }
    $('fleet-active').textContent=`${count} airborne`;
  }
  function isCompletion(e){return ['task_completed','inspection_completed'].includes(e.kind);}
  function readableEvent(e) {
    if(e.message)return e.message;
    const who=e.aircraft_id?`${e.aircraft_id} · `:'',site=e.site_id||e.asset_id||'',phase=e.phase;
    if(e.kind==='phase_changed')return `${who}${phases[phase]||String(phase||'').replaceAll('_',' ')}${site?` at ${site}`:''}`;
    if(isCompletion(e))return `${who}Inspection completed${site?` at ${site}`:''}`;
    if(e.kind.includes('interrupt'))return `${who}Sensing interrupted${site?` at ${site}`:''}`;
    if(e.kind==='weather_changed')return 'Wind conditions changed';
    if(e.kind==='unit_completed')return `${who}Sensing step completed${site?` at ${site}`:''}`;
    return who+String(e.kind||'Event').replaceAll('_',' ');
  }
  function updateEvents() {
    const events=state.replay.events.filter(e=>e.t<=state.time&&(activeEvents.has(e.kind)||e.message));
    const recent=events.slice(-8).reverse(),signature=recent.map(e=>`${e.t}:${e.kind}:${e.aircraft_id}`).join('|');if(signature===state.lastEventSignature)return;state.lastEventSignature=signature;
    $('event-count').textContent=`${events.length} events`;const log=$('event-log');log.replaceChildren();
    if(!recent.length){const p=document.createElement('p');p.className='empty-state';p.textContent='Start playback to follow operations.';log.append(p);return;}
    for(const e of recent){const div=document.createElement('div');div.className='event-item';const tone=isCompletion(e)?'complete':e.kind.includes('interrupt')?'interrupted':'';div.innerHTML=`<div class="event-time">${clock(e.t,true)}</div><div><i class="event-kind-dot ${tone}"></i>${escape(readableEvent(e))}</div>`;log.append(div);}
  }
  function updateSiteDetail() {
    const site=state.selectedSite;if(!site)return;
    const last=lastSiteCompletion(site.id);const age=last===null?(finite(site.initial_age_s)+state.time)/3600:(state.time-last)/3600;
    $('site-status').textContent=last===null?`No completed inspection yet · age ${age.toFixed(2)} h`:`Last update ${clock(last)} · age ${age.toFixed(2)} h`;
  }
  function selectSite(site) {
    state.selectedSite=site;$('site-detail').hidden=!site;
    if(site){$('site-code').textContent=site.id;$('site-title').textContent={S:'Short RGB inspection',L:'Continuous RGB inspection',M:'Multiview zoom inspection'}[site.template]||'Inspection requirement';$('site-description').textContent=`${site.evidence==='mapped'?'Mapped observation anchor':'Inferred observation site'} · target interval ${(finite(site.interval_s,10800)/3600).toFixed(1)} h`;updateSiteDetail();}
    needsDraw=true;
  }
  function drawChart() {
    if(!chartWidth||!chartHeight)return;const c=chartCtx,w=chartWidth,h=chartHeight;c.clearRect(0,0,w,h);
    if(!state.replay)return;const series=state.replay.frames,values=series.flatMap(f=>[finite(f.age_h),finite(f.platform_age_h)]);const min=0,max=Math.max(1,Math.ceil(Math.max(...values)*1.05));const left=24,right=w-5,top=5,bottom=h-18;
    c.font='8px system-ui,sans-serif';c.lineWidth=1;c.textAlign='right';
    for(const fraction of [0,.5,1]){const y=bottom-(bottom-top)*fraction;c.beginPath();c.moveTo(left,y);c.lineTo(right,y);c.strokeStyle='#e5ecef';c.stroke();c.fillStyle='#96a3ad';c.fillText((min+(max-min)*fraction).toFixed(max<4?1:0),left-5,y+3);}
    const stride=Math.max(1,Math.floor(series.length/500));
    for(const [field,color] of [['platform_age_h','#a9582c'],['age_h','#087f83']]){let first=true;c.beginPath();
      for(let i=0;i<series.length&&series[i].t<=state.time;i+=stride){const f=series[i],x=left+(right-left)*f.t/limit(),y=bottom-(bottom-top)*finite(f[field])/max;first?c.moveTo(x,y):c.lineTo(x,y);first=false;}
      if(!first)c.lineTo(left+(right-left)*state.time/limit(),bottom-(bottom-top)*finite(state.current?.[field])/max);c.strokeStyle=color;c.lineWidth=1.7;c.stroke();}
    c.fillStyle='#96a3ad';c.textAlign='left';c.fillText('0 h',left,h-2);c.textAlign='right';c.fillText(`${(limit()/3600).toFixed(0)} h`,right,h-2);
  }
  function updatePanels() {
    const f=state.current;if(!f)return;
    $('metric-completed').textContent=Math.round(finite(f.completed)).toLocaleString();
    $('metric-age').textContent=Number.isFinite(Number(f.age_h))?Number(f.age_h).toFixed(2):'—';
    $('metric-platform-age').textContent=Number(f.platform_age_h).toFixed(2);
    $('cross-base-count').hidden=state.replay.bases.length===1;
    $('cross-base-count').textContent=`Cross-base landings: ${state.replay.events.filter(e=>e.cross_base&&e.t<=state.time).length}`;
    $('metric-energy').textContent=(finite(f.energy_wh)/1000).toFixed(2);
    const count=x=>Array.isArray(x)?x.length:finite(x);$('metric-charging').textContent=`${count(f.charging)} / ${count(f.queue)}`;
    const east=finite(f.wind_east_mps),north=finite(f.wind_north_mps),speeds=(f.winds||[]).map(w=>Math.hypot(w.east,w.north));$('wind-speed').textContent=speeds.length>1?`${Math.min(...speeds).toFixed(1)}–${Math.max(...speeds).toFixed(1)} m/s`:`${Math.hypot(east,north).toFixed(1)} m/s`;$('wind-arrow').style.transform=`rotate(${Math.atan2(east,north)*180/Math.PI}deg)`;
    $('clock-current').textContent=clock(state.time);$('playback-label').textContent=periodLabel();$('timeline').value=state.time;const pct=100*state.time/limit();$('timeline').style.background=`linear-gradient(to right,#087f83 ${pct}%,#d1dee3 ${pct}%)`;
    updateFleet();updateEvents();updateSiteDetail();drawChart();
  }
  function renderAll() {if(!state.replay)return;state.current=frameAt(state.time);updatePanels();drawMap();}
  function tick(now) {
    if(state.playing&&state.replay&&!state.loading){const elapsed=Math.min(1,(now-state.lastWall)/1000);state.time=Math.min(limit(),state.time+elapsed*state.speed);state.current=frameAt(state.time);needsDraw=true;if(now-lastPanelUpdate>90){updatePanels();lastPanelUpdate=now;}if(state.time>=limit()){pause();updatePanels();}}
    state.lastWall=now;if(needsDraw)drawMap();requestAnimationFrame(tick);
  }

  function updateNetworkOptions(preferred){const single=$('network_id').value==='legacy_24',values=single?[2,4,8]:[6,12,24],old=Number(preferred||$('fleet_size').value);$('fleet_size').replaceChildren(...values.map(v=>{const o=document.createElement('option');o.value=v;o.textContent=`${v} UAVs`;return o;}));$('fleet_size').value=values.includes(old)?old:(single?4:12);$('recovery_policy').disabled=single;if(single)$('recovery_policy').value='HOME';}
  $('network_id').addEventListener('change',()=>updateNetworkOptions());
  $('play-button').addEventListener('click',()=>state.playing?pause():play());
  $('reset-button').addEventListener('click',()=>{pause();seek(0);});
  $('speed').addEventListener('change',e=>{state.speed=finite(e.target.value,300);});
  $('timeline').addEventListener('input',e=>{pause();seek(e.target.value);});
  $('zoom-in').addEventListener('click',()=>zoom(1.3));$('zoom-out').addEventListener('click',()=>zoom(1/1.3));$('fit-map').addEventListener('click',fitMap);
  $('close-site').addEventListener('click',()=>selectSite(null));
  $('configure-toggle').addEventListener('click',()=>{const open=$('configuration').classList.toggle('open');$('configure-toggle').setAttribute('aria-expanded',String(open));});
  canvas.addEventListener('pointerdown',e=>{if(!state.view)return;canvas.setPointerCapture(e.pointerId);gesture={x:e.clientX,y:e.clientY,cx:state.view.cx,cy:state.view.cy,moved:false};});
  canvas.addEventListener('pointermove',e=>{if(!gesture)return;const dx=e.clientX-gesture.x,dy=e.clientY-gesture.y;if(Math.hypot(dx,dy)>4){gesture.moved=true;state.autoFit=false;}state.view.cx=gesture.cx-dx/state.view.scale;state.view.cy=gesture.cy+dy/state.view.scale;needsDraw=true;});
  canvas.addEventListener('pointerup',e=>{if(!gesture)return;if(!gesture.moved&&state.replay){const rect=canvas.getBoundingClientRect(),x=e.clientX-rect.left,y=e.clientY-rect.top;let best=null,distance=16;for(const s of state.replay.sites||[]){const p=screen(s.lon,s.lat),d=Math.hypot(x-p[0],y-p[1]);if(d<distance){best=s;distance=d;}}selectSite(best);}gesture=null;});
  canvas.addEventListener('pointercancel',()=>{gesture=null;});
  canvas.addEventListener('wheel',e=>{e.preventDefault();const r=canvas.getBoundingClientRect();zoom(Math.exp(-e.deltaY*.0014),e.clientX-r.left,e.clientY-r.top);},{passive:false});
  canvas.addEventListener('keydown',e=>{if(!state.view)return;const v=state.view,step=45/v.scale;let handled=true;if(e.key==='+'||e.key==='=')zoom(1.25);else if(e.key==='-')zoom(.8);else if(e.key==='Home')fitMap();else if(e.key==='ArrowLeft')v.cx-=step;else if(e.key==='ArrowRight')v.cx+=step;else if(e.key==='ArrowUp')v.cy+=step;else if(e.key==='ArrowDown')v.cy-=step;else handled=false;if(handled){e.preventDefault();needsDraw=true;}});
  $('config-form').addEventListener('submit',async e=>{
    e.preventDefault();pause();$('run-error').hidden=true;const config=Object.fromEntries(new FormData(e.target));for(const key of ['fleet_size','targets_per_sortie','charge_ports','port_input_w','duration_hours'])config[key]=Number(config[key]);if(config.network_id==='legacy_24')config.recovery_policy='HOME';
    setLoading(true);try{const replay=await jsonFetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(config)});loadReplay(replay);$('configuration').classList.remove('open');$('configure-toggle').setAttribute('aria-expanded','false');}
    catch(error){$('run-error').textContent=error.message;$('run-error').hidden=false;$('connection-text').textContent='Run failed';}
    finally{setLoading(false);}
  });
  $('download-button').addEventListener('click',()=>{if(!state.replay)return;const blob=new Blob([JSON.stringify(state.replay,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='island-uav-run.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  new ResizeObserver(resize).observe($('map').parentElement);new ResizeObserver(resize).observe($('age-chart').parentElement);
  window.islandApp={seek,pause,play,getTime:()=>state.time,getReplay:()=>state.replay};
  resize();requestAnimationFrame(tick);
  Promise.all([jsonFetch('/api/default'),jsonFetch('/api/map').catch(()=>null)])
    .then(([replay,geography])=>{state.geography=geography;$('map-no-geography').hidden=!!geography;loadReplay(replay);setLoading(false);})
    .catch(error=>{setLoading(false);$('run-error').textContent=error.message;$('run-error').hidden=false;$('connection-text').textContent='Unavailable';$('run-status').textContent='Connection failed';$('run-status').className='status-error';});
})();
