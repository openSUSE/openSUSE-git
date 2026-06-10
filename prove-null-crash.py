#!/usr/bin/env python3
"""
prove-null-crash.py: Proof of Concept demonstrating how a crash after creating a pending WAL
entry but before updating it with pre/post SHAs causes state table corruption in git-wal.py.
"""

import os
import sys
import psycopg

# Import the git-wal module
git_wal = __import__("git-wal")

def print_banner(msg):
    print("\n" + "=" * 80)
    print(f" {msg}")
    print("=" * 80)

def main():
    print_banner("INITIALIZING SYSTEM ENVIRONMENT")
    # Initialize pristine environment (starts postgres, daemon, creates tables and repos)
    git_wal.setup_environment()
    
    manager = git_wal.SystemManager()
    
    # Verify initial state
    print("\nInitial DB and Git states:")
    manager.print_db_state()
    
    print_banner("SIMULATING MID-RIFT CRASH (WAL PENDING, SHAS NULL)")
    
    # We insert a pending WAL transaction directly, but with NULL pre_sha and post_sha.
    # This exactly mimics a crash right after the initial WAL insertion.
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("INSERT INTO wal (state) VALUES ('pending') RETURNING tx_id")
    tx_id = c.fetchone()[0]
    for repo in git_wal.REPOS:
        c.execute("INSERT INTO wal_entries (tx_id, repo, pre_sha, post_sha) VALUES (%s, %s, %s, %s)",
                  (tx_id, repo, None, None))
    conn.commit()
    conn.close()
    
    print(f"Manually injected crashed pending transaction ID: {tx_id} with NULL SHAs.")
    manager.print_db_state()
    
    print_banner("TRIGGERING HEALING PROCESS")
    # Executing any new transaction will detect the pending WAL transaction and heal it.
    print("[Manager] Running a new atomic push to trigger on-demand healing...")
    try:
        res = manager.do_atomic_push(["repo1"], "A new valid commit")
        print(f"[Manager] do_atomic_push finished. Result: {res}")
    except Exception as e:
        print(f"[Manager] CRITICAL: healing failed with exception: {e}")
        
    print_banner("EVALUATING FINAL INCONSISTENT STATE")
    
    # Let's inspect the database state
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("SELECT repo, sha FROM state ORDER BY repo")
    rows = c.fetchall()
    conn.close()
    
    print("Post-Healing State Table:")
    has_null_corruption = False
    for repo, sha in rows:
        print(f"  {repo}: {sha}")
        if sha is None or sha == "None":
            has_null_corruption = True
            
    # Clean up postgres
    git_wal.stop_postgres()
    
    if has_null_corruption:
        print("\nFAILURE: SCM-DB State Table was corrupted with NULL values!")
        print("This proves the consensus protocol is not crash-resilient for mid-rift crashes.")
        sys.exit(1)
    else:
        print("\nSUCCESS: SCM-DB State Table was not corrupted.")
        sys.exit(0)

if __name__ == "__main__":
    main()
