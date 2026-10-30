type ScrollTarget=Window|HTMLElement;

/** Observe cached result batches against their actual scrolling ancestor. */
export function autoPagingRoot(element:HTMLElement):HTMLElement|null {
  const document=element.ownerDocument;
  for(let parent=element.parentElement;parent&&parent!==document.body&&parent!==document.documentElement;parent=parent.parentElement){
    if(parent.matches('[data-ui-dialog-scroll]')||/auto|scroll|overlay/.test(document.defaultView?.getComputedStyle(parent).overflowY||''))return parent;
  }
  return null;
}

export function observeAutoPaging(element:HTMLElement,load:()=>void) {
  const root=autoPagingRoot(element),view=element.ownerDocument.defaultView;
  let active=true,consumed=false;
  const consume=()=>{if(active&&!consumed){consumed=true;load();}};
  if(typeof IntersectionObserver==='undefined'){
    const target=root||view;
    if(!target)return()=>{active=false;};
    const check=()=>{const bounds=element.getBoundingClientRect(),top=root?.getBoundingClientRect().top||0,bottom=root?.getBoundingClientRect().bottom??view?.innerHeight??0;if(bounds.top<=bottom+180&&bounds.bottom>=top-180)consume();};
    target.addEventListener('scroll',check,{passive:true});view?.addEventListener('resize',check);check();
    return()=>{active=false;target.removeEventListener('scroll',check);view?.removeEventListener('resize',check);};
  }
  const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting))consume();},{root,rootMargin:'180px 0px'});
  observer.observe(element);
  return()=>{active=false;observer.disconnect();};
}

const wheelTimes=new WeakMap<ScrollTarget,number>();
export function trackPagingMomentum(target: ScrollTarget, now:()=>number=Date.now) {
  const wheel=()=>wheelTimes.set(target,now());
  target.addEventListener('wheel',wheel as EventListener,{passive:true});
  return()=>target.removeEventListener('wheel',wheel as EventListener);
}
/** Network browse batches still require a fresh downward gesture. */
export function attachScrollPaging(target: ScrollTarget, edge: () => number, load: () => void, now:()=>number=Date.now) {
  const position=()=> 'scrollY' in target?target.scrollY:target.scrollTop;
  const height=()=> 'innerHeight' in target?target.innerHeight:target.clientHeight;
  let consumed=false,armed=false,previous=position(),touchY:number|null=null;
  const advance=()=>{if(armed&&!consumed&&edge()<=height()+180){consumed=true;load();}};
  const scroll=()=>{const down=position()>previous;previous=position();if(down)advance();};
  // A wheel at the bottom also works when a short page cannot scroll further.
  const wheel=(event:WheelEvent)=>{const time=now(),previousWheel=wheelTimes.get(target)??-Infinity;wheelTimes.set(target,time);if(event.deltaY>0){if(time-previousWheel>=180)armed=true;advance();}};
  const start=(e:TouchEvent)=>{touchY=e.touches[0]?.clientY??null;};
  const end=(e:TouchEvent)=>{if(touchY!==null&&touchY-(e.changedTouches[0]?.clientY??touchY)>10){armed=true;advance();}touchY=null;};
  const key=(e:KeyboardEvent)=>{if(!e.repeat&&['PageDown','End','ArrowDown',' '].includes(e.key)&&!((e.target as HTMLElement)?.closest?.('input,select,textarea,button'))){armed=true;advance();}};
  const pointer=(e:PointerEvent)=>{const right='document' in target?target.document.documentElement.clientWidth:target.getBoundingClientRect().right;if(e.clientX>=right-16)armed=true;};
  target.addEventListener('pointerdown',pointer as EventListener,{passive:true});
  target.addEventListener('touchstart',start as EventListener,{passive:true});target.addEventListener('touchend',end as EventListener,{passive:true});target.addEventListener('keydown',key as EventListener);
  target.addEventListener('scroll',scroll,{passive:true});
  target.addEventListener('wheel',wheel as EventListener,{passive:true});
  return()=>{target.removeEventListener('scroll',scroll);target.removeEventListener('wheel',wheel as EventListener);target.removeEventListener('touchstart',start as EventListener);target.removeEventListener('touchend',end as EventListener);target.removeEventListener('keydown',key as EventListener);target.removeEventListener('pointerdown',pointer as EventListener);};
}
