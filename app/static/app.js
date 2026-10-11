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
const STATUS_LABEL={NEW:'new',ENTRY_TRIGGERED:'entry hit',TP1_HIT:'TP1 hit',TP2_HIT:'TP2 hit',TP3_HIT:'TP3 hit',SL_HIT:'SL hit',INVALIDATED:'invalidated'};
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
      <button onclick="refreshStatus(${s.id})">🔄 Refresh status</button>
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
function setScanStatus(text, tone=''){const el=$('#scanStatus');el.hidden=!text;el.textContent=text;el.className=`generate-status ${tone}`}
async function runScan(){return api('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbols:$('#symbols').value.split(',').map(x=>x.trim()).filter(Boolean),interval:$('#interval').value,bullish_only:$('#bullish').checked})})}
$('#scan').onclick=async()=>{try{await runScan();await load()}catch(x){alert(x.message)}}
$('#oiScan').onclick=async()=>{const btn=$('#oiScan'),hours=$('#oiHours').value;btn.disabled=true
try{setScanStatus(`Finding the top ${$('#oiCount').value} open-interest gainers over the last ${hours}h…`)
const d=await api('/api/discover',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit:+$('#oiCount').value,hours:+hours})})
if(!d.symbols.length){setScanStatus('Binance returned no open-interest data. Try again in a minute.','error');return}
$('#symbols').value=d.symbols.join(',')
const top=d.movers.slice(0,3).map(m=>`${m.symbol} ${m.oi_change_pct>0?'+':''}${m.oi_change_pct}%`).join(', ')
setScanStatus(`Found ${d.symbols.length} coins (top: ${top}). Scanning…`)
const r=await runScan();await load()
setScanStatus(`Scanned ${d.symbols.length} OI gainers → ${r.results.length} signal${r.results.length===1?'':'s'} added${r.errors.length?`. Skipped: ${r.errors[0]}${r.errors.length>1?` (+${r.errors.length-1} more)`:''}`:''}.`,r.errors.length?'':'success')}
catch(x){setScanStatus(`OI scan failed: ${x.message}`,'error')}finally{btn.disabled=false}}

window.refreshStatus=async id=>{try{const d=await api(`/api/signals/${id}/refresh-status`,{method:'POST'});await load()}catch(x){alert(x.message)}};
window.delSignal=async id=>{if(confirm('Delete this signal?')){try{await api('/api/signals/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signal_ids:[id]})});await load()}catch(x){alert(x.message)}}};
$('#deleteSelectedSignals').onclick=async()=>{const ids=[...document.querySelectorAll('.pickSignal:checked')].map(x=>+x.value);if(!ids.length)return alert('Select signals to delete');if(!confirm(`Delete ${ids.length} selected signal(s)?`))return;try{await api('/api/signals/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signal_ids:ids})});await load()}catch(x){alert(x.message)}};
$('#deleteAllSignals').onclick=async()=>{if(!state.signals.length)return alert('No signals to delete');if(!confirm(`Delete all ${state.signals.length} signal(s)?`))return;try{await api('/api/signals/delete-all',{method:'POST'});await load()}catch(x){alert(x.message)}};
window.gen=async id=>{
  const tpl=$('#templateSel').value;
  try{
    if(tpl==='btc_market_analysis'){await api('/api/generate-btc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({template_name:tpl})})}
    else if(tpl==='top_10_signals'){await api('/api/generate-top10',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})}
    else{await api('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signal_id:id,template_name:tpl})})}
    await load()
  }catch(x){alert(x.message)}
};
async function generateDrafts(button, ids, label, endpoint='/api/generate-bulk'){
  if(!ids.length)return alert('Scan for signals first');
  button.disabled=true;
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
$('#genBtc').onclick=async()=>{$('#genBtc').disabled=true;try{await api('/api/generate-btc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({template_name:'btc_market_analysis'})});await load()}catch(x){alert(x.message)}finally{$('#genBtc').disabled=false}};
$('#genTop10').onclick=async()=>{$('#genTop10').disabled=true;try{await api('/api/generate-top10',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});await load()}catch(x){alert(x.message)}finally{$('#genTop10').disabled=false}};
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
load();
