import { Badge, Group as Actions, Paper, Progress, Stack, Text } from '@mantine/core';
import { Button } from './components/ui';
import ActivityGroup from './components/ActivityGroup';
import { Loading, Notice, useAction } from './SettingsUI';
import { optimisationActive, optimisationInProgress, optimisationQueued, useLibraryOptimise } from './libraryOptimiseResource';

export type OptimisationAction = 'cancel' | 'retry' | 'request_publish' | 'cancel_publish';

export type LibraryOptimiseJob = {
  id: string; kind: 'tv' | 'movies'; path: string; profile: 'hevc' | 'h264'; stereo?: boolean; limit1080?: boolean;
  state: string; error: string; source_bytes: number; output_bytes: number; output: string; created?: number; updated?: number;
  progress_percent?: number | null; duration_seconds?: number | null; processed_seconds?: number | null;
  fps?: number | null; speed?: number | null; elapsed_seconds?: number | null; elapsed_basis?: 'started' | 'created' | null;
  phase_elapsed_seconds?: number | null; eta_seconds?: number | null; progress_updated?: number | null; live_output_bytes?: number | null;
  destination_mode?: 'appdata' | 'adjacent'; destination_name?: string;
  publication_state?: 'waiting' | 'queued' | 'copying' | 'complete' | 'failed' | 'cancelled' | null;
  publication_path?: string; publication_error?: string; publication_bytes?: number | null;
  publication_total_bytes?: number | null; publication_percent?: number | null; can_publish?: boolean;
};
export type LibraryOptimiseStatus = {
  enabled: boolean; tools_available: boolean; output_root: string; output_error: string;
  libraries: { kind: 'tv' | 'movies'; available: boolean; path: string; message: string;
    output_available?: boolean; output_path?: string; output_message?: string }[]; jobs: LibraryOptimiseJob[];
};
const labels: Record<string, string> = { queued: 'Queued', preparing: 'Checking source', encoding: 'Encoding copy', validating: 'Full decode check', complete: 'Complete', skipped: 'Kept original', failed: 'Failed', cancelled: 'Cancelled' };
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
export const optimisationBytes = (value: number) => value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GB` : value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MB` : value >= 1024 ? `${(value / 1024).toFixed(1)} KB` : `${value} B`;
export function optimisationTime(value: number) {
  const seconds = Math.floor(value), hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
  return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}
export function canQueueOptimisation(data: LibraryOptimiseStatus, kind: LibraryOptimiseJob['kind']) {
  const library = data.libraries.find(library => library.kind === kind);
  return data.enabled && data.tools_available && !data.output_error && library?.available === true && library.output_available === true;
}
export function canRetryOptimisation(data: LibraryOptimiseStatus, job: LibraryOptimiseJob) {
  return job.destination_mode === 'adjacent' ? canQueueOptimisation(data, job.kind) : data.enabled && data.tools_available && !data.output_error;
}
export function optimisationSaveMessage(job: LibraryOptimiseJob) {
  switch (job.publication_state) {
    case 'waiting': return job.state === 'complete' ? 'Waiting to save beside the original.' : 'Save beside original requested. Waiting for a successfully checked copy.';
    case 'queued': return 'Save beside original queued.';
    case 'copying': return 'Saving beside the original…';
    case 'complete': return 'Saved beside the original.';
    case 'failed': return 'Save beside original failed.';
    case 'cancelled': return 'Save beside original cancelled.';
    default: return job.destination_mode === 'adjacent' ? 'Planned destination: beside the original, after all checks pass.' : 'This job keeps its original save location unless you request a save beside the original.';
  }
}
export function optimisationProgress(job: LibraryOptimiseJob, now = Date.now() / 1000) {
  const copying = job.publication_state === 'copying';
  const active = optimisationActive.has(job.state) || copying;
  const percent = copying ? finite(job.publication_percent) ? Math.floor(Math.min(99.9, job.publication_percent) * 10) / 10 : null
    : job.state === 'complete' ? 100 : ['encoding', 'validating'].includes(job.state) && finite(job.progress_percent)
    ? Math.floor(Math.min(99.9, job.progress_percent) * 10) / 10 : null;
  const elapsed = finite(job.elapsed_seconds) ? job.elapsed_seconds : finite(job.created) && job.created > 0 ? Math.max(0, now - job.created) : null;
  const basis = job.elapsed_basis === 'started' ? 'elapsed' : 'since added';
  const timing = !copying && (active || job.state === 'queued') && elapsed !== null ? `${optimisationTime(elapsed)} ${basis}` : '';
  const fresh = finite(job.progress_updated) && job.progress_updated > 0 && now - job.progress_updated >= 0 && now - job.progress_updated <= 10;
  const eta = !copying && active && fresh && finite(job.eta_seconds) ? `About ${optimisationTime(job.eta_seconds)} left in this phase` : '';
  const details = [timing, eta];
  if (copying && finite(job.publication_bytes)) details.push(`Saved ${optimisationBytes(job.publication_bytes)}${finite(job.publication_total_bytes) ? ` of ${optimisationBytes(job.publication_total_bytes)}` : ''}`);
  if (!copying && active && finite(job.live_output_bytes)) details.push(`Copy so far ${optimisationBytes(job.live_output_bytes)}`);
  if (!copying && active && fresh && finite(job.speed) && job.speed > 0) details.push(`${job.speed.toFixed(2)}× speed`);
  const pendingSave = job.publication_state && job.publication_state !== 'complete' || job.destination_mode === 'adjacent' && job.publication_state !== 'complete';
  const label = copying ? 'Saving beside original' : job.state === 'complete' && pendingSave ? 'Conversion complete' : labels[job.state] || job.state;
  return { active, copying, percent, label, details: details.filter(Boolean).join(' · ') };
}

function JobCard({ job, busy, canRetry, onAction }: {
  job: LibraryOptimiseJob; busy: boolean; canRetry: boolean; onAction: (action: OptimisationAction, id: string) => void;
}) {
  const progress = optimisationProgress(job);
  return <Paper withBorder radius="md" p="md"><Stack gap="sm">
    <Actions justify="space-between" align="flex-start" wrap="wrap"><Text fw={500} size="sm" style={{ overflowWrap: 'anywhere', minWidth: 0, flex: '1 1 12rem' }}>{job.path}</Text><Badge variant="light" color={job.state === 'failed' ? 'red' : job.state === 'complete' ? 'teal' : 'gray'}>{progress.label}</Badge></Actions>
    {(progress.active || job.state === 'complete') && <Stack gap={4}>
      <Actions justify="space-between" gap="xs"><Text size="xs" c="dimmed">{progress.copying ? 'Save beside original progress' : job.state === 'complete' ? progress.label === 'Conversion complete' ? 'Conversion checked; saving beside original is separate' : 'Copy checked and saved' : job.state === 'validating' ? 'Full video and audio decode, then final checks' : progress.percent === null ? 'Progress unavailable' : 'Current phase progress'}</Text>{progress.percent !== null && <Text size="xs" fw={600}>{progress.percent}%</Text>}</Actions>
      {progress.percent !== null ? <Progress value={progress.percent} color={job.state === 'complete' && !progress.copying ? 'teal' : undefined} aria-label={`${progress.label} progress for ${job.path}`} aria-valuetext={`${progress.percent}% of ${progress.label.toLowerCase()}`}/>
        : <Progress.Root role="progressbar" aria-label={`${progress.label} progress for ${job.path}`} aria-valuetext={`${progress.label}. Progress unavailable.`}><Progress.Section value={100} animated withAria={false}/></Progress.Root>}
    </Stack>}
    {progress.details && <Text size="sm" c="dimmed" style={{ overflowWrap: 'anywhere' }}>{progress.details}</Text>}
    <Text size="sm" c="dimmed">{job.kind === 'tv' ? 'TV Shows' : 'Movies'} · {job.profile === 'hevc' ? 'HEVC' : 'H.264'} · Original {optimisationBytes(job.source_bytes)}{job.output_bytes > 0 ? ` · ${job.state === 'complete' ? 'Saved copy' : 'Candidate'} ${optimisationBytes(job.output_bytes)}` : ''}</Text>
    <Text size="sm" c="dimmed">{job.stereo ? 'AAC stereo audio' : 'Original audio'} · {job.limit1080 ? '1080p limit' : 'Original dimensions'}</Text>
    <Notice text={job.error} error={job.state === 'failed'} />
    <Text size="sm" fw={500}>{optimisationSaveMessage(job)}</Text>
    {job.destination_name && (job.destination_mode === 'adjacent' || job.publication_state || job.can_publish) && <Text size="sm" c="dimmed" style={{ overflowWrap: 'anywhere' }}>{job.publication_state === 'complete' ? 'Saved name' : job.publication_state || job.destination_mode === 'adjacent' ? 'Planned name' : 'Available name on request'}: {job.destination_name}</Text>}
    <Notice text={job.publication_error || ''} error/>
    {job.publication_state === 'complete' && job.publication_path ? <Text size="sm" c="dimmed" style={{ overflowWrap: 'anywhere' }}>Saved beside original: {job.publication_path}</Text>
      : job.output && <Text size="sm" c="dimmed" style={{ overflowWrap: 'anywhere' }}>Checked copy location: {job.output}</Text>}
    {(optimisationActive.has(job.state) || job.state === 'queued' || ['failed', 'cancelled'].includes(job.state) || job.can_publish || ['waiting', 'queued', 'copying'].includes(job.publication_state || '')) && <Actions wrap="wrap">
      {(optimisationActive.has(job.state) || job.state === 'queued') && <Button variant="secondary" disabled={busy} aria-label={`Cancel optimisation of ${job.path}`} onClick={() => onAction('cancel', job.id)}>Cancel</Button>}
      {['failed', 'cancelled'].includes(job.state) && <Button variant="secondary" disabled={busy || !canRetry} aria-label={`Retry optimisation of ${job.path}`} onClick={() => onAction('retry', job.id)}>Retry</Button>}
      {job.can_publish && <Button variant="secondary" disabled={busy} aria-label={`Save checked copy beside original for ${job.path}`} onClick={() => onAction('request_publish', job.id)}>{['failed', 'cancelled'].includes(job.publication_state || '') ? 'Retry save' : 'Save beside original'}</Button>}
      {['waiting', 'queued', 'copying'].includes(job.publication_state || '') && <Button variant="secondary" disabled={busy} aria-label={`Cancel save beside original for ${job.path}`} onClick={() => onAction('cancel_publish', job.id)}>Cancel save</Button>}
    </Actions>}
  </Stack></Paper>;
}

export function LibraryOptimiseJobs({ jobs, busy, canRetry, onAction }: {
  jobs: LibraryOptimiseJob[]; busy: boolean; canRetry: boolean | ((job: LibraryOptimiseJob) => boolean); onAction: (action: OptimisationAction, id: string) => void;
}) {
  const groups = [
    { title: 'In progress', rows: jobs.filter(optimisationInProgress) },
    { title: 'Queued', rows: jobs.filter(job => !optimisationInProgress(job) && optimisationQueued(job)) },
    { title: 'Recent outcomes', rows: jobs.filter(job => !optimisationInProgress(job) && !optimisationQueued(job)) },
  ];
  return <Stack gap="md">
    {!jobs.length && <Text size="sm" c="dimmed">No files queued. Libraries are never converted automatically.</Text>}
    {groups.filter(group => group.rows.length).map(group => <Stack key={group.title} gap="sm">
      <Text size="sm" fw={600}>{group.title} · {group.rows.length}</Text>
      {group.rows.map(job => <JobCard key={job.id} job={job} busy={busy} canRetry={typeof canRetry === 'function' ? canRetry(job) : canRetry} onAction={onAction}/>)}
    </Stack>)}
  </Stack>;
}

export function LibraryOptimiseActivity({ csrf }: { csrf: string }) {
  const resource = useLibraryOptimise(), action = useAction(csrf), data = resource.data;
  async function change(kind: OptimisationAction, id: string) {
    const next = await action.run<LibraryOptimiseStatus>('/api/library-optimise', { action: kind, id }, optimisationActionNotice(kind));
    if (next) resource.setData(next);
  }
  return <ActivityGroup id="library-optimisation" title="Library optimisation" count={data?.jobs.length || 0}
    actions={<Actions gap="xs" wrap="wrap">{data && <><Badge variant="light" color="gray">{data.jobs.filter(optimisationInProgress).length} active</Badge><Badge variant="light" color="gray">{data.jobs.filter(job => !optimisationInProgress(job) && optimisationQueued(job)).length} queued</Badge></>}<Button variant="secondary" disabled={action.busy} onClick={resource.reload}>Refresh jobs</Button></Actions>}>
    <Stack gap="md" p="md">
    <Text size="sm" c="dimmed">Smaller copies of library files. Originals are retained. Queue files in Settings.</Text>
    {!data ? <Loading error={resource.error} retry={resource.reload}/> : <>
      <Notice text={resource.error} error/><Notice text={action.error} error/><Notice text={action.busy ? 'Working…' : action.notice}/>
      <Notice text={data.output_error} error/>
      {data.libraries.filter(library => library.output_available === false && data.jobs.some(job => job.kind === library.kind && job.publication_state !== 'complete')).map(library => <Notice key={library.kind} text={library.output_message || `Saving beside originals is not configured for ${library.kind === 'tv' ? 'TV Shows' : 'Movies'}.`} error/>)}
      <LibraryOptimiseJobs jobs={data.jobs} busy={action.busy} canRetry={job => canRetryOptimisation(data, job)} onAction={(kind, id) => void change(kind, id)}/>
    </>}
    </Stack>
  </ActivityGroup>;
}

export function optimisationActionNotice(action: OptimisationAction) {
  return action === 'cancel' ? 'Cancellation requested.' : action === 'retry' ? 'Job queued.' : action === 'request_publish' ? 'Save beside original requested.' : 'Save cancellation requested.';
}
