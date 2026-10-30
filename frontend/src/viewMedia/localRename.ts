import type { MediaCard } from './types';

export type LocalChange = { from: string; to: string };
export type LibraryChange = { localChanges?: LocalChange[] };

/** Retarget only the files explicitly identified by a successful server rename. */
export function retargetLocalCard(card: MediaCard | null, changes: LocalChange[]): MediaCard | null {
  if (!card) return card;
  const valid = changes.filter(row => /^[a-f0-9]{32}$/.test(row?.from) && /^[a-f0-9]{32}$/.test(row?.to));
  const children = card.children?.map(child => retargetLocalCard(child, valid)!);
  const changedChildren = children?.some((child, index) => child !== card.children![index]);
  const id = card.action.type === 'local' ? card.action.id : '';
  const change = valid.find(row => row.from === id);
  const art = (value?: string) => {
    const mapping = valid.find(row => value?.startsWith('/api/view-media/local/' + row.from + '/'));
    return mapping ? value!.replace('/api/view-media/local/' + mapping.from + '/', '/api/view-media/local/' + mapping.to + '/') : value;
  };
  const image = art(card.image), backdrop = art(card.backdrop);
  if (!change && !changedChildren && image === card.image && backdrop === card.backdrop) return card;
  return { ...card, ...(change ? { id: 'local:' + change.to, action: { type: 'local' as const, id: change.to } } : {}), image, backdrop, ...(changedChildren ? { children } : {}) };
}
