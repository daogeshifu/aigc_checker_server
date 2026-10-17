// Run with: node --test tests/test_task_log_view.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {window:{}};
vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, '../web/execution-log.js'), 'utf8'), context);
const view = context.window.createTaskLogView({eventLabels:{'api.scan.return':'返回结果'}, detailLabels:{}, valueLabels:{}});

test('compact timeline hides per-chunk statistics and preserves failures', () => {
  const html = view.timeline([
    {step:'scan.chunk.features', status:'info', details:{token_count:300}},
    {step:'scan.chunk.score', status:'info', details:{qwen_score:45}},
    {step:'judge.qwen.fallback', status:'failed', details:{error_type:'RuntimeError'}},
    {step:'api.scan.return', status:'success', details:{ai_probability:'42.00'}},
  ]);
  assert.doesNotMatch(html, /scan.chunk.features|token_count|qwen_score/);
  assert.match(html, /judge.qwen.fallback/);
  assert.match(html, /RuntimeError/);
  assert.match(html, /返回结果/);
});

test('all dynamic result and event content is escaped', () => {
  const html = view.overview({task_id:'<img src=x onerror=alert(1)>', task_type:'scan', status:'success',
    result:{label:'<script>unsafe</script>', ai_probability:'42.00', confidence:'high'}})
    + view.timeline([{step:'api.scan.return', status:'success', details:{label:'<img src=x>'}}]);
  assert.doesNotMatch(html, /<script>|<img/);
  assert.match(html, /&lt;script&gt;/);
});

test('running tasks have no fabricated return time or result', () => {
  assert.equal(view.time(null), '等待返回');
  assert.equal(view.resultText({status:'running'}), '执行中，等待返回结果');
  assert.match(view.overview({task_id:'scan-live', status:'running'}), /等待返回/);
});

test('rewrite rollback and zero-valued results remain explicit', () => {
  assert.match(view.resultText({status:'success', result:{rewrite:false, ai_probability:'0.00', ai_reduced_probability:'0.00', quality_score:0}}), /0.00 → 0.00 · 保留原文 · 质量分 0/);
});

test('historical insufficient evidence and fallback warnings remain visible', () => {
  const html = view.overview({task_id:'scan-fallback', status:'success', result:{label:'Insufficient Evidence',
    ai_probability:'42.00', assessment_status:'insufficient_evidence', warnings:['heuristic_fallback']}});
  assert.match(html, /证据不足/);
  assert.match(html, /模型失败后统计回退/);
  assert.match(html, /attention/);
});

test('chunking milestone shows dynamic parallelism and its ceiling', () => {
  const html = view.timeline([{step:'scan.chunking.finish', status:'success', details:{chunks:23, parallelism:10, concurrency_limit:10}}]);
  assert.match(html, /chunks · 23/);
  assert.match(html, /parallelism · 10/);
  assert.match(html, /concurrency_limit · 10/);
});
