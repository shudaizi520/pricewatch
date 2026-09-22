document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
document.querySelectorAll('form[data-auto-save-time]').forEach(form => {
  form.querySelector('input[type="time"]').addEventListener('change', () => {
    form.requestSubmit(form.querySelector('[data-save-time]'));
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
  function selectCard(card) {
    document.querySelector('#selected-price').textContent = card.dataset.price || '—';
    cards.forEach(item => {
      item.querySelector('.card-select').setAttribute('aria-pressed', String(item === card));
    });
    localStorage.setItem('pricewatch-selected-product', card.dataset.productId);
  }
  cards.forEach(card => card.querySelector('.card-select').addEventListener('click', () => selectCard(card)));
  const saved = localStorage.getItem('pricewatch-selected-product');
  selectCard(cards.find(card => card.dataset.productId === saved) || cards[0]);
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
