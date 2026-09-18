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

  document.addEventListener('submit', event => {
    const form = event.target.closest('form[data-busy]');
    if (!form || event.defaultPrevented) return;
    const button = event.submitter || form.querySelector('button[type=submit]') || form.querySelector('button:not([type=button])');
    if (button && form.dataset.busy) {
      button.disabled = true;
      button.textContent = form.dataset.busy;
    }
  });

  const hideBrokenImage = img => {
    img.style.visibility = 'hidden';
  };

  const imageSelectors = '.cover img, .media-cover img, .detail-cover img, .candidate img, .candidate-cover-frame img';
  const initCovers = root => root.querySelectorAll(imageSelectors).forEach(img => {
    if (img.complete && img.naturalWidth === 0 && img.getAttribute('src')) {
      hideBrokenImage(img);
    } else {
      img.addEventListener('error', () => hideBrokenImage(img));
    }
  });
  initCovers(document);

  const discovery = document.querySelector('[data-discovery]');
  if (discovery) {
    const results = discovery.querySelector('[data-recommendations]');
    const retry = discovery.querySelector('[data-recommend-retry]');
    const kind = document.querySelector('.capture-form [name="media_type"]');
    let mode = 'personal';
    let controller;
    const loadRecommendations = async () => {
      controller?.abort();
      const request = new AbortController();
      controller = request;
      results.setAttribute('aria-busy', 'true');
      results.replaceChildren(Object.assign(document.createElement('p'), { className: 'recommendation-empty', textContent: '正在寻找作品…' }));
      retry.hidden = true;
      const url = new URL(discovery.dataset.url, location.origin);
      url.searchParams.set('type', kind.value);
      url.searchParams.set('mode', mode);
      try {
        const response = await fetch(url, { signal: request.signal });
        if (!response.ok || response.redirected) throw new Error('Recommendations unavailable');
        const html = await response.text();
        if (request !== controller || request.signal.aborted) return;
        results.innerHTML = html;
        initCovers(results);
        retry.hidden = !results.querySelector('[data-recommend-unavailable]');
      } catch (error) {
        if (request !== controller || error.name === 'AbortError') return;
        results.replaceChildren(Object.assign(document.createElement('p'), { className: 'recommendation-empty', textContent: '推荐暂时无法加载。可以重试，或继续搜索作品。' }));
        retry.hidden = false;
      } finally {
        if (request === controller) results.removeAttribute('aria-busy');
      }
    };
    kind.addEventListener('change', loadRecommendations);
    discovery.querySelectorAll('[data-recommend-mode]').forEach(button => {
      button.addEventListener('click', () => {
        mode = button.dataset.recommendMode;
        discovery.querySelectorAll('[data-recommend-mode]').forEach(tab => {
          const active = tab === button;
          tab.classList.toggle('is-active', active);
          tab.setAttribute('aria-pressed', String(active));
        });
        loadRecommendations();
      });
    });
    retry.addEventListener('click', loadRecommendations);
    loadRecommendations();
  }

  const filters = document.querySelector('[data-shelf-filter]');
  if (filters) {
    const feedback = document.querySelector('[data-filter-feedback]');
    const reset = filters.querySelector('[data-filter-reset]');
    const submit = filters.querySelector('button[type="submit"]');
    let controller;
    let searchTimer;
    const syncLabels = () => {
      for (const name of ['type', 'status']) {
        const count = filters.querySelectorAll(`input[name="${name}"]:checked`).length;
        filters.querySelector(`[data-filter-count="${name}"]`).textContent = count || '全部';
      }
      reset.hidden = !filters.elements.q.value && !filters.querySelector('input:checked');
    };
    const filterURL = () => {
      const url = new URL(filters.getAttribute('action'), location.origin);
      const params = new URLSearchParams(new FormData(filters));
      if (!params.get('q')) params.delete('q');
      url.search = params.toString();
      return url;
    };
    const loadResults = async (url, recordHistory = true) => {
      clearTimeout(searchTimer);
      controller?.abort();
      const request = new AbortController();
      controller = request;
      syncLabels();
      document.getElementById('shelf-results').setAttribute('aria-busy', 'true');
      feedback.hidden = true;
      submit.textContent = '筛选中…';
      try {
        const response = await fetch(url, { signal: request.signal });
        if (!response.ok) throw new Error('Filter request failed');
        const html = new DOMParser().parseFromString(await response.text(), 'text/html');
        const results = html.getElementById('shelf-results');
        const count = html.getElementById('shelf-count');
        if (!results || !count) throw new Error('Missing shelf results');
        if (request !== controller || request.signal.aborted) return;
        document.getElementById('shelf-results').replaceWith(results);
        document.getElementById('shelf-count').textContent = count.textContent;
        initCovers(results);
        if (recordHistory && url.href !== location.href) history.pushState(null, '', url);
      } catch (error) {
        if (request !== controller || error.name === 'AbortError') return;
        feedback.textContent = '筛选未完成，当前仍显示上次结果。请点击「筛选」重试。';
        feedback.hidden = false;
      } finally {
        if (request === controller) {
          document.getElementById('shelf-results').removeAttribute('aria-busy');
          submit.textContent = '筛选';
        }
      }
    };
    filters.addEventListener('submit', event => {
      event.preventDefault();
      loadResults(filterURL());
    });
    filters.addEventListener('change', event => {
      if (event.target.type === 'checkbox') loadResults(filterURL());
    });
    filters.elements.q.addEventListener('input', () => {
      clearTimeout(searchTimer);
      controller?.abort();
      syncLabels();
      searchTimer = setTimeout(() => loadResults(filterURL()), 300);
    });
    reset.addEventListener('click', event => {
      event.preventDefault();
      filters.elements.q.value = '';
      filters.querySelectorAll('input[type="checkbox"]').forEach(input => { input.checked = false; });
      loadResults(filterURL());
    });
    document.addEventListener('click', event => {
      filters.querySelectorAll('details[open]').forEach(menu => {
        if (!menu.contains(event.target)) menu.open = false;
      });
      const link = event.target.closest('#shelf-results .shelf-pagination a');
      if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const url = filterURL();
      url.searchParams.set('page', new URL(link.href).searchParams.get('page'));
      loadResults(url);
    });
    filters.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        const menu = event.target.closest('details');
        if (menu) { menu.open = false; menu.querySelector('summary').focus(); }
      }
    });
    window.addEventListener('popstate', () => {
      const params = new URL(location.href).searchParams;
      filters.elements.q.value = params.get('q') || '';
      filters.querySelectorAll('input[type="checkbox"]').forEach(input => {
        input.checked = params.getAll(input.name).includes(input.value);
      });
      loadResults(new URL(location.href), false);
    });
  }
});
