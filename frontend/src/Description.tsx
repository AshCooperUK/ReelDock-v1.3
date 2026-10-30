import { Anchor, Text } from '@mantine/core';
export default function Description({text}:{text:string}) {
 const parts=text.split(/(https?:\/\/[^\s<>]+|www\.[^\s<>]+)/gi);
 return <Text style={{whiteSpace:"pre-line",overflowWrap:"anywhere"}}>{parts.map((part,index)=>{
  if(!/^(https?:\/\/|www\.)/i.test(part))return part;
  const trimmed=part.replace(/[.,!?;:]+$/, '');
  const href=/^www\./i.test(trimmed)?'https://'+trimmed:trimmed;
  try { const url=new URL(href); if(!['http:','https:'].includes(url.protocol))return part; }
  catch {return part;}
  return <span key={index}><Anchor href={href} target="_blank" rel="noopener noreferrer">{trimmed}</Anchor>{part.slice(trimmed.length)}</span>;
 })}</Text>;
}
