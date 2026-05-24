import os
import json
import time
import ccxt
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime

# --- 1. รายชื่อสินทรัพย์เรดาร์ไฮบริด (Crypto + US Stocks 7 กลุ่ม) ---
CRYPTO_SYMBOLS = ['BTC/USDT', 'ETH/USDT']
STOCK_SYMBOLS = [
    'MSFT', 'AAPL', 'NVDA', 'AVGO', 'AMD', 'CRM', 'ADBE', 'ORCL', 'IBM', 'QCOM',
    'GOOGL', 'META', 'NFLX', 'DIS', 'VZ', 'AMZN', 'TSLA', 'HD', 'NKE', 'MCD',
    'LLY', 'JNJ', 'UNH', 'MRK', 'ABBV', 'PFE', 'TMO', 'ABT', 'JPM', 'BRK-B',
    'V', 'MA', 'BAC', 'GS', 'MS', 'PG', 'COST', 'WMT', 'KO', 'PEP',
    'PM', 'XOM', 'CVX', 'CAT', 'GE', 'LMT', 'RTX', 'UNP', 'UPS', 'HON'
]
ETF_SYMBOLS = ['VT', 'ACWI', 'VXUS', 'VEA', 'VWO', 
               'QQQ', 'SMH', 'XLK', 'XLV', 'XLF', 'IBUY', 'TAN',
               'GLD', 'IAU', 'SLV', 'BND', 'AGG', 'VNQ']  # 📊 [NEW!] เสริมทัพด้วย ETF ระดับโลก

DATA_PERIOD_DAYS = 1000
RISK_PER_TRADE = 0.02   # 🛡️ เสี่ยงไม้ละ 2% (ใช้คำนวณ Drawdown/Compounding จริง)
TRADING_FEE_PCT = 0.001 # 💸 ค่าคอมมิชชันเผื่อไว้ 0.1%
CACHE_DIR = "market_data_cache" # 📂 โฟลเดอร์เก็บข้อมูลดิบในเครื่อง

# สร้างโฟลเดอร์ Cache อัตโนมัติหากยังไม่มี
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

# --- 2. ช่วงตัวแปรพารามิเตอร์ที่ต้องการทำ Parameter Sweep ---
VOL_MULT_CHOICES = [round(x, 2) for x in np.arange(1.15, 1.55, 0.05)]
LOOKBACK_CHOICES = [60, 100, 140, 180]
INITIAL_SL_CHOICES = [0.05, 0.10, 0.15]       # 5%, 10%, 15%
TRAILING_CHOICES = [0.05, 0.10, 0.15, 0.20]    # 5%, 10%, 15%, 20%

# --- 3. [UPGRADE] ฟังก์ชันดึงข้อมูลแบบมีระบบ Local Cache ---
def fetch_crypto_data(symbol, days):
    safe_name = symbol.replace('/', '_')
    cache_path = os.path.join(CACHE_DIR, f"{safe_name}_daily.csv")
    
    # ⏱️ ถ้ามีไฟล์ในเครื่อง และไฟล์นั้นสร้างขึ้นวันนี้ -> โหลดจากเครื่องเลย
    if os.path.exists(cache_path):
        file_time = datetime.fromtimestamp(os.path.getmtime(cache_path)).date()
        if file_time == datetime.now().date():
            df = pd.read_csv(cache_path)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            return df

    # ถ้าไม่มี หรือไฟล์เก่าแล้ว -> ดึงใหม่จาก API
    exchange = ccxt.binance()
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1d', limit=days)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        # 🛠️ จัดการข้อมูล NaN (ถ้ามี) จากความผิดพลาดของ API
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].ffill().bfill()
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.to_csv(cache_path, index=False) # เซฟเก็บไว้ใช้รอบหน้า
        return df
    except Exception as e:
        print(f"❌ Crypto Fetch Error ({symbol}): {e}")
        return None

def fetch_us_market_data(symbol):
    """รองรับการดึงและทำ Cache ทั้งหุ้นรายตัวและ ETF สหรัฐฯ"""
    cache_path = os.path.join(CACHE_DIR, f"{symbol}_daily.csv")
    
    if os.path.exists(cache_path):
        file_time = datetime.fromtimestamp(os.path.getmtime(cache_path)).date()
        if file_time == datetime.now().date():
            df = pd.read_csv(cache_path)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            return df

    try:
        ticker = yf.Ticker(symbol)
        df_stock = ticker.history(period="5y")
        if df_stock.empty: return None
        df = df_stock.reset_index()
        df.columns.values[0] = 'timestamp'
        df.columns = df.columns.str.lower()
        df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
        # 🛠️ จัดการข้อมูล NaN จากช่วงวันหยุดตลาดหุ้นสหรัฐฯ
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].ffill().bfill()
        df_final = df.tail(DATA_PERIOD_DAYS + 200).reset_index(drop=True)
        df_final.to_csv(cache_path, index=False)
        return df_final
    except Exception as e:
        print(f"❌ Market Fetch Error ({symbol}): {e}")
        return None

# --- 4. 🏆 เครื่องยนต์จำลองการเทรดจริงรายวัน (Backtest Simulation Engine) ---
def run_backtest_simulation(df, vol_multiplier, lookback, initial_sl_pct, trailing_pct):
    """
    จำลองสถานะการเปิด-ปิดออเดอร์ในอดีตทีละวัน (Bar-by-Bar)
    """
    # คำนวณอินดิเคเตอร์พื้นฐาน
    df['high_max'] = df['high'].shift(1).rolling(window=lookback).max()
    df['vol_sma_20'] = df['volume'].rolling(window=20).mean()
    # 🛠️ เพิ่มการคำนวณมูลค่าการซื้อขาย (Price * Volume)
    df['value_traded'] = df['close'] * df['volume']
    df['value_sma_20'] = df['value_traded'].rolling(window=20).mean()
    
    # ลบแถวที่อินดิเคเตอร์ยังคำนวณไม่เสร็จออก
    df_clean = df.dropna(subset=['high_max', 'vol_sma_20', 'value_sma_20']).reset_index(drop=True)
    
    # ตัวแปรจำลองสถานะ (State Machine)
    in_position = False
    entry_price = 0.0
    highest_price_since_entry = 0.0
    current_sl_price = 0.0
    
    trades_log = []
    capital = 1000.0 # ทุนสมมติเริ่มต้นระบบ
    peak_capital = 1000.0
    max_drawdown = 0.0
    
    for i in range(len(df_clean)):
        row = df_clean.iloc[i]
        current_close = row['close']
        current_high = row['high']
        current_low = row['low']
        
        if not in_position:
            # 🟢 เงื่อนไขการเข้าซื้อ (Entry Logic)
            is_breakout = current_close > row['high_max']
            # 🛠️ เพิ่ม Absolute Volume Filter: โวลุ่มต้องเกินค่าเฉลี่ยตามตัวคูณ และมูลค่าซื้อขายต้องมากกว่าค่าเฉลี่ยด้วย
            is_vol_spike = (row['volume'] > (row['vol_sma_20'] * vol_multiplier)) and \
                           (row['value_traded'] > row['value_sma_20'])
            
            if is_breakout and is_vol_spike:
                in_position = True
                entry_price = current_close
                highest_price_since_entry = current_high
                current_sl_price = entry_price * (1 - initial_sl_pct)
        else:
            # 🔴 [Fidelity Update] เช็คราคาความผันผวนระหว่างวันระลอกย่อยก่อนขยับฐาน
            if current_high > highest_price_since_entry:
                highest_price_since_entry = current_high
                new_trailing_sl = highest_price_since_entry * (1 - trailing_pct)
                current_sl_price = max(current_sl_price, new_trailing_sl)
            
            # 🛑 เช็คเงื่อนไขการออก (Exit Logic)
            if current_low <= current_sl_price:
                in_position = False
                exit_price = current_sl_price
                
                trade_return = (exit_price - entry_price) / entry_price
                trade_return -= (TRADING_FEE_PCT * 2)
                
                # Position Sizing Effect (เสี่ยง 2% ของพอร์ต)
                simulated_pnl_pct = (trade_return / initial_sl_pct) * RISK_PER_TRADE
                capital *= (1 + simulated_pnl_pct)
                
                if capital > peak_capital:
                    peak_capital = capital
                dd = (peak_capital - capital) / peak_capital
                if dd > max_drawdown:
                    max_drawdown = dd
                trades_log.append(simulated_pnl_pct)
                
    total_trades = len(trades_log)
    if total_trades == 0:
        return -100.0, 0.0, 0, 0.0
        
    net_profit_pct = ((capital - 1000.0) / 1000.0) * 100
    win_trades = sum(1 for r in trades_log if r > 0)
    win_rate = (win_trades / total_trades) * 100
    max_dd_pct = max_drawdown * 100
    
    return net_profit_pct, win_rate, total_trades, max_dd_pct

# --- 5. ฟังก์ชันหลักสั่งรันวนลูปประมวลผลหาค่าที่ดีที่สุด (Optimization Loop) ---
def optimize_all_assets():
    start_runtime = time.time()
    print("--- 🛠️ STARTING ADVANCED HYBRID ASSET OPTIMIZATION (WITH ETFS) 🛠️ ---")
    optimized_results = {}
    all_symbols = CRYPTO_SYMBOLS + STOCK_SYMBOLS + ETF_SYMBOLS
    
    for symbol in all_symbols:
        print(f"\n⏳ Simulating & Optimizing: {symbol}...")
        
        if '/' in symbol:
            df = fetch_crypto_data(symbol, DATA_PERIOD_DAYS)
        else:
            df = fetch_us_market_data(symbol)
            
        if df is None or len(df) < 180:
            print(f"⚠️ ข้าม {symbol} เนื่องจากข้อมูลไม่เพียงพอ")
            continue
            
        best_pnl = -999999.0
        best_config = None
        
        for vol_mult in VOL_MULT_CHOICES:
            for lookback in LOOKBACK_CHOICES:
                for initial_sl in INITIAL_SL_CHOICES:
                    for trailing in TRAILING_CHOICES:
                        pnl, wr, trades, max_dd = run_backtest_simulation(
                            df.copy(), vol_mult, lookback, initial_sl, trailing
                        )
                        
                        # 🛠️ คัดเฉพาะ Config ที่มีการเทรด >= 5 ครั้ง เพื่อความเสถียรของสถิติ
                        if pnl > best_pnl and trades >= 5:
                            best_pnl = pnl
                            best_config = {
                                "vol_multiplier": vol_mult,
                                "lookback": lookback,
                                "initial_sl_pct": initial_sl,
                                "trailing_pct": trailing,
                                "metrics": {
                                    "net_profit_pct": round(pnl, 2),
                                    "win_rate_pct": round(wr, 2),
                                    "total_trades": trades,
                                    "max_drawdown_pct": round(max_dd, 2)
                                }
                            }
                        
        if best_config:
            optimized_results[symbol] = best_config
            m = best_config['metrics']
            print(f"🏆 Best: Multiplier {best_config['vol_multiplier']} | Lookback {best_config['lookback']} | SL {best_config['initial_sl_pct']*100}% | Trail {best_config['trailing_pct']*100}%")
            print(f"📊 Profit: {m['net_profit_pct']}% | WR: {m['win_rate_pct']}% | Trades: {m['total_trades']} | MaxDD: -{m['max_drawdown_pct']}%")
        else:
            print(f"❌ {symbol} ไม่พบพารามิเตอร์ที่ทำกำไรได้")
            
    with open('optimized_params.json', 'w') as f:
        json.dump(optimized_results, f, indent=4)
    print(f"\n💾 Saved all parameters to 'optimized_params.json'! Total Time: {time.time() - start_runtime:.2f}s 🎉")

if __name__ == "__main__":
    optimize_all_assets()