// Shared by demo, API test and task history. Expiry is the browser's local midnight.
(() => {
  const key = 'aigc_daily_token_v1';
  let memory = null;
  let persistent = true;
  let timer;
  const listeners = new Set();
  const day = date => `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
  // Undated credentials from the old UI must not survive the daily expiry policy.
  try { sessionStorage.removeItem('aigc_demo_token'); } catch {}

  function read() {
    let record = memory;
    try { if (persistent) {
      record = JSON.parse(localStorage.getItem(key));
    } } catch { persistent = false; }
    const now = new Date();
    if (!record || typeof record.token !== 'string' || !record.token.trim() ||
        record.day !== day(now) || !Number.isFinite(record.expiresAt) || record.expiresAt <= now.getTime()) {
      memory = null;
      try { localStorage.removeItem(key); } catch {}
      return '';
    }
    memory = record;
    return record.token;
  }

  function notify() {
    const value = read();
    clearTimeout(timer);
    if (memory) timer = setTimeout(notify, Math.max(1, memory.expiresAt - Date.now()));
    listeners.forEach(listener => listener(value));
  }

  function save(value) {
    const token = value.trim();
    if (!token) { clear(); return; }
    const now = new Date();
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    memory = { token, day: day(now), expiresAt: midnight.getTime() };
    try { localStorage.setItem(key, JSON.stringify(memory)); persistent = true; }
    catch { persistent = false; }
    // Do not re-read storage here: a browser may allow reads while rejecting writes.
    clearTimeout(timer);
    timer = setTimeout(notify, Math.max(1, memory.expiresAt - Date.now()));
    listeners.forEach(listener => listener(token));
  }

  function clear() {
    memory = null;
    clearTimeout(timer);
    try { localStorage.removeItem(key); } catch {}
    listeners.forEach(listener => listener(''));
  }

  window.demoTokenStore = {
    read, save, clear,
    get persistent() { return persistent; },
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }
  };
  window.addEventListener('storage', event => { if (event.key === key || event.key === null) notify(); });
  window.addEventListener('focus', notify);
  window.addEventListener('pageshow', notify);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) notify(); });
  notify();
})();
