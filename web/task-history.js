window.createTaskHistory = options => {
  const view = window.createTaskLogView(options);
  const $ = selector => document.querySelector(selector);
  const pageSize = 20;
  let page = 1, total = 0, selectedId = null;
  let listController = null, detailController = null, loading = false, detailLoading = false, generation = 0, searchTimer;
  const filters = ['#historySource', '#historyType', '#historyStatus'];

  async function read(url, controller) {
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(url, { cache:'no-store', signal:controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `日志接口 HTTP ${response.status}`);
      return data;
    } finally { clearTimeout(timeout); }
  }

  async function load() {
    const currentGeneration = ++generation;
    listController?.abort();
    const controller = listController = new AbortController();
    loading = true;
    $('#refreshHistoryBtn').disabled = true;
    $('#historyMessage').textContent = '正在读取任务日志…';
    const params = new URLSearchParams({limit:String(pageSize), offset:String((page - 1) * pageSize)});
    for (const [key, selector] of [['source','#historySource'], ['task_type','#historyType'], ['status','#historyStatus'], ['search','#historySearch']]) {
      const value = $(selector).value.trim();
      if (value) params.set(key, value);
    }
    try {
      const data = await read(`/api/tasks?${params}`, controller);
      if (currentGeneration !== generation) return;
      total = data.total;
      const maxPage = Math.max(1, Math.ceil(total / pageSize));
      if (page > maxPage) { page = maxPage; return load(); }
      $('#historyScope').textContent = '全部调用方 · 无需 Token';
      $('#historyMessage').textContent = `包含 Demo、测试页和外部 API 任务 · 服务端最多保留 ${data.retention_limit} 条 · 更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12:false})}`;
      $('#historyPageInfo').textContent = `第 ${page} / ${maxPage} 页 · 共 ${total} 条`;
      $('#historyPrevBtn').disabled = page <= 1;
      $('#historyNextBtn').disabled = page >= maxPage;
      const rows = data.items || [];
      $('#historyTable').innerHTML = rows.length ? `<table><thead><tr><th>任务 / 接口</th><th>来源</th><th>状态</th><th>返回结果</th><th>开始时间</th><th>返回时间</th><th>耗时</th><th>过程</th></tr></thead><tbody>${rows.map(rowHtml).join('')}</tbody></table>` : '<p class="execution-empty">暂无符合条件的任务记录。</p>';
      $('#historyTable').querySelectorAll('[data-history-task]').forEach(button => button.addEventListener('click', () => openDetail(button.dataset.historyTask)));
    } catch (error) {
      if (currentGeneration !== generation) return;
      $('#historyMessage').textContent = controller.signal.aborted ? '日志读取超时，请刷新重试。' : `日志读取失败：${error.message}。`;
    } finally {
      if (currentGeneration === generation) { loading = false; $('#refreshHistoryBtn').disabled = false; }
    }
  }

  function rowHtml(task) {
    const result = task.result;
    const needsAttention = result?.warnings?.length || [result?.assessment_status, result?.detection_status_before, result?.detection_status_after].includes('insufficient_evidence');
    return `<tr>
      <td><strong>${task.task_type === 'reduce' ? '降 AI 改写' : 'AI 检测'}</strong><small>POST /api/${view.esc(task.task_type)}</small><small>${view.esc(task.task_id)}</small></td>
      <td>${view.esc({demo:'Demo',test:'接口测试',api:'外部 API'}[task.source] || task.source)}</td>
      <td><span class="execution-badge ${view.esc(task.status)}">${view.esc({running:'执行中',success:'已返回',failed:'失败'}[task.status] || task.status)}</span></td>
      <td class="history-result">${view.esc(view.resultText(task))}${needsAttention ? '<small>含回退或证据不足，请查看过程</small>' : ''}</td>
      <td>${view.esc(view.time(task.created_at))}</td><td>${view.esc(view.time(task.returned_at))}</td><td>${view.duration(task.duration_ms)}</td>
      <td><button type="button" class="text-btn" data-history-task="${view.esc(task.task_id)}" aria-label="查看 ${view.esc(task.task_id)} 执行过程">查看过程</button></td>
    </tr>`;
  }

  async function openDetail(id, silent = false) {
    if (silent && detailLoading) return;
    selectedId = id;
    if (!silent) {
      $('#detailModal').classList.add('open');
      $('#modalTaskId').textContent = id;
      $('#modalBody').innerHTML = '<p class="execution-empty">正在读取任务执行过程…</p>';
    }
    detailController?.abort();
    const controller = detailController = new AbortController();
    detailLoading = true;
    const isCurrent = () => selectedId === id && detailController === controller && $('#detailModal').classList.contains('open');
    try {
      const task = await read(`/api/tasks/${encodeURIComponent(id)}`, controller);
      if (!isCurrent()) return;
      const scroll = $('#modalBody').querySelector('.execution-timeline')?.scrollTop || 0;
      $('#modalBody').innerHTML = `${view.overview(task)}<h3 class="timeline-title">核心执行过程</h3>${view.timeline(task.events)}`;
      const timeline = $('#modalBody').querySelector('.execution-timeline');
      if (timeline) timeline.scrollTop = scroll;
    } catch (error) {
      if (!isCurrent()) return;
      $('#modalBody').innerHTML = `<p class="execution-empty">${view.esc(controller.signal.aborted ? '日志读取超时，请刷新重试。' : error.message)}</p><button class="small-btn" type="button" data-retry-detail>重试</button>`;
      $('#modalBody').querySelector('[data-retry-detail]').addEventListener('click', () => openDetail(id));
    } finally { if (detailController === controller) detailLoading = false; }
  }

  function resetPage() { page = 1; load(); }
  filters.forEach(selector => $(selector).addEventListener('change', resetPage));
  $('#historySearch').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(resetPage, 250); });
  $('#refreshHistoryBtn').addEventListener('click', () => { load(); if (selectedId && $('#detailModal').classList.contains('open')) openDetail(selectedId, true); });
  $('#historyPrevBtn').addEventListener('click', () => { if (page > 1) { page--; load(); } });
  $('#historyNextBtn').addEventListener('click', () => { if (page * pageSize < total) { page++; load(); } });
  load();
  window.addEventListener('task-log-updated', () => load());
  setInterval(() => {
    if (document.hidden || !$('#historyAutoRefresh').checked) return;
    if (!loading) load();
    if (selectedId && $('#detailModal').classList.contains('open')) openDetail(selectedId, true);
  }, 5000);
  return { refresh:load };
};
