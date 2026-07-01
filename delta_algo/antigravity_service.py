from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import os
import json

app = FastAPI(title="Antigravity Execution Engine")

class TaskRequest(BaseModel):
    task_id: str
    action: str  # e.g., "check_positions", "execute_strangle", "generate_report"
    params: dict = {}

@app.get("/health")
def health():
    return {"status": "operational", "engine": "Antigravity-v2"}

@app.post("/execute")
async def execute_task(request: TaskRequest):
    """
    Executes a trading or analysis task using the core DeltaAlgo logic.
    """
    print(f"Received task {request.task_id}: {request.action}")
    
    try:
        if request.action == "check_positions":
            # Example: Run the test_pos_2 script and capture output
            result = subprocess.check_output(
                ["python", "test_pos_2.py"],
                cwd=os.getcwd(),
                stderr=subprocess.STDOUT,
                text=True
            )
            return {"status": "success", "output": result}
            
        elif request.action == "execute_trade":
            # Actually run execute_trade.py
            result = subprocess.check_output(
                ["python", "execute_trade.py"],
                cwd=os.getcwd(),
                stderr=subprocess.STDOUT,
                text=True
            )
            return {"status": "success", "output": result}

        elif request.action == "manage_risk":
            # Run the risk management script
            result = subprocess.check_output(
                ["python", "risk/place_sl_tp.py"],
                cwd=os.getcwd(),
                stderr=subprocess.STDOUT,
                text=True
            )
            return {"status": "success", "output": result}
            
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported action: {request.action}")
            
    except subprocess.CalledProcessError as e:
        return {"status": "error", "output": e.output, "code": e.returncode}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
