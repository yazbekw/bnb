import pandas as pd
import numpy as np
import requests
import json
from datetime import datetime, timedelta
import pytz
import time

# Symbols and intervals
symbols = ["SOLUSDT", "ETHUSDT", "BNBUSDT", "LINKUSDT"]
intervals = ['30m']

# Date range (last 2 months)
end_date = datetime.now(pytz.UTC)
start_date = end_date - timedelta(days=60)
start_ts = int(start_date.timestamp() * 1000)
end_ts = int(end_date.timestamp() * 1000)

print(f"Fetching data from {start_date} to {end_date}")

# Optimized settings
rsi_period = 14
sma_short = 10
sma_long = 50
rsi_buy_threshold = 50
rsi_sell_threshold = 50
atr_period = 14
atr_multiplier_sl = 1.0
atr_multiplier_tp = 1.5
max_leverage = 8.0
min_leverage = 2.0

# Additional indicators
volume_sma_period = 10
vwap_period = 14
macd_fast = 8
macd_slow = 21
macd_signal = 5

def get_historical_data(symbol, interval, start_ts, end_ts):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        'symbol': symbol,
        'interval': interval,
        'startTime': start_ts,  # Corrected: was 'starTime'
        'endTime': end_ts,
        'limit': 1000  # Reduced to avoid rate limits
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        klines = response.json()
        if not klines:
            print(f"No data returned for {symbol}")
            return None
        
        data = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        data['timestamp'] = pd.to_datetime(data['timestamp'], unit='ms')
        data[['open', 'high', 'low', 'close', 'volume']] = data[['open', 'high', 'low', 'close', 'volume']].astype(float)
        print(f"Successfully fetched {len(data)} candles for {symbol}")
        return data
    except Exception as e:
        print(f"Error fetching {symbol} {interval}: {e}")
        return None

def calculate_indicators(df):
    # Moving averages
    df['sma10'] = df['close'].rolling(window=sma_short).mean()
    df['sma50'] = df['close'].rolling(window=sma_long).mean()
    df['sma5'] = df['close'].rolling(window=5).mean()
    
    # RSI
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = (-delta).where(delta < 0, 0).rolling(rsi_period).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # ATR
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = np.maximum(np.maximum(high_low, high_close), low_close)
    df['atr'] = tr.rolling(atr_period).mean()
    
    # VWAP
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
    df['vwap'] = (df['typical_price'] * df['volume']).rolling(vwap_period).sum() / df['volume'].rolling(vwap_period).sum()
    
    # MACD
    exp1 = df['close'].ewm(span=macd_fast, adjust=False).mean()
    exp2 = df['close'].ewm(span=macd_slow, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=macd_signal, adjust=False).mean()
    df['macd_histogram'] = df['macd'] - df['macd_signal']
    
    # Volume indicators
    df['volume_sma'] = df['volume'].rolling(volume_sma_period).mean()
    df['volume_ratio'] = df['volume'] / df['volume_sma']
    
    # Price changes
    df['price_change'] = df['close'].pct_change()
    df['volatility'] = df['price_change'].rolling(10).std()
    
    return df.dropna().reset_index(drop=True)

def calculate_leverage(atr, price, volatility):
    """Calculate dynamic leverage"""
    if pd.isna(atr) or pd.isna(price) or price == 0:
        return min_leverage
    
    volatility_ratio = atr / price
    if volatility_ratio < 0.008:
        return max_leverage
    elif volatility_ratio < 0.015:
        return 6.0
    elif volatility_ratio < 0.025:
        return 4.0
    else:
        return min_leverage

def backtest(symbol, interval):
    print(f"\nBacktesting {symbol} on {interval}...")
    
    data = get_historical_data(symbol, interval, start_ts, end_ts)
    if data is None or len(data) < sma_long:
        print(f"Insufficient data for {symbol}")
        return {'buy_signals': 0, 'sell_signals': 0, 'expected_return': 0.0, 'total_trades': 0}
    
    data = calculate_indicators(data)
    print(f"Calculated indicators for {symbol}, {len(data)} rows")
    
    buy_signals = 0
    sell_signals = 0
    positions = []
    current_position = None
    total_trades = 0
    
    for i in range(3, len(data)):
        try:
            prev3 = data.iloc[i-3] if i >= 3 else None
            prev2 = data.iloc[i-2] if i >= 2 else None
            prev = data.iloc[i-1]
            curr = data.iloc[i]
            
            # Buy conditions
            buy_conditions = [
                # Moving average crossovers
                (curr['sma10'] > curr['sma50']) and (prev['sma10'] <= prev['sma50']),
                (curr['sma5'] > curr['sma10']) and (prev['sma5'] <= prev['sma10']),
                
                # RSI conditions
                (45 <= curr['rsi'] <= 70),
                (curr['rsi'] > 50) and (prev['rsi'] <= 50),
                
                # Trend direction
                curr['close'] > curr['sma50'],
                curr['close'] > curr['vwap'],
                
                # MACD momentum
                curr['macd'] > curr['macd_signal'],
                curr['macd_histogram'] > prev['macd_histogram'],
                
                # Volume conditions
                curr['volume_ratio'] > 0.7,
                curr['volume'] > prev['volume']
            ]
            
            # Sell conditions
            sell_conditions = [
                # Moving average crossovers
                (curr['sma10'] < curr['sma50']) and (prev['sma10'] >= prev['sma50']),
                (curr['sma5'] < curr['sma10']) and (prev['sma5'] >= prev['sma10']),
                
                # RSI conditions
                (30 <= curr['rsi'] <= 55),
                (curr['rsi'] < 50) and (prev['rsi'] >= 50),
                
                # Trend direction
                curr['close'] < curr['sma50'],
                curr['close'] < curr['vwap'],
                
                # MACD momentum
                curr['macd'] < curr['macd_signal'],
                curr['macd_histogram'] < prev['macd_histogram'],
                
                # Volume conditions
                curr['volume_ratio'] > 0.7,
                curr['volume'] > prev['volume']
            ]
            
            buy_score = sum(1 for condition in buy_conditions if condition)
            sell_score = sum(1 for condition in sell_conditions if condition)
            
            buy_signal = buy_score >= 4
            sell_signal = sell_score >= 4
            
            # Position management
            leverage = calculate_leverage(curr['atr'], curr['open'], curr['volatility'])
            
            if buy_signal:
                buy_signals += 1
                
                if current_position and current_position['side'] == 'SHORT':
                    # Close short position
                    exit_price = curr['open']
                    pnl = ((current_position['entry_price'] - exit_price) / 
                          current_position['entry_price'] * current_position['leverage'])
                    positions.append(pnl)
                    total_trades += 1
                    current_position = None
                
                if not current_position:
                    # Open long position
                    current_position = {
                        'side': 'LONG', 
                        'entry_price': curr['open'], 
                        'atr': curr['atr'], 
                        'leverage': leverage,
                        'entry_time': i
                    }
            
            if sell_signal:
                sell_signals += 1
                
                if current_position and current_position['side'] == 'LONG':
                    # Close long position
                    exit_price = curr['open']
                    pnl = ((exit_price - current_position['entry_price']) / 
                          current_position['entry_price'] * current_position['leverage'])
                    positions.append(pnl)
                    total_trades += 1
                    current_position = None
                
                if not current_position:
                    # Open short position
                    current_position = {
                        'side': 'SHORT', 
                        'entry_price': curr['open'], 
                        'atr': curr['atr'], 
                        'leverage': leverage,
                        'entry_time': i
                    }
            
            # Manage open positions
            if current_position:
                atr = current_position['atr']
                leverage = current_position['leverage']
                entry_price = current_position['entry_price']
                time_in_trade = i - current_position['entry_time']
                
                if current_position['side'] == 'LONG':
                    sl = entry_price - (atr * atr_multiplier_sl)
                    tp = entry_price + (atr * atr_multiplier_tp)
                    
                    # Exit conditions
                    if curr['low'] <= sl or curr['high'] >= tp or time_in_trade > 15:
                        if curr['low'] <= sl:
                            exit_price = sl
                        elif curr['high'] >= tp:
                            exit_price = tp
                        else:
                            exit_price = curr['close']
                        
                        pnl = (exit_price - entry_price) / entry_price * leverage
                        positions.append(pnl)
                        total_trades += 1
                        current_position = None
                        
                else:  # SHORT position
                    sl = entry_price + (atr * atr_multiplier_sl)
                    tp = entry_price - (atr * atr_multiplier_tp)
                    
                    if curr['high'] >= sl or curr['low'] <= tp or time_in_trade > 15:
                        if curr['high'] >= sl:
                            exit_price = sl
                        elif curr['low'] <= tp:
                            exit_price = tp
                        else:
                            exit_price = curr['close']
                        
                        pnl = (entry_price - exit_price) / entry_price * leverage
                        positions.append(pnl)
                        total_trades += 1
                        current_position = None
                        
        except Exception as e:
            print(f"Error processing row {i} for {symbol}: {e}")
            continue
    
    # Close any remaining position
    if current_position:
        exit_price = data.iloc[-1]['close']
        leverage = current_position['leverage']
        if current_position['side'] == 'LONG':
            pnl = (exit_price - current_position['entry_price']) / current_position['entry_price'] * leverage
        else:
            pnl = (current_position['entry_price'] - exit_price) / current_position['entry_price'] * leverage
        positions.append(pnl)
        total_trades += 1
    
    # Calculate statistics
    if positions:
        cumulative_return = (np.prod([1 + p for p in positions]) - 1) * 100
        win_rate = (sum(1 for p in positions if p > 0) / len(positions)) * 100
        losses = [p for p in positions if p < 0]
        profits = [p for p in positions if p > 0]
        profit_factor = sum(profits) / abs(sum(losses)) if losses and sum(losses) != 0 else float('inf')
    else:
        cumulative_return = 0.0
        win_rate = 0.0
        profit_factor = 0.0
    
    print(f"Completed backtest for {symbol}: {total_trades} trades, {cumulative_return:.2f}% return")
    
    return {
        'buy_signals': buy_signals,
        'sell_signals': sell_signals,
        'expected_return': round(cumulative_return, 2),
        'total_trades': total_trades,
        'win_rate': round(win_rate, 2),
        'profit_factor': round(profit_factor, 2),
        'avg_trade_return': round(np.mean(positions) * 100, 2) if positions else 0.0
    }

# Run backtests
print("Starting enhanced backtesting...")
print("=" * 50)

results = {}
for symbol in symbols:
    results[symbol] = {}
    for interval in intervals:
        # Add delay to avoid rate limiting
        time.sleep(1)
        results[symbol][interval] = backtest(symbol, interval)

# Display results
print("\n" + "=" * 60)
print("ENHANCED STRATEGY RESULTS - INCREASED TRADES")
print("=" * 60)

for symbol in symbols:
    for interval in intervals:
        result = results[symbol][interval]
        print(f"\n🎯 {symbol} ({interval}):")
        print(f"   📈 Buy Signals: {result['buy_signals']}")
        print(f"   📉 Sell Signals: {result['sell_signals']}")
        print(f"   🔢 Total Trades: {result['total_trades']}")
        print(f"   💰 Expected Return: {result['expected_return']}%")
        print(f"   ✅ Win Rate: {result['win_rate']}%")
        print(f"   📊 Profit Factor: {result['profit_factor']}")
        print(f"   📋 Avg Trade Return: {result['avg_trade_return']}%")

# Summary
total_trades_all = sum(results[symbol][interval]['total_trades'] for symbol in symbols for interval in intervals)
avg_return = np.mean([results[symbol][interval]['expected_return'] for symbol in symbols for interval in intervals])

print(f"\n📊 SUMMARY:")
print(f"   Total Trades Across All Symbols: {total_trades_all}")
print(f"   Average Return: {avg_return:.2f}%")
