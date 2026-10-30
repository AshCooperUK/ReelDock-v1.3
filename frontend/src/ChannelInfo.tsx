import type {MediaDuration} from './mediaDuration';
import { Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import VideoTitle from './VideoTitle';
import { mediaDate } from './mediaDate';
import Description from './Description';
import ChannelVideos from './ChannelVideos';
import { Group, Row, Loading, useSettings } from './SettingsUI';
export type Info={public?:boolean;name?:string;avatar?:string;banner?:string;description?:string;subscribers?:number|string;total_views?:number|string;video_count?:number|string;country?:string;created?:string;handle?:string;language?:string;topics?:string[];recent_average?:number;recent_sample?:number};
function count(value:unknown) {
 if(value==null || value==='')return undefined;
 const number=Number(value);
 return Number.isFinite(number)?number.toLocaleString('en-GB',{maximumFractionDigits:0}):undefined;
}
function displayName(value:string|undefined,type:'region'|'language') {
 if(!value)return undefined;
 try{return new Intl.DisplayNames(['en-GB'],{type}).of(value)||value;}catch{return value;}
}
function topicName(value:string) {
 try{return decodeURIComponent(new URL(value).pathname.split('/').filter(Boolean).pop()||'').replaceAll('_',' ');}catch{return value;}
}
export default function ChannelInfo({id,readOnly=false,data}:{id:string;readOnly?:boolean;data?:{channel:Info;csrf:string}}) {
 if(data)return <ChannelInfoBody id={id} readOnly={readOnly} data={data}/>;
 return <ChannelInfoFetch id={id} readOnly={readOnly}/>;
}
function ChannelInfoFetch({id,readOnly}:{id:string;readOnly?:boolean}) {
 const r=useSettings<{channel:Info;csrf:string}>('/api/guide/channel/'+encodeURIComponent(id)+'/info');
 if(!r.data)return <Loading error={r.error} retry={r.reload}/>;
 return <ChannelInfoBody id={id} readOnly={readOnly} data={r.data}/>;
}
// Stats + description, separated from the uploads list so the channel sheet
// can order header → videos → collapsed about → stats (brief §2.3).
export function channelFacts(c:Info):[string,string|undefined][] {
 const created=c.created?new Date(c.created):null;
 const validCreated=created&&Number.isFinite(created.getTime());
 const now=new Date();
 let years=validCreated?now.getFullYear()-created.getFullYear():null;
 if(years!==null&&created&&(now.getMonth()<created.getMonth()||(now.getMonth()===created.getMonth()&&now.getDate()<created.getDate())))years--;
 return [
  ['Subscribers',count(c.subscribers)],['Total views',count(c.total_views)],['Public videos',count(c.video_count)],
  ['Country',displayName(c.country,'region')],['Topics',c.topics?.map(topicName).filter(Boolean).join(', ')],
  ['Channel age',years!==null&&years>=0?(years===0?'Less than a year':`${years} ${years===1?'year':'years'}`):undefined],
  ['Created',validCreated?mediaDate(c.created,false):undefined],
  ['Language',displayName(c.language,'language')],['Handle',c.handle],
  ['Recent average views',c.recent_average==null?undefined:count(c.recent_average)+(c.recent_sample?` (${c.recent_sample} recent videos)`:'')],
  ['Total views / subscriber',Number(c.subscribers)>0&&c.total_views!=null&&Number.isFinite(Number(c.total_views))?(Number(c.total_views)/Number(c.subscribers)).toFixed(1)+'×':undefined]
 ];
}
export function ChannelAbout({channel}:{channel:Info}) {
 const c=channel,rows=channelFacts(c);
 return <Stack gap="lg">{c.description&&<Description text={c.description}/>}<Group title="Channel information">{(c.public?rows.filter(([,value])=>!!value):rows).map(([title,value])=><Row key={title} title={title}><span>{value||'Not available'}</span></Row>)}</Group></Stack>;
}
export function ChannelUploads({id,name,csrf,isPublic,readOnly,onVideoOpen,title='Recent uploads'}:{id:string;name:string;csrf:string;isPublic?:boolean;readOnly?:boolean;onVideoOpen?:(video:SheetVideo)=>void;title?:string}) {
 return <Stack component="section" gap="md" aria-label={title}>{isPublic?<><Title order={3}>{title}</Title><PublicUploads id={id} onVideoOpen={onVideoOpen}/></>:<ChannelVideos id={id} name={name} csrf={csrf} readOnly={readOnly} onOpen={onVideoOpen} title={title}/>}</Stack>;
}
function ChannelInfoBody({id,readOnly,data}:{id:string;readOnly?:boolean;data:{channel:Info;csrf:string}}) {
 return <Stack gap="lg"><ChannelAbout channel={data.channel}/><ChannelUploads id={id} name={data.channel.name||'Channel'} csrf={data.csrf} isPublic={data.channel.public} readOnly={readOnly}/></Stack>;
}

// A video opened from inside the channel sheet becomes the sheet's video view
// (SheetBack returns to the channel) instead of stacking a second overlay.
export type SheetVideo=MediaDuration&{id:string;title:string;image?:string;thumbnail?:string;state?:string;description?:string;metadata_pending?:boolean;duration?:string};
function PublicUploads({id,onVideoOpen}:{id:string;onVideoOpen?:(video:SheetVideo)=>void}) {
 const r=useSettings<{items:{videoId:string;title:string;image?:string;channel?:string;published?:string}[]}>('/api/youtube-discover/channel/'+encodeURIComponent(id)+'/uploads');
 if(!r.data)return <Loading error={r.error} retry={r.reload}/>;
 return <Stack gap="sm">{r.data.items.map(video=><Paper withBorder p="md" radius="md" key={video.videoId}><VideoTitle id={video.videoId} title={video.title} channel={id} channelName={video.channel} poster={video.image} onOpen={onVideoOpen?()=>onVideoOpen({id:video.videoId,title:video.title,image:video.image}):undefined}/></Paper>)}{!r.data.items.length&&<Text size="sm">No public uploads are available.</Text>}</Stack>;
}
