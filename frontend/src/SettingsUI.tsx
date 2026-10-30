import { Accordion, ActionIcon, Alert, Box, Group as MantineGroup, Input, Loader, Paper, Stack, Text, Title, Tooltip } from '@mantine/core';
import { Children, cloneElement, isValidElement, useEffect, useId, useState, type ReactElement, type ReactNode } from 'react';
import { Info } from 'lucide-react';
import { api } from './api';
import { Button, PanelButton, Switch } from './components/ui';
import { Panel } from './components/layout';

export function useSettings<T>(url: string) {
  const [data,setData]=useState<T|null>(null),[error,setError]=useState(''),[revision,setRevision]=useState(0);
  useEffect(()=>{const c=new AbortController();setError('');setData(null);api<T>(url,c.signal,undefined,undefined,{feedback:'inline'}).then(d=>{if(!c.signal.aborted)setData(d);}).catch(e=>{if(!c.signal.aborted)setError(e.message);});return()=>c.abort();},[url,revision]);
  return {data,setData,error,reload:()=>setRevision(v=>v+1)};
}

/** One settings section: heading and content share the same panel rhythm. */
export function Group({title,children}:{title:ReactNode;children:ReactNode}) {
  return <Panel title={title}><Stack gap="md">{children}</Stack></Panel>;
}
export function Help({text}:{text:string}) {
  const [open,setOpen]=useState(false);
  return <Tooltip label={text} opened={open} multiline w={280} withArrow withinPortal><ActionIcon variant="subtle" aria-label="More information" aria-expanded={open}
    onMouseEnter={()=>setOpen(true)} onMouseLeave={()=>setOpen(false)} onFocus={()=>setOpen(true)} onBlur={()=>setOpen(false)} onClick={()=>setOpen(true)}
    onKeyDown={event=>{if(event.key==='Escape'&&open){event.preventDefault();event.stopPropagation();setOpen(false);}}}><Info size={18}/></ActionIcon></Tooltip>;
}
type FieldControl={id?:string;required?:boolean;label?:ReactNode;description?:ReactNode;inputWrapperOrder?:('label'|'input'|'description'|'error')[];'aria-label'?:string;'aria-labelledby'?:string;'aria-describedby'?:string};
/** Labels above full-width controls, explanation below. No helper changes label height. */
export function Field({label,help,note,children}:{label:string;help?:string;note?:string;children:ReactNode}) {
  const generated=useId();
  const child=Children.count(children)===1&&isValidElement(children)?children as ReactElement<FieldControl>:null;
  const id=child?.props.id||generated;
  const description=[help,note].filter(Boolean).join(' ');
  const describedBy=[child?.props['aria-describedby'],description?`${id}-description`:null].filter(Boolean).join(' ')||undefined;
  const naming={id,'aria-label':child?.props['aria-label']||(!child?.props['aria-labelledby']?label:undefined),'aria-describedby':describedBy};
  if(child&&typeof child.type!=='string')return cloneElement(child,{...naming,label:child.props.label??label,description:child.props.description??(description||undefined),inputWrapperOrder:['label','input','description','error']});
  const field=child?cloneElement(child,naming):children;
  return <Input.Wrapper id={id} label={label} description={description||undefined} required={child?.props.required} inputWrapperOrder={['label','input','description','error']} w="100%">{field}</Input.Wrapper>;
}
export function Row({title,detail,children}:{title:string;detail?:string;children:ReactNode}) {
  const child=Children.count(children)===1&&isValidElement(children)?children as ReactElement<{onChange?:unknown;value?:unknown;defaultValue?:unknown}>:null;
  const control=child&&child.type!=='button'&&child.type!==Button&&(typeof child.props.onChange==='function'||'value' in child.props||'defaultValue' in child.props);
  if(control)return <Field label={title} help={detail}>{children}</Field>;
  return <MantineGroup justify="space-between" gap="md" wrap="wrap" align="flex-start"><Stack gap={4} style={{minWidth:0,flex:'1 1 12rem'}}><Text size="sm" c="dimmed">{title}</Text>{detail&&<Text size="xs" c="dimmed">{detail}</Text>}</Stack><Box style={{minWidth:0,maxWidth:'100%'}}>{children}</Box></MantineGroup>;
}
export function Toggle({title,detail,value,onChange,disabled}:{title:string;detail?:string;value:boolean;onChange:(v:boolean)=>void;disabled?:boolean}) {
  return <MantineGroup justify="space-between" gap="lg" wrap="nowrap" align="center"><Stack gap={4} style={{flex:1,minWidth:0}}><Text fw={500} size="sm">{title}</Text>{detail&&<Text size="sm" c="dimmed">{detail}</Text>}</Stack><Box style={{flexShrink:0}}><Switch label={title} checked={value} onCheckedChange={onChange} disabled={disabled}/></Box></MantineGroup>;
}
/** Advanced settings expand in the page; they never create another window. */
export function Disclosure({title,subject,children,disabled=false,icon}:{title:string;subject?:string;children:ReactNode;disabled?:boolean;dialogClassName?:string;icon?:ReactNode}) {
  return <PanelButton title={title} subject={subject} disabled={disabled} icon={icon}><Stack gap="md">{children}</Stack></PanelButton>;
}
export function Notice({text,error=false,inline=false}:{text:string;error?:boolean;inline?:boolean}) {
  if(!text)return null;
  if(inline||/^(Saving|Connecting|Uploading|Loading|Checking|Preparing|Opening|Removing|Processing|Working)[.…]*$/i.test(text.trim()))return <Text size="sm" c="dimmed" role="status">{text}</Text>;
  return <Alert color={error?'red':/\b(saved|added|removed|updated|queued|requested|completed|enabled|disabled)\b/i.test(text)?'teal':'blue'} variant="light" role={error?'alert':'status'}>{text}</Alert>;
}
export function Loading({error,retry}:{error:string;retry:()=>void}) {
  return error?<Stack align="flex-start"><Notice text={error} error/><Button variant="secondary" onClick={retry}>Try again</Button></Stack>:<MantineGroup role="status"><Loader size="sm"/><Text c="dimmed">Loading settings…</Text></MantineGroup>;
}
export function useAction(csrf:string){const[busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[error,setError]=useState('');async function run<T>(url:string,body:object|undefined,success='Saved.') {setBusy(true);setError('');setNotice('');try{const result=await api<T>(url,undefined,body,csrf,{feedback:'inline'});setNotice(success);return result;}catch(e){setError(e instanceof Error?e.message:'Please try again.');return null;}finally{setBusy(false);}}return{busy,notice,error,run,setNotice};}
