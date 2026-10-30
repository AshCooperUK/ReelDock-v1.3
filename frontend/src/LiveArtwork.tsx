import { Box, Center, Image } from '@mantine/core';
import { useEffect, useRef, useState, type ReactNode } from 'react';

// Keep the same image slot while the worker fetches artwork requested on scroll.
export default function LiveArtwork({src,placeholder,fit='cover'}: {src:string;placeholder?:ReactNode;fit?:'cover'|'contain'}) {
 return <ArtworkImage key={src} src={src} placeholder={placeholder} fit={fit}/>;
}
function ArtworkImage({src,placeholder,fit}: {src:string;placeholder?:ReactNode;fit:'cover'|'contain'}) {
 const image=useRef<HTMLImageElement>(null);
 const [failed,setFailed]=useState(false),[attempt,setAttempt]=useState(0),[loaded,setLoaded]=useState(false);
 useEffect(()=>{
  if(!failed)return;
  let visible=false;
  const observer=new IntersectionObserver(entries=>{visible=entries.some(entry=>entry.isIntersecting);});
  if(image.current)observer.observe(image.current);
  const timer=window.setInterval(()=>{
   if(visible&&!document.hidden)setAttempt(value=>value+1);
  },5000);
  return()=>{observer.disconnect();window.clearInterval(timer);};
 },[failed]);
 const retrySource=attempt ? src+(src.includes('?')?'&':'?')+'artwork_retry='+attempt : src;
 const artwork=<Image ref={image} fit={fit} w="100%" h="100%" src={retrySource} alt="" loading="lazy" style={{visibility:failed||(!loaded&&placeholder!=null)?'hidden':undefined}} onError={()=>{setFailed(true);setLoaded(false);}} onLoad={()=>{setFailed(false);setLoaded(true);}}/>;
 return placeholder==null?artwork:<Box component="span" pos="relative" w="100%" h="100%" style={{display:"block",overflow:"hidden"}}>{!loaded&&<Center component="span" pos="absolute" inset={0} aria-hidden="true">{placeholder}</Center>}{artwork}</Box>;
}
