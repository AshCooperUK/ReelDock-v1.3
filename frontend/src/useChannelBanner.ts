import {useEffect,useState} from 'react';
import {api} from './api';

// Added channels resolve entirely from saved information. Public failures leave
// the caller's existing video artwork intact; this never requests an import.
export function useChannelBanner(id?:string,provided?:string,enabled=true){
 const [saved,setSaved]=useState<{id:string;banner:string}|null>(null);
 useEffect(()=>{
  if(!enabled||provided||!id||!/^UC[A-Za-z0-9_-]{22}$/.test(id))return;
  const controller=new AbortController();
  api<{channel:{banner?:string}}>('/api/guide/channel/'+encodeURIComponent(id)+'/info',controller.signal,undefined,undefined,{feedback:'inline'}).then(value=>{
   if(!controller.signal.aborted)setSaved({id,banner:typeof value.channel.banner==='string'?value.channel.banner:''});
  }).catch(()=>{});
  return()=>controller.abort();
 },[id,provided,enabled]);
 return provided||(id&&saved?.id===id?saved.banner:undefined);
}
