import { useEffect, useState } from 'react';
import { canonicalView, viewFromSearch, type AppView } from './appNavigation';

export const pageSearchEvent = 'reeldock-page-search';
export function pageSearchQuery(search:string,view:AppView) {
  return viewFromSearch(search)===canonicalView(view) ? new URLSearchParams(search).get('filter')||'' : '';
}
export function pageSearchUrl(href:string,view:AppView,text:string) {
  const url=new URL(href);url.searchParams.set('view',canonicalView(view));url.searchParams.delete('q');
  const query=text.trim();if(query)url.searchParams.set('filter',query);else url.searchParams.delete('filter');
  return url.pathname+url.search+url.hash;
}
export function setPageSearch(view:AppView,text:string) {
  history.replaceState(history.state,'',pageSearchUrl(location.href,view,text));
  window.dispatchEvent(new Event(pageSearchEvent));
}
/** Page filters share the shell search field and browser history; pages render filters only. */
export function usePageSearch(view:AppView) {
  const[query,setQuery]=useState(()=>pageSearchQuery(location.search,view));
  useEffect(()=>{const sync=()=>setQuery(pageSearchQuery(location.search,view));window.addEventListener(pageSearchEvent,sync);window.addEventListener('popstate',sync);sync();return()=>{window.removeEventListener(pageSearchEvent,sync);window.removeEventListener('popstate',sync);};},[view]);
  return query;
}
