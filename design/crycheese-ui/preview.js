/* Design reference interactions only: no calculations, storage, network or orders. */
(() => {
  const all = selector => [...document.querySelectorAll(selector)];
  const dialog = document.querySelector('#methodDialog');
  const openHelp = () => dialog.showModal();
  const titles = {overview: '발주 신청', daily: '일일 재고 기록', history: '과거 발주 이력'};
  all('[data-nav]').forEach(button => button.addEventListener('click', () => {
    const view = button.dataset.nav;
    all('[data-nav]').forEach(item => {
      const selected = item === button;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
    all('[data-view]').forEach(section => { section.hidden = section.dataset.view !== view; });
    document.querySelector('#breadcrumb').textContent = titles[view];
    document.querySelector('#reviewBtn').hidden = view !== 'overview';
  }));
  all('[data-plan]').forEach(button => button.addEventListener('click', () => {
    all('[data-plan]').forEach(item => {
      const selected = item === button;
      item.classList.toggle('is-selected', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
  }));
  let selectedStatus = 'all';
  const search = document.querySelector('#search');
  const filterRows = () => {
    all('#inventoryBody tr').forEach(row => {
      row.hidden = (selectedStatus !== 'all' && row.dataset.filter !== selectedStatus)
        || !row.textContent.includes(search.value.trim());
    });
    const count = all('#inventoryBody tr').filter(row => !row.hidden).length;
    document.querySelector('#tableCount').textContent = `식자재 ${count}개 · 디자인 샘플`;
  };
  all('[data-status]').forEach(button => button.addEventListener('click', () => {
    selectedStatus = button.dataset.status;
    all('[data-status]').forEach(item => item.classList.toggle('active', item === button));
    filterRows();
  }));
  search.addEventListener('input', filterRows);
  document.querySelector('#resetFilters').addEventListener('click', () => {
    search.value = '';
    document.querySelector('[data-status="all"]').click();
  });
  const updateSnapshot = () => {
    const selectedStore = document.querySelector('.store-tab.active').textContent;
    const selectedDate = document.querySelector('#dateSelect').value;
    document.querySelector('#snapshotText').textContent = `${selectedDate.replaceAll('-', '.')} · ${selectedStore} 선택 미리보기 · 수치는 9/30 매장 A 샘플`;
  };
  all('.store-tab').forEach(button => button.addEventListener('click', () => {
    all('.store-tab').forEach(item => {
      const selected = item === button;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
    updateSnapshot();
  }));
  document.querySelector('#dateSelect').addEventListener('change', updateSnapshot);
  document.querySelector('#initialDateBtn').addEventListener('click', () => {
    document.querySelector('#dateSelect').value = '2026-09-30';
    updateSnapshot();
  });
  document.querySelector('#nextRecordBtn').addEventListener('click', () => {
    document.querySelector('#dateSelect').value = '2026-10-01';
    updateSnapshot();
  });
  all('#methodBtn, #assumptionsBtn, #reviewBtn, [data-help], [data-detail]').forEach(button => button.addEventListener('click', openHelp));
  document.querySelector('#closeMethod').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
})();
