from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import re

app = FastAPI(title="CodeRabbit Review Engine")

class ReviewRequest(BaseModel):
    task_id: str
    code: str
    context: str = ""

@app.get("/health")
def health():
    return {"status": "operational", "engine": "CodeRabbit-v1"}

@app.post("/review")
async def review_code(request: ReviewRequest):
    """
    Simulates a CodeRabbit AI review process.
    Validates code logic, check for common pitfalls, and suggests optimizations.
    """
    print(f"Reviewing code for task {request.task_id}...")
    
    # Simple static analysis (logic checks)
    issues = []
    optimizations = []
    
    # 1. Check for basic syntax errors or obvious issues
    if "import os" in request.code and "os.getcwd()" in request.code:
        optimizations.append("Use pathlib for path manipulations for better cross-platform compatibility.")
        
    if "print(" in request.code:
        optimizations.append("Consider using a logger instead of print statements for production code.")
        
    # 2. Logic Check (mocked)
    if "69400" in request.code and "65600" in request.code:
        # Check if the safety margins are too narrow (simulating a check)
        issues.append("Short Strangle strikes are near the 1.5% mark. Evaluate risk before executing.")
        
    # 3. Decision
    approved = len(issues) == 0
    score = 100 - (len(issues) * 20) - (len(optimizations) * 5)
    
    return {
        "status": "success",
        "approved": approved,
        "score": max(0, score),
        "issues": issues,
        "optimizations": optimizations,
        "summary": "Code validated for syntax. Optimization required for production-readiness." if approved else "Critical risk issues detected."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
