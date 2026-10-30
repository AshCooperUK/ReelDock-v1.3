/** No intersection-triggered chaining: each batch needs a new downward scroll. */
type ScrollTarget=Window|HTMLElement;
const wheelTimes=new WeakMap<ScrollTarget,number>();
export function trackPagingMomentum(target: ScrollTarget, now:()=>number=Date.now) {
  const wheel=()=>wheelTimes.set(target,now());
  target.addEventListener('wheel',wheel as EventListener,{passive:true});
  return()=>target.removeEventListener('wheel',wheel as EventListener);
}
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
