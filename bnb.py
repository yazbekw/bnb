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
            if not self.db:
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
        
        if self.telegram_token and self.telegram_chat_id:
            self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
        else:
            self.notifier = None
            
        self.health_monitor = HealthMonitor(self)
        
        # إعدادات التداول المتطورة
        self.symbols = self.get_all_trading_symbols()
        self.stable_coins = ['USDT', 'BUSD', 'USDC']
        self.min_daily_volume = 1000000  # 1M USD حجم يومي
        self.risk_per_trade = 50  # 50 USDT مخاطرة لكل صفقة
        self.max_position_size = 0.25  # 25% من الرصيد
        
        self.active_trades = {}
        self.last_scan_time = datetime.now()
        
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
            # المتوسطات المتحركة
            df['ema8'] = df['close'].ewm(span=8).mean()
            df['ema21'] = df['close'].ewm(span=21).mean()
            df['ema50'] = df['close'].ewm(span=50).mean()
            
            # RSI
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
            
            # حجم التداول
            df['volume_ma'] = df['volume'].rolling(window=20).mean()
            
            # ATR (Average True Range)
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            ranges = pd.concat([high_low, high_close, low_close], axis=1)
            true_range = np.max(ranges, axis=1)
            df['atr'] = true_range.rolling(window=14).mean()
            
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
            
            # حساب النقاط حسب الاستراتيجية المتطورة
            score = 0
            details = {}
            
            # 1. فلتر الاتجاه (30 نقطة)
            if latest['ema21'] > latest['ema50']:
                score += 30
                details['trend'] = 'صاعد'
            else:
                details['trend'] = 'هابط'
            
            # 2. السعر فوق EMA8 (25 نقطة)
            current_price = latest['close']
            if current_price > latest['ema8']:
                score += 25
                details['above_ema8'] = True
            else:
                details['above_ema8'] = False
            
            # 3. الزخم السعري (20 نقطة)
            price_change = ((current_price - data.iloc[-5]['close']) / data.iloc[-5]['close']) * 100
            details['price_change_5candles'] = round(price_change, 2)
            if price_change >= 1.5:
                score += 20
            
            # 4. حجم التداول (15 نقطة)
            volume_ratio = latest['volume'] / latest['volume_ma'] if latest['volume_ma'] > 0 else 1
            details['volume_ratio'] = round(volume_ratio, 2)
            if volume_ratio >= 1.8:
                score += 15
            
            # 5. RSI (10 نقطة)
            details['rsi'] = round(latest['rsi'], 2)
            if latest['rsi'] < 75:
                score += 10
            
            # معلومات إضافية
            details['current_price'] = current_price
            details['atr'] = latest['atr'] if not pd.isna(latest['atr']) else 0
            details['atr_percent'] = round((latest['atr'] / current_price) * 100, 2) if latest['atr'] > 0 else 0
            
            return score, details
            
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
                
                if momentum_score >= 60:
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
        حساب حجم الصفقة المتدرج حسب قوة الفرصة واحتمال نجاحها
        من 10% إلى 50% من رأس المال المتاح
        """
        try:
            score = opportunity['score']
            current_price = opportunity['details']['current_price']
            atr = opportunity['details']['atr']
        
            # التدرج في نسبة المخاطرة حسب قوة الصفقة
            if score >= 90:
                risk_percentage = 0.50  # 50% - فرصة استثنائية
                risk_level = "استثنائية 🚀"
            elif score >= 80:
                risk_percentage = 0.35  # 35% - فرصة قوية جداً
                risk_level = "قوية جداً 💪"
            elif score >= 70:
                risk_percentage = 0.25  # 25% - فرصة جيدة
                risk_level = "جيدة 👍"
            elif score >= 60:
                risk_percentage = 0.15  # 15% - فرصة متوسطة
                risk_level = "متوسطة 🔄"
            else:
                risk_percentage = 0.10  # 10% - فرصة عادية
                risk_level = "عادية ⚡"
        
            # حساب حجم الصفقة الأساسي
            base_position_size = usdt_balance * risk_percentage
        
            # تعديل إضافي حسب قوة المؤشرات
            adjustment_factor = 1.0
        
            # زيادة الحجم إذا كان الحجم قوي جداً
            if opportunity['details']['volume_ratio'] >= 2.5:
                adjustment_factor *= 1.2
                risk_level += " + حجم قوي"
        
            # زيادة الحجم إذا كان الاتجاه قوي
            if opportunity['details']['price_change_5candles'] >= 3.0:
                adjustment_factor *= 1.15
                risk_level += " + زخم قوي"
        
            # تقليل الحجم إذا كان ATR مرتفع (تقلبات عالية)
            if opportunity['details']['atr_percent'] >= 2.0:
                adjustment_factor *= 0.8
                risk_level += " - تقلبات عالية"
        
            # تقليل الحجم إذا كان RSI مرتفع
            if opportunity['details']['rsi'] >= 70:
                adjustment_factor *= 0.9
                risk_level += " - RSI مرتفع"
        
            # الحجم النهائي مع التعديلات
            final_position_size = base_position_size * adjustment_factor
        
            # الحدود الدنيا والعليا
            min_position = max(10, usdt_balance * 0.05)  # 5% كحد أدنى أو 10 USDT
            max_position = min(usdt_balance * 0.5, final_position_size)  # لا تتجاوز 50%
        
            final_position_size = max(min_position, min(max_position, final_position_size))
        
            # معلومات التقرير
            size_info = {
                'size_usdt': final_position_size,
                'risk_percentage': risk_percentage * 100,
                'adjustment_factor': adjustment_factor,
                'risk_level': risk_level,
                'base_size': base_position_size,
                'min_size': min_position,
                'max_size': max_position
            }
        
            logger.info(f"📊 حجم الصفقة لـ {opportunity['symbol']}: "
                       f"${final_position_size:.2f} ({risk_percentage*100:.1f}%) - "
                       f"التقييم: {risk_level}")
        
            return final_position_size, size_info
        
        except Exception as e:
            logger.error(f"خطأ في حساب حجم الصفقة: {e}")
            # العودة إلى قيمة افتراضية آمنة
            default_size = min(usdt_balance * 0.15, 100)
            return default_size, {'risk_level': 'افتراضي بسبب خطأ'}

    def get_symbol_precision(self, symbol):
        """الحصول على دقة الكمية والسعر للزوج"""
        try:
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
        
            # دقة الكمية
            lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            step_size = float(lot_size['stepSize']) if lot_size else 0.001
            qty_precision = len(str(step_size).split('.')[1]) if '.' in str(step_size) else 0
        
            # دقة السعر
            price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            tick_size = float(price_filter['tickSize']) if price_filter else 0.01
            price_precision = len(str(tick_size).split('.')[1]) if '.' in str(tick_size) else 0
        
            return {
                'quantity_precision': qty_precision,
                'price_precision': price_precision,
                'step_size': step_size,
                'tick_size': tick_size
            }
        
        except Exception as e:
            logger.error(f"خطأ في获取 دقة {symbol}: {e}")
            return {'quantity_precision': 6, 'price_precision': 2, 'step_size': 0.001, 'tick_size': 0.01}
    
    #然后在 execute_trade 中使用:
    def execute_trade(self, opportunity):
        symbol = opportunity['symbol']
        current_price = opportunity['details']['current_price']
        atr = opportunity['details']['atr']

        precision_info = self.get_symbol_precision(symbol)
        step_size = precision_info['step_size']
    
        # التقريب الصحيح
        quantity = (quantity // step_size) * step_size
        quantity = round(quantity, precision_info['quantity_precision'])
    
        try:
            if symbol in self.active_trades:
                logger.info(f"تخطي {symbol} - صفقة نشطة موجودة")
                return False
        
            balances = self.get_account_balance()
            usdt_balance = balances.get('USDT', {}).get('free', 0)
        
            if usdt_balance < 20:
                logger.warning("رصيد USDT غير كافي")
                return False
        
            # حساب حجم الصفقة المتدرج
            position_size_usdt, size_info = self.calculate_position_size(opportunity, usdt_balance)
        
            # حساب الكمية بناء على السعر
            quantity = position_size_usdt / current_price
        
            # التقريب حسب متطلبات Binance
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if lot_size:
                step_size = float(lot_size['stepSize'])
                quantity = round(quantity / step_size) * step_size
        
            # التاكد من أن الكمية لا تقل عن الحد الأدنى
            min_qty = float(next((f['minQty'] for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), 0))
            if quantity < min_qty:
                logger.warning(f"الكمية {quantity} أقل من الحد الأدنى {min_qty} لـ {symbol}")
                return False
        
            # حساب وقف الخسارة وأخذ الربح
            stop_loss_price = current_price - (atr * 1.5)
            take_profit_price = current_price + (2 * (current_price - stop_loss_price))
        
            # تنفيذ الأمر
            order = self.safe_binance_request(self.client.order_market_buy,
                                         symbol=symbol,
                                         quantity=quantity)
        
            trade_data = {
                'symbol': symbol,
                'type': 'buy',
                'quantity': quantity,
                'entry_price': current_price,
                'trade_size': quantity * current_price,
                'stop_loss': stop_loss_price,
                'take_profit': take_profit_price,
                'atr': atr,
                'position_size_usdt': position_size_usdt,
                'risk_percentage': size_info['risk_percentage'],
                'risk_level': size_info['risk_level'],
                'timestamp': datetime.now(),
                'status': 'open',
                'score': opportunity['score']
            }
        
            self.active_trades[symbol] = trade_data
            self.mongo_manager.save_trade(trade_data)
        
            if self.notifier:
                message = (
                    f"🚀 <b>صفقة جديدة - حجم متدرج</b>\n\n"
                    f"• العملة: {symbol}\n"
                    f"• السعر: ${current_price:.4f}\n"
                    f"• الكمية: {quantity:.6f}\n"
                    f"• الحجم: ${quantity * current_price:.2f}\n"
                    f"• النتيجة: {opportunity['score']}/100\n"
                    f"• مستوى المخاطرة: {size_info['risk_level']}\n"
                    f"• نسبة المخاطرة: {size_info['risk_percentage']:.1f}%\n"
                    f"• وقف الخسارة: ${stop_loss_price:.4f}\n"
                    f"• أخذ الربح: ${take_profit_price:.4f}\n"
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
                
                # التحقق من وقف الخسارة
                if current_price <= trade['stop_loss']:
                    self.close_trade(symbol, current_price, 'stop_loss')
                    continue
                
                # التحقق من أخذ الربح
                if current_price >= trade['take_profit']:
                    self.close_trade(symbol, current_price, 'take_profit')
                    continue
                
                # Trailing Stop (بعد تحقيق 3% ربح)
                profit_percent = ((current_price - trade['entry_price']) / trade['entry_price']) * 100
                if profit_percent >= 3:
                    new_sl = max(trade['stop_loss'], current_price - (trade['atr'] * 1.2))
                    if new_sl > trade['stop_loss']:
                        trade['stop_loss'] = new_sl
                        logger.info(f"تم تحديث وقف الخسارة لـ {symbol} إلى ${new_sl:.4f}")
                        
            except Exception as e:
                logger.error(f"خطأ في إدارة صفقة {symbol}: {e}")
    
    def close_trade(self, symbol, exit_price, reason):
        try:
            trade = self.active_trades[symbol]
            
            pnl = (exit_price - trade['entry_price']) * trade['quantity']
            pnl_percent = (exit_price / trade['entry_price'] - 1) * 100
            
            trade['exit_price'] = exit_price
            trade['exit_time'] = datetime.now()
            trade['profit_loss'] = pnl
            trade['pnl_percent'] = pnl_percent
            trade['status'] = 'completed'
            trade['exit_reason'] = reason
            
            self.mongo_manager.save_trade(trade)
            
            if self.notifier:
                emoji = "✅" if pnl > 0 else "❌"
                message = (
                    f"{emoji} <b>إغلاق الصفقة</b>\n\n"
                    f"• العملة: {symbol}\n"
                    f"• السبب: {reason}\n"
                    f"• السعر: ${exit_price:.4f}\n"
                    f"• الربح/الخسارة: ${pnl:.2f} ({pnl_percent:+.2f}%)\n"
                    f"• المدة: {(trade['exit_time'] - trade['timestamp']).total_seconds() / 60:.1f} دقيقة\n"
                    f"• المخاطرة: ${trade['risk_amount']:.2f}\n\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_close')
            
            logger.info(f"تم إغلاق {symbol} بـ {reason}: ${pnl:.2f}")
            del self.active_trades[symbol]
            
        except Exception as e:
            logger.error(f"خطأ في إغلاق صفقة {symbol}: {e}")
    
    def auto_convert_stuck_assets(self):
        try:
            balances = self.get_account_balance()
            usdt_value = 0
            
            for asset, balance in balances.items():
                if asset in self.stable_coins:
                    if asset != 'USDT':
                        if balance['free'] > 1:
                            self.convert_to_usdt(asset, balance['free'])
                    else:
                        usdt_value = balance['free']
                elif balance['free'] > 0.0001:
                    current_price = self.get_current_price(asset + 'USDT')
                    if current_price:
                        asset_value = balance['free'] * current_price
                        if asset_value > 5:
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
            
            order = self.safe_binance_request(self.client.order_market_sell,
                                             symbol=symbol,
                                             quantity=amount)
            
            logger.info(f"تم تحويل {amount} {asset} إلى USDT")
            return True
            
        except Exception as e:
            logger.error(f"خطأ في تحويل {asset} إلى USDT: {e}")
            return False

    def send_detailed_scan_report(self, opportunities, usdt_balance, scan_duration):
        """إرسال تقرير مفصل عن دورة المسح إلى التلغرام"""
        if not self.notifier or not opportunities:
            return
    
        try:
            total_opportunities = len(opportunities)
            best_opportunity = opportunities[0]
            worst_opportunity = opportunities[-1] if total_opportunities > 1 else best_opportunity
        
            # تحضير الرسالة المفصلة
            message = f"📊 <b>تقرير دورة المسح - البوت المتقدم</b>\n\n"
        
            # معلومات عامة
            message += f"⏰ <b>مدة المسح:</b> {scan_duration:.1f} ثانية\n"
            message += f"💼 <b>الرصيد المتاح:</b> ${usdt_balance:.2f}\n"
            message += f"🎯 <b>عدد الفرص:</b> {total_opportunities} فرصة\n"
            message += f"🔍 <b>عدد العملات الممسوحة:</b> {len(self.symbols)} عملة\n\n"
        
            # أفضل فرصة
            message += f"🚀 <b>أفضل فرصة:</b>\n"
            message += f"   • {best_opportunity['symbol']} - قوة: {best_opportunity['score']}/100\n"
            message += f"   • السعر: ${best_opportunity['details']['current_price']:.4f}\n"
            message += f"   • التغير: +{best_opportunity['details']['price_change_5candles']:.2f}%\n"
            message += f"   • الحجم: {best_opportunity['details']['volume_ratio']:.1f}x\n"
            message += f"   • RSI: {best_opportunity['details']['rsi']:.1f}\n"
            message += f"   • الاتجاه: {best_opportunity['details']['trend']}\n\n"
        
            # أفضل 5 فرص
            message += f"🏆 <b>أفضل 5 فرص:</b>\n"
            for i, opp in enumerate(opportunities[:5]):
                emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else "🔸"
                message += f"   {emoji} {opp['symbol']}: {opp['score']}/100 "
                message += f"(Δ{opp['details']['price_change_5candles']:+.1f}%)\n"
        
            message += f"\n"
        
            # توزيع النقاط
            score_distribution = {
                "ممتاز (90+)": len([o for o in opportunities if o['score'] >= 90]),
                "قوي (80-89)": len([o for o in opportunities if 80 <= o['score'] < 90]),
                "جيد (70-79)": len([o for o in opportunities if 70 <= o['score'] < 80]),
                "متوسط (60-69)": len([o for o in opportunities if 60 <= o['score'] < 70]),
                "ضعيف (<60)": len([o for o in opportunities if o['score'] < 60])
            }
        
            message += f"📈 <b>توزيع النقاط:</b>\n"
            for category, count in score_distribution.items():
                if count > 0:
                    message += f"   • {category}: {count} فرصة\n"
        
            message += f"\n"
        
            # الصفقات النشطة
            active_trades_count = len(self.active_trades)
            if active_trades_count > 0:
                message += f"📦 <b>الصفقات النشطة:</b> {active_trades_count}\n"
                for symbol, trade in list(self.active_trades.items())[:3]:
                    profit_pct = ((trade['entry_price'] - trade['entry_price']) / trade['entry_price']) * 100
                    message += f"   • {symbol}: {profit_pct:+.1f}%\n"
        
            # إحصائيات الأداء
            stats = self.get_performance_stats()
            if stats and stats.get('total_trades', 0) > 0:
                message += f"\n"
                message += f"📊 <b>إحصائيات الأداء:</b>\n"
                message += f"   • إجمالي الصفقات: {stats['total_trades']}\n"
                message += f"   • نسبة النجاح: {stats['win_rate']}%\n"
                message += f"   • صافي الربح: ${stats['total_profit']:.2f}\n"
        
            message += f"\n"
            message += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
            # إرسال الرسالة
            self.notifier.send_message(message, 'scan_report')
        
            logger.info(f"✅ تم إرسال تقرير المسح إلى التلغرام: {total_opportunities} فرصة")
        
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال تقرير المسح: {e}")

    # ثم في run_scan_cycle أضف:
    def run_scan_cycle(self):
        start_time = time.time()
        try:
            logger.info("🔍 بدء دورة المسح المتقدمة...")
        
            usdt_balance = self.auto_convert_stuck_assets()
            logger.info(f"🔸 الرصيد المتاح: {usdt_balance:.2f} USDT")
        
            opportunities = self.find_best_opportunities()
        
            # حساب مدة المسح
            scan_duration = time.time() - start_time
        
            # إرسال التقرير المفصل
            self.send_detailed_scan_report(opportunities, usdt_balance, scan_duration)
        
            if opportunities:
                best_opportunity = opportunities[0]
                logger.info(f"أفضل فرصة: {best_opportunity['symbol']} - قوة: {best_opportunity['score']}/100")
            
                if best_opportunity['score'] >= 70 and usdt_balance > 20:
                    self.execute_trade(best_opportunity)
        
            self.manage_active_trades()
            self.health_monitor.check_connections()
        
            logger.info(f"✅ اكتملت دورة المسح. الفرص الموجودة: {len(opportunities)}")
        
        except Exception as e:
            logger.error(f"❌ خطأ في دورة المسح: {e}")
    
    
    def get_performance_stats(self):
        return self.mongo_manager.get_performance_stats()
    
    def get_current_opportunities(self):
        opportunities = self.find_best_opportunities()
        return {'opportunities': opportunities, 'timestamp': datetime.now()}
    
    def start_trading(self):
        if self.notifier:
            self.notifier.send_message(
                "🚀 <b>بدء تشغيل البوت المتقدم</b>\n\n"
                "✅ البوت يعمل بالاستراتيجية المتطورة\n"
                "⏰ دورة المسح: كل 5 دقائق\n"
                "🎯 فلتر متعدد الطبقات: اتجاه + زخم + حجم\n"
                "🛡️ إدارة مخاطر بـ ATR ديناميكي\n"
                "💰 مخاطرة ثابتة: 50 USDT/صفقة\n"
                "📊 نسبة عائد:خطر 2:1",
                'bot_start'
            )
        
        logger.info("🚀 بدء تشغيل بوت صائد الصاعدات المتقدم")
        
        schedule.every(5).minutes.do(self.run_scan_cycle)
        self.run_scan_cycle()
        
        while True:
            try:
                schedule.run_pending()
                time.sleep(60)
            except Exception as e:
                logger.error(f"خطأ في التشغيل الرئيسي: {e}")
                time.sleep(300)

def main():
    try:
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        logger.info("تم بدء خادم Flask للرصد الصحي")
        
        bot = MomentumHunterBot()
        bot.start_trading()
        
    except Exception as e:
        logger.error(f"❌ خطأ في الدالة الرئيسية: {e}")
        if 'bot' in locals() and hasattr(bot, 'notifier') and bot.notifier:
            bot.notifier.send_message(f"❌ <b>فشل تشغيل البوت:</b>\n{str(e)}", 'error')

if __name__ == "__main__":
    main()
