#!/usr/bin/env python3
"""
prove-force-push-split-brain.py: Proof of Concept demonstrating a catastrophic,
permanent split-brain race condition in git-wal.py under a slow force push.

This verification script proves that the consensus protocol fails to maintain SCM-DB
consistency when a developer performs a force push that experiences network/upload
latency.
"""

import os
import sys
import time
import threading
import shutil
import psycopg

# Import the git-wal module
git_wal = __import__("git-wal")

ALLOWED_TRIGGER = os.path.join(git_wal.BASE_DIR, "hook_allowed_trigger")


def print_banner(msg):
    print("\n" + "=" * 80)
    print(f" {msg}")
    print("=" * 80)


def main():
    print_banner("INITIALIZING SYSTEM ENVIRONMENT")
    if os.path.exists(ALLOWED_TRIGGER):
        try: os.remove(ALLOWED_TRIGGER)
        except Exception: pass

    # Initialize pristine environment (starts postgres, daemon, creates tables and repos)
    git_wal.setup_environment()
    
    manager = git_wal.SystemManager()
    
    # Verify initial state
    print("\nInitial DB and Git states:")
    manager.print_db_state()
    
    # Overwrite the pre-receive hook in repo1 with our custom hook emulating network upload delay
    print("[Setup] Writing custom hook for repo1 emulating network upload delay...")
    repo1_hook_path = os.path.join(git_wal.BASE_DIR, "repo1", "hooks", "pre-receive")
    
    custom_hook_content = f"""#!/usr/bin/env python3
import sys
import os
import socket
import time

ALLOWED_TRIGGER = {repr(ALLOWED_TRIGGER)}

if os.environ.get("DISABLE_WAL_LOCKS") == "1":
    sys.exit(0)

# Apply network delay only to direct developer pushes (where ACTIVE_WAL_TX_ID is not set)
active_tx_id = os.environ.get("ACTIVE_WAL_TX_ID", "None")
if active_tx_id == "None":
    # Emulate 3.0 seconds network upload/transfer delay BEFORE running hook logic
    time.sleep(3.0)

pwd = os.getcwd()
repo_name = "repo1"

socket_path = "/tmp/wal_lock.sock"
try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(socket_path)
    
    message = repo_name + ":" + active_tx_id
    client.sendall(message.encode('utf-8'))
    response = client.recv(1024).decode('utf-8').strip()
    client.close()
    
    if response == "locked":
        sys.stderr.write("Push rejected: repo1 is locked\\n")
        sys.exit(1)
    elif response == "allow":
        with open(ALLOWED_TRIGGER, "w") as f:
            f.write("allowed")
        sys.stderr.write("Hook allowed, resuming force push to repo1\\n")
        sys.exit(0)
    else:
        sys.stderr.write("Push rejected: unknown response: " + response + "\\n")
        sys.exit(1)
except Exception as e:
    sys.stderr.write("Push rejected: Lock Daemon connection failed: " + str(e) + "\\n")
    sys.exit(1)
"""
    with open(repo1_hook_path, "w") as hf:
        hf.write(custom_hook_content)
    os.chmod(repo1_hook_path, 0o755)

    # Prepare developer workspace
    dev_ws = os.path.join(git_wal.BASE_DIR, "developer_workspace_force_push")
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
        f.write("Developer Force Push Change\n")
    git_wal.run_cmd(["git", "add", "developer_data.txt"], cwd=dev_ws)
    git_wal.run_cmd(["git", "commit", "-m", "Developer Force Commit (D1)"], cwd=dev_ws)
    
    developer_sha = git_wal.run_cmd(["git", "rev-parse", "main"], cwd=dev_ws).stdout.strip()
    print(f"[Developer] Target developer force commit SHA: {developer_sha[:8]}")

    # Launch Thread 1: Developer performs a FORCE push
    def developer_push_worker():
        print("[Thread 1 - Developer] Starting direct FORCE push to repo1...")
        res = git_wal.run_cmd(["git", "push", "-f", "origin", "main:main"], cwd=dev_ws, check=False)
        print(f"[Thread 1 - Developer] FORCE push finished with return code: {res.returncode}")
        if res.stderr:
            print(f"[Thread 1 - Developer] Push stderr:\n{res.stderr}")

    t1 = threading.Thread(target=developer_push_worker, name="Thread-1")
    t1.start()

    # Sleep briefly to ensure Thread 1 has initiated the push and entered the pre-hook delay
    print("[Main] Sleeping 1 second to let developer push start and enter the network delay...")
    time.sleep(1.0)

    # Launch Thread 2: Coordinated Transaction Manager do_atomic_push targeting ['repo1', 'repo3']
    def manager_push_worker():
        print("[Thread 2 - Manager] Starting concurrent atomic push targeting ['repo1', 'repo3']...")
        res = manager.do_atomic_push(["repo1", "repo3"], "Transaction Commit (T1)")
        print(f"[Thread 2 - Manager] do_atomic_push finished. Result: {res}")

    t2 = threading.Thread(target=manager_push_worker, name="Thread-2")
    t2.start()

    # Wait for both threads to complete
    t1.join()
    t2.join()

    print_banner("EVALUATING SCM-DB INTEGRITY")
    
    # Check the database state
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("SELECT repo, sha FROM state WHERE repo = 'repo1'")
    db_repo1_sha = c.fetchone()[1]
    conn.close()

    # Check the physical SCM states
    git_repo1_sha = manager.get_git_sha("repo1")

    print("\n--- STATE COMPARISON ---")
    print(f"Database 'state' record for repo1: {db_repo1_sha[:8] if db_repo1_sha else 'None'}")
    print(f"Physical Git HEAD for repo1:       {git_repo1_sha[:8] if git_repo1_sha else 'None'}")
    
    diverged = (db_repo1_sha != git_repo1_sha)

    # Clean up trigger files and developer workspace
    if os.path.exists(ALLOWED_TRIGGER):
        try: os.remove(ALLOWED_TRIGGER)
        except Exception: pass
    if os.path.exists(dev_ws):
        try: shutil.rmtree(dev_ws)
        except Exception: pass

    # Stop postgres
    git_wal.stop_postgres()

    if diverged:
        print("\n[CRITICAL FAILURE] SCM and DB have permanently and silently diverged!")
        print("This proves that SCM-DB Distributed Transaction Protocol suffers from a permanent split-brain vulnerability!")
        sys.exit(1)
    else:
        print("\n[SUCCESS] SCM-DB transactional integrity was preserved.")
        sys.exit(0)


if __name__ == "__main__":
    main()
