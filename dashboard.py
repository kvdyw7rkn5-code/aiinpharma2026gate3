"""Read-only jury dashboard over the ledger.   python3 dashboard.py --mode REALTIME  ->  http://localhost:8050

The dashboard opens the ledger with SQLite mode=ro, so it can never change records (URS-DI-005).
All derived values shown are recomputed from the stored original records with the frozen model.
"""
import argparse
import gzip
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import lab
from analysis import analyze
from model import LineMonitor, MaterialBook
from store import Ledger

LINES = ("ASM-01", "ASM-02", "ASM-03")


PAGE = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Gate 3 Digital Twin</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
body{font-family:-apple-system,Segoe UI,Arial,sans-serif;margin:0;background:#f5f6f8;color:#1d2433}
header{background:#1f3a5f;color:#fff;padding:12px 20px;display:flex;gap:24px;align-items:center}
header h1{font-size:18px;margin:0}.pill{padding:3px 10px;border-radius:12px;font-size:13px}
.ok{background:#2e8b57;color:#fff}.bad{background:#c0392b;color:#fff}.warn{background:#e0a100;color:#000}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:12px}
.card{background:#fff;border-radius:8px;padding:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h2{font-size:16px;margin:0 0 6px;display:flex;justify-content:space-between}
.kv{font-size:12px;line-height:1.6}.kv b{display:inline-block;width:150px;color:#555;font-weight:500}
.lock{background:#eef4ff;border-left:4px solid #1f3a5f;padding:6px 8px;font-size:12px;margin-top:6px;word-break:break-all}
table{border-collapse:collapse;width:100%;font-size:12px}td,th{border-bottom:1px solid #eee;padding:3px 6px;text-align:left}
.wide{grid-column:span 3}.half{grid-column:span 1}
.tab{background:#2c4f7c;color:#fff;border:0;padding:6px 14px;margin-right:4px;border-radius:6px;cursor:pointer;font-size:13px}
.tab.on{background:#fff;color:#1f3a5f;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;padding:12px 12px 0}
.kpi{background:#fff;border-radius:8px;padding:10px 12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.kpi .v{font-size:24px;font-weight:600}.kpi .l{font-size:12px;color:#666}
.PASS{color:#2e8b57;font-weight:600}.WARN{color:#b07d00;font-weight:600}.FAIL{color:#c0392b;font-weight:600}.INFO{color:#2471a3;font-weight:600}
.note{font-size:12px;color:#444;line-height:1.5}
.labctl{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;font-size:12px;margin:6px 0}
.labctl select,.labctl input{font-size:12px;padding:3px}.labctl input{width:80px}
fieldset{border:1px solid #ddd;border-radius:6px;display:flex;gap:10px;padding:4px 10px}legend{font-size:11px;color:#666}
.chk{border:1px solid #ddd;border-radius:14px;padding:3px 9px;cursor:pointer;user-select:none}.chk input{margin-right:4px}
.run{background:#1f3a5f;color:#fff;border:0;border-radius:6px;padding:7px 16px;cursor:pointer;font-size:13px}.sim{border:2px dashed #e0a100}
</style></head><body>
<header><h1>Enteral Feeding Device · Digital Twin (Gate 3)</h1>
<span><button class="tab on" id="b-live" onclick="show('live')">Live monitor</button><button class="tab" id="b-ana" onclick="show('ana')">Data analysis</button></span><span id="mode"></span><span id="integ"></span><span id="model" style="font-size:12px"></span></header>
<div id="tab-live"><div class="grid" id="lines"></div>
<div class="grid">
 <div class="card"><h2>BAG-01 · port ID by lot (in)</h2><canvas id="bag"></canvas></div>
 <div class="card"><h2>PVC-01 · tube OD by reel (in)</h2><canvas id="pvc"></canvas></div>
 <div class="card"><h2>Alerts (latest)</h2><div style="max-height:260px;overflow:auto"><table id="alerts"></table></div></div>
</div></div>
__ANALYSIS_HTML__
<script>
const LINES=["ASM-01","ASM-02","ASM-03"], charts={};
function card(l){return `<div class="card"><h2>${l}<span id="st-${l}" class="pill"></span></h2>
<canvas id="d-${l}" height="190"></canvas><canvas id="c-${l}" height="80"></canvas>
<div class="kv" id="kv-${l}"></div><div id="lk-${l}"></div></div>`}
document.getElementById("lines").innerHTML=LINES.map(card).join("");
function mk(id,cfg){if(charts[id]){charts[id].data=cfg.data;charts[id].update('none');return}
 charts[id]=new Chart(document.getElementById(id),cfg)}
const opt=(y)=>({animation:false,responsive:true,plugins:{legend:{labels:{boxWidth:10,font:{size:10}}}},
 scales:{x:{type:'linear',ticks:{font:{size:10}}},y:{title:{display:true,text:y,font:{size:10}},ticks:{font:{size:10}}}},elements:{point:{radius:1.5}}});
async function tick(){
 const s=await (await fetch('/api/state')).json();
 document.getElementById('mode').innerHTML=`<span class="pill warn">${s.mode}</span>`;
 document.getElementById('integ').innerHTML=s.integrity.ok?`<span class="pill ok">Ledger hash chain OK · ${s.integrity.entries} entries</span>`:`<span class="pill bad">INTEGRITY FAILURE: ${s.integrity.problem}</span>`;
 document.getElementById('model').textContent=`model ${s.model.version} · ${s.model.formula} · σ=${s.model.sigma_mm} mm · R²=${s.model.r2}`;
 for(const l of LINES){
  const L=s.lines[l],u=L.units,f=L.forecast,p=L.prediction,e=L.evaluation;
  const x0=u.length?u[u.length-1].event_no:0;
  const fc=f&&f.path?f.path.slice(0,40):[];
  const lim=(v)=>u.length?[{x:u[0].event_no,y:v},{x:x0+(fc.length||1),y:v}]:[];
  const ds=[{label:'depth mm',data:u.map(r=>({x:r.event_no,y:r.depth})),showLine:false,
      pointBackgroundColor:u.map(r=>r.nok?'#c0392b':'#1f77b4'),borderColor:'#1f77b4'},
    {label:'baseline (material+pressure)',data:u.map(r=>({x:r.event_no,y:r.baseline})),borderColor:'#aaa',borderWidth:1,pointRadius:0},
    {label:'forecast μ',data:fc.map(q=>({x:x0+q.k,y:q.mu})),borderColor:'#e67e22',borderDash:[4,3],pointRadius:0},
    {label:'±2σ',data:fc.map(q=>({x:x0+q.k,y:q.hi})),borderColor:'rgba(230,126,34,.35)',pointRadius:0,borderWidth:1},
    {label:'',data:fc.map(q=>({x:x0+q.k,y:q.lo})),borderColor:'rgba(230,126,34,.35)',pointRadius:0,borderWidth:1},
    {label:'LSL/USL',data:lim(17),borderColor:'#c0392b',pointRadius:0,borderWidth:1},
    {label:'',data:lim(13),borderColor:'#c0392b',pointRadius:0,borderWidth:1}];
  if(p){const pe=p.current_event_no+p.units_ahead;ds.push({label:'LOCKED prediction '+p.predicted_unit_id,data:[{x:pe,y:13},{x:pe,y:17}],borderColor:'#6c3483',borderWidth:2,pointRadius:0})}
  mk('d-'+l,{type:'scatter',data:{datasets:ds},options:Object.assign(opt('insertion depth (mm)'),{showLine:true})});
  mk('c-'+l,{type:'line',data:{datasets:[{label:'CUSUM+',data:u.map(r=>({x:r.event_no,y:r.cusum_pos})),borderColor:'#c0392b',pointRadius:0},
    {label:'CUSUM−',data:u.map(r=>({x:r.event_no,y:r.cusum_neg})),borderColor:'#2471a3',pointRadius:0},
    {label:'h=5',data:u.length?[{x:u[0].event_no,y:5},{x:x0,y:5}]:[],borderColor:'#999',borderDash:[3,3],pointRadius:0}]},options:opt('CUSUM (σ)')});
  const st=document.getElementById('st-'+l);
  if(e){st.className='pill '+(e.status=='HIT'?'ok':'bad');st.textContent='FAILED · '+e.status}
  else if(p){st.className='pill warn';st.textContent='PREDICTION LOCKED'}
  else if(f&&f.drift_alarm){st.className='pill warn';st.textContent='DE-CALIBRATION'}
  else{st.className='pill ok';st.textContent=u.length?'IN CONTROL':'WAITING'}
  const last=u[u.length-1]||{};
  document.getElementById('kv-'+l).innerHTML=`<b>last unit</b>${last.unit_id||'-'} · ${last.depth??'-'} mm<br>
   <b>drift slope</b>${f?f.slope_mm_per_unit+' mm/unit (t='+f.t_slope+')':'-'}<br>
   <b>P(next unit fails)</b>${f?(100*f.p_next).toFixed(2)+' %':'-'}<br>
   <b>first failure in</b>${f&&f.k_first_fail?f.k_first_fail+' units ('+f.direction+')':'-'}<br>
   <b>axis follow err / vision offset</b>${last.follow_err??'-'} / ${last.vision_offset??'-'} mm`;
  document.getElementById('lk-'+l).innerHTML=p?`<div class="lock">🔒 <b>${p.predicted_unit_id}</b> predicted first failure (${p.predicted_failure_mode})<br>
   committed ${p.prediction_time_utc} while processing ${p.current_unit_id}<br>sha256 ${p.entry_hash}
   ${e?`<br><b>actual first failure: ${e.actual_first_failure_unit_id}</b> (${e.actual_depth_mm} mm) → ${e.status}${e.error_units!=null?', error '+e.error_units+' units':''}`:''}</div>`:'';
 }
 const bar=(id,obj,lo,hi,c)=>mk(id,{type:'bar',data:{labels:Object.keys(obj),datasets:[{label:'mean',data:Object.values(obj),backgroundColor:c}]},
   options:{animation:false,plugins:{legend:{display:false}},scales:{y:{min:lo,max:hi},x:{ticks:{font:{size:9}}}}}});
 bar('bag',s.materials.bag,0.172,0.177,'#5dade2');bar('pvc',s.materials.pvc,0.166,0.174,'#58d68d');
 document.getElementById('alerts').innerHTML='<tr><th>line</th><th>#</th><th>code</th><th>detail</th></tr>'+
  s.alerts.slice().reverse().map(a=>`<tr><td>${a.line}</td><td>${a.event_no}</td><td>${a.code}</td><td>${a.text}</td></tr>`).join('');
}
tick();setInterval(()=>{if(cur=='live')tick()},2000);
__ANALYSIS_JS__
</script></body></html>"""


ANALYSIS_HTML = r"""<div id="tab-ana" style="display:none">
<div class="kpis" id="kpis"></div>
<div class="grid">
 <div class="card wide" id="lab"><h2>Model lab · train on one dataset, test on another, compare models <span id="l-badge"></span></h2>
  <div class="labctl">
   <label>Training data<br><select id="l-train"></select></label>
   <label>Test data<br><select id="l-test"></select></label>
   <fieldset><legend>Fake data generator (used when FAKE is selected)</legend>
    <label>drift<br><select id="l-fk"><option value="none">none</option><option value="axis" selected>actuator drift</option><option value="vision">camera drift</option><option value="step">sudden step</option></select></label>
    <label>mm per unit<br><input id="l-fs" type="number" step="0.001" value="0.010"></label>
    <label>starts at unit<br><input id="l-fst" type="number" value="150"></label>
    <label>units<br><input id="l-fn" type="number" value="600" min="50" max="1800"></label>
    <label>extra noise mm<br><input id="l-fnoise" type="number" step="0.01" value="0"></label>
    <label>seed<br><input id="l-fseed" type="number" value="1"></label></fieldset>
  </div>
  <div class="labctl" id="l-models"></div>
  <div><button class="run" onclick="runLab()">Train &amp; compare</button> <span id="l-status" class="note"></span></div>
  <div id="l-out" style="display:none">
   <table id="l-table" style="margin-top:8px"></table>
   <div class="grid" style="padding:0;grid-template-columns:2fr 1fr">
    <div><b class="note">Test data: measured depth (grey) vs each model's predicted depth</b><canvas id="l-pred" height="120"></canvas></div>
    <div><b class="note">Measured − predicted, 25-unit rolling mean (drift shows as a trend)</b><canvas id="l-res" height="240"></canvas></div>
   </div>
   <div class="note" id="l-note"></div>
  </div>
 </div>
</div>
<div class="grid">
 <div class="card wide"><h2>Whole run · insertion depth of every unit <span><select id="a-line" onchange="renderAna()"></select></span></h2>
  <canvas id="a-depth" height="95"></canvas>
  <div class="note">Each dot is one finished device (x = event no. at the source, hover for unit ID and time). Colour = assembly pressure.
  Red lines = 13.0 / 17.0 mm limits, green dashed = 15.0 mm nominal, grey = model baseline from bag port ID, tube OD and pressure.</div></div>
 <div class="card wide"><h2>Whole run · CUSUM de-calibration statistic and axis following error</h2><canvas id="a-cusum" height="60"></canvas></div>
 <div class="card"><h2>Timestamps per source</h2><table id="a-timing"></table><div class="note" id="a-timing-note"></div></div>
 <div class="card"><h2>Time between consecutive units</h2><canvas id="a-dt" height="190"></canvas><div class="note" id="a-dt-note"></div></div>
 <div class="card"><h2>Data checks (ALCOA+)</h2><table id="a-checks"></table></div>
 <div class="card"><h2>PVC-01 · tube OD, every reading (in)</h2><canvas id="a-pvc" height="190"></canvas><div class="note">Colour = reel OD level (H/M/L). Red = spec 0.1675–0.1725 in.</div></div>
 <div class="card"><h2>BAG-01 · port ID, every measurement (in)</h2><canvas id="a-bag" height="190"></canvas><div class="note">Colour = lot level (H/M/L). Red = spec 0.173–0.176 in.</div></div>
 <div class="card"><h2>What drives insertion depth (mean mm)</h2><canvas id="a-eff" height="190"></canvas></div>
 <div class="card wide"><h2>Point of failure</h2><div id="a-fail"></div></div>
 <div class="card wide sim" id="a-sim-card"><h2>Point of failure in a de-calibration run <span class="pill warn">SYNTHETIC test data · not REST data</span></h2>
  <div class="grid" style="padding:0"><canvas id="s-ASM-01" height="190"></canvas><canvas id="s-ASM-02" height="190"></canvas><canvas id="s-ASM-03" height="190"></canvas></div>
  <div class="note" id="a-sim-note"></div></div>
</div></div>"""

ANALYSIS_JS = r"""
let cur='live', ana=null;
function show(t){cur=t;document.getElementById('tab-live').style.display=t=='live'?'':'none';
 document.getElementById('tab-ana').style.display=t=='ana'?'':'none';
 document.getElementById('b-live').className='tab'+(t=='live'?' on':'');document.getElementById('b-ana').className='tab'+(t=='ana'?' on':'');
 if(t=='ana'){loadAna();initLab();}}
async function loadAna(){ana=await (await fetch('/api/analysis')).json();
 const sel=document.getElementById('a-line'),avail=Object.keys(ana.lines).filter(l=>ana.lines[l]);
 if(!sel.options.length)sel.innerHTML=avail.map(l=>`<option>${l}</option>`).join('');renderAna();}
const PCOL={'3.5':'#5dade2','4':'#f5b041','4.5':'#8e44ad'}, LVL={H:'#c0392b',M:'#f39c12',L:'#2471a3'};
const fmt=n=>Number(n).toLocaleString();
const hline=(y,x0,x1,c,d)=>({label:'',data:[{x:x0,y},{x:x1,y}],borderColor:c,borderDash:d||[],pointRadius:0,borderWidth:1.5,showLine:true});
const vline=(x,y0,y1,c,lab,d)=>({label:lab,data:[{x,y:y0},{x,y:y1}],borderColor:c,borderWidth:2,borderDash:d||[],pointRadius:0,showLine:true});
function sc(id,datasets,ytitle,extra){mk(id,{type:'scatter',data:{datasets},options:Object.assign(opt(ytitle),{plugins:{legend:{labels:{boxWidth:10,font:{size:10},filter:i=>i.text}},
 tooltip:{callbacks:{label:c=>c.raw.u?`${c.raw.u} · ${c.raw.y} · ${c.raw.t||''}`:`${c.raw.y}`}}}},extra||{})})}
function renderAna(){
 const l=document.getElementById('a-line').value, L=ana.lines[l]; if(!L)return;
 const S=L.series, n=S.depth.length, x0=S.event_no[0], x1=S.event_no[n-1];
 const margin=Math.min(L.closest_high[0].margin_mm,L.closest_low[0].margin_mm);
 const k=(v,lab)=>`<div class="kpi"><div class="v">${v}</div><div class="l">${lab}</div></div>`;
 document.getElementById('kpis').innerHTML=k(fmt(ana.total_timestamps),'timestamps in total (all sources)')+
  k(fmt(L.units),`${l} units (${L.first_unit} → ${L.last_unit})`)+k(L.n_nok,'nonconforming units')+
  k(L.capability.mean.toFixed(2)+' mm','mean depth (nominal 15.00)')+k(L.capability.cpk,'Cpk vs 13–17 mm')+k(margin.toFixed(3)+' mm','closest approach to a limit');
 // whole-run depth
 const ds=[];
 for(const p of [...new Set(S.pressure)].sort()){const idx=S.pressure.map((v,i)=>v==p?i:-1).filter(i=>i>=0);
  ds.push({label:p+' bar',data:idx.map(i=>({x:S.event_no[i],y:S.depth[i],u:S.unit_id[i],t:S.time[i]})),backgroundColor:PCOL[String(p)]||'#888',pointRadius:1.6})}
 const bad=S.nok.map((v,i)=>v?i:-1).filter(i=>i>=0);
 if(bad.length)ds.push({label:'NONCONFORMING',data:bad.map(i=>({x:S.event_no[i],y:S.depth[i],u:S.unit_id[i],t:S.time[i]})),backgroundColor:'#c0392b',pointRadius:5});
 const cl=[...L.closest_high.slice(0,3),...L.closest_low.slice(0,3)];
 ds.push({label:'closest to limit',data:cl.map(c=>({x:c.event_no,y:c.depth,u:c.unit_id})),backgroundColor:'#000',pointStyle:'triangle',pointRadius:6});
 ds.push({label:'baseline',data:S.event_no.map((x,i)=>({x,y:S.baseline[i]})),borderColor:'rgba(120,120,120,.5)',borderWidth:1,pointRadius:0,showLine:true});
 ds.push(hline(17,x0,x1,'#c0392b'),hline(13,x0,x1,'#c0392b'),hline(15,x0,x1,'#2e8b57',[6,4]));
 if(L.first_failure)ds.push(vline(L.first_failure.event_no,13,17.5,'#c0392b','first failure '+L.first_failure.unit_id));
 if(L.prediction)ds.push(vline(L.prediction.current_event_no+L.prediction.units_ahead,13,17.5,'#6c3483','predicted '+L.prediction.predicted_unit_id,[4,3]));
 sc('a-depth',ds,'insertion depth (mm)',{});
 sc('a-cusum',[{label:'CUSUM+ (too deep)',data:S.event_no.map((x,i)=>({x,y:S.cusum_pos[i]})),borderColor:'#c0392b',pointRadius:0,showLine:true,borderWidth:1},
  {label:'CUSUM− (too shallow)',data:S.event_no.map((x,i)=>({x,y:S.cusum_neg[i]})),borderColor:'#2471a3',pointRadius:0,showLine:true,borderWidth:1},
  {label:'axis following error (mm)',data:S.event_no.map((x,i)=>({x,y:S.follow_err[i]})),borderColor:'rgba(46,139,87,.6)',pointRadius:0,showLine:true,borderWidth:1},
  Object.assign(hline(5,x0,x1,'#999',[3,3]),{label:'alarm h = 5'})],'σ / mm');
 // timing
 document.getElementById('a-timing').innerHTML='<tr><th>source</th><th>records</th><th>unique</th><th>dup.</th><th>first → last (UTC)</th><th>span</th><th>step</th></tr>'+
  ana.timing.map(t=>`<tr><td>${t.source}</td><td>${fmt(t.records)}</td><td>${fmt(t.timestamps_unique)}</td><td>${t.duplicate_timestamps}</td><td>${t.first.slice(0,19)} → ${t.last.slice(0,19)}</td><td>${t.span_h} h</td><td>${t.typical_interval_s} s</td></tr>`).join('')+
  `<tr><th>total</th><th>${fmt(ana.total_timestamps)}</th><th colspan="5"></th></tr>`;
 document.getElementById('a-timing-note').innerHTML=`${ana.raw_responses} REST responses stored verbatim. BAG/PVC files list lots in non-chronological order (${ana.timing.filter(t=>t.source!=l).map(t=>t.source+': '+t.backward_steps_in_source_order).join(', ')} backward steps); every timestamp is preserved as received.`;
 const iv=L.timing.intervals_s, bins={}; for(const [s,c] of Object.entries(iv)){const b=+s<=10?`${s} s`:`${Math.floor(s/20)*20}-${Math.floor(s/20)*20+19} s`;bins[b]=(bins[b]||0)+c}
 const bk=Object.keys(bins).sort((a,b)=>parseInt(a)-parseInt(b));
 mk('a-dt',{type:'bar',data:{labels:bk,datasets:[{label:'unit pairs',data:bk.map(b=>bins[b]),backgroundColor:'#5d6d7e'}]},options:{animation:false,plugins:{legend:{display:false}}}});
 document.getElementById('a-dt-note').innerHTML=`${L.timing.setup_changes} setup changes (bag lot / reel / pressure). ${L.timing.pauses_gt_10s} pauses &gt; 10 s, of which ${L.timing.pauses_at_setup_change} happen exactly at a setup change: the line runs at a 3 s cycle and stops 60–130 s for every changeover. Average ${L.timing.units_per_hour} units/h.`;
 document.getElementById('a-checks').innerHTML='<tr><th>check</th><th>result</th><th>detail</th></tr>'+ana.checks.map(c=>`<tr><td>${c[0]}</td><td class="${c[1]}">${c[1]}</td><td>${c[2]}</td></tr>`).join('');
 // materials
 if(ana.pvc){const P=ana.pvc.series,g={};P.od.forEach((v,i)=>{const lv=P.reel[i].split('_').pop()[0];(g[lv]=g[lv]||[]).push({x:i+1,y:v,u:P.reel[i],t:P.time[i]})});
  sc('a-pvc',[...Object.entries(g).map(([lv,d])=>({label:'OD level '+lv,data:d,backgroundColor:LVL[lv],pointRadius:.6})),hline(.1725,1,P.od.length,'#c0392b'),hline(.1675,1,P.od.length,'#c0392b')],'OD (in)',{});}
 if(ana.bag){const B=ana.bag.series,g={};B.port.forEach((v,i)=>{const lv=B.lot[i].split('_').pop();(g[lv]=g[lv]||[]).push({x:i+1,y:v,u:B.lot[i]})});
  sc('a-bag',[...Object.entries(g).map(([lv,d])=>({label:'lot level '+lv,data:d,backgroundColor:LVL[lv],pointRadius:1})),hline(.176,1,B.port.length,'#c0392b'),hline(.173,1,B.port.length,'#c0392b')],'port ID (in)',{});}
 const E=L.effects,labs=[],vals=[],cols=[];
 for(const [kk,v] of Object.entries(E.bag_level)){labs.push('bag port '+kk);vals.push(v);cols.push('#5dade2')}
 for(const [kk,v] of Object.entries(E.reel_level)){labs.push('tube '+kk);vals.push(v);cols.push('#58d68d')}
 for(const [kk,v] of Object.entries(E.pressure)){labs.push(kk+' bar');vals.push(v);cols.push('#af7ac5')}
 mk('a-eff',{type:'bar',data:{labels:labs,datasets:[{data:vals,backgroundColor:cols}]},options:{animation:false,plugins:{legend:{display:false}},scales:{y:{min:15.4,max:16.4},x:{ticks:{font:{size:9}}}}}});
 // point of failure
 const row=c=>`<tr><td>${c.unit_id}</td><td>${c.depth}</td><td>${c.margin_mm}</td><td>${c.bag_lot}</td><td>${c.reel}</td><td>${c.pressure}</td></tr>`;
 const th='<tr><th>unit</th><th>depth mm</th><th>margin mm</th><th>bag lot</th><th>reel</th><th>bar</th></tr>';
 const wc=L.worst_case.map(w=>`<tr><td>${w.bag_lot}</td><td>${w.reel}</td><td>${w.pressure}</td><td>${w.pred_mm}</td></tr>`).join('');
 let head;
 if(L.first_failure){const f=L.first_failure;head=`<p><b>First nonconforming unit: ${f.unit_id}</b> (event ${f.event_no}, ${f.time}) depth ${f.depth} mm vs limits ${L.limits.join('–')} mm.
  Baseline expected ${f.baseline} mm → residual ${f.resid>0?'+':''}${f.resid} mm; CUSUM+ ${f.cusum_pos} / CUSUM− ${f.cusum_neg}. ${L.n_nok} nonconforming in total.</p>`+
  (L.prediction?`<p>Locked prediction: <b>${L.prediction.predicted_unit_id}</b> made while processing ${L.prediction.current_unit_id} at ${L.prediction.prediction_time_utc} → <b>${L.evaluation?L.evaluation.status:'pending'}</b></p>`:'<p>No prediction was locked before this failure.</p>');}
 else head=`<p><b>No unit failed: 0 of ${fmt(L.units)} outside ${L.limits.join('–')} mm.</b> The closest approach was <b>${L.closest_high[0].unit_id}</b> at ${L.closest_high[0].depth} mm (${L.closest_high[0].margin_mm} mm below the 17 mm limit),
  produced with the largest bag port (${L.closest_high[0].bag_lot}), a small tube OD (${L.closest_high[0].reel}) and the highest pressure (${L.closest_high[0].pressure} bar), the combination the model predicts deepest.</p>
  <p><b>Why the deep side is where this line would fail first:</b> the mean depth is ${L.capability.mean.toFixed(2)} mm against a 15.00 mm nominal (offset ${(L.capability.mean-15).toFixed(2)} mm, from actuator over-travel ≈ ${(-S.follow_err.reduce((a,b)=>a+b,0)/n).toFixed(2)} mm plus vision offset ≈ 0.41 mm).
  Margin is ${L.closest_low[0].margin_mm} mm on the shallow side but only ${L.closest_high[0].margin_mm} mm on the deep side. With the worst material + pressure combination, <b>${L.drift_to_fail_mm.to_USL_from_worst} mm of extra drift</b> moves the average part to the 17 mm limit
  (${L.drift_to_fail_mm.to_LSL_from_worst} mm to 13 mm). Cpk ${L.capability.cpk} → ≈ ${fmt(L.capability.ppm_expected)} ppm expected too deep even without de-calibration.</p>`;
 document.getElementById('a-fail').innerHTML=head+`<div class="grid" style="padding:0"><div><b>Closest to 17 mm</b><table>${th}${L.closest_high.map(row).join('')}</table></div>
  <div><b>Closest to 13 mm</b><table>${th}${L.closest_low.map(row).join('')}</table></div>
  <div><b>Model: deepest / shallowest set-ups</b><table><tr><th>bag lot</th><th>reel</th><th>bar</th><th>pred. mm</th></tr>${wc}</table></div></div>`;
 // synthetic failure runs
 const simc=document.getElementById('a-sim-card');
 if(!ana.sim){simc.style.display='none';return} simc.style.display='';
 const notes=[];
 for(const [ln,s] of Object.entries(ana.sim)){const e=s.evaluation,N=s.depth.length;
  const pi=s.prediction_unit?parseInt(s.prediction_unit.match(/\d+$/)[0]):null, mi=s.prediction_made_at?parseInt(s.prediction_made_at.match(/\d+$/)[0]):null;
  const d=[{label:'depth',data:s.depth.map((y,i)=>({x:i+1,y})),backgroundColor:'#1f77b4',pointRadius:1.3},hline(17,1,N,'#c0392b'),hline(13,1,N,'#c0392b')];
  if(s.first_alarm_event)d.push(vline(s.first_alarm_event,12.5,17.5,'#e67e22','first alarm #'+s.first_alarm_event));
  if(mi)d.push(vline(mi,12.5,17.5,'#6c3483','prediction locked #'+mi));
  if(pi)d.push(vline(pi,12.5,17.5,'#6c3483','predicted '+s.prediction_unit,[4,3]));
  d.push({label:'actual failure '+e.actual_first_failure_unit_id,data:[{x:e.actual_event_no,y:e.actual_depth_mm}],backgroundColor:'#c0392b',pointRadius:7,pointStyle:'crossRot',borderColor:'#c0392b',borderWidth:3});
  sc('s-'+ln,d,ln+' depth (mm)',{});
  notes.push(`<b>${ln}</b>: alarm at #${s.first_alarm_event}, prediction ${s.prediction_unit||'none'} locked at ${s.prediction_made_at||'-'}, actual first failure ${e.actual_first_failure_unit_id} (${e.actual_depth_mm} mm) → ${e.status}${e.error_units!=null?` (${e.error_units} units early)`:''}`);}
 document.getElementById('a-sim-note').innerHTML='Historical units with injected de-calibration (ASM-01 actuator drift +0.010 mm/unit from #150, ASM-02 camera drift −0.015 mm/unit from #100, ASM-03 actuator drift +0.006 mm/unit from #250), used because the REST data contains no failure.<br>'+notes.join('<br>');
}
"""

MODELS_HTML = r"""<div id="tab-mod" style="display:none"><div class="grid">
 <div class="card wide"><h2>Which model should predict insertion depth?</h2><div class="note" id="m-verdict"></div></div>
 <div class="card wide"><img src="/figures/model_accuracy.png" style="width:100%;max-width:1100px"></div>
 <div class="card wide"><img src="/figures/model_twin.png" style="width:100%;max-width:1100px"></div>
 <div class="card wide"><h2>All models · same data, same inputs, same tests <span class="note">RMSE in mm, lower is better</span></h2>
  <div style="overflow:auto"><table id="m-table"></table></div><div class="note" id="m-schemes"></div></div>
 <div class="card wide"><h2>What each model is</h2><table id="m-expl"></table></div>
 <div class="card"><h2>Which input matters (permutation importance)</h2><table id="m-imp"></table>
  <div class="note">RMSE increase (mm) when one input is randomly shuffled. Same method for every model.</div></div>
 <div class="card"><h2>Why actuator position is not an input</h2><div class="note" id="m-leak"></div></div>
 <div class="card"><h2>Production model (frozen)</h2><div class="note" id="m-prod"></div></div>
</div></div>"""

LAB_JS = r"""
const LCOL=['#2a78d6','#eb6834','#1baf7a','#eda100','#e87ba4','#008300','#6250d6','#e34948'];
let labInit=false, labDS=[];
async function initLab(){ if(labInit)return; labInit=true;
 const r=await (await fetch('/api/lab/datasets')).json(); labDS=r.datasets;
 const opts=r.datasets.map(d=>`<option value="${d.id}">${d.label}</option>`).join('');
 document.getElementById('l-train').innerHTML=opts; document.getElementById('l-test').innerHTML=opts;
 const sim=r.datasets.find(d=>d.id.startsWith('SIM:'));
 if(sim)document.getElementById('l-test').value=sim.id;
 const def=['M03F','M03','M09','M13'];
 document.getElementById('l-models').innerHTML='<b>Models (max 8):</b> '+r.models.map(m=>`<label class="chk" title="${m.name}"><input type="checkbox" value="${m.key}" ${def.includes(m.key)?'checked':''} onchange="capModels(this)">${m.key} ${m.name}</label>`).join('');}
function capModels(el){const on=[...document.querySelectorAll('#l-models input:checked')];if(on.length>8){el.checked=false;alert('Pick at most 8 models so each keeps its own colour.')}}
async function runLab(){
 const g=id=>document.getElementById(id).value, keys=[...document.querySelectorAll('#l-models input:checked')].map(i=>i.value);
 if(!keys.length){alert('Choose at least one model');return}
 const q=new URLSearchParams({train:g('l-train'),test:g('l-test'),models:keys.join(','),fk:g('l-fk'),fs:g('l-fs'),fst:g('l-fst'),fn:g('l-fn'),fnoise:g('l-fnoise'),fseed:g('l-fseed')});
 const st=document.getElementById('l-status'); st.textContent='training and replaying the twin… (a few seconds; the Gaussian process is the slowest)';
 const t0=performance.now(); const R=await (await fetch('/api/lab?'+q)).json();
 if(R.error){st.innerHTML=`<span class="FAIL">${R.error}</span>`;return}
 st.textContent=`done in ${((performance.now()-t0)/1000).toFixed(1)} s · ${R.method} · train ${R.n_train} units, test ${R.n_test} units`;
 const syn=[R.train,R.test].some(id=>(labDS.find(d=>d.id==id)||{}).synthetic);
 document.getElementById('l-badge').innerHTML=syn?'<span class="pill warn">uses SYNTHETIC data</span>':'<span class="pill ok">real REST data</span>';
 document.getElementById('l-out').style.display='';
 const tw=t=>{const s=t.status||'';const c=s=='HIT'||s.startsWith('no failure')?'PASS':s=='EARLY'?'WARN':'FAIL';
  return `<span class="${c}">${s}</span>${t.error_units!=null?` (${t.error_units} units early)`:''}`};
 const best=Math.min(...R.models.map(m=>m.rmse));
 document.getElementById('l-table').innerHTML='<tr><th></th><th>model</th><th>RMSE mm</th><th>MAE</th><th>R²</th><th>bias</th><th>max error</th><th>first drift alarm</th><th>locked prediction</th><th>actual first failure</th><th>twin result</th></tr>'+
  R.models.map((m,i)=>`<tr><td><span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:${LCOL[i]}"></span></td><td>${m.key} ${m.name}</td>
  <td style="${m.rmse==best?'font-weight:700':''}">${m.rmse.toFixed(3)}</td><td>${m.mae.toFixed(3)}</td><td>${m.r2.toFixed(2)}</td><td>${m.bias>0?'+':''}${m.bias.toFixed(3)}</td><td>${m.max_err.toFixed(2)}</td>
  <td>${m.twin.first_alarm||'–'}</td><td>${m.twin.predicted_unit?m.twin.predicted_unit+' <span class="note">(at '+m.twin.predicted_at+')</span>':'–'}</td><td>${m.twin.actual_failure||'none'}</td><td>${tw(m.twin)}</td></tr>`).join('');
 const n=R.actual.length, xs=[...Array(n).keys()].map(i=>i+1);
 const ds=[{label:'measured',data:R.actual.map((y,i)=>({x:i+1,y,u:R.unit_id[i]})),backgroundColor:'rgba(120,120,120,.45)',pointRadius:1.2}];
 R.models.forEach((m,i)=>ds.push({label:m.key,data:m.pred.map((y,j)=>({x:j+1,y,u:R.unit_id[j]})),borderColor:LCOL[i],borderWidth:1.5,pointRadius:0,showLine:true}));
 ds.push(hline(17,1,n,'#c0392b'),hline(13,1,n,'#c0392b'));
 sc('l-pred',ds,'insertion depth (mm)',{});
 const roll=(a,w)=>a.map((_,i)=>{const s=a.slice(Math.max(0,i-w+1),i+1);return s.reduce((p,c)=>p+c,0)/s.length});
 sc('l-res',R.models.map((m,i)=>({label:m.key,data:roll(R.actual.map((y,j)=>y-m.pred[j]),25).map((y,j)=>({x:j+1,y:+y.toFixed(3),u:R.unit_id[j]})),borderColor:LCOL[i],borderWidth:1.5,pointRadius:0,showLine:true})),'mm',{});
 document.getElementById('l-note').innerHTML=(R.same_data?'Training and test data are the same, so predictions come from 5-fold cross-validation (each unit is predicted by a model that did not see it). ':'')+
  'Twin result = this model plugged in as the digital twin\'s baseline and replayed over the test units (first line only), with the same alarm and commit rules as production. '+
  'Lab runs happen in memory only: nothing is written to the ledger and model.json stays frozen.';
}
"""

MODELS_JS = r"""
let mods=null;
async function loadMods(){if(!mods)mods=await (await fetch('/api/models')).json();renderMods()}
function renderMods(){
 const R=mods, box={white:'<span class="PASS">white box</span>',grey:'<span class="WARN">grey box</span>',black:'<span class="FAIL">black box</span>'};
 const f=v=>v>=1?`<b class="FAIL">${v.toFixed(2)}</b>`:v.toFixed(3);
 const best=k=>Math.min(...R.models.map(m=>m.tests[k].rmse));
 const cell=(m,k)=>{const v=m.tests[k].rmse;return `<td style="${v==best(k)?'font-weight:700':''}">${f(v)}</td>`};
 document.getElementById('m-table').innerHTML='<tr><th>#</th><th>model</th><th>family</th><th>transparency</th><th>random</th><th>new lots</th><th>unseen setting</th><th>future</th><th>R² new lots</th><th>parameters</th><th>fit s</th><th>predict ms</th><th>twin: caught early</th><th>twin: false alarm</th></tr>'+
  R.models.map(m=>{const t=R.twin[m.key];return `<tr style="${m.key=='M03'?'background:#eef4ff':''}"><td>${m.key}</td><td>${m.name}</td><td>${m.family}</td><td>${box[m.box]}</td>${cell(m,'random')}${cell(m,'new_lots')}${cell(m,'unseen')}${cell(m,'future')}
   <td>${m.tests.new_lots.r2.toFixed(2)}</td><td>${m.n_parameters??'-'}</td><td>${m.fit_s.toFixed(2)}</td><td>${m.predict_ms_per_unit.toFixed(2)}</td>
   <td>${t?t['caught_in_advance_%']+' %':'-'}</td><td>${t?(t.false_prediction_on_stable_line?'<b class="FAIL">yes</b>':'no'):'-'}</td></tr>`}).join('');
 document.getElementById('m-schemes').innerHTML='<b>random</b>: 5-fold random split · <b>new lots</b>: leave one bag lot + reel out · <b>unseen setting</b>: leave one pressure / port level / OD level out (extrapolation) · <b>future</b>: train on first 70 % of the run, test on last 30 %. Bold = best in column.';
 document.getElementById('m-expl').innerHTML='<tr><th>#</th><th>model</th><th>how it works</th><th>inside the model</th></tr>'+R.models.map(m=>{
  const d=m.detail;let ins='';
  if(d.coef)ins='intercept '+d.intercept+'; '+Object.entries(d.coef).map(([k,v])=>`${k} ${v>0?'+':''}${v}`).join(', ');
  else if(d.importance)ins='importance: '+Object.entries(d.importance).map(([k,v])=>`${k} ${v}`).join(', ');
  else if(d.rules)ins=`<details><summary>${d.rules.split('\n').length-1} lines of if-then rules</summary><pre style="font-size:10px">${d.rules}</pre></details>`;
  else if(d.cells)ins=Object.keys(d.cells).length+' table cells';
  return `<tr><td>${m.key}</td><td>${m.name}</td><td>${m.explanation}</td><td style="font-size:11px">${ins}</td></tr>`}).join('');
 const P=R.permutation_importance, cols=Object.keys(Object.values(P)[0]);
 document.getElementById('m-imp').innerHTML='<tr><th>model</th>'+cols.map(c=>`<th>${c.replace('_thou','').replace('_bar','')}</th>`).join('')+'</tr>'+
  Object.entries(P).map(([k,v])=>`<tr><td>${k}</td>${cols.map(c=>`<td>${v[c].toFixed(3)}</td>`).join('')}</tr>`).join('');
 document.getElementById('m-leak').innerHTML=`R² = ${R.leakage.r2_with_actuator} with actuator position alone. ${R.leakage.note}`;
 const p=R.models.find(m=>m.key=='M03');
 document.getElementById('m-prod').innerHTML=`depth_mm = ${p.detail.intercept} ${Object.entries(p.detail.coef).map(([k,v])=>`${v>0?'+':'−'} ${Math.abs(v)}·${k}`).join(' ')}<br>RMSE new lots ${p.tests.new_lots.rmse.toFixed(3)} mm · unseen ${p.tests.unseen.rmse.toFixed(3)} mm · ${p.n_parameters} parameters, each with a physical meaning.`;
 const g=R.models.find(m=>m.key=='M09'), nn=R.models.find(m=>m.key=='M13');
 document.getElementById('m-verdict').innerHTML=`<p><b>Keep the 4-coefficient linear model (M03) in production.</b> On settings it has already seen, the best machine-learning model (gradient boosting, ${g.tests.random.rmse.toFixed(3)} mm on a random split) is only
  ${(p.tests.random.rmse-g.tests.random.rmse).toFixed(3)} mm better than M03 (${p.tests.random.rmse.toFixed(3)} mm). The remaining ≈ 0.16 mm is part-to-part noise that no model can predict from material and pressure data.</p>
  <p>When a pressure or material level was <b>never seen in training</b>, tree models get ≈ 60 % worse, the lookup table can only return the average, and the neural network (${nn.tests.unseen.rmse.toFixed(1)} mm) and the
  extended linear models break down, while M03 (${p.tests.unseen.rmse.toFixed(3)} mm) and the Gaussian process stay accurate. Inside the digital twin every real model catches about the same share of failures.</p>
  <p>So machine learning adds validation effort (GAMP 5 category 5, model drift, explainability) without a measurable benefit here. The Gaussian process is the only credible alternative,
  because it also gives its own uncertainty.</p>`;
}
"""

PAGE = PAGE.replace("__ANALYSIS_HTML__", ANALYSIS_HTML + MODELS_HTML).replace("__ANALYSIS_JS__", ANALYSIS_JS + MODELS_JS + LAB_JS)
PAGE = PAGE.replace("""<button class="tab" id="b-ana" onclick="show('ana')">Data analysis</button>""",
                    """<button class="tab" id="b-ana" onclick="show('ana')">Data analysis</button><button class="tab" id="b-mod" onclick="show('mod')">Model comparison</button>""")
PAGE = PAGE.replace("""function show(t){cur=t;document.getElementById('tab-live').style.display=t=='live'?'':'none';
 document.getElementById('tab-ana').style.display=t=='ana'?'':'none';
 document.getElementById('b-live').className='tab'+(t=='live'?' on':'');document.getElementById('b-ana').className='tab'+(t=='ana'?' on':'');
 if(t=='ana'){loadAna();initLab();}}""", """function show(t){cur=t;for(const k of ['live','ana','mod']){document.getElementById('tab-'+k).style.display=t==k?'':'none';
 document.getElementById('b-'+k).className='tab'+(t==k?' on':'')}
 if(t=='ana'){loadAna();initLab();} if(t=='mod')loadMods(); if(t=='live')tick();}""")
# only fetch when something changed, and not while the browser tab is hidden
PAGE = PAGE.replace("""async function tick(){
 const s=await (await fetch('/api/state')).json();""", """let lastTip=-1;
async function tick(){
 if(document.hidden)return;
 const s=await (await fetch('/api/state?tip='+lastTip)).json();
 if(s.unchanged)return; lastTip=s.tip;""")


def _np(o):
    return o.item() if hasattr(o, "item") else str(o)


def _gz(obj):
    raw = obj if isinstance(obj, bytes) else json.dumps(obj, default=_np).encode()
    return gzip.compress(raw, 5)


class LiveState:
    """Incremental view of the ledger: only entries newer than the last request are processed."""

    def __init__(self, db, mode):
        self.db, self.mode, self.lock = db, mode, threading.Lock()
        self.led = Ledger(db, readonly=True)
        self.seq, self.tip, self.body = 0, None, None
        self.model, self.book, self.mons = None, MaterialBook(), {}
        self.pred, self.eval, self.alerts = {}, {}, []

    def current_tip(self):
        return self.led.db.execute("SELECT coalesce(max(seq), 0) FROM ledger").fetchone()[0]

    def get(self):
        with self.lock:
            tip = self.current_tip()
            if tip == self.tip:
                return tip, self.body
            for r in self.led.db.execute("SELECT * FROM ledger WHERE seq > ? ORDER BY seq", (self.seq,)):
                self._apply(dict(r))
                self.seq = r["seq"]
            ok, n, problem = self.led.verify()
            self.tip, self.body = tip, _gz(self._build(ok, n, problem, tip))
            return tip, self.body

    def _apply(self, r):
        kind = r["kind"]
        if kind == "MODEL":
            self.model = json.loads(r["payload"])
            return
        if r["mode"] != self.mode:
            return
        if kind == "BAG":
            self.book.add_bag(json.loads(r["payload"]))
        elif kind == "PVC":
            self.book.add_pvc(json.loads(r["payload"]))
        elif kind == "ASM" and self.model:
            mon = self.mons.setdefault(r["line_id"], LineMonitor(r["line_id"], self.model))
            mon.process(json.loads(r["payload"]), r["event_no"], self.book)
        elif kind == "PREDICTION":
            self.pred[r["line_id"]] = dict(json.loads(r["payload"]), entry_hash=r["entry_hash"], received_utc=r["received_utc"])
        elif kind == "EVALUATION":
            self.eval[r["line_id"]] = json.loads(r["payload"])
        elif kind == "ALERT":
            self.alerts.append(dict(json.loads(r["payload"]), line=r["line_id"], event_no=r["event_no"], utc=r["received_utc"]))

    def _build(self, ok, n, problem, tip):
        keys = ("event_no", "unit_id", "depth", "baseline", "resid", "cusum_pos", "cusum_neg", "nok", "follow_err",
                "vision_offset", "pressure")
        lines = {}
        for line in LINES:
            mon = self.mons.get(line)
            lines[line] = {"units": [{k: u[k] for k in keys} for u in mon.units] if mon else [],
                           "forecast": mon.last_forecast if mon else None,
                           "prediction": self.pred.get(line), "evaluation": self.eval.get(line)}
        mat = self.book.summary()
        m = self.model or {}
        return {"tip": tip, "mode": self.mode, "integrity": {"ok": ok, "entries": n, "problem": problem},
                "model": {k: m.get(k) for k in ("version", "formula", "coef", "sigma_mm", "r2")},
                "lines": lines, "alerts": self.alerts[-40:],
                "materials": {"bag": {k: round(v["mean"], 5) for k, v in mat["bag_lots"].items()},
                              "pvc": {k: round(v["mean"], 5) for k, v in mat["reels"].items()}}}


def state(db_path, mode):
    """Uncached full state (kept for tests)."""
    return json.loads(gzip.decompress(LiveState(db_path, mode).get()[1]))


_ANA_CACHE = {}


def analysis_cached(db, mode):
    with Ledger(db, readonly=True).db as c:
        tip = c.execute("SELECT max(seq) FROM ledger").fetchone()[0]
    if _ANA_CACHE.get("key") != (db, mode, tip):
        _ANA_CACHE.update(key=(db, mode, tip), value=_gz(analyze(db, mode)))
    return _ANA_CACHE["value"]


_STATIC = {}
_LAB = {}


def static(name, loader):
    if name not in _STATIC:
        _STATIC[name] = loader()
    return _STATIC[name]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        gz, ctype, cache = True, "application/json", "no-store"
        if url.path == "/api/state":
            tip, body = self.server.live.get()
            if parse_qs(url.query).get("tip", [""])[0] == str(tip):
                body = _gz({"unchanged": True, "tip": tip})
        elif url.path == "/api/analysis":
            body = analysis_cached(self.server.db, self.server.mode)
        elif url.path == "/api/lab/datasets":
            body = _gz({"datasets": lab.datasets(), "models": [{"key": k, "name": n} for k, n in lab.NAMES.items()]})
        elif url.path == "/api/lab":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            key = url.query
            if key not in _LAB:
                try:
                    keys = [k for k in q.get("models", "").split(",") if k in lab.NAMES][:8]
                    if not keys:
                        raise ValueError("choose at least one model")
                    fake = {"kind": q.get("fk", "axis"), "slope": float(q.get("fs", 0.01)), "start": int(q.get("fst", 150)),
                            "n": int(q.get("fn", 600)), "noise": float(q.get("fnoise", 0)), "seed": int(q.get("fseed", 1))}
                    _LAB[key] = _gz(lab.run(q["train"], q["test"], keys, fake))
                except Exception as e:  # report the reason to the page instead of a broken response
                    _LAB[key] = _gz({"error": f"{type(e).__name__}: {e}"})
                if len(_LAB) > 50:
                    _LAB.pop(next(iter(_LAB)))
            body = _LAB[key]
        elif url.path == "/api/models":
            if not os.path.exists("models_report.json"):
                return self.send_error(404, "run: python3 models.py")
            body = static("models", lambda: _gz(open("models_report.json", "rb").read()))
        elif url.path.startswith("/figures/") and re.fullmatch(r"/figures/[\w\-]+\.png", url.path) \
                and os.path.exists(url.path[1:]):
            body, ctype, gz, cache = open(url.path[1:], "rb").read(), "image/png", False, "max-age=3600"
        else:
            body, ctype, cache = static("page", lambda: _gz(PAGE.encode())), "text/html; charset=utf-8", "no-cache"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="REALTIME", choices=["HISTORICAL", "REALTIME", "SIM"])
    ap.add_argument("--db")
    ap.add_argument("--port", type=int, default=8050)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.db, srv.mode = a.db or f"ledger_{a.mode}.db", a.mode
    srv.live = LiveState(srv.db, a.mode)
    # warm the caches in the background so the first click on every tab is instant
    threading.Thread(target=lambda: (srv.live.get(), analysis_cached(srv.db, a.mode)), daemon=True).start()
    print(f"Dashboard: http://localhost:{a.port}  (ledger {srv.db}, read-only)")
    srv.serve_forever()
