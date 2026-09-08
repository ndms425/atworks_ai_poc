#!/usr/bin/env python3
"""자가발전(self-growth) 라이브 스모크 — 실제 모델 + 6만 run 데모 데이터셋, 한 페이지·한 세션.

읽는 방식은 테스트와 같다: 각 발화 뒤에 ``check(설명, 조건)``이 PASS/FAIL을 찍고, 마지막에 합계를
낸다. 실패에는 두 종류가 있고 종료 코드는 그중 하나만 본다 —

* FAIL(product)  호스트/카드/라우트가 잘못된 것. 종료 코드 1. 고치고 다시 돈다.
* FAIL(model)    모델이 다르게 답한 것(기대한 도구 대신 다른 도구를 썼다든지). 한 번 더 명확한
  문장으로 물어보고, 그래도 빗나가면 정직하게 기록만 하고 넘어간다 — 제품 버그가 아니다.

숫자는 이 스크립트가 계산하지 않는다: 2번 발화의 카드 숫자는 데모 SQLite를 직접 GROUP BY 해서
맞는지 대조한다(``--db``). 화면은 발화마다 ``scripts/smoke/shots/growth-NN.png``으로 남는다.

    <scratchpad>/pw/Scripts/python.exe scripts/smoke/growth_shots.py [--restart]

필요한 패키지: ``playwright`` + ``tzdata`` (Windows에는 시스템 tz 데이터베이스가 없어서
``ZoneInfo("Asia/Seoul")``이 tzdata 없이는 못 뜬다 — SQLite 대조의 날 단위 창이 그걸 쓴다).

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

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "scripts" / "smoke" / "shots"
DEMO = ROOT / "scripts" / "scale" / "out" / "demo"
PORTAL = "http://localhost:3110"
HOST = "http://127.0.0.1:8010"
TZ = "Asia/Seoul"
DEFAULT_WINDOW_DAYS = 30          # AtworksAgentConfig.max_aggregate_window_days
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
    "path_prefix_2": "a.path_prefix_2",
}


def sqlite_segment_non_pass(db: Path, dimension: str, filters: dict[str, Any]) -> list[tuple[str, int, int]]:
    """``(그룹키, non_pass, runs)`` 내림차순 — host의 query_sql이 쓰는 것과 같은 날 단위 로컬 창
    (``runs.day``, briefing_tz)과 같은 필터를 원본 테이블 위에 손으로 다시 만든다. 이 함수가
    카드 숫자의 유일한 검증 기준이다: 모델도, 카드도 아니라 runs ⋈ apis다.

    ``status`` 필터는 일부러 무시한다 — non_pass는 pass 행이 0을 더하므로 status=non_pass가
    있든 없든 그룹별 non_pass 값이 같다. 나머지 필터는 SQL로 옮기고, 옮길 수 없는 필터가 하나라도
    있으면 호출자가 대조를 포기한다(``cross_check_step_2``)."""
    zone = ZoneInfo(TZ)
    now = datetime.now(zone)
    window_days = int(filters.get("window_days") or DEFAULT_WINDOW_DAYS)
    day_from = _ceil_local_day(now - timedelta(days=window_days), zone).isoformat()
    day_to = (_ceil_local_day(now, zone) - timedelta(days=1)).isoformat()
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

    def spec(self, component: str = "query_table") -> dict[str, Any]:
        card = self.card(component)
        if card is None or not card.get("spec"):
            return {}
        try:
            return json.loads(card["spec"])
        except json.JSONDecodeError:
            return {}

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


def shoot(page: Page, index: int) -> Path:
    path = SHOTS / f"growth-{index:02d}.png"
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
    check("2. 스펙이 경로 축(path_segment_1/2 또는 path_prefix_2)을 쓴다",
          any(d.startswith("path_") for d in dimensions), kind="model")
    check('2. 답이 "지원하지 않습니다"로 끝나지 않는다', not NO_SUPPORT.search(turn.text))
    state["step2"] = (card, spec)
    state["asks"] = state.get("asks", 0) + 1


def cross_check_step_2(state: dict[str, Any], db: Path) -> None:
    """카드 숫자 ↔ SQLite. 축이 path_segment_2이고 필터가 창뿐일 때만 대조할 수 있다 —
    모델이 다른 축이나 추가 필터를 골랐으면 대조를 건너뛰고 그렇게 적는다."""
    card, spec = state.get("step2", (None, {}))
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
    truth = sqlite_segment_non_pass(db, dimensions[0], filters)
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
    return [row for row in page["items"]
            if row.get("session_id") == session_id and (row.get("seq") or 0) > before_seq]


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
    check(f"6. unmet_reason ∈ no_evidence|no_dimension (본 것: {sorted(r for r in reasons if r)})",
          bool(reasons & {"no_evidence", "no_dimension"}), kind="model")
    # 여기서는 "…하지 않습니다"가 규칙 위반이 아니라 정답이다 — note_unmet_ask를 부른 뒤
    # 무엇이 있어야 답할 수 있는지 말하는 것이 §5 규칙 (2)가 요구하는 모양이다.
    check("6. 답이 그 데이터가 없다고 인정한다",
          bool(re.search(r"않습니다|않는다|없|불가|미보유", turn.text)), kind="model")
    state["step6_reply"] = turn.first_line


def step_7(session_id: str, asked: int) -> None:
    log = api_get("/ask-log?limit=200", session_id)
    rows = [row for row in log["items"] if row.get("session_id") == session_id]
    outcomes = {row.get("outcome") for row in rows}
    print(f"\n[7] /ask-log: total={log['total']} 이 세션 {len(rows)}행 "
          f"outcomes={sorted(o for o in outcomes if o)}")
    check(f"7. 이 세션 ask_log ≥ {asked}행 (본 것: {len(rows)})", len(rows) >= asked)
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
    # GrowthSummary는 타일 셋(answered/partial/unmet)만 센다 -- action은 타일이 아니므로 합은
    # asks_total에서 action 개수만큼 모자란다. 창(7일) 안에 로그가 오늘치뿐일 때 성립한다.
    tiles = {k: summary.get(k, 0) for k in ("answered", "partial", "unmet")}
    check(f"7. growth/summary 합이 맞는다 ({tiles} + action {per_outcome['action']} "
          f"vs asks_total {summary.get('asks_total')})",
          sum(tiles.values()) + per_outcome["action"] == summary.get("asks_total"))


def step_8(session_id: str, before: dict[str, Any]) -> None:
    jobs = api_get("/jobs?limit=200", session_id)
    audit = api_get("/audit?limit=1", session_id)
    after_jobs = {j["job_id"]: j.get("status") for j in jobs["items"]}
    print(f"\n[8] jobs={len(after_jobs)} audit total={audit['total']} "
          f"(before: jobs={len(before['jobs'])} audit={before['audit']})")
    check("8. job 상태가 채팅 전후로 동일하다", after_jobs == before["jobs"])
    check("8. 채팅은 감사 로그를 한 줄도 쓰지 않는다", audit["total"] == before["audit"])


# -- main --------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--restart", action="store_true", help="데모 데이터셋 위에 호스트를 다시 띄운다")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--db", default=str(DEMO / "scale.sqlite"))
    parser.add_argument("--keep-shots", action="store_true", help="이전 스크린샷을 지우지 않는다")
    args = parser.parse_args()

    if args.restart:
        restart_host()
    if not args.keep_shots and SHOTS.exists():
        for old in SHOTS.glob("growth-*.png"):
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
        for label, run in (
            ("1", lambda: step_1(page, state)),
            ("2", lambda: step_2(page, state)),
            ("2-sqlite", lambda: cross_check_step_2(state, Path(args.db))),
            ("3", lambda: step_3(page, state)),
            ("4", lambda: step_4(page, state)),
            ("5", lambda: step_5(page, state)),
            ("6", lambda: step_6(page, state, session_id)),
            ("7", lambda: step_7(session_id, state.get("asks", 6))),
            ("8", lambda: step_8(session_id, before)),
        ):
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
