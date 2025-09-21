import os
import pandas as pd
import numpy as np
from binance.client import Client
from binance.enums import *
import time
from datetime import datetime, timedelta
import requests
import logging
import warnings
warnings.filterwarnings('ignore')
from dotenv import load_dotenv
import threading
import schedule
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import aiohttp
import asyncio
import pytz

# ضبط توقيت الخادم إلى توقيت دمشق
damascus_tz = pytz.timezone('Asia/Damascus')
os.environ['TZ'] = 'Asia/Damascus'
if hasattr(time, 'tzset'):
    time.tzset()

# تحميل متغيرات البيئة
load_dotenv()

# إنشاء تطبيق Flask للرصد الصحي
app = Flask(__name__)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per day", "50 per hour"])

@app.route('/')
def health_check():
    return {'status': 'healthy', 'service': 'momentum-hunter-bot', 'timestamp': datetime.now(damascus_tz).isoformat()}

@app.route('/active_trades')
@limiter.limit("5 per minute")
def active_trades():
    try:
        bot = MomentumHunterBot()
        return jsonify(list(bot.active_trades.values()))
    except Exception as e:
        return {'error': str(e)}

def run_flask_app():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

# إعداد logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('momentum_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.last_notifications = {}
        self.message_queue = []
        self.sending = False
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
            if message_type in self.last_notifications and (current_time - self.last_notifications[message_type] < 300):
                return True
            self.last_notifications[message_type] = current_time
            url = f"{self.base_url}/sendMessage"
            payload = {'chat_id': self.chat_id, 'text': message, 'parse_mode': 'HTML'}
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code != 200:
                logger.error(f"فشل إرسال رسالة Telegram: {response.text}")
                return False
            logger.info(f"✅ تم إرسال إشعار Telegram: {message_type}")
            return True
        except Exception as e:
            logger.error(f"خطأ في إرسال رسالة Telegram: {e}")
            return False

    def send_message(self, message, message_type='info'):
        with self.queue_lock:
            self.message_queue.append({'message': message, 'message_type': message_type})
        return True

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

class MomentumHunterBot:
    TRADING_SETTINGS = {
        'min_daily_volume': 1000000,
        'min_trade_size': 10,
        'max_trade_size': 50,
        'max_position_size': 0.35,
        'momentum_score_threshold': 45,
        'min_profit_threshold': 0.002,
        'first_profit_target': 0.65,
        'first_profit_percentage': 0.5,
        'min_required_profit': 0.01,
        'breakeven_sl_percent': 0.5,
        'min_remaining_profit': 0.2,
        'risk_per_trade': 2.0,
        'base_risk_pct': 0.004,
        'atr_multiplier_sl': 1.2,
        'risk_reward_ratio': 2.0,
        'min_volume_ratio': 1.8,
        'min_price_change_5m': 2.0,
        'max_active_trades': 3,
        'rsi_overbought': 75,
        'rsi_oversold': 35,
        'data_interval': '5m',
        'rescan_interval_minutes': 15,
        'request_delay_ms': 100,
        'trade_timeout_hours': 2,
        'min_asset_value_usdt': 10,
    }

    WEIGHTS = {
        'trend': 25,
        'crossover': 20,
        'price_change': 15,
        'volume': 15,
        'rsi': 10,
        'macd': 10,
        'bollinger': 5
    }

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.api_key = os.environ.get('BINANCE_API_KEY')
        self.api_secret = os.environ.get('BINANCE_API_SECRET')
        self.telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')

        if not all([self.api_key, self.api_secret]):
            raise ValueError("مفاتيح Binance مطلوبة")

        self.client = Client(self.api_key, self.api_secret)
        self.request_manager = RequestManager()
        self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id) if self.telegram_token and self.telegram_chat_id else None
        self.active_trades = {}
        self.symbols = self.get_all_trading_symbols()
        self.stable_coins = ['USDT', 'BUSD', 'USDC']
        self.last_scan_time = datetime.now(damascus_tz)

        # تحميل الصفقات المفتوحة عند التشغيل
        self.load_existing_trades()
        logger.info(f"✅ تم تحميل {len(self.active_trades)} صفقة مفتوحة")
        
        if self.notifier:
            self.notifier.send_message(f"🚀 <b>بدء تشغيل البوت</b>\nتم تحميل {len(self.active_trades)} صفقة مفتوحة", 'startup')

    def load_existing_trades(self):
        """تحميل الصفقات المفتوحة من رصيد Binance (balances)"""
        try:
            account = self.safe_binance_request(self.client.get_account)
            balances = account['balances']
            
            self.active_trades.clear()  # مسح الصفقات القديمة لإعادة المزامنة
            loaded_count = 0
            for balance in balances:
                asset = balance['asset']
                free_qty = float(balance['free'])
                if asset not in self.stable_coins and free_qty > 0:
                    symbol = asset + 'USDT'
                    if symbol not in self.symbols:
                        logger.warning(f"⚠️ الرمز {symbol} غير مدعوم - تخطي")
                        continue
                    
                    current_price = self.get_current_price(symbol)
                    if current_price is None:
                        logger.warning(f"⚠️ تعذر جلب سعر {symbol} - تخطي")
                        continue
                    
                    asset_value_usdt = free_qty * current_price
                    if asset_value_usdt < self.TRADING_SETTINGS['min_asset_value_usdt']:
                        logger.info(f"⚠️ قيمة {symbol} صغيرة جدًا (${asset_value_usdt:.2f}) - تخطي")
                        continue
                    
                    # جلب تاريخ التداولات للرمز لحساب متوسط سعر الدخول
                    trades = self.safe_binance_request(self.client.get_my_trades, symbol=symbol)
                    if not trades:
                        logger.warning(f"⚠️ لا تداولات سابقة لـ {symbol} - استخدام سعر حالي كافتراضي")
                        entry_price = current_price
                    else:
                        buy_trades = [t for t in trades if t['isBuyer']]
                        if not buy_trades:
                            logger.warning(f"⚠️ لا تداولات شراء لـ {symbol} - تخطي")
                            continue
                        
                        # حساب متوسط سعر الدخول المرجح
                        total_qty = sum(float(t['qty']) for t in buy_trades)
                        total_cost = sum(float(t['qty']) * float(t['price']) for t in buy_trades)
                        entry_price = total_cost / total_qty if total_qty > 0 else current_price
                    
                    # إنشاء بيانات الصفقة
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
                        'first_profit_taken': False
                    }
                    self.active_trades[symbol] = trade_data
                    loaded_count += 1
                    logger.info(f"✅ تم تحميل الصفقة من رصيد Binance: {symbol} - كمية: {free_qty:.6f} - سعر دخول: ${entry_price:.4f}")
                    if self.notifier:
                        self.notifier.send_message(
                            f"📥 <b>صفقة مفتوحة محملة من الرصيد</b>\nالعملة: {symbol}\nسعر الدخول: ${entry_price:.4f}\nالكمية: {free_qty:.6f}\nالقيمة: ${asset_value_usdt:.2f}",
                            f'load_{symbol}'
                        )
            
            if loaded_count == 0:
                logger.info("⚠️ لا صفقات مفتوحة في الرصيد حاليًا")
                if self.notifier:
                    self.notifier.send_message("⚠️ <b>لا صفقات مفتوحة</b>\nتم فحص الرصيد ولم يتم العثور على أي أصول مملوكة.", 'no_trades')
        except Exception as e:
            logger.error(f"❌ خطأ في تحميل الصفقات من رصيد Binance: {e}")
            if self.notifier:
                self.notifier.send_message(f"❌ <b>خطأ</b>\nفشل تحميل الصفقات من الرصيد: {e}", 'error')

    def get_all_trading_symbols(self):
        try:
            # قائمة ثابتة تحتوي على العملات العشرة المحددة
            selected_symbols = [
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "DOGEUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT", "LINKUSDT"
            ]
            logger.info(f"✅ تم تحديد {len(selected_symbols)} رموز للتداول: {selected_symbols}")
            return selected_symbols
        except Exception as e:
            logger.error(f"خطأ في جلب الرموز: {e}")
            return ["BTCUSDT", "ETHUSDT"]  # قائمة احتياطية صغيرة في حالة الخطأ

    def safe_binance_request(self, func, *args, **kwargs):
        try:
            result = self.request_manager.safe_request(func, *args, **kwargs)
            return result
        except Exception as e:
            logger.error(f"خطأ في طلب Binance: {e}")
            if self.notifier:
                self.notifier.send_message(f"❌ <b>خطأ في طلب Binance</b>\n{e}", 'error')
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
            return loop.run_until_complete(self.get_multiple_tickers_async(symbols))
        except Exception as e:
            logger.error(f"خطأ في جلب تيكرز متعددة: {e}")
            return []

    def get_current_price(self, symbol):
        try:
            ticker = self.safe_binance_request(self.client.get_symbol_ticker, symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            logger.error(f"خطأ في جلب سعر {symbol}: {e}")
            return None

    def get_historical_data(self, symbol, interval='5m', limit=100):
        try:
            klines = self.safe_binance_request(self.client.get_klines, symbol=symbol, interval=interval, limit=limit)
            data = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignored'
            ])
            data['close'] = data['close'].astype(float)
            data['volume'] = data['volume'].astype(float)
            data['high'] = data['high'].astype(float)
            data['low'] = data['low'].astype(float)
            return data
        except Exception as e:
            logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
            return None

    def calculate_technical_indicators(self, data):
        try:
            df = data.copy()
            if len(df) < 20:
                return df

            df['ema8'] = df['close'].ewm(span=8, adjust=False).mean()
            df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
            delta = df['close'].diff()
            gains = delta.where(delta > 0, 0)
            losses = -delta.where(delta < 0, 0)
            avg_gain = gains.rolling(window=14).mean()
            avg_loss = losses.rolling(window=14).mean()
            rs = avg_gain / (avg_loss + 1e-12)
            df['rsi'] = 100 - (100 / (1 + rs))
            df['volume_ma'] = df['volume'].rolling(window=20).mean()
            df['volume_ratio'] = df['volume'] / df['volume_ma']
            return df
        except Exception as e:
            logger.error(f"خطأ في حساب المؤشرات: {e}")
            return data

    def calculate_momentum_score(self, symbol):
        try:
            data = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'], 100)
            if data is None or len(data) < 20:
                return 0, {}

            data = self.calculate_technical_indicators(data)
            latest = data.iloc[-1]
            score = 0
            details = {}

            if latest['ema8'] > latest['ema21']:
                score += self.WEIGHTS['trend']
                details['trend'] = 'صاعد'

            window = data.iloc[-4:]
            for i in range(1, len(window)):
                if window['ema8'].iloc[i-1] <= window['ema21'].iloc[i-1] and window['ema8'].iloc[i] > window['ema21'].iloc[i]:
                    score += self.WEIGHTS['crossover']
                    details['crossover'] = 'إيجابي'
                    break

            price_change_5 = ((latest['close'] - data.iloc[-5]['close']) / data.iloc[-5]['close']) * 100
            details['price_change_5m'] = round(price_change_5, 2)
            if price_change_5 >= self.TRADING_SETTINGS['min_price_change_5m']:
                score += self.WEIGHTS['price_change']

            volume_ratio = latest['volume_ratio']
            details['volume_ratio'] = round(volume_ratio, 2) if not pd.isna(volume_ratio) else 1
            if volume_ratio >= self.TRADING_SETTINGS['min_volume_ratio']:
                score += self.WEIGHTS['volume']

            details['rsi'] = round(latest['rsi'], 2) if not pd.isna(latest['rsi']) else 50
            if self.TRADING_SETTINGS['rsi_oversold'] <= latest['rsi'] <= self.TRADING_SETTINGS['rsi_overbought']:
                score += self.WEIGHTS['rsi']

            details['current_price'] = latest['close']
            return min(score, 100), details
        except Exception as e:
            logger.error(f"خطأ في حساب زخم {symbol}: {e}")
            return 0, {}

    async def find_best_opportunities(self):
        opportunities = []
        symbols_to_analyze = self.symbols[:50]
        tickers = await self.get_multiple_tickers_async(symbols_to_analyze)

        for symbol, ticker in zip(symbols_to_analyze, tickers):
            if not ticker:
                continue
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
        return sorted(opportunities, key=lambda x: x['score'], reverse=True)

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

            position_size = min(usdt_balance * self.TRADING_SETTINGS['base_risk_pct'], self.TRADING_SETTINGS['max_trade_size'])
            position_size = max(position_size, self.TRADING_SETTINGS['min_trade_size'])
            quantity = position_size / current_price
            precision = self.get_symbol_precision(symbol)
            quantity = round(quantity - (quantity % precision['step_size']), precision['quantity_precision'])

            atr = opportunity['details'].get('atr', current_price * 0.02)
            stop_loss = current_price - (atr * self.TRADING_SETTINGS['atr_multiplier_sl'])
            take_profit = current_price + (self.TRADING_SETTINGS['risk_reward_ratio'] * (current_price - stop_loss))

            if not self.dry_run:
                order = self.safe_binance_request(self.client.order_market_buy, symbol=symbol, quantity=quantity)
                if order and order['status'] == 'FILLED':
                    avg_price = float(order['fills'][0]['price']) if order['fills'] else current_price
                    trade_data = {
                        'symbol': symbol,
                        'entry_price': avg_price,
                        'quantity': quantity,
                        'trade_size': quantity * avg_price,
                        'stop_loss': stop_loss,
                        'take_profit': take_profit,
                        'timestamp': datetime.now(damascus_tz),
                        'status': 'open',
                        'order_id': order['orderId'],
                        'first_profit_taken': False
                    }
                    self.active_trades[symbol] = trade_data
                    logger.info(f"✅ صفقة جديدة في {symbol} بسعر {avg_price:.4f}")
                    if self.notifier:
                        self.notifier.send_message(
                            f"🚀 <b>صفقة جديدة</b>\nالعملة: {symbol}\nسعر الدخول: ${avg_price:.4f}\nالكمية: {quantity:.6f}\nوقف الخسارة: ${stop_loss:.4f}\nأخذ الربح: ${take_profit:.4f}",
                            f'open_{symbol}'
                        )
                    return True
                else:
                    logger.error(f"❌ فشل تنفيذ أمر الشراء لـ {symbol}")
                    return False
            else:
                logger.info(f"🧪 محاكاة صفقة لـ {symbol}: حجم ${position_size:.2f}")
                return True
        except Exception as e:
            logger.error(f"❌ خطأ في تنفيذ صفقة {symbol}: {e}")
            return False

    def manage_active_trades(self):
        for symbol, trade in list(self.active_trades.items()):
            try:
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    continue

                pnl_percent = ((current_price - trade['entry_price']) / trade['entry_price']) * 100
                trade_duration = (datetime.now(damascus_tz) - trade['timestamp']).total_seconds() / 3600

                # أخذ ربح جزئي عند تحقيق 0.65%
                if pnl_percent >= self.TRADING_SETTINGS['first_profit_target'] and not trade['first_profit_taken']:
                    self.take_partial_profit(symbol, self.TRADING_SETTINGS['first_profit_percentage'], 'first_profit')
                    trade['first_profit_taken'] = True

                # تحريك وقف الخسارة بعد أخذ الربح الأول
                if trade['first_profit_taken'] and pnl_percent >= self.TRADING_SETTINGS['breakeven_sl_percent']:
                    new_sl = trade['entry_price'] * 1.002
                    if new_sl > trade['stop_loss']:
                        trade['stop_loss'] = new_sl
                        logger.info(f"📈 تحديث وقف الخسارة لـ {symbol} إلى ${new_sl:.4f}")
                        if self.notifier:
                            self.notifier.send_message(
                                f"📈 <b>تحديث وقف الخسارة</b>\nالعملة: {symbol}\nوقف الخسارة الجديد: ${new_sl:.4f}",
                                f'sl_update_{symbol}'
                            )

                # الخروج عند فقدان الزخم
                data = self.get_historical_data(symbol, '5m', 20)
                if data is not None and len(data) >= 10:
                    delta = data['close'].diff()
                    gain = delta.where(delta > 0, 0).rolling(14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                    rs = gain / (loss + 1e-12)
                    rsi = (100 - (100 / (1 + rs))).iloc[-1]
                    if rsi > self.TRADING_SETTINGS['rsi_overbought']:
                        self.close_trade(symbol, current_price, 'overbought')
                        continue

                # الخروج عند انعكاس الاتجاه
                if data is not None and len(data) >= 5:
                    ema8 = data['close'].ewm(span=8, adjust=False).mean().iloc[-3:]
                    ema21 = data['close'].ewm(span=21, adjust=False).mean().iloc[-3:]
                    if all(ema8 < ema21):
                        self.close_trade(symbol, current_price, 'trend_reversal')
                        continue

                # الخروج عند انخفاض الربح المتبقي
                if trade['first_profit_taken'] and pnl_percent < self.TRADING_SETTINGS['min_remaining_profit']:
                    self.close_trade(symbol, current_price, 'low_profit')
                    continue

                # الخروج بعد مرور الوقت المحدد (2 ساعة)
                if trade_duration > self.TRADING_SETTINGS['trade_timeout_hours']:
                    self.close_trade(symbol, current_price, 'timeout')
                    continue

            except Exception as e:
                logger.error(f"خطأ في إدارة صفقة {symbol}: {e}")

    def take_partial_profit(self, symbol, percentage, reason='partial_profit'):
        try:
            trade = self.active_trades[symbol]
            current_price = self.get_current_price(symbol)
            if current_price is None:
                return False

            quantity_to_sell = trade['quantity'] * percentage
            precision = self.get_symbol_precision(symbol)
            quantity_to_sell = round(quantity_to_sell - (quantity_to_sell % precision['step_size']), precision['quantity_precision'])

            gross_profit = (current_price - trade['entry_price']) * quantity_to_sell
            fees = gross_profit * 0.001
            net_profit = gross_profit - fees
            net_profit_percent = (net_profit / (trade['entry_price'] * quantity_to_sell)) * 100

            if net_profit_percent < 0.65:
                logger.info(f"🔄 تأجيل أخذ الربح لـ {symbol} - الربح: {net_profit_percent:.2f}% < 0.65%")
                return False

            if not self.dry_run:
                order = self.safe_binance_request(self.client.order_market_sell, symbol=symbol, quantity=quantity_to_sell)
                if order and order['status'] == 'FILLED':
                    avg_exit_price = float(order['fills'][0]['price']) if order['fills'] else current_price
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
                            f"✅ <b>أخذ ربح جزئي</b>\nالعملة: {symbol}\nالنسبة: {percentage*100}%\nالربح الصافي: ${actual_net_profit:.2f}\nالكمية المتبقية: {trade['quantity']:.6f}",
                            f'partial_profit_{symbol}'
                        )
                    return True
                else:
                    logger.error(f"❌ فشل أخذ الربح الجزئي لـ {symbol}")
                    return False
            else:
                logger.info(f"🧪 محاكاة أخذ ربح جزئي لـ {symbol}")
                return True
        except Exception as e:
            logger.error(f"❌ خطأ في أخذ الربح الجزئي لـ {symbol}: {e}")
            return False

    def close_trade(self, symbol, exit_price, reason):
        try:
            trade = self.active_trades[symbol]
            total_fees = trade['trade_size'] * 0.002
            gross_pnl = (exit_price - trade['entry_price']) * trade['quantity']
            net_pnl = gross_pnl - total_fees
            total_partial_profits = sum(p['profit_amount'] for p in trade.get('partial_profits', []))
            total_net_pnl = net_pnl + total_partial_profits
            total_profit_percent = (total_net_pnl / trade['trade_size']) * 100

            if total_net_pnl < trade['trade_size'] * self.TRADING_SETTINGS['min_required_profit'] and reason not in ['stop_loss', 'timeout']:
                logger.info(f"🔄 إلغاء إغلاق {symbol} - الربح الإجمالي {total_net_pnl:.2f} أقل من 1%")
                return False

            if not self.dry_run:
                quantity = round(trade['quantity'] - (trade['quantity'] % self.get_symbol_precision(symbol)['step_size']), self.get_symbol_precision(symbol)['quantity_precision'])
                order = self.safe_binance_request(self.client.order_market_sell, symbol=symbol, quantity=quantity)
                if order and order['status'] == 'FILLED':
                    trade['exit_price'] = exit_price
                    trade['exit_time'] = datetime.now(damascus_tz)
                    trade['profit_loss'] = total_net_pnl
                    trade['pnl_percent'] = total_profit_percent
                    trade['status'] = 'completed'
                    trade['exit_reason'] = reason
                    logger.info(f"🔚 إغلاق {symbol} بـ {reason}: ${total_net_pnl:.2f} ({total_profit_percent:+.2f}%)")
                    if self.notifier:
                        self.notifier.send_message(
                            f"{'✅' if total_net_pnl > 0 else '❌'} <b>إغلاق الصفقة</b>\nالعملة: {symbol}\nالسبب: {self.translate_exit_reason(reason)}\nالربح الإجمالي: ${total_net_pnl:.2f} ({total_profit_percent:+.2f}%)\nالأرباح الجزئية: ${total_partial_profits:.2f}",
                            f'close_{symbol}'
                        )
                    del self.active_trades[symbol]
                    return True
                else:
                    logger.error(f"❌ فشل إغلاق الصفقة {symbol}")
                    return False
            else:
                logger.info(f"🧪 محاكاة إغلاق صفقة {symbol}")
                del self.active_trades[symbol]
                return True
        except Exception as e:
            logger.error(f"❌ خطأ في إغلاق صفقة {symbol}: {e}")
            return False

    def track_open_trades(self):
        if not self.active_trades:
            logger.info("لا صفقات مفتوحة حاليًا")
            return
        for symbol, trade in self.active_trades.items():
            try:
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    continue
                net_pnl = ((current_price - trade['entry_price']) * trade['quantity']) - (trade['trade_size'] * 0.001)
                pnl_percent = (net_pnl / trade['trade_size']) * 100
                trade_duration = (datetime.now(damascus_tz) - trade['timestamp']).total_seconds() / 60
                logger.info(f"تتبع {symbol}: سعر حالي ${current_price:.4f}, ربح/خسارة ${net_pnl:.2f} ({pnl_percent:.2f}%)")
                if self.notifier:
                    self.notifier.send_message(
                        f"📈 <b>تتبع الصفقة</b>\nالعملة: {symbol}\nالسعر الحالي: ${current_price:.4f}\nالربح/الخسارة: ${net_pnl:.2f} ({pnl_percent:+.2f}%)\nالمدة: {trade_duration:.1f} دقيقة",
                        f'track_{symbol}'
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

    def run_trading_cycle(self):
        try:
            logger.info("🔄 بدء دورة التداول")
            self.load_existing_trades()  # تحديث الصفقات المفتوحة
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
        except Exception as e:
            logger.error(f"❌ خطأ في دورة التداول: {e}")
            if self.notifier:
                self.notifier.send_message(f"❌ <b>خطأ في دورة التداول</b>\n{e}", 'error')

    def run_bot(self):
        logger.info("🚀 بدء تشغيل البوت")
        schedule.every(self.TRADING_SETTINGS['rescan_interval_minutes']).minutes.do(self.run_trading_cycle)
        schedule.every(1).minute.do(self.track_open_trades)
        self.run_trading_cycle()
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logger.error(f"خطأ في الحلقة الرئيسية: {e}")
                time.sleep(60)

def main():
    try:
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        bot = MomentumHunterBot()
        bot.run_bot()
    except Exception as e:
        logger.error(f"❌ خطأ فادح في البوت: {e}")
        if 'bot' in locals() and bot.notifier:
            bot.notifier.send_message(f"❌ <b>إيقاف البوت</b>\nخطأ فادح: {e}", 'fatal_error')

if __name__ == "__main__":
    main()
