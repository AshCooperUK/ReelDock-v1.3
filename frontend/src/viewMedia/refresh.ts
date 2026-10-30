import type { MediaCard, MediaPage } from './types';

export function refreshDelay(page:MediaPage,source:string) {
  if(source==='all'&&page.observationSignature)return Math.max(2000,Math.min(30000,page.refreshAfterMs||10000));
  return page.providerChecksPending?Math.max(1000,page.refreshAfterMs||2000):page.scanning||page.loading||page.refreshing?2000:source==='discover'?60000:null;
}

/** Show arriving cards without advancing beyond a batch that is still loading. */
export function pendingPage(current:MediaPage,incoming:MediaPage):MediaPage {
  return {...incoming,items:mergeCards([...current.items,...incoming.items]),
    nextOffset:current.nextOffset,nextCursor:current.nextCursor,preparing:true};
}

/** Update the first page while keeping titles already loaded by scrolling. */
export function refreshedPage(current:MediaPage|null,incoming:MediaPage):MediaPage {
  if(incoming.observationSignature)return refreshedObservations(current,incoming);
  if(incoming.searchSnapshot){
    if(current?.searchSnapshot!==incoming.searchSnapshot)return incoming;
    // A search frame stays in place while background checks finish. Only
    // status metadata changes; cards and pagination remain the user's frame.
    const fields=['providerChecksPending','providerChecksComplete','searchUpdatesAvailable','refreshAfterMs','warnings','freshness','error'] as const;
    if(fields.every(field=>JSON.stringify(current[field])===JSON.stringify(incoming[field])))return current;
    return {...current,...Object.fromEntries(fields.map(field=>[field,incoming[field]]))};
  }
  if(!current||current.source!==incoming.source||current.media!==incoming.media||current.collection!==incoming.collection||
     current.nextOffset==null||incoming.nextOffset==null||current.nextOffset<=incoming.nextOffset)return incoming;
  const ids=new Set(incoming.items.map(item=>item.id));
  return {...incoming,items:[...incoming.items,...current.items.filter(item=>!ids.has(item.id))],
    nextOffset:current.nextOffset,nextCursor:current.nextCursor};
}

/** Reconcile the bounded saved window without losing loaded cards or skipping
 * an observation inserted between earlier pages. Account changes drop all tails. */
export function refreshedObservations(current:MediaPage|null,incoming:MediaPage):MediaPage {
  if(!current||current.observationSignature!==incoming.observationSignature||!incoming.observationIds)return incoming;
  const rows=new Map(mergeCards([...incoming.items,...current.items]).map(item=>[cardKey(item),item]));
  const items=incoming.observationIds.flatMap(key=>rows.has(key)?[rows.get(key)!]:[]);
  const missing=incoming.observationIds.findIndex(key=>!rows.has(key));
  return {...incoming,items,nextOffset:missing<0?null:missing,completeSet:missing<0};
}
function cardKey(item:MediaCard){const external=item.action?.type==='discover'?item.action.item.externalId:'';return item.kind==='movie'&&/^\d+$/.test(external||'')?'movie:'+String(Number(external)):item.id;}

/** Cards identify movies by numeric IMDb identity, including older padded caches. */
export function mergeCards(items:MediaCard[]):MediaCard[] {
  const result=new Map<string,MediaCard>();
  for(const item of items){
    const external=item.action?.type==='discover'?item.action.item.externalId:'';
    const key=item.kind==='movie'&&/^\d+$/.test(external||'')?'movie:'+String(Number(external)):item.id;
    const old=result.get(key);
    result.set(key,old?{...item,...old}:item);
  }
  return Array.from(result.values());
}
