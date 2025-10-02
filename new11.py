import os
import pandas as pd
import numpy as np
import hashlib
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
from flask import Flask, jsonify
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

@app.route('/')
def health_check():
    return {'status': 'healthy', 'service': 'futures-trading-bot', 'timestamp': datetime.now(damascus_tz).isoformat()}

@app.route('/active_trades')
def active_trades():
    try:
        bot = FuturesTradingBot.get_instance()
        if bot:
            return jsonify(bot.get_active_trades_details())
        return jsonify([])
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
        logging.FileHandler('futures_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TradeManager:
    """مدير محسن للصفقات مع تتبع دقيق"""
    
    def __init__(self, client, notifier):
        self.client = client
        self.notifier = notifier
        self.active_trades = {}
        self.trade_history = []
        self.last_sync = datetime.now(damascus_tz)
        
    def sync_with_exchange(self):
        """مزامنة الصفقات مع المنصة بدقة"""
        try:
            logger.info("🔄 مزامنة الصفقات مع المنصة...")
            
            # جلب جميع المراكز من المنصة
            account_info = self.client.futures_account()
            positions = account_info['positions']
            
            # جلب الأوامر المفتوحة
            open_orders = self.client.futures_get_open_orders()
            
            active_symbols = set()
            valid_trades = {}
            
            # تحليل المراكز النشطة
            for position in positions:
                symbol = position['symbol']
                quantity = float(position['positionAmt'])
                
                if quantity != 0:  # مركز نشط
                    active_symbols.add(symbol)
                    
                    # البحث عن أمر فتح مرتبط
                    entry_order = None
                    for order in open_orders:
                        if order['symbol'] == symbol and order['type'] == 'MARKET' and order['side'] in ['BUY', 'SELL']:
                            entry_order = order
                            break
                    
                    # إنشاء بيانات الصفقة
                    side = "LONG" if quantity > 0 else "SHORT"
                    entry_price = float(position['entryPrice'])
                    leverage = float(position['leverage'])
                    
                    trade_data = {
                        'symbol': symbol,
                        'quantity': abs(quantity),
                        'entry_price': entry_price,
                        'leverage': leverage,
                        'side': side,
                        'timestamp': datetime.now(damascus_tz),
                        'status': 'open',
                        'trade_type': 'futures',
                        'position_amt': quantity,
                        'unrealized_pnl': float(position['unrealizedProfit']),
                        'order_id': entry_order['orderId'] if entry_order else None
                    }
                    
                    valid_trades[symbol] = trade_data
                    logger.info(f"✅ تمت مزامنة صفقة نشطة: {symbol} - {side}")
            
            # تحديث الصفقات النشطة
            self.active_trades = valid_trades
            
            # تسجيل الصفقات المغلقة
            self.record_closed_trades(active_symbols)
            
            self.last_sync = datetime.now(damascus_tz)
            logger.info(f"✅ اكتملت المزامنة: {len(self.active_trades)} صفقات نشطة")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ خطأ في مزامنة الصفقات: {e}")
            return False
    
    def record_closed_trades(self, current_active_symbols):
        """تسجيل الصفقات المغلقة"""
        previous_symbols = set(self.active_trades.keys())
        closed_symbols = previous_symbols - current_active_symbols
        
        for symbol in closed_symbols:
            if symbol in self.active_trades:
                closed_trade = self.active_trades[symbol]
                closed_trade['status'] = 'closed'
                closed_trade['close_time'] = datetime.now(damascus_tz)
                self.trade_history.append(closed_trade)
                logger.info(f"📝 تم تسجيل إغلاق صفقة: {symbol}")
    
    def get_active_trades_count(self):
        """الحصول على عدد الصفقات النشطة بدقة"""
        return len(self.active_trades)
    
    def is_symbol_trading(self, symbol):
        """التحقق مما إذا كان الرمز يتداول حالياً"""
        return symbol in self.active_trades
    
    def add_trade(self, symbol, trade_data):
        """إضافة صفقة جديدة"""
        self.active_trades[symbol] = trade_data
    
    def remove_trade(self, symbol):
        """إزالة صفقة"""
        if symbol in self.active_trades:
            del self.active_trades[symbol]
    
    def get_trade(self, symbol):
        """الحصول على بيانات صفقة"""
        return self.active_trades.get(symbol)
    
    def get_all_trades(self):
        """الحصول على جميع الصفقات النشطة"""
        return self.active_trades.copy()

class PerformanceReporter:
    """كلاس محسن لتقارير أداء البوت"""
    
    def __init__(self, trade_manager, notifier):
        self.trade_manager = trade_manager
        self.notifier = notifier
        self.start_time = datetime.now(damascus_tz)
        self.initial_balance = 0.0
        self.current_balance = 0.0
        self.daily_stats = {
            'trades_opened': 0,
            'trades_closed': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_pnl': 0.0,
            'total_fees': 0.0
        }
        
    def initialize_balances(self, client):
        """تهيئة الأرصدة من المنصة"""
        try:
            account_info = client.futures_account()
            self.initial_balance = float(account_info['totalWalletBalance'])
            self.current_balance = self.initial_balance
            logger.info(f"💰 تم تهيئة الرصيد: ${self.initial_balance:.2f}")
        except Exception as e:
            logger.error(f"❌ خطأ في تهيئة الأرصدة: {e}")
    
    def generate_performance_report(self):
        """إنشاء تقرير أداء"""
        if not self.notifier:
            return
            
        try:
            current_time = datetime.now(damascus_tz)
            uptime = current_time - self.start_time
            hours = uptime.total_seconds() // 3600
            minutes = (uptime.total_seconds() % 3600) // 60
            
            active_trades = self.trade_manager.get_active_trades_count()
            
            report = f"""
📊 <b>تقرير أداء البوت</b>

⏰ <b>معلومات الوقت:</b>
• وقت التشغيل: {hours:.0f} ساعة {minutes:.0f} دقيقة
• وقت التقرير: {current_time.strftime('%Y-%m-%d %H:%M:%S')}

💰 <b>الأداء المالي:</b>
• الرصيد الأولي: ${self.initial_balance:.2f}
• الرصيد الحالي: ${self.current_balance:.2f}
• التغير: ${(self.current_balance - self.initial_balance):+.2f}

📈 <b>إحصائيات التداول:</b>
• الصفقات النشطة: {active_trades}
• الصفقات المفتوحة اليوم: {self.daily_stats['trades_opened']}
• الصفقات المغلقة اليوم: {self.daily_stats['trades_closed']}
• الصفقات الرابحة: {self.daily_stats['winning_trades']}
• الصفقات الخاسرة: {self.daily_stats['losing_trades']}

🎯 <b>الصفقات النشطة حالياً:</b>
"""
            
            active_trades_details = self.trade_manager.get_all_trades()
            if active_trades_details:
                for symbol, trade in active_trades_details.items():
                    report += f"• {symbol} ({trade['side']}) - الدخول: ${trade['entry_price']:.4f}\n"
            else:
                report += "• لا توجد صفقات نشطة\n"
                
            self.notifier.send_message(report, 'performance_report')
            logger.info("✅ تم إرسال تقرير الأداء")
            
        except Exception as e:
            logger.error(f"❌ خطأ في إنشاء تقرير الأداء: {e}")

class PriceManager:
    """مدير محسن للأسعار"""
    
    def __init__(self, symbols, client):
        self.symbols = symbols
        self.client = client
        self.prices = {}
        self.last_update = {}
        
    def update_prices(self):
        """تحديث الأسعار"""
        try:
            for symbol in self.symbols:
                try:
                    ticker = self.client.futures_symbol_ticker(symbol=symbol)
                    price = float(ticker.get('price', 0))
                    if price > 0:
                        self.prices[symbol] = price
                        self.last_update[symbol] = time.time()
                except Exception as e:
                    logger.error(f"❌ خطأ في تحديث سعر {symbol}: {str(e)}")
            return True
        except Exception as e:
            logger.error(f"❌ خطأ في تحديث الأسعار: {str(e)}")
            return False

    def get_price(self, symbol):
        """الحصول على سعر رمز"""
        return self.prices.get(symbol)

class TelegramNotifier:
    """مدير إشعارات التلغرام"""
    
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.recent_messages = {}
        self.message_cooldown = 60

    def send_message(self, message, message_type='info'):
        try:
            if len(message) > 4096:
                message = message[:4090] + "..."

            current_time = time.time()
            message_hash = hashlib.md5(f"{message_type}_{message}".encode()).hexdigest()
            
            # منع تكرار الرسائل
            if message_hash in self.recent_messages:
                if current_time - self.recent_messages[message_hash] < self.message_cooldown:
                    return True

            self.recent_messages[message_hash] = current_time
            
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id, 
                'text': message, 
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }
            
            response = requests.post(url, data=payload, timeout=15)
            return response.status_code == 200
                
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال رسالة تلغرام: {e}")
            return False

class FuturesTradingBot:
    _instance = None
    
    # الإعدادات المحسنة
    OPTIMAL_SETTINGS = {
        'symbols': ["LINKUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT"],
        'weights': {'LINKUSDT': 1.4, 'SOLUSDT': 1.2, 'ETHUSDT': 1.0, 'BNBUSDT': 0.7},
    }
    
    TOTAL_CAPITAL = 50

    TRADING_SETTINGS = {
        'base_trade_size': 10,
        'max_leverage': 5,
        'margin_type': 'ISOLATED',
        'max_active_trades': 3,
        'data_interval': '30m',
        'rescan_interval_minutes': 10,
        'price_update_interval': 3,
        'trade_timeout_hours': 8.0,
        'min_signal_score': 4.5,  # نظام مرجح بدلاً من العد الثابت
        'atr_stop_loss_multiplier': 1.5,
        'atr_take_profit_multiplier': 3.0,
        'min_trade_duration_minutes': 45,
        'min_notional_value': 10.0,
        'min_trend_strength': 0.5,
        'max_price_deviation': 8.0,
        'max_volatility': 5.0,
    }

    @classmethod
    def get_instance(cls):
        return cls._instance

    def __init__(self):
        if FuturesTradingBot._instance is not None:
            raise Exception("هذه الفئة تستخدم نمط Singleton")

        # تهيئة المتغيرات
        self.api_key = os.environ.get('BINANCE_API_KEY')
        self.api_secret = os.environ.get('BINANCE_API_SECRET')
        self.telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')

        if not all([self.api_key, self.api_secret]):
            raise ValueError("مفاتيح Binance مطلوبة")

        # تهيئة العميل
        try:
            self.client = Client(self.api_key, self.api_secret)
            self.test_api_connection()
        except Exception as e:
            logger.error(f"❌ فشل تهيئة العميل: {e}")
            raise

        # تهيئة الإشعارات
        self.notifier = None
        if self.telegram_token and self.telegram_chat_id:
            try:
                self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
                logger.info("✅ تهيئة Telegram Notifier ناجحة")
            except Exception as e:
                logger.error(f"❌ فشل تهيئة Telegram: {e}")

        # تهيئة المدراء
        self.symbols = self.OPTIMAL_SETTINGS['symbols']
        self.trade_manager = TradeManager(self.client, self.notifier)
        self.price_manager = PriceManager(self.symbols, self.client)
        self.performance_reporter = PerformanceReporter(self.trade_manager, self.notifier)
        
        # تهيئة الأرصدة
        self.symbol_balances = self.initialize_symbol_balances()
        self.performance_reporter.initialize_balances(self.client)
        
        # المزامنة الأولية
        self.trade_manager.sync_with_exchange()
        
        # بدء الخدمات
        self.start_price_updater()
        self.start_performance_reporting()
        self.start_trade_sync()
        self.send_startup_message()
        
        FuturesTradingBot._instance = self
        logger.info("✅ تم تهيئة البوت بنجاح")

    def initialize_symbol_balances(self):
        """تهيئة أرصدة الرموز"""
        weight_sum = sum(self.OPTIMAL_SETTINGS['weights'].values())
        return {
            symbol: (weight / weight_sum) * self.TOTAL_CAPITAL 
            for symbol, weight in self.OPTIMAL_SETTINGS['weights'].items()
        }

    def test_api_connection(self):
        """اختبار اتصال API"""
        try:
            server_time = self.client.futures_time()
            logger.info("✅ اتصال Binance API نشط")
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ Binance API: {e}")
            raise

    def start_price_updater(self):
        """بدء تحديث الأسعار"""
        def price_update_thread():
            while True:
                try:
                    self.price_manager.update_prices()
                    time.sleep(self.TRADING_SETTINGS['price_update_interval'] * 60)
                except Exception as e:
                    logger.error(f"❌ خطأ في تحديث الأسعار: {str(e)}")
                    time.sleep(30)

        threading.Thread(target=price_update_thread, daemon=True).start()

    def start_trade_sync(self):
        """بدء مزامنة الصفقات"""
        def sync_thread():
            while True:
                try:
                    self.trade_manager.sync_with_exchange()
                    time.sleep(60)  # مزامنة كل دقيقة
                except Exception as e:
                    logger.error(f"❌ خطأ في مزامنة الصفقات: {str(e)}")
                    time.sleep(30)

        threading.Thread(target=sync_thread, daemon=True).start()

    def start_performance_reporting(self):
        """بدء التقارير الدورية"""
        if self.notifier:
            schedule.every(3).hours.do(self.send_performance_report)
            schedule.every(30).minutes.do(self.send_heartbeat)
            logger.info("✅ تم جدولة تقارير الأداء")

    def send_performance_report(self):
        """إرسال تقرير الأداء"""
        self.performance_reporter.generate_performance_report()

    def send_heartbeat(self):
        """إرسال نبضة"""
        if self.notifier:
            active_trades = self.trade_manager.get_active_trades_count()
            heartbeat_msg = (
                "💓 <b>نبضة البوت</b>\n"
                f"الوقت: {datetime.now(damascus_tz).strftime('%H:%M:%S')}\n"
                f"الصفقات النشطة: {active_trades}\n"
                f"الحالة: 🟢 نشط"
            )
            self.notifier.send_message(heartbeat_msg, 'heartbeat')

    def send_startup_message(self):
        """إرسال رسالة بدء التشغيل"""
        if self.notifier:
            message = (
                "🚀 <b>بدء تشغيل بوت العقود الآجلة - النسخة المحسنة</b>\n\n"
                f"📊 <b>الميزات الجديدة:</b>\n"
                f"• نظام مزامنة دقيق للصفقات\n"
                f"• تحسين جودة الإشارات\n"
                f"• إدارة محسنة للموارد\n"
                f"• تقارير أداء دقيقة\n\n"
                f"🕒 <b>وقت البدء:</b>\n"
                f"{datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
            )
            self.notifier.send_message(message, 'startup')

    def get_current_price(self, symbol):
        """الحصول على السعر الحالي"""
        return self.price_manager.get_price(symbol)

    def get_historical_data(self, symbol, interval='30m', limit=100):
        """جلب البيانات التاريخية"""
        try:
            klines = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            
            data = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])
            
            for col in ['open', 'high', 'low', 'close', 'volume']:
                data[col] = data[col].astype(float)
            
            return data
        except Exception as e:
            logger.error(f"❌ خطأ في جلب البيانات لـ {symbol}: {e}")
            return None

    def calculate_indicators(self, data):
        """حساب المؤشرات الفنية"""
        try:
            df = data.copy()
            if len(df) < 50:
                return df

            # المتوسطات المتحركة
            df['sma10'] = df['close'].rolling(10).mean()
            df['sma50'] = df['close'].rolling(50).mean()
            df['sma20'] = df['close'].rolling(20).mean()
            
            # RSI
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = (-delta).where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-6)
            df['rsi'] = 100 - (100 / (1 + rs))
            
            # ATR
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            tr = np.maximum(np.maximum(high_low, high_close), low_close)
            df['atr'] = tr.rolling(14).mean()
            
            # الزخم والحجم
            df['momentum'] = df['close'] / df['close'].shift(5) - 1
            df['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
            
            # MACD
            exp12 = df['close'].ewm(span=12, adjust=False).mean()
            exp26 = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = exp12 - exp26
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
            
            return df.dropna()
        except Exception as e:
            logger.error(f"❌ خطأ في حساب المؤشرات: {e}")
            return data

    def analyze_symbol(self, symbol):
        """تحليل الرمز مع النظام المرجح المحسن"""
        try:
            data = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'])
            if data is None or len(data) < 50:
                return False, {}, None

            data = self.calculate_indicators(data)
            if len(data) == 0:
                return False, {}, None

            latest = data.iloc[-1]
            
            # ✅ شروط الدخول المحسنة بنظام مرجح
            buy_conditions = [
                (latest['sma10'] > latest['sma50']),  # شرط الاتجاه الرئيسي
                (latest['sma10'] > latest['sma20'] * 0.998),  # شرط مرن
                (45 <= latest['rsi'] <= 68),  # RSI في نطاق آمن
                (latest['momentum'] > 0.002),  # زخم إيجابي
                (latest['volume_ratio'] > 0.8),  # حجم معقول
                (latest['macd'] > latest['macd_signal'] * 0.95)  # MACD إيجابي
            ]
            
            sell_conditions = [
                (latest['sma10'] < latest['sma50']),
                (latest['sma10'] < latest['sma20'] * 1.002),
                (32 <= latest['rsi'] <= 55),
                (latest['momentum'] < -0.002),
                (latest['volume_ratio'] > 0.8),
                (latest['macd'] < latest['macd_signal'] * 1.05)
            ]
            
            # ✅ نظام الترجيح
            buy_score = sum([
                1.5 if buy_conditions[0] else 0,
                1.0 if buy_conditions[1] else 0,
                1.2 if buy_conditions[2] else 0,
                1.0 if buy_conditions[3] else 0,
                0.8 if buy_conditions[4] else 0,
                1.0 if buy_conditions[5] else 0,
            ])
            
            sell_score = sum([
                1.5 if sell_conditions[0] else 0,
                1.0 if sell_conditions[1] else 0,
                1.2 if sell_conditions[2] else 0,
                1.0 if sell_conditions[3] else 0,
                0.8 if sell_conditions[4] else 0,
                1.0 if sell_conditions[5] else 0,
            ])
            
            # ✅ تحديد الإشارة
            buy_signal = buy_score >= self.TRADING_SETTINGS['min_signal_score']
            sell_signal = sell_score >= self.TRADING_SETTINGS['min_signal_score']
            
            direction = None
            signal_strength = 0
            
            if buy_signal:
                direction = 'LONG'
                signal_strength = (buy_score / 6.5) * 100
            elif sell_signal:
                direction = 'SHORT'
                signal_strength = (sell_score / 6.5) * 100

            details = {
                'signal_strength': signal_strength,
                'sma10': latest['sma10'],
                'sma20': latest['sma20'],
                'sma50': latest['sma50'],
                'rsi': latest['rsi'],
                'price': latest['close'],
                'atr': latest['atr'],
                'momentum': latest['momentum'],
                'volume_ratio': latest['volume_ratio'],
                'buy_score': buy_score,
                'sell_score': sell_score,
                'direction': direction,
                'price_vs_sma20': (latest['close'] - latest['sma20']) / latest['sma20'] * 100,
                'trend_strength': (latest['sma10'] - latest['sma50']) / latest['sma50'] * 100,
            }

            return direction is not None, details, direction

        except Exception as e:
            logger.error(f"❌ خطأ في تحليل {symbol}: {e}")
            return False, {}, None

    def should_accept_signal(self, symbol, direction, analysis):
        """فلاتر الجودة للإشارات"""
        
        # تجنب الذروة في RSI
        if analysis['rsi'] > 70 and direction == 'LONG':
            logger.info(f"⏸️ تجنب LONG - RSI مرتفع: {analysis['rsi']:.1f}")
            return False
            
        if analysis['rsi'] < 30 and direction == 'SHORT':
            logger.info(f"⏸️ تجنب SHORT - RSI منخفض: {analysis['rsi']:.1f}")
            return False
        
        # قوة الاتجاه
        if abs(analysis['trend_strength']) < self.TRADING_SETTINGS['min_trend_strength']:
            logger.info(f"⏸️ إشارة ضعيفة - اتجاه ضعيف: {analysis['trend_strength']:.2f}%")
            return False
        
        # تقلبات السعر
        if analysis['atr'] / analysis['price'] > self.TRADING_SETTINGS['max_volatility'] / 100:
            logger.info(f"⏸️ تقلبات عالية - ATR: {(analysis['atr']/analysis['price']*100):.1f}%")
            return False
            
        # انحراف السعر عن المتوسط
        if abs(analysis['price_vs_sma20']) > self.TRADING_SETTINGS['max_price_deviation']:
            logger.info(f"⏸️ سعر بعيد عن المتوسط: {analysis['price_vs_sma20']:.1f}%")
            return False
        
        return True

    def can_open_trade(self, symbol):
        """التحقق من إمكانية فتح صفقة"""
        reasons = []
        
        # التحقق من الحد الأقصى للصفقات
        if self.trade_manager.get_active_trades_count() >= self.TRADING_SETTINGS['max_active_trades']:
            reasons.append(f"الحد الأقصى للصفقات ({self.TRADING_SETTINGS['max_active_trades']})")
            
        # التحقق من وجود صفقة نشطة لنفس الرمز
        if self.trade_manager.is_symbol_trading(symbol):
            reasons.append("صفقة نشطة موجودة")
            
        # التحقق من الرصيد المتاح
        available_balance = self.symbol_balances.get(symbol, 0)
        if available_balance < 5:
            reasons.append(f"رصيد غير كافي: ${available_balance:.2f}")
            
        return len(reasons) == 0, reasons

    def get_futures_precision(self, symbol):
        """الحصول على معلومات الدقة"""
        try:
            info = self.client.futures_exchange_info()
            symbol_info = next((s for s in info['symbols'] if s['symbol'] == symbol), None)
            
            if symbol_info:
                lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
                price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
                min_notional_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'MIN_NOTIONAL'), None)
                
                min_notional = float(min_notional_filter['notional']) if min_notional_filter else self.TRADING_SETTINGS['min_notional_value']
                step_size = float(lot_size['stepSize']) if lot_size else 0.001
                
                precision = 0
                if step_size < 1:
                    precision = int(round(-np.log10(step_size)))
                
                return {
                    'step_size': step_size,
                    'tick_size': float(price_filter['tickSize']) if price_filter else 0.001,
                    'precision': precision,
                    'min_qty': float(lot_size['minQty']) if lot_size else 0.001,
                    'min_notional': min_notional
                }
            
            return {
                'step_size': 0.001, 
                'tick_size': 0.001, 
                'precision': 3, 
                'min_qty': 0.001, 
                'min_notional': self.TRADING_SETTINGS['min_notional_value']
            }
        except Exception as e:
            logger.error(f"❌ خطأ في جلب دقة العقود: {e}")
            return {
                'step_size': 0.001, 
                'tick_size': 0.001, 
                'precision': 3, 
                'min_qty': 0.001, 
                'min_notional': self.TRADING_SETTINGS['min_notional_value']
            }

    def calculate_position_size(self, symbol, direction, analysis, available_balance):
        """حساب حجم المركز"""
        try:
            current_price = self.get_current_price(symbol)
            if not current_price:
                return None, None, None

            precision_info = self.get_futures_precision(symbol)
            step_size = precision_info['step_size']
            min_notional = precision_info['min_notional']
            
            leverage = self.TRADING_SETTINGS['max_leverage']
            position_value = min(available_balance * leverage, self.TRADING_SETTINGS['base_trade_size'])
            
            if position_value < min_notional:
                position_value = min_notional * 1.1
            
            quantity = position_value / current_price
            
            if step_size > 0:
                quantity = round(quantity / step_size) * step_size
            
            if quantity < precision_info['min_qty']:
                quantity = precision_info['min_qty']
                position_value = quantity * current_price
            
            if position_value < min_notional:
                return None, None, None
            
            atr = analysis.get('atr', current_price * 0.02)
            stop_loss_pct = (self.TRADING_SETTINGS['atr_stop_loss_multiplier'] * atr / current_price)
            take_profit_pct = (self.TRADING_SETTINGS['atr_take_profit_multiplier'] * atr / current_price)
            
            if direction == 'LONG':
                stop_loss_price = current_price * (1 - stop_loss_pct)
                take_profit_price = current_price * (1 + take_profit_pct)
            else:
                stop_loss_price = current_price * (1 + stop_loss_pct)
                take_profit_price = current_price * (1 - take_profit_pct)
            
            return quantity, stop_loss_price, take_profit_price
            
        except Exception as e:
            logger.error(f"❌ خطأ في حساب حجم المركز لـ {symbol}: {e}")
            return None, None, None

    def set_leverage(self, symbol, leverage):
        """ضبط الرافعة المالية"""
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            return True
        except Exception as e:
            logger.warning(f"⚠️ خطأ في ضبط الرافعة لـ {symbol}: {e}")
            return True

    def set_margin_type(self, symbol, margin_type):
        """ضبط نوع الهامش"""
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            return True
        except Exception as e:
            error_msg = str(e)
            if "No need to change margin type" in error_msg:
                return True
            elif "Account has open positions" in error_msg:
                return True
            else:
                logger.warning(f"⚠️ فشل ضبط نوع الهامش لـ {symbol}: {error_msg}")
                return True

    def execute_trade(self, symbol, direction, quantity, stop_loss_price, take_profit_price, analysis):
        """تنفيذ الصفقة"""
        try:
            current_price = self.get_current_price(symbol)
            if not current_price:
                raise Exception("لا يمكن الحصول على السعر الحالي")
                
            notional_value = quantity * current_price
            min_notional = self.get_futures_precision(symbol)['min_notional']
            
            if notional_value < min_notional:
                raise Exception(f"القيمة الاسمية ${notional_value:.2f} أقل من الحد الأدنى ${min_notional:.2f}")
            
            if not self.set_leverage(symbol, self.TRADING_SETTINGS['max_leverage']):
                raise Exception("فشل ضبط الرافعة")
                
            if not self.set_margin_type(symbol, self.TRADING_SETTINGS['margin_type']):
                raise Exception("فشل ضبط نوع الهامش")
            
            side = 'BUY' if direction == 'LONG' else 'SELL'
            
            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type='MARKET',
                quantity=quantity,
                reduceOnly=False
            )
            
            # انتظار التنفيذ
            executed_qty = 0
            for i in range(10):
                time.sleep(0.5)
                order_status = self.client.futures_get_order(symbol=symbol, orderId=order['orderId'])
                executed_qty = float(order_status.get('executedQty', 0))
                if executed_qty > 0:
                    break
            
            if executed_qty == 0:
                try:
                    self.client.futures_cancel_order(symbol=symbol, orderId=order['orderId'])
                except:
                    pass
                raise Exception("الأمر لم ينفذ")
            
            # الحصول على سعر الدخول الفعلي
            avg_price = float(order_status.get('avgPrice', 0))
            if avg_price == 0:
                avg_price = current_price
            
            # تسجيل الصفقة
            trade_data = {
                'symbol': symbol,
                'quantity': quantity,
                'entry_price': avg_price,
                'leverage': self.TRADING_SETTINGS['max_leverage'],
                'side': direction,
                'timestamp': datetime.now(damascus_tz),
                'status': 'open',
                'trade_type': 'futures',
                'stop_loss': stop_loss_price,
                'take_profit': take_profit_price,
                'order_id': order['orderId']
            }
            
            self.trade_manager.add_trade(symbol, trade_data)
            
            # تحديث الرصيد
            trade_value_leverage = notional_value / self.TRADING_SETTINGS['max_leverage']
            self.symbol_balances[symbol] = max(0, self.symbol_balances[symbol] - trade_value_leverage)
            
            if self.notifier:
                message = (
                    f"✅ <b>تم فتح الصفقة بنجاح</b>\n"
                    f"العملة: {symbol}\n"
                    f"الاتجاه: {direction}\n"
                    f"الكمية: {quantity:.6f}\n"
                    f"سعر الدخول: ${avg_price:.4f}\n"
                    f"وقف الخسارة: ${stop_loss_price:.4f}\n"
                    f"جني الأرباح: ${take_profit_price:.4f}\n"
                    f"الرافعة: {self.TRADING_SETTINGS['max_leverage']}x\n"
                    f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_open')
            
            logger.info(f"✅ تم فتح صفقة {direction} لـ {symbol}")
            return True
            
        except Exception as e:
            logger.error(f"❌ فشل تنفيذ صفقة {symbol}: {e}")
            
            if self.notifier:
                message = (
                    f"❌ <b>فشل تنفيذ صفقة</b>\n"
                    f"العملة: {symbol}\n"
                    f"الاتجاه: {direction}\n"
                    f"السبب: {str(e)}"
                )
                self.notifier.send_message(message, 'trade_failed')
            
            return False

    def send_trade_signal_notification(self, symbol, direction, analysis, can_trade, reasons=None):
        """إرسال إشعار إشارة التداول"""
        if not self.notifier:
            return
            
        try:
            if can_trade:
                message = (
                    f"🔔 <b>إشارة تداول قوية - جاهزة للتنفيذ</b>\n"
                    f"العملة: {symbol}\n"
                    f"الاتجاه: {direction}\n"
                    f"قوة الإشارة: {analysis['signal_strength']:.1f}%\n"
                    f"السعر الحالي: ${analysis['price']:.4f}\n"
                    f"الرصيد المتاح: ${self.symbol_balances.get(symbol, 0):.2f}\n"
                    f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"<b>تفاصيل المؤشرات:</b>\n"
                    f"• SMA10: {analysis['sma10']:.4f}\n"
                    f"• SMA20: {analysis['sma20']:.4f}\n"
                    f"• SMA50: {analysis['sma50']:.4f}\n"
                    f"• RSI: {analysis['rsi']:.2f}\n"
                    f"• Momentum: {analysis['momentum']:.4f}\n"
                    f"• Volume Ratio: {analysis['volume_ratio']:.2f}"
                )
            else:
                message = (
                    f"⚠️ <b>إشارة تداول - غير قابلة للتنفيذ</b>\n"
                    f"العملة: {symbol}\n"
                    f"الاتجاه: {direction}\n"
                    f"قوة الإشارة: {analysis['signal_strength']:.1f}%\n"
                    f"<b>أسباب عدم التنفيذ:</b>\n"
                )
                for reason in reasons:
                    message += f"• {reason}\n"
                message += f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
            
            self.notifier.send_message(message, 'trade_signal')
            
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال إشعار الإشارة: {e}")

    def check_trade_timeout(self):
        """فحص انتهاء وقت الصفقات"""
        current_time = datetime.now(damascus_tz)
        symbols_to_close = []
        
        for symbol, trade in self.trade_manager.get_all_trades().items():
            trade_age = current_time - trade['timestamp']
            hours_open = trade_age.total_seconds() / 3600
            
            if hours_open >= self.TRADING_SETTINGS['trade_timeout_hours']:
                symbols_to_close.append(symbol)
                logger.info(f"⏰ انتهاء وقت الصفقة لـ {symbol}")
        
        for symbol in symbols_to_close:
            self.close_trade(symbol, 'timeout')

    def close_trade(self, symbol, reason='manual'):
        """إغلاق الصفقة"""
        try:
            trade = self.trade_manager.get_trade(symbol)
            if not trade:
                return False

            current_price = self.get_current_price(symbol)
            if not current_price:
                return False

            side = 'SELL' if trade['side'] == 'LONG' else 'BUY'
            
            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type='MARKET',
                quantity=trade['quantity'],
                reduceOnly=True
            )
            
            # انتظار التنفيذ
            executed_qty = 0
            for i in range(10):
                time.sleep(0.5)
                order_status = self.client.futures_get_order(symbol=symbol, orderId=order['orderId'])
                executed_qty = float(order_status.get('executedQty', 0))
                if executed_qty > 0:
                    break
            
            if executed_qty == 0:
                raise Exception("أمر الإغلاق لم ينفذ")
            
            # إزالة الصفقة
            self.trade_manager.remove_trade(symbol)
            
            # استعادة الرصيد
            trade_value_leverage = (trade['quantity'] * trade['entry_price']) / trade['leverage']
            self.symbol_balances[symbol] += trade_value_leverage
            
            if self.notifier:
                message = (
                    f"🔒 <b>تم إغلاق الصفقة</b>\n"
                    f"العملة: {symbol}\n"
                    f"الاتجاه: {trade['side']}\n"
                    f"السبب: {reason}\n"
                    f"سعر الدخول: ${trade['entry_price']:.4f}\n"
                    f"سعر الخروج: ${current_price:.4f}\n"
                    f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_close')
            
            logger.info(f"✅ تم إغلاق صفقة {symbol}")
            return True
            
        except Exception as e:
            logger.error(f"❌ فشل إغلاق صفقة {symbol}: {e}")
            return False

    def get_active_trades_details(self):
        """الحصول على تفاصيل الصفقات النشطة"""
        return self.trade_manager.get_all_trades()

    def scan_and_trade(self):
        """المسح الضوئي وتنفيذ الصفقات"""
        try:
            logger.info("🔍 بدء المسح الضوئي للفرص...")
            
            # فحص انتهاء وقت الصفقات
            self.check_trade_timeout()
            
            # التحقق من عدد الصفقات النشطة
            if self.trade_manager.get_active_trades_count() >= self.TRADING_SETTINGS['max_active_trades']:
                logger.info("⏸️ إيقاف المسح - الحد الأقصى للصفقات")
                return
            
            for symbol in self.symbols:
                try:
                    # تخطي الرموز التي بها صفقات نشطة
                    if self.trade_manager.is_symbol_trading(symbol):
                        continue
                    
                    # تحليل الرمز
                    has_signal, analysis, direction = self.analyze_symbol(symbol)
                    
                    if has_signal and direction:
                        # تطبيق فلاتر الجودة
                        if not self.should_accept_signal(symbol, direction, analysis):
                            continue
                        
                        can_trade, reasons = self.can_open_trade(symbol)
                        
                        self.send_trade_signal_notification(symbol, direction, analysis, can_trade, reasons)
                        
                        if can_trade:
                            available_balance = self.symbol_balances.get(symbol, 0)
                            quantity, stop_loss, take_profit = self.calculate_position_size(
                                symbol, direction, analysis, available_balance
                            )
                            
                            if quantity and quantity > 0:
                                success = self.execute_trade(symbol, direction, quantity, stop_loss, take_profit, analysis)
                                
                                if success:
                                    logger.info(f"✅ تم تنفيذ صفقة {direction} لـ {symbol}")
                    
                    time.sleep(1)
                    
                except Exception as e:
                    logger.error(f"❌ خطأ في معالجة {symbol}: {e}")
                    continue
            
            logger.info("✅ اكتمل المسح الضوئي")
            
        except Exception as e:
            logger.error(f"❌ خطأ في المسح الضوئي: {e}")

    def run(self):
        """تشغيل البوت الرئيسي"""
        logger.info("🚀 بدء تشغيل بوت العقود الآجلة...")
        
        # بدء خادم Flask
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        try:
            while True:
                try:
                    schedule.run_pending()
                    self.scan_and_trade()
                    
                    sleep_minutes = self.TRADING_SETTINGS['rescan_interval_minutes']
                    logger.info(f"⏳ انتظار {sleep_minutes} دقيقة للمسح التالي...")
                    time.sleep(sleep_minutes * 60)
                    
                except KeyboardInterrupt:
                    logger.info("⏹️ إيقاف البوت يدوياً...")
                    break
                except Exception as e:
                    logger.error(f"❌ خطأ في الحلقة الرئيسية: {e}")
                    time.sleep(60)
                    
        except Exception as e:
            logger.error(f"❌ خطأ غير متوقع: {e}")
        finally:
            logger.info("🛑 إيقاف البوت...")

def main():
    try:
        bot = FuturesTradingBot()
        bot.run()
    except Exception as e:
        logger.error(f"❌ فشل تشغيل البوت: {e}")

if __name__ == "__main__":
    main()
