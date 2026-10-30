const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}}),out={},calls=[],dismissals=[];
let fail=false,states=[],cursor=0;
const imports={react:{lazy:()=>"LazyPanel",Suspense:"Suspense",useEffect:()=>{},useState:v=>{const i=cursor++;if(!(i in states))states[i]=v;return [states[i],v=>states[i]=typeof v==='function'?v(states[i]):v];}},'./components/ui':{Button:'Button'},'./notifications':{NotificationMessage:'NotificationMessage',dismissNotification:async(id,runCallback)=>dismissals.push({id,runCallback})},'./ChannelIdentity':{default:'ChannelIdentity'},'lucide-react':{Check:'Check',CircleAlert:'CircleAlert',X:'X'},'./api':{api:async(...args)=>{calls.push(args);if(fail)throw Error('Retry failed');}},'./downloadFeed':{refreshDownloads:async()=>calls.push(['refresh'])}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/ActivityNotifications.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,Error,Event,window:{dispatchEvent:()=>{}},require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'fragment'}:imports[n]||{}});
function nodes(t){return Array.isArray(t)?t.flatMap(nodes):t&&typeof t==='object'?[t,...nodes(t.props?.children),...nodes(t.props?.actions)]:[];}
function render(props){cursor=0;return nodes(out.AlertTile(props));}
const textOf=value=>Array.isArray(value)?value.map(textOf).join(''):typeof value==='string'?value:typeof value==='object'&&value?textOf(value.props?.children):'';
const button=(tree,text)=>tree.find(n=>n.type==='Button'&&textOf(n.props.children)===text),flush=()=>new Promise(setImmediate);
(async()=>{
 const props={csrf:'fixture-csrf',alert:{id:'alert',title:'Failed',message:'Interrupted',retry:{id:'abcdefghijk'}}};
 let tree=render(props),notice=tree.find(n=>n.type==='NotificationMessage'&&n.props.id==='activity-alert:alert');
 assert.equal(notice.props.message,'Interrupted');assert.equal(notice.props.kind,'error');
 assert.ok(button(tree,'Retry'));button(tree,'Retry').props.onClick();await flush();
 assert.equal(calls[0][0],'/api/downloads');assert.equal(calls[0][2].action,'retry');assert.equal(calls[0][2].id,'abcdefghijk');assert.equal(calls[0][3],'fixture-csrf');assert.equal(calls[1][0],'refresh');
 assert.deepEqual(dismissals,[{id:'activity-alert:alert',runCallback:false}],'a successful retry retires the balloon without posting dismiss twice');assert.equal(render(props).length,0);
 fail=true;states=[];calls.length=0;dismissals.length=0;tree=render(props);button(tree,'Retry').props.onClick();await flush();tree=render(props);
 assert.ok(tree.some(n=>n.type==='NotificationMessage'&&n.props.kind==='error'&&n.props.message==='Retry failed'),'retry failure remains visible through the shared error producer');assert.ok(button(tree,'Retry'),'retry remains available after failure');assert.equal(dismissals.length,0);
 notice=tree.find(n=>n.props.id==='activity-alert:alert');await assert.rejects(notice.props.onDismiss(),/Could not dismiss this alert/,'persistent X rejects on a failed API call so the shared provider keeps the original alert');assert.equal(calls.at(-1)[0],'/api/activity/dismiss');assert.equal(calls.at(-1)[2].id,'alert');assert.equal(calls.at(-1)[3],'fixture-csrf');
 fail=false;await render(props).find(n=>n.props.id==='activity-alert:alert').props.onDismiss();assert.equal(render(props).length,0,'a successful persistent dismiss removes the alert');
 states=[];tree=render({...props,alert:{id:'other',title:'Other',message:'Failure'}});assert.equal(!!button(tree,'Retry'),false);
 states=[];calls.length=0;tree=render({...props,local:true});await tree.find(n=>n.props.id==='activity-alert:alert').props.onDismiss();assert.equal(calls.length,0,'local feed errors never write a persistent dismissal');
 states=[];calls.length=0;const subscription={...props,alert:{id:'subscription',title:'Restore?',message:'Add back?',channel:{id:'channel',name:'Creator'},subscription:'return-token'}};tree=render(subscription);button(tree,'Add back').props.onClick();await flush();assert.equal(calls[0][0],'/api/activity/subscription-return');assert.equal(calls[0][2].accept,true);assert.equal(calls[0][2].channel,'channel');assert.equal(calls[0][2].subscription,'return-token');assert.equal(calls[0][3],'fixture-csrf');
 states=[];calls.length=0;tree=render(subscription);button(tree,'Keep removed').props.onClick();await flush();assert.equal(calls[0][2].accept,false,'Keep removed remains an explicit subscription decision');
 console.log('Download alerts: shared balloon actions, CSRF retry, failure recovery, async persistent/local X and both subscription choices passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
