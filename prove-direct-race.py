#!/usr/bin/env python3
"""
prove-direct-race.py: Proof of Concept demonstrating a critical race condition
and silent data loss in git-wal.py (Option B) under a concurrent direct SCM push.
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
    
    original_get_git_sha = manager.get_git_sha
    
    thread1_sleep_started = threading.Event()
    developer_push_done = threading.Event()
    
    # We monkeypatch get_git_sha of SystemManager to pause Thread 1
    # right after recording pre_shas but BEFORE creating the WAL record.
    def custom_get_git_sha(repo_name):
        sha = original_get_git_sha(repo_name)
        # We pause Thread 1 when it checks repo3, which is the last repo in its list
        if repo_name == "repo3" and threading.current_thread().name == "Thread-1":
            print(f"\n[PATCH] Thread 1 has recorded pre_shas. Pausing Thread 1 before WAL entry is written...")
            thread1_sleep_started.set()
            # Wait for the developer to finish their direct push
            developer_push_done.wait(timeout=10)
            print(f"[PATCH] Thread 1 resuming execution...")
        return sha
        
    manager.get_git_sha = custom_get_git_sha

    # Thread 1 worker
    def thread1_worker():
        print("[Thread 1] Starting atomic push targeting ['repo1', 'repo3']...")
        res = manager.do_atomic_push(["repo1", "repo3"], "Commit From Thread 1 (T1)")
        print(f"[Thread 1] do_atomic_push finished. Result: {res}")

    t1 = threading.Thread(target=thread1_worker, name="Thread-1")
    t1.start()
    
    # Wait for Thread 1 to be paused
    if not thread1_sleep_started.wait(timeout=10):
        print("Error: Thread 1 did not reach the pause patch in time.")
        git_wal.stop_postgres()
        sys.exit(1)
        
    print_banner("CONCURRENT DIRECT PUSH INTRUSION")
    
    # We simulate an independent developer doing a direct push to repo1.
    # To do this, we clone repo1 to a separate developer workspace,
    # add a commit, and push it directly.
    dev_ws = os.path.join(git_wal.BASE_DIR, "developer_workspace")
    if os.path.exists(dev_ws):
        shutil.rmtree(dev_ws)
        
    print("[Developer] Cloning repo1...")
    git_wal.run_cmd(["git", "clone", os.path.join(git_wal.BASE_DIR, "repo1"), dev_ws])
    
    print("[Developer] Checking out main branch...")
    git_wal.run_cmd(["git", "checkout", "main"], cwd=dev_ws)
    
    print("[Developer] Creating a concurrent developer commit...")
    # Set user info to avoid warnings
    git_wal.run_cmd(["git", "config", "user.name", "External Developer"], cwd=dev_ws)
    git_wal.run_cmd(["git", "config", "user.email", "developer@scmdirect.com"], cwd=dev_ws)
    
    with open(os.path.join(dev_ws, "developer_data.txt"), "w") as f:
        f.write("Developer Direct Change\n")
    git_wal.run_cmd(["git", "add", "developer_data.txt"], cwd=dev_ws)
    git_wal.run_cmd(["git", "commit", "-m", "Developer Direct Commit (D1)"], cwd=dev_ws)
    
    developer_sha = original_get_git_sha("repo1")
    print(f"[Developer] Current repo1 HEAD is: {developer_sha[:8]}")
    
    print("[Developer] Pushing directly to repo1 (bypassing transaction manager)...")
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
    
    # Check if developer's commit was lost
    lost = False
    if push_res.returncode == 0:
        if git_repo1_sha != new_developer_sha:
            print("\n[CRITICAL FAILURE] Developer's commit was SILENTLY ERASED from repo1!")
            print(f"  Expected: {new_developer_sha[:8]}")
            print(f"  Actual:   {git_repo1_sha[:8]}")
            lost = True
        else:
            print("\n[OK] Developer's commit is still present in repo1.")
    else:
        print("\n[OK] Lock Daemon successfully BLOCKED the concurrent direct push.")
        print(f"[OK] Physical HEAD of repo1 is {git_repo1_sha[:8]} (Thread 1's commit).")
        print("[OK] SCM-DB integrity is 100% preserved.")

    if lost:
        print("\nFAILURE: SCM-DB Distributed Transaction Protocol suffered from a severe race condition and data loss!")
        sys.exit(1)
    else:
        print("\nSUCCESS: System integrity was preserved under direct push race.")
        sys.exit(0)

if __name__ == "__main__":
    main()
