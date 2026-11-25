/* Token Boss: find agents that waste tokens, get recommendations, apply them, undo them.
   API: /api/tokenboss/*. Helpers (AU) are in apps.js. */
const TB={tab:'agents',filter:{search:'',deleted:false,since:true,high:5000},review:null,single:false,
  hist:{agent:null,sel:null},open:new Set(),flash:{},state:null,showSql:new Set(),score:null,replace:false};
AU.mounts.tokenboss=async el=>{
  const live=()=>el.isConnected,body=()=>el.querySelector('#tbbody'),$q=s=>el.querySelector(s);
  const TABS=[['agents','Agents'],['performance','Performance'],['history','History'],['setup','Setup']];
  const money=x=>x==null?'':(x>=1?'$'+x.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}):x>=0.01?'$'+x.toFixed(3):'$'+x.toFixed(4));
  const num=x=>x==null?'':Number(x).toLocaleString();
  const LOW=3;

  async function shell(){
    el.innerHTML=`<div id="tbstatus" class="panel pad" style="margin-bottom:14px">Checking…</div><div id="tbtabs">${AU.tabs(TABS,TB.tab)}</div><div id="tbbody" style="margin-top:14px"></div>`;
    await status();tab();
  }
  async function status(){
    const s=await AU.get('/api/tokenboss/state');if(!live())return;TB.state=s;
    if(s.high_default&&!TB.filter.touched)TB.filter.high=s.high_default;
    $q('#tbstatus').innerHTML=`<div style="display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center"><b>System status</b>
      <span>${s.conn_ok?'✅':'❌'} Database connection</span><span>${s.setup_complete?'✅':'❌'} Setup</span>
      ${s.setup_complete?'':'<span style="color:var(--muted)">Open the Setup tab to finish.</span>'}
      <button class="btn alt" data-act="refresh" style="margin-left:auto">🔄 Refresh</button></div>
      ${s.conn_error?AU.err('Cannot reach the database: '+s.conn_error):''}`;
  }
  const tab=()=>({agents,performance,history,setup}[TB.tab])();
  const notReady=()=>AU.msg('info','The monitor is not ready yet. Run the steps on the Setup tab, and check that the database user can read aidb_internal.');

  /* ------------------------------------------------------------ Agents */
  function agents(){
    const f=TB.filter;
    body().innerHTML=`<div class="frm" style="grid-template-columns:2fr 1fr 1fr;align-items:end">
      <label>Agent name<input id="tbsearch" value="${esc(f.search)}" placeholder="Wildcards: * and ?   for example demo_agent_cc*" title="* matches any text and ? matches one character. Without a wildcard, any agent whose name contains the text is shown."></label>
      <label class="chk" style="margin:0 0 9px"><input type="checkbox" id="tbdel" ${f.deleted?'checked':''}> Show deleted agents</label>
      <label>High tokens per task<input type="number" id="tbhigh" min="0" step="500" value="${f.high}" title="Agents above this estimated input tokens per task get the 'high tokens' flag."></label></div>
      <div class="toolbar"><button class="btn alt" data-act="reload">↻ Refresh</button>
        <label class="chk" style="margin:0"><input type="checkbox" id="tbsince" ${f.since?'checked':''}> Only count tasks since each agent's last change</label></div>
      <div id="tbscore">Loading…</div><div id="tbreview"></div>`;
    loadScore();drawReview();
  }
  async function loadScore(){
    const f=TB.filter,q=new URLSearchParams({search:f.search,show_deleted:f.deleted?1:0,since:f.since?1:0,high:f.high||0});
    const d=await AU.get('/api/tokenboss/scorecard?'+q);if(!live()||TB.tab!=='agents')return;
    const t=$q('#tbscore');if(!t)return;
    if(d.ok===false){t.innerHTML=AU.err('Could not load the scorecard: '+d.error);return;}
    if(!d.ready){t.innerHTML=notReady();return;}
    TB.score=d;drawScore();
  }
  function drawScore(){
    const d=TB.score,t=$q('#tbscore');if(!d||!t)return;
    const cols=['agent_name',{k:'review',label:' ',html:r=>r.can_review?`<button class="lnk" data-act="review" data-a="${esc(r.agent_name)}" title="Ask the reviewer agent for recommendations for this agent.">📝 Review</button>`:''},
      {k:'modified',html:r=>r.modified?`<button class="lnk" data-act="hist" data-a="${esc(r.agent_name)}" style="white-space:normal;text-align:left;max-width:170px" title="Open this agent's changes on the History tab.">${esc(r.modified)}</button>`:AU.cell(null)},
      'tasks','avg_rounds','tool_errors','compactions','compaction_errors','est_tokens_per_task','slowest_s','flags'];
    t.innerHTML=(!d.views_ok?AU.msg('info','Counting all tasks. Run step 2 (Schema, table and views) on the Setup tab to count only the tasks since each agent\'s last change.'):'')+
      (d.rows.length?`<p><b>${d.rows.length}</b> agent(s), <b>${d.flagged}</b> with flags</p>${AU.table(cols,d.rows,{hl:r=>TB.review&&r.agent_name===TB.review.agent})}
        <p style="color:var(--muted);font-size:13px">Click a column header to sort. Click Review next to an agent to get recommendations below the table. Click a Modified entry to see that agent's history.</p>`
        :AU.msg('info','No agents match. Try a different search, or tick Show deleted agents.'));
  }

  /* review panel */
  function drawReview(){
    const p=$q('#tbreview');if(!p)return;const R=TB.review;
    if(!R){p.innerHTML='';return;}
    let h=`<hr class="sep"><h2 id="tbrevh" style="margin-top:0">Recommendations for ${esc(R.agent)}</h2>`;
    if(R.state==='working')h+=AU.working(`The reviewer is analyzing ${R.agent}. This can take a minute…`);
    else if(R.error)h+=AU.err('The reviewer returned an error: '+R.error)+'<button class="btn alt" data-act="retry">Try again</button> <button class="lnk" data-act="cancel">Cancel</button>';
    else{
      const s=TB.score||{};
      h+=`<label class="chk"><input type="checkbox" data-act="edit" ${R.edit?'checked':''}> ✏️ Edit recommendation</label>`;
      h+=R.edit?`<textarea id="tbrec" class="sqlbox" style="min-height:320px">${esc(R.text)}</textarea>`:`<div class="md panel pad">${R.text?md(R.text):'<i>The reviewer returned no recommendation.</i>'}</div>`;
      if(!s.modifier_ok)h+='<p style="color:var(--muted)">The modifier agent <code>agent_modifier</code> does not exist yet. Run the steps on the Setup tab.</p>';
      h+=`<div style="margin-top:10px"><button class="btn" data-act="apply" ${R.text.trim()&&s.modifier_ok&&!R.applying?'':'disabled'} title="The modifier agent saves the current configuration, then applies the changes.">Apply recommendations</button> <button class="lnk" data-act="cancel">Cancel</button></div>`;
      if(R.applying)h+=AU.working(`The modifier is applying the changes to ${R.agent}…`);
      if(R.result){h+=R.result.error?AU.err('The modifier returned an error: '+R.result.error)
        :AU.msg('ok','The modifier agent replied:')+`<div class="md">${md(R.result.message||'')}</div><p style="color:var(--muted);font-size:13px">The previous configuration is saved in monitor.agent_config_history. To undo, open the History tab.</p>`;}
    }
    p.innerHTML=h;
  }
  async function startReview(agent){
    const R=TB.review={agent,state:'working',text:'',original:'',error:null,edit:false,result:null,applying:false};
    drawScore();drawReview();$q('#tbrevh')?.scrollIntoView({behavior:'smooth',block:'start'});
    const r=await AU.post('/api/tokenboss/review',{agent});
    let res;
    if(!r.ok)res={error:r.error};else{const j=await AU.job(r.job);res=j.result||{error:j.error||'No result.'};}
    if(TB.review!==R)return;
    R.state='done';R.error=res.error||null;R.text=res.text||'';R.original=R.text;
    if(live()&&TB.tab==='agents')drawReview();
  }
  async function applyReview(){
    const R=TB.review;R.text=$q('#tbrec')?$q('#tbrec').value:R.text;R.applying=true;R.result=null;drawReview();
    const r=await AU.post('/api/tokenboss/apply',{agent:R.agent,text:R.text,original:R.original});
    let res;
    if(!r.ok)res={error:r.error};else{const j=await AU.job(r.job);res=j.result||{error:j.error||'No result.'};}
    if(TB.review!==R)return;
    R.applying=false;R.result=res;if(live()&&TB.tab==='agents')drawReview();
  }
  const cancelReview=()=>{TB.review=null;drawScore();drawReview();window.scrollTo({top:0,behavior:'smooth'});};

  /* ------------------------------------------------------------ Performance */
  async function performance(){
    const b=body();
    b.innerHTML=`<div class="toolbar"><button class="btn alt" data-act="reload">↻ Refresh</button>
      <label class="chk" style="margin:0"><input type="checkbox" id="tbsingle" ${TB.single?'checked':''}> Include agents that have not been changed</label></div><div id="tbperf">Loading…</div>`;
    const d=await AU.get('/api/tokenboss/performance?include_single='+(TB.single?1:0));if(!live()||TB.tab!=='performance')return;
    const t=$q('#tbperf');
    if(d.ok===false){t.innerHTML=AU.err(d.error);return;}
    if(!d.ready){t.innerHTML=notReady();return;}
    if(d.info){t.innerHTML=AU.msg('info',esc(d.info));return;}
    if(!d.rows.length){t.innerHTML=AU.msg('info','No agent has been changed and run again yet. Apply a recommendation on the Agents tab and run the agent, or turn on the switch above to see every agent.');return;}
    t.innerHTML=`<p><b>${d.agents}</b> agent(s), <b>${d.rows.length}</b> version(s)</p>
      ${!d.costs_ok?AU.msg('info','Costs are not shown yet. Run step 2 on the Setup tab to add the model prices.'):d.unpriced.length?AU.msg('warn','No price for: '+esc(d.unpriced.join(', '))+'. Add it under Model prices on the Setup tab to see costs.'):''}
      <div class="panel pad">${chart(d.rows)}</div>
      <p style="color:var(--muted);font-size:13px">v1 is the configuration before any recorded change, and each change or undo starts the next version. Bars are split into input and output tokens, and the number above a bar is its version, with the estimated cost per task above it when the model has a price. An asterisk means fewer than ${LOW} tasks, so treat it with care. Different versions may have been tested with different questions.</p>
      <div class="md panel pad" style="margin:12px 0"><p><b>What are input and output tokens?</b> A token is a small chunk of text, roughly three-quarters of a word, and AI providers charge by the token.</p>
      <ul><li><b>Input</b> is everything the agent reads before it answers: your question, its standing instructions, the descriptions of its tools, and whatever the tools send back, such as database results.</li>
      <li><b>Output</b> is what the agent writes back: its answer, plus the requests it makes to tools.</li></ul>
      <p>Input usually outweighs output by a lot. The agent re-reads its instructions and earlier results on every round, so a long set of instructions or a large tool result is paid for again each time. That is why shorter instructions and smaller tool results save the most.</p></div>
      ${AU.table([ 'agent_name','version','tasks',{k:'model_id'},'avg_input_tokens','avg_output_tokens','avg_total_tokens',
        {k:'avg_cost',html:r=>AU.cell(money(r.avg_cost))},{k:'pct_change',label:'Pct Change',html:r=>AU.cell(r.pct_change==null?'':(r.pct_change>0?'+':'')+Math.round(r.pct_change)+'%')},'avg_rounds','change_reason'],d.rows)}`;
  }
  /* Grouped, stacked bars as inline SVG: one group per agent, one bar per version, split into input and output. */
  function chart(rows){
    const agents=[...new Set(rows.map(r=>r.agent_name))],W=Math.max(900,agents.length*150),H=380,L=64,B=96,T=28,pw=W-L-12,ph=H-B-T;
    const max=Math.max(...rows.map(r=>r.avg_total_tokens))||1,y=v=>T+ph-v/max*ph;
    const ticks=[0,.25,.5,.75,1].map(f=>Math.round(max*f));
    let g='';
    ticks.forEach(v=>{g+=`<line x1="${L}" x2="${W-12}" y1="${y(v)}" y2="${y(v)}" stroke="currentColor" stroke-opacity=".12"/><text x="${L-8}" y="${y(v)+4}" text-anchor="end" font-size="11" fill="currentColor" opacity=".7">${num(v)}</text>`;});
    const gw=pw/agents.length;
    agents.forEach((a,i)=>{
      const vs=rows.filter(r=>r.agent_name===a),bw=Math.min(46,gw*.8/vs.length),x0=L+i*gw+(gw-bw*vs.length)/2;
      vs.forEach((r,j)=>{
        const x=x0+j*bw,hin=r.avg_input_tokens/max*ph,hout=r.avg_output_tokens/max*ph,label=r.version+(r.tasks<LOW?'*':''),cost=money(r.avg_cost);
        const tip=`${a}\nVersion ${label}\nInput ${num(r.avg_input_tokens)}, output ${num(r.avg_output_tokens)}, total ${num(r.avg_total_tokens)}\n${cost?'Cost per task '+cost+'\n':''}Tasks ${r.tasks}\n${r.pct_change==null?'first version':(r.pct_change>0?'+':'')+Math.round(r.pct_change)+'% vs previous version'}${r.change_reason?'\nReason: '+r.change_reason:''}`;
        g+=`<g><title>${esc(tip)}</title><rect x="${x+1}" y="${y(r.avg_input_tokens)}" width="${bw-2}" height="${hin}" fill="#4c78a8"/><rect x="${x+1}" y="${y(r.avg_input_tokens)-hout}" width="${bw-2}" height="${hout}" fill="#f58518"/>
          <text x="${x+bw/2}" y="${y(r.avg_total_tokens)-(cost?18:5)}" text-anchor="middle" font-size="12" fill="currentColor">${esc(label)}</text>
          ${cost?`<text x="${x+bw/2}" y="${y(r.avg_total_tokens)-5}" text-anchor="middle" font-size="12" font-weight="700" fill="currentColor">${esc(cost)}</text>`:''}</g>`;
      });
      g+=`<text transform="translate(${L+i*gw+gw/2},${H-B+14}) rotate(-30)" text-anchor="end" font-size="12" fill="currentColor">${esc(a.length>34?a.slice(0,33)+'…':a)}</text>`;
    });
    g+=`<text transform="translate(14,${T+ph/2}) rotate(-90)" text-anchor="middle" font-size="12" fill="currentColor" opacity=".8">Average tokens per task (estimated)</text>
      <rect x="${L}" y="${H-14}" width="11" height="11" fill="#4c78a8"/><text x="${L+16}" y="${H-4}" font-size="12" fill="currentColor">Input</text>
      <rect x="${L+70}" y="${H-14}" width="11" height="11" fill="#f58518"/><text x="${L+86}" y="${H-4}" font-size="12" fill="currentColor">Output</text>`;
    return `<div style="overflow-x:auto"><svg viewBox="0 0 ${W} ${H}" style="width:100%;min-width:640px;height:auto;color:var(--fg)" role="img" aria-label="Average tokens per task by agent and version">${g}</svg></div>`;
  }

  /* ------------------------------------------------------------ History */
  async function history(agent){
    const b=body();if(agent)TB.hist.agent=agent;
    b.innerHTML='Loading…';
    const d=await AU.get('/api/tokenboss/history?agent='+encodeURIComponent(TB.hist.agent||(TB.review&&TB.review.agent)||''));if(!live()||TB.tab!=='history')return;
    if(d.ok===false){b.innerHTML=AU.err(d.error);return;}
    if(!d.available){b.innerHTML=AU.msg('info','The history table does not exist yet. Run the steps on the Setup tab.');return;}
    if(!d.choices.length){b.innerHTML=AU.msg('info','There are no agents yet.');return;}
    TB.hist.agent=d.agent;TB.hist.data=d;
    b.innerHTML=`<p style="color:var(--muted)">Every change the modifier agent or an undo made to an agent, the reviews behind them, and whether things got better afterwards. Token counts are estimates (characters ÷ 4).</p>
      ${TB.flash.history?TB.flash.history:''}
      <div class="frm" style="grid-template-columns:minmax(220px,420px)"><label>Agent<select id="tbhagent">${d.choices.map(c=>`<option value="${esc(c.name)}" ${c.name===d.agent?'selected':''}>${esc(c.label)}</option>`).join('')}</select></label></div>
      ${!d.exists?AU.msg('warn',`The agent ${esc(d.agent)} no longer exists, so its history is read-only.`)
        :`<details><summary>Current configuration</summary><p><b>Tools:</b> ${esc(d.current.tools.join(', ')||'none')}</p><pre class="code">${esc(d.current.instructions||'(no instructions)')}</pre></details>`}
      ${d.changes.length?`<p style="margin-top:14px"><b>${d.changes.length}</b> recorded change(s) for <b>${esc(d.agent)}</b></p>
        ${AU.table([{k:'details',label:'Details',html:r=>`<button class="lnk" data-act="detail" data-id="${r.id}">🔍 View</button>`},{k:'when'},{k:'action'},{k:'reason'},{k:'tasks',label:'Tasks (Before → After)'},{k:'tokens',label:'Est Tokens Per Task'},{k:'errors',label:'Tool Errors'}],d.changes,{hl:r=>r.id===TB.hist.sel})}
        <p style="color:var(--muted);font-size:13px">Before and after compare the tasks run since the previous change with those run since this one. A lower tokens or errors number after a change means it helped.</p>`
        :AU.msg('info',`No changes have been applied to ${esc(d.agent)}.`)}
      <div id="tbdetail"></div>
      <hr class="sep"><h2 style="margin-top:0">Recommendations applied to ${esc(d.agent)}</h2>
      ${d.reviews.length?d.reviews.map(r=>`<details><summary>${esc(r.when)}</summary><div class="md">${md(r.text||'')}</div>${r.edited?'<p style="color:var(--muted);font-size:13px">This is the edited text that was sent. The original recommendation was different.</p>':''}</details>`).join(''):'<p style="color:var(--muted)">None yet. A recommendation is saved here when you click Apply recommendations.</p>'}`;
    TB.flash.history='';
    if(TB.hist.sel&&d.changes.some(c=>c.id===TB.hist.sel))detail(TB.hist.sel);else TB.hist.sel=null;
  }
  async function detail(id){
    TB.hist.sel=id;const box=$q('#tbdetail');if(!box)return;box.innerHTML='Loading…';
    const d=await AU.get(`/api/tokenboss/history/change?agent=${encodeURIComponent(TB.hist.agent)}&id=${id}`);if(!live()||!$q('#tbdetail'))return;
    if(d.ok===false){box.innerHTML=AU.err(d.error);return;}
    const c=d.change;
    box.innerHTML=`<hr class="sep" id="tbdh"><h2 style="margin-top:0">Change ${d.id}: ${esc(d.when)}</h2>
      ${d.reason?`<p><b>Reason:</b> ${esc(d.reason)}</p>`:''}
      ${c?`<p><b>Instructions</b>${c.diff?'':': unchanged'}</p>${c.diff?`<pre class="code diff">${c.diff.split('\n').map(l=>`<span class="${l[0]==='+'&&l[1]!=='+'?'add':l[0]==='-'&&l[1]!=='-'?'del':''}">${esc(l)}</span>`).join('\n')}</pre>`:''}
        <p><b>Tools:</b> ${!c.added.length&&!c.removed.length?'unchanged':[c.added.length?'added '+esc(c.added.join(', ')):'',c.removed.length?'removed '+esc(c.removed.join(', ')):''].filter(Boolean).join('; ')}</p>`:''}
      ${d.recommendation?`<p><b>Recommendation behind this change</b>${d.recommendation.edited?' (edited before it was sent)':''}</p><div class="md panel pad">${md(d.recommendation.text||'')}</div>`:''}
      <div style="margin-top:12px">${d.undo==='gone'?'<span style="color:var(--muted)">The agent no longer exists, so this change cannot be undone.</span>'
        :d.undo==='already'?'<span style="color:var(--muted)">The agent already has the configuration from before this change.</span>'
        :`<button class="btn" data-act="undo" data-id="${d.id}" title="Saves the current configuration first, so this can be undone too. Later changes are undone as well.">↩ Restore the version before this change</button>`}
        <button class="lnk" data-act="closedetail">Close</button></div>`;
    box.querySelector('#tbdh').scrollIntoView({behavior:'smooth',block:'start'});
    const t=el.querySelector('[data-tid]');if(t&&AU.T[t.dataset.tid])t.outerHTML=AU.tableHtml(t.dataset.tid);
  }

  /* ------------------------------------------------------------ Setup */
  async function setup(){
    const b=body();b.innerHTML='Loading…';
    await status();const s=TB.state;if(!live()||TB.tab!=='setup')return;
    b.innerHTML=`<h2 style="margin-top:0">Setup</h2>
      <p style="color:var(--muted)">Creates everything the reviewer and modifier agents need. Each step can be run again on its own; it replaces what it created before. <b>Run all</b> runs steps 1 to 6 in order.</p>
      <form id="tbkeyform" class="panel pad" autocomplete="off"><label style="display:grid;gap:4px;max-width:520px;font-size:12px;color:var(--muted)">Claude API key
        <input id="tbkey" type="password" autocomplete="off" style="background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:7px 10px;height:36px;color:var(--fg)"></label>
        <p style="color:var(--muted);font-size:12.5px;margin:6px 0">Used to create the model (step 1), which Run all recreates. Sent to AIDB as a parameter; it is not displayed or stored by this app.</p>
        <button class="btn" data-act="runall" type="button">▶ Run all</button> <button class="btn alt" data-act="modelonly" type="button">Create model only</button></form>
      <div id="tball">${TB.flash.all||''}</div>
      <div style="margin:14px 0"><div class="track" style="height:8px"><div style="height:100%;width:${Math.round(s.done/s.total*100)}%;background:var(--ok);border-radius:3px"></div></div>
        <div style="color:var(--muted);font-size:13px;margin-top:4px">${s.done} of ${s.total} steps complete</div></div>
      <div class="cards">${s.groups.map(g=>`<div class="panel pad"><b>${esc(g.title)}</b> <span style="color:var(--muted)">${g.got}/${g.total}</span><div class="chips" style="margin-top:6px">
        ${g.chips.map(c=>`<span class="chip" style="cursor:default"><span class="dot" style="--c:${c.ok?'var(--ok)':'var(--mute)'}"></span>${esc(c.name)}</span>`).join('')}</div></div>`).join('')}</div>
      <div style="margin-top:14px">${s.steps.map(st=>{
        const open=!st.done||TB.open.has(st.key)||TB.showSql.has(st.key)||!!TB.flash['s_'+st.key];
        return `<details data-k="${st.key}" ${open?'open':''} class="panel" style="margin-bottom:8px"><summary style="padding:10px 14px;cursor:pointer">${st.done?'✅':'⬜'}&nbsp; ${st.n} · ${esc(st.title)}</summary><div class="pad" style="padding-top:0">
          ${TB.flash['s_'+st.key]||''}
          ${st.key==='model'?'<p style="color:var(--muted);font-size:13px">Use the API key box and <b>Create model only</b> at the top of this page. <code>:creds</code> in the SQL is your key as JSON: {"api_key": "…"}. It is never shown here.</p>'
            :`${st.missing.length?`<p style="color:var(--muted);font-size:13px">Needs first: ${esc(st.missing.join(', '))}.</p>`:''}<button class="btn alt" data-act="step" data-k="${st.key}" ${st.missing.length?'disabled':''}>Create ${esc(st.title.split(' (')[0].toLowerCase())}</button>`}
          <label class="chk"><input type="checkbox" data-act="sql" data-k="${st.key}" ${TB.showSql.has(st.key)?'checked':''}> Show SQL</label>
          ${TB.showSql.has(st.key)?`<pre class="code">${esc(st.sql)}</pre>`:''}</div></details>`;}).join('')}</div>
      <hr class="sep"><h2 style="margin-top:0">Model prices</h2>
      <p style="color:var(--muted)">What each model costs per million tokens, in US dollars. The Performance tab uses these to show the estimated cost per task. You can edit any price or add your own, for example a negotiated rate.</p>
      <div id="tbprices">Loading…</div>`;
    TB.flash={history:TB.flash.history};
    prices();
  }
  async function prices(flash){
    const d=await AU.get('/api/tokenboss/prices');const t=$q('#tbprices');if(!t||!live())return;
    if(d.ok===false){t.innerHTML=AU.err(d.error);return;}
    if(!d.available){t.innerHTML=AU.msg('info','Run step 2 (Schema, table and views) above to create the price table.');return;}
    t.innerHTML=`${flash||''}${d.unpriced.length?AU.msg('warn','Models used by your agents, now or in earlier versions, that have no price yet: '+esc(d.unpriced.join(', '))+'.'):d.used.length?`<p style="color:var(--muted);font-size:13px">Models your agents use or used: ${esc(d.used.join(', '))}. All have a price.</p>`:''}
      <div class="toolbar"><button class="btn alt" data-act="loadprices" ${d.used.length?'':'disabled'} title="Looks the models your agents use up in LiteLLM's public price list on GitHub.">Load prices from LiteLLM</button>
        <label class="chk" style="margin:0"><input type="checkbox" id="tbreplace" ${TB.replace?'checked':''}> Also replace prices that are already set</label></div>
      <div class="tw"><table style="min-width:0"><thead><tr><th>Model</th><th>Input $ per million</th><th>Output $ per million</th><th>Source</th><th></th></tr></thead><tbody id="tbprows">${d.rows.map(prow).join('')}</tbody></table></div>
      <div style="margin-top:10px"><button class="btn alt" data-act="addrow">+ Add model</button> <button class="btn" data-act="saveprices">Save prices</button></div><div id="tbpmsg"></div>`;
  }
  const prow=r=>{r=r||{};const i=(c,v,ty)=>`<td><input data-c="${c}" type="${ty||'text'}" ${ty==='number'?'min="0" step="any"':''} value="${esc(v??'')}" style="width:100%;min-width:90px;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:5px 8px;color:var(--fg)"></td>`;
    return `<tr data-prow>${i('model_id',r.model_id)}${i('input_per_mtok',r.input_per_mtok,'number')}${i('output_per_mtok',r.output_per_mtok,'number')}${i('source',r.source)}<td><button class="lnk" data-act="delrow" title="Remove">✕</button></td></tr>`;};

  async function runAll(){
    const key=$q('#tbkey').value.trim(),box=$q('#tball');
    box.innerHTML=AU.working('Starting setup…');
    const r=await AU.post('/api/tokenboss/setup/all',{api_key:key});
    if(!r.ok){box.innerHTML=AU.err(r.error);return;}
    const j=await AU.job(r.job,j=>{const x=$q('#tball');if(x)x.innerHTML=`<div class="panel pad" style="margin-top:12px"><b>Running… ${j.elapsed}s</b>
      <div class="track" style="margin:8px 0"><div style="height:100%;width:${Math.round((j.progress||0)*100)}%;background:var(--accent);border-radius:3px"></div></div><pre class="log" style="margin:0">${esc(j.lines.join('\n'))}</pre></div>`;});
    const res=j.result||{ok:false,message:j.error||'Failed.'};
    TB.flash.all=AU.msg(res.ok?'ok':'err',esc(res.message))+`<pre class="log" style="margin-top:0">${esc((j.lines||[]).join('\n'))}</pre>`;
    if(live()&&TB.tab==='setup')setup();
  }

  /* ------------------------------------------------------------ events */
  let timer;
  el.onclick=async e=>{
    const t=e.target.closest('[data-tab]');
    if(t){TB.tab=t.dataset.tab;el.querySelector('#tbtabs').innerHTML=AU.tabs(TABS,TB.tab);tab();return;}
    const b=e.target.closest('[data-act]');if(!b)return;const a=b.dataset.act;
    if(a==='refresh'){await status();tab();}
    else if(a==='reload')tab();
    else if(a==='review')startReview(b.dataset.a);
    else if(a==='retry')startReview(TB.review.agent);
    else if(a==='cancel')cancelReview();
    else if(a==='edit'){if(TB.review.edit&&$q('#tbrec'))TB.review.text=$q('#tbrec').value;TB.review.edit=b.checked;drawReview();}
    else if(a==='apply')applyReview();
    else if(a==='hist'){TB.hist.agent=b.dataset.a;TB.hist.sel=null;TB.tab='history';el.querySelector('#tbtabs').innerHTML=AU.tabs(TABS,TB.tab);history();}
    else if(a==='detail')detail(+b.dataset.id);
    else if(a==='closedetail'){TB.hist.sel=null;history();window.scrollTo({top:0,behavior:'smooth'});}
    else if(a==='undo'){b.disabled=true;const r=await AU.post('/api/tokenboss/undo',{agent:TB.hist.agent,history_id:+b.dataset.id});
      TB.flash.history=r.ok?AU.msg('ok',esc(r.message)):AU.err(r.error);TB.hist.sel=null;history();window.scrollTo({top:0,behavior:'smooth'});}
    else if(a==='runall')runAll();
    else if(a==='modelonly'||a==='step'){const k=a==='step'?b.dataset.k:'model';b.disabled=true;
      const r=await AU.post('/api/tokenboss/setup/step',{key:k,api_key:$q('#tbkey')?$q('#tbkey').value:''});
      const m=r.ok?AU.msg('ok',esc(r.message)):AU.err(r.error);
      if(k==='model')TB.flash.all=m;else TB.flash['s_'+k]=m;
      if(r.ok&&$q('#tbkey'))$q('#tbkey').value='';
      setup();}
    else if(a==='sql'){b.checked?TB.showSql.add(b.dataset.k):TB.showSql.delete(b.dataset.k);const d=b.closest('details');TB.open.add(b.dataset.k);setup();}
    else if(a==='loadprices'){b.disabled=true;const r=await AU.post('/api/tokenboss/prices/load',{replace:TB.replace});
      prices(r.ok?AU.msg({success:'ok',warning:'warn',error:'err',info:'info'}[r.kind]||'info',esc(r.message)):AU.err(r.error));}
    else if(a==='addrow')$q('#tbprows').insertAdjacentHTML('beforeend',prow());
    else if(a==='delrow')b.closest('tr').remove();
    else if(a==='saveprices'){
      const rows=[...el.querySelectorAll('[data-prow]')].map(tr=>Object.fromEntries([...tr.querySelectorAll('input')].map(i=>[i.dataset.c,i.value])));
      const r=await AU.post('/api/tokenboss/prices/save',{rows});
      if(r.ok)prices(AU.msg('ok',esc(r.message)));else $q('#tbpmsg').innerHTML=AU.err(r.error);}
  };
  el.addEventListener('toggle',e=>{const d=e.target;if(d.tagName==='DETAILS'&&d.dataset.k){d.open?TB.open.add(d.dataset.k):TB.open.delete(d.dataset.k);}},true);
  el.onchange=e=>{const i=e.target;
    if(i.id==='tbdel'){TB.filter.deleted=i.checked;loadScore();}
    else if(i.id==='tbsince'){TB.filter.since=i.checked;loadScore();}
    else if(i.id==='tbhigh'){TB.filter.high=+i.value||0;TB.filter.touched=true;loadScore();}
    else if(i.id==='tbsingle'){TB.single=i.checked;performance();}
    else if(i.id==='tbhagent'){TB.hist.agent=i.value;TB.hist.sel=null;history();}
    else if(i.id==='tbreplace')TB.replace=i.checked;
  };
  el.oninput=e=>{if(e.target.id==='tbsearch'){clearTimeout(timer);const v=e.target.value;timer=setTimeout(()=>{TB.filter.search=v;loadScore();},350);}};
  shell();
};
