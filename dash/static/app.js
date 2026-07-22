const INST_COLOR = {A:'#58a6ff', B:'#bc8cff', LIVE:'#39d0d8'};
let lastGood = null;
const charts = {};

function fmtPnl(v){ if(v==null) return '—'; return (v>=0?'+':'')+v.toFixed(2); }
function ageLabel(s){ if(s==null) return 'Tick —'; return Math.round(s/60)+'m ago'; }
function setDot(id, kind){ const el=document.getElementById(id); if(el) el.className='dot '+kind; }

function renderHealth(h){
  for(const [name,i] of Object.entries(h.instances)){
    const dotKind = i.halted ? 'crit' : (name==='LIVE' && i.last_tick_age!=null && i.last_tick_age>300 ? 'warn' : 'ok');
    setDot('dot-'+name, dotKind);
  }
  const live = h.instances.LIVE || {};
  document.getElementById('stat-pnl').textContent = 'PnL '+(live.bankroll!=null?fmtPnl(live.bankroll-200):'—');
  document.getElementById('stat-bankroll').textContent = 'Bankroll '+(live.bankroll!=null?live.bankroll.toFixed(0):'—');
  document.getElementById('stat-gas').textContent = 'Gas '+(live.gas!=null?live.gas.toFixed(1):'—');
  document.getElementById('stat-age').textContent = ageLabel(live.last_tick_age);
}

function renderFeed(feed){
  const el=document.getElementById('feed-list'); if(!feed||!feed.rows) return;
  el.innerHTML = feed.rows.map(r=>{
    const tag=(r.note||'').split(':')[0] || r.action;
    const cls = (r.note||'').startsWith('skip')?'tag-skip':(r.note==='posted'?'tag-posted':(r.note==='rej'?'tag-rej':'tag-FILLED'));
    return `<div class="row"><span>${(r.ts||'').slice(11,19)}</span><span>${r.instance}</span><span>${r.action}</span><span class="tag ${cls}">${tag}</span></div>`;
  }).join('');
}

function renderPositions(p){
  const el=document.getElementById('positions-table'); if(!p||!p.rows) return;
  const rows=p.rows;
  el.innerHTML = `<table><thead><tr><th>Inst</th><th>Market</th><th>Side</th><th>Px</th><th>Size</th><th>Status</th></tr></thead><tbody>${
    rows.map(r=>`<tr><td>${r.instance}</td><td>${r.market_id||''}</td><td>${r.side||''}</td><td>${r.price!=null?r.price.toFixed(3):'—'}</td><td>${r.size!=null?r.size.toFixed(0):'—'}</td><td>${r.status||''}</td></tr>`).join('')
  }</tbody></table>`;
}

function renderRisk(r){
  const el=document.getElementById('risk-gauges'); if(!r) return;
  const bar=(label,val,max,frac)=>{
    const pct = max? Math.min(100, Math.abs(val)/max*100):0;
    const cls = val>=max*frac ? 'crit':'warn';
    return `<div class="gauge"><span>${label}</span><div class="bar"><div class="fill" style="width:${pct}%"></div></div><span>${val}/${max}</span></div>`;
  };
  el.innerHTML = bar('Open',r.open_positions,r.max_open,1)+bar('Consec loss',r.consec_loss,r.max_consec,1)
    +`<div class="gauge"><span>Daily loss</span><div class="bar"><div class="fill" style="width:${Math.min(100,Math.abs(r.daily_loss)/r.daily_loss_halt*100)}%"></div></div><span>${r.daily_loss}/${r.daily_loss_halt}</span></div>`
    +`<div class="gauge"><span>HALT</span><span style="color:${r.halted?'var(--red)':'var(--green)'}">${r.halted?'HALTED':'ok'}</span></div>`;
}

async function getJSON(url){
  try{
    const r=await fetch(url,{cache:'no-store'});
    if(!r.ok) throw 0;
    return await r.json();
  }catch(e){
    document.getElementById('stat-reconnect').classList.remove('hidden');
    return null;
  }
}

function checkStale(ts){
  if(!ts) return;
  const age=(Date.now()-new Date(ts).getTime())/1000;
  document.getElementById('stat-stale').classList.toggle('hidden', age<=20);
}

async function pollState(){
  const s=await getJSON('/api/state');
  if(!s) return;
  document.getElementById('stat-reconnect').classList.add('hidden');
  lastGood=s; checkStale(s.ts);
  if(s.health) renderHealth(s.health);
  if(s.feed) renderFeed(s.feed);
  if(s.positions) renderPositions(s.positions);
  if(s.risk) renderRisk(s.risk);
}

function lineChart(id, datasets, labels){
  const ctx=document.getElementById(id); if(!ctx) return;
  if(charts[id]) charts[id].destroy();
  charts[id]=new Chart(ctx,{type:'line',data:{labels,datasets},
    options:{animation:false,plugins:{legend:{labels:{color:'#7d8794',font:{size:10}}}},
      scales:{x:{ticks:{color:'#525c6b',font:{size:9}},grid:{color:'#222b3a'}},
              y:{ticks:{color:'#525c6b',font:{size:9}},grid:{color:'#222b3a'}}}}});
}

function barChart(id, config){
  const ctx=document.getElementById(id); if(!ctx) return;
  if(charts[id]) charts[id].destroy();
  charts[id]=new Chart(ctx, config);
}

async function pollCharts(){
  const [eq,dp,ep,wr,rj,ed,cal,sb] = await Promise.all([
    getJSON('/api/equity?days=30'),getJSON('/api/daily-pnl?days=30'),
    getJSON('/api/edge-pnl'),getJSON('/api/winrate'),
    getJSON('/api/rejections?hours=24'),getJSON('/api/edge-dist?days=7'),
    getJSON('/api/calib'),getJSON('/api/station-bias?days=30')]);
  if(eq) lineChart('chart-equity',
    Object.entries(eq.series).map(([n,pts])=>({label:n,data:pts.map(p=>p.cum),borderColor:INST_COLOR[n],borderWidth:1.5,pointRadius:0})),
    (eq.series.A||eq.series.B||[]).map(p=>p.day));
  if(dp) lineChart('chart-daily-pnl',
    Object.entries(dp.series).map(([n,pts])=>({label:n,data:pts.map(p=>p.pnl),borderColor:INST_COLOR[n],borderWidth:1.5,pointRadius:0})),
    (dp.series.A||[]).map(p=>p.day));
  if(ep){ const labels=ep.edges.map(e=>e.edge);
    barChart('chart-edge-pnl',
      {type:'bar',data:{labels,datasets:[{data:ep.edges.map(e=>e.pnl),backgroundColor:ep.edges.map(e=>e.pnl>=0?'#3fb950':'#f85149')}]},
       options:{plugins:{legend:{display:false}}}}); }
  if(wr){ const labels=wr.edges.map(e=>e.edge);
    barChart('chart-winrate',{type:'bar',
      data:{labels,datasets:[{data:wr.edges.map(e=>e.rate!=null?e.rate*100:0),backgroundColor:'#8b5cf6'}]},
      options:{plugins:{legend:{display:false}},scales:{y:{max:100}}}}); }
  if(rj){ const labels=rj.rows.map(r=>r.reason);
    barChart('chart-rejections',{type:'bar',
      data:{labels,datasets:[{data:rj.rows.map(r=>r.count),backgroundColor:'#d29922'}]},
      options:{plugins:{legend:{display:false}},indexAxis:'y'}}); }
  if(ed){ barChart('chart-edge-dist',{type:'bar',
      data:{labels:ed.buckets.map(b=>b.bucket),datasets:[{data:ed.buckets.map(b=>b.count),backgroundColor:'#58a6ff'}]},
      options:{plugins:{legend:{display:false}}}}); }
  if(cal&&cal.series){ const by={};
    cal.series.forEach(r=>{(by[r.edge]=by[r.edge]||[]).push(r)});
    lineChart('chart-calib',Object.entries(by).map(([e,pts])=>({label:e+'_model',data:pts.map(p=>p.brier_model),borderColor:'#58a6ff',pointRadius:0})
      ).concat(Object.entries(by).map(([e,pts])=>({label:e+'_mkt',data:pts.map(p=>p.brier_market),borderColor:'#f85149',pointRadius:0}))),
      [...new Set(cal.series.map(p=>p.ts))]);
  }
  if(sb&&sb.cities){ lineChart('chart-station-bias',
      sb.cities.map(c=>({label:c.city,data:c.points.map(p=>p.residual),borderColor:INST_COLOR.A,pointRadius:0,borderWidth:1})),
      (sb.cities[0]?.points||[]).map(p=>p.ts)); }
}

pollState(); pollCharts();
setInterval(pollState, 10000);
setInterval(pollCharts, 30000);
