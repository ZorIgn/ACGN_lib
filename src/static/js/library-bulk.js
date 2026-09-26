document.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('[data-bulk-form]');
  if (!form) return;
  const toggle = document.querySelector('[data-bulk-toggle]');
  const feedback = document.querySelector('[data-bulk-feedback]');
  const storageKey = `acglib-selection:${document.body.dataset.userId}`;
  const resultKey = `${storageKey}:result`;
  const selected = new Set();
  let active = false;
  let busy = false;
  try {
    const stored = JSON.parse(sessionStorage.getItem(storageKey) || '[]');
    if (Array.isArray(stored)) stored.forEach(key => { if (typeof key === 'string') selected.add(key); });
    const result = sessionStorage.getItem(resultKey);
    if (result) { feedback.textContent = result; feedback.hidden = false; sessionStorage.removeItem(resultKey); }
  } catch (_) {}
  active = selected.size > 0;
  toggle.hidden = false;
  const save = () => { try { sessionStorage.setItem(storageKey, JSON.stringify([...selected])); } catch (_) {} };
  const sync = () => {
    form.hidden = !active;
    toggle.textContent = active ? '退出多选' : '批量整理';
    toggle.setAttribute('aria-expanded', String(active));
    form.querySelector('[data-selection-count]').textContent = `已选 ${selected.size} 部`;
    form.querySelector('[type=submit]').disabled = busy || !selected.size;
    form.elements.selection.value = JSON.stringify([...selected]);
    document.querySelectorAll('[data-select-record]').forEach(input => {
      input.checked = selected.has(input.value);
      input.disabled = busy;
      input.closest('label').hidden = !active;
      input.closest('.shelf-entry').classList.toggle('is-selected', input.checked);
    });
  };
  const syncAction = () => {
    const folders = form.elements.action.value !== 'status';
    form.querySelector('[data-bulk-folder]').hidden = !folders;
    form.querySelector('[data-bulk-status]').hidden = folders;
    form.elements.folder.disabled = !folders;
    form.elements.folder.required = folders;
    form.elements.status.disabled = folders;
  };
  toggle.addEventListener('click', () => {
    if (busy) return;
    active = !active;
    if (!active) { selected.clear(); save(); }
    sync();
  });
  document.addEventListener('change', event => {
    if (!event.target.matches('[data-select-record]') || busy) return;
    const input = event.target;
    if (input.checked) selected.add(input.value); else selected.delete(input.value);
    save(); sync();
  });
  form.querySelector('[data-select-page]').addEventListener('click', () => {
    if (busy) return;
    document.querySelectorAll('[data-select-record]').forEach(input => selected.add(input.value));
    save(); sync();
  });
  form.querySelector('[data-clear-selection]').addEventListener('click', () => {
    if (busy) return;
    selected.clear(); save(); sync();
  });
  form.elements.action.addEventListener('change', syncAction);
  document.addEventListener('library:shelf-updated', sync);
  window.addEventListener('pageshow', sync);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !selected.size) return;
    const data = new FormData(form);
    busy = true;
    form.querySelector('fieldset').disabled = true;
    feedback.textContent = '正在更新已选作品…'; feedback.hidden = false;
    sync();
    try {
      const response = await fetch(form.getAttribute('action'), { method: 'POST', body: data });
      if (response.redirected) throw new Error('登录已过期，请刷新后重试。');
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('更新未完成，请重试。');
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || '更新未完成，请重试。');
      selected.clear(); save();
      try { sessionStorage.setItem(resultKey, result.message); } catch (_) {}
      location.reload();
    } catch (error) {
      feedback.textContent = error instanceof TypeError ? '连接失败，已保留选择，请重试。' : error.message;
      busy = false;
      form.querySelector('fieldset').disabled = false;
      sync();
    }
  });
  syncAction(); sync();
});
