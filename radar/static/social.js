/* Social + Research pages: every platform people talk on (posts, comment threads with replies, short video, live web,
   company accounts) and the research papers by and about each company. Data: /api/social*, /api/research (radar/social.py).
   Uses index.html's helpers ($, api, post, esc, fmt, ago, head, tabbed, toast, spark, STATS). */
(function(){
const X={};
const PL={x:["X","var(--ink)"],bluesky:["Bluesky","#1185fe"],hn:["Hacker News","#ff6600"],reddit:["Reddit","#ff4500"],lemmy:["Lemmy","#00bc8c"],
  mastodon:["Mastodon","#6364ff"],youtube:["YouTube Shorts","#ff0033"],whatsapp:["WhatsApp","#25d366"],substack:["Substack","#ff6719"],
  telegram:["Telegram","#2aabee"],web:["Live web","var(--s3)"],producthunt:["Product Hunt","#da552f"],instagram:["Instagram","#e1306c"],
  facebook:["Facebook","#1877f2"],linkedin:["LinkedIn","#0a66c2"],threads:["Threads","var(--ink-2)"]};
const plName=p=>(PL[p]||[p])[0];const plCol=p=>(PL[p]||[0,"var(--accent)"])[1];
const plChip=p=>`<span class="chip" style="--c:${plCol(p)}"><i></i>${esc(plName(p))}</span>`;
const chip=(t,c)=>`<span class="chip" style="${c?`border-color:${c};color:${c}`:""}">${esc(t)}</span>`;
const kpi=(l,v,s,c)=>`<div class="xk"><div class="l">${l}</div><div class="v num" style="${c?"color:"+c:""}">${v}</div>${s?`<div class="s">${s}</div>`:""}</div>`;
const tabsBar=(items,on,attr)=>`<div class="tabs inline">${items.map(([k,l])=>`<button class="${k===on?"on":""}" ${attr}="${esc(k)}">${esc(l)}</button>`).join("")}</div>`;
const STATE={live:["live","var(--good)"],needs_token:["needs token","var(--s4)"],links:["profile links","var(--muted)"],blocked:["blocked here","var(--s8)"],
  error:["error","var(--s8)"],pending:["first read pending","var(--muted)"],quiet:["no mentions yet","var(--muted)"],found:["found","var(--muted)"],unreachable:["blocked","var(--s8)"]};
const stChip=s=>{const [l,c]=STATE[s]||[s,"var(--muted)"];return chip(l,c);};
const sentDot=s=>s==null?"":`<span class="dotc" title="sentiment ${s}" style="display:inline-block;background:${s>0.15?"var(--good)":s<-0.15?"var(--s8)":"var(--axis)"}"></span>`;
const short=(t,n)=>{t=String(t||"").replace(/\s+/g," ").trim();return t.length>n?t.slice(0,n-1)+"…":t;};
const nf=n=>n>=1e6?(n/1e6).toFixed(n>=1e7?0:1)+"M":n>=1e3?(n/1e3).toFixed(n>=1e4?0:1)+"K":String(n||0);
const metrics=p=>[p.views?`▶ ${nf(p.views)}`:"",p.likes?`♥ ${nf(p.likes)}`:"",p.replies?`💬 ${nf(p.replies)}`:"",p.reposts?`↻ ${nf(p.reposts)}`:""].filter(Boolean).join(" · ");
const seatOf=b=>STATS&&STATS.self&&b===STATS.self;

function postRow(p){const txt=p.title||p.text||"";return`<div class="act" style="overflow-wrap:anywhere;min-width:0">
  <div class="meta">${plChip(p.platform)}<b>${esc(p.brand||"")}</b>${p.official?chip("official","var(--accent)"):""}${p.author?`<span>${esc(short(p.author,40))}${p.handle&&p.handle!==p.author?` <span class="muted">@${esc(short(p.handle,30))}</span>`:""}</span>`:""}<span style="margin-left:auto">${p.created?ago(p.created):""}</span></div>
  <div class="t" style="overflow-wrap:anywhere">${p.url?`<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(short(txt,260))}</a>`:esc(short(txt,260))}</div>
  ${p.title&&p.text&&!/^https?:/.test(p.text)?`<div class="why">${esc(short(p.text,220))}</div>`:""}
  <div class="imp">${sentDot(p.sentiment)}${metrics(p)||'<span class="muted">no engagement counts</span>'}</div></div>`;}

function commentNode(c,platform,depth){const kids=(c.replies||[]);return`<div class="act" style="margin-left:calc(${Math.min(depth,6)} * clamp(6px, 2.4vw, 20px));padding:10px 12px;min-width:0">
  <div class="meta">${sentDot(c.sentiment)}<b>${esc(short(c.author||"anonymous",40))}</b>${depth?`<span class="muted">reply</span>`:""}<span>${c.likes?`♥ ${nf(c.likes)}`:""}</span><span style="margin-left:auto">${c.created?ago(c.created):""}</span></div>
  <div class="why" style="color:var(--ink);overflow-wrap:anywhere">${esc(short(c.text,700))}</div>
  <div class="btns"><button class="sm" data-reply="${esc(platform)}" data-cid="${esc(c.comment_id)}">Draft a reply</button>${c.url?`<a class="small muted" href="${esc(c.url)}" target="_blank" rel="noopener">open on ${esc(plName(platform))} →</a>`:""}</div>
  <div class="replybox"></div></div>${kids.map(k=>commentNode(k,platform,depth+1)).join("")}`;}

function threadCard(t){const p=t.post;const head_=p.title||p.text||"(thread)";const first=t.tree.slice(0,4),rest=t.tree.slice(4);
  return`<div class="card" style="overflow-wrap:anywhere"><h2>${plChip(p.platform)} ${esc(p.brand||t.brand||"")} <span class="hint">${t.comments} comments · ${t.last?ago(t.last):""} ${sentDot(t.sentiment)}</span></h2>
   <div class="t" style="font-weight:600;margin-bottom:8px;overflow-wrap:anywhere">${p.url?`<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(short(head_,220))}</a>`:esc(short(head_,220))}</div>
   ${p.title&&p.text&&!/^https?:/.test(p.text)?`<div class="small muted" style="margin-bottom:8px">${esc(short(p.text,300))}</div>`:""}
   <div class="col" style="gap:8px">${first.map(c=>commentNode(c,p.platform,0)).join("")}</div>
   ${rest.length?`<details style="margin-top:8px"><summary class="small" style="cursor:pointer">${rest.length} more top-level comments</summary><div class="col" style="gap:8px;margin-top:8px">${rest.map(c=>commentNode(c,p.platform,0)).join("")}</div></details>`:""}</div>`;}

function bindReplies(root){root.querySelectorAll("[data-reply]").forEach(b=>b.onclick=async()=>{const box=b.closest(".act").querySelector(".replybox");b.disabled=true;b.textContent="Drafting…";
  try{const r=await post("/api/social/reply",{platform:b.dataset.reply,comment_id:b.dataset.cid});
    box.innerHTML=`<textarea rows="3" style="width:100%;margin-top:8px">${esc(r.draft)}</textarea><div class="row" style="margin-top:6px"><button class="sm primary" data-copyopen="${esc(r.url||"")}">Copy and open the comment</button><span class="muted small">${r.mode==="ai"?"AI draft":"template draft (add an AI key for tailored replies)"}, written for ${esc(STATS&&STATS.self?STATS.self:"a neutral analyst")}; you post it yourself</span></div>`;
    box.querySelector("[data-copyopen]").onclick=async e=>{const t=box.querySelector("textarea").value;try{await navigator.clipboard.writeText(t);toast("Reply copied: paste it on the platform");}catch(_){toast("Select the text and copy it");}const u=e.target.dataset.copyopen;if(u)window.open(u,"_blank","noopener");};}
  catch(e){toast("Could not draft: "+e.message);}b.disabled=false;b.textContent="Draft a reply";});}

X.vSocial=async function(v){head("Social","every platform people talk on: posts, comment threads with replies, short video, live web, company accounts · refreshed every sweep");
  const [o,th,acc]=await Promise.all([api("/api/social?days=30"),api("/api/social/threads?days=30&limit=24"),api("/api/social/accounts")]);
  const live=o.status.filter(s=>s.state==="live");
  const before=`<div class="xkpis">${kpi("posts · 7 days",fmt(o.posts_7d),`${live.length} platforms live`)}${kpi("comments & replies · 7 days",fmt(o.comments_7d),`${th.length} active threads`)}${kpi("platforms live",`${live.length}<span class="small muted">/${o.status.length}</span>`,live.slice(0,4).map(s=>s.label).join(" · "))}${kpi("company accounts",fmt(o.accounts),"read off each company's website")}${kpi("activity · 14 days",spark(o.spark,110,26),"posts per day")}</div>`;
  const statusGrid=`<div class="grid gauto">${o.status.map(s=>`<div class="act"><div class="meta">${plChip(s.platform)}${stChip(s.state)}<span style="margin-left:auto">${s.last?ago(s.last):""}</span></div>
     <div class="small"><b>${fmt(s.posts)}</b> posts · <b>${fmt(s.comments)}</b> comments${s.accounts?` · <b>${s.accounts}</b> accounts`:""}</div>${s.why?`<div class="small muted">${esc(s.why)}</div>`:""}</div>`).join("")}</div>`;
  const brands=o.brands;const mx=Math.max(1,...brands.map(b=>b.posts));
  const sov=brands.length?`<table><tr><th>Company</th><th class="num">Posts · 30d</th><th>Share of voice</th><th class="num">Engagement</th><th>Sentiment</th><th>Where</th></tr>${brands.map(b=>`<tr class="go" onclick="location.hash='#/brand/${encodeURIComponent(b.brand)}'" style="${b.self?"background:color-mix(in srgb,var(--accent) 8%,transparent)":""}"><td><b>${esc(b.brand)}</b>${b.self?' <span class="pill">you</span>':""}</td><td class="num">${fmt(b.posts)}</td><td><div class="mini" style="width:160px"><div style="width:${b.posts/mx*100}%;background:${b.self?"var(--accent)":"var(--s1)"};position:static"></div></div><span class="small muted">${b.share_pct}%</span></td><td class="num">${fmt(b.engagement)}</td><td>${sentDot(b.sentiment)} <span class="small muted">${b.sentiment??"–"}</span></td><td>${Object.entries(b.platforms).sort((a,c)=>c[1]-a[1]).map(([p,n])=>`<span class="chip" style="--c:${plCol(p)}"><i></i>${esc(plName(p))} ${n}</span>`).join("")}</td></tr>`).join("")}</table>`:'<div class="empty">No posts yet: the social sensor reads every platform with the next sweep (or press Run).</div>';
  const top=o.top.length?o.top.map(postRow).join(""):'<div class="empty">Nothing with engagement in the last 14 days yet.</div>';
  const threads=th.length?th.map(threadCard).join(""):'<div class="empty">No comment threads yet. Hacker News, Bluesky, Reddit, Lemmy, Substack and YouTube threads appear here with every reply.</div>';
  const shortsPosts=(await api("/api/social/posts?days=60&limit=60&kind=short")).concat(await api("/api/social/posts?days=60&limit=30&kind=reel"));
  const shorts=shortsPosts.length?`<div class="grid gauto">${shortsPosts.sort((a,b)=>(b.views||b.likes||0)-(a.views||a.likes||0)).map(p=>`<a class="card" style="padding:10px;display:grid;gap:6px;text-decoration:none" href="${esc(p.url)}" target="_blank" rel="noopener">${p.thumb?`<img src="${esc(p.thumb)}" alt="" loading="lazy" style="width:100%;aspect-ratio:16/10;object-fit:cover;border-radius:12px">`:""}<div class="small"><b>${esc(p.brand)}</b> ${plChip(p.platform)}</div><div class="small">${esc(short(p.title||p.text,90))}</div><div class="small muted">${metrics(p)}</div></a>`).join("")}</div>`:'<div class="empty">Shorts arrive a few times a day; Instagram Reels need a Meta token (see the Platforms tab).</div>';
  const web=await api("/api/social/posts?days=7&limit=80&platform=web");const ph=await api("/api/social/posts?days=60&limit=30&platform=producthunt");
  const webHtml=(web.length?web.map(postRow).join(""):'<div class="empty">Live web coverage (GDELT, news sites worldwide) arrives with the next sweep.</div>')+(ph.length?`<h3 style="margin-top:16px">Product Hunt launches</h3>${ph.map(postRow).join("")}`:"");
  const plats=acc.platforms;const accHtml=`<table><tr><th>Company</th>${plats.map(p=>`<th>${esc(plName(p))}</th>`).join("")}</tr>${acc.brands.map(b=>`<tr><td><b>${esc(b.brand)}</b>${b.self?' <span class="pill">you</span>':""}</td>${plats.map(p=>{const a=b.accounts[p];if(!a)return'<td class="muted small">–</td>';const [l,c]=STATE[a.status]||[a.status,"var(--muted)"];return`<td class="small" title="${esc(a.note||l)}"><span class="dotc" style="display:inline-block;background:${c}"></span> <a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(short(String(a.handle).replace(/^(company|channel|c)\//,""),22))}</a>${a.followers?`<div class="muted">${nf(a.followers)} followers</div>`:""}${a.posts_30d?`<div class="muted">${a.posts_30d} posts · 30d</div>`:""}</td>`;}).join("")}</tr>`).join("")}</table>
   <div class="small muted" style="margin-top:8px">Handles are read off each company's website weekly (override with <code>socials:</code> in companies.yaml). Instagram and Facebook show profiles only to signed-in users: set <code>INSTAGRAM_ACCESS_TOKEN</code> + <code>INSTAGRAM_BUSINESS_ID</code> and <code>FACEBOOK_ACCESS_TOKEN</code> (Meta Graph API) to read followers, posts and Reels.</div>`;
  const F=JSON.parse(sessionStorage.getItem("socf")||"{}");const plat=F.platform||"";const brandF=F.brand||"";
  const stream=await api(`/api/social/posts?days=30&limit=150${plat?"&platform="+plat:""}${brandF?"&brand="+encodeURIComponent(brandF):""}`);
  const streamHtml=`<div class="row wrap maptools">${tabsBar([["","All"],...live.map(s=>[s.platform,s.label])],plat,"data-plat")}<select class="pillsel" id="socbrand" aria-label="company"><option value="">every company</option>${(STATS.brands||[]).map(b=>`<option ${b.name===brandF?"selected":""}>${esc(b.name)}</option>`).join("")}</select></div>${stream.length?stream.map(postRow).join(""):'<div class="empty">No posts match.</div>'}`;
  tabbed(v,[{key:"overview",label:"Overview",html:`<div class="grid" style="grid-template-columns:1fr"><div class="card"><h2>Share of voice <span class="hint">posts about each company, 30 days, every platform</span></h2>${sov}</div><div class="card"><h2>Most engaged <span class="hint">14 days · likes, replies, reposts, views</span></h2><div class="grid gauto">${top}</div></div></div>`},
    {key:"threads",label:"Conversations",count:th.length||null,html:`<div class="col" style="gap:16px">${threads}</div>`},
    {key:"posts",label:"Posts",html:`<div class="card">${streamHtml}</div>`},
    {key:"shorts",label:"Shorts & Reels",count:shortsPosts.length||null,html:`<div class="card"><h2>Short video <span class="hint">YouTube Shorts about each company (views) · Instagram Reels with a Meta token</span></h2>${shorts}</div>`},
    {key:"web",label:"Live web",count:web.length||null,html:`<div class="card"><h2>Live web <span class="hint">news sites worldwide in the last day (GDELT), live blogs flagged</span></h2><div class="col">${webHtml}</div></div>`},
    {key:"accounts",label:"Accounts",html:`<div class="card">${accHtml}</div>`},
    {key:"platforms",label:"Platforms",count:live.length,html:`<div class="card"><h2>Platforms <span class="hint">what is read, and what a platform needs when it is not</span></h2>${statusGrid}</div>`}],{before});
  bindReplies(v);
  v.querySelectorAll("[data-plat]").forEach(b=>b.onclick=()=>{sessionStorage.setItem("socf",JSON.stringify({...F,platform:b.dataset.plat}));sessionStorage.setItem("tab:social","posts");render();});
  const sel=$("#socbrand");if(sel)sel.onchange=()=>{sessionStorage.setItem("socf",JSON.stringify({...F,brand:sel.value}));sessionStorage.setItem("tab:social","posts");render();};};

X.vResearch=async function(v){head("Research","papers by and about each company, and the sector's most cited recent research · OpenAlex and arXiv, daily");
  const F=JSON.parse(sessionStorage.getItem("resf")||"{}");const brandF=F.brand||"";
  const d=await api(`/api/research?days=730${brandF?"&brand="+encodeURIComponent(brandF):""}`);
  const paper=p=>`<div class="act"><div class="meta">${chip(p.relation==="authored"?"by the company":p.relation==="sector"?"sector research":"about the company",p.relation==="authored"?"var(--good)":p.relation==="sector"?"var(--s7)":"var(--s1)")}<b>${esc(p.brand==="Sector"?"":p.brand)}</b><span>${esc(p.venue||p.source)}</span><span style="margin-left:auto">${esc(p.published||"")}</span></div>
    <div class="t"><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(short(p.title,240))}</a></div>${p.authors?`<div class="small muted">${esc(short(p.authors,160))}</div>`:""}${p.abstract?`<div class="why">${esc(short(p.abstract,320))}</div>`:""}
    <div class="imp">${p.cited_by?`cited ${fmt(p.cited_by)} times`:"new"}${p.topic?` · ${esc(p.topic)}`:""}</div></div>`;
  const by=d.by_brand;const mine=d.papers.filter(p=>p.relation==="authored"),about=d.papers.filter(p=>p.relation==="about"),sector=d.papers.filter(p=>p.relation==="sector");
  const before=`<div class="xkpis">${kpi("papers by the companies",fmt(d.authored),`${d.institutions} companies publish as research institutions`)}${kpi("papers about them",fmt(d.about),"title or abstract names the company")}${kpi("sector research",fmt(d.sector),"most cited, last 8 months")}${kpi("topics",fmt(d.topics.length),d.topics.slice(0,2).map(t=>t.topic).join(" · "))}</div>
   <div class="row wrap" style="margin-bottom:14px"><select class="pillsel" id="resbrand" aria-label="company"><option value="">every company</option>${(STATS.brands||[]).map(b=>`<option ${b.name===brandF?"selected":""}>${esc(b.name)}</option>`).join("")}</select><button class="sm" id="resrun">Refresh research</button></div>`;
  const table=by.length?`<table><tr><th>Company</th><th class="num">By them</th><th class="num">About them</th><th class="num">Citations</th></tr>${by.map(b=>`<tr class="go" data-rb="${esc(b.brand)}" style="${seatOf(b.brand)?"background:color-mix(in srgb,var(--accent) 8%,transparent)":""}"><td><b>${esc(b.brand)}</b></td><td class="num">${b.authored}</td><td class="num">${b.about}</td><td class="num">${fmt(b.cited)}</td></tr>`).join("")}</table>`:'<div class="empty">Research is read once a day; press Refresh research to read it now.</div>';
  const topics=d.topics.length?d.topics.map(t=>`<div class="bar"><span title="${esc(t.topic)}">${esc(short(t.topic,30))}</span><div class="tr"><div class="f" style="width:${t.n/d.topics[0].n*100}%;background:var(--s7)"></div></div><span class="v">${t.n}</span></div>`).join(""):'<div class="empty">no topics yet</div>';
  tabbed(v,[{key:"companies",label:"By company",html:`<div class="grid g2"><div class="card"><h2>Who publishes <span class="hint">two years · click a row to filter</span></h2>${table}</div><div class="card"><h2>Topics <span class="hint">OpenAlex primary topic</span></h2>${topics}</div></div>`},
    {key:"by",label:"By the companies",count:mine.length||null,html:`<div class="col">${mine.map(paper).join("")||'<div class="empty">No papers with authors at these companies yet.</div>'}</div>`},
    {key:"about",label:"About them",count:about.length||null,html:`<div class="col">${about.map(paper).join("")||'<div class="empty">No papers naming these companies yet.</div>'}</div>`},
    {key:"sector",label:"Sector research",count:sector.length||null,html:`<div class="col">${sector.map(paper).join("")||'<div class="empty">Sector research arrives with the daily read.</div>'}</div>`}],{before});
  const sel=$("#resbrand");if(sel)sel.onchange=()=>{sessionStorage.setItem("resf",JSON.stringify({brand:sel.value}));render();};
  v.querySelectorAll("[data-rb]").forEach(r=>r.onclick=()=>{sessionStorage.setItem("resf",JSON.stringify({brand:r.dataset.rb}));render();});
  const rb=$("#resrun");if(rb)rb.onclick=async()=>{rb.disabled=true;try{const r=await post("/api/collect/research");toast(r.state==="running"?"Reading research now: refresh in a few minutes":(r.message||r.state));}catch(e){toast("Failed: "+e.message);}rb.disabled=false;};};

Object.assign(window,X);
})();
