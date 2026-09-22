document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
document.querySelectorAll('.card-time-picker').forEach(picker => {
  const hour = picker.querySelector('select[name="check_hour"]');
  const minute = picker.querySelector('select[name="check_minute"]');
  function restoreSavedTime() {
    const saved = picker.dataset.savedTime || '';
    hour.value = saved ? saved.slice(0, 2) : '';
    minute.value = saved ? saved.slice(3, 5) : '';
  }
  picker.addEventListener('toggle', () => {
    if (!picker.open) restoreSavedTime();
  });
  picker.querySelector('[data-cancel-time]').addEventListener('click', () => {
    restoreSavedTime();
    picker.open = false;
  });
});
const productGrid = document.querySelector('.product-grid');
if (productGrid) {
  const choice = localStorage.getItem('pricewatch-view') === 'list' ? 'list' : 'cards';
  function setView(view) {
    productGrid.dataset.view = view;
    document.querySelectorAll('[data-view-choice]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.viewChoice === view));
    });
    localStorage.setItem('pricewatch-view', view);
  }
  document.querySelectorAll('[data-view-choice]').forEach(button => {
    button.addEventListener('click', () => setView(button.dataset.viewChoice));
  });
  setView(choice);
  const cards = [...document.querySelectorAll('.product-card[data-product-id]')];
  const cardById = new Map(cards.map(card => [card.dataset.productId, card]));
  let pollTimer;
  let pollInFlight = false;
  let statusGeneration = 0;
  function setChecking(card, checking) {
    const button = card.querySelector('.manual-refresh');
    button.disabled = checking;
    button.classList.toggle('is-checking', checking);
    button.setAttribute('aria-busy', String(checking));
  }
  function showCheckStatus(card, message, kind) {
    const status = card.querySelector('.card-check-status');
    status.textContent = message;
    status.dataset.kind = kind;
  }
  function updateCard(card, state) {
    const oldRunId = card.dataset.runId || '';
    const newRunId = state.run_id == null ? '' : String(state.run_id);
    if (card.dataset.awaitingRun === 'true' && !state.pending && newRunId === oldRunId) {
      if (card.dataset.requestAccepted === 'true') {
        card.dataset.awaitingRun = '';
        card.dataset.requestAccepted = '';
        setChecking(card, false);
        showCheckStatus(card, '检查未完成，请查看状态页', 'failed');
      } else if (card.dataset.requestAccepted === 'unknown') {
        card.dataset.awaitingRun = '';
        card.dataset.requestAccepted = '';
        setChecking(card, false);
        showCheckStatus(card, '未能确认检查已提交，请重试', 'failed');
      }
      return;
    }
    if (!state.pending) {
      card.dataset.awaitingRun = '';
      card.dataset.requestAccepted = '';
    }
    card.dataset.runId = newRunId;
    setChecking(card, state.pending);
    showCheckStatus(card, state.status_text, state.status_kind);
    card.querySelector('.price').textContent = state.price;
    card.querySelector('.card-lowest').textContent = state.lowest;
    const change = card.querySelector('.price-change');
    change.textContent = state.price_change_text;
    change.dataset.kind = state.price_change_kind;
  }
  function schedulePoll(delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => { void pollStatus(); }, delay);
  }
  async function pollStatus() {
    if (document.visibilityState === 'hidden') {
      schedulePoll(30000);
      return;
    }
    if (pollInFlight) {
      schedulePoll(1000);
      return;
    }
    pollInFlight = true;
    const generationAtStart = statusGeneration;
    try {
      const response = await fetch('/check-status', {
        credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}
      });
      if (!response.ok) throw new Error('status unavailable');
      const result = await response.json();
      if (generationAtStart !== statusGeneration) {
        schedulePoll(0);
        return;
      }
      for (const [id, state] of Object.entries(result.products)) {
        const card = cardById.get(id);
        if (card) updateCard(card, state);
      }
      if (result.summary) {
        document.querySelector('#monitoring-count').textContent = result.summary.monitoring_count;
        const abnormal = document.querySelector('#abnormal-count');
        abnormal.textContent = result.summary.abnormal_count;
        abnormal.dataset.hasIssues = String(result.summary.abnormal_count > 0);
        document.querySelector('#next-check-time').textContent = result.summary.next_check_time;
      }
      schedulePoll(cards.some(card => card.querySelector('.manual-refresh').disabled) ? 2000 : 30000);
    } catch (_) {
      cards.filter(card => card.dataset.awaitingRun === 'true').forEach(card => {
        showCheckStatus(card, '状态暂不可用，正在重试…', 'running');
      });
      schedulePoll(15000);
    } finally {
      pollInFlight = false;
    }
  }
  cards.forEach(card => {
    const form = card.querySelector('form[data-manual-check]');
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (form.querySelector('.manual-refresh').disabled) return;
      statusGeneration += 1;
      card.dataset.awaitingRun = 'true';
      card.dataset.requestAccepted = '';
      setChecking(card, true);
      showCheckStatus(card, '正在检查…', 'running');
      try {
        const response = await fetch(form.action, {
          method: 'POST', credentials: 'same-origin',
          headers: {Accept: 'application/json'}, body: new FormData(form)
        });
        if (response.status !== 202 || !(await response.json()).pending) {
          throw new Error('check was not accepted');
        }
        card.dataset.requestAccepted = 'true';
        statusGeneration += 1;
        schedulePoll(0);
      } catch (_) {
        card.dataset.requestAccepted = 'unknown';
        statusGeneration += 1;
        showCheckStatus(card, '正在确认检查状态…', 'running');
        schedulePoll(0);
      }
    });
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') schedulePoll(0);
  });
  schedulePoll(0);
}
const backupForm = document.getElementById('backup-form');
if (backupForm) {
  backupForm.addEventListener('submit', async event => {
    event.preventDefault();
    const result = document.getElementById('backup-result');
    result.textContent = '正在创建备份…';
    try {
      const response = await fetch(backupForm.action, {
        method: 'POST', credentials: 'same-origin', body: new FormData(backupForm)
      });
      if (!response.ok) throw new Error('备份失败');
      const backup = await response.json();
      const link = document.createElement('a');
      link.href = backup.download_url;
      link.textContent = '备份完成 · 点击下载';
      link.className = 'link';
      result.replaceChildren(link);
    } catch (_) {
      result.textContent = '备份失败，请稍后再试';
    }
  });
}
