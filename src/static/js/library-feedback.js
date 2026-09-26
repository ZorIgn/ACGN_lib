document.addEventListener('DOMContentLoaded', () => {
  const discovery = document.querySelector('[data-discovery]');
  const message = discovery?.querySelector('[data-recommend-feedback]');
  if (!message) return;
  let latest;
  let pending = false;
  const showMessage = (text, undo = false) => {
    message.replaceChildren(document.createTextNode(text));
    if (undo) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'recommendation-undo';
      button.dataset.recommendUndo = '';
      button.textContent = '撤销';
      message.append(' ', button);
    }
    message.hidden = false;
  };
  discovery.addEventListener('click', async event => {
    const button = event.target.closest('[data-recommend-dismiss], [data-recommend-undo]');
    if (!button || pending || button.disabled) return;
    const undo = button.hasAttribute('data-recommend-undo');
    const target = undo ? latest : {
      candidate: button.dataset.candidate,
      url: button.dataset.feedbackUrl,
      card: button.closest('.recommendation-card'),
    };
    if (!target) return;
    pending = true;
    button.disabled = true;
    const data = new FormData();
    data.set('csrfmiddlewaretoken', document.querySelector('[name="csrfmiddlewaretoken"]').value);
    data.set('candidate', target.candidate);
    data.set('action', undo ? 'undo' : 'dismiss');
    try {
      const response = await fetch(target.url, {method: 'POST', body: data});
      if (response.redirected) throw new Error('登录已过期，请刷新页面后重试。');
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('操作失败，请刷新推荐后重试。');
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || '操作失败，请重试。');
      if (target.card?.isConnected) {
        const placeholder = document.createElement('p');
        placeholder.className = 'recommendation-empty';
        placeholder.textContent = undo ? '已恢复推荐，加载后可继续查看。' : '已标记不感兴趣，加载后补入新推荐。';
        target.card.replaceChildren(placeholder);
      }
      latest = undo ? null : {candidate: result.candidate, url: target.url, card: target.card};
      showMessage(undo ? `已恢复《${result.title}》的推荐。` : `已标记对《${result.title}》不感兴趣。`, !undo);
      discovery.dispatchEvent(new CustomEvent('library:recommendations-changed'));
    } catch (error) {
      showMessage(error instanceof TypeError ? '连接失败，请重试。' : error.message, Boolean(latest));
    } finally {
      pending = false;
      button.disabled = false;
    }
  });
});
