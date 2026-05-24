import json
import os
import numpy as np

STATE_FILE = 'portfolio_state.json'

def load_portfolio_state():
    if not os.path.exists(STATE_FILE):
        default_state = {
            "current_capital": 1500.0,
            "max_open_positions": 4,
            "cash_flow_history": [],
            "active_positions": {}
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(default_state, f, indent=4)
        return default_state
    with open(STATE_FILE, 'r') as f:
        try:
            return json.load(f)
        except:
            return {"current_capital": 1500.0, "max_open_positions": 4, "cash_flow_history": [], "active_positions": {}}

def calculate_final_score(metrics):
    raw_score = (metrics["net_profit_pct"] * metrics["win_rate_pct"]) / (1 + metrics["max_drawdown_pct"])
    confidence = min(1.0, metrics["total_trades"] / 12.0)
    dd_penalty = np.exp(-metrics["max_drawdown_pct"] / 5.0)
    return round(raw_score * confidence * dd_penalty, 2)