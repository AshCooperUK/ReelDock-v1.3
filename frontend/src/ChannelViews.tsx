import surfaces from './components/MediaSurfaces.module.css';
import { Button as ReelDockButton } from './components/ui';
import { UnstyledButton as ControlTrigger } from '@mantine/core';
import { SelectField as NativeSelect } from './components/SelectField';
import { NotificationMessage } from './notifications';
import { mediaDate } from './mediaDate';
import { useEffect, useRef, useState, type MouseEvent, type KeyboardEvent, type PointerEvent } from 'react';
import { Heart, Settings2 } from 'lucide-react';
import TileActions from './collections/TileActions';
import { Button } from './components/ui';
import ChannelIdentity from './ChannelIdentity';
import ChannelAvatar from './ChannelAvatar';
import { openChannel } from './channelNavigation';
import { api } from './api';
import { diskSize, type Subscription } from './ChannelOptions';
import { Badge, Box, Group, Image, Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { MediaArtwork, MediaCard } from './components/browse';
export type ChannelView='tile'|'list'|'compact'|'detailed'|'max';
export const channelViews:ChannelView[]=['tile','list','compact','detailed','max'];
export function favouriteChannels<T extends {id:string;name:string;favourite:boolean}>(items:T[]):T[]{return [...items].sort((a,b)=>Number(b.favourite)-Number(a.favourite)||a.name.toLowerCase().localeCompare(b.name.toLowerCase(),'en-GB')||a.id.localeCompare(b.id));}
export type ChannelSort='favourites'|'az'|'za'|'latest'|'subscribers';
export const channelSorts:{value:ChannelSort;label:string}[]=[{value:'favourites',label:'Favourites first'},{value:'az',label:'Channel A–Z'},{value:'za',label:'Channel Z–A'},{value:'latest',label:'Latest upload'},{value:'subscribers',label:'Most subscribers'}];
export function sortChannels<T extends {id:string;name:string;favourite:boolean;latest_upload?:string|null;subscribers?:number|string|null}>(items:T[],sort:ChannelSort='favourites'):T[]{
 if(sort==='favourites')return favouriteChannels(items);
 const rank=(item:T)=>{
  if(sort==='latest'){const time=item.latest_upload?Date.parse(item.latest_upload):NaN;return Number.isNaN(time)?-Infinity:time;}
  if(sort==='subscribers'){const value=Number(item.subscribers);return Number.isFinite(value)?value:-Infinity;}
  return 0;
 };
 return [...items].sort((a,b)=>{
  const difference=rank(b)-rank(a);if(difference)return difference;
  const names=a.name.toLowerCase().localeCompare(b.name.toLowerCase(),'en-GB');if(names)return sort==='za'?-names:names;
  return a.id.localeCompare(b.id);
 });
}
const relativeFormat=new Intl.RelativeTimeFormat('en-GB',{numeric:'auto'});
/** Friendly elapsed time ("3 days ago") for a source timestamp; '' when unknown. */
export function relativeTime(value?:string|null):string{
 if(!value)return '';
 const parsed=new Date(value);if(Number.isNaN(parsed.getTime()))return '';
 const minutes=Math.round((parsed.getTime()-Date.now())/60000);
 if(Math.abs(minutes)<1)return 'just now';
 if(Math.abs(minutes)<60)return relativeFormat.format(minutes,'minute');
 const hours=Math.round(minutes/60);
 if(Math.abs(hours)<24)return relativeFormat.format(hours,'hour');
 const days=Math.round(hours/24);
 if(Math.abs(days)<30)return relativeFormat.format(days,'day');
 const months=Math.round(days/30.4);
 if(Math.abs(months)<12)return relativeFormat.format(months,'month');
 return relativeFormat.format(Math.round(days/365.25),'year');
}
/** One-line upload summary for channel rows; '' hides the field (never "Not available"). */
export function uploadLabel(value?:string|null):string{const relative=relativeTime(value);return relative?'Uploaded '+relative:'';}
const labels={tile:'Tile',list:'List',compact:'Compact',detailed:'Detailed',max:'Max'};
export function ChannelViewSelector({csrf,view,onChange}:{csrf:string;view:ChannelView;onChange:(view:ChannelView)=>void}) {
 const [ready,setReady]=useState(false),[saving,setSaving]=useState(false),[status,setStatus]=useState(''),[error,setError]=useState('');
 const revision=useRef(0),busy=useRef(false);
 useEffect(()=>{const c=new AbortController();async function load(){if(document.hidden||busy.current)return;const version=revision.current;try{const data=await api<{view:ChannelView}>('/api/channel-view',c.signal);if(!c.signal.aborted&&version===revision.current){if(channelViews.includes(data.view))onChange(data.view);setReady(true);setError('');}}catch(e){if(!c.signal.aborted)setError(e instanceof Error?e.message:'Could not load your view.');}}
 void load();const timer=setInterval(load,30000);window.addEventListener('focus',load);document.addEventListener('visibilitychange',load);return()=>{c.abort();clearInterval(timer);window.removeEventListener('focus',load);document.removeEventListener('visibilitychange',load);};},[onChange]);
 async function save(value:ChannelView){const previous=view;revision.current++;busy.current=true;setSaving(true);onChange(value);setError('');setStatus('Saving...');try{await api('/api/channel-view',undefined,{view:value},csrf);setStatus('');}catch(e){onChange(previous);setStatus('');setError(e instanceof Error?e.message:'Could not save your view.');}finally{busy.current=false;setSaving(false);}}
 return <Stack data-channel-view-control gap="xs" mih={44} w={{base:'100%',sm:208,md:220}} maw="100%" style={{minWidth:0}}><NativeSelect aria-label="Channel view" value={view} disabled={!ready||saving} onChange={e=>void save(e.target.value as ChannelView)}>{channelViews.map(value=><option value={value} key={value}>{labels[value]}</option>)}</NativeSelect>{status&&<Text size="xs" c="dimmed" role="status">{status}</Text>}{error&&<NotificationMessage kind="error" message={error}/>}</Stack>;
}
function count(value:unknown,compact=false){return value==null||value===''?'Not available':Number(value).toLocaleString('en-GB',compact?{notation:'compact',maximumFractionDigits:1}:{});}
function date(value?:string|null){return mediaDate(value,false)||'Not available';}
export function uploadStamp(value?:string|null){
 if(!value)return 'Date: Not available · Time: Not available';
 const parsed=new Date(value);if(Number.isNaN(parsed.getTime()))return 'Date: Not available · Time: Not available';
 const day=parsed.toLocaleDateString('en-GB',{day:'2-digit',month:'2-digit',year:'numeric',timeZone:'Europe/London'}).replaceAll('/','.');
 const time=/T\d{2}:\d{2}/.test(value)?parsed.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',hourCycle:'h23',timeZone:'Europe/London'}):'Not available';
 return 'Date: '+day+' · Time: '+time;
}
export default function ChannelCard({item,view,saving,onFavourite,selected=false,onSelect}:{item:Subscription;view:ChannelView;saving:boolean;onFavourite:()=>void;selected?:boolean;onSelect?:()=>void}) {
 const hold=useRef<{timer?:ReturnType<typeof setTimeout>;x:number;y:number;touch:boolean;selected:boolean}>({x:0,y:0,touch:false,selected:false});
 const cancelHold=()=>{clearTimeout(hold.current.timer);hold.current.timer=undefined;};
 useEffect(()=>()=>clearTimeout(hold.current.timer),[]);
 const touchSelection={onPointerDownCapture:(e:PointerEvent<HTMLElement>)=>{cancelHold();hold.current.selected=false;hold.current.touch=e.pointerType==='touch';if(!onSelect||!hold.current.touch||!(e.target as HTMLElement).closest('[data-channel-identity]'))return;hold.current.x=e.clientX;hold.current.y=e.clientY;hold.current.timer=setTimeout(()=>{hold.current.selected=true;onSelect();},550);},onPointerMoveCapture:(e:PointerEvent<HTMLElement>)=>{if(Math.hypot(e.clientX-hold.current.x,e.clientY-hold.current.y)>10)cancelHold();},onPointerUpCapture:cancelHold,onPointerCancelCapture:cancelHold,onContextMenuCapture:(e:MouseEvent<HTMLElement>)=>{if(hold.current.touch&&(e.target as HTMLElement).closest('[data-channel-identity]'))e.preventDefault();}};
 const selection={...touchSelection,onMouseDownCapture:(e:MouseEvent<HTMLElement>)=>{if(onSelect&&e.shiftKey){e.preventDefault();}},tabIndex:onSelect?0:undefined,role:onSelect?'option':undefined,'aria-selected':onSelect?selected:undefined,'data-selected':selected||undefined,'aria-label':onSelect?item.name+(selected?', selected':''):undefined,onKeyDown:(e:KeyboardEvent<HTMLElement>)=>{if(onSelect&&e.target===e.currentTarget&&(e.key===' '||e.key==='Enter')){e.preventDefault();onSelect();}},onClickCapture:(e:MouseEvent<HTMLElement>)=>{if(!onSelect)return;const target=e.target as HTMLElement;const identity=target.closest('[data-channel-identity]');if(hold.current.selected&&identity){hold.current.selected=false;e.preventDefault();e.stopPropagation();return;}if((identity&&e.shiftKey)||!target.closest('button,a,input,select')){e.preventDefault();e.stopPropagation();onSelect();}}};
 const options=()=>openChannel({...item,mode:'options'});
 const actions=<Group gap="xs"><Button iconOnly variant="secondary" aria-label={'Favourite '+item.name} title={item.favourite?'Remove from favourites':'Add to favourites'} aria-pressed={item.favourite} disabled={saving} onClick={onFavourite}><Heart size={18} fill={item.favourite?'currentColor':'none'}/></Button><TileActions inline onlyWatchlist item={{id:item.id,kind:'channel',channelId:item.id,title:item.name,image:item.avatar,backdrop:item.banner}}/><Button iconOnly variant="secondary" aria-label={'Channel options: '+item.name} title="Channel options" onClick={options}><Settings2 size={17}/></Button></Group>;
 const automatic=(['videos','shorts','live'] as const).filter(k=>item.downloads[k]).map(k=>({videos:'Videos',shorts:'Shorts',live:'Live'}[k])).join(' · ')||'Off';
 const upload=uploadLabel(item.latest_upload),uploadTitle=mediaDate(item.latest_upload)||undefined;
 const basic=[['Subscribers',count(item.subscribers,true)],['Total videos',count(item.video_count)],['Last video uploaded',relativeTime(item.latest_upload)]];
 const stats=view==='max'?[...basic,['Total views',count(item.total_views)],['Average views · latest '+(item.recent_sample||0)+' indexed uploads',count(item.recent_average==null?null:Math.round(item.recent_average))],['Indexed uploads',count(item.indexed_videos)],['Saved videos',count(item.saved_count)],['Storage used',diskSize(item.disk_bytes)],['Channel created',date(item.created)],['Country',item.country||'Not available'],['Language',item.language||'Not available'],['Handle',item.handle||'Not available'],['Automatic downloads',automatic],['Quality',item.downloads.quality+'p'],['Download range',item.downloads.range_days?'Last '+item.downloads.range_days+' days':'All uploads'],['Show in guide',item.shown?'On':'Off'],['Upload forecasts',item.forecasts?'On':'Off'],['Last checked',item.checked_label||'Not checked yet']]:basic;
 const metadata=<SimpleGrid component="dl" cols={{base:1,sm:2}} spacing="sm" m={0}>{stats.filter(([,value])=>value!=='Not available'&&value!=='').map(([label,value])=><Box key={label}><Text component="dt" size="xs" c="dimmed">{label}</Text><Text component="dd" m={0} size="sm" fw={500}>{value}</Text></Box>)}</SimpleGrid>;
 if(view==='tile')return <MediaCard {...selection} active={selected} art={<ControlTrigger data-channel-identity w="100%" style={{display:'block'}} aria-label={'Channel information: '+item.name} onClick={()=>openChannel(item)}><MediaArtwork src={item.avatar||item.banner} kind="channel"/></ControlTrigger>} title={<ChannelIdentity id={item.id} name={item.name} avatar={item.avatar}/>} meta={count(item.subscribers,true)+' subscribers · '+count(item.video_count)+' videos'} status={<>{upload&&<Text size="xs" title={uploadTitle}>{upload}</Text>}<Text size="xs">Auto-download: {automatic}</Text><Text size="xs">{diskSize(item.disk_bytes)}</Text></>} actions={actions}><Text size="sm" c="dimmed" lineClamp={2}>{item.description||'No channel description available.'}</Text></MediaCard>;
 return <Paper component="article" {...selection} data-media-surface="tile" className={[surfaces.surface,surfaces.tile].filter(Boolean).join(' ')} withBorder p={view==='compact'?'sm':'md'} radius="md" style={{outline:selected?'2px solid var(--mantine-primary-color-filled)':undefined,minWidth:0}}><Stack gap={view==='compact'?'xs':'md'}>{view==='max'&&item.banner&&<Image src={item.banner} alt="" h={96} radius="sm" fit="cover" loading="lazy"/>}<Group justify="space-between" align="flex-start"><Stack gap={4} style={{flex:1,minWidth:0}}><ChannelIdentity id={item.id} name={item.name} avatar={item.avatar}/>{upload&&<Text size="xs" c="dimmed" title={uploadTitle}>{upload}</Text>}</Stack>{actions}</Group>{(view==='detailed'||view==='max')&&metadata}{view==='max'&&<><Text size="sm" c="dimmed" style={{whiteSpace:'pre-line'}}>{item.description||'No channel description available.'}</Text><Text size="xs">{item.coverage||'Upload history not checked yet.'}</Text>{!!item.topics?.length&&<Text size="sm">Topics: {item.topics.map(t=>decodeURIComponent(t.split('/').pop()||'').replaceAll('_',' ')).join(' · ')}</Text>}<Text size="xs" c="dimmed">Statistics and latest upload are based on data ReelDock has indexed.</Text></>}</Stack></Paper>;
}
