import { ActionIcon, Box, Collapse, Group, Menu, Paper, Stack, Text, Tooltip } from '@mantine/core';
import { useMediaQuery } from '@mantine/hooks';
import { Children, cloneElement, createContext, Fragment, isValidElement, useContext, useId, useState, type MouseEventHandler, type ReactNode } from 'react';
import { ArrowRight, Ban, ChevronDown, ChevronUp, EllipsisVertical, Info, Pause, RotateCcw, SlidersHorizontal, Trash2 } from 'lucide-react';
import styles from './ActivityRow.module.css';

const CompactActivityContext=createContext(false);
const QueueActivityContext=createContext(false);
export function ActivityQueueRows({children}:{children:ReactNode}) {return <QueueActivityContext.Provider value={true}><CompactActivityRows>{children}</CompactActivityRows></QueueActivityContext.Provider>;}
export function CompactActivityRows({children}:{children:ReactNode}) {
 const phone=useMediaQuery('(max-width: 47.99em)',false,{getInitialValueInEffect:false});
 return <CompactActivityContext.Provider value={phone}>{children}</CompactActivityContext.Provider>;
}
type ActionProps={children?:ReactNode;name?:string;title?:string;onClick?:MouseEventHandler<HTMLButtonElement>;disabled?:boolean;variant?:string;'aria-label'?:string};
function readable(node:ReactNode):string {
 return Children.toArray(node).map(child=>typeof child==='string'||typeof child==='number'?String(child):isValidElement<ActionProps>(child)?child.props.children!=null?readable(child.props.children):child.props.name||child.props.title||'':'').filter(Boolean).join(' · ').replace(/(?:\s*·\s*){2,}/g,' · ');
}
function actionNodes(node:ReactNode):ReactNode[] {
 return Children.toArray(node).flatMap(child=>isValidElement<ActionProps>(child)&&child.type===Fragment?actionNodes(child.props.children):[child]);
}
function QueueActionGlyph({label}:{label:string}){const Icon=/cancel|stop/i.test(label)?Ban:/retry/i.test(label)?RotateCcw:/pause/i.test(label)?Pause:/resume/i.test(label)?ArrowRight:/metadata/i.test(label)?SlidersHorizontal:/remove/i.test(label)?Trash2:/hide/i.test(label)?ChevronUp:/show/i.test(label)?ChevronDown:Info;return <Icon size={20} strokeWidth={1.8} aria-hidden="true"/>;}
/** One fresh Mantine activity card. Engine owners supply every original action. */
export default function ListRow({media,title,badge,statusColour,meta,progress,actions,contextAction,clearAction,actionsDisabled=false,details,detailsLabel='details',className,timestamp}:{media?:ReactNode;title:ReactNode;badge?:ReactNode;statusColour?:string;meta?:ReactNode;progress?:ReactNode;actions?:ReactNode;contextAction?:ReactNode;clearAction?:ReactNode;actionsDisabled?:boolean;details?:ReactNode;detailsLabel?:string;className?:string;timestamp?:string}) {
 const [open,setOpen]=useState(false),phone=useContext(CompactActivityContext),queue=useContext(QueueActivityContext),detailsId=useId();
 const name=readable(title)||'activity item',summary=readable(meta);
 const originalOpen=isValidElement<ActionProps>(title)?title.props.onClick:undefined;
 const items=actionNodes(actions).filter(item=>!(originalOpen&&isValidElement<ActionProps>(item)&&!item.props.disabled&&/^(open|info)\b/i.test(item.props['aria-label']||readable(item.props.children)))).map(item=>actionsDisabled&&isValidElement<ActionProps>(item)&&typeof item.props.onClick==='function'?cloneElement(item,{disabled:true}):item);
 const expandable=!!(details||meta);
 const quick=phone?[]:items.filter(item=>isValidElement<ActionProps>(item)&&/^(cancel|pause|resume|retry)\b/i.test(item.props['aria-label']||readable(item.props.children))).slice(0,1);
 const menuItems=items.filter(item=>!quick.includes(item));
 const statusProps=isValidElement<{state?:string;phaseLabel?:string;hideIcon?:boolean}>(badge)?badge.props:undefined;
 const queueBadge=statusProps?.state&&isValidElement<{hideIcon?:boolean;concise?:boolean}>(badge)?cloneElement(badge,{hideIcon:true,concise:true}):badge;
 const iconStyle=statusColour?{border:`2px solid var(--mantine-color-${statusColour}-light-color)`,background:`var(--mantine-color-${statusColour}-light)`,padding:6}: {padding:6};
 const detailsControl=expandable?<Tooltip label={(open?'Hide ':'Show ')+detailsLabel}><ActionIcon size={44} variant="subtle" aria-expanded={open} aria-controls={detailsId} aria-label={(open?'Hide ':'Show ')+detailsLabel} onClick={()=>setOpen(value=>!value)}>{open?<ChevronUp size={20} strokeWidth={1.8}/>:<ChevronDown size={20} strokeWidth={1.8}/>}</ActionIcon></Tooltip>:null;
 const menu=menuItems.length?<Menu position="bottom-end" withinPortal>
      <Menu.Target><ActionIcon size={44} variant="default" radius="md" aria-label={'Actions for '+name}><EllipsisVertical size={queue?20:18} strokeWidth={queue?1.8:2} aria-hidden="true"/></ActionIcon></Menu.Target>
      <Menu.Dropdown w={260} maw="calc(100vw - 32px)" mah="min(60dvh, 24rem)" style={{overflowY:'auto'}}>
       {menuItems.map((item,index)=>{if(!isValidElement<ActionProps>(item)||typeof item.props.onClick!=='function')return <Box key={index}>{item}</Box>;const props=item.props,label=props['aria-label']||readable(props.children)||props.title||'Action';return <Menu.Item key={index} mih={44} leftSection={queue?<QueueActionGlyph label={label}/>:undefined} disabled={props.disabled} onClick={props.onClick} color={props.variant==='danger'?'red':undefined} aria-label={label} style={{whiteSpace:'normal',overflowWrap:'anywhere'}}>{label}</Menu.Item>;})}
      </Menu.Dropdown>
     </Menu>:null;

 if(queue)return <Box component="article" data-activity-queue-item data-compact={phone||undefined} className={styles.queueRow}>
  <Box className={styles.queueStatus} data-activity-status>{queueBadge}{timestamp&&<Text size="xs" c="dimmed" lineClamp={1} title={timestamp}>{timestamp}</Text>}</Box>
  <Stack gap={2} className={styles.queueMain}><Box className={styles.queueTitle} title={name}>{title}</Box>{summary&&<Text size="xs" c="dimmed" lineClamp={1} title={summary}>{summary}</Text>}{progress&&<Box className={styles.progress}>{progress}</Box>}{contextAction&&<Group mt="xs" gap="xs">{contextAction}</Group>}</Stack>
  {phone?<Stack data-activity-actions className={styles.queueActions} gap={4}>{clearAction}{menu}{detailsControl}</Stack>:<Group data-activity-actions className={styles.queueActions} gap={4} wrap="nowrap">{quick.map(action=>isValidElement<ActionProps>(action)?cloneElement(action,{children:<Group component="span" gap={6} wrap="nowrap"><QueueActionGlyph label={action.props['aria-label']||readable(action.props.children)}/>{action.props.children}</Group>}):action)}{clearAction}{menu}{detailsControl}</Group>}

  {expandable&&open&&<Stack id={detailsId} gap="xs" className={styles.details+' '+styles.queueDetails}>{meta&&<Group gap="xs">{meta}</Group>}{statusProps?.phaseLabel&&<Text size="sm">{statusProps.phaseLabel}</Text>}{details}</Stack>}
 </Box>;
 return <Paper component="article" withBorder radius="lg" p={phone?'xs':'md'} className={className} data-activity-card data-compact={phone||undefined}>
  <Stack gap={phone?4:'xs'}>
   {badge&&<Box data-activity-status style={{minWidth:0}}>{badge}</Box>}
   <Group gap={phone?'xs':'md'} align="flex-start" wrap="nowrap" style={{minWidth:0}}>
    {media&&<Box className={styles.media} data-status-colour={statusColour} w={phone?44:52} h={phone?44:52} style={{flexShrink:0,...iconStyle}}>{media}</Box>}
    <Stack gap={4} style={{flex:1,minWidth:0}}>
     <Box className={styles.title} mih={44}>{title}</Box>
     {meta&&(phone?<Text size="xs" c="dimmed" lineClamp={1} title={summary}>{summary}</Text>:<Group gap="xs" c="dimmed" fz="sm">{meta}</Group>)}
     {progress&&<Box className={styles.progress}>{progress}</Box>}
     {contextAction&&<Group gap="xs">{contextAction}</Group>}
    </Stack>
    <Group data-activity-actions gap="xs" wrap="wrap" align="flex-start" style={{maxWidth:phone?44:undefined}}>
     {quick}
     {clearAction}
     {menu}
     {detailsControl}
    </Group>
   </Group>
   <Collapse expanded={open}>{expandable&&open&&<Stack id={detailsId} gap="xs" pt="sm" className={styles.details}>{phone&&meta&&<Group gap="xs">{meta}</Group>}{details}</Stack>}</Collapse>
  </Stack>
 </Paper>;
}
