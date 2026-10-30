import { ActionIcon, Button, createTheme, Drawer, FileInput, Modal, PasswordInput, Select, Text, Textarea, TextInput } from '@mantine/core';
const order=['label','input','description','error'] as const;
const field={input:{minHeight:44},label:{marginBottom:6},description:{marginTop:6},error:{marginTop:6}};
const overlayClose={backgroundColor:'var(--mantine-color-default)',border:'1px solid var(--mantine-color-default-border)',borderRadius:'var(--mantine-radius-md)',color:'var(--mantine-color-text)'};
export const mantineTheme=createTheme({
 defaultRadius:'md',
 autoContrast:true,
 components:{
  Text:Text.extend({defaultProps:{size:'sm'}}),
  Button:Button.extend({defaultProps:{size:'sm',h:44},styles:{label:{fontWeight:500}}}),
  ActionIcon:ActionIcon.extend({defaultProps:{size:44,variant:'default'}}),
  Modal:Modal.extend({styles:{close:overlayClose}}),
  Drawer:Drawer.extend({styles:{close:overlayClose}}),
  ModalCloseButton:Modal.CloseButton.extend({defaultProps:{size:44,iconSize:20}}),
  DrawerCloseButton:Drawer.CloseButton.extend({defaultProps:{size:44,iconSize:20}}),
  TextInput:TextInput.extend({defaultProps:{size:'sm',inputWrapperOrder:[...order]},styles:field}),
  PasswordInput:PasswordInput.extend({defaultProps:{size:'sm',rightSectionWidth:44,inputWrapperOrder:[...order]},styles:{...field,visibilityToggle:{width:44,height:44,minWidth:44,minHeight:44}}}),
  FileInput:FileInput.extend({defaultProps:{size:'sm',inputWrapperOrder:[...order]},styles:field}),
  Select:Select.extend({defaultProps:{size:'sm',inputWrapperOrder:[...order]},styles:field}),
  Textarea:Textarea.extend({defaultProps:{size:'sm',inputWrapperOrder:[...order]},styles:{label:field.label,description:field.description,error:field.error}}),
 },
});
