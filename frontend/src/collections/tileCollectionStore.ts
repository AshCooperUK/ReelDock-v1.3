import {useSyncExternalStore} from 'react';
import {api} from '../api';
import type {CollectionData} from './collectionFavourites';
import type {CollectionItem} from './CollectionShelf';
type Rows=CollectionData&{episodes?:CollectionItem[];csrf?:string};
type Snapshot={watchlist:Rows|null;favourites:Rows|null;error:string};
const empty:Snapshot={watchlist:null,favourites:null,error:''};
let state=empty,request:AbortController|null=null,timer:ReturnType<typeof setInterval>|undefined;
const listeners=new Set<()=>void>();
const events=['reeldock-watchlist-changed','reeldock-movie-options-changed','reeldock-show-options-changed','reeldock-channel-changed','reeldock-channel-removed','reeldock-video-favourite-changed','reeldock-episode-favourite-changed'];
function publish(next:Snapshot){state=next;listeners.forEach(fn=>fn());}
export async function refreshTileCollections(){
 request?.abort();const c=new AbortController();request=c;
 try{const [watchlist,favourites]=await Promise.all([api<Rows>('/api/watchlist',c.signal),api<Rows>('/api/favourites',c.signal)]);if(!c.signal.aborted)publish({watchlist,favourites,error:''});}
 catch(e){if(!c.signal.aborted)publish({...state,error:e instanceof Error?e.message:'Could not load collection controls.'});}
}
const refresh=()=>{void refreshTileCollections();};
function subscribe(fn:()=>void){
 listeners.add(fn);
 if(listeners.size===1){events.forEach(name=>window.addEventListener(name,refresh));window.addEventListener('focus',refresh);timer=setInterval(()=>{if(!document.hidden)refresh();},30000);refresh();}
 return()=>{listeners.delete(fn);if(!listeners.size){request?.abort();events.forEach(name=>window.removeEventListener(name,refresh));window.removeEventListener('focus',refresh);clearInterval(timer);state=empty;}};
}
export function useTileCollections(){return useSyncExternalStore(subscribe,()=>state,()=>empty);}
