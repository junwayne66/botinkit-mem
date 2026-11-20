"""Reproduce the report's session-evidence retrieval experiment on LongMemEval_S."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from bmem.store import _fts_match_expression, _fts_tokens


CLEANED_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
METHODS = ("recent", "raw_session", "bmem_session", "bmem_turn")


@lru_cache(maxsize=4096)
def index_terms(body):
    return " ".join(_fts_tokens(body))


def rank_sessions(connection, table, query):
    rows = connection.execute(
        f"SELECT session_id, rowid FROM {table} WHERE {table} MATCH ? "
        f"ORDER BY bm25({table}), rowid DESC", (query,)
    )
    ranked = {}
    for session_id, rowid in rows:
        if session_id not in ranked:
            ranked[session_id] = rowid
            if len(ranked) == 5:
                break
    return ranked


def retrieve(question):
    sessions = question["haystack_session_ids"]
    query = _fts_match_expression(question["question"])
    rankings = {"recent": sessions[::-1][:5]}
    with closing(sqlite3.connect(":memory:")) as connection:
        for table in METHODS[1:]:
            connection.execute(
                f"CREATE VIRTUAL TABLE {table} USING fts5("
                """session_id UNINDEXED, body, tokenize="unicode61 tokenchars '._-/@:'")"""
            )
        for session_id, turns in zip(sessions, question["haystack_sessions"], strict=True):
            messages = [turn["content"] for turn in turns]
            body = "\n".join(messages)
            connection.execute("INSERT INTO raw_session VALUES (?,?)", (session_id, body))
            connection.execute("INSERT INTO bmem_session VALUES (?,?)", (session_id, index_terms(body)))
            connection.executemany(
                "INSERT INTO bmem_turn VALUES (?,?)",
                ((session_id, index_terms(message)) for message in messages),
            )
        for table in METHODS[1:]:
            best_rows = rank_sessions(connection, table, query)
            rankings[table] = list(best_rows)
            if table == "bmem_turn":
                best_turns = best_rows
    return rankings, best_turns


def score_winning_turns(question, gold, best_turns, counts):
    marked = defaultdict(set)
    rowid = 0
    for session_id, turns in zip(question["haystack_session_ids"], question["haystack_sessions"], strict=True):
        for turn in turns:
            rowid += 1
            if turn.get("has_answer") is True:
                marked[session_id].add(rowid)
    if not gold.issubset(marked):
        return
    supported = {sid for sid in gold if best_turns.get(sid) in marked[sid]}
    counts["n"] += 1
    counts["all_gold_sessions_in_top5"] += gold.issubset(best_turns)
    counts["all_winning_turns_marked"] += gold.issubset(supported)
    counts["any_winning_turn_marked"] += bool(supported)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    with args.dataset.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != CLEANED_SHA256:
            parser.error("expected the cleaned S file identified in the report")
    questions = json.loads(args.dataset.read_bytes())
    groups = defaultdict(lambda: defaultdict(Counter))
    paired = Counter()
    excluded = Counter()
    winning_turns = Counter()
    for question in questions:
        if question["question_id"].endswith("_abs"):
            excluded["abstention"] += 1
            continue
        gold = set(question["answer_session_ids"])
        if not gold or not gold.issubset(question["haystack_session_ids"]):
            excluded["missing_gold"] += 1
            continue
        rankings, best_turns = retrieve(question)
        score_winning_turns(question, gold, best_turns, winning_turns)
        for method, ranked in rankings.items():
            for group in ("all", question["question_type"]):
                counts = groups[group][method]
                counts["n"] += 1
                counts["hit1"] += bool(gold.intersection(ranked[:1]))
                counts["hit5"] += bool(gold.intersection(ranked))
                counts["all5"] += gold.issubset(ranked)
                counts["feasible_all5"] += len(gold) <= 5
        baseline = gold.issubset(rankings["bmem_session"])
        by_turn = gold.issubset(rankings["bmem_turn"])
        paired[f"session={int(baseline)},turn={int(by_turn)}"] += 1
    print(json.dumps({"input": len(questions), "excluded": excluded, "groups": groups,
                      "paired_all5": paired, "winning_turns": winning_turns}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
