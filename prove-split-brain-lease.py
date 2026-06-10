#!/usr/bin/env python3
"""
prove-split-brain-lease.py: Proof of Concept demonstrating a catastrophic
split-brain race condition and permanent state divergence in git-wal.py
due to a slow direct push exceeding the 5-second temporary lease lock.
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
    # Clean up any lingering trigger files first
    if os.path.exists(ALLOWED_TRIGGER):
        try: os.remove(ALLOWED_TRIGGER)
        except Exception: pass

    # Initialize pristine environment (starts postgres, daemon, creates tables and repos)
    git_wal.setup_environment()
    
    manager = git_wal.SystemManager()
    
    # Verify initial state
    print("\nInitial DB and Git states:")
    manager.print_db_state()
    
    # Overwrite the pre-receive hook in repo1 with our custom slow hook (6.0s sleep)
    print("[Setup] Writing custom delayed pre-receive hook for repo1 (6.0s delay)...")
    repo1_hook_path = os.path.join(git_wal.BASE_DIR, "repo1", "hooks", "pre-receive")
    
    custom_hook_content = f"""#!/usr/bin/env python3
import sys
import os
import socket
import time

ALLOWED_TRIGGER = {repr(ALLOWED_TRIGGER)}

if os.environ.get("DISABLE_WAL_LOCKS") == "1":
    sys.exit(0)

pwd = os.getcwd()
repo_name = "repo1"

socket_path = "/tmp/wal_lock.sock"
try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(socket_path)
    active_tx_id = os.environ.get("ACTIVE_WAL_TX_ID", "None")
    
    # Request format: "repo_name:active_tx_id"
    message = repo_name + ":" + active_tx_id
    client.sendall(message.encode('utf-8'))
    response = client.recv(1024).decode('utf-8').strip()
    client.close()
    
    if response == "locked":
        sys.stderr.write("Push rejected: repo1 is locked\\n")
        sys.exit(1)
    elif response == "allow":
        if active_tx_id != "None":
            # Coordinated transaction pushes should not delay
            sys.stderr.write("Hook allowed (Coordinated Push), resuming immediately\\n")
            sys.exit(0)

        # Write trigger file to signal we received 'allow' from Lock Daemon
        with open(ALLOWED_TRIGGER, "w") as f:
            f.write("allowed")
        
        # Emulate slow SCM physical update phase by sleeping for 6.0 seconds.
        # This completely exceeds the Lock Daemon's 5.0-second temporary push lease!
        sys.stderr.write("Hook allowed, emulating physical transfer delay (6.0s)...\\n")
        time.sleep(6.0)
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

    # Prepare developer workspace to perform the direct push
    dev_ws = os.path.join(git_wal.BASE_DIR, "developer_workspace_pre_lock")
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
        f.write("Developer Slow Push Change\n")
    git_wal.run_cmd(["git", "add", "developer_data.txt"], cwd=dev_ws)
    git_wal.run_cmd(["git", "commit", "-m", "Developer Slow Push Commit (D1)"], cwd=dev_ws)
    
    developer_sha = git_wal.run_cmd(["git", "rev-parse", "main"], cwd=dev_ws).stdout.strip()
    print(f"[Developer] Target developer commit SHA: {developer_sha[:8]}")

    # Launch Thread 1: Direct developer push to repo1 (slow push)
    def developer_push_worker():
        print("[Thread 1 - Developer] Starting direct push to repo1...")
        res = git_wal.run_cmd(["git", "push", "origin", "main:main"], cwd=dev_ws, check=False)
        print(f"[Thread 1 - Developer] Push finished with return code: {res.returncode}")
        if res.stderr:
            print(f"[Thread 1 - Developer] Push stderr:\n{res.stderr}")

    t1 = threading.Thread(target=developer_push_worker, name="Thread-1")
    t1.start()

    # Wait for the developer's pre-receive hook to receive 'allow' and start sleeping
    print("[Main] Waiting for developer pre-receive hook to receive 'allow'...")
    while not os.path.exists(ALLOWED_TRIGGER):
        if not t1.is_alive():
            print("ERROR: Developer push thread died unexpectedly before hook was allowed!")
            git_wal.stop_postgres()
            sys.exit(1)
        time.sleep(0.1)
    print("[Main] Developer pre-receive hook has been ALLOWED and is now in its physical gap delay.")

    # Sleep for 5.2 seconds to ensure the 5-second Lock Daemon lease lock has been released!
    print("[Main] Sleeping for 5.2 seconds to let the push lease expire...")
    time.sleep(5.2)
    print("[Main] Lease expired. Launching concurrent transaction...")

    # Launch Thread 2: Coordinated Transaction Manager do_atomic_push targeting ['repo1', 'repo3']
    def manager_push_worker():
        print("[Thread 2 - Manager] Starting atomic push targeting ['repo1', 'repo3']...")
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
    c.execute("SELECT repo, sha FROM state ORDER BY repo")
    db_states = {repo: sha for repo, sha in c.fetchall()}
    
    c.execute("SELECT tx_id, state FROM wal ORDER BY tx_id")
    wal_txs = c.fetchall()
    conn.close()

    # Check the physical SCM states
    git_repo1_sha = manager.get_git_sha("repo1")
    git_repo3_sha = manager.get_git_sha("repo3")

    print("\n--- WAL TRANSACTION LOGS ---")
    for tx_id, state in wal_txs:
        print(f"  TX {tx_id}: {state}")

    print("\n--- STATE COMPARISON ---")
    print(f"Database 'state' record for repo1: {db_states.get('repo1', 'None')[:8]}")
    print(f"Physical Git HEAD for repo1:       {git_repo1_sha[:8]}")
    print(f"Database 'state' record for repo3: {db_states.get('repo3', 'None')[:8]}")
    print(f"Physical Git HEAD for repo3:       {git_repo3_sha[:8]}")
    
    diverged = (db_states.get('repo1') != git_repo1_sha) or (db_states.get('repo3') != git_repo3_sha)

    # Clean up trigger files
    if os.path.exists(ALLOWED_TRIGGER):
        try: os.remove(ALLOWED_TRIGGER)
        except Exception: pass

    # Stop postgres
    git_wal.stop_postgres()

    if diverged:
        print("\nFAILURE: SCM-DB Distributed Transaction Protocol suffered a catastrophic split-brain race condition!")
        print("[CRITICAL] SCM physical HEAD and Database target state have permanently diverged!")
        sys.exit(1)
    else:
        print("\nSUCCESS: SCM-DB transactional integrity was preserved.")
        sys.exit(0)


if __name__ == "__main__":
    main()
