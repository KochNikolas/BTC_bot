from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import json
import os

app = FastAPI()

CONFIG_FILE = "config.json"
STATUS_FILE = "trade_status.json"

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    with open("index.html", "r") as f:
        return f.read()

@app.get("/api/status")
async def get_status():
    if not os.path.exists(STATUS_FILE):
        return JSONResponse(content={"error": "Bot is not running yet..."}, status_code=404)
    with open(STATUS_FILE, "r") as f:
        data = json.load(f)
    return JSONResponse(content=data, headers={"Cache-Control": "no-store"})

@app.get("/api/config")
async def get_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        data = json.load(f)
    return JSONResponse(content=data, headers={"Cache-Control": "no-store"})

@app.post("/api/config")
async def update_config(request: Request):
    new_config = await request.json()
    with open(CONFIG_FILE, "w") as f:
        json.dump(new_config, f, indent=4)
    return {"status": "success"}

@app.get("/api/trades")
async def get_trades():
    # Dynamisch je nach Modus laden
    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
    
    dry_run = config.get("DRY_RUN", True)
    TRADES_FILE = "btc_trades_log.csv" if dry_run else "real_trades_log.csv"
    
    if not os.path.exists(TRADES_FILE):
        return []
    import csv
    trades = []
    try:
        with open(TRADES_FILE, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
    except:
        return []
    return JSONResponse(content=trades[::-1], headers={"Cache-Control": "no-store"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
