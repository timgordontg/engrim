#!/usr/bin/env python3
"""Three operations on an engrim store, with the standard library's sqlite3.

    store.py count DB           print "N records, M active"
    store.py capture SRC DST    a consistent copy through sqlite's online backup
                                API, safe while the MCP server still holds SRC
    store.py retire DB          set every active resume-pointer to done

When the store sits on the Docker daemon's filesystem (a dind runner) or its
owner is the server's, run this inside the server's image with the script on
stdin, so nothing needs mounting:

    docker run -i --rm -v /tmp/gh-aw/engrim:/dtmp python:3.13.15-alpine3.24 \
      python - count /dtmp/memory.db < store.py
"""
import os
import sqlite3
import sys


def counts(conn):
    total = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
    active = conn.execute("SELECT count(*) FROM memories WHERE status = 'active'").fetchone()[0]
    return f"{total} records, {active} active"


def main(argv):
    op, args = (argv[0] if argv else ""), argv[1:]
    if op == "count" and len(args) == 1:
        conn = sqlite3.connect(args[0])
        print(counts(conn))
        conn.close()
    elif op == "capture" and len(args) == 2:
        src, dst = args
        if not os.path.exists(src):
            print("no store to capture")
            return 0
        source, copy = sqlite3.connect(src), sqlite3.connect(dst)
        source.backup(copy)
        source.close()
        print("captured", counts(copy))
        copy.close()
    elif op == "retire" and len(args) == 1:
        # A resume-pointer describes a working tree that no longer exists once
        # its job is over. engrim pins the newest active one in the boot pack
        # and never retires one itself, so the merge workflow does, here.
        conn = sqlite3.connect(args[0])
        retired = conn.execute(
            "UPDATE memories SET status = 'done' "
            "WHERE status = 'active' AND tags LIKE '%resume-pointer%'").rowcount
        conn.commit()
        print(f"retired {retired} resume-pointer(s); now {counts(conn)}")
        conn.close()
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
