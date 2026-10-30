import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';
import { onVisible, pageHidden } from './visibility';
import type { DownloadItem } from './downloadFeed';
import type { Job } from './M3uDownloads';
import type { ActivityVisibility } from './activityClear';
import type { SourceChoice } from './SourceAlternatives';
import type { DiscoverItem } from './Discover';
export type SyncItem={id:number;state:string;title:string;subscription:string;message:string;finished:number;stats:{changes?:Record<string,{added:number;updated:number;removed:number}>;added?:number;updated?:number;removed?:number}};
export type DiscoverHistory={id:string;title:string;state:string;message:string;created?:number;updated?:number;destination?:string;artwork?:string;subscription:string;mediaKind?:'movie'|'show';year?:string;genre?:string;rating?:string;description?:string;progress?:number;engine?:string;engine_id?:string;sourceChoice?:SourceChoice;mediaItem?:DiscoverItem};
export type HistoryRow={kind:'youtube';item:DownloadItem&{destination?:string;banner?:string}}|{kind:'m3u';item:Job}|{kind:'m3u_sync';item:SyncItem}|{kind:'event';item:SyncItem}|{kind:'discover';item:DiscoverHistory};
type ArchivedEngineItem={id:string;state:string;archived:true;originalId?:string;progress?:number;added?:number;updated?:number;[key:string]:unknown};
export type ArchivedEngineHistory={kind:'torrent';item:ArchivedEngineItem}|{kind:'nzb';item:ArchivedEngineItem};
export type ActivityHistoryRow=HistoryRow|ArchivedEngineHistory;
type Page=ActivityVisibility&{items:HistoryRow[];more:boolean;total:number;acquisitionHold?:boolean;active?:HistoryRow[]};
export const MAX_ACTIVITY_HISTORY=5_000;
function aborted(){const problem=new Error('Activity loading cancelled.');problem.name='AbortError';return problem;}
/** Commit one complete frame, never a scroll-dependent subset of the groups. */
export async function loadActivitySnapshot(readSnapshot:(signal:AbortSignal)=>Promise<Page>,signal:AbortSignal):Promise<Page>{
 if(signal.aborted)throw aborted();
 const batch=await readSnapshot(signal);if(signal.aborted)throw aborted();
 if(!batch||!Array.isArray(batch.items)||typeof batch.more!=='boolean'||!Number.isSafeInteger(batch.total)||batch.total<0)throw new Error('Activity returned an incomplete list. No partial activity was shown.');
 if(batch.total>MAX_ACTIVITY_HISTORY)throw new Error('Activity has more than 5,000 entries. No partial list was shown. Export the log to view the full history.');
 const revision=batch.activityRevision??0;
 if(!Number.isSafeInteger(revision)||revision<0)throw new Error('Activity returned an invalid revision. No partial activity was shown.');
 if(batch.more||batch.items.length!==batch.total)throw new Error('Activity returned an incomplete list. No partial activity was shown.');
 const unique=new Set<string>();
 for(const row of batch.items){
  if(!row?.item||row.item.id==null||!['youtube','m3u','m3u_sync','event','discover'].includes(row.kind))throw new Error('Activity returned an incomplete list. No partial activity was shown.');
  const key=row.kind+':'+String(row.item.id);if(unique.has(key))throw new Error('Activity returned an inconsistent list. No partial activity was shown.');unique.add(key);
 }
 return batch;
}
export function useDownloadHistory(){
 const[data,setData]=useState<Page>({items:[],more:false,total:0}),[error,setError]=useState(''),[busy,setBusy]=useState(true),[revision,setRevision]=useState(0);
 const end=useRef<HTMLDivElement>(null),request=useRef<AbortController|null>(null),generation=useRef(0);
 const refresh=useCallback(()=>{generation.current++;request.current?.abort();setBusy(true);setRevision(value=>value+1);},[]);
 useEffect(()=>{
  const controller=new AbortController(),owner=generation.current;request.current=controller;let timer:ReturnType<typeof setTimeout>,loading=false;
  async function load(){
   if(loading||controller.signal.aborted||owner!==generation.current)return;loading=true;clearTimeout(timer);
   try{
    const snapshot=await loadActivitySnapshot(signal=>api<Page>('/api/download-history/snapshot',signal,undefined,undefined,{feedback:'inline'}),controller.signal);
    if(!controller.signal.aborted&&owner===generation.current){setData({...snapshot,acquisitionHold:Boolean(snapshot.acquisitionHold),active:snapshot.active||[],activityRevision:snapshot.activityRevision||0});setError('');}
   }catch(problem){if(!controller.signal.aborted&&owner===generation.current)setError(problem instanceof Error?problem.message:'Could not load activity.');}
   finally{loading=false;if(!controller.signal.aborted&&owner===generation.current){setBusy(false);if(!pageHidden())timer=setTimeout(load,5000);}}
  }
  void load();const stopVisible=onVisible(()=>void load());return()=>{controller.abort();clearTimeout(timer);stopVisible();};
 },[revision]);
 useEffect(()=>{window.addEventListener('reeldock-library-changed',refresh);return()=>window.removeEventListener('reeldock-library-changed',refresh);},[refresh]);
 return {...data,error,busy,end,refresh,reset:refresh};
}
