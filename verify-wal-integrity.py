#!/usr/bin/env python3
"""
verify-wal-integrity.py: Comprehensive validation suite for the Git-WAL consensus engine.
Runs all proof-of-concept tests and verifies strict transactional integrity, resilience,
and crash-safety.
"""

import sys
import subprocess
import time

TESTS = [
    ("prove-race.py", "Concurrent transaction serialization (SELECT FOR UPDATE / Advisory Locks)"),
    ("prove-direct-race.py", "Direct push blocking before WAL entry creation (Advisory Rift Locking)"),
    ("prove-rift-race.py", "Direct push rift window protection between locking and WAL creation"),
    ("prove-real-rift-race.py", "Direct push rift window protection between row-locking and advisory lock acquisition"),
    ("prove-null-crash.py", "Crash safety and healing of aborted transactions with NULL SHAs"),
    ("prove-offline-corruption.py", "Fail-closed lock-daemon and ancestry-aware recovery verification"),
    ("prove-commit-phase-crash.py", "Crash-resilience and physical roll-forward of incomplete committed transactions"),
    ("prove-pre-lock-push-race.py", "SCM-DB divergence and silent corruption under pre-receive timing gap (Pre-Lock Push Race)")
]

def run_test(script_name: str, description: str) -> bool:
    print(f"\nRunning test: {script_name} - {description}")
    print("-" * 80)
    
    start_time = time.perf_counter()
    # Always set check=False so we can inspect return code ourselves
    res = subprocess.run([sys.executable, script_name], capture_output=True, text=True, check=False)
    duration = time.perf_counter() - start_time
    
    # Print test stdout/stderr
    print(res.stdout)
    if res.stderr:
        print("--- STDERR ---", file=sys.stderr)
        print(res.stderr, file=sys.stderr)
        
    print("-" * 80)
    if res.returncode == 0:
        print(f"Result: SUCCESS (Duration: {duration:.2f}s)")
        return True
    else:
        print(f"Result: FAILED with exit code {res.returncode} (Duration: {duration:.2f}s)")
        return False

def main():
    print("=" * 80)
    print("                GIT-WAL TRANSACTION INTEGRITY TEST SUITE")
    print("=" * 80)
    
    success = True
    results = []
    
    for script_name, description in TESTS:
        passed = run_test(script_name, description)
        results.append((script_name, passed))
        if not passed:
            success = False
            
    print("\n" + "=" * 80)
    print("                         TEST SUITE SUMMARY")
    print("=" * 80)
    for script_name, passed in results:
        status = "PASSED" if passed else "FAILED"
        print(f"  {script_name:30} : {status}")
    print("=" * 80)
    
    if success:
        print("\nALL INTEGRITY CHECKS PASSED. SCM-DB transactional integrity is 100% verified.")
        sys.exit(0)
    else:
        print("\nCRITICAL INTEGRITY FAILURE. One or more verification scripts failed.")
        sys.exit(1)

if __name__ == "__main__":
    main()
