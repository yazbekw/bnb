import os
import time
import threading
import logging
import warnings
import requests
import asyncio
import math
from datetime import datetime
from dotenv import load_dotenv
import aiohttp
import schedule
import pandas as pd
import numpy as np
import pytz

from binance.client import Client
from flask import Flask, jsonify

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
# Logging
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
# Flask Health Server
# -------------------------
app = Flask(__name__)

@app.route('/')
def health_check():
    return {'status': 'healthy', 'service': 'momentum-hunter-bot', 'timestamp': datetime.now(damascus_tz).isoformat()}

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
# The Bot (Main)
# -------------------------
class MomentumHunterBot:
    TRADING_SETTINGS = {
        'symbols': ['BTCUSDT', 'ETHUSDT', 'LTCUSDT', 'XRPUSDT', 'SOLUSDT'],
        'data_interval': '5m',
        'sma_short': 5,
        'sma_long': 15,
        'rsi_period': 14,
        'rsi_oversold': 30,
        'rsi_overbought': 70,
        'stop_loss_percent': -0.05,
        'take_profit_percent': 0.03,
        'risk_per_trade_usdt': 10.0,
        'rescan_interval_minutes': 30,
        'trade_timeout_hours': 6,
        'max_active_trades': 3,
    }

    def __init__(self, dry_run=True):
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
        self.last_scan_time = datetime.now(damascus_tz)
        self.price_cache = {}
        self.historical_data_cache = {}

        self.load_existing_trades()
        logger.info(f"✅ تم تحميل {len(self.active_trades)} صفقة مفتوحة")
        if self.notifier:
            self.notifier.send_message(f"🚀 <b>بدء تشغيل البوت</b>\nتم تحميل {len(self.active_trades)} صفقة مفتوحة", 'startup')

    def send_health_notification(self):
        if self.notifier:
            self.notifier.send_message("✅ <b>تحقق صحة البوت</b>\nالبوت يعمل بشكل طبيعي.", 'health')

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
                    data = await response.json()
                    if data.get('code') or not data.get('symbol'):
                        logger.warning(f"استجابة غير صالحة من API لـ {symbol}: {data}")
                        return None
                    return data
                logger.error(f"فشل جلب {symbol}: {response.status}")
                return None
        except Exception as e:
            logger.error(f"خطأ في جلب تيكر {symbol}: {e}")
            return None

    def get_multiple_tickers(self, symbols):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # استخدام coroutine thread-safe إذا كانت الحلقة تعمل
                async def fetch_tickers():
                    async with aiohttp.ClientSession() as session:
                        tasks = [self.fetch_ticker_async(symbol, session) for symbol in symbols]
                        return await asyncio.gather(*tasks, return_exceptions=True)
                future = asyncio.run_coroutine_threadsafe(fetch_tickers(), loop)
                tickers = future.result(timeout=10)
            else:
                async def fetch_tickers():
                    async with aiohttp.ClientSession() as session:
                        tasks = [self.fetch_ticker_async(symbol, session) for symbol in symbols]
                        return await asyncio.gather(*tasks, return_exceptions=True)
                tickers = asyncio.run(fetch_tickers())
            return [t for t in tickers if t and not isinstance(t, Exception)]
        except Exception as e:
            logger.error(f"خطأ في جلب تيكرز متعددة: {e}")
            return []

    def get_current_price(self, symbol):
        try:
            if symbol in self.price_cache:
                price, timestamp = self.price_cache[symbol]
                if time.time() - timestamp < 300:
                    return price
            tickers = self.get_multiple_tickers([symbol])
            if tickers and len(tickers) > 0 and 'lastPrice' in tickers[0]:
                price = float(tickers[0]['lastPrice'])
                self.price_cache[symbol] = (price, time.time())
                return price
            return None
        except Exception as e:
            logger.error(f"خطأ في جلب سعر {symbol}: {e}")
            return None

    def get_historical_data(self, symbol, interval='5m', limit=50):
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
            df['close'] = df['close'].astype(float)
            df['volume'] = df['volume'].astype(float)
            df['open_time'] = pd.to_datetime(df['open_time'], unit='ms').dt.tz_localize('UTC').dt.tz_convert(damascus_tz)
            self.historical_data_cache[cache_key] = (df, time.time())
            return df
        except Exception as e:
            logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
            return None

    def calculate_technical_indicators(self, df):
        try:
            if len(df) < max(self.TRADING_SETTINGS['sma_long'], self.TRADING_SETTINGS['rsi_period']):
                return None
            df['sma5'] = df['close'].rolling(window=self.TRADING_SETTINGS['sma_short']).mean()
            df['sma15'] = df['close'].rolling(window=self.TRADING_SETTINGS['sma_long']).mean()
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(window=self.TRADING_SETTINGS['rsi_period']).mean()
            avg_loss = loss.rolling(window=self.TRADING_SETTINGS['rsi_period']).mean()
            rs = avg_gain / (avg_loss + 1e-12)
            df['rsi'] = 100 - (100 / (1 + rs))
            return df
        except Exception as e:
            logger.error(f"خطأ في حساب المؤشرات: {e}")
            return None

    def calculate_momentum_score(self, symbol):
        try:
            df = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'], 50)
            if df is None or len(df) < self.TRADING_SETTINGS['sma_long']:
                return 0, {}

            indicators = self.calculate_technical_indicators(df)
            if indicators is None:
                return 0, {}
            latest = indicators.iloc[-1]
            prev = indicators.iloc[-2] if len(indicators) >= 2 else latest

            score = 0
            details = {}

            if latest['sma5'] > latest['sma15'] and prev['sma5'] <= prev['sma15']:
                score += 50
                details['sma_crossover'] = 'bullish'
            elif latest['sma5'] < latest['sma15'] and prev['sma5'] >= prev['sma15']:
                details['sma_crossover'] = 'bearish'
            else:
                details['sma_crossover'] = 'no_cross'

            rsi_val = latest['rsi'] if not np.isnan(latest['rsi']) else 50
            details['rsi'] = round(rsi_val, 2)
            if rsi_val < self.TRADING_SETTINGS['rsi_oversold']:
                score += 50
                details['rsi_condition'] = 'oversold'
            elif rsi_val > self.TRADING_SETTINGS['rsi_overbought']:
                details['rsi_condition'] = 'overbought'
            else:
                details['rsi_condition'] = 'neutral'

            details['current_price'] = latest['close']
            return min(score, 100), details
        except Exception as e:
            logger.error(f"خطأ في حساب زخم {symbol}: {e}")
            return 0, {}

    def get_symbol_precision(self, symbol):
        try:
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            if not symbol_info:
                return {'quantity_precision': 6, 'step_size': 0.001}
            lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            step_size = float(lot_size['stepSize']) if lot_size else 0.001
            qty_precision = int(round(-np.log10(step_size))) if step_size < 1 else 0
            return {'quantity_precision': qty_precision, 'step_size': step_size}
        except Exception as e:
            logger.error(f"خطأ في جلب دقة {symbol}: {e}")
            return {'quantity_precision': 6, 'step_size': 0.001}

    def calculate_position_size(self, symbol, current_price):
        risk_usdt = self.TRADING_SETTINGS['risk_per_trade_usdt']
        sl_distance = current_price * abs(self.TRADING_SETTINGS['stop_loss_percent'])
        if sl_distance <= 0:
            return 0
        qty = risk_usdt / sl_distance
        precision = self.get_symbol_precision(symbol)
        step = precision['step_size']
        if step > 0:
            qty = math.floor(qty / step) * step
        return round(qty, precision['quantity_precision'])

    def execute_trade(self, symbol):
        current_price = self.get_current_price(symbol)
        if current_price is None:
            return False

        if len(self.active_trades) >= self.TRADING_SETTINGS['max_active_trades']:
            logger.info(f"⏸️ تخطي {symbol} - الحد الأقصى للصفقات النشطة")
            return False

        balances = self.safe_binance_request(self.client.get_account)
        usdt_balance = float(next((b['free'] for b in balances['balances'] if b['asset'] == 'USDT'), 0))
        if usdt_balance < self.TRADING_SETTINGS['risk_per_trade_usdt']:
            logger.warning(f"💰 رصيد USDT غير كافي: {usdt_balance:.2f}")
            return False

        quantity = self.calculate_position_size(symbol, current_price)
        if quantity <= 0:
            logger.warning(f"❌ كمية محسوبة غير صالحة لـ {symbol}: {quantity}")
            return False

        stop_loss = current_price * (1 + self.TRADING_SETTINGS['stop_loss_percent'])
        take_profit = current_price * (1 + self.TRADING_SETTINGS['take_profit_percent'])

        if not self.dry_run:
            order = self.safe_binance_request(self.client.order_market_buy, symbol=symbol, quantity=quantity)
            if order and order.get('status') in ['FILLED', 'PARTIALLY_FILLED']:
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
                    'order_id': order.get('orderId', 'market_order')
                }
                self.active_trades[symbol] = trade_data
                logger.info(f"✅ صفقة جديدة في {symbol} بسعر {avg_price:.4f}, qty={quantity:.6f}, SL={stop_loss:.4f}, TP={take_profit:.4f}")
                if self.notifier:
                    self.notifier.send_message(
                        f"🚀 <b>صفقة جديدة</b>\nالعملة: {symbol}\nسعر الدخول: ${avg_price:.4f}\nالكمية: {quantity:.6f}\nوقف الخسارة: ${stop_loss:.4f}\nأخذ الربح: ${take_profit:.4f}",
                        f'open_{symbol}'
                    )
                return True
            else:
                logger.error(f"❌ فشل تنفيذ أمر الشراء لـ {symbol}: {order}")
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
                'order_id': 'simulated'
            }
            return True

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

                df = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'], 20)
                if df is not None and len(df) >= self.TRADING_SETTINGS['sma_long']:
                    indicators = self.calculate_technical_indicators(df)
                    if indicators is not None:
                        latest = indicators.iloc[-1]
                        prev = indicators.iloc[-2]

                        if latest['sma5'] < latest['sma15'] and prev['sma5'] >= prev['sma15']:
                            self.close_trade(symbol, current_price, 'sma_bearish_cross')
                            continue

                        if latest['rsi'] > self.TRADING_SETTINGS['rsi_overbought']:
                            self.close_trade(symbol, current_price, 'overbought')
                            continue

                if current_price >= trade['take_profit']:
                    self.close_trade(symbol, current_price, 'take_profit')
                    continue
                elif current_price <= trade['stop_loss']:
                    self.close_trade(symbol, current_price, 'stop_loss')
                    continue

                if trade_duration > self.TRADING_SETTINGS['trade_timeout_hours']:
                    self.close_trade(symbol, current_price, 'timeout')
                    continue

            except Exception as e:
                logger.error(f"خطأ في إدارة صفقة {symbol}: {e}")

    def close_trade(self, symbol, exit_price, reason):
        try:
            trade = self.active_trades[symbol]
            total_fees = trade['trade_size'] * 0.002
            gross_pnl = (exit_price - trade['entry_price']) * trade['quantity']
            net_pnl = gross_pnl - total_fees
            total_profit_percent = (net_pnl / trade['trade_size']) * 100 if trade['trade_size'] > 0 else 0

            if not self.dry_run:
                quantity = round(trade['quantity'] - (trade['quantity'] % self.get_symbol_precision(symbol)['step_size']), self.get_symbol_precision(symbol)['quantity_precision'])
                order = self.safe_binance_request(self.client.order_market_sell, symbol=symbol, quantity=quantity)
                if order and order['status'] == 'FILLED':
                    trade['exit_price'] = exit_price
                    trade['exit_time'] = datetime.now(damascus_tz)
                    trade['profit_loss'] = net_pnl
                    trade['pnl_percent'] = total_profit_percent
                    trade['status'] = 'completed'
                    trade['exit_reason'] = reason
                    logger.info(f"🔚 إغلاق {symbol} بـ {reason}: ${net_pnl:.2f} ({total_profit_percent:+.2f}%)")
                    if self.notifier:
                        self.notifier.send_message(
                            f"{'✅' if net_pnl > 0 else '❌'} <b>إغلاق الصفقة</b>\nالعملة: {symbol}\nالسبب: {self.translate_exit_reason(reason)}\nالربح: ${net_pnl:.2f} ({total_profit_percent:+.2f}%)",
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
                pnl_percent = (net_pnl / trade['trade_size']) * 100 if trade['trade_size'] > 0 else 0
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
            'sma_bearish_cross': 'تقاطع هابط لـ SMA'
        }
        return reasons.get(reason, reason)

    async def find_best_opportunities(self):
        opportunities = []
        tickers = self.get_multiple_tickers(self.TRADING_SETTINGS['symbols'])
        if not tickers:
            logger.warning("⚠️ لا تيكرز متاحة من API Binance")
            return opportunities

        for ticker in tickers:
            symbol = ticker.get('symbol')
            if not symbol or float(ticker.get('volume', 0)) * float(ticker.get('lastPrice', 0)) < 1000000:
                continue
            score, details = self.calculate_momentum_score(symbol)
            if score >= 80 and details['sma_crossover'] == 'bullish' and details['rsi_condition'] == 'oversold':
                opportunities.append({
                    'symbol': symbol,
                    'score': score,
                    'details': details,
                    'timestamp': datetime.now(damascus_tz)
                })
        return sorted(opportunities, key=lambda x: x['score'], reverse=True)

    def run_trading_cycle(self):
        try:
            logger.info("🔄 بدء دورة التداول")
            self.manage_active_trades()
            if len(self.active_trades) < self.TRADING_SETTINGS['max_active_trades']:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(self.find_best_opportunities(), loop)
                    opportunities = future.result(timeout=10)
                else:
                    opportunities = asyncio.run(self.find_best_opportunities())
                if opportunities:
                    logger.info(f"🔍 تم العثور على {len(opportunities)} فرصة")
                    for opportunity in opportunities[:2]:
                        if opportunity['symbol'] not in self.active_trades:
                            self.execute_trade(opportunity['symbol'])
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
        schedule.every(5).minutes.do(self.track_open_trades)
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        self.run_trading_cycle()
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logger.error(f"خطأ في الحلقة الرئيسية: {e}")
                if self.notifier:
                    self.notifier.send_message(f"❌ <b>خطأ في الحلقة الرئيسية</b>\n{e}", 'error')
                time.sleep(60)

    def load_existing_trades(self):
        try:
            account = self.safe_binance_request(self.client.get_account)
            if not account:
                return
            balances = account['balances']
            self.active_trades.clear()
            loaded_count = 0
            for balance in balances:
                asset = balance['asset']
                free_qty = float(balance['free'])
                if asset not in ['USDT', 'BUSD', 'USDC'] and free_qty > 0:
                    symbol = asset + 'USDT'
                    if symbol not in self.TRADING_SETTINGS['symbols']:
                        continue
                    current_price = self.get_current_price(symbol)
                    if current_price is None:
                        continue
                    asset_value_usdt = free_qty * current_price
                    if asset_value_usdt < 10:
                        continue
                    trades = self.safe_binance_request(self.client.get_my_trades, symbol=symbol)
                    entry_price = current_price if not trades else sum(float(t['price']) * float(t['qty']) for t in [t for t in trades if t['isBuyer']]) / sum(float(t['qty']) for t in [t for t in trades if t['isBuyer']])
                    stop_loss = entry_price * (1 + self.TRADING_SETTINGS['stop_loss_percent'])
                    take_profit = entry_price * (1 + self.TRADING_SETTINGS['take_profit_percent'])
                    self.active_trades[symbol] = {
                        'symbol': symbol,
                        'entry_price': entry_price,
                        'quantity': free_qty,
                        'trade_size': free_qty * entry_price,
                        'stop_loss': stop_loss,
                        'take_profit': take_profit,
                        'timestamp': datetime.now(damascus_tz),
                        'status': 'open',
                        'order_id': 'from_balance'
                    }
                    loaded_count += 1
                    logger.info(f"✅ تم تحميل الصفقة من رصيد Binance: {symbol}")
            if loaded_count == 0 and self.notifier:
                self.notifier.send_message("⚠️ <b>لا صفقات مفتوحة</b>", 'no_trades')
        except Exception as e:
            logger.error(f"❌ خطأ في تحميل الصفقات: {e}")
            if self.notifier:
                self.notifier.send_message(f"❌ <b>خطأ في تحميل الصفقات</b>\n{e}", 'error')

def main():
    try:
        bot = MomentumHunterBot(dry_run=True)
        bot.run_bot()
    except Exception as e:
        logger.error(f"❌ خطأ فادح في البوت: {e}")
        if 'bot' in locals() and bot.notifier:
            bot.notifier.send_message(f"❌ <b>إيقاف البوت</b>\nخطأ فادح: {e}", 'fatal_error')

if __name__ == "__main__":
    main()
