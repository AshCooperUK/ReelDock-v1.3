import { useState } from 'react';
import { Badge, Group as Actions, Paper, ScrollArea, Stack, Text } from '@mantine/core';
import { Button } from './components/ui';
import { SelectField } from './components/SelectField';
import { TextInput } from './TextField';
import { Field, Group, Loading, Notice, Toggle, useAction } from './SettingsUI';
import { canQueueOptimisation, canRetryOptimisation, LibraryOptimiseJobs, optimisationActionNotice, optimisationBytes as bytes, type LibraryOptimiseStatus } from './LibraryOptimiseJobs';
import { useLibraryOptimise } from './libraryOptimiseResource';

type Kind = 'tv' | 'movies';
type Profile = 'hevc' | 'h264';
type FolderSize = { bytes: number | null; complete: boolean; reason: string };
type Listing = { kind: Kind; folder: string; files: { path: string; bytes: number }[]; folders: string[]; folderSizes?: Record<string, FolderSize>; truncated: boolean; unreadableEntries?: number };
type ScanEntry = FolderSize & { kind: 'folder' | 'file'; path: string };
type Sort = 'name' | 'size';
const name = (path: string) => path.split('/').pop() || path;

export function optimiseListingEntries(listing: Listing, sort: Sort): ScanEntry[] {
  const entries: ScanEntry[] = [
    ...listing.folders.map(path => ({ kind: 'folder' as const, path, ...(listing.folderSizes?.[path] || { bytes: null, complete: false, reason: 'unknown' }) })),
    ...listing.files.map(file => ({ kind: 'file' as const, ...file, complete: true, reason: '' })),
  ];
  return entries.sort((first, second) => {
    if (sort === 'size') {
      const firstKnown = first.complete && first.bytes !== null;
      const secondKnown = second.complete && second.bytes !== null;
      if (firstKnown !== secondKnown) return firstKnown ? -1 : 1;
      if (firstKnown && secondKnown && first.bytes !== second.bytes) return (second.bytes || 0) - (first.bytes || 0);
    }
    return name(first.path).localeCompare(name(second.path), undefined, { sensitivity: 'base', numeric: true });
  });
}

export function optimiseEntrySize(entry: FolderSize): string {
  if (entry.complete && entry.bytes !== null) return bytes(entry.bytes);
  if (entry.bytes !== null && entry.bytes > 0) return `At least ${bytes(entry.bytes)} · partial`;
  return entry.reason === 'unreadable' ? 'Size unavailable · unreadable' : 'Size unavailable · partial';
}

export default function LibraryOptimiseSettings({ csrf }: { csrf: string }) {
  const resource = useLibraryOptimise();
  const action = useAction(csrf);
  const [kind, setKind] = useState<Kind>('movies');
  const [profile, setProfile] = useState<Profile>('hevc');
  const [folder, setFolder] = useState('');
  const [sort, setSort] = useState<Sort>('name');
  const [listing, setListing] = useState<Listing | null>(null);
  const [stereo, setStereo] = useState(false);
  const [limit1080, setLimit1080] = useState(false);
  const data = resource.data;
  if (!data) return <Loading error={resource.error} retry={resource.reload} />;
  const available = !!data.libraries.find(library => library.kind === kind)?.available;
  const canQueue = canQueueOptimisation(data, kind);
  const entries = listing?.kind === kind ? optimiseListingEntries(listing, sort) : [];
  async function change(body: object, notice: string) {
    const result = await action.run<LibraryOptimiseStatus>('/api/library-optimise', body, notice);
    if (result) resource.setData(result);
  }
  async function browse(nextFolder: string) {
    const result = await action.run<Listing>('/api/library-optimise', { action: 'scan', kind, folder: nextFolder }, 'Folder checked.');
    if (result) { setListing(result); setFolder(result.folder); }
  }
  return <>
    <Group title="Library optimisation">
      <Toggle title="Enable library optimisation" value={data.enabled} disabled={action.busy}
        detail="Queue individual files to create smaller copies. Originals are always retained. Disabling cancels queued and active jobs."
        onChange={enabled => void change({ action: 'settings', enabled }, enabled ? 'Library optimisation enabled.' : 'Library optimisation disabled.')} />
      <Text size="sm" c="dimmed">Each job keeps all supported audio tracks, subtitles, chapters and metadata. ReelDock checks the copy, decodes its video and audio, and keeps it only when it saves at least 5%. Encoding uses the CPU and can take several hours.</Text>
      <Text size="sm" c="dimmed">HDR, Dolby Vision, rotated videos and unsupported tracks are kept unchanged. Both profiles use lossy compression; review the resulting picture quality and playback on your devices.</Text>
      {!data.tools_available && <Notice text="FFmpeg and FFprobe are unavailable. Jobs cannot be queued until they are installed." error />}
      <Notice text={data.output_error} error />
      {data.libraries.map(library => <Paper key={library.kind} withBorder radius="md" p="md"><Stack gap="xs">
        <Actions justify="space-between"><Text fw={500}>{library.kind === 'tv' ? 'TV Shows' : 'Movies'}</Text><Badge variant="light" color={library.available ? 'teal' : 'gray'}>{library.available ? 'Available' : 'Not configured'}</Badge></Actions>
        <Text size="sm" c="dimmed" style={{ overflowWrap: 'anywhere' }}>{library.path || library.message}</Text>
        <Text size="sm" fw={500}>Save beside original: {library.output_available ? 'Ready' : 'Not configured'}</Text>
        {!library.output_available && <Notice text={library.output_message || 'Saving beside originals is not available for this library. Configure its output mapping before queuing a copy.'} error/>}
      </Stack></Paper>)}
      <Text size="sm" c="dimmed">New copies are saved beside their originals with a clean movie or episode title and the selected HEVC or H264 profile in the filename. Originals and existing files are never overwritten.</Text>
      <Text size="sm" c="dimmed">Existing jobs keep their recorded destination. Save beside original requests and their outcomes appear with each job.</Text>
    </Group>
    <Group title="Choose a file">
      <Field label="Library"><SelectField aria-label="Optimisation library" value={kind} disabled={action.busy} onChange={event => { setKind(event.target.value as Kind); setFolder(''); setListing(null); }}><option value="movies">Movies</option><option value="tv">TV Shows</option></SelectField></Field>
      <Field label="Video profile" help="HEVC makes smaller copies for compatible devices. H.264 supports a wider range of devices. Subtitles keep their original codecs; audio conversion is optional below."><SelectField aria-label="Optimisation video profile" value={profile} disabled={action.busy} onChange={event => setProfile(event.target.value as Profile)}><option value="hevc">HEVC · MKV · quality 24</option><option value="h264">H.264 · MKV · quality 22</option></SelectField></Field>
      <Toggle title="Convert audio to stereo" value={stereo} disabled={action.busy}
        detail="For newly queued copies, convert each audio track to two-channel AAC at a 160 kbps target. Mono also becomes stereo. Languages, track names and subtitles remain. Off keeps the original audio."
        onChange={setStereo} />
      <Toggle title="Limit video to 1080p" value={limit1080} disabled={action.busy}
        detail="For newly queued copies, fit the picture within 1920 × 1080 while keeping its shape. Smaller videos are not enlarged. Off keeps the original dimensions."
        onChange={setLimit1080} />
      <Text size="sm" c="dimmed">Each job saves these choices. Retry uses the job’s saved choices; originals remain unchanged.</Text>
      <form onSubmit={event => { event.preventDefault(); void browse(folder); }}><Stack gap="md">
        <Field label="Folder within library" help="Leave blank to list the library root. Folders are checked only when you ask."><TextInput aria-label="Optimisation folder" value={folder} disabled={action.busy || !available} onChange={event => setFolder(event.target.value)} /></Field>
        <Actions><Button type="submit" variant="secondary" disabled={action.busy || !available}>List files</Button></Actions>
      </Stack></form>
      <Field label="Sort files and folders"><SelectField aria-label="Optimisation file and folder sort" value={sort} onChange={event => setSort(event.target.value as Sort)}><option value="name">Name A-Z</option><option value="size">Size (largest first)</option></SelectField></Field>
      {listing?.kind === kind && <Stack gap="md">
        <Text size="sm" c="dimmed">{listing.folder || 'Library root'}</Text>
        <Text size="sm" c="dimmed">Folder totals include their files and subfolders. Linked targets are excluded.</Text>
        <Actions wrap="wrap">{listing.folder && <Button variant="secondary" disabled={action.busy} onClick={() => void browse(listing.folder.split('/').slice(0, -1).join('/'))}>Parent folder</Button>}</Actions>
        <ScrollArea.Autosize mah={420} type="auto"><Stack gap="sm">
          {entries.map(entry => <Paper key={entry.kind + ':' + entry.path} withBorder radius="md" p="md"><Stack gap="sm">
            <Actions justify="space-between" align="flex-start" wrap="wrap"><Text size="sm" fw={500} style={{ overflowWrap: 'anywhere', minWidth: 0, flex: '1 1 12rem' }}>{name(entry.path)}</Text><Text size="sm" c="dimmed">{optimiseEntrySize(entry)}</Text></Actions>
            <Actions>{entry.kind === 'folder'
              ? <Button variant="secondary" aria-label={'Open folder ' + name(entry.path)} disabled={action.busy} onClick={() => void browse(entry.path)}>Open folder</Button>
              : <Button disabled={action.busy || !canQueue} onClick={() => void change({ action: 'enqueue', kind, path: entry.path, profile, stereo, limit1080 }, 'Job list updated.')}>Queue copy</Button>}</Actions>
          </Stack></Paper>)}
        </Stack></ScrollArea.Autosize>
        {!listing.folders.length && !listing.files.length && <Text size="sm" c="dimmed">No supported video files or folders found.</Text>}
        {listing.truncated && <Notice text="This folder has more than 1,000 entries. Open a smaller folder to choose its files." />}
        {entries.some(entry => !entry.complete) && <Notice text="Some folder totals are incomplete. Open a folder to check its files. Incomplete totals appear last when sorting by size." />}
        {!!listing.unreadableEntries && <Notice text="Some entries could not be read. Check the library permissions before listing this folder again." />}
      </Stack>}
    </Group>
    <Notice text={resource.error} error /><Notice text={action.error} error /><Notice text={action.busy ? 'Working…' : action.notice} />
    <Group title="Optimisation jobs">
      <Actions><Button variant="secondary" disabled={action.busy} onClick={resource.reload}>Refresh jobs</Button></Actions>
      <LibraryOptimiseJobs jobs={data.jobs} busy={action.busy} canRetry={job => canRetryOptimisation(data, job)} onAction={(action, id) => void change({ action, id }, optimisationActionNotice(action))}/>
    </Group>
  </>;
}
