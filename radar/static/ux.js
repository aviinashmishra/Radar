/* Pebble Radar - UX layer. Loaded last; works on whatever a view renders, so every page (including the extension
   views) gets the same behaviour without touching its code:
     - skeleton loading and a soft fade when a view arrives
     - long lists fold to a sensible length with "show N more"
     - repeated feed rows (same brand, kind and title in a row) collapse into one row with a count
     - every table sorts by clicking a column header (numbers, currencies, percentages and dashes understood)
     - every card collapses from its heading (remembered per page)
     - rows that lead somewhere highlight on hover; a back-to-top button; brand pages get a back link
     - keyboard: / or Ctrl K search, ? shortcuts, g then a letter to jump, Esc closes
   Nothing here changes data; it only organises what is already on the page. */
(function(){
const q=(s,r=document)=>r.querySelector(s), qa=(s,r=document)=>[...r.querySelectorAll(s)];
const base=()=>(location.hash.replace(/^#\/?/,"").split("/")[0]||"today");

/* ---------- 1. folding long lists ---------- */
const FOLDS=[["#acts",".act",6,"actions"],["#sigs",".act",5,"signals"],["#stories",".story",10,"stories"],["#rows",".fr",50,"changes"],["#t-tl",".fr",40,"events"],["#segpulse",".fr",15,"events"],
             ["#t-nw .card",".story",8,"stories"],["h2 + .grid.g2",".card",8,"brands"],["#vlist",".xq",40,"items"],["#t-rv .card",".xq",8,"quotes"],
             [".grid",".card:has(> .answer)",2,"older briefs"]];
/* long written text (briefs, reports, AI reads) clamps to a readable height with "read more" */
function clampLong(root){
  for(const el of qa("#view .card > .answer, #view .xrep",root)){
    if(el.dataset.uxClamp||el.closest(".ux-hide"))continue;
    if(el.scrollHeight<=520)continue;el.dataset.uxClamp="1";el.classList.add("ux-clamp");
    const b=document.createElement("button");b.className="sm ux-more";b.textContent="Read the whole thing";b.onclick=()=>{el.classList.remove("ux-clamp");b.remove();};el.after(b);
  }
}
function fold(root){
  for(const [cs,is,n,label] of FOLDS){
    for(const c of qa(cs,root)){
      if(c.dataset.uxOpen==="1"||c.querySelector(":scope > .ux-more"))continue;
      const items=qa(":scope > "+is,c).filter(el=>!el.classList.contains("ux-hide"));if(items.length<=n+2)continue;
      items.slice(n).forEach(el=>el.classList.add("ux-hide"));
      const b=document.createElement("button");b.className="sm ux-more";b.textContent=`Show ${items.length-n} more ${label}`;
      b.onclick=()=>{items.forEach(el=>el.classList.remove("ux-hide"));c.dataset.uxOpen="1";b.remove();};
      c.appendChild(b);
    }
  }
}
/* Financials: brands with no figure at all sit in one folded group instead of 20 rows of dashes */
function foldDashRows(root){
  const t=q("tr[data-fb]",root)&&q("tr[data-fb]",root).closest("table");if(!t||t.dataset.uxDash)return;
  const empty=qa("tr[data-fb]",t).filter(r=>!/\d/.test(r.innerText));if(empty.length<4)return;t.dataset.uxDash="1";
  empty.forEach(r=>r.classList.add("ux-hide"));
  const tr=document.createElement("tr");tr.innerHTML=`<td colspan="20" class="small muted" style="text-align:center;cursor:pointer">${empty.length} private brands without a figure on file yet · <b>show them</b></td>`;
  tr.onclick=()=>{empty.forEach(r=>r.classList.remove("ux-hide"));tr.remove();};empty[0].before(tr);
}

/* ---------- 2. collapsing repeated feed rows ---------- */
function groupRepeats(root){
  /* the same brand + kind + title repeated in one list (a watch page that "changed" six times today) shows once,
     newest first, with a ×n badge; clicking the badge unfolds the rest */
  for(const list of new Set(qa(".fr:not([data-ux-g])",root).map(e=>e.parentElement))){
    const seen=new Map();
    for(const r of qa(":scope > .fr",list)){
      r.dataset.uxG="1";
      const b=q(".b",r),k=q(".k",r);const title=b&&b.parentElement.lastElementChild?b.parentElement.lastElementChild.textContent.trim().slice(0,90):"";
      const key=(b?b.textContent:"")+"|"+(k?k.textContent:"")+"|"+title;
      const first=seen.get(key);
      if(first){first.n++;r.classList.add("ux-hide");first.rows.push(r);let c=q(".ux-count",first.el);if(!c){c=document.createElement("span");c.className="ux-count";(q(".k",first.el)||q(".b",first.el)).after(c);c.onclick=e=>{e.stopPropagation();first.rows.forEach(x=>x.classList.toggle("ux-hide"));c.classList.toggle("on");};}c.textContent="×"+first.n;c.title=`${first.n} identical changes in this list; the newest is shown. Click to see all.`;}
      else seen.set(key,{el:r,n:1,rows:[]});
    }
  }
}

/* ---------- 3. sortable tables ---------- */
const num=s=>{s=(s||"").trim();if(!s||s==="–"||s==="-")return null;const m=s.replace(/,/g,"").match(/-?\d+(\.\d+)?/);if(!m)return null;let v=parseFloat(m[0]);if(/\bcr\b/i.test(s))v*=1e7;else if(/\bL\b/.test(s))v*=1e5;else if(/[\d.]B\b/.test(s))v*=1e9;else if(/[\d.]M\b/.test(s))v*=1e6;return v;};
document.addEventListener("click",e=>{
  const th=e.target.closest("#view table th");if(!th||e.target.closest("a,button,input,select"))return;
  const table=th.closest("table");if(table.classList.contains("heat")||table.dataset.nosort)return;
  const hdr=th.parentElement,idx=[...hdr.children].indexOf(th);
  const rows=qa("tr",table).filter(r=>r!==hdr&&r.children.length>1&&!r.querySelector("td[colspan]")&&r.children[idx]);if(rows.length<2)return;
  const dir=th.classList.contains("ux-asc")?"desc":"asc";qa("th",hdr).forEach(h=>h.classList.remove("ux-asc","ux-desc"));th.classList.add("ux-"+dir);
  const key=r=>{const t=r.children[idx].innerText;const n=num(t);return n==null?{s:t.trim().toLowerCase()}:{n};};
  const sorted=rows.map(r=>({r,k:key(r)})).sort((a,b)=>{if("n"in a.k&&"n"in b.k)return a.k.n-b.k.n;if("n"in a.k)return 1;if("n"in b.k)return -1;return a.k.s<b.k.s?-1:a.k.s>b.k.s?1:0;}).map(x=>x.r);
  if(dir==="desc")sorted.reverse();
  const parent=rows[0].parentElement;sorted.forEach(r=>parent.appendChild(r));  // the header row stays first; sorted rows follow in order
});
function markSortable(root){qa("#view table:not(.heat):not([data-nosort]) th",root).forEach(th=>{if(th.parentElement.children.length>1&&th.textContent.trim())th.classList.add("ux-sort");});}

/* ---------- 4. collapsible cards ---------- */
const COLL="ux-collapsed-v1";const coll=()=>{try{return JSON.parse(localStorage.getItem(COLL)||"{}")}catch(e){return{}}};
const ckey=h=>base()+"|"+(h.firstChild&&h.firstChild.textContent||h.textContent).trim().slice(0,40);
function collapsibles(root){
  const saved=coll();
  for(const h of qa("#view .card > h2:first-child",root)){
    if(h.querySelector(".ux-cv"))continue;
    const cv=document.createElement("span");cv.className="ux-cv";cv.title="Collapse / expand (remembered)";cv.textContent="▾";h.appendChild(cv);
    const card=h.parentElement;if(saved[ckey(h)])card.classList.add("ux-collapsed");
    h.addEventListener("click",e=>{if(e.target.closest("a,button,input,select,label"))return;card.classList.toggle("ux-collapsed");const s=coll();if(card.classList.contains("ux-collapsed"))s[ckey(h)]=1;else delete s[ckey(h)];try{localStorage.setItem(COLL,JSON.stringify(s))}catch(x){}});
  }
}

/* ---------- 5. small affordances ---------- */
function backLink(){const r=base();if(r!=="brand"&&r!=="segment"&&r!=="financials"&&r!=="channels")return;const sub=q("#subtitle");if(!sub||sub.querySelector(".ux-back"))return;
  const to={brand:["#/brands","all brands"],segment:["#/segments","all segments"],financials:["#/financials","all companies"],channels:["#/channels","distribution matrix"]}[r];
  const p=(location.hash.replace(/^#\/?/,"").split("/")[1]||"");if(!p)return;if(sub.innerHTML.includes(to[0]))return;
  sub.insertAdjacentHTML("afterbegin",`<a class="ux-back" href="${to[0]}">← ${to[1]}</a> · `);}
const topBtn=document.createElement("button");topBtn.className="ux-top";topBtn.title="Back to top";topBtn.innerHTML="↑";topBtn.onclick=()=>window.scrollTo({top:0,behavior:"smooth"});document.body.appendChild(topBtn);
window.addEventListener("scroll",()=>topBtn.classList.toggle("on",window.scrollY>600),{passive:true});

/* ---------- 6. keyboard ---------- */
const GO={t:"#/today",f:"#/feed",n:"#/news",$:"#/financials",b:"#/brands",p:"#/pricing",m:"#/market",c:"#/channels",r:"#/voice",s:"#/strategy",w:"#/briefs"};
let gPending=0;
const HELP=`<div class="card ux-helpcard"><h2>Keyboard <span class="hint">press ? any time</span></h2><table data-nosort>
<tr><td><kbd>/</kbd> or <kbd>Ctrl K</kbd></td><td>search a brand or page, or ask the radar</td></tr>
<tr><td><kbd>g</kbd> then <kbd>t</kbd> <kbd>f</kbd> <kbd>n</kbd> <kbd>$</kbd> <kbd>b</kbd> <kbd>p</kbd> <kbd>m</kbd> <kbd>c</kbd> <kbd>r</kbd> <kbd>s</kbd> <kbd>w</kbd></td><td>go to Today, Feed, News, Financials, Brands, Pricing, Market, Channels, Reviews, Strategy, Briefs</td></tr>
<tr><td><kbd>Esc</kbd></td><td>close this or the search</td></tr>
<tr><td>click a column header</td><td>sort any table (click again to reverse)</td></tr>
<tr><td>click a card title</td><td>collapse it; the radar remembers per page</td></tr></table></div>`;
function help(){let o=q("#uxhelp");if(o){o.remove();return;}o=document.createElement("div");o.id="uxhelp";o.className="ov on";o.innerHTML=HELP;o.onclick=e=>{if(e.target===o)o.remove();};document.body.appendChild(o);}
document.addEventListener("keydown",e=>{
  const typing=/^(input|textarea|select)$/i.test((e.target||{}).tagName)||(e.target||{}).isContentEditable;
  if(e.key==="Escape"){const o=q("#uxhelp");if(o){o.remove();return;}}
  if(typing||e.ctrlKey||e.metaKey||e.altKey)return;
  if(e.key==="/"){e.preventDefault();const s=q("#searchbtn");if(s)s.click();return;}
  if(e.key==="?"){e.preventDefault();help();return;}
  if(gPending&&Date.now()-gPending<1500){const to=GO[e.key];if(to){e.preventDefault();location.hash=to;}gPending=0;return;}
  if(e.key==="g")gPending=Date.now();
});
const hb=q("#searchbtn");if(hb){const k=document.createElement("button");k.className="ghost sm ux-helpbtn";k.title="Keyboard shortcuts (?)";k.textContent="?";k.onclick=help;hb.after(k);}

/* ---------- 7. run after every render / partial reload ---------- */
let t=null,lastRoute=null;
function polish(){const v=q("#view");if(!v)return;const r=location.hash;if(r!==lastRoute){lastRoute=r;v.classList.remove("ux-in");void v.offsetWidth;v.classList.add("ux-in");}
  groupRepeats(v);fold(v);foldDashRows(v);markSortable(v);collapsibles(v);clampLong(v);backLink();}
new MutationObserver(ms=>{if(ms.every(m=>[...m.addedNodes].every(n=>n.nodeType!==1||n.classList.contains("ux-more")||n.classList.contains("ux-cv")||n.classList.contains("ux-count"))))return;clearTimeout(t);t=setTimeout(polish,80);}).observe(q("#view"),{childList:true,subtree:true});
polish();
})();
