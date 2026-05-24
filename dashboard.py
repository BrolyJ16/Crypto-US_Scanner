import streamlit as st
import pandas as pd
import sqlite3
import json
import os
import yfinance as yf
import ccxt
import numpy as np
from datetime import datetime

# --- ตั้งค่าหน้าเว็บสไตล์คลีนหรู ---
st.set_page_config(page_title="Hybrid Portfolio Dashboard", layout="wide")
st.title("📊 Hybrid Portfolio & Risk Management (Phase 3.2 - Live Edge Matrix)")

DB_NAME = 'trade_history.db'
STATE_FILE = 'portfolio_state.json'
PARAMS_FILE = 'optimized_params.json'

# --- ฟังก์ชันหลักจัดการฐานข้อมูลและไฟล์สถานะ ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            symbol TEXT,
            side TEXT,
            entry_price REAL,
            exit_price REAL,
            pnl_usdt REAL,
            status TEXT
        )
    ''')
    conn.commit()
    conn.close()

def load_portfolio_state():
    if not os.path.exists(STATE_FILE):
        default_state = {"current_capital": 1500.0, "max_open_positions": 4, "cash_flow_history": [], "active_positions": {}}
        with open(STATE_FILE, 'w') as f: json.dump(default_state, f, indent=4)
        return default_state
    with open(STATE_FILE, 'r') as f:
        try: return json.load(f)
        except: return {"current_capital": 1500.0, "max_open_positions": 4, "cash_flow_history": [], "active_positions": {}}

def save_portfolio_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=4)

def calculate_final_score(metrics):
    """คำนวณคะแนนความคมตามสูตร Final Formula"""
    raw_score = (metrics["net_profit_pct"] * metrics["win_rate_pct"]) / (1 + metrics["max_drawdown_pct"])
    # Confidence (ข้อมูลต้องหนา 12 ไม้ขึ้นไป)
    confidence = min(1.0, metrics["total_trades"] / 12.0)
    # Stability Penalty (Drawdown เยอะ คะแนนวูบ)
    dd_penalty = np.exp(-metrics["max_drawdown_pct"] / 5.0)
    return raw_score * confidence * dd_penalty

def evaluate_hold_position(symbol, info, metrics, current_price):
    """คำนวณ Hold Score เพื่อเปรียบเทียบแต้มต่อกับโอกาสใหม่"""
    base_score = calculate_final_score(metrics)
    entry = info["entry_price"]
    pnl_pct = (current_price - entry) / entry
    pnl_val = pnl_pct * 100  # แปลงเป็นหน่วยเปอร์เซ็นต์ (เช่น 12)
    
    # Trend Boost: ยิ่งกำไรเยอะ ยิ่งรักษาสถานะ (ใช้ PnL % เป็นตัวคูณ)
    trend_boost = pnl_val * 50 if pnl_val > 0 else pnl_val * 100
    return base_score + trend_boost

def decide_switch(current_score, new_score, is_sell_signal=False):
    """ฟันธงการตัดสินใจตามเกณฑ์แต้มต่อ 30% หรือสัญญาณขาย"""
    if is_sell_signal:
        return "SELL"
    if new_score > current_score * 1.3:
        return "SWITCH"
    elif new_score < current_score * 0.8:
        return "HOLD"
    else:
        return "NEUTRAL"

def load_daily_signals():
    """โหลดสัญญาณล่าสุดจากการสแกนของ main.py (ตรวจสอบความเป็นปัจจุบันของข้อมูล)"""
    if not os.path.exists('daily_signals.json'):
        return []
    try:
        # ตรวจสอบว่าไฟล์ถูกสร้างขึ้นในวันนี้หรือไม่
        file_time = datetime.fromtimestamp(os.path.getmtime('daily_signals.json')).date()
        if file_time != datetime.now().date():
            return []
        with open('daily_signals.json', 'r') as f:
            return json.load(f)
    except:
        return []

@st.cache_data(ttl=600)
def get_live_price(symbol):
    """ดึงราคาล่าสุดแบบ Real-time"""
    try:
        if '/' in symbol:
            exchange = ccxt.binance()
            ticker = exchange.fetch_ticker(symbol)
            return ticker['last']
        else:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="1d")
            if not df.empty: return df['close'].iloc[-1]
    except: return None
    return None

def check_us_market_regime():
    """ตรวจสอบสุขภาพตลาดภาพรวม (S&P 500)"""
    try:
        spy = yf.Ticker("SPY")
        df_spy = spy.history(period="2y")
        if df_spy.empty: return True
        df_spy.columns = df_spy.columns.str.lower()
        df_spy['sma_200'] = df_spy['close'].rolling(window=200).mean()
        return df_spy['close'].iloc[-1] > df_spy['sma_200'].iloc[-1]
    except: return True

def get_exit_signals(symbol, entry_date_str, curr_p):
    """ดึงข้อมูลเทคนิคเพื่อเช็ค Exit Rule (Trailing Stop & SMA50)"""
    try:
        if '/' in symbol: # Crypto
            exchange = ccxt.binance()
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1d', limit=100)
            df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df.set_index('ts', inplace=True)
        else: # Stock
            df = yf.download(symbol, period="1y", progress=False)

        if df.empty: return curr_p, curr_p
        
        df.columns = df.columns.str.lower()
        # คำนวณ SMA50
        df['sma50'] = df['close'].rolling(window=50).mean()
        
        # ราคาสูงสุดนับจากเข้าซื้อ
        df_since = df[df.index >= entry_date_str]
        highest_p = df_since['high'].max() if not df_since.empty else curr_p
        sma50 = df['sma50'].iloc[-1]
        
        return highest_p, sma50
    except: return curr_p, curr_p

def load_optimized_params():
    if os.path.exists(PARAMS_FILE):
        with open(PARAMS_FILE, 'r') as f: return json.load(f)
    return {}

def calculate_expectancy(df):
    """คำนวณค่ากำไรคาดหวังต่อการเทรด 1 ครั้ง (Expectancy)"""
    wins = df[df["pnl_usdt"] > 0]
    losses = df[df["pnl_usdt"] <= 0]
    win_rate = len(wins) / len(df) if len(df) > 0 else 0
    loss_rate = 1 - win_rate
    avg_win = wins["pnl_usdt"].mean() if len(wins) > 0 else 0
    avg_loss = abs(losses["pnl_usdt"].mean()) if len(losses) > 0 else 0
    expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
    return round(expectancy, 2)

def calculate_live_win_rate(pnl_pct):
    """ประมาณค่า Win Rate จากสถานะกำไรขาดทุนปัจจุบัน"""
    if pnl_pct > 0:
        return 60  # สมมติ win probability สูงขึ้นเมื่อมีกำไร
    else:
        return 40

def calculate_rr(entry, current_price, stop_loss):
    """คำนวณ Risk/Reward Ratio หน้างาน"""
    reward = current_price - entry
    risk = entry - stop_loss
    if risk == 0: return 0
    return round(reward / risk, 2)

def calculate_sharpe(df):
    """คำนวณความนิ่งของผลตอบแทน (Sharpe Ratio)"""
    if len(df) < 2:
        return 0
    returns = df["pnl_usdt"]
    mean = returns.mean()
    std = returns.std()
    if std == 0:
        return 0
    sharpe = mean / std
    return round(sharpe, 2)

# โหลดข้อมูลทำงานต้นงวด
init_db()
state = load_portfolio_state()
optimized_params = load_optimized_params()

# --- ส่วนที่ 1: การจัดการหน้าตัก คุมความเสี่ยง และบันทึกพอร์ต (Sidebar Control) ---
st.sidebar.header("🕹️ แผงควบคุมนโยบายพอร์ต")

# บล็อกปรับเปลี่ยนเงินทุนและประวัติฝากถอน
st.sidebar.subheader("💰 การบริหารเงินสด (Cash Flow)")
cf_type = st.sidebar.selectbox("ประเภทธุรกรรม", ["ฝากเงินเพิ่ม (DEPOSIT)", "ถอนเงินออก (WITHDRAW)"])
cf_amount = st.sidebar.number_input("จำนวนเงิน (USD/USDT)", min_value=0.0, value=0.0, step=100.0)

if st.sidebar.button("บันทึกธุรกรรมการเงิน"):
    if cf_amount > 0:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        actual_amount = cf_amount if "DEPOSIT" in cf_type else -cf_amount
        
        state["current_capital"] = round(state["current_capital"] + actual_amount, 2)
        state["cash_flow_history"].append({"date": now_str, "type": cf_type.split()[0], "amount": cf_amount})
        save_portfolio_state(state)
        st.sidebar.success(f"บันทึกสำเร็จ! ทุนปัจจุบัน: {state['current_capital']:,.2f}")
        st.rerun()

st.sidebar.markdown("---")

# บล็อกตั้งค่าจำนวนโควตาไม้ (Slots Config)
st.sidebar.subheader("📦 ตั้งค่าระบบโควตาไม้")
new_max_slots = st.sidebar.number_input("จำนวนไม้สูงสุดที่ถือพร้อมกัน", min_value=1, max_value=10, value=int(state.get("max_open_positions", 4)))
if new_max_slots != state.get("max_open_positions", 4):
    state["max_open_positions"] = int(new_max_slots)
    save_portfolio_state(state)
    st.sidebar.info(f"ปรับโควตาพอร์ตเป็น {new_max_slots} ไม้ เรียบร้อย!")
    st.rerun()

st.sidebar.markdown("---")

# บล็อกสมุดจดบันทึกการถือหุ้นจริงหน้างาน
st.sidebar.subheader("📝 สมุดจดบันทึกการถือหุ้นจริง")
position_form = st.sidebar.form("position_form", clear_on_submit=True)
pos_symbol = position_form.text_input("ชื่อหุ้น / คริปโต (เช่น AAPL, BTC/USDT)").upper().strip()
pos_price = position_form.number_input("ราคาที่พี่ซื้อได้จริง", min_value=0.0, value=0.0)
pos_units = position_form.number_input("จำนวนหน่วยที่ได้รับ (Units)", min_value=0.0, value=0.0, format="%.4f")
pos_date = position_form.date_input("วันที่กดเข้าซื้อจริง")
submit_pos = position_form.form_submit_button("💥 บันทึกเปิดสถานะ")

if submit_pos and pos_symbol and pos_price > 0 and pos_units > 0:
    state["active_positions"][pos_symbol] = {
        "entry_date": pos_date.strftime("%Y-%m-%d"),
        "entry_price": float(pos_price),
        "units": float(pos_units)
    }
    save_portfolio_state(state)
    st.sidebar.success(f"บันทึกหุ้น {pos_symbol} เข้าสล็อตเรียบร้อย!")
    st.rerun()

# --- ส่วนที่ 2: หน้าจอแสดงผลหลัก (Main Dashboard) ---

# กางตารางหุ้นที่ถืออยู่จริง ณ ปัจจุบันมาโชว์หัวแถว
st.subheader("📦 สล็อตพอร์ตโฟลิโอหน้างานจริง (Active Slots)")
active_positions = state.get("active_positions", {})
max_slots = state.get("max_open_positions", 4)
is_us_healthy = check_us_market_regime()

if active_positions:
    pos_data = []
    for sym, info in list(active_positions.items()):
        # คำนวณวันถือครอง
        entry_date_str = info["entry_date"]
        entry_date_dt = datetime.strptime(entry_date_str, "%Y-%m-%d")
        days_held = max(0, (datetime.now() - entry_date_dt).days)
        
        # คำนวณ Live Win Rate (Performance Decay Logic)
        live_wr = "N/A"
        if sym in optimized_params:
            config = optimized_params[sym]
            h_lookback = config.get("lookback", 60)
            win_rate_pct = config["metrics"]["win_rate_pct"]
            decay_factor = max(0.1, 1.0 - (days_held / (h_lookback * 2)))
            live_wr = f"{round(win_rate_pct * decay_factor, 1)}%"

        # ดึงราคาปัจจุบันและคำนวณ PnL
        curr_p = get_live_price(sym)
        pnl_text = "N/A"
        decision = "✅ HOLD"
        
        if curr_p:
            pnl_val = (curr_p - info['entry_price']) / info['entry_price'] * 100
            pnl_text = f"{pnl_val:+.2f}%"
            
            # ตรวจสอบเงื่อนไข SELL
            if sym in optimized_params:
                cfg = optimized_params[sym]
                highest_p, sma50 = get_exit_signals(sym, entry_date_str, curr_p)
                t_stop = highest_p * (1 - cfg['trailing_pct'])
                
                if curr_p <= t_stop:
                    decision = "❌ SELL (หลุด Trailing Stop)"
                elif curr_p < sma50:
                    decision = "❌ SELL (หลุด SMA50 Trend)"
                elif '/' not in sym and not is_us_healthy:
                    decision = "❌ SELL (Market Flip)"

        pos_data.append({
            "สินทรัพย์": sym, 
            "ถือมาแล้ว": f"{days_held} วัน",
            "Live WR": live_wr,
            "PnL %": pnl_text,
            "ฟันธง (Decision)": decision,
            "ราคาต้นทุน": f"{info['entry_price']:,.2f}", 
            "จำนวนหน่วย (Units)": f"{info['units']:.4f}"
        })
    df_pos = pd.DataFrame(pos_data)
    
    # หัวตาราง (เพิ่มคอลัมน์ให้รองรับข้อมูลใหม่)
    h_sym, h_held, h_wr, h_pnl, h_status, h_btn = st.columns([1, 1, 1, 1.2, 2, 1])
    h_sym.write("**สินทรัพย์**")
    h_held.write("**ถือมาแล้ว**")
    h_wr.write("**Live WR**")
    h_pnl.write("**PnL %**")
    h_status.write("**ฟันธง (Decision)**")
    h_btn.write("**แอคชัน**")

    # แสดงตารางพร้อมปุ่มเคลียร์สล็อตเวลาขายของทำกำไรเสร็จแล้ว
    for idx, row in df_pos.iterrows():
        col_sym, col_held, col_wr, col_pnl, col_status, col_btn = st.columns([1, 1, 1, 1.2, 2, 1])
        col_sym.write(f"**{row['สินทรัพย์']}**")
        col_held.write(row['ถือมาแล้ว'])
        col_wr.write(row['Live WR'])
        col_pnl.write(row['PnL %'])
        col_status.write(row['ฟันธง (Decision)'])
        if col_btn.button(f"🗑️ เคลียร์ไม้", key=f"del_{row['สินทรัพย์']}"):
            del state["active_positions"][row['สินทรัพย์']]
            save_portfolio_state(state)
            st.success(f"เคลียร์สล็อต {row['สินทรัพย์']} คืนโควตาเงินสดเรียบร้อย!")
            st.rerun()
else:
    st.info(f"พอร์ตว่างเปล่า (0 / {max_slots} ไม้) สามารถนำสัญญาณเช้านี้ไปคีย์ออเดอร์เปิดสถานะได้ครับพี่")

st.markdown("---")

# ⚖️ ------------------ [UPGRADED - PHASE 3.3]: SMART ALLOCATION ENGINE ------------------ ⚖️
st.subheader("⚖️ Smart Allocation Engine")
st.caption("ระบบวิเคราะห์เปรียบเทียบแต้มต่อของพอร์ตทั้งหมดกับสัญญาณใหม่เช้านี้")

daily_signals_data = load_daily_signals()
if active_positions and daily_signals_data:
    matrix = []
    for sym, pos in active_positions.items():
        if sym not in optimized_params: continue
        
        curr_p = get_live_price(sym) or pos["entry_price"]
        metrics = optimized_params[sym]["metrics"]
        hold_score = evaluate_hold_position(sym, pos, metrics, curr_p)
        
        # เช็คสัญญาณขายเบื้องต้น (Trailing Stop & SMA50)
        highest_p, sma50_h = get_exit_signals(sym, pos["entry_date"], curr_p)
        t_stop = highest_p * (1 - optimized_params[sym]['trailing_pct'])
        is_sell = curr_p <= t_stop or curr_p < sma50_h or ('/' not in sym and not is_us_healthy)
        
        for sig in daily_signals_data:
            new_sym = sig['symbol']
            new_score = sig['final_score']
            decision = decide_switch(hold_score, new_score, is_sell)
            
            matrix.append({
                "Holding": sym,
                "PnL %": round((curr_p - pos["entry_price"]) / pos["entry_price"] * 100, 2),
                "Hold Score": round(hold_score, 2),
                "Candidate": new_sym,
                "Candidate Score": round(new_score, 2),
                "Decision": decision
            })
    
    if matrix:
        df_matrix = pd.DataFrame(matrix)
        def color_decision(val):
            if val in ["SWITCH", "SELL"]: 
                return 'background-color: #ff4b4b; color: white' # สีแดงเตือน
            elif val == "HOLD": 
                return 'background-color: #28a745; color: white' # สีเขียวปลอดภัย
            return ''
        st.dataframe(df_matrix.style.applymap(color_decision, subset=["Decision"]), use_container_width=True)

st.markdown("---")

# 📊 ------------------ [NEW FEATURE - PHASE 3]: DYNAMIC OPPORTUNITY COST MATRIX ------------------ 📊
st.subheader("⚖️ ตารางวิเคราะห์ความคุ้มค่าเพื่อพิจารณาสลับตัวเล่น (Opportunity Cost Matrix)")
st.caption("ระบบดึงสถิติของหุ้นในพอร์ตมาปะทะกับตัวสแกน เพื่อช่วยพี่ตัดสินใจหน้างานว่าควร 'ถือตัวเดิมต่อ (Hold)' หรือ 'สลับไปตัวใหม่ (Switch)'")

if optimized_params:
    col_left, col_right = st.columns(2)
    
    # 1. คัดเลือกฝั่งตัวในพอร์ต
    with col_left:
        st.markdown("#### 🔒 สินทรัพย์ในพอร์ตของพี่")
        if active_positions:
            hold_symbol = st.selectbox("เลือกหุ้นในพอร์ตที่ต้องการวิเคราะห์ความคุ้มค่า", list(active_positions.keys()), key="hold_select")
            if hold_symbol in optimized_params:
                h_metrics = optimized_params[hold_symbol]["metrics"]
                st.success(f"**{hold_symbol}** ย้อนหลัง: กำไรสุทธิ {h_metrics['net_profit_pct']}% | Win Rate: {h_metrics['win_rate_pct']}% | เทรด {h_metrics['total_trades']} ครั้ง")
            else:
                st.info(f"💡 {hold_symbol} ไม่มีสถิติบันทึกในระบบพารามิเตอร์")
        else:
            st.warning("⚠️ ตอนนี้พอร์ตว่างเปล่า ไม่มีหุ้นในมือให้เปรียบเทียบครับ")
            hold_symbol = None

    # 2. คัดเลือกฝั่งตัวที่แจ้งเตือนเช้านี้ (หรือตัวเรดาร์ที่น่าสนใจ)
    with col_right:
        st.markdown("#### 🔍 สินทรัพย์ตัวเลือกใหม่ (สัญญาณเช้านี้)")
        
        # ดึงเฉพาะตัวที่มีสัญญาณจากการสแกนล่าสุด
        daily_signals_data = load_daily_signals()
        signal_symbols = [s['symbol'] for s in daily_signals_data]
        radar_choices = [sym for sym in signal_symbols if sym not in active_positions]
        
        if radar_choices:
            new_symbol = st.selectbox("เลือกหุ้นตัวใหม่ที่เกิดสัญญาณซื้อเช้านี้", radar_choices, key="new_select")
            n_metrics = optimized_params[new_symbol]["metrics"]
            st.info(f"**{new_symbol}** ย้อนหลัง: กำไรสุทธิ {n_metrics['net_profit_pct']}% | Win Rate: {n_metrics['win_rate_pct']}% | เทรด {n_metrics['total_trades']} ครั้ง")
        else:
            st.warning("⚠️ วันนี้ยังไม่มีสัญญาณใหม่จากการสแกน (กรุณารัน main.py เพื่ออัปเดตข้อมูล)")
            new_symbol = None

    # 3. บล็อกตารางประเมินผลคำแนะนำสากล (Action Matrix Scorecard)
    if hold_symbol and new_symbol and h_metrics:
        st.markdown("##### 📈 ตารางเปรียบเทียบแต้มต่อ (Quant Scorecard)")
        
        h_stat = optimized_params[hold_symbol]["metrics"]
        n_stat = optimized_params[new_symbol]["metrics"]
        
        # คำนวณ Performance Decay สำหรับการเปรียบเทียบ
        entry_date = datetime.strptime(active_positions[hold_symbol]["entry_date"], "%Y-%m-%d")
        days_held = max(0, (datetime.now() - entry_date).days)

        # คำนวณ Live Risk/Reward และ Win Rate ตามสถานะปัจจุบัน
        curr_p_h = get_live_price(hold_symbol)
        h_cfg = optimized_params[hold_symbol]
        h_lookback = h_cfg.get("lookback", 60)
        h_decay = max(0.1, 1.0 - (days_held / (h_lookback * 2)))
        
        # คำนวณสถิติพื้นฐานสำหรับเปรียบเทียบ
        h_reward_avg = h_stat['net_profit_pct'] / max(1, h_stat['total_trades'])
        h_risk_val = optimized_params[hold_symbol]['initial_sl_pct'] * 100

        if curr_p_h:
            hold_score = evaluate_hold_position(hold_symbol, active_positions[hold_symbol], h_stat, curr_p_h)
            pnl_pct_h = (curr_p_h - active_positions[hold_symbol]['entry_price']) / active_positions[hold_symbol]['entry_price'] * 100
            
            # [FIX FULL]: ใช้ฟังก์ชันใหม่คำนวณแต้มต่อหน้างาน
            live_win_rate = calculate_live_win_rate(pnl_pct_h)
            h_sl_price = active_positions[hold_symbol]['entry_price'] * (1 - h_cfg['initial_sl_pct'])
            hold_rr_live = calculate_rr(active_positions[hold_symbol]['entry_price'], curr_p_h, h_sl_price)
        else:
            hold_score = 0
            pnl_pct_h = 0
            live_win_rate = 0
            hold_rr_live = 0
        
        new_score = 0
        for s in daily_signals_data:
            if s['symbol'] == new_symbol:
                new_score = s['final_score']
                break

        # --- 🥊 [BONUS]: สรุปคำวินิจฉัยสลับไม้ (Decision Matrix Table) ---
        st.markdown("##### 🏁 สรุปคำวินิจฉัยสลับไม้ (Opportunity Cost Decision)")
        
        # Logic: SWITCH เมื่อตัวใหม่ Score สูงกว่าตัวเดิมเกิน 30% หรือ SELL เมื่อหลุดวินัย
        highest_p, sma50_h = get_exit_signals(hold_symbol, active_positions[hold_symbol]["entry_date"], curr_p_h or 0)
        t_stop = highest_p * (1 - optimized_params[hold_symbol]['trailing_pct'])
        
        if curr_p_h and (curr_p_h <= t_stop or curr_p_h < sma50_h or ('/' not in hold_symbol and not is_us_healthy)):
            decision_val = "❌ SELL"
        elif new_score < (hold_score * 0.8):
            decision_val = "✅ HOLD"
        elif new_score <= (hold_score * 1.3):
            decision_val = "➡️ NEUTRAL"
        elif new_score > (hold_score * 1.3):
            decision_val = "🔁 SWITCH"

        summary_df = pd.DataFrame([{
            "Current Asset": hold_symbol,
            "PnL": f"{pnl_pct_h:+.2f}%",
            "Hold Score (Live)": round(hold_score, 1),
            "New Candidate": new_symbol,
            "New Score": round(new_score, 1),
            "Decision Matrix": decision_val
        }])
        st.table(summary_df)

        n_reward_avg = n_stat['net_profit_pct'] / max(1, n_stat['total_trades'])
        n_risk_val = optimized_params[new_symbol]['initial_sl_pct'] * 100
        new_rr_live = round(n_reward_avg / n_risk_val, 2)

        # สร้างโครงสร้าง Dataframe มาแสดงผลตารางเปรียบเทียบคลีน ๆ
        matrix_data = {
            "มิติชี้วัดความได้เปรียบ": [
                "⏱️ ระยะเวลาที่ถือครองมาแล้ว (Days Held)",
                "🎯 Win Rate สถิติภาพรวมอดีต",
                "🔥 Live Win Rate (แต้มต่อหน้างาน ณ วันนี้)",
                "📊 Live Risk/Reward (ความคุ้มค่าจากราคาปัจจุบัน)",
                "💰 กำไรสะสมในอดีต (Net Profit)",
                "📉 การยุบตัวสูงสุด (Max Drawdown)"
            ],
            f"ตัวเดิมในพอร์ต ({hold_symbol})": [
                f"{days_held} วัน",
                f"{h_stat['win_rate_pct']}%",
                f"{live_win_rate}% (ประเมินจาก PnL)",
                f"{hold_rr_live} เท่า",
                f"{h_stat['net_profit_pct']}%",
                f"-{h_stat['max_drawdown_pct']}%"
            ],
            f"ตัวเลือกใหม่เกิดสัญญาณ ({new_symbol})": [
                "0 วัน (เพิ่งเกิดสัญญาณวันนี้)",
                f"{n_stat['win_rate_pct']}%",
                f"{n_stat['win_rate_pct']}% (แต้มต่อสดใหม่ 100%)",
                f"{new_rr_live} เท่า (คัทลอสแคบ)",
                f"{n_stat['net_profit_pct']}%",
                f"-{n_stat['max_drawdown_pct']}%"
            ]
        }
        df_matrix = pd.DataFrame(matrix_data)
        st.table(df_matrix)
        
        # --- ✅ Action Matrix Scorecard (ฟันธง) ---
        st.markdown("##### 🤖 บทวิเคราะห์และคำแนะนำเชิงสถิติหน้างาน:")

        if new_score > (hold_score * 1.3) and len(active_positions) >= max_slots:
            st.warning(f"🔁 **คำแนะนำ: SWITCH** | {new_symbol} มีคะแนนความคมสูงกว่า {hold_symbol} เกิน 30% ({new_score:.1f} vs {hold_score:.1f}) พิจารณาสลับตัวเพื่อประสิทธิภาพสูงสุด")
        elif live_win_rate >= 50: # หรือใช้เกณฑ์ความมั่นใจจาก Live Win Rate
            st.success(f"💡 **คำแนะนำถือรันเทรนด์ต่อ (HOLD):** **{hold_symbol}** ยังมีแต้มต่อหน้างานที่แข็งแกร่ง (Live WR: {live_win_rate}%) และยังได้เปรียบกว่าตัวใหม่ในเชิงสถิติ ปล่อยรันกำไรต่อไปครับ")
        else:
            st.info(f"💡 **คำแนะนำเชิงลึก (กึ่งกึ่งถือคู่ - HOLD & WATCH):** ทั้งสองตัวมีข้อดีข้อเสียสลับกัน ตัวหนึ่งสถิติแม่นยำกว่า แต่อีกตัวกำไรคำโตกว่า หากพี่มีเงินสดในมือเหลืออยู่ แนะนำให้กระจายไปถือควบคู่กันได้ตามระบบบริหารหน้าตักครับ")
else:
    st.warning("⚠️ กรุณารันบอทหรือระบบค้นหาพารามิเตอร์เพื่อให้มีข้อมูลสถิติมารันระบบเปรียบเทียบความคุ้มค่าครับ")

st.markdown("---")

# บล็อกบันทึกประวัติการเทรด (Closed Trades) เพื่อเก็บ Track Record ระยะยาว
st.subheader("📝 บันทึกสถิติเมื่อปิดดีลขายขาด (Trade Closer)")
col_a, col_b, col_c, col_d, col_e = st.columns(5)
trade_symbol = col_a.text_input("หุ้น/คริปโตที่ปิดดีล").upper().strip()
trade_side = col_b.selectbox("ทิศทาง", ["LONG", "SHORT"])
t_entry = col_c.number_input("ราคาฝั่งซื้อ", min_value=0.0, key="t_entry")
t_exit = col_d.number_input("ราคาฝั่งขาย", min_value=0.0, key="t_exit")
t_pnl = col_e.number_input("กำไร/ขาดทุนสุทธิ (USD/USDT)", value=0.0, key="t_pnl")

if st.button("💾 บันทึกประวัติปิดดีลลงแทร็กเรคคอร์ด"):
    if trade_symbol and t_entry > 0 and t_exit > 0:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trades (date, symbol, side, entry_price, exit_price, pnl_usdt, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now().strftime("%Y-%m-%d"), trade_symbol, trade_side, t_entry, t_exit, t_pnl, "CLOSED"))
        conn.commit()
        conn.close()
        st.success(f"บันทึกดีล {trade_symbol} ลงฐานข้อมูลสำเร็จ!")
        st.rerun()

st.markdown("---")

# แสดงผลประวัติและกราฟ Equity Curve รวมระยะยาว
conn = sqlite3.connect(DB_NAME)
df_trades = pd.read_sql_query("SELECT * FROM trades ORDER BY date DESC", conn)
conn.close()

if not df_trades.empty:
    total_trades = len(df_trades)
    win_trades = len(df_trades[df_trades['pnl_usdt'] > 0])
    win_rate_total = (win_trades / total_trades) * 100
    total_pnl = df_trades['pnl_usdt'].sum()
    
    # คำนวณ Performance Analytics
    expectancy = calculate_expectancy(df_trades)
    sharpe = calculate_sharpe(df_trades)
    
    # แสดงแผงแดชบอร์ดสรุปชัยชนะด้านบนสุดของผลลัพธ์
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("💰 ทุนรวมลิงก์บอท", f"{state['current_capital']:,.2f} USD")
    m2.metric("📊 ปิดดีลไปแล้วทั้งหมด", f"{total_trades} ไม้")
    m3.metric("🎯 Win Rate รวม", f"{win_rate_total:.1f} %")
    m4.metric("📈 กำไรรวมสะสม (Net PnL)", f"{total_pnl:+,.2f} USD")
    
    # แสดงแถบ Performance Analytics ลึกระดับมืออาชีพ
    st.markdown("#### 🔬 Performance Analytics")
    a1, a2 = st.columns(2)
    a1.metric("📊 Expectancy / Trade", f"{expectancy} USD", help="กำไรที่คาดหวังได้จริงต่อการเทรด 1 ไม้")
    a2.metric("📈 Sharpe Ratio", sharpe, help="วัดความคุ้มค่าต่อความเสี่ยง (> 1 คือดี, > 2 คือยอดเยี่ยม)")

    st.markdown("### 📈 กราฟการเติบโตของเงินทุนสะสม (Cumulative Equity Curve)")
    df_trades['date'] = pd.to_datetime(df_trades['date'])
    df_reversed = df_trades.iloc[::-1].copy()
    df_reversed['cum_pnl'] = df_reversed['pnl_usdt'].cumsum()
    
    st.line_chart(df_reversed.set_index('date')['cum_pnl'])
    
    st.markdown("### 📜 ประวัติบันทึกการเทรดแบบละเอียด")
    st.dataframe(df_trades, use_container_width=True)
else:
    # กรณีพอร์ตเพิ่งตั้งไข่และยังไม่มีประวัติปิดดีล
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💰 ทุนรวมลิงก์บอท", f"{state['current_capital']:,.2f} USD")
    c2.metric("📊 ปิดดีลไปแล้วทั้งหมด", "0 ไม้")
    c3.metric("🎯 Win Rate รวม", "0.0 %")
    c4.metric("📈 กำไรรวมสะสม (Net PnL)", "0.00 USD")