import type {CollectionItem} from './CollectionShelf';
export function collectionIdentity(item:CollectionItem):string{
 if(item.kind==='video')return 'video:'+(item.videoId||item.id.replace(/^(youtube:)?video:/,''));
 if(item.kind==='channel')return 'channel:'+(item.channelId||item.id.replace(/^youtube:channel:/,''));
 // Unnumbered release titles sharing an umbrella series ID stay separate.
 const text=item.title.replace(/[._]+/g,' '),direct=item.kind==='show'&&!/\b(?:S[0-9]{1,4}(?:E[0-9]{1,4})?|season\s*\d+|\d{4}[ -]\d{2}[ -]\d{2})\b/i.test(text)&&text.match(/\b((?:19|20)\d{2})\s+(?=(?:2160|1080|720|480)[pi]\b|HDTV\b|WEB\b|DVDRIP\b|BLURAY\b)/i);
 if(direct)return item.id+':title:'+text.slice(0,direct.index).toLowerCase().replace(/[^a-z0-9]+/g,'')+':'+direct[1];
 if(item.kind==='movie'){const id=item.externalId||item.imdbId?.replace(/^tt/,'')||item.id.match(/^(?:imdb:)?movie:(\d+)$/)?.[1];if(id&&/^\d+$/.test(id))return 'movie:'+id.replace(/^0+(?=\d)/,'');}
 return item.id;
}
export function watchlistHint(item:CollectionItem,selected:boolean){
 if(selected)return 'Remove from Watchlist and turn off future automatic downloads.';
 if(item.kind==='channel')return 'Add to Watchlist. Future uploads will download automatically using your saved upload choices (Videos if none are saved). Existing pauses stay in effect.';
 if(item.kind==='video')return 'Add to Watchlist. This video will download automatically when available. Existing download holds stay in effect.';
 if(item.kind==='show'&&collectionIdentity(item).includes(':title:'))return 'Add to Watchlist. Automatic matching is unavailable for this unnumbered release.';
 return 'Add to Watchlist. Future releases will download automatically when available, using your saved quality settings. Existing download holds stay in effect.';
}
