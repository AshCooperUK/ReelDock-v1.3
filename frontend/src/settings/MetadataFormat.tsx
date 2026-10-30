import { SelectField as NativeSelect } from '../components/SelectField';
import { Stack as SettingsStack } from '@mantine/core';

import { Group, Loading, Notice, Row, useAction, useSettings } from '../SettingsUI';

export default function MetadataFormat({csrf}:{csrf:string}) {
 const resource=useSettings<{metadata_type:string}>('/api/downloads'),action=useAction(csrf),d=resource.data;
 if(!d)return <Loading error={resource.error} retry={resource.reload}/>;
 return <><fieldset style={{ border: 0, margin: 0, padding: 0, minWidth: 0 }} disabled={action.busy}><SettingsStack gap="md"><Group title="Metadata format"><Row title="Metadata format" detail="Shared library metadata preference. Existing files are unchanged until refreshed."><NativeSelect aria-label="Metadata format" value={d.metadata_type} onChange={async e=>{const value=e.target.value;if(await action.run('/api/downloads',{action:'metadata',metadata_type:value}))resource.setData({...d,metadata_type:value});}}>{['emby','plex','kodi'].map(value=><option value={value} key={value}>{value==='emby'?'Emby / Jellyfin':value==='plex'?'Plex':'Kodi'}</option>)}</NativeSelect></Row></Group></SettingsStack></fieldset><Notice text={action.error} error/><Notice text={action.busy?'Saving…':action.notice}/></>;
}
