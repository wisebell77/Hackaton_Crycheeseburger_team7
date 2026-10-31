/* Tutorial writes stay in memory. Reloading or leaving the tour keeps real records intact. */
(function () {
  let practice = null, generation = 0;
  globalThis.WorkspaceStorage = {
    getItem(key) { return practice ? (practice.get(key) ?? null) : localStorage.getItem(key); },
    setItem(key, value) { if (practice) practice.set(key, String(value)); else localStorage.setItem(key, value); },
    beginPractice() { if (practice) throw Error('이미 연습 중입니다.'); practice = new Map(); generation++; },
    endPractice() { practice = null; generation++; },
    get practicing() { return practice !== null; },
    get generation() { return generation; }
  };
})();
