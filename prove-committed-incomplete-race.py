#!/usr/bin/env python3
"""
prove-committed-incomplete-race.py: Proof of Concept demonstrating a catastrophic
split-brain race condition and permanent state divergence in git-wal.py.
This occurs because the Lock Daemon fails to block direct SCM pushes on repositories
that are part of a COMMITTED but PHYSICALLY INCOMPLETE WAL transaction.
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
    
    # Capture initial SHA of repositories
    initial_sha = manager.get_git_sha("repo3")
    print(f"Initial physical SHA for repo3: {initial_sha[:8]}")

    print_banner("SIMULATING COORDINATOR CRASH MID-FF (AFTER DB COMMIT, BEFORE SCM UPDATES)")
    
    # We monkeypatch update_git_ref to raise an exception right after database transaction commits,
    # preventing any SCM main branch fast-forwards.
    original_update_git_ref = manager.update_git_ref
    
    def custom_update_git_ref(repo_name, sha, ref_name="refs/heads/main", old_sha=None):
        print(f"[PATCH] Refusing to update {repo_name} to simulate coordinator crash...")
        raise RuntimeError("SIMULATED CRASH right after DB commit, before SCM ref update!")
        
    manager.update_git_ref = custom_update_git_ref

    # Execute atomic push targeting ['repo1', 'repo3']
    print("[Manager] Running atomic push targeting ['repo1', 'repo3']...")
    try:
        manager.do_atomic_push(["repo1", "repo3"], "Commit with Post-Commit Crash")
    except Exception as e:
        print(f"[Manager] Expected exception caught: {e}")

    # Restore the original update_git_ref so subsequent healing can run normally
    manager.update_git_ref = original_update_git_ref

    print_banner("EVALUATING INCOMPLETE COMMITTED STATE")
    
    # Check DB state
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("SELECT tx_id, state FROM wal ORDER BY tx_id DESC LIMIT 1")
    last_wal = c.fetchone()
    c.execute("SELECT repo, sha FROM state WHERE repo = 'repo3'")
    db_repo3_sha = c.fetchone()[1]
    conn.close()

    git_repo3_sha = manager.get_git_sha("repo3")
    print(f"Database WAL state for TX {last_wal[0]}: {last_wal[1]}")
    print(f"Database state record for repo3: {db_repo3_sha[:8]}")
    print(f"Physical SCM HEAD for repo3:       {git_repo3_sha[:8]}")
    
    # Confirm that the physical SCM remains at initial SHA while DB is updated to post_sha
    if git_repo3_sha == initial_sha and db_repo3_sha != initial_sha:
        print("[OK] Verified: DB transaction committed, but physical SCM references were NOT updated.")
    else:
        print("[ERROR] Simulated crash setup failed.")
        git_wal.stop_postgres()
        sys.exit(1)

    print_banner("CONCURRENT DIRECT SCM PUSH (EXPLOITING THE PHYSICALLY INCOMPLETE WINDOW)")
    
    # Clone repo3 to a developer workspace and push a change.
    # Since the WAL state is 'committed', the Lock Daemon will allow this direct push!
    dev_ws = os.path.join(git_wal.BASE_DIR, "developer_workspace_incomplete")
    if os.path.exists(dev_ws):
        shutil.rmtree(dev_ws)
        
    print("[Developer] Cloning repo3...")
    git_wal.run_cmd(["git", "clone", os.path.join(git_wal.BASE_DIR, "repo3"), dev_ws])
    
    print("[Developer] Checking out main branch...")
    git_wal.run_cmd(["git", "checkout", "main"], cwd=dev_ws)
    
    print("[Developer] Creating a concurrent developer commit on repo3...")
    git_wal.run_cmd(["git", "config", "user.name", "External Developer"], cwd=dev_ws)
    git_wal.run_cmd(["git", "config", "user.email", "developer@scmdirect.com"], cwd=dev_ws)
    
    with open(os.path.join(dev_ws, "developer_data.txt"), "w") as f:
        f.write("Developer Direct Change\n")
    git_wal.run_cmd(["git", "add", "developer_data.txt"], cwd=dev_ws)
    git_wal.run_cmd(["git", "commit", "-m", "Developer Commit (D1)"], cwd=dev_ws)
    
    developer_sha = git_wal.run_cmd(["git", "rev-parse", "main"], cwd=dev_ws).stdout.strip()
    print(f"[Developer] New developer commit SHA: {developer_sha[:8]}")
    
    print("[Developer] Pushing directly to repo3...")
    # This push should succeed because the Lock Daemon only blocks 'pending' transactions!
    push_res = git_wal.run_cmd(["git", "push", "origin", "HEAD:main"], cwd=dev_ws, check=False)
    print(f"[Developer] Push finished with return code: {push_res.returncode}")
    if push_res.stderr:
        print(f"[Developer] Push output:\n{push_res.stderr.strip()}")

    if push_res.returncode != 0:
        print("[OK] Direct push was successfully REJECTED! The Lock Daemon correctly blocked direct pushes during the committed but physically incomplete window.")
    else:
        print("[VULNERABILITY TRIGGERED] Lock Daemon allowed direct push on physically incomplete committed repo3!")
        git_wal.stop_postgres()
        sys.exit(1)

    print_banner("TRIGGERING HEALING PROCESS (SUBSEQUENT OPERATION)")
    
    # A subsequent write operation starts and tries to push to repo1.
    # It will detect the physically incomplete committed transaction and try to roll it forward (heal it).
    print("[Manager] Running subsequent atomic push targeting ['repo1'] to trigger healing of TX 1...")
    try:
        res = manager.do_atomic_push(["repo1"], "Subsequent Atomic Commit")
        print(f"[Manager] do_atomic_push result: {res}")
    except Exception as e:
        print(f"[Manager] Healing failed with exception: {e}")

    print_banner("EVALUATING FINAL PERMANENT SPLIT-BRAIN")
    
    # Check DB state
    conn = psycopg.connect(f"host=/tmp port={git_wal.PG_PORT} dbname=postgres user={git_wal.PG_USER}")
    c = conn.cursor()
    c.execute("SELECT repo, sha FROM state ORDER BY repo")
    db_states = {repo: sha for repo, sha in c.fetchall()}
    conn.close()

    # Check physical SCM HEADs
    final_repo1_sha = manager.get_git_sha("repo1")
    final_repo3_sha = manager.get_git_sha("repo3")

    print(f"Database state for repo1: {db_states.get('repo1', 'None')[:8]}")
    print(f"Physical Git HEAD for repo1: {final_repo1_sha[:8]}")
    print(f"Database state for repo3: {db_states.get('repo3', 'None')[:8]}")
    print(f"Physical Git HEAD for repo3: {final_repo3_sha[:8]}")

    diverged = (db_states.get('repo3') != final_repo3_sha) or (db_states.get('repo1') != final_repo1_sha)

    # Clean up postgres
    git_wal.stop_postgres()

    if diverged:
        print("\nFAILURE: SCM-DB Distributed Transaction Protocol suffered permanent split-brain divergence!")
        print("[CRITICAL] SCM and database are not aligned!")
        sys.exit(1)
    else:
        print("\nSUCCESS: Transactional integrity was preserved and SCM/DB states have successfully aligned after healing.")
        sys.exit(0)


if __name__ == "__main__":
    main()
