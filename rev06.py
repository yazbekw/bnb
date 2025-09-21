import os
import time
import threading
import logging
import warnings
import requests
import asyncio
import math
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import aiohttp
import schedule
import pandas as pd
import numpy as np
import pytz

from binance.client import Client
from binance.enums import SIDE_SELL, ORDER_TYPE_STOP_LOSS, TIME_IN_FORCE_GTC

warnings.filterwarnings('ignore')

# -------------------------
# إعدادات الزمن وبيئة التشغيل
# -------------------------
damascus_tz = pytz.timezone('Asia/Damascus')
os.environ['TZ'] = 'Asia/Damascus'
if hasattr(time, 'tzset'):
    time.tzset()

load_dotenv()

# -------------------------
# logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('momentum_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# -------------------------
# Flask health server
# -------------------------
app = Flask(__name__)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per day", "50 per hour"])

@app.route('/')
def health_check():
    return {'status': 'healthy', 'service': 'momentum-hunter-bot', 'timestamp': datetime.now(damascus_tz).isoformat()}

@app.route('/active_trades')
@limiter.limit("5 per minute")
def active_trades_endpoint():
    try:
        bot = MomentumHunterBot(dry_run=True)
        return jsonify(list(bot.active_trades.values()))
    except Exception as e:
        return {'error': str(e)}

def run_flask_app():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

# -------------------------
# Telegram Notifier
# -------------------------
class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.last_notifications = {}
        self.message_queue = []
        self.queue_lock = threading.Lock()
        self.process_thread = threading.Thread(target=self._process_message_queue, daemon=True)
        self.process_thread.start()

    def _process_message_queue(self):
        while True:
            try:
                with self.queue_lock:
                    if not self.message_queue:
                        time.sleep(0.1)
                        continue
                    message_data = self.message_queue.pop(0)
                self._send_message_immediate(message_data['message'], message_data['message_type'])
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"خطأ في معالجة طابور الرسائل: {e}")
                time.sleep(1)

    def _send_message_immediate(self, message, message_type='info'):
        try:
            current_time = time.time()
            if message_type in self.last_notifications and (current_time - self.last_notifications[message_type] < 600):
                return True
            self.last_notifications[message_type] = current_time
            url = f"{self.base_url}/sendMessage"
            payload = {'chat_id': self.chat_id, 'text': message, 'parse_mode': 'HTML', 'disable_notification': False}
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code != 200:
                logger.error(f"فشل إرسال رسالة Telegram: {response.text}")
                return False
            logger.info(f"✅ تم إرسال إشعار Telegram: {message_type}")
            return True
        except Exception as e:
            logger.error(f"خطأ في إرسال رسالة Telegram: {e}")
            return False

    def send_message(self, message, message_type='info', detailed=False):
        with self.queue_lock:
            if detailed:
                message = f"🕒 {datetime.now(damascus_tz).strftime('%H:%M:%S')} - {message}"
            self.message_queue.append({'message': message, 'message_type': message_type})
        return True

# -------------------------
# Request Manager
# -------------------------
class RequestManager:
    def __init__(self):
        self.request_count = 0
        self.last_request_time = time.time()
        self.max_requests_per_minute = 500
        self.request_lock = threading.Lock()

    def safe_request(self, func, *args, **kwargs):
        with self.request_lock:
            current_time = time.time()
            elapsed = current_time - self.last_request_time
            if elapsed < 0.2:
                time.sleep(0.2 - elapsed)
            if current_time - self.last_request_time >= 60:
                self.request_count = 0
                self.last_request_time = current_time
            if self.request_count >= self.max_requests_per_minute:
                sleep_time = 60 - (current_time - self.last_request_time)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                self.request_count = 0
                self.last_request_time = time.time()
            self.request_count += 1
            return func(*args, **kwargs)

# -------------------------
# The Bot (main)
# -------------------------
class MomentumHunterBot:
    TRADING_SETTINGS = {
        'min_daily_volume': 1000000,
        'min_trade_size': 10,
        'max_trade_size': 50,
        'max_position_size': 0.35,
        'momentum_score_threshold': 70,
        'min_profit_threshold': 0.002,
        'first_profit_target': 0.95,
        'first_profit_percentage': 0.5,
        'min_required_profit': 0.01,
        'breakeven_sl_percent': 0.5,
        'min_remaining_profit': 0.2,
        'risk_per_trade_usdt': 20.0,
        'base_risk_pct': 0.004,
        'atr_multiplier_sl': 1.5,
        'risk_reward_ratio': 2.0,
        'min_volume_ratio': 1.8,
        'min_price_change_5m': 2.0,
        'max_active_trades': 3,
        'rsi_overbought': 75,
        'rsi_oversold': 35,
        'data_interval': '15m',
        'rescan_interval_minutes': 30,
        'request_delay_ms': 200,
        'trade_timeout_hours': 6,
        'min_asset_value_usdt': 10,
        'slippage_margin': 1.005
    }

    WEIGHTS = {
        'trend': 17,
        'macd': 14,
        'price_change': 10,
        'volume': 14,
        'rsi': 13,
        'atr': 12,
        'bollinger': 10,
        'vwap': 10
    }

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.api_key = os.environ.get('BINANCE_API_KEY')
        self.api_secret = os.environ.get('BINANCE_API_SECRET')
        self.telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')

        if not all([self.api_key, self.api_secret]):
            raise ValueError("مفاتيح Binance مطلوبة")

        self.client = Client(self.api_key, self.api_secret)  # Testnet مفعل كافتراضي
        self.request_manager = RequestManager()
        self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id) if self.telegram_token and self.telegram_chat_id else None
        self.active_trades = {}
        self.symbols = self.get_all_trading_symbols()
        self.stable_coins = ['USDT', 'BUSD', 'USDC']
        self.last_scan_time = datetime.now(damascus_tz)
        self.price_cache = {}
        self.historical_data_cache = {}
        self.performance_metrics = {'total_trades': 0, 'total_pnl': 0.0, 'win_rate': 0.0}

        self.load_existing_trades()
        self.adjust_dynamic_parameters()
        logger.info(f"✅ تم تحميل {len(self.active_trades)} صفقة مفتوحة")
        
        if self.notifier:
            self.notifier.send_message(f"🚀 <b>بدء تشغيل البوت</b>\nتم تحميل {len(self.active_trades)} صفقة مفتوحة\nالأداء الحالي: ربح إجمالي ${self.performance_metrics['total_pnl']:.2f}, نسبة النجاح {self.performance_metrics['win_rate']:.1%}", 'startup', detailed=True)

    def adjust_dynamic_parameters(self):
        """ضبط الأوزان والمعايير بناءً على التقلب لكل رمز"""
        volatilities = {}
        for symbol in self.symbols:
            volatilities[symbol] = self.calculate_market_volatility(symbol)
        avg_volatility = np.mean(list(volatilities.values())) if volatilities else 0.05

        self.WEIGHTS = self.adjust_weights(avg_volatility)
        self.TRADING_SETTINGS = self.adjust_settings(avg_volatility)
        logger.info(f"⚙️ تم تعديل الأوزان والمعايير بناءً على التقلب المتوسط: {avg_volatility:.4f}")

    def calculate_market_volatility(self, symbol):
        """حساب مؤشر التقلب باستخدام ATR اليومي المتوسط"""
        df = self.get_historical_data(symbol, '1d', 30)
        if df is None or len(df) < 30:
            return 1.0
        indicators = self.calculate_technical_indicators(df)
        avg_atr = indicators['atr'].mean()
        current_price = self.get_current_price(symbol) or 1.0
        volatility_index = avg_atr / current_price
        return min(max(volatility_index, 0.01), 0.1)

    def adjust_weights(self, volatility):
        """ضبط الأوزان ديناميكيًا بناءً على التقلب"""
        base_weights = self.WEIGHTS.copy()
        if volatility > 0.05:
            base_weights['atr'] *= 1.5
            base_weights['bollinger'] *= 1.2
            base_weights['price_change'] *= 0.8
        else:
            base_weights['trend'] *= 1.2
            base_weights['macd'] *= 1.1
        total_weight = sum(base_weights.values())
        for key in base_weights:
            base_weights[key] = (base_weights[key] / total_weight) * 100
        return base_weights

    def adjust_settings(self, volatility):
        """ضبط المعايير ديناميكيًا بناءً على التقلب"""
        settings = self.TRADING_SETTINGS.copy()
        settings['momentum_score_threshold'] = max(50, 70 - (volatility * 1000))
        settings['min_price_change_5m'] = max(1.0, 2.0 - (volatility * 50))
        return settings

    # -------------------------
    # Utilities: symbols, safe requests, tickers
    # -------------------------
    def get_all_trading_symbols(self):
        try:
            selected_symbols = [
                "ETHUSDT", "SOLUSDT", "XRPUSDT",
                "DOGEUSDT", "ADAUSDT", "LTCUSDT"
            ]
            logger.info(f"✅ تم تحديد {len(selected_symbols)} رموز للتداول: {selected_symbols}")
            return selected_symbols
        except Exception as e:
            logger.error(f"خطأ في جلب الرموز: {e}")
            return ["SOLUSDT", "ETHUSDT"]

    def safe_binance_request(self, func, *args, **kwargs):
        try:
            result = self.request_manager.safe_request(func, *args, **kwargs)
            return result
        except Exception as e:
            logger.error(f"خطأ في طلب Binance: {e}")
            if self.notifier:
                self.notifier.send_message(f"❌ <b>خطأ في طلب Binance</b>\n{e}", 'error', detailed=True)
            return None

    async def fetch_ticker_async(self, symbol, session):
        try:
            url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
                logger.error(f"فشل جلب {symbol}: {response.status}")
                return None
        except Exception as e:
            logger.error(f"خطأ في جلب تيكر {symbol}: {e}")
            return None

    async def get_multiple_tickers_async(self, symbols):
        async with aiohttp.ClientSession() as session:
            tasks = [self.fetch_ticker_async(symbol, session) for symbol in symbols]
            return await asyncio.gather(*tasks, return_exceptions=True)

    def get_multiple_tickers(self, symbols):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = loop.run_until_complete(self.get_multiple_tickers_async(symbols))
            return [r for r in results if r and not isinstance(r, Exception)]
        except Exception as e:
            logger.error(f"خطأ في جلب تيكرز متعددة: {e}")
            return []

    def get_current_price(self, symbol):
        try:
            if symbol in self.price_cache:
                price, timestamp = self.price_cache[symbol]
                if time.time() - timestamp < 300:
                    return price
            ticker = self.safe_binance_request(self.client.get_symbol_ticker, symbol=symbol)
            if not ticker:
                return None
            price = float(ticker['price'])
            self.price_cache[symbol] = (price, time.time())
            return price
        except Exception as e:
            logger.error(f"خطأ في جلب سعر {symbol}: {e}")
            return None

    def get_historical_data(self, symbol, interval='15m', limit=100):
        try:
            cache_key = (symbol, interval, limit)
            if cache_key in self.historical_data_cache:
                data, timestamp = self.historical_data_cache[cache_key]
                if time.time() - timestamp < 300:
                    return data
            klines = self.safe_binance_request(self.client.get_klines, symbol=symbol, interval=interval, limit=limit)
            if not klines:
                return None
            df = pd.DataFrame(klines, columns=[
                'open_time', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignored'
            ])
            df['open'] = df['open'].astype(float)
            df['high'] = df['high'].astype(float)
            df['low'] = df['low'].astype(float)
            df['close'] = df['close'].astype(float)
            df['volume'] = df['volume'].astype(float)
            df['open_time'] = pd.to_datetime(df['open_time'], unit='ms').dt.tz_localize('UTC').dt.tz_convert(damascus_tz)
            self.historical_data_cache[cache_key] = (df, time.time())
            return df
        except Exception as e:
            logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
            return None

    # -------------------------
    # Improved indicators calculation
    # -------------------------
    def calculate_technical_indicators(self, df):
        try:
            data = df.copy().reset_index(drop=True)
            if len(data) < 21:
                return data

            data['ema8'] = data['close'].ewm(span=8, adjust=False).mean()
            data['ema21'] = data['close'].ewm(span=21, adjust=False).mean()

            delta = data['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(window=14).mean()
            avg_loss = loss.rolling(window=14).mean()
            rs = avg_gain / (avg_loss + 1e-12)
            data['rsi'] = 100 - (100 / (1 + rs))

            ema12 = data['close'].ewm(span=12, adjust=False).mean()
            ema26 = data['close'].ewm(span=26, adjust=False).mean()
            data['macd'] = ema12 - ema26
            data['macd_signal'] = data['macd'].ewm(span=9, adjust=False).mean()
            data['macd_hist'] = data['macd'] - data['macd_signal']

            high_low = data['high'] - data['low']
            high_close_prev = (data['high'] - data['close'].shift()).abs()
            low_close_prev = (data['low'] - data['close'].shift()).abs()
            tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
            data['atr'] = tr.rolling(window=14).mean()

            sma20 = data['close'].rolling(window=20).mean()
            std20 = data['close'].rolling(window=20).std()
            data['bb_middle'] = sma20
            data['bb_upper'] = sma20 + 2 * std20
            data['bb_lower'] = sma20 - 2 * std20

            typical_price = (data['high'] + data['low'] + data['close']) / 3
            cum_vol = data['volume'].cumsum()
            cum_vtp = (typical_price * data['volume']).cumsum()
            data['vwap'] = cum_vtp / (cum_vol.replace(0, np.nan))

            data['volume_ma'] = data['volume'].rolling(window=20).mean()
            data['volume_ratio'] = data['volume'] / (data['volume_ma'] + 1e-12)

            return data
        except Exception as e:
            logger.error(f"خطأ في حساب المؤشرات: {e}")
            return df

    # -------------------------
    # Improved scoring function
    # -------------------------
    def calculate_momentum_score(self, symbol):
        try:
            df = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'], limit=100)
            if df is None or len(df) < 30:
                return 0, {}

            indicators = self.calculate_technical_indicators(df)
            latest = indicators.iloc[-1]
            prev = indicators.iloc[-2] if len(indicators) >= 2 else latest

            score = 0
            details = {}

            if latest['ema8'] > latest['ema21']:
                score += self.WEIGHTS['trend']
                details['trend'] = 'bullish'
            else:
                details['trend'] = 'bearish'

            macd_cross = False
            window = indicators['macd'][-4:]
            sigwin = indicators['macd_signal'][-4:]
            for i in range(1, len(window)):
                if window.iloc[i-1] <= sigwin.iloc[i-1] and window.iloc[i] > sigwin.iloc[i]:
                    macd_cross = True
                    break
            if macd_cross:
                score += self.WEIGHTS['macd']
                details['macd'] = 'bullish_cross'
            else:
                details['macd'] = 'no_cross'

            price_change_5 = ((latest['close'] - indicators['close'].iloc[-6]) / indicators['close'].iloc[-6]) * 100 if len(indicators) >= 6 else 0.0
            details['price_change_5'] = round(price_change_5, 3)
            if price_change_5 >= self.TRADING_SETTINGS['min_price_change_5m']:
                score += self.WEIGHTS['price_change']

            vol_ratio = latest['volume_ratio'] if not np.isnan(latest['volume_ratio']) else 1.0
            details['volume_ratio'] = round(vol_ratio, 2)
            if vol_ratio >= self.TRADING_SETTINGS['min_volume_ratio']:
                score += self.WEIGHTS['volume']

            rsi_val = latest['rsi'] if not np.isnan(latest['rsi']) else 50
            details['rsi'] = round(rsi_val, 2)
            if 40 < rsi_val < 70:
                score += self.WEIGHTS['rsi']

            atr = latest['atr'] if not np.isnan(latest['atr']) else (latest['close'] * 0.02)
            details['atr'] = round(atr, 6)
            if latest['close'] > (latest['ema21'] + self.TRADING_SETTINGS['atr_multiplier_sl'] * atr):
                score += self.WEIGHTS['atr']

            if not np.isnan(latest.get('bb_upper', np.nan)) and latest['close'] > latest['bb_upper']:
                score += self.WEIGHTS['bollinger']
                details['bollinger'] = 'upper_break'
            else:
                details['bollinger'] = 'no_break'

            if not np.isnan(latest.get('vwap', np.nan)) and latest['close'] > latest['vwap']:
                score += self.WEIGHTS['vwap']
                details['vwap'] = 'above'
            else:
                details['vwap'] = 'below'

            details['current_price'] = latest['close']
            details['ema8'] = latest['ema8']
            details['ema21'] = latest['ema21']
            details['macd_hist'] = latest['macd_hist']

            return min(int(score), 100), details
        except Exception as e:
            logger.error(f"خطأ في حساب زخم {symbol}: {e}")
            return 0, {}

    # -------------------------
    # Precision helper
    # -------------------------
    def get_symbol_precision(self, symbol):
        try:
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            if not symbol_info:
                return {'quantity_precision': 6, 'price_precision': 2, 'step_size': 0.001}
            lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            step_size = float(lot_size['stepSize']) if lot_size else 0.001
            qty_precision = int(round(-np.log10(step_size))) if step_size < 1 else 0
            return {'quantity_precision': qty_precision, 'step_size': step_size}
        except Exception as e:
            logger.error(f"خطأ في جلب دقة {symbol}: {e}")
            return {'quantity_precision': 6, 'step_size': 0.001}

    # -------------------------
    # Position sizing using ATR risk
    # -------------------------
    def calculate_position_size(self, symbol, atr, account_usdt):
        try:
            risk_usdt = min(self.TRADING_SETTINGS['risk_per_trade_usdt'], account_usdt * self.TRADING_SETTINGS['base_risk_pct'])
            sl_distance_per_unit = atr * self.TRADING_SETTINGS['atr_multiplier_sl']
            if sl_distance_per_unit <= 0:
                return 0
            usd_per_unit = self.get_current_price(symbol)
            if usd_per_unit is None or usd_per_unit == 0:
                return 0
            qty = risk_usdt / sl_distance_per_unit
            max_trade_size = min(self.TRADING_SETTINGS['max_trade_size'], account_usdt * self.TRADING_SETTINGS['base_risk_pct'])
            trade_size_usdt = qty * usd_per_unit
            if trade_size_usdt > max_trade_size:
                qty = max_trade_size / usd_per_unit
            precision = self.get_symbol_precision(symbol)
            step = precision['step_size']
            if step > 0:
                qty = math.floor(qty / step) * step
            qty = round(qty, precision['quantity_precision'])
            return max(qty, 0)
        except Exception as e:
            logger.error(f"خطأ في حساب حجم الصفقة: {e}")
            return 0

    # -------------------------
    # Execute trade
    # -------------------------
    def execute_trade(self, symbol, opportunity):
        try:
            current_price = self.get_current_price(symbol)
            if current_price is None:
                return False

            if len(self.active_trades) >= self.TRADING_SETTINGS['max_active_trades']:
                logger.info(f"⏸️ تخطي {symbol} - الحد الأقصى للصفقات النشطة")
                return False

            balances = self.safe_binance_request(self.client.get_account)
            usdt_balance = float(next((b['free'] for b in balances['balances'] if b['asset'] == 'USDT'), 0))
            if usdt_balance < self.TRADING_SETTINGS['min_trade_size']:
                logger.warning(f"💰 رصيد USDT غير كافي: {usdt_balance:.2f}")
                return False

            atr = opportunity['details'].get('atr', current_price * 0.02)
            quantity = self.calculate_position_size(symbol, atr, usdt_balance)
            if quantity <= 0:
                logger.warning(f"❌ كمية محسوبة غير صالحة لـ {symbol}: {quantity}")
                return False

            sl_distance = atr * self.TRADING_SETTINGS['atr_multiplier_sl']
            stop_loss = current_price - (sl_distance * self.TRADING_SETTINGS['slippage_margin'])
            take_profit = current_price + (self.TRADING_SETTINGS['risk_reward_ratio'] * sl_distance * self.TRADING_SETTINGS['slippage_margin'])

            if not self.dry_run:
                order = self.safe_binance_request(self.client.order_market_buy, symbol=symbol, quantity=quantity)
                if order and order.get('status') in ['FILLED', 'FILLED_PARTIALLY', 'NEW']:
                    fills = order.get('fills', [])
                    avg_price = float(fills[0]['price']) if fills else current_price
                    trade_data = {
                        'symbol': symbol,
                        'entry_price': avg_price,
                        'quantity': quantity,
                        'trade_size': quantity * avg_price,
                        'stop_loss': stop_loss,
                        'take_profit': take_profit,
                        'timestamp': datetime.now(damascus_tz),
                        'status': 'open',
                        'order_id': order.get('orderId', 'market_order'),
                        'first_profit_taken': False,
                        'stop_order_id': None
                    }
                    stop_order = self.safe_binance_request(
                        self.client.create_order,
                        symbol=symbol,
                        side=SIDE_SELL,
                        type=ORDER_TYPE_STOP_LOSS,
                        quantity=quantity,
                        stopPrice=round(stop_loss, self.get_symbol_precision(symbol)['price_precision']),
                        timeInForce=TIME_IN_FORCE_GTC
                    )
                    if stop_order:
                        trade_data['stop_order_id'] = stop_order['orderId']
                    else:
                        logger.error(f"❌ فشل إنشاء أمر وقف الخسارة لـ {symbol}")
                    self.active_trades[symbol] = trade_data
                    logger.info(f"✅ صفقة جديدة في {symbol} بسعر {avg_price:.4f}, qty={quantity:.6f}, SL={stop_loss:.4f}, TP={take_profit:.4f}")
                    if self.notifier:
                        self.notifier.send_message(
                            f"🚀 <b>صفقة جديدة</b>\nالعملة: {symbol}\nسعر الدخول: ${avg_price:.4f}\nالكمية: {quantity:.6f}\nوقف الخسارة: ${stop_loss:.4f}\nأخذ الربح: ${take_profit:.4f}\nرصيد USDT: ${usdt_balance:.2f}",
                            f'open_{symbol}', detailed=True
                        )
                    return True
                else:
                    logger.error(f"❌ أمر {symbol} مرفوض أو ملغى: {order}")
                    if self.notifier:
                        self.notifier.send_message(f"❌ <b>أمر مرفوض</b>\n{symbol}: {order}", 'error', detailed=True)
                    return False
            else:
                logger.info(f"🧪 محاكاة فتح صفقة لـ {symbol}: qty={quantity}, price={current_price:.4f}, SL={stop_loss:.4f}, TP={take_profit:.4f}")
                self.active_trades[symbol] = {
                    'symbol': symbol,
                    'entry_price': current_price,
                    'quantity': quantity,
                    'trade_size': quantity * current_price,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'timestamp': datetime.now(damascus_tz),
                    'status': 'open',
                    'order_id': 'simulated',
                    'first_profit_taken': False,
                    'stop_order_id': None
                }
                return True
        except Exception as e:
            logger.error(f"❌ خطأ في تنفيذ صفقة {symbol}: {e}")
            return False

    # -------------------------
    # Manage active trades
    # -------------------------
    def manage_active_trades(self):
        if not self.active_trades:
            return
        symbols = list(self.active_trades.keys())
        tickers = self.get_multiple_tickers(symbols)
        prices = {ticker['symbol']: float(ticker['lastPrice']) for ticker in tickers if ticker}

        for symbol, trade in list(self.active_trades.items()):
            try:
                current_price = prices.get(symbol) or self.get_current_price(symbol)
                if current_price is None:
                    continue

                pnl_percent = ((current_price - trade['entry_price']) / trade['entry_price']) * 100
                trade_duration = (datetime.now(damascus_tz) - trade['timestamp']).total_seconds() / 3600

                if pnl_percent >= self.TRADING_SETTINGS['first_profit_target'] and not trade['first_profit_taken']:
                    self.take_partial_profit(symbol, self.TRADING_SETTINGS['first_profit_percentage'], 'first_profit')
                    trade['first_profit_taken'] = True

                if trade['first_profit_taken'] and pnl_percent >= self.TRADING_SETTINGS['breakeven_sl_percent']:
                    new_sl = trade['entry_price'] * 1.002
                    if new_sl > trade['stop_loss']:
                        trade['stop_loss'] = new_sl
                        if not self.dry_run and trade['stop_order_id']:
                            self.safe_binance_request(
                                self.client.cancel_order,
                                symbol=symbol,
                                orderId=trade['stop_order_id']
                            )
                            stop_order = self.safe_binance_request(
                                self.client.create_order,
                                symbol=symbol,
                                side=SIDE_SELL,
                                type=ORDER_TYPE_STOP_LOSS,
                                quantity=trade['quantity'],
                                stopPrice=round(new_sl, self.get_symbol_precision(symbol)['price_precision']),
                                timeInForce=TIME_IN_FORCE_GTC
                            )
                            if stop_order:
                                trade['stop_order_id'] = stop_order['orderId']
                        logger.info(f"📈 تحديث وقف الخسارة لـ {symbol} إلى ${new_sl:.4f}")
                        if self.notifier:
                            self.notifier.send_message(
                                f"📈 <b>تحديث وقف الخسارة</b>\nالعملة: {symbol}\nالقيمة الجديدة: ${new_sl:.4f}\nالسعر الحالي: ${current_price:.4f}",
                                f'sl_update_{symbol}', detailed=True
                            )

                data_5m = self.get_historical_data(symbol, '5m', 30)
                if data_5m is not None and len(data_5m) >= 15:
                    indicators_5m = self.calculate_technical_indicators(data_5m)
                    rsi = indicators_5m['rsi'].iloc[-1]
                    if rsi > self.TRADING_SETTINGS['rsi_overbought']:
                        self.close_trade(symbol, current_price, 'overbought')
                        continue

                data_short = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'], 20)
                if data_short is not None and len(data_short) >= 10:
                    ind = self.calculate_technical_indicators(data_short)
                    ema8 = ind['ema8'].iloc[-3:]
                    ema21 = ind['ema21'].iloc[-3:]
                    if all(ema8 < ema21) and (current_price < trade['entry_price'] * 0.99):
                        self.close_trade(symbol, current_price, 'trend_reversal')
                        continue

                if trade['first_profit_taken'] and pnl_percent < self.TRADING_SETTINGS['min_remaining_profit']:
                    self.close_trade(symbol, current_price, 'low_profit')
                    continue

                if trade_duration > self.TRADING_SETTINGS['trade_timeout_hours']:
                    self.close_trade(symbol, current_price, 'timeout')
                    continue

            except Exception as e:
                logger.error(f"خطأ في إدارة صفقة {symbol}: {e}")

    # -------------------------
    # Partial profit taking
    # -------------------------
    def take_partial_profit(self, symbol, percentage, reason='partial_profit'):
        try:
            trade = self.active_trades[symbol]
            current_price = self.get_current_price(symbol)
            if current_price is None:
                return False

            quantity_to_sell = trade['quantity'] * percentage
            precision = self.get_symbol_precision(symbol)
            step = precision['step_size']
            if step > 0:
                quantity_to_sell = math.floor(quantity_to_sell / step) * step
            quantity_to_sell = round(quantity_to_sell, precision['quantity_precision'])

            if quantity_to_sell <= 0:
                logger.warning(f"❌ كمية لأخذ الربح غير صالحة لـ {symbol}")
                return False

            gross_profit = (current_price - trade['entry_price']) * quantity_to_sell
            fees = abs(gross_profit) * 0.001
            net_profit = gross_profit - fees
            net_profit_percent = (net_profit / (trade['entry_price'] * quantity_to_sell)) * 100 if trade['entry_price'] * quantity_to_sell > 0 else 0

            if net_profit_percent < 0.65:
                logger.info(f"🔄 تأجيل أخذ الربح لـ {symbol} - الربح: {net_profit_percent:.2f}% < 0.65%")
                return False

            if not self.dry_run:
                order = self.safe_binance_request(self.client.order_market_sell, symbol=symbol, quantity=quantity_to_sell)
                if order and order.get('status') in ['FILLED', 'NEW']:
                    avg_exit_price = float(order.get('fills', [{}])[0].get('price', current_price))
                    actual_net_profit = (avg_exit_price - trade['entry_price']) * quantity_to_sell - fees
                    trade['quantity'] *= (1 - percentage)
                    trade['trade_size'] = trade['quantity'] * trade['entry_price']
                    if 'partial_profits' not in trade:
                        trade['partial_profits'] = []
                    trade['partial_profits'].append({
                        'percentage': percentage,
                        'profit_amount': actual_net_profit,
                        'timestamp': datetime.now(damascus_tz),
                        'reason': reason
                    })
                    logger.info(f"✅ أخذ ربح جزئي لـ {symbol}: {percentage*100}%")
                    if self.notifier:
                        self.notifier.send_message(
                            f"✅ <b>أخذ ربح جزئي</b>\nالعملة: {symbol}\nالنسبة: {percentage*100}%\nالربح الصافي: ${actual_net_profit:.2f}\nالكمية المتبقية: {trade['quantity']:.6f}\nالسعر الحالي: ${current_price:.4f}",
                            f'partial_profit_{symbol}', detailed=True
                        )
                    return True
                else:
                    logger.error(f"❌ فشل أخذ الربح الجزئي لـ {symbol}: {order}")
                    return False
            else:
                logger.info(f"🧪 محاكاة أخذ ربح جزئي لـ {symbol} - quantity={quantity_to_sell}")
                trade['quantity'] *= (1 - percentage)
                trade['trade_size'] = trade['quantity'] * trade['entry_price']
                if 'partial_profits' not in trade:
                    trade['partial_profits'] = []
                trade['partial_profits'].append({
                    'percentage': percentage,
                    'profit_amount': 0.0,
                    'timestamp': datetime.now(damascus_tz),
                    'reason': reason
                })
                return True
        except Exception as e:
            logger.error(f"❌ خطأ في أخذ الربح الجزئي لـ {symbol}: {e}")
            return False

    # -------------------------
    # Close trade
    # -------------------------
    def close_trade(self, symbol, exit_price, reason):
        try:
            trade = self.active_trades[symbol]
            total_fees = trade['trade_size'] * 0.002
            gross_pnl = (exit_price - trade['entry_price']) * trade['quantity']
            net_pnl = gross_pnl - total_fees
            total_partial_profits = sum(p['profit_amount'] for p in trade.get('partial_profits', []))
            total_net_pnl = net_pnl + total_partial_profits
            total_profit_percent = (total_net_pnl / trade['trade_size']) * 100 if trade['trade_size'] > 0 else 0

            if total_net_pnl < trade['trade_size'] * self.TRADING_SETTINGS['min_required_profit'] and reason not in ['stop_loss', 'timeout', 'trend_reversal']:
                logger.info(f"🔄 إلغاء إغلاق {symbol} - الربح الإجمالي {total_net_pnl:.2f} أقل من الحد الأدنى")
                return False

            if not self.dry_run:
                quantity = round(trade['quantity'] - (trade['quantity'] % self.get_symbol_precision(symbol)['step_size']), self.get_symbol_precision(symbol)['quantity_precision'])
                if trade['stop_order_id']:
                    self.safe_binance_request(self.client.cancel_order, symbol=symbol, orderId=trade['stop_order_id'])
                order = self.safe_binance_request(self.client.order_market_sell, symbol=symbol, quantity=quantity)
                if order and order.get('status') in ['FILLED', 'NEW']:
                    trade['exit_price'] = exit_price
                    trade['exit_time'] = datetime.now(damascus_tz)
                    trade['profit_loss'] = total_net_pnl
                    trade['pnl_percent'] = total_profit_percent
                    trade['status'] = 'completed'
                    trade['exit_reason'] = reason
                    self.performance_metrics['total_trades'] += 1
                    self.performance_metrics['total_pnl'] += total_net_pnl
                    win_rate = len([t for t in self.active_trades.values() if t.get('pnl_percent', 0) > 0]) / self.performance_metrics['total_trades'] if self.performance_metrics['total_trades'] > 0 else 0.0
                    self.performance_metrics['win_rate'] = win_rate
                    logger.info(f"🔚 إغلاق {symbol} بـ {reason}: ${total_net_pnl:.2f} ({total_profit_percent:+.2f}%)")
                    if self.notifier:
                        self.notifier.send_message(
                            f"{'✅' if total_net_pnl > 0 else '❌'} <b>إغلاق الصفقة</b>\nالعملة: {symbol}\nالسبب: {self.translate_exit_reason(reason)}\nالربح الإجمالي: ${total_net_pnl:.2f} ({total_profit_percent:+.2f}%)\nالأرباح الجزئية: ${total_partial_profits:.2f}\nالأداء: ربح إجمالي ${self.performance_metrics['total_pnl']:.2f}, نسبة النجاح {self.performance_metrics['win_rate']:.1%}",
                            f'close_{symbol}', detailed=True
                        )
                    del self.active_trades[symbol]
                    return True
                else:
                    logger.error(f"❌ فشل إغلاق الصفقة {symbol}: {order}")
                    return False
            else:
                logger.info(f"🧪 محاكاة إغلاق صفقة {symbol}")
                del self.active_trades[symbol]
                return True
        except Exception as e:
            logger.error(f"❌ خطأ في إغلاق صفقة {symbol}: {e}")
            return False

    # -------------------------
    # Track open trades
    # -------------------------
    def track_open_trades(self):
        if not self.active_trades:
            logger.info("لا صفقات مفتوحة حاليًا")
            return
        symbols = list(self.active_trades.keys())
        tickers = self.get_multiple_tickers(symbols)
        prices = {ticker['symbol']: float(ticker['lastPrice']) for ticker in tickers if ticker}
        for symbol, trade in self.active_trades.items():
            try:
                current_price = prices.get(symbol) or self.get_current_price(symbol)
                if current_price is None:
                    continue
                net_pnl = ((current_price - trade['entry_price']) * trade['quantity']) - (trade['trade_size'] * 0.001)
                pnl_percent = (net_pnl / trade['trade_size']) * 100 if trade['trade_size'] > 0 else 0
                trade_duration = (datetime.now(damascus_tz) - trade['timestamp']).total_seconds() / 60
                logger.info(f"تتبع {symbol}: سعر حالي ${current_price:.4f}, ربح/خسارة ${net_pnl:.2f} ({pnl_percent:.2f}%)")
                if self.notifier:
                    self.notifier.send_message(
                        f"📈 <b>تتبع الصفقة</b>\nالعملة: {symbol}\nالسعر الحالي: ${current_price:.4f}\nالربح/الخسارة: ${net_pnl:.2f} ({pnl_percent:+.2f}%)\nالمدة: {trade_duration:.1f} دقيقة\nوقف الخسارة: ${trade['stop_loss']:.4f}, أخذ الربح: ${trade['take_profit']:.4f}",
                        f'track_{symbol}', detailed=True
                    )
            except Exception as e:
                logger.error(f"خطأ في تتبع صفقة {symbol}: {e}")

    def translate_exit_reason(self, reason):
        reasons = {
            'stop_loss': 'وقف الخسارة',
            'take_profit': 'أخذ الربح',
            'timeout': 'انتهاء الوقت',
            'overbought': 'شراء زائد',
            'trend_reversal': 'انعكاس الاتجاه',
            'low_profit': 'ربح منخفض',
            'first_profit': 'أخذ ربح أولي'
        }
        return reasons.get(reason, reason)

    # -------------------------
    # Main trading cycle
    # -------------------------
    async def find_best_opportunities(self):
        opportunities = []
        symbols_to_analyze = self.symbols[:10]
        tickers = await self.get_multiple_tickers_async(symbols_to_analyze)

        for symbol, ticker in zip(symbols_to_analyze, tickers):
            if not ticker:
                continue
            try:
                daily_volume = float(ticker['volume']) * float(ticker['lastPrice'])
                if daily_volume < self.TRADING_SETTINGS['min_daily_volume']:
                    continue
                score, details = self.calculate_momentum_score(symbol)
                if score >= self.TRADING_SETTINGS['momentum_score_threshold']:
                    opportunities.append({
                        'symbol': symbol,
                        'score': score,
                        'details': details,
                        'daily_volume': daily_volume,
                        'timestamp': datetime.now(damascus_tz)
                    })
            except Exception as e:
                logger.error(f"خطأ أثناء تحليل {symbol}: {e}")

        return sorted(opportunities, key=lambda x: x['score'], reverse=True)

    def run_trading_cycle(self):
        try:
            logger.info("🔄 بدء دورة التداول")
            self.load_existing_trades()
            self.manage_active_trades()
            if len(self.active_trades) < self.TRADING_SETTINGS['max_active_trades']:
                opportunities = asyncio.run(self.find_best_opportunities())
                if opportunities:
                    logger.info(f"🔍 تم العثور على {len(opportunities)} فرصة")
                    for opportunity in opportunities[:2]:
                        if opportunity['symbol'] not in self.active_trades:
                            self.execute_trade(opportunity['symbol'], opportunity)
                            time.sleep(1)
                else:
                    logger.info("🔍 لا فرص مناسبة")
            self.last_scan_time = datetime.now(damascus_tz)
            self.adjust_dynamic_parameters()
        except Exception as e:
            logger.error(f"❌ خطأ في دورة التداول: {e}")
            if self.notifier:
                self.notifier.send_message(f"❌ <b>خطأ في دورة التداول</b>\n{e}", 'error', detailed=True)

    def run_bot(self):
        logger.info("🚀 بدء تشغيل البوت")
        schedule.every(self.TRADING_SETTINGS['rescan_interval_minutes']).minutes.do(self.run_trading_cycle)
        schedule.every(5).minutes.do(self.track_open_trades)
        schedule.every(6).hours.do(self.adjust_dynamic_parameters)
        self.run_trading_cycle()
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logger.error(f"خطأ في الحلقة الرئيسية: {e}")
                time.sleep(60)

    # -------------------------
    # Load existing trades
    # -------------------------
    def load_existing_trades(self):
        try:
            account = self.safe_binance_request(self.client.get_account)
            if not account:
                return
            balances = account['balances']
            
            self.active_trades.clear()
            loaded_count = 0
            symbols_to_fetch = []
            for balance in balances:
                asset = balance['asset']
                free_qty = float(balance['free'])
                if asset not in self.stable_coins and free_qty > 0:
                    symbol = asset + 'USDT'
                    if symbol not in self.symbols:
                        logger.warning(f"⚠️ الرمز {symbol} غير مدعوم - تخطي")
                        continue
                    symbols_to_fetch.append(symbol)
            
            prices = {}
            if symbols_to_fetch:
                tickers = self.get_multiple_tickers(symbols_to_fetch)
                prices = {ticker['symbol']: float(ticker['lastPrice']) for ticker in tickers if ticker}

            for balance in balances:
                asset = balance['asset']
                free_qty = float(balance['free'])
                if asset not in self.stable_coins and free_qty > 0:
                    symbol = asset + 'USDT'
                    current_price = prices.get(symbol)
                    if current_price is None:
                        logger.warning(f"⚠️ تعذر جلب سعر {symbol} - تخطي")
                        continue
                    
                    asset_value_usdt = free_qty * current_price
                    if asset_value_usdt < self.TRADING_SETTINGS['min_asset_value_usdt']:
                        logger.info(f"⚠️ قيمة {symbol} صغيرة جدًا (${asset_value_usdt:.2f}) - تخطي")
                        continue
                    
                    trades = self.safe_binance_request(self.client.get_my_trades, symbol=symbol)
                    if not trades:
                        entry_price = current_price
                    else:
                        buy_trades = [t for t in trades if t.get('isBuyer')]
                        if not buy_trades:
                            entry_price = current_price
                        else:
                            total_qty = sum(float(t['qty']) for t in buy_trades)
                            total_cost = sum(float(t['qty']) * float(t['price']) for t in buy_trades)
                            entry_price = total_cost / total_qty if total_qty > 0 else current_price
                    
                    trade_data = {
                        'symbol': symbol,
                        'entry_price': entry_price,
                        'quantity': free_qty,
                        'trade_size': free_qty * entry_price,
                        'stop_loss': entry_price * 0.98,
                        'take_profit': entry_price * 1.04,
                        'timestamp': datetime.now(damascus_tz),
                        'status': 'open',
                        'order_id': 'from_balance',
                        'first_profit_taken': False,
                        'stop_order_id': None
                    }
                    self.active_trades[symbol] = trade_data
                    loaded_count += 1
                    logger.info(f"✅ تم تحميل الصفقة من الرصيد Binance: {symbol} - كمية: {free_qty:.6f} - سعر دخول: ${entry_price:.4f}")
                    if self.notifier:
                        self.notifier.send_message(
                            f"📥 <b>صفقة مفتوحة محملة من الرصيد</b>\nالعملة: {symbol}\nسعر الدخول: ${entry_price:.4f}\nالكمية: {free_qty:.6f}\nالقيمة: ${asset_value_usdt:.2f}",
                            f'load_{symbol}', detailed=True
                        )
            
            if loaded_count == 0:
                logger.info("⚠️ لا صفقات مفتوحة في الرصيد حاليًا")
                if self.notifier:
                    self.notifier.send_message("⚠️ <b>لا صفقات مفتوحة</b>\nتم فحص الرصيد ولم يتم العثور على أي أصول مملوكة.", 'no_trades', detailed=True)
        except Exception as e:
            logger.error(f"❌ خطأ في تحميل الصفقات من رصيد Binance: {e}")
            if self.notifier:
                self.notifier.send_message(f"❌ <b>خطأ</b>\nفشل تحميل الصفقات من الرصيد: {e}", 'error', detailed=True)

# -------------------------
# main
# -------------------------
def main():
    try:
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        bot = MomentumHunterBot(dry_run=False)  # تفعيل التداول الحقيقي (غيّر إلى False)
        bot.run_bot()
    except Exception as e:
        logger.error(f"❌ خطأ فادح في البوت: {e}")
        if 'bot' in locals() and bot.notifier:
            bot.notifier.send_message(f"❌ <b>إيقاف البوت</b>\nخطأ فادح: {e}", 'fatal_error', detailed=True)

if __name__ == "__main__":
    main()
