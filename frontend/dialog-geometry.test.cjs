const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const {createRequire}=require('node:module'),React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),mantine=require('@mantine/core');
const postcss=createRequire(require.resolve('vite'))('postcss');
function uiFor(phone) {
 const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/ui.tsx','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{
 exports:out,document:{activeElement:null},HTMLElement:class{},require:name=>name==='../ScrollToTop'?{default:()=>null}:name==='../notifications-state'?{registerNotificationHost:()=>()=>{}}:name==='@mantine/hooks'?{useMediaQuery:()=>phone}:require(name)});return out;
}
const themeOut={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/mantineTheme.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:themeOut,require:()=>mantine});
const theme=mantine.mergeMantineTheme(mantine.mergeMantineTheme(mantine.DEFAULT_THEME,themeOut.mantineTheme),mantine.createTheme({components:{ModalRoot:mantine.Modal.Root.extend({defaultProps:{withinPortal:false,keepMounted:true,transitionProps:{duration:0}}})}}));
const cases=[['media-dialog compact-settings','64rem'],['settings-dialog','64rem'],['media-dialog detail-sheet channel-options-dialog','64rem'],['epg-detail-dialog','64rem'],['help-dialog','md'],['media-dialog reset-reeldock-dialog','md'],['media-dialog favourites-picker','64rem'],['directory-dialog','md']];
for(const phone of [false,true]) {
 const ui=uiFor(phone);
 for(const [className,size] of cases) {
  const html=renderToStaticMarkup(React.createElement(mantine.MantineProvider,{forceColorScheme:'dark',theme},React.createElement(ui.Dialog,{open:true},React.createElement(ui.DialogContent,{className},React.createElement('header',{className:'media-dialog-header'},React.createElement(ui.DialogTitle,null,'Fixture dialog')),React.createElement('div',{className:'media-dialog-body'},'Body')))));
  const inner=html.match(/<div\b[^>]*class="[^"]*mantine-Modal-inner[^>]*>/)?.[0];
  const content=html.match(/<section\b[^>]*role="dialog"[^>]*>/)?.[0];
  assert.ok(inner&&content,'actual Mantine modal markup renders');
  assert.doesNotMatch(inner,/rd-dialog-content|media-dialog|settings-dialog|detail-sheet|epg-detail-dialog/,'custom content classes never leak onto the fixed positioning inner');
  assert.match(inner,/style="bottom:var\(--reeldock-cookie-bar-height, 0px\)"/,'only the measured warning reservation adjusts the fixed inner; zero preserves the viewport');
  assert.match(content,/mantine-Modal-content/);
  assert.equal((html.match(/data-ui-dialog-scroll=/g)||[]).length,1,'one scroll viewport per detail surface');
  assert.match(html,/min-height:0;overflow-y:auto;flex:1/,'body can shrink and scroll within viewport');
  assert.match(content,/position:relative/,'close and scroll controls anchor to the content, not the viewport');
  assert.match(content,/display:flex;flex-direction:column/);
  assert.match(content,/min-width:0/);
  assert.doesNotMatch(content,/resize:(?:both|horizontal|vertical)/,'dialogs do not add a resize handle');
  assert.match(content,phone?/height:calc\(100dvh - var\(--reeldock-cookie-bar-height, 0px\)\)/:/max-height:calc\(100dvh - 32px - var\(--reeldock-cookie-bar-height, 0px\)\)/,'content is bounded above the actual warning bar');
  assert.doesNotMatch(content,/(?:^|[;" ])(?:width|max-width|transform):/,'framework owns dialog width and positioning');
  assert.ok(html.includes('--modal-size:'+(size==='md'?'var(--modal-size-md)':size)),className+' uses shared standard size');
  assert.ok(html.includes('--modal-x-offset:calc(1rem * var(--mantine-scale))'),'offset is a valid numeric CSS length, not a spacing-name token');
  assert.match(html,/aria-label="Close window"/,'every dialog keeps a close control');
  const close=html.match(/<button[^>]*aria-label="Close window"[^>]*>/)?.[0];assert.match(close,/--cb-icon-size:calc\(1\.25rem/,'the glyph is20px inside the44px target');assert.match(close,/background-color:var\(--mantine-color-default\)/,'a solid framework surface separates Close from artwork');assert.match(close,/border:1px solid var\(--mantine-color-default-border\)/);assert.match(close,/right:calc\(1\.5rem/,'close control is inset from the scrollbar');
  if(phone) assert.match(content,/data-full-screen="true"/,'phone dialogs use Mantine fullScreen');
  else assert.doesNotMatch(content,/data-full-screen/);
 }
}

// Intake tabs share one stable frame, while ordinary media sheets keep natural height.
for(const phone of [false,true]){
 const ui=uiFor(phone);
 const html=renderToStaticMarkup(React.createElement(mantine.MantineProvider,{forceColorScheme:'dark',theme},React.createElement(ui.Dialog,{open:true},React.createElement(ui.DialogContent,{stableHeight:true},'Long intake content'))));
 const content=html.match(/<section\b[^>]*role="dialog"[^>]*>/)?.[0];
 assert.match(content,phone?/height:calc\(100dvh - var\(--reeldock-cookie-bar-height, 0px\)\)/:/height:min\(44rem, calc\(100dvh - 32px - var\(--reeldock-cookie-bar-height, 0px\)\)\)/);
 assert.equal((html.match(/data-ui-dialog-scroll=/g)||[]).length,1);
 assert.ok(html.includes('overflow-y:auto;flex:1;display:flex;flex-direction:column'),'stable frame has one growing scroll owner');
}
assert.doesNotMatch(fs.readFileSync(__dirname+'/src/app/base.css','utf8'),/scrollbar-gutter/,'Mantine alone owns overlay scrollbar compensation');

const roots=/\.(?:dialog-overlay|dialog-content|media-dialog|settings-dialog|help-dialog|channel-options-dialog|channel-confirm-dialog|epg-detail-dialog|detail-sheet|movie-options-dialog|playlist-naming-dialog|reset-reeldock-dialog|directory-dialog|favourites-picker|mobile-nav-dialog|mobile-widgets-dialog|add-download-dialog|activity-dialog)(?=$|[.:#\[])/;
const geometry=/^(?:position|inset(?:-.+)?|top|right|bottom|left|transform|translate|resize|width|min-width|max-width|height|min-height|max-height|display|flex(?:-.+)?|grid-template(?:-.+)?|overflow(?:-x|-y)?|border-radius|margin(?:-.+)?|padding(?:-.+)?)$/;
function inspect(dir) {for(const entry of fs.readdirSync(dir,{withFileTypes:true})) {const file=path.join(dir,entry.name);if(entry.isDirectory())inspect(file);else if(file.endsWith('.css')&&!file.endsWith('mantine-bridge.css'))postcss.parse(fs.readFileSync(file,'utf8')).walkRules(rule=>{if(rule.selectors.some(selector=>roots.test(selector.trim().split(/\s+/).at(-1))))for(const decl of rule.nodes.filter(node=>node.type==='decl'))assert.ok(!geometry.test(decl.prop),'legacy root geometry cannot override Mantine: '+file+' '+rule.selector+' '+decl.prop);});}}
inspect(path.join(__dirname,'src'));
console.log('Shared dialogs: real Mantine fixed inner remains clean, content-only layout, framework sizes/fullScreen/offsets, close control and no legacy root geometry.');

// Capture the real-rendered shared content handler. A closed Mantine combobox
// consumes Escape internally, so the dialog must handle that state in capture.
let capturedContent,closes=0;
const captureCore={...mantine,Modal:{...mantine.Modal,Content:props=>{capturedContent=props;return React.createElement(mantine.Modal.Content,props);}}};
const captureOut={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/ui.tsx','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{
 exports:captureOut,document:{activeElement:null},HTMLElement:class{},require:name=>name==='@mantine/core'?captureCore:name==='../ScrollToTop'?{default:()=>null}:name==='../notifications-state'?{registerNotificationHost:()=>()=>{}}:name==='@mantine/hooks'?{useMediaQuery:()=>false}:require(name)});
renderToStaticMarkup(React.createElement(mantine.MantineProvider,{forceColorScheme:'dark',theme},React.createElement(captureOut.Dialog,{open:true,onOpenChange:value=>{if(!value)closes++;}},React.createElement(captureOut.DialogContent,{className:'settings-dialog'},'Settings'))));
function escapeEvent({expanded='false',role='combobox',guarded=false,prevented=false}={}){return{key:'Escape',defaultPrevented:prevented,target:{getAttribute:name=>name==='role'?role:name==='aria-expanded'?expanded:null,closest:()=>guarded?{}:null},preventDefault(){this.defaultPrevented=true;},stopPropagation(){this.stopped=true;}};}
let event=escapeEvent({expanded:'true'});capturedContent.onKeyDownCapture(event);assert.equal(closes,0,'an open menu keeps the first Escape');assert.equal(event.stopped,undefined);
event=escapeEvent();capturedContent.onKeyDownCapture(event);assert.equal(closes,1,'a closed menu gives the next Escape to its dialog');assert.equal(event.defaultPrevented,true);assert.equal(event.stopped,true);
for(const props of [{guarded:true},{prevented:true},{role:'button'},{expanded:null}]){capturedContent.onKeyDownCapture(escapeEvent(props));assert.equal(closes,1,'inline confirmation/notification and existing non-combobox handlers retain ownership');}

// A cookie link can close global Add download while its header opener remains
// mounted. Neither the exit transition nor the queued cleanup may steal focus
// back from the Cookies region; ordinary closes must still restore the opener.
function openerLifecycle(settingsNavigation){
 const target=new EventTarget(),frames=[],cleanups=[],changes=[];let context,focuses=0;
 class Element{isConnected=true;closest(){return null;}focus(){focuses++;document.activeElement=this;}}
 const opener=new Element(),cookies=new Element(),document={activeElement:opener,body:new Element(),querySelector:()=>null};
 const fakeReact={...React,useContext:()=>context,useState:initial=>[initial,()=>{}],useRef:initial=>({current:initial}),useCallback:fn=>fn,useEffect:run=>{const cleanup=run();if(cleanup)cleanups.push(cleanup);}};
 const owned={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/ui.tsx','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:owned,HTMLElement:Element,document,window:target,Event,requestAnimationFrame:fn=>frames.push(fn),require:name=>name==='react'?fakeReact:name==='@mantine/hooks'?{useMediaQuery:()=>false}:name==='../ScrollToTop'?{default:()=>null}:name==='../notifications-state'?{registerNotificationHost:()=>()=>{}}:require(name)});
 context=owned.Dialog({open:true,onOpenChange:value=>changes.push(value)}).props.value;
 const modal=owned.DialogContent({children:'Global Add download'});
 if(settingsNavigation){target.dispatchEvent(new Event('reeldock-notification-settings'));document.activeElement=cookies;}else modal.props.onClose();
 modal.props.onExitTransitionEnd();for(const cleanup of cleanups)cleanup();for(const frame of frames)frame();
 assert.deepEqual(changes,[false]);assert.equal(focuses,settingsNavigation?0:1);
 assert.equal(document.activeElement,settingsNavigation?cookies:opener,settingsNavigation?'delayed exit and cleanup preserve explicit Cookies navigation focus':'ordinary modal close restores its persistent opener');
}
openerLifecycle(false);openerLifecycle(true);
console.log('Dialog focus: ordinary close restores its opener; explicit Cookies navigation suppresses both delayed restoration paths.');
