import { Avatar, Box, Center } from '@mantine/core';
import LiveArtwork from './LiveArtwork';
import { Youtube } from 'lucide-react';
export default function ChannelAvatar({src,detail=false}:{src?:string;detail?:boolean}){
 const placeholder=<Youtube size={detail?28:18} aria-hidden="true"/>;
 if(detail)return <Box data-channel-detail-art w="100%" bg="var(--mantine-color-default)" c="dimmed" style={{aspectRatio:'1',overflow:'hidden',borderRadius:'var(--mantine-radius-md)',flexShrink:0}}>{src?<LiveArtwork src={src} fit="contain" placeholder={placeholder}/>:<Center h="100%">{placeholder}</Center>}</Box>;
 return <Avatar size={32} radius="xl" style={{flexShrink:0}}>{src?<LiveArtwork src={src} placeholder={placeholder}/>:placeholder}</Avatar>;
}
