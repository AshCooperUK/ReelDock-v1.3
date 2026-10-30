import { Group, Progress, Stack, Text } from '@mantine/core';
import { HardDrive } from 'lucide-react';
import { Panel } from '../components/layout';
import { TorrentProgress } from '../TorrentDownloads';
import { NzbProgress } from '../NzbDownloads';
import { YoutubeDownloadProgress, VodDownloadProgress } from '../ManualDownloadTiles';
import NetworkActivity from '../NetworkActivity';
import AtAGlance from './AtAGlance';
import type { ReadyDashboard } from './types';
function size(bytes:number){return `${(bytes/(bytes>=1e12?1e12:1e9)).toFixed(1)} ${bytes>=1e12?'TB':'GB'}`;}
export default function SystemStatus({ready}:{ready:ReadyDashboard}){const disk=ready.overview.media_disk;return <Stack gap="md"><TorrentProgress csrf={ready.csrf}/><NzbProgress csrf={ready.csrf}/><YoutubeDownloadProgress csrf={ready.csrf}/><VodDownloadProgress csrf={ready.csrf}/><Panel title={<Group gap="xs"><HardDrive size={17}/>Storage</Group>}>{disk?<Stack gap="sm"><Group justify="space-between"><Text fw={600}>{size(disk.free)} free</Text><Text c="dimmed">{size(disk.total)} total</Text></Group><Progress value={disk.total?disk.used/disk.total*100:0} aria-label="Storage used"/><Text size="xs" c="dimmed">{size(disk.used)} used</Text></Stack>:<Text c="dimmed">Storage information is unavailable.</Text>}</Panel><NetworkActivity live/><AtAGlance summary={ready.glance}/></Stack>;}
