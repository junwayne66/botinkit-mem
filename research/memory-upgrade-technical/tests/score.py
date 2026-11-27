"""Validate the case catalogue or compare captured observations against its checks."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parent
GROUPS = ("F01", "F02", "F03", "F04", "F05", "F06", "F07", "X01", "E2E")
MISSING = object()


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    def invalid_constant(token):
        raise ValueError(f"invalid JSON number: {token}")

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys,
                      parse_constant=invalid_constant)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_case(case, group):
    case_id = case["id"]
    require(re.fullmatch(group + r"-[0-9]{2}", case_id), f"invalid ID {case_id}")
    require(isinstance(case["title"], str) and case["title"].strip(), "missing title")
    tags = case["covers"]
    require(isinstance(tags, list) and tags and len(tags) == len(set(tags))
            and all(re.fullmatch(r"[a-z][a-z0-9_]*", t) for t in tags), "invalid coverage tags")
    require(isinstance(case["fixture"], dict) and case["fixture"], "empty fixture")
    require(isinstance(case["steps"], list) and case["steps"], "empty steps")
    steps = set()
    for step in case["steps"]:
        name = step["id"]
        require(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) and name not in steps,
                f"invalid or duplicate step {name}")
        require(isinstance(step["action"], str) and step["action"].strip()
                and isinstance(step["args"], dict), f"invalid action or arguments for {name}")
        steps.add(name)
    checks = case["checks"]
    require(isinstance(checks, list) and len(checks) >= 2, "need at least two checks")
    for check in checks:
        require(check["at"] in steps, f"unknown step {check['at']}")
        path, op = check["path"], check["op"]
        require(path.startswith("/") and not re.search(r"~(?![01])", path), f"bad pointer {path}")
        require(op in ("eq", "contains", "excludes", "absent"), f"unknown operator {op}")
        require(("value" in check) == (op != "absent"), f"invalid value for {op}")


def catalogue():
    cases = {}
    for group in GROUPS:
        rows = read_json(DIRECTORY / f"{group}.json")
        require(isinstance(rows, list) and len(rows) >= (1 if group == "E2E" else 10),
                f"{group}: insufficient samples")
        titles = set()
        fixtures = set()
        for n, row in enumerate(rows, 1):
            try:
                validate_case(row, group)
                fixture_key = json.dumps(row["fixture"], sort_keys=True, ensure_ascii=False)
                require(row["id"] not in cases and row["title"] not in titles, "duplicate ID/title")
                # Different exercises may use the same starting data with different operations.
                scenario = (fixture_key, json.dumps(row["steps"], sort_keys=True, ensure_ascii=False))
                require(scenario not in fixtures, "duplicate fixture and actions")
                cases[row["id"]] = row
                titles.add(row["title"])
                fixtures.add(scenario)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{group} row {n}: {exc}") from exc
    return cases


def member(value, key):
    if isinstance(value, dict):
        return value.get(key, MISSING)
    if isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < len(value):
        return value[int(key)]
    return MISSING


def equal(left, right):
    # Python otherwise treats true and 1 as equal even inside arrays or objects.
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal(left[k], right[k]) for k in left)
    return left == right


def evaluate(observed_step, check):
    path = [k.replace("~1", "/").replace("~0", "~") for k in check["path"][1:].split("/")]
    parent = observed_step
    for key in path[:-1]:
        parent = member(parent, key)
    if not isinstance(parent, (dict, list)):
        return False
    if isinstance(parent, list) and not re.fullmatch(r"0|[1-9][0-9]*", path[-1]):
        return False
    actual = member(parent, path[-1])
    op = check["op"]
    if op == "absent":
        return actual is MISSING
    if actual is MISSING:
        return False
    expected = check["value"]
    if op == "eq":
        return equal(actual, expected)
    if isinstance(actual, list):
        found = any(equal(entry, expected) for entry in actual)
    elif isinstance(actual, str) and isinstance(expected, str):
        found = expected in actual
    else:
        return False
    return found if op == "contains" else not found


def grade(case, observed):
    if observed is MISSING:
        return "NOT_RUN", ["no observations"]
    if not isinstance(observed, dict):
        return "FAIL", ["case observations must be keyed by step"]
    problems = []
    for step in case["steps"]:
        if not isinstance(observed.get(step["id"]), dict):
            problems.append(f"{step['id']}: missing or malformed step observation")
    for check in case["checks"]:
        if not evaluate(observed.get(check["at"]), check):
            problems.append(f"{check['at']}{check['path']} {check['op']} "
                            + json.dumps(check.get("value", ""), ensure_ascii=False))
    return ("FAIL" if problems else "PASS"), problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=GROUPS)
    parser.add_argument("--case", help="select one case ID")
    parser.add_argument("--list", action="store_true", help="print coverage tags per case")
    parser.add_argument("--observations", type=Path, help="grade a JSON case → step → observation map")
    args = parser.parse_args()
    cases = catalogue()
    selected = [case for cid, case in cases.items()
                if (args.group is None or cid.startswith(args.group + "-"))
                and (args.case is None or cid == args.case)]
    require(selected, "no matching case")
    if args.observations is None:
        for case in selected:
            if args.list:
                print(case["id"], case["title"], ", ".join(case["covers"]), sep="  ")
        print(f"VALID: {len(selected)} cases, {sum(len(c['checks']) for c in selected)} checks (catalogue only)")
        return 0
    observations = read_json(args.observations)
    require(isinstance(observations, dict), "expected JSON case → step → observation map")
    require(not observations.keys() - cases.keys(), "observations contain an unknown case ID")
    summary = Counter()
    for case in selected:
        status, details = grade(case, observations.get(case["id"], MISSING))
        summary[status] += 1
        print(status, case["id"], case["title"])
        for detail in details:
            print(f"  {detail}")
    print(f"PASS={summary['PASS']} FAIL={summary['FAIL']} NOT_RUN={summary['NOT_RUN']}")
    return int(summary["PASS"] != len(selected))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}")
        raise SystemExit(2)
