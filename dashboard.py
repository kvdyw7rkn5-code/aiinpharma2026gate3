"""Read-only jury dashboard over the ledger.   python3 dashboard.py --mode REALTIME  ->  http://localhost:8050

The dashboard opens the ledger with SQLite mode=ro, so it can never change records (URS-DI-005).
All derived values shown are recomputed from the stored original records with the frozen model.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from analysis import analyze
from model import LineMonitor, MaterialBook
from store import Ledger

LINES = ("ASM-01", "ASM-02", "ASM-03")


def state(db_path, mode):
    led = Ledger(db_path, readonly=True)
    ok, n, problem = led.verify()
    models = led.rows(kind="MODEL")
    model = json.loads(models[-1]["payload"])
    book = MaterialBook()
    for r in led.rows(kind="BAG", mode=mode):
        book.add_bag(json.loads(r["payload"]))
    for r in led.rows(kind="PVC", mode=mode):
        book.add_pvc(json.loads(r["payload"]))
    lines = {}
    for line in LINES:
        mon = LineMonitor(line, model)
        for r in led.rows(kind="ASM", line_id=line, mode=mode):
            mon.process(json.loads(r["payload"]), r["event_no"], book)
        pred = led.rows(kind="PREDICTION", line_id=line, mode=mode)
        ev = led.rows(kind="EVALUATION", line_id=line, mode=mode)
        f = mon.last_forecast
        lines[line] = {
            "units": [{k: u[k] for k in ("event_no", "unit_id", "depth", "baseline", "resid", "cusum_pos",
                                         "cusum_neg", "nok", "follow_err", "vision_offset", "pressure")}
                      for u in mon.units],          # whole run
            "forecast": f,
            "prediction": dict(json.loads(pred[0]["payload"]), entry_hash=pred[0]["entry_hash"],
                               received_utc=pred[0]["received_utc"]) if pred else None,
            "evaluation": json.loads(ev[0]["payload"]) if ev else None,
        }
    alerts = [dict(json.loads(r["payload"]), line=r["line_id"], event_no=r["event_no"], utc=r["received_utc"])
              for r in led.rows(kind="ALERT", mode=mode)][-40:]
    mat = book.summary()
    return {"mode": mode, "integrity": {"ok": ok, "entries": n, "problem": problem},
            "model": {k: model[k] for k in ("version", "formula", "coef", "sigma_mm", "r2")},
            "lines": lines, "alerts": alerts,
            "materials": {"bag": {k: round(v["mean"], 5) for k, v in mat["bag_lots"].items()},
                          "pvc": {k: round(v["mean"], 5) for k, v in mat["reels"].items()}}}


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
.note{font-size:12px;color:#444;line-height:1.5}.sim{border:2px dashed #e0a100}
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
 if(t=='ana')loadAna();}
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

PAGE = PAGE.replace("__ANALYSIS_HTML__", ANALYSIS_HTML).replace("__ANALYSIS_JS__", ANALYSIS_JS)
_ANA_CACHE = {}


def _np(o):
    return o.item() if hasattr(o, "item") else str(o)


def analysis_cached(db, mode):
    with Ledger(db, readonly=True).db as c:
        tip = c.execute("SELECT max(seq) FROM ledger").fetchone()[0]
    if _ANA_CACHE.get("key") != (db, mode, tip):
        _ANA_CACHE.update(key=(db, mode, tip), value=json.dumps(analyze(db, mode), default=_np).encode())
    return _ANA_CACHE["value"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            body, ctype = json.dumps(state(self.server.db, self.server.mode), default=str).encode(), "application/json"
        elif self.path.startswith("/api/analysis"):
            body, ctype = analysis_cached(self.server.db, self.server.mode), "application/json"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
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
    print(f"Dashboard: http://localhost:{a.port}  (ledger {srv.db}, read-only)")
    srv.serve_forever()
