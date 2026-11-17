/*
 * 오버레이 UI 로직. 같은 페이지를 세 가지 창으로 쓴다 (state.mode)
 * - nav : 네비게이션 바 (하나뿐). 스페이스 이름 ▾ 를 누르면 스페이스 메뉴(팀 / 내), 할일 날짜 이동, 할일·달력 버튼
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
    return `${String(d.getFullYear()).slice(2)}${pad(d.getMonth() + 1)}${pad(d.getDate())} ${WD[d.getDay()]}`;
  };

  const state = {
    mode: "nav",       // nav | note | cal
    space: null,       // { key, name, server, prefix, kind: me|team|peer, status }
    spaces: [],        // nav: 메뉴 목록
    base: "",          // "http://ip:port/api" 또는 ".../api/team"
    today: iso(new Date()),
    noteDate: iso(new Date()),
    calYM: iso(new Date()).slice(0, 7),
    calView: "month",  // month | doc
    status: "offline", // online | offline | paused | blocked | team_only
    es: null,
    retryTimer: null,
    writable: false,
    calHidden: new Set(), // 달력·할일 일정에서 숨긴 스페이스 키 (설정 cal_hidden, 창끼리 같이 씀)
    evBySpace: {},     // 스페이스 키 → /schedule 일정 목록
    sched: null,       // 일정 색인 (CAL.index)
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
    paused: "상대방이 공유를 일시 중지했습니다.",
    blocked: "상대방이 접속을 차단했습니다.",
    team_only: "팀원에게만 공유하는 노트입니다. (트레이 > 팀 스페이스 > 팀 참여)",
    not_member: "팀 로그인이 필요합니다. (트레이 > 팀 스페이스)",
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
  }

  function connect() {
    clearTimeout(state.retryTimer);
    if (state.es) state.es.close();
    state.es = null;
    state.status = "";
    setStatus("offline");
    const sp = state.space;
    state.base = sp && sp.server ? "http://" + sp.server + sp.prefix : "";
    rebuildSched();
    if (!state.base) return;

    const es = new EventSource(state.base + "/events");
    const applyPaused = (e) => {
      setStatus(JSON.parse(e.data).paused ? "paused" : "online");
      refresh();
    };
    es.addEventListener("hello", (e) => {
      applyPaused(e);
      loadSchedule();
      api("/info").then((info) => {
        setWritable(!!info.writable);
        if (sp.kind === "peer" && info.name && bridge) bridge.setName(sp.key, info.name);
      }).catch(() => {});
    });
    es.addEventListener("status", applyPaused);
    es.addEventListener("schedule", () => loadSchedule());
    es.addEventListener("changed", (e) => {
      const dates = JSON.parse(e.data).dates || [];
      if (state.mode === "note" && dates.includes(state.noteDate)) note.reloadIfIdle();
      if (state.mode === "cal" && dates.some((d) => d.startsWith(state.calYM))) loadMonth(true);
    });
    es.addEventListener("reload", () => { loadSchedule(); refresh(); });
    es.onerror = () => {
      if (state.es !== es) return;
      // 브라우저가 자동 재연결을 포기한 경우(CLOSED: 403 등)에는 이유를 확인하고 직접 재시도
      if (es.readyState === EventSource.CLOSED) {
        api("/info")
          .then(() => api("/month?ym=" + state.calYM))   // 정보는 되는데 이벤트가 막힘 = 팀원 전용 등
          .then(() => setStatus("offline"))
          .catch((e) => setStatus(["blocked", "team_only", "not_member"].includes(e.code)
            ? (e.code === "blocked" ? "blocked" : "team_only") : "offline"))
          .finally(() => { refresh(); state.retryTimer = setTimeout(connect, 5000); });
      } else {
        setStatus("offline");
      }
    };
    state.es = es;
  }

  function refresh() {
    if (state.mode === "note") note.reloadIfIdle();
    else if (state.mode === "cal") { if (state.calView === "doc") doc.reloadIfIdle(); else loadMonth(); }
  }

  // ------------------------------------------------------------ schedule (보이는 스페이스들의 일정을 합쳐 그림)
  const spBase = (sp) => (sp && sp.server ? "http://" + sp.server + sp.prefix : "");
  const spaceByKey = (k) => state.spaces.find((s) => s.key === k) || (state.space && state.space.key === k ? state.space : null);
  /** 일정을 읽을 수 있는 스페이스 (지금 보는 스페이스 먼저). 다시 로그인 필요(blocked)·꺼진 항목은 뺌 */
  function schedSpaces() {
    const list = state.spaces.length ? state.spaces.filter((s) => s.server && !s.disabled && s.status !== "blocked")
      : state.space ? [state.space] : [];
    const cur = state.space && list.find((s) => s.key === state.space.key);
    return cur ? [cur, ...list.filter((s) => s !== cur)] : list;
  }
  const shownSpaces = () => schedSpaces().filter((s) => !state.calHidden.has(s.key));
  /** 일정을 고칠 수 있음 = 쓰기 권한 + 그 일정이 있는 스페이스가 모두 연결됨 (꺼진 호스트에 쓰다 멈추지 않게) */
  const canEdit = (ev) => state.writable && (ev.spaces || []).every((k) => { const sp = spaceByKey(k); return sp && sp.status === "online"; });
  /** 스페이스 표시 색: 내 스페이스 = 회색, 팀 = 이름 해시 색 (태그 색과 같은 방식) */
  const spaceColor = (sp) => (!sp || sp.kind === "me" ? "var(--sp-me)" : `hsl(${CAL.tagHue("space:" + sp.name)} 55% 55%)`);

  async function loadSchedule(sp) {
    sp = sp || state.space;
    if (!sp || !spBase(sp)) return;
    try {
      state.evBySpace[sp.key] = (await api("/schedule", undefined, spBase(sp))).events || [];
    } catch (e) {
      return; // 예전 버전 dogani(일정 API 없음) 이거나 연결 끊김: 기존 것 유지
    }
    rebuildSched();
  }

  /** 지금 보는 스페이스 말고 보이는 스페이스들 (지금 스페이스는 SSE 로 바로 받음, 나머지는 1분마다) */
  function loadOtherSchedules() {
    if (state.mode === "nav") return;
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
        m.spaces.push(sp.key);
      }
    }
    for (const m of list) {
      // 스페이스가 둘 이상 보일 때만 어느 스페이스 일정인지 표시 (색 막대 + 툴팁)
      m.spaceMarks = shown.length > 1 ? m.spaces.map((k) => { const sp = spaceByKey(k); return { name: sp ? sp.name : k, color: spaceColor(sp) }; }) : null;
    }
    list.sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : timeKey(a) < timeKey(b) ? -1 : timeKey(a) > timeKey(b) ? 1 : 0));
    state.sched = CAL.index(list);
    if (state.mode === "cal" && state.calView === "month") renderMonth();
    if (state.mode === "note") renderSchedRow();
  }

  /** 달력 위 스페이스 체크 (끄면 그 스페이스 일정을 달력·할일 창에서 숨김. 화면 공유할 때 내 일정 숨기기 등) */
  function renderSpaceFilter() {
    const box = $("cal-spaces");
    const list = schedSpaces();
    box.hidden = !list.length;
    box.innerHTML = list.map((sp) => `<label title="${esc(sp.kind === "me" ? "내 스페이스" : "팀 스페이스")} 일정 보이기"><input type="checkbox" data-key="${esc(sp.key)}"${state.calHidden.has(sp.key) ? "" : " checked"}>` +
      `<i style="background:${spaceColor(sp)}"></i>${esc(sp.name)}</label>`).join("");
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
          readOnly: !state.writable,
          tableHtml: MD.tableHtml,
          onChange(text, immediate) {
            p.lines = text.split("\n");
            if (o.progress) setProgress(...countTasks(p.lines));
            clearTimeout(timer);
            timer = setTimeout(p.save, 700);
            if (immediate) p.save();   // 체크박스 클릭은 바로 저장
          },
          onFocus() { if (state.writable) p.editing = true; },
          onBlur() { if (p.editing) { p.editing = false; p.save(); } },
          onSave: () => p.save(),
          openUrl: (url) => (bridge ? bridge.openUrl(url) : window.open(url)),
        });
      } else if (p.editor.getDoc() !== text) {
        p.editor.setDoc(text, keepScroll);
      }
      p.editor.setReadOnly(!state.writable);
      o.body.classList.add("has-editor");
      o.body.replaceChildren(host);
    }

    p.message = (html) => {
      p.lines = null;
      o.body.classList.remove("has-editor");
      o.body.innerHTML = `<div class="empty">${html}</div>`;
      if (o.progress) $("note-bar").parentElement.parentElement.classList.add("no-tasks");
      o.footer.textContent = "";
    };

    p.load = async (keepScroll) => {
      const key = o.key();
      let d;
      try {
        d = await o.fetch(key);
      } catch (e) {
        if (key !== o.key()) return;
        p.doc = null;
        p.message(`${esc(errorText(e))}<br><small>${esc(state.space ? state.space.server : "")}</small>`);
        return;
      }
      if (key !== o.key() || p.editing) return;
      p.doc = d;
      p.key = key;
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

    p.reloadIfIdle = () => { if (!p.editing) p.load(true); };

    /** 저장은 순서대로 하나씩 (자동 저장이 겹치면 mtime 충돌이 나므로). 스페이스·날짜는 부른 시점 기준 */
    p.save = () => {
      clearTimeout(timer);
      if (!p.lines || p.key == null) return chain;
      const content = p.lines.join(p.eol);
      const key = p.key, base = state.base;
      chain = chain.then(async () => {
        const target = p.doc && p.doc.exists && p.key === key ? p.doc : null;
        if (target && target.content === content) return true;
        try {
          const d = await o.post(key, content, target ? target.mtime : null, base);
          if (p.key === key && state.base === base) {
            p.doc = d;
            if (o.loaded) o.loaded(d);
            o.footer.textContent = "저장됨";
          }
          return true;
        } catch (e) {
          if (state.base === base) {
            o.footer.textContent = errorText(e);
            if (e.code === "conflict" && !p.editing) p.load(true);
          }
          return false;
        }
      });
      return chain;
    };

    p.exit = () => {
      if (p.editor && p.editor.hasFocus()) p.editor.blur();   // onBlur 에서 저장
      else if (p.editing) { p.editing = false; p.save(); }
    };

    /** 문서가 없는 날짜/파일: 빈 문서로 작성 시작 (첫 저장 때 파일 생성) */
    p.startBlank = () => {
      if (!state.writable || p.lines || !p.doc || p.doc.exists || p.doc.no_dir) return false;
      p.lines = [""];
      p.eol = "\n";
      p.key = o.key();
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
    fetch: (key) => api(`/note?date=${key.split("|")[1]}`),
    post: (key, content, base_mtime, base) => api("/note", { date: key.split("|")[1], content, base_mtime }, base),
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
      reportNoteDate(d.title);
    },
  });

  function renderSchedRow() {
    const row = $("note-sched");
    const html = CAL.dayEvents(state.noteDate, state.sched);
    row.innerHTML = html;
    row.hidden = !html;
  }

  /** 노트가 없거나 비었을 때: 직전 노트를 그대로 복사하는 버튼 */
  function copyButton(d) {
    if (!state.writable || !d || !d.prev || d.no_dir) return "";
    return `<button class="copy-prev" data-prev="${d.prev}">어제 노트 복사 <span>(${dayLabel(d.prev)})</span></button>`;
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

  function reportNoteDate(title) {
    const t = title || dayLabel(state.noteDate);
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
    note.load(false);
  }

  // ------------------------------------------------------------ calendar (달력 창)
  // 받아 온 월 데이터 (일정만 바뀌거나 선택 날짜만 바뀌면 다시 받지 않고 그리기만)
  let monthData = { key: "", days: {}, failed: false };
  let monthReq = null;

  async function loadMonth(force) {
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

  function renderMonth() {
    if (monthData.key !== state.base + "|" + state.calYM) return loadMonth();
    const r = CAL.renderMonth({ ym: state.calYM, days: monthData.days, idx: state.sched, today: state.today, selected: state.noteDate });
    $("cal-grid").innerHTML = r.html;
    CAL.fitCells($("cal-grid"));
    $("cal-footer").textContent = monthData.failed || `노트 ${r.notes}개 · 완료 ${r.done}/${r.total}`;
  }

  const doc = DocPane({
    body: $("doc-body"),
    footer: $("doc-footer"),
    key: () => state.base + "|sched",
    fetch: () => api("/sched-doc"),
    post: (key, content, base_mtime, base) => api("/sched-doc", { content, base_mtime }, base),
    empty: (d) => (d.no_dir ? "스페이스 폴더가 지정되지 않았습니다." : state.writable ? "일정 파일이 없습니다.<br>클릭해서 작성" : "일정 파일이 없습니다."),
    loaded: (d) => { $("doc-title").textContent = d.title || "일정"; },
  });

  function setCalView(view) {
    if (view !== "doc") doc.exit();
    state.calView = view;
    $("view-cal").hidden = view !== "month";
    $("view-doc").hidden = view !== "doc";
    if (view === "doc") doc.load(false);
    else loadMonth();
  }

  // ------------------------------------------------------------ nav (네비게이션 바 + 스페이스 메뉴)
  const GROUPS = [["team", "팀 스페이스"], ["me", "내 스페이스"]];

  function renderNav(cfg) {
    const sp = state.space;
    if (!sp) return;
    const dot = $("status-dot");
    for (const s of ["online", "paused", "blocked"]) dot.classList.toggle(s, (sp.status === "team_only" ? "blocked" : sp.status) === s);
    $("space-name").textContent = sp.name;
    $("nav").title = `${sp.kind === "team" ? "팀 스페이스" : "내 스페이스"} · 드래그해서 이동`;
    renderMenu(cfg);
    reportNavSize();
  }

  function renderMenu(cfg) {
    const cur = state.space && state.space.key;
    let html = "";
    for (const [group, title] of GROUPS) {
      const items = state.spaces.filter((s) => s.group === group);
      if (group === "team" && !items.length) {
        html += `<div class="sm-title">팀 스페이스</div>`;
        html += `<button class="sm-item sm-action" data-action="join_team"><span class="sm-glyph">+</span><span class="sm-name">팀 참여하기...</span></button>`;
        if ((cfg || {}).teamState === "none") {
          html += `<button class="sm-item sm-action" data-action="start_host"><span class="sm-glyph">⌂</span><span class="sm-name">새 팀 만들기 (이 PC 가 호스트)...</span></button>`;
        }
        continue;
      }
      if (!items.length) continue;
      if (title) html += `<div class="sm-title">${title}</div>`;
      for (const s of items) {
        const st = s.status === "team_only" ? "blocked" : s.status;
        html += `<button class="sm-item${s.key === cur ? " active" : ""}" data-key="${esc(s.key)}"${s.disabled ? " disabled" : ""}>` +
          `<span class="sm-name">${esc(s.name)}</span>` +
          (s.sub ? `<span class="sm-sub">${esc(s.sub)}</span>` : "") +
          `<span class="dot ${st}"></span></button>`;
      }
    }
    html += `<div class="sm-sep"></div>`;
    html += `<button class="sm-item sm-action" data-action="rename"><span class="sm-glyph">✎</span><span class="sm-name">내 이름 바꾸기</span></button>`;
    html += `<button class="sm-item sm-action" data-action="browse_dir"><span class="sm-glyph">+</span><span class="sm-name">스페이스 열기...</span></button>`;
    $("space-menu").innerHTML = html;
  }

  function setMenu(open) {
    $("space-menu").hidden = !open;
    $("space-btn").classList.toggle("active", open);
    reportNavSize();
  }

  function setOpen(open) {
    $("btn-note").classList.toggle("active", !!open.note);
    $("btn-cal").classList.toggle("active", !!open.cal);
  }

  function reportNavSize() {
    if (state.mode !== "nav") return;
    requestAnimationFrame(() => {
      // 그림자가 잘리지 않도록 #root 여백까지 포함한 크기
      const cs = getComputedStyle($("root"));
      const nav = $("nav").getBoundingClientRect();
      let w = nav.width, h = nav.height;
      if (!$("space-menu").hidden) {
        const m = $("space-menu").getBoundingClientRect();
        w = Math.max(w, m.width);
        h = m.bottom - nav.top;
      }
      w += parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
      h += parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
      if (bridge) bridge.setNavSize(Math.ceil(w), Math.ceil(h));
    });
  }

  // ------------------------------------------------------------ edit permission
  function setWritable(w) {
    state.writable = w;
    document.body.classList.toggle("writable", w);
    if (!w) { note.exit(); doc.exit(); }
    note.setReadOnly(!w);
    doc.setReadOnly(!w);
    $("cal-edit").hidden = false;
    refresh();
  }

  function checkDateRollover() {
    const now = iso(new Date());
    if (now === state.today) return;
    const prev = state.today;
    state.today = now;
    // 오늘 노트를 보고 있었다면 자정에 다음 날로 넘어간다 (편집 중이면 끝난 뒤에)
    if (state.mode === "note" && state.noteDate === prev && !note.editing) showNote(now);
    if (state.mode === "cal" && state.calView === "month") renderMonth();
  }

  // ------------------------------------------------------------ events
  $("space-btn").addEventListener("click", () => setMenu($("space-menu").hidden));
  $("space-menu").addEventListener("click", (e) => {
    const item = e.target.closest(".sm-item");
    if (!item || item.disabled) return;
    setMenu(false);
    if (item.dataset.action) { if (bridge) bridge.action(item.dataset.action); return; }
    if (bridge) bridge.selectSpace(item.dataset.key);
  });
  window.addEventListener("blur", () => { if (state.mode === "nav" && !$("space-menu").hidden) setMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("space-menu").hidden) setMenu(false); });

  $("btn-note").addEventListener("click", () => { setMenu(false); if (bridge) bridge.togglePanel("note"); });
  $("btn-cal").addEventListener("click", () => { setMenu(false); if (bridge) bridge.togglePanel("cal"); });
  // 화살표는 토/일·쉬는 날을 건너뜀 (그 날 노트는 달력에서 눌러서 열기). 할일 창에 전달
  $("note-prev").addEventListener("click", () => bridge && bridge.stepNote(-1));
  $("note-next").addEventListener("click", () => bridge && bridge.stepNote(1));
  $("note-today").addEventListener("click", () => bridge && bridge.stepNote(0));

  for (const btn of document.querySelectorAll(".close-btn")) {
    btn.addEventListener("click", () => {
      note.exit();
      doc.exit();
      // 닫기 전에 저장이 끝나도록
      Promise.all([note.save(), doc.save()]).then(() => { if (bridge) bridge.closePanel(); });
    });
  }
  // 편집 영역 밖(바 등)을 누르거나 창이 포커스를 잃으면 편집 끝
  document.addEventListener("mousedown", (e) => {
    if (note.editing && !e.target.closest("#note-body")) note.exit();
    if (doc.editing && !e.target.closest("#doc-body")) doc.exit();
  });
  window.addEventListener("blur", () => { note.exit(); doc.exit(); });

  $("cal-prev").addEventListener("click", () => { state.calYM = addMonths(state.calYM, -1); loadMonth(); });
  $("cal-next").addEventListener("click", () => { state.calYM = addMonths(state.calYM, 1); loadMonth(); });
  $("cal-today").addEventListener("click", () => { state.calYM = state.today.slice(0, 7); loadMonth(); });
  $("cal-edit").addEventListener("click", () => setCalView("doc"));
  $("doc-back").addEventListener("click", () => doc.save().then(() => setCalView("month")));
  $("cal-grid").addEventListener("click", (e) => {
    if (dragged) { dragged = false; return; }  // 드래그로 기간 고르거나 일정 옮긴 뒤의 click 은 무시
    const ev = evOf(e.target.closest(".ev[data-k]"));
    // 일정 클릭 = 수정 팝업 (읽기 전용이면 그냥 노트 열기)
    if (ev && canEdit(ev)) { evAdd.open("", "", e.clientX, e.clientY, ev); return; }
    const cell = e.target.closest(".cal-day");
    if (!cell) return;
    state.noteDate = cell.dataset.date;
    for (const c of $("cal-grid").querySelectorAll(".cal-day.selected")) c.classList.remove("selected");
    cell.classList.add("selected");
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
  const yymmdd = (v) => v.slice(2).replace(/-/g, "");
  const cleanField = (v) => String(v || "").replace(/,/g, " ").trim();
  /** 일정 dict → 파일 한 줄 (팝업 입력값과 같은 모양: kind, s, e, t1, t2, about, bold, off, title, tags[], place, memo) */
  function schedLine(o) {
    let date = o.s && o.e && o.s !== o.e ? `${yymmdd(o.s)}-${yymmdd(o.e)}` : o.s && o.e ? yymmdd(o.s) : o.s ? `${yymmdd(o.s)}-` : `-${yymmdd(o.e)}`;
    if (o.off) date = `==${date}==`;
    const mk = (o.bold ? "**" : "") + (o.kind === "span" ? "*" : "");
    const time = o.t1 ? " " + o.t1 + (o.t2 ? "-" + o.t2 : "") + (o.about ? "?" : "") : "";
    const parts = [mk + date + mk + time, cleanField(o.title)];
    const tags = o.tags.map((t) => t.replace(/[#\s]/g, "")).filter(Boolean).map((t) => "#" + t);
    if (tags.length) parts.push(tags.join(" "));
    const place = cleanField(o.place), memo = cleanField(o.memo);
    if (place || memo) parts.push(place);  // 장소 없이 메모만이면 "제목, , 메모"
    if (memo) parts.push(memo);
    return parts.join(", ");
  }
  /** 화면의 일정(ev) → schedLine 입력 모양 (시작/끝 미정은 그쪽을 비움) */
  function evToObj(ev) {
    const [t1, t2] = (ev.time || "").split("-");
    return {
      kind: ev.italic ? "span" : "event",
      s: ev.open === "start" ? "" : ev.start, e: ev.open === "end" ? "" : ev.end,
      t1: t1 || "", t2: t2 || "", about: !!ev.uncertain, bold: !!ev.important, off: !!ev.holiday,
      title: ev.title, tags: ev.tags || [], place: ev.place || "", memo: ev.memo || "",
    };
  }
  /** 스페이스 sp 의 일정 파일 고치기: fn(text) → 새 text. 충돌(409) 나면 다시 읽고 한 번 더 */
  async function editSchedDoc(sp, fn) {
    const base = spBase(sp);
    for (let tries = 0; tries < 2; tries++) {
      try {
        const d = await api("/sched-doc", undefined, base);
        if (d.no_dir) throw new Error("스페이스 폴더가 지정되지 않았습니다.");
        await api("/sched-doc", { content: fn(d.exists ? d.content : ""), base_mtime: d.exists ? d.mtime : undefined }, base);
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
  async function writeEv(ev, line, keys) {
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
            if (i < 0) throw new Error("일정 파일에서 이 줄을 찾지 못했습니다 (다른 일정 파일에 있거나 그 사이 바뀜). '일정 편집'에서 고쳐 주세요.");
            if (want && n === 0) {
              const lead = lines[i].match(/^\s*/)[0], m = lines[i].trim().match(LIST_RE);
              lines[i] = lead + (m ? m[1] : "") + line + (lines[i].endsWith("\r") ? "\r" : "");
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
        $("ev-add-off").checked = o.off;
        for (const [id, t] of [["ev-add-t1", o.t1], ["ev-add-t2", o.t2]]) {
          const sel = $(id);
          if (t && ![...sel.options].some((op) => op.value === t)) sel.add(new Option(t, t));  // 7:15 같은 30분 단위 밖 시각
          sel.value = t;
        }
        $("ev-add-tags").value = o.tags.join(", ");
        $("ev-add-place").value = o.place;
        $("ev-add-memo").value = o.memo;
      }
      const hasMemo = !!(o && o.memo);
      $("ev-add-memo-row").hidden = $("ev-add-memo-k").hidden = !hasMemo;
      $("ev-add-memo-btn").hidden = hasMemo;
      // 스페이스 고르기: 보이는 스페이스만 (새 일정 = 지금 보는 스페이스, 수정 = 그 일정이 있는 스페이스들)
      const keys = ev ? ev.spaces || [] : [state.space && state.space.key];
      // 지금 보는 스페이스는 숨겼어도 고를 수 있게 (화면 공유 중에 새 일정 추가)
      const cur = state.space && !state.calHidden.has(state.space.key) ? null : schedSpaces().find((s) => state.space && s.key === state.space.key);
      $("ev-add-spaces").innerHTML = (cur ? [cur, ...shownSpaces()] : shownSpaces()).map((sp) => `<label title="${sp.kind === "me" ? "내 스페이스" : "팀 스페이스"}">` +
        `<input type="checkbox" data-key="${esc(sp.key)}"${keys.includes(sp.key) ? " checked" : ""}><i style="background:${spaceColor(sp)}"></i>${esc(sp.name)}</label>`).join("");
      $("ev-add-del").hidden = !ev;
      $("ev-add-ok").textContent = ev ? "저장" : "추가";
      $("ev-add-err").textContent = "";
      this.setKind(o ? o.kind : "event");
      f.hidden = false;
      const w = f.offsetWidth, h = f.offsetHeight;
      f.style.left = Math.max(4, Math.min(x, innerWidth - w - 4)) + "px";
      f.style.top = Math.max(4, Math.min(y, innerHeight - h - 4)) + "px";
      $("ev-add-name").focus();
    },
    close() { $("ev-add").hidden = true; this.ev = null; markRange(null); },
    setKind(kind) {
      this.kind = kind;
      for (const b of $("ev-add-seg").children) b.classList.toggle("on", b.dataset.kind === kind);
      $("ev-add-start").title = kind === "event" ? "시작 날짜" : "비우면 시작 미정 (마감만 있을 때)";
      $("ev-add-end").title = kind === "event" ? "비우면 하루" : "비우면 끝 미정";
      this.update();
    },
    /** [시작, 끝] — 일정은 끝을 비우면 하루, 기간은 비운 쪽이 미정("") */
    range() {
      let s = $("ev-add-start").value, e = $("ev-add-end").value;
      if (s && e && e < s) [s, e] = [e, s];
      if (this.kind === "event") return s ? [s, e || s] : ["", ""];
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
      const kind = this.kind === "span" ? "기간 →" : s && e && s !== e ? "일정 ▬" : "일정";
      $("ev-add-hint").textContent = date ? `${kind} ${date}${time}${t1 && about ? " (애매함)" : ""}` : "";
      markRange(s || e ? [s || e, e || s] : null);
    },
    values() {
      const [s, e] = this.range();
      return {
        kind: this.kind, s, e,
        t1: $("ev-add-t1").value, t2: $("ev-add-t2").value, about: $("ev-add-about").checked,
        bold: $("ev-add-bold").checked, off: $("ev-add-off").checked,
        title: $("ev-add-name").value, tags: $("ev-add-tags").value.split(/[,，]/),
        place: $("ev-add-place").value, memo: $("ev-add-memo").value,
      };
    },
    spaceKeys() { return [...$("ev-add-spaces").querySelectorAll("input:checked")].map((i) => i.dataset.key); },
    async submit() {
      const [st, en] = this.range();
      const keys = this.spaceKeys();
      if (!keys.length) { $("ev-add-err").textContent = "스페이스를 하나 이상 고르세요"; return; }
      if (!st && !en) { $("ev-add-err").textContent = this.kind === "event" ? "시작 날짜를 고르세요" : "날짜를 하나 이상 고르세요"; return; }
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
        if (e.partial) { this.close(); alert(msg + "\n\n나머지 스페이스는 저장됐습니다. 일정을 다시 열어 확인하세요."); }
        else $("ev-add-err").textContent = msg;
      }
    },
    async remove() {
      if (!this.ev) return;
      const where = (this.ev.spaces || []).length > 1
        ? `\n(${this.ev.spaces.map((k) => (spaceByKey(k) || { name: k }).name).join(", ")} 모두에서 지웁니다. 한 곳에서만 빼려면 그 스페이스 체크를 끄고 저장)` : "";
      if (!confirm(`'${this.ev.title}' 일정을 지울까요?${where}`)) return;
      try {
        await writeEv(this.ev, null, []);
        this.close();
      } catch (e) {
        if (e.partial) { this.close(); alert("삭제 실패: " + (e.message || e) + "\n\n나머지 스페이스에서는 지웠습니다."); }
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
  $("cal-grid").addEventListener("mousedown", (e) => {
    dragged = false;
    measureCells();
    const evEl = e.target.closest(".ev[data-k]");
    const ev = evOf(evEl);
    if (ev && e.button === 0) {
      // 일정 위 왼쪽 버튼: 옮기기 준비 (그냥 떼면 click → 수정 팝업). 읽기 전용은 안 됨
      const c = canEdit(ev) && cellAt(e.clientX, e.clientY);
      if (c) move = { ev, el: evEl, from: c.dataset.date, to: c.dataset.date };
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
    markRange(n ? [CAL.addDays(move.ev.start, n), CAL.addDays(move.ev.end, n)] : null);
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
      markRange(null);
      if (!n) return;  // 제자리 = 클릭 (수정 팝업은 click 에서)
      dragged = true;
      const o = evToObj(m.ev);
      if (o.s) o.s = CAL.addDays(o.s, n);
      if (o.e) o.e = CAL.addDays(o.e, n);
      try {
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
  $("cal-spaces").addEventListener("change", (e) => {
    const key = e.target.dataset.key;
    if (!key) return;
    if (e.target.checked) state.calHidden.delete(key); else state.calHidden.add(key);
    if (bridge) bridge.setCalHidden(JSON.stringify([...state.calHidden]));  // 설정 저장 + 할일 창도 따라감
    rebuildSched();
    if (e.target.checked) loadSchedule(spaceByKey(key));
  });
  $("ev-add").addEventListener("submit", (e) => { e.preventDefault(); evAdd.submit(); });
  $("ev-add").addEventListener("input", () => evAdd.update());
  $("ev-add-seg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) evAdd.setKind(b.dataset.kind); });
  $("ev-add-cancel").addEventListener("click", () => evAdd.close());
  $("ev-add-del").addEventListener("click", () => evAdd.remove());
  $("ev-add-memo-btn").addEventListener("click", () => {
    $("ev-add-memo-btn").hidden = true;
    $("ev-add-memo-k").hidden = $("ev-add-memo-row").hidden = false;
    $("ev-add-memo").focus();
  });
  $("ev-add").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); evAdd.close(); } });
  document.addEventListener("mousedown", (e) => { if (!$("ev-add").hidden && !e.target.closest("#ev-add")) evAdd.close(); });

  // 드래그 이동: 네비게이션 바, 창 위쪽 막대
  for (const el of [$("nav"), ...document.querySelectorAll(".panel-bar")]) {
    el.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || e.target.closest("button")) return;
      e.preventDefault();
      if (bridge) bridge.startMove();
    });
  }
  $("resize-grip").addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    if (bridge) bridge.startResize();
  });
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
  new ResizeObserver(() => {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(() => { if (state.mode === "cal" && state.calView === "month") CAL.fitCells($("cal-grid")); }, 100);
  }).observe($("cal-grid"));

  // ------------------------------------------------------------ init
  const sameSpace = (a, b) => a && b && a.key === b.key && a.server === b.server && a.prefix === b.prefix;

  window.App = {
    /**
     * 파이썬에서 호출 (바뀐 값만 와도 됨)
     * { mode, theme, space, spaces, calHidden, date, noteDate,  nav: open, noteLabel, me, teamState }
     */
    configure(cfg) {
      if (cfg.theme) {
        document.body.classList.toggle("theme-light", cfg.theme !== "dark");
        document.body.classList.toggle("theme-dark", cfg.theme === "dark");
      }
      let first = false;
      if (cfg.mode && cfg.mode !== state.mode) {
        state.mode = cfg.mode;
        first = true;
        for (const m of ["nav", "note", "cal"]) document.body.classList.toggle("mode-" + m, m === cfg.mode);
        const panel = cfg.mode !== "nav";
        $("panel").hidden = !panel;
        $("view-note").hidden = cfg.mode !== "note";
        $("view-cal").hidden = cfg.mode !== "cal";
        $("view-doc").hidden = true;
        document.body.classList.toggle("expanded", panel);
        if (bridge) bridge.setExpanded(panel);
      }
      if (cfg.spaces) state.spaces = cfg.spaces;
      if (cfg.calHidden) state.calHidden = new Set(cfg.calHidden);
      let moved = false;
      if (cfg.space) {
        moved = !sameSpace(cfg.space, state.space);
        if (moved) { note.exit(); doc.exit(); }
        state.space = cfg.space;
        document.body.classList.toggle("space-team", cfg.space.kind === "team");
      }

      if (state.mode === "nav") {
        if (cfg.open) setOpen(cfg.open);
        renderNav(cfg);
        return;
      }
      if (moved) {
        state.writable = false;
        document.body.classList.remove("writable");
        connect();
      }
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
      if (state.mode === "note") showNote(date);
    },
    /** 네비게이션 바의 ‹ › 오늘 (step: -1 / 1 / 0) */
    step(step) {
      if (state.mode !== "note") return;
      showNote(step === 0 ? state.today : CAL.nextWorkday(state.noteDate, step, state.sched));
    },
  };

  if (window.qt && window.QWebChannel) {
    new QWebChannel(qt.webChannelTransport, (channel) => {
      bridge = channel.objects.bridge;
      bridge.getConfig((json) => App.configure(JSON.parse(json)));
    });
  } else {
    // 일반 브라우저에서 테스트: index.html?mode=note&server=192.168.0.10:8765&prefix=/api&theme=dark
    const q = new URLSearchParams(location.search);
    App.configure({
      mode: q.get("mode") || "note", theme: q.get("theme") || "light",
      space: { key: "test", name: q.get("server") || "test", server: q.get("server") || "", prefix: q.get("prefix") || "/api", kind: "peer", status: "offline" },
    });
  }
})();
