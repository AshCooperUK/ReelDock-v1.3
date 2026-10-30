import { Stack as SettingsStack, Group as SettingsActions } from '@mantine/core';
import { Button } from '../components/ui';
import { Group, Loading, Notice, Toggle, useAction, useSettings } from '../SettingsUI';

type DownloadData={options:Record<string,boolean|number|string>;quality:string;metadata_type:string;auto_download_folder?:string};
export default function DownloadControls({csrf}:{csrf:string}) {
 const resource=useSettings<DownloadData>('/api/downloads'), action=useAction(csrf);const d=resource.data;
 if(!d)return <Loading error={resource.error} retry={resource.reload}/>;
 async function change(key:string,value:boolean|number|string){
  if(!d)return;const previous=d.options[key];resource.setData({...d,options:{...d.options,[key]:value}});
  if(!await action.run('/api/downloads',{action:'options',options:{[key]:value}}))resource.setData(current=>current?{...current,options:{...current.options,[key]:previous}}:current);
 }
 return <><fieldset style={{ border: 0, margin: 0, padding: 0, minWidth: 0 }} disabled={action.busy}><SettingsStack gap="md"><Group title="Shared download controls"><Toggle title="Pause downloads" detail="Pauses active and queued YouTube, VOD, NZB and torrent downloads, plus Auto-Download intake. Resuming keeps individual job pauses." value={!!d.options.pause_downloads} onChange={v=>change('pause_downloads',v)}/></Group></SettingsStack></fieldset><SettingsActions gap="sm" wrap="wrap"><Button variant="secondary" disabled={action.busy} title="Cancel active, queued and pending downloads across every engine. Completed files are kept. Downloads remain stopped until you turn Pause downloads off." onClick={async()=>{const result=await action.run<{message:string;acknowledged:boolean}>('/api/downloads',{action:'cancel_all'});if(result){action.setNotice(result.message);resource.reload();window.dispatchEvent(new Event('reeldock-library-changed'));}}}>Cancel all downloads</Button></SettingsActions><Notice text={action.error} error/><Notice text={action.busy?'Saving…':action.notice}/></>;
}
