import { ActionIcon, Textarea as MantineTextarea, TextInput as MantineTextInput } from '@mantine/core';
import { forwardRef, useImperativeHandle, useRef, useState, type InputHTMLAttributes, type ReactNode, type TextareaHTMLAttributes } from 'react';
import { X } from 'lucide-react';

function clearField(element:HTMLInputElement|HTMLTextAreaElement|null){
 if(!element||element.matches(':disabled')||element.readOnly)return;
 const prototype=element instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
 Object.getOwnPropertyDescriptor(prototype,'value')?.set?.call(element,'');element.dispatchEvent(new Event('input',{bubbles:true}));element.focus();
}
type FieldCopy={label?:ReactNode;description?:ReactNode;error?:ReactNode;inputWrapperOrder?:('label'|'input'|'description'|'error')[]};
type TextInputProps=InputHTMLAttributes<HTMLInputElement>&FieldCopy&{onClear?:()=>void;clearable?:boolean;leftSection?:ReactNode;rightSection?:ReactNode};
export const TextInput=forwardRef<HTMLInputElement,TextInputProps>(function TextInput({onClear,clearable=true,leftSection,rightSection,className,size:_size,...props},forwardedRef){
 const ref=useRef<HTMLInputElement>(null);useImperativeHandle(forwardedRef,()=>ref.current!);
 const[filled,setFilled]=useState(Boolean(props.defaultValue));const hasText=props.value!=null?String(props.value).length>0:filled;
 const clear=clearable&&hasText&&!props.readOnly&&!props.disabled?<ActionIcon variant="subtle" size={44} aria-label={'Clear '+(props['aria-label']||props.name||props.placeholder||'text')} onMouseDown={event=>event.preventDefault()} onClick={()=>{clearField(ref.current);setFilled(false);onClear?.();}}><X size={16} aria-hidden="true"/></ActionIcon>:undefined;
 return <MantineTextInput {...props} ref={ref} className={className} leftSection={leftSection} rightSection={rightSection??clear} rightSectionPointerEvents="auto" rightSectionWidth={rightSection||clear?44:undefined} onInput={event=>{setFilled(Boolean(event.currentTarget.value));props.onInput?.(event);}}/>;
});
export function TextArea({clearable=true,fit=false,className,...props}:TextareaHTMLAttributes<HTMLTextAreaElement>&FieldCopy&{clearable?:boolean;fit?:boolean}){
 const ref=useRef<HTMLTextAreaElement>(null),[filled,setFilled]=useState(Boolean(props.defaultValue));const hasText=props.value!=null?String(props.value).length>0:filled;
 const clear=clearable&&hasText&&!props.readOnly&&!props.disabled?<ActionIcon variant="subtle" size={44} aria-label={'Clear '+(props['aria-label']||props.name||props.placeholder||'text')} onMouseDown={event=>event.preventDefault()} onClick={()=>{clearField(ref.current);setFilled(false);}}><X size={16} aria-hidden="true"/></ActionIcon>:undefined;
 return <MantineTextarea {...props} ref={ref} className={className} autosize={fit} minRows={props.rows||3} maxRows={fit?12:undefined} rightSection={clear} rightSectionWidth={clear?44:undefined} rightSectionPointerEvents="auto" styles={{section:{alignItems:'flex-start'}}} onInput={event=>{setFilled(Boolean(event.currentTarget.value));props.onInput?.(event);}}/>;
}
