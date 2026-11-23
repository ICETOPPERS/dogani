/*
 * 달력·일정 모듈
 * - 날짜 계산 도우미 (app.js 와 같이 씀)
 * - 일정 목록(/api/schedule) → 날짜별 색인, 쉬는 날(==하이라이트== 일정 + 토/일) 판정
 * - 월 달력 HTML 그리기, 태그별 색, 노트 위 일정 줄
 */
(function () {
  "use strict";

  const WD = ["일", "월", "화", "수", "목", "금", "토"];
  const pad = (n) => String(n).padStart(2, "0");
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parseISO = (s) => {
    const [y, m, d] = s.split("-").map(Number);
    return new Date(y, m - 1, d);
  };
  const addDays = (s, n) => {
    const d = parseISO(s);
    d.setDate(d.getDate() + n);
    return iso(d);
  };
  const addMonths = (ym, n) => {
    const [y, m] = ym.split("-").map(Number);
    const d = new Date(y, m - 1 + n, 1);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
  };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  /** 태그 → 색 (태그 이름 해시로 정해서 다시 켜도 같은 색) */
  function tagHue(tag) {
    let h = 2166136261;
    for (const ch of tag) h = Math.imul(h ^ ch.codePointAt(0), 16777619);
    // 골든 앵글로 흩어서 비슷한 이름도 색이 멀어지게
    return Math.round(((h >>> 0) * 137.508) % 360);
  }
  // 근태 = 빨강(--sun), 태그 = 태그 색, 태그 없는 할일 = --text-muted (본문보다 한 단계 옅게), 나머지 = --accent
  const eventColor = (ev) => (ev.off ? "var(--sun)" : ev.tags && ev.tags.length ? `hsl(${tagHue(ev.tags[0])} 70% 50%)` : ev.task ? "var(--text-muted)" : "var(--accent)");

  // ---- @이름: cleanName = 일정 줄에 적는 꼴(공백·@·#·쉼표 뺌), nameKey = 비교용(+소문자, schedule.name_key 와 같은 규칙)
  const cleanName = (v) => String(v || "").replace(/[@#,，\s]/g, "");
  const nameKey = (v) => cleanName(v).toLowerCase();
  /** 이름 목록 → cleanName 꼴, 빈 이름·같은 nameKey 는 처음 것만 */
  function uniqNames(list) {
    const seen = new Set();
    return (list || []).map(cleanName).filter((n) => n && !seen.has(nameKey(n)) && seen.add(nameKey(n)));
  }

  /**
   * 근태 (==날짜== 줄, 날짜 줄 자리에 표시, 일정 칸에는 안 그림):
   * 보는 사람마다 다름: "rest" = 멤버 없음(모두) 또는 멤버에 내가 있음 → 내 쉬는 날(빨간 칸·노트 이동 건너뜀),
   * "leave" = 남의 근태 → 날짜 옆 빨간 글자만. 아니면 ""
   * reach = 팀스페이스가 @그룹·@all 을 팀원 이름으로 펼친 목록. @all 은 개인 스페이스에서도 모두
   */
  function offKind(ev, me) {
    if (!ev.holiday) return "";
    const ms = (ev.reach || ev.members || []).map(nameKey);
    return !ms.length || ms.includes("all") || (me && ms.includes(nameKey(me))) ? "rest" : "leave";
  }

  /**
   * 일정 목록 → { byDate: {iso: [ev]}, off: Set(iso), slot: {iso: [ev|공휴일]}, list }
   * (ev.k = list 안 번호, 달력에서 data-k 로 찾음). hol = {iso: [이름]} 받아 온 공휴일, me = 내 이름
   */
  function index(events, hol, me) {
    const byDate = {}, slot = {};
    const off = new Set();
    const list = events || [];
    list.forEach((ev, k) => { ev.k = k; ev.off = offKind(ev, me); });
    for (const ev of list) {
      let d = ev.start;
      for (let k = 0; d <= ev.end && k < 366; k++, d = addDays(d, 1)) {
        // 시작/끝 미정 일정의 페이드용 하루는 쉬는 날로 치지 않음
        const fake = (ev.open === "start" && d === ev.start) || (ev.open === "end" && d === ev.end);
        if (ev.off) {
          // 여러 날 근태는 토·일을 빼고 (연차를 늘려도 주말은 그대로). 하루짜리를 주말에 직접 넣은 건 보임
          if (fake || (ev.start !== ev.end && isWeekend(d))) continue;
          (slot[d] = slot[d] || []).push(ev);
          if (ev.off === "rest") off.add(d);
          continue;
        }
        (byDate[d] = byDate[d] || []).push(ev);
      }
    }
    for (const [d, names] of Object.entries(hol || {})) {
      (slot[d] = slot[d] || []).unshift(...names.map((title) => ({ pub: true, title })));
      off.add(d);
    }
    // 자리 순서: 공휴일 > 쉬는 날 > 근태
    const rank = { rest: 2, leave: 3 };
    for (const d in slot) slot[d].sort((a, b) => (a.pub ? 1 : rank[a.off]) - (b.pub ? 1 : rank[b.off]));
    return { byDate, off, slot, list };
  }

  const isWeekend = (s) => { const w = parseISO(s).getDay(); return w === 0 || w === 6; };
  const isOff = (s, idx) => isWeekend(s) || !!(idx && idx.off.has(s));

  /** 날짜 줄 자리 (공휴일·근태). 공휴일은 받은 것이라 못 고침 (누르면 빈 칸처럼 그날 노트), 근태는 눌러서 수정 */
  function slotHtml(d, idx) {
    const items = (idx && idx.slot[d]) || [];
    if (!items.length) return "";
    const html = items.map((x) => {
      if (x.pub) return `<span class="hol pub" title="공휴일 · ${esc(x.title)}">${esc(x.title)}</span>`;
      const who = x.members.length ? x.members.map((m) => "@" + m).join(" ") + " " : "";
      const cls = ["ev", "hol", x.off];
      return `<span ${evAttrs(x, cls)}>${esc(who + x.title)}</span>`;
    }).join("");
    return `<span class="slot">${html}</span>`;
  }

  /** 날짜 줄 자리 글자 목록 (정보 카드 머리용): ["성탄절", "@송민 연차"] */
  function slotLabels(d, idx) {
    return ((idx && idx.slot[d]) || []).map((x) => (x.pub || !x.members.length ? "" : x.members.map((m) => "@" + m).join(" ") + " ") + x.title);
  }

  /** s 에서 dir(±1) 방향으로 다음 근무일 (토/일·쉬는 날 건너뜀) */
  function nextWorkday(s, dir, idx) {
    let d = addDays(s, dir);
    for (let k = 0; k < 60 && isOff(d, idx); k++) d = addDays(d, dir);
    return d;
  }

  const OFF_NAME = { rest: "근태 · 쉬는 날", leave: "근태" };
  function eventTip(ev) {
    if (ev.task) {
      const r = ev.start === ev.end ? ev.end : `${ev.start} ~ ${ev.end}`;
      return [`${ev.task.done ? "[완료] " : ""}${ev.title}`, r, `할일 · ${ev.task.note} 노트`].join("\n");
    }
    const range = ev.open === "start" ? `~ ${ev.end}`
      : ev.open === "end" ? `${ev.start} ~`
      : ev.start === ev.end ? ev.start : `${ev.start} ~ ${ev.end}`;
    return [
      `${ev.title}${OFF_NAME[ev.off] ? ` (${OFF_NAME[ev.off]})` : ""}`,
      `${range}${ev.time ? " " + timeText(ev) : ""}${ev.uncertain ? " (정확한 시각 미정)" : ""}`,
      ev.place && `장소: ${ev.place}`,
      ev.tags && ev.tags.length && ev.tags.map((t) => "#" + t).join(" "),
      ev.members && ev.members.length && "멤버: " + ev.members.join(", "),
      ev.memo && `메모: ${ev.memo}`,
      ev.who && whoText(ev.who),
    ].filter(Boolean).join("\n");
  }

  /** 달력 일정 호버 카드 (네모 상자에 정보 전부). tagColor(t) → 색 */
  function eventCard(ev, tagColor) {
    const row = (k, v) => (v ? `<div class="tc-row"><span class="tc-k">${k}</span><span class="tc-v">${v}</span></div>` : "");
    const t = ev.task;
    const range = t && t.plan ? `${ev.start} 완료 (예정 ${t.plan[0] === t.plan[1] ? t.plan[1] : t.plan.join(" ~ ")})`
      : t ? (ev.start === ev.end ? ev.end : `${ev.start} ~ ${ev.end}`)
      : ev.open === "start" ? `~ ${ev.end}`
      : ev.open === "end" ? `${ev.start} ~`
      : ev.start === ev.end ? ev.start : `${ev.start} ~ ${ev.end}`;
    const when = esc(range) + (ev.time ? " " + esc(timeText(ev)) : "") + (ev.uncertain ? ' <span class="tc-dim">(시각 미정)</span>' : "");
    const badges = [t && (t.done ? "완료" : "할일"), ev.important && "중요", OFF_NAME[ev.off]].filter(Boolean)
      .map((b) => `<span class="tc-badge">${b}</span>`).join("");
    return `<div class="tc-title" style="--ev:${eventColor(ev)}">${esc(ev.title)}${badges}</div>` +
      (t ? "" : row("날짜", when)) +
      row("장소", ev.place && esc(ev.place)) +
      row("스페이스", ev.spaceMarks && ev.spaceMarks.map((m) => `<span class="tc-chip">${esc(m.name)}</span>`).join("")) +
      row("태그", (ev.tags || []).map((g) => `<span class="tc-chip tag-chip" style="--tc:${tagColor(g)}">${esc(g)}</span>`).join("")) +
      row("멤버", (ev.members || []).map((m) => `<span class="tc-chip">@${esc(m)}</span>`).join("")) +
      row("메모", ev.memo && esc(ev.memo)) +
      row("노트", t && esc(t.note) + " 등록") +
      row("작성", ev.who && ev.who.by && esc(ev.who.by)) +
      row("수정", ev.who && ev.who.mod && esc(ev.who.mod));
  }

  /** 팀스페이스 일정의 작성·수정 팀원 (줄 끝 꼬리표) */
  const whoText = (w) => [w.by && `작성 ${w.by}`, w.mod && `수정 ${w.mod}`].filter(Boolean).join(" · ");

  const q = (ev) => (ev.uncertain ? "쯤" : "");  // TODO 로캘: 영어면 "~10:00" 등
  const timeText = (ev) => (ev.time || "") + q(ev);
  const shortTime = (ev) => (ev.time || "").split("-")[0] + q(ev);
  // 스페이스가 둘 이상 보이면(ev.spaceMarks) 툴팁 앞에 [스페이스 이름들]. 스페이스 색은 달력 위 칩에 마우스를 올릴 때만 (app.js)
  // 할일(내 스페이스 노트의 날짜 붙은 체크박스): 앞에 체크 네모(누르면 노트 줄 체크), 완료면 취소선 (ev.task = {sp, note, line, raw, done})
  const taskBox = (ev) => (ev.task ? `<span class="ev-check${ev.task.done ? " on" : ""}"></span>` : "");
  const evAttrs = (ev, cls) => `class="${cls.join(" ")}${ev.task ? " task" + (ev.task.done ? " done" : "") : ""}${ev.stale ? " stale" : ""}" data-k="${ev.k ?? ""}" style="--ev:${eventColor(ev)}" ` +
    `data-tip="${esc((ev.spaceMarks ? `[${ev.spaceMarks.map((m) => m.name).join(" · ")}] ` : "") + eventTip(ev))}"`;

  /** 노트 위 일정 줄용 칩 (시간·장소까지) */
  function chip(ev) {
    const cls = ["ev"];
    if (ev.important) cls.push("important");
    if (ev.off) cls.push("holiday");
    if (ev.uncertain) cls.push("unsure");
    const who = ev.off && ev.members.length ? ev.members.map((m) => "@" + m).join(" ") + " " : "";
    const time = ev.time ? `<span class="ev-time">${esc(timeText(ev))}</span>` : "";
    const place = ev.place ? `<span class="ev-place">· ${esc(ev.place)}</span>` : "";
    return `<span ${evAttrs(ev, cls)}>${taskBox(ev)}${time}${flag(ev)}${esc(who + ev.title)}${place}</span>`;
  }

  const FLAG = '<svg class="ev-flag" viewBox="0 0 12 12"><path d="M2.5 1v10.5M2.5 1.5h7l-1.8 2.75L9.5 7h-7"/></svg>';
  const flag = (ev) => (ev.important ? FLAG : "");

  /** 달력 칸 하루짜리 일정: 쉬는 날·중요 아니고 시간 있으면 "[15:00] 제목"(시간만 종일 일정과 같은 네모), 아니면 칸 너비 칩 (중요는 시간이 있어도 칠함) */
  function dayItem(ev) {
    const time = ev.time ? `<span class="ev-time">${esc(shortTime(ev))}</span>` : "";
    if (ev.time && !ev.holiday && !ev.important) {
      const cls = ["ev", "ev-timed"];
      if (ev.important) cls.push("important");
      if (ev.uncertain) cls.push("unsure");  // 시간 네모 테두리만 + 옅은 글씨
      return `<span ${evAttrs(ev, cls)}>${time}${flag(ev)}${esc(ev.title)}</span>`;
    }
    const cls = ["ev"];
    if (ev.important) cls.push("important");
    if (ev.holiday) cls.push("holiday");
    if (ev.uncertain) cls.push("unsure");
    return `<span ${evAttrs(ev, cls)}>${taskBox(ev)}${time}${flag(ev)}${esc(ev.title)}</span>`;
  }

  /**
   * 기간 일정 한 주 구간: 시작 칸에만 여러 칸 너비로 그리고 나머지 칸은 자리만 비움.
   * head/tail = 이 구간에서 실제로 시작/끝나는지 (아니면 다음 주로 이어짐)
   */
  function rangeBar(ev, n, head, tail) {
    const cls = ["ev", "bar"];
    if (ev.italic) cls.push("arrow");
    if (ev.important) cls.push("important");
    if (ev.holiday) cls.push("holiday");
    if (head) cls.push("head");
    if (tail) cls.push("tail");
    // 시작/끝 미정: 그 끝 쪽 첫 칸에서 서서히 들어오거나(fade-in) 사라짐(fade-out)
    const fadeIn = ev.open === "start" && head, fadeOut = ev.open === "end" && tail;
    if (fadeIn) cls.push("fade-in");
    if (fadeOut) cls.push("fade-out");
    const cut = (head ? 5 : 0) + (tail ? 5 : 0);
    const style = `width:calc(${n} * 100% + ${n - 1} * var(--cal-gap) - ${cut}px);--n:${n}`;
    let label = `<span class="bar-label">${head ? taskBox(ev) : ""}${esc(ev.title)}</span>`;
    if (ev.italic) {
      label = (head && !fadeIn ? '<i class="ah l"></i>' : "") + '<i class="ln"></i>' + label +
        '<i class="ln"></i>' + (tail && !fadeOut ? '<i class="ah r"></i>' : "");
    }
    return `<span ${evAttrs(ev, cls).replace('style="', `style="${style};`)}>${label}</span>`;
  }

  /** 한 주(7일)의 기간 일정 줄 배치: { 날짜: [lane별 html] } */
  function layoutWeek(week, idx) {
    const seen = new Set(), multi = [];
    for (const d of week) {
      for (const ev of (idx && idx.byDate[d]) || []) {
        if (ev.start !== ev.end && !seen.has(ev)) { seen.add(ev); multi.push(ev); }
      }
    }
    // 이 주 안에서 차지하는 칸 범위. 짧은 띠가 위 줄로 (같으면 먼저 시작하는 것)
    const segs = multi.map((ev) => {
      const s = Math.max(0, week.findIndex((d) => d >= ev.start));
      let e = week.length - 1;
      while (e > s && week[e] > ev.end) e--;
      return { ev, s, e };
    });
    // 주마다 따로: 그 주에서 제일 긴 띠가 바닥, 같은 태그(첫 태그 = 색)의 짧은 띠는 그 바로 위에 붙임.
    // 태그 묶음 순서 = 묶음 안 가장 긴 띠 길이, 묶음 안은 짧은 순. 태그 없는 건 각자 따로
    const len = (g) => g.e - g.s, grp = (g) => (g.ev.tags && g.ev.tags[0]) || null, gMax = {};
    for (const g of segs) { const t = grp(g); if (t !== null) gMax[t] = Math.max(gMax[t] ?? -1, len(g)); }
    const rank = (g) => (grp(g) === null ? len(g) : gMax[grp(g)]);
    segs.sort((a, b) => rank(a) - rank(b) || (grp(a) ?? "").localeCompare(grp(b) ?? "") ||
      len(a) - len(b) || a.s - b.s || (a.ev.start < b.ev.start ? -1 : 1));
    const used = [];  // used[lane][칸] = true
    const cells = week.map(() => []);
    for (const { ev, s, e } of segs) {
      let lane = used.findIndex((u) => !u.slice(s, e + 1).some(Boolean));
      if (lane < 0) { lane = used.length; used.push([]); }
      for (let k = s; k <= e; k++) used[lane][k] = true;
      cells[s][lane] = rangeBar(ev, e - s + 1, week[s] === ev.start, week[e] === ev.end);
      for (let k = s + 1; k <= e; k++) cells[k][lane] = '<span class="ev spacer"></span>';
    }
    // 한 주의 모든 칸을 같은 줄 수로 채움 (띠 영역 높이가 같아야 옆 칸과 줄이 맞음)
    const n = used.length;
    return cells.map((c) => Array.from({ length: n }, (_, l) => c[l] || '<span class="ev spacer"></span>'));
  }

  /**
   * 월 달력 HTML.
   * opts: { ym, days: {iso: {done,total}}, idx, today, selected }
   * 반환: { html, notes, done, total }
   */
  function renderMonth({ ym, days, idx, today, selected, decos }) {
    const [y, m] = ym.split("-").map(Number);
    let html = WD.map((w, k) => `<div class="wd${k === 0 ? " sun" : k === 6 ? " sat" : ""}">${w}</div>`).join("");
    const first = new Date(y, m - 1, 1);
    const cursor = new Date(y, m - 1, 1 - first.getDay());
    let notes = 0, done = 0, total = 0;
    let lanes = [];

    for (let k = 0; k < 42; k++) {
      const d = iso(cursor);
      if (k % 7 === 0) lanes = layoutWeek(Array.from({ length: 7 }, (_, i) => addDays(d, i)), idx);
      const info = days[d];
      const singles = ((idx && idx.byDate[d]) || []).filter((ev) => ev.start === ev.end);
      const cls = ["cal-day"];
      if (cursor.getMonth() !== m - 1) cls.push("other");
      if (cursor.getDay() === 0) cls.push("sun");
      if (cursor.getDay() === 6) cls.push("sat");
      if (idx && idx.off.has(d)) cls.push("off");
      if (d === today) cls.push("today");
      if (d === selected) cls.push("selected");
      let count = "";
      if (info) {
        notes++;
        done += info.done;
        total += info.total;
        const c = info.total === 0 ? "none" : info.done === info.total ? "done" : "";
        count = `<span class="count ${c}">${info.done}/${info.total}</span>`;
      }
      // 하루 일정은 위(남는 자리만, 넘치면 +N), 기간 띠는 칸 아래 고정 영역 (숨기지 않아 옆 칸과 줄이 항상 맞음)
      const lane = lanes[k % 7], deco = ((decos && decos[d]) || []).filter((x) => x.text);
      // 장식: 하루 일정 맨 뒤 (자리가 모자라면 장식부터 숨김 → fitCells)
      const decoHtml = deco.map((x) =>
        `<span class="deco" data-sp="${esc(x.sp)}" data-raw="${esc(x.raw)}">${esc(x.text)}</span>`).join("");
      const list = singles.length || lane.length || deco.length ? `<div class="evs"><div class="singles">${singles.map(dayItem).join("")}${decoHtml}</div>` +
        (lane.length ? `<div class="lanes">${lane.join("")}</div>` : "") + "</div>" : "";
      // 주 안에서 앞 칸이 위: 기간 띠가 오른쪽 칸들 위로 뻗어도 클릭이 띠로 감 (안 그러면 뒤 칸이 덮음)
      // 날짜 줄: 숫자 · (+N·✦) · 휴일 자리(남는 너비, 넘치면 …) · 할일 수
      const head = `<div class="head"><span class="num">${cursor.getDate()}</span>${slotHtml(d, idx)}${count}</div>`;
      html += `<div class="${cls.join(" ")}" data-date="${d}" style="z-index:${7 - (k % 7)}">${head}${list}</div>`;
      cursor.setDate(cursor.getDate() + 1);
    }
    return { html, notes, done, total };
  }

  /**
   * 칸에 안 들어가는 하루 일정은 숨기고 "+N" 표시 (그린 뒤·크기 바뀐 뒤 호출). 기간 띠는 건드리지 않음.
   * 장식은 맨 뒤라 먼저 숨고, 숨은 장식은 숫자 없이 옅은 ✦ 하나 (일정 +N 과 무게를 다르게)
   */
  function fitCells(grid) {
    for (const cell of grid.querySelectorAll(".cal-day")) {
      cell.querySelectorAll(".ev-more, .deco-more").forEach((el) => el.remove());
      const box = cell.querySelector(".singles");
      if (!box) continue;
      const items = [...box.children];
      for (const el of items) el.hidden = false;
      if (!items.length || box.scrollHeight <= box.clientHeight + 1) continue;
      for (let k = items.length - 1; k >= 0; k--) {
        if (box.scrollHeight <= box.clientHeight + 1) break;
        items[k].hidden = true;
      }
      const evs = items.filter((el) => el.hidden && !el.classList.contains("deco"));
      const decos = items.filter((el) => el.hidden && el.classList.contains("deco"));
      let at = cell.querySelector(".num");   // 표시는 늘 날짜 숫자 오른쪽 (일정 칸 자리를 안 먹음)
      if (evs.length) {
        const more = document.createElement("span");
        more.className = "ev ev-more";
        more.textContent = String(evs.length);
        more.dataset.ks = evs.map((el) => el.dataset.k).filter(Boolean).join(",");   // 마우스 = 숨은 일정만 카드로 (app.js tipRender)
        at.after(more);
        at = more;
      }
      if (decos.length) {
        const mark = document.createElement("span");
        mark.className = "deco-more";
        mark.textContent = "✦";
        mark.title = decos.map((el) => el.textContent).join("\n");
        at.after(mark);
        if (at.classList.contains("ev-more")) mark.style.left = at.offsetLeft + at.offsetWidth + 3 + "px";   // +N 배지 너비만큼
      }
    }
  }

  /** 노트 위에 보여줄 그날 일정 줄 HTML (없으면 "") */
  function dayEvents(d, idx) {
    // 그날 노트에 이미 있는 할일은 일정 줄에 또 띄우지 않음
    // 근태도 칩으로 (공휴일은 받은 것이라 뺌)
    const offs = ((idx && idx.slot[d]) || []).filter((x) => !x.pub);
    const evs = [...offs, ...((idx && idx.byDate[d]) || [])].filter((ev) => !(ev.task && ev.task.note === d));
    return evs.map(chip).join("").replace(/ data-tip="/g, ' title="');   // 할일 창엔 미리보기 카드가 없어 기본 툴팁 유지
  }

  // 필터(스페이스·태그·멤버) 자리: 위 ↔ 오른쪽 사이드바. 이 PC 에만 기억
  (function initLayout() {
    const view = document.getElementById("view-cal");
    const btn = document.getElementById("cal-layout");
    if (!view || !btn) return;
    const apply = (side) => {
      view.classList.toggle("cal-side", side);
      btn.title = side ? "필터를 달력 위에 보기" : "필터를 오른쪽에 세로로 보기";
    };
    let side = false;
    try { side = localStorage.getItem("dogani.calSide") === "1"; } catch (e) {}
    apply(side);
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      side = !side;
      apply(side);
      try { localStorage.setItem("dogani.calSide", side ? "1" : "0"); } catch (e2) {}
    });
  })();

  // 사이드바 그룹·멤버·태그 묶음: 제목 클릭 = 접기, 제목 끌기 = 순서 바꾸기 (스페이스는 늘 맨 위). 이 PC 에만 기억
  (function initSections() {
    const box = document.querySelector("#view-cal .cal-filters");
    if (!box) return;
    const KEY = "dogani.calSecs";
    const secs = () => Array.from(box.querySelectorAll(":scope > .cal-sec"));
    const save = () => {
      const v = { order: secs().map((s) => s.dataset.sec), folded: secs().filter((s) => s.classList.contains("folded")).map((s) => s.dataset.sec) };
      try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) {}
    };
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
    if (saved && Array.isArray(saved.order)) {
      // 기억한 순서대로 맨 끝으로 옮김 → 모르는 묶음(새로 생긴 것)은 앞쪽에 남음
      for (const k of saved.order) { const s = box.querySelector(`:scope > .cal-sec[data-sec="${k}"]`); if (s) box.appendChild(s); }
    }
    if (saved && Array.isArray(saved.folded)) for (const s of secs()) s.classList.toggle("folded", saved.folded.includes(s.dataset.sec));
    // 제목 누르고 움직이면 그 묶음을 마우스 따라 위아래로 바로 옮김 (HTML5 끌기는 크롬 커서·그림이 떠서 안 씀). 안 움직이고 떼면 접기
    box.addEventListener("mousedown", (e) => {
      const h = e.button === 0 && e.target.closest && e.target.closest(".cal-sec-h");
      if (!h) return;
      e.preventDefault();
      const sec = h.parentElement, y0 = e.clientY;
      let moved = false;
      const move = (ev) => {
        if (!moved && Math.abs(ev.clientY - y0) < 4) return;
        if (!moved) { moved = true; sec.classList.add("moving"); document.body.classList.add("cal-grabbing"); }
        // 마우스가 다른 묶음의 가운데를 넘으면 자리를 바꿈
        for (const o of secs()) {
          if (o === sec) continue;
          const r = o.getBoundingClientRect(), mid = r.top + r.height / 2;
          const after = !!(sec.compareDocumentPosition(o) & Node.DOCUMENT_POSITION_FOLLOWING);
          if (after && ev.clientY > mid) { box.insertBefore(sec, o.nextSibling); break; }
          if (!after && ev.clientY < mid) { box.insertBefore(sec, o); break; }
        }
      };
      const up = () => {
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
        sec.classList.remove("moving");
        document.body.classList.remove("cal-grabbing");
        if (!moved) sec.classList.toggle("folded");
        save();
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    });
  })();

  window.doganiCalendar = {
    WD, pad, iso, parseISO, addDays, addMonths, tagHue, eventCard, cleanName, nameKey, uniqNames,
    index, isOff, nextWorkday, slotLabels, renderMonth, fitCells, dayEvents,
  };
})();
