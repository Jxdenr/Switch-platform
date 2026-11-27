/* Help panels remember open/closed, tooltips for .tip / [data-tip], and the dashboard setup card's Hide. */
(function(){
  const get=k=>{try{return localStorage.getItem(k)}catch(e){return null}};
  const set=(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}};
  document.querySelectorAll('details.pagehelp').forEach(d=>{
    const k='help:'+d.dataset.help;
    if(get(k)==='open')d.open=true;
    d.addEventListener('toggle',()=>set(k,d.open?'open':'closed'));
  });
  let box=null;
  function show(el){
    const t=el.getAttribute('data-tip');if(!t)return;
    if(!box){box=document.createElement('div');box.className='tipbox';box.setAttribute('role','tooltip');document.body.appendChild(box)}
    box.textContent=t;box.hidden=false;
    const r=el.getBoundingClientRect(),b=box.getBoundingClientRect();
    let x=Math.min(Math.max(8,r.left+r.width/2-b.width/2),innerWidth-b.width-8);
    let y=r.bottom+8;if(y+b.height>innerHeight-8)y=r.top-b.height-8;
    box.style.left=x+'px';box.style.top=y+'px';
  }
  function hide(){if(box)box.hidden=true}
  document.addEventListener('mouseover',e=>{const el=e.target.closest('[data-tip]');if(el)show(el)});
  document.addEventListener('mouseout',e=>{if(e.target.closest('[data-tip]'))hide()});
  document.addEventListener('focusin',e=>{const el=e.target.closest('[data-tip]');if(el)show(el)});
  document.addEventListener('focusout',hide);
  document.addEventListener('click',e=>{const el=e.target.closest('.tip');if(el){e.preventDefault();e.stopPropagation();show(el)}},true);
  addEventListener('scroll',hide,true);
  const s=document.querySelector('[data-setup]');
  if(s){
    const show_=document.querySelector('[data-setup-show]');
    const apply=()=>{const h=get('setup:hidden')==='1';s.hidden=h;if(show_)show_.hidden=!h};
    s.querySelector('[data-setup-hide]').addEventListener('click',()=>{set('setup:hidden','1');apply()});
    if(show_)show_.addEventListener('click',()=>{set('setup:hidden','0');apply()});
    apply();
  }
})();
