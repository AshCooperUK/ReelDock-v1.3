import { MediaArtwork, MediaLengthBadge } from './components/browse';
import { mediaLength, type MediaDuration } from './mediaDuration';
import { Stack, Text } from '@mantine/core';
import { isValidElement, useEffect, useState, type ReactNode } from 'react';
import QuickDownload from './QuickDownload';
import { acquisitionItem } from './discoverPayload';
import { Button } from './components/ui';
import { DetailSheet, type DetailSheetTab } from './components/DetailSheet';
import MediaManagement, { MediaTitleHistory } from './MediaManagement';
import ChannelIdentity from './ChannelIdentity';
import {useChannelBanner} from './useChannelBanner';

// One video surface: information, caller-authorized downloads, file/policy
// management and existing history. Callers retain all download permissions.
export default function VideoSheet({csrf,open,onOpenChange,video,subtitle,actions,content,downloads,description,canManage=true,readOnly=false,className,bodyClassName,back}:{
 csrf:string;open:boolean;onOpenChange:(open:boolean)=>void;
 video:MediaDuration&{id:string;title:string;image?:string;backdrop?:string;banner?:string;channel?:string;channelId?:string;avatar?:string};
 subtitle?:ReactNode;actions?:ReactNode;content?:ReactNode;downloads?:ReactNode;description?:ReactNode;
 canManage?:boolean;readOnly?:boolean;className?:string;bodyClassName?:string;
 back?:{label:ReactNode;onBack:()=>void};
}) {
 const item={kind:'video' as const,id:'video:'+video.id,videoId:video.id,title:video.title,image:video.image,channel:video.channel,channelId:video.channelId};
 const manage=!readOnly&&canManage&&!!csrf&&!!video.id;
 const quickAllowed=!!csrf&&!readOnly&&!!video.id&&isValidElement<{id?:string;canDownload?:boolean;csrf?:string;readOnly?:boolean;localOnly?:boolean}>(downloads)&&downloads.props.canDownload===true&&downloads.props.id===video.id&&downloads.props.csrf===csrf&&!downloads.props.readOnly&&!downloads.props.localOnly;
 const channelBanner=useChannelBanner(video.channelId,video.banner,open);
 const [expanded,setExpanded]=useState(false),[tab,setTab]=useState('overview'),[historyFocus,setHistoryFocus]=useState('');
 useEffect(()=>{setExpanded(false);setTab('overview');setHistoryFocus('');},[video.id,open]);
 const overview=<Stack gap="md"><MediaLengthBadge length={mediaLength(video)}/>{content}{description&&<>
  <Text lineClamp={expanded?undefined:4} style={{whiteSpace:"pre-line"}}>{description}</Text>
  {typeof description==='string'&&description.length>240&&<Button type="button" variant="ghost" onClick={()=>setExpanded(v=>!v)}>{expanded?'Show less':'More'}</Button>}
 </>}{!content&&!description&&<Text c="dimmed" size="sm">No description is available for this video.</Text>}</Stack>;
 const tabs:DetailSheetTab[]=[{id:'overview',label:'Overview',content:overview}];
 if(downloads!==undefined&&downloads!==null&&downloads!==false)tabs.push({id:'downloads',label:'Downloads',content:downloads});
 if(manage)tabs.push(
  {id:'manage',label:'Manage',content:<MediaManagement csrf={csrf} manageKey={item.id} item={item} onOpenTab={(next,row)=>{if(next==='history'){setHistoryFocus(row?.key||'');setTab('history');}}}/>},
  {id:'history',label:'History',content:<MediaTitleHistory csrf={csrf} item={item} focus={historyFocus}/>},
 );
 return <DetailSheet open={open} onOpenChange={onOpenChange}
  className={className}
  bodyClassName={bodyClassName}
  banner={channelBanner||video.backdrop||video.image}
  bannerFallback={video.image}
  media={<MediaArtwork src={video.image} kind="video" fit="contain"/>}
  title={video.title}
  subtitle={subtitle??'YouTube'}
  channel={video.channel?<ChannelIdentity id={video.channelId} name={video.channel} avatar={video.avatar}/>:undefined}
  actions={quickAllowed?<QuickDownload actions={actions} item={acquisitionItem(item)} csrf={csrf} onChoices={()=>setTab('downloads')}/>:actions}
  back={back}
  tabs={tabs.length>1?tabs:undefined}
  tab={tabs.some(entry=>entry.id===tab)?tab:'overview'}
  onTab={setTab}
 >{overview}</DetailSheet>;
}
