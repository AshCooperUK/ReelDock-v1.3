import { Badge, Box, Fieldset, Group as MantineGroup, Image, Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { Button as ReelDockButton } from './components/ui';
import { NotificationMessage } from './notifications';
import {useState} from 'react';
import {Star,Settings2,X} from 'lucide-react';
import {Button,Dialog,DialogContent,DialogTitle,DialogDescription} from './components/ui';
import {ChannelSettingsFields,type Subscription,type DownloadRules} from './ChannelOptions';
import {api} from './api';
export function mixedChannelFields(items:Subscription[]){
 const mixed=new Set<string>();if(!items.length)return mixed;
 const values=(item:Subscription):Record<string,unknown>=>({...item.downloads,subscription_paused:!!item.downloads.subscription_paused,minimum_video_minutes:item.downloads.minimum_override?item.downloads.minimum_video_minutes:'default',videos:!!item.downloads.videos,shorts:!!item.downloads.shorts,live:!!item.downloads.live,keep_files:!!item.downloads.keep_files,range_days:item.downloads.range_override===0?'default':item.downloads.range_days,quality:item.downloads.quality_override?item.downloads.quality:'default',shown:item.shown,forecasts:item.forecasts,favourite:item.favourite});
 const first=values(items[0]);for(const item of items.slice(1)){const next=values(item);for(const key of Object.keys(first))if(first[key]!==next[key])mixed.add(key);}return mixed;
}
export function selectedFavouriteState(items:Pick<Subscription,'favourite'>[]){
 const count=items.filter(item=>item.favourite).length;
 const all=items.length>0&&count===items.length;
 return {pressed:count===0?false:all?true:'mixed' as const,next:!all};
}
export default function BulkChannelOptions({items,csrf,onChange,onClear}:{items:Subscription[];csrf:string;onChange:(items:Subscription[])=>void;onClear:()=>void}){
 const[open,setOpen]=useState(false),[busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[error,setError]=useState('');
 const favourite=selectedFavouriteState(items),favouriteLabel=favourite.next?'Mark selected as favourites':'Remove selected from favourites';
 async function apply(key:string,value:boolean|string|number,rule=false){
  setBusy(true);setNotice('');setError('');const updated:Subscription[]=[],failed:string[]=[];
  for(const item of items){try{
   if(rule){const result=await api<{downloads:DownloadRules}>('/api/channel-downloads/'+encodeURIComponent(item.id),undefined,{settings:{[key]:value}},csrf);updated.push({...item,downloads:result.downloads});}
   else{await api(key==='favourite'?'/api/channel-favourite/'+encodeURIComponent(item.id):'/api/subscriptions',undefined,key==='favourite'?{favourite:value}:{ids:[item.id],[key]:value},csrf);updated.push({...item,[key]:value});}
  }catch{failed.push(item.name);}}
  onChange(updated);setBusy(false);setNotice('Updated '+updated.length+' of '+items.length+' channels.');if(failed.length){setError('Could not update: '+failed.join(', ')+'. Please try again.');setOpen(true);}
 }
 return <><Paper withBorder p="sm" radius="md" role="toolbar" aria-label="Selected channel actions"><MantineGroup gap="sm"><Text size="sm" aria-live="polite">{items.length} selected</Text><ReelDockButton variant="secondary" iconOnly title={favouriteLabel} aria-label={favouriteLabel} aria-pressed={favourite.pressed} disabled={busy} onClick={()=>void apply('favourite',favourite.next)}><Box component="span" pos="relative" aria-hidden="true"><Star size={18} fill={favourite.pressed===true?'currentColor':'none'}/>{favourite.pressed==='mixed'&&<Star size={18} fill="currentColor" style={{position:"absolute",left:0,clipPath:"inset(0 50% 0 0)"}}/>}</Box></ReelDockButton><ReelDockButton variant="secondary" iconOnly title="Selected channel options" aria-label="Selected channel options" disabled={busy} onClick={()=>setOpen(true)}><Settings2 size={18}/></ReelDockButton><ReelDockButton variant="secondary" iconOnly title="Clear selection" aria-label="Clear selection" disabled={busy} onClick={onClear}><X size={18}/></ReelDockButton><NotificationMessage kind="success" message={notice}/><NotificationMessage kind="error" message={error}/></MantineGroup></Paper><Dialog open={open} onOpenChange={value=>{if(!busy)setOpen(value);}}><DialogContent ><Stack gap="xs" component="header"><DialogTitle>Channel Options</DialogTitle><DialogDescription>{items.length} selected channels. Only settings you change are applied.</DialogDescription></Stack><Stack gap="lg"><Fieldset variant="unstyled" p={0} disabled={busy}><ChannelSettingsFields channel={items[0]} mixed={mixedChannelFields(items)} preference={(key,value)=>void apply(key,value)} rule={(key,value)=>void apply(key,value,true)}/></Fieldset>{busy&&<Text size="sm" role="status">Saving…</Text>}<MantineGroup gap="sm"><Button variant="secondary" disabled={busy} onClick={()=>setOpen(false)}>Done</Button></MantineGroup></Stack></DialogContent></Dialog></>;
}
