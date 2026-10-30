const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const h=React.createElement;
const nodes=node=>node==null?[]:Array.isArray(node)?node.flatMap(nodes):typeof node==='object'?[node,...nodes(node.props?.children),...nodes(node.props?.content),...nodes(node.props?.downloads)]:[];
const text=node=>node==null?'':Array.isArray(node)?node.map(text).join(''):typeof node==='string'||typeof node==='number'?String(node):text(node.props?.children);
function load(file,imports){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:name=>imports[name]||(name.endsWith('.module.css')?{default:{}}:require(name))});return out;}
const artwork=({src,fit})=>h('img',{src,alt:'',style:{width:'100%',height:'100%',objectFit:fit}}),sourceCalls=[];
const avatar=load('ChannelAvatar.tsx',{'./LiveArtwork':{__esModule:true,default:artwork}}).default;
const programme={id:'event-video',videoId:'ABCDEFGHIJK',title:'An exact programme title that remains fully accessible',date:'2026-10-26',start:19.5,durationSeconds:654,status:'Uploaded',note:'Full unique programme note',description:'Full unique programme description',thumbnail:'/programme.jpg',category:'Documentary'};
const expected={...programme,id:'event-expected',videoId:undefined,title:'Expected upload',start:21,status:'Expected',note:'Estimated from this channel',description:'An estimate, not an announced video'};
const movie={id:'movie:123',kind:'movie',externalId:'123',title:'Original movie identity'};
const channels=[{id:'UC'+'a'.repeat(22),name:'Exact creator',avatar:'/creator.jpg',favourite:false,programmes:[programme,expected]},{id:'movie-source',name:movie.title,movie,avatar:'/movie.jpg',programmes:[{id:'movie-date',title:'Release date',date:'2026-10-27',start:0,allDay:true,status:'Release date',note:'Known catalogue date',description:'Movie details'}]}];
function scene(phone){
 const states=[];let cursor=0;
 const hooks={useState:initial=>{const i=cursor++;if(!(i in states))states[i]=typeof initial==='function'?initial():initial;return[states[i],next=>states[i]=typeof next==='function'?next(states[i]):next];},useRef:value=>({current:value}),useEffect(){}};
 const Placeholder=()=>null;
 const imports={react:hooks,'react/jsx-runtime':require('react/jsx-runtime'),'@mantine/core':M,'@mantine/hooks':{useMediaQuery:()=>phone},'./components/SelectField':{SelectField:Placeholder},'./components/ui':{Button:({variant,...props})=>h(M.Button,{...props,variant:variant==='secondary'?'default':variant}),Switch:Placeholder},'./TextField':{TextInput:Placeholder},'./pageSearch':{usePageSearch:()=>'',setPageSearch(){}},'./mediaDate':{mediaDate:day=>day},'./mediaDuration':require('./media-duration-test-helper.cjs')(),'./LiveArtwork':{__esModule:true,default:artwork},'./ChannelAvatar':{__esModule:true,default:avatar},'./channelNavigation':{openChannel:value=>sourceCalls.push(value)},'./guideData':{shiftDay:(day,n)=>new Date(Date.parse(day+'T12:00Z')+n*86400000).toISOString().slice(0,10),sortChannels:rows=>rows},'./VideoSheet':{__esModule:true,default:Placeholder},'./VideoDownloadControls':{__esModule:true,default:Placeholder},'./discovery/DiscoveryTitle':{__esModule:true,default:Placeholder},'./collections/TileActions':{__esModule:true,default:Placeholder},'./notifications':{NotificationMessage:Placeholder},'./components/states':{SkeletonRow:Placeholder},'./components/LoadingStatus':{__esModule:true,default:Placeholder}};
 const module=load('Guide.tsx',imports),favourites=[];
 const live={csrf:'exact-csrf',channels,date:'2026-10-26',today:'2026-10-26',now:20,loading:false,error:'',truncated:false,saving:false,changeDay(){},retry(){},selectDate(){},favourite:(...args)=>favourites.push(args)};
 return{module,imports,live,favourites,draw(){cursor=0;return module.default({live,favouritesOnTop:false,onFavouritesOnTop(){}});}};
}
for(const phone of [false,true]){
 const s=scene(phone);let tree=s.draw(),rows=nodes(tree).filter(node=>node.props?.['data-guide-programme']);
 assert.equal(rows.length,3);assert.equal(nodes(tree).filter(node=>String(node.props?.['aria-label']||'').startsWith('Programmes for ')).length,2);
 for(const row of rows){
  assert.equal(row.type,M.Paper);assert.equal(row.props.p,'md');assert.equal(row.props.radius,'md');assert.equal(row.props.withBorder,true,'same list-card framework surface and spacing as Subscriptions');
  assert.equal(row.props.style.minWidth,0);assert.equal(row.props.style.height,undefined,'rows grow only for their content');
  const all=nodes(row),actions=all.filter(node=>node.type===M.ActionIcon),details=actions.filter(node=>node.props['aria-label'].startsWith('Open '));
  assert.equal(details.length,1,'one explicit programme details control');assert.equal(details[0].props.size,44);assert.equal(actions.at(-1),details[0],'the details chevron remains farthest right');
  const group=all.find(node=>node.type===M.Group&&Array.isArray(node.props.children)&&node.props.children.includes(details[0]));assert.equal(group.props.wrap,'nowrap');
  assert.ok(!all.some(node=>['Details','Source information'].includes(node.props?.children)),'old duplicate explicit actions are removed');
  const heading=all.find(node=>node.type===M.Text&&node.props.component==='h3');assert.equal(heading.props.title,text(heading));assert.equal(heading.props.lineClamp,2);
  const badge=all.find(node=>node.type===M.Badge),metadata=all.find(node=>node.type===M.Group&&nodes(node.props.children).includes(badge));assert.ok(metadata,'status is inline with compact time metadata');
  const source=all.find(node=>node.props?.['data-guide-source']),video=all.find(node=>node.props?.['data-guide-video']),actionsGroup=all.find(node=>node.props?.['data-guide-actions']);
  assert.ok(source&&video&&actionsGroup,'source identity and programme details have separate layout columns');
  assert.ok(nodes(source).some(node=>node.props?.['data-guide-upload']),'upload information is below the source identity');
  assert.ok(!nodes(video).some(node=>node.props?.['data-guide-source-open']),'source does not compete with programme description');
  const description=all.find(node=>node.props?.['data-guide-description']);assert.equal(description.props.lineClamp,2);assert.equal(description.props.size,'sm','description stays readable instead of tiny metadata');
  if(phone){assert.equal(row.props.children.type,M.Stack);assert.equal(row.props.children.props.children[1],video,'phone programme description uses the full row below source and actions');}
  else{assert.equal(row.props.children.type,M.Group);assert.equal(row.props.children.props.wrap,'nowrap');assert.equal(row.props.children.props.children[1],video);assert.equal(row.props.children.props.children.at(-1),actionsGroup,'desktop has source left, description next to it, actions far right');}
  for(const scheme of ['light','dark'])for(const width of [320,768,1440]){
   const html=renderToStaticMarkup(h(M.MantineProvider,{forceColorScheme:scheme},h('div',{style:{width}},row)));
   assert.match(html,/mantine-Paper-root/);assert.ok(html.includes(details[0].props['aria-label']));assert.ok(html.includes('calc(2.75rem'),'explicit actions retain44px sizing');
   assert.equal((html.match(/aria-label="Open /g)||[]).length,1);assert.ok(!html.includes('data-activity-card'),'Guide no longer uses the padded Activity card');
   assert.equal((html.match(/<img /g)||[]).length,1,'one source image, without an extra video thumbnail');assert.match(html,/object-fit:contain/,'channel brand/poster artwork is contained rather than cropped');
  }
 }
 const videoRow=rows[0],all=nodes(videoRow),source=all.find(node=>node.props?.['data-guide-source']),opener=nodes(source).find(node=>node.props?.['data-guide-source-open']),avatarNode=nodes(source).find(node=>node.type===avatar);
 assert.equal(opener.props['aria-label'],'Channel information: Exact creator');assert.equal(opener.props.mih,44);assert.equal(avatarNode.props.src,'/creator.jpg');assert.equal(avatarNode.props.detail,true,'large contained brand presentation uses the existing detail avatar');
 const frame=nodes(source).find(node=>node.type===M.Box&&nodes(node.props.children).includes(avatarNode));assert.equal(frame.props.w,phone?48:64);assert.equal(frame.props.h,phone?48:64);
 opener.props.onClick();assert.deepEqual({...sourceCalls.at(-1)},{id:channels[0].id,name:channels[0].name,avatar:channels[0].avatar},'source opener preserves the exact channel identity');
 assert.ok(text(nodes(source).find(node=>node.props?.['data-guide-upload'])).includes('Published · 2026-10-26 · 19:30'),'publish date and time stay readable below the channel');
 assert.ok(text(all.find(node=>node.props?.['data-guide-description'])).startsWith(programme.description),'the video description leads the adjacent detail column');
 assert.ok(text(videoRow).includes('19:30'));assert.ok(text(videoRow).includes('10m 54s'));assert.ok(text(videoRow).includes('Published'));
 const favourite=nodes(videoRow).find(node=>node.props?.['aria-label']==='Favourite Exact creator');favourite.props.onClick();assert.deepEqual(s.favourites,[[channels[0].id,true]]);
 s.live.saving=true;assert.equal(nodes(s.draw()).find(node=>node.props?.['aria-label']==='Favourite Exact creator').props.disabled,true);s.live.saving=false;
 nodes(videoRow).find(node=>node.props?.['aria-label']==='Open '+programme.title).props.onClick();tree=s.draw();
 let sheet=nodes(tree).find(node=>node.type===s.imports['./VideoSheet'].default&&node.props.video);
 assert.equal(sheet.props.video.id,programme.videoId);assert.equal(sheet.props.video.image,programme.thumbnail);assert.equal(sheet.props.video.channelId,channels[0].id);assert.equal(sheet.props.video.durationSeconds,654);
 assert.equal(text(sheet.props.content).split(programme.note).length-1,1);assert.equal(text(sheet.props.content).split(programme.description).length-1,1,'Overview retains note and description exactly once');
 const download=nodes(sheet.props.downloads).find(node=>node.props?.id===programme.videoId);assert.equal(download.props.canDownload,true);assert.equal(download.props.csrf,'exact-csrf');
 rows=nodes(s.draw()).filter(node=>node.props?.['data-guide-programme']);nodes(rows[1]).find(node=>node.props?.['aria-label']==='Open Expected upload').props.onClick();
 assert.ok(text(nodes(rows[1]).find(node=>node.props?.['data-guide-upload'])).startsWith('Expected · '),'forecast is not mislabeled as published');
 sheet=nodes(s.draw()).find(node=>node.type===s.imports['./VideoSheet'].default&&node.props.video);assert.equal(sheet.props.canManage,false);assert.equal(sheet.props.video.id,'');assert.ok(!nodes(sheet.props.downloads).some(node=>node.props?.id));
 assert.ok(text(rows[2]).includes('Known catalogue date'));assert.ok(text(rows[2]).includes('Movie details'),'non-video programme context remains accessible without changing discovery metadata');
 nodes(rows[2]).find(node=>node.props?.['aria-label']==='Open '+movie.title).props.onClick();assert.equal(nodes(s.draw()).find(node=>node.props?.item===movie).props.item,movie,'movie dates keep their exact discovery identity');
 nodes(rows[2]).find(node=>node.props?.['data-guide-source-open']).props.onClick();assert.equal(nodes(s.draw()).find(node=>node.props?.item===movie).props.item,movie,'metadata source image/name opens the same exact movie');
 const timeline=nodes(s.draw()).find(node=>node.props?.['aria-label']==='Guide view');timeline.props.onChange({currentTarget:{value:'timeline'}});assert.ok(nodes(s.draw()).some(node=>node.props?.['aria-label']==='Seven-day channel guide'),'optional timeline remains available');timeline.props.onChange({currentTarget:{value:'agenda'}});
 s.live.channels=[{...channels[0],programmes:[{...programme,note:programme.description}]}];
 nodes(s.draw()).find(node=>node.props?.['aria-label']==='Open '+programme.title).props.onClick();
 sheet=nodes(s.draw()).find(node=>node.type===s.imports['./VideoSheet'].default&&node.props.video);
 assert.equal(text(sheet.props.content).split(programme.description).length-1,1,'identical note and description have one Overview owner');
}
const source=fs.readFileSync(__dirname+'/src/Guide.tsx','utf8');assert.doesNotMatch(source,/components\/ListRow|CompactActivityRows/,'the Guide uses the Subscriptions compact anatomy independently of Activity');
console.log('Guide Subscriptions rows: large contained channel brand with publish info below, adjacent readable programme description, responsive full-width phone details, one final44px chevron, exact identities/permissions/notes/timeline preserved.');
