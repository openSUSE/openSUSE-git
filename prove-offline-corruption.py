#!/usr/bin/env python3
"""
prove-offline-corruption.py: Proof of Concept demonstrating a catastrophic
data corruption, state divergence, and fail-open vulnerability in git-wal.py.
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
    
    print_banner("SIMULATING MID-TRANSACTION MANAGER CRASH")
    # We execute a transaction that successfully pushes to all repositories
    # but simulate a crash right after the pushes but before updating the DB state.
    # This leaves WAL transaction 1 in 'pending' state.
    try:
        manager.do_atomic_push(git_wal.REPOS, "Commit From Thread 1 (T1)", simulate_crash=True)
    except Exception as e:
        print(f"[Orchestrator] Interrupted: {e}")
        
    print("\nDatabase state after simulated crash:")
    manager.print_db_state()
    
    # Now, the orchestrator process crashed, which means its Lock Daemon is dead.
    # We simulate this by stopping the Lock Daemon socket (removing the socket file).
    # Since we are running in the same Python process in this PoC, we will delete the socket path
    # and wait a brief moment.
    print_banner("SIMULATING LOCK DAEMON GOING OFFLINE")
    socket_path = "/tmp/wal_lock.sock"
    if os.path.exists(socket_path):
        print(f"[Sim] Deleting {socket_path} to simulate offline Lock Daemon.")
        os.remove(socket_path)
        
    print_banner("CONCURRENT DIRECT SCM PUSH (FAIL-OPEN EXPLOIT)")
    # Since the Lock Daemon is offline, any direct developer push will fail-open!
    # Let's clone repo1 to a developer workspace and push a change.
    dev_ws = os.path.join(git_wal.BASE_DIR, "developer_workspace_offline")
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
        f.write("Developer Offline Change\n")
    git_wal.run_cmd(["git", "add", "developer_data.txt"], cwd=dev_ws)
    git_wal.run_cmd(["git", "commit", "-m", "Developer Offline Commit (D1)"], cwd=dev_ws)
    
    developer_sha = git_wal.run_cmd(["git", "rev-parse", "main"], cwd=dev_ws).stdout.strip()
    print(f"[Developer] Current repo1 HEAD is: {developer_sha[:8]}")
    
    print("[Developer] Pushing directly to repo1 (bypassing transaction manager)...")
    # This direct push should succeed because the Lock Daemon is offline (hook fails open).
    push_res = git_wal.run_cmd(["git", "push", "origin", "HEAD:main"], cwd=dev_ws, check=False)
    print(f"[Developer] Push output:\n{push_res.stderr.strip()}")
    print(f"[Developer] Push return code: {push_res.returncode}")
    
    new_developer_sha = manager.get_git_sha("repo1")
    print(f"[Developer] New repo1 HEAD after push is: {new_developer_sha[:8]}")
    
    print_banner("NEW ORCHESTRATOR STARTS & TRIGGERS HEALING")
    # A new transaction manager starts up and attempts an atomic push.
    # To avoid the offline socket error on its own push, we re-initialize the Lock Daemon first!
    print("[Orchestrator] Starting a new Lock Daemon for the new run...")
    import threading
    daemon_thread = threading.Thread(target=git_wal.run_lock_daemon, daemon=True)
    daemon_thread.start()
    time.sleep(1) # Allow daemon to bind to socket
    
    new_manager = git_wal.SystemManager()
    print("[Orchestrator] Running a new atomic push on repo1 to trigger healing on REPOS...")
    # The new push will detect the pending WAL transaction and heal it.
    heal_res = new_manager.do_atomic_push(["repo1"], "A new valid commit")
    print(f"[Orchestrator] Atomic push result: {heal_res}")
    
    print_banner("EVALUATING FINAL CORRUPTED STATE")
    # Inspect physical states and DB states
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("SELECT repo, sha FROM state ORDER BY repo")
    db_states = {repo: sha for repo, sha in c.fetchall()}
    
    c.execute("SELECT tx_id, state FROM wal ORDER BY tx_id")
    wal_txs = c.fetchall()
    conn.close()
    
    git_states = {repo: new_manager.get_git_sha(repo) for repo in git_wal.ALL_REPOS}
    
    print("\n--- WAL TRANSACTION LOGS ---")
    for tx_id, state in wal_txs:
        print(f"  TX {tx_id}: {state}")
        
    print("\n--- STATE COMPARISON ---")
    diverged = False
    for repo in git_wal.ALL_REPOS:
        db_sha = db_states.get(repo, "MISSING")
        git_sha = git_states.get(repo, "MISSING")
        print(f"  {repo:16} | DB: {db_sha[:8] if db_sha else 'None':8} | Git: {git_sha[:8] if git_sha else 'None':8}")
        if db_sha != git_sha:
            diverged = True
            
    # Clean up postgres
    git_wal.stop_postgres()
    
    if push_res.returncode != 0:
        print("\n[INFO] Push failed as blocked, this scenario was not triggered.")
        sys.exit(0)
        
    if diverged:
        print("\nFAILURE: SCM-DB Distributed Transaction Protocol suffered catastrophic data corruption!")
        print("1. The hook failed open when the Lock Daemon was offline, allowing a direct push.")
        print("2. The healing protocol incorrectly assumed the transaction failed due to the direct push, rolling back DB states while physical SCM remained fragmented.")
        sys.exit(1)
    else:
        print("\nSUCCESS: SCM-DB transactional integrity was preserved.")
        sys.exit(0)

if __name__ == "__main__":
    main()
