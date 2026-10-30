const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript'),React=require('react'),jsx=require('react/jsx-runtime'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const browse=require('./media-browse-test-helper.cjs')(),cache=new Map(),channel='UC'+'n'.repeat(22);
const collection={movies:[],shows:[],videos:[],channels:[],episodes:[],csrf:'fixture'};
const Button=({variant,iconOnly,children,...props})=>React.createElement(iconOnly?M.ActionIcon:M.Button,{...props,variant:variant==='secondary'?'default':variant==='ghost'?'subtle':'filled',size:44},children);
function load(file,react=React,append=''){
 const key=file+append;if(react===React&&cache.has(key))return cache.get(key);const out={};if(react===React)cache.set(key,out);
 vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'src',file),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText+append,{exports:out,Intl,Date,Set,URLSearchParams,AbortController,encodeURIComponent,require:name=>{
  if(name==='react')return react;if(name==='react/jsx-runtime')return jsx;if(name==='@mantine/core')return M;if(name==='lucide-react')return require(name);
  if(/components\/browse$/.test(name))return browse;
  if(/components\/ui$/.test(name))return {Button};
  if(name.endsWith('tileCollectionStore'))return {useTileCollections:()=>({watchlist:collection,favourites:collection,error:''}),refreshTileCollections:async()=>{}};
  if(/\/api$/.test(name))return {api:()=>{throw Error('SSR must never make media requests');}};
  if(name.endsWith('HoverCard'))return {__esModule:true,default:()=>null,useCardHover:()=>({anchor:{current:null}}),titleHover:()=>({})};
  if(name.endsWith('ChannelIdentity'))return {__esModule:true,default:({name})=>React.createElement(M.UnstyledButton,{mih:44},name)};
  if(name.endsWith('VideoTitle'))return {__esModule:true,default:({title})=>React.createElement(M.UnstyledButton,{mih:44},title)};
  if(name.endsWith('SelectField'))return require('./select-field-test-helper.cjs')();
  if(name.endsWith('.css'))return {};
  if(name.endsWith('/mediaDuration'))return require('./media-duration-test-helper.cjs')();
  const local=path.posix.normalize(path.posix.join(path.posix.dirname(file),name));
  if(/(?:TileActions|tileIdentity|collectionFavourites|discoverPayload|mediaDate|movieOptions|showOptions|ordering)$/.test(local))return load(local+(/(?:Options|Identity|Favourites|Payload|Date|ordering)$/.test(local)?'.ts':'.tsx'));
  return new Proxy({},{get:(_,key)=>key==='__esModule'?true:()=>null});
 }});return out;
}
// Read actual private card implementations without adding production-only exports.
const playlist=load('M3uLibrary.tsx',React,'\nexports.PlaylistCard=PlaylistCard;').PlaylistCard;
const Shelf=load('collections/CollectionShelf.tsx').default;
const Tile=load('viewMedia/ViewMedia.tsx').MediaTile;
const data=[{id:'abcdefghijk',title:'Channel upload',state:'published',date:'2026-10-24',thumbnail:'/channel.jpg',duration:'12:03'}];let state=0;
const channelReact={...React,useEffect(){},useCallback:fn=>fn,useRef:value=>({current:value}),useState:value=>[state++===0?data:typeof value==='function'?value():value,()=>{}]};
const ChannelVideos=load('ChannelVideos.tsx',channelReact).default;
const cases=[
 ['NZB movie',()=>React.createElement(Tile,{item:{id:'movie:77',kind:'movie',title:'Movie',image:'/movie.jpg',runtimeMinutes:125,year:'2026',origin:'NZB',action:{type:'discover',item:{id:'movie:77',kind:'movie',title:'Movie'}}},layout:'grid',searched:true,onOpen(){}}),'2h 5m'],
 ['TV show',()=>React.createElement(Tile,{item:{id:'show:88',kind:'show',title:'TV show',image:'/show.jpg',year:'2026',origin:'NZB',action:{type:'discover',item:{id:'show:88',kind:'show',title:'TV show'}}},layout:'grid',searched:true,onOpen(){}})],
 ['YouTube search',()=>React.createElement(Tile,{item:{id:'youtube:abcdefghijk',kind:'video',title:'YouTube clip',image:'/youtube.jpg',durationSeconds:123,date:'2026-10-24',action:{type:'youtube',id:'abcdefghijk'}},layout:'grid',searched:true,onOpen(){}}),'2m 3s'],
 ['VOD playlist',()=>React.createElement(playlist,{source:'source-exact',item:{path:'movie.strm',kind:'movies',title:'VOD movie',artwork:'/vod.jpg',runtimeMinutes:95,year:2026,group_name:'Movies'},onOpen(){}}),'1h 35m'],
 ['Favourites',()=>React.createElement(Shelf,{id:'movies',title:'Movies',items:[{id:'movie:77',kind:'movie',title:'Favourite movie',image:'/favourite.jpg',runtimeMinutes:110,year:'2026'}],kind:'movie',open(){}}),'1h 50m'],
 ['Channel uploads',()=>{state=0;return React.createElement(ChannelVideos,{id:channel,name:'Channel',csrf:'fixture'});},'12m 3s']
];
for(const [label,render,length] of cases){const html=renderToStaticMarkup(React.createElement(M.MantineProvider,{forceColorScheme:'dark'},render()));for(const slot of ['artwork','title','meta','status','actions'])assert.equal((html.match(new RegExp('data-media-'+slot+'=', 'g'))||[]).length,1,label+' uses the one shared '+slot+' slot');assert.equal((html.match(/mantine-AspectRatio-root/g)||[]).length,1,label+' has one portrait image slot');assert.equal((html.match(/<li\b/g)||[]).length,1,label+' uses the one shared media card');assert.equal((html.match(/data-media-duration=/g)||[]).length,length?1:0,label+' uses the common single-title length badge');if(length)assert.ok(html.includes(length),label+' maps source units explicitly');const footer=html.slice(html.indexOf('data-media-actions'));assert.equal((footer.match(/mantine-ActionIcon-root/g)||[]).length,2,label+' has the same heart/bookmark action footer');}
console.log('Source parity: actual NZB/TV/YouTube/VOD/Favourites/channel-upload cards share one portrait/title/meta/status/two-action anatomy.');
