// Shared compact view for live requests and persisted API task history.
window.createTaskLogView = ({ eventLabels, detailLabels, valueLabels }) => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const duration = ms => ms == null ? '进行中' : `${(Math.max(0, ms) / 1000).toFixed(1)} s`;
  const time = iso => iso ? new Date(iso).toLocaleString('zh-CN', { hour12:false }) : '等待返回';
  const statuses = { running:'执行中', success:'已返回', failed:'失败', skipped:'跳过', info:'记录' };
  const values = { ...valueLabels, high:'高', medium:'中', low:'低', measured:'已测量', disabled:'未启用', unavailable:'不可用',
    heuristic_skip:'按条件跳过模型', heuristic_fallback:'模型失败后统计回退', statistics_only:'仅使用统计计算', partial_heuristic:'部分分块使用统计',
    probability_unavailable:'原文概率服务不可用', detector_disagreement:'模型与统计判断分歧', review_disagreement:'复核意见分歧',
    short_text:'文本较短', insufficient_evidence:'证据不足' };
  const shown = value => values[String(value)] || (typeof value === 'boolean' ? (value ? '是' : '否') : Array.isArray(value) ? value.map(item => values[item] || item).join('、') : value);
  const coreSteps = new Set(['api.scan.accept', 'api.reduce.accept', 'scan.start', 'scan.preprocess.clean',
    'scan.chunking.finish', 'scan.aggregate.finish', 'scan.probability.finish', 'review.trigger', 'review.skip', 'review.merge',
    'rewrite.pipeline.start', 'rewrite.step.finish', 'rewrite.step.skip', 'reduce.start', 'reduce.quality.calculate',
    'provider.azure.call', 'provider.azure.success', 'provider.openrouter.call', 'provider.openrouter.proxy_call', 'provider.openrouter.success',
    'reduce.finish', 'api.scan.return', 'api.reduce.return']);
  const coreKeys = new Set(['scan_phase', 'chars_after', 'chunks', 'parallelism', 'concurrency_limit', 'scan_source', 'scan_model', 'provider', 'model',
    'deployment', 'stage', 'reason', 'error_type', 'fallback', 'label', 'ai_probability', 'ai_reduced_probability',
    'confidence', 'final_score', 'rewrite', 'quality_score', 'before', 'after', 'status', 'perplexity']);

  function resultText(task, data = task.result) {
    if (task.status === 'running') return '执行中，等待返回结果';
    if (task.status === 'failed') return `执行失败 · ${task.error_code || '引擎异常'}`;
    if (!data) return '已完成，未记录结果摘要';
    if (data.ai_reduced_probability != null) return `风险分 ${data.ai_probability ?? '—'} → ${data.ai_reduced_probability} · ${data.rewrite ? '保留改写' : '保留原文'} · 质量分 ${data.quality_score ?? '—'}`;
    return `${data.label || '检测完成'} · 风险分 ${data.ai_probability ?? '—'} · 置信度 ${shown(data.confidence ?? '—')}`;
  }

  function timeline(events = []) {
    const core = events.filter(event => coreSteps.has(event.step) || event.status === 'failed');
    return core.length ? `<div class="execution-timeline">${core.map(event => {
      const details = Object.entries(event.details || {}).filter(([key, value]) => coreKeys.has(key) && value != null && value !== '' && value !== 'none')
        .map(([key, value]) => `<span class="detail-chip">${esc(detailLabels[key] || key)} · ${esc(shown(value))}</span>`).join('');
      return `<div class="event ${esc(event.status)}"><span class="event-dot"></span><div class="event-title-row"><span class="event-title">${esc(eventLabels[event.step] || event.step)}${event.status === 'failed' ? ' · 失败 / 回退' : ''}</span><span class="event-time">${esc(time(event.created_at))} · +${duration(event.elapsed_ms || 0)}</span></div>${details ? `<div class="event-details">${details}</div>` : ''}</div>`;
    }).join('')}</div>` : '<p class="execution-notice">等待服务端记录核心步骤。</p>';
  }

  function engines(execution) {
    if (!execution) return '';
    return `<div class="execution-meta">${[execution.local, ...(execution.third_party || [])].filter(Boolean).map(engine => {
      const attempts = Number(engine.attempts ?? engine.calls ?? 0);
      const successes = Number(engine.successes || 0), failures = Number(engine.failures || 0);
      const pending = Math.max(0, attempts - successes - failures);
      return `<code>${esc(engine.display_name || engine.provider)} · ${attempts ? `成功 ${successes} / 尝试 ${attempts}${failures ? ` · 失败 ${failures}` : ''}${pending ? ` · 待确认 ${pending}` : ''}` : '未调用'}${engine.heuristic_chunks ? ` · 统计直算 ${Number(engine.heuristic_chunks)} 块` : ''}</code>`;
    }).join('')}</div>`;
  }

  function overview(task, data = task.result) {
    const issues = [...(data?.warnings || [])];
    if (data?.assessment_status === 'insufficient_evidence' || data?.detection_status_before === 'insufficient_evidence' || data?.detection_status_after === 'insufficient_evidence') issues.push('insufficient_evidence');
    if ((task.events || []).some(event => event.status === 'failed')) issues.push('存在失败或回退步骤');
    return `<div class="execution-meta"><code>Task ${esc(task.task_id)}</code><code>POST /api/${esc(task.task_type)}</code><code>${esc({demo:'Demo', test:'接口测试', api:'外部 API'}[task.source] || task.source)}</code><code>${Number(task.char_count || 0)} 字符</code><code>${esc(statuses[task.status] || task.status)}</code></div>
      <div class="execution-times"><span>开始时间 <strong>${esc(time(task.created_at))}</strong></span><span>返回时间 <strong>${esc(time(task.returned_at || (task.status !== 'running' ? task.updated_at : null)))}</strong></span><span>总耗时 <strong>${duration(task.duration_ms)}</strong></span></div>
      <p class="execution-notice ${task.status === 'failed' ? 'failed' : issues.length ? 'attention' : ''}"><strong>${esc(resultText(task, data))}</strong>${issues.length ? `<br>${esc([...new Set(issues)].map(shown).join('；'))}` : ''}</p>${engines(task.execution_summary)}`;
  }
  return { esc, duration, time, resultText, timeline, overview };
};

window.createExecutionLog = options => {
  const view = window.createTaskLogView(options);
  const root = document.querySelector('#executionRuns');
  const records = [];
  let sequence = 0;
  function requestId() {
    const bytes = new Uint8Array(16);
    if (window.crypto?.getRandomValues) window.crypto.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }

  function render(run) {
    const task = { ...(run.task || {}), task_id:run.taskId, task_type:run.meta.mode, source:run.meta.source,
      char_count:run.meta.chars, created_at:run.task?.created_at || run.startedAt.toISOString(),
      status:run.task?.status || (run.done ? (run.error ? 'failed' : 'success') : 'running'),
      error_code:run.error || run.task?.error_code,
      duration_ms:run.done ? (run.task?.duration_ms ?? run.endedAt - run.startedAt.getTime()) : Date.now() - run.startedAt.getTime(),
      returned_at:run.task?.returned_at || (run.endedAt ? new Date(run.endedAt).toISOString() : null) };
    const failed = run.error || task.status === 'failed';
    run.node.querySelector('[data-run-status]').className = `execution-badge ${failed ? 'failed' : run.done ? 'success' : ''}`;
    run.node.querySelector('[data-run-status]').textContent = failed ? '请求失败' : run.done ? '已返回' : '执行中';
    run.node.querySelector('[data-run-time]').textContent = run.done ? `返回 ${view.time(run.endedAt)} · ${view.duration(task.duration_ms)}` : `已用时 ${view.duration(task.duration_ms)}`;
    const body = run.node.querySelector('.execution-content');
    const previousScroll = body.querySelector('.execution-timeline')?.scrollTop || 0;
    body.innerHTML = `${view.overview(task, run.data || run.task?.result)}<div class="execution-meta"><code>HTTP ${view.esc(run.httpStatus || '等待响应')}</code></div>${run.traceError ? `<p class="execution-notice">${view.esc(run.traceError)}</p>` : ''}<h3>核心执行步骤 <button class="text-btn" type="button" data-refresh-log>刷新日志</button></h3>${view.timeline(run.task?.events)}`;
    body.querySelector('[data-refresh-log]').addEventListener('click', () => run.refresh());
    if (body.querySelector('.execution-timeline')) body.querySelector('.execution-timeline').scrollTop = previousScroll;
  }

  function updateCount() {
    document.querySelector('#executionCount').textContent = `${records.length} 次请求`;
    if (!records.length) root.innerHTML = '<p class="execution-empty">发起检测或改写后，这里展示核心步骤、返回结果、返回时间和耗时。</p>';
  }

  function start(meta, authToken) {
    const id = requestId();
    const run = { requestId:id, taskId:`${meta.mode}-${id.replaceAll('-', '')}`, meta, startedAt:new Date(), done:false, task:null, data:null, traceError:'' };
    if (!records.length) root.replaceChildren();
    records.forEach(record => { record.node.open = false; });
    run.node = document.createElement('details'); run.node.className = 'execution-run'; run.node.open = true;
    run.node.innerHTML = `<summary><span class="execution-run-title">第 ${++sequence} 次 · ${meta.mode === 'reduce' ? '降 AI 改写' : 'AI 内容检测'}</span><span data-run-status class="execution-badge"></span><span data-run-time class="execution-run-time"></span></summary><div class="execution-content"></div>`;
    root.prepend(run.node); records.unshift(run);
    while (records.length > 20) records.pop().node.remove();
    let inFlight = null;
    async function readTrace() {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch(`/api/tasks/${encodeURIComponent(run.taskId)}`, { headers:{ Authorization:`Bearer ${authToken}` }, signal:controller.signal, cache:'no-store' });
        if (response.ok) { run.task = await response.json(); run.traceError = ''; }
        else if (response.status === 404 && run.done) run.traceError = '任务记录不存在或已过期。';
        else if (response.status !== 404) run.traceError = `日志接口 HTTP ${response.status}，暂时无法读取执行步骤。`;
      } catch { run.traceError = '日志读取暂时失败，可手动刷新；检测结果单独显示。'; }
      finally { clearTimeout(timeout); render(run); }
    }
    run.refresh = async (force = false) => {
      if (inFlight) { await inFlight; if (!force) return; }
      const current = readTrace(); inFlight = current;
      try { await current; } finally { if (inFlight === current) inFlight = null; }
    };
    async function poll() { await run.refresh(); if (!run.done) run.pollTimer = setTimeout(poll, 2000); }
    run.pollTimer = setTimeout(poll, 300);
    run.clock = setInterval(() => {
      run.node.querySelector('[data-run-time]').textContent = `已用时 ${view.duration(Date.now() - run.startedAt.getTime())}`;
    }, 1000);
    updateCount(); render(run);
    return run;
  }

  async function settle(run, data, error) {
    run.done = true; run.endedAt = Date.now(); run.data = data; run.error = error?.message;
    run.httpStatus = error ? (error.status || '未取得响应') : 200;
    if (data?.task_id) run.taskId = data.task_id;
    if (error?.taskId) run.taskId = error.taskId;
    clearTimeout(run.pollTimer); clearInterval(run.clock); render(run);
    if (error && [400,401,403,409,422].includes(error.status)) {
      run.traceError = '请求在鉴权或参数校验阶段被拒绝，未创建本次检测任务。';
    } else await run.refresh(true);
    render(run);
    window.dispatchEvent(new Event('task-log-updated'));
  }
  document.querySelector('#clearExecutionBtn').addEventListener('click', () => {
    for (let i = records.length - 1; i >= 0; i--) if (records[i].done) { records[i].node.remove(); records.splice(i, 1); }
    updateCount();
  });
  return { start, finish:(run, data) => settle(run, data, null), fail:(run, error) => settle(run, null, error) };
};
