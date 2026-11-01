import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';
import { useSettings } from './SettingsUI';
import { onVisible, pageHidden } from './visibility';
import type { LibraryOptimiseJob, LibraryOptimiseStatus } from './LibraryOptimiseJobs';

export const optimisationActive = new Set(['preparing', 'encoding', 'validating']);
export function optimisationInProgress(job: LibraryOptimiseJob) {
  return optimisationActive.has(job.state) || job.publication_state === 'copying';
}
export function optimisationQueued(job: LibraryOptimiseJob) {
  return job.state === 'queued' || job.state === 'complete' && ['waiting', 'queued'].includes(job.publication_state || '');
}
export function optimisationPollDelay(jobs: readonly LibraryOptimiseJob[]) {
  return jobs.some(optimisationInProgress) ? 2000 : jobs.some(optimisationQueued) ? 8000 : 0;
}

/** Read-only status polling, shared by Settings and the separate Activity panel. */
export function useLibraryOptimise() {
  const resource = useSettings<LibraryOptimiseStatus>('/api/library-optimise');
  const request = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const [pollError, setPollError] = useState('');
  const setData = useCallback((next: LibraryOptimiseStatus) => {
    generation.current++;
    request.current?.abort();
    resource.setData(next);
    setPollError('');
  }, [resource.setData]);
  const reload = () => {
    generation.current++;
    request.current?.abort();
    setPollError('');
    resource.reload();
  };
  useEffect(() => {
    if (!resource.data || typeof window === 'undefined') return;
    const delay = optimisationPollDelay(resource.data.jobs);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    let reading = false;
    let owned: AbortController | null = null;
    async function read() {
      if (stopped || reading || pageHidden()) return;
      reading = true;
      const controller = new AbortController(), owner = ++generation.current;
      owned = controller; request.current = controller;
      try {
        const next = await api<LibraryOptimiseStatus>('/api/library-optimise', controller.signal, undefined, undefined, { feedback: 'inline' });
        if (!stopped && !controller.signal.aborted && owner === generation.current) {
          resource.setData(next); setPollError('');
        }
      } catch (problem) {
        if (!stopped && !controller.signal.aborted && owner === generation.current) {
          setPollError(problem instanceof Error ? problem.message : 'Could not refresh library optimisation.');
          if (delay && !pageHidden()) timer = setTimeout(() => void read(), delay);
        }
      } finally {
        reading = false;
        if (request.current === controller) request.current = null;
      }
    }
    if (delay && !pageHidden()) timer = setTimeout(() => void read(), delay);
    const stopVisible = onVisible(() => { clearTimeout(timer); void read(); });
    return () => { stopped = true; clearTimeout(timer); owned?.abort(); stopVisible(); };
  }, [resource.data]);
  return { ...resource, error: resource.error || pollError, setData, reload };
}
