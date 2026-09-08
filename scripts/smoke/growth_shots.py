#!/usr/bin/env python3
"""자가발전(self-growth) 라이브 스모크 — 실제 모델 + 6만 run 데모 데이터셋, 한 페이지·한 세션.

읽는 방식은 테스트와 같다: 각 발화 뒤에 ``check(설명, 조건)``이 PASS/FAIL을 찍고, 마지막에 합계를
낸다. 실패에는 두 종류가 있고 종료 코드는 그중 하나만 본다 —

* FAIL(product)  호스트/카드/라우트가 잘못된 것. 종료 코드 1. 고치고 다시 돈다.
* FAIL(model)    모델이 다르게 답한 것(기대한 도구 대신 다른 도구를 썼다든지). 한 번 더 명확한
  문장으로 물어보고, 그래도 빗나가면 정직하게 기록만 하고 넘어간다 — 제품 버그가 아니다.

숫자는 이 스크립트가 계산하지 않는다: 2번 발화의 카드 숫자는 데모 SQLite를 직접 GROUP BY 해서
맞는지 대조한다(``--db``). 화면은 발화마다 ``scripts/smoke/shots/growth-NN.png``(1단계) /
``growth2-NN.png``(2단계)으로 남는다.

    .venv-pw/Scripts/python.exe scripts/smoke/growth_shots.py [--restart] [--phase 1|2]

* ``--phase 1``(기본) 질의 엔진 6발화: 카탈로그로 답하기 / 경로 축 grouping+SQLite 대조 /
  후속 좁히기 / 실패율 / 내 범위 / 답할 수 없는 질문(note_unmet_ask).
* ``--phase 2`` 1단계 여섯 발화를 회귀로 다시 돌고, 그 위에 어휘 확인(2세션) · 승격된 저장
  질문 · 👍→eval 케이스 · Growth 화면 네 탭 · 안전(감사/job 불변)까지 본다.

Playwright 런타임은 저장소 안의 ``.venv-pw``다(gitignore, msedge headless).
필요한 패키지: ``playwright`` + ``requests`` + ``tzdata`` (Windows에는 시스템 tz 데이터베이스가
없어서 ``ZoneInfo("Asia/Seoul")``이 tzdata 없이는 못 뜬다 — 날 단위 창 계산이 그걸 쓴다).

전제: web 개발 서버가 :3110에 이미 떠 있다(핫리로드). 호스트는 ``--restart``로 이 스크립트가
데모 데이터셋 위에 다시 띄우거나(권장), 아래 환경변수로 직접 띄운 뒤 생략한다.

    ATWORKS_STORE_PATH=<repo>/scripts/scale/out/demo/scale.sqlite
    ATWORKS_FIXTURES_DIR=<repo>/scripts/scale/out/demo
    ATWORKS_INSIGHT_NARRATION=0
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from playwright.sync_api import Page, sync_playwright

# 이 스크립트가 찍는 것은 한국어 발화, 카드 문구, 그리고 👍/👎다 — Windows 콘솔의 기본 코드페이지
# (cp949)로는 이모지가 UnicodeEncodeError를 낸다. 실제로 그렇게 터졌다: 👍를 누른 뒤 결과를
# **찍다가** 단계가 죽어서, 케이스 파일은 멀쩡히 쓰였는데 스모크는 제품 FAIL을 보고했다. 로그가
# 자기가 재는 것을 부수면 안 된다. `errors="replace"`까지 두는 이유는 같다: 인코딩 하나 때문에
# 스모크가 다시 죽는 것보다 물음표 한 글자가 낫다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "scripts" / "smoke" / "shots"
DEMO = ROOT / "scripts" / "scale" / "out" / "demo"
PORTAL = "http://localhost:3110"
HOST = "http://127.0.0.1:8010"
TZ = "Asia/Seoul"
COMPOSER = "aTworks 어시스턴트에게 메시지 보내기"
OPERATOR = "op-0000"
TURN_TIMEOUT_MS = 240_000

# -- 결과 집계 ---------------------------------------------------------------------------

PASSED: list[str] = []
FAILED_PRODUCT: list[str] = []
FAILED_MODEL: list[str] = []


def check(desc: str, cond: bool, *, kind: str = "product") -> bool:
    """한 줄 단언. ``kind='model'``은 모델 행동 차이라 종료 코드에 반영하지 않는다."""
    if cond:
        PASSED.append(desc)
        print(f"  PASS  {desc}")
    else:
        (FAILED_MODEL if kind == "model" else FAILED_PRODUCT).append(desc)
        print(f"  FAIL({kind})  {desc}")
    return cond


# -- 호스트 (선택: --restart) -------------------------------------------------------------


def _pids_on(port: int) -> list[str]:
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = set()
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            pids.add(line.split()[-1])
    return sorted(pids)


def restart_host() -> None:
    for pid in _pids_on(8010):
        print(f"[host] killing pid {pid} on :8010")
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, text=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    log = (SHOTS / "host.log").open("w", encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "ATWORKS_STORE_PATH": str(DEMO / "scale.sqlite").replace("\\", "/"),
        "ATWORKS_FIXTURES_DIR": str(DEMO).replace("\\", "/"),
        "ATWORKS_INSIGHT_NARRATION": "0",
    }
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    print(f"[host] starting {python} -m atworks_host.main on {DEMO}")
    subprocess.Popen([str(python), "-m", "atworks_host.main"], cwd=ROOT, env=env,
                     stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            with urllib.request.urlopen(f"{HOST}/api/atworks/operators", timeout=5) as response:
                if response.status == 200:
                    print("[host] up")
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise SystemExit("host did not answer /operators within 120s")


def api_get(path: str, session_id: str | None = None) -> Any:
    request = urllib.request.Request(f"{HOST}/api/atworks{path}")
    if session_id:
        request.add_header("X-Session-Id", session_id)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


# -- SQLite 대조 (2번 발화) ---------------------------------------------------------------


def _ceil_local_day(moment: datetime, zone: ZoneInfo) -> date:
    local = moment.astimezone(zone)
    if local.timetz() == clock(0, 0, tzinfo=local.tzinfo):
        return local.date()
    return local.date() + timedelta(days=1)


# 대조할 수 있는 경로 축 → apis의 물질화된 열. 이 셋 밖의 축이 오면 대조를 건너뛴다.
SEGMENT_COLUMN = {
    "path_segment_1": "a.path_segment_1",
    "path_segment_2": "a.path_segment_2",
    "path_segment_3": "a.path_segment_3",
    "path_prefix_2": "a.path_prefix_2",
}


def window_days_from_card(window: dict[str, Any]) -> tuple[str, str]:
    """카드가 실은 창(``payload.window``, 서버가 해석한 UTC 인스턴트 두 개) → ``runs.day``가
    쓰는 로컬 날짜 두 개. 이 값을 ``now``에서 다시 계산하지 않는 것이 요점이다: 턴이 자정을
    넘기면 카드가 센 구간과 대조가 세는 구간이 하루 어긋나고, 그 어긋남은 모델 잘못으로도
    제품 버그로도 보이지 않는 조용한 거짓 FAIL이 된다.

    ``until``은 배타적 상한(다음 날 자정)이라 ``runs.day``의 마지막 날은 그 하루 전이다 —
    ``query_sql.align_days``가 세는 방식과 같다."""
    zone = ZoneInfo(TZ)
    since = datetime.fromisoformat(str(window["since"]).replace("Z", "+00:00")).astimezone(zone)
    until = datetime.fromisoformat(str(window["until"]).replace("Z", "+00:00")).astimezone(zone)
    return since.date().isoformat(), (_ceil_local_day(until, zone) - timedelta(days=1)).isoformat()


def sqlite_segment_non_pass(db: Path, dimension: str, filters: dict[str, Any],
                            days: tuple[str, str]) -> list[tuple[str, int, int]]:
    """``(그룹키, non_pass, runs)`` 내림차순 — host의 query_sql이 쓰는 것과 같은 날 단위 로컬 창
    (``runs.day``, briefing_tz)과 같은 필터를 원본 테이블 위에 손으로 다시 만든다. 이 함수가
    카드 숫자의 유일한 검증 기준이다: 모델도, 카드도 아니라 runs ⋈ apis다.

    창은 인자로 받는다 — **카드가 실은 창 그대로**(``window_days_from_card``), 이 함수가
    ``now``에서 다시 만든 창이 아니다.

    ``status`` 필터는 일부러 무시한다 — non_pass는 pass 행이 0을 더하므로 status=non_pass가
    있든 없든 그룹별 non_pass 값이 같다. 나머지 필터는 SQL로 옮기고, 옮길 수 없는 필터가 하나라도
    있으면 호출자가 대조를 포기한다(``cross_check_step_2``)."""
    day_from, day_to = days
    where = ["r.day BETWEEN ? AND ?"]
    params: list[Any] = [day_from, day_to]
    if filters.get("path_contains"):
        needles = [str(n).lower() for n in filters["path_contains"]]
        where.append("(" + " OR ".join(["LOWER(a.path) LIKE ?"] * len(needles)) + ")")
        params += [f"%{n}%" for n in needles]
    if filters.get("path_prefix"):
        where.append("LOWER(a.path) LIKE ?")
        params.append(f"{str(filters['path_prefix']).lower()}%")
    if filters.get("method"):
        methods = list(filters["method"])
        where.append("a.method IN (" + ",".join("?" * len(methods)) + ")")
        params += methods
    if filters.get("target_env"):
        envs = list(filters["target_env"])
        where.append("r.target_env IN (" + ",".join("?" * len(envs)) + ")")
        params += envs
    column = SEGMENT_COLUMN[dimension]
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            f"SELECT {column} AS seg, "
            "       SUM(CASE WHEN r.status IN ('fail','error') THEN 1 ELSE 0 END) AS non_pass, "
            "       COUNT(*) AS runs "
            "FROM runs r JOIN apis a ON a.api_id = r.api_id "
            f"WHERE {' AND '.join(where)} GROUP BY seg ORDER BY non_pass DESC",
            params,
        ).fetchall()
    finally:
        connection.close()
    print(f"  [sqlite] {dimension} {day_from}..{day_to} where={where[1:]}: "
          + ", ".join(f"{s}={n}/{t}" for s, n, t in rows[:5]))
    return [(str(s), int(n), int(t)) for s, n, t in rows]


# -- 페이지 ------------------------------------------------------------------------------

READ_TURN = """
() => {
  const turns = [...document.querySelectorAll('[data-turn]')];
  const last = turns[turns.length - 1];
  if (!last) return null;
  const text = [];
  const cards = [];
  for (const child of last.children) {
    const component = child.getAttribute('data-component');
    if (component) {
      const pre = child.querySelector('details pre');
      cards.push({
        component,
        text: child.innerText,
        raw: child.textContent,
        spec: pre ? pre.textContent : null,
        rows: child.querySelectorAll('table tbody tr').length,
        columns: [...child.querySelectorAll('table thead th')].map((th) => th.innerText.trim()),
      });
    } else {
      text.push(child.innerText);
    }
  }
  return { turn: last.getAttribute('data-turn'), text: text.join('\\n').trim(), cards };
}
"""


class Turn:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.turn_id: str = payload.get("turn") or ""
        self.text: str = payload.get("text") or ""
        self.cards: list[dict[str, Any]] = payload.get("cards") or []

    def card(self, component: str) -> dict[str, Any] | None:
        return next((c for c in self.cards if c["component"] == component), None)

    def _detail(self, component: str) -> dict[str, Any]:
        """카드의 ``실행된 질의(JSON)`` 블록 = ``{spec, window, source}``. 창을 여기서 읽는 것이
        핵심이다 — 헤더의 날짜는 사람이 읽는 형식이고, 스모크가 ``now``로 창을 다시 계산하면
        턴이 자정을 넘긴 순간 카드와 다른 구간을 대조하게 된다."""
        card = self.card(component)
        if card is None or not card.get("spec"):
            return {}
        try:
            return json.loads(card["spec"])
        except json.JSONDecodeError:
            return {}

    def spec(self, component: str = "query_table") -> dict[str, Any]:
        return self._detail(component).get("spec") or {}

    def window(self, component: str = "query_table") -> dict[str, Any]:
        return self._detail(component).get("window") or {}

    @property
    def components(self) -> list[str]:
        return [c["component"] for c in self.cards]

    @property
    def first_line(self) -> str:
        for line in self.text.splitlines():
            if line.strip():
                return line.strip()
        return ""


def send(page: Page, message: str) -> Turn:
    box = page.get_by_label(COMPOSER)
    box.click()
    box.fill(message)
    page.get_by_role("button", name="Send").click()
    # busy가 켜졌다 꺼질 때까지 — placeholder가 유일하게 믿을 수 있는 신호다.
    page.wait_for_function(
        "() => document.querySelector('textarea')?.placeholder === 'Working…'", timeout=30_000)
    page.wait_for_function(
        "() => document.querySelector('textarea')?.placeholder !== 'Working…'", timeout=TURN_TIMEOUT_MS)
    page.wait_for_timeout(1500)   # 마지막 카드가 붙는 프레임
    payload = page.evaluate(READ_TURN)
    if payload is None:
        raise RuntimeError("no assistant turn in the transcript")
    return Turn(payload)


#: 스크린샷 파일 이름 앞부분. 1단계는 ``growth-NN.png``, 2단계는 ``growth2-NN.png`` — 두 단계를
#: 잇달아 돌려도 서로의 그림을 덮지 않는다.
SHOT_PREFIX = "growth"


def shoot(page: Page, index: int) -> Path:
    path = SHOTS / f"{SHOT_PREFIX}-{index:02d}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


NO_SUPPORT = re.compile(r"지원하지\s*않|지원되지\s*않|not supported|unsupported|할 수 없습니다만", re.I)


TRANSCRIPT: list[dict[str, Any]] = []


def report(index: int, ask: str, turn: Turn, shot: Path) -> None:
    """콘솔 한 문단 + ``shots/turns.json`` 한 행 — 보고서에 붙일 '모델이 실제로 뭐라 했나'는
    스크린샷이 아니라 이 파일에서 읽는다(스크린샷은 커밋하지 않는다)."""
    print(f"\n[{index}] {ask}")
    print(f"  reply: {turn.first_line[:160] or '(no text)'}")
    print(f"  cards: {turn.components or '(none)'}   shot: {shot.name}")
    spec = turn.spec()
    if spec:
        print(f"  spec:  {json.dumps(spec, ensure_ascii=False, sort_keys=True)}")
    TRANSCRIPT.append({"step": index, "ask": ask, "first_line": turn.first_line,
                       "reply": turn.text, "components": turn.components, "spec": spec,
                       "shot": shot.name})
    (SHOTS / "turns.json").write_text(json.dumps(TRANSCRIPT, ensure_ascii=False, indent=2),
                                      encoding="utf-8", newline="\n")


# -- 발화 --------------------------------------------------------------------------------


def step_1(page: Page, state: dict[str, Any]) -> None:
    ask = "실패한 결과 중 가장 많이 발생한 케이스는 뭐야? 1~5위 뽑아줘"
    turn = send(page, ask)
    shot = shoot(page, 1)
    report(1, ask, turn, shot)
    card = turn.card("run_groups") or turn.card("query_table")
    if not check("1. run_groups 또는 query_table 카드가 행과 함께 나온다",
                 card is not None and card["rows"] > 0, kind="model"):
        turn = send(page, "방금 질문 다시: 최근 실패·에러를 원인별로 묶어서 상위 5개를 카드로 보여줘")
        shot = shoot(page, 1)
        report(1, "(retry)", turn, shot)
        card = turn.card("run_groups") or turn.card("query_table")
        check("1. (재시도) 카드가 행과 함께 나온다", card is not None and card["rows"] > 0, kind="model")
    state["asks"] = state.get("asks", 0) + 1


def step_2(page: Page, state: dict[str, Any]) -> None:
    ask = ("실패는 총 몇천 건인데 endpoint 기준으로 grouping해줘. "
           "items나 history가 들어간 endpoint 계열이 많이 깨졌는지 보고 싶어")
    turn = send(page, ask)
    shot = shoot(page, 2)
    report(2, ask, turn, shot)
    if turn.card("query_table") is None:
        turn = send(page, "endpoint 경로 조각(두 번째 segment) 기준으로 실패·에러 건수를 묶어서 표로 보여줘")
        shot = shoot(page, 2)
        report(2, "(retry)", turn, shot)
    card, spec = turn.card("query_table"), turn.spec()
    check("2. query_table 카드가 나온다", card is not None, kind="model")
    dimensions = spec.get("dimensions") or []
    check("2. 스펙이 경로 축(path_segment_1/2/3 또는 path_prefix_2)을 쓴다",
          any(d.startswith("path_") for d in dimensions), kind="model")
    check('2. 답이 "지원하지 않습니다"로 끝나지 않는다', not NO_SUPPORT.search(turn.text))
    state["step2"] = (card, spec, turn.window())
    state["asks"] = state.get("asks", 0) + 1


def cross_check_step_2(state: dict[str, Any], db: Path) -> None:
    """카드 숫자 ↔ SQLite. 축이 path_segment_2이고 필터가 창뿐일 때만 대조할 수 있다 —
    모델이 다른 축이나 추가 필터를 골랐으면 대조를 건너뛰고 그렇게 적는다."""
    card, spec, window = state.get("step2", (None, {}, {}))
    dimensions = spec.get("dimensions") or []
    if card is None or len(dimensions) != 1 or dimensions[0] not in SEGMENT_COLUMN:
        print(f"  [sqlite] 대조 생략: 경로 축 단독 스펙이 아니다 (dimensions={dimensions})")
        return
    filters = {k: v for k, v in (spec.get("filters") or {}).items() if v not in (None, [], "")}
    translatable = {"window_days", "status", "path_contains", "path_prefix", "method", "target_env"}
    untranslatable = set(filters) - translatable
    if untranslatable:
        print(f"  [sqlite] 대조 생략: SQL로 옮길 수 없는 필터가 있다 ({sorted(untranslatable)})")
        return
    if not window.get("since") or not window.get("until"):
        print("  [sqlite] 대조 생략: 카드가 창을 싣지 않았다")
        return
    truth = sqlite_segment_non_pass(db, dimensions[0], filters, window_days_from_card(window))
    if not truth:
        check("2. SQLite 대조: 창 안에 run이 있다", False)
        return
    text = card["raw"]
    # 카드에 실제로 실린 상위 3그룹만 대조한다 -- limit이 더 작을 수 있으니 카드에 없는 그룹까지
    # 요구하지 않는다.
    checked = 0
    for seg, non_pass, _runs in truth[:3]:
        if seg not in text:
            continue
        checked += 1
        check(f"2. SQLite 대조: {seg} non_pass={non_pass:,} 가 카드 숫자와 같다",
              f"{non_pass:,}" in text or str(non_pass) in text)
    check(f"2. SQLite 대조: 상위 그룹이 카드에 실렸다 (대조 {checked}건)", checked > 0)


def step_3(page: Page, state: dict[str, Any]) -> None:
    ask = "그중 history 계열만 환경별로 나눠줘"
    turn = send(page, ask)
    shot = shoot(page, 3)
    report(3, ask, turn, shot)
    spec = turn.spec()
    dimensions = spec.get("dimensions") or []
    filters = spec.get("filters") or {}
    history = json.dumps(filters, ensure_ascii=False).lower()
    check("3. query_table 카드가 또 나온다", turn.card("query_table") is not None, kind="model")
    check("3. 환경 축이거나 history 필터가 잡혔다",
          "target_env" in dimensions or "history" in history, kind="model")
    state["asks"] = state.get("asks", 0) + 1


def step_4(page: Page, state: dict[str, Any]) -> None:
    ask = "메서드별 실패율은?"
    turn = send(page, ask)
    shot = shoot(page, 4)
    report(4, ask, turn, shot)
    spec = turn.spec()
    check("4. query_table 카드", turn.card("query_table") is not None, kind="model")
    check("4. method 축", "method" in (spec.get("dimensions") or []), kind="model")
    check("4. fail_rate 측정값", "fail_rate" in (spec.get("measures") or []), kind="model")
    state["asks"] = state.get("asks", 0) + 1


NUMBER = re.compile(r"\d[\d,]{2,}")


def step_5(page: Page, state: dict[str, Any]) -> None:
    ask = "내가 실행한 거 몇 개야?"
    turn = send(page, ask)
    shot = shoot(page, 5)
    report(5, ask, turn, shot)
    card = turn.card("query_table") or turn.card("run_groups")
    check("5. 카드가 나온다", card is not None, kind="model")
    spec = turn.spec()
    filters = spec.get("filters") or {}
    if not (filters.get("scope_operator") or filters.get("executed_by")):
        # 축으로 executed_by를 잡고 필터는 안 거는 답(=오퍼레이터 순위표)도 카드로는 맞지만
        # "내 것"이 아니다. 한 번 더 분명하게 묻는다.
        turn = send(page, "op-0000, 그러니까 내가 승인해서 실행된 run만 세어줘")
        shot = shoot(page, 5)
        report(5, "(retry) op-0000이 실행한 run만", turn, shot)
        state["asks"] = state.get("asks", 0) + 1
        card = turn.card("query_table") or turn.card("run_groups")
        spec = turn.spec()
        filters = spec.get("filters") or {}
    check("5. 스펙이 내 범위(scope_operator/executed_by)를 쓴다",
          bool(filters.get("scope_operator") or filters.get("executed_by")), kind="model")
    on_card = card["raw"] if card else ""
    invented = [n for n in NUMBER.findall(turn.text) if n not in on_card and n.replace(",", "") not in on_card]
    check(f"5. 카드 밖에서 숫자를 지어내지 않는다 (본 것: {invented})", not invented)
    state["asks"] = state.get("asks", 0) + 1


def _unmet_rows(session_id: str, before_seq: int) -> list[dict[str, Any]]:
    page = api_get("/ask-log?outcome=unmet&limit=50", session_id)
    check("6. /ask-log?outcome=unmet 라우트가 봉투를 돌려준다",
          set(page) >= {"items", "total", "next_cursor"})
    # `seq` alone, not `session_id`: the wire record deliberately carries no session id
    # (`/ask-log` is a team read, and a live session id there is a header someone can replay).
    # A monotonic seq taken before the turn is the same fence for a smoke that owns the host.
    return [row for row in page["items"] if (row.get("seq") or 0) > before_seq]


def step_6(page: Page, state: dict[str, Any], session_id: str) -> None:
    """카탈로그로 답할 수 없는 질문. 여기서 검증하는 제품 동작은 라우트와 봉투뿐 —
    ``note_unmet_ask``를 실제로 부를지는 모델 행동이라 FAIL(model)로만 적는다."""
    before = max([(row.get("seq") or 0)
                  for row in api_get("/ask-log?limit=200", session_id)["items"]], default=0)
    ask = "이 run들 왜 실패했어? 서버 로그 보여줘"
    turn = send(page, ask)
    shot = shoot(page, 6)
    report(6, ask, turn, shot)
    state["asks"] = state.get("asks", 0) + 1
    rows = _unmet_rows(session_id, before)
    if not rows:
        # 한 번의 재시도: 이 배포가 아예 갖고 있지 않은 증거를 명확히 요구한다.
        retry = ("대상 서버의 애플리케이션 로그 원문(stack trace)을 그대로 보여줘. "
                 "aTworks가 남긴 상태 말고 서버 로그 파일 내용이 필요해")
        turn = send(page, retry)
        shot = shoot(page, 6)
        report(6, "(retry) " + retry, turn, shot)
        state["asks"] += 1
        rows = _unmet_rows(session_id, before)
    reasons = {row.get("unmet_reason") for row in rows}
    check("6. unmet 행이 이 세션에 새로 생겼다", bool(rows), kind="model")
    # `note_unmet_ask`의 enum 전체를 받는다. 처음에는 no_evidence|no_dimension만 받았는데, 모델이
    # "서버 로그"를 out_of_scope로 분류한 것은 그 enum 안의 정당한 값이다 — 좁은 기대는 모델이
    # 아니라 이 검사가 틀린 것이었다. 여기서 봐야 하는 것은 "설명 전에 기록했는가"이지 네 사유 중
    # 어느 것을 골랐는가가 아니다.
    valid = {"no_dimension", "no_evidence", "out_of_scope", "refused"}
    check(f"6. unmet_reason이 note_unmet_ask의 enum 안이다 (본 것: {sorted(r for r in reasons if r)})",
          bool(reasons & valid), kind="model")
    # 여기서는 "…하지 않습니다"가 규칙 위반이 아니라 정답이다 — note_unmet_ask를 부른 뒤
    # 무엇이 있어야 답할 수 있는지 말하는 것이 §5 규칙 (2)가 요구하는 모양이다.
    check("6. 답이 그 데이터가 없다고 인정한다",
          bool(re.search(r"않습니다|않는다|없|불가|미보유", turn.text)), kind="model")
    state["step6_reply"] = turn.first_line


def step_7(session_id: str, asked: int) -> None:
    log = api_get("/ask-log?limit=200", session_id)
    # The wire record carries no session id on purpose (a team read must not hand out another
    # operator's live header), so this smoke -- which owns the host and is the only chatter --
    # counts the whole log instead of filtering to itself.
    rows = log["items"]
    outcomes = {row.get("outcome") for row in rows}
    print(f"\n[7] /ask-log: total={log['total']} {len(rows)}행 "
          f"outcomes={sorted(o for o in outcomes if o)}")
    check(f"7. ask_log ≥ {asked}행 (본 것: {len(rows)})", len(rows) >= asked)
    check("7. outcome이 answered/partial/unmet/action 안에 있다",
          bool(outcomes) and outcomes <= {"answered", "partial", "unmet", "action"})
    # 라우트의 total은 필터 뒤의 참 개수라 outcome별 total 4개가 곧 모집단의 분해다.
    per_outcome = {name: api_get(f"/ask-log?outcome={name}&limit=1", session_id)["total"]
                   for name in ("answered", "partial", "unmet", "action")}
    print(f"    per-outcome totals: {per_outcome} (전체 {log['total']})")
    check(f"7. outcome별 total 합 = 전체 ({sum(per_outcome.values())} vs {log['total']})",
          sum(per_outcome.values()) == log["total"])
    summary = api_get("/growth/summary?days=7", session_id)
    print(f"    /growth/summary: {json.dumps(summary, ensure_ascii=False)}")
    # GrowthSummary now carries all FOUR outcomes, so the tiles are the population's whole
    # decomposition -- their sum must be `asks_total` with nothing borrowed from another read.
    tiles = {k: summary.get(k, 0) for k in ("answered", "partial", "unmet", "action")}
    check(f"7. growth/summary 타일 합 = asks_total ({tiles} vs {summary.get('asks_total')})",
          sum(tiles.values()) == summary.get("asks_total"))
    # 이 비교는 창(7일) 안에 로그가 이 실행분뿐일 때 성립한다 -- per_outcome은 창이 없는 total이다.
    check(f"7. summary.action = /ask-log?outcome=action의 total ({tiles['action']} vs "
          f"{per_outcome['action']})", tiles["action"] == per_outcome["action"])


def step_8(session_id: str, before: dict[str, Any]) -> None:
    jobs = api_get("/jobs?limit=200", session_id)
    audit = api_get("/audit?limit=1", session_id)
    after_jobs = {j["job_id"]: j.get("status") for j in jobs["items"]}
    print(f"\n[8] jobs={len(after_jobs)} audit total={audit['total']} "
          f"(before: jobs={len(before['jobs'])} audit={before['audit']})")
    check("8. job 상태가 채팅 전후로 동일하다", after_jobs == before["jobs"])
    check("8. 채팅은 감사 로그를 한 줄도 쓰지 않는다", audit["total"] == before["audit"])


# -- 2단계: 어휘 · 승격 · 피드백 · Growth 화면 --------------------------------------------


def _button(page: Page, name: str):
    return page.get_by_role("button", name=name, exact=True)


def _audit_total(session_id: str) -> int:
    return api_get("/audit?limit=1", session_id)["total"]


def _run_in_host_venv(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    """메인 venv의 별도 프로세스에서 한 조각 실행. 스모크가 도는 ``.venv-pw``에는
    ``atworks_host``가 없고, 호스트는 같은 SQLite 파일을 연 채로 돌고 있다 — 심는 경로는
    제품이 쓰는 것과 같은 Store API여야 하므로 SQL을 손으로 쓰지 않는다."""
    return subprocess.run([str(ROOT / ".venv" / "Scripts" / "python.exe"), "-c", code, *args],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


_SEED_CODE = """
import json, sys
from datetime import UTC, datetime, timedelta
from atworks_agent import AskEntry, QuerySpec
from atworks_host.store import Store

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
store = Store(payload["db"])
now = datetime.now(UTC)
planted = 0
for index, operator in enumerate(["op-0001", "op-0002", "op-0003", "op-0001", "op-0002"]):
    store.insert_ask(AskEntry(
        at=now - timedelta(hours=index + 1), session_id=f"seed-{index}", operator=operator,
        role="qa", question=payload["question"], intent="aggregate",
        spec=QuerySpec.model_validate(payload["spec"]), outcome="answered", tool_calls=2, cards=1,
        cluster_key=payload["cluster_key"], turn_id=f"seed-{index}"))
    planted += 1
print(planted)
"""

_PROMOTE_CODE = """
import asyncio, sys
from atworks_agent import AtworksAgentConfig
from atworks_host.promoter import Promoter
from atworks_host.store import Store

print(asyncio.run(Promoter(Store(sys.argv[1]), AtworksAgentConfig(model="smoke")).run()))
"""


def _seed_asks(db: Path, spec: dict[str, Any], cluster_key: str, question: str) -> int:
    """같은 군집의 ask_log 행 다섯 개를 세 오퍼레이터 이름으로 심는다 — 승격 문턱
    (``promote_min_users`` / ``promote_min_asks``)을 실제 사람 다섯 명 없이 넘기기 위한 것."""
    seed_json = SHOTS / "_seed.json"
    seed_json.write_text(
        json.dumps({"db": str(db), "spec": spec, "cluster_key": cluster_key, "question": question},
                   ensure_ascii=False),
        encoding="utf-8", newline="\n")
    done = _run_in_host_venv(_SEED_CODE, str(seed_json))
    if done.returncode != 0:
        print(f"  [seed] FAILED: {(done.stderr or '').strip()[-600:]}")
        return 0
    return int(((done.stdout or "").strip().splitlines() or ["0"])[-1])


def step_v1(page: Page, state: dict[str, Any], session_id: str) -> None:
    """어휘 제안 → 확인 클릭. 확정은 오직 이 클릭에서 일어난다(채팅에 "맞아"라고 써도 아무것도
    저장되지 않는다) — 그래서 감사 로그의 짝도 여기서만 는다."""
    ask = "결제 계열 실패만 보여줘"
    turn = send(page, ask)
    shot = shoot(page, 10)
    report(10, ask, turn, shot)
    card = turn.card("query_table")
    if card is None or "맞나요" not in (card.get("text") or ""):
        retry = "결제 계열(= /v1/payment 경로) 실패만 보여줘; 이 표현은 앞으로도 쓸 거야"
        turn = send(page, retry)
        shot = shoot(page, 10)
        report(10, "(retry) " + retry, turn, shot)
        card = turn.card("query_table")
    prompted = card is not None and "맞나요" in (card.get("text") or "")
    check("V1. 카드에 어휘 확인 줄(‘…’ … 맞나요? [예][아니오])이 붙는다", prompted, kind="model")
    if not prompted:
        return
    yes = _button(page, "예").last
    if yes.count() == 0:
        check("V1. [예] 버튼이 있다", False)
        return
    yes.click()
    page.wait_for_timeout(3000)
    print(f"  shot: {shoot(page, 11).name}")
    state["clicks"] = state.get("clicks", 0) + 1
    confirmed = api_get("/vocabulary?status=confirmed&limit=50", session_id)
    terms = [row.get("term") for row in confirmed["items"]]
    check(f"V1. /vocabulary?status=confirmed 에 용어가 들어갔다 (본 것: {terms})", bool(terms))
    actions = [row.get("action") for row in api_get("/audit?limit=20", session_id)["items"]]
    check(f"V1. 감사 로그에 vocabulary_confirm 짝이 남는다 (본 것: {actions[:4]})",
          "vocabulary_confirm" in actions and "vocabulary_confirm:ok" in actions)
    state["term"] = terms[0] if terms else None


def step_v2(page: Page, state: dict[str, Any]) -> str:
    """다른 오퍼레이터, 새 세션. 확정된 용어는 팀 공용이므로 두 번째 사람은 뜻을 다시 묻지 않아야
    한다 — 이것이 어휘가 '조직의 기억'인지 '그 세션의 우연'인지 가르는 유일한 관찰이다."""
    session_ids: list[str] = []
    page.on("request", lambda request: session_ids.append(request.headers["x-session-id"])
            if request.headers.get("x-session-id") else None)
    page.get_by_label("운영자 선택").select_option("op-0001")
    page.wait_for_timeout(2000)
    page.reload(wait_until="networkidle", timeout=120_000)
    page.wait_for_timeout(3000)
    ask = "결제 계열 실패 추이 보여줘"
    turn = send(page, ask)
    shot = shoot(page, 12)
    report(12, ask, turn, shot)
    filters = json.dumps(turn.spec().get("filters") or {}, ensure_ascii=False).lower()
    check("V2. 다른 세션이 ‘결제 계열’의 뜻을 되묻지 않는다",
          not re.search(r"무슨 뜻|뜻인가요|어떤 의미|정의해 ?주|무엇을 뜻", turn.text), kind="model")
    check(f"V2. 질의 필터가 결제 경로를 쓴다 (본 것: {filters[:140]})",
          "payment" in filters or "결제" in filters, kind="model")
    return session_ids[-1] if session_ids else ""


def step_promotion(page: Page, state: dict[str, Any], session_id: str, db: Path) -> None:
    """승격: ask_log를 문턱까지 채우고 스케줄러 tick(LLM 없음)을 눌러 저장 질문을 만든 뒤,
    Home 카드 → [실행] → Growth 탭 → [숨기기]까지 사람이 하는 순서 그대로 본다."""
    log = api_get("/ask-log?limit=200", session_id)
    seeds = [row for row in log["items"]
             if row.get("spec") and (row.get("cluster_key") or "").startswith("dims=path_")]
    seeds = seeds or [row for row in log["items"] if row.get("spec")]
    if not seeds:
        check("P. 씨앗으로 쓸 ask_log 행이 있다", False)
        return
    seed = seeds[0]
    planted = _seed_asks(db, seed["spec"], seed["cluster_key"],
                         seed.get("question") or "엔드포인트별 실패")
    check(f"P. ask_log에 같은 군집 행을 심었다 (본 것: {planted}행)", planted == 5)

    request = urllib.request.Request(f"{HOST}/api/atworks/scheduler/tick", method="POST")
    request.add_header("X-Session-Id", session_id)
    with urllib.request.urlopen(request, timeout=180) as response:
        response.read()
    saved = api_get("/saved-questions?limit=50", session_id)
    if not saved["items"]:
        # 하루 1회 가드가 이미 오늘 돌았다면 tick은 승격을 건너뛴다 — 승격기를 직접 돌린다.
        done = _run_in_host_venv(_PROMOTE_CODE, str(db))
        print(f"  [promoter] {(done.stdout or '').strip() or (done.stderr or '').strip()[-400:]}")
        saved = api_get("/saved-questions?limit=50", session_id)
    titles = [row.get("title") for row in saved["items"]]
    check(f"P. 저장 질문이 생겼다 (본 것: {titles})", bool(titles))
    if not titles:
        return
    check("P. 제목이 카탈로그 문장이다 (‘기본 기간’이 아니다)",
          all("기본 기간" not in (title or "") for title in titles))

    page.reload(wait_until="networkidle", timeout=120_000)
    page.wait_for_timeout(3500)
    body = page.inner_text("body")
    check("P. Home에 ‘저장 질문’ 카드가 보인다", "저장 질문" in body)
    print(f"  shot: {shoot(page, 13).name}")
    run_button = _button(page, "실행").first
    if run_button.count() == 0:
        check("P. [실행] 버튼이 있다", False)
    else:
        run_button.click()
        page.wait_for_timeout(5000)
        print(f"  shot: {shoot(page, 14).name}")
        table = page.locator('[data-testid="saved_question_table"]')
        check("P. [실행]이 표를 그린다",
              table.count() > 0 and table.first.locator("tbody tr").count() > 0)

    page.get_by_role("button", name="Growth").first.click()
    page.wait_for_timeout(2500)
    _button(page, "저장 질문").first.click()
    page.wait_for_timeout(2500)
    print(f"  shot: {shoot(page, 15).name}")
    growth_body = page.inner_text("body")
    check("P. Growth의 ‘저장 질문’ 탭에 그 질문이 있다",
          any(title and title in growth_body for title in titles))

    audit_before = _audit_total(session_id)
    hide = _button(page, "숨기기").first
    if hide.count() == 0:
        check("P. [숨기기] 버튼이 있다", False)
        return
    hide.click()
    page.wait_for_timeout(3000)
    state["clicks"] = state.get("clicks", 0) + 1
    active = api_get("/saved-questions?status=active&limit=50", session_id)
    check(f"P. 숨긴 질문이 활성 목록에서 빠진다 (남은 {active['total']}개 / 전 {saved['total']}개)",
          active["total"] < saved["total"])
    actions = [row.get("action") for row in api_get("/audit?limit=10", session_id)["items"]]
    check(f"P. 감사 로그에 saved_question_hide 짝이 남는다 (본 것: {actions[:4]})",
          "saved_question_hide" in actions and "saved_question_hide:ok" in actions)
    check("P. 숨기기는 감사 로그를 정확히 두 줄 늘린다",
          _audit_total(session_id) == audit_before + 2)
    print(f"  shot: {shoot(page, 16).name}")


def _case_files() -> set[str]:
    return {path.name for path in (ROOT / "evals" / "cases").glob("*.json")}


def step_feedback(page: Page, state: dict[str, Any], session_id: str) -> None:
    """👍 → eval 케이스 파일 하나, 👎 → 파일 없음, 그리고 그 파일을 포함한 ``pytest -m evals``가
    통과해야 한다 — 만들어만 놓고 돌지 않는 케이스는 회귀 스위트가 아니다. 어느 쪽도 감사 로그를
    쓰지 않는다: 평가는 승인이 아니다."""
    # 채팅은 별도 뷰가 아니라 오른쪽 패널이지만, Growth 화면에서 바로 보내면 방금 클릭한 탭이
    # 스크린샷을 채운다 — 카드를 보려고 Home으로 돌아온다.
    page.get_by_role("button", name="Home").first.click()
    page.wait_for_timeout(2000)
    audit_before = _audit_total(session_id)
    ask = "엔드포인트 경로 두 번째 조각 기준으로 최근 30일 실패·에러를 묶어서 표로 보여줘"
    turn = send(page, ask)
    shot = shoot(page, 17)
    report(17, ask, turn, shot)
    if turn.card("query_table") is None:
        check("F. 👍를 누를 query_table 카드가 있다", False, kind="model")
        return
    before = _case_files()
    up = _button(page, "👍").last
    if up.count() == 0:
        check("F. 카드에 👍 버튼이 있다", False)
        return
    up.click()
    page.wait_for_timeout(3500)
    print(f"  shot: {shoot(page, 18).name}")
    new_files = sorted(_case_files() - before)
    check(f"F. 👍가 evals/cases에 파일을 하나 만든다 (본 것: {new_files})", len(new_files) == 1)
    if new_files:
        case = json.loads((ROOT / "evals" / "cases" / new_files[0]).read_text(encoding="utf-8"))
        dimensions = ((case.get("expected") or {}).get("spec_equals") or {}).get("dimensions") or []
        check(f"F. 케이스의 expected.spec_equals.dimensions에 경로 축이 있다 (본 것: {dimensions})",
              any(str(d).startswith("path_segment_") for d in dimensions), kind="model")
        state["eval_case"] = new_files[0]

    ask2 = "메서드별 실패율 다시 보여줘"
    turn2 = send(page, ask2)
    shot = shoot(page, 19)
    report(19, ask2, turn2, shot)
    before2 = _case_files()
    down = _button(page, "👎").last
    if down.count() == 0:
        check("F. 카드에 👎 버튼이 있다", False, kind="model")
    else:
        down.click()
        page.wait_for_timeout(3500)
        check("F. 👎는 케이스 파일을 만들지 않는다", _case_files() == before2)
    check(f"F. 피드백은 감사 로그를 쓰지 않는다 (승인이 아니다) — {audit_before} 그대로",
          _audit_total(session_id) == audit_before)

    done = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "pytest", "-m", "evals", "-q"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    tail = ((done.stdout or "").strip().splitlines() or [""])[-1]
    check(f"F. 새 케이스를 포함해 pytest -m evals가 통과한다 ({tail})", done.returncode == 0)


def step_growth_view(page: Page, state: dict[str, Any], session_id: str) -> None:
    """Growth 네 탭이 실제 행으로 그려진다. 숫자는 화면이 아니라 라우트가 진실이라
    ``/growth/summary``와 맞춰 본다."""
    page.get_by_role("button", name="Growth").first.click()
    page.wait_for_timeout(2500)
    summary = api_get("/growth/summary?days=7", session_id)
    print(f"  /growth/summary: {json.dumps(summary, ensure_ascii=False)[:400]}")
    seen: dict[str, str] = {}
    for index, tab in enumerate(("배운 어휘", "저장 질문", "미충족 질문", "이번 주"), start=20):
        _button(page, tab).first.click()
        page.wait_for_timeout(2500)
        seen[tab] = page.inner_text("body")
        print(f"  [{tab}] shot: {shoot(page, index).name}")
        check(f"G. ‘{tab}’ 탭이 그려진다", tab in seen[tab])
    if state.get("term"):
        check(f"G. ‘배운 어휘’ 탭에 확정된 용어 ‘{state['term']}’가 보인다",
              state["term"] in seen["배운 어휘"])
    unmet = summary.get("unmet_clusters") or []
    check(f"G. /growth/summary가 미충족 군집을 갖고 있다 (본 것: {len(unmet)}개)", bool(unmet))
    check(f"G. 군집 총계가 목록 길이가 아니라 서버가 센 값이다 "
          f"({summary.get('unmet_clusters_total')} >= {len(unmet)})",
          summary.get("unmet_clusters_total", 0) >= len(unmet))
    week = seen.get("이번 주", "")
    tiles = {name: summary.get(name, 0) for name in ("answered", "partial", "unmet", "action")}
    check(f"G. ‘이번 주’ 탭 숫자가 /growth/summary와 같다 (기대 {tiles})",
          all(str(value) in week for value in tiles.values()))


def step_safety(session_id: str, before: dict[str, Any], clicks: int) -> None:
    jobs = api_get("/jobs?limit=200", session_id)
    total = _audit_total(session_id)
    after_jobs = {job["job_id"]: job.get("status") for job in jobs["items"]}
    delta = total - before["audit"]
    print(f"\n[S] jobs={len(after_jobs)} audit total={total} "
          f"(before {before['audit']}, 승인 클릭 {clicks}회 → 기대 증가 {clicks * 2})")
    check("S. job 상태가 스모크 전후로 동일하다", after_jobs == before["jobs"])
    check(f"S. 감사 로그 증가분이 승인 클릭의 짝과 정확히 같다 ({delta} vs {clicks * 2})",
          delta == clicks * 2)


# -- main --------------------------------------------------------------------------------


def main() -> int:
    global SHOT_PREFIX
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--restart", action="store_true", help="데모 데이터셋 위에 호스트를 다시 띄운다")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--db", default=str(DEMO / "scale.sqlite"))
    parser.add_argument("--phase", type=int, choices=(1, 2), default=1,
                        help="1=질의 엔진 6발화(기본), 2=1단계 회귀 + 어휘·승격·피드백·Growth")
    parser.add_argument("--steps", default="",
                        help="쉼표로 구분한 단계 라벨만 돈다(예: --steps F,G). 고친 단계 하나를 "
                             "다시 재려고 스무 번의 모델 호출을 다시 사지 않기 위한 것")
    parser.add_argument("--keep-shots", action="store_true", help="이전 스크린샷을 지우지 않는다")
    args = parser.parse_args()
    only = {label.strip() for label in args.steps.split(",") if label.strip()}

    SHOT_PREFIX = "growth" if args.phase == 1 else "growth2"
    if args.restart:
        restart_host()
    if not args.keep_shots and SHOTS.exists():
        for old in SHOTS.glob(f"{SHOT_PREFIX}-*.png"):
            old.unlink()
    SHOTS.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=not args.headed)
        page = browser.new_page(viewport={"width": 1600, "height": 1100})
        session_ids: list[str] = []
        page.on("request", lambda request: session_ids.append(request.headers["x-session-id"])
                if request.headers.get("x-session-id") else None)
        page.goto(PORTAL, wait_until="networkidle", timeout=120_000)
        page.get_by_label("운영자 선택").select_option(OPERATOR)
        page.wait_for_timeout(2500)
        session_ids.clear()
        page.reload(wait_until="networkidle", timeout=120_000)
        page.wait_for_timeout(2500)
        if not session_ids:
            raise SystemExit("no X-Session-Id seen on any portal request")
        session_id = session_ids[-1]
        print(f"session: {session_id}  operator: {OPERATOR}")

        before = {
            "jobs": {j["job_id"]: j.get("status") for j in api_get("/jobs?limit=200", session_id)["items"]},
            "audit": api_get("/audit?limit=1", session_id)["total"],
        }
        state: dict[str, Any] = {}
        # 한 단계가 터져도 나머지를 계속 돈다 — 모델 호출은 비싸고, 뒤 단계(감사·ask_log 불변)는
        # 앞 단계와 독립이다. 터진 단계는 product FAIL로 남는다.
        steps: list[tuple[str, Any]] = [
            ("1", lambda: step_1(page, state)),
            ("2", lambda: step_2(page, state)),
            ("2-sqlite", lambda: cross_check_step_2(state, Path(args.db))),
            ("3", lambda: step_3(page, state)),
            ("4", lambda: step_4(page, state)),
            ("5", lambda: step_5(page, state)),
            ("6", lambda: step_6(page, state, session_id)),
            ("7", lambda: step_7(session_id, state.get("asks", 6))),
        ]
        if args.phase == 1:
            steps.append(("8", lambda: step_8(session_id, before)))
        else:
            # 2단계. 위 여섯 발화는 그대로 회귀로 돌고, 그 위에 어휘 → 승격 → 피드백 → Growth를
            # 얹는다. V2가 오퍼레이터를 바꾸며 새 세션을 열므로 그 뒤 라우트 조회는 새 세션 id를
            # 쓴다(둘 다 같은 프로젝트라 /audit·/jobs·/growth 는 어느 쪽으로 읽어도 같다).
            second: dict[str, str] = {}
            steps += [
                ("V1", lambda: step_v1(page, state, session_id)),
                ("V2", lambda: second.update(session=step_v2(page, state) or session_id)),
                ("P", lambda: step_promotion(page, state, second.get("session", session_id),
                                             Path(args.db))),
                ("F", lambda: step_feedback(page, state, second.get("session", session_id))),
                ("G", lambda: step_growth_view(page, state, second.get("session", session_id))),
                ("S", lambda: step_safety(second.get("session", session_id), before,
                                          state.get("clicks", 0))),
            ]
        for label, run in steps:
            if only and label not in only:
                continue
            try:
                run()
            except Exception as failure:                    # noqa: BLE001 — 스모크는 계속 돈다
                check(f"{label}. 단계가 예외 없이 끝난다 ({type(failure).__name__}: {failure})", False)
        browser.close()

    print(f"\n{len(PASSED)} PASS · {len(FAILED_MODEL)} FAIL(model) · {len(FAILED_PRODUCT)} FAIL(product)")
    for line in FAILED_MODEL:
        print(f"  model:   {line}")
    for line in FAILED_PRODUCT:
        print(f"  product: {line}")
    return 1 if FAILED_PRODUCT else 0


if __name__ == "__main__":
    sys.exit(main())
