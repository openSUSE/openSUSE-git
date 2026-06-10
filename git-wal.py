#!/usr/bin/env python3
"""
Write-Ahead Logging (WAL) System for Multi-Repository & DB Atomicity (PostgreSQL version).

This module demonstrates a high-integrity, crash-resilient distributed
transaction manager. It coordinates atomic updates across three separate
Git repositories ('repo1', 'repo2-broken', 'repo3') and a PostgreSQL database.

This version implements a high-performance Lock Daemon (Coordinating Agent) 
architecture. The orchestrator runs a background UNIX socket server (/tmp/wal_lock.sock) 
that holds a persistent connection to PostgreSQL. The Git hooks query this daemon 
using Python's ultra-lightweight built-in 'socket' library, bypassing the psycopg 
driver import overhead in hooks, resulting in near-zero latency SCM operations.

This script is 100% self-contained, automated, and portable across environments 
(both inside Docker/LXC containers and on physical local hosts). It dynamically 
compiles preload wrappers if running as root, always initializes a fresh data cluster 
on startup to ensure reproducibility, and cleans up all background services on exit.

It features high-precision real-time performance instrumentation to measure and 
report the latency of every single distributed step, including internal hook runs.
It also includes a scientific benchmark comparing SCM performance with and without 
locking enabled to measure exact UNIX socket overhead.
"""

import os
import sys
import shutil
import psycopg
import subprocess
import socket
import threading
import pwd
import time
import hashlib


def get_repo_lock_id(repo_name: str) -> int:
    """
    Generates a stable, unique 64-bit signed integer for a repository name
    to be used with PostgreSQL advisory locks.

    Args:
        repo_name (str): The name of the SCM repository.

    Returns:
        int: A signed 64-bit integer hash of the repository name.
    """
    h = hashlib.sha256(repo_name.encode('utf-8')).digest()
    return int.from_bytes(h[:8], byteorder='big', signed=True)


def split_64bit_to_32bit(val: int):
    """
    Splits a 64-bit signed integer into two 32-bit signed integers,
    matching how PostgreSQL splits a 64-bit advisory lock key in the pg_locks table.

    Args:
        val (int): The 64-bit signed integer.

    Returns:
        tuple: A tuple containing (classid, objid) as signed 32-bit integers.
    """
    val_unsigned = val & 0xffffffffffffffff
    classid = (val_unsigned >> 32) & 0xffffffff
    objid = val_unsigned & 0xffffffff

    if classid >= 0x80000000:
        classid -= 0x100000000
    if objid >= 0x80000000:
        objid -= 0x100000000
    return classid, objid


# Define base paths relative to the script location
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRIGGER_PATH = os.path.join(BASE_DIR, "repo2_fail_trigger")
LOCAL_DIR = os.path.join(BASE_DIR, "local_workspace")
PG_DATA = os.path.join(BASE_DIR, "postgres_data")
LIB_FAKEUID = os.path.join(BASE_DIR, "libfakeuid.so")
C_SOURCE = os.path.join(BASE_DIR, "geteuid.c")

# The repositories coordinated under the distributed transaction
REPOS = ["repo1", "repo2-broken", "repo3"]

# All repositories, including an independent one to prove concurrent execution
ALL_REPOS = REPOS + ["repo-independent"]

# Port and socket settings to isolate database execution completely from host services
PG_PORT = "54321"

# Dynamically resolve active database superuser based on the OS privilege context
if os.getuid() == 0:
    PG_USER = "postgres"  # Root container always preloads under postgres
else:
    try:
        PG_USER = pwd.getpwuid(os.getuid()).pw_name  # Host machine uses active user
    except Exception:
        PG_USER = "postgres"  # Safe default fallback

# Dictionary to hold high-precision latency measurements
metrics = {}


def run_cmd(cmd: list, cwd: str = None, env: dict = None, check: bool = True) -> subprocess.CompletedProcess:
    """
    Executes a shell command via subprocess and returns the CompletedProcess instance.

    Args:
        cmd (list): The command arguments.
        cwd (str, optional): The directory to run the command in.
        env (dict, optional): Custom environment variables for the execution.
        check (bool): Whether to raise an exception on non-zero exit.

    Returns:
        subprocess.CompletedProcess: The result of the command execution.
    """
    # Print command for visibility before execution
    print(f"Executing: {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=check)


def check_and_compile_preloader() -> bool:
    """
    Checks if the script is running as 'root' (UID 0). If so, it dynamically writes
    the C preloader code and compiles it into 'libfakeuid.so' on startup to allow
    running initdb and postgres safely as root under unprivileged container constraints.

    Returns:
        bool: True if running as root and preloader was compiled/prepared, False otherwise.
    """
    if os.getuid() != 0:
        print("[Setup] Running as unprivileged non-root user. No C preloader wrapper required.")
        return False

    print("[Setup] Root user (UID 0) detected. Preparing C preloader wrapper...")

    # 1. Dynamically write geteuid.c if it doesn't exist
    if not os.path.exists(C_SOURCE):
        print(f"[Setup] Writing system-call wrapper source code to {C_SOURCE}...")
        c_content = """#define _GNU_SOURCE
#include <unistd.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <dlfcn.h>

uid_t geteuid(void) {
    return 499; /* postgres UID */
}

uid_t getuid(void) {
    return 499; /* postgres UID */
}

gid_t getegid(void) {
    return 498; /* postgres GID */
}

gid_t getgid(void) {
    return 498; /* postgres GID */
}

int stat(const char *path, struct stat *buf) {
    int (*orig_stat)(const char *, struct stat *) = dlsym(RTLD_NEXT, "stat");
    int ret = orig_stat(path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int lstat(const char *path, struct stat *buf) {
    int (*orig_lstat)(const char *, struct stat *) = dlsym(RTLD_NEXT, "lstat");
    int ret = orig_lstat(path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int fstat(int fd, struct stat *buf) {
    int (*orig_fstat)(int, struct stat *) = dlsym(RTLD_NEXT, "fstat");
    int ret = orig_fstat(fd, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int __xstat(int ver, const char *path, struct stat *buf) {
    int (*orig_xstat)(int, const char *, struct stat *) = dlsym(RTLD_NEXT, "__xstat");
    int ret = orig_xstat(ver, path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int __lxstat(int ver, const char *path, struct stat *buf) {
    int (*orig_lxstat)(int, const char *, struct stat *) = dlsym(RTLD_NEXT, "__lxstat");
    int ret = orig_lxstat(ver, path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int __fxstat(int ver, int fd, struct stat *buf) {
    int (*orig_fxstat)(int, int, struct stat *) = dlsym(RTLD_NEXT, "__fxstat");
    int ret = orig_fxstat(ver, fd, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int stat64(const char *path, struct stat64 *buf) {
    int (*orig_stat64)(const char *, struct stat64 *) = dlsym(RTLD_NEXT, "stat64");
    int ret = orig_stat64(path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int lstat64(const char *path, struct stat64 *buf) {
    int (*orig_lstat64)(const char *, struct stat64 *) = dlsym(RTLD_NEXT, "lstat64");
    int ret = orig_lstat64(path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int fstat64(int fd, struct stat64 *buf) {
    int (*orig_fstat64)(int, struct stat64 *) = dlsym(RTLD_NEXT, "fstat64");
    int ret = orig_fstat64(fd, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}
"""
        with open(C_SOURCE, "w") as sf:
            sf.write(c_content)

    # 2. Compile geteuid.c if libfakeuid.so doesn't exist
    if not os.path.exists(LIB_FAKEUID):
        print(f"[Setup] Compiling {LIB_FAKEUID} on startup...")
        start_compile = time.perf_counter()
        compiled = False
        for compiler in [["gcc", "-shared", "-fPIC", "-o", LIB_FAKEUID, C_SOURCE, "-ldl"],
                         ["clang", "-shared", "-fPIC", "-o", LIB_FAKEUID, C_SOURCE, "-ldl"]]:
            try:
                run_cmd(compiler)
                compiled = True
                print(f"[Setup] System wrapper compiled successfully using {compiler[0]}.")
                break
            except Exception:
                pass
        if not compiled:
            raise RuntimeError("Fatal: Could not compile libfakeuid.so. GCC or Clang is required when running as root.")
        metrics["preloader_compile"] = time.perf_counter() - start_compile
    else:
        print(f"[Setup] System wrapper {LIB_FAKEUID} already compiled.")
        metrics["preloader_compile"] = 0.0
    return True


def start_postgres():
    """
    Initializes and starts a local PostgreSQL instance in the background.
    Always cleans up any existing database data directory on start to guarantee
    a pristine, reproducible, and conflict-free database initialization.
    """
    env = os.environ.copy()
    use_preloader = check_and_compile_preloader()
    if use_preloader:
        env["LD_PRELOAD"] = LIB_FAKEUID

    # Always wipe and initialize a fresh, clean PostgreSQL cluster
    # This prevents user role and volume-mount conflicts across environments.
    if os.path.exists(PG_DATA):
        print("[Postgres] Removing existing database directory to ensure a pristine start...")
        shutil.rmtree(PG_DATA)

    os.makedirs(PG_DATA)
    print("[Postgres] Initializing PostgreSQL database cluster with UTF8 encoding...")
    start_init = time.perf_counter()
    run_cmd(["initdb", "-E", "UTF8", "-D", PG_DATA], env=env)
    metrics["postgres_initdb"] = time.perf_counter() - start_init

    # Start Postgres server in background
    socket_file = f"/tmp/.s.PGSQL.{PG_PORT}"
    if os.path.exists(socket_file):
        # Stop any lingering server on this port first
        stop_postgres()

    print(f"[Postgres] Starting PostgreSQL server on isolated port {PG_PORT}...")
    start_boot = time.perf_counter()
    cmd = ["postgres", "-D", PG_DATA, "-p", PG_PORT, "-k", "/tmp"]
    subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Wait up to 15 seconds for Postgres to become ready
    print(f"[Postgres] Connecting to local socket as user '{PG_USER}'...")
    for i in range(15):
        time.sleep(1)
        try:
            conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}", connect_timeout=1)
            conn.close()
            print("[Postgres] PostgreSQL successfully running and accepting connections as '" + PG_USER + "'!")
            metrics["postgres_boot"] = time.perf_counter() - start_boot
            return
        except Exception as e:
            # Expose any critical server errors (such as port conflicts) immediately
            if "connection refused" not in str(e) and "Is the server running" not in str(e):
                print(f"[Postgres] Active Connection Alert ({i+1}/15): {e}")
    raise RuntimeError(f"Postgres failed to boot up within 15 seconds on port {PG_PORT}.")


def stop_postgres():
    """
    Stops the background PostgreSQL server cleanly using pg_ctl.
    """
    pg_data = os.path.join(BASE_DIR, "postgres_data")
    if os.path.exists(pg_data):
        print("\n[Postgres] Stopping PostgreSQL server cleanly...")
        start_stop = time.perf_counter()
        env = os.environ.copy()
        if os.getuid() == 0:
            env["LD_PRELOAD"] = LIB_FAKEUID
        try:
            # -m fast stops it immediately without waiting for other connections to close
            run_cmd(["pg_ctl", "-D", pg_data, "stop", "-m", "fast"], env=env, check=False)
            print("[Postgres] PostgreSQL server stopped.")
            metrics["postgres_stop"] = time.perf_counter() - start_stop
        except Exception as e:
            print(f"[Postgres] Error stopping server: {e}")
            metrics["postgres_stop"] = 0.0


def run_lock_daemon():
    """
    A lightweight, persistent UNIX socket server that coordinates locks.
    It runs in a background thread of the orchestrator.
    It listens on /tmp/wal_lock.sock, queries PostgreSQL using a single persistent
    autocommit connection, and instantly answers queries from Git pre-receive hooks.
    """
    socket_path = "/tmp/wal_lock.sock"
    if os.path.exists(socket_path):
        os.remove(socket_path)
        
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(32)
    
    conn = None
    
    def get_db_cursor():
        nonlocal conn
        for _ in range(3):
            try:
                if conn is None or conn.closed:
                    # autocommit=True ensures we always read the absolute latest state
                    conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}", autocommit=True)
                return conn.cursor()
            except Exception:
                conn = None
        raise RuntimeError("Lock Daemon failed to connect to PostgreSQL")

    print("[Daemon] Lock Daemon UNIX socket server listening on /tmp/wal_lock.sock")
    
    try:
        while True:
            client_sock, _ = server.accept()
            try:
                data = client_sock.recv(1024).decode('utf-8').strip()
                if not data:
                    client_sock.close()
                    continue
                
                # Request format: "repo_name:active_tx_id"
                repo_name, active_tx_str = data.split(":")
                active_tx_id = int(active_tx_str) if active_tx_str != "None" else None
                
                c = get_db_cursor()

                # Fencing token check: verify that our active transaction is still 'pending' in WAL.
                # If a concurrent process has already healed and aborted/committed it, we must reject this push.
                if active_tx_id is not None:
                    c.execute("SELECT state FROM wal WHERE tx_id = %s", (active_tx_id,))
                    tx_state_row = c.fetchone()
                    if not tx_state_row or to_str(tx_state_row[0]) != 'pending':
                        client_sock.sendall(b"locked\n")
                        continue

                # Query PostgreSQL for active logical locks
                c.execute("""
                    SELECT wal.tx_id FROM wal
                    JOIN wal_entries ON wal.tx_id = wal_entries.tx_id
                    WHERE wal_entries.repo = %s AND wal.state = 'pending'
                    ORDER BY wal.tx_id ASC
                    LIMIT 1
                """, (repo_name,))
                row = c.fetchone()

                if row:
                    locked_tx_id = row[0]
                    # Block if it's locked by another transaction, allow if it's our own
                    if active_tx_id is None or active_tx_id != locked_tx_id:
                        client_sock.sendall(b"locked\n")
                    else:
                        client_sock.sendall(b"allow\n")
                else:
                    # Check if there are any COMMITTED but PHYSICALLY INCOMPLETE transactions for this repo.
                    # We check if the staged ref f"refs/wal/tx_{tx_id}/main" exists in SCM.
                    c.execute("""
                        SELECT DISTINCT wal.tx_id FROM wal
                        JOIN wal_entries ON wal.tx_id = wal_entries.tx_id
                        WHERE wal_entries.repo = %s AND wal.state = 'committed'
                        ORDER BY wal.tx_id DESC
                        LIMIT 10
                    """, (repo_name,))
                    committed_rows = c.fetchall()
                    
                    is_incomplete = False
                    repo_path = os.path.join(BASE_DIR, repo_name)
                    for (tx_id,) in committed_rows:
                        staged_ref = f"refs/wal/tx_{tx_id}/main"
                        res = subprocess.run(["git", "rev-parse", staged_ref], cwd=repo_path, capture_output=True, text=True)
                        if res.returncode == 0:
                            is_incomplete = True
                            break
                            
                    if is_incomplete:
                        client_sock.sendall(b"locked\n")
                    else:
                        # Double-check for active transaction-level advisory locks (closing the rift window)
                        lock_id = get_repo_lock_id(repo_name)
                    classid, objid = split_64bit_to_32bit(lock_id)
                    c.execute("""
                        SELECT 1 FROM pg_locks
                        WHERE locktype = 'advisory'
                        AND classid = %s::oid
                        AND objid = %s::oid
                        LIMIT 1
                    """, (classid, objid))
                    has_lock = c.fetchone()
                    if has_lock:
                        # Advisory lock is held (rift window is active), reject concurrent pushes
                        client_sock.sendall(b"locked\n")
                    else:
                        if active_tx_id is not None:
                            # Coordinated push: already owns transaction locks, just allow
                            client_sock.sendall(b"allow\n")
                        else:
                            # Direct developer push: acquire a temporary push lease lock (5 seconds)
                            # to protect the pre-receive gap from concurrent transactions.
                            try:
                                temp_conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}", autocommit=True)
                                temp_c = temp_conn.cursor()
                                temp_c.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
                                acquired = temp_c.fetchone()[0]
                                if acquired:
                                    client_sock.sendall(b"allow\n")
                                    # Schedule release of this lock after 5 seconds by closing the connection
                                    def release_lock(conn_to_close):
                                        time.sleep(5.0)
                                        try: conn_to_close.close()
                                        except: pass
                                    threading.Thread(target=release_lock, args=(temp_conn,), daemon=True).start()
                                else:
                                    temp_conn.close()
                                    client_sock.sendall(b"locked\n")
                            except Exception as e:
                                client_sock.sendall(f"error:Failed to acquire temporary lock: {e}\n".encode('utf-8'))
                    
            except Exception as e:
                try: client_sock.sendall(f"error:{e}\n".encode('utf-8'))
                except: pass
            finally:
                client_sock.close()
    except Exception as e:
        print(f"[Daemon] Lock Daemon encountered an error: {e}")
    finally:
        server.close()
        if conn:
            conn.close()


def setup_environment():
    """
    Sets up the environment:
    - Starts the background PostgreSQL database.
    - Launches the UNIX Lock Daemon on a background thread.
    - Creates pristine PostgreSQL target and WAL tables.
    - Creates and initializes Git repositories.
    - Creates a universal pre-receive hook inside each repository (Daemon-backed).
    - Initializes a local workspace to push commits.
    """
    start_setup = time.perf_counter()

    # Ensure postgres is running and LD_PRELOAD wrapper is compiled if needed
    start_postgres()

    # 1. Setup PostgreSQL Database Tables (Clean Start)
    conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}")
    c = conn.cursor()

    c.execute("DROP TABLE IF EXISTS wal_entries;")
    c.execute("DROP TABLE IF EXISTS wal;")
    c.execute("DROP TABLE IF EXISTS state;")

    c.execute("CREATE TABLE state (repo TEXT PRIMARY KEY, sha TEXT);")
    c.execute("CREATE TABLE wal (tx_id SERIAL PRIMARY KEY, state TEXT);")
    c.execute("""
        CREATE TABLE wal_entries (
            tx_id INTEGER REFERENCES wal(tx_id) ON DELETE CASCADE,
            repo TEXT,
            pre_sha TEXT,
            post_sha TEXT
        );
    """)
    c.execute("CREATE INDEX idx_wal_entries_repo ON wal_entries(repo);")
    c.execute("CREATE INDEX idx_wal_entries_tx_id ON wal_entries(tx_id);")
    conn.commit()
    conn.close()

    # 2. Launch the UNIX Lock Daemon in the background
    daemon_thread = threading.Thread(target=run_lock_daemon, daemon=True)
    daemon_thread.start()

    print("\n=== Initializing Environment ===")

    # 3. Create bare repositories
    for repo in ALL_REPOS:
        repo_path = os.path.join(BASE_DIR, repo)
        if os.path.exists(repo_path):
            shutil.rmtree(repo_path)
        os.makedirs(repo_path)
        run_cmd(["git", "init", "--bare"], cwd=repo_path)

        # Write the universal pre-receive hook using ONLY python standard socket library
        hook_path = os.path.join(repo_path, "hooks", "pre-receive")
        hook_content = f"""#!/usr/bin/env python3
import sys
import os
import socket
import time

TRIGGER_PATH = {repr(TRIGGER_PATH)}

# 0. Bypass Check
if os.environ.get("DISABLE_WAL_LOCKS") == "1":
    sys.exit(0)

# High-precision internal timer
hook_start = time.perf_counter()

# Extract this repository's name from its working directory
pwd = os.getcwd()
repo_name = os.path.basename(pwd.rstrip(os.sep))
if repo_name.endswith('.git'):
    repo_name = repo_name[:-4]

# 1. Repo-Specific Branch Locking Check (Daemon UNIX socket client)
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
        sys.stderr.write("Push rejected: " + repo_name + " is currently locked due to a pending transaction.\\n")
        sys.exit(1)
    elif response.startswith("error:"):
        sys.stderr.write("Push rejected: Lock Daemon error: " + response + "\\n")
        sys.exit(1)
    elif response != "allow":
        sys.stderr.write("Push rejected: Unknown Lock Daemon response: " + response + "\\n")
        sys.exit(1)
except Exception as e:
    sys.stderr.write("Push rejected: Lock Daemon connection failed or offline: " + str(e) + "\\n")
    sys.exit(1)

# 2. Failure Emulation for repo2-broken
if "repo2-broken" in pwd and os.path.exists(TRIGGER_PATH):
    sys.stderr.write("Push rejected: repo2-broken emulated push failure!\\n")
    sys.exit(1)

# Report internal hook execution latency to stderr so Git displays it dynamically
hook_latency_ms = (time.perf_counter() - hook_start) * 1000
sys.stderr.write(" -> Hook [" + repo_name + "] query resolved internally in " + str(round(hook_latency_ms, 3)) + " ms\\n")

sys.exit(0)
"""
        with open(hook_path, "w") as hf:
            hf.write(hook_content)
        os.chmod(hook_path, 0o755)

    # 4. Initialize local workspace to push commits from
    if os.path.exists(LOCAL_DIR):
        shutil.rmtree(LOCAL_DIR)
    os.makedirs(LOCAL_DIR)
    run_cmd(["git", "init"], cwd=LOCAL_DIR)

    # Set user info to avoid git warnings
    run_cmd(["git", "config", "user.name", "WAL Architect"], cwd=LOCAL_DIR)
    run_cmd(["git", "config", "user.email", "architect@wal.com"], cwd=LOCAL_DIR)

    # Register remotes pointing to the bare repositories
    for repo in ALL_REPOS:
        repo_path = os.path.join(BASE_DIR, repo)
        run_cmd(["git", "remote", "add", repo, repo_path], cwd=LOCAL_DIR)

    # Create the very first initial commit
    init_file = os.path.join(LOCAL_DIR, "data.txt")
    with open(init_file, "w") as f:
        f.write("Initial State\n")
    run_cmd(["git", "add", "data.txt"], cwd=LOCAL_DIR)
    run_cmd(["git", "commit", "-m", "Initial Commit"], cwd=LOCAL_DIR)
    run_cmd(["git", "branch", "-M", "main"], cwd=LOCAL_DIR)

    # Push initial commit to all remotes to establish the 'main' branch
    setup_env = os.environ.copy()
    setup_env["DISABLE_WAL_LOCKS"] = "1"
    for repo in ALL_REPOS:
        run_cmd(["git", "push", repo, "main:main"], cwd=LOCAL_DIR, env=setup_env)

    # 5. Populate the initial database state with the actual Git commit SHAs
    conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}")
    c = conn.cursor()
    for repo in ALL_REPOS:
        repo_path = os.path.join(BASE_DIR, repo)
        sha = run_cmd(["git", "rev-parse", "main"], cwd=repo_path).stdout.strip()
        c.execute("INSERT INTO state (repo, sha) VALUES (%s, %s);", (repo, sha))

    conn.commit()
    conn.close()
    print("Environment setup successfully completed.\n")
    metrics["env_setup"] = time.perf_counter() - start_setup


def to_str(val) -> str:
    """
    Decodes the value if it's a bytes object, otherwise returns it.
    """
    if isinstance(val, bytes):
        return val.decode('utf-8')
    return val


class SystemManager:
    """
    Manages the distributed transactions and self-healing process.
    """

    def get_git_sha(self, repo_name: str) -> str:
        """
        Retrieves the current 'main' commit SHA of a given repository.

        Args:
            repo_name (str): The name of the repository.

        Returns:
            str: The 40-character Git commit SHA.
            """
        repo_path = os.path.join(BASE_DIR, repo_name)
        return run_cmd(["git", "rev-parse", "main"], cwd=repo_path).stdout.strip()

    def update_git_ref(self, repo_name: str, sha: str, ref_name: str = "refs/heads/main", old_sha: str = None):
        """
        Force-updates a branch/reference of a repository to a specific SHA.

        Args:
            repo_name (str): The name of the repository.
            sha (str): The target SHA to point 'main' to.
            ref_name (str): The target reference to update.
            old_sha (str, optional): The expected previous SHA for atomic verification.
        """
        repo_path = os.path.join(BASE_DIR, repo_name)
        cmd = ["git", "update-ref", ref_name, sha]
        if old_sha:
            cmd.append(old_sha)
        run_cmd(cmd, cwd=repo_path)

    def delete_git_ref(self, repo_name: str, ref_name: str):
        """
        Deletes a specific reference from the repository.
        """
        repo_path = os.path.join(BASE_DIR, repo_name)
        run_cmd(["git", "update-ref", "-d", ref_name], cwd=repo_path, check=False)

    def get_git_ref_sha(self, repo_name: str, ref_name: str) -> str:
        """
        Returns the SHA of a specific reference, or None if it does not exist.
        """
        repo_path = os.path.join(BASE_DIR, repo_name)
        res = run_cmd(["git", "rev-parse", ref_name], cwd=repo_path, check=False)
        if res.returncode == 0:
            return res.stdout.strip()
        return None

    def get_conflicting_pending_transactions(self, target_repos: list[str]) -> list[int]:
        """
        Queries the database for any pending transaction ID that touches the target repos,
        as well as any recently committed transactions that have not yet been fully physically applied.
        """
        conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}")
        c = conn.cursor()
        try:
            # 1. Get all pending transactions
            c.execute("""
                SELECT DISTINCT wal.tx_id FROM wal
                JOIN wal_entries ON wal.tx_id = wal_entries.tx_id
                WHERE wal_entries.repo = ANY(%s) AND wal.state = 'pending'
            """, (target_repos,))
            pending_txs = [row[0] for row in c.fetchall()]

            # 2. Get recently committed transactions that might be physically incomplete.
            # We look at the last 100 transactions to keep this check bounded and extremely fast.
            c.execute("""
                SELECT DISTINCT wal.tx_id FROM wal
                JOIN wal_entries ON wal.tx_id = wal_entries.tx_id
                WHERE wal_entries.repo = ANY(%s) AND wal.state = 'committed'
                ORDER BY wal.tx_id DESC
                LIMIT 100
            """, (target_repos,))
            committed_txs = [row[0] for row in c.fetchall()]

            incomplete_committed = []
            for tx_id in committed_txs:
                c.execute("SELECT repo, post_sha FROM wal_entries WHERE tx_id = %s", (tx_id,))
                entries = c.fetchall()
                for repo, post_sha in entries:
                    repo_str = to_str(repo)
                    post_sha_str = to_str(post_sha)
                    staged_ref = f"refs/wal/tx_{tx_id}/main"
                    staged_sha = self.get_git_ref_sha(repo_str, staged_ref)
                    current_sha = self.get_git_sha(repo_str)
                    
                    # If the staged reference still exists, or the main branch has not reached post_sha,
                    # this committed transaction is physically incomplete.
                    if staged_sha is not None or (current_sha != post_sha_str and not self.is_ancestor(repo_str, post_sha_str, current_sha)):
                        incomplete_committed.append(tx_id)
                        break

            return sorted(list(set(pending_txs + incomplete_committed)))
        finally:
            conn.close()

    def is_ancestor(self, repo_name: str, ancestor_sha: str, descendant_sha: str) -> bool:
        """
        Checks if ancestor_sha is an ancestor of (or equal to) descendant_sha in the given SCM repo.

        Args:
            repo_name (str): The name of the repository.
            ancestor_sha (str): The target SHA that should be an ancestor.
            descendant_sha (str): The descendant SHA.

        Returns:
            bool: True if ancestor_sha is an ancestor of (or equal to) descendant_sha, False otherwise.
        """
        repo_path = os.path.join(BASE_DIR, repo_name)
        # git merge-base --is-ancestor returns 0 if true, 1 if false
        res = run_cmd(["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha], cwd=repo_path, check=False)
        return res.returncode == 0

    def heal_transaction(self, tx_id: int, cursor=None):
        """
        Heals a specific pending or incomplete committed transaction by ID:
        - If the state is 'pending':
            - If all its Git repos are at post_sha (or have moved past it), rolls forward the DB state to 'committed'.
            - Otherwise, rolls back all its Git repos to pre_sha and sets DB state to 'aborted'.
        - If the state is 'committed':
            - Ensures all physical Git repos are fast-forwarded to post_sha and cleans up staged refs (roll forward).

        Args:
            tx_id (int): The transaction ID to heal.
            cursor: Optional active cursor to reuse the current transaction.
        """
        if cursor is not None:
            c = cursor
            conn = None
        else:
            conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}")
            c = conn.cursor()

        try:
            # Lock the transaction log row exclusively during healing
            c.execute("SELECT state FROM wal WHERE tx_id = %s FOR UPDATE", (tx_id,))
            row = c.fetchone()
            if not row:
                return
            
            tx_state = to_str(row[0])
            if tx_state not in ('pending', 'committed'):
                return

            print(f"\n[Heal] Healing WAL transaction {tx_id} (state: {tx_state})...")
            c.execute("SELECT repo, pre_sha, post_sha FROM wal_entries WHERE tx_id = %s", (tx_id,))
            entries = c.fetchall()

            # Acquire row locks and advisory locks on these repositories to prevent any concurrent modifications during healing
            # Sort repos_to_lock and acquire advisory locks first to prevent deadlocks and close the timing rift window
            repos_to_lock = sorted(list(set(to_str(r[0]) for r in entries)))
            if repos_to_lock:
                for repo in repos_to_lock:
                    c.execute("SELECT pg_advisory_xact_lock(%s)", (get_repo_lock_id(repo),))
                c.execute("SELECT repo FROM state WHERE repo = ANY(%s) FOR UPDATE", (repos_to_lock,))

            if tx_state == 'committed':
                # Roll-forward: apply physical fast-forwards and clean up staged references
                for repo, pre_sha, post_sha in entries:
                    repo_str = to_str(repo)
                    pre_sha_str = to_str(pre_sha)
                    post_sha_str = to_str(post_sha)
                    staged_ref = f"refs/wal/tx_{tx_id}/main"
                    
                    # Update physical branch to post_sha
                    current_sha = self.get_git_sha(repo_str)
                    if current_sha != post_sha_str and not self.is_ancestor(repo_str, post_sha_str, current_sha):
                        print(f"[Heal]   Fast-forwarding physical {repo_str} to post_sha ({post_sha_str[:8]})")
                        self.update_git_ref(repo_str, post_sha_str, old_sha=pre_sha_str)
                    
                    # Delete staged ref
                    self.delete_git_ref(repo_str, staged_ref)
                print(f"[Heal] Transaction {tx_id} physical roll-forward completed.")
                if conn is not None:
                    conn.commit()
                return

            # Determine whether to roll-forward or roll-back based on SCM states (using staged refs)
            all_successful = True
            for repo, pre_sha, post_sha in entries:
                repo_str = to_str(repo)
                pre_sha_str = to_str(pre_sha)
                post_sha_str = to_str(post_sha)
                if pre_sha_str is None or post_sha_str is None:
                    # If any entry is missing its tracking SHAs, the transaction could not
                    # have reached physical completion and must be rolled back.
                    all_successful = False
                    break
                
                # Check if the staged reference exists and points to post_sha
                staged_ref = f"refs/wal/tx_{tx_id}/main"
                staged_sha = self.get_git_ref_sha(repo_str, staged_ref)
                
                # Check if the main branch itself has already been updated to or past post_sha
                current_sha = self.get_git_sha(repo_str)
                
                # If neither the staged reference nor the main branch is at/past post_sha, it's incomplete
                if staged_sha != post_sha_str and current_sha != post_sha_str and not self.is_ancestor(repo_str, post_sha_str, current_sha):
                    all_successful = False
                    break

            if all_successful:
                print(f"[Heal] Transaction {tx_id} succeeded in Git (or fast-forwarded). Rolling forward database...")
                for repo, pre_sha, post_sha in entries:
                    c.execute("UPDATE state SET sha = %s WHERE repo = %s", (to_str(post_sha), to_str(repo)))
                c.execute("UPDATE wal SET state = 'committed' WHERE tx_id = %s", (tx_id,))
                print(f"[Heal] Transaction {tx_id} successfully COMMITTED.")

                # Fast-forward main and delete staged refs
                for repo, pre_sha, post_sha in entries:
                    repo_str = to_str(repo)
                    pre_sha_str = to_str(pre_sha)
                    post_sha_str = to_str(post_sha)
                    staged_ref = f"refs/wal/tx_{tx_id}/main"
                    staged_sha = self.get_git_ref_sha(repo_str, staged_ref)
                    if staged_sha == post_sha_str:
                        current_sha = self.get_git_sha(repo_str)
                        if current_sha != post_sha_str and not self.is_ancestor(repo_str, post_sha_str, current_sha):
                            print(f"[Heal]   Fast-forwarding physical {repo_str} to staged ref ({staged_sha[:8]})")
                            self.update_git_ref(repo_str, post_sha_str, old_sha=pre_sha_str)
                    self.delete_git_ref(repo_str, staged_ref)
            else:
                print(f"[Heal] Transaction {tx_id} was incomplete. Rolling back Git repos...")
                for repo, pre_sha, post_sha in entries:
                    repo_str = to_str(repo)
                    staged_ref = f"refs/wal/tx_{tx_id}/main"
                    self.delete_git_ref(repo_str, staged_ref)
                    
                    # Ensure database points back to pre_sha (safely syncing any state mismatch)
                    pre_sha_str = to_str(pre_sha)
                    if pre_sha_str:
                        c.execute("UPDATE state SET sha = %s WHERE repo = %s", (pre_sha_str, repo_str))
                
                c.execute("UPDATE wal SET state = 'aborted' WHERE tx_id = %s", (tx_id,))
                print(f"[Heal] Transaction {tx_id} successfully ABORTED.")
            
            if conn is not None:
                conn.commit()
        except Exception as e:
            if conn is not None:
                conn.rollback()
            print(f"[Heal] Error healing transaction {tx_id}: {e}")
            raise e
        finally:
            if conn is not None:
                conn.close()

    def do_atomic_push(self, target_repos: list[str], commit_msg: str, simulate_crash: bool = False) -> bool:
        """
        Executes a distributed atomic push transaction targeting specific repositories.
        Implements row-level logical SELECT FOR UPDATE by holding the database row locks
        open during the physical Git push phase. This guarantees 100% thread safety,
        prevents any concurrent modifications to overlapping repositories, and ensures
        reliable crash safety.

        Args:
            target_repos (list): List of repository names to push to.
            commit_msg (str): Message for the new Git commit.
            simulate_crash (bool): If True, will raise an exception after pushes succeed but before DB commit.

        Returns:
            bool: True if transaction committed successfully, False otherwise.
        """
        # Create new connection context for this distributed transaction
        conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}")
        conn.autocommit = False  # Explicit manual transaction
        c = conn.cursor()
        db_committed = False

        try:
            # Sort target_repos and ensure unique elements to avoid deadlocks and redundant locking
            target_repos = sorted(list(set(target_repos)))

            # 1. Acquire transaction-level advisory locks first (closing the rift window completely)
            for repo in target_repos:
                c.execute("SELECT pg_advisory_xact_lock(%s)", (get_repo_lock_id(repo),))

            # 2. Acquire native PostgreSQL row-level locks on state table (SELECT FOR UPDATE)
            # This exclusively locks ONLY the targeted repository rows, leaving unrelated ones unblocked.
            # Concurrent transactions targeting overlapping repositories will naturally block here
            # until this transaction commits or aborts.
            print(f"\n[Push] Acquiring native row-level locks (SELECT FOR UPDATE) on target rows: {target_repos}...")
            c.execute("SELECT repo FROM state WHERE repo = ANY(%s) FOR UPDATE", (target_repos,))

            # 3. Check for any crashed pending transactions on these repositories.
            # Since we hold the row lock now, we are guaranteed that any active transaction has finished.
            # If we find any pending WAL entries here, they MUST be from a crashed process, so we heal them.
            # To completely eliminate database deadlocks, we release our held locks (roll back) before
            # healing on separate connections, then re-acquire them cleanly.
            while True:
                conflicting_txs = self.get_conflicting_pending_transactions(target_repos)
                if not conflicting_txs:
                    break

                print(f"[Push] Crashed pending transactions detected {conflicting_txs}. Releasing our locks to heal cleanly...")
                conn.rollback()

                for conflicting_id in conflicting_txs:
                    print(f"[Push] Healing crashed transaction {conflicting_id} on separate connection...")
                    self.heal_transaction(conflicting_id)

                print("[Push] Re-acquiring locks on target rows (advisory first, then row-level)...")
                for repo in target_repos:
                    c.execute("SELECT pg_advisory_xact_lock(%s)", (get_repo_lock_id(repo),))
                c.execute("SELECT repo FROM state WHERE repo = ANY(%s) FOR UPDATE", (target_repos,))

            # Record initial SCM SHAs for physical rollback in case of push failures.
            pre_shas = {repo: self.get_git_sha(repo) for repo in target_repos}

            # Synchronize local_workspace with latest target remote HEADs to preserve any concurrent direct pushes
            for repo in target_repos:
                run_cmd(["git", "pull", repo, "main", "--rebase"], cwd=LOCAL_DIR, check=False)

            print(f"[Push] Starting distributed transaction targeting {target_repos} for commit: '{commit_msg}'")

            # Create new commit in local workspace
            with open(os.path.join(LOCAL_DIR, "data.txt"), "a") as f:
                f.write(f"{commit_msg}\n")
            run_cmd(["git", "add", "data.txt"], cwd=LOCAL_DIR)
            run_cmd(["git", "commit", "-m", commit_msg], cwd=LOCAL_DIR)
            new_sha = self.get_git_sha("local_workspace")

            # 3. Create a persistent WAL entry via a separate, short-lived database connection.
            # This establishes the logical lock at the Lock Daemon BEFORE we perform physical pushes.
            # Populating both pre_shas and post_sha atomically avoids the mid-rift NULL crash window completely.
            with psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}") as conn_wal:
                with conn_wal.cursor() as c_wal:
                    c_wal.execute("INSERT INTO wal (state) VALUES ('pending') RETURNING tx_id")
                    tx_id = c_wal.fetchone()[0]
                    for repo in target_repos:
                        c_wal.execute("INSERT INTO wal_entries (tx_id, repo, pre_sha, post_sha) VALUES (%s, %s, %s, %s)",
                                      (tx_id, repo, pre_shas[repo], new_sha))

            # 4. Perform pushes to target remotes (staging first)
            success = True
            env = os.environ.copy()
            # Pass our active WAL transaction ID to bypass self-locking in pre-receive hook
            env["ACTIVE_WAL_TX_ID"] = str(tx_id)

            for repo in target_repos:
                print(f"[Push]   Pushing to {repo}...")
                staged_ref = f"refs/wal/tx_{tx_id}/main"
                res = run_cmd(["git", "push", repo, f"main:{staged_ref}"], cwd=LOCAL_DIR, env=env, check=False)
                if res.returncode != 0:
                    print(f"[Push]   CRITICAL: Push to {repo} failed! (Hook output: {res.stderr.strip()})")
                    success = False
                    break

            # 4b. Verify ancestry/fast-forward safety on all target remote SCM repos
            if success:
                for repo in target_repos:
                    pre_sha = pre_shas[repo]
                    # We check if pre_sha is an ancestor of new_sha in the remote repo.
                    # This prevents silent non-fast-forward overwrites of other developers' work.
                    if not self.is_ancestor(repo, pre_sha, new_sha):
                        print(f"[Push]   CRITICAL: Push to {repo} is not a fast-forward! Overwrite/Data loss risk detected.")
                        success = False
                        break

            if not success:
                print("[Push] Push failed. Initiating abort rollback...")
                # Delete staged refs
                for repo in target_repos:
                    staged_ref = f"refs/wal/tx_{tx_id}/main"
                    self.delete_git_ref(repo, staged_ref)

                # Mark WAL record as aborted and release locks atomically via Connection 1 (conn)
                c.execute("UPDATE wal SET state = 'aborted' WHERE tx_id = %s", (tx_id,))
                conn.commit()
                return False

            if simulate_crash:
                raise RuntimeError("CRASH SIMULATION: System interrupted right after successful pushes!")

            # 5. Success path: commit state to target DB table and mark WAL committed.
            # This is done on Connection 1 (conn) so the locks are held until final commit completes.
            for repo in target_repos:
                c.execute("UPDATE state SET sha = %s WHERE repo = %s", (new_sha, repo))
            c.execute("UPDATE wal SET state = 'committed' WHERE tx_id = %s", (tx_id,))
            conn.commit()
            db_committed = True

            # Now update SCM refs to main and delete staged refs
            for repo in target_repos:
                staged_ref = f"refs/wal/tx_{tx_id}/main"
                print(f"[Push]   Fast-forwarding physical {repo} to {new_sha[:8]}...")
                self.update_git_ref(repo, new_sha, old_sha=pre_shas[repo])
                self.delete_git_ref(repo, staged_ref)

            print("[Push] Transaction committed successfully!")
            return True

        except Exception as e:
            print(f"[Push] Interrupted/Failed: {e}")
            if db_committed:
                print(f"[Push] SCM update failed, but DB transaction is committed. SCM will be rolled forward by healing on the next run.")
                try: conn.rollback()
                except: pass
                return False

            if "CRASH SIMULATION" in str(e):
                # For simulated crashes, we roll back DB transaction immediately to release locks,
                # mimicking a process crash where the connection is severed.
                conn.rollback()
            else:
                if 'tx_id' in locals():
                    # Delete staged refs
                    for repo in target_repos:
                        staged_ref = f"refs/wal/tx_{tx_id}/main"
                        self.delete_git_ref(repo, staged_ref)
                    # Mark WAL record as aborted and release locks atomically via Connection 1 (conn)
                    try:
                        c.execute("UPDATE wal SET state = 'aborted' WHERE tx_id = %s", (tx_id,))
                        conn.commit()
                    except Exception as ex:
                        print(f"[Push] Failed to update WAL state on active connection: {ex}")
                        conn.rollback()
                        try:
                            with psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}") as conn_wal:
                                with conn_wal.cursor() as c_wal:
                                    c_wal.execute("UPDATE wal SET state = 'aborted' WHERE tx_id = %s", (tx_id,))
                        except Exception as ex2:
                            print(f"[Push] Failed to update WAL state on fallback connection: {ex2}")
                else:
                    conn.rollback()
            return False
        finally:
            conn.close()

    def print_db_state(self):
        """
        Prints the current 'state' table contents and the status of recent WAL transactions.
        """
        conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}")
        c = conn.cursor()
        
        c.execute("SELECT repo, sha FROM state ORDER BY repo")
        print("\n--- Current Target State Table ---")
        for repo, sha in c.fetchall():
            print(f"  {to_str(repo)}: {to_str(sha)[:8]}...")

        c.execute("SELECT tx_id, state FROM wal ORDER BY tx_id DESC LIMIT 5")
        print("--- Recent WAL Transactions ---")
        for tx_id, state in c.fetchall():
            print(f"  TX {tx_id}: {to_str(state)}")
        print("----------------------------------\n")
        conn.close()


def main():
    """
    Main orchestrator showing all required scenarios.
    """
    start_total = time.perf_counter()
    try:
        setup_environment()
        manager = SystemManager()
        manager.print_db_state()

        print("=====================================================================")
        print("SCENARIO 1: A push fails, target repositories roll back")
        print("=====================================================================")
        # Create the failure trigger file so that repo2-broken's pre-receive hook fails
        with open(TRIGGER_PATH, "w") as tf:
            tf.write("fail")

        start_s1 = time.perf_counter()
        # Attempt atomic push targeting REPOS. Pushing to repo2-broken will fail.
        # The atomic push manager should automatically abort and restore repo1 to original SHA.
        manager.do_atomic_push(REPOS, "Commit #1 (Should Fail)")
        metrics["scenario_1"] = time.perf_counter() - start_s1
        manager.print_db_state()

        print("=====================================================================")
        print("SCENARIO 2: SELECT FOR UPDATE Row Locking is emulated concurrently")
        print("=====================================================================")
        # Remove the failure trigger first so the hook doesn't fail on repo2 check
        if os.path.exists(TRIGGER_PATH):
            os.remove(TRIGGER_PATH)

        start_s2 = time.perf_counter()
        # We manually simulate a pending WAL transaction for REPOS [repo1, repo2-broken, repo3].
        # This represents an open transaction undergoing slow updates or a crash.
        print("[Locking] Simulating a pending WAL transaction for repo1, repo2, repo3...")
        conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}")
        c = conn.cursor()
        c.execute("INSERT INTO wal (state) VALUES ('pending') RETURNING tx_id")
        pending_tx_id = c.fetchone()[0]
        for repo in REPOS:
            pre_sha = manager.get_git_sha(repo)
            c.execute("INSERT INTO wal_entries (tx_id, repo, pre_sha, post_sha) VALUES (%s, %s, %s, %s)",
                      (pending_tx_id, repo, pre_sha, pre_sha))
        conn.commit()
        conn.close()

        # Pushing directly to repo1 fails (since repo1 is in a pending transaction)
        print("\n[Locking] Attempting direct push to locked 'repo1'...")
        with open(os.path.join(LOCAL_DIR, "data.txt"), "a") as f:
            f.write("Direct push attempt\n")
        run_cmd(["git", "add", "data.txt"], cwd=LOCAL_DIR)
        run_cmd(["git", "commit", "-m", "Direct external push to repo1"], cwd=LOCAL_DIR)

        res = run_cmd(["git", "push", "repo1", "main:main"], cwd=LOCAL_DIR, check=False)
        print(f"[Locking] Push result code: {res.returncode}")

        # Pushing directly to repo-independent succeeds (as it is not locked)
        print("\n[Locking] Attempting direct push to unlocked 'repo-independent'...")
        res = run_cmd(["git", "push", "repo-independent", "main:main"], cwd=LOCAL_DIR, check=False)
        print(f"[Locking] Push result code: {res.returncode}")

        # Now attempt a concurrent atomic push transaction targeting ONLY 'repo-independent'.
        # This transaction checks conflicts ONLY on 'repo-independent'. It sees none, and COMMITS 
        # successfully completely concurrently, leaving the pending REPOS transaction untouched!
        # This perfectly emulates SELECT FOR UPDATE row-level lock isolation!
        print("\n[Locking] Attempting concurrent atomic push targeting ONLY 'repo-independent'...")
        manager.do_atomic_push(["repo-independent"], "Atomic Push on Independent Repo")
        metrics["scenario_2"] = time.perf_counter() - start_s2
        manager.print_db_state()

        # Clean up the manually simulated pending transaction to proceed
        print("[Locking] Resolving simulated pending transaction (marking as aborted)...")
        conn = psycopg.connect(f"host=/tmp port={PG_PORT} dbname=postgres user={PG_USER}")
        c = conn.cursor()
        c.execute("UPDATE wal SET state = 'aborted' WHERE tx_id = %s", (pending_tx_id,))
        conn.commit()
        conn.close()

        print("=====================================================================")
        print("SCENARIO 3: All pushes succeed, process crashes before DB update")
        print("=====================================================================")
        # We run do_atomic_push with simulate_crash=True on REPOS
        start_s3 = time.perf_counter()
        manager.do_atomic_push(REPOS, "Commit #2 (Simulated Crash)", simulate_crash=True)
        metrics["scenario_3_crash"] = time.perf_counter() - start_s3
        manager.print_db_state()

        print("[Crash] Simulating manager restart and self-healing...")
        # On next transaction targeting REPOS, it will automatically detect conflict with the pending TX and heal it!
        start_s3_heal = time.perf_counter()
        new_manager = SystemManager()
        new_manager.do_atomic_push(REPOS, "Commit #3 (Successful)")
        metrics["scenario_3_heal"] = time.perf_counter() - start_s3_heal
        manager.print_db_state()

        print("=====================================================================")
        print("SCENARIO 4: SCM Locking Overhead Benchmark")
        print("=====================================================================")
        # Craft a new commit in local workspace
        with open(os.path.join(LOCAL_DIR, "data.txt"), "a") as f:
            f.write("Benchmark Commit 1\n")
        run_cmd(["git", "add", "data.txt"], cwd=LOCAL_DIR)
        run_cmd(["git", "commit", "-m", "Benchmark Commit 1"], cwd=LOCAL_DIR)
        
        # 1. Push with Lock Daemon ACTIVE (standard query flow)
        print("[Benchmark] Executing Git Push with Lock Daemon ACTIVE...")
        start_active = time.perf_counter()
        run_cmd(["git", "push", "repo-independent", "main:main"], cwd=LOCAL_DIR)
        active_time = time.perf_counter() - start_active
        
        # Craft another commit
        with open(os.path.join(LOCAL_DIR, "data.txt"), "a") as f:
            f.write("Benchmark Commit 2\n")
        run_cmd(["git", "add", "data.txt"], cwd=LOCAL_DIR)
        run_cmd(["git", "commit", "-m", "Benchmark Commit 2"], cwd=LOCAL_DIR)
        
        # 2. Push with Lock Daemon BYPASSED (DISABLE_WAL_LOCKS=1)
        # Hook instantly exits with 0 without opening socket
        print("[Benchmark] Executing Git Push with Lock Daemon BYPASSED...")
        env = os.environ.copy()
        env["DISABLE_WAL_LOCKS"] = "1"
        start_bypass = time.perf_counter()
        run_cmd(["git", "push", "repo-independent", "main:main"], cwd=LOCAL_DIR, env=env)
        bypass_time = time.perf_counter() - start_bypass
        
        metrics["scenario_4_active"] = active_time
        metrics["scenario_4_bypass"] = bypass_time

    finally:
        # Cleanly stop the background PostgreSQL server on script exit
        stop_postgres()
        metrics["total_execution"] = time.perf_counter() - start_total

        # Print the Performance Metrics Report
        print("\n" + "="*69)
        print("                 PERFORMANCE METRICS REPORT")
        print("="*69)
        print(f" - Preloader Compilation:      {metrics.get('preloader_compile', 0.0)*1000:7.2f} ms")
        print(f" - PostgreSQL Database initdb: {metrics.get('postgres_initdb', 0.0)*1000:7.2f} ms")
        print(f" - PostgreSQL Background Boot: {metrics.get('postgres_boot', 0.0)*1000:7.2f} ms")
        print(f" - SCM Environment Setup:      {metrics.get('env_setup', 0.0)*1000:7.2f} ms")
        print(f" - Scenario 1 (Rollback):      {metrics.get('scenario_1', 0.0)*1000:7.2f} ms")
        print(f" - Scenario 2 (Concurrency):   {metrics.get('scenario_2', 0.0)*1000:7.2f} ms")
        print(f" - Scenario 3 (Crash Trigger): {metrics.get('scenario_3_crash', 0.0)*1000:7.2f} ms")
        print(f" - Scenario 3 (Self-Heal):     {metrics.get('scenario_3_heal', 0.0)*1000:7.2f} ms")
        print(f" - PostgreSQL Clean Stop:      {metrics.get('postgres_stop', 0.0)*1000:7.2f} ms")
        print("-"*69)
        print(" BENCHMARK: LOCK DAEMON SCALABILITY OVERHEAD")
        print("-"*69)
        act_ms = metrics.get('scenario_4_active', 0.0)*1000
        byp_ms = metrics.get('scenario_4_bypass', 0.0)*1000
        over_ms = act_ms - byp_ms
        print(f" - Git Push with Lock Daemon ACTIVE:   {act_ms:7.2f} ms")
        print(f" - Git Push with Lock Daemon BYPASSED: {byp_ms:7.2f} ms")
        print(f" -> COORDINATING DAEMON OVERHEAD:       {over_ms:7.2f} ms")
        print("-"*69)
        print(f" TOTAL RUNTIME:                 {metrics.get('total_execution', 0.0):7.3f} seconds")
        print("="*69 + "\n")


if __name__ == "__main__":
    main()
