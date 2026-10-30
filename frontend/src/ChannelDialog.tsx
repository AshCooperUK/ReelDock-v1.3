import { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { Text } from '@mantine/core';
import { channelEvent, type ChannelTarget } from './channelNavigation';
import { channelParam, withChannel } from './appNavigation';
import { DetailSheet } from './components/DetailSheet';

// The listener stays ready on every page; channel data and download panels
// enter the bundle only when a channel is opened.
const ChannelDetail=lazy(()=>import('./ChannelDetail'));

export default function ChannelDialog({readOnly=false}:{readOnly?:boolean}={}){
 const sequence=useRef(0);
 const [target,setTarget]=useState<(ChannelTarget&{revision:number})|null>(null);
 useEffect(()=>{const close=()=>setTarget(null);window.addEventListener('reeldock-notification-settings',close);return()=>window.removeEventListener('reeldock-notification-settings',close);},[]);
 useEffect(()=>{const open=(event:Event)=>{const detail=(event as CustomEvent<ChannelTarget>).detail;if(detail?.id)setTarget({...detail,mode:detail.mode||'overview',revision:++sequence.current});};window.addEventListener(channelEvent,open);return()=>window.removeEventListener(channelEvent,open);},[]);
 // The open channel is kept in the address bar (?channel=UC…) so the link can be shared or bookmarked.
 const shownId=useRef<string|null>(null);
 useEffect(()=>{const id=target?.id||null;if(id===shownId.current)return;const wasOpen=shownId.current!==null;shownId.current=id;if(id||wasOpen){const url=withChannel(location.href,id);if(url!==location.pathname+location.search+location.hash)history.replaceState(history.state,'',url);}},[target?.id]);
 useEffect(()=>{const back=()=>{if(!channelParam(location.search))setTarget(null);};window.addEventListener('popstate',back);return()=>window.removeEventListener('popstate',back);},[]);
 // One channel sheet; "options" mode is the in-sheet tab, not a second dialog.
 return <>{target&&<Suspense fallback={<DetailSheet open closeOnChannelNavigation={false} title={target.name||'YouTube channel'} onOpenChange={open=>{if(!open)setTarget(null);}}><Text role="status">Opening channel…</Text></DetailSheet>}><ChannelDetail key={target.id+target.revision} target={target} readOnly={readOnly} mode={m=>setTarget(t=>t?{...t,mode:m as ChannelTarget['mode']}:t)} close={()=>setTarget(null)}/></Suspense>}</>;
}
