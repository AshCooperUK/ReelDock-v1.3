import * as React from 'react';
import { Accordion, ActionIcon, Box, Button as MantineButton, Group, Image, Modal, Stack, Switch as MantineSwitch, Text, Title } from '@mantine/core';
import { useMediaQuery } from '@mantine/hooks';
import { clsx, type ClassValue } from 'clsx';
import { registerNotificationHost } from '../notifications-state';
import ScrollToTop from '../ScrollToTop';

export function cn(...inputs:ClassValue[]){return clsx(inputs);}
export type ButtonProps=React.ButtonHTMLAttributes<HTMLButtonElement>&{variant?:'default'|'secondary'|'ghost'|'danger'|null;iconOnly?:boolean};
export const Button=React.forwardRef<HTMLButtonElement,ButtonProps>(function Button({variant='default',type='button',iconOnly=false,className,children,...props},ref){
 const appearance=variant==='secondary'?'default':variant==='ghost'?'subtle':'filled';
 const color=variant==='danger'?'red':undefined;
 const square=iconOnly||className?.split(/\s+/).some(n=>['icon-button','channel-download-icon','channel-favourite-icon'].includes(n));
 return square?<ActionIcon {...props} ref={ref} type={type} variant={appearance} color={color} className={className}>{children}</ActionIcon>:<MantineButton {...props} ref={ref} type={type} variant={appearance} color={color} className={className}>{children}</MantineButton>;
});
type SwitchProps=Omit<React.InputHTMLAttributes<HTMLInputElement>,'onChange'|'size'>&{label:string;showLabel?:boolean;onCheckedChange?:(checked:boolean)=>void};
export function Switch({label,showLabel=false,onCheckedChange,...props}:SwitchProps){return <MantineSwitch {...props} label={showLabel?label:undefined} aria-label={label} size="md" styles={{body:{minHeight:44,alignItems:'center'}}} onChange={event=>onCheckedChange?.(event.currentTarget.checked)}/>;}

type DialogState={open:boolean;change:(value:boolean)=>void};
const DialogContext=React.createContext<DialogState|null>(null);
const ContentContext=React.createContext(false);
let pageOpener:HTMLElement|null=null;
function rememberOpener(){const a=document.activeElement;if(a instanceof HTMLElement&&a!==document.body&&!a.closest('[role="dialog"]'))pageOpener=a;}
function restoreOpener(){if(document.querySelector('[role="dialog"]'))return;const a=pageOpener;pageOpener=null;if(a?.isConnected)a.focus();}
export function Dialog({open,defaultOpen=false,onOpenChange,children}:{open?:boolean;defaultOpen?:boolean;onOpenChange?:(value:boolean)=>void;children?:React.ReactNode;modal?:boolean}){
 const[local,setLocal]=React.useState(defaultOpen),shown=open??local,previous=React.useRef(false);
 if(shown&&!previous.current)rememberOpener();previous.current=shown;
 const change=React.useCallback((value:boolean)=>{if(value)rememberOpener();if(open===undefined)setLocal(value);onOpenChange?.(value);},[open,onOpenChange]);
 const current=React.useRef({shown,change});current.current={shown,change};
 // Explicit Settings navigation owns focus, including delayed modal exits.
 React.useEffect(()=>{const close=()=>{pageOpener=null;if(current.current.shown)current.current.change(false);};window.addEventListener('reeldock-notification-settings',close);return()=>window.removeEventListener('reeldock-notification-settings',close);},[]);
 return <DialogContext.Provider value={{open:shown,change}}>{children}</DialogContext.Provider>;
}
export function DialogTrigger({asChild=false,children,...props}:React.ButtonHTMLAttributes<HTMLButtonElement>&{asChild?:boolean}){
 const context=React.useContext(DialogContext);const click=(event:React.MouseEvent<HTMLButtonElement>)=>{props.onClick?.(event);if(!event.defaultPrevented)context?.change(true);};
 if(asChild&&React.isValidElement<React.ButtonHTMLAttributes<HTMLButtonElement>>(children))return React.cloneElement(children,{...props,onClick:event=>{children.props.onClick?.(event);if(!event.defaultPrevented)click(event);},'aria-haspopup':'dialog','aria-expanded':context?.open});
 return <Button {...props} aria-haspopup="dialog" aria-expanded={context?.open} onClick={click}>{children}</Button>;
}
export function DialogTitle({children,...props}:React.HTMLAttributes<HTMLHeadingElement>){const inside=React.useContext(ContentContext);return inside?<Modal.Title {...props} fw={600} fz="xl" pr={props.style?.paddingRight??64}>{children}</Modal.Title>:<Title {...props} order={2}>{children}</Title>;}
export function DialogDescription({children,...props}:React.HTMLAttributes<HTMLParagraphElement>){const inside=React.useContext(ContentContext);return <Text {...props} c="dimmed" size="sm" pr={inside?64:undefined}>{children}</Text>;}
export function DialogContent({children,className,scrollBottom=false,scrollRef,stableHeight=false}:{children:React.ReactNode;className?:string;scrollBottom?:boolean;scrollRef?:React.Ref<HTMLDivElement>;stableHeight?:boolean}){
 const dialog=React.useContext(DialogContext),phone=useMediaQuery('(max-width:47.99em)');
 const ref=React.useRef<HTMLDivElement>(null),unregister=React.useRef<(()=>void)|undefined>(undefined);
 const attach=React.useCallback((node:HTMLDivElement|null)=>{unregister.current?.();ref.current=node;unregister.current=node?registerNotificationHost(node):undefined;},[]);
 React.useEffect(()=>{if(dialog?.open)window.dispatchEvent(new Event('reeldock-overlay-open'));},[dialog?.open]);
 React.useEffect(()=>()=>{unregister.current?.();requestAnimationFrame(restoreOpener);},[]);
 if(!dialog)throw new Error('DialogContent must be inside Dialog');
 const small=/directory-dialog|help-dialog|channel-confirm-dialog|reset-reeldock-dialog/.test(className||'');
 return <Modal.Root opened={dialog.open} onClose={()=>dialog.change(false)} fullScreen={phone} size={small?'md':'64rem'} centered xOffset={16} yOffset={16} padding={0} closeOnEscape={false} returnFocus={false} onExitTransitionEnd={restoreOpener} styles={{inner:{bottom:'var(--reeldock-cookie-bar-height, 0px)'}}}>
  <Modal.Overlay backgroundOpacity={.65}/>
  <Modal.Content ref={attach} classNames={{content:className}} p={0} styles={{content:{position:'relative',display:'flex',flexDirection:'column',minWidth:0,maxHeight:phone?'calc(100dvh - var(--reeldock-cookie-bar-height, 0px))':'calc(100dvh - 32px - var(--reeldock-cookie-bar-height, 0px))',height:phone?'calc(100dvh - var(--reeldock-cookie-bar-height, 0px))':stableHeight?'min(44rem, calc(100dvh - 32px - var(--reeldock-cookie-bar-height, 0px)))':undefined,overflow:'hidden'}}}
   onKeyDownCapture={event=>{const target=event.target as HTMLElement;if(event.key==='Escape'&&!event.defaultPrevented&&target.getAttribute('role')==='combobox'&&target.getAttribute('aria-expanded')==='false'&&!target.closest('[data-notification-viewport],[role="alertdialog"]')){event.preventDefault();event.stopPropagation();dialog.change(false);}}}
   onKeyDown={event=>{if(event.key!=='Escape'||event.defaultPrevented||(event.target as HTMLElement).closest('[data-notification-viewport],[role="alertdialog"]'))return;event.preventDefault();dialog.change(false);}}>
   <ContentContext.Provider value><Box ref={scrollRef} data-ui-dialog-scroll="" data-dialog-scroll="" p="md" style={{minWidth:0,minHeight:0,overflowY:'auto',flex:1,...(stableHeight?{display:'flex',flexDirection:'column' as const}: {})}}>{children}</Box><Modal.CloseButton aria-label="Close window" size={44} pos="absolute" top={12} right={24} variant="default" style={{zIndex:5}}/><ScrollToTop root={ref} scrollBottom={scrollBottom}/></ContentContext.Provider>
  </Modal.Content>
 </Modal.Root>;
}
export function PopupShell({banner,title,subtitle,actions,media,children,footer,className,bodyClassName,bodyRef}:{banner?:string;title:React.ReactNode;subtitle?:React.ReactNode;actions?:React.ReactNode;media?:React.ReactNode;children:React.ReactNode;footer?:React.ReactNode;className?:string;bodyClassName?:string;bodyRef?:React.Ref<HTMLDivElement>}){
 return <Stack gap="lg" style={{minWidth:0}}>{banner&&<Image src={banner} alt="" h={96} radius="md" fit="cover" fallbackSrc={undefined}/>}<Group component="header" className={className} gap="md" wrap="nowrap" align="flex-start">{media&&<Box w={64} style={{flexShrink:0,overflow:'hidden',borderRadius:'var(--mantine-radius-md)'}}>{media}</Box>}<Stack gap={6} style={{flex:1,minWidth:0}}><DialogTitle>{title}</DialogTitle>{subtitle&&<DialogDescription>{subtitle}</DialogDescription>}{actions&&<Group gap="xs" wrap="wrap">{actions}</Group>}</Stack></Group><Box ref={bodyRef} className={bodyClassName} style={{minWidth:0}}>{children}</Box>{footer&&<Group component="footer" gap="xs" wrap="wrap" pt="sm" style={{borderTop:'1px solid var(--mantine-color-default-border)'}}>{footer}</Group>}</Stack>;
}
export function PanelButton({title,subject,children,disabled=false,icon}:{title:string;subject?:string;children:React.ReactNode;disabled?:boolean;dialogClassName?:string;icon?:React.ReactNode}){
 return <Accordion variant="contained" radius="md" style={{width:'100%',minWidth:0}}><Accordion.Item value="details"><Accordion.Control disabled={disabled} icon={icon}>{title}</Accordion.Control><Accordion.Panel>{subject&&<Text fw={600} mb="sm">{subject}</Text>}<Box component="fieldset" disabled={disabled} m={0} p={0} style={{border:0,minWidth:0}}>{children}</Box></Accordion.Panel></Accordion.Item></Accordion>;
}
