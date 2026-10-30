const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),mantine=require('@mantine/core');
function load(file,imports={}) {
 const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,{exports:out,Intl,Date,require:name=>name.endsWith('/mediaDate')?require('./media-date-test-helper.cjs')():name.endsWith('/mediaDuration')?require('./media-duration-test-helper.cjs')():name.endsWith('SelectField')?require('./select-field-test-helper.cjs')():imports[name]||(name.endsWith('.css')?{}:require(name))});return out;
}
const ui=load('components/ui.tsx',{'../ScrollToTop':{default:()=>null},'../notifications-state':{registerNotificationHost:()=>()=>{}}});
const {mantineTheme}=load('mantineTheme.ts');
const item={id:'movie:77',kind:'movie',title:'Fixture',watchlistKey:'movie:77',favourite:true};
const render=node=>renderToStaticMarkup(React.createElement(mantine.MantineProvider,{forceColorScheme:'dark',theme:mantineTheme},node));
const hover={default:()=>null,useCardHover:()=>({anchor:{current:null}}),titleHover:()=>({})};
const shelf=load('collections/CollectionShelf.tsx',{'../components/ui':ui,'../components/browse':{
 MediaGrid:({children})=>React.createElement('ul',null,children),MediaCard:({title,actions})=>React.createElement('li',null,title,actions)},
 '../ChannelIdentity':{default:()=>null},'../VideoTitle':{default:()=>null},'./TileActions':{default:()=>null},'../HoverCard':hover});
const shelfHtml=render(React.createElement(shelf.default,{id:'fixture',title:'Movies',items:[item],kind:'movie',collection:'watchlist',open(){},onRemove(){},onFavourite(){},removing:['movie:77']}));
let requests=0;
const tiles=load('collections/TileActions.tsx',{'../api':{api:()=>{requests++;throw Error('SSR must not send requests');}},'../components/ui':ui,
 '../discoverPayload':{acquisitionItem:value=>value},'./collectionFavourites':{favouriteWrite:()=>({})},
 './tileCollectionStore':{useTileCollections:()=>({favourites:{movies:[item]},watchlist:{movies:[],csrf:'fixture-csrf'}}),refreshTileCollections:async()=>{}},
 './tileIdentity':{collectionIdentity:value=>value.id,watchlistHint:()=> 'Follow this movie'},'../notifications':{NotificationMessage:()=>null}});
const tileHtml=render(React.createElement(tiles.default,{item,inline:true}));
for(const [name,html] of [['collection',shelfHtml],['media tile',tileHtml]]) {
 const buttons=html.match(/<button\b[^>]*>/g)||[];
 const icons=buttons.filter(button=>button.includes('mantine-ActionIcon-root'));
 assert.equal(icons.length,2,name+' keeps two real Mantine ActionIcon controls');
 assert.ok(icons.every(button=>button.includes('--ai-size:')&&button.includes('2.75rem')),name+' icons share the actual 44px square geometry');
 assert.ok(icons.every(button=>button.includes('type="button"')),name+' actions cannot submit an enclosing form');
 assert.ok(icons.every(button=>button.includes('aria-label="')),name+' icon controls retain accessible names');
 assert.ok(icons.some(button=>button.includes('aria-pressed="true"')),name+' favourite state survives the framework control');
}
assert.match(shelfHtml,/aria-label="Remove from Watchlist · Fixture"[^>]*disabled=""/,'per-item pending removal stays disabled');
assert.equal(requests,0);
console.log('Browse controls: four real Mantine icon actions retain 44px geometry, names, state and disabled behaviour.');
