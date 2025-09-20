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
import schedule
from flask import Flask, jsonify, request
import concurrent.futures
import pytz
from cachetools import TTLCache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import aiohttp
import asyncio
from xgboost import XGBClassifier

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

            if elapsed < 0.2:  # زيادة التأخير إلى 200 مللي ثانية
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

class FileStorageManager:
    """مدير تخزين محلي بدلاً من MongoDB"""
    def __init__(self, data_dir='data'):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.trades_file = os.path.join(data_dir, 'trades.json')
        self.performance_file = os.path.join(data_dir, 'performance.json')
        self.initialize_storage()
        
    def initialize_storage(self):
        """تهيئة ملفات التخزين إذا لم تكن موجودة"""
        if not os.path.exists(self.trades_file):
            with open(self.trades_file, 'w', encoding='utf-8') as f:
                json.dump([], f)
        
        if not os.path.exists(self.performance_file):
            with open(self.performance_file, 'w', encoding='utf-8') as f:
                json.dump({}, f)
    
    def _load_trades(self):
        """تحميل الصفقات من الملف"""
        try:
            with open(self.trades_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []
    
    def _save_trades(self, trades):
        """حفظ الصفقات إلى الملف"""
        try:
            with open(self.trades_file, 'w', encoding='utf-8') as f:
                json.dump(trades, f, indent=2, default=str)
            return True
        except Exception as e:
            logger.error(f"خطأ في حفظ الصفقات: {e}")
            return False
    
    def save_trade(self, trade_data):
        """حفظ صفقة جديدة"""
        try:
            trades = self._load_trades()
            trade_data['timestamp'] = datetime.now(damascus_tz).isoformat()
            trades.append(trade_data)
            success = self._save_trades(trades)
            if success:
                logger.info(f"✅ تم حفظ الصفقة بنجاح: {trade_data['symbol']}")
            return success
        except Exception as e:
            logger.error(f"خطأ في حفظ الصفقة: {e}")
            return False
    
    def update_trade_status(self, symbol, order_id, updates):
        """تحديث حالة الصفقة"""
        try:
            trades = self._load_trades()
            updated = False
            
            for trade in trades:
                if trade.get('symbol') == symbol and trade.get('order_id') == order_id:
                    trade.update(updates)
                    updated = True
                    break
            
            if updated:
                success = self._save_trades(trades)
                if success:
                    logger.info(f"✅ تم تحديث صفقة {symbol} بنجاح")
                return success
            else:
                logger.warning(f"⚠️ لم يتم العثور على صفقة {symbol} للتحديث")
                return False
        except Exception as e:
            logger.error(f"خطأ في تحديث صفقة {symbol}: {e}")
            return False
    
    def update_trade_stop_loss(self, symbol, new_sl):
        """تحديث وقف الخسارة"""
        try:
            trades = self._load_trades()
            updated = False
            
            for trade in trades:
                if trade.get('symbol') == symbol and trade.get('status') == 'open':
                    trade['stop_loss'] = new_sl
                    updated = True
                    break
            
            if updated:
                success = self._save_trades(trades)
                if success:
                    logger.info(f"✅ تم تحديث وقف الخسارة لـ {symbol} إلى {new_sl}")
                return success
            else:
                logger.warning(f"⚠️ لم يتم العثور على صفقة نشطة لـ {symbol}")
                return False
        except Exception as e:
            logger.error(f"خطأ في تحديث وقف الخسارة: {e}")
            return False
    
    def get_performance_stats(self):
        """جلب إحصائيات الأداء"""
        try:
            trades = self._load_trades()
            completed_trades = [t for t in trades if t.get('status') == 'completed']
            
            if not completed_trades:
                return {}
            
            total_trades = len(completed_trades)
            profitable_trades = sum(1 for t in completed_trades if t.get('profit_loss', 0) > 0)
            total_profit = sum(t.get('profit_loss', 0) for t in completed_trades if t.get('profit_loss', 0) > 0)
            total_loss = sum(t.get('profit_loss', 0) for t in completed_trades if t.get('profit_loss', 0) < 0)
            
            win_rate = (profitable_trades / total_trades * 100) if total_trades > 0 else 0
            
            return {
                'total_trades': total_trades,
                'win_rate': round(win_rate, 2),
                'total_profit': round(total_profit, 2),
                'total_loss': round(abs(total_loss), 2),
                'profit_factor': round(total_profit / abs(total_loss), 2) if total_loss < 0 else float('inf')
            }
        except Exception as e:
            logger.error(f"خطأ في جلب الإحصائيات: {e}")
            return {}

class HealthMonitor:
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.error_count = 0
        self.max_errors = 10
        self.last_health_check = datetime.now(damascus_tz)
        
    def check_connections(self):
        try:
            self.bot.request_manager.safe_request(self.bot.client.get_server_time)
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
    # ============ الإعدادات الأساسية القابلة للتعديل ============
    TRADING_SETTINGS = {
        # إعدادات التداول العامة
        'min_daily_volume': 1000000,      # الحد الأدنى للحجم اليومي (بالدولار)
        'min_trade_size': 10,             # الحد الأدنى لحجم الصفقة (بالدولار)
        'max_trade_size': 50,             # الحد الأقصى لحجم الصفقة (بالدولار)
        'max_position_size': 0.35,        # الحد الأقصى لحجم المركز (نسبة من الرصيد)
        'momentum_score_threshold': 45,   # الحد الأدنى للنقاط للدخول (تم تخفيضه)
        'min_profit_threshold': 0.002,    # الحد الأدنى للربح المتوقع
        
        'first_profit_target': 1.15,        # 1% ربح + 0.15% عمولة
        'first_profit_percentage': 0.5,     # أخذ 50% من الصفقة
        'min_required_profit': 0.01,        # 1% ربح mínimo مطلوب على كامل الصفقة
        'breakeven_sl_percent': 0.5,        # تحريك وقف الخسارة عند تحقيق 0.5% ربح
        'min_remaining_profit': 0.2,        # أقل ربح مسموح للجزء المتبقي
        
        # إعدادات المخاطرة
        'risk_per_trade': 2.0,            # نسبة المخاطرة لكل صفقة (٪)
        'base_risk_pct': 0.004,           # نسبة المخاطرة الأساسية
        'aggressive_risk_pct': 0.008,     # نسبة مخاطرة عدوانية للفرص القوية
        'conservative_risk_pct': 0.003,   # نسبة مخاطرة محافظة
        
        # إعدادات وقف الخسارة وأخذ الربح
        'atr_multiplier_sl': 1.2,         # مضاعف ATR لوقف الخسارة
        'risk_reward_ratio': 2.0,         # نسبة العائد إلى المخاطرة
        'breakeven_sl_percent': 1.5,      # النسبة لتحريك وقف الخسارة إلى نقطة التعادل
        'partial_profit_percent': 3.0,    # النسبة لأخذ ربح جزئي
        'partial_profit_size': 0.5,       # حجم الربح الجزئي (50%)
        
        # إعدادات الفلاتر
        'min_volume_ratio': 1.8,          # الحد الأدنى لنسبة الحجم
        'min_price_change_5m': 2.0,       # الحد الأدنى لتغير السعر في 5 دقائق
        'max_active_trades': 3,           # الحد الأقصى للصفقات النشطة
        'correlation_threshold': 0.8,     # حد الارتباط العالي
        
        # إعدادات المؤشرات
        'rsi_overbought': 75,             # مستوى الشراء الزائد لـ RSI
        'rsi_oversold': 35,               # مستوى البيع الزائد لـ RSI
        'adx_strong_trend': 20,           # مستوى ADX للاتجاه القوي
        'adx_medium_trend': 15,           # مستوى ADX للاتجاه المتوسط
        
        # إعدادات الفحص
        'max_symbols_to_analyze': 50,     # الحد الأقصى للرموز للتحليل
        'historical_data_limit': 30,      # عدد الشمعات للبيانات التاريخية
        'data_interval': '5m',            # الفترة الزمنية للبيانات
        'rescan_interval_minutes': 15,    # فترة إعادة الفحص (دقائق)
        
        # إعدادات الأداء
        'cache_ttl_seconds': 300,         # مدة التخزين المؤقت (ثواني)
        'request_delay_ms': 100,          # التأخير بين الطلبات (مللي ثانية)
    }

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
    # ============ نهاية الإعدادات ============

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
        self.cache = TTLCache(maxsize=1000, ttl=self.TRADING_SETTINGS['cache_ttl_seconds'])
    
        if self.telegram_token and self.telegram_chat_id:
            self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
            logger.info("✅ Telegram notifier initialized successfully")
        else:
            self.notifier = None
            logger.warning("⚠️ Telegram notifier not initialized - check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
            
        self.active_trades = {}
        self.storage_manager = FileStorageManager()  # استخدام التخزين المحلي بدلاً من MongoDB
    
        # ✅ تحميل الصفقات الموجودة عند التشغيل
        self.sync_active_trades_with_binance()
        self.load_existing_trades()
    
        logger.info(f"✅ تم تحميل {len(self.active_trades)} صفقة مفتوحة")
        
        self.health_monitor = HealthMonitor(self)
    
        # تهيئة الإعدادات من القاموس
        self.min_daily_volume = self.TRADING_SETTINGS['min_daily_volume']
        self.min_trade_size = self.TRADING_SETTINGS['min_trade_size']
        self.max_trade_size = self.TRADING_SETTINGS['max_trade_size']
        self.max_position_size = self.TRADING_SETTINGS['max_position_size']
        self.momentum_score_threshold = self.TRADING_SETTINGS['momentum_score_threshold']
        self.min_profit_threshold = self.TRADING_SETTINGS['min_profit_threshold']
        self.risk_per_trade = self.TRADING_SETTINGS['risk_per_trade']

        self.symbols = self.get_all_trading_symbols()
        self.stable_coins = ['USDT', 'BUSD', 'USDC']
    
        self.active_trades = {}
        self.last_scan_time = datetime.now()
    

        logger.info("✅ تم تهيئة بوت صائد الصاعدات المتقدم بنجاح")

    def test_notifier(self):
        if self.notifier:
            message = "🔔 <b>اختبار إشعار Telegram</b>\nالنظام يعمل بشكل طبيعي"
            sent = self.notifier.send_message(message, 'test')
            if sent:
                logger.info("✅ تم إرسال اختبار Telegram بنجاح")
            else:
                logger.error("❌ فشل إرسال اختبار Telegram")
        else:
            logger.warning("⚠️ Notifier غير مفعل - لا يمكن إجراء الاختبار")
            
    def load_existing_trades(self):
        """تحميل الصفقات المفتوحة الحالية من Binance فقط"""
        try:
            # جلب جميع الأوامر المفتوحة من Binance
            open_orders = self.safe_binance_request(self.client.get_open_orders)
        
            if not open_orders:
                logger.info("لا توجد أوامر مفتوحة في Binance")
                return
        
            for order in open_orders:
                if order['side'] == 'BUY' and order['status'] == 'FILLED':
                    symbol = order['symbol']
                    current_price = self.get_current_price(symbol)
                
                    if current_price is None:
                        continue
                
                    # إنشاء بيانات الصفقة من Binance فقط
                    trade_data = {
                        'symbol': symbol,
                        'entry_price': float(order['price']),
                        'quantity': float(order['executedQty']),
                        'trade_size': float(order['executedQty']) * float(order['price']),
                        'stop_loss': current_price * 0.98,  # وقف افتراضي
                        'take_profit': current_price * 1.04,  # ربح افتراضي
                        'timestamp': datetime.fromtimestamp(order['time'] / 1000),
                        'status': 'open',
                        'order_id': order['orderId'],
                        'first_profit_taken': False  # إضافة افتراضية
                    }
                 
                    self.active_trades[symbol] = trade_data
                    logger.info(f"✅ تم تحميل الصفقة من Binance: {symbol}")
                    
        except Exception as e:
            logger.error(f"❌ خطأ في تحميل الصفقات من Binance: {e}")
            
    def sync_active_trades_with_binance(self):
        """مزامنة الصفقات النشطة مع Binance فقط"""
        try:
            # جلب جميع الأوامر المفتوحة من Binance
            open_orders = self.safe_binance_request(self.client.get_open_orders)
        
            if not open_orders:
                logger.info("لا توجد أوامر مفتوحة في Binance للمزامنة")
                return
        
            # مسح الصفقات النشطة الحالية وإعادة بنائها من Binance
            self.active_trades.clear()
        
            for order in open_orders:
                if order['side'] == 'BUY' and order['status'] == 'FILLED':
                    symbol = order['symbol']
                    current_price = self.get_current_price(symbol)
                
                    if current_price is None:
                        continue
                
                    trade_data = {
                        'symbol': symbol,
                        'entry_price': float(order['price']),
                        'quantity': float(order['executedQty']),
                        'trade_size': float(order['executedQty']) * float(order['price']),
                        'stop_loss': current_price * 0.98,
                        'take_profit': current_price * 1.04,
                        'timestamp': datetime.fromtimestamp(order['time'] / 1000),
                        'status': 'open',
                        'order_id': order['orderId'],
                        'first_profit_taken': False
                    }
                
                    self.active_trades[symbol] = trade_data
                    logger.info(f"✅ تم مزامنة الصفقة من Binance: {symbol}")
            
        except Exception as e:
            logger.error(f"❌ خطأ في مزامنة الصفقات من Binance: {e}")
            
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
            return important_symbols
    
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

    async def get_multiple_tickers_async(self, symbols):
        """جلب بيانات التيكرز لعدة رموز بشكل غير متزامن"""
        async with aiohttp.ClientSession() as session:
            tasks = [self.fetch_ticker_async(symbol, session) for symbol in symbols]
            tickers = await asyncio.gather(*tasks, return_exceptions=True)
            return [ticker for ticker in tickers if ticker is not None]

    def get_multiple_tickers(self, symbols):
        """واجهة متزامنة لجلب التيكرز"""
        try:
            loop = asyncio.new_event_loop()  # إنشاء حلقة حدث جديدة
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self.get_multiple_tickers_async(symbols))
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
            
            # ديناميكي: إضافة نقاط لـ breakout إذا تجاوز السعر EMA100 بنسبة 1% (دخول سريع)
            if latest['close'] > latest['ema100'] * 1.01:
                score += 10
                details['breakout_ema100'] = 10
            
            # ديناميكي: إضافة نقاط لـ breakout إذا تجاوز السعر EMA50 بنسبة 0.5% (دخول سريع)
            if latest['close'] > latest['ema50'] * 1.005:
                score += 8
                details['breakout_ema50'] = 8
            
            # اتجاه EMA (أقصى وزن)
            if latest['ema8'] > latest['ema21'] > latest['ema50']:
                score += self.WEIGHTS['trend']
                details['ema_trend'] = self.WEIGHTS['trend']
            elif latest['ema8'] > latest['ema21']:
                score += self.WEIGHTS['trend'] * 0.7
                details['ema_trend'] = self.WEIGHTS['trend'] * 0.7
            
            # تقاطع EMA (وزن عالي)
            if latest['ema8'] > latest['ema21'] and prev['ema8'] <= prev['ema21']:
                score += self.WEIGHTS['crossover']
                details['ema_crossover'] = self.WEIGHTS['crossover']
            
            # تغير السعر (وزن متوسط)
            price_change_5m = ((latest['close'] - prev['close']) / prev['close']) * 100
            if price_change_5m > 0.5:
                score += self.WEIGHTS['price_change']
                details['price_change'] = self.WEIGHTS['price_change']
            
            # الحجم (وزن متوسط)
            if latest['volume_ratio'] > 1.5:
                score += self.WEIGHTS['volume']
                details['volume'] = self.WEIGHTS['volume']
            
            # RSI (وزن منخفض)
            if 40 < latest['rsi'] < 70:
                score += self.WEIGHTS['rsi'] * 0.8
                details['rsi'] = self.WEIGHTS['rsi'] * 0.8
            elif 30 < latest['rsi'] < 80:
                score += self.WEIGHTS['rsi'] * 0.5
                details['rsi'] = self.WEIGHTS['rsi'] * 0.5
            
            # MACD (وزن منخفض)
            if latest['macd'] > latest['macd_signal']:
                score += self.WEIGHTS['macd']
                details['macd'] = self.WEIGHTS['macd']
            
            # ADX (وزن متوسط)
            if latest['adx'] > 20:
                score += self.WEIGHTS['adx']
                details['adx'] = self.WEIGHTS['adx']
            
            # بولينجر باند (وزن منخفض)
            if latest['close'] > latest['middle_bb']:
                score += self.WEIGHTS['bollinger']
                details['bollinger'] = self.WEIGHTS['bollinger']
            
            # ✅ إضافة نقاط إضافية للزخم القوي
            # إذا كان هناك زيادة كبيرة في الحجم مع حركة سعر قوية
            if latest['volume_ratio'] > 2.0 and price_change_5m > 1.0:
                score += 15
                details['strong_volume_momentum'] = 15
            
            # إذا كان هناك breakout من نطاق ضيق
            bb_width = (latest['upper_bb'] - latest['lower_bb']) / latest['middle_bb']
            if bb_width < 0.02 and price_change_5m > 0.8:  # نطاق ضيق + حركة قوية
                score += 12
                details['breakout_from_tight_range'] = 12
            
            # ✅ خصم النقاط للإشارات السلبية
            # إذا كان السعر تحت EMA100 الرئيسي
            if latest['close'] < latest['ema100']:
                score -= 15
                details['below_ema100'] = -15
            
            # إذا كان هناك انخفاض كبير في الحجم مع حركة سعر
            if latest['volume_ratio'] < 0.8 and abs(price_change_5m) > 0.5:
                score -= 10
                details['low_volume_move'] = -10
            
            # إذا كان RSI في منطقة ذروة الشراء (>80)
            if latest['rsi'] > 80:
                score -= 12
                details['overbought_rsi'] = -12
            
            # ✅ ضمان أن النتيجة لا تقل عن 0
            score = max(0, score)
            
            return score, details
            
        except Exception as e:
            logger.error(f"خطأ في حساب نقاط الزخم لـ {symbol}: {e}")
            return 0, {}
    
    def calculate_risk_parameters(self, symbol, entry_price, atr):
        try:
            # حساب المخاطرة بناءً على تقلبات السوق
            volatility_risk = min(max(atr / entry_price, 0.005), 0.02)
            
            # حساب حجم المركز بناءً على المخاطرة
            balance = self.get_account_balance()
            usdt_balance = balance.get('USDT', {}).get('free', 0)
            
            if usdt_balance <= 0:
                logger.error("الرصيد غير كافٍ")
                return None, None, None
            
            # حجم المركز بناءً على المخاطرة
            risk_amount = usdt_balance * (self.risk_per_trade / 100)
            position_size = risk_amount / volatility_risk
            
            # الحد من حجم المركز
            max_position = usdt_balance * self.max_position_size
            position_size = min(position_size, max_position)
            
            # حساب وقف الخسارة وأخذ الربح
            stop_loss = entry_price * (1 - volatility_risk)
            take_profit = entry_price * (1 + (volatility_risk * self.risk_reward_ratio))
            
            return position_size, stop_loss, take_profit
            
        except Exception as e:
            logger.error(f"خطأ في حساب معاملات المخاطرة لـ {symbol}: {e}")
            return None, None, None
    
    def place_order(self, symbol, side, quantity, price=None, order_type=ORDER_TYPE_MARKET, stop_price=None):
        if self.dry_run:
            logger.info(f"[DRY RUN] {side} {quantity} {symbol} @ {price if price else 'MARKET'}")
            return {'order_id': 'dry_run_' + str(time.time())}
        
        try:
            order_params = {
                'symbol': symbol,
                'side': side,
                'type': order_type,
                'quantity': quantity
            }
            
            if price:
                order_params['price'] = price
            if stop_price:
                order_params['stopPrice'] = stop_price
            
            order = self.safe_binance_request(self.client.create_order, **order_params)
            return order
            
        except Exception as e:
            logger.error(f"خطأ في وضع أمر لـ {symbol}: {e}")
            return None
    
    def close_position(self, symbol, reason="manual"):
        try:
            if symbol not in self.active_trades:
                logger.warning(f"لا توجد صفقة نشطة لـ {symbol}")
                return False
            
            trade = self.active_trades[symbol]
            quantity = trade['quantity']
            
            # بيع الكمية بالكامل
            order = self.place_order(symbol, 'SELL', quantity)
            
            if order:
                # حساب الربح/الخسارة
                current_price = self.get_current_price(symbol)
                profit_loss = (current_price - trade['entry_price']) * quantity
                
                # تحديث حالة الصفقة
                trade.update({
                    'exit_price': current_price,
                    'exit_time': datetime.now(),
                    'profit_loss': profit_loss,
                    'status': 'completed',
                    'close_reason': reason
                })
                
                # حفظ في التخزين
                self.storage_manager.update_trade_status(symbol, trade['order_id'], {
                    'exit_price': current_price,
                    'exit_time': datetime.now().isoformat(),
                    'profit_loss': profit_loss,
                    'status': 'completed',
                    'close_reason': reason
                })
                
                # إرسال إشعار
                if self.notifier:
                    profit_text = "ربح" if profit_loss > 0 else "خسارة"
                    message = f"📊 <b>إغلاق صفقة</b>\n"
                    message += f"🔸 العملة: {symbol}\n"
                    message += f"🔸 السعر: {current_price:.4f}\n"
                    message += f"🔸 النتيجة: {profit_text} ${abs(profit_loss):.2f}\n"
                    message += f"🔸 السبب: {reason}"
                    self.notifier.send_message(message, 'trade')
                
                # إزالة من الصفقات النشطة
                del self.active_trades[symbol]
                
                logger.info(f"✅ تم إغلاق صفقة {symbol} بنجاح")
                return True
            else:
                logger.error(f"❌ فشل إغلاق صفقة {symbol}")
                return False
                
        except Exception as e:
            logger.error(f"❌ خطأ في إغلاق صفقة {symbol}: {e}")
            return False
    
    def manage_open_positions(self):
        try:
            symbols_to_remove = []
            
            for symbol, trade in list(self.active_trades.items()):
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    continue
                
                entry_price = trade['entry_price']
                stop_loss = trade.get('stop_loss')
                take_profit = trade.get('take_profit')
                
                # حساب الربح/الخسارة الحالي
                profit_pct = ((current_price - entry_price) / entry_price) * 100
                
                # 1. التحقق من وقف الخسارة
                if stop_loss and current_price <= stop_loss:
                    logger.info(f"🛑 تم تشغيل وقف الخسارة لـ {symbol}")
                    self.close_position(symbol, "وقف الخسارة")
                    symbols_to_remove.append(symbol)
                    continue
                
                # 2. التحقق من أخذ الربح
                if take_profit and current_price >= take_profit:
                    logger.info(f"🎯 تم تحقيق هدف الربح لـ {symbol}")
                    self.close_position(symbol, "أخذ الربح")
                    symbols_to_remove.append(symbol)
                    continue
                
                # 3. إدارة وقف الخسارة المتحرك
                self.update_trailing_stop(symbol, current_price, profit_pct)
                
                # 4. أخذ الربح الجزئي
                self.take_partial_profit(symbol, current_price, profit_pct)
            
            # إزالة الصفقات المغلقة
            for symbol in symbols_to_remove:
                if symbol in self.active_trades:
                    del self.active_trades[symbol]
                    
        except Exception as e:
            logger.error(f"خطأ في إدارة الصفقات المفتوحة: {e}")
    
    def update_trailing_stop(self, symbol, current_price, profit_pct):
        try:
            trade = self.active_trades[symbol]
            entry_price = trade['entry_price']
            
            # تحديث وقف الخسارة عند تحقيق ربح معين
            if profit_pct >= self.TRADING_SETTINGS['breakeven_sl_percent']:
                # تحريك وقف الخسارة إلى نقطة التعادل + هامش صغير
                new_sl = entry_price * 1.001  # 0.1% فوق نقطة الدخول
                
                if new_sl > trade.get('stop_loss', 0):
                    trade['stop_loss'] = new_sl
                    self.storage_manager.update_trade_stop_loss(symbol, new_sl)
                    logger.info(f"📈 تم تحديث وقف الخسارة لـ {symbol} إلى {new_sl:.4f}")
            
        except Exception as e:
            logger.error(f"خطأ في تحديث وقف الخسارة لـ {symbol}: {e}")
    
    def take_partial_profit(self, symbol, current_price, profit_pct):
        try:
            trade = self.active_trades[symbol]
            
            # أخذ ربح جزئي عند تحقيق هدف معين
            if (profit_pct >= self.TRADING_SETTINGS['partial_profit_percent'] and 
                not trade.get('partial_profit_taken', False)):
                
                # بيع جزء من المركز
                partial_qty = trade['quantity'] * self.TRADING_SETTINGS['partial_profit_size']
                order = self.place_order(symbol, 'SELL', partial_qty)
                
                if order:
                    # تحديث كمية المركز المتبقية
                    trade['quantity'] -= partial_qty
                    trade['partial_profit_taken'] = True
                    
                    # حساب الربح الجزئي
                    partial_profit = (current_price - trade['entry_price']) * partial_qty
                    
                    # إرسال إشعار
                    if self.notifier:
                        message = f"🎯 <b>أخذ ربح جزئي</b>\n"
                        message += f"🔸 العملة: {symbol}\n"
                        message += f"🔸 الكمية: {partial_qty:.4f}\n"
                        message += f"🔸 الربح: ${partial_profit:.2f}\n"
                        message += f"🔸 الباقي: {trade['quantity']:.4f}"
                        self.notifier.send_message(message, 'profit')
                    
                    logger.info(f"✅ تم أخذ ربح جزئي لـ {symbol}")
                
        except Exception as e:
            logger.error(f"خطأ في أخذ الربح الجزئي لـ {symbol}: {e}")
    
    def scan_for_opportunities(self):
        try:
            logger.info("🔍 بدء البحث عن فرص تداول...")
            
            # جلب بيانات التيكرز لجميع الرموز
            tickers = self.get_multiple_tickers(self.symbols)
            if not tickers:
                logger.warning("❌ لم يتم جلب أي بيانات تيكرز")
                return
            
            # تصفية الرموز ذات الحجم الكافي
            valid_symbols = []
            for ticker in tickers:
                symbol = ticker['symbol']
                volume = float(ticker['volume'])
                avg_price = float(ticker['weightedAvgPrice'])
                dollar_volume = volume * avg_price
                
                if dollar_volume >= self.min_daily_volume:
                    valid_symbols.append(symbol)
            
            logger.info(f"📊 عدد الرموز المؤهلة: {len(valid_symbols)} من أصل {len(self.symbols)}")
            
            # تحليل كل رمز
            opportunities = []
            for symbol in valid_symbols[:self.TRADING_SETTINGS['max_symbols_to_analyze']]:
                try:
                    score, details = self.calculate_momentum_score(symbol)
                    if score >= self.momentum_score_threshold:
                        current_price = self.get_current_price(symbol)
                        if current_price:
                            opportunities.append({
                                'symbol': symbol,
                                'score': score,
                                'price': current_price,
                                'details': details
                            })
                except Exception as e:
                    logger.error(f"خطأ في تحليل {symbol}: {e}")
                    continue
            
            # ترتيب الفرص حسب النقاط
            opportunities.sort(key=lambda x: x['score'], reverse=True)
            
            logger.info(f"🎯 عدد الفرص المكتشفة: {len(opportunities)}")
            
            # معالجة أفضل الفرص
            for opp in opportunities[:3]:  # معالجة أفضل 3 فرص فقط
                self.evaluate_and_trade(opp)
            
            self.last_scan_time = datetime.now()
            
        except Exception as e:
            logger.error(f"خطأ في فحص الفرص: {e}")
    
    def evaluate_and_trade(self, opportunity):
        symbol = opportunity['symbol']
        score = opportunity['score']
        current_price = opportunity['price']
        
        try:
            # التحقق من عدم وجود صفقة نشطة على نفس الرمز
            if symbol in self.active_trades:
                logger.info(f"⏭️ تخطي {symbol} - صفقة نشطة موجودة")
                return
            
            # التحقق من الحد الأقصى للصفقات النشطة
            if len(self.active_trades) >= self.TRADING_SETTINGS['max_active_trades']:
                logger.info("⏭️ تخطي - وصلت للحد الأقصى للصفقات النشطة")
                return
            
            # جلب البيانات التاريخية لحساب ATR
            data = self.get_historical_data(symbol, '15m', 100)
            if data is None:
                return
            
            data = self.calculate_technical_indicators(data)
            atr = data['atr'].iloc[-1]
            
            # حساب معاملات المخاطرة
            position_size, stop_loss, take_profit = self.calculate_risk_parameters(
                symbol, current_price, atr
            )
            
            if position_size is None:
                return
            
            # التحقق من الحد الأدنى والأقصى لحجم الصفقة
            if position_size < self.min_trade_size:
                logger.info(f"⏭️ تخطي {symbol} - حجم الصفقة صغير جدًا")
                return
            
            position_size = min(position_size, self.max_trade_size)
            
            # حساب الكمية
            quantity = position_size / current_price
            
            # تقريب الكمية إلى المنزلة العشرية الصحيحة
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            if symbol_info:
                step_size = None
                for filt in symbol_info['filters']:
                    if filt['filterType'] == 'LOT_SIZE':
                        step_size = float(filt['stepSize'])
                        break
                
                if step_size:
                    quantity = round(quantity / step_size) * step_size
            
            # وضع أمر الشراء
            order = self.place_order(symbol, 'BUY', quantity)
            
            if order:
                # حفظ بيانات الصفقة
                trade_data = {
                    'symbol': symbol,
                    'entry_price': current_price,
                    'quantity': quantity,
                    'trade_size': position_size,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'timestamp': datetime.now(),
                    'status': 'open',
                    'order_id': order['orderId'],
                    'momentum_score': score,
                    'score_details': opportunity['details']
                }
                
                self.active_trades[symbol] = trade_data
                self.storage_manager.save_trade(trade_data)
                
                # إرسال إشعار
                if self.notifier:
                    message = f"🎯 <b>فتح صفقة جديدة</b>\n"
                    message += f"🔸 العملة: {symbol}\n"
                    message += f"🔸 السعر: {current_price:.4f}\n"
                    message += f"🔸 الكمية: {quantity:.4f}\n"
                    message += f"🔸 الحجم: ${position_size:.2f}\n"
                    message += f"🔸 النقاط: {score:.1f}\n"
                    message += f"🔸 وقف الخسارة: {stop_loss:.4f}\n"
                    message += f"🔸 أخذ الربح: {take_profit:.4f}"
                    self.notifier.send_message(message, 'trade')
                
                logger.info(f"✅ تم فتح صفقة {symbol} بنجاح")
            
        except Exception as e:
            logger.error(f"❌ خطأ في فتح صفقة لـ {symbol}: {e}")
    
    def run(self):
        logger.info("🚀 بدء تشغيل بوت صائد الصاعدات المتقدم")
        
        if self.notifier:
            self.notifier.send_message("🚀 <b>بدء تشغيل البوت</b>\nتم تشغيل نظام صائد الصاعدات المتقدم", "startup")
        
        # جدولة المهام
        schedule.every(5).minutes.do(self.manage_open_positions)
        schedule.every(15).minutes.do(self.scan_for_opportunities)
        schedule.every(30).minutes.do(self.health_monitor.check_connections)
        schedule.every(1).hours.do(self.sync_active_trades_with_binance)
        
        # تشغيل خادم Flask في خيط منفصل
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        # الحلقة الرئيسية
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("⏹️ إيقاف البوت بواسطة المستخدم")
                if self.notifier:
                    self.notifier.send_message("⏹️ <b>إيقاف البوت</b>\nتم إيقاف التشغيل بواسطة المستخدم", "shutdown")
                break
            except Exception as e:
                logger.error(f"❌ خطأ غير متوقع في الحلقة الرئيسية: {e}")
                time.sleep(60)
    
    def get_performance_stats(self):
        return self.storage_manager.get_performance_stats()
    
    def get_current_opportunities(self):
        opportunities = []
        for symbol in self.symbols[:10]:  # تحليل أول 10 رموز فقط للعرض
            try:
                score, details = self.calculate_momentum_score(symbol)
                current_price = self.get_current_price(symbol)
                if current_price:
                    opportunities.append({
                        'symbol': symbol,
                        'score': score,
                        'price': current_price,
                        'details': details
                    })
            except:
                continue
        
        opportunities.sort(key=lambda x: x['score'], reverse=True)
        return opportunities[:5]  # أفضل 5 فرص فقط
    
    def backtest_strategy(self, symbol, start_date, end_date):
        try:
            # تحويل التواريخ
            start_ts = int(datetime.strptime(start_date, '%Y-%m-%d').timestamp() * 1000)
            end_ts = int(datetime.strptime(end_date, '%Y-%m-%d').timestamp() * 1000)
            
            # جلب البيانات التاريخية
            klines = self.safe_binance_request(
                self.client.get_historical_klines,
                symbol, '15m', start_ts, end_ts
            )
            
            if not klines:
                return {'error': 'لا توجد بيانات للفترة المحددة'}
            
            # تحويل إلى DataFrame
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
            df = self.calculate_technical_indicators(df)
            
            # محاكاة التداول
            initial_balance = 1000  # رصيد ابتدائي
            balance = initial_balance
            position = 0
            entry_price = 0
            trades = []
            
            for i in range(50, len(df)):
                current = df.iloc[i]
                prev = df.iloc[i-1]
                
                # حساب النقاط (نفس منطق calculate_momentum_score)
                score = 0
                
                if current['ema8'] > current['ema21'] > current['ema50']:
                    score += self.WEIGHTS['trend']
                elif current['ema8'] > current['ema21']:
                    score += self.WEIGHTS['trend'] * 0.7
                
                if current['ema8'] > current['ema21'] and prev['ema8'] <= prev['ema21']:
                    score += self.WEIGHTS['crossover']
                
                price_change = ((current['close'] - prev['close']) / prev['close']) * 100
                if price_change > 0.5:
                    score += self.WEIGHTS['price_change']
                
                if current['volume_ratio'] > 1.5:
                    score += self.WEIGHTS['volume']
                
                if 40 < current['rsi'] < 70:
                    score += self.WEIGHTS['rsi'] * 0.8
                
                if current['macd'] > current['macd_signal']:
                    score += self.WEIGHTS['macd']
                
                if current['adx'] > 20:
                    score += self.WEIGHTS['adx']
                
                if current['close'] > current['middle_bb']:
                    score += self.WEIGHTS['bollinger']
                
                # قرار التداول
                if score >= self.momentum_score_threshold and position == 0:
                    # شراء
                    position = balance / current['close']
                    entry_price = current['close']
                    trades.append({
                        'type': 'BUY',
                        'price': entry_price,
                        'timestamp': current['timestamp'],
                        'balance': balance
                    })
                
                elif position > 0:
                    # بيع عند تحقيق ربح أو خسارة
                    profit_pct = ((current['close'] - entry_price) / entry_price) * 100
                    
                    if profit_pct >= 2.0 or profit_pct <= -1.0:
                        balance = position * current['close']
                        trades.append({
                            'type': 'SELL',
                            'price': current['close'],
                            'timestamp': current['timestamp'],
                            'balance': balance,
                            'profit_pct': profit_pct
                        })
                        position = 0
            
            # حساب الأداء النهائي
            final_balance = balance if position == 0 else position * df.iloc[-1]['close']
            total_return = ((final_balance - initial_balance) / initial_balance) * 100
            
            return {
                'initial_balance': initial_balance,
                'final_balance': final_balance,
                'total_return': total_return,
                'total_trades': len(trades),
                'winning_trades': sum(1 for t in trades if t.get('profit_pct', 0) > 0),
                'losing_trades': sum(1 for t in trades if t.get('profit_pct', 0) < 0)
            }
            
        except Exception as e:
            logger.error(f"خطأ في اختبار الاستراتيجية: {e}")
            return {'error': str(e)}

# تشغيل البوت
if __name__ == "__main__":
    # وضع الاختبار - تغيير إلى False للتداول الحقيقي
    DRY_RUN = os.environ.get('DRY_RUN', 'True').lower() == 'true'
    
    bot = MomentumHunterBot(dry_run=DRY_RUN)
    
    # اختبار إشعار Telegram
    if bot.notifier:
        bot.test_notifier()
    
    # بدء التشغيل
    bot.run()
