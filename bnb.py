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
import json
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
import schedule
from flask import Flask, jsonify, request
import concurrent.futures
import pytz
from cachetools import TTLCache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import aiohttp  # جديد: لتحسين الأداء (طلبات غير متزامنة)
import asyncio  # جديد: لدعم التنفيذ غير المتزامن
from xgboost import XGBClassifier  # جديد: لتحسين الإستراتيجية بالتعلم الآلي

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

@app.route('/stats')
@limiter.limit("10 per minute")
def stats():
    try:
        bot = MomentumHunterBot()
        stats = bot.get_performance_stats()
        return jsonify(stats)
    except Exception as e:
        return {'error': str(e)}

@app.route('/opportunities')
@limiter.limit("10 per minute")
def opportunities():
    try:
        bot = MomentumHunterBot()
        opportunities = bot.get_current_opportunities()
        return jsonify(opportunities)
    except Exception as e:
        return {'error': str(e)}

@app.route('/active_trades')
@limiter.limit("5 per minute")
def active_trades():
    try:
        bot = MomentumHunterBot()
        return jsonify(list(bot.active_trades.values()))
    except Exception as e:
        return {'error': str(e)}

@app.route('/backtest', methods=['POST'])
@limiter.limit("2 per minute")
def run_backtest():
    try:
        bot = MomentumHunterBot()
        data = request.json
        symbol = data.get('symbol')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        results = bot.backtest_strategy(symbol, start_date, end_date)
        return jsonify({'status': 'Backtest completed', 'results': results})
    except Exception as e:
        return {'error': str(e)}

def run_flask_app():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

# إعداد logging مع تحسين الأداء
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('momentum_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_time=300):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.last_failure = None
        self.state = "CLOSED"

    def record_failure(self):
        self.failure_count += 1
        self.last_failure = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning("Circuit breaker OPEN - pausing trading")

    def can_proceed(self):
        if self.state == "OPEN":
            if time.time() - self.last_failure > self.recovery_time:
                self.state = "HALF_OPEN"
                self.failure_count = 0
                return True
            return False
        return True

    def record_success(self):
        if self.state == "HALF_OPEN":
            self.state = "CLOSED"
        self.failure_count = 0

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
            if (message_type in self.last_notifications and 
                current_time - self.last_notifications[message_type] < 300):
                return True
                
            self.last_notifications[message_type] = current_time
            
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code != 200:
                logger.error(f"فشل إرسال رسالة Telegram: {response.text}")
                return False
            return True
        except Exception as e:
            logger.error(f"خطأ في إرسال رسالة Telegram: {e}")
            return False
    
    def send_message(self, message, message_type='info'):
        with self.queue_lock:
            self.message_queue.append({
                'message': message,
                'message_type': message_type
            })
        return True

class RequestManager:
    def __init__(self):
        self.request_count = 0
        self.last_request_time = time.time()
        self.max_requests_per_minute = 500  # خفض الحد لتجنب الحظر
        self.request_lock = threading.Lock()

    def safe_request(self, func, *args, **kwargs):
        with self.request_lock:
            current_time = time.time()
            elapsed = current_time - self.last_request_time

            if elapsed < 0.1:  # زيادة التأخير إلى 100 مللي ثانية
                time.sleep(0.1 - elapsed)

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

class MongoManager:
    def __init__(self, connection_string=None):
        self.connection_string = (connection_string or 
                                 os.environ.get('MANGO_DB_CONNECTION_STRING') or
                                 os.environ.get('MONGODB_URI') or
                                 os.environ.get('DATABASE_URL'))
        
        if self.connection_string:
            logger.info("✅ تم العثور على رابط MongoDB")
        else:
            logger.warning("❌ لم يتم العثور على رابط MongoDB")
            
        self.client = None
        self.db = None
        self.connect(retries=5, delay=5)
        
    def connect(self, retries=3, delay=5):
        for attempt in range(retries):
            try:
                self.client = MongoClient(self.connection_string, serverSelectionTimeoutMS=5000)
                self.client.admin.command('ping')
                self.db = self.client['momentum_hunter_bot']
                self.initialize_db()
                logger.info("✅ تم الاتصال بـ MongoDB بنجاح")
                return True
            except ConnectionFailure as e:
                logger.error(f"❌ فشل الاتصال بـ MongoDB (محاولة {attempt+1}): {e}")
                time.sleep(delay * (2 ** attempt))
        return False
    
    def initialize_db(self):
        if self.db is not None:  # معدل: استخدام is not None
            self.db['trades'].create_index([('symbol', 1), ('status', 1)])
            self.db['opportunities'].create_index([('scanned_at', 1)])
    
    def save_trade(self, trade_data):
        try:
            if self.db is not None:  # معدل: استخدام is not None
                collection = self.db['trades']
                trade_data['timestamp'] = datetime.now(damascus_tz)
                result = collection.insert_one(trade_data)
                return True
            else:
                logger.error("❌ قاعدة البيانات غير متصلة")
                return False
        except Exception as e:
            logger.error(f"خطأ في حفظ الصفقة: {e}")
            return False
    
    def save_opportunity(self, opportunity):
        try:
            if self.db is not None:  # معدل: استخدام is not None
                collection = self.db['opportunities']
                opportunity['scanned_at'] = datetime.now(damascus_tz)
                collection.insert_one(opportunity)
                return True
            else:
                logger.error("❌ قاعدة البيانات غير متصلة")
                return False
        except Exception as e:
            logger.error(f"خطأ في حفظ الفرصة: {e}")
            return False
    
    def get_performance_stats(self):
        try:
            if self.db is not None:  # معدل: استخدام is not None
                collection = self.db['trades']
                stats = collection.aggregate([
                    {'$match': {'status': 'completed'}},
                    {'$group': {
                        '_id': None,
                        'total_trades': {'$sum': 1},
                        'profitable_trades': {
                            '$sum': {'$cond': [{'$gt': ['$profit_loss', 0]}, 1, 0]}
                        },
                        'total_profit': {
                            '$sum': {'$cond': [{'$gt': ['$profit_loss', 0]}, '$profit_loss', 0]}
                        },
                        'total_loss': {
                            '$sum': {'$cond': [{'$lt': ['$profit_loss', 0]}, '$profit_loss', 0]}
                        }
                    }}
                ])
                
                result = list(stats)
                if result:
                    stats_data = result[0]
                    win_rate = (stats_data['profitable_trades'] / stats_data['total_trades'] * 100) if stats_data['total_trades'] > 0 else 0
                    return {
                        'total_trades': stats_data['total_trades'],
                        'win_rate': round(win_rate, 2),
                        'total_profit': round(stats_data['total_profit'], 2),
                        'total_loss': round(abs(stats_data['total_loss']), 2),
                        'profit_factor': round(stats_data['total_profit'] / abs(stats_data['total_loss']), 2) if stats_data['total_loss'] < 0 else float('inf')
                    }
                return {}
            else:
                logger.error("❌ قاعدة البيانات غير متصلة")
                return {}
        except Exception as e:
            logger.error(f"خطأ في جلب الإحصائيات: {e}")
            return {}

    def update_trade_stop_loss(self, symbol, new_sl):
        try:
            if self.db is not None:  # معدل: استخدام is not None
                collection = self.db['trades']
                result = collection.update_one(
                    {'symbol': symbol, 'status': 'open'},
                    {'$set': {'stop_loss': new_sl}}
                )
                return result.modified_count > 0
            else:
                logger.error("❌ قاعدة البيانات غير متصلة")
                return False
        except Exception as e:
            logger.error(f"خطأ في تحديث وقف الخسارة: {e}")
            return False

class HealthMonitor:
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.error_count = 0
        self.max_errors = 10
        self.last_health_check = datetime.now(damascus_tz)
        
    def check_connections(self):
        try:
            self.bot.request_manager.safe_request(self.bot.client.get_server_time)
            
            if not self.bot.mongo_manager.connect():
                logger.warning("⚠️  فشل الاتصال بـ MongoDB - لكن البوت سيستمر في العمل")
                return True
                
            self.error_count = 0
            return True
            
        except Exception as e:
            self.error_count += 1
            logger.error(f"خطأ في فحص الصحة: {e}")
            
            if self.error_count >= self.max_errors:
                self.restart_bot()
                
            return False
    
    def restart_bot(self):
        logger.warning("🔄 إعادة تشغيل البوت بسبب كثرة الأخطاء")
        if self.bot.notifier:
            self.bot.notifier.send_message("🔄 <b>إعادة تشغيل البوت</b>\nكثرة الأخطاء تتطلب إعادة التشغيل", "restart")
        
        os._exit(1)

class MomentumHunterBot:
    WEIGHTS = {
        'trend': 25,
        'crossover': 20,
        'price_change': 15,
        'volume': 15,
        'rsi': 10,
        'macd': 10,
        'adx': 15,
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
        self.circuit_breaker = CircuitBreaker()
        self.mongo_manager = MongoManager()
        self.cache = TTLCache(maxsize=1000, ttl=300)
        
        if self.telegram_token and self.telegram_chat_id:
            self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
        else:
            self.notifier = None
            
        self.health_monitor = HealthMonitor(self)
        
        self.symbols = self.get_all_trading_symbols()
        self.stable_coins = ['USDT', 'BUSD', 'USDC']
        self.min_daily_volume = 1000000
        self.min_trade_size = 10
        self.max_trade_size = 50
        self.risk_per_trade = 2.0
        self.max_position_size = 0.35
        self.momentum_score_threshold = 60
        
        self.active_trades = {}
        self.last_scan_time = datetime.now()
        self.min_profit_threshold = 0.003
        
        # جديد: تهيئة نموذج XGBoost
        self.ml_model = None
        self.train_ml_model()  # تدريب النموذج عند التهيئة

        logger.info("✅ تم تهيئة بوت صائد الصاعدات المتقدم بنجاح")


    def get_all_trading_symbols(self):
        try:
            # القائمة الأولية الموسعة
            important_symbols = [
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "AVAXUSDT", "XLMUSDT", "SUIUSDT", "TONUSDT", "WLDUSDT",
                "ADAUSDT", "DOTUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT",
                "DOGEUSDT", "MATICUSDT", "ATOMUSDT", "NEARUSDT", "FILUSDT",
                "INJUSDT", "RUNEUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
                "TRXUSDT", "ALGOUSDT", "VETUSDT", "HBARUSDT", "FTMUSDT",
                "EGLDUSDT", "XMRUSDT", "GALAUSDT"  # العملات المقترحة الجديدة
            ]
            logger.info(f"🔸 استخدام القائمة الأولية الموسعة: {len(important_symbols)} عملة")

            # محاولة جلب رموز ديناميكية إضافية
            tickers = self.get_multiple_tickers(important_symbols)
            dynamic_symbols = []
            for ticker in tickers:
                symbol = ticker['symbol']
                if float(ticker['volume']) * float(ticker['weightedAvgPrice']) > self.min_daily_volume:
                    dynamic_symbols.append(symbol)

            # دمج القائمتين (إزالة التكرار)
            all_symbols = list(set(important_symbols + dynamic_symbols))
            logger.info(f"🔸 إجمالي الرموز بعد الدمج: {len(all_symbols)}")
            return all_symbols if all_symbols else important_symbols  # الرجوع إلى القائمة الأولية إذا فشل الجلب
        except Exception as e:
            logger.error(f"خطأ في جلب الرموز: {e}")
            logger.info("🔄 الرجوع إلى القائمة الأولية الموسعة")
            return important_symbols  # قائمة احتياطية موسعة
    
    def safe_binance_request(self, func, *args, **kwargs):
        if not self.circuit_breaker.can_proceed():
            logger.warning("دائرة الكسر مفتوحة - تجاهل الطلب")
            return None
        try:
            result = self.request_manager.safe_request(func, *args, **kwargs)
            self.circuit_breaker.record_success()
            return result
        except Exception as e:
            self.circuit_breaker.record_failure()
            logger.error(f"خطأ في طلب Binance: {e}")
            return None

    async def fetch_ticker_async(self, symbol, session):
        """جلب تيكر بشكل غير متزامن باستخدام aiohttp"""
        try:
            url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"فشل جلب {symbol}: {response.status}")
                    return None
        except asyncio.TimeoutError:
            logger.error(f"انتهت مهلة جلب {symbol}")
            return None
        except Exception as e:
            logger.error(f"خطأ في جلب تيكر {symbol}: {e}")
            return None

    async def fetch_ticker_async(self, symbol, session):
        """جلب تيكر بشكل غير متزامن باستخدام aiohttp"""
        try:
            url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"فشل جلب {symbol}: {response.status}")
                    return None
        except asyncio.TimeoutError:
            logger.error(f"انتهت مهلة جلب {symbol}")
            return None
        except Exception as e:
            logger.error(f"خطأ في جلب تيكر {symbol}: {e}")
        return None

    # جديد: دالة غير متزامنة لجلب تيكر واحد
    async def fetch_ticker_async(self, symbol, session):
        """جلب تيكر بشكل غير متزامن باستخدام aiohttp"""
        try:
            url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"فشل جلب {symbol}: {response.status}")
                    return None
        except Exception as e:
            logger.error(f"خطأ في جلب تيكر {symbol}: {e}")
            return None

    async def get_multiple_tickers_async(self, symbols):
        """جلب بيانات التيكرز لعدة رموز بشكل غير متزامن"""
        async with aiohttp.ClientSession() as session:
            tasks = [self.fetch_ticker_async(symbol, session) for symbol in symbols]
            tickers = await asyncio.gather(*tasks, return_exceptions=True)
            return [ticker for ticker in tickers if ticker is not None]

    
    def get_multiple_tickers(self, symbols):
        """واجهة متزامنة لجلب التيكرز"""
        try:
            return asyncio.run(self.get_multiple_tickers_async(symbols))
        except Exception as e:
            logger.error(f"خطأ في جلب تيكرز متعددة: {e}")
            return []
    
    # معدل: واجهة متزامنة للدالة غير المتزامنة
    def get_multiple_tickers(self, symbols):
        """واجهة متزامنة لجلب التيكرز"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # إذا كانت الحلقة قيد التشغيل (على Render)، استخدم طريقة بديلة
                return asyncio.run_coroutine_threadsafe(self.get_multiple_tickers_async(symbols), loop).result()
            else:
                return asyncio.run(self.get_multiple_tickers_async(symbols))
        except Exception as e:
            logger.error(f"خطأ في جلب تيكرز متعددة: {e}")
            return []

    def get_account_balance(self):
        try:
            account = self.safe_binance_request(self.client.get_account)
            balances = {}
            for asset in account['balances']:
                free = float(asset['free'])
                locked = float(asset['locked'])
                if free + locked > 0:
                    balances[asset['asset']] = {
                        'free': free,
                        'locked': locked,
                        'total': free + locked
                    }
            return balances
        except Exception as e:
            logger.error(f"خطأ في جلب الرصيد: {e}")
            return {}
    
    def get_current_price(self, symbol):
        cache_key = f"price_{symbol}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        try:
            ticker = self.safe_binance_request(self.client.get_symbol_ticker, symbol=symbol)
            price = float(ticker['price'])
            self.cache[cache_key] = price
            return price
        except Exception as e:
            logger.error(f"خطأ في جلب سعر {symbol}: {e}")
            return None
    
    def get_historical_data(self, symbol, interval='15m', limit=100):
        cache_key = f"hist_{symbol}_{interval}_{limit}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        try:
            klines = self.safe_binance_request(self.client.get_klines, 
                                              symbol=symbol, 
                                              interval=interval, 
                                              limit=limit)
            data = []
            for k in klines:
                data.append({
                    'timestamp': k[0],
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
            df = pd.DataFrame(data)
            self.cache[cache_key] = df
            return df
        except Exception as e:
            logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
            return None
    
    def calculate_adx(self, df, period=14):
        high, low, close = df['high'], df['low'], df['close']
        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
        tr = pd.concat([(high - low),
                        (high - close.shift()).abs(),
                        (low - close.shift()).abs()], axis=1).max(axis=1)
        atr_series = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / (atr_series + 1e-12))
        minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / (atr_series + 1e-12))
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-12)) * 100
        return dx.ewm(alpha=1/period, adjust=False).mean()
    
    def update_ema(self, previous_ema, new_price, span):
        alpha = 2 / (span + 1)
        return alpha * new_price + (1 - alpha) * previous_ema

    def calculate_technical_indicators(self, data):
        try:
            df = data.copy()
            if len(df) < 50:
                return df
                
            df['ema8'] = df['close'].ewm(span=8, adjust=False).mean()
            df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
            df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
            df['ema100'] = df['close'].ewm(span=100, adjust=False).mean()
            
            delta = df['close'].diff()
            gains = delta.where(delta > 0, 0)
            losses = -delta.where(delta < 0, 0)
            avg_gain = gains.rolling(window=14).mean()
            avg_loss = losses.rolling(window=14).mean()
            rs = avg_gain / avg_loss
            df['rsi'] = 100 - (100 / (1 + rs))
            
            ema12 = df['close'].ewm(span=12, adjust=False).mean()
            ema26 = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = ema12 - ema26
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
            df['macd_hist'] = df['macd'] - df['macd_signal']
            
            df['volume_ma'] = df['volume'].rolling(window=20).mean()
            df['volume_ratio'] = df['volume'] / df['volume_ma']
            
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            ranges = pd.concat([high_low, high_close, low_close], axis=1)
            true_range = np.max(ranges, axis=1)
            df['atr'] = true_range.rolling(window=14).mean()
            
            df['middle_bb'] = df['close'].rolling(window=20).mean()
            bb_std = df['close'].rolling(window=20).std()
            df['upper_bb'] = df['middle_bb'] + (bb_std * 2)
            df['lower_bb'] = df['middle_bb'] - (bb_std * 2)
            
            df['adx'] = self.calculate_adx(df)
            
            return df
        except Exception as e:
            logger.error(f"خطأ في حساب المؤشرات: {e}")
            return data
    
    # جديد: دالة لتدريب نموذج XGBoost
    def train_ml_model(self):
        """تدريب نموذج XGBoost بناءً على بيانات الصفقات السابقة"""
        try:
            trades = list(self.mongo_manager.db['trades'].find({'status': 'completed'}))
            if len(trades) < 10:
                logger.warning("بيانات غير كافية لتدريب النموذج")
                return

            X = []
            y = []
            for trade in trades:
                details = trade.get('details', {})
                X.append([
                    trade.get('score', 0),
                    details.get('rsi', 50),
                    details.get('adx', 0),
                    details.get('volume_ratio', 1),
                    details.get('atr_percent', 0)
                ])
                y.append(1 if trade.get('profit_loss', 0) > 0 else 0)

            self.ml_model = XGBClassifier(n_estimators=100, random_state=42)
            self.ml_model.fit(X, y)
            logger.info("✅ تم تدريب نموذج XGBoost بنجاح")
        except Exception as e:
            logger.error(f"خطأ في تدريب النموذج: {e}")

    # معدل: دمج XGBoost في حساب نقاط الزخم
    def calculate_momentum_score(self, symbol):
        try:
            data = self.get_historical_data(symbol, '15m', 100)
            if data is None or len(data) < 50:
                return 0, {}
            
            data = self.calculate_technical_indicators(data)
            latest = data.iloc[-1]
            prev = data.iloc[-2]
            
            score = 0
            details = {}
            
            if latest['ema21'] > latest['ema50'] and latest['ema50'] > latest['ema100']:
                score += self.WEIGHTS['trend']
                details['trend'] = 'صاعد قوي'
            elif latest['ema21'] > latest['ema50']:
                score += self.WEIGHTS['trend'] * 0.6
                details['trend'] = 'صاعد'
            else:
                details['trend'] = 'هابط'
                return 0, details
            
            window = data.iloc[max(0, len(data)-4):]
            for i in range(1, len(window)):
                if window['ema8'].iat[i-1] <= window['ema21'].iat[i-1] and window['ema8'].iat[i] > window['ema21'].iat[i]:
                    score += self.WEIGHTS['crossover']
                    details['crossover'] = 'إيجابي'
                    break
            
            price_change_5 = ((latest['close'] - data.iloc[-5]['close']) / data.iloc[-5]['close']) * 100 if len(data) >= 5 else 0
            price_change_15 = ((latest['close'] - data.iloc[-15]['close']) / data.iloc[-15]['close']) * 100 if len(data) >= 15 else 0
            
            details['price_change_5candles'] = round(price_change_5, 2)
            details['price_change_15candles'] = round(price_change_15, 2)
            
            if price_change_5 >= 2.0 and price_change_15 >= 3.0:
                score += self.WEIGHTS['price_change']
            
            volume_ratio = latest['volume_ratio']
            details['volume_ratio'] = round(volume_ratio, 2) if not pd.isna(volume_ratio) else 1
            
            if volume_ratio >= 1.8:
                score += self.WEIGHTS['volume']
            
            details['rsi'] = round(latest['rsi'], 2) if not pd.isna(latest['rsi']) else 50
            
            if 40 <= latest['rsi'] <= 65:
                score += self.WEIGHTS['rsi']
            
            if latest['macd'] > latest['macd_signal'] and latest['macd_hist'] > 0:
                score += self.WEIGHTS['macd']
                details['macd'] = 'إيجابي'
            
            details['adx'] = round(latest['adx'], 2) if not pd.isna(latest['adx']) else 0
            if latest['adx'] >= 25:
                score += self.WEIGHTS['adx']
                details['adx_strength'] = 'قوي'
            elif latest['adx'] >= 20:
                score += self.WEIGHTS['adx'] * 0.6
                details['adx_strength'] = 'متوسط'
            
            if latest['close'] > latest['middle_bb']:
                score += self.WEIGHTS['bollinger']
                details['bollinger'] = 'فوق المتوسط'
            
            details['current_price'] = latest['close']
            details['atr'] = latest['atr'] if not pd.isna(latest['atr']) else 0
            details['atr_percent'] = round((latest['atr'] / latest['close']) * 100, 2) if latest['atr'] > 0 else 0
            
            # جديد: دمج تنبؤ XGBoost
            if self.ml_model:
                try:
                    input_data = np.array([[
                        score,
                        details.get('rsi', 50),
                        details.get('adx', 0),
                        details.get('volume_ratio', 1),
                        details.get('atr_percent', 0)
                    ]])
                    pred_prob = self.ml_model.predict_proba(input_data)[0][1]  # احتمالية النجاح
                    score += pred_prob * 20  # إضافة نقاط إضافية (حد أقصى 20)
                    score = min(score, 100)
                    details['ml_prediction'] = round(pred_prob * 100, 2)
                    logger.info(f"🔮 تنبؤ XGBoost لـ {symbol}: {details['ml_prediction']}%")
                except Exception as e:
                    logger.error(f"خطأ في تنبؤ XGBoost لـ {symbol}: {e}")

            return min(score, 100), details
            
        except Exception as e:
            logger.error(f"خطأ في حساب زخم {symbol}: {e}")
            return 0, {}
    
    async def find_best_opportunities(self):
        opportunities = []
        rejected_symbols = []
        symbols_to_analyze = self.symbols[:100]  # تحليل أول 100 رمز لتقليل الطلبات

        async def process_symbol(symbol):
            try:
                tickers = await self.get_multiple_tickers_async([symbol])
                ticker = tickers[0] if tickers else None
                if not ticker:
                    return None
                daily_volume = float(ticker['volume']) * float(ticker['lastPrice'])
    
                if daily_volume < self.min_daily_volume:
                    rejected_symbols.append({'symbol': symbol, 'reason': f'حجم غير كافي: {daily_volume:,.0f}'})
                    return None
    
                momentum_score, details = self.calculate_momentum_score(symbol)
    
                if momentum_score >= self.momentum_score_threshold:
                    opportunity = {
                        'symbol': symbol,
                        'score': momentum_score,
                        'details': details,
                        'daily_volume': daily_volume,
                        'timestamp': datetime.now(damascus_tz)
                    }
                    return opportunity
                else:
                    rejected_symbols.append({'symbol': symbol, 'reason': f'نقاط غير كافية: {momentum_score}'})
                    return None
        
            except Exception as e:
                logger.error(f"خطأ في تحليل {symbol}: {e}")
                return None

        # Process symbols concurrently
        async with aiohttp.ClientSession() as session:
            tasks = [process_symbol(symbol) for symbol in symbols_to_analyze]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Error processing symbol: {result}")
                elif result is not None:
                    opportunities.append(result)

        opportunities.sort(key=lambda x: x['score'], reverse=True)

        if rejected_symbols and not opportunities:
            top_rejected = sorted([r for r in rejected_symbols if 'نقاط' in r['reason']], 
                                 key=lambda x: float(x['reason'].split(': ')[1]), reverse=True)[:5]
            logger.info(f"🔍 تم رفض {len(rejected_symbols)} عملة. أفضل العملات المرفوضة: {top_rejected}")

        return opportunities
    
    def check_correlation(self, symbol, active_symbols):
        if not active_symbols:
            return True
        data1 = self.get_historical_data(symbol, '1h', 100)
        for active_symbol in active_symbols:
            data2 = self.get_historical_data(active_symbol, '1h', 100)
            if data1 is None or data2 is None:
                continue
            correlation = data1['close'].corr(data2['close'])
            if correlation > 0.8:
                logger.info(f"تخطي {symbol} بسبب ارتباط عالي ({correlation:.2f}) مع {active_symbol}")
                return False
        return True
    
    def calculate_position_size(self, opportunity, usdt_balance):
        try:
            score = opportunity['score']
            current_price = opportunity['details']['current_price']
            atr = opportunity['details']['atr']
            atr_percent = opportunity['details']['atr_percent']
            
            if score >= 80:
                risk_pct = 0.007
                risk_level = "استثنائية 🚀"
            elif score >= 70:
                risk_pct = 0.006
                risk_level = "قوية جداً 💪"
            elif score >= 65:
                risk_pct = 0.005
                risk_level = "قوية 👍"
            elif score >= 60:
                risk_pct = 0.004
                risk_level = "جيدة 🔄"
            else:
                return 0, {'risk_level': 'ضعيفة - لا تتداول'}
            
            volatility_factor = min(1.0, 5.0 / atr_percent) if atr_percent > 0 else 1.0
            stop_distance = atr * 2.5
            risk_amount = usdt_balance * risk_pct * volatility_factor
            position_size_usdt = min(risk_amount / (stop_distance / current_price), self.max_trade_size)
            position_size_usdt = max(self.min_trade_size, position_size_usdt)
            
            min_profit_needed = position_size_usdt * self.min_profit_threshold
            potential_profit = (opportunity['details'].get('price_change_5candles', 0) / 100) * position_size_usdt
            
            if potential_profit < min_profit_needed:
                logger.info(f"تخطي {opportunity['symbol']} - الربح المتوقع {potential_profit:.2f} أقل من الحد الأدنى {min_profit_needed:.2f}")
                return 0, {'risk_level': 'ربح غير كافي'}
        
            size_info = {
                'size_usdt': position_size_usdt,
                'risk_percentage': (position_size_usdt / usdt_balance) * 100 if usdt_balance > 0 else 0,
                'risk_level': risk_level,
                'min_trade_size': self.min_trade_size
            }
        
            logger.info(f"📊 حجم الصفقة لـ {opportunity['symbol']}: "
                       f"${position_size_usdt:.2f} - "
                       f"التقييم: {risk_level}")
        
            return position_size_usdt, size_info
        
        except Exception as e:
            logger.error(f"خطأ في حساب حجم الصفقة: {e}")
            return 0, {'risk_level': 'خطأ في الحساب'}

    def get_symbol_precision(self, symbol):
        cache_key = f"precision_{symbol}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        try:
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            if not symbol_info:
                return {'quantity_precision': 6, 'price_precision': 2, 'step_size': 0.001, 'tick_size': 0.01}
        
            lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            step_size = float(lot_size['stepSize']) if lot_size else 0.001
            qty_precision = int(round(-np.log10(step_size))) if step_size < 1 else 0
        
            price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            tick_size = float(price_filter['tickSize']) if price_filter else 0.01
            price_precision = int(round(-np.log10(tick_size))) if tick_size < 1 else 0
        
            precision = {
                'quantity_precision': qty_precision,
                'price_precision': price_precision,
                'step_size': step_size,
                'tick_size': tick_size
            }
            self.cache[cache_key] = precision
            return precision
        except Exception as e:
            logger.error(f"خطأ في جلب دقة {symbol}: {e}")
            return {'quantity_precision': 6, 'price_precision': 2, 'step_size': 0.001, 'tick_size': 0.01}

    def manage_active_trades(self):
        for symbol, trade in list(self.active_trades.items()):
            try:
                trade_age = (datetime.now() - trade['timestamp']).total_seconds()
                if trade_age < 60:
                    continue
                
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    continue
                
                estimated_fees = trade['trade_size'] * 0.001
                net_pnl = ((current_price - trade['entry_price']) * trade['quantity']) - estimated_fees
                net_pnl_percent = (net_pnl / trade['trade_size']) * 100
                
                stop_loss_with_margin = trade['stop_loss'] * 0.995
                if current_price <= stop_loss_with_margin:
                    logger.info(f"🔻 وقف خسارة لـ {symbol}: {current_price:.4f} <= {stop_loss_with_margin:.4f}")
                    self.close_trade(symbol, current_price, 'stop_loss')
                    continue
                
                if current_price >= trade['take_profit'] and net_pnl_percent >= 1.0:
                    logger.info(f"✅ أخذ ربح لـ {symbol}: {current_price:.4f} >= {trade['take_profit']:.4f}")
                    self.close_trade(symbol, current_price, 'take_profit')
                    continue
                
                if net_pnl_percent >= 5.0:
                    new_sl = max(trade['stop_loss'], current_price - (trade['atr'] * 1.5))
                    if new_sl > trade['stop_loss']:
                        trade['stop_loss'] = new_sl
                        logger.info(f"📈 تم تحديث وقف الخسارة لـ {symbol} إلى ${new_sl:.4f}")
                        self.mongo_manager.update_trade_stop_loss(symbol, new_sl)
                        
                trade_duration_hours = trade_age / 3600
                if trade_duration_hours > 6 and net_pnl_percent < 0.5:
                    self.close_trade(symbol, current_price, 'timeout')
                    continue
                
                data = self.get_historical_data(symbol, '15m', 100)
                if data is None or len(data) < 50:
                    continue
                data = self.calculate_technical_indicators(data)
                latest = data.iloc[-1]
                if latest['ema8'] < latest['ema21'] and latest['macd'] < latest['macd_signal']:
                    self.close_trade(symbol, current_price, 'trend_weakness')
                    continue
                
            except Exception as e:
                logger.error(f"خطأ في إدارة صفقة {symbol}: {e}")

    def execute_trade(self, symbol, opportunity):
        current_price = opportunity['details']['current_price']
        atr = opportunity['details']['atr']
        
        try:
            if symbol in self.active_trades:
                logger.info(f"⏭️ تخطي {symbol} - صفقة نشطة موجودة")
                return False
            
            if not self.check_correlation(symbol, list(self.active_trades.keys())):
                return False
        
            balances = self.get_account_balance()
            usdt_balance = balances.get('USDT', {}).get('free', 0)
        
            if usdt_balance < self.min_trade_size:
                logger.warning(f"💰 رصيد USDT غير كافي: {usdt_balance:.2f} < {self.min_trade_size}")
                return False
        
            position_size_usdt, size_info = self.calculate_position_size(opportunity, usdt_balance)
            
            if position_size_usdt < self.min_trade_size:
                logger.info(f"📉 تخطي {symbol} - حجم الصفقة صغير: {position_size_usdt:.2f}")
                return False
        
            quantity = position_size_usdt / current_price
        
            precision_info = self.get_symbol_precision(symbol)
            step_size = precision_info['step_size']
            quantity = (quantity // step_size) * step_size
            quantity = round(quantity, precision_info['quantity_precision'])
        
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            if not symbol_info:
                return False
                
            lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if lot_size:
                min_qty = float(lot_size['minQty'])
                if quantity < min_qty:
                    logger.warning(f"⚖️ الكمية {quantity} أقل من الحد الأدنى {min_qty} لـ {symbol}")
                    return False
        
            atr_multiplier = 2.5
            risk_reward_ratio = 3.0
            
            stop_loss_price = current_price - (atr * atr_multiplier)
            take_profit_price = current_price + (risk_reward_ratio * (current_price - stop_loss_price))
            
            min_sl_distance = current_price * 0.005
            if (current_price - stop_loss_price) < min_sl_distance:
                stop_loss_price = current_price - min_sl_distance
                take_profit_price = current_price + (risk_reward_ratio * (current_price - stop_loss_price))
        
            if self.dry_run:
                logger.info(f"🧪 محاكاة صفقة لـ {symbol}: حجم {position_size_usdt:.2f}")
                return True
            
            order = self.safe_binance_request(self.client.order_market_buy,
                                         symbol=symbol,
                                         quantity=quantity)
        
            if not order or order['status'] != 'FILLED':
                logger.error(f"❌ فشل تنفيذ أمر الشراء لـ {symbol}")
                return False
                
            avg_fill_price = float(order['fills'][0]['price']) if order['fills'] else current_price
            
            trade_data = {
                'symbol': symbol,
                'type': 'buy',
                'quantity': quantity,
                'entry_price': avg_fill_price,
                'trade_size': quantity * avg_fill_price,
                'stop_loss': stop_loss_price,
                'take_profit': take_profit_price,
                'atr': atr,
                'position_size_usdt': position_size_usdt,
                'risk_percentage': size_info.get('risk_percentage', 0),
                'risk_level': size_info.get('risk_level', ''),
                'timestamp': datetime.now(),
                'status': 'open',
                'score': opportunity['score'],
                'order_id': order['orderId'],
                'min_profit_threshold': self.min_profit_threshold
            }
        
            self.active_trades[symbol] = trade_data
            self.mongo_manager.save_trade(trade_data)
        
            if self.notifier:
                message = (
                    f"🚀 <b>صفقة جديدة - إستراتيجية محسنة</b>\n\n"
                    f"• العملة: {symbol}\n"
                    f"• السعر: ${avg_fill_price:.4f}\n"
                    f"• الكمية: {quantity:.6f}\n"
                    f"• الحجم: ${quantity * avg_fill_price:.2f}\n"
                    f"• النتيجة: {opportunity['score']}/100\n"
                    f"• مستوى المخاطرة: {size_info.get('risk_level', '')}\n"
                    f"• نسبة المخاطرة: {size_info.get('risk_percentage', 0):.1f}%\n"
                    f"• وقف الخسارة: ${stop_loss_price:.4f}\n"
                    f"• أخذ الربح: ${take_profit_price:.4f}\n"
                    f"• نسبة العائد: {risk_reward_ratio}:1\n"
                    f"• ATR: {opportunity['details']['atr_percent']}%\n"
                    f"• هامش الأمان: {atr_multiplier} ATR\n\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_execution')
        
            logger.info(f"✅ تم شراء {symbol} - الحجم: ${position_size_usdt:.2f}")
            
            time.sleep(2)
            return True
        
        except Exception as e:
            logger.error(f"❌ خطأ في تنفيذ صفقة {symbol}: {e}")
            return False

    def close_trade(self, symbol, exit_price, reason):
        try:
            trade = self.active_trades[symbol]
            
            gross_pnl = (exit_price - trade['entry_price']) * trade['quantity']
            estimated_fees = trade['trade_size'] * 0.002
            net_pnl = gross_pnl - estimated_fees
            pnl_percent = (net_pnl / trade['trade_size']) * 100
            
            min_expected_pnl = trade['trade_size'] * trade.get('min_profit_threshold', 0.002)
            if abs(net_pnl) < min_expected_pnl and reason != 'stop_loss':
                logger.info(f"🔄 إلغاء إغلاق {symbol} - الربح/الخسارة أقل من الحد الأدنى")
                return False
            
            trade['exit_price'] = exit_price
            trade['exit_time'] = datetime.now()
            trade['profit_loss'] = net_pnl
            trade['pnl_percent'] = pnl_percent
            trade['status'] = 'completed'
            trade['exit_reason'] = reason
            trade['fees_estimated'] = estimated_fees
            
            self.mongo_manager.save_trade(trade)
            
            if self.notifier:
                emoji = "✅" if net_pnl > 0 else "❌"
                message = (
                    f"{emoji} <b>إغلاق الصفقة</b>\n\n"
                    f"• العملة: {symbol}\n"
                    f"• السبب: {self.translate_exit_reason(reason)}\n"
                    f"• سعر الدخول: ${trade['entry_price']:.4f}\n"
                    f"• سعر الخروج: ${exit_price:.4f}\n"
                    f"• الربح/الخسارة: ${net_pnl:.2f} ({pnl_percent:+.2f}%)\n"
                    f"• الرسوم التقديرية: ${estimated_fees:.2f}\n"
                    f"• المدة: {(trade['exit_time'] - trade['timestamp']).total_seconds() / 60:.1f} دقيقة\n"
                    f"• المخاطرة: ${trade['position_size_usdt']:.2f}\n\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_close')
            
            logger.info(f"🔚 تم إغلاق {symbol} بـ {reason}: ${net_pnl:.2f} ({pnl_percent:+.2f}%)")
            del self.active_trades[symbol]
            return True
            
        except Exception as e:
            logger.error(f"❌ خطأ في إغلاق صفقة {symbol}: {e}")
            return False

    def translate_exit_reason(self, reason):
        reasons = {
            'stop_loss': 'وقف الخسارة',
            'take_profit': 'أخذ الربح',
            'timeout': 'انتهاء الوقت',
            'manual': 'يدوي',
            'trend_weakness': 'ضعف الاتجاه'
        }
        return reasons.get(reason, reason)
    
    def auto_convert_stuck_assets(self):
        try:
            balances = self.get_account_balance()
            usdt_value = balances.get('USDT', {}).get('free', 0)
            
            for asset, balance in balances.items():
                if asset in self.stable_coins and asset != 'USDT':
                    if balance['free'] > 10:
                        self.convert_to_usdt(asset, balance['free'])
                elif asset not in self.stable_coins and balance['free'] > 0:
                    symbol = asset + 'USDT'
                    current_price = self.get_current_price(symbol)
                    if current_price and (balance['free'] * current_price) > 20:
                        self.convert_to_usdt(asset, balance['free'])
            
            return usdt_value
            
        except Exception as e:
            logger.error(f"خطأ في تحويل الأصول: {e}")
            return 0
    
    def convert_to_usdt(self, asset, amount):
        try:
            if asset == 'USDT':
                return True
                
            symbol = asset + 'USDT'
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            if not symbol_info:
                return False
            
            precision_info = self.get_symbol_precision(symbol)
            step_size = precision_info['step_size']
            quantity = (amount // step_size) * step_size
            quantity = round(quantity, precision_info['quantity_precision'])
            
            order = self.safe_binance_request(self.client.order_market_sell,
                                         symbol=symbol,
                                         quantity=quantity)
            
            if order and order['status'] == 'FILLED':
                logger.info(f"تم تحويل {quantity} {asset} إلى USDT")
                return True
                
        except Exception as e:
            logger.error(f"خطأ في تحويل {asset} إلى USDT: {e}")
        return False
    
    def get_performance_stats(self):
        return self.mongo_manager.get_performance_stats()
    
    def get_current_opportunities(self):
        opportunities = self.find_best_opportunities()
        return {
            'total_opportunities': len(opportunities),
            'opportunities': [{
                'symbol': opp['symbol'],
                'score': opp['score'],
                'price': opp['details']['current_price'],
                'volume': opp['daily_volume'],
                'trend': opp['details']['trend']
            } for opp in opportunities[:5]],
            'scan_time': datetime.now().isoformat()
        }
    
    def backtest_strategy(self, symbol, start_date, end_date):
        try:
            data = self.get_historical_data(symbol, '15m', 1000)
            trades = []
            for i in range(50, len(data), 15):
                subset = data.iloc[:i]
                score, details = self.calculate_momentum_score(symbol)
                if score >= self.momentum_score_threshold:
                    entry_price = subset.iloc[-1]['close']
                    if i + 10 < len(data):
                        exit_price = data.iloc[i+10]['close']
                        pnl = (exit_price - entry_price) / entry_price * 100
                        trades.append({'pnl': pnl, 'score': score})
            return {'total_trades': len(trades), 'average_pnl': np.mean([t['pnl'] for t in trades]) if trades else 0}
        except Exception as e:
            logger.error(f"خطأ في الباكتيست: {e}")
            return {}
    
    def shutdown(self):
        logger.info("🛑 إيقاف البوت...")
        for symbol in list(self.active_trades.keys()):
            current_price = self.get_current_price(symbol)
            if current_price:
                self.close_trade(symbol, current_price, 'shutdown')
        if self.notifier:
            self.notifier.send_message("🛑 <b>إيقاف البوت</b>", 'shutdown')
    
    # معدل: إعادة تدريب النموذج دوريًا
    def run_trading_cycle(self):
        try:
            logger.info("🔄 بدء دورة التداول الجديدة")
    
            if not self.health_monitor.check_connections():
                logger.warning("⚠️ مشاكل في الاتصال - تأجيل الدورة")
                return
    
            usdt_balance = self.auto_convert_stuck_assets()
    
            if usdt_balance < self.min_trade_size:
                logger.warning(f"رصيد USDT غير كافي: {usdt_balance:.2f}")
                return
    
            self.train_ml_model()  # إعادة تدريب نموذج XGBoost
    
            self.manage_active_trades()
    
            if len(self.active_trades) < 3:
                # Use asyncio.run to call the async method
                opportunities = asyncio.run(self.find_best_opportunities())
        
                if opportunities:
                    logger.info(f"🔍 تم العثور على {len(opportunities)} فرصة")
                    for opportunity in opportunities[:2]:
                        if opportunity['symbol'] not in self.active_trades:
                            self.execute_trade(opportunity['symbol'], opportunity)
                            time.sleep(1)
                else:
                    logger.info("🔍 لم يتم العثور على فرص مناسبة")
            else:
                logger.info(f"⏸️ عدد الصفقات النشطة ({len(self.active_trades)}) - تخطي البحث عن فرص جديدة")
    
            self.last_scan_time = datetime.now(damascus_tz)
            logger.info(f"✅ اكتملت دورة التداول في {self.last_scan_time}")
    
        except Exception as e:
            logger.error(f"❌ خطأ في دورة التداول: {e}")
            if self.notifier:
                self.notifier.send_message(f"❌ <b>خطأ في دورة التداول</b>\n{e}", 'error')
    
    def send_daily_report(self):
        try:
            stats = self.get_performance_stats()
            
            if not stats:
                return
                
            if self.notifier:
                message = (
                    f"📊 <b>تقرير أداء البوت</b>\n\n"
                    f"• إجمالي الصفقات: {stats['total_trades']}\n"
                    f"• نسبة النجاح: {stats['win_rate']}%\n"
                    f"• إجمالي الربح: ${stats['total_profit']:.2f}\n"
                    f"• إجمالي الخسارة: ${stats['total_loss']:.2f}\n"
                    f"• عامل الربح: {stats['profit_factor']:.2f}\n"
                    f"• الصفقات النشطة: {len(self.active_trades)}\n\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'daily_report')
                
        except Exception as e:
            logger.error(f"خطأ في إرسال التقرير اليومي: {e}")
    
    def run_bot(self):
        logger.info("🚀 بدء تشغيل البوت بشكل مستمر")
        
        if self.notifier:
            self.notifier.send_message("🚀 <b>بدء تشغيل البوت</b>\nتم تشغيل إستراتيجية الصعود المحسنة", 'startup')
        
        schedule.every(15).minutes.do(self.run_trading_cycle)
        
        schedule.every(5).minutes.do(self.health_monitor.check_connections)
        
        schedule.every(6).hours.do(self.send_daily_report)
        
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
