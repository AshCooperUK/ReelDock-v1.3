import { NotificationMessage } from './notifications';
import {useEffect,useState} from 'react';
import {Heart} from 'lucide-react';
import {api} from './api';
import {Button} from './components/ui';
type State={id:string;favourite:boolean;csrf:string};
export default function VideoFavourite({id,compact=false,known,csrf='',title='',disabled=false,onError}:{id:string;compact?:boolean;known?:boolean|null;csrf?:string;title?:string;disabled?:boolean;onError?:(message:string)=>void}){
 const [value,setValue]=useState<State|null>(typeof known==='boolean'?{id,favourite:known,csrf}:null),[busy,setBusy]=useState(false),[error,setError]=useState('');
 useEffect(()=>{const c=new AbortController();setError('');if(known!==undefined)setValue(typeof known==='boolean'?{id,favourite:known,csrf}:null);else{setValue(null);api<State>('/api/favourites/video/'+encodeURIComponent(id),c.signal).then(r=>{if(!c.signal.aborted)setValue(r);}).catch(()=>{});}const changed=(e:Event)=>{const r=(e as CustomEvent<State>).detail;if(r?.id===id)setValue(old=>old?{...old,favourite:r.favourite}:old);};window.addEventListener('reeldock-video-favourite-changed',changed);return()=>{c.abort();window.removeEventListener('reeldock-video-favourite-changed',changed);};},[id,known,csrf]);
 async function toggle(){if(!value||busy||disabled||known===null)return;setBusy(true);setError('');onError?.('');try{const r=await api<State>('/api/favourites/video/'+encodeURIComponent(id),undefined,{favourite:!value.favourite},value.csrf);setValue(r);window.dispatchEvent(new CustomEvent('reeldock-video-favourite-changed',{detail:r}));}catch(e){const message=e instanceof Error?e.message:'Could not save favourite.';setError(message);onError?.(message);}finally{setBusy(false);}}
 const label=(value?.favourite?'Remove video from favourites':'Favourite video')+(title?' · '+title:'');
 return value||compact?<><Button variant="secondary" iconOnly={compact} aria-label={label} title={label} aria-pressed={value?.favourite||false} disabled={disabled||busy||!value||known===null} onClick={()=>void toggle()}><Heart size={18} fill={value?.favourite?'currentColor':'none'}/>{!compact&&'Favourite'}</Button>{error&&!onError&&<NotificationMessage kind="error" message={error}/>}</>:null;
}
