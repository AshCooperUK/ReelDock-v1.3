const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const h=React.createElement,css={default:new Proxy({},{get:(_,key)=>String(key)})};
let selected;
function compile(file,imports={}){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,localStorage:{getItem:()=>JSON.stringify(selected)},require:name=>imports[name]||(name.endsWith('.module.css')?css:require(name))});return out;}
const appearance=compile('app/appearance.ts'),theme=compile('mantineTheme.ts');
const Provider=compile('app/AppearanceProvider.tsx',{'../mantineTheme':theme,'./appearance':appearance}).default;
const {ActivityStatus}=compile('components/ActivityStatus.tsx');
const Button=({variant,...props})=>h(Mantine.Button,{...props,variant:variant==='secondary'?'default':variant});
const Panel=compile('downloadUI/DownloadPanel.tsx',{'../components/SelectField':{},'../components/ui':{Button},'../SettingsUI':{},'../notifications':{NotificationMessage:({kind,message})=>message?h(Mantine.Alert,{color:kind==='error'?'red':undefined},message):null}}).default;
const Network=compile('NetworkActivity.tsx',{'./SettingsUI':{Help:()=>null},'./api':{api(){throw Error('SSR must not request network activity');}}}).default;
function Probe(){const {colorScheme}=Mantine.useMantineColorScheme(),value=Mantine.useMantineTheme();return h('output',{'data-selected-scheme':colorScheme,'data-selected-accent':value.primaryColor});}
for(const phone of [false,true]){
 const row=compile('components/ListRow.tsx',{'@mantine/hooks':{useMediaQuery:()=>phone}});
 for(const scheme of ['light','dark'])for(const {value:accent}of appearance.ACCENTS){
  selected={scheme,accent};
  const queue=h(row.ActivityQueueRows,null,...[['downloading','blue'],['queued','yellow'],['failed','red'],['completed','green'],['encoding','violet']].map(([state,colour])=>h(row.default,{key:state,title:h('button',{type:'button'},'Fixture '+state),badge:h(ActivityStatus,{state}),statusColour:colour,meta:'Fixture source',progress:h(Mantine.Progress,{value:37,'aria-label':'Fixture progress'}),details:'Full outcome'})));
  const intake=h(Panel,{fill:true,status:'Ready to start',children:h(Mantine.TextInput,{label:'Download URL',value:'https://example.invalid/file',readOnly:true}),actions:h(Mantine.Button,{type:'submit',style:{minWidth:160,height:44}},'Start download')});
  const html=renderToStaticMarkup(h(Provider,null,h(Probe),queue,intake,h(Network)));
  assert.equal((html.match(/data-theme-background=/g)||[]).length,1,'one shared decorative background applies to every appearance');assert.match(html,/<div[^>]*aria-hidden="true"[^>]*data-theme-background/);
  assert.ok(html.includes('data-selected-scheme="'+scheme+'"'),'owned surfaces receive the selected '+scheme+' mode');assert.ok(html.includes('data-selected-accent="'+accent+'"'),'owned surfaces inherit '+accent);
  if(accent!=='blue')assert.ok(html.includes('--mantine-primary-color-filled: var(--mantine-color-'+accent+'-filled)'),'the provider resolves primary accents through Mantine');
  for(const colour of ['blue','yellow','red','green','violet'])assert.ok(html.includes('color:var(--mantine-color-'+colour+'-light-color)'),'semantic '+colour+' status survives '+scheme+'/'+accent);
  assert.equal((html.match(/data-activity-queue-item/g)||[]).length,5);assert.equal((html.match(/aria-label="Show details"/g)||[]).length,5,'one Details control survives each appearance');assert.ok(!html.includes('data-status-colour'),'appearance does not restore removed Activity icon squares');
  assert.ok(html.includes('Fixture source')&&html.includes('Fixture progress'),'queue identity and progress remain visible');assert.ok(!html.includes('Full outcome'),'verbose outcomes remain collapsed');
  assert.match(html,/<section[^>]*data-download-fill="true"/);assert.match(html,/<footer[^>]*margin-top:auto/,'intake actions remain at the common footer');assert.match(html,/min-width:160px;height:calc\(2\.75rem/);assert.ok(html.includes('Ready to start'),'intake feedback is preserved');
  selected={scheme,accent,background:false};const plainHtml=renderToStaticMarkup(h(Provider,null,h(Probe)));assert.ok(!plainHtml.includes('data-theme-background'),'switching off removes the decorative layer');assert.ok(plainHtml.includes('data-selected-scheme="'+scheme+'"')&&plainHtml.includes('data-selected-accent="'+accent+'"'),'plain background keeps the theme');
  assert.match(html,/stroke="var\(--mantine-primary-color-filled\)"/,'download graph follows the selected primary accent');assert.match(html,/stroke="var\(--mantine-color-violet-5\)"[^>]*stroke-dasharray="4 2"/,'the dashed upload series remains distinct');
 }
}
const guide=fs.readFileSync(__dirname+'/src/Guide.tsx','utf8');assert.match(guide,/day===live\.today\?'var\(--mantine-primary-color-light-color\)'/,'the current Guide day follows the primary accent');
for(const file of ['components/ActivityRow.module.css','GuideTimeline.module.css']){const source=fs.readFileSync(__dirname+'/src/'+file,'utf8');assert.doesNotMatch(source,/(?:background(?:-color)?|color)\s*:\s*(?:#[0-9a-fA-F]{3,8}\b|white\b|black\b|rgba?\()/,'owned layout surfaces use scheme-aware Mantine tokens: '+file);}
console.log('Owned appearance: actual global provider renders Activity, intake and network surfaces in all 24 stock appearances at phone/desktop widths; semantic status colours, identity, progress, Details and common footer remain intact.');

const backdrop=fs.readFileSync(__dirname+'/src/app/AppearanceBackdrop.module.css','utf8');assert.ok(backdrop.includes('--mantine-primary-color-filled'));assert.ok(!/url\(|animation|background-attachment/.test(backdrop),'theme backgrounds make no image requests or animated/fixed-attachment repaints');assert.match(backdrop,/pointer-events:none/,'background never intercepts controls');
