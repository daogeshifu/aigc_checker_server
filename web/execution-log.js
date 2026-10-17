// Per-page diagnostics. Only authenticated server events establish execution outcomes.
window.createExecutionLog = ({ eventLabels, detailLabels, valueLabels }) => {
  const root = document.querySelector('#executionRuns');
  const records = [];
  let sequence = 0;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const json = value => esc(JSON.stringify(value, null, 2));
  const duration = ms => `${(ms / 1000).toFixed(1)} s`;
  const num = value => typeof value === 'number' ? value.toFixed(2) : '—';
  const labels = { running:'进行中', success:'成功', failed:'失败', skipped:'跳过', info:'记录' };
  const reasons = {
    ...valueLabels, heuristic_skip:'按条件跳过模型', heuristic_fallback:'模型失败后统计回退',
    partial_heuristic:'部分分块使用统计', statistics_only:'统计特征计算', qwen:'Qwen 模型判别',
    detector_disagreement:'模型与统计分数分歧较大', short_text:'文本较短',
    probability_unavailable:'原文概率服务不可用', review_disagreement:'远端复核意见不一致'
  };
  const shown = value => reasons[String(value)] || (typeof value === 'boolean' ? (value ? '是' : '否') : typeof value === 'object' ? JSON.stringify(value) : value);
  const extra = (key, title, content) => `<details class="execution-output" data-log-section="${key}"><summary>${esc(title)}</summary>${content}</details>`;

  function requestId() {
    // crypto.randomUUID requires HTTPS; getRandomValues also works on the HTTP demo.
    const bytes = new Uint8Array(16);
    if (window.crypto?.getRandomValues) window.crypto.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }

  function outcome(run) {
    if (!run.done) return ['running', '执行中', '请求已发出，等待服务端执行。'];
    if (run.error) return ['failed', '请求失败', run.error];
    const issues = (run.data?.warnings || []).map(value => reasons[value] || value);
    const failedSteps = (run.task?.events || []).filter(event => event.status === 'failed');
    if (failedSteps.length) issues.push(`${failedSteps.length} 个步骤出现失败或回退`);
    if (run.data?.assessment_status === 'insufficient_evidence') issues.push('检测证据不足');
    return issues.length
      ? ['attention', '已返回 · 需要关注', `接口已返回结果；${[...new Set(issues)].join('；')}。`]
      : ['success', '接口成功', '接口已正常返回结果。模型是否成功调用，请核对下方真实调用次数与执行步骤。'];
  }

  function engineCard(name, engine, active) {
    const attempts = Number(engine.attempts ?? engine.calls ?? 0);
    const successes = Number(engine.successes || 0), failures = Number(engine.failures || 0);
    const pending = Math.max(0, attempts - successes - failures);
    const state = !attempts ? (active ? '目前未调用' : '本次未调用') : pending ? (active ? '调用中' : '终态未记录') : failures ? (successes ? '部分成功' : '调用失败') : '调用成功';
    const models = (engine.models?.length ? engine.models : engine.configured_models || []).join('、');
    return `<div class="execution-engine"><strong>${esc(name)} · ${esc(state)}</strong><small>${esc(models || '无模型调用记录')}</small>尝试 ${attempts} · 成功 ${successes} · 失败 ${failures} · 待确认 ${pending}${engine.heuristic_chunks ? `<small>另有 ${Number(engine.heuristic_chunks)} 个分块按条件统计直算</small>` : ''}</div>`;
  }

  function render(run) {
    const [status, title, message] = outcome(run);
    run.node.querySelector('[data-run-status]').className = `execution-badge ${status}`;
    run.node.querySelector('[data-run-status]').textContent = title;
    run.node.querySelector('[data-run-time]').textContent = `${run.startedAt.toLocaleTimeString('zh-CN', { hour12:false })} · ${duration((run.endedAt || Date.now()) - run.startedAt.getTime())}`;
    const body = run.node.querySelector('.execution-content');
    const openSections = new Set([...body.querySelectorAll('details[open]')].map(node => node.dataset.logSection));
    const timeline = body.querySelector('.execution-timeline');
    const previousScroll = timeline?.scrollTop || 0;
    const events = run.task?.events || [];
    const execution = run.task?.execution_summary;
    const engines = execution
      ? `<div class="execution-engines">${engineCard(execution.local?.display_name || 'Qwen 判别', execution.local || {}, !run.done)}${(execution.third_party || []).length ? execution.third_party.map(engine => engineCard(engine.display_name || engine.provider, engine, !run.done)).join('') : engineCard('Azure 复核 / 语义改写', {}, !run.done)}</div>`
      : `<p class="execution-notice">${run.done ? ([400,401,403,409,422].includes(run.httpStatus) ? '请求未进入引擎，本次未调用检测模型。' : '未取得模型调用记录，执行状态尚未确认。') : '模型调用状态：等待服务端日志，暂不能判断是否调用成功。'}</p>`;
    const eventHtml = events.map(event => {
      const details = Object.entries(event.details || {}).filter(([,value]) => value !== null && value !== '').map(([key,value]) => `<span class="detail-chip">${esc(detailLabels[key] || key)} · ${esc(shown(value))}</span>`).join('');
      return `<div class="event ${esc(event.status)}"><span class="event-dot"></span><div class="event-title-row"><span class="event-title">${esc(event.sequence)}. ${esc(eventLabels[event.step] || event.step)} · ${esc(event.status === 'running' ? '开始 / 发起' : labels[event.status] || event.status)}</span><span class="event-time">+${duration(event.elapsed_ms || 0)}</span></div><div class="event-code">${esc(event.step)}</div><div class="event-details">${details}</div></div>`;
    }).join('');
    const chunks = run.data?.chunks || [];
    const chunkRows = chunks.map(chunk => {
      const breakdown = chunk.score_breakdown || {};
      return `<tr><td>${esc(chunk.chunk_id)}</td><td>${esc(shown(chunk.judge_mode || '未返回'))}</td><td>${chunk.judge_mode === 'qwen' ? num(breakdown.qwen_score) : '—（未使用）'}</td><td>${num(breakdown.feature_score)}</td><td>${num(chunk.ai_score)}</td><td>${num(chunk.confidence)}</td></tr>`;
    }).join('');
    const probability = run.data?.probability;
    const ppl = probability?.status === 'measured'
      ? `已测量 PPL ${num(probability.perplexity)} / NLL ${num(probability.nll)}；${probability.used_in_risk_score ? '已参与评分' : '未参与风险分换算'}`
      : probability?.status === 'disabled' ? '原文 PPL / NLL 服务未启用' : probability?.status === 'unavailable' ? '原文概率测量失败，本次未获得 PPL / NLL' : '服务端未返回 PPL / NLL 测量状态';
    const review = run.data?.review;
    const resultNotes = run.data ? `<div class="execution-notice"><strong>结果判断</strong><br>AI 风格风险分 ${esc(run.data.ai_probability ?? '—')} · 置信度 ${esc(run.data.confidence || '—')} · ${esc(run.data.label || '—')}${run.data.ai_reduced_probability != null ? `<br>改写后风险分 ${esc(run.data.ai_reduced_probability)} · ${run.data.rewrite ? '保留改写' : '保留原文'}` : ''}<br>远端复核：${review ? (review.used ? '已使用' : '未使用') + ' · ' + esc(shown(review.reason || '未提供原因')) : '未返回复核状态'}<br>${esc(ppl)} · Binoculars ${run.data.metrics?.binoculars == null ? '无测量值' : esc(run.data.metrics.binoculars)}</div>` : '';
    const traceMessage = run.traceError || (!events.length ? (run.done ? '没有取得服务端步骤；请求可能未通过鉴权或参数校验。' : '正在等待任务创建，尚无服务端步骤。') : `已读取 ${events.length} 个真实步骤；时间为相对任务开始的累计时间。`);
    body.innerHTML = `<div class="execution-notice ${status}">${esc(message)}</div>
      <div class="execution-meta"><code>Task ${esc(run.task?.task_id || run.taskId)}</code><code>POST /api/${esc(run.meta.mode)}</code><code>HTTP ${esc(run.httpStatus || '等待响应')}</code><code>${run.meta.chars} 字符</code><code>服务端状态 ${esc(run.task ? (labels[run.task.status] || run.task.status) : '尚未确认')}</code></div>
      ${engines}${resultNotes}<h3>服务端执行步骤 <button class="text-btn" type="button" data-refresh-log>刷新执行日志</button></h3><p class="execution-notice">${esc(traceMessage)}</p>
      ${events.length ? `<div class="execution-timeline">${eventHtml}</div>` : ''}
      ${chunks.length ? extra('chunks', `分块评分输出 · ${chunks.length} 块`, `<p class="execution-notice">正常分块使用模型判断 70% + 统计特征 30%，再做文体等校正；跳过或回退分块仅使用统计。文档按分块字符数加权聚合。风险分尚未校准为来源概率。</p><div class="execution-table"><table><thead><tr><th>分块</th><th>执行方式</th><th>模型分</th><th>统计分</th><th>校正后分数</th><th>置信度</th></tr></thead><tbody>${chunkRows}</tbody></table></div>`) : ''}
      ${extra('request', '请求参数（不含 Token 和原文）', `<pre>${json({ ...run.meta, request_id:run.requestId })}</pre>`)}
      ${extra('output', run.data ? '完整接口输出 JSON' : run.done ? '接口错误输出 JSON' : '等待接口输出 JSON', `<pre>${json(run.data || run.errorBody || { status:run.done ? '未获得响应内容' : '等待接口返回' })}</pre>`)}
      ${run.task ? extra('trace', '完整服务端日志 JSON', `<pre>${json(run.task)}</pre>`) : ''}
      <h3>浏览器请求记录</h3><ul class="execution-client">${run.clientEvents.map(event => `<li>${esc(event)}</li>`).join('')}</ul>`;
    body.querySelectorAll('details').forEach(node => { node.open = openSections.has(node.dataset.logSection); });
    body.querySelector('[data-refresh-log]').addEventListener('click', () => run.refresh());
    if (body.querySelector('.execution-timeline')) body.querySelector('.execution-timeline').scrollTop = previousScroll;
  }

  function updateCount() {
    document.querySelector('#executionCount').textContent = `${records.length} 次请求`;
    if (!records.length) root.innerHTML = '<p class="execution-empty">发起检测或改写后，执行细节会显示在这里。</p>';
  }

  function start(meta, authToken) {
    const id = requestId();
    const run = { requestId:id, taskId:`${meta.mode}-${id.replaceAll('-', '')}`, meta, startedAt:new Date(), done:false, task:null, data:null, traceError:'', clientEvents:['已在浏览器发起请求；等待服务端鉴权和创建任务。'] };
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
        else if (response.status !== 404) run.traceError = `日志接口 HTTP ${response.status}，暂时无法读取执行步骤；检测请求的状态单独显示。`;
      } catch { run.traceError = '日志读取暂时失败，将继续重试；这不代表检测引擎失败。'; }
      finally { clearTimeout(timeout); render(run); }
    }
    run.refresh = async (force = false) => {
      if (inFlight) { await inFlight; if (!force) return; }
      const current = readTrace();
      inFlight = current;
      try { await current; } finally { if (inFlight === current) inFlight = null; }
    };
    async function poll() {
      await run.refresh();
      if (!run.done) run.pollTimer = setTimeout(poll, 2000);
    }
    run.pollTimer = setTimeout(poll, 300);
    run.clock = setInterval(() => {
      run.node.querySelector('[data-run-time]').textContent = `${run.startedAt.toLocaleTimeString('zh-CN', { hour12:false })} · ${duration(Date.now() - run.startedAt.getTime())}`;
    }, 1000);
    updateCount(); render(run);
    return run;
  }

  async function settle(run, data, error) {
    run.done = true; run.endedAt = Date.now(); run.data = data;
    run.error = error?.message; run.errorBody = error?.body;
    run.httpStatus = error ? (error.status || '未取得响应') : 200;
    if (data?.task_id) run.taskId = data.task_id;
    if (error?.taskId) run.taskId = error.taskId;
    run.clientEvents.push(error ? `请求未成功：${error.message}` : `已收到 HTTP 200，Task ${run.taskId}。`);
    clearTimeout(run.pollTimer); clearInterval(run.clock);
    render(run);
    // An early validation/auth rejection has no task and must not be called an engine failure.
    if (error && [400,401,403,409,422].includes(error.status)) {
      run.traceError = '请求在鉴权、参数或请求标识校验阶段被拒绝，未创建本次检测任务。';
    } else await run.refresh(true);
    render(run);
  }

  document.querySelector('#clearExecutionBtn').addEventListener('click', () => {
    for (let i = records.length - 1; i >= 0; i--) if (records[i].done) { records[i].node.remove(); records.splice(i, 1); }
    updateCount();
  });
  return { start, finish:(run, data) => settle(run, data, null), fail:(run, error) => settle(run, null, error) };
};
