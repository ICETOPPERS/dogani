// Python↔JS 공용 정답(fixtures/shared.json) 시험. app.js·calendar.js 에서 원본 코드를 잘라 써서 복사본이 낡을 일이 없음.
// 같은 정답 파일을 test_parse.py 가 Python 쪽에서 읽음. 실행: node --test tests/
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const read = (...p) => readFileSync(join(here, ...p), "utf8");
const app = read("..", "app", "ui", "app.js");
const cal = read("..", "app", "ui", "calendar.js");
const shared = JSON.parse(read("fixtures", "shared.json"));

/** src 에서 from 부터 to 직전까지 (못 찾으면 시험을 고치라고 알림) */
function cut(src, from, to, what) {
  const a = src.indexOf(from), b = src.indexOf(to, a + 1);
  assert.ok(a > 0 && b > a, `${what} 를 찾지 못함 (코드 위치·모양이 바뀌었으면 이 시험을 고칠 것)`);
  return src.slice(a, b);
}
/** 한 줄짜리 선언 `const NAME = ...;` */
const line = (src, start) => cut(src, start, "\n", start);

const sched = new Function(
  line(app, "const cleanName = ") + "\n" +
  cut(app, "const LIST_RE = ", "// ---- 할일(노트 체크박스 줄) 날짜", "schedLine·evToObj") + "\n" +
  line(app, "const TASK_BOX_RE = ") + "\n" + line(app, "const TASK_DATE_RE = ") + "\n" +
  "return { schedLine, evToObj, LIST_RE, WHO_RE, TASK_BOX_RE, TASK_DATE_RE };")();
const offKind = new Function(cut(cal, "function offKind(", "/**", "offKind") + "\nreturn offKind;")();
// 태그 이름 일괄 변경: 원본 정규식 줄을 그대로 실행
const tagRe = new Function("old",
  line(app, "const reEsc = ") + "\n" + line(app, "const re = new RegExp(`(^|") + "\nreturn re;");
const renameTag = (text, old, name) => text.replace(tagRe(old), `$1#${name}`);

for (const c of shared.sched) {
  test(`일정 줄 왕복: ${c.line}`, () => {
    const ev = { ...c.ev, ...(c.js || {}) };
    assert.equal(offKind(ev, c.me), c.off || "");
    ev.off = offKind(ev, c.me);
    assert.equal(sched.schedLine(sched.evToObj(ev)), c.line);
  });
}

test("정규식 (할일 체크 칸·목록 기호·작성자 꼬리표·할일 날짜)", () => {
  for (const c of shared.lines) {
    const t = c.text;
    const m = t.match(sched.TASK_BOX_RE);
    assert.equal(m ? m[2] : null, c.task, t);
    assert.equal((t.match(sched.LIST_RE) || [""])[0], c.list, t);
    assert.equal(sched.WHO_RE.test(t), c.who, t);
    if (c.dates) {
      const d = {};
      for (const x of t.matchAll(sched.TASK_DATE_RE)) d[x[1]] = x[2];
      assert.deepEqual(d, c.dates, t);
    }
  }
});

test("태그 이름 일괄 변경", () => {
  const src = "# 팀 일정\n261120, 점검, #전산 #외근\n261121, 교육, #전산팀\n261122,가,#전산,장소\n- [ ] 할일 #전산\n261123，나，#전산\n";
  const out = renameTag(src, "전산", "IT");
  assert.match(out, /#IT #외근/);
  assert.match(out, /#전산팀/);          // 앞부분만 같은 태그는 그대로
  assert.match(out, /,#IT,장소/);
  assert.match(out, /할일 #IT/);
  assert.match(out, /，#IT\n/);          // 전각 쉼표
  assert.match(out, /^# 팀 일정$/m);     // 제목 줄은 그대로
  assert.equal(renameTag("261120, a, #c++ #c", "c++", "cpp"), "261120, a, #cpp #c");   // 정규식 기호가 든 태그
});
