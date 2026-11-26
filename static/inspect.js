/* Inspect: native pages for what AIDB holds in this database (models, agents, tools, knowledge bases,
   pipelines, functions) plus a read-only SQL console. Same look and feel as the rest of the app.
   Data comes from the read-only Introspector backend mounted at /introspect/api. Secrets are redacted there. */
const INS={cfg:null,cat:null,inv:null,br:{},err:'',busy:null,fn:{}};

async function insPost(path,body){
  if(!INS.cfg){
    try{const j=await (await fetch('/introspect/api/local-config')).json();INS.cfg=j.config||{};}catch(e){INS.cfg={};}
  }
  const c=INS.cfg;
  const r=await fetch('/introspect/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-By':'aidb-introspect'},
    body:JSON.stringify(Object.assign({host:c.host||'',port:c.port||'',dbname:c.dbname||'',user:c.user||'',password:c.password||''},body||{}))});
  return r.json();
}
function insLoad(force){
  if(force){INS.cat=null;INS.inv=null;INS.br={};INS.fn={};INS.err='';INS.busy=null;}
  if(INS.cat&&INS.inv) return Promise.resolve();
  if(!INS.busy) INS.busy=Promise.all([insPost('catalog'),insPost('inventory')]).then(([c,i])=>{
    INS.busy=null;
    if(!c.ok){INS.err=c.error||'Could not read the database.';return;}
    INS.err='';INS.cat=c.catalog;INS.inv={};(i.items||[]).forEach(x=>INS.inv[x.key]=x);
  }).catch(e=>{INS.busy=null;INS.err=String(e.message||e);});
  return INS.busy;
}
const insRows=k=>((INS.inv||{})[k]||{}).rows||[];
const insOk=k=>!!((INS.inv||{})[k]||{}).ok;
async function insBrowse(rel){
  if(rel in INS.br) return INS.br[rel];
  const r=(INS.cat.relations||[]).find(x=>x.name===rel);
  let rows=[];
  if(r&&r.can_select){try{const d=await insPost('browse',{relation:rel,limit:200});if(d.ok)rows=d.data.rows||[];}catch(e){}}
  return INS.br[rel]=rows;
}

/* ---------- small renderers ---------- */
const insDash='<span style="color:var(--muted)">&ndash;</span>';
function insCell(v){
  if(v===null||v===undefined||v==='') return insDash;
  if(typeof v==='boolean') return `<span class="pill ${v?'p-ok':'p-mute'}">${v?'yes':'no'}</span>`;
  if(Array.isArray(v)){
    if(!v.length) return insDash;
    if(v.every(x=>typeof x!=='object')) return `<div class="chips">${v.slice(0,6).map(x=>`<span class="tag">${esc(x)}</span>`).join('')}${v.length>6?`<span class="tag">+${v.length-6}</span>`:''}</div>`;
  }
  if(typeof v==='object'){const s=JSON.stringify(v);return `<span class="mono" title="${esc(s)}">${esc(s.length>70?s.slice(0,70)+'…':s)}</span>`;}
  const s=String(v);
  return s.length>90?`<span title="${esc(s)}">${esc(s.slice(0,90))}…</span>`:esc(s);
}
const INS_PREF=['name','kb_name','pipeline','provider','kind','model','credential','tool_type','read_only','status','step','source','destination','processed','total','uri','owner','endpoint'];
function insCols(rows,max){
  const all=[];rows.forEach(r=>Object.keys(r).forEach(k=>{if(!all.includes(k))all.push(k);}));
  const first=INS_PREF.filter(k=>all.includes(k));
  return first.concat(all.filter(k=>!first.includes(k))).slice(0,max||7);
}
const insHead=k=>esc(k.replace(/_/g,' '));
/* A table whose rows open to show every field of that row. */
function insTable(rows,opts){
  opts=opts||{};
  if(!rows.length) return `<div class="panel empty">${opts.empty||'Nothing here yet.'}</div>`;
  const cols=insCols(rows,opts.max);
  return `<div class="tw"><table><thead><tr>${cols.map(c=>`<th>${insHead(c)}</th>`).join('')}</tr></thead><tbody>${rows.map((r,i)=>
    `<tr class="rowd" data-i="${i}" tabindex="0" aria-expanded="false">${cols.map(c=>`<td class="${c==='name'?'nm':''}">${insCell(r[c])}</td>`).join('')}</tr>`+
    `<tr class="rowx" hidden><td colspan="${cols.length}"><pre class="code">${esc(JSON.stringify(r,null,2))}</pre></td></tr>`).join('')}</tbody></table></div>`;
}
function insBindRows(box){
  box.querySelectorAll('tr.rowd').forEach(tr=>{
    const tog=()=>{const x=tr.nextElementSibling;x.hidden=!x.hidden;tr.setAttribute('aria-expanded',!x.hidden);};
    tr.onclick=tog;tr.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();tog();}};
  });
}
function insFilter(box,inputSel,rowSel){
  const inp=box.querySelector(inputSel); if(!inp)return;
  inp.oninput=()=>{const q=inp.value.toLowerCase();box.querySelectorAll(rowSel).forEach(r=>{
    const hit=!q||r.textContent.toLowerCase().includes(q);r.hidden=!hit;
    if(r.classList.contains('rowd')&&!hit)r.nextElementSibling.hidden=true;
    if(r.classList.contains('rowd')&&hit&&r.getAttribute('aria-expanded')==='true')r.nextElementSibling.hidden=false;});};
}
const insSearch=(ph)=>`<div class="toolbar"><input type="search" id="ins-q" placeholder="${ph}" autocomplete="off" style="min-width:260px"><button class="btn alt" id="ins-refresh">Refresh</button></div>`;
function insNeedsAidb(msg){
  return `<div class="note"><b>AIDB is not available in this database.</b> ${esc(msg)}<br>The container creates the extension on first start. See the container log: <code>docker compose logs pg</code>.</div>`;
}

/* ---------- page shell ---------- */
function insPage(title,lead,build){
  const v=state.view;
  setTimeout(async()=>{
    const put=h=>{const b=$('#ins-body');if(b&&state.view===v)b.innerHTML=h;return b;};
    try{
      await insLoad();
      if(state.view!==v||!$('#ins-body'))return;
      if(INS.err){put(insNeedsAidb(INS.err));return;}
      const r=await build();
      if(state.view!==v)return;
      const b=put(r.html); if(!b)return;
      insBindRows(b); const rf=b.querySelector('#ins-refresh'); if(rf) rf.onclick=()=>{INS.err='';insLoad(true);render();};
      b.querySelectorAll('[data-go]').forEach(x=>x.onclick=()=>{location.hash=x.dataset.go;});
      if(r.after) r.after(b);
    }catch(e){put(`<div class="note"><b>Could not read the database.</b> ${esc(e.message||e)}</div>`);}
  },0);
  return `<h1>${title}</h1><p class="lead">${lead}</p><div id="ins-body"><div class="panel empty">Reading the database…</div></div>`;
}

/* ---------- Overview ---------- */
function inspect(){
  return insPage('Inspect','What AIDB holds in this database, read live. Nothing here changes data.',async()=>{
    const c=INS.cat,ext=c.extension||{},sv=c.server||{};
    const agents=await insBrowse('agents');
    const kbs=insRows('semantic_kbs').length+(await insBrowse('knowledge_bases')).length;
    const pipes=(await insBrowse('pipelines')).length;
    const tools=insRows('native_tools').length+insRows('mcp_tools').length;
    const fns=await insFnDoc().then(d=>insDocFns(d).length,()=>'–');
    const tile=(n,l,go)=>`<button class="tile" data-go="${go}"><b>${n}</b><span>${l}</span></button>`;
    const health=[];
    const perr=insRows('pipeline_errors').filter(r=>Object.values(r).some(v=>typeof v==='number'&&v>0));
    if(perr.length) health.push(['warn',`${perr.length} pipeline${perr.length>1?'s have':' has'} errors.`,'inspect-pipelines']);
    const leak=insRows('leaked_credentials');
    if(leak.length) health.push(['bad',`${leak.length} possible key${leak.length>1?'s':''} found in model settings. Move them to credentials.`,'inspect-models']);
    const noc=insRows('models').filter(m=>m.credential&&insRows('credentials').length&&!insRows('credentials').some(x=>x.name===m.credential));
    if(noc.length) health.push(['warn',`${noc.length} model${noc.length>1?'s refer':' refers'} to a credential that is not listed.`,'inspect-models']);
    const bad=Object.values(INS.inv).filter(x=>!x.ok).map(x=>x.label);
    if(!health.length) health.push(['ok','No problems found in pipelines, credentials or models.','']);
    const worker=(c.workers||[]).length;
    const settings=(c.settings||[]);
    return {html:`<p><button class="btn alt" id="ins-refresh">Refresh</button></p>
    <div class="grid">${tile(insRows('models').length,'Models','inspect-models')}${tile(agents.length,'Agents','inspect-agents')}${tile(tools,'Tools','inspect-agents')}${tile(kbs,'Knowledge bases','inspect-kbs')}${tile(pipes,'Pipelines','inspect-pipelines')}${tile(fns,'AI SQL functions','inspect-functions')}</div>
    <h2>Health</h2>
    <div class="panel pad">${health.map(([l,t,g])=>`<div style="display:flex;gap:10px;align-items:baseline;margin:3px 0"><span class="pill p-${l}">${{ok:'OK',warn:'Check',bad:'Fix'}[l]}</span><span>${esc(t)}${g?` <a href="#${g}">Open</a>`:''}</span></div>`).join('')}
      ${bad.length?`<p style="color:var(--muted);font-size:12.5px;margin:10px 0 0">Not available in this AIDB version: ${bad.map(esc).join(', ')}.</p>`:''}</div>
    <h2>Database</h2>
    <div class="two">
      <div class="panel pad"><div class="ck">
        <span style="color:var(--muted)">AIDB</span><span>${esc(ext.version||'-')} ${(c.available||{}).default_version&&(c.available.default_version!==ext.version)?`<span class="pill p-warn">${esc(c.available.default_version)} available</span>`:''}</span>
        <span style="color:var(--muted)">Database</span><span class="mono">${esc(sv.database)} as ${esc(sv.user)}${sv.is_superuser?' (superuser)':''}</span>
        <span style="color:var(--muted)">PostgreSQL</span><span>${esc(sv.server_version)}</span>
        <span style="color:var(--muted)">Preloaded</span><span class="mono">${esc(sv.shared_preload_libraries||'-')}</span>
        <span style="color:var(--muted)">Background workers</span><span>${worker?worker+' running':'none seen'}</span></div></div>
      <div class="panel pad"><div style="color:var(--muted);margin-bottom:6px">Extensions in this database</div>
        <div class="chips">${(sv.installed_extensions||[]).map(e=>`<span class="tag">${esc(e.name)} ${esc(e.version)}</span>`).join('')}</div></div>
    </div>
    <h2>AIDB settings</h2>
    ${settings.length?`<div class="tw"><table><thead><tr><th>Setting</th><th>Value</th><th>Default</th><th>What it does</th></tr></thead><tbody>${settings.map(s=>`<tr><td class="mono">${esc(s.name)}</td><td class="mono">${esc(s.setting)}${s.unit?' '+esc(s.unit):''}</td><td class="mono">${esc(s.boot_val)}</td><td>${esc(s.short_desc||'')}</td></tr>`).join('')}</tbody></table></div>`:'<div class="panel empty">No aidb.* settings found.</div>'}`};
  });
}

/* ---------- Models ---------- */
function inspectModels(){
  return insPage('Models','Every model registered with AIDB and the credentials it may use. Select a row for every field. Keys are never shown.',async()=>{
    const rows=insRows('models'),cred=insRows('credentials'),prov=insRows('list_providers');
    return {html:`${insSearch('Search models')}
      ${insTable(rows,{empty:'No models are registered yet. Register one with <code>aidb.create_model()</code>.'})}
      <h2>Credentials</h2>${insTable(cred,{empty:'No credentials. Models that run locally do not need one.'})}
      ${prov.length?`<h2>Providers</h2>${insTable(prov)}`:''}`,after:b=>insFilter(b,'#ins-q','tr.rowd')};
  });
}

/* ---------- Agents and tools ---------- */
function insAgentCard(a){
  const budget=[a.max_iterations!=null&&`${a.max_iterations} steps max`,a.input_token_budget!=null&&`${a.input_token_budget} input tokens`,
    a.output_token_budget!=null&&`${a.output_token_budget} output tokens`,a.budget_strategy&&`at the limit: ${a.budget_strategy}`].filter(Boolean);
  const tl=Array.isArray(a.tools)?a.tools:[];
  const dl=Array.isArray(a.delegates)?a.delegates:[];
  return `<div class="panel pad agcard" data-t="${esc(JSON.stringify(a).toLowerCase())}">
    <div class="agh"><h3>${esc(a.name||a.agent_name)}</h3>${a.model?`<span class="tag">${esc(a.model)}</span>`:''}</div>
    ${a.instructions?`<p style="margin:8px 0;color:var(--muted)">${esc(String(a.instructions).slice(0,220))}${String(a.instructions).length>220?'…':''}</p>`:''}
    ${tl.length?`<div style="margin:8px 0 0"><span style="color:var(--muted);font-size:12px">TOOLS</span><div class="chips" style="margin-top:4px">${tl.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div></div>`:''}
    ${dl.length?`<div style="margin:8px 0 0"><span style="color:var(--muted);font-size:12px">DELEGATES TO</span><div class="chips" style="margin-top:4px">${dl.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div></div>`:''}
    ${a.purpose?`<div style="margin:8px 0 0"><span style="color:var(--muted);font-size:12px">PURPOSE</span> <span class="tag">${esc(a.purpose)}</span></div>`:''}
    ${budget.length?`<div style="margin:8px 0 0;color:var(--muted);font-size:12.5px">Budget: ${budget.map(esc).join(' · ')}</div>`:''}
  </div>`;
}
function inspectAgents(){
  return insPage('Agents and tools','The agents defined in this database and what each one is allowed to do.',async()=>{
    const agents=await insBrowse('agents');
    const rel=(INS.cat.relations||[]).find(r=>r.name==='agents');
    const native=insRows('native_tools'),mcp=insRows('mcp_tools');
    const ro=native.filter(t=>t.read_only).length;
    const tools=native.concat(mcp).map(t=>Object.assign({},t));
    return {html:`${insSearch('Search agents and tools')}
      <h2 style="margin-top:8px">Agents</h2>
      ${agents.length?`<div class="two">${agents.map(insAgentCard).join('')}</div>`:
        `<div class="panel empty">${rel?'No agents defined yet.':'This AIDB version has no agents table.'} Create one with <code>aidb.create_agent()</code> or run a notebook from <a href="#agents">Agents</a>.</div>`}
      <h2>Tools</h2>
      <div class="grid" style="margin-bottom:12px"><div class="tile"><b>${native.length}</b><span>built-in and SQL tools</span></div><div class="tile"><b>${ro}</b><span>read-only</span></div><div class="tile"><b>${native.length-ro}</b><span>can change data</span></div><div class="tile"><b>${mcp.length}</b><span>from MCP servers</span></div></div>
      ${insTable(tools,{empty:'No tools yet. Create one with <code>aidb.create_sql_tool()</code>.'})}`,
      after:b=>{insFilter(b,'#ins-q','tr.rowd');const q=b.querySelector('#ins-q');
        const f=()=>{const s=q.value.toLowerCase();b.querySelectorAll('.agcard').forEach(c=>c.hidden=!!s&&!c.dataset.t.includes(s));};q.addEventListener('input',f);}};
  });
}

/* ---------- Knowledge bases ---------- */
function inspectKbs(){
  return insPage('Knowledge bases','Semantic knowledge bases over your tables and knowledge bases over your documents.',async()=>{
    const sem=insRows('semantic_kbs'),stats=insRows('semantic_kb_stats'),alias=insRows('semantic_aliases');
    const doc=await insBrowse('knowledge_bases');
    const merged=sem.map(k=>Object.assign({},k,...stats.filter(s=>s.kb_name===(k.name||k.kb_name)).map(s=>{const o=Object.assign({},s);delete o.kb_name;return o;})));
    return {html:`${insSearch('Search knowledge bases')}
      <h2 style="margin-top:8px">Semantic knowledge bases</h2>
      ${insTable(merged,{empty:'None yet. Create one with <code>aidb.create_semantic_kb()</code>; see the Semantic KB notebook.'})}
      <h2>Document knowledge bases</h2>
      ${insTable(doc,{empty:'None yet. A pipeline with a knowledge base destination creates one.'})}
      ${alias.length?`<h2>Saved queries (semantic aliases)</h2>${insTable(alias)}`:''}`,after:b=>insFilter(b,'#ins-q','tr.rowd')};
  });
}

/* ---------- Pipelines and storage ---------- */
function inspectPipelines(){
  return insPage('Pipelines and storage','Pipelines that turn documents and tables into embeddings, any errors, and where files are read from.',async()=>{
    const pipes=await insBrowse('pipelines');
    const errs=insRows('pipeline_errors'),vol=insRows('volumes');
    const pc=r=>{const p=r.processed,t=r.total;return (typeof p==='number'&&typeof t==='number'&&t>0)?Math.round(100*p/t):null;};
    const bars=pipes.filter(r=>pc(r)!==null).map(r=>`<div style="display:grid;grid-template-columns:200px 1fr 48px;gap:10px;align-items:center;margin:6px 0"><span class="mono">${esc(r.name)}</span><div style="background:var(--frost);border-radius:999px;height:8px;overflow:hidden"><div style="width:${pc(r)}%;height:100%;background:var(--ok)"></div></div><span class="mono">${pc(r)}%</span></div>`).join('');
    return {html:`${insSearch('Search pipelines')}
      <h2 style="margin-top:8px">Pipelines</h2>
      ${bars?`<div class="panel pad" style="margin-bottom:12px">${bars}</div>`:''}
      ${insTable(pipes,{empty:'No pipelines yet. Create one with <code>aidb.create_pipeline()</code>; see the RAG over Documents notebook.'})}
      <h2>Errors</h2>${insTable(errs,{empty:'No pipeline errors.'})}
      <h2>Storage volumes</h2>${insTable(vol,{empty:'No volumes. Documents can be read from local or object storage through PGFS.'})}`,after:b=>insFilter(b,'#ins-q','tr.rowd')};
  });
}

/* ---------- AI SQL functions ---------- */
/* Only the functions the AIDB docs document, grouped like the docs. The list, a one-line summary and a docs link for each
   live in static/aidb-functions.json; signatures and definitions are read from the database through the read-only console. */
let INS_DOC=null;
async function insFnDoc(){
  if(!INS_DOC){const r=await fetch('/static/aidb-functions.json');if(!r.ok)throw new Error('Could not load aidb-functions.json ('+r.status+').');INS_DOC=await r.json();}
  return INS_DOC;
}
const insDocFns=d=>d.groups.flatMap(g=>g.sections.flatMap(s=>s.functions));
async function insQuery(sql){
  const r=await insPost('console',{sql}),res=(r.results||[])[0]||{};
  if(!r.ok||res.status!=='ok'||!res.rows) throw new Error(res.error||r.error||'The query failed.');
  return res.rows.map(row=>Object.fromEntries(res.columns.map((c,i)=>[c,row[i]])));
}
async function insSignatures(names){
  const list=names.map(n=>"'"+n.replace(/'/g,"''")+"'").join(',');
  const rows=await insQuery(`SELECT p.oid, n.nspname || '.' || p.proname AS name, pg_get_function_arguments(p.oid) AS args,
    pg_get_function_result(p.oid) AS result FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname || '.' || p.proname IN (${list}) ORDER BY 2, 1`);
  const by={};rows.forEach(r=>(by[r.name]=by[r.name]||[]).push(r));return by;
}
async function insDefinition(f,oid){
  if(f.startsWith('aidb.')){   // the Introspector's own lookup (aidb schema only): source unredacted, aggregate details
    const d=await insPost('function',{oid:+oid});
    if(!d.ok) throw new Error(d.error||'Could not read the definition.');
    return (d.data&&d.data.definition)||JSON.stringify(d.data&&d.data.aggregate||{},null,2);
  }
  const rows=await insQuery(`SELECT pg_get_functiondef(${+oid}) AS definition`);
  return (rows[0]||{}).definition||'';
}
function inspectFunctions(){
  return insPage('AI SQL functions','The functions documented in the AIDB docs, by docs area. Select a function to read its definition.',async()=>{
    const doc=await insFnDoc(),base=doc.docs_base;
    const sigs=await insSignatures(insDocFns(doc).map(f=>f.name));
    const ext=(u,t)=>`<a href="${esc(base+u)}" target="_blank" rel="noopener">${t}</a>`;
    const sum=t=>esc(t).replace(/`([^`]+)`/g,'<code>$1</code>');
    const row=f=>{const ov=sigs[f.name]||[];
      return `<tr class="fnr" tabindex="0" data-oids="${ov.map(o=>o.oid).join(',')}" data-q="${esc((f.name+' '+f.summary+' '+ov.map(o=>o.args).join(' ')).toLowerCase())}">
        <td><b>${esc(f.name)}</b>${ov.map(o=>`<div class="mono" style="color:var(--muted);font-size:12px;max-width:60ch;white-space:normal">(${esc(o.args)}) &rarr; ${esc(o.result)}</div>`).join('')}</td>
        <td>${sum(f.summary)}</td><td>${ext(f.docs,'Docs')}</td></tr><tr hidden class="fnx"><td colspan="3"></td></tr>`;};
    const groups=doc.groups.map((g,gi)=>`<section class="fng" data-g="${gi}"><h2>${esc(g.name)} <span style="font-size:13px;font-weight:400">${ext(g.docs,'Docs')}</span></h2>
      <p class="lead" style="max-width:none">${esc(g.description)}</p>${g.sections.map(s=>`<div class="fns" style="margin:18px 0">
        <h3>${esc(s.name)}</h3>${s.description?`<p style="color:var(--muted);margin:4px 0 8px">${esc(s.description)}</p>`:'<div style="height:8px"></div>'}
        <div class="tw"><table style="min-width:0"><thead><tr><th style="width:38%">Function</th><th>What it does</th><th style="width:1%"></th></tr></thead>
        <tbody>${s.functions.map(row).join('')}</tbody></table></div></div>`).join('')}</section>`).join('');
    const total=insDocFns(doc).length;
    return {html:`<div class="toolbar"><input type="search" id="ins-q" placeholder="Search ${total} functions" autocomplete="off" style="min-width:260px">
      <div class="seg" id="fn-g"><button data-g="" aria-pressed="true">All</button>${doc.groups.map((g,i)=>`<button data-g="${i}" aria-pressed="false">${esc(g.name)}</button>`).join('')}</div>
      <button class="btn alt" id="ins-refresh">Refresh</button></div><p id="fn-more" style="color:var(--muted)"></p>${groups}`,
      after:b=>{
        let g='';
        const draw=()=>{
          const q=b.querySelector('#ins-q').value.toLowerCase();let n=0;
          b.querySelectorAll('section.fng').forEach(sec=>{
            let secHits=0;
            sec.querySelectorAll('.fns').forEach(box=>{
              let hits=0;
              box.querySelectorAll('tr.fnr').forEach(tr=>{const hit=!q||tr.dataset.q.includes(q);tr.hidden=!hit;if(!hit)tr.nextElementSibling.hidden=true;hits+=hit;});
              box.hidden=!hits;secHits+=hits;
            });
            sec.hidden=!secHits||(g!==''&&sec.dataset.g!==g);if(!sec.hidden)n+=secHits;
          });
          b.querySelector('#fn-more').textContent=n?`${n} function${n===1?'':'s'}`:'No function matches.';
        };
        b.querySelector('#ins-q').oninput=draw;
        b.querySelectorAll('#fn-g button').forEach(x=>x.onclick=()=>{g=x.dataset.g;b.querySelectorAll('#fn-g button').forEach(y=>y.setAttribute('aria-pressed',y===x));draw();});
        b.querySelectorAll('tr.fnr').forEach(tr=>{const tog=async e=>{
          if(e.target.closest('a'))return;
          const x=tr.nextElementSibling;x.hidden=!x.hidden;if(x.hidden)return;
          const td=x.firstElementChild,oids=tr.dataset.oids.split(',').filter(Boolean);
          if(!oids.length){td.innerHTML='<span style="color:var(--muted)">Not found in this database.</span>';return;}
          td.innerHTML='<span style="color:var(--muted)">Reading…</span>';
          const parts=await Promise.all(oids.map(async o=>{if(!(o in INS.fn)){try{INS.fn[o]={ok:true,def:await insDefinition(tr.querySelector('b').textContent,o)};}catch(err){INS.fn[o]={ok:false,error:String(err.message||err)};}}return INS.fn[o];}));
          td.innerHTML=parts.map(d=>d.ok?`<pre class="code">${esc(d.def)}</pre>`:`<span class="res err">${esc(d.error)}</span>`).join('');
        };tr.onclick=tog;tr.onkeydown=e=>{if(e.key==='Enter'&&!e.target.closest('a')){e.preventDefault();tog(e);}};});
        draw();}};
  });
}

/* ---------- SQL console (read-only) ---------- */
const INS_EX=[
 ['Which models are registered?',"SELECT name, provider, credential\nFROM aidb.list_models()\nORDER BY provider, name;"],
 ['Which agents exist?',"SELECT name, model, tools\nFROM aidb.agents\nORDER BY name;"],
 ['What may an agent use?',"SELECT name, tool_type, read_only\nFROM aidb.list_native_tools()\nORDER BY tool_type, name;"],
 ['Has any pipeline step failed?',"SELECT * FROM aidb.get_all_error_summaries();"],
 ['List the credentials (keys are never shown)',"SELECT name, owner, endpoint, shared, has_secret, can_use\nFROM aidb.credentials\nORDER BY name;"],
 ['Scan for keys pasted into model settings',"SELECT * FROM aidb.audit_leaked_credentials();"],
 ['How many commands does AIDB add?',"SELECT count(*) AS commands\nFROM pg_proc\nWHERE pronamespace = 'aidb'::regnamespace;"],
 ['Where may AIDB connect?',"SELECT name, setting\nFROM pg_settings\nWHERE name LIKE '%egress%'\nORDER BY name;"]];
function inspectSql(){
  const saved=(()=>{try{return sessionStorage.getItem('ins-sql')||'';}catch(e){return '';}})();
  setTimeout(()=>{
    const ta=$('#sq'); if(!ta)return;
    const run=async()=>{
      const out=$('#sq-out'),go=$('#sq-go');
      try{sessionStorage.setItem('ins-sql',ta.value);}catch(e){}
      go.disabled=true;out.innerHTML='<div class="panel empty">Running…</div>';
      try{
        const d=await insPost('console',{sql:ta.value,format:$('#sq-fmt').value,timeout:30});
        if(!d.ok){out.innerHTML=`<div class="note"><b>Could not run it.</b> ${esc(d.error)}</div>`;return;}
        const notes=(d.notices||[]).length?`<pre class="log" style="margin:8px 0">${esc(d.notices.join('\n'))}</pre>`:'';
        if(d.format==='text'){out.innerHTML=`<pre class="log" style="max-height:520px">${esc(d.output)}</pre>${notes}`;return;}
        out.innerHTML=d.results.map(r=>{
          const head=`<div style="color:var(--muted);font-size:12.5px;margin:10px 0 6px"><code>${esc(r.text.replace(/\s+/g,' ').slice(0,110))}</code>${r.ms!=null?` · ${r.ms} ms`:''}${r.total_rows!=null?` · ${r.total_rows} row${r.total_rows===1?'':'s'}`:''}${r.truncated?' (first 5000 shown)':''}</div>`;
          if(r.status==='error') return head+`<div class="note"><b>Error</b><pre class="code" style="margin:6px 0 0">${esc(r.error)}</pre></div>`;
          if(r.status==='skipped') return head+'<div class="panel empty">Skipped because an earlier statement failed.</div>';
          if(r.raw) return head+`<pre class="code">${esc(r.raw)}</pre>`;
          if(r.no_result||!r.columns.length) return head+'<div class="panel empty">Done. No rows returned.</div>';
          return head+`<div class="tw"><table style="min-width:0"><thead><tr>${r.columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${r.rows.map(row=>`<tr>${row.map(v=>`<td class="mono" style="white-space:pre-wrap;max-width:60ch">${v===''?insDash:esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
        }).join('')+notes;
      }catch(e){out.innerHTML=`<div class="note"><b>Could not run it.</b> ${esc(e.message||e)}</div>`;}
      finally{go.disabled=false;}
    };
    $('#sq-go').onclick=run;
    ta.onkeydown=e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();run();}};
    $('#sq-ex').onchange=e=>{const i=e.target.value;if(i!==''){ta.value=INS_EX[+i][1];ta.focus();}e.target.value='';};
  },0);
  return `<h1>SQL console</h1>
  <p class="lead">Run a query against this database. It is read-only: nothing you type here can change data. Press Ctrl+Enter to run.</p>
  <div class="toolbar"><select id="sq-ex" aria-label="Examples"><option value="">Examples…</option>${INS_EX.map((e,i)=>`<option value="${i}">${esc(e[0])}</option>`).join('')}</select>
    <select id="sq-fmt" aria-label="Result format"><option value="grid">Table</option><option value="text">psql text</option></select></div>
  <textarea id="sq" class="sqlbox" spellcheck="false" rows="7" placeholder="SELECT * FROM aidb.list_models();">${esc(saved)}</textarea>
  <p style="margin:10px 0"><button class="btn" id="sq-go">Run</button> <span style="color:var(--muted);font-size:12.5px">SQL and the psql describe commands (<code>\\d</code>, <code>\\df</code>, <code>\\sf</code>) are accepted.</span></p>
  <div id="sq-out"></div>`;
}
