#!/usr/bin/env python3
"""
prove-deadlock.py: Proof of Concept demonstrating a critical database deadlock
in git-wal.py under concurrent healing of a multi-repository crashed transaction.
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
    git_wal.setup_environment()
    
    manager = git_wal.SystemManager()
    
    # Verify initial state
    print("\nInitial DB and Git states:")
    manager.print_db_state()
    
    print_banner("SIMULATING MULTI-REPO CRASHED TRANSACTION")
    # Manually inject a pending WAL transaction involving both repo1 and repo3
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("INSERT INTO wal (state) VALUES ('pending') RETURNING tx_id")
    pending_tx_id = c.fetchone()[0]
    for repo in ["repo1", "repo3"]:
        pre_sha = manager.get_git_sha(repo)
        c.execute("INSERT INTO wal_entries (tx_id, repo, pre_sha, post_sha) VALUES (%s, %s, %s, %s)",
                  (pending_tx_id, repo, pre_sha, pre_sha))
    conn.commit()
    conn.close()
    
    print(f"Manually injected crashed pending transaction ID: {pending_tx_id} targeting ['repo1', 'repo3'].")
    manager.print_db_state()
    
    print_banner("TRIGGERING CONCURRENT HEALING DEADLOCK")
    
    thread1_ready = threading.Event()
    thread2_ready = threading.Event()
    start_healing = threading.Event()
    
    original_get_conflicting = manager.get_conflicting_pending_transactions
    
    def patched_get_conflicting_pending_transactions(target_repos):
        thread_name = threading.current_thread().name
        print(f"[{thread_name}] Has acquired row-level lock on {target_repos}. Waiting for rendezvous...")
        if thread_name == "Thread-1":
            thread1_ready.set()
        elif thread_name == "Thread-2":
            thread2_ready.set()
            
        # Wait for both threads to have acquired their respective row locks
        if not start_healing.wait(timeout=10):
            print(f"[{thread_name}] Timed out waiting for start_healing signal.")
        print(f"[{thread_name}] Proceeding to find conflicts and heal...")
        return original_get_conflicting(target_repos)
        
    manager.get_conflicting_pending_transactions = patched_get_conflicting_pending_transactions
    
    thread1_result = []
    thread2_result = []
    
    # Intercept stderr to capture deadlock details
    import io
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    
    def thread1_worker():
        try:
            res = manager.do_atomic_push(["repo1"], "Commit from Thread 1")
            thread1_result.append(("success", res))
        except Exception as e:
            thread1_result.append(("error", e))
            
    def thread2_worker():
        try:
            res = manager.do_atomic_push(["repo3"], "Commit from Thread 2")
            thread2_result.append(("success", res))
        except Exception as e:
            thread2_result.append(("error", e))
            
    t1 = threading.Thread(target=thread1_worker, name="Thread-1")
    t2 = threading.Thread(target=thread2_worker, name="Thread-2")
    
    t1.start()
    t2.start()
    
    # Wait for both threads to acquire their row locks
    if thread1_ready.wait(timeout=5) and thread2_ready.wait(timeout=5):
        old_stdout.write("\n[Coordinator] Both threads have acquired their row locks on state table.\n")
        old_stdout.write("[Coordinator] Releasing both threads to proceed to healing concurrently...\n")
        start_healing.set()
    else:
        old_stdout.write("Error: Threads failed to reach rendezvous point.\n")
        git_wal.stop_postgres()
        sys.stdout = old_stdout
        sys.exit(1)
        
    t1.join()
    t2.join()
    
    captured_output = sys.stdout.getvalue()
    sys.stdout = old_stdout
    
    print(captured_output)
    
    print_banner("EVALUATING DEADLOCK OUTCOME")
    print(f"Thread 1 Result: {thread1_result}")
    print(f"Thread 2 Result: {thread2_result}")
    
    # Stop postgres
    git_wal.stop_postgres()
    
    # Analyze if deadlock occurred
    deadlock_detected = "deadlock detected" in captured_output.lower() or "deadlock detected" in str(thread1_result).lower() or "deadlock detected" in str(thread2_result).lower()
                
    if deadlock_detected:
        print("\nFAILURE: SCM-DB Distributed Transaction Protocol suffered a database deadlock.")
        print("This proves that concurrent non-overlapping transactions can deadlock when healing a shared multi-repo crashed transaction.")
        sys.exit(1)
    else:
        print("\nSUCCESS: No deadlock detected.")
        sys.exit(0)

if __name__ == "__main__":
    main()
