#!/usr/bin/env python3
"""
prove-race.py: Proof of Concept demonstrating the SCM-DB transactional integrity failure
in git-wal.py due to early lock release and unsafe 'healing' of active transactions.
"""

import os
import sys
import time
import threading
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
    
    # We will run Thread 1 which pushes to repo1 and repo3
    # We monkeypatch run_cmd to sleep BEFORE pushing to repo3.
    # This simulates a slow/delayed network push for repo3.
    original_run_cmd = git_wal.run_cmd
    
    thread2_started = threading.Event()
    thread1_sleep_started = threading.Event()
    
    def custom_run_cmd(cmd, cwd=None, env=None, check=True):
        cmd_str = " ".join(cmd)
        if "push" in cmd_str and "repo3" in cmd_str:
            print(f"\n[PATCH] Thread 1 is about to push to repo3. Sleeping to let Thread 2 run...")
            thread1_sleep_started.set()
            # Wait up to 5 seconds for Thread 2 to run and finish its attempt
            time.sleep(5)
            print(f"[PATCH] Thread 1 waking up to finish push to repo3...")
        return original_run_cmd(cmd, cwd, env, check)
        
    git_wal.run_cmd = custom_run_cmd

    # Thread 1 worker
    def thread1_worker():
        print("[Thread 1] Starting atomic push targeting ['repo1', 'repo3']...")
        # do_atomic_push will commit WAL tx_id 1 as 'pending' and release DB locks,
        # then push to repo1 (succeeds), then our patch will sleep before pushing to repo3.
        res = manager.do_atomic_push(["repo1", "repo3"], "Commit From Thread 1 (T1)")
        print(f"[Thread 1] do_atomic_push finished. Result: {res}")

    t1 = threading.Thread(target=thread1_worker, name="Thread-1")
    t1.start()
    
    # Wait for Thread 1 to start its sleep (which means it has completed the push to repo1, but not repo3)
    if not thread1_sleep_started.wait(timeout=10):
        print("Error: Thread 1 did not reach the sleep patch in time.")
        git_wal.stop_postgres()
        sys.exit(1)
        
    print_banner("CONCURRENT CONFLICT INTRUSION (THREAD 2)")
    print("[Thread 2] Starting concurrent atomic push targeting ['repo1']...")
    # Thread 2 will detect Thread 1's pending transaction (tx 1).
    # Since Thread 1 is sleeping, and repo3 is not yet at post_sha, Thread 2's heal_transaction(1)
    # will think Thread 1 has crashed/failed, and will ROLL BACK Thread 1's transaction!
    # It will reset repo1 to pre_sha, and then push its own Commit T2 to repo1.
    res2 = manager.do_atomic_push(["repo1"], "Commit From Thread 2 (T2)")
    print(f"[Thread 2] do_atomic_push finished. Result: {res2}")
    thread2_started.set()
    
    # Wait for Thread 1 to finish completely
    t1.join()
    
    print_banner("EVALUATING FINAL INCONSISTENT STATE")
    
    # Let's inspect the database state
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("SELECT repo, sha FROM state WHERE repo = 'repo1'")
    db_repo1_sha = c.fetchone()[1]
    
    c.execute("SELECT repo, sha FROM state WHERE repo = 'repo3'")
    db_repo3_sha = c.fetchone()[1]
    conn.close()
    
    # Let's inspect physical SCM repository states
    git_repo1_sha = manager.get_git_sha("repo1")
    git_repo3_sha = manager.get_git_sha("repo3")
    
    print(f"Database 'state' record for repo1: {db_repo1_sha[:8]}")
    print(f"Physical Git HEAD for repo1:       {git_repo1_sha[:8]}")
    print(f"Database 'state' record for repo3: {db_repo3_sha[:8]}")
    print(f"Physical Git HEAD for repo3:       {git_repo3_sha[:8]}")
    
    # Clean up postgres
    git_wal.stop_postgres()
    
    # Check if there is a divergence between DB and SCM
    diverged = False
    if db_repo1_sha != git_repo1_sha:
        print("\n[CRITICAL FAILURE] repo1 has diverged! DB state points to one SHA, but physical Git repository is at another!")
        diverged = True
    else:
        print("\n[OK] repo1 DB and SCM are perfectly consistent.")

    if db_repo3_sha != git_repo3_sha:
        print("\n[CRITICAL FAILURE] repo3 has diverged! DB state points to one SHA, but physical Git repository is at another!")
        diverged = True
    else:
        print("[OK] repo3 DB and SCM are perfectly consistent.")

    print("\nVerification of transactional isolation:")
    if not diverged:
        print("\nSUCCESS: Transactional Locking (Option B) has successfully serialized concurrent threads!")
        print("[OK] No divergence occurred between the database and the SCM repositories.")
        print("[OK] System integrity is 100% preserved.")
        sys.exit(0)
    else:
        print("\nFAILURE: Transactional mechanisms failed to maintain consistency. Race condition still present.")
        sys.exit(1)

if __name__ == "__main__":
    main()
