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
from flask import Flask, jsonify
import concurrent.futures

# تحميل متغيرات البيئة
load_dotenv()

# إنشاء تطبيق Flask للرصد الصحي
app = Flask(__name__)

@app.route('/')
def health_check():
    return {'status': 'healthy', 'service': 'momentum-hunter-bot', 'timestamp': datetime.now().isoformat()}

@app.route('/stats')
def stats():
    try:
        bot = MomentumHunterBot()
        stats = bot.get_performance_stats()
        return jsonify(stats)
    except Exception as e:
        return {'error': str(e)}

@app.route('/opportunities')
def opportunities():
    try:
        bot = MomentumHunterBot()
        opportunities = bot.get_current_opportunities()
        return jsonify(opportunities)
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

class TechnicalIndicators:
    @staticmethod
    def EMA(data, period):
        """حساب المتوسط المتحرك الأسي"""
        return data.ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def SMA(data, period):
        """حساب المتوسط المتحرك البسيط"""
        return data.rolling(window=period).mean()
    
    @staticmethod
    def RSI(data, period=14):
        """حساب مؤشر RSI"""
        delta = data.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    @staticmethod
    def MACD(data, fast_period=12, slow_period=26, signal_period=9):
        """حساب مؤشر MACD"""
        ema_fast = data.ewm(span=fast_period, adjust=False).mean()
        ema_slow = data.ewm(span=slow_period, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal = macd.ewm(span=signal_period, adjust=False).mean()
        histogram = macd - signal
        return macd, signal, histogram
    
    @staticmethod
    def ATR(high, low, close, period=14):
        """حساب Average True Range"""
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        return atr
    
    @staticmethod
    def BollingerBands(data, period=20, nbdev=2):
        """حساب Bollinger Bands"""
        middle_band = data.rolling(window=period).mean()
        std = data.rolling(window=period).std()
        upper_band = middle_band + (std * nbdev)
        lower_band = middle_band - (std * nbdev)
        return upper_band, middle_band, lower_band

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.last_notifications = {}
    
    def send_message(self, message, message_type='info'):
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

class RequestManager:
    def __init__(self):
        self.request_count = 0
        self.last_request_time = time.time()
        self.max_requests_per_minute = 1100
        self.request_lock = threading.Lock()
        
    def safe_request(self, func, *args, **kwargs):
        with self.request_lock:
            current_time = time.time()
            elapsed = current_time - self.last_request_time
            
            if elapsed < 0.5:
                time.sleep(0.5 - elapsed)
            
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
            logger.info(f"✅ تم العثور على رابط MongoDB")
        else:
            logger.warning("❌ لم يتم العثور على رابط MongoDB")
            
        self.client = None
        self.db = None
        self.connect()
        
    def connect(self):
        try:
            if not self.connection_string:
                return False
                
            self.client = MongoClient(self.connection_string, serverSelectionTimeoutMS=5000)
            self.client.admin.command('ping')
            self.db = self.client['momentum_hunter_bot']
            logger.info("✅ تم الاتصال بـ MongoDB بنجاح")
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ MongoDB: {e}")
            return False
    
    def save_trade(self, trade_data):
        try:
            if not self.db:
                return False
            collection = self.db['trades']
            trade_data['timestamp'] = datetime.now()
            result = collection.insert_one(trade_data)
            return True
        except Exception as e:
            logger.error(f"خطأ في حفظ الصفقة: {e}")
            return False
    
    def save_opportunity(self, opportunity):
        try:
            if not self.db:
                return False
            collection = self.db['opportunities']
            opportunity['scanned_at'] = datetime.now()
            collection.insert_one(opportunity)
            return True
        except Exception as e:
            logger.error(f"خطأ في حفظ الفرصة: {e}")
            return False
    
    def get_performance_stats(self):
        try:
            if self.db is None:
                return {}
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
        except Exception as e:
            logger.error(f"خطأ في جلب الإحصائيات: {e}")
            return {}

class HealthMonitor:
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.error_count = 0
        self.max_errors = 10
        self.last_health_check = datetime.now()
        
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
    def __init__(self):
        self.api_key = os.environ.get('BINANCE_API_KEY')
        self.api_secret = os.environ.get('BINANCE_API_SECRET')
        self.telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')
        
        if not all([self.api_key, self.api_secret]):
            raise ValueError("مفاتيح Binance مطلوبة")
            
        self.client = Client(self.api_key, self.api_secret)
        self.request_manager = RequestManager()
        self.mongo_manager = MongoManager()
        self.tech_indicators = TechnicalIndicators()
        
        if self.telegram_token and self.telegram_chat_id:
            self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
        else:
            self.notifier = None
            
        self.health_monitor = HealthMonitor(self)
        
        # إعدادات التداول المعدلة لحجم صفقة صغير
        self.symbols = self.get_all_trading_symbols()
        self.stable_coins = ['USDT', 'BUSD', 'USDC']
        self.min_daily_volume = 5000000  # 5M USD حجم يومي
        self.min_trade_size = 20  # الحد الأدنى للصفقة: 20 دولار
        self.max_trade_size = 50  # الحد الأقصى للصفقة: 50 دولار
        self.risk_per_trade = 2.0  # 2% مخاطرة لكل صفقة
        self.max_position_size = 0.15  # 15% من الرصيد كحد أقصى
        
        self.active_trades = {}
        self.last_scan_time = datetime.now()
        self.min_profit_threshold = 0.005  # 0.5% كحد أدنى للربح بعد الرسوم
        
        logger.info("✅ تم تهيئة بوت صائد الصاعدات المتقدم بنجاح")

    def get_all_trading_symbols(self):
        important_symbols = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
            "AVAXUSDT", "XLMUSDT", "SUIUSDT", "TONUSDT", "WLDUSDT",
            "ADAUSDT", "DOTUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT",
            "DOGEUSDT", "MATICUSDT", "ATOMUSDT", "NEARUSDT", "FILUSDT",
            "INJUSDT", "RUNEUSDT", "APTUSDT", "ARBUSDT", "OPUSDT"
        ]
        
        logger.info(f"🔸 استخدام القائمة المخصصة: {len(important_symbols)} عملة")
        return important_symbols
        
    def safe_binance_request(self, func, *args, **kwargs):
        return self.request_manager.safe_request(func, *args, **kwargs)
    
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
        try:
            ticker = self.safe_binance_request(self.client.get_symbol_ticker, symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            logger.error(f"خطأ في جلب سعر {symbol}: {e}")
            return None
    
    def get_historical_data(self, symbol, interval='15m', limit=100):
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
            return pd.DataFrame(data)
        except Exception as e:
            logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
            return None
    
    def calculate_technical_indicators(self, data):
        try:
            df = data.copy()
            if len(df) < 50:
                return df
                
            # المتوسطات المتحركة باستخدام pandas
            df['ema8'] = self.tech_indicators.EMA(df['close'], 8)
            df['ema21'] = self.tech_indicators.EMA(df['close'], 21)
            df['ema50'] = self.tech_indicators.EMA(df['close'], 50)
            df['ema100'] = self.tech_indicators.EMA(df['close'], 100)
            
            # RSI
            df['rsi'] = self.tech_indicators.RSI(df['close'], 14)
            
            # MACD
            macd, macd_signal, macd_hist = self.tech_indicators.MACD(df['close'])
            df['macd'] = macd
            df['macd_signal'] = macd_signal
            df['macd_hist'] = macd_hist
            
            # حجم التداول
            df['volume_ma'] = self.tech_indicators.SMA(df['volume'], 20)
            df['volume_ratio'] = df['volume'] / df['volume_ma']
            
            # ATR (Average True Range)
            df['atr'] = self.tech_indicators.ATR(df['high'], df['low'], df['close'], 14)
            
            # Bollinger Bands
            upper_bb, middle_bb, lower_bb = self.tech_indicators.BollingerBands(df['close'])
            df['upper_bb'] = upper_bb
            df['middle_bb'] = middle_bb
            df['lower_bb'] = lower_bb
            
            return df
        except Exception as e:
            logger.error(f"خطأ في حساب المؤشرات: {e}")
            return data
    
    def calculate_momentum_score(self, symbol):
        try:
            data = self.get_historical_data(symbol, '15m', 100)
            if data is None or len(data) < 50:
                return 0, {}
            
            data = self.calculate_technical_indicators(data)
            latest = data.iloc[-1]
            prev = data.iloc[-2]
            
            # حساب النقاط حسب الاستراتيجية المتطورة
            score = 0
            details = {}
            
            # 1. فلتر الاتجاه الرئيسي (25 نقطة)
            if latest['ema21'] > latest['ema50'] and latest['ema50'] > latest['ema100']:
                score += 25
                details['trend'] = 'صاعد قوي'
            elif latest['ema21'] > latest['ema50']:
                score += 15
                details['trend'] = 'صاعد'
            else:
                details['trend'] = 'هابط'
                return 0, details  # لا تتداول في الاتجاه الهابط
            
            # 2. تقاطع المتوسطات (20 نقطة)
            if latest['ema8'] > latest['ema21'] and prev['ema8'] <= prev['ema21']:
                score += 20
                details['crossover'] = 'إيجابي'
            
            # 3. الزخم السعري (15 نقطة)
            price_change_5 = ((latest['close'] - data.iloc[-5]['close']) / data.iloc[-5]['close']) * 100
            price_change_15 = ((latest['close'] - data.iloc[-15]['close']) / data.iloc[-15]['close']) * 100
            
            details['price_change_5candles'] = round(price_change_5, 2)
            details['price_change_15candles'] = round(price_change_15, 2)
            
            if price_change_5 >= 2.0 and price_change_15 >= 3.0:
                score += 15
            
            # 4. حجم التداول (15 نقطة)
            volume_ratio = latest['volume_ratio']
            details['volume_ratio'] = round(volume_ratio, 2) if not pd.isna(volume_ratio) else 1
            
            if volume_ratio >= 1.8:
                score += 15
            
            # 5. RSI (10 نقطة)
            details['rsi'] = round(latest['rsi'], 2) if not pd.isna(latest['rsi']) else 50
            
            if 40 <= latest['rsi'] <= 65:
                score += 10
            
            # 6. MACD (10 نقطة)
            if latest['macd'] > latest['macd_signal'] and latest['macd_hist'] > 0:
                score += 10
                details['macd'] = 'إيجابي'
            
            # 7. مؤشر البولنجر (5 نقطة)
            if latest['close'] > latest['middle_bb']:
                score += 5
                details['bollinger'] = 'فوق المتوسط'
            
            # معلومات إضافية
            details['current_price'] = latest['close']
            details['atr'] = latest['atr'] if not pd.isna(latest['atr']) else 0
            details['atr_percent'] = round((latest['atr'] / latest['close']) * 100, 2) if latest['atr'] > 0 else 0
            
            return min(score, 100), details
            
        except Exception as e:
            logger.error(f"خطأ في حساب زخم {symbol}: {e}")
            return 0, {}
    
    def find_best_opportunities(self):
        opportunities = []
        
        def process_symbol(symbol):
            try:
                # الفحص السريع للحجم اليومي
                ticker = self.safe_binance_request(self.client.get_ticker, symbol=symbol)
                daily_volume = float(ticker['volume']) * float(ticker['lastPrice'])
                
                if daily_volume < self.min_daily_volume:
                    return None
                
                # التحليل التقني المتقدم
                momentum_score, details = self.calculate_momentum_score(symbol)
                
                if momentum_score >= 75:  # زيادة الحد الأدنى إلى 75 نقطة
                    opportunity = {
                        'symbol': symbol,
                        'score': momentum_score,
                        'details': details,
                        'daily_volume': daily_volume,
                        'timestamp': datetime.now()
                    }
                    return opportunity
                    
            except Exception as e:
                logger.error(f"خطأ في تحليل {symbol}: {e}")
            return None
        
        # المعالجة المتوازية
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            results = list(executor.map(process_symbol, self.symbols))
        
        opportunities = [result for result in results if result is not None]
        opportunities.sort(key=lambda x: x['score'], reverse=True)
        
        return opportunities
    
    def calculate_position_size(self, opportunity, usdt_balance):
        """
        حساب حجم الصفقة بين 20 و 50 دولار فقط
        """
        try:
            score = opportunity['score']
            current_price = opportunity['details']['current_price']
            atr = opportunity['details']['atr']
        
            # تحديد حجم الصفقة الثابت بين 20-50 دولار
            if score >= 90:
                position_size_usdt = 50  # 50 دولار للفرص الاستثنائية
                risk_level = "استثنائية 🚀"
            elif score >= 85:
                position_size_usdt = 45
                risk_level = "قوية جداً 💪"
            elif score >= 80:
                position_size_usdt = 40
                risk_level = "قوية 👍"
            elif score >= 75:
                position_size_usdt = 35
                risk_level = "جيدة 🔄"
            else:
                return 0, {'risk_level': 'ضعيفة - لا تتداول'}
        
            # التأكد من أن الحجم لا يقل عن الحد الأدنى ولا يتجاوز الحد الأقصى
            position_size_usdt = max(self.min_trade_size, min(self.max_trade_size, position_size_usdt))
            
            # حساب نسبة المخاطرة الفعلية
            risk_percentage = (position_size_usdt / usdt_balance) * 100 if usdt_balance > 0 else 0
            
            # التأكد من أن الصفقة يمكن أن تحقق ربحاً بعد الرسوم
            min_profit_needed = position_size_usdt * self.min_profit_threshold
            potential_profit = (opportunity['details'].get('price_change_5candles', 0) / 100) * position_size_usdt
            
            if potential_profit < min_profit_needed:
                logger.info(f"تخطي {opportunity['symbol']} - الربح المتوقع {potential_profit:.2f} أقل من الحد الأدنى {min_profit_needed:.2f}")
                return 0, {'risk_level': 'ربح غير كافي'}
        
            size_info = {
                'size_usdt': position_size_usdt,
                'risk_percentage': round(risk_percentage, 2),
                'risk_level': risk_level,
                'min_trade_size': self.min_trade_size,
                'max_trade_size': self.max_trade_size
            }
        
            logger.info(f"📊 حجم الصفقة لـ {opportunity['symbol']}: "
                       f"${position_size_usdt:.2f} ({risk_percentage:.1f}%) - "
                       f"التقييم: {risk_level}")
        
            return position_size_usdt, size_info
        
        except Exception as e:
            logger.error(f"خطأ في حساب حجم الصفقة: {e}")
            return 0, {'risk_level': 'خطأ في الحساب'}

    def get_symbol_precision(self, symbol):
        """الحصول على دقة الكمية والسعر للزوج"""
        try:
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            if not symbol_info:
                return {'quantity_precision': 6, 'price_precision': 2, 'step_size': 0.001, 'tick_size': 0.01}
        
            # دقة الكمية
            lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            step_size = float(lot_size['stepSize']) if lot_size else 0.001
            qty_precision = int(round(-np.log10(step_size))) if step_size < 1 else 0
        
            # دقة السعر
            price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            tick_size = float(price_filter['tickSize']) if price_filter else 0.01
            price_precision = int(round(-np.log10(tick_size))) if tick_size < 1 else 0
        
            return {
                'quantity_precision': qty_precision,
                'price_precision': price_precision,
                'step_size': step_size,
                'tick_size': tick_size
            }
        
        except Exception as e:
            logger.error(f"خطأ في获取 دقة {symbol}: {e}")
            return {'quantity_precision': 6, 'price_precision': 2, 'step_size': 0.001, 'tick_size': 0.01}
    
    def execute_trade(self, opportunity):
        symbol = opportunity['symbol']
        current_price = opportunity['details']['current_price']
        atr = opportunity['details']['atr']
        
        try:
            if symbol in self.active_trades:
                logger.info(f"تخطي {symbol} - صفقة نشطة موجودة")
                return False
        
            balances = self.get_account_balance()
            usdt_balance = balances.get('USDT', {}).get('free', 0)
        
            if usdt_balance < self.min_trade_size:
                logger.warning(f"رصيد USDT غير كافي: {usdt_balance:.2f} < {self.min_trade_size}")
                return False
        
            # حساب حجم الصفقة بين 20-50 دولار
            position_size_usdt, size_info = self.calculate_position_size(opportunity, usdt_balance)
            
            if position_size_usdt < self.min_trade_size:
                logger.info(f"تخطي {symbol} - حجم الصفقة صغير: {position_size_usdt:.2f}")
                return False
        
            # حساب الكمية بناء على السعر
            quantity = position_size_usdt / current_price
        
            # التقريب حسب متطلبات Binance
            precision_info = self.get_symbol_precision(symbol)
            step_size = precision_info['step_size']
            quantity = (quantity // step_size) * step_size
            quantity = round(quantity, precision_info['quantity_precision'])
        
            # التاكد من أن الكمية لا تقل عن الحد الأدنى
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            if not symbol_info:
                return False
                
            lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if lot_size:
                min_qty = float(lot_size['minQty'])
                if quantity < min_qty:
                    logger.warning(f"الكمية {quantity} أقل من الحد الأدنى {min_qty} لـ {symbol}")
                    return False
        
            # حساب وقف الخسارة وأخذ الربح مع هوامش أكبر
            stop_loss_price = current_price - (atr * 2.0)  # زيادة الهامش إلى 2 ATR
            take_profit_price = current_price + (3.0 * (current_price - stop_loss_price))  # نسبة 3:1
        
            # تنفيذ الأمر
            order = self.safe_binance_request(self.client.order_market_buy,
                                         symbol=symbol,
                                         quantity=quantity)
        
            if not order or order['status'] != 'FILLED':
                logger.error(f"فشل تنفيذ أمر الشراء لـ {symbol}")
                return False
                
            # الحصول على سعر التنفيذ الفعلي
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
                'order_id': order['orderId']
            }
        
            self.active_trades[symbol] = trade_data
            self.mongo_manager.save_trade(trade_data)
        
            if self.notifier:
                message = (
                    f"🚀 <b>صفقة جديدة - حجم صغير</b>\n\n"
                    f"• العملة: {symbol}\n"
                    f"• السعر: ${avg_fill_price:.4f}\n"
                    f"• الكمية: {quantity:.6f}\n"
                    f"• الحجم: ${quantity * avg_fill_price:.2f}\n"
                    f"• النتيجة: {opportunity['score']}/100\n"
                    f"• مستوى المخاطرة: {size_info.get('risk_level', '')}\n"
                    f"• نسبة المخاطرة: {size_info.get('risk_percentage', 0):.1f}%\n"
                    f"• وقف الخسارة: ${stop_loss_price:.4f}\n"
                    f"• أخذ الربح: ${take_profit_price:.4f}\n"
                    f"• نسبة العائد: 3:1\n"
                    f"• ATR: {opportunity['details']['atr_percent']}%\n\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_execution')
        
            logger.info(f"✅ تم شراء {symbol} - الحجم: ${position_size_usdt:.2f}")
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
                
                # حساب الربح/الخسارة مع مراعاة الرسوم (تقريباً 0.1%)
                estimated_fees = trade['trade_size'] * 0.001
                net_pnl = ((current_price - trade['entry_price']) * trade['quantity']) - estimated_fees
                net_pnl_percent = (net_pnl / trade['trade_size']) * 100
                
                # التحقق من وقف الخسارة المعدل
                if current_price <= trade['stop_loss']:
                    self.close_trade(symbol, current_price, 'stop_loss')
                    continue
                
                # التحقق من أخذ الربح مع هامش ربح صافي
                if current_price >= trade['take_profit'] and net_pnl_percent >= 1.0:
                    self.close_trade(symbol, current_price, 'take_profit')
                    continue
                
                # Trailing Stop (بعد تحقيق 5% ربح صافي)
                if net_pnl_percent >= 5.0:
                    new_sl = max(trade['stop_loss'], current_price - (trade['atr'] * 1.5))
                    if new_sl > trade['stop_loss']:
                        trade['stop_loss'] = new_sl
                        logger.info(f"تم تحديث وقف الخسارة لـ {symbol} إلى ${new_sl:.4f}")
                        
                # إغلاق الصفقات التي لم تتحرك بعد فترة
                trade_duration = (datetime.now() - trade['timestamp']).total_seconds() / 3600
                if trade_duration > 4 and net_pnl_percent < 0.5:  # 4 ساعات بدون حركة
                    self.close_trade(symbol, current_price, 'timeout')
                    continue
                        
            except Exception as e:
                logger.error(f"خطأ في إدارة صفقة {symbol}: {e}")
    
    def close_trade(self, symbol, exit_price, reason):
        try:
            trade = self.active_trades[symbol]
            
            # حساب الربح/الخسارة مع الرسوم
            gross_pnl = (exit_price - trade['entry_price']) * trade['quantity']
            estimated_fees = trade['trade_size'] * 0.002  # 0.1% للشراء + 0.1% للبيع
            net_pnl = gross_pnl - estimated_fees
            pnl_percent = (net_pnl / trade['trade_size']) * 100
            
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
                    f"• السبب: {reason}\n"
                    f"• سعر الدخول: ${trade['entry_price']:.4f}\n"
                    f"• سعر الخروج: ${exit_price:.4f}\n"
                    f"• الربح/الخسارة: ${net_pnl:.2f} ({pnl_percent:+.2f}%)\n"
                    f"• الرسوم التقديرية: ${estimated_fees:.2f}\n"
                    f"• المدة: {(trade['exit_time'] - trade['timestamp']).total_seconds() / 60:.1f} دقيقة\n"
                    f"• المخاطرة: ${trade['position_size_usdt']:.2f}\n\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_close')
            
            logger.info(f"تم إغلاق {symbol} بـ {reason}: ${net_pnl:.2f} ({pnl_percent:+.2f}%)")
            del self.active_trades[symbol]
            
        except Exception as e:
            logger.error(f"خطأ في إغلاق صفقة {symbol}: {e}")
    
    def auto_convert_stuck_assets(self):
        try:
            balances = self.get_account_balance()
            usdt_value = balances.get('USDT', {}).get('free', 0)
            
            for asset, balance in balances.items():
                if asset in self.stable_coins and asset != 'USDT':
                    if balance['free'] > 10:  # تحويل فقط إذا كان المبلغ كبير enough
                        self.convert_to_usdt(asset, balance['free'])
                elif asset not in self.stable_coins and balance['free'] > 0:
                    # تحويل العملات الصغيرة فقط إذا كانت قيمتها كبيرة
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
            
            # التقريب حسب متطلبات Binance
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
        opportunities = []
        for symbol in self.symbols[:10]:  # أول 10 عملات فقط للعرض
            try:
                momentum_score, details = self.calculate_momentum_score(symbol)
                if momentum_score > 0:
                    opportunities.append({
                        'symbol': symbol,
                        'score': momentum_score,
                        'details': details
                    })
            except:
                continue
        return sorted(opportunities, key=lambda x: x['score'], reverse=True)
    
    def run(self):
        logger.info("🚀 بدأ تشغيل بوت صائد الصاعدات المتقدم")
        
        if self.notifier:
            self.notifier.send_message("🚀 <b>تم تشغيل البوت بنجاح</b>\n"
                                     f"• حجم الصفقة: ${self.min_trade_size}-${self.max_trade_size}\n"
                                     f"• عدد العملات: {len(self.symbols)}\n"
                                     f"• الحد الأدنى للربح: {self.min_profit_threshold*100}%\n"
                                     f"• ⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 
                                     'startup')
        
        while True:
            try:
                # فحص الصحة كل 5 دقائق
                if (datetime.now() - self.health_monitor.last_health_check).total_seconds() >= 300:
                    self.health_monitor.check_connections()
                    self.health_monitor.last_health_check = datetime.now()
                
                # تحويل الأصول العالقة كل 30 دقيقة
                if (datetime.now().minute % 30 == 0):
                    usdt_balance = self.auto_convert_stuck_assets()
                    logger.info(f"💰 الرصيد الحالي: ${usdt_balance:.2f}")
                
                # البحث عن فرص التداول كل دقيقة
                if (datetime.now() - self.last_scan_time).total_seconds() >= 60:
                    opportunities = self.find_best_opportunities()
                    self.last_scan_time = datetime.now()
                    
                    if opportunities:
                        logger.info(f"🔍 تم العثور على {len(opportunities)} فرصة")
                        
                        # حفظ أفضل الفرص في قاعدة البيانات
                        for opp in opportunities[:3]:
                            self.mongo_manager.save_opportunity(opp)
                        
                        # تنفيذ أفضل فرصة فقط (لتجنب المخاطرة الزائدة)
                        best_opportunity = opportunities[0]
                        if best_opportunity['score'] >= 75:
                            self.execute_trade(best_opportunity)
                
                # إدارة الصفقات النشطة كل 30 ثانية
                if len(self.active_trades) > 0:
                    self.manage_active_trades()
                
                # إرسال تحديثات الأداء كل ساعة
                if datetime.now().minute == 0:
                    stats = self.get_performance_stats()
                    if stats and self.notifier:
                        message = (
                            f"📊 <b>تقرير الأداء الساعي</b>\n\n"
                            f"• إجمالي الصفقات: {stats['total_trades']}\n"
                            f"• نسبة النجاح: {stats['win_rate']}%\n"
                            f"• إجمالي الربح: ${stats['total_profit']:.2f}\n"
                            f"• إجمالي الخسارة: ${stats['total_loss']:.2f}\n"
                            f"• عامل الربح: {stats['profit_factor']:.2f}\n"
                            f"• الصفقات النشطة: {len(self.active_trades)}\n\n"
                            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                        )
                        self.notifier.send_message(message, 'performance')
                
                time.sleep(30)
                
            except Exception as e:
                logger.error(f"❌ خطأ غير متوقع في الدورة الرئيسية: {e}")
                time.sleep(60)

def main():
    try:
        # تشغيل خادم Flask في خيط منفصل
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        # تشغيل البوت
        bot = MomentumHunterBot()
        bot.run()
        
    except Exception as e:
        logger.error(f"❌ فشل تشغيل البوت: {e}")
        if 'bot' in locals() and bot.notifier:
            bot.notifier.send_message(f"❌ <b>فشل تشغيل البوت</b>\n{e}", 'error')
        time.sleep(60)
        os._exit(1)

if __name__ == "__main__":
    main()
