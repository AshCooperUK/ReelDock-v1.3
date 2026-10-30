/** Display snapshots only: callers still refresh on every mount. No transport,
 * storage, background work or account state is retained outside this memory. */
export function createWarmSnapshots<T>({limit=6,ttl=60000,now=Date.now}:{limit?:number;ttl?:number;now?:()=>number}={}){
 let owner:string|undefined,epoch=0,accountEpoch=0;
 const entries=new Map<string,{at:number;value:T}>();
 const copy=(value:T):T=>typeof structuredClone==='function'?structuredClone(value):JSON.parse(JSON.stringify(value)) as T;
 const invalidate=()=>{entries.clear();epoch++;};
 return {invalidate,watch(target:Pick<EventTarget,'addEventListener'|'removeEventListener'>,events:readonly string[]){
  events.forEach(event=>target.addEventListener(event,invalidate));
  return()=>events.forEach(event=>target.removeEventListener(event,invalidate));
 },open(account:string,key:string){
  if(owner!==account){owner=account;epoch++;accountEpoch++;entries.clear();}
  const version=epoch,accountVersion=accountEpoch;
  const currentAccount=()=>owner===account&&accountEpoch===accountVersion;
  const valid=()=>currentAccount()&&epoch===version;
  return {
   valid,currentAccount,
   read():T|undefined{
    if(!valid())return;
    const saved=entries.get(key);
    if(!saved)return;
    if(now()-saved.at>=ttl){entries.delete(key);return;}
    entries.delete(key);entries.set(key,saved);
    return copy(saved.value);
   },
   write(value:T){
    if(!valid())return;
    entries.delete(key);entries.set(key,{at:now(),value:copy(value)});
    while(entries.size>limit)entries.delete(entries.keys().next().value!);
   },
  };
 }};
}
