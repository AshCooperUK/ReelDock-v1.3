import { Alert, Group, Progress, Stack, Text } from '@mantine/core';
import { HardDrive } from 'lucide-react';
import { Panel } from '../components/layout';
import { TorrentProgress } from '../TorrentDownloads';
import { NzbProgress } from '../NzbDownloads';
import { YoutubeDownloadProgress, VodDownloadProgress } from '../ManualDownloadTiles';
import NetworkActivity from '../NetworkActivity';
import AtAGlance from './AtAGlance';
import type { ReadyDashboard } from './types';
function size(bytes:number){return `${(bytes/(bytes>=1e12?1e12:1e9)).toFixed(1)} ${bytes>=1e12?'TB':'GB'}`;}
export default function SystemStatus({ready}:{ready:ReadyDashboard}){
 const overview=ready.overview;
 const disks=overview.storage_disks??(overview.media_disk?[{...overview.media_disk,id:'media',label:'Media',paths:[]}]:[]);
 return <Stack gap="md"><TorrentProgress csrf={ready.csrf}/><NzbProgress csrf={ready.csrf}/><YoutubeDownloadProgress csrf={ready.csrf}/><VodDownloadProgress csrf={ready.csrf}/><Panel title={<Group gap="xs"><HardDrive size={17}/>Storage</Group>}>
  {!!overview.storage_configuration_warnings?.length&&<Alert color="yellow" title="Storage configuration"><Stack gap="xs">{overview.storage_configuration_warnings.map(warning=><Text size="sm" key={warning} style={{overflowWrap:'anywhere'}}>{warning}</Text>)}</Stack></Alert>}
  {disks.length?<Stack gap="lg">{disks.map(disk=><Stack gap="xs" key={disk.id}>
   <Text fw={600}>{disk.label}</Text>
   {disk.paths.length>0&&<Text size="xs" c="dimmed" style={{overflowWrap:'anywhere'}}>{disk.paths.join(' · ')}</Text>}
   {disk.total!==null&&disk.used!==null&&disk.free!==null?<><Group justify="space-between"><Text fw={600}>{size(disk.free)} free</Text><Text c="dimmed">{size(disk.total)} total</Text></Group><Progress value={disk.total?Math.min(100,Math.max(0,disk.used/disk.total*100)):0} aria-label={disk.label+(disk.paths[0]?' ('+disk.paths[0]+')':'')+' storage used'}/><Text size="xs" c="dimmed">{size(disk.used)} used</Text></>:<Text size="sm" c="dimmed">Storage information is unavailable for this location.</Text>}
  </Stack>)}</Stack>:<Text c="dimmed">Storage information is unavailable.</Text>}
  {overview.storage_disks_incomplete&&<Text size="xs" c="dimmed" mt="sm">Some mounted storage locations could not be listed.</Text>}
 </Panel><NetworkActivity live/><AtAGlance summary={ready.glance}/></Stack>;
}
