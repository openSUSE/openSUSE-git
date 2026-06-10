#!/usr/bin/env python3
"""
prove-concurrency-divergence.py: Proof of Concept demonstrating a catastrophic
data loss and transaction isolation failure in git-wal.py.

This demonstrates that when a developer pushes a valid direct commit, a concurrent
out-of-sync transaction manager can silently overwrite and completely destroy the
developer's commit from the main branch history, resulting in silent data loss.
"""

import os
import sys
import time
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
    
    # Capture the initial SHA of repo1
    initial_sha = manager.get_git_sha("repo1")
    print(f"Initial physical SHA for repo1: {initial_sha[:8]}")

    print_banner("CONCURRENT DIRECT DEVELOPER PUSH (UPDATING PHYSICAL SCM TO D1)")
    # A developer clones repo1 and pushes a direct change.
    # Since no transaction is active, this push succeeds instantly.
    dev_ws = os.path.join(git_wal.BASE_DIR, "developer_workspace_divergence")
    if os.path.exists(dev_ws):
        shutil.rmtree(dev_ws)
        
    print("[Developer] Cloning repo1...")
    git_wal.run_cmd(["git", "clone", os.path.join(git_wal.BASE_DIR, "repo1"), dev_ws])
    
    print("[Developer] Checking out main branch...")
    git_wal.run_cmd(["git", "checkout", "main"], cwd=dev_ws)
    
    print("[Developer] Creating a direct developer commit...")
    git_wal.run_cmd(["git", "config", "user.name", "External Developer"], cwd=dev_ws)
    git_wal.run_cmd(["git", "config", "user.email", "developer@scmdirect.com"], cwd=dev_ws)
    
    with open(os.path.join(dev_ws, "developer_data.txt"), "w") as f:
        f.write("Developer Direct Change\n")
    git_wal.run_cmd(["git", "add", "developer_data.txt"], cwd=dev_ws)
    git_wal.run_cmd(["git", "commit", "-m", "Developer Commit (D1)"], cwd=dev_ws)
    
    developer_sha = git_wal.run_cmd(["git", "rev-parse", "main"], cwd=dev_ws).stdout.strip()
    print(f"[Developer] Target developer commit SHA: {developer_sha[:8]}")
    
    print("[Developer] Pushing directly to repo1...")
    push_res = git_wal.run_cmd(["git", "push", "origin", "main:main"], cwd=dev_ws, check=False)
    print(f"[Developer] Push finished with return code: {push_res.returncode}")
    
    # Verify physical HEAD has changed
    new_git_sha = manager.get_git_sha("repo1")
    print(f"[SCM] Physical HEAD of repo1 is now: {new_git_sha[:8]}")

    print_banner("RUNNING OUT-OF-DATE COORDINATED TRANSACTION")
    # Now, the Transaction Manager initiates an atomic push targeting ['repo1', 'repo3'].
    # Because its local workspace is NOT synchronized (it still has initial_sha),
    # the manager's new commit will be created on top of initial_sha, not developer_sha.
    print("[Manager] Attempting atomic push targeting ['repo1', 'repo3']...")
    res = manager.do_atomic_push(["repo1", "repo3"], "Transaction Commit (T1)")
    print(f"[Manager] do_atomic_push result: {res}")

    print_banner("EVALUATING HISTORICAL DATA INTEGRITY")
    
    # Check if the developer's commit D1 is still in the history of physical repo1's main branch
    git_repo1_sha = manager.get_git_sha("repo1")
    developer_commit_preserved = manager.is_ancestor("repo1", developer_sha, git_repo1_sha)

    print(f"Final Physical Git HEAD for repo1: {git_repo1_sha[:8]}")
    print(f"Developer commit ({developer_sha[:8]}) preserved in history? {developer_commit_preserved}")

    # Clean up developer workspace
    if os.path.exists(dev_ws):
        try: shutil.rmtree(dev_ws)
        except Exception: pass

    # Stop postgres
    git_wal.stop_postgres()

    if not developer_commit_preserved:
        print("\n[CRITICAL FAILURE] Developer commit D1 was SILENTLY OVERWRITTEN and completely LOST from history!")
        print("This proves that SCM-DB Distributed Transaction Protocol lacks proper isolation, allowing silent data loss.")
        sys.exit(1)
    else:
        print("\n[SUCCESS] SCM transactional history was preserved.")
        sys.exit(0)


if __name__ == "__main__":
    main()
