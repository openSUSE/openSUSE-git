#!/usr/bin/env python3
"""
prove-split-brain-race.py: Proof of Concept demonstrating a catastrophic
split-brain race condition and permanent state divergence in git-wal.py.

This occurs when a transaction manager's database connection is severed (e.g. due to
a network partition, firewall cut, or idle transaction timeout) while its slow
physical Git pushes are still in-flight. Since PostgreSQL automatically releases
all row-level and advisory locks when a connection terminates, a concurrent process
is allowed to acquire the locks and 'heal' (roll back) the pending transaction
by resetting the already pushed repositories. When the original slow pusher finally
completes its remaining pushes, some repositories are rolled back while others
remain updated, causing physical Git states to permanently diverge from the database.
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
    
    # We intercept psycopg.connect to capture Thread 1's database connections
    original_connect = psycopg.connect
    thread1_conns = []
    
    def custom_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        if threading.current_thread().name == "Thread-1":
            thread1_conns.append(conn)
        return conn
        
    psycopg.connect = custom_connect
    
    original_run_cmd = git_wal.run_cmd
    
    thread1_sleep_started = threading.Event()
    thread2_done = threading.Event()
    
    def custom_run_cmd(cmd, cwd=None, env=None, check=True):
        cmd_str = " ".join(cmd)
        # We intercept Thread 1 right after it successfully pushed to repo1, but BEFORE it pushes to repo3.
        if "push" in cmd_str and "repo3" in cmd_str and threading.current_thread().name == "Thread-1":
            print(f"\n[PATCH] Thread 1 has successfully pushed to repo1, and is about to push to repo3.")
            print("[PATCH] Simulating database connection loss / timeout for Thread 1...")
            
            # Close all connections associated with Thread 1 to release PostgreSQL locks
            closed_count = 0
            for conn in thread1_conns:
                try:
                    if not conn.closed:
                        conn.close()
                        closed_count += 1
                except Exception as e:
                    print(f"[PATCH] Error closing connection: {e}")
            print(f"[PATCH] Closed {closed_count} Thread 1 database connection(s). Row-level and advisory locks are now released.")
                
            print("[PATCH] Signaling Thread 2 to run its concurrent transaction and heal Thread 1's transaction...")
            thread1_sleep_started.set()
            
            # Wait for Thread 2 to complete its healing and transaction
            if not thread2_done.wait(timeout=10):
                print("[PATCH] ERROR: Thread 2 did not complete in time.")
                
            print(f"[PATCH] Thread 1 resuming and executing slow push to repo3...")
            
        return original_run_cmd(cmd, cwd, env, check)
        
    git_wal.run_cmd = custom_run_cmd

    # Thread 1 worker (Slow pusher targeting ['repo1', 'repo3'])
    def thread1_worker():
        print("[Thread 1] Starting atomic push targeting ['repo1', 'repo3']...")
        res = manager.do_atomic_push(["repo1", "repo3"], "Commit From Thread 1 (T1)")
        print(f"[Thread 1] do_atomic_push finished. Result: {res}")

    t1 = threading.Thread(target=thread1_worker, name="Thread-1")
    t1.start()
    
    # Wait for Thread 1 to push to repo1 and close its DB connection
    if not thread1_sleep_started.wait(timeout=10):
        print("Error: Thread 1 did not reach the patch rendezvous in time.")
        git_wal.stop_postgres()
        sys.exit(1)
        
    print_banner("CONCURRENT CONFLICT & SCM-DB SPLIT-BRAIN (THREAD 2)")
    print("[Thread 2] Starting concurrent atomic push targeting ['repo1']...")
    
    # Thread 2 will detect Thread 1's pending transaction (tx 1).
    # Since Thread 1's locks are released, Thread 2 can acquire the locks and call heal_transaction(1).
    # Because Thread 1's push to repo3 is not finished, heal_transaction will see repo3 at pre_sha,
    # conclude that the transaction is incomplete, and roll back Thread 1's transaction!
    # It will reset repo1 to pre_sha, mark tx 1 as 'aborted', and then push its own Commit T2 to repo1.
    res2 = manager.do_atomic_push(["repo1"], "Commit From Thread 2 (T2)")
    print(f"[Thread 2] do_atomic_push finished. Result: {res2}")
    
    # Signal Thread 1 to resume and finish pushing to repo3
    thread2_done.set()
    
    # Wait for Thread 1 to complete
    t1.join()
    
    print_banner("EVALUATING FINAL INCONSISTENT STATE")
    
    # Let's inspect the database state
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("SELECT repo, sha FROM state WHERE repo = 'repo1'")
    db_repo1_sha = c.fetchone()[1]
    
    c.execute("SELECT repo, sha FROM state WHERE repo = 'repo3'")
    db_repo3_sha = c.fetchone()[1]
    
    c.execute("SELECT tx_id, state FROM wal ORDER BY tx_id")
    wal_txs = c.fetchall()
    conn.close()
    
    # Let's inspect physical SCM repository states
    git_repo1_sha = manager.get_git_sha("repo1")
    git_repo3_sha = manager.get_git_sha("repo3")
    
    print("\n--- WAL TRANSACTION LOGS ---")
    for tx_id, state in wal_txs:
        print(f"  TX {tx_id}: {state}")
        
    print("\n--- STATE COMPARISON ---")
    print(f"Database 'state' record for repo1: {db_repo1_sha[:8] if db_repo1_sha else 'None'}")
    print(f"Physical Git HEAD for repo1:       {git_repo1_sha[:8] if git_repo1_sha else 'None'}")
    print(f"Database 'state' record for repo3: {db_repo3_sha[:8] if db_repo3_sha else 'None'}")
    print(f"Physical Git HEAD for repo3:       {git_repo3_sha[:8] if git_repo3_sha else 'None'}")
    
    # Clean up postgres
    git_wal.stop_postgres()
    
    # Check if we diverged or suffered data corruption
    diverged = False
    if db_repo3_sha != git_repo3_sha:
        print("\n[CRITICAL FAILURE] repo3 has permanently diverged!")
        print(f"  Database table thinks repo3 is at: {db_repo3_sha[:8] if db_repo3_sha else 'None'}")
        print(f"  Physical Git repository is at:     {git_repo3_sha[:8] if git_repo3_sha else 'None'}")
        diverged = True
        
    if db_repo1_sha != git_repo1_sha:
        print("\n[CRITICAL FAILURE] repo1 has permanently diverged!")
        print(f"  Database table thinks repo1 is at: {db_repo1_sha[:8] if db_repo1_sha else 'None'}")
        print(f"  Physical Git repository is at:     {git_repo1_sha[:8] if git_repo1_sha else 'None'}")
        diverged = True

    if diverged:
        print("\nFAILURE: SCM-DB Distributed Transaction Protocol suffered a catastrophic split-brain race condition!")
        sys.exit(1)
    else:
        print("\nSUCCESS: SCM-DB transactional integrity was preserved under connection-loss split-brain race.")
        sys.exit(0)


if __name__ == "__main__":
    main()
