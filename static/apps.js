/* The demo apps as native pages: shared helpers, EDB Agent Converse, Customs Document Validation.
   Token Boss is in tokenboss.js. Data comes from the JSON APIs in webapps/ (/api/converse, /api/validation,
   /api/tokenboss). esc() and md() come from index.html. */
const AU={T:{},seq:0,mounts:{}};
AU.get=async u=>{try{return await (await fetch(u)).json();}catch(e){return {ok:false,error:String(e.message||e)};}};
AU.post=async(u,b)=>{try{return await (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})})).json();}
  catch(e){return {ok:false,error:String(e.message||e)};}};
AU.sleep=ms=>new Promise(r=>setTimeout(r,ms));
/* Poll a background job until it finishes. tick(job) is called on every poll. */
AU.job=async(id,tick)=>{for(;;){const j=await AU.get('/api/jobs/'+id);if(tick)tick(j);if(!j.ok||j.done)return j;await AU.sleep(1000);}};
AU.msg=(kind,html)=>`<div class="msg ${kind}">${html}</div>`;
AU.err=t=>AU.msg('err',esc(t));
AU.working=t=>`<div class="working"><div class="ring"></div><div class="wtext"><b>${esc(t)}</b><div class="track"><div class="bar"></div></div></div></div>`;
AU.pretty=k=>({n:'Count',slowest_s:'Slowest (Seconds)',history_id:'Id',avg_cost:'Avg Cost Per Task',model_id:'Model',
  est_tokens_per_task:'Est Tokens Per Task'}[k]||String(k).replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase()));
AU.cell=v=>v==null||v===''?'<span style="color:var(--muted)">&ndash;</span>':esc(typeof v==='object'?JSON.stringify(v):v);
AU.ok=v=>`<span class="pill ${v?'p-ok':'p-mute'}">${v?'✓':'○'}</span>`;
AU.session=(k,v)=>{try{if(v===undefined)return JSON.parse(sessionStorage.getItem(k)||'null');sessionStorage.setItem(k,JSON.stringify(v));}catch(e){return null;}};

/* Sortable table. cols: key or {k,label,html(row)}. o.hl(row) shades a row. Click a header to sort. */
AU.table=(cols,rows,o={})=>{const id='t'+(++AU.seq);AU.T[id]={cols:cols.map(c=>typeof c==='string'?{k:c}:c),rows,o,sort:null,dir:1};return AU.tableHtml(id);};
AU.tableHtml=id=>{
  const t=AU.T[id];let rows=t.rows.slice();
  if(t.sort){const k=t.sort;rows.sort((a,b)=>{const x=a[k],y=b[k];if(x==null)return 1;if(y==null)return -1;
    return (typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y),undefined,{numeric:true}))*t.dir;});}
  return `<div class="tw app" data-tid="${id}"><table style="min-width:0"><thead><tr>${t.cols.map(c=>
    `<th data-k="${esc(c.k)}" style="cursor:pointer" title="Click to sort">${esc(c.label||AU.pretty(c.k))}${t.sort===c.k?(t.dir>0?' ▲':' ▼'):''}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r=>`<tr${t.o.hl&&t.o.hl(r)?' class="hl"':''}>${t.cols.map(c=>`<td${typeof r[c.k]==='string'&&r[c.k].length>28?' class="w"':''}>${c.html?c.html(r):AU.cell(r[c.k])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
};
document.addEventListener('click',e=>{
  const th=e.target.closest('th[data-k]'),w=th&&th.closest('[data-tid]');if(!w)return;
  const t=AU.T[w.dataset.tid];if(!t)return;
  t.dir=t.sort===th.dataset.k?-t.dir:1;t.sort=th.dataset.k;w.outerHTML=AU.tableHtml(w.dataset.tid);
});
AU.tabs=(items,cur)=>`<div class="seg tabs">${items.map(([k,l])=>`<button data-tab="${k}" aria-pressed="${k===cur}">${esc(l)}</button>`).join('')}</div>`;
/* Plain table for {columns, rows} from the API. link: {key: row=>html} for special cells. */
AU.dtable=(d,link={},o={})=>AU.table(d.columns.map(c=>link[c]?{k:c,html:link[c]}:c),d.rows,o);
AU.pdfLink=r=>r.pdf_url?`<a href="${esc(r.pdf_url)}" target="_blank" rel="noopener">${esc(decodeURIComponent(r.pdf_url.split('/').pop()))}</a>`:AU.cell(null);

window.AppsUI={mount(key,el){AU.T={};const m=AU.mounts[key];if(m)m(el);else el.innerHTML='<div class="empty">Unknown app.</div>';}};

/* ===================================================================== EDB Agent Converse */
const CV=AU.session('cv')||{convs:[],active:null,debug:false};
CV.agents=null;CV.err='';CV.busy=false;
AU.mounts.converse=async el=>{
  const save=()=>AU.session('cv',{convs:CV.convs,active:CV.active,debug:CV.debug});
  const live=()=>el.isConnected;
  const active=()=>CV.convs.find(c=>c.id===CV.active);
  function side(){
    return `<aside class="cvside panel pad">
      ${CV.convs.length?'<button class="btn alt" data-act="new" style="width:100%;margin-bottom:10px">➕ New conversation</button>':''}
      <label class="chk"><input type="checkbox" data-act="debug" ${CV.debug?'checked':''}> Debug</label>
      <h4 style="margin:12px 0 6px;font:600 12px var(--f-head);letter-spacing:.07em;text-transform:uppercase;color:var(--muted)">Recents</h4>
      ${CV.convs.length?[...CV.convs].reverse().map(c=>`<button class="cvitem ${c.id===CV.active?'on':''}" data-act="open" data-id="${c.id}" title="${esc(c.title)}">${esc(c.title)}</button>`).join(''):'<div style="color:var(--muted);font-size:13px">No conversations yet</div>'}
    </aside>`;
  }
  function main(){
    const a=active(),ag=CV.agents||[];
    if(!a){
      return `<div class="cvhome"><div class="cvlogo">EDB&nbsp;Agent&nbsp;Converse</div><div class="cvtag">Ask an EDB Postgres AI agent</div>
        <select id="cvagent" class="cvsel" ${ag.length?'':'disabled'}>${ag.length?ag.map(x=>`<option>${esc(x)}</option>`).join(''):'<option>No agents found</option>'}</select>
        ${!ag.length?(CV.err?AU.err('Could not read the list of agents: '+CV.err):AU.msg('warn','<b>No agents found.</b> Create one with <code>aidb.create_agent()</code>. The demo Jupyter notebooks include examples that create agents. Then refresh this page.')):''}
        <form id="cvform" class="cvform"><input id="cvq" placeholder="Ask your agent anything..." autocomplete="off" ${ag.length&&!CV.busy?'':'disabled'}><button class="btn" ${ag.length&&!CV.busy?'':'disabled'}>Search</button></form></div>`;
    }
    return `<div style="color:var(--muted);margin-bottom:8px">Agent: <b style="color:var(--fg)">${esc(a.agent)}</b>${a.cid?` · conversation <code>${esc(a.cid)}</code>`:''}</div>
      <div class="cvmsgs">${a.messages.map(m=>`<div class="cvmsg ${m.role}"><div class="who">${m.role==='user'?'You':'Agent'}</div><div class="md">${md(m.content)}</div>
        ${m.extra?`<details><summary>Debug</summary><pre class="code">${esc(JSON.stringify(m.extra,null,2))}</pre></details>`:''}</div>`).join('')}
        ${CV.busy?AU.working('Agent is thinking…'):''}</div>
      <form id="cvform" class="cvform"><input id="cvq" placeholder="Ask a follow-up..." autocomplete="off" ${CV.busy?'disabled':''}><button class="btn" ${CV.busy?'disabled':''}>Send</button></form>`;
  }
  function draw(){if(!live())return;el.innerHTML=`<div class="cvwrap">${side()}<div class="cvmain">${main()}</div></div>`;
    const q=el.querySelector('#cvq');if(q&&!CV.busy)q.focus();const m=el.querySelector('.cvmsgs');if(m)m.lastElementChild?.scrollIntoView({block:'nearest'});}
  async function ask(agent,question){
    let a=active();
    if(!a){a={id:Math.random().toString(16).slice(2),title:question.slice(0,40),agent,messages:[],cid:null};CV.convs.push(a);CV.active=a.id;}
    a.messages.push({role:'user',content:question});CV.busy=true;save();draw();
    const r=await AU.post('/api/converse/ask',{agent:a.agent,message:question,conversation_id:a.cid,debug:CV.debug});
    CV.busy=false;
    if(r.ok){a.cid=r.conversation_id;a.messages.push({role:'assistant',content:r.answer,extra:r.extra||null});}
    else a.messages.push({role:'assistant',content:'⚠️ Error: '+r.error});
    save();draw();
  }
  el.onclick=e=>{const b=e.target.closest('[data-act]');if(!b)return;
    if(b.dataset.act==='new'){CV.active=null;save();draw();}
    else if(b.dataset.act==='open'){CV.active=b.dataset.id;save();draw();}
    else if(b.dataset.act==='debug'){CV.debug=b.checked;save();}};
  el.onsubmit=e=>{e.preventDefault();const q=el.querySelector('#cvq').value.trim();if(!q||CV.busy)return;
    const sel=el.querySelector('#cvagent');ask(active()?active().agent:sel&&sel.value,q);};
  draw();
  const r=await AU.get('/api/converse/agents');CV.agents=r.agents||[];CV.err=r.error||'';draw();
};

/* ===================================================================== Customs Document Validation */
const VA={tab:'upload',sel:new Set(),confirm:null,note:'',chat:[],busy:false,proc:null,log:null,filter:{inv:'All',types:null},ruleType:'All'};
AU.mounts.validation=async el=>{
  const live=()=>el.isConnected,body=()=>el.querySelector('#vabody');
  const TABS=[['upload','1 · Upload & process'],['findings','2 · Findings'],['rules','3 · Rules'],['ask','4 · Ask the validator']];
  async function shell(){
    el.innerHTML=`<div id="vastatus" class="panel pad" style="margin-bottom:14px">Checking…</div><div id="vatabs">${AU.tabs(TABS,VA.tab)}</div><div id="vabody" style="margin-top:14px"></div>`;
    const s=await AU.get('/api/validation/status');if(!live())return;
    el.querySelector('#vastatus').innerHTML=`<div style="display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center"><b>System status</b>
      ${(s.checks||[]).map(c=>`<span>${c.ok?'✅':'❌'} ${esc(c.label)}</span>`).join('')}</div>
      ${s.error?AU.err('Cannot reach the database: '+s.error):''}
      ${(s.checks||[]).some(c=>!c.ok)?AU.msg('info','Run the setup cells of the demo notebook first (model, volume, pipeline, agents, rules, views).'):''}
      <div style="color:var(--muted);font-size:12.5px;margin-top:6px">PDF folder: <code>${esc(s.pdf_dir||'')}</code></div>`;
    tab();
  }
  const tab=()=>({upload,findings,rules,ask}[VA.tab])();
  async function upload(){
    const b=body();b.innerHTML='Loading…';
    const d=await AU.get('/api/validation/documents');if(!live())return;
    const rows=d.rows||[];
    const all=rows.length&&rows.every(r=>VA.sel.has(r.file));
    b.innerHTML=`<h2 style="margin-top:0">Upload documents</h2>
      <div class="panel pad"><label style="display:block;color:var(--muted);margin-bottom:8px">Invoices, packing lists, bills of lading, pedimentos, CBP 7501s, USMCA certifications</label>
      <input type="file" id="vafiles" accept=".pdf,application/pdf" multiple> <button class="btn" data-act="save" id="vasave">Save uploaded files</button>
      <div id="vaupmsg"></div></div>
      <h2>Documents in the system</h2>
      ${d.ok===false?AU.err(d.error):rows.length?`<label class="chk"><input type="checkbox" data-act="all" ${all?'checked':''}> Select all</label>
        ${AU.table([{k:'delete',label:'Delete',html:r=>`<input type="checkbox" data-act="sel" data-f="${esc(r.file)}" ${VA.sel.has(r.file)?'checked':''}>`},'file','doc_type','invoice_no','status'],rows)}
        <div style="margin-top:10px"><button class="btn alt" data-act="del" ${VA.sel.size?'':'disabled'}>Delete selected (${VA.sel.size})</button></div>`
        :AU.msg('info','No documents yet. Upload PDFs above.')}
      ${VA.confirm?(()=>{const n=VA.confirm,shown=n.slice(0,8).join(', ')+(n.length>8?` … and ${n.length-8} more`:'');
        return `<div class="msg warn">Permanently delete ${n.length} PDF(s) and their extracted data? ${esc(shown)}
        <div style="margin-top:8px"><button class="btn" data-act="yes">Yes, delete</button> <button class="btn alt" data-act="no">Cancel</button></div></div>`;})():''}
      ${VA.note?VA.note:''}
      <h2>Process documents</h2>
      <p>Reads the PDFs into text (AI pipeline), then extracts the fields from each document. This can take a few minutes.</p>
      <label class="chk"><input type="checkbox" id="vaRedo"> Re-extract every document (use if you replaced a file with the same name)</label>
      <button class="btn" data-act="process" ${VA.proc==='running'?'disabled':''}>Process documents</button>
      <div id="vaproc">${procHtml()}</div>`;
  }
  function procHtml(){
    const l=VA.log;if(!l)return '';
    let h='';
    if(VA.proc==='running'||l.lines.length){h+=`<div class="panel pad" style="margin-top:12px">${VA.proc==='running'?`<b>Processing… ⏱ ${l.elapsed||0}s elapsed</b>`:'<b>Processing finished</b>'}
      ${l.progress!=null?`<div class="track" style="margin:8px 0"><div style="height:100%;width:${Math.round(l.progress*100)}%;background:var(--accent);border-radius:3px"></div></div>`:''}
      <pre class="log" style="margin-top:8px">${esc(l.lines.join('\n'))}</pre></div>`;}
    if(l.done){
      if(l.error)h+=AU.err('Processing failed: '+l.error);
      else{const r=l.result||{};
        if((r.failed||[]).length||r.errors){h+=AU.msg('warn','Some documents failed extraction. Click Process again to retry them.'+((r.failed||[]).length?'<ul>'+r.failed.map(x=>`<li>${esc(x)}</li>`).join('')+'</ul>':''));
          if(r.errors)h+=AU.dtable(r.errors);}
        else h+=AU.msg('ok','All documents processed. See the Findings tab.');}}
    return h;
  }
  async function findings(){
    const b=body();b.innerHTML='Loading…';
    const d=await AU.get('/api/validation/findings');if(!live())return;
    if(d.ok===false){b.innerHTML=AU.err(d.error);return;}
    if(!d.available){b.innerHTML=AU.msg('info','No findings available yet. Process some documents first.');return;}
    const f=d.findings,m=d.metrics,col=n=>f.columns.includes(n);
    const invoices=['All',...[...new Set(f.rows.map(r=>r.invoice_no).filter(x=>x!=null))].sort()];
    const types=[...new Set(f.rows.map(r=>r.finding_type))].sort();
    if(!VA.filter.types)VA.filter.types=new Set(types);
    const view=()=>f.rows.filter(r=>VA.filter.types.has(r.finding_type)&&(VA.filter.inv==='All'||r.invoice_no===VA.filter.inv));
    const link={pdf_url:AU.pdfLink};
    const tbl=()=>AU.dtable({columns:f.columns,rows:view()},link);
    b.innerHTML=`<div class="grid" style="max-width:560px"><div class="tile"><b>${m.shipments}</b><span>Shipments</span></div><div class="tile"><b>${m.ready}</b><span>Ready to file</span></div><div class="tile"><b>${m.attention}</b><span>Need attention</span></div></div>
      <h2>Shipments</h2>${AU.dtable(d.shipments)}
      <h2>Documents with missing or incomplete elements</h2>${d.doc_issues?AU.dtable(d.doc_issues,link):AU.msg('ok','No document-level problems found.')}
      <h2>All findings</h2>
      <div class="toolbar"><label>Shipment <select id="vainv">${invoices.map(i=>`<option ${i===VA.filter.inv?'selected':''}>${esc(i)}</option>`).join('')}</select></label></div>
      <div class="seg" id="vatypes" style="margin-bottom:10px">${types.map(t=>`<button data-act="type" data-t="${esc(t)}" aria-pressed="${VA.filter.types.has(t)}">${esc(t)}</button>`).join('')}</div>
      <div id="vaall">${tbl()}</div>
      <p style="color:var(--muted);font-size:13px">found_elsewhere shows where a missing value already appears in another document of the same shipment, so the fix can be a copy-and-paste.</p>`;
    VA.redraw=()=>{const a=el.querySelector('#vaall');if(a)a.innerHTML=tbl();};
  }
  async function rules(){
    const b=body();b.innerHTML='Loading…';
    const d=await AU.get('/api/validation/rules');if(!live())return;
    const types=d.available?['All',...[...new Set(d.rows.map(r=>r.doc_type))].sort()]:[];
    const rows=()=>d.rows.filter(r=>VA.ruleType==='All'||r.doc_type===VA.ruleType);
    b.innerHTML=`<h2 style="margin-top:0">Required fields by document type</h2><p>Every document of a given type must contain these fields, otherwise a finding is raised.</p>
      ${d.available?`<div class="toolbar"><label>Document type <select id="varule">${types.map(t=>`<option ${t===VA.ruleType?'selected':''}>${esc(t)}</option>`).join('')}</select></label></div>
        <div id="varules">${AU.table([{k:'doc_type',label:'Document type'},{k:'field',label:'Required field'},{k:'rule',label:'Regulation / reason'}],rows())}</div>`
        :AU.msg('info','The required_fields table does not exist yet.')}
      <p><b>Cross-document checks</b> (built into the findings view): short HTS codes (under 10 digits), missing unit prices, vague descriptions, and gross weight, goods value and quantity mismatches between documents of the same shipment.</p>`;
    VA.redraw=()=>{const a=el.querySelector('#varules');if(a)a.innerHTML=AU.table([{k:'doc_type',label:'Document type'},{k:'field',label:'Required field'},{k:'rule',label:'Regulation / reason'}],rows());};
  }
  function ask(){
    const b=body(),ex='Which shipments are ready to file and which are not?';
    b.innerHTML=`<h2 style="margin-top:0">Ask the validator</h2><p style="color:var(--muted)">Answers come only from the validation results in the database.</p>
      <button class="btn alt" data-act="ex" ${VA.busy?'disabled':''}>${esc(ex)}</button>
      <div class="cvmsgs" style="margin-top:12px">${VA.chat.map(m=>`<div class="cvmsg ${m.role}"><div class="who">${m.role==='user'?'You':'Validator'}</div><div class="md">${md(m.content)}</div></div>`).join('')}${VA.busy?AU.working('Checking…'):''}</div>
      <form id="vaform" class="cvform"><input id="vaq" placeholder="Ask about a shipment…" autocomplete="off" ${VA.busy?'disabled':''}><button class="btn" ${VA.busy?'disabled':''}>Send</button></form>
      ${VA.chat.length?`<button class="btn alt" data-act="clear" ${VA.busy?'disabled':''}>Clear conversation</button>`:''}`;
    if(!VA.busy)b.querySelector('#vaq').focus();
  }
  async function send(q){
    VA.chat.push({role:'user',content:q});VA.busy=true;ask();
    const r=await AU.post('/api/validation/ask',{question:q});
    VA.chat.push({role:'assistant',content:r.ok?r.answer:'Could not reach the agent: '+r.error});VA.busy=false;
    if(live()&&VA.tab==='ask')ask();
  }
  async function process(redo){
    VA.proc='running';VA.log={lines:[],elapsed:0,done:false};VA.note='';upload2();
    const r=await AU.post('/api/validation/process',{redo});
    if(!r.ok){VA.proc=null;VA.log=null;body().insertAdjacentHTML('beforeend',AU.err(r.error));return;}
    const j=await AU.job(r.job,j=>{VA.log=j;const p=el.querySelector('#vaproc');if(p&&live())p.innerHTML=procHtml();});
    VA.log=j;VA.proc='done';if(live()&&VA.tab==='upload')upload();
  }
  const upload2=()=>{const p=el.querySelector('#vaproc');if(p)p.innerHTML=procHtml();const bt=el.querySelector('[data-act=process]');if(bt)bt.disabled=true;};
  const readB64=f=>new Promise((ok,no)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(',')[1]);r.onerror=no;r.readAsDataURL(f);});
  el.onclick=async e=>{
    const t=e.target.closest('[data-tab]');
    if(t){VA.tab=t.dataset.tab;el.querySelector('#vatabs').innerHTML=AU.tabs(TABS,VA.tab);tab();return;}
    const b=e.target.closest('[data-act]');if(!b)return;const a=b.dataset.act;
    if(a==='save'){const fs=[...el.querySelector('#vafiles').files],m=el.querySelector('#vaupmsg');
      if(!fs.length){m.innerHTML=AU.msg('info','Choose one or more PDF files first.');return;}
      b.disabled=true;m.innerHTML='Saving…';
      try{const files=await Promise.all(fs.map(async f=>({name:f.name,data:await readB64(f)})));
        const r=await AU.post('/api/validation/upload',{files});
        VA.note=r.ok?AU.msg('ok',`Saved ${r.saved} file(s). Now run the processing step below.`):AU.err('Could not save files: '+r.error);}
      catch(x){VA.note=AU.err('Could not save files: '+x);}
      upload();}
    else if(a==='all'){VA.sel=b.checked?new Set([...el.querySelectorAll('[data-act=sel]')].map(x=>x.dataset.f)):new Set();upload();}
    else if(a==='sel'){b.checked?VA.sel.add(b.dataset.f):VA.sel.delete(b.dataset.f);const d=el.querySelector('[data-act=del]');d.disabled=!VA.sel.size;d.textContent=`Delete selected (${VA.sel.size})`;}
    else if(a==='del'){VA.confirm=[...VA.sel];VA.note='';upload();}
    else if(a==='no'){VA.confirm=null;upload();}
    else if(a==='yes'){const n=VA.confirm;VA.confirm=null;const r=await AU.post('/api/validation/delete',{names:n});
      VA.note=r.ok?AU.msg('ok',`Deleted ${n.length} document(s).`):AU.err('Delete failed: '+r.error);VA.sel=new Set();upload();}
    else if(a==='process')process(el.querySelector('#vaRedo').checked);
    else if(a==='type'){const t=b.dataset.t;VA.filter.types.has(t)?VA.filter.types.delete(t):VA.filter.types.add(t);b.setAttribute('aria-pressed',VA.filter.types.has(t));VA.redraw();}
    else if(a==='ex')send('Which shipments are ready to file and which are not?');
    else if(a==='clear'){VA.chat=[];ask();}
  };
  el.onchange=e=>{
    if(e.target.id==='vainv'){VA.filter.inv=e.target.value;VA.redraw();}
    else if(e.target.id==='varule'){VA.ruleType=e.target.value;VA.redraw();}
  };
  el.onsubmit=e=>{e.preventDefault();const q=el.querySelector('#vaq').value.trim();if(q&&!VA.busy)send(q);};
  shell();
};
