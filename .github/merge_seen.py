#!/usr/bin/env python3
"""Union-merge two state/seen.json files so a run never clobbers state that
another push (a merge, a parallel run) added to the branch meanwhile.

    python .github/merge_seen.py OURS THEIRS   # writes the union back to OURS

Used by the workflow's persist step when a push is rejected because the remote
moved: we fold the remote's seen.json into ours instead of overwriting it, so
nothing gets re-posted and the live codes-card id survives.
"""
import json
import sys

# Must match the caps in post_feeds.py so the merged file stays bounded.
KEEP_IDS = 300
KEEP_DEDUP = 500
KEEP_DIGEST = 120
RESERVED = {"__dedup__", "__codes_msg__", "__codes__", "__digest_log__"}


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def union(a, b, cap):
    """Ours (a) first — it holds this run's freshest ids — then theirs, deduped."""
    seen, out = set(), []
    for x in list(a or []) + list(b or []):
        key = json.dumps(x, sort_keys=True) if isinstance(x, (dict, list)) else x
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out[:cap] if cap else out


def main():
    ours, theirs = load(sys.argv[1]), load(sys.argv[2])
    out = {}
    for k in set(ours) | set(theirs):
        if k in RESERVED:
            continue
        out[k] = union(ours.get(k), theirs.get(k), KEEP_IDS)      # per-source id lists
    out["__dedup__"] = union(ours.get("__dedup__"), theirs.get("__dedup__"), KEEP_DEDUP)
    out["__digest_log__"] = union(ours.get("__digest_log__"),
                                  theirs.get("__digest_log__"), KEEP_DIGEST)
    # codes: prefer OURS (this run's fresher scrape + the live card message id).
    out["__codes_msg__"] = ours.get("__codes_msg__") or theirs.get("__codes_msg__")
    out["__codes__"] = ours.get("__codes__", theirs.get("__codes__", []))
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
