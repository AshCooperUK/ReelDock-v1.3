import { Card, SimpleGrid, Stack, Text } from '@mantine/core';
import { Button } from './components/ui';
import { useId, useLayoutEffect, useRef, useState } from 'react';
import { channelFacts, type Info } from './ChannelInfo';

const guess=(text:string)=>text.length>240||text.split('\n').length>3;

/** Plain-text description plus the known channel facts, in the movie-popup fact style. */
export default function ChannelOverview({channel}:{channel:Info}) {
 const description=(channel.description||'').trim();
 const [expanded,setExpanded]=useState(false),[overflow,setOverflow]=useState(()=>guess(description)),[seen,setSeen]=useState(description);
 const id=useId(),paragraph=useRef<HTMLParagraphElement>(null),open=useRef(false);
 if(seen!==description){setSeen(description);setExpanded(false);setOverflow(guess(description));}
 open.current=expanded;
 // The collapsed paragraph is measured after layout and on width changes; the length guess only covers the first paint.
 useLayoutEffect(()=>{
  const element=paragraph.current;if(!element)return;
  const measure=()=>{if(!open.current)setOverflow(element.scrollHeight>element.clientHeight+1);};
  measure();
  const observer=typeof ResizeObserver==='undefined'?null:new ResizeObserver(measure);
  observer?.observe(element);
  return()=>observer?.disconnect();
 },[description]);
 const facts=channelFacts(channel).filter((entry):entry is [string,string]=>!!entry[1]);
 return <Stack gap="md" component="section" aria-label="Channel overview">
  {description?<>
   <Text ref={paragraph} id={id} lineClamp={expanded?undefined:4} style={{whiteSpace:"pre-line"}}>{description}</Text>
   {(overflow||expanded)&&<Button type="button" variant="ghost" aria-expanded={expanded} aria-controls={id} onClick={()=>setExpanded(value=>!value)}>{expanded?'Show less':'More'}</Button>}
  </>:<Text c="dimmed">No channel description is available.</Text>}
  {facts.length?<SimpleGrid component="dl" cols={{base:1,sm:2}} spacing="sm" mt="md">{facts.map(([label,value])=><Card key={label} withBorder p="sm"><Text component="dt" size="xs" c="dimmed">{label}</Text><Text component="dd" size="sm" m={0} mt={4}>{value}</Text></Card>)}</SimpleGrid>:<Text c="dimmed">No channel details are available yet.</Text>}
 </Stack>;
}
