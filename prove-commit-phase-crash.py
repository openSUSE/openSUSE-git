#!/usr/bin/env python3
"""
prove-commit-phase-crash.py: Proof of Concept demonstrating the Commit Phase Crash Vulnerability.

This test simulates a hard crash of the transaction coordinator during the SCM fast-forward phase.
Specifically, it occurs after the PostgreSQL transaction has committed the state as 'committed',
and the coordinator has successfully fast-forwarded 'repo1', but crashes BEFORE it can
fast-forward 'repo3'.

We verify that:
1. SCM and DB states permanently diverge (repo3 database state is updated but physical repo3 Git is stale).
2. The self-healing protocol successfully detects and resolves this divergence during subsequent runs because
   our patch added support for rolling forward incomplete committed transactions.
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
    
    print_banner("SIMULATING COMMIT-PHASE CRASH DURING FAST-FORWARD")
    
    # Capture initial SHAs
    initial_repo1_sha = manager.get_git_sha("repo1")
    initial_repo3_sha = manager.get_git_sha("repo3")
    
    # Intercept update_git_ref to simulate a crash (exception) right after repo1 is updated,
    # but before repo3 can be updated.
    original_update_git_ref = manager.update_git_ref
    
    repo1_updated = False
    
    def custom_update_git_ref(repo_name, sha, ref_name="refs/heads/main", old_sha=None):
        nonlocal repo1_updated
        if repo_name == "repo1":
            print(f"[PATCH] Fast-forwarding physical {repo_name} to {sha[:8]}... (Success)")
            original_update_git_ref(repo_name, sha, ref_name, old_sha)
            repo1_updated = True
        elif repo_name == "repo3" and repo1_updated:
            print(f"[PATCH] CRASH SIMULATION: Coordinator process killed/crashed before {repo_name} could be fast-forwarded!")
            raise RuntimeError("CRASH SIMULATION: Process crashed during fast-forward phase!")
        else:
            original_update_git_ref(repo_name, sha, ref_name, old_sha)

    manager.update_git_ref = custom_update_git_ref

    # Execute atomic push targeting ['repo1', 'repo3']
    print("[Manager] Running atomic push targeting ['repo1', 'repo3']...")
    try:
        manager.do_atomic_push(["repo1", "repo3"], "Commit with Commit-Phase Crash")
    except Exception as e:
        print(f"[Manager] Expected exception caught: {e}")

    print_banner("EVALUATING INCONSISTENT / DIVERGED STATE")
    
    # Let's inspect database state
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("SELECT repo, sha FROM state WHERE repo = 'repo1'")
    db_repo1_sha = c.fetchone()[1]
    
    c.execute("SELECT repo, sha FROM state WHERE repo = 'repo3'")
    db_repo3_sha = c.fetchone()[1]
    
    c.execute("SELECT tx_id, state FROM wal ORDER BY tx_id DESC LIMIT 1")
    last_wal = c.fetchone()
    conn.close()
    
    # Let's inspect physical SCM repository states
    git_repo1_sha = manager.get_git_sha("repo1")
    git_repo3_sha = manager.get_git_sha("repo3")
    
    print("\n--- WAL TRANSACTION LOG ---")
    if last_wal:
        print(f"  TX {last_wal[0]}: {last_wal[1]}")
    else:
        print("  No transactions found.")
        
    print("\n--- STATE COMPARISON (POST-CRASH) ---")
    print(f"Database 'state' record for repo1: {db_repo1_sha[:8] if db_repo1_sha else 'None'}")
    print(f"Physical Git HEAD for repo1:       {git_repo1_sha[:8] if git_repo1_sha else 'None'}")
    print(f"Database 'state' record for repo3: {db_repo3_sha[:8] if db_repo3_sha else 'None'}")
    print(f"Physical Git HEAD for repo3:       {git_repo3_sha[:8] if git_repo3_sha else 'None'}")

    # Verify that the database thinks repo3 is updated, but physical Git is still at initial SHA
    db_updated_repo3 = (db_repo3_sha != initial_repo3_sha)
    git_updated_repo3 = (git_repo3_sha != initial_repo3_sha)
    
    diverged_immediately = (db_updated_repo3 and not git_updated_repo3)
    if diverged_immediately:
        print("\n[ALERT] Permanent SCM-DB State Divergence detected for repo3!")
        print(f"  Database 'state' has moved to: {db_repo3_sha[:8]}")
        print(f"  Physical Git 'main' is stuck at: {git_repo3_sha[:8]}")
    else:
        print("\n[ALERT] No immediate divergence detected.")

    print_banner("SUBSEQUENT OPERATION & ATTEMPT TO HEAL")
    
    # Restore the original update_git_ref so future operations aren't artificially blocked
    manager.update_git_ref = original_update_git_ref
    
    # Run a subsequent push to see if on-demand self-healing can detect and resolve this divergence.
    # We push a new commit targeting ['repo3'] to check if it heals and finishes the commit first.
    print("[Manager] Running a subsequent atomic push to 'repo3'...")
    try:
        manager.do_atomic_push(["repo3"], "Subsequent Commit")
    except Exception as e:
        print(f"[Manager] Subsequent push failed: {e}")
        
    # Re-evaluate final state
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("SELECT repo, sha FROM state WHERE repo = 'repo3'")
    db_repo3_sha_final = c.fetchone()[1]
    conn.close()
    
    git_repo3_sha_final = manager.get_git_sha("repo3")
    
    print("\n--- FINAL STATE COMPARISON ---")
    print(f"Database 'state' record for repo3: {db_repo3_sha_final[:8] if db_repo3_sha_final else 'None'}")
    print(f"Physical Git HEAD for repo3:       {git_repo3_sha_final[:8] if git_repo3_sha_final else 'None'}")
    
    # Check if we diverged or suffered data corruption
    diverged = (db_repo3_sha_final != git_repo3_sha_final)
    
    # Clean up postgres
    git_wal.stop_postgres()
    
    if diverged:
        print("\nFAILURE: SCM-DB Distributed Transaction Protocol suffered permanent state divergence!")
        print("The subsequent run completely ignored the physical Git lag because the WAL state was 'committed'.")
        sys.exit(1)
    else:
        print("\nSUCCESS: SCM-DB transactional integrity was preserved under commit-phase crash.")
        sys.exit(0)

if __name__ == "__main__":
    main()
