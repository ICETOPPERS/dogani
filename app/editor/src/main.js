/*
 * dogani 라이브 프리뷰 편집기 (CodeMirror 6 — 옵시디언 편집기와 같은 기반)
 *
 * 옵시디언 라이브 프리뷰 흉내:
 *  - 노트 전체가 편집기 하나. 커서/선택이 닿은 서식만 원문 기호(**, ==, [[ ]], # ...)가 보이고 나머지는 렌더링
 *  - 체크박스·글머리표·들여쓰기 가이드·구분선은 위젯, 표는 커서가 밖에 있으면 표로
 *  - 편집기에 포커스가 없으면(읽기 전용 포함) 전부 렌더링된 상태
 *
 * window.DoganiEditor.create(parent, opts) 로 생성 (app.js 에서 사용). 빌드: app/editor/build.bat
 */
import { EditorState, StateField, StateEffect, Compartment, Transaction } from "@codemirror/state";
import { EditorView, Decoration, ViewPlugin, WidgetType, keymap, drawSelection } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentMore, indentLess, insertTab } from "@codemirror/commands";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { syntaxTree, ensureSyntaxTree, indentUnit } from "@codemirror/language";

// ------------------------------------------------------------ focus (상태에 넣어야 표 위젯 StateField 도 반응)
const setFocus = StateEffect.define();
const focusField = StateField.define({
  create: () => false,
  update: (v, tr) => {
    for (const e of tr.effects) if (e.is(setFocus)) v = e.value;
    return v;
  },
});
const focusSync = EditorView.focusChangeEffect.of((_, focusing) => setFocus.of(focusing));

/** 선택(커서)이 [from, to] 에 닿는지. 포커스가 없으면 항상 false → 전부 렌더링 */
function toucher(state) {
  const focused = state.field(focusField, false);
  const ranges = state.selection.ranges;
  return (from, to) => focused && ranges.some((r) => r.from <= to && r.to >= from);
}

// ------------------------------------------------------------ widgets
class Checkbox extends WidgetType {
  constructor(checked) { super(); this.checked = checked; }
  eq(o) { return o.checked === this.checked; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-task-wrap";
    s.innerHTML = `<span class="task-checkbox${this.checked ? " checked" : ""}"></span>`;
    return s;
  }
  ignoreEvent() { return false; }
}
class Bullet extends WidgetType {
  eq() { return true; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-bullet-wrap";
    s.innerHTML = '<span class="lp-bullet"></span>';
    return s;
  }
}
/** 목록 앞 들여쓰기(탭/스페이스) → 단계마다 고정 폭 + 가이드선 (옵시디언처럼) */
class Indent extends WidgetType {
  constructor(lvl) { super(); this.lvl = lvl; }
  eq(o) { return o.lvl === this.lvl; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-indent";
    s.style.width = `calc(${this.lvl} * var(--ind))`;
    for (let k = 0; k < this.lvl; k++) s.insertAdjacentHTML("beforeend", `<span class="cm-guide" style="--k:${k}"></span>`);
    return s;
  }
}
class Rule extends WidgetType {
  eq() { return true; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-hr";
    return s;
  }
}
class Label extends WidgetType {
  constructor(text, cls) { super(); this.text = text; this.cls = cls; }
  eq(o) { return o.text === this.text && o.cls === this.cls; }
  toDOM() {
    const s = document.createElement("span");
    s.className = this.cls;
    s.textContent = this.text;
    return s;
  }
}
class Table extends WidgetType {
  constructor(text, html, from) { super(); this.text = text; this.html = html; this.from = from; }
  eq(o) { return o.text === this.text; }
  toDOM(view) {
    const d = document.createElement("div");
    d.className = "cm-table-widget";
    d.innerHTML = this.html(this.text.split("\n"));
    d.addEventListener("mousedown", (e) => {
      if (!view.state.facet(EditorView.editable) || e.target.closest("a")) return;
      e.preventDefault();
      view.dispatch({ selection: { anchor: this.from } });
      view.focus();
    });
    return d;
  }
  ignoreEvent() { return true; }
}

const hide = Decoration.replace({});
const line = (cls, attrs) => Decoration.line({ class: cls, attributes: attrs });
const mark = (cls, attrs) => Decoration.mark({ class: cls, attributes: attrs });

// ------------------------------------------------------------ frontmatter
function frontmatterEnd(doc) {
  if (doc.lines < 2 || doc.line(1).text !== "---") return -1;
  for (let n = 2; n <= doc.lines; n++) if (doc.line(n).text === "---") return doc.line(n).to;
  return -1;
}

// ------------------------------------------------------------ inline (lezer 가 모르는 옵시디언 문법)
const OBS_INLINE = [
  // %%주석%%: 커서가 닿을 때만 흐리게 보임
  { re: /%%.*?%%/g, run: (d, m, f, t, touched) => d.push((touched(f, t) ? mark("cm-comment") : hide).range(f, t)) },
  // ==하이라이트==
  { re: /==(?=\S)(.+?)==/g, run: (d, m, f, t, touched) => {
    d.push(mark("cm-highlight").range(f, t));
    if (!touched(f, t)) d.push(hide.range(f, f + 2), hide.range(t - 2, t));
  } },
  // [[위키링크|별칭]] / ![[임베드]]
  { re: /(!?)\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]/g, run: (d, m, f, t, touched) => {
    const embed = !!m[1];
    const cls = embed ? "cm-embed" : "cm-ilink";
    if (touched(f, t)) {
      d.push(mark(cls).range(f, t));
      return;
    }
    const open = f + m[1].length + 2;
    const textFrom = m[3] ? open + m[2].length + 1 : open;
    d.push(hide.range(f, textFrom), mark(cls, { title: m[2] }).range(textFrom, t - 2), hide.range(t - 2, t));
  } },
  // #태그 (숫자만은 태그 아님)
  { re: /(^|\s)(#(?:[\p{L}\p{N}_\-/])*[\p{L}_\-/](?:[\p{L}\p{N}_\-/])*)/gu, run: (d, m, f, t) => {
    d.push(mark("cm-tag").range(f + m[1].length, t));
  } },
];

const HEADINGS = { ATXHeading1: 1, ATXHeading2: 2, ATXHeading3: 3, ATXHeading4: 4, ATXHeading5: 5, ATXHeading6: 6 };
const INLINE_CLS = { Emphasis: "cm-em", StrongEmphasis: "cm-strong", Strikethrough: "cm-strike", InlineCode: "cm-inline-code" };
const INLINE_MARKS = { Emphasis: "EmphasisMark", StrongEmphasis: "EmphasisMark", Strikethrough: "StrikethroughMark", InlineCode: "CodeMark" };
const CALLOUT = /^\s*>\s*\[!([\w-]+)\][+-]?[ \t]*/;

function build(view) {
  const { state } = view;
  const doc = state.doc;
  const touched = toucher(state);
  const lineTouched = (pos) => { const l = doc.lineAt(pos); return touched(l.from, l.to); };
  const decos = [];
  const atomic = [];
  const skip = [];          // 정규식 인라인을 적용하지 않을 범위 (코드, 표, URL ...)
  const fm = frontmatterEnd(doc);

  if (fm > 0) {
    skip.push([0, fm]);
    for (let p = 0; p <= fm;) { const l = doc.lineAt(p); decos.push(line("cm-fm").range(l.from)); p = l.to + 1; }
  }

  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({
      from, to,
      enter(node) {
        const name = node.name;
        if (name === "Document") return;
        if (node.to <= fm) return false;

        if (HEADINGS[name]) {
          const l = doc.lineAt(node.from);
          decos.push(line(`cm-h cm-h${HEADINGS[name]}`).range(l.from));
          if (!touched(l.from, l.to)) {
            const c = node.node.cursor();
            let first = true;
            if (c.firstChild()) do {
              if (c.name !== "HeaderMark") continue;
              // 앞쪽 "## " 은 공백까지, 뒤쪽 닫는 " ##" 은 앞 공백까지
              if (first) decos.push(hide.range(c.from, Math.min(c.to + 1, l.to)));
              else decos.push(hide.range(Math.max(l.from, c.from - 1), c.to));
              first = false;
            } while (c.nextSibling());
          }
          return;
        }
        if (name === "SetextHeading1" || name === "SetextHeading2") {
          const l = doc.lineAt(node.from);
          decos.push(line(`cm-h cm-h${name.endsWith("1") ? 1 : 2}`).range(l.from));
          return;
        }

        if (INLINE_CLS[name]) {
          decos.push(mark(INLINE_CLS[name]).range(node.from, node.to));
          if (name === "InlineCode") skip.push([node.from, node.to]);
          if (!touched(node.from, node.to)) {
            const c = node.node.cursor();
            if (c.firstChild()) do { if (c.name === INLINE_MARKS[name]) decos.push(hide.range(c.from, c.to)); } while (c.nextSibling());
          }
          return;
        }

        if (name === "Link" || name === "Image") {
          const url = node.node.getChild("URL");
          if (!url) return;      // [x] 같은 참조 링크 모양은 그대로
          const href = doc.sliceString(url.from, url.to);
          skip.push([node.from, node.to]);
          const marks = node.node.getChildren("LinkMark");
          if (marks.length < 2) return false;
          const textFrom = marks[0].to, textTo = marks[1].from;
          if (touched(node.from, node.to)) {
            decos.push(mark("cm-link").range(textFrom, Math.max(textFrom, textTo)));
          } else if (name === "Image") {
            decos.push(Decoration.replace({ widget: new Label(doc.sliceString(textFrom, textTo) || "이미지", "cm-embed") }).range(node.from, node.to));
          } else if (textTo > textFrom) {
            decos.push(hide.range(node.from, textFrom),
              mark("cm-link cm-link-rendered", { "data-href": href, title: href }).range(textFrom, textTo),
              hide.range(textTo, node.to));
          }
          return false;
        }
        if (name === "URL") {
          // 맨 URL (자동 링크)
          skip.push([node.from, node.to]);
          decos.push(mark("cm-link cm-url", { "data-href": doc.sliceString(node.from, node.to) }).range(node.from, node.to));
          return;
        }

        if (name === "HorizontalRule") {
          if (!lineTouched(node.from)) decos.push(Decoration.replace({ widget: new Rule() }).range(node.from, node.to));
          return;
        }

        if (name === "Blockquote") {
          if (node.node.parent && node.node.parent.name !== "Document" && hasAncestor(node.node, "Blockquote")) return;
          const first = doc.lineAt(node.from);
          const c = first.text.match(CALLOUT);
          const type = c && c[1].toLowerCase();
          for (let p = node.from; p <= node.to;) {
            const l = doc.lineAt(p);
            const depth = (l.text.match(/^(\s*>\s?)+/) || [""])[0].split(">").length - 1;
            const cls = type ? "cm-callout" + (l.number === first.number ? " cm-callout-title" : "") : "cm-quote";
            decos.push(line(cls, { style: `--q:${Math.max(depth, 1)}`, ...(type ? { "data-callout": type } : {}) }).range(l.from));
            p = l.to + 1;
          }
          if (type && !touched(first.from, first.to)) {
            const start = first.from + c[0].length;
            if (start >= first.to) {
              decos.push(Decoration.replace({ widget: new Label(type.charAt(0).toUpperCase() + type.slice(1), "") }).range(first.from, first.to));
            } else {
              decos.push(hide.range(first.from, start));
            }
          }
          return;
        }
        if (name === "QuoteMark") {
          if (!lineTouched(node.from)) {
            const next = doc.sliceString(node.to, node.to + 1);
            decos.push(hide.range(node.from, next === " " ? node.to + 1 : node.to));
          }
          return;
        }

        if (name === "ListItem") {
          const l = doc.lineAt(node.from);
          const lvl = listDepth(node.node);
          const ws = l.text.match(/^[ \t]*/)[0].length;
          const quote = /^\s*>/.test(l.text);
          if (!quote) {
            if (ws) {
              decos.push(Decoration.replace({ widget: new Indent(lvl) }).range(l.from, l.from + ws));
              atomic.push([l.from, l.from + ws]);
            }
            decos.push(line("cm-li", { style: `--lvl:${lvl}` }).range(l.from));
          }
          const markNode = node.node.getChild("ListMark");
          const task = node.node.getChild("Task");
          const tm = task && task.getChild("TaskMarker");
          if (markNode && tm) {
            const checked = doc.sliceString(tm.from + 1, tm.to - 1) !== " ";
            const end = doc.sliceString(tm.to, tm.to + 1) === " " ? tm.to + 1 : tm.to;
            decos.push(line("cm-task" + (checked ? " is-checked" : "")).range(l.from));
            if (!touched(markNode.from, end)) {
              decos.push(Decoration.replace({ widget: new Checkbox(checked) }).range(markNode.from, end));
            }
            if (checked && end < l.to) decos.push(mark("cm-checked-text").range(end, l.to));
          } else if (markNode) {
            const ordered = /\d/.test(doc.sliceString(markNode.from, markNode.to));
            const end = doc.sliceString(markNode.to, markNode.to + 1) === " " ? markNode.to + 1 : markNode.to;
            if (ordered) decos.push(mark("cm-num").range(markNode.from, markNode.to));
            else if (!touched(markNode.from, end)) decos.push(Decoration.replace({ widget: new Bullet() }).range(markNode.from, end));
            if (!quote) decos.push(line(ordered ? "cm-li-ol" : "cm-li-ul").range(l.from));
          }
          return;
        }

        if (name === "FencedCode" || name === "CodeBlock") {
          skip.push([node.from, node.to]);
          const active = touched(node.from, node.to);
          const firstL = doc.lineAt(node.from).number, lastL = doc.lineAt(node.to).number;
          for (let n = firstL; n <= lastL; n++) {
            const l = doc.line(n);
            let cls = "cm-codeblock";
            if (n === firstL) cls += " cm-codeblock-begin";
            if (n === lastL) cls += " cm-codeblock-end";
            const fence = name === "FencedCode" && (n === firstL || (n === lastL && /^\s*(```|~~~)/.test(l.text)));
            if (fence) {
              cls += " cm-fence";
              if (!active && l.to > l.from) {
                const info = n === firstL ? l.text.replace(/^\s*(`{3,}|~{3,})\s*/, "") : "";
                decos.push(Decoration.replace(info ? { widget: new Label(info, "cm-code-info") } : {}).range(l.from, l.to));
              }
            }
            decos.push(line(cls).range(l.from));
          }
          return false;
        }

        if (name === "Table") {
          skip.push([node.from, node.to]);
          for (let p = node.from; p <= node.to;) { const l = doc.lineAt(p); decos.push(line("cm-table-raw").range(l.from)); p = l.to + 1; }
          return false;
        }
        if (name === "HTMLBlock" || name === "CommentBlock") {
          skip.push([node.from, node.to]);
          return false;
        }
      },
    });

    // 옵시디언 전용 인라인 문법: 줄 단위 정규식
    for (let p = from; p <= to;) {
      const l = doc.lineAt(p);
      p = l.to + 1;
      if (!l.text || inRanges(skip, l.from, l.to, true)) continue;
      for (const rule of OBS_INLINE) {
        rule.re.lastIndex = 0;
        let m;
        while ((m = rule.re.exec(l.text))) {
          const f = l.from + m.index, t = f + m[0].length;
          if (!m[0].length) { rule.re.lastIndex++; continue; }
          if (inRanges(skip, f, t, false)) continue;
          rule.run(decos, m, f, t, touched);
          if (rule.re === OBS_INLINE[0].re) skip.push([f, t]);
        }
      }
    }
  }

  return { decorations: Decoration.set(decos, true), atomic: Decoration.set(atomic.map(([f, t]) => hide.range(f, t)), true) };
}

function inRanges(ranges, from, to, whole) {
  return ranges.some(([f, t]) => (whole ? f <= from && to <= t : f < to && from < t));
}
function hasAncestor(node, name) {
  for (let p = node.parent; p; p = p.parent) if (p.name === name) return true;
  return false;
}
function listDepth(node) {
  let d = 0;
  for (let p = node.parent; p; p = p.parent) if (p.name === "ListItem") d++;
  return d;
}

const livePreview = ViewPlugin.fromClass(class {
  constructor(view) { Object.assign(this, build(view)); }
  update(u) {
    if (u.docChanged || u.viewportChanged || u.selectionSet || u.focusChanged ||
        u.transactions.some((tr) => tr.effects.some((e) => e.is(setFocus))) ||
        syntaxTree(u.startState) !== syntaxTree(u.state)) {
      Object.assign(this, build(u.view));
    }
  }
}, {
  decorations: (v) => v.decorations,
  provide: (p) => EditorView.atomicRanges.of((view) => (view.plugin(p) ? view.plugin(p).atomic : Decoration.none)),
});

// ------------------------------------------------------------ table (블록 위젯은 StateField 에서만 가능)
function tableField(html) {
  const make = (state) => {
    const touched = toucher(state);
    const tree = ensureSyntaxTree(state, state.doc.length, 200) || syntaxTree(state);
    const decos = [];
    tree.iterate({
      enter(node) {
        if (node.name !== "Table") return;
        const from = state.doc.lineAt(node.from).from, to = state.doc.lineAt(node.to).to;
        if (!touched(from, to)) {
          decos.push(Decoration.replace({ widget: new Table(state.doc.sliceString(from, to), html, from), block: true }).range(from, to));
        }
        return false;
      },
    });
    return Decoration.set(decos, true);
  };
  return StateField.define({
    create: make,
    update: (v, tr) => (tr.docChanged || tr.selection || tr.effects.some((e) => e.is(setFocus)) ? make(tr.state) : v),
    provide: (f) => EditorView.decorations.from(f),
  });
}

// ------------------------------------------------------------ commands
const isListLine = (state) => /^\s*(?:>\s*)*([-*+]|\d+[.)])(\s|$)/.test(state.doc.lineAt(state.selection.main.head).text);
const tab = (view) => (isListLine(view.state) || !view.state.selection.main.empty ? indentMore(view) : insertTab(view));

function toggleTaskAt(view, pos) {
  const l = view.state.doc.lineAt(pos);
  const m = l.text.match(/^((?:[ \t]{0,3}>[ \t]?)*[ \t]*(?:[-*+]|\d+[.)])[ \t]+\[)(.)\]/);
  if (!m) return false;
  const at = l.from + m[1].length;
  view.dispatch({ changes: { from: at, to: at + 1, insert: m[2] === " " ? "x" : " " }, userEvent: "input.toggle" });
  return true;
}

// ------------------------------------------------------------ 줄 손잡이 (노션 블록처럼 보이게: 줄 = 블록, 들여쓴 아래 줄 = 자식)
const indentOf = (t) => { let w = 0; for (const c of t) { if (c === " ") w++; else if (c === "\t") w += 4; else break; } return w; };

/** 줄 n(1부터)과 그 아래 더 들여쓴 줄들 → [첫 줄, 끝 줄] (빈 줄에서 끊음) */
function blockLines(doc, n) {
  const base = indentOf(doc.line(n).text);
  let e = n;
  while (e < doc.lines) {
    const t = doc.line(e + 1).text;
    if (!t.trim() || indentOf(t) <= base) break;
    e++;
  }
  return [n, e];
}

/** 줄 a~b 를 줄 to 앞으로 옮김 (to = 1..lines+1, lines+1 = 맨 끝). 제자리면 false */
function moveLines(view, a, b, to) {
  const doc = view.state.doc;
  if (to >= a && to <= b + 1) return false;
  const from = doc.line(a).from, end = doc.line(b).to, text = doc.sliceString(from, end);
  const changes = [b < doc.lines ? { from, to: end + 1 } : { from: from - 1, to: end }];
  changes.push(to <= doc.lines ? { from: doc.line(to).from, insert: text + "\n" } : { from: doc.length, insert: "\n" + text });
  view.dispatch({ changes, userEvent: "move.line" });
  return true;
}

const DOTS = '<svg viewBox="0 0 10 16" aria-hidden="true">' +
  [3, 8, 13].map((y) => `<circle cx="3" cy="${y}" r="1.4"/><circle cx="7" cy="${y}" r="1.4"/>`).join("") + "</svg>";

/**
 * 마우스를 올린 줄(블록)을 옅게 강조하고 왼쪽에 ⋮⋮ 손잡이. 손잡이를 끌어 놓으면 줄 옮기기(하위 줄 같이).
 * 다른 창(달력)에 놓을 때 데이터 = opts.dragData(줄 번호 0부터, 줄 글자) 가 주면 그것, 아니면 줄 글자
 */
function blockHandle(call) {
  return ViewPlugin.fromClass(class {
    constructor(view) {
      this.view = view;
      this.line = 0;
      this.drag = null;
      const mk = (cls) => { const el = document.createElement("div"); el.className = cls; el.hidden = true; view.scrollDOM.appendChild(el); return el; };
      this.hl = mk("cm-block-hl");
      this.mark = mk("cm-drop-mark");
      this.h = mk("cm-block-handle");
      this.h.draggable = true;
      this.h.title = "끌어서 줄 옮기기 (할일은 달력 칸에 놓으면 날짜가 붙음)";
      this.h.innerHTML = DOTS;
      this.on = [
        [view.scrollDOM, "mousemove", (e) => this.hover(e)],
        [view.scrollDOM, "mouseleave", () => { if (!this.drag) this.hide(); }],
        [view.dom, "keydown", () => { if (!this.drag) this.hide(); }],
        [view.dom, "dragover", (e) => this.over(e), true],
        [view.dom, "drop", (e) => this.drop(e), true],
        [this.h, "dragstart", (e) => this.start(e)],
        [this.h, "dragend", () => this.end()],
      ];
      for (const [el, t, f, cap] of this.on) el.addEventListener(t, f, !!cap);
    }
    lineAtY(y) {
      const v = this.view, b = v.lineBlockAtHeight(y - v.documentTop);
      return v.state.doc.lineAt(b.from);
    }
    /** 문서 위치 → scrollDOM 안 좌표 */
    rel(top, left) {
      const v = this.view, sr = v.scrollDOM.getBoundingClientRect();
      return [top + v.documentTop - sr.top + v.scrollDOM.scrollTop, left - sr.left + v.scrollDOM.scrollLeft];
    }
    hover(e) {
      if (this.drag || !this.view.state.facet(EditorView.editable) || this.h.contains(e.target)) return;
      const ln = this.lineAtY(e.clientY);
      if (!ln.text.trim()) { this.hide(); return; }
      if (ln.number !== this.line || this.h.hidden) this.show(ln.number);
    }
    show(n) {
      const v = this.view, doc = v.state.doc, ln = doc.line(n), [, b] = blockLines(doc, n);
      const first = v.lineBlockAt(ln.from), last = v.lineBlockAt(doc.line(b).from);
      // 손잡이는 들여쓰기(하위 할일)·헤더·가로줄 상관없이 같은 x (본문 왼쪽 여백 안), 강조 블록이 손잡이까지 감쌈
      const dom = v.domAtPos(ln.from).node, lineEl = dom && (dom.nodeType === 1 ? dom : dom.parentElement).closest(".cm-line");
      const cr = v.contentDOM.getBoundingClientRect(), padL = parseFloat(getComputedStyle(v.contentDOM).paddingLeft) || 0;
      const [top, left] = this.rel(first.top, cr.left + padL);
      // 첫 줄 높이: 헤더·가로줄은 글자 좌표가 숨은 기호 자리라 어긋나서 줄 요소 기준
      const c = /^\s*(#{1,6}\s|[-*_](\s*[-*_]){2,}\s*$)/.test(ln.text) ? null : v.coordsAtPos(ln.from + ln.text.match(/^\s*/)[0].length, 1);
      let lh = c ? c.bottom - c.top : lineEl ? parseFloat(getComputedStyle(lineEl).lineHeight) || lineEl.getBoundingClientRect().height : v.defaultLineHeight;
      lh = Math.min(lh, first.height);
      this.line = n;
      Object.assign(this.h.style, { top: top + (lh - 22) / 2 + "px", left: Math.max(0, left - 18) + "px" });
      Object.assign(this.hl.style, { top: top + "px", left: Math.max(0, left - 20) + "px", height: last.bottom - first.top + "px" });
      this.h.hidden = this.hl.hidden = false;
    }
    hide() { this.h.hidden = this.hl.hidden = this.mark.hidden = true; }
    start(e) {
      const v = this.view, doc = v.state.doc;
      if (!this.line || this.line > doc.lines) { e.preventDefault(); return; }
      const [a, b] = blockLines(doc, this.line);
      this.drag = { a, b, to: 0, moved: false };
      call("onDragStart");
      const text = doc.sliceString(doc.line(a).from, doc.line(b).to);
      e.dataTransfer.effectAllowed = "copyMove";
      e.dataTransfer.setData("text/plain", call("dragData", a - 1, doc.line(a).text) || text);
      const dom = v.domAtPos(doc.line(a).from + 1).node;
      const lineEl = dom && (dom.nodeType === 1 ? dom : dom.parentElement).closest(".cm-line");
      if (lineEl) e.dataTransfer.setDragImage(lineEl, 0, 8);
    }
    over(e) {
      if (!this.drag) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = "move";
      const v = this.view, doc = v.state.doc, ln = this.lineAtY(e.clientY), blk = v.lineBlockAt(ln.from);
      const below = e.clientY - v.documentTop > blk.top + blk.height / 2;
      const to = below ? ln.number + 1 : ln.number;
      this.drag.to = to;
      const ok = !(to >= this.drag.a && to <= this.drag.b + 1);
      const y = to <= doc.lines ? v.lineBlockAt(doc.line(to).from).top : v.lineBlockAt(doc.line(doc.lines).from).bottom;
      const [top, left] = this.rel(y, v.contentDOM.getBoundingClientRect().left);
      Object.assign(this.mark.style, { top: top - 1 + "px", left: left + 12 + "px" });
      this.mark.hidden = !ok;
    }
    drop(e) {
      if (!this.drag) return;
      e.preventDefault();
      e.stopPropagation();
      const d = this.drag;
      if (d.to) d.moved = moveLines(this.view, d.a, d.b, d.to);
      this.mark.hidden = true;
    }
    end() {
      const moved = !!(this.drag && this.drag.moved);
      this.drag = null;
      this.hide();
      call("onDragEnd", moved);
    }
    update(u) { if ((u.docChanged || u.geometryChanged) && !this.drag) this.hide(); }
    destroy() {
      for (const [el, t, f, cap] of this.on) el.removeEventListener(t, f, !!cap);
      this.h.remove(); this.hl.remove(); this.mark.remove();
    }
  });
}

/** 탭/스페이스 들여쓰기 중 노트가 이미 쓰고 있는 것 */
function detectIndent(text) {
  if (/^\t+\S/m.test(text)) return "\t";
  if (/^ {4}(?:[-*+]|\d+[.)])\s/m.test(text)) return "    ";
  if (/^ {2}(?:[-*+]|\d+[.)])\s/m.test(text)) return "  ";
  return "\t";
}

// ------------------------------------------------------------ public
function create(parent, opts) {
  const ro = new Compartment();
  const ind = new Compartment();
  const readOnlyExt = (r) => [EditorState.readOnly.of(r), EditorView.editable.of(!r)];
  const call = (name, ...a) => opts[name] && opts[name](...a);
  const table = tableField(opts.tableHtml || ((l) => l.join("<br>")));
  const handle = blockHandle(call);

  const extensions = (text, r) => [
    focusField, focusSync,
    history(),
    drawSelection(),
    EditorView.lineWrapping,
    markdown({ base: markdownLanguage }),
    ind.of(indentUnit.of(detectIndent(text))),
    EditorState.tabSize.of(4),
    keymap.of([
      { key: "Mod-s", run: () => { call("onSave"); return true; } },
      { key: "Escape", run: (v) => { v.contentDOM.blur(); return true; } },
      { key: "Tab", run: tab, shift: indentLess },
      ...historyKeymap,
      ...defaultKeymap,
    ]),
    livePreview,
    table,
    handle,
    ro.of(readOnlyExt(r)),
    EditorView.updateListener.of((u) => {
      if (u.docChanged) call("onChange", u.state.doc.toString(), u.transactions.some((tr) => tr.isUserEvent("input.toggle")));
      if (u.focusChanged) call(u.view.hasFocus ? "onFocus" : "onBlur");
    }),
    EditorView.domEventHandlers({
      mousedown(e, view) {
        const box = e.target.closest(".cm-task-wrap");
        if (box) {
          e.preventDefault();
          if (view.state.facet(EditorView.editable)) toggleTaskAt(view, view.posAtDOM(box));
          return true;
        }
        const link = e.target.closest("[data-href]");
        if (link && e.button === 0 && (link.classList.contains("cm-link-rendered") || e.ctrlKey || !view.state.facet(EditorView.editable))) {
          e.preventDefault();
          call("openUrl", link.dataset.href);
          return true;
        }
        return false;
      },
    }),
  ];

  let readOnly = !!opts.readOnly;
  const view = new EditorView({ parent, state: EditorState.create({ doc: opts.doc || "", extensions: extensions(opts.doc || "", readOnly) }) });

  return {
    view,
    getDoc: () => view.state.doc.toString(),
    /** 문서 통째로 교체 (날짜 이동·외부 변경). 실행 취소 기록도 새로 */
    setDoc(text, keepScroll) {
      const top = view.scrollDOM.scrollTop;
      view.setState(EditorState.create({ doc: text, extensions: extensions(text, readOnly) }));
      view.scrollDOM.scrollTop = keepScroll ? top : 0;
    },
    setReadOnly(r) {
      readOnly = !!r;
      view.dispatch({ effects: ro.reconfigure(readOnlyExt(readOnly)) });
    },
    focus(pos) {
      const at = pos === undefined ? view.state.doc.length : Math.min(pos, view.state.doc.length);
      view.dispatch({ selection: { anchor: at }, scrollIntoView: true });
      view.focus();
    },
    blur() { view.contentDOM.blur(); },
    hasFocus: () => view.hasFocus,
  };
}

window.DoganiEditor = { create };
