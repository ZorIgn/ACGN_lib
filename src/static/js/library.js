document.addEventListener('DOMContentLoaded', () => {
  const review = document.querySelector('form[data-draft-id]');
  const fieldName = /^(choice_\d+|manual_title_\d+|\d+-\d+-(score|status|notes|viewing_url|status_manual))$/;
  const draftKey = review ? `acglib-draft:${review.dataset.userId}:${review.dataset.draftId}:${review.dataset.page}` : null;
  const draftFields = () => [...review.querySelectorAll('input, select, textarea')].filter(input => fieldName.test(input.name));
  const snapshot = () => JSON.stringify(draftFields().filter(input => input.type !== 'checkbox' || input.checked).map(input => [input.name, input.value]));
  let acknowledged = review ? snapshot() : '';
  if (review) {
    try {
      const pending = localStorage.getItem(draftKey);
      if (pending) {
        const values = new Map();
        JSON.parse(pending).forEach(([name, value]) => {
          if (!values.has(name)) values.set(name, []);
          values.get(name).push(value);
        });
        draftFields().forEach(input => {
          if (input.type === 'checkbox') input.checked = (values.get(input.name) || []).includes(input.value);
          else if (values.has(input.name)) input.value = values.get(input.name)[0];
        });
      }
    } catch (_) { /* Server-side drafts remain available when browser storage is disabled. */ }
  }

  const initStarRating = widget => {
    const hiddenInput = widget.querySelector('input[type="hidden"]');
    if (!hiddenInput) return;

    const stars = widget.querySelectorAll('.star-unit');
    const scoreNum = widget.querySelector('.rating-val-text');
    const scoreMax = widget.querySelector('.rating-val-max');
    const btnZero = widget.querySelector('[data-action="zero"]');
    const btnClear = widget.querySelector('[data-action="clear"]');
    const recordFields = widget.closest('.personal-fields, .detail-form');
    const status = recordFields?.querySelector('select[name$="status"]');
    const statusManual = recordFields?.querySelector('input[name$="status_manual"]');
    status?.addEventListener('change', () => {
      if (statusManual) statusManual.value = '1';
    });
    const applyScore = score => {
      hiddenInput.value = score;
      const manual = statusManual && ['1', 'true', 'True', 'on'].includes(statusManual.value);
      if (score !== '' && status && !status.value && !manual) status.value = 'Completed';
      updateDisplay(score);
      hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
    };

    const updateDisplay = val => {
      const hasVal = val !== '' && val !== null && val !== undefined;
      const num = hasVal ? Number(val) : 0;
      if (scoreNum) scoreNum.textContent = hasVal ? val : '未评分';
      if (scoreMax) scoreMax.style.display = hasVal ? '' : 'none';
      if (btnZero) btnZero.classList.toggle('is-active', hasVal && num === 0);
      stars.forEach((star, idx) => {
        const fill = Math.max(0, Math.min(100, (num / 2 - idx) * 100));
        star.querySelector('.star-fill-clip').style.width = `${fill}%`;
        star.querySelectorAll('[data-score]').forEach(button => {
          button.setAttribute('aria-pressed', String(hasVal && Number(button.dataset.score) === num));
        });
      });
    };

    updateDisplay(hiddenInput.value);

    widget.querySelectorAll('.star-click-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const score = btn.dataset.score;
        applyScore(score);
      });
    });

    if (btnZero) {
      btnZero.addEventListener('click', () => {
        applyScore('0');
      });
    }

    if (btnClear) {
      btnClear.addEventListener('click', () => {
        applyScore('');
      });
    }
  };

  document.querySelectorAll('[data-rating-widget]').forEach(initStarRating);

  document.querySelectorAll('.candidate-checkbox').forEach(cb => {
    const editor = document.getElementById(cb.dataset.editorId) ||
                   document.querySelector(`[data-editor-for="${cb.id}"]`);

    const syncEditor = () => {
      const isChecked = cb.checked;
      const card = cb.closest('.candidate-card, .candidate');
      if (card) {
        card.classList.toggle('is-active', isChecked);
      }
      if (editor) {
        editor.style.display = isChecked ? '' : 'none';
        editor.querySelectorAll('input, select, textarea, button').forEach(ctrl => {
          if (ctrl !== cb) {
            ctrl.disabled = !isChecked;
          }
        });
      }
    };

    syncEditor();
    cb.addEventListener('change', syncEditor);
    window.addEventListener('pageshow', syncEditor);
  });

  if (review) {
    const status = review.querySelector('[data-save-status]');
    const count = review.querySelector('[data-selected-count]');
    let timer;
    let queue = Promise.resolve();
    let pendingSaves = 0;
    let leaving = false;
    const remember = () => {
      const value = snapshot();
      try {
        if (value === acknowledged && pendingSaves === 0) localStorage.removeItem(draftKey);
        else localStorage.setItem(draftKey, value);
      } catch (_) {}
      if (value !== acknowledged) status.textContent = '有修改待保存';
    };
    const save = () => {
      clearTimeout(timer);
      const value = snapshot();
      pendingSaves += 1;
      remember();
      queue = queue.catch(() => {}).then(async () => {
        if (value === acknowledged) {
          try {
            if (localStorage.getItem(draftKey) === value) localStorage.removeItem(draftKey);
          } catch (_) {}
          if (snapshot() === value) status.textContent = '草稿已保存';
          return;
        }
        status.textContent = '正在保存…';
        const data = new FormData();
        for (const name of ['csrfmiddlewaretoken', 'draft_id', 'page']) {
          data.set(name, review.elements.namedItem(name).value);
        }
        data.set('action', 'draft');
        JSON.parse(value).forEach(([name, content]) => data.append(name, content));
        try {
          const response = await fetch(review.getAttribute('action'), { method: 'POST', body: data });
          const result = await response.json();
          if (!response.ok || !result.ok) throw new Error(result.error || '保存失败');
          acknowledged = value;
          try {
            if (localStorage.getItem(draftKey) === value) localStorage.removeItem(draftKey);
          } catch (_) {}
          status.textContent = snapshot() === value ? '草稿已保存' : '有修改待保存';
          count.textContent = `已选 ${result.selected_count} 部（全部页面）`;
        } catch (error) {
          status.textContent = '保存失败，请重试后再离开';
          throw error;
        }
      }).finally(() => { pendingSaves -= 1; });
      return queue;
    };
    const changed = event => {
      if (!fieldName.test(event.target.name || '')) return;
      remember();
      clearTimeout(timer);
      timer = setTimeout(() => save().catch(() => {}), 600);
    };
    review.addEventListener('input', changed);
    review.addEventListener('change', changed);
    review.querySelector('[data-save-draft]').addEventListener('click', () => save().catch(() => {}));
    review.addEventListener('submit', async event => {
      event.preventDefault();
      const button = event.submitter;
      if (leaving) return;
      leaving = true;
      try {
        await save();
        if (button?.name === 'target_page') {
          window.location.assign(`/library/add/?draft=${review.dataset.draftId}&page=${button.value}`);
        } else {
          if (button) { button.disabled = true; button.textContent = '正在导入…'; }
          HTMLFormElement.prototype.submit.call(review);
        }
      } catch (_) { leaving = false; }
    });
    document.addEventListener('click', async event => {
      const link = event.target.closest('a[href]');
      if (!link || event.defaultPrevented || event.ctrlKey || event.metaKey || link.target === '_blank') return;
      if (new URL(link.href).origin !== location.origin || leaving) return;
      event.preventDefault();
      leaving = true;
      try { await save(); window.location.assign(link.href); }
      catch (_) { leaving = false; }
    });
    window.addEventListener('pagehide', remember);
    window.addEventListener('pageshow', () => {
      leaving = false;
      if (snapshot() !== acknowledged) save().catch(() => {});
    });
    if (snapshot() !== acknowledged) save().catch(() => {});
  }

  document.querySelectorAll('form[data-busy]').forEach(form => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type=submit]') || form.querySelector('button:not([type=button])');
      if (button && form.dataset.busy) {
        button.disabled = true;
        button.textContent = form.dataset.busy;
      }
    });
  });

  const hideBrokenImage = img => {
    img.style.visibility = 'hidden';
  };

  const imageSelectors = '.cover img, .media-cover img, .detail-cover img, .candidate img, .candidate-cover-frame img';
  document.querySelectorAll(imageSelectors).forEach(img => {
    if (img.complete && img.naturalWidth === 0 && img.getAttribute('src')) {
      hideBrokenImage(img);
    } else {
      img.addEventListener('error', () => hideBrokenImage(img));
    }
  });
});
