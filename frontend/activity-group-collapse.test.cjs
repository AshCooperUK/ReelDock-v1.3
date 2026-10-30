const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),runtime=require('react/jsx-runtime'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const h=React.createElement,source=fs.readFileSync(__dirname+'/src/components/ActivityGroup.tsx','utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const preferences=new Map();
const storage={getItem:key=>preferences.get(key)??null,setItem:(key,value)=>preferences.set(key,value)};
function load(react=React,localStorage=storage,core=Mantine){const out={};vm.runInNewContext(code,{exports:out,localStorage,require:name=>name==='react'?react:name==='react/jsx-runtime'?runtime:name==='@mantine/core'?core:require(name)});return out.default;}
const Group=load(),key=id=>'reeldock-activity-group-'+id+'-collapsed';
const titles=[['in-progress','In progress'],['waiting','Waiting'],['attention','Needs attention'],['completed','Completed']];
for(const width of [320,768,1440])for(const scheme of ['light','dark']){
 for(const [id,title] of titles){
  preferences.delete(key(id));
  const props={id,title,count:1740,actions:h('button',{type:'button','aria-label':'Clear '+title+' section'},'Clear section')};
  const render=()=>renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:scheme},h('div',{style:{width}},h(Group,props,h('p',null,'Fixture row retained while open')))));
  const open=render();
  assert.match(open,new RegExp('aria-label="Collapse '+title+' activity"[^>]*aria-expanded="true"[^>]*aria-controls="[^"]+"'));
  const disclosure=open.match(/<button\b[^>]*aria-expanded="true"[^>]*>/)?.[0];assert.ok(disclosure?.includes('type="button"')&&disclosure.includes('min-height:calc(2.75rem'),'the disclosure is a native 44px button');
  assert.ok(open.includes('Fixture row retained while open'));
  preferences.set(key(id),'true');
  const closed=render();
  assert.match(closed,new RegExp('aria-label="Expand '+title+' activity"[^>]*aria-expanded="false"'));
  assert.match(closed,/<div[^>]*id="[^"]+"[^>]*hidden=""/,'the controlled content exists and is hidden');
  assert.ok(!closed.includes('Fixture row retained while open'),'collapsed groups avoid mounting all queue rows');
  assert.ok(closed.includes('1740')&&closed.includes(title),'heading and current count remain visible');
  assert.ok(closed.includes('aria-label="Clear '+title+' section"'),'section actions remain available while collapsed');
  assert.ok(!/<button\b[^>]*>[\s\S]*?<button\b/.test(closed.split('</button>')[0]),'disclosure never nests the Clear button');
 }
}
preferences.clear();
const state=new Map();let activeId;
const hooks={...React,useId:()=> 'controlled-'+activeId,useState:initial=>{const id=activeId;if(!state.has(id))state.set(id,typeof initial==='function'?initial():initial);return[state.get(id),next=>state.set(id,typeof next==='function'?next(state.get(id)):next)];}};
const core=Object.fromEntries(['Badge','Box','Divider','Group','Paper','Text','UnstyledButton'].map(name=>[name,name]));
const Interactive=load(hooks,storage,core),nodes=value=>Array.isArray(value)?value.flatMap(nodes):value&&typeof value==='object'?[value,...nodes(value.props?.children)]:[];
function render(id,title){activeId=id;return Interactive({id,title,count:2,children:h('p',null,'Fixture body')});}
function control(tree){return nodes(tree).find(node=>node.type==='UnstyledButton');}
let tree=render('waiting','Waiting');assert.equal(control(tree).props['aria-expanded'],true);control(tree).props.onClick();
tree=render('waiting','Waiting');assert.equal(control(tree).props['aria-expanded'],false);assert.equal(preferences.get(key('waiting')),'true');
assert.equal(control(render('in-progress','In progress')).props['aria-expanded'],true,'groups toggle independently');
state.delete('waiting');assert.equal(control(render('waiting','Waiting')).props['aria-expanded'],false,'the browser preference survives a group remount');
control(render('waiting','Waiting')).props.onClick();assert.equal(preferences.get(key('waiting')),'false');assert.equal(control(render('waiting','Waiting')).props['aria-expanded'],true);
preferences.set(key('completed'),'invalid');state.delete('completed');assert.equal(control(render('completed','Completed')).props['aria-expanded'],true,'invalid saved values default expanded');
const blocked=load(hooks,{getItem(){throw Error('blocked');},setItem(){throw Error('blocked');}},core);activeId='blocked';tree=blocked({id:'attention',title:'Needs attention',count:1,children:'Fixture'});assert.equal(control(tree).props['aria-expanded'],true);assert.doesNotThrow(()=>control(tree).props.onClick());assert.equal(state.get('blocked'),true,'blocked storage still allows collapse');
const integration=fs.readFileSync(__dirname+'/src/LibraryPages.tsx','utf8');
for(const [id]of titles)assert.ok(integration.includes("id:'"+id+"'"),'each queue type has a stable preference identity');
assert.match(integration,/<ActivityGroup key=\{group.id\} id=\{group.id\} title=\{group.title\} count=\{group.entries.length\}/);
console.log('Activity groups: real Mantine native 44px disclosures at 320/768/1440 light/dark, controlled visibility, live counts, separate Clear actions, independent persisted choices and blocked-storage fallback passed.');
