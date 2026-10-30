export type ChannelRefresh={state:'queued'|'running'|'fresh'|'retry'|'paused'|'idle';notice:string;refreshAfterMs:number;retryAt?:number};
export type ChannelBrowse<T>={cursor:string;state:'idle'|'queued'|'running'|'retry'|'paused'|'complete';nextCursor:string|null;hasMore:boolean;refreshAfterMs:number;notice:string;page?:{cursor:string;items:T[]}};
export const channelRefreshEvent='reeldock-channel-refresh';
export function mergeChannelUploads<T extends{id:string;metadata_pending?:boolean}>(current:T[],incoming:T[],firstPage=false):T[]{
 const known=new Map(current.map(item=>[item.id,item]));
 const fresh=incoming.map(item=>{const old=known.get(item.id);return old&&!old.metadata_pending&&item.metadata_pending?old:item;});
 const incomingIds=new Set(fresh.map(item=>item.id));
 return firstPage?[...fresh,...current.filter(item=>!incomingIds.has(item.id))]:[...current,...fresh.filter(item=>!known.has(item.id))];
}
export function channelUploadPage<T extends{id:string;metadata_pending?:boolean}>(current:T[],page:{items:T[];more:boolean;total?:number},previousOffset:number,previousMore:boolean,firstPage=false){
 const items=mergeChannelUploads(current,page.items,firstPage);
 // A growing head can repeat the page boundary. Count unique saved rows once.
 const offset=items.length;
 const knownTotal=typeof page.total==='number'&&Number.isSafeInteger(page.total)&&page.total>=0;
 const endedBeyondHead=firstPage&&!previousMore&&previousOffset>page.items.length;
 const more=page.items.length>0&&(firstPage||items.length>current.length)&&(knownTotal?offset<page.total!:page.more&&!endedBeyondHead);
 return{items,offset,more};
}
export function channelRefreshDelay(refresh:ChannelRefresh|undefined,empty:boolean,notice:string,more:boolean){
 if(refresh?.state==='paused')return 0;
 if(refresh&&refresh.refreshAfterMs>0)return Math.max(1000,Math.min(5000,refresh.refreshAfterMs));
 if(empty&&notice&&!refresh)return 2000;
 return more?0:5000;
}
