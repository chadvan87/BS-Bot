const $=s=>document.querySelector(s); let state={signals:[],posts:[],config:{}};
async function api(url,opt={}){const r=await fetch(url,opt);let d;try{d=await r.json()}catch{d={detail:await r.text()}}if(!r.ok)throw new Error(d.detail||JSON.stringify(d));return d}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function setLog(text){const el=$('#setupResult');el.style.display='block';el.textContent=text}
function setGenerateStatus(text, tone=''){
  const el=$('#generateStatus');
  if(!el)return;
  el.textContent=text;
  el.className=`generate-status ${tone}`;
}
function currentTheme(){const t=document.documentElement.getAttribute('data-theme');return t||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light')}
function applyTheme(t){document.documentElement.setAttribute('data-theme',t);localStorage.setItem('theme',t);$('#themeToggle').textContent=t==='dark'?'☀️':'🌙'}
applyTheme(currentTheme());
$('#themeToggle').onclick=()=>applyTheme(currentTheme()==='dark'?'light':'dark');
async function load(){state=await api('/api/state');render()}
const STATUS_LABEL={NEW:'new',ENTRY_TRIGGERED:'entry hit',TP1_HIT:'TP1 hit',TP2_HIT:'TP2 hit',TP3_HIT:'TP3 hit',SL_HIT:'SL hit',INVALIDATED:'invalidated',EXPIRED:'expired',AMBIGUOUS:'unclear'};
function render(){
  const c=state.config;
  $('#mode').value=c.publish_mode||'mock';
  $('#key').placeholder=c.masked_square_key||'Paste Binance Square OpenAPI key';
  $('#health').innerHTML=`<span class="pill ${c.square_skill_available?'ok':'bad'}">${c.square_skill_available?'● Skill ready':'● Skill missing'}</span><span class="pill ${c.has_square_key?'ok':'bad'}">${c.has_square_key?'● Key '+c.masked_square_key:'● Key missing'}</span>`;

  $('#signals').innerHTML = state.signals.length ? `<div class="signal-list">${state.signals.map(s=>`<div class="signal">
    <div class="signal-top">
      <input class="pickSignal" type="checkbox" value="${s.id}">
      <span class="side ${s.side==='LONG'?'long':'short'}">${esc(s.side)}</span>
      <span class="signal-symbol">${esc(s.symbol)}</span>
      <span class="pill">${STATUS_LABEL[s.status]||esc(s.status||'new')}</span>
      <span class="signal-score">score <b>${s.score}</b></span>
    </div>
    <div class="stat-grid">
      <div><span class="stat-label">Entry</span><span class="stat-value">${s.entry_low}–${s.entry_high}</span></div>
      <div><span class="stat-label">Stop loss</span><span class="stat-value sl">${s.stop_loss}</span></div>
      <div><span class="stat-label">Targets</span><span class="stat-value tp">${s.tp1} / ${s.tp2} / ${s.tp3}</span></div>
    </div>
    <div class="actions">
      <button class="primary" onclick="gen(${s.id})">📝 Generate post</button>
      <button onclick="refreshStatus(${s.id},'${s.symbol}')">🔄 Refresh status</button>
      <button class="danger" onclick="delSignal(${s.id})">Delete</button>
    </div>
  </div>`).join('')}</div>` : '<div class="empty">No signals yet — run a scan above.</div>';

  $('#posts').innerHTML = state.posts.length ? `<div class="draft-list">${state.posts.map(p=>`<div class="draft">
    <div class="draft-head">
      <input class="pick" type="checkbox" value="${p.id}">
      <span class="id">#${p.id}</span>
      <span>${esc(p.status)}</span>
      ${p.template_name?`<span class="pill accent">${esc(p.template_name)}</span>`:''}
      ${p.scheduled_at?`<span class="pill">${esc(p.scheduled_at)}</span>`:''}
      ${p.delete_after_publish?'<span class="pill accent">🔥 auto-queue</span>':''}
    </div>
    <div class="draft-body">
      ${p.chart_path?`<img class="chart" src="/charts/${esc(p.chart_path.split('/').pop())}" alt="chart">`:''}
      <textarea id="p${p.id}">${esc(p.content)}</textarea>
      <div class="preview" id="prev${p.id}" style="display:none">${esc(p.content)}</div>
      <div class="actions">
        <button onclick="savePost(${p.id})">💾 Save edit</button>
        <button onclick="anotherStyle(${p.id})">🔁 Another style</button>
        <button onclick="togglePreview(${p.id})">👁️ Preview</button>
        <button class="primary" onclick="publishPost(${p.id})">🚀 Post now</button>
        <button onclick="copyPost(${p.id})">📋 Copy</button>
        <button class="danger" onclick="delPost(${p.id})">Delete</button>
      </div>
      ${p.publish_result?`<span class="result-msg">${esc(p.publish_result)}</span>`:''}
    </div>
  </div>`).join('')}</div>` : '<div class="empty">No drafts yet — generate a post from a signal above.</div>';
}
$('#settings').onsubmit=async e=>{e.preventDefault();try{const d=await api('/api/settings',{method:'POST',body:new FormData(e.target)});setLog('Saved. '+JSON.stringify(d.config,null,2));$('#key').value='';await load()}catch(x){alert(x.message)}};
$('#install').onclick=async()=>{if(!confirm('Install only Binance official square-post skill from binance/binance-skills-hub?'))return;setLog('Installing...');try{setLog(JSON.stringify(await api('/api/setup-square-skill',{method:'POST'}),null,2));await load()}catch(x){setLog(x.message)}};
$('#test').onclick=async()=>{try{setLog(JSON.stringify(await api('/api/test-square',{method:'POST'}),null,2))}catch(x){setLog(x.message)}};
$('#clear').onclick=async()=>{if(confirm('Remove saved API key?')){await api('/api/settings/key',{method:'DELETE'});await load()}};
// Market data is fetched from the browser (Binance allows CORS) so scans don't depend on the server's IP,
// which Binance may rate-limit or ban. Symbols that fail here are fetched by the server as a fallback.
const FAPI='https://fapi.binance.com'
async function browserKlines(symbols,interval){const out={};let i=0
const worker=async()=>{while(i<symbols.length){const sym=symbols[i++];try{const r=await fetch(`${FAPI}/fapi/v1/klines?symbol=${sym}&interval=${interval}&limit=220`);if(r.ok)out[sym]=await r.json()}catch{}}}
await Promise.all(Array.from({length:5},worker));return out}
async function shareKlines(symbols,interval){const klines=await browserKlines(symbols,interval)
await api('/api/klines',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval,klines})})}
async function shareSignalKlines(ids){const byTf={};state.signals.filter(s=>ids.includes(s.id)).forEach(s=>(byTf[s.timeframe]??=new Set()).add(s.symbol))
await Promise.all(Object.entries(byTf).map(([tf,syms])=>shareKlines([...syms],tf)))}
function setScanStatus(text, tone=''){const el=$('#scanStatus');el.hidden=!text;el.textContent=text;el.className=`generate-status ${tone}`}
$('#scan').onclick=async()=>{const btn=$('#scan');btn.disabled=true;setScanStatus('Scanning…')
try{const symbols=[...new Set($('#symbols').value.split(',').map(x=>x.trim().toUpperCase().replace('/','')).filter(Boolean))],interval=$('#interval').value
const klines=await browserKlines(symbols,interval)
const r=await api('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbols,interval,bullish_only:$('#bullish').checked,klines})});await load()
const msg=`${r.results.length} signal${r.results.length===1?'':'s'} added.`
setScanStatus(r.errors.length?`${msg} ${r.errors.length} failed — ${r.errors.join(' | ')}`:msg,r.errors.length?'error':'success')}
catch(x){setScanStatus(`Scan failed: ${x.message}`,'error')}finally{btn.disabled=false}};
window.refreshStatus=async(id,symbol)=>{try{let price=null,klines=null;const sig=state.signals.find(s=>s.id===id)
try{const r=await fetch(`${FAPI}/fapi/v1/ticker/price?symbol=${symbol}`);if(r.ok)price=+(await r.json()).price}catch{}
try{if(sig){const r=await fetch(`${FAPI}/fapi/v1/klines?symbol=${symbol}&interval=${sig.timeframe}&limit=200`);if(r.ok)klines=await r.json()}}catch{}
await api(`/api/signals/${id}/refresh-status`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({price,klines})});await load()}catch(x){alert(x.message)}};
window.delSignal=async id=>{if(confirm('Delete this signal?')){try{await api('/api/signals/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signal_ids:[id]})});await load()}catch(x){alert(x.message)}}};
$('#deleteSelectedSignals').onclick=async()=>{const ids=[...document.querySelectorAll('.pickSignal:checked')].map(x=>+x.value);if(!ids.length)return alert('Select signals to delete');if(!confirm(`Delete ${ids.length} selected signal(s)?`))return;try{await api('/api/signals/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signal_ids:ids})});await load()}catch(x){alert(x.message)}};
$('#deleteAllSignals').onclick=async()=>{if(!state.signals.length)return alert('No signals to delete');if(!confirm(`Delete all ${state.signals.length} signal(s)?`))return;try{await api('/api/signals/delete-all',{method:'POST'});await load()}catch(x){alert(x.message)}};
window.gen=async id=>{
  const tpl=$('#templateSel').value;
  try{await shareSignalKlines([id]);await shareKlines(['BTCUSDT'],'1h');
    if(tpl==='btc_market_analysis'){await api('/api/generate-btc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({template_name:tpl})})}
    else if(tpl==='top_10_signals'){await api('/api/generate-top10',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})}
    else{await api('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signal_id:id,template_name:tpl})})}
    await load()
  }catch(x){alert(x.message)}
};
async function generateDrafts(button, ids, label, endpoint='/api/generate-bulk'){
  if(!ids.length)return alert('Scan for signals first');
  button.disabled=true;
  try{await shareSignalKlines(ids);await shareKlines(['BTCUSDT'],'1h')}catch{}
  button.textContent=`⏳ Generating ${ids.length} draft${ids.length===1?'':'s'}...`;
  setGenerateStatus(`Generating ${ids.length} draft${ids.length===1?'':'s'} — charts can take a moment.`);
  try{
    const d=await api(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signal_ids:ids,template_name:$('#templateSel').value})});
    const created=d.results.length;
    if(d.errors.length){
      setGenerateStatus(`Created ${created} draft${created===1?'':'s'}; ${d.errors.length} failed.`, 'error');
      alert('Some failed:\n'+d.errors.join('\n'));
    }else setGenerateStatus(`Created ${created} draft${created===1?'':'s'}.`, 'success');
    await load();
  }catch(x){setGenerateStatus(`Could not create drafts: ${x.message}`, 'error');alert(x.message)}finally{button.disabled=false;button.textContent=label}
}
$('#genBulk').onclick=()=>generateDrafts($('#genBulk'),state.signals.slice(0,10).map(s=>s.id),'⚡ Generate 10 posts');
$('#genAll').onclick=()=>generateDrafts($('#genAll'),state.signals.map(s=>s.id),'📝 Generate drafts for all signals','/api/generate-all');
$('#genBtc').onclick=async()=>{$('#genBtc').disabled=true;try{await shareKlines(['BTCUSDT'],'1h');await api('/api/generate-btc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({template_name:'btc_market_analysis'})});await load()}catch(x){alert(x.message)}finally{$('#genBtc').disabled=false}};
$('#genTop10').onclick=async()=>{$('#genTop10').disabled=true;try{await shareKlines(['BTCUSDT'],'1h');await api('/api/generate-top10',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});await load()}catch(x){alert(x.message)}finally{$('#genTop10').disabled=false}};
window.savePost=async id=>{await api(`/api/posts/${id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:$(`#p${id}`).value})});await load()};
window.anotherStyle=async id=>{try{await api(`/api/posts/${id}/regenerate`,{method:'POST'});await load()}catch(x){alert(x.message)}};
window.togglePreview=id=>{const ta=$(`#p${id}`),pv=$(`#prev${id}`);const show=pv.style.display==='none';pv.style.display=show?'block':'none';ta.style.display=show?'none':'block'};
window.publishPost=async id=>{if(!confirm('Publish this post now?'))return;try{alert((await api(`/api/posts/${id}/publish`,{method:'POST'})).result);await load()}catch(x){alert(x.message)}};
window.copyPost=async id=>{await navigator.clipboard.writeText($(`#p${id}`).value);alert('Copied')};
window.delPost=async id=>{if(confirm('Delete draft?')){await api(`/api/posts/${id}/delete`,{method:'POST'});await load()}};
$('#schedule').onclick=async()=>{const ids=[...document.querySelectorAll('.pick:checked')].map(x=>+x.value);if(!ids.length)return alert('Select posts');const local=$('#startAt').value;if(!local)return alert('Choose start time');try{await api('/api/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({post_ids:ids,start_at:local,spacing_minutes:+$('#spacing').value})});await load()}catch(x){alert(x.message)}};
$('#autoQueue').onclick=async()=>{
  const ids=[...document.querySelectorAll('.pick:checked')].map(x=>+x.value);
  const spacing=+$('#autoQueueSpacing').value;
  if(!ids.length)return alert('Tick the drafts you want in the queue first');
  if(!confirm(`Queue ${ids.length} draft(s), posting one every ${spacing} minutes? Each draft is deleted right after it's sent.`))return;
  try{
    await api('/api/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({post_ids:ids,spacing_minutes:spacing,delete_after_publish:true})});
    await load();
  }catch(x){alert(x.message)}
};
$('#selectAllPosts').onchange=e=>{document.querySelectorAll('.pick').forEach(cb=>cb.checked=e.target.checked)};
$('#cancelAutoQueue').onclick=async()=>{
  const ids=[...document.querySelectorAll('.pick:checked')].map(x=>+x.value);
  if(!ids.length)return alert('Tick the queued drafts you want to cancel');
  try{await api('/api/schedule/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({post_ids:ids})});await load()}catch(x){alert(x.message)}
};
$('#deleteAllDrafts').onclick=async()=>{
  if(!state.posts.length)return alert('No drafts to delete');
  if(!confirm(`Delete all ${state.posts.length} draft(s)? This cannot be undone.`))return;
  try{await api('/api/posts/delete-all',{method:'POST'});await load()}catch(x){alert(x.message)}
};

// ---- Signal Opportunities -------------------------------------------------------------------------
// Rate-limit safety: every request first takes weight from a sliding-window limiter (soft cap well below
// Binance's 2400/min), results are cached, concurrency is bounded, and HTTP 418/429 aborts the whole scan.
const WEIGHT_CAP=1000,OI_CAP=400   // per minute / per 5 minutes (Binance: 2400 weight/min, 1000 openInterestHist/5min)
const OPP_TTL={'15m':5*60000,'1h':10*60000,'4h':30*60000,oi:10*60000,bulk:60000}
const OPP_CACHE=new Map();let weightLim=new WeightLimiter(WEIGHT_CAP),oiLim=new WeightLimiter(OI_CAP,300000),oppAbort=false
const klineWeight=limit=>limit>=500?5:limit>=100?2:1   // Binance: <100 rows = 1, 100-499 = 2
async function binJson(url,{weight=1,ttl=OPP_TTL.bulk,oi=false}={}){const hit=OPP_CACHE.get(url);if(hit&&Date.now()-hit.t<ttl)return hit.d
for(let attempt=0;attempt<3;attempt++){if(oppAbort)throw new Error('scan stopped')
await (oi?oiLim:weightLim).acquire(weight)
try{const r=await fetch(url)
if(r.status===418||r.status===429){const s=(+r.headers.get('retry-after')||120);weightLim.blockFor(s*1000);oppAbort=true;throw new Error(`Binance rate limit (HTTP ${r.status}); scan stopped. Wait ${s}s.`)}
if(r.ok){const d=await r.json();OPP_CACHE.set(url,{t:Date.now(),d});return d}
if(r.status<500)throw new Error(`HTTP ${r.status}`)}catch(e){if(/rate limit|scan stopped/.test(e.message)||attempt===2)throw e}
await new Promise(res=>setTimeout(res,600*2**attempt+Math.random()*300))}}
async function pool(jobs,size,fn){let i=0;await Promise.all(Array.from({length:size},async()=>{while(i<jobs.length&&!oppAbort){const j=jobs[i++];await fn(j)}}))}
const kl=(s,tf,lim,ttl)=>binJson(`${FAPI}/fapi/v1/klines?symbol=${s}&interval=${tf}&limit=${lim}`,{weight:klineWeight(lim),ttl:ttl??OPP_TTL[tf]}).then(rows=>rows.map(r=>r.slice(0,8)))
function oppStatus(text,tone=''){const el=$('#oppStatus');el.hidden=!text;el.textContent=text;el.className=`generate-status ${tone}`}
const oppDrafts={}
const fmtPct=v=>v==null?'n/a':(v>0?'+':'')+v.toFixed(1)+'%'
const LABELS={unusual_volume:'unusual volume',one_off_spike:'one-off spike',breakout:'breakout',potential_breakout:'potential breakout',retest:'retest',reversal:'reversal',mover:'mover',oi_anomaly:'OI anomaly',price_oi_divergence:'price/OI divergence',funding_extreme:'extreme funding',exhausted:'exhausted',manipulation_risk:'manipulation risk'}
function oppCard(o){const l=o.liquidity||{};const comps=Object.entries(o.components||{}).map(([k,c])=>`${k}: ${c.value==null?'n/a (excluded)':c.value} × ${(c.effective_weight*100).toFixed(0)}% — ${c.note||''}`).join('\n')
const d=oppDrafts[o.symbol]
return `<div class="signal">
 <div class="signal-top"><span class="side ${o.direction==='LONG'?'long':o.direction==='SHORT'?'short':''}">${esc(o.direction)}</span>
  <span class="signal-symbol">${esc(o.symbol)}</span>
  <span class="pill ${o.tier==='high'?'ok':''}">${o.tier}</span>
  ${o.labels.map(x=>`<span class="pill accent">${esc(LABELS[x]||x)}</span>`).join('')}
  <span class="signal-score">opportunity <b class="opp-score">${o.opportunity_score}</b> · technical <b>${o.technical_score}/95</b></span></div>
 <div class="stat-grid">
  <div><span class="stat-label">Rel. volume (15m)</span><span class="stat-value">${o.relative_volume==null?'n/a':o.relative_volume+'×'}</span></div>
  <div><span class="stat-label">15m / 1h / 24h</span><span class="stat-value">${fmtPct(o.change_15m)} / ${fmtPct(o.change_1h)} / ${fmtPct(o.change_24h)}</span></div>
  <div><span class="stat-label">Liquidity</span><span class="stat-value">$${(l.quote_volume_24h/1e6).toFixed(0)}M · spread ${l.spread_pct==null?'n/a':l.spread_pct+'%'} · ${l.spot?'spot+':''}futures</span></div>
 </div>
 <p class="opp-why-title"><b>Why this coin?</b></p>
 <ul class="opp-why">${(o.reasons.length?o.reasons:['No fresh event; ranked on technical quality and liquidity only.']).map(x=>`<li>${esc(x)}</li>`).join('')}
 ${o.risks.map(x=>`<li class="opp-risk">⚠ ${esc(x)}</li>`).join('')}
 ${o.blockers.length?`<li class="opp-risk">Not publish-ready: ${esc(o.blockers.join('; '))}</li>`:''}</ul>
 ${o.missing.length?`<div class="opp-missing">Missing data: ${esc(o.missing.join(', '))}</div>`:''}
 <div class="actions">
  <button onclick="oppView('${o.symbol}')">🔎 View signal</button>
  <button class="primary" onclick="oppGenerate('${o.symbol}')">📝 Generate post + chart</button>
  <button onclick="oppPreview('${o.symbol}')" ${d?'':'disabled'}>👁️ Preview</button>
  <button onclick="oppPost('${o.symbol}')" ${d?'':'disabled'}>🚀 Post (asks to confirm)</button>
 </div>
 <pre class="opp-detail" id="oppd-${o.symbol}" style="display:none"></pre>
 <div class="opp-preview" id="oppp-${o.symbol}"></div>
</div>`}
function renderOpps(data){const rs=data.results||[]
const el=$('#oppUpdated');el.hidden=!data.scanned_at;el.textContent=data.scanned_at?`Last update ${data.scanned_at}`:''
if(!rs.length){$('#oppList').innerHTML='<div class="empty">No opportunities yet — run a scan above.</div>';return}
const groups=[['publish_ready','Publish-ready (score ≥ '+data.config.high_threshold+', setup checks pass)'],['watchlist','Watchlist / needs confirmation'],['low','Low priority']]
$('#oppList').innerHTML=groups.map(([g,title])=>{const items=rs.filter(r=>r.group===g);return items.length?`<div class="opp-group">${title} (${items.length})</div><div class="signal-list">${items.map(oppCard).join('')}</div>`:''}).join('')+
(data.skipped&&data.skipped.length?`<div class="opp-missing">Skipped ${data.skipped.length}: ${esc(data.skipped.slice(0,8).map(s=>s.symbol+' ('+s.reason+')').join('; '))}${data.skipped.length>8?'…':''}</div>`:'')}
window.oppView=sym=>{const o=(window.oppData?.results||[]).find(r=>r.symbol===sym);if(!o)return;const el=$('#oppd-'+sym);const sg=o.signal||{}
el.textContent=`${sym} ${sg.side} ${sg.timeframe} — entry ${sg.entry_low}–${sg.entry_high}, stop ${sg.stop_loss}, targets ${sg.tp1} / ${sg.tp2} / ${sg.tp3}, technical score ${sg.score}/95\n\nScore breakdown (unavailable parts are excluded, weights re-normalised):\n`+Object.entries(o.components).map(([k,c])=>`  ${k}: ${c.value==null?'n/a':c.value} × ${(c.effective_weight*100).toFixed(0)}% — ${c.note||''}`).join('\n')
el.style.display=el.style.display==='none'?'block':'none'}
window.oppGenerate=async sym=>{try{oppStatus(`Generating post and chart for ${sym}…`)
await shareKlines(['BTCUSDT'],'1h')
const force=false;let d
try{d=await api('/api/opportunities/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,template_name:$('#templateSel').value==='auto'?'auto':'auto',force})})}
catch(x){if(/already exists|stale/.test(x.message)&&confirm(x.message+'\n\nCreate a new draft anyway?'))d=await api('/api/opportunities/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,template_name:'auto',force:true})});else throw x}
oppDrafts[sym]=d;oppStatus(`Draft #${d.id} created for ${sym}${d.ready?'':' (watchlist post: setup is not publish-ready)'}. Review it in Drafts before posting.`,'success')
await load();renderOpps(window.oppData)}catch(x){oppStatus(x.message,'error')}}
window.oppPreview=sym=>{const d=oppDrafts[sym];if(!d)return;const el=$('#oppp-'+sym);el.innerHTML=(d.chart_path?`<img src="/charts/${esc(d.chart_path.split('/').pop())}" alt="chart">`:'')+`<div class="preview">${esc(d.content)}</div>`}
window.oppPost=sym=>{const d=oppDrafts[sym];if(d)window.publishPost(d.id)}
$('#oppCancel').onclick=()=>{oppAbort=true;oppStatus('Cancelling…')}
const post=(url,body)=>api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
$('#scanOpp').onclick=async()=>{const btn=$('#scanOpp'),deep=$('#oppMode').value==='all';btn.disabled=true;$('#oppCancel').hidden=false;oppAbort=false;weightLim.total=0
const errors=[],stats={mode:deep?'all':'top'}
try{oppStatus('Loading 24h tickers, funding and spreads (3 bulk calls)…')
const [tickers,fundArr,bookArr]=await Promise.all([binJson(`${FAPI}/fapi/v1/ticker/24hr`,{weight:40}),binJson(`${FAPI}/fapi/v1/premiumIndex`,{weight:10}).catch(e=>{errors.push('funding: '+e.message);return[]}),binJson(`${FAPI}/fapi/v1/ticker/bookTicker`,{weight:5}).catch(e=>{errors.push('spread: '+e.message);return[]})])
const funding=Object.fromEntries(fundArr.map(x=>[x.symbol,x])),book=Object.fromEntries(bookArr.map(b=>[b.symbol,b]))
const spot=await binJson('https://api.binance.com/api/v3/ticker/price',{weight:0}).catch(e=>{errors.push('spot list: '+e.message);return[]})
const cand=await post('/api/opportunities/candidates',{tickers,mode:deep?'all':'top',funding,book})
let symbols=cand.symbols;stats.universe=symbols.length
const klines={};let k15={}
if(deep){// Stage 1: one cheap 15m call (weight 1) per coin. Stage 2: full candles only for coins with a fresh trigger.
 let done=0
 await pool(symbols,4,async s=>{try{k15[s]=await kl(s,'15m',99)}catch(e){errors.push(`${s} 15m: ${e.message}`)}done++;if(done%10===0)oppStatus(`Stage 1/2: 15m look ${done}/${symbols.length} · weight used ${weightLim.used()}/${WEIGHT_CAP} per min`)})
 if(oppAbort)throw new Error('scan stopped')
 const sl=await post('/api/opportunities/shortlist',{tickers,klines15:k15,funding})
 symbols=sl.symbols;stats.stage1=Object.keys(k15).length;stats.triggered=sl.triggered;stats.shortlisted=symbols.length
 symbols.forEach(s=>{klines[s]={'15m':k15[s]}})}
const need=deep?['1h','4h']:['15m','1h','4h'],oi_hist={}
const jobs=symbols.flatMap(s=>[...need.map(tf=>[tf,s]),['oi',s]]);let n=0
await pool(jobs,4,async([kind,s])=>{try{if(kind==='oi')oi_hist[s]=await binJson(`${FAPI}/futures/data/openInterestHist?symbol=${s}&period=1h&limit=12`,{weight:1,oi:true,ttl:OPP_TTL.oi})
else(klines[s]??={})[kind]=await kl(s,kind,kind==='15m'?130:kind==='1h'?220:150)}catch(e){errors.push(`${s} ${kind}: ${e.message}`)}n++;if(n%10===0)oppStatus(`${deep?'Stage 2/2':'Fetching'}: ${n}/${jobs.length} requests · weight used ${weightLim.used()}/${WEIGHT_CAP} per min`)})
if(oppAbort&&errors.some(e=>/rate limit/.test(e)))throw new Error(errors.find(e=>/rate limit/.test(e)))
stats.weight_spent=weightLim.total
oppStatus('Ranking…')
const data=await post('/api/opportunities/scan',{tickers:tickers.filter(t=>symbols.includes(t.symbol)),klines,oi_hist,symbols,funding:Object.fromEntries(symbols.map(s=>[s,funding[s]]).filter(x=>x[1])),
 book:Object.fromEntries(symbols.map(s=>[s,book[s]]).filter(x=>x[1])),spot_symbols:spot.map(x=>x.symbol),stats})
window.oppData=data;renderOpps(data)
const f=deep?`Deep scan: ${stats.universe} liquid coins → ${stats.triggered} with a 15m trigger → ${stats.shortlisted} analysed. `:`Scanned ${symbols.length} coins. `
oppStatus(f+`Request weight spent: ${stats.weight_spent} (limit 2400/min).`+(errors.length?` ${errors.length} data gap(s): ${errors.slice(0,4).join(' | ')}`:''),errors.length?'error':'success')}
catch(x){oppStatus(`Scan stopped: ${x.message}`,'error')}finally{btn.disabled=false;$('#oppCancel').hidden=true}}
api('/api/opportunities').then(d=>{window.oppData=d;renderOpps(d)}).catch(()=>{})
load();
