// 이 PC 에만 기억하는 화면 설정 (글자 크기·필터 자리·묶음 접힘·순서)
// localStorage 는 쓰지 않음: 앱의 웹 프로필이 시크릿(off-the-record)이라 껐다 켜면 비어 버림.
// 값은 파이썬이 만들어 주는 prefs-data.js 로 다른 스크립트보다 먼저 받고(브리지는 비동기라 늦음),
// 바꾸면 브리지(setPref)로 설정 파일에 저장. 브리지가 붙기 전에 바꾼 값은 모아 뒀다 붙을 때 보냄.
// 일반 브라우저 시험(prefs-data.js 없음)에서는 localStorage 를 대신 씀.
window.Prefs = (() => {
  const browser = !window.doganiPrefs;
  const data = window.doganiPrefs || {};
  const queue = [];
  let send = null;
  const local = (k) => {
    try { const v = localStorage.getItem("dogani." + k); return v == null ? undefined : JSON.parse(v); } catch (e) { return undefined; }
  };
  return {
    get(k, fallback) {
      const v = browser ? local(k) : data[k];
      return v === undefined ? fallback : v;
    },
    set(k, v) {
      const j = JSON.stringify(v);
      if (browser) { try { localStorage.setItem("dogani." + k, j); } catch (e) {} return; }
      if (JSON.stringify(data[k]) === j) return;   // 그대로면 저장 안 함 (창 열 때마다 쓰지 않게)
      data[k] = v;
      if (send) send(k, j); else queue.push([k, j]);
    },
    connect(fn) {
      send = fn;
      queue.splice(0).forEach(([k, j]) => fn(k, j));
    },
  };
})();
