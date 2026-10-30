import type { DiscoverItem } from '../Discover';
import type { MediaCard } from './types';

export type LocalParentTarget = { type: 'discover'; item: DiscoverItem } | { type: 'channel'; id: string; name: string; avatar?: string } | null;
export type LocalParentHandlers = {
  request: (id: string, signal: AbortSignal) => Promise<{ target: LocalParentTarget }>;
  discover: (card: MediaCard) => void;
  group: (card: MediaCard) => void;
};

export const parentCandidate = (item: MediaCard) => item.action.type === 'local-group' && ['show', 'shows'].includes(item.kind);

/** Resolves a Local Library parent card on click; only the latest click may act. */
export function createParentOpener(handlers: LocalParentHandlers) {
  let generation = 0;
  let controller: AbortController | null = null;
  const cancel = () => { generation++; controller?.abort(); controller = null; };
  function open(item: MediaCard) {
    cancel();
    const action = item.action;
    if (action.type !== 'local-group' || !parentCandidate(item)) { handlers.group(item); return; }
    const version = generation;
    const own = controller = new AbortController();
    handlers.request(action.id, own.signal).then(({ target }) => {
      if (version !== generation) return;
      if (target?.type === 'discover' && target.item?.kind === 'show' && typeof target.item.id === 'string') {
        handlers.discover({ ...item, action: { type: 'discover', item: target.item } });
      } else handlers.group(item);
    }).catch(() => { if (version === generation) handlers.group(item); });
  }
  return { open, cancel };
}
