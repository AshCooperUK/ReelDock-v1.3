const sharedSelect=require('./select-field-test-helper.cjs')();
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),Mantine=require('@mantine/core'),jsxRuntime=require('react/jsx-runtime');
const {renderToStaticMarkup}=require('react-dom/server');const h=React.createElement;
let over=false;
function load(name,extra={}){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+name,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:key=>extra[key]||({'../components/SelectField':sharedSelect,'@mantine/core':Mantine,react:{...React,useState:()=>[over,value=>over=value],useRef:value=>({current:value})},'react/jsx-runtime':jsxRuntime,'lucide-react':require('lucide-react')}[key])||{}});return out;}
const {mantineTheme}=load('mantineTheme.ts');
const render=children=>renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:'dark',theme:mantineTheme},children));
const chosen=[],file=new File(['x'.repeat(2048)],'fixture.nzb',{type:'application/x-nzb'}),second=new File(['x'],'second.nzb');
const DropZone=load('components/DropZone.tsx').default;
const props={label:'NZB file',accept:'.nzb,application/x-nzb',file:null,onFile:value=>chosen.push(value)};
let root=DropZone(props),control=root.props.children;
assert.equal(control.type,Mantine.FileInput,'actual framework file control replaces the unstyled trigger');
assert.equal(control.props.multiple,false);assert.equal(control.props.accept,props.accept);assert.equal(control.props['aria-label'],props.label);
let html=render(root),native=html.match(/<input[^>]*type="file"[^>]*>/)?.[0];
assert.ok(native);assert.match(native,/accept="\.nzb,application\/x-nzb"/);assert.doesNotMatch(native,/multiple/);
assert.match(html,/min-height:44px/,'file picker inherits the shared control height');
assert.match(html,/Choose file or drop it here/);assert.match(html,/type="button"/,'file picker never submits an enclosing download form');
let resets=0;control.props.resetRef.current=()=>resets++;
control.props.onChange(file);assert.equal(chosen.at(-1),file,'browse passes the original single File');assert.equal(resets,1,'reset native selection so the same file can be selected again');
let prevented=0,stopped=0;
root.props.onDragOver({preventDefault(){prevented++;}});root=DropZone(props);assert.equal(root.props.children.props.variant,'filled');
root.props.onDragLeave();assert.equal(DropZone(props).props.children.props.variant,'default');
root.props.onDrop({preventDefault(){prevented++;},stopPropagation(){stopped++;},dataTransfer:{files:[file,second]}});
assert.equal(chosen.at(-1),file);assert.equal(chosen.length,2,'dropping multiple files selects the first without queueing another');assert.equal(stopped,1);
const beforeEmpty=chosen.length;root.props.onDrop({preventDefault(){prevented++;},stopPropagation(){},dataTransfer:{files:[]}});assert.equal(chosen.length,beforeEmpty);
root=DropZone({...props,file});control=root.props.children;html=render(root);
assert.match(html,/fixture\.nzb/);assert.match(html,/2 KB/);assert.match(html,/choose or drop another file to replace it/);
const clear=html.match(/<button[^>]*aria-label="Remove fixture.nzb"[^>]*>/)?.[0];assert.ok(clear,'clear has the specific filename');
assert.match(clear,/width:44px/);assert.match(clear,/height:44px/);assert.equal(control.props.rightSectionWidth,44);
control.props.onChange(null);assert.equal(chosen.at(-1),null,'framework clear passes null to the caller');
root=DropZone({...props,file,disabled:true});control=root.props.children;html=render(root);
assert.equal(control.props.disabled,true);assert.equal(control.props.fileInputProps.disabled,true);assert.equal(control.props.clearButtonProps.disabled,true);
const beforeDisabled=chosen.length;root.props.onDragOver({preventDefault(){prevented++;}});
root.props.onDrop({preventDefault(){prevented++;},stopPropagation(){},dataTransfer:{files:[second]}});
control.props.onChange(second);control.props.onChange(null);
assert.equal(chosen.length,beforeDisabled,'disabled browse, clear and drop cannot replace the selection');assert.ok(prevented>=5,'drops always prevent browser navigation');
assert.match(html.match(/<input[^>]*type="file"[^>]*>/)[0],/disabled=""/);

const nodes=value=>Array.isArray(value)?Array.from(value).flatMap(nodes):value&&typeof value==='object'?[value,...nodes(value.props?.children)]:[];
const mocks={
 '../components/ui':{Button:({variant,...props})=>h(Mantine.Button,{...props,variant:variant==='secondary'?'default':'filled'})},
 '../SettingsUI':{Toggle:props=>h('input',{type:'checkbox','aria-label':props.title,checked:props.value,onChange:()=>props.onChange(!props.value)})},
 '../notifications':{NotificationMessage:({message,kind})=>message?h('p',{'data-notice':kind},message):null},
};
let downloads=0,fallback;
const panel=load('downloadUI/DownloadPanel.tsx',mocks).default({children:'Engine file controls',quality:'1080',onQuality(){},fallback:true,onFallback:value=>fallback=value,onDownload:()=>downloads++,status:'Accepted',error:'Fixture error',actions:h('button',{type:'button'},'Extra action')});
assert.equal(panel.type,Mantine.Paper);assert.equal(panel.props.component,'section');assert.equal(panel.props.withBorder,true);assert.equal(panel.props.radius,'md');assert.equal(panel.props.p,'md');assert.equal(panel.props['aria-label'],'Download options');
const stack=panel.props.children;assert.equal(stack.type,Mantine.Stack);assert.equal(stack.props.gap,'md');
const actionGroup=nodes(panel).find(node=>node.type===Mantine.Group);assert.ok(actionGroup);
const action=nodes(actionGroup).find(node=>typeof node.props?.onClick==='function');action.props.onClick();assert.equal(downloads,1);
const toggle=nodes(panel).find(node=>node.props?.title==='Allow lower quality');toggle.props.onChange(false);assert.equal(fallback,false);
html=render(panel);assert.match(html,/<section[^>]*aria-label="Download options"/);assert.match(html,/<h3/);assert.match(html,/Engine file controls|Extra action/);assert.match(html,/Accepted/);assert.match(html,/Fixture error/);
const compact=load('downloadUI/DownloadPanel.tsx',mocks).default({compact:true});assert.equal(compact.props['data-compact'],true);assert.ok(!nodes(compact).some(node=>node.type===Mantine.Title),'compact mode keeps its heading omitted');
console.log('File picker: real Mantine single browse/drop/clear, accept hint, disabled handling, 44px targets and framework download surface/actions passed.');

const filled=load('downloadUI/DownloadPanel.tsx',mocks).default({fill:true,children:h(Mantine.Text,null,'Long preview content'),actions:h(Mantine.Button,{type:'submit',style:{minWidth:160,height:44}},'Scan')});assert.equal(filled.props['data-download-fill'],true);assert.equal(filled.props.style.flex,'1 0 auto');assert.equal(filled.props.children.props.style.flex,'1 0 auto');const footer=nodes(filled).find(node=>node.type===Mantine.Group);assert.equal(footer.props.component,'footer');assert.equal(footer.props.mt,'auto');assert.equal(footer.props.justify,'flex-end');assert.equal(footer.props.style.flexShrink,0);const filledHtml=render(filled);assert.match(filledHtml,/<footer/);assert.match(filledHtml,/margin-top:auto/);assert.match(filledHtml,/min-width:160px;height:calc\(2\.75rem/);assert.doesNotMatch(filledHtml,/overflow-y:(auto|scroll)|max-height:/,'intake content delegates all vertical scrolling to the modal');const compactFill=load('downloadUI/DownloadPanel.tsx',mocks).default({compact:true,fill:true});assert.equal(compactFill.props['data-download-fill'],undefined,'compact media download controls never adopt intake fill geometry');assert.equal(compactFill.props.style,undefined);
console.log('Intake fill: flexible growth, one bottom-aligned160x44 primary and no nested scroll, isolated from compact media panels.');
