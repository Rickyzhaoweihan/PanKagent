'use strict';
let snapshot=null, busy=false, lastSuccess=0;
const $=id=>document.getElementById(id);
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
const age=value=>typeof value==='number'?value<60?`${Math.round(value)}s ago`:`${Math.round(value/60)}m ago`:'Not observed';
const date=value=>value?new Date(typeof value==='number'?value*1000:value).toLocaleString():'Not observed';
const badge=state=>el('span',state||'unknown',`badge ${['healthy','degraded','unavailable','unknown'].includes(state)?state:'unknown'}`);
function observationLabel(c, operation=false){
 if(c.error_category==='not_configured')return 'Not configured';
 if(c.state!=='unknown')return c.state;
 if(['collector_stale','dashboard_disconnected','stale_observation'].includes(c.error_category))return 'Stale';
 if(c.error_category)return 'Telemetry unavailable';
 if(operation||c.kind==='operation')return typeof c.age_seconds==='number'?'No recent activity':'Not yet observed';
 return typeof c.age_seconds==='number'?'No recent evidence':'Not observed';
}
function observationBadge(c, operation=false){const n=badge(c.state);n.textContent=observationLabel(c,operation);return n;}
function card(title,value,note){const n=el('div',undefined,'card');n.append(el('h3',title),typeof value==='string'?el('strong',value):value,el('p',note));return n;}
function render(){
 if(!snapshot)return;
 const monitor=snapshot.monitor||{};
 $('connection').className=snapshot.stale?'error':'';
 $('connection').textContent=`Monitor ${monitor.state} · collected ${date(snapshot.collected_at)} · ${age(monitor.age_seconds)}${snapshot.stale?' · data is stale':''}`;
 const components=snapshot.components||[];
 $('summary').replaceChildren(card('Monitor',badge(monitor.state),'Independent collector and history'),...['dev.frontend.delivery','frontend.delivery','results.ready','agent.ready'].map(id=>{const c=components.find(c=>c.id===id)||{label:id,state:'unknown'};return card(c.label,badge(c.state),c.error_category||'Current operational observation');}));
 const filter=$('filter').value;
 $('components').replaceChildren(...components.filter(c=>filter==='all'||filter==='attention'&&c.state!=='healthy'&&c.kind!=='operation'||filter==='operations'&&(c.kind==='operation'||c.operation)).map(c=>{
  const n=el('article',undefined,'component'),top=el('div',undefined,'top'),title=el('div');title.append(el('h3',c.label),el('div',c.id,'id'));top.append(title,observationBadge(c));n.append(top,el('p',`${c.kind==='operation'?'Recorded operation':'Access / telemetry'} · ${age(c.age_seconds)}${c.required?' · required':''}`));
  if(c.latency_ms!==null&&c.latency_ms!==undefined)n.append(el('p',`Probe ${c.latency_ms} ms`));
  if(c.error_category==='not_configured')n.append(el('p','No endpoint is configured; no connectivity probe is run.'));
  else if(c.state==='unknown')n.append(el('p',c.kind==='operation'?'This operation has no fresh recorded outcome; it is not a periodic connectivity check.':'A current usable observation is unavailable; see timestamps and details.'));
  if(c.error_category)n.append(el('p',c.error_category.replaceAll('_',' ')));
  if(c.operation){const op=el('div',undefined,'operation');op.append(el('p','Actual operation'),observationBadge(c.operation,true),el('p',`${age(c.operation.age_seconds)}${c.operation.stale?' · stale / no recent evidence':''}`),el('p',`Last success: ${date(c.operation.last_success)}`));if(c.operation.error_category)op.append(el('p',c.operation.error_category.replaceAll('_',' ')));n.append(op);}
  if(c.scope)n.append(el('p',c.scope));
  if(Object.keys(c.details||{}).length){const detail=el('details'),dl=el('dl');detail.append(el('summary','Details'));Object.entries(c.details).forEach(([k,v])=>dl.append(el('dt',k.replaceAll('_',' ')),el('dd',v??'Unknown')));detail.append(dl);n.append(detail);}
  return n;
 }));
 const b=snapshot.budget||{},q=snapshot.queues||{};
 const money=v=>typeof v==='number'?`$${v.toFixed(2)}`:'Unknown';
 $('capacity').replaceChildren(card('Claude remaining',money(b.remaining_usd),'Shared ledger; excludes external HIRN spending'),card('Reserved / spent',`${money(b.reserved_usd)} / ${money(b.spent_usd)}`,'Reservations can outlive interrupted calls'),card('Agent queue',`${q.agent?.active_queries??'?'} active · ${q.agent?.queue_depth??'?'} waiting`,`Concurrency capacity: ${q.agent?.capacity??'unknown'}`),card('Results queue',`${q.results?.active??'?'} active · ${q.results?.depth??'?'} waiting`,`Waiting capacity: ${q.results?.capacity??'unknown'}`));
 const table=el('table');Object.entries(snapshot.metrics||{}).forEach(([service,metrics])=>Object.entries(metrics).forEach(([key,value])=>{const row=el('tr');row.append(el('td',service),el('td',key),el('td',value));table.append(row);}));$('metrics').replaceChildren(table);
}
async function fetchJSON(path){const response=await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(10000)});if(!response.ok)throw new Error(response.status===401?'Authentication required':`Monitor HTTP ${response.status}`);return response.json();}
async function update(){if(busy)return;busy=true;try{
 const responses=await Promise.allSettled([fetchJSON('api/snapshot'),fetchJSON(`api/history?hours=${$('hours').value}`),fetchJSON('api/incidents')]);
 if(responses[0].status==='rejected')throw responses[0].reason;
 const data=responses[0].value,history=responses[1].status==='fulfilled'?responses[1].value:null,incidents=responses[2].status==='fulfilled'?responses[2].value:null;
 snapshot=data;lastSuccess=Date.now();render();
 if(history){$('coverage').textContent=`${history.samples} observations · ${date(history.from)} to ${date(history.to)}`;
 const ids=['dev.frontend.delivery','frontend.delivery','results.ready','agent.ready','cypher.gateway','functional.api'];
 $('history').replaceChildren(...ids.map(id=>{const row=el('div',undefined,'timeline'),bar=el('div',undefined,'segments');row.append(el('span',id),bar);for(const point of history.points){const s=point.components[id]||'unknown',n=el('span',undefined,`segment ${s}`);n.title=`${date(point.time)} – ${date(point.end)}: ${s}`;bar.append(n);}return row;}));
 }else{$('coverage').textContent='History temporarily unavailable; live status remains current.';$('history').replaceChildren();}
 if(incidents){$('incidents').replaceChildren(...(incidents.incidents.length?incidents.incidents.map(i=>{const row=el('div',undefined,'incident');row.append(badge(i.ended&&i.resolved_state==='healthy'?'healthy':i.state),el('strong',i.component),el('span',`${i.reason} · ${date(i.started)} → ${i.ended?`${i.resolved_state==='healthy'?'recovered':'state changed'} ${date(i.ended)}`:'open'}`));return row;}):[el('p','No confirmed incidents recorded.')]));
 }else{$('incidents').replaceChildren(el('p','Incident history temporarily unavailable.'));}
 }catch(error){$('connection').className='error';$('connection').textContent=`${error.message}. Last successful view: ${lastSuccess?new Date(lastSuccess).toLocaleTimeString():'none'}. Displayed data may be stale.`;if(snapshot){snapshot.stale=true;snapshot.components.forEach(c=>{c.state='unknown';c.error_category='dashboard_disconnected';if(c.operation){c.operation.state='unknown';c.operation.stale=true;}});snapshot.monitor.state='unknown';render();$('connection').textContent=`Monitor disconnected. Last successful view: ${lastSuccess?new Date(lastSuccess).toLocaleTimeString():'none'}. Displayed data is stale.`;}}finally{busy=false;}}
$('refresh').onclick=update;$('hours').onchange=update;$('filter').onchange=render;
$('download').onclick=()=>{if(!snapshot)return;const url=URL.createObjectURL(new Blob([JSON.stringify(snapshot,null,2)],{type:'application/json'}));const a=el('a');a.href=url;a.download='pankgraph-health.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
update();setInterval(()=>{if(!document.hidden)update();},15000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)update();});
