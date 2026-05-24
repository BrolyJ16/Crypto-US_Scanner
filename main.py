import asyncio
import aiohttp
import ccxt.async_support as ccxt_async
import yfinance as yf
import pandas as pd
import requests
import time
import os
import json
import numpy as np
from datetime import datetime
from shared_logic import load_portfolio_state, calculate_final_score

# --- 1. CONFIGURATION & STATE SYSTEM ---
LINE_TOKEN = os.getenv('LINE_NOTIFY_TOKEN', 'YOUR_LINE_NOTIFY_TOKEN') 
RISK_PER_TRADE = 0.02    # 🛡️ เสี่ยงสูงสุดไม้ละ 2% ของพอร์ต
TRADING_FEE_PCT = 0.001  # 💸 ค่าเผื่อคอมมิชชัน

# --- CONFIGURATION: SCANNER ENGINE ---
TIMEFRAME = '1d'
VOL_SMA_PERIOD = 20
RSI_PERIOD = 14
RSI_MAX_THRESHOLD = 70.0 
MAX_CONCURRENT_REQUESTS = 5 # 🚦 จำกัดการเรียก API พร้อมกัน 5 ตัวเพื่อป้องกันโดน Block

def load_optimized_params():
    if os.path.exists('optimized_params.json'):
        with open('optimized_params.json', 'r') as f:
            return json.load(f)
    print("❌ ไม่พบไฟล์ optimized_params.json! กรุณารัน optimizer.py ก่อน")
    return {}

# โหลดสถานะล่าสุดจาก Dashboard เพื่อใช้งาน
PORTFOLIO_STATE = load_portfolio_state()
TOTAL_CAPITAL = PORTFOLIO_STATE.get("current_capital", 1500.0)
MAX_OPEN_POSITIONS = PORTFOLIO_STATE.get("max_open_positions", 4)
ACTIVE_POSITIONS = PORTFOLIO_STATE.get("active_positions", {})
CURRENT_POSITION_COUNT = len(ACTIVE_POSITIONS)

print(f"💰 ลิงก์ยอดทุนปัจจุบันจาก Dashboard: {TOTAL_CAPITAL:,.2f} USD/USDT")
print(f"📦 สถานะสล็อตพอร์ตปัจจุบัน: ถือครองอยู่ {CURRENT_POSITION_COUNT} / {MAX_OPEN_POSITIONS} ไม้")

# --- 4. ENGINE ดึงข้อมูลความเร็วสูง ASYNC HYBRID ---
async def fetch_stock_data_async(symbol, fetch_limit, semaphore):
    loop = asyncio.get_running_loop()
    async with semaphore:
        try:
            def get_data():
                ticker = yf.Ticker(symbol)
                return ticker.history(period="2y")
            df_stock = await loop.run_in_executor(None, get_data)
            if df_stock.empty: return symbol, None
            df = df_stock.reset_index()
            df.columns.values[0] = 'timestamp'
            df.columns = df.columns.str.lower()
            df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
            return symbol, df.tail(fetch_limit).reset_index(drop=True)
        except:
            return symbol, None

async def fetch_crypto_data_async(symbol, fetch_limit, semaphore):
    async with semaphore:
        exchange = ccxt_async.binance()
        try:
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=fetch_limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return symbol, df
        except:
            return symbol, None
        finally:
            await exchange.close()

def check_us_market_regime():
    try:
        # ดึงข้อมูล SPY เพื่อเช็ค SMA200 (Market Filter)
        ticker = yf.Ticker("SPY")
        df_spy = ticker.history(period="2y")
        if df_spy.empty: return True
        df_spy.columns = df_spy.columns.str.lower()
        df_spy['sma_200'] = df_spy['close'].rolling(window=200).mean()
        return df_spy['close'].iloc[-1] > df_spy['sma_200'].iloc[-1]
    except: return True # กรณี Error ให้ถือว่า Bullish ไว้ก่อนเพื่อไม่ให้ระบบค้าง

def calculate_signals(df, lookback, vol_multiplier):
    df['high_max'] = df['high'].shift(1).rolling(window=lookback).max()
    df['vol_sma_20'] = df['volume'].rolling(window=VOL_SMA_PERIOD).mean()
    
    # 🛠️ เพิ่มการคำนวณมูลค่าการซื้อขาย (Value Traded)
    df['value_sma_20'] = (df['close'] * df['volume']).rolling(window=VOL_SMA_PERIOD).mean()

    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=RSI_PERIOD - 1, min_periods=RSI_PERIOD).mean()
    avg_loss = loss.ewm(com=RSI_PERIOD - 1, min_periods=RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))

    last_row = df.iloc[-1]
    is_breakout = last_row['close'] > last_row['high_max']
    
    # 🛠️ เพิ่มเงื่อนไข Absolute Volume Filter: มูลค่าซื้อขาย (Close * Vol) ต้องมากกว่าค่าเฉลี่ย
    current_value_traded = last_row['close'] * last_row['volume']
    is_vol_spike = (last_row['volume'] > (last_row['vol_sma_20'] * vol_multiplier)) and \
                   (current_value_traded > last_row['value_sma_20'])
                   
    return is_breakout, is_vol_spike, last_row

def send_line_notify(message, token):
    url = 'https://notify-api.line.me/api/notify'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/x-www-form-urlencoded'}
    try: requests.post(url, headers=headers, data={'message': message})
    except: pass

# --- 5. CORE SCANNING PROCESS ---
async def main():
    start_time = time.time()
    print(f"\n🔍 --- เริ่มสแกนและวิเคราะห์พอร์ต: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    is_us_healthy = check_us_market_regime()
    print(f"📊 S&P 500 Market Filter: {'🟢 BULL (เปิดสัญญาณหุ้น/ETF)' if is_us_healthy else '🔴 BEAR (ระงับสัญญาณฝั่งหุ้น)'}")
    
    optimized_params = load_optimized_params()
    all_results = {}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    
    # 1. ดึงข้อมูลสินทรัพย์ทั้งหมดแบบขนาน (Async)
    tasks = []
    for symbol, config in optimized_params.items():
        limit = config['lookback'] + 50
        if '/' in symbol:
            tasks.append(fetch_crypto_data_async(symbol, limit, semaphore))
        else:
            tasks.append(fetch_stock_data_async(symbol, limit, semaphore))
            
    fetched_data = await asyncio.gather(*tasks)
    for sym, df in fetched_data:
        if df is not None:
            all_results[sym] = df

    # 2. 🛡️ คำนวณรันเทรนด์และอัปเดตจุด Trailing Stop ของตัวที่มีอยู่จริงในพอร์ต
    print("\n📦 [รายงานอัปเดตหุ้นในพอร์ตปัจจุบัน]")
    holding_performance = {}
    
    for symbol, hold_info in ACTIVE_POSITIONS.items():
        if symbol not in all_results:
            print(f"   • ⚠️ ไม่พบข้อมูลราคาของ {symbol}")
            continue
            
        df = all_results[symbol]
        
        # 🛡️ ป้องกันการใช้ค่าคงที่ทดสอบ: หากไม่มีพารามิเตอร์ ให้ข้ามหรือใช้ค่า Default ที่ปลอดภัยที่สุด
        if symbol not in optimized_params:
            print(f"   • ⚠️ ไม่พบ Optimized Params สำหรับ {symbol} (ข้ามการวิเคราะห์ Decay)")
            continue
        config = optimized_params[symbol]
        
        # --- [UPGRADE: Live Edge Analysis] ---
        df['timestamp_clean'] = pd.to_datetime(df['timestamp']).dt.date
        entry_date_obj = datetime.strptime(hold_info['entry_date'], "%Y-%m-%d").date()
        days_held = (datetime.now().date() - entry_date_obj).days
        days_held = max(0, days_held)
        
        df_held = df[df['timestamp_clean'] >= entry_date_obj]
        current_price = df['close'].iloc[-1]
        pnl_pct = ((current_price - hold_info['entry_price']) / hold_info['entry_price']) * 100
        
        # คำนวณความเสื่อมถอยของแต้มต่อ (Performance Decay)
        lookback = config.get('lookback', 60)
        decay_factor = max(0.1, 1.0 - (days_held / (lookback * 2)))
        live_win_rate = round(config['metrics']['win_rate_pct'] * decay_factor, 1)
        
        if not df_held.empty:
            highest_high = df_held['high'].max()
            current_trailing_stop = highest_high * (1 - config['trailing_pct'])
        else:
            current_trailing_stop = hold_info['entry_price'] * (1 - config['trailing_pct'])
            
        currency = "USDT" if "/" in symbol else "USD"
        print(f"   • 🔹 {symbol} | ถือมาแล้ว: {days_held} วัน | Live WR: {live_win_rate}%")
        print(f"     👉 Trailing Stop วันนี้: {current_trailing_stop:.2f} {currency} (PnL: {pnl_pct:+.1f}%)")
        
        # เก็บสถิติความสดเพื่อเอาไว้เทียบมวย
        holding_performance[symbol] = {
            "pnl_pct": pnl_pct,
            "live_win_rate": live_win_rate,
            "days_held": days_held,
            "current_price": current_price
        }

    # 3. สแกนหาตัวใหม่ที่ระเบิดสัญญาณเช้านี้
    print("\n🔍 [ผลการสแกนหาสัญญาณเช้านี้]")
    new_signals = []
    
    for symbol, df in all_results.items():
        if symbol in ACTIVE_POSITIONS: continue # ข้ามตัวที่มีอยู่แล้ว
        
        # 📖 ดึงการตั้งค่าเฉพาะตัวของสินทรัพย์นั้น ๆ จาก JSON
        config = optimized_params.get(symbol, {})
        lookback = config.get('lookback', 60)
        # 🛡️ ดึงค่า Multiplier เฉพาะตัว ถ้าไม่มีให้ใช้ค่ามาตรฐาน 1.5 กันเหนียว
        current_vol_multiplier = config.get('vol_multiplier', 1.5)
        
        is_breakout, is_vol_spike, data = calculate_signals(df, lookback, current_vol_multiplier)
        
        status_msg = "❌ No Signal"
        if is_breakout and not is_vol_spike: status_msg = "Breakout แต่ Volume ไม่เข้า"
        elif not is_breakout and is_vol_spike: status_msg = "Volume Spike แต่ราคาไม่เบรค"
        elif is_breakout and is_vol_spike: status_msg = "🔥 SIGNAL PASS! (ครบเงื่อนไขพื้นฐาน)"
        
        print(f"   • {symbol:<9} | ราคา: {data['close']:<8.2f} | RSI: {data['rsi']:<4.1f} | สถานะ: {status_msg}")
        
        if is_breakout and is_vol_spike:
            # ด่านกรองความปลอดภัยขั้นสูง
            if data['rsi'] > RSI_MAX_THRESHOLD:
                print(f"   ↳ ⏭️  ข้าม {symbol} (RSI {data['rsi']:.1f} สูงเกินเกณฑ์)")
                continue
                
            if '/' not in symbol and not is_us_healthy:
                print(f"   ↳ ⚠️  [งดซื้อ] {symbol} ตลาดภาพใหญ่ (SPY) เป็นหมี")
                continue

            # [Rule 5] Chase limit +2% (ไม่ไล่ราคา)
            breakout_lvl = data['high_max']
            chase_limit = breakout_lvl * 1.02
            if data['close'] > chase_limit:
                print(f"   ↳ ⏭️  ข้าม {symbol} (ราคา {data['close']:.2f} ห่างจากจุดเบรค {breakout_lvl:.2f} เกิน 2%)")
                continue

            # ✅ Step 2 & 3: Ranking & Confidence (FINAL FORMULA)
            final_score = calculate_final_score(config['metrics'])
            confidence = min(1.0, config['metrics']['total_trades'] / 12.0)

            new_signals.append({
                "symbol": symbol,
                "price": data['close'],
                "config": config,
                "final_score": round(final_score, 2),
                "confidence": round(confidence, 2),
                "conf_level": "HIGH ✅" if confidence >= 0.8 else "MED ✅" if confidence >= 0.5 else "LOW ⚠️"
            })

    # จัดลำดับความคม: เรียงจาก Final Score มากไปน้อย
    new_signals.sort(key=lambda x: x['final_score'], reverse=True)
    
    # ✅ Step 4 — เลือก Top 4 (ถ้ามีไม่ถึง 4 ก็เอาเท่าที่มี ไม่ฝืน)
    top_signals = new_signals[:4]
    
    # ✅ [Pro Upgrade] 1. Conviction Score (คำนวณน้ำหนักความมั่นใจรวมของกลุ่มหัวกะทิ)
    total_top_score = sum(s['final_score'] for s in top_signals)

    # [SAVE SIGNALS] บันทึกสัญญาณที่ผ่านการคัดกรองแล้วลงไฟล์ เพื่อให้ Dashboard นำไปแสดงผลเฉพาะตัวที่มีความสดใหม่
    with open('daily_signals.json', 'w') as f:
        json.dump(top_signals, f, indent=4)

    # 4. 🧠 สมองส่วนวิเคราะห์เปรียบเทียบเชิงลึก (Hold vs. Switch Analysis)
    report_message = ""
    if top_signals:
        # ✅ [Pro Upgrade] 2. Kill Switch (ลดระดับความเสี่ยงลง 50% หากตลาดภาพใหญ่เป็น Bear)
        market_multiplier = 1.0 if is_us_healthy else 0.5
        effective_risk = RISK_PER_TRADE * market_multiplier
        
        report_message += f"\n📋 [สรุปอันดับหุ้นหัวกะทิเช้านี้]\n"
        report_message += f"ตลาดภาพรวม: {'🟢 ปกติ (เสี่ยง 2%)' if market_multiplier == 1.0 else '🔴 อันตราย (Kill Switch: ลดเสี่ยงเหลือ 1%)'}\n"
        report_message += f"--------------------------------------\n"
        report_message += f"Rank | Symbol | Score | Conf | Weight\n"
        for i, sig in enumerate(top_signals, 1):
            conviction_pct = (sig['final_score'] / total_top_score * 100) if total_top_score > 0 else 0
            report_message += f"{i} | {sig['symbol']:<7} | {sig['final_score']:<5} | {sig['conf_level']} | {conviction_pct:.1f}%\n"
        report_message += f"--------------------------------------\n"

        if len(new_signals) > 4:
            report_message += f"💡 พบทั้งหมด {len(new_signals)} ตัว แต่เลือกมาเฉพาะ Top 4 ที่คมที่สุด\n"
        elif len(new_signals) < 4:
            report_message += f"💡 วันนี้สัญญาณน้อย (พบ {len(new_signals)} ตัว) เทรดเท่าที่มี ไม่ฝืนครับ\n"
        
        # รายละเอียดเชิงลึกสำหรับแต่ละตัวใน Top 4
        for i, sig in enumerate(top_signals, 1):
            sym = sig['symbol']
            price = sig['price']
            cfg = sig['config']
            currency = "USDT" if "/" in sym else "USD"
            
            # ✅ [Pro Upgrade] 3. Real Position Sizing (คำนวณตาม Risk และ Stop Loss จริง)
            # สูตร: position_size = (Capital * Risk%) / StopLoss%
            risk_amount = TOTAL_CAPITAL * effective_risk
            pos_size = risk_amount / cfg['initial_sl_pct']
            pos_size = min(pos_size, TOTAL_CAPITAL / MAX_OPEN_POSITIONS) # เฉลี่ยหน้าตักไม่ให้กระจุกตัว
            units = (pos_size * (1 - TRADING_FEE_PCT)) / price
            
            sl_price = price * (1 - cfg['initial_sl_pct'])
            
            report_message += (
                f"\n🔥 [Rank #{i}] {sym} (Score: {sig['final_score']} | Conf: {sig['conf_level']})\n"
                f"   • ราคาเข้าซื้อเปิดตลาด: {price:,.2f}\n"
                f"   • ตั้ง Stop Loss: {sl_price:,.2f} (-{cfg['initial_sl_pct']*100:.0f}%)\n"
                f"   • ตั้ง Trailing Stop % ในแอป: {cfg['trailing_pct']*100:.0f}%\n"
                f"   👉 แนะนำขนาดไม้เทรด: ซื้อด้วยเงิน {pos_size:,.2f} {currency} (จำนวน {units:.4f} หน่วย)\n"
            )
            
            # 🥊 ตรรกะเปรียบเทียบมวยคู่เอกหากพอร์ตถือของเต็มโควตา
            if CURRENT_POSITION_COUNT >= MAX_OPEN_POSITIONS:
                report_message += f"\n   ⚠️ [PORTFOLIO FULL ALERT - วิเคราะห์การสลับตัว]\n"
                report_message += f"   ตอนนี้พอร์ตถือเต็ม {MAX_OPEN_POSITIONS} ไม้แล้ว หากต้องการเข้าซื้อ {sym} วันนี้ นี่คือตารางวิเคราะห์ความคุ้มค่า:\n"
                
                for hold_sym, perf in holding_performance.items():
                    # [UPGRADE] เทียบความสด (New WR) ปะทะ ความแก่ (Live Win Rate)
                    wr_diff = cfg['metrics']['win_rate_pct'] - perf['live_win_rate']
                    trend_age_limit = optimized_params.get(hold_sym, {}).get('lookback', 60) * 0.75 # อายุขัยที่ 75% ของรอบเบรค
                    
                    if wr_diff > 10 or perf['days_held'] > trend_age_limit:
                        report_message += (
                            f"   🔄 [คำแนะนำสลับไม้]: พิจารณาขาย {hold_sym} (ถือมา {perf['days_held']} วัน / Live WR เหลือ {perf['live_win_rate']}%)\n"
                            f"       ↳ เพื่อเข้า {sym} แทน เพราะตัวใหม่มีแต้มต่อสดใหม่สูงกว่าถึง {wr_diff:.1f}%!\n"
                        )
                    else:
                        report_message += f"   📦 [คำแนะนำถือต่อ]: {hold_sym} ยังมีแต้มต่อหน้างานที่ไว้ใจได้ (Live WR: {perf['live_win_rate']}%)\n"

    print(f"\n⚡ สแกนสินทรัพย์ทั้งหมดเรียบร้อยใน {time.time()-start_time:.2f} วินาที")
    if report_message:
        print(report_message)
        if LINE_TOKEN != 'YOUR_LINE_NOTIFY_TOKEN':
            send_line_notify(report_message, LINE_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())