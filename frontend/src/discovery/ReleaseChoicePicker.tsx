import { Box, Button, Grid, Group, Paper, Radio, Stack, Text, VisuallyHidden } from '@mantine/core';
import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { fullReleaseLabel, type ReleaseLabel } from './releaseLabels';

export type ReleaseChoice=ReleaseLabel&{id:string;quality?:string};
export function releaseChoiceFacts(release:ReleaseChoice){
 const quality=release.quality||release.title.match(/\b(?:2160|1080|720|576|480)p\b/i)?.[0];
 return [quality,release.size?(release.size/1073741824).toFixed(1)+' GB':''].filter(Boolean).join(' · ');
}
/** Keyboard movement uses exact IDs and never lands on a rejected file. */
export function releaseChoiceKey(releases:ReleaseChoice[],value:string,key:string){
 const available=releases.filter(release=>!release.reason);
 if(!available.length)return null;
 const index=available.findIndex(release=>release.id===value);
 if(key==='Home')return available[0].id;
 if(key==='End')return available[available.length-1].id;
 if(key==='ArrowDown'||key==='ArrowRight')return available[(index+1)%available.length].id;
 if(key==='ArrowUp'||key==='ArrowLeft')return available[index<0?available.length-1:(index-1+available.length)%available.length].id;
 return null;
}

/** Every width uses a readable full-width file choice in the sheet's flow.
 * Expanded files share the existing body scroll and exact provider identity. */
export function ReleaseChoicePicker({releases,value,onChange,label='Movie release',kind='file'}:{releases:ReleaseChoice[];value:string;onChange:(id:string)=>void;label?:string;kind?:'file'|'source'}){
 const [opened,setOpened]=useState(false),trigger=useRef<HTMLButtonElement>(null),choices=useId();
 const selected=releases.find(release=>release.id===value),source=kind==='source';
 const availableLabel=releases.length+' '+(source?(releases.length===1?'source available':'sources available'):'files available');
 const facts=source?availableLabel:selected?releaseChoiceFacts(selected):availableLabel;
 function close(){setOpened(false);trigger.current?.focus();}
 function choose(id:string,collapse=true){
  if(!releases.some(release=>release.id===id&&!release.reason))return;
  if(id!==value)onChange(id);
  if(collapse)close();
 }
 function keyDown(event:KeyboardEvent<HTMLDivElement>){
  if(event.key==='Escape'&&opened){event.preventDefault();event.stopPropagation();close();return;}
  const target=event.target as HTMLInputElement;
  if(target.type!=='radio')return;
  const id=releaseChoiceKey(releases,target.value,event.key);
  if(!id)return;
  event.preventDefault();event.stopPropagation();choose(id,false);
  Array.from(event.currentTarget.querySelectorAll<HTMLInputElement>('input[type="radio"]')).find(input=>input.value===id)?.focus();
 }
 return <Stack gap="xs" w="100%" style={{minWidth:0}} data-release-chooser="inline" data-choice-kind={kind} onKeyDown={keyDown}>
  <Button ref={trigger} variant="default" fullWidth h="auto" mih={44} px="sm" py="sm" title={selected?fullReleaseLabel(selected):source?'Choose a source':'Choose a file'} aria-label={label} aria-expanded={opened} aria-controls={choices}
   rightSection={opened?<ChevronUp size={18}/>:<ChevronDown size={18}/>} onClick={()=>setOpened(current=>!current)}
   styles={{inner:{alignItems:'flex-start'},label:{display:'block',whiteSpace:'normal',textAlign:'left',minWidth:0,flex:1},section:{alignSelf:'center'}}}>
   <Stack gap={4} style={{minWidth:0}}><Text size="sm" fw={500} lineClamp={2} style={{overflowWrap:'anywhere'}}>{selected?.title||(source?'Choose a source':'Choose a file')}</Text><Text size="xs" c="dimmed">{facts} · {opened?(source?'Hide sources':'Hide files'):(source?'Change source':'Change file')}</Text></Stack>
  </Button>
  {opened&&<Box id={choices} w="100%" style={{minWidth:0}}><Radio.Group value={value} onChange={id=>choose(id)} label={<VisuallyHidden>{label}</VisuallyHidden>}>
   <Stack gap="xs">{releases.map(release=><Paper key={release.id} withBorder radius="md" p="sm" w="100%" style={{minWidth:0}}>
    <Radio value={release.id} disabled={!!release.reason} aria-label={fullReleaseLabel(release)}
     styles={{root:{width:'100%'},body:{alignItems:'flex-start',minHeight:44},labelWrapper:{flex:1,minWidth:0},label:{display:'block',minHeight:44,whiteSpace:'normal',overflowWrap:'anywhere'}}}
     label={<Stack gap={4}><Text size="sm" fw={500} style={{overflowWrap:'anywhere'}}>{release.title}</Text>{!source&&releaseChoiceFacts(release)&&<Text size="xs" c="dimmed">{releaseChoiceFacts(release)}</Text>}{release.reason&&<Text size="xs" c="dimmed">Unavailable · {release.reason}</Text>}</Stack>}/>
   </Paper>)}</Stack>
  </Radio.Group></Box>}
 </Stack>;
}

/** Source names need the same readable in-sheet space as long filenames. */
export function DownloadSourcePicker({sources,value,onChange}:{sources:string[];value:string;onChange:(source:string)=>void}){
 return <ReleaseChoicePicker kind="source" label="Download source" releases={Array.from(new Set(sources),source=>({id:source,title:source,size:0,reason:''}))} value={value} onChange={onChange}/>;
}

/** File, source and action each own a row so expanded choices cannot be
 * constrained by the Download action or by space beneath a floating menu. */
export function DownloadChoiceLayout({source,file,action}:{source:ReactNode;file:ReactNode;action:ReactNode}){
 return <Grid gap="sm" align="end" data-download-choice-layout="" style={{minWidth:0}}>
  <Grid.Col span={{base:12,sm:12}} style={{minWidth:0}}><Stack gap={6} style={{minWidth:0}}><Text size="xs" c="dimmed">File</Text>{file}</Stack></Grid.Col>
  <Grid.Col span={{base:12,sm:12}} style={{minWidth:0}}><Stack gap={6} style={{minWidth:0}}><Text size="xs" c="dimmed">Source</Text>{source}</Stack></Grid.Col>
  <Grid.Col span={{base:12,sm:12}} style={{minWidth:0}}><Group grow gap="xs" wrap="nowrap" style={{minWidth:0}}>{action}</Group></Grid.Col>
 </Grid>;
}
