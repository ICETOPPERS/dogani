// 노트 합치기(merge3) 단위 시험. app.js 에서 함수를 직접 잘라 써서 복사본이 낡을 일이 없음.
// 실행: node --test tests/
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "app", "ui", "app.js"), "utf8");
const a = src.indexOf("function lcsMap(");
const b = src.indexOf("// ---", src.indexOf("function merge3("));
assert.ok(a > 0 && b > a, "app.js 에서 lcsMap·merge3 를 찾지 못함 (함수 위치가 바뀌었으면 이 파일을 고칠 것)");
const merge3 = new Function(src.slice(a, b) + "\nreturn merge3;")();

const T = (name, base, mine, theirs, want) =>
  test(name, () => assert.equal(merge3(base, mine, theirs, "\n"), want));

T("다른 줄을 각자 고침", "a\nb\nc\n", "a A1\nb\nc\n", "a\nb B1\nc\n", "a A1\nb B1\nc\n");
T("같은 줄을 다르게 고침 = 충돌", "a\nb\nc\n", "a\nb\nc-A\n", "a\nb\nc-B\n", null);
T("새 노트 둘이 동시에 씀", "", "A\n", "B\n", "A\nB\n");
T("멀리 떨어진 두 줄", "a\nb\nc\n", "a X\nb\nc\n", "a\nb\nc Y\n", "a X\nb\nc Y\n");
T("끝에 각자 줄 추가", "a\nb\n", "a\nb\nnew1\n", "a\nb\nnew2\n", "a\nb\nnew1\nnew2\n");
T("한쪽은 지우고 한쪽은 끝에 추가", "a\nb\nc\n", "a\nc\n", "a\nb\nc\nd\n", "a\nc\nd\n");
T("한쪽은 고치고 한쪽은 지움 = 충돌", "a\nb\nc\n", "a\nb2\nc\n", "a\nc\n", null);
T("붙은 할일 체크를 각자", "- [ ] a\n- [ ] b\n- [ ] c\n", "- [x] a\n- [ ] b\n- [ ] c\n", "- [ ] a\n- [x] b\n- [x] c\n", "- [x] a\n- [x] b\n- [x] c\n");
T("한쪽만 줄바꿈 형식이 다름 (고친 쪽 따름)", "a\r\nb\r\n", "a\nb\nc\n", "a\r\nb\r\n", "a\nb\nc\n");
T("끝 줄바꿈 없는 새 노트", "", "B", "A", "B\nA");
T("한쪽이 다 지움", "x\n", "", "x\n", "");
T("둘 다 끝에 줄 추가 (내 것 → 남의 것)", "a\nb\n", "a\nb\nx\n", "a\nb\ny\n", "a\nb\nx\ny\n");
T("남이 끝 줄바꿈 없이 추가", "a\n", "a\n", "a\nb", "a\nb");
T("같은 고침 두 번은 한 번만", "a\nb\n", "a\nB\n", "a\nB\n", "a\nB\n");
