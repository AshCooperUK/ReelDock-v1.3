import { Alert, Box, Group, Stack, Text, Title } from '@mantine/core';
import { useState } from 'react';
import { Button } from './components/ui';
import { TextInput } from './TextField';
import { Field, Group as SettingsGroup, Notice } from './SettingsUI';
import { api } from './api';

export default function ResetReelDock({csrf}:{csrf:string}) {
  const [open,setOpen]=useState(false),[locked,setLocked]=useState(false);
  return <SettingsGroup title="Reset installation"><Stack gap="md"><Text size="sm" c="dimmed">Start again by removing this installation’s account, settings and media. Review the confirmation before anything changes.</Text>{!open?<Group><Button variant="danger" onClick={()=>setOpen(true)}>Reset ReelDock</Button></Group>:<ResetConfirmation csrf={csrf} close={()=>{if(!locked)setOpen(false);}} lock={setLocked}/>}</Stack></SettingsGroup>;
}
function ResetConfirmation({csrf,close,lock}:{csrf:string;close:()=>void;lock:(busy:boolean)=>void}) {
  const [password,setPassword]=useState(''),[confirmOpen,setConfirmOpen]=useState(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[progress,setProgress]=useState('');
  async function reset() {
    if(busy)return;setBusy(true);lock(true);setError('');setProgress('Stopping active work…');
    try {
      while(true) {
        const result=await api<{state?:string;url?:string}>('/api/account/reset',undefined,{password,confirmed:true},csrf);
        if(result.url) {
          setProgress('Reset complete. Opening setup…');
          try {for(const storage of [localStorage,sessionStorage])for(const key of Object.keys(storage))if(key.startsWith('reeldock-'))storage.removeItem(key);} catch { /* Reset remains complete if browser storage is unavailable. */ }
          location.assign(result.url);return;
        }
        if(result.state!=='stopping')throw new Error('The reset response was incomplete. Please try again.');
        await new Promise(resolve=>setTimeout(resolve,1000));
      }
    } catch(e) {setError(e instanceof Error?e.message:'Reset could not finish. Please try again.');setProgress('');setBusy(false);lock(false);}
  }
  return <Alert color="red" variant="light" role={confirmOpen?'alertdialog':undefined} aria-label={confirmOpen?'Delete everything and reset?':undefined} onKeyDown={event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();if(!busy){if(confirmOpen)setConfirmOpen(false);else close();}}}}>
    <Stack gap="md">{confirmOpen?<><Title order={3} size="h5">Delete everything and reset?</Title><Text size="sm">This permanently deletes ReelDock’s account, connections, credentials, library, history and files in its media folders. Docker mounts and environment settings stay in place.</Text><Text size="sm">ReelDock will stop active downloads and scans, then return to setup. This cannot be undone.</Text><Notice text={error} error/>{progress&&<Text role="status">{progress}</Text>}<Group><Button variant="secondary" disabled={busy} onClick={()=>setConfirmOpen(false)}>Keep this installation</Button><Button variant="danger" disabled={busy} onClick={()=>void reset()}>{busy?'Resetting…':'Reset ReelDock'}</Button></Group></>:<Box component="form" onSubmit={event=>{event.preventDefault();setError('');setConfirmOpen(true);}}><Stack gap="md"><Text size="sm">Enter your current password to continue to the final confirmation.</Text><Field label="Current password"><TextInput type="password" autoComplete="current-password" required maxLength={256} value={password} disabled={busy} onChange={event=>setPassword(event.target.value)}/></Field><Group><Button type="button" variant="secondary" disabled={busy} onClick={close}>Cancel</Button><Button type="submit" variant="danger" disabled={busy||!password}>Continue</Button></Group></Stack></Box>}</Stack>
  </Alert>;
}
