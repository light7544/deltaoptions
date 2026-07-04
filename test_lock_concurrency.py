import subprocess
import time
import sys
import os

def run_test():
    print("Starting process 1...")
    # Start the first instance in the background
    p1 = subprocess.Popen(
        [sys.executable, "delta_chain_core.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Wait a moment to ensure p1 has created the lock file
    time.sleep(1.5)
    
    print("Starting process 2 (should fail)...")
    # Start the second instance
    p2 = subprocess.run(
        [sys.executable, "delta_chain_core.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Wait for process 1 to finish
    p1_stdout, p1_stderr = p1.communicate()
    
    print("\n--- Process 1 Status ---")
    print(f"Exit code: {p1.returncode}")
    
    print("\n--- Process 2 Status ---")
    print(f"Exit code: {p2.returncode}")
    print(f"Stdout: {p2.stdout.strip()}")
    print(f"Stderr: {p2.stderr.strip()}")
    
    # Verify expectations
    lock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_fetcher.lock")
    print(f"\nLock file exists? {os.path.exists(lock_file)}")
    
    if p2.returncode == 1 and "An instance of the data fetcher is already running!" in p2.stderr:
        print("\n✅ SUCCESS: Concurrency lock successfully blocked duplicate instance.")
    else:
        print("\n❌ FAILURE: Concurrency lock did not work as expected.")
        sys.exit(1)

if __name__ == "__main__":
    run_test()
