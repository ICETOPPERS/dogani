/*
 * 오버레이 UI 로직. 같은 페이지를 두 가지 창으로 쓴다 (state.mode). 창 열고 닫기·스페이스 고르기는 트레이 메뉴
 * - note: 할일 창. 고른 스페이스의 그날 노트 (CodeMirror 라이브 프리뷰, 쓰기 권한 있을 때만 편집)
 * - cal : 달력 창. 고른 스페이스의 월 달력 + 일정 파일 편집 (일정은 위에서 체크한 스페이스들을 합쳐 표시)
 * 창 이동/크기/창끼리 연락은 QWebChannel 의 bridge 객체로 파이썬에 요청
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const MD = window.ObsidianMarkdown;
  const CAL = window.doganiCalendar;
  const { WD, pad, iso, parseISO, addMonths } = CAL;
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  const dayLabel = (s) => {
    const d = parseISO(s);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} (${WD[d.getDay()]})`;
  };

  const state = {
    mode: "",          // note | cal
    space: null,       // { key, name, server, prefix, kind: me|team, status }
    spaces: [],        // 달력에 같이 그릴 수 있는 스페이스 전부
    base: "",          // "http://ip:port/api/s/<스페이스id>"
    today: iso(new Date()),
    noteDate: iso(new Date()),
    calYM: iso(new Date()).slice(0, 7),
    calView: "month",
    status: "offline", // online | offline | blocked(팀 로그인 풀림)
    es: null,
    retryTimer: null,
    writable: false,
    myName: "",        // 설정의 내 이름 (멤버 칩의 "나")
    membersBySpace: {}, // 스페이스 키 → 팀원 이름 (/schedule 의 members, 멤버 칩 후보)
    groupsBySpace: {},  // 스페이스 키 → 멤버 그룹 [{name, members: [{name, perm}]}] (사이드바 그룹 칩)
    calHidden: new Set(), // 달력·할일 일정에서 숨긴 스페이스 키 (설정 cal_hidden, 창끼리 같이 씀)
    evBySpace: {},     // 스페이스 키 → /schedule 일정 목록
    tasksBySpace: {},  // 스페이스 키 → /schedule 의 날짜 붙은 할일 (개인 스페이스만 옴)
    decoBySpace: {},   // 스페이스 키 → /schedule 의 장식 줄 (일정 파일 "261119 ✦ 글자")
    roSpaces: new Set(), // 읽기만 허용된 스페이스 키 (팀 관리자가 편집 허용을 끔. /schedule 의 writable)
    sched: null,       // 일정 색인 (CAL.index)
    holidays: {},      // 받아 온 공휴일 {iso: [이름]} (연도별로 합침)
  };
  let bridge = null;

  // ------------------------------------------------------------ server
  async function api(path, body, base) {
    const opts = { cache: "no-store" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch((base || state.base) + path, opts);
    const res = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(res.message || res.error || "HTTP " + r.status);
      err.code = res.error || "http";
      throw err;
    }
    return res;
  }

  const STATUS_TEXT = {
    offline: "연결할 수 없습니다.",
    blocked: "로그인이 풀렸습니다. 트레이 > 스페이스에서 초대장으로 다시 참여해 주세요.",
    not_member: "이 스페이스의 팀원이 아닙니다. 트레이 > 다른 스페이스에 참여하기 로 들어와 주세요.",
    no_space: "그 PC 에 이 스페이스가 없습니다.",
    client_only: "접속 키가 맞지 않습니다. (dogani 버전 확인)",
    conflict: "그 사이 다른 곳(옵시디언·다른 팀원)에서 수정되어 저장하지 않았습니다. 다시 불러옵니다.",
    read_only: "편집 권한이 없습니다.",
    write_failed: "저장에 실패했습니다.",
  };
  const errorText = (e) => STATUS_TEXT[e.code] || STATUS_TEXT[state.status] || STATUS_TEXT.offline;

  function setStatus(status) {
    if (state.status === status) return;
    state.status = status;
    if (bridge && state.space) bridge.setStatus(state.space.key, status);
    note.setReadOnly(!canType());   // 끊긴 동안은 못 씀 (쓰던 내용은 보관, 다시 연결되면 저장)
    rebuildSched();
  }
  /** 노트에 쓸 수 있음 = 쓰기 권한 + 연결됨 */
  const canType = () => state.writable && state.status === "online";

  function connect() {
    clearTimeout(state.retryTimer);
    clearInterval(state.watchTimer);
    if (state.es) state.es.close();
    state.es = null;
    const sp = state.space;
    state.base = sp && sp.server ? "http://" + sp.server + sp.prefix : "";
    state.status = "";
    setStatus("offline");   // 상태를 비웠으므로 항상 실행 (일정 다시 그리기 포함, 새 base 기준)
    if (!state.base) return;

    const es = new EventSource(state.base + "/events");
    es.addEventListener("hello", () => {
      setStatus("online");
      refresh();
      loadSchedule();
      api("/info").then((info) => { setWritable(!!info.writable); note.flushOthers(); }).catch(() => {});
      note.retryPending();   // 끊긴 동안 저장 못 한 내용
    });
    es.addEventListener("schedule", () => loadSchedule());
    es.addEventListener("changed", (e) => {
      const dates = JSON.parse(e.data).dates || [];
      if (state.mode === "note" && dates.includes(state.noteDate)) note.reloadIfIdle();
      if (state.mode === "cal" && dates.some((d) => d.startsWith(state.calYM))) loadMonth(true);
      // 노트가 바뀌면 날짜 붙은 할일(달력에 비친 것)도 바뀜 → 일정 다시 읽기 (개인 스페이스만 할일이 옴)
      if (dates.length && state.space && state.space.kind === "me") loadSchedule();
    });
    es.addEventListener("reload", () => { loadSchedule(); refresh(); });
    // 끊김 감시: 노트북을 덮거나 망이 바뀌면 연결이 소리 없이 죽음 → 서버 ping(15초)이 40초 넘게 안 오면 다시 연결.
    // ping 이벤트를 한 번이라도 받은 뒤에만 (예전 호스트는 ping 을 주석으로 보내 이벤트가 안 옴)
    let beat = 0;
    for (const ev of ["hello", "ping", "changed", "schedule", "reload"]) es.addEventListener(ev, () => { if (ev === "ping" || beat) beat = Date.now(); });
    state.watchTimer = setInterval(() => {
      if (state.es === es && beat && Date.now() - beat > 40000) connect();
    }, 5000);
    es.onerror = () => {
      if (state.es !== es) return;
      // 브라우저가 자동 재연결을 포기한 경우(CLOSED: 401·403 등)에는 이유를 확인하고 직접 재시도
      if (es.readyState === EventSource.CLOSED) {
        api("/info")
          .then(() => setStatus("offline"))
          .catch((e) => setStatus(e.code === "not_member" ? "blocked" : "offline"))
          .finally(() => { refresh(); state.retryTimer = setTimeout(connect, 5000); });
      } else {
        setStatus("offline");
      }
    };
    state.es = es;
  }

  function refresh() {
    if (state.mode === "note") note.reloadIfIdle();
    else if (state.mode === "cal") loadMonth();
  }

  // ------------------------------------------------------------ schedule (보이는 스페이스들의 일정을 합쳐 그림)
  const spBase = (sp) => (sp && sp.server ? "http://" + sp.server + sp.prefix : "");
  /** 할일 창 위 스페이스 탭 (둘 이상일 때만). 달력은 모든 스페이스를 함께 그리므로 탭이 없음 */
  function renderSpaceTabs() {
    const box = $("note-tabs");
    if (!box) return;
    const list = state.spaces || [];
    box.hidden = state.mode !== "note" || list.length < 2;
    if (box.hidden) { box.textContent = ""; return; }
    box.textContent = "";
    for (const sp of list) {
      const b = document.createElement("button");
      b.className = "space-tab" + (state.space && sp.key === state.space.key ? " active" : "");
      b.textContent = sp.name;
      b.title = sp.server || "";
      b.addEventListener("click", () => {
        if (bridge && !(state.space && sp.key === state.space.key)) bridge.selectSpace(sp.key);
      });
      box.appendChild(b);
    }
  }

  const spaceByKey = (k) => state.spaces.find((s) => s.key === k) || (state.space && state.space.key === k ? state.space : null);
  /** 일정을 읽을 수 있는 스페이스 (트레이 메뉴 순서 그대로, 탭을 바꿔도 순서 고정). 다시 로그인 필요(blocked)·꺼진 항목은 뺌 */
  function schedSpaces() {
    return state.spaces.length ? state.spaces.filter((s) => s.server && !s.disabled && s.status !== "blocked")
      : state.space ? [state.space] : [];
  }
  const shownSpaces = () => schedSpaces().filter((s) => !state.calHidden.has(s.key));
  /** 일정을 고칠 수 있음 = 쓰기 권한 + 그 일정이 있는 스페이스가 모두 연결됨 (꺼진 호스트에 쓰다 멈추지 않게) */
  /** 연결됨: 지금 보는 스페이스는 이 창의 SSE 상태, 나머지는 트레이가 알려준 상태 */
  const spOnline = (sp) => !!sp && (state.space && sp.key === state.space.key ? state.status === "online" : sp.status === "online");
  const canEdit = (ev) => (ev.spaces || []).every((k) => { const sp = spaceByKey(k); return spOnline(sp) && !state.roSpaces.has(k); });

  async function loadSchedule(sp) {
    sp = sp || state.space;
    if (!sp || !spBase(sp)) return;
    try {
      const res = await api("/schedule", undefined, spBase(sp));
      state.evBySpace[sp.key] = res.events || [];
      state.tasksBySpace[sp.key] = res.tasks || [];
      state.decoBySpace[sp.key] = res.decos || [];
      if (res.members) state.membersBySpace[sp.key] = res.members;
      state.groupsBySpace[sp.key] = res.groups || [];
      const ro = res.writable === false;
      if (ro !== state.roSpaces.has(sp.key)) {
        if (ro) state.roSpaces.add(sp.key); else state.roSpaces.delete(sp.key);
        if (state.space && sp.key === state.space.key) setWritable(!ro, true);   // 관리자가 권한을 바꾸면 바로 반영
      }
    } catch (e) {
      return; // 예전 버전 dogani(일정 API 없음) 이거나 연결 끊김: 기존 것 유지
    }
    rebuildSched();
  }

  /** 지금 보는 스페이스 말고 보이는 스페이스들 (지금 스페이스는 SSE 로 바로 받음, 나머지는 1분마다) */
  function loadOtherSchedules() {
    for (const sp of shownSpaces()) if (!state.space || sp.key !== state.space.key) loadSchedule(sp);
  }

  const timeKey = (ev) => (ev.time || "").split("-")[0].padStart(5, "0");
  /**
   * 보이는 스페이스의 일정을 합침. 같은 일정(같은 줄로 저장되는 내용)이 여러 스페이스에 있으면 하나로 그리고
   * copies = [{sp, raw}] 에 어디 있는지 모아 둠 → 고치면 모든 사본을 같이 고침.
   * 한 스페이스 안의 똑같은 줄 두 개는 합치지 않음 (스페이스당 사본 하나)
   */
  function rebuildSched() {
    const shown = shownSpaces(), merged = new Map(), list = [];
    for (const sp of shown) {
      for (const ev of state.evBySpace[sp.key] || []) {
        const id = schedLine(evToObj(ev)), same = merged.get(id) || [];
        let m = same.find((x) => !x.spaces.includes(sp.key));
        if (!m) { m = { ...ev, copies: [], spaces: [] }; same.push(m); merged.set(id, same); list.push(m); }
        m.copies.push({ sp: sp.key, raw: ev.raw });
        if (ev.who && !m.who) m.who = ev.who;
        m.spaces.push(sp.key);
      }
    }
    for (const m of list) {
      // 스페이스가 둘 이상 보일 때만 어느 스페이스 일정인지 표시 (색 막대 + 툴팁)
      m.stale = !m.spaces.some((k) => spOnline(spaceByKey(k)));   // 끊긴 스페이스 것만 = 마지막으로 받은 내용 (흐리게)
      m.spaceMarks = shown.length > 1 ? m.spaces.map((k) => { const sp = spaceByKey(k); return { name: sp ? sp.name : k }; }) : null;
    }
    list.sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : timeKey(a) < timeKey(b) ? -1 : timeKey(a) > timeKey(b) ? 1 : 0));
    state.schedAll = list;   // 태그로 거르기 전 (태그 필터 목록을 만들 때 씀)
    // 노트의 날짜 붙은 할일: 일정처럼 그리지만 원본은 노트 줄 (태그·멤버 체크와 상관없이 보임)
    // 전날 못 한 할일을 다음 날 노트에 복사해 쓰는 경우: 같은 제목·같은 예정 날짜면 가장 최근 노트의 줄 하나만 (그 줄이 지금 상태)
    // 완료(✅ 날짜)면 예정 날짜 대신 완료한 날 하루에 그림
    const tasks = [], latest = new Map();
    for (const sp of shown) {
      for (const t of state.tasksBySpace[sp.key] || []) {
        const id = sp.key + "|" + t.title + "|" + t.start + "|" + t.end, prev = latest.get(id);
        if (!prev || t.note > prev.note || (t.note === prev.note && t.line > prev.line)) latest.set(id, { ...t, sp: sp.key });
      }
    }
    for (const t of latest.values()) {
      const on = t.done && t.done_on;
      // 할일 제목의 #태그 → 색만 맞춤 (태그 필터에는 안 걸림)
      const ttags = [...(t.title || "").matchAll(/(?:^|\s)#([^\s#]+)/g)].map((m) => m[1]);
      tasks.push({ start: on || t.start, end: on || t.end, title: t.title, tags: ttags, members: [], spaces: [t.sp],
        task: { sp: t.sp, note: t.note, line: t.line, raw: t.raw, done: t.done, plan: on ? [t.start, t.end] : null } });
    }
    // 장식: 스페이스별로 파일 순서 그대로 (장식끼리만 순서, 일정과는 안 섞음)
    const decos = {};
    for (const sp of shown) for (const d of state.decoBySpace[sp.key] || []) (decos[d.start] = decos[d.start] || []).push({ ...d, sp: sp.key });
    state.decos = decos;
    state.schedItems = [...list.filter((ev) => tagShown(ev) && memberShown(ev)), ...tasks];
    ensureHolidays();
    state.sched = CAL.index(state.schedItems, state.holidays, cleanName(state.myName));
    if (state.mode === "cal" && state.calView === "month") renderMonth();
    if (state.mode === "note") renderSchedRow();
  }

  // 공휴일: 파이썬이 PC 마다 받아 저장해 둔 것 (12시간 지나면 뒤에서 새로 받음 → 받는 중이면 조금 뒤 다시 물음)
  const holYears = {};   // year -> "ok" | "wait" | 다시 물은 횟수
  function ensureHolidays() {
    if (!bridge || !bridge.getHolidays) return;
    const ys = new Set([state.today, state.noteDate, state.calYM + "-01"].filter(Boolean).map((d) => +d.slice(0, 4)));
    if (state.calYM) { ys.add(+addMonths(state.calYM, -1).slice(0, 4)); ys.add(+addMonths(state.calYM, 1).slice(0, 4)); }
    for (const y of ys) if (!holYears[y]) askHolidays(y, 0);
  }
  function askHolidays(y, tries) {
    holYears[y] = "wait";
    bridge.getHolidays(y, (json) => {
      let r;
      try { r = JSON.parse(json); } catch (e) { r = { days: {} }; }
      const prefix = y + "-";
      const before = JSON.stringify(Object.entries(state.holidays).filter(([d]) => d.startsWith(prefix)));
      for (const d of Object.keys(state.holidays)) if (d.startsWith(prefix)) delete state.holidays[d];
      Object.assign(state.holidays, r.days || {});
      if (r.pending && tries < 5) setTimeout(() => askHolidays(y, tries + 1), 4000);
      else holYears[y] = "ok";
      const after = JSON.stringify(Object.entries(state.holidays).filter(([d]) => d.startsWith(prefix)));
      if (before !== after) rebuildSched();
    });
  }

  /** 달력 위 스페이스 체크 (끄면 그 스페이스 일정을 달력·할일 창에서 숨김. 화면 공유할 때 내 일정 숨기기 등) */
  function renderSpaceFilter() {
    const box = $("cal-spaces");
    const list = schedSpaces();
    box.hidden = !list.length;
    box.innerHTML = list.map((sp) => `<label><input type="checkbox" data-key="${esc(sp.key)}"${state.calHidden.has(sp.key) ? "" : " checked"}>` +
      `<span class="chip-t" title="${esc(sp.name)}">${esc(sp.name)}</span></label>`).join("");
  }

  // ------------------------------------------------------------ 태그 필터 (숨긴 태그 = calHidden 안의 "#태그", "#" = 태그 없는 일정)
  const NO_TAG = "#";
  const tagColor = (t) => `hsl(${CAL.tagHue(t)} 70% 50%)`;
  /** 태그가 여러 개면 하나라도 켜져 있으면 보임 */
  function tagShown(ev) {
    const tags = ev.tags || [];
    return tags.length ? tags.some((t) => !state.calHidden.has("#" + t)) : !state.calHidden.has(NO_TAG);
  }
  /** 그 달에 걸친 일정의 태그(field = tags) 또는 멤버(members) (가나다순) */
  function tagsOfMonth(ym, field = "tags") {
    const s = ym + "-01", e = ym + "-31", set = new Set();
    for (const ev of state.schedAll || []) if (ev.start <= e && ev.end >= s) for (const t of ev[field] || []) set.add(t);
    return [...set].sort((a, b) => a.localeCompare(b, "ko"));
  }
  /** 화면에 보이는 달의 태그. 하나도 없으면(월초 등) 지난달 태그 */
  function shownTags(field = "tags") {
    const tags = tagsOfMonth(state.calYM, field);
    return tags.length ? tags : tagsOfMonth(addMonths(state.calYM, -1), field);
  }

  // ------------------------------------------------------------ 멤버 필터 (일정 줄의 @이름. 숨긴 멤버 = calHidden 안의 "@이름", "@" = 멤버 없는 일정)
  // 이름 글자로만 맞춤 (팀원 id 와 묶지 않음): 같은 이름 = 같은 사람, 팀원 아닌 이름도 됨
  const NO_MEMBER = "@";
  const cleanName = (v) => String(v || "").replace(/[@#,，\s]/g, "");
  function memberShown(ev) {
    // reach = @그룹·@all 을 펼친 팀원 이름 (팀스페이스): 그룹 일정도 그 그룹원 칩으로 보이고 숨겨짐
    const ms = ev.reach ? [...(ev.members || []), ...ev.reach] : ev.members || [];
    return ms.length ? ms.some((m) => !state.calHidden.has("@" + m)) : !state.calHidden.has(NO_MEMBER);
  }
  /** 모든 스페이스 팀원 이름 (스페이스마다 따로 받아 합침) */
  const teamNames = () => Object.values(state.membersBySpace).flat();
  /** 고를 수 있는 멤버: 나 → 팀원 → 보이는 달 일정에 적힌 이름 (중복 없이) */
  function memberPicks() {
    const out = [];
    for (const n of [state.myName, ...teamNames(), ...shownTags("members")]) {
      const c = cleanName(n);
      if (c && !out.includes(c)) out.push(c);
    }
    return out;
  }
  /** 모든 스페이스의 멤버 그룹 (같은 이름은 구성원을 합침) → {이름: [{name, perm}]} */
  function allGroups() {
    const out = {};
    for (const gs of Object.values(state.groupsBySpace)) for (const g of gs) {
      const k = cleanName(g.name), list = out[k] || (out[k] = []);
      for (const m of g.members) if (!list.some((x) => x.name === m.name)) list.push(m);
    }
    return out;
  }
  const GROUP_ICON = '<svg class="person-ic" viewBox="0 0 16 16" aria-hidden="true"><circle cx="5.5" cy="5" r="2.5"/><circle cx="11" cy="5.5" r="2.2"/><path d="M0.5 14.5c0-2.9 2.2-4.8 5-4.8s5 1.9 5 4.8z"/><path d="M11.5 14.5c0-1.8-.6-3.3-1.7-4.3.4-.1.8-.2 1.2-.2 2.5 0 4.5 1.7 4.5 4.5z"/></svg>';
  const PERSON_ICON = '<svg class="person-ic" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="5" r="3"/><path d="M2 15c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5z"/></svg>';
  const meMark = (m) => (m === cleanName(state.myName) ? " (나)" : "");
  function renderMemberFilter() {
    const box = $("cal-members"), found = shownTags("members");
    // 스페이스 실제 멤버(나·팀원)를 앞에, 일정에만 적힌 이름은 뒤에
    // 멤버 그룹은 사람 칩과 따로: 그룹 칩은 #cal-groups 묶음 (일정에 안 적혀도 늘 보임), all 은 칩 없음
    const groups = allGroups(), isGroup = (n) => n in groups || n.toLowerCase() === "all";
    const real = [state.myName, ...teamNames()].map(cleanName).filter((n, i, a) => n && a.indexOf(n) === i && !isGroup(n));
    const ms = [...real.filter((n) => found.includes(n)), ...found.filter((n) => !real.includes(n) && !isGroup(n))];
    const gs = Object.keys(groups).sort();
    // 멤버 그룹은 따로 묶음 (#cal-groups). 숨김 키는 멤버와 같은 "@이름"
    const gbox = $("cal-groups");
    gbox.hidden = !gs.length;
    gbox.innerHTML = gs.map((g) => `<label class="mem-group"><input type="checkbox" data-mem="${esc(g)}"${state.calHidden.has("@" + g) ? "" : " checked"}>` +
      `${GROUP_ICON}<span class="chip-t">${esc(g)}</span></label>`).join("");
    box.hidden = !ms.length && !state.calHidden.has(NO_MEMBER);
    box.innerHTML = ms.map((m) => `<label${real.includes(m) ? ' class="mem-real"' : ""}><input type="checkbox" data-mem="${esc(m)}"${state.calHidden.has("@" + m) ? "" : " checked"}>` +
      `${PERSON_ICON}<span class="chip-t" title="${esc(m)}">${esc(m)}${meMark(m)}</span></label>`).join("") +
      `<label class="no-tag"><input type="checkbox" data-mem=""${state.calHidden.has(NO_MEMBER) ? "" : " checked"}>멤버 없는 일정</label>`;
  }
  function renderTagFilter() {
    const box = $("cal-tags"), tags = shownTags();
    box.hidden = !tags.length && !state.calHidden.has(NO_TAG);
    box.innerHTML = tags.map((t) => `<label class="tag-chip" style="--tc:${tagColor(t)}"><input type="checkbox" data-tag="${esc(t)}"${state.calHidden.has("#" + t) ? "" : " checked"}>` +
      `<span class="chip-t" title="${esc(t)}">${esc(t)}</span></label>`).join("") +
      `<label class="no-tag"><input type="checkbox" data-tag=""${state.calHidden.has(NO_TAG) ? "" : " checked"}>태그 없는 일정</label>`;
  }

  // ------------------------------------------------------------ 저장 못 한 내용 보관 (끊김·충돌 대비)
  /** 키 = 스페이스 키|날짜 → {content, from: 바탕 문서 {content, mtime} | null, at}. 트레이 프로그램이 파일로 남김(앱을 꺼도 유지) */
  let pendMem = {};
  const copyText = (t) => {
    if (bridge && bridge.copyText) bridge.copyText(t);
    else if (navigator.clipboard) navigator.clipboard.writeText(t).catch(() => {});
  };
  const pendGet = (k) => pendMem[k] || null;
  function pendSet(k, v) {
    if (!v && !(k in pendMem)) return;   // 없는 것 지우기는 알리지 않음 (자동 저장마다 파일을 읽지 않게. 보관·저장은 할일 창만 함)
    if (v) pendMem[k] = v; else delete pendMem[k];
    if (bridge && bridge.setUnsaved) bridge.setUnsaved(k, v ? JSON.stringify(v) : "");
  }

  /** 줄 단위 LCS: o[i] 가 a 의 몇 번째 줄과 짝인지 (없으면 -1) */
  function lcsMap(o, a) {
    let p = 0, so = o.length, sa = a.length;
    while (p < so && p < sa && o[p] === a[p]) p++;
    while (so > p && sa > p && o[so - 1] === a[sa - 1]) { so--; sa--; }
    const map = new Int32Array(o.length).fill(-1);
    for (let i = 0; i < p; i++) map[i] = i;
    for (let i = so; i < o.length; i++) map[i] = i - so + sa;
    const n = so - p, m = sa - p;
    if (n && m) {
      if (n * m > 4e6) return null;
      const w = m + 1, dp = new Uint16Array((n + 1) * w);
      for (let i = n - 1; i >= 0; i--)
        for (let j = m - 1; j >= 0; j--)
          dp[i * w + j] = o[p + i] === a[p + j] ? dp[(i + 1) * w + j + 1] + 1 : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
      let i = 0, j = 0;
      while (i < n && j < m) {
        if (o[p + i] === a[p + j]) { map[p + i] = p + j; i++; j++; }
        else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) i++;
        else j++;
      }
    }
    return map;
  }
  /**
   * 3-way 합치기 (줄 단위): 바탕 base 에서 내가 고친 mine 과 남이 고친 theirs.
   * 다른 줄을 고쳤으면 둘 다 반영, 같은 자리에 둘 다 줄을 넣었으면 내 것 → 남의 것 순서로 둘 다. 같은 줄을 서로 다르게 고쳤으면 null
   */
  function merge3(base, mine, theirs, eol) {
    const sp = (t) => (t === "" ? [] : t.split(/\r\n|\n/));   // 빈 문서 = 줄 없음 (빈 줄 하나가 아님: 새 노트 동시 작성)
    const o = sp(base), a = sp(mine), b = sp(theirs);
    // 둘 다 끝에 줄바꿈 = 마지막 빈 줄은 따로 (끝에 각자 줄을 넣었을 때 사이에 빈 줄이 끼지 않게)
    const nl = a.length && b.length && a[a.length - 1] === "" && b[b.length - 1] === "";
    if (nl) { a.pop(); b.pop(); if (o.length && o[o.length - 1] === "") o.pop(); }
    const ma = lcsMap(o, a), mb = lcsMap(o, b);
    if (!ma || !mb) return null;
    const same = (x, y) => x.length === y.length && x.every((v, k) => v === y[k]);
    const out = [];
    let i = 0, ja = 0, jb = 0;
    for (;;) {
      let k = i;
      while (k < o.length && !(ma[k] >= 0 && mb[k] >= 0)) k++;
      const ea = k < o.length ? ma[k] : a.length, eb = k < o.length ? mb[k] : b.length;
      const oc = o.slice(i, k), ac = a.slice(ja, ea), bc = b.slice(jb, eb);
      if (same(ac, oc)) out.push(...bc);
      else if (same(bc, oc) || same(ac, bc)) out.push(...ac);
      else if (!oc.length) out.push(...ac, ...bc);
      else if (ac.length === oc.length && bc.length === oc.length) {
        // 붙어 있는 줄을 각자 고침 (할일 체크 등): 줄마다 바뀐 쪽, 같은 줄을 둘 다 다르게 고쳤을 때만 충돌
        for (let n = 0; n < oc.length; n++) {
          if (ac[n] !== oc[n] && bc[n] !== oc[n] && ac[n] !== bc[n]) return null;
          out.push(ac[n] !== oc[n] ? ac[n] : bc[n]);
        }
      } else return null;
      if (k >= o.length) break;
      out.push(o[k]);
      i = k + 1; ja = ea + 1; jb = eb + 1;
    }
    if (nl) out.push("");
    return out.join(eol);
  }

  // ------------------------------------------------------------ 문서 편집 칸 (할일 노트 / 일정 파일 같이 씀)
  /**
   * o = { body, footer, key(): 지금 보여줄 문서 키, fetch(key), post(key, content, baseMtime, base),
   *       empty(doc): 문서 없을 때 HTML, loaded(doc), progress: bool }
   */
  function DocPane(o) {
    const p = { doc: null, key: null, lines: null, eol: "\n", editing: false, editor: null };
    const host = document.createElement("div");
    host.className = "lp-host";
    let chain = Promise.resolve(true);
    let timer = null;

    function show(text, keepScroll) {
      if (!p.editor) {
        p.editor = window.DoganiEditor.create(host, {
          doc: text,
          readOnly: !canType(),
          tableHtml: MD.tableHtml,
          onChange(text, immediate) {
            p.lines = text.split("\n");
            // 글을 쓰기 시작하면 빈 노트 안내(마지막 노트 복사 버튼)는 바로 지움
            if (text.trim()) o.body.querySelector(":scope > .empty")?.remove();
            if (o.progress) setProgress(...countTasks(p.lines));
            clearTimeout(timer);
            timer = setTimeout(p.save, 700);
            if (immediate) p.save();   // 체크박스 클릭은 바로 저장
          },
          onFocus() { if (canType()) p.editing = true; },
          onBlur() { if (p.editing) endEdit(); },
          onSave: () => p.save(),
          // 줄 손잡이: 끌기 시작 = 저장, 다른 창(달력)에 놓았으면 편집 끝 (달력이 같은 노트를 고침 → 다시 읽기)
          onDragStart: () => p.save(),
          onDragEnd: (moved) => { if (bridge) bridge.setCalDrag(""); if (!moved) p.exit(); },
          // 달력에 놓을 수 있는 줄(할일·장식)이면 달력 창에 알림 (끄는 중엔 데이터를 못 읽어서, 달력 칸 강조를 이걸로 판단)
          // 장식 데이터는 브리지로만 보냄 (text/plain 은 줄 글자 그대로 → 다른 앱에 놓아도 글자가 들어감)
          dragData: (i, text) => {
            const d = o.dragData ? o.dragData(i, text) : null;
            const cal = d || (o.calPayload ? o.calPayload(i, text) : null);
            if (bridge) bridge.setCalDrag(cal || "");
            return d;
          },
          openUrl: (url) => (bridge ? bridge.openUrl(url) : window.open(url)),
        });
      } else if (p.editor.getDoc() !== text) {
        p.editor.setDoc(text, keepScroll);
      }
      p.editor.setReadOnly(!canType());
      o.body.classList.add("has-editor");
      // 이미 붙어 있으면 다시 붙이지 않음 (뗐다 붙이면 스크롤이 맨 위로)
      if (host.parentNode !== o.body || o.body.childNodes.length !== 1) o.body.replaceChildren(host);
    }

    p.message = (html) => {
      p.lines = null;
      o.body.classList.remove("has-editor");
      o.body.innerHTML = `<div class="empty">${html}</div>`;
      if (o.progress) $("note-bar").parentElement.parentElement.classList.add("no-tasks");
      o.footer.textContent = "";
    };

    /** 보관해 둔 (저장 못 한) 내용을 편집 칸에 띄움. 바탕 문서 = 그 내용을 쓰기 시작한 서버 문서 */
    function showPending(key, pend, keepScroll) {
      p.doc = pend.from ? { exists: true, content: pend.from.content, mtime: pend.from.mtime } : { exists: false };
      p.key = key;
      p.pk = o.pendKey(key);
      p.eol = pend.content.includes("\r\n") ? "\r\n" : "\n";
      p.lines = pend.content.split(/\r\n|\n/);
      show(p.lines.join("\n"), keepScroll);
      if (o.progress) setProgress(...countTasks(p.lines));
    }

    /** 편집 칸 내용이 바탕 문서(서버에서 마지막으로 받거나 저장한 것)와 다름 */
    const dirty = () => !!p.lines && !!p.doc && p.lines.join(p.eol) !== (p.doc.exists ? p.doc.content : "");

    p.load = async (keepScroll) => {
      const key = o.key(), pk = o.pendKey(key);
      let d;
      try {
        d = await o.fetch(key);
      } catch (e) {
        if (key !== o.key() || p.editing) return;
        if (p.key === key && dirty()) { o.footer.textContent = errorText(e); return; }   // 쓰던 내용은 그대로 (저장이 보관함)
        const pend = pendGet(pk);
        if (pend) {   // 끊김: 서버 대신 저장 못 한 내용을 보여 줌 (읽기 전용, 다시 연결되면 저장)
          showPending(key, pend, keepScroll);
          o.footer.textContent = `${errorText(e)} 저장하지 못한 내용은 보관했다가 다시 연결되면 저장합니다.`;
          return;
        }
        p.doc = null;
        p.message(`${esc(errorText(e))}<br><small>${esc(state.space ? state.space.server : "")}</small>`);
        return;
      }
      if (key !== o.key() || p.editing) return;
      // 아직 저장 안 된 변경이 있음(자동 저장 대기·저장 중): 덮지 않고 저장 → 그 사이 바뀐 서버 내용과 합쳐짐
      if (p.key === key && dirty()) { p.save(); return; }
      const pend = pendGet(pk);
      if (pend) {   // 저장 못 한 내용이 있으면 서버 것으로 덮지 않고 그걸 다시 저장 (그 사이 바뀌었으면 합침)
        if (o.loaded) o.loaded({ title: d.title });
        showPending(key, pend, keepScroll);
        if (!state.writable) { o.footer.textContent = "보관한 내용이 있지만 편집 권한이 없습니다."; return; }
        o.footer.textContent = "보관한 내용을 저장하는 중입니다.";
        p.save();
        return;
      }
      p.doc = d;
      p.key = key;
      p.pk = pk;
      if (!d.exists) {
        p.message(o.empty(d));
        if (o.loaded) o.loaded(d);
        return;
      }
      p.eol = d.content.includes("\r\n") ? "\r\n" : "\n";
      p.lines = d.content.split(/\r\n|\n/);
      show(p.lines.join("\n"), keepScroll);
      if (o.loaded) o.loaded(d);
      const t = new Date(d.mtime);
      o.footer.textContent =
        `마지막 수정 ${t.getMonth() + 1}/${t.getDate()} ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
    };

    /** 편집 중엔 다시 불러오지 않음 → 편집이 끝나면(endEdit) 놓친 변경을 불러옴 */
    p.reloadIfIdle = () => { if (!p.editing) p.load(true); else p.missed = true; };
    function endEdit() {
      p.editing = false;
      p.save().then(() => { if (p.missed && !p.editing) { p.missed = false; p.load(true); } });
    }
    /** 다시 연결됨: 편집 중이라 load 가 건너뛰는 경우에도 보관한 내용 저장 */
    p.retryPending = () => {
      if (p.editing && p.key != null && pendGet(p.pk)) p.save();
    };
    /** 지금 스페이스에 보관된, 열려 있지 않은 날짜의 노트도 저장. 같은 줄 충돌이면 그대로 둠 (그 날짜를 열면 물어봄) */
    // 할일 창만 (달력 창의 보관 목록은 시작할 때 받은 옛 사본이라 지운 줄을 되살릴 수 있음)
    p.flushOthers = () => {
      const sp = state.space, base = state.base;
      if (!sp || !base || !state.writable || state.mode !== "note") return;
      for (const pk of Object.keys(pendMem)) {
        if (!pk.startsWith(sp.key + "|") || pk === p.pk) continue;
        const pend = pendMem[pk], key = base + "|" + pk.slice(sp.key.length + 1);
        chain = chain.then(async () => {
          if (state.base !== base || pendGet(pk) !== pend || pk === p.pk) return;
          try {
            const cur = await o.fetch(key, base);
            const eol = pend.content.includes("\r\n") ? "\r\n" : "\n";
            const merged = merge3(pend.from ? pend.from.content : "", pend.content, cur.exists ? cur.content : "", eol);
            if (merged == null) return;
            if (!(cur.exists && cur.content === merged)) await o.post(key, merged, cur.exists ? cur.mtime : null, base);   // 이미 같으면 쓰지 않음
            if (pendGet(pk) === pend) pendSet(pk, null);
          } catch (e) {   // 권한이 없어졌으면 클립보드로 넘기고 지움, 그 밖엔 그대로 보관 (다음 연결이나 그 날짜를 열 때 다시)
            if (e.code === "read_only" && pendGet(pk) === pend) { copyText(pend.content); pendSet(pk, null); }
          }
        });
      }
    };

    /** 편집 칸을 합친 내용으로 맞춤. 저장하는 동안 더 쓴 게 있으면(posted ≠ 지금) 그것도 합침 */
    function adopt(posted, merged) {
      const now = p.lines.join(p.eol);
      const next = now === posted ? merged : merge3(posted, now, merged, p.eol);
      if (next == null) return false;
      if (next === now) return true;
      if (p.editor.composing()) return false;   // 한글 조합 중엔 안 넣음 → 다음 저장이 충돌·합치기로 다시 반영
      p.lines = next.split(/\r\n|\n/);
      p.editor.mergeDoc(p.lines.join("\n"));
      if (o.progress) setProgress(...countTasks(p.lines));
      return true;
    }

    /**
     * 저장은 순서대로 하나씩 (자동 저장이 겹치면 mtime 충돌이 나므로). 스페이스·날짜는 부른 시점 기준.
     * - 충돌(409, 그 사이 옵시디언·다른 팀원이 고침): 서버 것을 다시 받아 줄 단위로 합쳐 다시 저장.
     *   같은 줄을 서로 다르게 고쳤을 때만 물어봄 (내 것으로 덮기 / 서버 것 쓰기 + 내 것은 클립보드)
     * - 연결 끊김 등: 내용을 바탕 문서와 같이 보관 → 다시 연결되면(hello) 같은 방식으로 저장
     */
    p.save = () => {
      clearTimeout(timer);
      if (!p.lines || p.key == null) return chain;
      const called = p.lines.join(p.eol);
      // 스페이스 주소·보관 키는 노트 키에서 (스페이스를 바꾼 뒤 늦게 실행돼도 원래 스페이스에 저장)
      const key = p.key, base = key.slice(0, key.lastIndexOf("|")), pk = p.pk || o.pendKey(key), eol = p.eol;
      const here = () => p.key === key && state.base === base;
      const snap = p.doc;   // 부른 시점의 바탕 (실행될 때 날짜를 옮겼으면 이걸 씀)
      chain = chain.then(async () => {
        // 실행될 때의 편집 칸 내용 (앞 저장이 합친 결과가 들어갔을 수 있음). 다른 노트로 옮겼으면 부른 시점 것
        const content = here() && p.lines ? p.lines.join(p.eol) : called;
        // 이 내용의 바탕 = 서버에서 마지막으로 받은 것 (앞 저장이 끝났으면 그 결과)
        let from = (here() ? p.doc : snap) || { exists: false };
        if (from.exists && from.content === content) { pendSet(pk, null); return true; }
        let mine = content;
        for (let tries = 0; tries < 4; tries++) {
          let e;
          try {
            const d = await o.post(key, mine, from.exists ? from.mtime : null, base);
            pendSet(pk, null);
            if (here()) {
              p.doc = d;
              // 저장 중에 더 쓴 내용과 합치지 못함: 바탕을 '합치기 전 내 것'으로 두고 mtime 을 틀리게 → 다음 저장이 충돌 → 물어봄
              if (mine !== content && !adopt(content, mine)) p.doc = { exists: true, content, mtime: -1 };
              if (p.doc.mtime !== -1 && dirty()) { clearTimeout(timer); timer = setTimeout(p.save, 700); }   // 저장 중에 더 쓴 것이 남음
              if (o.loaded) o.loaded(d);
              o.footer.textContent = mine !== content ? "다른 곳에서 고친 내용과 합쳐 저장했습니다." : "저장했습니다.";
            }
            return true;
          } catch (err) { e = err; }
          if (e.code === "conflict") {
            let cur = null;
            try { cur = await o.fetch(key, base); } catch (err) { e = err; }
            if (cur) {
              const theirs = cur.exists ? cur.content : "";
              const merged = merge3(from.exists ? from.content : "", mine, theirs, eol);
              if (merged !== null) { mine = merged; from = cur; continue; }
              // 같은 줄을 서로 다르게 고침 → 사용자에게
              if (confirm("그 사이 옵시디언이나 다른 팀원이 같은 줄을 다르게 고쳤습니다.\n\n" +
                "확인을 누르면 내 내용으로 덮어쓰고, 상대 내용은 클립보드에 복사합니다.\n취소를 누르면 상대 내용을 쓰고, 내 내용은 클립보드에 복사합니다.")) {
                copyText(theirs);
                from = cur; continue;
              }
              copyText(mine);
              pendSet(pk, null);
              if (here()) {
                p.doc = cur;
                p.lines = theirs.split(/\r\n|\n/);
                p.eol = theirs.includes("\r\n") ? "\r\n" : "\n";
                if (cur.exists) { show(p.lines.join("\n"), true); if (o.loaded) o.loaded(cur); }
                else p.load(true);
                o.footer.textContent = "상대 내용으로 바꿨습니다. 내 내용은 클립보드에 복사했습니다.";
              }
              return false;
            }
          }
          if (e.code === "read_only") {   // 권한이 없어짐: 보관해도 저장할 수 없음
            copyText(mine);
            pendSet(pk, null);
            if (here()) {   // 편집 칸을 서버 것으로 (변경이 남으면 load 마다 다시 저장·복사함)
              p.doc = null;
              p.lines = null;
              p.editing = p.missed = false;
              p.load(true).then(() => { o.footer.textContent = errorText(e) + " 쓰던 내용은 클립보드에 복사했습니다."; });
            }
            return false;
          }
          // 끊김·로그인 풀림·서버 오류: 보관 → 다시 연결되면 저장
          pendSet(pk, { content: mine, from: from.exists ? { content: from.content, mtime: from.mtime } : null, at: Date.now() });
          if (here()) o.footer.textContent = errorText(e) + " 쓴 내용은 보관했다가 다시 연결되면 저장합니다.";
          // 요청 자체가 안 감(망 끊김) = SSE 도 죽었을 수 있음 → 바로 다시 연결 (연결되면 hello 에서 다시 저장)
          // POST 만 막히는 망이면 저장 → 다시 연결 → 저장이 되풀이되므로 1분에 한 번만
          if (!e.code && state.base === base && state.status === "online" && Date.now() - (state.saveReconnectAt || 0) > 60000) {
            state.saveReconnectAt = Date.now();
            connect();
          }
          return false;
        }
        pendSet(pk, { content: mine, from: from.exists ? { content: from.content, mtime: from.mtime } : null, at: Date.now() });
        if (here()) o.footer.textContent = "계속 충돌해서 저장하지 못했습니다. 쓴 내용은 보관했다가 다시 불러올 때 저장합니다.";
        return false;
      });
      return chain;
    };

    p.exit = () => {
      if (p.editor && p.editor.hasFocus()) p.editor.blur();   // onBlur 에서 저장
      else if (p.editing) endEdit();
    };

    /** 문서가 없는 날짜/파일: 빈 문서로 작성 시작 (첫 저장 때 파일 생성) */
    p.startBlank = () => {
      if (!canType() || p.lines || !p.doc || p.doc.exists || p.doc.no_dir) return false;
      p.lines = [""];
      p.eol = "\n";
      p.key = o.key();
      p.pk = o.pendKey(p.key);
      show("", false);
      p.editor.focus(0);
      return true;
    };

    p.setReadOnly = (r) => { if (p.editor) p.editor.setReadOnly(r); };

    o.body.addEventListener("click", (e) => {
      const copy = e.target.closest(".copy-prev");
      if (copy) return copyPrev(copy.dataset.prev);
      if (state.status === "online") p.startBlank();
    });
    return p;
  }

  // ------------------------------------------------------------ note (할일 창)
  const note = DocPane({
    body: $("note-body"),
    footer: $("note-footer"),
    progress: true,
    key: () => state.base + "|" + state.noteDate,
    fetch: (key, base) => api(`/note?date=${key.split("|").pop()}`, undefined, base),
    pendKey: (key) => (state.space ? state.space.key : "") + "|" + key.split("|").pop(),
    post: (key, content, base_mtime, base) => api("/note", { date: key.split("|").pop(), content, base_mtime }, base),
    // 줄 손잡이로 달력에 끌 때: 개인 스페이스의 할일 줄이면 dogani-task 데이터 (아니면 줄 글자 그대로)
    dragData: (i, text) => (state.space && state.space.kind === "me" && TASK_BOX_RE.test(text)
      ? TASK_DRAG + JSON.stringify({ sp: state.space.key, note: state.noteDate, line: i, raw: text }) : null),
    // 할일 아닌 글자 줄 = 달력 장식 (브리지로 달력 창에만 전달)
    calPayload: (i, text) => {
      const t = !TASK_BOX_RE.test(text) && state.space && state.writable && decoText(text);
      return t ? DECO_DRAG + JSON.stringify({ sp: state.space.key, text: t }) : null;
    },
    empty(d) {
      if (d.no_dir) {
        return state.writable && state.space.kind === "me"
          ? "todo 폴더가 지정되지 않았습니다.<br>트레이 &gt; todo 경로 &gt; 찾아보기..."
          : "상대방이 todo 폴더를 지정하지 않았습니다.";
      }
      return state.writable ? `이 날짜의 노트가 없습니다.<br>클릭해서 작성${copyButton(d)}` : "이 날짜의 노트가 없습니다.";
    },
    loaded(d) {
      if (d.exists) {
        setProgress(d.done, d.total);
        if (!d.content.trim() && copyButton(d)) $("note-body").insertAdjacentHTML("afterbegin", `<div class="empty">${copyButton(d)}</div>`);
      }
      reportNoteDate();
    },
  });

  function renderSchedRow() {
    const row = $("note-sched");
    // 노트(할일) 창은 지금 탭의 스페이스 일정만 보여 줌 (달력은 보이는 스페이스를 모두 합침)
    const key = state.space && state.space.key;
    const own = (state.schedItems || []).filter((ev) => (ev.spaces || []).includes(key));
    const html = CAL.dayEvents(state.noteDate, CAL.index(own));
    row.innerHTML = html;
    row.hidden = !html;
  }

  /** 노트가 없거나 비었을 때: 직전 노트를 그대로 복사하는 버튼 */
  function copyButton(d) {
    if (!state.writable || !d || !d.prev || d.no_dir) return "";
    return `<button class="copy-prev" data-prev="${d.prev}">마지막 노트 복사<span>(${dayLabel(d.prev)})</span></button>`;
  }

  async function copyPrev(prev) {
    const date = state.noteDate;
    const target = note.doc;
    try {
      const src = await api(`/note?date=${prev}`);
      if (!src.exists) throw Object.assign(new Error(), { code: "write_failed" });
      await api("/note", { date, content: src.content, base_mtime: target && target.exists ? target.mtime : null });
    } catch (e) {
      $("note-footer").textContent = errorText(e);
      return;
    }
    if (state.noteDate === date) note.load(false);
  }

  function setProgress(done, total) {
    $("note-bar").parentElement.parentElement.classList.toggle("no-tasks", total === 0);
    $("note-count").textContent = total ? `${done}/${total}` : "";
    const bar = $("note-bar");
    bar.style.width = total ? `${(done / total) * 100}%` : "0";
    bar.classList.toggle("done", total > 0 && done === total);
  }

  function countTasks(lines) {
    let done = 0, total = 0, fence = false;
    for (const line of lines) {
      if (/^\s*(```|~~~)/.test(line)) { fence = !fence; continue; }
      const m = !fence && line.match(/^\s*(?:[-*+]|\d+[.)])\s+\[(.)\]/);
      if (m) { total++; if (m[1] !== " ") done++; }
    }
    return [done, total];
  }

  /** 제목은 파일명이 아니라 날짜로 (2026-11-18 (수)) */
  function reportNoteDate() {
    const t = dayLabel(state.noteDate);
    $("note-title").textContent = t;
    $("note-title").classList.toggle("is-today", state.noteDate === state.today);
    if (bridge) bridge.noteDateChanged(state.noteDate, t, state.noteDate === state.today);
  }

  function showNote(date) {
    note.exit();
    state.noteDate = date;
    setProgress(0, 0);
    renderSchedRow();
    reportNoteDate();
    return note.load(false);
  }

  // ------------------------------------------------------------ calendar (달력 창)
  // 받아 온 월 데이터 (일정만 바뀌거나 선택 날짜만 바뀌면 다시 받지 않고 그리기만)
  let monthData = { key: "", days: {}, failed: false };
  let monthReq = null;

  async function loadMonth(force) {
    ensureHolidays();
    const ym = state.calYM, key = state.base + "|" + ym;
    const [y, m] = ym.split("-").map(Number);
    $("cal-title").textContent = `${y}년 ${m}월`;
    // 같은 달을 동시에 여러 번 부르면(접속 직후 등) 요청 하나를 같이 기다림
    if (force || !monthReq || monthReq.key !== key) {
      const req = { key };
      req.p = api(`/month?ym=${ym}`)
        .then((r) => ({ key, days: r.days, failed: false }), (e) => ({ key, days: {}, failed: errorText(e) }))
        .finally(() => { if (monthReq === req) monthReq = null; });
      monthReq = req;
    }
    const data = await monthReq.p;
    if (key !== state.base + "|" + state.calYM) return;
    monthData = data;
    renderMonth();
  }

  let lastMonthHtml = "";
  const evSel = new Set();   // Ctrl+클릭으로 고른 일정 번호(data-k). 칩을 누르거나 끌어 놓으면 한꺼번에 더함
  function renderMonth() {
    if (monthData.key !== state.base + "|" + state.calYM) return loadMonth();
    const r = CAL.renderMonth({ ym: state.calYM, days: monthData.days, idx: state.sched, decos: state.decos, today: state.today, selected: state.noteDate });
    // 내용이 같으면 다시 그리지 않음 (스페이스 전환 때 여러 번 불려 깜빡이던 것)
    if (r.html !== lastMonthHtml) { lastMonthHtml = r.html; $("cal-grid").innerHTML = r.html; CAL.fitCells($("cal-grid")); evSel.clear(); }   // 다시 그리면 번호가 바뀌어 선택 풂
    renderTagFilter();
    renderMemberFilter();
    $("cal-footer").textContent = monthData.failed || `노트 ${r.notes}개 · 완료 ${r.done}/${r.total}`;
  }

  function setCalView(view) {
    state.calView = view;
    $("view-cal").hidden = view !== "month";
    loadMonth();
  }

  // ------------------------------------------------------------ edit permission
  /** soft = 편집 중인 내용은 그대로 두고 읽기 전용으로만 (권한이 바뀐 경우, 쓰던 글 날리지 않게) */
  function setWritable(w, soft) {
    state.writable = w;
    document.body.classList.toggle("writable", w);
    if (!w && !soft) note.exit();
    note.setReadOnly(!canType());
    refresh();
  }

  function checkDateRollover() {
    const now = iso(new Date());
    if (now === state.today) return;
    const prev = state.today;
    state.today = now;
    for (const y in holYears) if (holYears[y] === "ok") delete holYears[y];   // 하루 한 번은 공휴일 다시 확인 (대체·임시공휴일)
    // 오늘 노트를 보고 있었다면 자정에 다음 날로 넘어간다 (편집 중이면 끝난 뒤에)
    if (state.mode === "note" && state.noteDate === prev && !note.editing) showNote(now);
    if (state.mode === "cal" && state.calView === "month") renderMonth();
  }

  // ------------------------------------------------------------ events
  // 화살표는 토/일·쉬는 날을 건너뜀 (그 날 노트는 달력에서 눌러서 열기)
  $("note-prev").addEventListener("click", () => App.step(-1));
  $("note-next").addEventListener("click", () => App.step(1));
  $("note-today").addEventListener("click", () => App.step(0));

  // 편집 영역 밖(바 등)을 누르거나 창이 포커스를 잃으면 편집 끝
  document.addEventListener("mousedown", (e) => {
    if (note.editing && !e.target.closest("#note-body")) note.exit();
    if (note.editor) note.editor.highlightLine(null);   // 달력에서 누른 할일 줄 강조: 아무 데나 누르면 풀림
  });
  window.addEventListener("blur", () => note.exit());

  $("cal-prev").addEventListener("click", () => { state.calYM = addMonths(state.calYM, -1); loadMonth(); });
  $("cal-next").addEventListener("click", () => { state.calYM = addMonths(state.calYM, 1); loadMonth(); });
  $("cal-grid").addEventListener("click", (e) => {
    if (dragged) { dragged = false; return; }  // 드래그로 기간 고르거나 일정 옮긴 뒤의 click 은 무시
    const ev = evOf(e.target.closest(".ev[data-k]"));
    // 할일: 체크 네모 = 노트 줄 체크/해제, 나머지 = 그 노트 열기
    if (ev && ev.task) {
      if (e.target.closest(".ev-check")) {
        if (canEdit(ev)) editTask(ev.task, toggleTaskLine).catch((err) => alert("체크 실패: " + (err.message || err)));
      } else if (bridge) bridge.openTask(ev.task.note, ev.task.line, ev.task.raw);   // 할일 창에서 그 줄로 가서 강조
      return;
    }
    // 일정 클릭 = 수정 팝업 (읽기 전용이면 그냥 노트 열기)
    if (ev && canEdit(ev)) { evAdd.open("", "", e.clientX, e.clientY, ev); return; }
    const cell = e.target.closest(".cal-day");
    if (!cell) return;
    state.noteDate = cell.dataset.date;
    for (const c of $("cal-grid").querySelectorAll(".cal-day.selected")) c.classList.remove("selected");
    cell.classList.add("selected");
    lastMonthHtml = "";   // 화면을 직접 바꿨으니 다음 그리기는 비교 없이
    if (bridge) bridge.openNote(cell.dataset.date);
  });

  // 달력 칸 우클릭·드래그: 일정 추가 팝업 → 일정 파일 끝에 한 줄 덧붙임
  // 일정 = 하는 일: 시작 날짜 필수, 끝은 비우면 하루 (여러 날이면 띠)
  // 기간 = 이어지는 상태: 화살표(*기울임*)로 저장, 시작·끝 중 한쪽 비우면 미정 (끝만 = 마감)
  const TIMES = [];
  for (let h = 7; h <= 22; h++) for (const m of ["00", "30"]) TIMES.push(`${h}:${m}`);
  $("ev-add-t1").innerHTML = '<option value="">종일</option>' + TIMES.map((t) => `<option>${t}</option>`).join("");
  $("ev-add-t2").innerHTML = '<option value="">-</option>' + TIMES.map((t) => `<option>${t}</option>`).join("");
  const LIST_RE = /^(\s*(?:[-*+]|\d+[.)])\s+(?:\[.\]\s+)?)/;
  const WHO_RE = /\s+~[0-9a-f]{8}(?:>[0-9a-f]{8})?\s*$/;   // 줄 끝 작성자 꼬리표 (schedule.py _WHO_RE)
  const yymmdd = (v) => v.slice(2).replace(/-/g, "");
  const cleanField = (v) => String(v || "").replace(/,/g, " ").trim();
  /** 일정 dict → 파일 한 줄 (팝업 입력값과 같은 모양: kind, s, e, t1, t2, about, bold, off, title, tags[], place, memo) */
  function schedLine(o) {
    if (o.kind === "off") return offLine(o);
    let date = o.s && o.e && o.s !== o.e ? `${yymmdd(o.s)}-${yymmdd(o.e)}` : o.s && o.e ? yymmdd(o.s) : o.s ? `${yymmdd(o.s)}-` : `-${yymmdd(o.e)}`;
    if (o.off) date = `==${date}==`;
    const mk = (o.bold ? "**" : "") + (o.kind === "span" ? "*" : "");
    const time = o.t1 ? " " + o.t1 + (o.t2 ? "-" + o.t2 : "") + (o.about ? "?" : "") : "";
    const parts = [mk + date + mk + time, cleanField(o.title)];
    const tags = o.tags.map((t) => t.replace(/[#\s]/g, "")).filter(Boolean).map((t) => "#" + t);
    const ms = [...new Set((o.members || []).map(cleanName).filter(Boolean))].map((m) => "@" + m);
    if (tags.length || ms.length) parts.push([...tags, ...ms].join(" "));   // 태그 칸: "#근태 @홍길동"
    const place = cleanField(o.place), memo = cleanField(o.memo);
    if (place || memo) parts.push(place);  // 장소 없이 메모만이면 "제목, , 메모"
    if (memo) parts.push(memo);
    return parts.join(", ");
  }
  /** 근태 줄 "==날짜==, 제목, @사람" (태그 없음. 사람 없으면 모두 쉬는 날) */
  function offLine(o) {
    const date = o.s && o.e && o.s !== o.e ? `${yymmdd(o.s)}-${yymmdd(o.e)}` : yymmdd(o.s || o.e);
    const ms = [...new Set((o.members || []).map(cleanName).filter(Boolean))];
    const parts = [`==${date}==`, cleanField(o.title)];
    const tagCol = ms.map((m) => "@" + m).join(" ");
    const memo = cleanField(o.memo);
    if (tagCol || memo) parts.push(tagCol);
    if (memo) parts.push("", memo);
    return parts.join(", ");
  }
  /** 화면의 일정(ev) → schedLine 입력 모양 (시작/끝 미정은 그쪽을 비움) */
  function evToObj(ev) {
    const [t1, t2] = (ev.time || "").split("-");
    return {
      kind: ev.off ? "off" : ev.italic ? "span" : "event",
      s: ev.open === "start" ? "" : ev.start, e: ev.open === "end" ? "" : ev.end,
      t1: t1 || "", t2: t2 || "", about: !!ev.uncertain, bold: !!ev.important,
      title: ev.title, tags: ev.off ? [] : ev.tags || [], members: ev.members || [], place: ev.place || "", memo: ev.memo || "",
    };
  }
  // ---- 할일(노트 체크박스 줄) 날짜: 옵시디언 Tasks 형식 `🛫 시작` `📅 마감` (YYYY-MM-DD)
  let calDrag = "";                   // 할일 창에서 달력에 놓을 수 있는 줄을 끄는 중 = 그 데이터 (파이썬이 App.setCalDrag 로 알려 줌)
  const TASK_DRAG = "dogani-task:";   // 할일 창 손잡이 → 달력 끌어 놓기 (text/plain 앞머리)
  const DECO_DRAG = "dogani-deco:";   // 할일·가로선 아닌 줄 → 달력 장식
  /** 노트 줄 → 장식 글자 (목록·인용·제목 기호와 굵게 등 기호 뺌). 가로선·빈 줄은 "" */
  function decoText(line) {
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) return "";   // 가로선
    if (/^\s*(```|~~~|\||>\s*\[!|[\w-]+:\s*$)/.test(line)) return "";   // 코드 울타리·표·콜아웃 머리·속성 키
    return line.replace(/^\s*(?:>\s*)*(?:#{1,6}\s+)?(?:(?:[-*+]|\d+[.)])\s+)?/, "")
      .replace(/\*\*|==|~~|`/g, "").replace(/\s+\^[\w-]+$/, "").trim().slice(0, 60);
  }
  const TASK_BOX_RE = /^(\s*(?:[-*+]|\d+[.)])\s+\[)(.)(\])/;
  const TASK_DATE_RE = /\s*(🛫|📅)️?\s*(\d{4}-\d{2}-\d{2})/gu;
  const TASK_DONE_RE = /\s*✅️?\s*\d{4}-\d{2}-\d{2}/gu;
  /** 체크 ↔ 해제. 날짜(🛫/📅) 붙은 할일이면 체크할 때 `✅ 오늘` 을 줄 끝(블록 id 앞)에, 해제하면 뺌 (편집기 toggleTaskAt 와 같은 규칙) */
  function toggleTaskLine(l) {
    const checking = (l.match(TASK_BOX_RE) || [])[2] === " ";
    l = l.replace(TASK_BOX_RE, (_, a, c, b) => a + (checking ? "x" : " ") + b).replace(TASK_DONE_RE, "");
    if (!checking || !taskDatesOf(l)) return l;
    const m = l.trimEnd().match(/\s\^[\w-]+$/), body = l.trimEnd();
    return (m ? body.slice(0, -m[0].length) : body) + " ✅ " + CAL.iso(new Date()) + (m ? m[0] : "");
  }
  /** 줄의 날짜 → {s, e} (없으면 null). 서버 dated_tasks 와 같은 규칙 */
  function taskDatesOf(l) {
    const d = {};
    for (const m of l.matchAll(TASK_DATE_RE)) d[m[1]] = m[2];
    if (!d["🛫"] && !d["📅"]) return null;
    const s = d["🛫"] || d["📅"], e = d["📅"] || s;
    return s <= e ? { s, e } : { s: e, e: s };
  }
  /** 줄의 날짜를 s~e 로 바꿈 (하루 = 📅 만). 줄 끝 블록 id(^abc) 앞에 둠 */
  function setTaskDates(l, s, e) {
    const body = l.replace(TASK_DATE_RE, "").trimEnd(), m = body.match(/\s\^[\w-]+$/);
    const head = m ? body.slice(0, -m[0].length) : body, dates = s === e ? ` 📅 ${e}` : ` 🛫 ${s} 📅 ${e}`;
    return head + dates + (m ? m[0] : "");
  }
  /** 할일 줄 고치기: t = {sp, note, line, raw}, fn(줄) → 새 줄. 줄 번호가 안 맞으면 같은 글자 줄을 찾음. 충돌 시 한 번 더 */
  async function editTask(t, fn) {
    const sp = spaceByKey(t.sp), base = spBase(sp);
    if (!sp) throw new Error("스페이스를 찾지 못했습니다.");
    for (let tries = 0; tries < 2; tries++) {
      try {
        const d = await api(`/note?date=${t.note}`, undefined, base);
        if (!d.exists) throw new Error("노트를 찾지 못했습니다.");
        const eol = d.content.includes("\r\n") ? "\r\n" : "\n", lines = d.content.split(/\r\n|\n/);
        const i = lines[t.line] === t.raw ? t.line : lines.indexOf(t.raw);
        if (i < 0 || !TASK_BOX_RE.test(lines[i])) throw new Error("노트에서 이 할일 줄을 찾지 못했습니다 (그 사이 바뀜).");
        lines[i] = fn(lines[i]);
        const r = await api("/note", { date: t.note, content: lines.join(eol), base_mtime: d.mtime }, base);
        undo.note({ sp, date: t.note, before: d.content, mtime: r && r.mtime });
        await loadSchedule(sp);
        return;
      } catch (e) {
        if (e.code === "conflict" && tries === 0) continue;
        throw e;
      }
    }
  }
  /** 달력에서 끌기: mode = move(통째로) | start(앞 가장자리) | end(뒤 가장자리) → [시작, 끝] */
  function dragRange(ev, mode, n) {
    if (mode === "start") return sorted(CAL.addDays(ev.start, n), ev.end);
    if (mode === "end") return sorted(ev.start, CAL.addDays(ev.end, n));
    return [CAL.addDays(ev.start, n), CAL.addDays(ev.end, n)];
  }

  /**
   * 달력 되돌리기(Ctrl+Z): 달력에서 한 저장마다 고치기 전 파일 내용을 기억.
   * 한 동작(여러 스페이스 저장)은 group() 으로 묶어 한 번에 되돌림. 그 사이 파일이 바뀌었으면(mtime 다름) 되돌리지 않음
   */
  const undo = {
    stack: [], cur: null,
    note(x) { if (this.cur) this.cur.push(x); else this.stack.push([x]); if (this.stack.length > 30) this.stack.shift(); },
    async group(fn) {
      const outer = !this.cur;
      if (outer) this.cur = [];
      try { return await fn(); } finally {
        if (outer) { if (this.cur.length) this.stack.push(this.cur); this.cur = null; }
      }
    },
    async pop() {
      const g = this.stack.pop();
      if (!g) return;
      const errs = [];
      for (const x of g.reverse()) {
        const base = spBase(x.sp), url = x.date ? `/note?date=${x.date}` : "/sched-doc";
        try {
          const d = await api(url, undefined, base);
          if (x.mtime && d.mtime !== x.mtime) { errs.push(`${x.sp.name}: 그 사이 파일이 바뀌어 되돌리지 않음`); continue; }
          await api(x.date ? "/note" : "/sched-doc", { date: x.date, content: x.before, base_mtime: d.mtime }, base);
          await loadSchedule(x.sp);
        } catch (e) { errs.push(`${x.sp.name}: ${e.message || e}`); }
      }
      if (errs.length) alert("되돌리기 실패: " + errs.join(" / "));
    },
  };
  /** 스페이스 sp 의 일정 파일 고치기: fn(text) → 새 text. 충돌(409) 나면 다시 읽고 한 번 더 */
  async function editSchedDoc(sp, fn) {
    const base = spBase(sp);
    for (let tries = 0; tries < 2; tries++) {
      try {
        const d = await api("/sched-doc", undefined, base);
        if (d.no_dir) throw new Error("스페이스 폴더가 지정되지 않았습니다.");
        const r = await api("/sched-doc", { content: fn(d.exists ? d.content : ""), base_mtime: d.exists ? d.mtime : null }, base);
        undo.note({ sp, before: d.exists ? d.content : "", mtime: r && r.mtime });
        await loadSchedule(sp);   // 달력은 rebuildSched 가 다시 그림
        return;
      } catch (e) {
        if (e.code === "conflict" && tries === 0) continue;  // 그 사이 파일이 바뀜
        throw e;
      }
    }
  }
  /**
   * 일정 쓰기: line 을 keys(스페이스 키들)에 둠. ev(수정 중인 일정)의 사본(copies)은
   * keys 에 든 스페이스면 첫 줄을 바꾸고(목록 기호·들여쓰기 유지) 나머지는 지움. 사본이 없는 스페이스엔 끝에 덧붙임.
   * line = null 이면 모든 사본 삭제. 스페이스마다 따로 저장 → 일부 실패하면 실패한 곳만 모아 알림
   */
  const writeEv = (ev, line, keys) => undo.group(() => writeEv0(ev, line, keys));
  async function writeEv0(ev, line, keys) {
    const copies = ev ? ev.copies || [] : [];
    const errs = [], targets = new Set([...copies.map((c) => c.sp), ...keys]);
    for (const key of targets) {
      const sp = spaceByKey(key);
      if (!sp) { errs.push(`${key}: 스페이스를 찾지 못했습니다`); continue; }
      const mine = copies.filter((c) => c.sp === key), want = line !== null && keys.includes(key);
      try {
        await editSchedDoc(sp, (text) => {
          const lines = text.split("\n");
          mine.forEach((c, n) => {
            const i = lines.findIndex((l) => l.trim() === c.raw);
            if (i < 0) throw new Error("일정 파일에서 이 줄을 찾지 못했습니다 (다른 일정 파일에 있거나 그 사이 바뀜).");
            if (want && n === 0) {
              const lead = lines[i].match(/^\s*/)[0], m = lines[i].trim().match(LIST_RE);
              const who = (lines[i].match(WHO_RE) || [""])[0].trimEnd();   // 작성자 꼬리표 유지 → 서버가 수정자를 붙임
              lines[i] = lead + (m ? m[1] : "") + line + who + (lines[i].endsWith("\r") ? "\r" : "");
            } else lines.splice(i, 1);
          });
          let out = lines.join("\n");
          if (want && !mine.length) out = (out && !out.endsWith("\n") ? out + "\n" : out) + line + "\n";
          return out;
        });
      } catch (e) {
        errs.push(`${sp.name}: ${e.message || e}`);
      }
    }
    if (errs.length) {
      const err = new Error(errs.join(" / "));
      err.partial = errs.length < targets.size;  // 일부는 저장됨
      throw err;
    }
  }

  const evAdd = {
    kind: "event",
    ev: null,  // 수정 중인 일정 (null = 새로 추가)
    open(start, end, x, y, ev) {
      if (!state.writable) return;
      const f = $("ev-add");
      f.reset();
      this.ev = ev || null;
      const o = ev ? evToObj(ev) : null;
      $("ev-add-start").value = o ? o.s : start;
      $("ev-add-end").value = o ? (o.e !== o.s ? o.e : "") : end > start ? end : "";
      if (o) {
        $("ev-add-name").value = o.title;
        $("ev-add-bold").checked = o.bold;
        $("ev-add-about").checked = o.about;
        for (const [id, t] of [["ev-add-t1", o.t1], ["ev-add-t2", o.t2]]) {
          const sel = $(id);
          if (t && ![...sel.options].some((op) => op.value === t)) sel.add(new Option(t, t));  // 7:15 같은 30분 단위 밖 시각
          sel.value = t;
        }
        $("ev-add-tags").value = o.tags.join(", ");
        $("ev-add-members").value = o.members.join(", ");
      }
      // 이 달에 쓴 태그: 눌러서 넣고 빼기
      const picks = shownTags();
      $("ev-add-tagpick").hidden = !picks.length;
      $("ev-add-tagpick").innerHTML = picks.map((t) => `<button type="button" class="tag-chip" style="--tc:${tagColor(t)}" data-tag="${esc(t)}">${esc(t)}</button>`).join("");
      // 멤버: 나·팀원·이 달에 쓴 이름. 만든 사람을 자동으로 넣지 않음 (만든 사람은 서버가 줄 끝 꼬리표로)
      const mp = memberPicks();
      $("ev-add-mempick").hidden = !mp.length;
      $("ev-add-mempick").innerHTML = mp.map((m) => `<button type="button" data-tag="${esc(m)}">@${esc(m)}${meMark(m)}</button>`).join("");
      this.markTags();
      if (o) {
        $("ev-add-place").value = o.place;
        $("ev-add-memo").value = o.memo;
      }
      const hasMemo = !!(o && o.memo);
      $("ev-add-memo-row").hidden = $("ev-add-memo-k").hidden = !hasMemo;
      $("ev-add-memo-btn").hidden = hasMemo;
      // 스페이스 고르기: 숨긴 것 포함 전부 (새 일정 = 지금 보는 스페이스, 수정 = 그 일정이 있는 스페이스들)
      const keys = ev ? ev.spaces || [] : [state.space && state.space.key];
      // 숨긴(체크 끈) 스페이스도 고를 수 있게 모두 띄움 (흐리게 표시)
      // 연결 안 된 스페이스는 흐리게·못 고름 (툴팁으로 이유)
      $("ev-add-spaces").innerHTML = schedSpaces().map((sp) => `<label title="${sp.kind === "me" ? "내 PC" : "다른 PC"}${spOnline(sp) ? "" : " · 연결되지 않았습니다"}${state.calHidden.has(sp.key) ? " · 달력에서 숨겼습니다" : ""}"${state.calHidden.has(sp.key) || !spOnline(sp) ? ' class="sp-hidden"' : ""}>` +
        `<input type="checkbox" data-key="${esc(sp.key)}"${keys.includes(sp.key) ? " checked" : ""}${state.roSpaces.has(sp.key) || !spOnline(sp) ? " disabled" : ""}>${esc(sp.name)}</label>`).join("");
      $("ev-add-del").hidden = !ev;
      $("ev-add-ok").textContent = ev ? "저장" : "추가";
      $("ev-add-err").textContent = "";
      // 근태는 근태끼리만: 수정할 때 일정·기간 ↔ 근태로는 못 바꿈 (일정 ↔ 기간은 표시만 달라 바꿀 수 있음)
      for (const b of $("ev-add-seg").querySelectorAll("button")) b.disabled = !!o && (b.dataset.kind === "off") !== (o.kind === "off");
      this.setKind(o ? o.kind : "event");
      f.hidden = false;
      const w = f.offsetWidth, h = f.offsetHeight;
      f.style.left = Math.max(4, Math.min(x, innerWidth - w - 4)) + "px";
      f.style.top = Math.max(4, Math.min(y, innerHeight - h - 4)) + "px";
      $("ev-add-name").focus();
    },
    close() { $("ev-add").hidden = true; this.ev = null; markRange(null); },
    /** id = "ev-add-tags" | "ev-add-members" (쉼표 구분 입력칸) */
    inputTags(id = "ev-add-tags") { return $(id).value.split(/[,，]/).map(cleanName).filter(Boolean); },
    toggleTag(t, id = "ev-add-tags") {
      const tags = this.inputTags(id), k = tags.indexOf(t);
      if (k < 0) tags.push(t); else tags.splice(k, 1);
      $(id).value = tags.join(", ");
      this.markTags();
    },
    markTags() {
      for (const [id, pick] of [["ev-add-tags", "ev-add-tagpick"], ["ev-add-members", "ev-add-mempick"]]) {
        const tags = this.inputTags(id);
        for (const b of $(pick).children) b.classList.toggle("on", tags.includes(b.dataset.tag));
      }
    },
    setKind(kind) {
      this.kind = kind;
      $("ev-add").dataset.kind = kind;
      for (const b of $("ev-add-seg").querySelectorAll("button")) b.classList.toggle("on", b.dataset.kind === kind);
      $("ev-add-start").title = kind === "span" ? "비우면 시작 미정 (마감만 있을 때)" : "시작 날짜";
      $("ev-add-end").title = kind === "span" ? "비우면 끝 미정" : "비우면 하루";
      this.update();
    },
    /** [시작, 끝] — 일정은 끝을 비우면 하루, 기간은 비운 쪽이 미정("") */
    range() {
      let s = $("ev-add-start").value, e = $("ev-add-end").value;
      if (s && e && e < s) [s, e] = [e, s];
      if (this.kind !== "span") return s ? [s, e || s] : ["", ""];
      return [s, e];
    },
    /** 아래 미리 보기 문구 + 달력 선택 표시를 입력에 맞춰 갱신 */
    update() {
      const [s, e] = this.range();
      const md = (v) => { const d = parseISO(v); return `${d.getMonth() + 1}/${d.getDate()}(${WD[d.getDay()]})`; };
      const t1 = $("ev-add-t1").value, t2 = $("ev-add-t2").value, about = $("ev-add-about").checked;
      $("ev-add-t2").disabled = !t1;
      let date = "";
      if (s && e && s !== e) date = `${md(s)} - ${md(e)}`;
      else if (s && e) date = md(s);
      else if (s) date = `${md(s)}부터 (끝 미정)`;
      else if (e) date = `${md(e)}까지 (마감)`;
      const time = !t1 ? "" : t2 ? ` ${t1}-${t2}` : ` ${t1}`;
      if (this.kind === "off") {
        const who = this.inputTags("ev-add-members");
        const what = who.length ? `근태 ${who.map((m) => "@" + m).join(" ")}` : "근태 (사람을 비우면 모두 쉬는 날)";
        $("ev-add-hint").textContent = date ? `${date} ${what}` : "";
        markRange(s || e ? [s || e, e || s] : null);
        return;
      }
      const kind = this.kind === "span" ? "기간 →" : s && e && s !== e ? "일정 ▬" : "일정";
      $("ev-add-hint").textContent = date ? `${kind} ${date}${time}${t1 && about ? " (애매함)" : ""}` : "";
      markRange(s || e ? [s || e, e || s] : null);
    },
    values() {
      const [s, e] = this.range();
      return {
        kind: this.kind, s, e,
        t1: $("ev-add-t1").value, t2: $("ev-add-t2").value, about: $("ev-add-about").checked,
        bold: $("ev-add-bold").checked,
        title: $("ev-add-name").value, tags: $("ev-add-tags").value.split(/[,，]/),
        members: $("ev-add-members").value.split(/[,，]/),
        place: $("ev-add-place").value, memo: $("ev-add-memo").value,
      };
    },
    spaceKeys() { return [...$("ev-add-spaces").querySelectorAll("input:checked:not(:disabled)")].map((i) => i.dataset.key); },
    async submit() {
      const [st, en] = this.range();
      const keys = this.spaceKeys();
      if (!keys.length) { $("ev-add-err").textContent = "연결된 스페이스를 하나 이상 고르세요"; return; }
      if (!st && !en) { $("ev-add-err").textContent = this.kind !== "span" ? "시작 날짜를 고르세요" : "날짜를 하나 이상 고르세요"; return; }
      const t1 = $("ev-add-t1").value, t2 = $("ev-add-t2").value;
      if (t1 && t2 && timeMin(t2) <= timeMin(t1)) {
        $("ev-add-err").textContent = "끝 시각이 시작보다 늦어야 합니다"; return;
      }
      if (!$("ev-add-name").value.trim()) return;
      const line = schedLine(this.values());
      try {
        await writeEv(this.ev, line, keys);
        this.close();
      } catch (e) {
        const msg = (this.ev ? "저장 실패: " : "추가 실패: ") + (e.message || e);
        // 일부 스페이스만 저장됨 → 팝업 내용(사본 위치)이 낡아 다시 누르면 꼬임. 닫고 다시 열게 함
        if (e.partial) { this.close(); alert(msg + "\n\n다른 스페이스에는 저장되었습니다. 일정을 다시 열어 확인하세요."); }
        else $("ev-add-err").textContent = msg;
      }
    },
    async remove() {
      if (!this.ev) return;
      const where = (this.ev.spaces || []).length > 1
        ? `\n(${this.ev.spaces.map((k) => (spaceByKey(k) || { name: k }).name).join(", ")} 모두에서 삭제됩니다. 한 곳에서만 지우려면 해당 스페이스 선택을 해제하고 저장하세요.)` : "";
      if (!confirm(`'${this.ev.title}' 일정을 지울까요?${where}`)) return;
      try {
        await writeEv(this.ev, null, []);
        this.close();
      } catch (e) {
        if (e.partial) { this.close(); alert("삭제 실패: " + (e.message || e) + "\n\n다른 스페이스에서는 삭제되었습니다."); }
        else $("ev-add-err").textContent = "삭제 실패: " + (e.message || e);
      }
    },
  };
  const timeMin = (t) => { const [h, m] = t.split(":").map(Number); return h * 60 + m; };
  const evOf = (el) => {
    const k = el && el.dataset.k;
    return k && state.sched && state.sched.list ? state.sched.list[Number(k)] || null : null;
  };
  // 날짜 고르기: 칸을 드래그(왼쪽·오른쪽 버튼)해서 기간 선택, 우클릭만 하면 그 하루
  // 일정 위에서 왼쪽 드래그 = 날짜 옮기기 (띠는 여러 칸에 걸쳐 있어서 칸은 마우스 위치로 찾음)
  let drag = null, dragged = false, move = null;
  function markRange(r) {
    for (const c of $("cal-grid").querySelectorAll(".cal-day")) {
      const d = c.dataset.date;
      c.classList.toggle("range-sel", !!r && d >= r[0] && d <= r[1]);
    }
  }
  // 칸 위치는 누를 때 한 번만 잼 (끄는 동안 mousemove 마다 42칸 레이아웃을 읽지 않게)
  let cellRects = null;
  const measureCells = () => {
    cellRects = [...$("cal-grid").querySelectorAll(".cal-day")].map((c) => [c, c.getBoundingClientRect()]);
  };
  function cellAt(x, y) {
    if (!cellRects) measureCells();
    for (const [c, r] of cellRects) if (x >= r.left && x < r.right && y >= r.top && y < r.bottom) return c;
    return null;
  }
  const dayDiff = (a, b) => Math.round((parseISO(b) - parseISO(a)) / 86400000);
  const sorted = (a, b) => (a <= b ? [a, b] : [b, a]);
  /** 장식 우클릭 메뉴: 삭제 하나뿐 (수정 없음) */
  function decoMenu(el, x, y) {
    let m = $("deco-menu");
    if (!m) {
      m = document.createElement("button");
      m.id = "deco-menu";
      m.textContent = "삭제";
      document.body.appendChild(m);
      document.addEventListener("mousedown", (e) => { if (e.target !== m) m.hidden = true; });
    }
    const sp = spaceByKey(el.dataset.sp), raw = el.dataset.raw;
    m.onclick = () => {
      m.hidden = true;
      if (!sp) return;
      editSchedDoc(sp, (text) => {
        const lines = text.split("\n"), i = lines.findIndex((l) => l.trim() === raw);
        if (i < 0) throw new Error("일정 파일에서 이 줄을 찾지 못했습니다.");
        lines.splice(i, 1);
        return lines.join("\n");
      }).catch((err) => alert("장식 삭제 실패: " + (err.message || err)));
    };
    m.hidden = false;
    m.style.left = Math.min(x, innerWidth - 60) + "px";
    m.style.top = Math.min(y, innerHeight - 30) + "px";
  }
  /** 일정 위 마우스 x 가 가장자리(6px)면 start|end, 아니면 move. 할일·미정 끝(open)은 늘이기 없음 */
  function edgeMode(el, ev, x) {
    if (!ev || ev.task || !canEdit(ev)) return "move";
    const r = el.getBoundingClientRect(), bar = el.classList.contains("bar");
    if (x - r.left < 6 && ev.open !== "start" && (!bar || el.classList.contains("head"))) return "start";
    if (r.right - x < 6 && ev.open !== "end" && (!bar || el.classList.contains("tail"))) return "end";
    return "move";
  }
  // 마우스 아래 일정·칸 (Ctrl+C / Ctrl+V 대상), 가장자리면 크기 조절 커서
  let hoverEv = null, hoverDate = null, edgeEl = null, clip = null;
  // 칸 강조(.hov)도 여기서: 바뀔 때만 클래스 1개 토글 (CSS :has(:hover) 는 이동마다 격자 전체 재계산이라 느림)
  let hovCell = null;
  const setHov = (c) => { if (c !== hovCell) { if (hovCell) hovCell.classList.remove("hov"); if (c) c.classList.add("hov"); hovCell = c; } };
  $("cal-grid").addEventListener("mousemove", (e) => {
    const el = e.target.closest(".ev[data-k]"), cell = e.target.closest(".cal-day");
    setHov(el ? null : cell);
    hoverEv = evOf(el);
    hoverDate = cell ? cell.dataset.date : null;
    // 가장자리 판정(레이아웃 읽기)은 쓰기 가능일 때만
    const edge = el && state.writable && !move && edgeMode(el, hoverEv, e.clientX) !== "move" ? el : null;
    if (edge !== edgeEl) { if (edgeEl) edgeEl.classList.remove("edge"); if (edge) edge.classList.add("edge"); edgeEl = edge; }
  });
  $("cal-grid").addEventListener("mouseleave", () => { hoverEv = hoverDate = null; setHov(null); });
  /** 일정 ev 를 date 날짜로 시작하게 복사 (길이·시각·태그 그대로, 같은 스페이스들에) */
  function copyEvTo(ev, date) {
    const o = evToObj(ev), n = dayDiff(ev.start, date);
    if (o.s) o.s = CAL.addDays(o.s, n);
    if (o.e) o.e = CAL.addDays(o.e, n);
    return writeEv(null, schedLine(o), (ev.spaces || []).filter((k) => canEdit({ spaces: [k] })));
  }
  // 달력 창 단축키: Ctrl+C = 마우스 아래 일정 복사, Ctrl+V = 마우스 아래 칸에 붙여넣기, Ctrl+Z = 달력에서 한 마지막 변경 되돌리기
  document.addEventListener("keydown", (e) => {
    if (state.mode !== "cal" || !e.ctrlKey || e.shiftKey || e.altKey) return;
    if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable], #ev-add")) return;
    const k = e.key.toLowerCase();
    if (k === "c" && hoverEv && !hoverEv.task) { clip = hoverEv; e.preventDefault(); }
    else if (k === "v" && clip && hoverDate && state.writable) {
      e.preventDefault();
      copyEvTo(clip, hoverDate).catch((err) => alert("붙여넣기 실패: " + (err.message || err)));
    } else if (k === "z" && state.writable) { e.preventDefault(); undo.pop(); }
  });
  $("cal-grid").addEventListener("mousedown", (e) => {
    dragged = false;
    measureCells();
    const evEl = e.target.closest(".ev[data-k]");
    const ev = evOf(evEl);
    if (ev && e.button === 0) {
      // 일정 위 왼쪽 버튼: 옮기기 준비 (그냥 떼면 click → 수정 팝업). 읽기 전용은 안 됨
      if (e.target.closest(".ev-check")) return;   // 할일 체크 네모 = click 에서 체크
      const c = canEdit(ev) && cellAt(e.clientX, e.clientY);
      const mode = c ? edgeMode(evEl, ev, e.clientX) : "move";   // 일정·기간 가장자리 = 날짜 늘이고 줄이기
      if (mode !== "move") document.body.classList.add("cal-resizing");
      // 완료한 할일(✅ 날)은 완료한 날에 고정: 끌어서 옮기지 않음 (클릭·체크 해제는 됨)
      if (c && !(ev.task && ev.task.plan)) move = { ev, el: evEl, mode, from: c.dataset.date, to: c.dataset.date };
      if (move && mode === "move") document.body.classList.add("cal-grabbing");   // 끄는 동안 쥔 손 커서
      return;
    }
    const deco = e.target.closest(".deco");
    if (deco && e.button === 2) {   // 장식 우클릭 = 삭제 버튼만 (쓸 수 있는 스페이스일 때)
      e.stopPropagation();
      if (!canEdit({ spaces: [deco.dataset.sp] })) return;
      decoMenu(deco, e.clientX, e.clientY);
      return;
    }
    if (deco && e.button === 0) {   // 장식 왼쪽 끌기 = 다른 날짜로 옮기기 (일정 옮기기와 같은 move 흐름)
      const c = canEdit({ spaces: [deco.dataset.sp] }) && cellAt(e.clientX, e.clientY);
      if (c) {
        move = { deco, el: deco, mode: "move", from: c.dataset.date, to: c.dataset.date };
        document.body.classList.add("cal-grabbing");
      }
      return;
    }
    if (ev && e.button === 2) {
      // 일정·할일 위 오른쪽 버튼 = 새 일정 말고 바로 그 항목 편집 (할일 = 노트 열기)
      if (ev.task) {   // 할일 우클릭 = 달력에서 빼기 (노트 줄은 그대로, 날짜·✅ 만 지움). 노트 열기는 왼쪽 클릭
        if (canEdit(ev) && confirm(`'${ev.title}' 할일 보기를 지울까요?
노트에는 그대로 남습니다.`))
          editTask(ev.task, (l) => l.replace(TASK_DATE_RE, "").replace(TASK_DONE_RE, "")).catch((err) => alert("할일 보기를 지우지 못했습니다.\n" + (err.message || err)));
      }
      else if (canEdit(ev)) {
        evAdd.close();   // 열려 있던 팝업은 닫고
        evAdd.open("", "", e.clientX, e.clientY, ev);
        e.stopPropagation();   // 아래 document "팝업 밖 누르면 닫기" 가 방금 연 팝업을 바로 닫지 않게
      }
      return;
    }
    const cell = e.target.closest(".cal-day");
    if (!cell || (e.button !== 0 && e.button !== 2) || !state.writable) return;
    drag = { from: cell.dataset.date, to: cell.dataset.date, button: e.button };
  });
  document.addEventListener("mousemove", (e) => {
    if (!move) return;
    const c = cellAt(e.clientX, e.clientY);
    if (!c || c.dataset.date === move.to) return;
    move.to = c.dataset.date;
    const n = dayDiff(move.from, move.to);
    move.el.classList.toggle("moving", n !== 0);
    if (move.deco) return markRange(n ? [move.to, move.to] : null);
    document.body.classList.toggle("cal-copying", n !== 0 && move.mode === "move" && e.ctrlKey && !move.ev.task);
    markRange(n ? dragRange(move.ev, move.mode, n) : null);
  });
  $("cal-grid").addEventListener("mouseover", (e) => {
    const cell = drag && e.target.closest(".cal-day");
    if (!cell || cell.dataset.date === drag.to) return;
    drag.to = cell.dataset.date;
    markRange(sorted(drag.from, drag.to));
  });
  document.addEventListener("mouseup", async (e) => {
    if (move) {
      const m = move, n = dayDiff(m.from, m.to);
      move = null;
      cellRects = null;
      m.el.classList.remove("moving");
      document.body.classList.remove("cal-resizing", "cal-copying", "cal-grabbing");
      markRange(null);
      if (!n) return;  // 제자리 = 클릭 (수정 팝업은 click 에서)
      dragged = true;
      if (m.deco) {
        const sp = spaceByKey(m.deco.dataset.sp), raw = m.deco.dataset.raw;
        if (!sp) return;
        // 일정 파일 줄의 날짜만 바꿈 (형식은 schedule.py _DATE: 261119 또는 2026-11-19)
        editSchedDoc(sp, (text) => {
          const lines = text.split("\n"), i = lines.findIndex((l) => l.trim() === raw);
          if (i < 0) throw new Error("일정 파일에서 이 줄을 찾지 못했습니다.");
          lines[i] = lines[i].replace(/\d{4}-\d{2}-\d{2}|\d{6}/, yymmdd(m.to));
          return lines.join("\n");
        }).catch((err) => alert("장식을 옮기지 못했습니다.\n" + (err.message || err)));
        return;
      }
      if (m.ev.task) {
        const [s, en] = dragRange(m.ev, m.mode, n);
        editTask(m.ev.task, (l) => setTaskDates(l, s, en)).catch((err) => alert("옮기기 실패: " + (err.message || err)));
        return;
      }
      const o = evToObj(m.ev);
      if (m.mode === "move") {
        if (o.s) o.s = CAL.addDays(o.s, n);
        if (o.e) o.e = CAL.addDays(o.e, n);
      } else {   // 가장자리 끌기 = 날짜만 (시각은 그대로)
        [o.s, o.e] = dragRange(m.ev, m.mode, n);
      }
      try {
        if (m.mode === "move" && e.ctrlKey) { await copyEvTo(m.ev, CAL.addDays(m.ev.start, n)); return; }   // Ctrl+끌기 = 복사
        await writeEv(m.ev, schedLine(o), m.ev.spaces || []);
      } catch (err) {
        alert("옮기기 실패: " + (err.message || err));
      }
      return;
    }
    if (!drag) return;
    const r = sorted(drag.from, drag.to), btn = drag.button;
    drag = null;
    if (r[0] === r[1] && btn === 0) return;  // 그냥 클릭 = 노트 열기
    dragged = btn === 0;
    markRange(r);
    evAdd.open(r[0], r[1], e.clientX, e.clientY);
  });
  // 바탕화면 아이콘 등 바깥 파일을 창 위로 끌면 "+복사" 표시가 떠서 놓을 수 있는 것처럼 보임 → 받지 않는다고 알림
  // (창 안 끌기는 types 에 Files 가 없어 영향 없음. 투명 창 아래 아이콘을 옮기다 걸리는 경우 대비)
  const noFileDrop = (e) => {
    if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
    e.preventDefault();
    if (e.type === "dragover") e.dataTransfer.dropEffect = "none";
  };
  window.addEventListener("dragover", noFileDrop, true);
  window.addEventListener("drop", noFileDrop, true);
  // 할일 창 손잡이(⋮⋮)를 달력 칸에 끌어 놓기 = 그 할일 줄에 날짜를 붙임 (이미 기간이면 길이 유지)
  const grid = $("cal-grid");
  grid.addEventListener("dragover", (e) => {
    const c = e.target.closest(".cal-day");
    // 할일·장식 줄만 받음 (가로선·빈 줄·바깥 글자는 칸 강조도 안 함 = 놓을 수 없다는 표시)
    if (!c || !(calDrag || e.dataTransfer.types.includes("application/x-dogani-cal"))) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    markRange([c.dataset.date, c.dataset.date]);
  });
  grid.addEventListener("dragleave", (e) => { if (!grid.contains(e.relatedTarget)) markRange(null); });
  grid.addEventListener("drop", (e) => {
    const c = e.target.closest(".cal-day"), pending = calDrag;
    let txt = e.dataTransfer.getData("text/plain") || "";
    calDrag = "";   // 끌기 끝 알림이 안 와도(창 닫힘 등) 다음 바깥 끌기를 받지 않게
    markRange(null);
    if (!txt.startsWith(TASK_DRAG) && pending) txt = pending;   // 장식은 브리지로 받은 데이터
    if (c && txt.startsWith(DECO_DRAG)) {
      e.preventDefault();
      let t;
      try { t = JSON.parse(txt.slice(DECO_DRAG.length)); } catch (err) { return; }
      const sp = spaceByKey(t.sp), line = `- ${yymmdd(c.dataset.date)} ✦ ${String(t.text).replace(/[\r\n]/g, " ")}`;
      if (!sp || !canEdit({ spaces: [sp.key] })) return;
      if (state.calHidden.has(sp.key)) return alert(`'${sp.name}' 스페이스가 꺼져 있어 장식이 표시되지 않습니다.
스페이스를 켠 뒤 다시 놓아 주세요.`);
      editSchedDoc(sp, (text) => (text && !text.endsWith("\n") ? text + "\n" : text) + line + "\n")
        .catch((err) => alert("장식 붙이기 실패: " + (err.message || err)));
      return;
    }
    if (!c || !txt.startsWith(TASK_DRAG)) return;
    e.preventDefault();
    let t;
    try { t = JSON.parse(txt.slice(TASK_DRAG.length)); } catch (err) { return; }
    const d = c.dataset.date, old = taskDatesOf(t.raw || ""), len = old ? dayDiff(old.s, old.e) : 0;
    editTask(t, (l) => setTaskDates(l, d, CAL.addDays(d, len))).catch((err) => alert("달력에 올리기 실패: " + (err.message || err)));
  });

  // 스페이스 칩에 마우스 올림 = 그 스페이스 일정 표시 (평소엔 색을 안 그림)
  function spaceHover(key) {
    const grid = $("cal-grid"), sp = key && spaceByKey(key);
    grid.classList.toggle("sp-hover", !!sp);
    if (sp) grid.style.setProperty("--spc", "var(--accent)");
    for (const el of grid.querySelectorAll(".ev[data-k]")) {
      const ev = evOf(el);
      el.classList.toggle("sp-on", !!(sp && ev && (ev.spaces || []).includes(key)));
    }
  }
  $("cal-spaces").addEventListener("mouseover", (e) => {
    const inp = e.target.closest("label") && e.target.closest("label").querySelector("input");
    if (!inp) return;   // 칩 사이 틈 = 하던 강조 그대로 (줄 밖으로 나가야 풀림)
    spaceHover(inp && inp.checked ? inp.dataset.key : null);
  });
  $("cal-spaces").addEventListener("mouseleave", () => spaceHover(null));
  $("cal-spaces").addEventListener("change", (e) => {
    const key = e.target.dataset.key;
    if (!key) return;
    if (e.target.checked) state.calHidden.delete(key); else state.calHidden.add(key);
    if (bridge) bridge.setCalHidden(JSON.stringify([...state.calHidden]));  // 설정 저장 + 할일 창도 따라감
    rebuildSched();
    if (e.target.checked) loadSchedule(spaceByKey(key));
  });
  // 태그 칩: 끄면 그 태그 일정 숨김 (스페이스 칩과 같이 calHidden 에 저장), 마우스 올리면 그 태그 일정 표시
  $("cal-tags").addEventListener("change", (e) => {
    const t = e.target.dataset.tag;
    if (t === undefined) return;
    const key = "#" + t;
    if (e.target.checked) state.calHidden.delete(key); else state.calHidden.add(key);
    if (bridge) bridge.setCalHidden(JSON.stringify([...state.calHidden]));
    rebuildSched();
  });
  // 멤버 칩: 태그 칩과 같음 (calHidden 에 "@이름")
  for (const box of [$("cal-members"), $("cal-groups")]) box.addEventListener("change", (e) => {
    const m = e.target.dataset.mem;
    if (m === undefined) return;
    const key = "@" + m;
    if (e.target.checked) state.calHidden.delete(key); else state.calHidden.add(key);
    if (bridge) bridge.setCalHidden(JSON.stringify([...state.calHidden]));
    rebuildSched();
  });
  // 칩 강조: 칩 사이 틈·꺼진 칩·태그 없는 칩 = 하던 강조 그대로 (줄 밖으로 나가야 풀림). 같은 칩 안 이동은 무시
  let hoverKey = null;
  const CHIP_BOXES = ["cal-groups", "cal-members", "cal-tags"];
  /** 그룹 g 에 멤버 m 이 드는가 (이름은 cleanName 으로 맞춤) */
  const inGroup = (g, m) => (allGroups()[g] || []).some((x) => cleanName(x.name) === m);
  const memOf = (l) => { const i = l.querySelector("input[data-mem]"); return i ? i.dataset.mem : null; };
  /** 칩 줄 강조: pred 에 맞는 칩만 그대로, 나머지는 옅게 (스타일은 .hl·.hl-cur) */
  function markChips(box, pred) {
    for (const l of box.querySelectorAll("label")) l.classList.toggle("hl-cur", !!pred(l));
    box.classList.add("hl");
  }
  function clearChipMarks() {
    for (const id of CHIP_BOXES) {
      $(id).classList.remove("hl");
      for (const l of $(id).querySelectorAll("label.hl-cur")) l.classList.remove("hl-cur");
    }
  }
  function chipHover(box, attr, field) {
    box.addEventListener("mouseover", (e) => {
      if (chipDrag && chipDrag.on) return;   // 끄는 중엔 강조를 바꾸지 않음
      const lab = e.target.closest("label"), inp = lab && lab.querySelector("input");
      if (inp && inp.checked && inp.dataset[attr] !== undefined) {
        const name = inp.dataset[attr];
        tagHover(name, field);
        markChips(box, (l) => l === lab);
        // 그룹 ↔ 멤버는 서로 비춤: 그룹 칩 = 그 구성원 칩, 멤버 칩 = 그 사람이 든 그룹 칩 (나머지는 옅게)
        if (box.id === "cal-groups") markChips($("cal-members"), (l) => inGroup(name, memOf(l)));
        else if (box.id === "cal-members") markChips($("cal-groups"), (l) => inGroup(memOf(l), name));
      }
    });
    box.addEventListener("mouseleave", () => { tagHover(null); clearChipMarks(); });
  }
  chipHover($("cal-members"), "mem", "members");
  chipHover($("cal-groups"), "mem", "members");   // 그룹 칩 = @그룹 을 적은 일정 강조
  // 멤버 칩 우클릭: 스페이스에 아직 없는 이름(일정에만 적힘) → 내 PC 스페이스 초대장 만들기 (트레이 프로그램이 스페이스를 묻고 복사)
  $("cal-members").addEventListener("contextmenu", (e) => {
    const lab = e.target.closest("label"), inp = lab && lab.querySelector("input[data-mem]");
    const m = inp && inp.dataset.mem;
    // 실제 멤버 = 넣을 멤버 그룹 고르기 (트레이 프로그램이 메뉴를 띄움. 내 PC 스페이스만)
    if (m && lab.classList.contains("mem-real") && bridge && bridge.groupMenu) return bridge.groupMenu("member", m);
    if (!m || lab.classList.contains("mem-real") || !bridge || !bridge.inviteFor) return;
    const keys = schedSpaces().filter((sp) => (state.evBySpace[sp.key] || []).some((ev) => (ev.members || []).includes(m))).map((sp) => sp.key);
    bridge.inviteFor(m, JSON.stringify(keys));
  });
  // 그룹 칩 우클릭 = 그 그룹에 넣을 멤버 고르기
  $("cal-groups").addEventListener("contextmenu", (e) => {
    const inp = e.target.closest("label") && e.target.closest("label").querySelector("input[data-mem]");
    if (inp && bridge && bridge.groupMenu) bridge.groupMenu("group", inp.dataset.mem);
  });
  function tagHover(tag, field = "tags") {
    const key = tag != null ? field + ":" + tag : null;
    if (key === hoverKey) return;
    hoverKey = key;
    const grid = $("cal-grid");
    const on = tag != null;   // "" = 태그(멤버) 없는 일정
    // 그룹 = @그룹 을 적은 일정 + 그 구성원 일정
    const grp = on && field === "members" && !!tag && tag in allGroups();
    const hit = (v) => v === tag || (grp && inGroup(tag, v));
    grid.classList.toggle("sp-hover", on);
    if (on) grid.style.setProperty("--spc", field === "tags" && tag ? tagColor(tag) : "var(--accent)");
    for (const el of grid.querySelectorAll(".ev[data-k]")) {
      const ev = evOf(el);
      const vs = (ev && ev[field]) || [];
      el.classList.toggle("sp-on", !!(on && ev && (tag ? vs.some(hit) : !vs.length)));
    }
  }
  chipHover($("cal-tags"), "tag", "tags");
  // 칩 끌어 놓기 (HTML5 drag 말고 마우스 이벤트 = 커서는 윈도우 기본 그대로)
  //  멤버·그룹·태그 칩 → 일정 = 그 일정에 @이름·#태그 더하기, 멤버 칩 → 그룹 칩 = 그 그룹에 넣기
  //  Ctrl+클릭 = 칩 여러 개 고르기 (고른 칩 하나를 끌면 같이 끌림, Esc·그냥 클릭 = 풀기)
  let chipDrag = null, chipDragged = false;
  function chipOf(lab) {
    const i = lab && lab.querySelector("input");
    if (!i) return null;
    if (i.dataset.tag) return { kind: "tag", name: i.dataset.tag, lab };
    if (i.dataset.mem) return { kind: lab.classList.contains("mem-group") ? "group" : "member", name: i.dataset.mem, real: lab.classList.contains("mem-real"), lab };
    return null;
  }
  const chipSel = () => CHIP_BOXES.flatMap((id) => Array.from($(id).querySelectorAll("label.chip-sel")));
  const clearChipSel = () => { for (const l of chipSel()) l.classList.remove("chip-sel"); };
  const markDrop = (el) => { for (const x of document.querySelectorAll(".drop-on")) if (x !== el) x.classList.remove("drop-on"); if (el) el.classList.add("drop-on"); };
  /** 마우스 아래 놓을 곳: {ev, el} = 고칠 수 있는 일정, {group, el} = 실제 멤버만 끌 때의 그룹 칩 */
  function chipTarget(x, y, items) {
    const el = document.elementFromPoint(x, y);
    if (!el) return null;
    const gl = el.closest("#cal-groups label.mem-group");
    if (gl && items.every((c) => c.kind === "member" && c.real)) return { group: memOf(gl), el: gl };
    const evEl = el.closest("#cal-grid .ev[data-k]"), ev = evOf(evEl);
    if (ev && !ev.task && state.writable && canEdit(ev)) return { ev, el: evEl };
    return null;
  }
  for (const id of CHIP_BOXES) {
    const box = $(id);
    box.addEventListener("mousedown", (e) => {
      const c = e.button === 0 && chipOf(e.target.closest("label"));
      if (!c) return;
      e.preventDefault();   // 글자 선택 막기 (클릭은 그대로 옴)
      chipDrag = { x: e.clientX, y: e.clientY, c, on: false };
    });
    // 끌고 난 클릭·Ctrl+클릭은 체크를 안 바꿈
    box.addEventListener("click", (e) => {
      const lab = e.target.closest("label");
      if (!lab || e.target.tagName === "INPUT") return;   // label 이 input 에 한 번 더 보내는 클릭
      if (chipDragged) { e.preventDefault(); return; }
      if (e.ctrlKey && chipOf(lab)) { e.preventDefault(); lab.classList.toggle("chip-sel"); return; }
      // 일정을 골라 둔 상태에서 칩 클릭 = 보기 끄기 대신 고른 일정 모두에 더하기 (고른 칩이면 고른 칩 모두)
      if (evSel.size && chipOf(lab)) {
        e.preventDefault();
        const items = lab.classList.contains("chip-sel") ? chipSel().map(chipOf).filter(Boolean) : [chipOf(lab)];
        const evs = selEvs();
        clearEvSel(); clearChipSel();
        addChipsToEvs(evs, items);
        return;
      }
      clearChipSel();
    });
  }
  document.addEventListener("mousemove", (e) => {
    const d = chipDrag;
    if (!d) return;
    if (!d.on) {
      if (Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) < 5) return;
      d.items = d.c.lab.classList.contains("chip-sel") ? chipSel().map(chipOf).filter(Boolean) : [d.c];
      d.on = true;
      tipClear(); tagHover(null); clearChipMarks();
      d.ghost = document.createElement("div");
      d.ghost.className = "cal-spaces cal-tags chip-ghost";
      for (const c of d.items) { const g = c.lab.cloneNode(true); g.classList.remove("chip-sel", "hl-cur", "drop-on"); d.ghost.appendChild(g); }
      document.body.appendChild(d.ghost);
    }
    const gw = d.ghost.offsetWidth;   // 오른쪽 끝이면 마우스 왼쪽에
    d.ghost.style.left = (e.clientX + 12 + gw <= innerWidth ? e.clientX + 12 : Math.max(0, e.clientX - 12 - gw)) + "px";
    d.ghost.style.top = e.clientY + 10 + "px";
    d.target = chipTarget(e.clientX, e.clientY, d.items);
    markDrop(d.target && d.target.el);
  });
  document.addEventListener("mouseup", () => {
    const d = chipDrag;
    chipDrag = null;
    if (!d || !d.on) return;
    d.ghost.remove();
    markDrop(null);
    chipDragged = true;
    setTimeout(() => { chipDragged = false; }, 0);
    const t = d.target;
    if (!t) return;
    clearChipSel();
    if (t.group) {
      if (bridge && bridge.addToGroup) for (const c of d.items) bridge.addToGroup(c.name, t.group);
      return;
    }
    const k = t.el.dataset.k, evs = evSel.has(k) ? selEvs() : [t.ev];   // 고른 일정에 놓으면 고른 것 모두에
    if (evSel.has(k)) clearEvSel();
    addChipsToEvs(evs, d.items);
  });
  // Ctrl+클릭 일정 = 고르기/풀기 (캡처 단계라 옮기기·수정 팝업보다 먼저 받고 막음)
  let evSelClick = false;
  $("cal-grid").addEventListener("mousedown", (e) => {
    if (!e.ctrlKey || e.button !== 0) return;
    const evEl = e.target.closest(".ev[data-k]"), ev = evOf(evEl);
    if (!ev || ev.task || !state.writable || !canEdit(ev)) return;
    e.preventDefault(); e.stopImmediatePropagation();
    evSelClick = true;
    const k = evEl.dataset.k;
    if (evSel.has(k)) evSel.delete(k); else evSel.add(k);
    markEvSel();
  }, true);
  $("cal-grid").addEventListener("click", (e) => {
    if (evSelClick) { evSelClick = false; e.stopImmediatePropagation(); e.preventDefault(); }
  }, true);
  function markEvSel() {
    for (const el of $("cal-grid").querySelectorAll(".ev[data-k]")) el.classList.toggle("ev-sel", evSel.has(el.dataset.k));
  }
  const clearEvSel = () => { evSel.clear(); markEvSel(); };
  const selEvs = () => [...evSel].map((k) => evOf({ dataset: { k } })).filter(Boolean);
  /** 칩들을 일정 여럿에 차례로 더함. 모두 이미 붙어 있으면 = 한 번 더 적용 = 모두에서 뺌. 실패는 모아서 한 번에 알림 */
  async function addChipsToEvs(evs, items) {
    const errs = [], remove = evs.every((ev) => evHasChips(ev, items));
    for (const ev of evs) {
      try { await addChipsToEv(ev, items, remove); } catch (err) { errs.push(`${ev.title}: ${err.message || err}`); }
    }
    if (errs.length) alert("일정에 더하지 못했습니다.\n" + errs.join("\n"));
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { clearChipSel(); clearEvSel(); } });
  /** 일정 ev 에 칩이 이미 다 붙었나 (근태의 태그는 셈에서 뺌) */
  const evHasChips = (ev, items) => items.every((c) => c.kind === "tag"
    ? ev.off || (ev.tags || []).includes(c.name)
    : (ev.members || []).map(cleanName).includes(cleanName(c.name)));
  /**
   * 일정 ev 에 칩들 더하기(remove = 빼기): 멤버·그룹 = @이름, 태그 = #태그 (근태 줄엔 태그 칸이 없어 태그는 무시).
   * 바뀐 게 없으면 저장 안 함
   */
  async function addChipsToEv(ev, items, remove = false) {
    const o = evToObj(ev), ms = new Set(o.members.map(cleanName)), ts = new Set(o.tags);
    const n0 = ms.size + ts.size;
    for (const c of items) {
      if (c.kind === "tag") { if (!ev.off) ts[remove ? "delete" : "add"](c.name); }
      else ms[remove ? "delete" : "add"](cleanName(c.name));
    }
    if (ms.size + ts.size === n0) return;
    o.members = [...ms]; o.tags = [...ts];
    await writeEv(ev, schedLine(o), (ev.spaces || []).filter((k) => canEdit({ spaces: [k] })));
  }
  // 태그 칩 우클릭 = 이름 일괄 변경: 쓸 수 있는 모든 스페이스 일정 파일의 "#옛이름" 을 "#새이름" 으로 (이미 있는 태그면 합쳐짐)
  const reEsc = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  $("cal-tags").addEventListener("contextmenu", async (e) => {
    const inp = e.target.closest("label") && e.target.closest("label").querySelector("input[data-tag]");
    const old = inp && inp.dataset.tag;
    if (!old) return;
    const raw = prompt(`'#${old}' 태그의 새 이름을 입력해 주세요.\n모든 스페이스 일정에서 함께 바뀝니다.`, old);
    const name = raw == null ? "" : raw.replace(/[#,，\s]/g, "");
    if (!name || name === old) return;
    // 그 태그 일정이 있는 스페이스만 (안 바뀌는 파일은 저장하지 않음)
    const has = (sp) => (state.evBySpace[sp.key] || []).some((ev) => (ev.tags || []).includes(old));
    const all = schedSpaces().filter(has), sps = all.filter((sp) => !state.roSpaces.has(sp.key) && spOnline(sp));
    const re = new RegExp(`(^|[\\s,，])#${reEsc(old)}(?=[\\s,，]|$)`, "gm");
    const errs = [];
    await undo.group(async () => {
      for (const sp of sps) {
        try { await editSchedDoc(sp, (t) => t.replace(re, `$1#${name}`)); }
        catch (err) { errs.push(`${sp.name}: ${err.message || err}`); }
      }
    });
    if (state.calHidden.delete("#" + old)) {
      state.calHidden.add("#" + name);
      if (bridge) bridge.setCalHidden(JSON.stringify([...state.calHidden]));
    }
    rebuildSched();
    for (const sp of all) if (!sps.includes(sp)) errs.push(`${sp.name}: 연결되지 않았거나 읽기 전용입니다`);
    if (errs.length) alert("태그 이름을 바꾸지 못한 스페이스가 있습니다.\n" + errs.join("\n"));
  });
  // 달력 일정에 마우스 올림 = 정보 카드 (기본 title 툴팁은 겹치니 뗌)
  const tagTip = $("ev-tagtip");
  // 일정에 마우스 = 마우스가 있는 날짜에 걸친 일정 카드를 시간 순으로 모두. 일정 밖 0.15초면 닫힘
  // 마우스를 잠깐(0.1초) 멈춰야 뜨고 날짜를 바꿈 (막 움직일 땐 안 뜸). 일정 밖 0.15초면 닫힘
  let tipEl = null, tipHide = 0, tipShow = 0;
  const tipClear = () => { clearTimeout(tipHide); clearTimeout(tipShow); tagTip.hidden = true; tipEl = null; };
  function tipRender(x, y) {
    if (move || drag) return;
    const el = document.elementFromPoint(x, y);
    const moreEl = el && el.closest(".ev-more[data-ks]");
    if (moreEl) return tipMore(moreEl);
    const evEl = el && el.closest(".ev[data-k]"), ev = evOf(evEl);
    if (!ev) return;
    measureCells();
    const cell = cellAt(x, y), day = cell ? cell.dataset.date : ev.start;
    if (day === tipEl && !tagTip.hidden) return;   // 같은 날 다른 일정으로 옮겨도 그대로 (위치 고정)
    tipEl = day;
    evEl.removeAttribute("title");
    const all = (state.sched && state.sched.list || []).filter((v) => v.start <= day && (v.end || v.start) >= day);
    // 근태는 날짜 옆 글자로 (카드는 근태에 마우스를 올렸을 때 그것만)
    const cards = all.filter((v) => !v.off);
    const shown = ev.off || cards.includes(ev) ? (ev.off ? [ev, ...cards] : cards) : cards.concat(ev);   // list 는 이미 시간 순
    const dd = parseISO(day), wk = "일월화수목금토"[dd.getDay()];
    const offs = CAL.slotLabels(day, state.sched);
    tagTip.innerHTML = `<div class="tc-date">${day.replace(/-/g, ".")} (${wk})` +
      (offs.length ? ` <span class="tc-off">${offs.map(esc).join(" · ")}</span>` : "") + "</div>" +
      shown.map((v) => `<div class="tc-card">${CAL.eventCard(v, tagColor)}</div>`).join("");
    tagTip.hidden = false;
    // 위치 = 그 날짜 칸 오른쪽(칸 위쪽에 맞춤). 보이는 영역 = 창과 모니터 화면이 겹치는 부분
    // (창이 화면 오른쪽 밖으로 걸쳐 있으면 화면 끝까지만). 오른쪽에 안 들어가면 칸 왼쪽, 거기도 안 되면 끝에 붙임
    const r = (cell || evEl).getBoundingClientRect(), w = tagTip.offsetWidth, h = tagTip.offsetHeight;
    const sc = screen, sx = window.screenX, sy = window.screenY;
    const minX = Math.max(0, (sc.availLeft || 0) - sx) + 4, maxX = Math.min(innerWidth, (sc.availLeft || 0) + sc.availWidth - sx) - 4;
    const minY = Math.max(0, (sc.availTop || 0) - sy) + 4, maxY = Math.min(innerHeight, (sc.availTop || 0) + sc.availHeight - sy) - 4;
    const lx = r.right + 4 + w <= maxX ? r.right + 4 : r.left - 4 - w >= minX ? r.left - 4 - w : maxX - w;
    tagTip.style.left = Math.max(minX, lx) + "px";
    tagTip.style.top = Math.max(minY, Math.min(r.top, maxY - h)) + "px";
  }
  /** 숨은 일정 수 배지에 마우스 = 그 칸에서 숨은 일정만 카드로 (배지 아래쪽에) */
  function tipMore(moreEl) {
    const key = "more:" + moreEl.dataset.ks;
    if (key === tipEl && !tagTip.hidden) return;
    tipEl = key;
    const evs = moreEl.dataset.ks.split(",").map((k) => evOf({ dataset: { k } })).filter(Boolean);
    if (!evs.length) return;
    tagTip.innerHTML = `<div class="tc-date">숨은 일정 ${evs.length}개</div>` +
      evs.map((v) => `<div class="tc-card">${CAL.eventCard(v, tagColor)}</div>`).join("");
    tagTip.hidden = false;
    const r = moreEl.getBoundingClientRect(), w = tagTip.offsetWidth, h = tagTip.offsetHeight;
    tagTip.style.left = Math.max(4, Math.min(r.left, innerWidth - w - 4)) + "px";
    tagTip.style.top = Math.max(4, r.bottom + 4 + h <= innerHeight ? r.bottom + 4 : r.top - 4 - h) + "px";
  }
  // 멤버 그룹 칩에 마우스 = 일정 카드와 같은 카드로 구성원·권한 (윈도우 title 툴팁은 투명 웹뷰 창에서 안 뜸)
  $("cal-groups").addEventListener("mouseover", (e) => {
    const lab = e.target.closest("label.mem-group"), inp = lab && lab.querySelector("input[data-mem]");
    if (!inp) return;
    const name = inp.dataset.mem, ms = allGroups()[name] || [];
    clearTimeout(tipShow); clearTimeout(tipHide);
    tagTip.innerHTML = `<div class="tc-date">@${esc(name)} · ${ms.length}명</div>` + (ms.length
      ? ms.map((m) => `<div class="tc-card">${esc(m.name)} <span class="tc-off">${m.perm === "read" ? "보기만" : "편집"}</span></div>`).join("")
      : `<div class="tc-card">구성원이 없습니다.</div>`);
    tagTip.hidden = false;
    // 위치 = 칩 왼쪽(사이드바가 오른쪽이라), 안 들어가면 오른쪽
    const r = lab.getBoundingClientRect(), w = tagTip.offsetWidth, h = tagTip.offsetHeight;
    tagTip.style.left = (r.left - 6 - w >= 4 ? r.left - 6 - w : Math.min(r.right + 6, innerWidth - w - 4)) + "px";
    tagTip.style.top = Math.max(4, Math.min(r.top, innerHeight - h - 4)) + "px";
  });
  $("cal-groups").addEventListener("mouseout", (e) => {
    const to = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest("label.mem-group") : null;
    if (e.target.closest("label.mem-group") && !to) tipClear();
  });
  $("cal-grid").addEventListener("mousemove", (e) => {
    if (move || drag) return tipClear();
    const ev = evOf(e.target.closest(".ev[data-k]")) || e.target.closest(".ev-more[data-ks]");
    clearTimeout(tipShow);
    if (!ev) return tipClear();
    clearTimeout(tipHide);
    const x = e.clientX, y = e.clientY;
    tipShow = setTimeout(() => tipRender(x, y), 100);
  });
  $("cal-grid").addEventListener("mouseleave", tipClear);
  $("cal-grid").addEventListener("mousedown", tipClear);
  $("ev-add-tagpick").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) evAdd.toggleTag(b.dataset.tag); });
  $("ev-add-mempick").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) evAdd.toggleTag(b.dataset.tag, "ev-add-members"); });
  $("ev-add").addEventListener("submit", (e) => { e.preventDefault(); evAdd.submit(); });
  $("ev-add").addEventListener("input", () => { evAdd.update(); evAdd.markTags(); });
  $("ev-add-seg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b && !b.disabled) evAdd.setKind(b.dataset.kind); });
  $("ev-add-cancel").addEventListener("click", () => evAdd.close());
  $("ev-add-del").addEventListener("click", () => evAdd.remove());
  $("ev-add-memo-btn").addEventListener("click", () => {
    $("ev-add-memo-btn").hidden = true;
    $("ev-add-memo-k").hidden = $("ev-add-memo-row").hidden = false;
    $("ev-add-memo").focus();
  });
  $("ev-add").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); evAdd.close(); } });
  document.addEventListener("mousedown", (e) => { if (!$("ev-add").hidden && !e.target.closest("#ev-add")) evAdd.close(); });

  // 드래그 이동: 창 위쪽 막대
  for (const el of document.querySelectorAll(".panel-bar")) {
    el.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || e.target.closest("button")) return;
      e.preventDefault();
      if (bridge) bridge.startMove();
    });
  }
  for (const el of document.querySelectorAll(".rz")) {
    el.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      if (bridge) bridge.startResize(+el.dataset.e);
    });
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a) return;
    e.preventDefault();
    if (bridge) bridge.openUrl(a.href);
    else window.open(a.href);
  });
  document.addEventListener("contextmenu", (e) => e.preventDefault());
  setInterval(checkDateRollover, 30 * 1000);
  setInterval(loadOtherSchedules, 60 * 1000);
  let fitTimer = null;
  // 창 크기뿐 아니라 숨어 있다 보일 때도(그릴 때 숨어 있으면 넘침 정리가 안 돼 띠가 겹침) 다시 맞춤
  let gridSize = "";
  new ResizeObserver(([en]) => {
    const sz = Math.round(en.contentRect.width) + "x" + Math.round(en.contentRect.height);
    if (sz === gridSize) return;   // 넘침 정리(fitCells)로 생긴 미세 변화엔 다시 안 맞춤 → 깜빡임 방지
    gridSize = sz;
    clearTimeout(fitTimer);
    fitTimer = setTimeout(() => { if (state.mode === "cal" && state.calView === "month") CAL.fitCells($("cal-grid")); }, 100);
  }).observe($("cal-grid"));
  window.addEventListener("dogani-fz", () => { if (state.mode === "cal" && state.calView === "month") CAL.fitCells($("cal-grid")); });   // Ctrl+휠 = 달력 글자도 커짐 → +N 다시 셈

  // ------------------------------------------------------------ init
  const sameSpace = (a, b) => a && b && a.key === b.key && a.server === b.server && a.prefix === b.prefix;

  window.App = {
    setCalDrag(data) { calDrag = data || ""; },
    /**
     * 파이썬에서 호출 (바뀐 값만 와도 됨)
     * { mode, theme, space, spaces, calHidden, date, noteDate }
     */
    configure(cfg) {
      // 클릭 시 깨어나기 옵션: 다른 프로그램을 쓰는 중 → asleep (배경만 옅게, 글자·선은 그대로. DESIGN.md)
      if ("awake" in cfg) document.body.classList.toggle("asleep", !cfg.awake);
      if (cfg.awake === false) {   // 잠들면 클릭이 안 닿아 열린 편집이 떠 있는 채로 굳음 → 다 닫음
        evAdd.close();
        note.exit();
        const m = $("deco-menu");
        if (m) m.hidden = true;
      }
      if (cfg.theme) {
        document.body.classList.toggle("theme-light", cfg.theme !== "dark");
        document.body.classList.toggle("theme-dark", cfg.theme === "dark");
      }
      let first = false;
      if (cfg.mode && cfg.mode !== state.mode) {
        state.mode = cfg.mode;
        first = true;
        for (const m of ["note", "cal"]) document.body.classList.toggle("mode-" + m, m === cfg.mode);
        $("view-note").hidden = cfg.mode !== "note";
        $("view-cal").hidden = cfg.mode !== "cal";
      }
      if (cfg.spaces) state.spaces = cfg.spaces;
      if (cfg.calHidden) state.calHidden = new Set(cfg.calHidden);
      if (cfg.myName !== undefined) state.myName = cfg.myName;
      let moved = false;
      if (cfg.space) {
        moved = !sameSpace(cfg.space, state.space);
        if (moved) note.exit();
        state.space = cfg.space;
        document.body.classList.toggle("space-team", cfg.space.kind === "team");
      }

      if (moved) {
        state.writable = false;
        document.body.classList.remove("writable");
        connect();
      }
      if (moved || cfg.spaces) renderSpaceTabs();
      if (moved || cfg.spaces || cfg.calHidden) {
        renderSpaceFilter();
        rebuildSched();
        // 상태만 바뀐 push(20초마다 팀 확인 등)엔 다시 받지 않음: 보이는 스페이스 목록이 바뀔 때만
        const sig = shownSpaces().map((s) => s.key + "@" + spBase(s)).join("|");
        if (sig !== state.schedSig) { state.schedSig = sig; loadOtherSchedules(); }
      }
      if (state.mode === "note" && (first || moved)) showNote(first ? cfg.date || state.today : state.noteDate);
      if (state.mode === "cal") {
        if (cfg.noteDate) state.noteDate = cfg.noteDate;
        if (first) { state.calYM = state.noteDate.slice(0, 7); setCalView("month"); }
        else if (moved) setCalView(state.calView);
        else if (cfg.noteDate && state.calView === "month") renderMonth();
      }
    },
    /** 달력 창에서 고른 날짜를 할일 창에 표시 */
    showDate(date) {
      // 이미 그 날짜를 보고 있으면 다시 읽지 않음 (다시 읽으면 맨 위로 올라갔다 내려오며 깜빡임)
      if (state.mode === "note" && !(state.noteDate === date && note.doc)) showNote(date);
    },
    /** 달력에서 누른 할일: 그 노트로 가서 그 줄(줄 번호가 안 맞으면 같은 글자 줄)을 가운데로 + 강조 */
    async showTask(date, line, raw) {
      if (state.mode !== "note") return;
      if (!(state.noteDate === date && note.doc)) await showNote(date);
      if (!note.editor || !note.lines || state.noteDate !== date) return;
      const i = note.lines[line] === raw ? line : note.lines.indexOf(raw);
      note.editor.highlightLine(i < 0 ? line : i);
    },
    /** 할일 창의 ‹ › 오늘 (step: -1 / 1 / 0) */
    step(step) {
      if (state.mode !== "note") return;
      showNote(step === 0 ? state.today : CAL.nextWorkday(state.noteDate, step, state.sched));
    },
  };

  if (window.qt && window.QWebChannel) {
    new QWebChannel(qt.webChannelTransport, (channel) => {
      bridge = channel.objects.bridge;
      const start = () => bridge.getConfig((json) => App.configure(JSON.parse(json)));
      if (bridge.getUnsaved) bridge.getUnsaved((json) => { try { pendMem = JSON.parse(json) || {}; } catch (e) { pendMem = {}; } start(); });
      else start();
    });
  } else {
    // 일반 브라우저에서 테스트: index.html?mode=note&server=192.168.0.10:8765&prefix=/api&theme=dark
    const q = new URLSearchParams(location.search);
    window.__dogani = { note };   // 자동 시험용 (브라우저 모드에서만)
    App.configure({
      mode: q.get("mode") || "note", theme: q.get("theme") || "light",
      space: { key: "test", name: q.get("server") || "test", server: q.get("server") || "", prefix: q.get("prefix") || "/api", kind: "me", status: "offline" },
    });
  }
})();

// Ctrl+휠 = 글자 크기만 (기본 페이지 확대는 보더·여백까지 커져서 막음). 75~150%, Ctrl+0 원래대로
(() => {
  const KEY = "dogani-fz", MIN = 0.75, MAX = 1.5, STEP = 0.05;
  let fz = 1;
  try { fz = Math.min(MAX, Math.max(MIN, parseFloat(localStorage.getItem(KEY)) || 1)); } catch {}
  const apply = (v) => {
    fz = Math.round(Math.min(MAX, Math.max(MIN, v)) * 100) / 100;
    document.documentElement.style.setProperty("--fz", fz);
    try { localStorage.setItem(KEY, fz); } catch {}
    window.dispatchEvent(new Event("dogani-fz"));  // 편집기 줄 높이 다시 재기 (editor.js)
  };
  apply(fz);
  window.addEventListener("wheel", (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    apply(fz + (e.deltaY < 0 ? STEP : -STEP));
  }, { passive: false, capture: true });
  window.addEventListener("keydown", (e) => { if (e.ctrlKey && e.key === "0") { e.preventDefault(); apply(1); } }, true);
})();
