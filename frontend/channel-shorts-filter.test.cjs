const assert=require('node:assert/strict'),fs=require('node:fs');
const React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const {scene,cards,nodes,tick,item,browse,helper}=require('./channel-videos-test-helper.cjs');
const toggle=tree=>nodes(tree).find(node=>node.type==='Switch'&&node.props.label==='Show Shorts');
const moreButton=tree=>nodes(tree).find(node=>node.type==='Button'&&node.props.children==='Load more uploads');
const ids=tree=>cards(tree).map(card=>card.props.actions.props.item.videoId);
const known=(id,kind)=>item(id,{kind,metadata_pending:false,state:'published',date:'2026-10-25',duration:'12:34'});
const settle=async(s,data)=>{s.pending.shift().resolve(data);await tick();const tree=s.draw();s.h.commit();return tree;};
const change=(s,checked)=>{toggle(s.draw()).props.onChange({currentTarget:{checked}});const tree=s.draw();s.h.commit();return tree;};

(async()=>{
 for(const stored of [undefined,null,'','false','yes','1','TRUE','invalid']){
  const s=scene({stored}),tree=s.draw();assert.equal(toggle(tree).props.checked,false,'missing/invalid preferences hide Shorts');
  assert.equal(s.calls.length,0,'reading a display preference performs no API write');s.h.unmount();
 }
 for(const stored of ['true','false']){
  const s=scene({stored});assert.equal(toggle(s.draw()).props.checked,stored==='true');s.h.unmount();
 }
 const denied=scene({storageFailure:true});assert.equal(toggle(denied.draw()).props.checked,false);assert.doesNotThrow(()=>change(denied,true));assert.equal(toggle(denied.draw()).props.checked,true);denied.h.unmount();

 const s=scene(),opened=[];s.props.onOpen=video=>opened.push(video);s.draw();s.h.commit();
 const all=[known('normal','video'),known('short','shorts'),known('unknown','unknown'),known('unclassified',undefined)];
 let tree=await settle(s,{items:all,more:false,total:4});
 assert.deepEqual(ids(tree),['video-normal','video-unknown','video-unclassified'],'only exact known Shorts are hidden');
 const count=s.calls.length;
 tree=change(s,true);assert.deepEqual(ids(tree),all.map(video=>video.id));
 cards(tree)[1].props.title.props.onClick();assert.equal(opened[0],all[1],'restored cards keep their exact original identity/actions');
 tree=change(s,false);assert.deepEqual(ids(tree),['video-normal','video-unknown','video-unclassified']);
 tree=change(s,true);assert.deepEqual(ids(tree),all.map(video=>video.id));
 assert.equal(s.calls.length,count,'toggle on/off restores retained cards without GET/POST, reset or automation changes');
 assert.deepEqual(s.writes.map(write=>write.value),['true','false','true']);
 assert.equal(s.values.get(s.module.showShortsPreference),'true');
 assert.deepEqual(s.module.visibleChannelUploads(all,false).map(video=>video.id),['video-normal','video-unknown','video-unclassified']);
 assert.equal(s.module.visibleChannelUploads(all,true),all);
 s.h.unmount();

 // Classification may arrive later without changing an upload's id or date.
 const hydration=scene();hydration.draw();hydration.h.commit();const pending=known('pending','unknown');
 tree=await settle(hydration,{items:[pending],more:false,total:1});assert.deepEqual(ids(tree),[pending.id]);
 hydration.window.dispatchEvent(new CustomEvent(helper.channelRefreshEvent,{detail:{id:hydration.props.id}}));
 tree=await settle(hydration,{items:[{...pending,kind:'shorts'}],more:false,total:1});
 assert.deepEqual(ids(tree),[],'same-date/same-id known classification immediately applies');
 assert.match(JSON.stringify(tree),/Only Shorts/);assert.equal(moreButton(tree),undefined,'an exhausted filtered list has no dead Load more button');
 assert.deepEqual(ids(change(hydration,true)),[pending.id]);hydration.h.unmount();

 // A page that adds only hidden Shorts pauses the near-bottom chain. The
 // explicit continuation still advances by unfiltered rows, never visible count.
 const local=scene();local.draw();local.h.commit();tree=await settle(local,{items:[known(0,'video'),known(1,'video')],more:true,total:5});
 local.edge();assert.match(local.calls.at(-1).url,/offset=2&browse_cursor=0$/);
 tree=await settle(local,{items:[known(2,'shorts'),known(3,'shorts')],more:true,total:5});
 assert.deepEqual(ids(tree),['video-0','video-1']);const paused=local.calls.length;
 local.edge();local.edge();assert.equal(local.calls.length,paused,'no unbounded auto-chain across a filtered page');
 moreButton(tree).props.onClick();assert.match(local.calls.at(-1).url,/offset=4&browse_cursor=0$/);
 tree=await settle(local,{items:[known(4,'video')],more:false,total:5});
 assert.deepEqual(ids(tree),['video-0','video-1','video-4']);assert.equal(moreButton(tree),undefined);
 assert.equal(local.calls.filter(call=>call.body).length,0);assert.equal(ids(change(local,true)).length,5,'all filtered older rows remain retained');local.h.unmount();

 // Initial all-Shorts pages do not strand pagination or silently schedule
 // providers. One explicit action requests one page; a completed Shorts-only
 // provider page remains paused until another explicit continuation.
 const remote=scene();remote.draw();remote.h.commit();const shorts=Array.from({length:25},(_,i)=>known(i,'shorts'));
 tree=await settle(remote,{items:shorts.slice(0,15),more:false,total:15,browse:browse()});
 assert.deepEqual(ids(tree),[]);assert.ok(moreButton(tree));remote.edge();assert.equal(remote.calls.length,1);
 moreButton(tree).props.onClick();assert.deepEqual(JSON.parse(JSON.stringify(remote.calls.at(-1).body)),{action:'browse_uploads',cursor:'0'});
 tree=await settle(remote,{browse:browse('0',{state:'queued',refreshAfterMs:2000})});assert.equal(moreButton(tree).props.disabled,true);
 remote.poll();assert.match(remote.calls.at(-1).url,/offset=0&browse_cursor=0$/);
 tree=await settle(remote,{items:shorts.slice(0,15),more:true,total:25,browse:browse('0',{nextCursor:'25',page:{cursor:'0',items:shorts}})});
 const remotePaused=remote.calls.length;remote.edge();remote.edge();assert.equal(remote.calls.length,remotePaused);
 moreButton(tree).props.onClick();assert.match(remote.calls.at(-1).url,/offset=15&browse_cursor=0$/);
 tree=await settle(remote,{items:shorts.slice(15),more:false,total:25,browse:browse('0',{nextCursor:'25',page:{cursor:'0',items:shorts}})});
 assert.equal(remote.calls.filter(call=>call.body).length,1,'local catch-up and filtering never advance the provider cursor accidentally');
 moreButton(tree).props.onClick();assert.equal(remote.calls.at(-1).body.cursor,'25');
 remote.pending.shift().reject(Error('Provider unavailable'));await tick();tree=remote.draw();remote.h.commit();
 assert.ok(nodes(tree).some(node=>node.type==='NotificationMessage'&&node.props.kind==='error'));
 assert.ok(nodes(tree).some(node=>node.type==='Button'&&node.props.children==='Try again'));change(remote,true);change(remote,false);
 assert.equal(remote.calls.filter(call=>call.body).length,2,'toggling a failed filtered page never retries its POST');
 remote.h.unmount();assert.equal(remote.calls.at(-1).signal.aborted,true);

 // Cached provider replay can contain only Shorts already loaded from SQLite.
 // A surviving normal row must not let that duplicate-only hidden page chain.
 const duplicates=scene();duplicates.draw();duplicates.h.commit();const cachedShorts=shorts.slice(0,15);
 tree=await settle(duplicates,{items:[known('normal','video'),...cachedShorts],more:false,total:16,
  browse:browse('0',{nextCursor:'25',page:{cursor:'0',items:cachedShorts}})});
 assert.deepEqual(ids(tree),['video-normal']);duplicates.edge();duplicates.edge();
 assert.equal(duplicates.calls.length,1,'already-cached hidden Shorts also pause automatic provider paging');
 moreButton(tree).props.onClick();assert.equal(duplicates.calls.at(-1).body.cursor,'25','explicit continuation preserves the consumed provider cursor');
 tree=await settle(duplicates,{browse:browse('25',{nextCursor:'50',page:{cursor:'25',items:[known('new-normal','video')]}})});
 assert.deepEqual(ids(tree),['video-normal','video-new-normal']);assert.equal(duplicates.calls.at(-1).body.cursor,'50','a new visible normal row resumes the ordinary near-bottom continuation');
 assert.equal(duplicates.calls.filter(call=>call.body).length,2);duplicates.edge();assert.equal(duplicates.calls.filter(call=>call.body).length,2,'resumed paging still coalesces its active request');
 duplicates.h.unmount();

 const empty=scene();empty.draw();empty.h.commit();tree=await settle(empty,{items:[],more:false,total:0,browse:browse('0',{state:'complete',nextCursor:null,hasMore:false})});
 assert.deepEqual(ids(tree),[]);assert.equal(moreButton(tree),undefined);empty.edge();assert.equal(empty.calls.length,1);empty.h.unmount();

 const readonly=scene({readOnly:true});readonly.draw();readonly.h.commit();tree=await settle(readonly,{items:[known(1,'shorts')],more:false,total:1,browse:browse()});
 assert.equal(moreButton(tree),undefined);assert.doesNotMatch(JSON.stringify(tree),/Choose Load more/);readonly.edge();change(readonly,true);assert.equal(readonly.calls.filter(call=>call.body).length,0);readonly.h.unmount();

 // Missing Shorts classification must still respect the effective minimum.
 // These are the actual reported lengths, with no invented Shorts flag.
 const minimum=scene();minimum.draw();minimum.h.commit();
 const reports=[['tea','1:24'],['details','1:12'],['vault','1:31']].map(([id,duration])=>({...known(id,'unknown'),duration}));
 const long={...known('long','video'),duration:'41:32'},unknown={...known('no-length','unknown'),duration:''},boundary={...known('boundary','video'),duration:'5:00'};
 const minimumItems=[long,...reports,unknown,boundary,known('confirmed-short','shorts')];
 tree=await settle(minimum,{items:minimumItems,more:false,total:minimumItems.length,minimumVideoMinutes:5});
 assert.deepEqual(ids(tree),[long.id,unknown.id,boundary.id],'off filters reported subminimum unknown kinds, keeps unknown lengths and exact boundary');
 assert.doesNotMatch(JSON.stringify(tree),/Off hides|minute minimum/,'the always-visible minimum helper is removed');
 const minimumCalls=minimum.calls.length;
 assert.deepEqual(ids(change(minimum,true)),minimumItems.map(v=>v.id));
 assert.deepEqual(ids(change(minimum,false)),[long.id,unknown.id,boundary.id]);
 assert.equal(minimum.calls.length,minimumCalls,'display toggle retains source rows and never changes settings or provider state');
 assert.equal(reports.every(row=>row.kind==='unknown'),true,'duration filtering never relabels stored video kind');
 minimum.window.dispatchEvent(new CustomEvent(helper.channelRefreshEvent,{detail:{id:minimum.props.id}}));
 tree=await settle(minimum,{items:minimumItems,more:false,total:minimumItems.length,minimumVideoMinutes:0});
 assert.deepEqual(ids(tree),minimumItems.filter(v=>v.kind!=='shorts').map(v=>v.id),'explicit channel zero disables minimum, still hides confirmed Shorts');
 assert.doesNotMatch(JSON.stringify(tree),/Off hides|identified as Shorts/,'zero does not introduce replacement helper text');
 minimum.h.unmount();

 // The same duration predicate bounds paging even when a page has no known
 // Shorts flags. Raw offsets, not the visible count, remain authoritative.
 const lengthPage=scene();lengthPage.draw();lengthPage.h.commit();
 tree=await settle(lengthPage,{items:[long,boundary],more:true,total:5,minimumVideoMinutes:5});lengthPage.edge();
 assert.match(lengthPage.calls.at(-1).url,/offset=2&browse_cursor=0$/);
 tree=await settle(lengthPage,{items:reports.slice(0,2),more:true,total:5,minimumVideoMinutes:5});
 const lengthPaused=lengthPage.calls.length;lengthPage.edge();lengthPage.edge();assert.equal(lengthPage.calls.length,lengthPaused);
 assert.deepEqual(ids(tree),[long.id,boundary.id]);moreButton(tree).props.onClick();
 assert.match(lengthPage.calls.at(-1).url,/offset=4&browse_cursor=0$/);
 tree=await settle(lengthPage,{items:[unknown],more:false,total:5,minimumVideoMinutes:5});
 assert.deepEqual(ids(tree),[long.id,boundary.id,unknown.id]);assert.equal(moreButton(tree),undefined);
 assert.equal(ids(change(lengthPage,true)).length,5,'filtered rows survive raw paging and are restored on request');lengthPage.h.unmount();

 const lengthDuplicates=scene();lengthDuplicates.draw();lengthDuplicates.h.commit();
 tree=await settle(lengthDuplicates,{items:[long,...reports],more:false,total:4,minimumVideoMinutes:5,browse:browse('0',{nextCursor:'25',page:{cursor:'0',items:reports}})});
 lengthDuplicates.edge();lengthDuplicates.edge();assert.equal(lengthDuplicates.calls.length,1,'duplicate-only subminimum provider page cannot chain');
 moreButton(tree).props.onClick();assert.equal(lengthDuplicates.calls.at(-1).body.cursor,'25');
 tree=await settle(lengthDuplicates,{browse:browse('25',{nextCursor:null,hasMore:false,page:{cursor:'25',items:[]}})});
 assert.deepEqual(ids(tree),[long.id]);lengthDuplicates.h.unmount();

 // Hydrated durations apply immediately without a new identity/date, and an
 // entirely filtered known-length page describes the filter rather than
 // falsely claiming those unclassified videos are YouTube Shorts.
 const lengthHydration=scene();lengthHydration.draw();lengthHydration.h.commit();
 tree=await settle(lengthHydration,{items:[unknown],more:false,total:1,minimumVideoMinutes:5});assert.deepEqual(ids(tree),[unknown.id]);
 lengthHydration.window.dispatchEvent(new CustomEvent(helper.channelRefreshEvent,{detail:{id:lengthHydration.props.id}}));
 tree=await settle(lengthHydration,{items:[{...unknown,duration:'1:24'}],more:false,total:1,minimumVideoMinutes:5});
 assert.deepEqual(ids(tree),[]);assert.match(JSON.stringify(tree),/No loaded uploads match this filter/);assert.doesNotMatch(JSON.stringify(tree),/Only Shorts/);
 assert.deepEqual(ids(change(lengthHydration,true)),[unknown.id]);lengthHydration.h.unmount();

 // Actual Mantine renders an accessible, 44px labelled switch in both schemes;
 // the containing toolbar wraps at narrow widths without adding another label.
 const actual=scene(),toolbarTree=actual.draw(),control=toggle(toolbarTree);
 const toolbar=nodes(toolbarTree).find(node=>node.type==='Group'&&node.props.mih===44);
 assert.equal(toolbar.props.wrap,'wrap');assert.equal(toolbar.props.align,'center');
 for(const scheme of ['dark','light'])for(const width of [320,768,1440]){
  const html=renderToStaticMarkup(React.createElement(M.MantineProvider,{forceColorScheme:scheme},React.createElement('div',{style:{width}},React.createElement(M.Switch,control.props))));
  assert.match(html,/role="switch"/);assert.match(html,/Show Shorts/);assert.match(html,/min-height:44px/);assert.equal((html.match(/<input\b/g)||[]).length,1);
 }
 const info=fs.readFileSync(__dirname+'/src/ChannelInfo.tsx','utf8');assert.match(info,/<ChannelVideos[^>]+title=\{title\}/,'the existing Uploads title shares the filter toolbar');
 assert.match(info,/isPublic\?<><Title/,'public uploads retain their heading without a misleading kind filter');
 actual.h.unmount();
 console.log('Channel Shorts: default hidden, strict persisted preference, known-kind-only display, retained identities/cursors, hydration, bounded filtered paging, retry/readOnly safety and real Mantine 44px switch passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
