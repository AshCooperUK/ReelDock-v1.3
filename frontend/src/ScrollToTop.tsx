import { ActionIcon, Affix, Group } from '@mantine/core';
import { useEffect, useRef, useState, type RefObject } from 'react';
import { ArrowDown, ArrowUp } from 'lucide-react';
export default function ScrollToTop({root,scrollBottom=false,bottomOffset}:{root?:RefObject<HTMLElement|null>;scrollBottom?:boolean;bottomOffset?:number}){
 const[visible,setVisible]=useState(false),scroller=useRef<HTMLElement|null>(null);
 useEffect(()=>{const update=(event?:Event)=>{const node=event?.target instanceof HTMLElement?event.target:null;if(root){if(!node||!root.current?.contains(node))return;scroller.current=node;setVisible(node.scrollTop>(scrollBottom?0:240));}else if(!node||node===document.documentElement||node===document.body)setVisible(window.scrollY>400);};update();window.addEventListener('scroll',update,true);return()=>window.removeEventListener('scroll',update,true);},[root,scrollBottom]);
 if(!visible)return null;
 const behavior=()=>window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto' as const:'smooth' as const;
 const buttons=<Group gap="xs"><ActionIcon variant="default" aria-label="Scroll to top" onClick={()=>root?scroller.current?.scrollTo({top:0,behavior:behavior()}):window.scrollTo({top:0,behavior:behavior()})}><ArrowUp size={18}/></ActionIcon>{root&&scrollBottom&&<ActionIcon variant="default" aria-label="Scroll to bottom" onClick={()=>scroller.current?.scrollTo({top:scroller.current.scrollHeight,behavior:behavior()})}><ArrowDown size={18}/></ActionIcon>}</Group>;
 return root?<Group pos="absolute" bottom={12} right={12} style={{zIndex:6}}>{buttons}</Group>:<Affix position={{bottom:bottomOffset===undefined?'calc(82px + env(safe-area-inset-bottom))':bottomOffset+16,right:16}} zIndex={80}>{buttons}</Affix>;
}
