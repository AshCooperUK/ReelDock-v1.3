const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const out={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/collections/collectionFavourites.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:out,Set,encodeURIComponent,require:()=>({acquisitionItem:({id,kind,title,externalId})=>({id,kind,title,externalId})})});
const movie={id:'tmdb:movie:100',watchlistKey:'movie:77',externalId:'77',kind:'movie',title:'Movie',automatic:true};
const show={id:'show:88',watchlistKey:'show:88',kind:'show',title:'Show',downloadFuture:true};
const channel={id:'UC'+'a'.repeat(22),channelId:'UC'+'a'.repeat(22),kind:'channel',title:'Channel'};
const video={id:'youtube:video:abcdefghijk',videoId:'abcdefghijk',kind:'video',title:'Video'};
const data={movies:[movie],shows:[show,{...show,id:'show:89',watchlistKey:'show:89',title:'Show'}],channels:[channel],videos:[video]};
const favourites={movies:[{...movie,watchlistKey:undefined,collectionKey:'movie:77'}],shows:[show],channels:[channel],videos:[video]};
let selected=out.withFavourites(data,favourites);
assert.equal(selected.movies[0].favourite,true,'Canonical IDs match across NZB/VOD snapshots');
assert.equal(selected.shows[0].favourite,true);assert.equal(selected.shows[1].favourite,false,'Titles alone must never match');
assert.equal(selected.channels[0].favourite,true);assert.equal(selected.videos[0].favourite,true);
assert.equal(movie.favourite,undefined,'Loading favourite state does not mutate Watchlist snapshots');
assert.equal(selected.movies[0].automatic,true);assert.equal(selected.shows[0].downloadFuture,true);
for(const item of [movie,show,channel,video]){
 const write=out.favouriteWrite(item);
 assert.ok(!JSON.stringify(write.body).includes('watchlisted'));
 if(item.kind==='movie'||item.kind==='show'){
  assert.deepEqual(JSON.parse(JSON.stringify(write.body.settings)),{favourite:true});
  assert.equal(write.body.item.id,item.id);
 }else assert.deepEqual(JSON.parse(JSON.stringify(write.body)),{favourite:true});
 const off=out.favouriteWrite({...item,favourite:true});
 assert.equal(off.body.favourite??off.body.settings.favourite,false);
 assert.doesNotMatch(write.url,/watchlist/);
}
assert.equal(out.favouriteWrite(channel).url,'/api/channel-favourite/'+channel.id);
assert.equal(out.favouriteWrite(video).url,'/api/favourites/video/abcdefghijk');
selected=out.withFavourites(data,{movies:[],shows:[],channels:[],videos:[]});
assert.equal(selected.movies[0].favourite,false);assert.equal(selected.shows[0].downloadFuture,true);
console.log('Watchlist hearts: canonical saved state, on/off routes and favourite-only writes preserve membership and download policies.');
