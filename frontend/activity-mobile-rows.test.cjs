const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),runtime=require('react/jsx-runtime'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const h=React.createElement,source=fs.readFileSync(__dirname+'/src/components/ListRow.tsx','utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const status={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/ActivityStatus.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:status,require});
let expanded=false;
function load(phone,real){
 const out={};
 function OpenMenu(props){return h(Mantine.Menu,{...props,opened:true,withinPortal:false,transitionProps:{duration:0}},props.children);}
 for(const part of ['Target','Dropdown','Item'])OpenMenu[part]=Mantine.Menu[part];
 const FakeMenu=Object.assign(()=>null,{Target:'MenuTarget',Dropdown:'MenuDropdown',Item:'MenuItem'});
 const core=real?{...Mantine,Menu:OpenMenu}:{Box:'Box',Paper:'Paper',Group:'Group',Stack:'Stack',Text:'Text',Collapse:'Collapse',ActionIcon:'ActionIcon',Tooltip:'Tooltip',Menu:FakeMenu};
 let contextIndex=0;const contexts=[];
 const hooks=real?React:{...React,createContext:value=>{const context=React.createContext(value);contexts[contextIndex++]=context;return context;},useContext:context=>context===contexts[0]?phone:false,useId:()=> 'fixture-details',useState:()=>[expanded,value=>expanded=typeof value==='function'?value(expanded):value]};
 const Button=real?({variant,...props})=>h(Mantine.Button,{...props,h:44,variant:variant==='secondary'?'default':'filled'}):'Button';
 vm.runInNewContext(code,{exports:out,require:name=>name==='react'?hooks:name==='react/jsx-runtime'?runtime:name==='@mantine/core'?core:name==='@mantine/hooks'?{useMediaQuery:()=>phone}:name==='./ActivityStatus'?status:name==='./ActivityRow.module.css'?{default:{media:'fixture-media',title:'fixture-title',progress:'fixture-progress',details:'fixture-details'}}:name==='./ui'?{Button,cn:(...values)=>values.filter(Boolean).join(' ')}:require(name)});
 return out;
}
const calls=[];
const actions=[['Open Fixture film','open'],['Cancel','cancel'],['Retry','retry'],['Resume','resume'],['Pause','pause'],['Change metadata','metadata'],['Remove','remove']];
const meta=h(React.Fragment,null,h('span',{className:'lrow-kind'},'YouTube'),h('button',{className:'channel-identity','aria-label':'Creator details',onClick:()=>calls.push('channel')},'Creator channel'),h('span',{className:'lrow-note'},'A full provider message retained in details.'));
const props={media:h('img',{src:'/fixture.png',alt:''}),title:h('button',{type:'button','aria-label':'Open Fixture film',className:'video-title',onClick:()=>calls.push('open')},'Fixture film with a longer title'),badge:h(status.ActivityStatus,{state:'downloading'}),statusColour:status.activityStatusColour('downloading'),meta,
 progress:h('progress',{value:40,max:100,'aria-label':'Download progress'}),actions:h(React.Fragment,null,...actions.map(([label,key])=>h('button',{key,'aria-label':label,disabled:key==='retry',onClick:()=>calls.push(key)},label))),details:h('span',null,'Path: /fixture/media/film.mkv')};
const render=(phone,provider=true)=>{const mod=load(phone,true),row=h(mod.default,props);return renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:'dark'},provider?h(mod.CompactActivityRows,null,row):row));};
const mobile=render(true);
assert.match(mobile,/data-activity-card/);assert.match(mobile,/data-compact="true"/);assert.match(mobile,/aria-label="Actions for Fixture film with a longer title"/);
assert.ok(!mobile.includes('lrow-side'),'fresh cards do not depend on a legacy wrapping toolbar');
assert.match(mobile,/min-height:calc\(2\.75rem/);assert.match(mobile,/width:calc\(2\.75rem[^;]+;height:calc\(2\.75rem/);
for(const [label] of actions)assert.ok(mobile.includes('aria-label="'+label+'"'),'mobile menu retains '+label);
assert.match(mobile,/role="menuitem"[^>]*aria-label="Retry"[^>]*disabled=""|aria-label="Retry"[^>]*disabled=""/);
assert.ok(!mobile.includes('Path: /fixture/media/film.mkv'),'technical metadata stays collapsed until requested');
assert.ok(mobile.includes('Show details'));
const desktop=render(false);assert.ok(desktop.includes('data-activity-card')&&desktop.includes('mantine-Paper-root'));
assert.ok(!desktop.includes('data-compact="true"')&&desktop.includes('Actions for Fixture'),'desktop/tablet keep a quick action with Details and the remaining original actions in overflow');
for(const [label] of actions)assert.ok(desktop.includes('aria-label="'+label+'"'));
for(const html of [mobile,desktop]){
 assert.ok(html.indexOf('data-activity-status')<html.indexOf('data-status-colour'),'status precedes the media/content on phone, tablet and desktop');
 assert.equal((html.match(/data-activity-status-badge/g)||[]).length,1,'a tile shows its status once');
 const frame=html.match(/<div\b[^>]*data-status-colour="blue"[^>]*>/)?.[0];assert.ok(frame);
 assert.ok(frame.includes('border:2px solid var(--mantine-color-blue-light-color)'));
 assert.ok(frame.includes('padding:6px'),'requested padding stays inside the shared media frame');
 assert.ok(html.includes('color:var(--mantine-color-blue-light-color)'),'frame and status use the same resolved colour');
 assert.ok(html.includes('<img src="/fixture.png"'),'the icon/artwork remains inside the padded frame');
}
assert.ok(!render(true,false).includes('data-compact="true"'),'the compact provider governs responsive card scope');

const nodes=value=>Array.isArray(value)?Array.from(value).flatMap(nodes):value&&typeof value==='object'?[value,...nodes(value.props?.children)]:[];
const text=value=>typeof value==='string'?value:Array.isArray(value)?Array.from(value).map(text).join(''):value&&typeof value==='object'?text(value.props?.children):'';
const mod=load(true,false);let tree=mod.default(props);
let items=nodes(tree).filter(node=>node.type==='MenuItem');
assert.equal(items.filter(node=>node.props['aria-label']==='Open Fixture film').length,0,'the clickable title is the single route to the media sheet');props.title.props.onClick();
for(const [label,key] of actions){if(key==='open')continue;const item=items.find(node=>node.props['aria-label']===label);assert.ok(item);assert.equal(item.props.mih,44);if(key!=='retry')item.props.onClick();else assert.equal(item.props.disabled,true);}
assert.deepEqual(calls,['open','cancel','resume','pause','metadata','remove'],'all engine callbacks keep their original behavior');
const detailControl=nodes(tree).filter(node=>node.props?.['aria-label']==='Show details');assert.equal(detailControl.length,1,'exactly one explicit Details affordance');assert.equal(detailControl[0].props.size,44);assert.ok(!items.some(node=>/^(Show|Hide) details$/.test(text(node))),'overflow contains actions only');detailControl[0].props.onClick();tree=mod.default(props);
assert.ok(nodes(tree).some(node=>node.props?.id==='fixture-details'),'menu disclosure expands the same in-place details');
assert.ok(nodes(tree).some(node=>text(node)==='Path: /fixture/media/film.mkv'));
const creator=nodes(tree).find(node=>node.props?.['aria-label']==='Creator details');assert.ok(creator,'channel navigation is retained in expanded metadata');creator.props.onClick();assert.equal(calls.at(-1),'channel');
assert.equal(creator.props.className,'channel-identity');
const postcss=require('node:module').createRequire(require.resolve('vite'))('postcss');
const css=postcss.parse(fs.readFileSync(__dirname+'/src/components/ActivityRow.module.css','utf8'));
let mediaRule,detailsRule;css.walkRules(rule=>{if(rule.selector==='.media')mediaRule=rule;if(rule.selector==='.details button,.details a')detailsRule=rule;});
assert.equal(mediaRule.nodes.find(node=>node.prop==='overflow').value,'hidden','padded artwork cannot overflow its frame');
assert.equal(detailsRule.nodes.find(node=>node.prop==='min-height').value,'44px','all expanded controls remain touch sized');
assert.ok(!fs.existsSync(__dirname+'/src/library.css'),'replaced global Activity layout is removed');
const hide=nodes(tree).find(node=>node.props?.['aria-label']==='Hide details');assert.equal(hide.props['aria-expanded'],true);hide.props.onClick();assert.equal(expanded,false);
// Engine title-only open handlers (e.g. torrent trackers) become a menu action.
const titleOnly=mod.default({...props,actions:h('button',{onClick:()=>calls.push('resume')},'Resume')});
assert.ok(!nodes(titleOnly).some(node=>node.type==='MenuItem'&&/^Open Fixture/.test(text(node))),'interactive titles do not gain a duplicate automatic Open item');
console.log('Mobile Activity: compact real Mantine rows, one 44px menu, every engine action/disabled state, title opener, in-place full details/channel navigation and unchanged desktop scope passed.');

for(const html of [mobile,desktop]){assert.equal((html.match(/aria-label="Show details"/g)||[]).length,1,'one rendered Details button at every breakpoint');assert.doesNotMatch(html,/<button[^>]*role="menuitem"[^>]*aria-label="(?:Show|Hide) details"/,'the open overflow has no duplicate Details command');}
const noActions=load(true,false).default({...props,actions:undefined});assert.ok(!nodes(noActions).some(node=>node.props?.['aria-label']?.startsWith('Actions for ')),'a row without remaining actions has no empty overflow');assert.equal(nodes(noActions).filter(node=>node.props?.['aria-label']==='Show details').length,1);
