#!/usr/bin/env python3
"""
prove-real-rift-race.py: Proof of Concept demonstrating a critical timing rift
and transactional isolation failure in git-wal.py between row-locking and
advisory lock acquisition.
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
    
    thread1_paused = threading.Event()
    developer_push_done = threading.Event()
    
    # Save the original cursor execute
    original_execute = psycopg.Cursor.execute

    def custom_execute(self, query, params=None):
        res = original_execute(self, query, params)
        query_str = str(query).lower()
        # Detect row locking query, but pause BEFORE any advisory locks are acquired
        if "select repo from state" in query_str and "for update" in query_str:
            if threading.current_thread().name == "Thread-1":
                print(f"\n[PATCH] Thread 1 has acquired row locks but NOT advisory locks (REAL RIFT window). Pausing Thread 1...")
                thread1_paused.set()
                # Wait for the developer to finish their direct push
                developer_push_done.wait(timeout=10)
                print(f"[PATCH] Thread 1 resuming execution...")
        return res
        
    psycopg.Cursor.execute = custom_execute

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
        
    print_banner("CONCURRENT DIRECT PUSH INTRUSION IN THE REAL RIFT")
    
    # We simulate an independent developer doing a direct push to repo1.
    dev_ws = os.path.join(git_wal.BASE_DIR, "developer_workspace_real_rift")
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
        f.write("Developer Real Rift Change\n")
    git_wal.run_cmd(["git", "add", "developer_data.txt"], cwd=dev_ws)
    git_wal.run_cmd(["git", "commit", "-m", "Developer Real Rift Commit (D1)"], cwd=dev_ws)
    
    developer_sha = git_wal.run_cmd(["git", "rev-parse", "main"], cwd=dev_ws).stdout.strip()
    print(f"[Developer] Current repo1 HEAD is: {developer_sha[:8]}")
    
    print("[Developer] Pushing directly to repo1...")
    # This direct push will trigger the pre-receive hook which queries the Lock Daemon.
    # Since Thread 1 has not acquired the advisory lock on repo1 yet, the Lock Daemon should say "allow".
    push_res = git_wal.run_cmd(["git", "push", "origin", "HEAD:main"], cwd=dev_ws, check=False)
    print(f"[Developer] Push output:\n{push_res.stderr.strip()}")
    print(f"[Developer] Push return code: {push_res.returncode}")
    
    new_developer_sha = git_wal.get_repo_lock_id # dummy read to access original module functions
    new_developer_sha = git_wal.run_cmd(["git", "rev-parse", "main"], cwd=os.path.join(git_wal.BASE_DIR, "repo1")).stdout.strip()
    print(f"[Developer] New repo1 HEAD after push is: {new_developer_sha[:8]}")
    
    # Signal Thread 1 to resume
    developer_push_done.set()
    
    # Wait for Thread 1 to finish completely
    t1.join()
    
    print_banner("EVALUATING FINAL TRANSACTION RESULT")
    
    # Let's inspect physical SCM repository states
    git_repo1_sha = git_wal.run_cmd(["git", "rev-parse", "main"], cwd=os.path.join(git_wal.BASE_DIR, "repo1")).stdout.strip()
    
    # Clean up postgres
    git_wal.stop_postgres()
    
    if push_res.returncode == 0:
        print("\n[CRITICAL FAILURE] Direct push SUCCEEDED in the real rift window!")
        print(f"Developer commit is physically at: {new_developer_sha[:8]}")
        print("This proves that the row lock first, advisory lock second pattern leaves an open timing vulnerability.")
        sys.exit(1)
    else:
        print("\n[SUCCESS] Direct push was successfully blocked.")
        sys.exit(0)

if __name__ == "__main__":
    main()
