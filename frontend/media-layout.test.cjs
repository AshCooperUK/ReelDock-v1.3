const assert=require('node:assert/strict'),fs=require('node:fs'),React=require('react'),{renderToStaticMarkup}=require('react-dom/server');
const {MantineProvider,ActionIcon,UnstyledButton,Text}=require('@mantine/core');
const {MediaGrid,MediaCard,MediaArtwork}=require('./media-browse-test-helper.cjs')();
for(const scheme of ['light','dark'])for(const accent of ['blue','cyan','teal','green','lime','yellow','orange','red','pink','grape','violet','indigo'])for(const src of [undefined,'/channel.jpg']){
 const art=MediaArtwork({kind:'channel',src}),slot=art.props.children;
 const caption=React.Children.toArray(slot.props.children).find(child=>child.props?.['data-media-kind-caption']!==undefined);
 assert.ok(caption,'channel caption remains inside the one artwork slot');assert.equal(caption.props.variant,'filled');assert.equal(caption.props.autoContrast,true,'Mantine chooses contrasting text for every accent');assert.equal(caption.props.maw,'calc(100% - 16px)','caption is bounded by the artwork width');
 const markup=renderToStaticMarkup(React.createElement(MantineProvider,{theme:{primaryColor:accent},forceColorScheme:scheme},art));
 assert.match(markup,/data-media-kind-caption/);assert.match(markup,/mantine-Badge-root/);assert.equal((markup.match(/<img\b/g)||[]).length,src?1:0,scheme+' '+accent+' caption adds no image or fallback layer');assert.equal((markup.match(/mantine-AspectRatio-root/g)||[]).length,1);
}
for(const src of ['/poster.jpg',undefined]){
 const art=MediaArtwork({src,badge:'Saved'});
 assert.equal(React.Children.count(art.props.children),1,'Mantine9 AspectRatio receives one media slot, never independent placeholder/poster siblings');
 const slot=art.props.children;
 const fill=React.Children.toArray(slot.props.children)[0];
 assert.equal(fill.props.pos,'absolute','artwork is bounded inside that one ratio slot');assert.equal(fill.props.inset,0);
 assert.equal(React.Children.count(fill.props.children),1,'exactly one live image or fallback occupies the slot');
}
const cards=Array.from({length:3},(_,i)=>React.createElement(MediaCard,{key:i,title:React.createElement(UnstyledButton,{'aria-label':'Open title '+i},'Title '+i),art:React.createElement(MediaArtwork,{src:'/poster'+i+'.jpg'}),meta:'Movie · 2026',status:'Saved',actions:React.createElement(ActionIcon,{'aria-label':'Favourite '+i},'♥')}));
const html=renderToStaticMarkup(React.createElement(MantineProvider,{forceColorScheme:'dark'},React.createElement(MediaGrid,{label:'Three saved titles'},cards)));
assert.equal((html.match(/<li\b/g)||[]).length,3,'every saved item renders, including the third item');
assert.equal((html.match(/mantine-Card-root/g)||[]).length,3,'every item uses the same framework card');
assert.equal((html.match(/aria-label="Favourite /g)||[]).length,3,'each card retains its always-present action');
assert.ok(html.includes('--sg-cols:2')&&html.includes('--sg-cols:4'),'phone and desktop column limits are explicit');
assert.ok(!html.includes('--sg-cols:5')&&!html.includes('--sg-cols:6'),'poster grids never become six-column legacy shelves');
assert.equal((html.match(/loading="lazy"/g)||[]).length,3,'posters retain lazy artwork loading');
assert.ok(!/<button[^>]*>(?:(?!<\/button>)[\s\S])*<button/.test(html),'art/title and actions never form nested controls');
for(const file of ['components/browse.tsx','components/DetailSheet.tsx','viewMedia/ViewMedia.tsx','collections/CollectionShelf.tsx','discovery/DiscoveryTitle.tsx','ChannelViews.tsx','ChannelVideos.tsx','M3uLibrary.tsx']){
 const source=fs.readFileSync(__dirname+'/src/'+file,'utf8');
 assert.doesNotMatch(source,/import ['"][^'"]*(?:collections|view-media|discovery|channel-views|channel-detail|m3u-library|styles)\.css['"]/,'media layouts cannot import a legacy skin: '+file);
}
const detail=fs.readFileSync(__dirname+'/src/components/DetailSheet.tsx','utf8');
assert.match(detail,/scrollRef=\{bodyRef\}/,'paged details observe the actual shared dialog scroll root');
assert.doesNotMatch(detail,/ref=\{bodyRef\}/,'an inner panel cannot replace the scroll-root reference');
console.log('Shared media layout: three visible cards, responsive 2–4 grid, independent actions, lazy posters and one scroll root passed.');
