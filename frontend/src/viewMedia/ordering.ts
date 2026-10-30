import type { MediaCard } from './types';

export const searchPageSize=24;
export function searchPage(items:MediaCard[],shown=searchPageSize) {
  return {items:items.slice(0,shown),hasMore:shown<items.length};
}

/** Partition the loaded, already-sorted search results without changing source actions. */
export function searchSections(items:MediaCard[]) {
  const sections=[{label:'Movies',items:[] as MediaCard[]},{label:'TV Shows',items:[] as MediaCard[]},{label:'YouTube',items:[] as MediaCard[]}];
  for(const item of items){
    const index=['youtube','video','channel'].includes(item.kind)||item.action.type==='youtube'?2:
      ['show','shows','episode','series','tv'].includes(item.kind)?1:0;
    sections[index].items.push(item);
  }
  return sections.filter(section=>section.items.length);
}
export const sortGroups = [
  {label:'NZB Order', values:['source'], directions:['NZB Order']},
  {label:'Date added', values:['new','oldest'], directions:['Newest first','Oldest first']},
  {label:'Title', values:['title','title_desc'], directions:['A–Z','Z–A']},
  {label:'Year', values:['year','year_asc'], directions:['Newest first','Oldest first']},
  {label:'Release date', values:['release','release_asc'], directions:['Newest first','Oldest first']},
  {label:'Last episode released', values:['episode_released','episode_released_asc'], directions:['Newest first','Oldest first']},
  {label:'Last episode added', values:['episode_added','episode_added_asc'], directions:['Newest first','Oldest first']},
  {label:'Rating', values:['rating','rating_asc'], directions:['Highest first','Lowest first']},
];
export function orderCards(items:MediaCard[],sort:string) {
  if(sort==='source')return items;
  const field=['new','oldest'].includes(sort)?'added':sort.startsWith('title')?'title':sort.startsWith('year')?'year':sort.startsWith('release')?'releaseDate':sort.startsWith('episode_released')?'lastEpisodeReleased':'rating';
  const reverse=['new','title_desc','year','rating','release','episode_released'].includes(sort);
  const value=(item:MediaCard)=>{const raw=item[field]||(field==='releaseDate'?item.year:null);return field==='title'?String(raw||'').toLocaleLowerCase():['releaseDate','lastEpisodeReleased'].includes(field)?raw?String(raw):null:raw!==''&&raw!=null&&Number.isFinite(Number(raw))?Number(raw):null;};
  return [...items].sort((a,b)=>{const x=value(a),y=value(b);if(x==null)return y==null?0:1;if(y==null)return -1;const result=typeof x==='string'&&typeof y==='string'?x.localeCompare(y):Number(x)-Number(y);return (reverse?-result:result)||a.id.localeCompare(b.id);});
}
