import { NotificationMessage } from '../notifications';
import {useEffect,useRef,useState} from 'react';
import {Bookmark,BookmarkCheck} from 'lucide-react';
import {api} from '../api';
import {acquisitionItem} from '../discoverPayload';
import type {DiscoverItem} from '../Discover';
import {Button} from '../components/ui';
import {watchlistHint} from './tileIdentity';

type State={key:string;watchlisted:boolean;csrf:string};
export default function WatchlistButton({item,compact=false,disabled=false}:{item:DiscoverItem;compact?:boolean;disabled?:boolean}){
 const payload=JSON.stringify({...acquisitionItem(item),imdbId:item.imdbId}),[value,setValue]=useState<State|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[revision,setRevision]=useState(0);
 const current=useRef<State|null>(null),sending=useRef(false),generation=useRef(0);
 useEffect(()=>{const c=new AbortController();const version=++generation.current;current.current=null;setValue(null);setBusy(false);sending.current=false;setError('');
  const {id,kind,title,externalId,imdbId,videoId}=JSON.parse(payload);
  api<State>('/api/watchlist/item?'+new URLSearchParams({item:JSON.stringify({id,kind,title,externalId,imdbId,videoId})}),c.signal).then(r=>{if(!c.signal.aborted){current.current=r;setValue(r);}}).catch(e=>{if(!c.signal.aborted)setError(e instanceof Error?e.message:'Could not load Watchlist.');});
  const changed=(event:Event)=>{const r=(event as CustomEvent<State>).detail;if(generation.current===version&&r?.key===current.current?.key){current.current={...current.current!,watchlisted:r.watchlisted};setValue(current.current);}};
  const policies=['reeldock-movie-options-changed','reeldock-show-options-changed','reeldock-channel-changed'],refresh=()=>setRevision(v=>v+1);
  policies.forEach(name=>window.addEventListener(name,refresh));
  window.addEventListener('reeldock-watchlist-changed',changed);return()=>{c.abort();generation.current++;window.removeEventListener('reeldock-watchlist-changed',changed);policies.forEach(name=>window.removeEventListener(name,refresh));};
 },[payload,revision]);
 async function toggle(){const state=current.current;if(!state||sending.current||disabled)return;const version=generation.current;sending.current=true;setBusy(true);setError('');
  try{const r=await api<State>('/api/watchlist/item',undefined,{item:JSON.parse(payload),watchlisted:!state.watchlisted},state.csrf);window.dispatchEvent(new CustomEvent('reeldock-watchlist-changed',{detail:r}));if(generation.current===version){current.current=r;setValue(r);}}
  catch(e){if(generation.current===version)setError(e instanceof Error?e.message:'Could not update Watchlist.');}
  finally{if(generation.current===version){sending.current=false;setBusy(false);}}
 }
 const selected=!!value?.watchlisted,label=(selected?'Remove from Watchlist':'Add to Watchlist')+' · '+item.title,Icon=selected?BookmarkCheck:Bookmark;
 return <><Button variant="secondary" iconOnly={compact} aria-label={label} title={watchlistHint(item,selected)} aria-pressed={selected} disabled={disabled||busy||!value} onClick={()=>void toggle()}><Icon size={18}/>{!compact&&(selected?'On Watchlist':'Add to Watchlist')}</Button>{error&&<NotificationMessage kind="error" message={error} actions={<> {!value&&<Button variant="secondary" onClick={()=>setRevision(v=>v+1)}>Try again</Button>} </>}/>}</>;
}
