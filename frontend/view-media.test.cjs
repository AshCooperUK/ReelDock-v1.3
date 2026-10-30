const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
function moduleFor(name){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/viewMedia/'+name+'.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:out});return out;}
const {attachScrollPaging,trackPagingMomentum}=moduleFor('scrollPaging');
const listeners={},target={scrollY:0,innerHeight:800,addEventListener:(k,v)=>listeners[k]=v,removeEventListener:k=>delete listeners[k]};
let calls=0,edge=2000,time=1000;
let cleanup=attachScrollPaging(target,()=>edge,()=>calls++,()=>time);
assert.equal(calls,0,'mount must not load');listeners.wheel({deltaY:50});assert.equal(calls,0,'must reach edge');
edge=810;listeners.wheel({deltaY:50});listeners.wheel({deltaY:50});assert.equal(calls,1,'one batch per attachment');
cleanup();cleanup=attachScrollPaging(target,()=>edge,()=>calls++,()=>time);assert.equal(calls,1,'completion cannot chain');
listeners.wheel({deltaY:20});assert.equal(calls,1,'momentum after completion cannot chain');listeners.wheel({deltaY:-20});assert.equal(calls,1);target.scrollY=30;listeners.scroll();assert.equal(calls,1,'layout shift must not load');time+=200;listeners.wheel({deltaY:20});assert.equal(calls,2,'fresh downward scroll loads next batch');cleanup();
const {orderCards}=moduleFor('ordering');const cards=[{id:'1',title:'Zulu',year:''},{id:'2',title:'Beta',year:'1990'},{id:'3',title:'Alpha',year:'2026'}];
assert.equal(orderCards(cards,'title').map(x=>x.id).join(','),'3,2,1');
assert.equal(orderCards(cards,'year_asc').map(x=>x.id).join(','),'2,3,1');
assert.equal(cards[0].id,'1','sort must not mutate pagination buffer');
console.log('View Media scroll gating and stable ordering passed.');

let busyCleanup=trackPagingMomentum(target,()=>time);
time+=1000;listeners.wheel({deltaY:20});busyCleanup();
cleanup=attachScrollPaging(target,()=>edge,()=>calls++,()=>time);
time+=50;listeners.wheel({deltaY:20});assert.equal(calls,2,'momentum tracked during loading cannot start another page');
listeners.keydown({key:'End',repeat:true});assert.equal(calls,2,'held keys cannot chain pages');
time+=200;listeners.wheel({deltaY:20});assert.equal(calls,3,'a fresh gesture after loading still works');cleanup();

// Popup batches use the same gate, measured against the popup's own scroll area.
const popupListeners={},popup={scrollTop:0,clientHeight:400,addEventListener:(k,v)=>popupListeners[k]=v,removeEventListener:k=>delete popupListeners[k],getBoundingClientRect:()=>({right:800})};
let popupCalls=0,popupClock=0;
let popupCleanup=attachScrollPaging(popup,()=>450,()=>popupCalls++,()=>popupClock);
popupListeners.wheel({deltaY:30});assert.equal(popupCalls,1);
popupCleanup();popupCleanup=attachScrollPaging(popup,()=>450,()=>popupCalls++,()=>popupClock);
popupClock+=80;popupListeners.wheel({deltaY:30});assert.equal(popupCalls,1,'momentum must not chain popup batches');
popupClock+=200;popupListeners.wheel({deltaY:30});assert.equal(popupCalls,2,'a fresh scroll may load the next popup batch');
popupCleanup();

const {refreshDelay,refreshedPage,pendingPage}=moduleFor('refresh');
assert.equal(refreshDelay({refreshing:true},'discover'),2000);
assert.equal(refreshDelay({},'discover'),60000,'an open NZB list must continue checking for updates');
assert.equal(refreshDelay({},'subscriptions'),null,'existing YouTube source keeps its refresh behaviour');
const prior={source:'discover',media:'movies',items:[{id:'old'},{id:'later'}],nextOffset:2,nextCursor:'{"nzb":200}'};
const fresh={source:'discover',media:'movies',items:[{id:'new'},{id:'old',title:'Updated'}],nextOffset:1,nextCursor:'{"nzb":100}'};
const merged=refreshedPage(prior,fresh);
assert.equal(merged.items.map(x=>x.id).join(','),'new,old,later');
assert.equal(merged.items[1].title,'Updated');
assert.equal(merged.nextCursor,prior.nextCursor,'background refresh must keep the scroll cursor');
assert.equal(refreshedPage({...prior,nextOffset:1},fresh),fresh,'first page refresh replaces removed titles');
assert.equal(refreshedPage(prior,{...fresh,nextOffset:null}).nextOffset,null,'an exhausted replacement resets pagination');
console.log('View Media NZB refresh and pagination preservation passed.');

const pinned={...prior,source:'global',searchSnapshot:'snapshot-a'};
assert.equal(refreshedPage(pinned,{...fresh,source:'global',searchSnapshot:'snapshot-a'}),pinned,'refreshing a pinned search keeps every loaded page and its cursor');
const replacement={...fresh,source:'global',searchSnapshot:'snapshot-b'};
assert.equal(refreshedPage(pinned,replacement),replacement,'a new search window must not merge with pages from the old one');
assert.equal(refreshDelay({...replacement,loading:false,refreshing:false},'global'),null,'a ready fixed search must not refresh itself while scrolling');
assert.equal(refreshDelay({...replacement,loading:true},'global'),2000,'the initial collection keeps polling until its complete window is ready');

const films=[{id:'old',title:'Old',year:'1999'},{id:'new',title:'New',year:'2026'},{id:'dated',title:'Dated',releaseDate:'2025-12-31',year:'2025'}];
assert.equal(orderCards(films,'release').map(x=>x.id).join(','),'new,dated,old','missing exact dates still sort by known movie year');
assert.equal(orderCards([{id:'old',added:2},{id:'new',added:3}],'new')[0].id,'new','NZB upload sort uses its own timestamp');

const movieChoices=moduleFor('movieOptions');
assert.equal(movieChoices.movieSections.map(s=>s.label).join('|'),'Premiering Movies|New Movies|Trending Movies|Top Rated Movies|View All Movies');
assert.equal(movieChoices.movieSorts.map(s=>s.label).join('|'),'A-Z|Z-A|Latest|Oldest|NZB Order|Year|Rating');
assert.equal(movieChoices.defaultMovieSection,'trending');assert.equal(movieChoices.defaultMovieSort,'release');
assert.equal(refreshedPage({...prior,collection:'top'},{...fresh,collection:'new'}).items.length,2,'refresh must not mix sections');
const {mergeCards}=moduleFor('refresh');
const duplicates=[
 {id:'movie:00305224',kind:'movie',title:'Anger Management',action:{type:'discover',item:{externalId:'00305224'}}},
 {id:'movie:0305224',kind:'movie',title:'Anger Management',action:{type:'discover',item:{externalId:'0305224'}}},
 {id:'movie:12345',kind:'movie',title:'Anger Management',action:{type:'discover',item:{externalId:'12345'}}},
];
assert.equal(mergeCards(duplicates).length,2,'padded IMDb identities merge across pages, different movies with the same name stay separate');

const arriving=pendingPage({items:[{id:'first',title:'First'}],nextOffset:1,nextCursor:'{"movies":60}'},{items:[{id:'second',title:'Second'}],nextOffset:2,nextCursor:'{"movies":120}'});
assert.equal(arriving.items.map(item=>item.id).join(','),'first,second');
assert.equal(arriving.nextCursor,'{"movies":60}','Partial cards must not skip an unfinished batch');
assert.equal(arriving.nextOffset,1);assert.equal(arriving.preparing,true);

// Provider completion updates the feedback around a frame without moving cards.
const checking={...pinned,providerChecksPending:true,providerChecksComplete:false,searchUpdatesAvailable:false,refreshAfterMs:2000};
const checked=refreshedPage(checking,{...replacement,searchSnapshot:checking.searchSnapshot,providerChecksPending:false,providerChecksComplete:true,searchUpdatesAvailable:true,refreshAfterMs:0});
assert.equal(checked.items,checking.items,'completed checks must not insert or reorder the visible search frame');
assert.equal(checked.nextCursor,checking.nextCursor,'status updates must preserve the scroll cursor');
assert.equal(checked.nextOffset,checking.nextOffset);
assert.equal(checked.searchUpdatesAvailable,true,'completed results expose an explicit update choice');
assert.equal(checking.providerChecksPending,true,'feedback refresh must not mutate a cached frame');
assert.equal(refreshDelay(checking,'global'),2000,'saved matches keep checking provider completion');
assert.equal(refreshDelay({...checking,refreshAfterMs:50},'global'),1000,'a malformed retry interval cannot cause a tight polling loop');
assert.equal(refreshDelay(checked,'global'),null,'completed checks stop polling until the user updates');

const {filterMediaCards}=moduleFor('filtering');
const mixed=[
 {id:'movie-a',kind:'movie',title:'Shared title',origins:['NZB','VOD'],action:{type:'discover'}},
 {id:'movie-b',kind:'movie',title:'Shared title',origin:'Local Library',action:{type:'local'}},
 {id:'show-a',kind:'shows',action:{type:'local-group'}},
 {id:'channel-a',kind:'channel',origin:'YouTube',action:{type:'youtube'}},
 {id:'video-a',kind:'video',action:{type:'youtube'}},
];
assert.equal(filterMediaCards(mixed,'vod').map(card=>card.id).join(','),'movie-a','a merged media card remains visible in every known source');
assert.equal(filterMediaCards(mixed,'local','movies').map(card=>card.id).join(','),'movie-b','same-name titles from distinct identities remain separate');
assert.equal(filterMediaCards(mixed,'all','shows').map(card=>card.id).join(','),'show-a','local show groups use the same media-type filter');
assert.equal(filterMediaCards(mixed,'subscriptions','youtube').map(card=>card.id).join(','),'channel-a,video-a','YouTube filters include channels and videos');
assert.equal(filterMediaCards(mixed,'all','all').length,mixed.length);
assert.equal(filterMediaCards(mixed,'vod')[0],mixed[0],'filters retain the original card and download action');
assert.equal(mixed[0].origins.join(','),'NZB,VOD','filtering must not rewrite source provenance');
console.log('Search source/type filters and stable provider completion feedback passed.');
const alerted=refreshedPage(checked,{...checked,items:[{id:'must-not-appear'}],warnings:['One source is unavailable.'],freshness:{source:'Saved search',scope:'Showing saved matches.'}});
assert.equal(alerted.items,checking.items,'current source alerts cannot replace the pinned cards');
assert.equal(alerted.warnings[0],'One source is unavailable.','finished provider failures update the surrounding feedback');
assert.equal(alerted.freshness.scope,'Showing saved matches.');
assert.equal(refreshedPage(alerted,{...alerted,warnings:[...alerted.warnings],freshness:{...alerted.freshness}}),alerted,'equivalent status metadata avoids an unnecessary frame update');
