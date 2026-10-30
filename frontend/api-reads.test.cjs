const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync(__dirname + '/src/api.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const response = (status = 200, result = { ok: true }, retry) => ({ ok: status >= 200 && status < 300, status, json: async () => result, headers: { get: () => retry } });
function setup(fetch) {
  const exports = {}, notices = [], timers = new Map(); let now = 0, next = 0;
  class Clock extends Date { static now() { return now; } }
  vm.runInNewContext(compiled, { exports, Error, Date: Clock, AbortController, FormData,
    fetch, setTimeout: (fn, delay) => { timers.set(++next, { fn, delay }); return next; }, clearTimeout: id => timers.delete(id),
    require: name => name === './plainNames' ? { cleanDisplay: value => JSON.parse(JSON.stringify(value)) } : { notify: value => notices.push(value) },
  });
  return { api: exports.api, notices, timers, tick: async () => { const [id, timer] = timers.entries().next().value; timers.delete(id); now += timer.delay; timer.fn(); await flush(); } };
}
(async () => {
  let calls = [], finish;
  const shared = setup((url, options) => { calls.push({ url, options }); return new Promise(resolve => finish = resolve); });
  const a = new AbortController(), b = new AbortController();
  const first = shared.api('/shared', a.signal), second = shared.api('/shared', b.signal);
  assert.equal(calls.length, 1, 'concurrent GETs share exactly one fetch');
  a.abort(); await assert.rejects(first, { name: 'AbortError' });
  assert.equal(calls[0].options.signal.aborted, false, 'one consumer cannot cancel another');
  finish(response(200, { rows: [1] })); assert.deepEqual(await second, { rows: [1] });
  const next = shared.api('/shared'); assert.equal(calls.length, 2, 'a settled read is never a response cache');
  finish(response()); await next; assert.equal(shared.notices.length, 0);

  calls = [];
  const cancelled = setup((url, options) => { calls.push(options); return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => { const error = Error('cancelled'); error.name = 'AbortError'; reject(error); })); });
  const c = new AbortController(), d = new AbortController();
  const third = cancelled.api('/cancel', c.signal), fourth = cancelled.api('/cancel', d.signal);
  c.abort(); d.abort();
  await Promise.all([assert.rejects(third, { name: 'AbortError' }), assert.rejects(fourth, { name: 'AbortError' })]);
  assert.equal(calls[0].signal.aborted, true, 'last subscriber aborts the transport');
  const e = new AbortController(); const later = cancelled.api('/cancel', e.signal); assert.equal(calls.length, 2);
  e.abort(); await assert.rejects(later); assert.equal(cancelled.notices.length, 0);
  const already = new AbortController(); already.abort(); await assert.rejects(cancelled.api('/never', already.signal)); assert.equal(calls.length, 2);

  let attempts = 0;
  const retry = setup(async () => ++attempts === 1 ? response(503, { error: 'busy' }, '10') : response());
  const retried = retry.api('/busy'); await flush();
  assert.equal(attempts, 1); assert.equal([...retry.timers.values()][0].delay, 10000, 'Retry-After is honoured');
  await retry.tick(); await retried; assert.equal(attempts, 2); assert.equal(retry.notices.length, 0, 'a recovered busy read does not raise an error balloon');

  attempts = 0;
  const bounded = setup(async () => { attempts++; return response(503, { error: 'busy' }); });
  const failed = assert.rejects(bounded.api('/busy'), /busy/); await flush();
  await bounded.tick(); await bounded.tick(); await failed;
  assert.equal(attempts, 3); assert.equal(bounded.timers.size, 0); assert.equal(bounded.notices.length, 1);
  const again = assert.rejects(bounded.api('/busy')); await flush(); await bounded.tick(); await bounded.tick(); await again;
  assert.equal(bounded.notices.length, 1, 'unchanged final polling failures coalesce');

  attempts = 0;
  const backingOff = setup(async () => { attempts++; return response(429, { error: 'limited' }, '5'); });
  const controller = new AbortController(), waiting = backingOff.api('/limited', controller.signal); await flush();
  controller.abort(); await assert.rejects(waiting, { name: 'AbortError' }); await flush();
  assert.equal(attempts, 1); assert.equal(backingOff.timers.size, 0); assert.equal(backingOff.notices.length, 0);

  calls = [];
  const write = setup(async (url, options) => { calls.push(options); return response(503, { error: 'busy' }, '10'); });
  await assert.rejects(write.api('/write', undefined, { action: 'cancel' }, 'csrf'));
  assert.equal(calls.length, 1, 'writes are never retried'); assert.equal(write.timers.size, 0);
  assert.equal(calls[0].method, 'POST'); assert.equal(calls[0].body, '{"action":"cancel"}'); assert.equal(calls[0].headers['X-CSRF-Token'], 'csrf');

  const form=new FormData();form.append('strm',new File(['https://example.invalid/video.mp4'],'Fixture.strm'));form.append('preview_id','opaque-preview');
  calls=[];const multipart=setup(async(url,options)=>{calls.push(options);return response(503,{error:'Upload unavailable'},'10');});
  await assert.rejects(multipart.api('/upload',undefined,form,'upload-csrf',{feedback:'inline'}),/Upload unavailable/);
  assert.equal(calls.length,1,'multipart writes are also sent exactly once');assert.equal(calls[0].body,form,'the original FormData is sent without JSON encoding');assert.equal(calls[0].headers['X-CSRF-Token'],'upload-csrf');assert.ok(!('Content-Type' in calls[0].headers),'the browser chooses the multipart boundary');assert.equal(calls[0].credentials,'same-origin');assert.equal(multipart.timers.size,0);assert.equal(multipart.notices.length,0,'inline multipart failures have no duplicate notification');
  const abortUpload=new AbortController();calls=[];const interruptedUpload=setup((url,options)=>{calls.push(options);return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const error=Error('cancelled');error.name='AbortError';reject(error);}));});const uploading=interruptedUpload.api('/upload',abortUpload.signal,form,'upload-csrf',{feedback:'inline'});abortUpload.abort();await assert.rejects(uploading,{name:'AbortError'});assert.equal(calls.length,1);assert.equal(calls[0].signal,abortUpload.signal);assert.equal(interruptedUpload.notices.length,0);

  const inline = setup(async () => response(409, {error:'Inline failure'}));
  await assert.rejects(inline.api('/settings',undefined,{value:1},'csrf',{feedback:'inline'}),/Inline failure/);
  assert.equal(inline.notices.length,0,'inline save errors have one owner, without a duplicate balloon');
  await assert.rejects(inline.api('/settings',undefined,undefined,undefined,{feedback:'inline'}),/Inline failure/);
  assert.equal(inline.notices.length,0,'inline load errors stay with the form');
  await assert.rejects(inline.api('/settings'),/Inline failure/);
  assert.equal(inline.notices.length,1,'an inline consumer cannot silence another notification consumer');

  let done;
  const quiet = setup(() => new Promise(resolve => done = resolve));
  const expected = assert.rejects(quiet.api('/identity', undefined, undefined, undefined, { expectedReadStatuses: [400] }));
  const normal = assert.rejects(quiet.api('/identity')); done(response(400, { error: 'unmatched' }));
  await Promise.all([expected, normal]); assert.equal(quiet.notices.length, 1, 'expected statuses are a consumer choice');

  const pending = [];
  const invalidation = setup((url, options) => options.method === 'POST' ? Promise.resolve(response()) : new Promise(resolve => pending.push(resolve)));
  const old = invalidation.api('/read'); await invalidation.api('/write', undefined, {});
  const fresh = invalidation.api('/read'); assert.equal(pending.length, 2, 'write acknowledgement prevents stale-flight reuse');
  pending[0](response(200, { generation: 0 })); pending[1](response(200, { generation: 1 }));
  assert.equal((await old).generation, 0); assert.equal((await fresh).generation, 1);

  const multipartPending=[];const multipartInvalidation=setup((url,options)=>options.method==='POST'?Promise.resolve(response()):new Promise(resolve=>multipartPending.push(resolve)));
  const beforeUpload=multipartInvalidation.api('/history');await multipartInvalidation.api('/upload',undefined,form,'upload-csrf',{feedback:'inline'});const afterUpload=multipartInvalidation.api('/history');assert.equal(multipartPending.length,2,'multipart acknowledgement also prevents stale history-flight reuse');multipartPending[0](response(200,{generation:0}));multipartPending[1](response(200,{generation:1}));assert.equal((await beforeUpload).generation,0);assert.equal((await afterUpload).generation,1);

  let deliver;
  const copies = setup(() => new Promise(resolve => deliver = resolve));
  const copyA = copies.api('/copies'), copyB = copies.api('/copies');
  deliver(response(200, { items: [1] }));
  const [valueA, valueB] = await Promise.all([copyA, copyB]);
  valueA.items.push(2); assert.deepEqual(valueB.items, [1], 'shared transport never shares mutable caller data');

  calls = []; const freshResolvers = [];
  const remount = setup((url, options) => { calls.push(options); return new Promise((resolve, reject) => {
    freshResolvers.push(resolve);options.signal.addEventListener('abort', () => { const error = Error('cancelled'); error.name = 'AbortError'; reject(error); });
  }); });
  const formerController = new AbortController();
  const former = assert.rejects(remount.api('/remount', formerController.signal), { name: 'AbortError' });
  formerController.abort();
  const replacement = remount.api('/remount'); await flush();
  const companion = remount.api('/remount');
  assert.equal(calls.length, 2, 'a settling cancelled flight cannot evict its replacement');
  freshResolvers[1](response()); await Promise.all([former,replacement,companion]);

  attempts = 0;
  const dated = setup(async () => ++attempts === 1 ? response(503, {error:'busy'},new Date(10000).toUTCString()) : response());
  const datedRead = dated.api('/dated'); await flush();
  assert.equal([...dated.timers.values()][0].delay,10000,'HTTP-date Retry-After is honoured');
  await dated.tick(); await datedRead;

  attempts = 0;
  const cumulative = setup(async () => { attempts++; return response(503,{error:'busy'},'20'); });
  const budgetFailure = assert.rejects(cumulative.api('/budget')); await flush(); await cumulative.tick(); await budgetFailure;
  assert.equal(attempts,2);assert.equal(cumulative.timers.size,0,'retry budget covers the entire read, not each attempt');

  calls = [];
  const independentWrites = setup(async (url,options) => { calls.push(options); return response(); });
  await Promise.all([independentWrites.api('/action',undefined,{}),independentWrites.api('/action',undefined,{})]);
  assert.equal(calls.length,2,'separate explicit writes are never coalesced');

  attempts = 0;
  const longWait = setup(async () => { attempts++; return response(429, { error: 'limited' }, '60'); });
  await assert.rejects(longWait.api('/long')); assert.equal(attempts, 1); assert.equal(longWait.timers.size, 0, 'long Retry-After is not shortened to fit budget');
  attempts = 0;
  const network = setup(async () => { attempts++; throw Error('offline'); });
  await assert.rejects(network.api('/offline')); assert.equal(attempts, 1, 'network errors do not replay unknown requests');
  console.log('API reads: shared transport, independent cancellation, bounded Retry-After, quiet recovery, write-once and post-write invalidation passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
