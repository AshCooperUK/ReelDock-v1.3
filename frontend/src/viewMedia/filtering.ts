import type { MediaCard } from './types';

export function mediaKind(card:MediaCard) {
  return ['youtube','video','channel'].includes(card.kind)||card.action.type==='youtube'?'youtube':
    ['show','shows','episode','series','tv'].includes(card.kind)?'shows':'movies';
}

/** Source filters narrow saved results without changing their action or identity. */
export function filterMediaCards(cards:MediaCard[],source='all',kind='all') {
  const name=(value:string)=>value.toLowerCase().replace(/[^a-z]/g,'');
  const origin=({discover:'nzb',vod:'vod',local:'locallibrary',subscriptions:'youtube'} as Record<string,string>)[source]||name(source);
  return cards.filter(card=>{
    if(kind!=='all'&&mediaKind(card)!==kind)return false;
    if(source==='all')return true;
    const fallback=card.action.type==='discover'?'NZB':card.action.type==='vod'?'VOD':card.action.type==='youtube'?'YouTube':'Local Library';
    return [...(card.origins||[]),card.origin||fallback].some(value=>name(value)===origin);
  });
}
