"""superfine 접근 로그 -> MVP 데이터셋 (ApiSpec + RunResult).

    .venv/Scripts/python.exe scripts/import/superfine.py --out data/superfine

내는 것 (scripts/scale/generate.py 와 같은 모양이라 호스트가 그대로 부팅한다):

    <out>/superfine.sqlite  Store.ingest 로 채운 스토어 (롤업·워터마크·current_state 포함)
    <out>/apis.json         로그에서 뽑은 ApiSpec 전부
    <out>/operators.json    로그의 user= 를 운영자 프로필로
    <out>/runs.json         빈 배열 (실행은 스토어가 들고 있다)
    <out>/meta.json         무엇을 몇 건 읽어 무엇을 몇 건 썼는지

띄우기:
    ATWORKS_STORE_PATH=data/superfine/superfine.sqlite
    ATWORKS_FIXTURES_DIR=data/superfine
    .venv/Scripts/python.exe -m atworks_host.main

읽기 전용: 원본(D:/projects/ai_master)에는 아무것도 쓰지 않는다.

매핑 (한 줄 = 한 RunResult):

    route=/api/orders/{id}   -> ApiSpec.path (템플릿이 로그에 그대로 있다)
    GET                      -> ApiSpec.method
    pathVars/query 의 키      -> ApiSpec.params
    -> 200 (2ms)             -> http_status, duration_ms, status
    user=827                 -> executed_by
    res={...}                -> response_body

status 는 HTTP 코드에서 결정론으로 나온다: 2xx/3xx = pass, 4xx = fail, 5xx = error.
모델도 사람도 끼지 않는다 -- MVP 에서 판정은 코드가 하는 일이고, 여기서도 그렇다.

api_id 와 run_id 는 결정론이다: api_id 는 (method, path) 를 정렬한 순서, run_id 는
(timestamp, cid) 를 정렬한 순서. 같은 로그를 다시 넣으면 같은 id 가 나오고,
Store.ingest 는 이미 있는 run_id 를 걸러내므로 재실행이 롤업을 두 번 세지 않는다.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "atworks-agent" / "core"), str(ROOT / "host")]

from atworks_agent import ApiSpec, AtworksAgentConfig, RunResult, RunStatus  # noqa: E402
from atworks_agent.types import ApiField  # noqa: E402
from atworks_agent.masking import mask_body_paths, policy_from_config  # noqa: E402
from atworks_host.store import Store  # noqa: E402

DEFAULT_SOURCE = Path("D:/projects/ai_master")
LOGS = "data/logs/superfine"
BACKEND_SRC = "superfine/superfine-backend/src"

# 한 줄에서 필요한 것만 집는다. headers=/req=/res= 는 중첩 중괄호라 정규식으로 끝을 찾지 않고,
# " res=" 뒤를 줄 끝까지 통째로 가져온다.
LINE = re.compile(
    r"^(?P<ts>\S+) +\w+ +\[[^\]]*\] +\S+ +: +"
    r"(?P<method>[A-Z]+) +\S+ +route=(?P<route>\S+) +-> +(?P<status>\d{3}) +\((?P<ms>\d+)ms\) +"
    r"pathVars=\{(?P<pathvars>[^}]*)\} +query=\{(?P<query>[^}]*)\} +user=(?P<user>\S+)"
)
CID = re.compile(r" cid=(\S+)")
RES = re.compile(r" res=(.*)$")
# req= 는 res= 앞에 온다. 둘 다 중첩 중괄호라 끝을 정규식으로 찾지 않고, 다음 키 앞까지 자른다.
REQ = re.compile(r" req=(.*?)(?= res=|$)")
# "a=1,b=2" 에서 키만. 값에 쉼표가 들어가도 키 자리(쉼표 뒤 또는 처음)만 집으므로 안전하다.
KEY = re.compile(r"(?:^|,)\s*([A-Za-z_][\w.]*)=")
#: "a=1,b=x" -> {"a": "1", "b": "x"}. 경로 변수·쿼리는 로그에 전부 문자열로 찍히므로 타입은
#: 늘 string 이다 -- 숫자처럼 보여도 그렇게 왔다는 사실만 말한다.
PAIR = re.compile(r"(?:^|,)\s*([A-Za-z_][\w.]*)=([^,]*)")


def PAIR_VALUES(text: str) -> dict[str, str]:
    return {name: value for name, value in PAIR.findall(text)}
MAPPING = re.compile(r'@RequestMapping\("([^"]+)"\)')


def _json_object(found: re.Match | None) -> dict | None:
    """`req=`/`res=` 뒤가 JSON 객체일 때만 돌려준다. `-`(본문 없음)이나 배열·스칼라는 None."""
    if found is None:
        return None
    raw = found.group(1).strip()
    if raw in ("", "-"):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def controller_groups(backend_src: Path) -> list[tuple[str, str]]:
    """(base path, group) 목록. group 은 컨트롤러가 사는 패키지 이름이다.

    가장 긴 접두어가 이긴다 -- "/api/admin" (recipe) 이 "/api/admin/banners" (banner) 를
    삼키지 않게 하려면 길이 역순으로 봐야 한다.
    """
    pairs: list[tuple[str, str]] = []
    if not backend_src.exists():
        return pairs
    for path in sorted(backend_src.rglob("*Controller.java")):
        parts = path.parts
        if "api" not in parts:
            continue
        group = parts[parts.index("api") + 1]
        found = MAPPING.search(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            pairs.append((found.group(1), group))
    return sorted(set(pairs), key=lambda pair: len(pair[0]), reverse=True)


def group_for(route: str, pairs: list[tuple[str, str]]) -> str | None:
    for base, group in pairs:
        if route == base or route.startswith(base + "/"):
            return group
    return None


def status_of(http_status: int) -> RunStatus:
    if http_status >= 500:
        return RunStatus.ERROR
    if http_status >= 400:
        return RunStatus.FAIL
    return RunStatus.PASS


def log_files(logs_dir: Path) -> list[Path]:
    return sorted(logs_dir.glob("access*.log*"))


def read_lines(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:  # type: ignore[operator]
        yield from handle


def parse(source: Path, keep_bodies: bool, quiet: bool) -> tuple[list[dict], dict]:
    """로그를 읽어 정규화된 항목 목록으로. 파일 순서가 아니라 (시각, cid) 로 정렬해 돌려준다."""
    entries: list[dict] = []
    seen_lines = 0
    skipped = 0
    for path in log_files(source / LOGS):
        before = len(entries)
        for line in read_lines(path):
            if "ApiAccessLoggingFilter" not in line:
                continue
            seen_lines += 1
            found = LINE.match(line)
            if not found:
                skipped += 1
                continue
            route = found.group("route")
            if not route.startswith("/"):
                skipped += 1      # "route=-" 같은 줄: 어느 엔드포인트인지 모른다
                continue
            body = request = None
            if keep_bodies:
                body = _json_object(RES.search(line))
                request = _json_object(REQ.search(line))
            cid = CID.search(line)
            entries.append({
                "ts": found.group("ts"),
                "cid": cid.group(1) if cid else "",
                "method": found.group("method"),
                "route": route,
                "http_status": int(found.group("status")),
                "ms": int(found.group("ms")),
                "user": found.group("user"),
                "params": [*KEY.findall(found.group("pathvars")), *KEY.findall(found.group("query"))],
                "param_values": {**PAIR_VALUES(found.group("pathvars")), **PAIR_VALUES(found.group("query"))},
                "body": body,
                "request": request,
            })
        if not quiet:
            print(f"  {path.name}: {len(entries) - before:,}건", flush=True)
    entries.sort(key=lambda row: (row["ts"], row["cid"]))
    return entries, {"log_lines": seen_lines, "skipped": skipped}


def json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def observe(slot: dict, location: str, name: str, value: object) -> None:
    """한 호출에서 본 필드 하나를 누적한다. 타입은 **본 것을 전부** 모은다 -- 같은 필드가
    어떤 호출에서는 값이고 어떤 호출에서는 null 이면 그 사실 자체가 스펙의 일부다."""
    field = slot["fields"].setdefault((location, name), {"types": set(), "item": set(), "seen": 0})
    field["seen"] += 1
    field["types"].add(json_type(value))
    if isinstance(value, list) and value:
        field["item"].update(json_type(item) for item in value)


def build(entries: list[dict], pairs: list[tuple[str, str]]) -> tuple[list[ApiSpec], dict[tuple[str, str], str]]:
    """엔드포인트별로 ApiSpec 하나.

    필드는 **관측**이다. 이 배포에는 OpenAPI 문서가 없으므로 선언된 스펙이란 게 없고, 아는
    것은 실제로 오간 요청·응답뿐이다. 그래서 위치별 호출 수(`of`)와 그 필드를 본 횟수(`seen`)
    를 같이 싣는다 -- 둘이 같으면 그 위치의 모든 호출에 있었다는 뜻이고, 그게 "필수로 보인다"
    고 말할 수 있는 최대치다.
    """
    seen: dict[tuple[str, str], dict] = {}
    for row in entries:
        key = (row["method"], row["route"])
        slot = seen.setdefault(key, {
            "params": set(), "last": row["ts"], "fields": {}, "calls": 0, "with_request": 0, "with_response": 0,
        })
        slot["calls"] += 1
        slot["params"].update(row["params"])
        for name in row["params"]:
            # 경로 변수인지 쿼리인지는 이름이 경로 템플릿에 들어 있는지로 가른다.
            location = "path" if "{" + name + "}" in row["route"] else "query"
            observe(slot, location, name, row["param_values"].get(name))
        if row["request"]:
            slot["with_request"] += 1
            # 요청 본문의 최상위 키도 그 API 가 받는 입력이다. 이게 없으면 POST 계열 엔드포인트는
            # 파라미터 0건으로 보이고, 검증 규칙을 걸 대상 자체가 없게 된다.
            slot["params"].update(k for k in row["request"] if isinstance(k, str))
            for name, value in row["request"].items():
                if isinstance(name, str):
                    observe(slot, "body", name, value)
        if row["body"]:
            slot["with_response"] += 1
            for name, value in row["body"].items():
                if isinstance(name, str):
                    observe(slot, "response", name, value)
        slot["last"] = max(slot["last"], row["ts"])
    ids: dict[tuple[str, str], str] = {}
    specs: list[ApiSpec] = []
    for index, key in enumerate(sorted(seen), start=1):
        method, route = key
        api_id = f"api-{index:03d}"
        ids[key] = api_id
        specs.append(ApiSpec(
            api_id=api_id, method=method, path=route,
            # 이름은 지어내지 않는다. 로그에 사람이 붙인 이름이 없으므로 경로가 곧 이름이다.
            name=f"{method} {route}",
            group=group_for(route, pairs),
            updated_at=datetime.fromisoformat(seen[key]["last"]),
            has_rules=False,
            params=sorted(seen[key]["params"]),
            fields=build_fields(seen[key]),
        ))
    return specs, ids


#: 위치별로 "전체 호출 수"가 다르다 -- 본문은 그것이 있었던 호출만 분모가 된다.
OF_BY_LOCATION = {"path": "calls", "query": "calls", "body": "with_request", "response": "with_response"}


def build_fields(slot: dict) -> list[ApiField]:
    fields: list[ApiField] = []
    order = {"path": 0, "query": 1, "body": 2, "response": 3}
    for (location, name), found in sorted(slot["fields"].items(), key=lambda kv: (order[kv[0][0]], kv[0][1])):
        item_types = sorted(found["item"])
        fields.append(ApiField(
            name=name, location=location,
            types=sorted(found["types"]),
            item_type=item_types[0] if len(item_types) == 1 else ("mixed" if item_types else None),
            seen=found["seen"], of=slot[OF_BY_LOCATION[location]],
        ))
    return fields


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="ai_master 체크아웃 (읽기 전용)")
    parser.add_argument("--out", type=Path, default=Path("data/superfine"))
    parser.add_argument("--batch", type=int, default=20_000, help="Store.ingest 한 번에 넣을 run 수")
    parser.add_argument("--env", default="dev", help="RunResult.target_env (로그는 서버 하나에서 나왔다)")
    parser.add_argument("--no-bodies", action="store_true", help="응답 본문을 넣지 않는다")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    keep_bodies = not args.no_bodies

    if not args.quiet:
        print(f"읽는 중: {args.source / LOGS}")
    entries, stats = parse(args.source, keep_bodies, args.quiet)
    pairs = controller_groups(args.source / BACKEND_SRC)
    specs, ids = build(entries, pairs)
    ungrouped = sorted({spec.path for spec in specs if spec.group is None})

    store_path = out / "superfine.sqlite"
    store_path.unlink(missing_ok=True)
    store = Store(store_path)
    store.load_apis(specs)

    policy = policy_from_config(AtworksAgentConfig(model="superfine"))
    runs: list[RunResult] = []
    ingested = 0
    status_counts = {"pass": 0, "fail": 0, "error": 0}
    operators: dict[str, int] = {}

    def flush() -> None:
        nonlocal runs, ingested
        if not runs:
            return
        ingested += store.ingest(runs, mask=lambda body: mask_body_paths(body, policy))
        runs = []
        if not args.quiet:
            print(f"  ... {ingested:,}건 적재 ({time.perf_counter() - started:.1f}s)", flush=True)

    for index, row in enumerate(entries, start=1):
        at = datetime.fromisoformat(row["ts"])
        status = status_of(row["http_status"])
        status_counts[status.value] += 1
        operator = f"user-{row['user']}" if row["user"].isdigit() else "anonymous"
        operators[operator] = operators.get(operator, 0) + 1
        runs.append(RunResult(
            run_id=f"run-{index:06d}",
            api_id=ids[(row["method"], row["route"])],
            executed_at=at,
            target_env=args.env,
            status=status,
            failed_rules=[],     # 검증 규칙이 아직 없다. HTTP 코드만으로 판정했다.
            http_status=row["http_status"],
            duration_ms=row["ms"],
            response_body=row["body"],
            request_body=row["request"],
            executed_by=operator,
            day=at.date().isoformat(),
            api_method=row["method"],
            api_path=row["route"],
        ))
        if len(runs) >= args.batch:
            flush()
    flush()

    api_rows = [json.loads(spec.model_dump_json()) for spec in specs]
    operator_rows = [
        {"operator_id": name, "name": name, "role": "qa"}
        for name, _count in sorted(operators.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    (out / "apis.json").write_text(json.dumps(api_rows, ensure_ascii=False), encoding="utf-8")
    (out / "operators.json").write_text(json.dumps(operator_rows, ensure_ascii=False), encoding="utf-8")
    (out / "runs.json").write_text("[]", encoding="utf-8")
    summary = {
        "source": str(args.source), "log_lines": stats["log_lines"], "skipped": stats["skipped"],
        "apis": len(specs), "runs_ingested": ingested, "status": status_counts,
        "operators": len(operator_rows), "bodies": keep_bodies,
        "ungrouped_paths": ungrouped,
        "elapsed_s": round(time.perf_counter() - started, 1),
    }
    (out / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
