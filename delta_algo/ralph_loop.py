import time
import json
import requests
import logging
import os
from datetime import datetime

# Setup GSD Logging
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] GSD-RALPH: %(message)s',
    handlers=[
        logging.FileHandler("logs/gsd_flow.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("ralph_loop")

# Configuration
ANTIGRAVITY_URL = os.getenv("ANTIGRAVITY_URL", "http://antigravity:8000")
CODERABBIT_URL = os.getenv("CODERABBIT_URL", "http://coderabbit:8001")
MAX_RETRIES = 3
RETRY_DELAY = 5

def process_task(task):
    """
    Main GSD (Get Shit Done) loop for a single task.
    """
    task_id = task.get("id")
    logger.info(f"--- Starting Task {task_id}: {task.get('description')} ---")
    
    retries = 0
    while retries < MAX_RETRIES:
        try:
            # 1. Execute via Antigravity
            logger.info(f"[{task_id}] Calling Antigravity (Execution)... Attempt {retries + 1}")
            antigravity_resp = requests.post(
                f"{ANTIGRAVITY_URL}/execute",
                json={
                    "task_id": task_id,
                    "action": task.get("action"),
                    "params": task.get("params", {})
                },
                timeout=10
            )
            antigravity_resp.raise_for_status()
            execution_output = antigravity_resp.json()
            
            # 2. Review via CodeRabbit
            logger.info(f"[{task_id}] Calling CodeRabbit (Review)...")
            review_resp = requests.post(
                f"{CODERABBIT_URL}/review",
                json={
                    "task_id": task_id,
                    "code": str(execution_output.get("output", "")), # Code for review
                    "context": task.get("description")
                },
                timeout=10
            )
            review_resp.raise_for_status()
            review_result = review_resp.json()
            
            # 3. Decision
            if review_result.get("approved"):
                logger.info(f"[{task_id}] COMPLETED SUCCESSFULLY! {review_result.get('summary')}")
                return True
            else:
                logger.warning(f"[{task_id}] REVIEW FAILED: {review_result.get('issues')}")
                retries += 1
                time.sleep(RETRY_DELAY)
                
        except Exception as e:
            logger.error(f"[{task_id}] Error in loop: {str(e)}")
            retries += 1
            time.sleep(RETRY_DELAY)
            
    logger.error(f"[{task_id}] FAILED after {MAX_RETRIES} attempts.")
    return False

def ralph_driver():
    """
    Continuous driver that reads tasks and feeds them into the system.
    """
    logger.info("Ralph Driver initialization complete. Entering main loop.")
    
    while True:
        try:
            if os.path.exists("gsd_tasks.json"):
                with open("gsd_tasks.json", "r") as f:
                    tasks = json.load(f)
                
                # Filter out completed tasks (could also use a status field)
                pending_tasks = [t for t in tasks if t.get("status") == "pending"]
                
                if not pending_tasks:
                    logger.info("No pending tasks. Sleeping for 10s.")
                    time.sleep(10)
                    continue
                
                for task in pending_tasks:
                    success = process_task(task)
                    # Update task status (Note: In a stateless system, you might use a DB)
                    task["status"] = "success" if success else "failed"
                    task["completed_at"] = datetime.now().isoformat()
                    
                    with open("gsd_tasks.json", "w") as f:
                        json.dump(tasks, f, indent=4)
            else:
                logger.warning("gsd_tasks.json not found. Waiting...")
                time.sleep(10)
                
        except Exception as e:
            logger.error(f"Global driver error: {str(e)}")
            time.sleep(10)

if __name__ == "__main__":
    ralph_driver()
