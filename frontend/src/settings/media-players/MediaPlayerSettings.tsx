import { SegmentedControl, Stack } from '@mantine/core';
import { useMediaQuery } from '@mantine/hooks';
import { useState } from 'react';
import { SelectField } from '../../components/SelectField';
import EmbySettings from './EmbySettings';
import ServerSettings from './ServerSettings';

const players=['Emby','Jellyfin','Plex','Kodi'] as const;
export default function MediaPlayerSettings({csrf}:{csrf:string}) {
  const [player,setPlayer]=useState<typeof players[number]>('Emby');
  const phone=useMediaQuery('(max-width: 47.99em)');
  return <Stack gap="lg">{phone?<SelectField aria-label="Media server type" value={player} data={[...players]} onChange={event=>setPlayer(event.currentTarget.value as typeof player)}/>:<SegmentedControl aria-label="Media server type" value={player} data={[...players]} onChange={value=>setPlayer(value as typeof player)} fullWidth size="md"/>}
    <Stack key={player} gap="lg">{player==='Emby'?<EmbySettings csrf={csrf}/>:<ServerSettings player={player.toLowerCase() as 'jellyfin'|'plex'|'kodi'} csrf={csrf}/>}</Stack>
  </Stack>;
}
