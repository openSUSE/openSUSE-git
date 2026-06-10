#!/usr/bin/env python3
"""
prove-rift-race.py: Proof of Concept demonstrating a critical race condition,
inconsistent states, and silent data loss/corruption in git-wal.py under a
concurrent direct SCM push hitting the window between row-locking and WAL entry.
"""

import os
import sys
import time
import threading
import shutil
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
    
    original_get_conflicting = manager.get_conflicting_pending_transactions
    original_get_git_sha = manager.get_git_sha
    
    thread1_paused = threading.Event()
    developer_push_done = threading.Event()
    
    # We monkeypatch get_conflicting_pending_transactions of SystemManager to pause Thread 1
    # right after acquiring the SELECT FOR UPDATE row locks but BEFORE creating the WAL record.
    def custom_get_conflicting_pending_transactions(target_repos):
        if threading.current_thread().name == "Thread-1":
            print(f"\n[PATCH] Thread 1 has acquired row locks. Pausing Thread 1 before WAL entry is written (RIFT window)...")
            thread1_paused.set()
            # Wait for the developer to finish their direct push
            developer_push_done.wait(timeout=10)
            print(f"[PATCH] Thread 1 resuming execution...")
        return original_get_conflicting(target_repos)
        
    manager.get_conflicting_pending_transactions = custom_get_conflicting_pending_transactions

    # Thread 1 worker
    def thread1_worker():
        print("[Thread 1] Starting atomic push targeting ['repo1', 'repo3']...")
        res = manager.do_atomic_push(["repo1", "repo3"], "Commit From Thread 1 (T1)")
        print(f"[Thread 1] do_atomic_push finished. Result: {res}")

    t1 = threading.Thread(target=thread1_worker, name="Thread-1")
    t1.start()
    
    # Wait for Thread 1 to be paused inside the rift
    if not thread1_paused.wait(timeout=10):
        print("Error: Thread 1 did not reach the rift patch in time.")
        git_wal.stop_postgres()
        sys.exit(1)
        
    print_banner("CONCURRENT DIRECT PUSH INTRUSION IN THE RIFT")
    
    # We simulate an independent developer doing a direct push to repo1.
    dev_ws = os.path.join(git_wal.BASE_DIR, "developer_workspace_rift")
    if os.path.exists(dev_ws):
        shutil.rmtree(dev_ws)
        
    print("[Developer] Cloning repo1...")
    git_wal.run_cmd(["git", "clone", os.path.join(git_wal.BASE_DIR, "repo1"), dev_ws])
    
    print("[Developer] Checking out main branch...")
    git_wal.run_cmd(["git", "checkout", "main"], cwd=dev_ws)
    
    print("[Developer] Creating a concurrent developer commit...")
    git_wal.run_cmd(["git", "config", "user.name", "External Developer"], cwd=dev_ws)
    git_wal.run_cmd(["git", "config", "user.email", "developer@scmdirect.com"], cwd=dev_ws)
    
    with open(os.path.join(dev_ws, "developer_data.txt"), "w") as f:
        f.write("Developer Rift Change\n")
    git_wal.run_cmd(["git", "add", "developer_data.txt"], cwd=dev_ws)
    git_wal.run_cmd(["git", "commit", "-m", "Developer Rift Commit (D1)"], cwd=dev_ws)
    
    developer_sha = original_get_git_sha("repo1")
    print(f"[Developer] Current repo1 HEAD is: {developer_sha[:8]}")
    
    print("[Developer] Pushing directly to repo1 (bypassing transaction manager)...")
    # This direct push will trigger the pre-receive hook which queries the Lock Daemon.
    # Because Thread 1 has not written a pending WAL entry yet, the Lock Daemon will say "allow".
    push_res = git_wal.run_cmd(["git", "push", "origin", "HEAD:main"], cwd=dev_ws, check=False)
    print(f"[Developer] Push output:\n{push_res.stderr.strip()}")
    print(f"[Developer] Push return code: {push_res.returncode}")
    
    new_developer_sha = original_get_git_sha("repo1")
    print(f"[Developer] New repo1 HEAD after push is: {new_developer_sha[:8]}")
    
    # Signal Thread 1 to resume
    developer_push_done.set()
    
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
    git_repo1_sha = original_get_git_sha("repo1")
    git_repo3_sha = original_get_git_sha("repo3")
    
    print(f"Developer's pushed SHA (should be here): {new_developer_sha[:8]}")
    print(f"Database 'state' record for repo1:        {db_repo1_sha[:8]}")
    print(f"Physical Git HEAD for repo1:              {git_repo1_sha[:8]}")
    print(f"Database 'state' record for repo3:        {db_repo3_sha[:8]}")
    print(f"Physical Git HEAD for repo3:              {git_repo3_sha[:8]}")
    
    # Clean up postgres
    git_wal.stop_postgres()
    
    # Check if we diverged or suffered data loss
    diverged = False
    
    if push_res.returncode == 0:
        print("\n[CRITICAL ERROR] Direct push succeeded in the rift window!")
        if git_repo1_sha != new_developer_sha:
            print("[FAILURE] Developer's commit was SILENTLY OVERWRITTEN / ERASED from repo1!")
            diverged = True
        if db_repo1_sha != git_repo1_sha:
            print(f"[FAILURE] repo1 has diverged! DB state points to {db_repo1_sha[:8]}, but SCM physical is at {git_repo1_sha[:8]}!")
            diverged = True
        if db_repo3_sha != git_repo3_sha:
            print(f"[FAILURE] repo3 has diverged! DB state points to {db_repo3_sha[:8]}, but SCM physical is at {git_repo3_sha[:8]}!")
            diverged = True
    else:
        print("\n[INFO] Direct push was blocked (no race found).")
        
    if diverged:
        print("\nFAILURE: SCM-DB Distributed Transaction Protocol failed to protect transactional integrity!")
        sys.exit(1)
    else:
        print("\nSUCCESS: No divergence or data loss detected under this race scenario.")
        sys.exit(0)

if __name__ == "__main__":
    main()
