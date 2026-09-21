document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
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
}
