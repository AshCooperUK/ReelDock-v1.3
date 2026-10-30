import { useSyncExternalStore } from 'react';
import { api } from './api';
import { onVisible, pageHidden } from './visibility';
export type DownloadItem={id:string;title:string;automatic?:number|boolean;created?:number;updated?:number;channel_name?:string;channel?:string;state?:string;phase?:string;progress?:number;message?:string;avatar?:string;banner?:string;description?:string;video?:{thumbnail?:string}};
export type ActivityPreview={title:string;channel?:string;overview?:string;image?:string;fallbackImage?:string};
export type ActivityTask={id:string;title:string;state:string;message:string;progress?:number|null;updated?:number;preview?:ActivityPreview;waiting?:number;engine?:string;engine_id?:string};
export type ActivityAlert={id:string;title:string;message:string;retry?:{id:string};channel?:{id:string;name:string;avatar?:string};subscription?:string};
export type ActivityData={tasks?:ActivityTask[];recent?:ActivityTask[];alerts?:ActivityAlert[];download:DownloadItem|null;queued:number;syncing:boolean;sync_message:string;m3u:null|{name:string;message:string};cookie_warning:null|{id:string;message:string}};
type Snapshot={items:DownloadItem[];activity:ActivityData|null;error:string;loading:boolean};
let snapshot:Snapshot={items:[],activity:null,error:'',loading:true};
const listeners=new Set<()=>void>();
let controller:AbortController|undefined,timer:ReturnType<typeof setTimeout>|undefined,running=false,again=false,stopVisible:(()=>void)|undefined;
export function downloadLabel(item:DownloadItem){
 if(item.state==='cancelling')return 'Cancelling';
 return ({encoding:'Encoding with FFmpeg',merging:'Merging audio and video',sponsors:'Removing sponsor sections',metadata:'Saving metadata',preparing:'Preparing',processing:'Processing'} as Record<string,string>)[item.phase||'']||'Downloading';
}
export async function refreshDownloads(){
 if(!controller)return;
 if(running){again=true;return;}
 clearTimeout(timer);running=true;const current=controller;
 try{
  const [downloads,activity]=await Promise.all([api<{items:DownloadItem[]}>('/api/downloads',current.signal),api<ActivityData>('/api/activity',current.signal)]);
  if(current.signal.aborted)return;
  // Both views use this exact list snapshot, including completion and cancellation.
  const download=downloads.items.find(item=>['downloading','cancelling'].includes(item.state||''))||null;
  snapshot={items:downloads.items,activity:{...activity,download,queued:downloads.items.filter(item=>item.state==='queued').length},error:'',loading:false};
 }catch(e){if(!current.signal.aborted)snapshot={...snapshot,error:e instanceof Error?e.message:'Downloads unavailable',loading:false};}
 finally{
  if(!current.signal.aborted){running=false;listeners.forEach(listener=>listener());if(again){again=false;void refreshDownloads();}else if(!pageHidden())timer=setTimeout(refreshDownloads,5000);}
 }
}
function subscribe(listener:()=>void){
 listeners.add(listener);
 if(listeners.size===1){controller=new AbortController();running=false;again=false;void refreshDownloads();window.addEventListener('reeldock-library-changed',refreshDownloads);stopVisible=onVisible(()=>{clearTimeout(timer);void refreshDownloads();});}
 return()=>{listeners.delete(listener);if(!listeners.size){controller?.abort();controller=undefined;clearTimeout(timer);window.removeEventListener('reeldock-library-changed',refreshDownloads);stopVisible?.();stopVisible=undefined;}};
}
export function useDownloadFeed(){return useSyncExternalStore(subscribe,()=>snapshot);}
