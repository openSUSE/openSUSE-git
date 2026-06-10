#!/usr/bin/env python3
"""
prove-real-split-brain.py: Proof of Concept demonstrating a catastrophic
split-brain race condition and permanent SCM-DB state divergence in git-wal.py.

This demonstrates how a severed database connection right after the pre-receive
hook has allowed a push, but before the physical ref update completes, allows
a concurrent transaction to "heal" (abort) the transaction, leading to physical SCM
updating to post_sha while the database remains permanently at pre_sha.
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
RESUME_TRIGGER = os.path.join(git_wal.BASE_DIR, "hook_resume_trigger")

def print_banner(msg):
    print("\n" + "=" * 80)
    print(f" {msg}")
    print("=" * 80)

def main():
    print_banner("INITIALIZING SYSTEM ENVIRONMENT")
    # Clean up any lingering trigger files first
    for f in [ALLOWED_TRIGGER, RESUME_TRIGGER]:
        if os.path.exists(f):
            os.remove(f)

    # Initialize pristine environment (starts postgres, daemon, creates tables and repos)
    git_wal.setup_environment()
    
    manager = git_wal.SystemManager()
    
    # Verify initial state
    print("\nInitial DB and Git states:")
    manager.print_db_state()
    
    # Overwrite the pre-receive hook in repo3 with our custom synchronized hook
    print("[Setup] Writing custom synchronized pre-receive hook for repo3...")
    repo3_hook_path = os.path.join(git_wal.BASE_DIR, "repo3", "hooks", "pre-receive")
    
    custom_hook_content = f"""#!/usr/bin/env python3
import sys
import os
import socket
import time

ALLOWED_TRIGGER = {repr(ALLOWED_TRIGGER)}
RESUME_TRIGGER = {repr(RESUME_TRIGGER)}

if os.environ.get("DISABLE_WAL_LOCKS") == "1":
    sys.exit(0)

pwd = os.getcwd()
repo_name = "repo3"

socket_path = "/tmp/wal_lock.sock"
try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(socket_path)
    active_tx_id = os.environ.get("ACTIVE_WAL_TX_ID", "None")
    
    message = repo_name + ":" + active_tx_id
    client.sendall(message.encode('utf-8'))
    response = client.recv(1024).decode('utf-8').strip()
    client.close()
    
    if response == "locked":
        sys.stderr.write("Push rejected: repo3 is locked\\n")
        sys.exit(1)
    elif response == "allow":
        # Write trigger file to signal we received 'allow' from Lock Daemon
        with open(ALLOWED_TRIGGER, "w") as f:
            f.write("allowed")
        
        # Wait until resume trigger file is created by the coordinator
        while not os.path.exists(RESUME_TRIGGER):
            time.sleep(0.1)
            
        sys.stderr.write("Hook allowed, resuming push to repo3\\n")
        sys.exit(0)
    else:
        sys.stderr.write("Push rejected: unknown response: " + response + "\\n")
        sys.exit(1)
except Exception as e:
    sys.stderr.write("Push rejected: Lock Daemon connection failed: " + str(e) + "\\n")
    sys.exit(1)
"""
    with open(repo3_hook_path, "w") as hf:
        hf.write(custom_hook_content)
    os.chmod(repo3_hook_path, 0o755)

    # We intercept psycopg.connect to capture Thread 1's database connections
    original_connect = psycopg.connect
    thread1_conns = []
    
    def custom_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        if threading.current_thread().name == "Thread-1":
            thread1_conns.append(conn)
        return conn
        
    psycopg.connect = custom_connect

    original_update_git_ref = manager.update_git_ref
    def custom_update_git_ref(repo_name, sha, ref_name="refs/heads/main", old_sha=None):
        if threading.current_thread().name == "Thread-1":
            print(f"[PATCH] Thread 1 is trying to roll back {repo_name} during exception cleanup. Refusing to let it clean up (simulating hard process crash)...")
            return # Simulate process crash by doing nothing
        return original_update_git_ref(repo_name, sha, ref_name, old_sha)
    manager.update_git_ref = custom_update_git_ref
    
    # Thread 1 worker (Pusher targeting ['repo1', 'repo3'])
    def thread1_worker():
        print("[Thread 1] Starting atomic push targeting ['repo1', 'repo3']...")
        try:
            # This will succeed in pushing to repo1, then push to repo3
            res = manager.do_atomic_push(["repo1", "repo3"], "Commit From Thread 1 (T1)")
            print(f"[Thread 1] do_atomic_push finished. Result: {res}")
        except Exception as e:
            print(f"[Thread 1] do_atomic_push raised expected connection error: {e}")

    t1 = threading.Thread(target=thread1_worker, name="Thread-1")
    t1.start()
    
    # Wait for repo3's pre-receive hook to receive 'allow' and pause
    print("[Main] Waiting for repo3 pre-receive hook to receive 'allow'...")
    while not os.path.exists(ALLOWED_TRIGGER):
        time.sleep(0.1)
    print("[Main] repo3 pre-receive hook has been ALLOWED and is now paused in-flight.")

    # Now, simulate Connection Loss for Thread 1 to release PostgreSQL locks
    print_banner("SIMULATING DATABASE CONNECTION LOSS")
    closed_count = 0
    for conn in thread1_conns:
        try:
            if not conn.closed:
                conn.close()
                closed_count += 1
        except Exception as e:
            print(f"[Main] Error closing Thread 1 connection: {e}")
    print(f"[Main] Closed {closed_count} Thread 1 database connection(s). Row-level and advisory locks are now released.")

    # At this moment, Thread 2 starts. It targets repo1 and triggers concurrent healing.
    print_banner("CONCURRENT TRANSACTION & HEALING INTRUSION (THREAD 2)")
    print("[Thread 2] Starting concurrent atomic push targeting ['repo1']...")
    res2 = manager.do_atomic_push(["repo1"], "Commit From Thread 2 (T2)")
    print(f"[Thread 2] do_atomic_push finished. Result: {res2}")

    # Now, resume repo3's hook to let the physical Git update ref finish!
    print_banner("RESUMING IN-FLIGHT SCM REF UPDATE")
    print("[Main] Signaling repo3 pre-receive hook to resume...")
    with open(RESUME_TRIGGER, "w") as f:
        f.write("resume")

    # Wait for Thread 1 to finish completely
    t1.join()

    print_banner("EVALUATING FINAL INCONSISTENT STATE")
    # Let's inspect database state
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

    # Clean up trigger files
    for f in [ALLOWED_TRIGGER, RESUME_TRIGGER]:
        if os.path.exists(f):
            os.remove(f)

    # Stop postgres
    git_wal.stop_postgres()

    diverged = False
    if db_repo3_sha != git_repo3_sha:
        print("\n[CRITICAL FAILURE] repo3 has permanently diverged!")
        print(f"  Database thinks repo3 is at: {db_repo3_sha[:8] if db_repo3_sha else 'None'}")
        print(f"  Physical Git repo3 is at:    {git_repo3_sha[:8] if git_repo3_sha else 'None'}")
        diverged = True

    if diverged:
        print("\nFAILURE: System suffered a catastrophic split-brain race condition!")
        sys.exit(1)
    else:
        print("\nSUCCESS: SCM-DB transactional integrity was preserved.")
        sys.exit(0)

if __name__ == "__main__":
    main()
