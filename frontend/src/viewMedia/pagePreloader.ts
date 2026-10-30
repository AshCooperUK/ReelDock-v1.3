import type { MediaPage } from './types';

function pause(signal:AbortSignal):Promise<void> {
  return new Promise((resolve,reject)=>{
    const abort=()=>{clearTimeout(timer);reject(new DOMException('Aborted','AbortError'));};
    const timer=setTimeout(()=>{signal.removeEventListener('abort',abort);resolve();},2000);
    if(signal.aborted)abort();else signal.addEventListener('abort',abort,{once:true});
  });
}

/** One unconsumed batch; browsing gestures still control when cards are appended. */
export function createPagePreloader(read:(url:string,signal:AbortSignal)=>Promise<MediaPage>,wait=pause) {
  type Entry={url:string;controller:AbortController;promise:Promise<MediaPage>;partial?:MediaPage;listeners:Set<(page:MediaPage)=>void>;finished:number};
  let entry:Entry|undefined;
  function clear(){entry?.controller.abort();entry=undefined;}
  function load(url:string,signal:AbortSignal,onPartial?:(page:MediaPage)=>void):Promise<MediaPage> {
    if(entry&&(entry.url!==url||entry.controller.signal.aborted||(entry.finished&&Date.now()-entry.finished>300000)))clear();
    if(!entry){
      const controller=new AbortController(),abort=()=>controller.abort();
      if(signal.aborted)controller.abort();else signal.addEventListener('abort',abort,{once:true});
      const next:Entry={url,controller,promise:undefined!,listeners:new Set(),finished:0};
      entry=next;
      next.promise=(async()=>{
        try{
          let result=await read(url,controller.signal);
          for(let attempt=0;(result.loading||result.preparing)&&attempt<60;attempt++){
            if(controller.signal.aborted)throw new DOMException('Aborted','AbortError');
            if(result.items.length){next.partial=result;for(const listener of next.listeners)listener(result);}
            await wait(controller.signal);result=await read(url,controller.signal);
          }
          if(controller.signal.aborted)throw new DOMException('Aborted','AbortError');
          if(result.loading||result.preparing)throw new Error('This source is still refreshing. Try again shortly.');
          next.finished=Date.now();return result;
        }catch(error){if(entry===next)entry=undefined;throw error;}
        finally{signal.removeEventListener('abort',abort);}
      })();
    }
    const current=entry;
    if(onPartial){current.listeners.add(onPartial);if(current.partial)onPartial(current.partial);}
    return current.promise.finally(()=>{if(onPartial)current.listeners.delete(onPartial);});
  }
  return {load,clear};
}
