import {acquisitionItem} from '../discoverPayload';
import type {CollectionItem} from './CollectionShelf';

export type CollectionData={movies:CollectionItem[];shows:CollectionItem[];videos:CollectionItem[];channels:CollectionItem[]};
export function favouriteKey(item:CollectionItem){return item.collectionKey||item.watchlistKey||item.id;}
export function withFavourites(data:CollectionData,favourites:CollectionData):CollectionData{
 const result={...data};
 for(const group of ['movies','shows','videos','channels'] as const){
  const selected=new Set(favourites[group].map(favouriteKey));
  result[group]=data[group].map(item=>({...item,favourite:selected.has(favouriteKey(item))}));
 }
 return result;
}
export function favouriteWrite(item:CollectionItem){
 const favourite=!item.favourite;
 if(item.episodeKey)return {url:'/api/favourites/episode',body:{key:item.episodeKey,favourite},event:'reeldock-episode-favourite-changed'};
 if(item.kind==='channel')return {url:'/api/channel-favourite/'+encodeURIComponent(item.channelId||item.id),body:{favourite},event:'reeldock-channel-changed'};
 if(item.kind==='video')return {url:'/api/favourites/video/'+encodeURIComponent(item.videoId!),body:{favourite},event:'reeldock-video-favourite-changed'};
 return {url:'/api/discovery-engine/'+(item.kind==='show'?'show':'movie')+'-options',body:{item:acquisitionItem(item),settings:{favourite}},event:'reeldock-'+(item.kind==='show'?'show':'movie')+'-options-changed'};
}
