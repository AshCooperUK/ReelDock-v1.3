import { Group, Text, UnstyledButton } from '@mantine/core';
import { useEffect, useState } from 'react';
import ChannelAvatar from './ChannelAvatar';
import { openChannel } from './channelNavigation';
import { api } from './api';
const images=new Map<string,Promise<string>>();
export default function ChannelIdentity({id,name,avatar}:{id?:string;name:string;avatar?:string}) {
 const [image,setImage]=useState(avatar||'');
 useEffect(()=>{let active=true;setImage(avatar||'');if(!avatar&&id){if(!images.has(id))images.set(id,api<{channel:{avatar?:string}}>('/api/guide/channel/'+encodeURIComponent(id)+'/info').then(data=>{if(!data.channel.avatar)images.delete(id);return data.channel.avatar||'';}).catch(()=>{images.delete(id);return '';}));images.get(id)!.then(value=>{if(active)setImage(value);});}return()=>{active=false;};},[id,avatar]);
 return <UnstyledButton data-channel-identity type="button" disabled={!id} aria-label={'Channel information: '+name} mih={44} maw="100%" style={{minWidth:0,textAlign:'left'}} onClick={event=>{event.stopPropagation();if(id)openChannel({id,name,avatar:image});}}><Group gap="xs" wrap="nowrap" align="center" style={{minWidth:0}}><ChannelAvatar src={image}/><Text size="sm" lineClamp={2} style={{flex:1,minWidth:0,overflowWrap:'anywhere'}}>{name}</Text></Group></UnstyledButton>;
}
