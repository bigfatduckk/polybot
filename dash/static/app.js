const INST_COLOR = {A:'#58a6ff', B:'#bc8cff', LIVE:'#39d0d8'};
let lastGood = null;
const charts = {};
let lastOkAt = Date.now();

function fmtPnl(v){ if(v==null) return '—'; return (v>=0?'+':'')+v.toFixed(2); }
function ageLabel(s){ if(s==null) return 'Tick —'; return Math.round(s/60)+'m ago'; }
function setDot(id, kind){ const el=document.getElementById(id); if(el) el.className='dot '+kind; }

// HKT = UTC+8. All displayed times are Hong Kong time.
function toHKT(iso, withDate){
  if(!iso) return '—';
  let d = new Date(iso);
  if(isNaN(d)) return iso;
  d = new Date(d.getTime()+8*3600*1000);
  const hh = String(d.getUTCHours()).padStart(2,'0');
  const mm = String(d.getUTCMinutes()).padStart(2,'0');
  const ss = String(d.getUTCSeconds()).padStart(2,'0');
  if(withDate){
    const mo = String(d.getUTCMonth()+1).padStart(2,'0');
    const dd = String(d.getUTCDate()).padStart(2,'0');
    return `${mo}-${dd} ${hh}:${mm}`;
  }
  return `${hh}:${mm}:${ss}`;
}
// ISO date "2026-07-24" -> "07-24" for compact HKT-day axis labels.
function md(day){ if(!day) return ''; return day.slice(5); }

function renderHealth(h){
  for(const [name,i] of Object.entries(h.instances)){
    const dotKind = (i.halted || i.status === 'unreachable') ? 'crit' : (name==='LIVE' && i.last_tick_age!=null && i.last_tick_age>300 ? 'warn' : 'ok');
    setDot('dot-'+name, dotKind);
  }
  const live = h.instances.LIVE || {};
  const pnlEl = document.getElementById('stat-pnl');
  const pnl = live.realized_pnl;
  if(pnl!=null){
    pnlEl.textContent = 'PnL '+fmtPnl(pnl)+(live.settled?` (${live.settled})`:'');
    pnlEl.className = 'stat '+(pnl>=0?'pos-pnl-pos':'pos-pnl-neg');
  } else {
    pnlEl.textContent = 'PnL —';
    pnlEl.className = 'stat';
  }
  document.getElementById('stat-bankroll').textContent = 'Cash '+(live.bankroll!=null?'$'+Number(live.bankroll).toFixed(2):'—');
  document.getElementById('stat-gas').textContent = 'Gas '+(live.gas!=null?Number(live.gas).toFixed(1):'—')+' POL';
  document.getElementById('stat-age').textContent = ageLabel(live.last_tick_age);
}

function renderFeed(feed){
  const el=document.getElementById('feed-list'); if(!feed||!feed.rows) return;
  el.innerHTML = feed.rows.map(r=>{
    const tag=(r.note||'').split(':')[0] || r.action;
    const cls = (r.note||'').startsWith('skip')?'tag-skip':(r.note==='posted'?'tag-posted':(r.note==='rej'?'tag-rej':'tag-FILLED'));
    return `<div class="row"><span>${toHKT(r.ts)}</span><span>${r.instance}</span><span>${r.action}</span><span class="tag ${cls}">${tag}</span></div>`;
  }).join('');
}

function renderPositions(p){
  const el=document.getElementById('positions-table'); if(!p||!p.rows) return;
  const rows=p.rows;
  const sideCell=(s)=>{
    if(!s) return '—';
    const up = String(s).toUpperCase();
    const cls = up==='YES'?'pos-yes':(up==='NO'?'pos-no':'');
    return `<span class="${cls}">${s}</span>`;
  };
  el.innerHTML = `<table><thead><tr><th>Inst</th><th>Trade</th><th>Side</th><th>Date</th><th>Px</th><th>Size</th><th>Status</th></tr></thead><tbody>${
    rows.map(r=>`<tr><td>${r.instance}</td><td class="desc">${r.desc||r.market_id||''}</td><td>${sideCell(r.side)}</td><td>${r.date||'—'}</td><td>${r.price!=null?r.price.toFixed(3):'—'}</td><td>${r.size!=null?r.size.toFixed(0):'—'}</td><td>${r.status||''}</td></tr>`).join('')
  }</tbody></table>`;
}

function renderCandidates(c){
  const el=document.getElementById('candidates-table'); if(!c||!c.rows) return;
  const rows=c.rows;
  el.innerHTML = `<table><thead><tr><th>Inst</th><th>Edge</th><th>Market</th><th>Side</th><th>p</th><th>edge</th><th>→</th></tr></thead><tbody>${
    rows.map(r=>`<tr><td>${r.instance}</td><td>${r.edge||'—'}</td><td>${r.market_id||''}</td><td>${r.side||''}</td><td>${r.p_model!=null?(+r.p_model).toFixed(2):'—'}</td><td>${r.edge_val!=null?(+r.edge_val).toFixed(3):'—'}</td><td style="color:${r.became_order?'var(--green)':'var(--dim)'}">${r.became_order?'Y':'·'}</td></tr>`).join('') || '<tr><td colspan="7">no candidates</td></tr>'
  }</tbody></table>`;
}

function renderRisk(r){
  const el=document.getElementById('risk-gauges'); if(!r) return;
  const bar=(label,val,max,frac)=>{
    const pct = max? Math.min(100, Math.abs(val)/max*100):0;
    const cls = val>=max*frac ? 'crit':'warn';
    const bg = val>=max*frac ? 'var(--red)' : 'var(--amber)';
    return `<div class="gauge"><span>${label}</span><div class="bar"><div class="fill ${cls}" style="width:${pct}%;background:${bg}"></div></div><span>${val}/${max}</span></div>`;
  };
  el.innerHTML = `<div class="gauge"><span>Open</span><div class="bar"></div><span>${r.open_positions}</span></div>`+bar('Consec loss',r.consec_loss,r.max_consec,1)
    +`<div class="gauge"><span>Daily loss</span><div class="bar"><div class="fill" style="width:${r.daily_loss_halt==null?0:Math.min(100,Math.abs(r.daily_loss)/r.daily_loss_halt*100)}%"></div></div><span>${r.daily_loss}/${r.daily_loss_halt==null?'—':r.daily_loss_halt}</span></div>`
    +`<div class="gauge"><span>Bankroll</span><div class="bar"></div><span>${r.bankroll==null?'stale':'$'+r.bankroll.toFixed(0)}</span></div>`
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

function markErr(cardId, contentId){
  const card=document.getElementById(cardId); if(!card) return;
  card.classList.add('err');
  if(contentId){
    if(charts[contentId]){ charts[contentId].destroy(); delete charts[contentId]; }
    const c=document.getElementById(contentId);
    if(c){
      if(c.tagName==='CANVAS'){
        let m=card.querySelector('.err-msg');
        if(!m){ m=document.createElement('span'); m.className='err-msg'; m.textContent='—'; card.appendChild(m); }
      }else{
        c.textContent='—';
      }
    }
  }
}

function clearErr(cardId){
  const card=document.getElementById(cardId); if(!card) return;
  card.classList.remove('err');
  const m=card.querySelector('.err-msg'); if(m) m.remove();
}

function checkStale(){
  const stale = (Date.now()-lastOkAt)/1000 > 20;
  document.getElementById('stat-stale').classList.toggle('hidden', !stale);
}

async function pollState(){
  const s=await getJSON('/api/state');
  if(!s) return;
  document.getElementById('stat-reconnect').classList.add('hidden');
  lastOkAt=Date.now(); checkStale();
  if(s.health) renderHealth(s.health);
  if(s.feed){
    if(s.feed.error) markErr('card-feed','feed-list');
    else { clearErr('card-feed'); renderFeed(s.feed); }
  }
  if(s.positions){
    if(s.positions.error) markErr('card-positions','positions-table');
    else { clearErr('card-positions'); renderPositions(s.positions); }
  }
  if(s.risk){
    if(s.risk.error) markErr('card-risk','risk-gauges');
    else { clearErr('card-risk'); renderRisk(s.risk); }
  }
}

// Shared options: fixed-height box (maintainAspectRatio:false) + index hover so
// the whole column highlights and the tooltip shows every series at that point.
function baseOpts(extra){
  const o = {
    animation:false,
    responsive:true,
    maintainAspectRatio:false,
    interaction:{intersect:false, mode:'index'},
    plugins:{
      legend:{labels:{color:'#7d8794',font:{size:10}}, onClick:Chart.defaults.plugins.legend.onClick},
      tooltip:{backgroundColor:'#0b1117',titleColor:'#e6edf3',bodyColor:'#7d8794',
               borderColor:'#222b3a',borderWidth:1,padding:8}
    },
    scales:{
      x:{ticks:{color:'#525c6b',font:{size:9},maxRotation:0,autoSkip:true},grid:{color:'#222b3a'}},
      y:{ticks:{color:'#525c6b',font:{size:9}},grid:{color:'#222b3a'}}
    }
  };
  return Object.assign({}, o, extra||{});
}

function lineChart(id, datasets, labels){
  const ctx=document.getElementById(id); if(!ctx) return;
  if(charts[id]) charts[id].destroy();
  charts[id]=new Chart(ctx,{type:'line',data:{labels,datasets},
    options:baseOpts()});
}

// Draw the value above each bar so win rate / pnl bars are readable even when tiny.
const barValuePlugin = {
  id:'barValue',
  afterDatasetsDraw(chart){
    const {ctx} = chart;
    chart.data.datasets.forEach((ds,di)=>{
      const meta = chart.getDatasetMeta(di);
      for(const bar of meta.data){
        const v = ds.data[bar.index];
        if(v==null) continue;
        ctx.save();
        ctx.fillStyle = '#7d8794';
        ctx.font = '10px "SFMono-Regular",Consolas,monospace';
        ctx.textAlign='center';
        const lbl = ds.labelFmt ? ds.labelFmt(v, bar.index) : String(v);
        ctx.fillText(lbl, bar.x, bar.y - 4);
        ctx.restore();
      }
    });
  }
};

function barChart(id, config){
  const ctx=document.getElementById(id); if(!ctx) return;
  if(charts[id]) charts[id].destroy();
  const cfg = Object.assign({}, config, {options:baseOpts(config.options||{})});
  // keep per-dataset labelFmt for the value plugin
  cfg.plugins = (cfg.plugins||[]).concat(barValuePlugin);
  charts[id]=new Chart(ctx, cfg);
}

// Build a unified day axis (sorted union of all instances' days) and align each
// series to it with null where the instance has no point — fixes the bug where
// B/LIVE were plotted at A's first-day label by index.
function unifiedAxis(series, key, carryForward){
  const days = [...new Set([].concat(...Object.values(series).map(pts=>(pts||[]).map(p=>p.day))))].sort();
  const datasets = Object.entries(series).map(([n,pts])=>{
    const by = {}; (pts||[]).forEach(p=>by[p.day]=p[key]);
    let last = null;
    const data = days.map(d=>{
      if(d in by){ last = by[d]; return by[d]; }
      return carryForward ? last : null;
    });
    return {label:n, data, borderColor:INST_COLOR[n]||'#888',
            borderWidth:1.5, pointRadius:0, spanGaps:true, tension:0.1};
  });
  return {labels: days.map(md), datasets};
}

async function pollCharts(){
  const [eq,dp,ep,wr,rj,ed,cal,sb,cd] = await Promise.all([
    getJSON('/api/equity?days=30'),getJSON('/api/daily-pnl?days=30'),
    getJSON('/api/edge-pnl'),getJSON('/api/winrate'),
    getJSON('/api/rejections?hours=24'),getJSON('/api/edge-dist?days=7'),
    getJSON('/api/calib'),getJSON('/api/station-bias?days=30'),
    getJSON('/api/candidates?limit=50')]);
  if(eq && eq.error) markErr('card-equity','chart-equity');
  else if(eq){ clearErr('card-equity');
    const a = unifiedAxis(eq.series,'cum',true);
    lineChart('chart-equity', a.datasets, a.labels); }
  if(dp && dp.error) markErr('card-daily-pnl','chart-daily-pnl');
  else if(dp){ clearErr('card-daily-pnl');
    const a = unifiedAxis(dp.series,'pnl',false);
    lineChart('chart-daily-pnl', a.datasets, a.labels); }
  if(ep && ep.error) markErr('card-edge-pnl','chart-edge-pnl');
  else if(ep){ clearErr('card-edge-pnl'); const labels=ep.edges.map(e=>e.edge);
    barChart('chart-edge-pnl',
      {type:'bar',data:{labels,datasets:[{data:ep.edges.map(e=>e.pnl),
        backgroundColor:ep.edges.map(e=>e.pnl>=0?'#3fb950':'#f85149'),
        labelFmt:v=>fmtPnl(v)}]},
       options:{plugins:{legend:{display:false}}}}); }
  if(wr && wr.error) markErr('card-winrate','chart-winrate');
  else if(wr){ clearErr('card-winrate'); const labels=wr.edges.map(e=>e.edge);
    barChart('chart-winrate',{type:'bar',
      data:{labels,datasets:[{data:wr.edges.map(e=>e.rate!=null?e.rate*100:null),
        backgroundColor:'#8b5cf6',
        labelFmt:(v,i)=>{ const e=wr.edges[i]; return e&&e.total?Math.round(v)+'% ('+e.won+'/'+e.total+')':Math.round(v)+'%'; }}]},
      options:{plugins:{legend:{display:false}},scales:{y:{min:0,max:100,ticks:{stepSize:10,callback:v=>v+'%'}}}}}); }
  if(rj && rj.error) markErr('card-rejections','chart-rejections');
  else if(rj){ clearErr('card-rejections'); const labels=rj.rows.map(r=>r.reason);
    barChart('chart-rejections',{type:'bar',
      data:{labels,datasets:[{data:rj.rows.map(r=>r.count),backgroundColor:'#d29922',labelFmt:v=>v}]},
      options:{plugins:{legend:{display:false}},indexAxis:'y'}}); }
  if(ed && ed.error) markErr('card-edge-dist','chart-edge-dist');
  else if(ed){ clearErr('card-edge-dist'); barChart('chart-edge-dist',{type:'bar',
      data:{labels:ed.buckets.map(b=>b.bucket),datasets:[{data:ed.buckets.map(b=>b.count),backgroundColor:'#58a6ff',labelFmt:v=>v}]},
      options:{plugins:{legend:{display:false}}}}); }
  if(cal && cal.error) markErr('card-calib','chart-calib');
  else if(cal&&cal.series){ clearErr('card-calib'); const by={};
    cal.series.forEach(r=>{(by[r.edge]=by[r.edge]||[]).push(r)});
    const labels=[...new Set(cal.series.map(p=>p.ts))].map(t=>toHKT(t,true));
    lineChart('chart-calib',Object.entries(by).map(([e,pts])=>({label:e+'_model',data:pts.map(p=>p.brier_model),borderColor:'#58a6ff',pointRadius:0})
      ).concat(Object.entries(by).map(([e,pts])=>({label:e+'_mkt',data:pts.map(p=>p.brier_market),borderColor:'#f85149',pointRadius:0}))),
      labels);
  }
  if(sb && sb.error) markErr('card-station-bias','chart-station-bias');
  else if(sb&&sb.cities){ clearErr('card-station-bias'); lineChart('chart-station-bias',
      sb.cities.map(c=>({label:c.city,data:c.points.map(p=>p.residual),borderColor:INST_COLOR.A,pointRadius:0,borderWidth:1})),
      (sb.cities[0]?.points||[]).map(p=>toHKT(p.ts,true))); }
  if(cd && cd.error) markErr('card-candidates','candidates-table');
  else if(cd){ clearErr('card-candidates'); renderCandidates(cd); }
}

pollState(); pollCharts();
setInterval(pollState, 10000);
setInterval(pollCharts, 30000);
setInterval(checkStale, 1000);
