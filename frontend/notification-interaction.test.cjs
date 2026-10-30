const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),jsxRuntime=require('react/jsx-runtime'),Mantine=require('@mantine/core');
const {renderToStaticMarkup}=require('react-dom/server');
const source=fs.readFileSync(__dirname+'/src/notifications.tsx','utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const item={id:'fixture-notice',kind:'warning',title:'Needs attention',message:'Keep this notice above the underlying media controls.',revision:1};
const out={},dismissals=[];
const modules={
 react:{...React,useState:value=>[value,()=>{}],useEffect:()=>{},useLayoutEffect:()=>{},useRef:value=>({current:value}),useSyncExternalStore:()=>({items:[item],host:null})},
 'react/jsx-runtime':jsxRuntime,'react-dom':{createPortal:content=>content},'@mantine/core':Mantine,
 'lucide-react':require('lucide-react'),
 './app/Notifications.module.css':{default:{viewport:'viewport',card:'card'}},
 './notifications-state':{dismissNotification:id=>dismissals.push(id),emptyNotifications:{items:[]},notificationSnapshot:()=>({items:[item]}),subscribeNotifications:()=>()=>{},notify:()=>{},updateNotification:()=>{}},
};
vm.runInNewContext(code,{exports:out,require:name=>modules[name],document:{body:{}},window:{}});
const viewport=out.NotificationCenter();
let stopped=false;viewport.props.onPointerDown({stopPropagation(){stopped=true;}});
assert.equal(stopped,true,'notification pointer events stay within the viewport');
const html=renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:'dark'},viewport));
const root=html.match(/<div\b[^>]*data-notification-id="fixture-notice"[^>]*>/)?.[0];
assert.ok(root,'real Mantine Notification renders the item identity on its root');
assert.match(root,/pointer-events:auto/,'notification explicitly restores hit testing under the transparent viewport');
assert.match(root,/flex-shrink:0/,'stacked notices retain their own clickable area');
assert.match(html,/aria-label="Dismiss Needs attention notification"/,'close control stays accessible');
const css=fs.readFileSync(__dirname+'/src/app/Notifications.module.css','utf8');
const viewportStyle=css.match(/\.viewport\s*\{([^}]+)\}/)?.[1];
assert.match(viewportStyle,/pointer-events\s*:\s*none/,'unused viewport space remains transparent to pointer events');
// Exercise the actual Notification onClose callback rather than only its label.
const balloon=viewport.props.children[0];
const notification=balloon.type(balloon.props);
notification.props.onClose();assert.deepEqual(dismissals,['fixture-notice']);
console.log('Notifications: real Mantine SSR restores pointer hit testing, retains close target, prevents viewport propagation and dismisses the correct item.');
