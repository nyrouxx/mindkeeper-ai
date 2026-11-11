"""Recover legacy calendar links using event identity and recorded provenance.

Never merge plans by their titles or dates alone. Only a recorded reimport of
the exact same remote event may be folded back into its original task.
"""

import re
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256

from .model import InvalidPlan, apply_operations, timestamp

OWNED_ID = re.compile(r"mk[0-9a-f]{32}$")


def calendar_key(calendar_id):
    """Google calendar IDs identify the calendar independently of an HA login."""
    return sha256(f"google-calendar:{calendar_id}".encode()).hexdigest()


def recovery_sources(state, key, legacy_key, remote_ids):
    boards = state.get("google", {})
    owned = {uid for uid in remote_ids if OWNED_ID.fullmatch(uid)}
    sources = {
        k
        for k, board in boards.items()
        if k in (key, legacy_key)
        or (board.get("identity_version", 1) < 2 and owned.intersection(board["records"]))
    }
    return sorted(
        sources,
        key=lambda k: (
            timestamp(boards[k].get("last_success")) or datetime.min.replace(tzinfo=UTC),
            k,
        ),
    )


def recover_links(state, key, sources, observed, now, options, normalize):
    boards = state.get("google", {})
    if sources == [key] and boards[key].get("identity_version") == 2:
        return state, None
    result = deepcopy(state)
    tasks = result["tasks"]
    candidates = {}
    for source in sources:
        for uid, record in boards[source]["records"].items():
            candidates.setdefault(uid, []).append(record)

    aliases = {}

    def original(task_id):
        while task_id in aliases:
            task_id = aliases[task_id]
        return task_id

    for uid, records in candidates.items():
        ids = {original(r["task_id"]) for r in records}
        if len(ids) < 2:
            continue
        if not ids.issubset(tasks):
            raise InvalidPlan("В сохранённых связях календаря не найдены исходные дела")
        canonical = min(
            ids,
            key=lambda task_id: (
                tasks[task_id].get("source", "").startswith("google:"),
                timestamp(tasks[task_id]["created_at"]),
                int(task_id),
            ),
        )
        for duplicate in ids - {canonical}:
            task = tasks[duplicate]
            imported = task.get("source") == "google:" + uid
            untouched = all(
                h.get("source", "").startswith(("google:", "classification:", "auto-complete:"))
                for h in task.get("history", [])
            )
            if not imported or not untouched or normalize(task) != normalize(tasks[canonical]):
                raise InvalidPlan(
                    "У повторно импортированного дела есть отдельные правки. Автоматическое объединение остановлено"
                )
            aliases[duplicate] = canonical
    aliases = {task_id: original(target) for task_id, target in aliases.items()}

    records = {}
    for uid, choices in candidates.items():
        chosen = min(
            choices, key=lambda r: (r["task_id"] != original(r["task_id"]), r.get("base") is None)
        )
        records[uid] = {**deepcopy(chosen), "task_id": original(chosen["task_id"])}

    retired, removed = {}, {}
    for source in sources:
        retired.update(deepcopy(boards[source].get("retired_events", {})))
        removed.update(deepcopy(boards[source].get("removed_duplicates", {})))
    groups = {}
    for uid, record in records.items():
        groups.setdefault(record["task_id"], []).append(uid)
    for task_id, uids in groups.items():
        if len(uids) < 2:
            continue
        present = [uid for uid in uids if observed.get(uid) is not None]
        if present and any(observed[uid] != observed[present[0]] for uid in present):
            raise InvalidPlan(
                "У событий-дублей в Google разные правки. Автоматическая очистка остановлена"
            )
        # Keep the oldest known event; an external original always takes precedence.
        keeper = min(
            present or uids, key=lambda uid: (bool(OWNED_ID.fullmatch(uid)), uids.index(uid))
        )
        for uid in uids:
            if uid == keeper:
                continue
            if not OWNED_ID.fullmatch(uid):
                raise InvalidPlan("Автоматически удалять сторонние события Google нельзя")
            retired[uid] = {
                "task_id": task_id,
                "keep_uid": keeper,
                "expected": deepcopy(observed.get(uid) or records[uid].get("base")),
            }
            del records[uid]

    # All validation above is side-effect free. Archive duplicates only after proof.
    duplicate_ids = list(aliases)
    for offset in range(0, len(duplicate_ids), 20):
        result, _ = apply_operations(
            result,
            [
                {"action": "cancel", "task_id": task_id}
                for task_id in duplicate_ids[offset : offset + 20]
            ],
            now,
            "google-recovery:" + key,
            options,
        )
    for duplicate, canonical in aliases.items():
        result["tasks"][duplicate]["merged_into"] = canonical
    # Other calendars can reference the same local task; preserve those links too.
    for board in result["google"].values():
        for record in board["records"].values():
            record["task_id"] = original(record["task_id"])
    for source in sources:
        del result["google"][source]
    result["google"][key] = {
        "identity_version": 2,
        "records": records,
        "retired_events": retired,
        "removed_duplicates": removed,
    }
    report = {
        "merged_tasks": aliases,
        "retired_events": list(retired),
        "migrated_boards": len(sources),
    }
    if sources:
        result["google"][key]["recovery"] = {**report, "at": now.isoformat()}
    return result, report
