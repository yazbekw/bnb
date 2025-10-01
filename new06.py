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
            active = list(bot.active_trades.values())
            return jsonify(active)
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

class PerformanceReporter:
    """كلاس مستقل لتقارير أداء البوت"""
    
    def __init__(self, bot_instance, notifier):
        self.bot = bot_instance
        self.notifier = notifier
        self.start_time = datetime.now(damascus_tz)
        self.trade_history = []
        self.daily_stats = {
            'trades_opened': 0,
            'trades_closed': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_pnl': 0.0,
            'max_balance': 0.0,
            'min_balance': float('inf')
        }
        
    def record_trade_open(self, symbol, direction, entry_price, size_usd):
        """تسجيل فتح صفقة جديدة"""
        trade_record = {
            'symbol': symbol,
            'direction': direction,
            'entry_price': entry_price,
            'exit_price': None,
            'size_usd': size_usd,
            'open_time': datetime.now(damascus_tz),
            'close_time': None,
            'pnl_percent': 0.0,
            'pnl_usd': 0.0,
            'status': 'open'
        }
        self.trade_history.append(trade_record)
        self.daily_stats['trades_opened'] += 1
        
    def record_trade_close(self, symbol, exit_price, pnl_percent, pnl_usd, reason):
        """تسجيل إغلاق صفقة"""
        for trade in self.trade_history:
            if trade['symbol'] == symbol and trade['status'] == 'open':
                trade['exit_price'] = exit_price
                trade['close_time'] = datetime.now(damascus_tz)
                trade['pnl_percent'] = pnl_percent
                trade['pnl_usd'] = pnl_usd
                trade['status'] = 'closed'
                trade['close_reason'] = reason
                
                self.daily_stats['trades_closed'] += 1
                self.daily_stats['total_pnl'] += pnl_usd
                
                if pnl_usd > 0:
                    self.daily_stats['winning_trades'] += 1
                else:
                    self.daily_stats['losing_trades'] += 1
                break
                
    def update_balance_stats(self, current_balance):
        """تحديث إحصائيات الرصيد"""
        total_balance = sum(current_balance.values()) if isinstance(current_balance, dict) else current_balance
        self.daily_stats['max_balance'] = max(self.daily_stats['max_balance'], total_balance)
        self.daily_stats['min_balance'] = min(self.daily_stats['min_balance'], total_balance)
        
    def calculate_performance_metrics(self):
        """حساب مقاييس الأداء"""
        closed_trades = [t for t in self.trade_history if t['status'] == 'closed']
        open_trades = [t for t in self.trade_history if t['status'] == 'open']
        
        if not closed_trades:
            return {
                'win_rate': 0,
                'avg_win': 0,
                'avg_loss': 0,
                'profit_factor': 0,
                'total_trades': 0,
                'active_trades': len(open_trades)
            }
        
        winning_trades = [t for t in closed_trades if t['pnl_usd'] > 0]
        losing_trades = [t for t in closed_trades if t['pnl_usd'] < 0]
        
        win_rate = (len(winning_trades) / len(closed_trades)) * 100
        avg_win = np.mean([t['pnl_usd'] for t in winning_trades]) if winning_trades else 0
        avg_loss = np.mean([t['pnl_usd'] for t in losing_trades]) if losing_trades else 0
        
        total_profit = sum(t['pnl_usd'] for t in winning_trades)
        total_loss = abs(sum(t['pnl_usd'] for t in losing_trades))
        profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')
        
        return {
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': profit_factor,
            'total_trades': len(closed_trades),
            'active_trades': len(open_trades)
        }
        
    def generate_performance_report(self):
        """إنشاء تقرير أداء مفصل"""
        if not self.notifier:
            return
            
        try:
            # حساب الوقت المنقضي
            current_time = datetime.now(damascus_tz)
            uptime = current_time - self.start_time
            hours = uptime.total_seconds() // 3600
            minutes = (uptime.total_seconds() % 3600) // 60
            
            # مقاييس الأداء
            metrics = self.calculate_performance_metrics()
            
            # الرصيد الحالي
            current_balance = sum(self.bot.symbol_balances.values()) if hasattr(self.bot, 'symbol_balances') else 0
            initial_balance = self.bot.TOTAL_CAPITAL
            balance_change = current_balance - initial_balance
            balance_change_percent = (balance_change / initial_balance) * 100
            
            # الصفقات النشطة
            active_trades = self.bot.active_trades if hasattr(self.bot, 'active_trades') else {}
            
            # إنشاء التقرير
            report = f"""
📊 <b>تقرير أداء البوت - كل 3 ساعات</b>

⏰ <b>معلومات الوقت:</b>
• وقت التشغيل: {hours:.0f} ساعة {minutes:.0f} دقيقة
• وقت التقرير: {current_time.strftime('%Y-%m-%d %H:%M:%S')}

💰 <b>الأداء المالي:</b>
• الرصيد الأولي: ${initial_balance:.2f}
• الرصيد الحالي: ${current_balance:.2f}
• التغير: ${balance_change:+.2f} ({balance_change_percent:+.2f}%)
• أعلى رصيد: ${self.daily_stats['max_balance']:.2f}
• أقل رصيد: ${self.daily_stats['min_balance']:.2f}

📈 <b>إحصائيات التداول:</b>
• إجمالي الصفقات: {metrics['total_trades']}
• الصفقات النشطة: {metrics['active_trades']}
• نسبة الربح: {metrics['win_rate']:.1f}%
• متوسط الربح: ${metrics['avg_win']:.2f}
• متوسط الخسارة: ${metrics['avg_loss']:.2f}
• عامل الربحية: {metrics['profit_factor']:.2f}

🔍 <b>تفاصيل الصفقات:</b>
• الصفقات المفتوحة: {self.daily_stats['trades_opened']}
• الصفقات المغلقة: {self.daily_stats['trades_closed']}
• الصفقات الرابحة: {self.daily_stats['winning_trades']}
• الصفقات الخاسرة: {self.daily_stats['losing_trades']}

🎯 <b>الصفقات النشطة حالياً:</b>
"""
            
            if active_trades:
                for symbol, trade in active_trades.items():
                    trade_age = current_time - trade['timestamp']
                    age_minutes = trade_age.total_seconds() / 60
                    report += f"• {symbol} ({trade['side']}) - {age_minutes:.0f} دقيقة\n"
            else:
                report += "• لا توجد صفقات نشطة\n"
                
            report += f"\n⚡ <b>حالة البوت:</b> {'🟢 نشط' if self.bot else '🔴 متوقف'}"
            
            # إرسال التقرير
            self.notifier.send_message(report, 'performance_report')
            logger.info("✅ تم إرسال تقرير الأداء")
            
        except Exception as e:
            logger.error(f"❌ خطأ في إنشاء تقرير الأداء: {e}")
            
    def reset_daily_stats(self):
        """إعادة تعيين إحصائيات اليوم"""
        self.daily_stats = {
            'trades_opened': 0,
            'trades_closed': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_pnl': 0.0,
            'max_balance': 0.0,
            'min_balance': float('inf')
        }

class PriceManager:
    def __init__(self, symbols, client):
        self.symbols = symbols
        self.client = client
        self.prices = {}
        self.last_update = {}

    def update_prices(self):
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
        try:
            last_update = self.last_update.get(symbol, 0)
            if time.time() - last_update > 30:
                self.update_prices()
            return self.prices.get(symbol)
        except Exception as e:
            logger.error(f"❌ خطأ في جلب سعر {symbol}: {str(e)}")
            return None

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.recent_messages = {}
        self.message_cooldown = 60

    def send_message(self, message, message_type='info', retries=3, delay=5):
        try:
            if len(message) > 4096:
                message = message[:4090] + "..."

            current_time = time.time()
            message_hash = hashlib.md5(f"{message_type}_{message}".encode()).hexdigest()
            
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
            
            for attempt in range(retries):
                try:
                    response = requests.post(url, data=payload, timeout=15)
                    if response.status_code == 200:
                        result = response.json()
                        if result.get('ok'):
                            return True
                    
                    time.sleep(delay * (2 ** attempt))
                        
                except Exception as e:
                    time.sleep(delay * (2 ** attempt))
            
            return False
                
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال رسالة تلغرام: {e}")
            return False

class FuturesTradingBot:
    _instance = None
    
    OPTIMAL_SETTINGS = {
        'symbols': ["LINKUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT"],
        'intervals': ['30m', '1h'],
        'weights': {'LINKUSDT': 1.4, 'SOLUSDT': 1.2, 'ETHUSDT': 1.0, 'BNBUSDT': 0.7},
    }
    
    TOTAL_CAPITAL = 50

    TRADING_SETTINGS = {
        'base_trade_size': 10,
        'max_leverage': 5,
        'margin_type': 'ISOLATED',
        'max_active_trades': 4,
        'data_interval': '30m',
        'rescan_interval_minutes': 15,
        'price_update_interval': 2,
        'trade_timeout_hours': 8.0,
        'min_signal_conditions': 4,
        'atr_stop_loss_multiplier': 1.5,
        'atr_take_profit_multiplier': 3.0,
        'min_trade_duration_minutes': 30,
    }

    @classmethod
    def get_instance(cls):
        return cls._instance

    def __init__(self):
        if FuturesTradingBot._instance is not None:
            raise Exception("هذه الفئة تستخدم نمط Singleton")

        self.WEIGHT_SUM = sum(self.OPTIMAL_SETTINGS['weights'].values())
        self.CAPITAL_ALLOCATION = {
            symbol: (weight / self.WEIGHT_SUM) * self.TOTAL_CAPITAL 
            for symbol, weight in self.OPTIMAL_SETTINGS['weights'].items()
        }
        
        self.api_key = os.environ.get('BINANCE_API_KEY')
        self.api_secret = os.environ.get('BINANCE_API_SECRET')
        self.telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')

        if not all([self.api_key, self.api_secret]):
            raise ValueError("مفاتيح Binance مطلوبة")

        self.notifier = None
        if self.telegram_token and self.telegram_chat_id:
            try:
                self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
                logger.info("✅ تهيئة Telegram Notifier ناجحة")
                # اختبار الاتصال فوراً
                self.test_telegram_connection()
            except Exception as e:
                logger.error(f"❌ فشل تهيئة Telegram: {e}")
                self.notifier = None

        # تهيئة تقارير الأداء
        self.performance_reporter = PerformanceReporter(self, self.notifier)

        try:
            self.client = Client(self.api_key, self.api_secret)
            self.test_api_connection()
        except Exception as e:
            logger.error(f"❌ فشل تهيئة العميل: {e}")
            raise

        self.symbols = self.OPTIMAL_SETTINGS['symbols']
        self.verify_symbols_availability()
    
        self.active_trades = {}
        self.price_manager = PriceManager(self.symbols, self.client)
        self.symbol_balances = self.CAPITAL_ALLOCATION.copy()
        
        self.load_existing_trades()
        self.start_price_updater()
        self.start_performance_reporting()
        self.send_startup_message()
        
        FuturesTradingBot._instance = self
        logger.info("✅ تم تهيئة البوت بنجاح")

    def test_telegram_connection(self):
        """اختبار اتصال التلغرام"""
        if self.notifier:
            test_message = "🔊 <b>اختبار اتصال التلغرام</b>\n✅ البوت يعمل بشكل صحيح"
            success = self.notifier.send_message(test_message, 'test')
            if success:
                logger.info("✅ اختبار التلغرام ناجح")
            else:
                logger.error("❌ فشل اختبار التلغرام")

    def start_performance_reporting(self):
        """بدء إرسال التقارير الدورية"""
        if self.notifier:
            # تقرير كل 3 ساعات
            schedule.every(3).hours.do(self.send_performance_report)
            # تقرير يومي في منتصف الليل
            schedule.every().day.at("00:00").do(self.performance_reporter.reset_daily_stats)
            # نبضات كل 30 دقيقة
            schedule.every(30).minutes.do(self.send_heartbeat)
            logger.info("✅ تم جدولة تقارير الأداء الدورية")

    def send_performance_report(self):
        """إرسال تقرير الأداء"""
        if hasattr(self, 'performance_reporter'):
            # تحديث إحصائيات الرصيد
            current_balance = sum(self.symbol_balances.values())
            self.performance_reporter.update_balance_stats(current_balance)
            # إرسال التقرير
            self.performance_reporter.generate_performance_report()

    def send_heartbeat(self):
        """إرسال نبضات دورية للتأكد من عمل البوت"""
        if self.notifier:
            current_time = datetime.now(damascus_tz)
            active_trades = len(self.active_trades)
            
            heartbeat_msg = (
                "💓 <b>نبضة البوت</b>\n"
                f"الوقت: {current_time.strftime('%H:%M:%S')}\n"
                f"الصفقات النشطة: {active_trades}\n"
                f"الحالة: 🟢 نشط"
            )
            self.notifier.send_message(heartbeat_msg, 'heartbeat')

    def verify_symbols_availability(self):
        try:
            exchange_info = self.client.futures_exchange_info()
            available_symbols = [s['symbol'] for s in exchange_info['symbols']]
        
            valid_symbols = [s for s in self.symbols if s in available_symbols]
            if len(valid_symbols) != len(self.symbols):
                logger.warning(f"⚠️ تصحيح الرموز من {self.symbols} إلى {valid_symbols}")
                self.symbols = valid_symbols
            
        except Exception as e:
            logger.error(f"❌ خطأ في التحقق من الرموز: {e}")

    def test_api_connection(self):
        try:
            server_time = self.client.futures_time()
            logger.info(f"✅ اتصال Binance API نشط")
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ Binance API: {e}")
            raise

    def send_startup_message(self):
        if self.notifier:
            try:
                message = (
                    "🚀 <b>بدء تشغيل بوت العقود الآجلة - الإصدار المحسن</b>\n\n"
                    f"📊 <b>الميزات المحسنة:</b>\n"
                    f"• إشعارات فتح وإغلاق الصفقات\n"
                    f"• إشعارات أسباب عدم فتح الصفقات\n"
                    f"• منع فتح صفقات مكررة\n"
                    f"• تقرير أداء كل 3 ساعات\n"
                    f"• نبضات كل 30 دقيقة\n\n"
                    f"🕒 <b>وقت البدء:</b>\n"
                    f"{datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
                )
            
                self.notifier.send_message(message, 'startup')
                logger.info("✅ تم إرسال رسالة بدء التشغيل")
                
            except Exception as e:
                logger.error(f"❌ خطأ في إرسال رسالة البدء: {e}")

    def start_price_updater(self):
        def price_update_thread():
            while True:
                try:
                    self.price_manager.update_prices()
                    time.sleep(self.TRADING_SETTINGS['price_update_interval'] * 60)
                except Exception as e:
                    logger.error(f"❌ خطأ في تحديث الأسعار: {str(e)}")
                    time.sleep(30)

        threading.Thread(target=price_update_thread, daemon=True).start()

    def load_existing_trades(self):
        """تحميل الصفقات النشطة من Binance"""
        try:
            account_info = self.client.futures_account()
            positions = account_info['positions']
            
            open_positions = [p for p in positions if float(p['positionAmt']) != 0]
            
            for position in open_positions:
                symbol = position['symbol']
                if symbol in self.symbols:
                    quantity = float(position['positionAmt'])
                    if quantity != 0:
                        # تحديث الرصيد المتاح
                        trade_value = abs(quantity) * float(position['entryPrice']) / float(position['leverage'])
                        if symbol in self.symbol_balances:
                            self.symbol_balances[symbol] = max(0, self.symbol_balances[symbol] - trade_value)
                        
                        entry_price = float(position['entryPrice'])
                        leverage = float(position['leverage'])
                        side = "LONG" if quantity > 0 else "SHORT"
                        
                        trade_data = {
                            'symbol': symbol,
                            'quantity': abs(quantity),
                            'entry_price': entry_price,
                            'leverage': leverage,
                            'side': side,
                            'timestamp': datetime.now(damascus_tz),
                            'status': 'open',
                            'trade_type': 'futures',
                        }
                        
                        self.active_trades[symbol] = trade_data
                        logger.info(f"✅ تم تحميل صفقة نشطة: {symbol} - {side}")
            
            logger.info(f"📊 تم تحميل {len(self.active_trades)} صفقة نشطة")
            
        except Exception as e:
            logger.error(f"❌ خطأ في تحميل الصفقات: {e}")

    def get_current_price(self, symbol):
        return self.price_manager.get_price(symbol)

    def set_leverage(self, symbol, leverage):
        """ضبط الرافعة المالية مع معالجة الأخطاء"""
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            logger.info(f"✅ تم ضبط الرافعة لـ {symbol} إلى {leverage}x")
            return True
        except Exception as e:
            error_msg = str(e)
            if "leverage" in error_msg.lower():
                logger.warning(f"⚠️ خطأ في ضبط الرافعة لـ {symbol}: {error_msg}")
                # نستمر مع الرافعة الحالية
                return True
            else:
                logger.error(f"❌ فشل ضبط الرافعة لـ {symbol}: {e}")
                return False

    def set_margin_type(self, symbol, margin_type):
        """ضبط نوع الهامش مع معالجة أفضل للأخطاء"""
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            logger.info(f"✅ تم ضبط نوع الهامش لـ {symbol} إلى {margin_type}")
            return True
        except Exception as e:
            error_msg = str(e)
            
            # إذا كان الخطأ لأن نوع الهامش مضبوط مسبقاً
            if "No need to change margin type" in error_msg:
                logger.info(f"ℹ️ نوع الهامش لـ {symbol} مضبوط مسبقاً على {margin_type}")
                return True
            # إذا كان الخطأ بسبب وجود صفقات نشطة
            elif "Account has open positions" in error_msg:
                logger.warning(f"⚠️ لا يمكن تغيير نوع الهامش لـ {symbol} - يوجد صفقات نشطة")
                return True  # نعتبره نجاحاً لأن الصفقات موجودة
            else:
                logger.warning(f"⚠️ فشل ضبط نوع الهامش لـ {symbol}: {error_msg}")
                return True  # نستمر مع الإعدادات الحالية

    def get_historical_data(self, symbol, interval='30m', limit=100):
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
        try:
            df = data.copy()
            if len(df) < 50:
                return df

            df['sma10'] = df['close'].rolling(10).mean()
            df['sma50'] = df['close'].rolling(50).mean()
            df['sma20'] = df['close'].rolling(20).mean()
            
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = (-delta).where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-10)
            df['rsi'] = 100 - (100 / (1 + rs))
            
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            tr = np.maximum(np.maximum(high_low, high_close), low_close)
            df['atr'] = tr.rolling(14).mean()
            
            df['momentum'] = df['close'] / df['close'].shift(5) - 1
            df['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
            
            return df.dropna()
        except Exception as e:
            logger.error(f"❌ خطأ في حساب المؤشرات: {e}")
            return data

    def analyze_symbol(self, symbol):
        try:
            data = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'])
            if data is None or len(data) < 50:
                return False, {}, None

            data = self.calculate_indicators(data)
            if len(data) == 0:
                return False, {}, None

            latest = data.iloc[-1]
            
            buy_conditions = [
                (latest['sma10'] > latest['sma50']),
                (latest['sma10'] > latest['sma20']),
                (40 <= latest['rsi'] <= 75),
                (latest['momentum'] > 0.001),
                (latest['volume_ratio'] > 0.8),
            ]
            
            sell_conditions = [
                (latest['sma10'] < latest['sma50']),
                (latest['sma10'] < latest['sma20']),
                (25 <= latest['rsi'] <= 70),
                (latest['momentum'] < -0.001),
                (latest['volume_ratio'] > 0.8),
            ]
            
            buy_signal = sum(buy_conditions) >= self.TRADING_SETTINGS['min_signal_conditions']
            sell_signal = sum(sell_conditions) >= self.TRADING_SETTINGS['min_signal_conditions']
            
            direction = None
            if buy_signal:
                direction = 'LONG'
            elif sell_signal:
                direction = 'SHORT'

            details = {
                'signal_strength': max(sum(buy_conditions), sum(sell_conditions)) * 20,
                'sma10': latest['sma10'],
                'sma20': latest['sma20'],
                'sma50': latest['sma50'],
                'rsi': latest['rsi'],
                'price': latest['close'],
                'atr': latest['atr'],
                'momentum': latest['momentum'],
                'volume_ratio': latest['volume_ratio'],
                'buy_conditions_met': sum(buy_conditions),
                'sell_conditions_met': sum(sell_conditions),
                'direction': direction,
            }

            return direction is not None, details, direction

        except Exception as e:
            logger.error(f"❌ خطأ في تحليل {symbol}: {e}")
            return False, {}, None

    def get_futures_precision(self, symbol):
        try:
            info = self.client.futures_exchange_info()
            symbol_info = next((s for s in info['symbols'] if s['symbol'] == symbol), None)
            
            if symbol_info:
                lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
                price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
                min_notional_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'MIN_NOTIONAL'), None)
                
                min_notional = float(min_notional_filter['notional']) if min_notional_filter else 5.0
                
                return {
                    'step_size': float(lot_size['stepSize']) if lot_size else 0.001,
                    'tick_size': float(price_filter['tickSize']) if price_filter else 0.001,
                    'precision': int(round(-np.log10(float(lot_size['stepSize'])))) if lot_size else 3,
                    'min_qty': float(lot_size['minQty']) if lot_size else 0.001,
                    'min_notional': min_notional
                }
            
            return {'step_size': 0.001, 'tick_size': 0.001, 'precision': 3, 'min_qty': 0.001, 'min_notional': 5.0}
        except Exception as e:
            logger.error(f"❌ خطأ في جلب دقة العقود: {e}")
            return {'step_size': 0.001, 'tick_size': 0.001, 'precision': 3, 'min_qty': 0.001, 'min_notional': 5.0}

    def can_open_trade(self, symbol):
        """التحقق من إمكانية فتح صفقة جديدة مع تفاصيل أكثر"""
        reasons = []
        
        # التحقق من الحد الأقصى للصفقات النشطة
        if len(self.active_trades) >= self.TRADING_SETTINGS['max_active_trades']:
            reasons.append(f"الحد الأقصى للصفقات ({self.TRADING_SETTINGS['max_active_trades']}) تم الوصول إليه")
            
        # التحقق من وجود صفقة نشطة لنفس الرمز
        if symbol in self.active_trades:
            current_trade = self.active_trades[symbol]
            trade_age = datetime.now(damascus_tz) - current_trade['timestamp']
            age_minutes = trade_age.total_seconds() / 60
            reasons.append(f"صفقة نشطة موجودة منذ {age_minutes:.1f} دقيقة")
            
        # التحقق من الرصيد المتاح
        available_balance = self.symbol_balances.get(symbol, 0)
        if available_balance < 5:
            reasons.append(f"رصيد غير كافي: ${available_balance:.2f} (المطلوب: $5.00)")
            
        # تسجيل أسباب المنع للتصحيح
        if reasons:
            logger.warning(f"⏸️ منع فتح صفقة لـ {symbol}: {reasons}")
            
        return len(reasons) == 0, reasons

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
                    f"شروط الدخول: {analysis['buy_conditions_met'] if direction == 'LONG' else analysis['sell_conditions_met']}/5\n"
                    f"قوة الإشارة: {analysis['signal_strength']:.1f}%\n"
                    f"السعر الحالي: ${analysis['price']:.4f}\n"
                    f"الرصيد المتاح: ${self.symbol_balances.get(symbol, 0):.2f}\n"
                    f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
                )
            else:
                message = (
                    f"⚠️ <b>إشارة تداول - غير قابلة للتنفيذ</b>\n"
                    f"العملة: {symbol}\n"
                    f"الاتجاه: {direction}\n"
                    f"شروط الدخول: {analysis['buy_conditions_met'] if direction == 'LONG' else analysis['sell_conditions_met']}/5\n"
                    f"قوة الإشارة: {analysis['signal_strength']:.1f}%\n"
                    f"<b>أسباب عدم التنفيذ:</b>\n"
                )
                for reason in reasons:
                    message += f"• {reason}\n"
                message += f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
            
            self.notifier.send_message(message, f'signal_{symbol}')
            logger.info(f"✅ تم إرسال إشعار إشارة لـ {symbol}")
            
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال إشعار الإشارة: {e}")

    def calculate_trade_size(self, symbol, current_price, leverage, available_balance):
        """حجم الصفقة بدقة"""
        try:
            precision_info = self.get_futures_precision(symbol)
            
            # حساب الهامش المطلوب
            margin_usd = min(self.TRADING_SETTINGS['base_trade_size'], available_balance)
            
            # حساب الكمية: quantity = (margin * leverage) / price
            quantity = (margin_usd * leverage) / current_price
            
            # تقريب الكمية حسب step_size
            step_size = precision_info['step_size']
            precision = precision_info['precision']
            quantity = round(quantity / step_size) * step_size
            quantity = round(quantity, precision)
            
            # التحقق من الحد الأدنى للكمية والقيمة الاسمية
            notional_value = quantity * current_price
            
            if quantity <= 0 or quantity < precision_info['min_qty']:
                raise Exception(f"الكمية أقل من المسموح: {quantity} < {precision_info['min_qty']}")
                
            if notional_value < precision_info['min_notional']:
                raise Exception(f"القيمة الاسمية أقل من المسموح: ${notional_value:.2f} < ${precision_info['min_notional']:.2f}")
            
            return margin_usd, quantity, notional_value
            
        except Exception as e:
            logger.error(f"❌ خطأ في حساب حجم الصفقة لـ {symbol}: {e}")
            raise

    def execute_futures_trade(self, symbol, direction, signal_strength, analysis):
        """تنفيذ صفقة العقود الآجلة مع معالجة محسنة للأخطاء"""
        try:
            logger.info(f"🔧 بدء تنفيذ صفقة {symbol} - {direction}")
            
            # 1. التحقق من إمكانية فتح الصفقة
            can_trade, reasons = self.can_open_trade(symbol)
            if not can_trade:
                logger.warning(f"⏸️ لا يمكن فتح صفقة لـ {symbol}: {reasons}")
                self.send_trade_rejection_notification(symbol, direction, analysis, reasons)
                return False

            # 2. الحصول على السعر الحالي
            current_price = self.get_current_price(symbol)
            if current_price is None:
                raise Exception("لا يمكن الحصول على السعر الحالي")

            # 3. حساب الرافعة المثلى
            symbol_weight = self.OPTIMAL_SETTINGS['weights'][symbol]
            atr = analysis['atr']
            atr_percentage = atr / current_price
            
            leverage = min(3 / max(atr_percentage, 0.001), self.TRADING_SETTINGS['max_leverage']) * symbol_weight
            leverage = max(leverage, 1)
            leverage = int(leverage)

            # 4. ضبط الرافعة أولاً (أهم خطوة)
            if not self.set_leverage(symbol, leverage):
                logger.warning(f"⚠️ فشل ضبط الرافعة لـ {symbol}، جرب الرافعة الافتراضية")
                leverage = 3  # رافعة افتراضية
                if not self.set_leverage(symbol, leverage):
                    raise Exception("فشل ضبط الرافعة")

            # 5. ضبط نوع الهامش (مع تجاهل الأخطاء غير الحرجة)
            if not self.set_margin_type(symbol, self.TRADING_SETTINGS['margin_type']):
                logger.warning(f"⚠️ فشل ضبط نوع الهامش لـ {symbol}، لكننا نستمر")

            # 6. حساب حجم الصفقة
            available_balance = self.symbol_balances[symbol]
            margin_usd, quantity, notional_value = self.calculate_trade_size(
                symbol, current_price, leverage, available_balance
            )

            # 7. تنفيذ الأمر
            side = Client.SIDE_BUY if direction == 'LONG' else Client.SIDE_SELL
            
            logger.info(f"💰 تنفيذ أمر {symbol}: {direction} - كمية: {quantity} - رافعة: {leverage}x - هامش: ${margin_usd:.2f}")

            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type=Client.ORDER_TYPE_MARKET,
                quantity=quantity
            )

            if order['status'] == 'FILLED':
                return self.handle_successful_order(symbol, direction, order, quantity, margin_usd, analysis, leverage, notional_value)
            else:
                raise Exception(f"فشل تنفيذ الأمر: {order}")

        except Exception as e:
            logger.error(f"❌ خطأ في تنفيذ صفقة {symbol}: {e}")
            self.send_trade_error_notification(symbol, direction, str(e))
            return False

    def handle_successful_order(self, symbol, direction, order, quantity, margin_usd, analysis, leverage, notional_value):
        """معالجة الأمر الناجح"""
        try:
            avg_price = float(order['avgPrice'])
            
            # حساب وقف الخسارة وجني الأرباح
            atr_multiplier_sl = self.TRADING_SETTINGS['atr_stop_loss_multiplier']
            atr_multiplier_tp = self.TRADING_SETTINGS['atr_take_profit_multiplier']
            
            if direction == 'LONG':
                stop_loss = avg_price - (analysis['atr'] * atr_multiplier_sl)
                take_profit = avg_price + (analysis['atr'] * atr_multiplier_tp)
            else:
                stop_loss = avg_price + (analysis['atr'] * atr_multiplier_sl)
                take_profit = avg_price - (analysis['atr'] * atr_multiplier_tp)
            
            # تحديث الرصيد
            self.symbol_balances[symbol] -= margin_usd
            
            # حفظ بيانات الصفقة
            trade_data = {
                'symbol': symbol,
                'quantity': quantity,
                'entry_price': avg_price,
                'leverage': leverage,
                'side': direction,
                'timestamp': datetime.now(damascus_tz),
                'status': 'open',
                'order_id': order['orderId'],
                'trade_type': 'futures',
                'atr': analysis['atr'],
                'margin_usd': margin_usd,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'min_duration_end': datetime.now(damascus_tz) + timedelta(minutes=self.TRADING_SETTINGS['min_trade_duration_minutes'])
            }
            
            self.active_trades[symbol] = trade_data
            
            # تسجيل في تقارير الأداء
            self.performance_reporter.record_trade_open(symbol, direction, avg_price, margin_usd)
            
            # إرسال إشعار النجاح
            success_message = (
                f"✅ <b>تم فتح صفقة بنجاح</b>\n"
                f"العملة: {symbol}\n"
                f"الاتجاه: {direction}\n"
                f"السعر: ${avg_price:.4f}\n"
                f"الكمية: {quantity:.4f}\n"
                f"الحجم الاسمي: ${notional_value:.2f}\n"
                f"الهامش: ${margin_usd:.2f}\n"
                f"الرافعة: {leverage}x\n"
                f"وقف الخسارة: ${stop_loss:.4f}\n"
                f"جني الأرباح: ${take_profit:.4f}\n"
                f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            self.notifier.send_message(success_message, f'trade_open_{symbol}')
            logger.info(f"✅ تم فتح صفقة {symbol} بنجاح")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ خطأ في معالجة الأمر الناجح لـ {symbol}: {e}")
            return False

    def send_trade_rejection_notification(self, symbol, direction, analysis, reasons):
        """إرسال إشعار رفض الصفقة"""
        if not self.notifier:
            return
            
        try:
            message = (
                f"🚫 <b>تم رفض فتح الصفقة</b>\n"
                f"العملة: {symbol}\n"
                f"الاتجاه: {direction}\n"
                f"قوة الإشارة: {analysis['signal_strength']:.1f}%\n"
                f"<b>أسباب الرفض:</b>\n"
            )
            for reason in reasons:
                message += f"• {reason}\n"
            message += f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
            
            self.notifier.send_message(message, f'trade_rejection_{symbol}')
            
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال إشعار الرفض: {e}")

    def send_trade_error_notification(self, symbol, direction, error_message):
        """إرسال إشعار خطأ في الصفقة"""
        if not self.notifier:
            return
            
        try:
            message = (
                f"❌ <b>فشل تنفيذ صفقة</b>\n"
                f"العملة: {symbol}\n"
                f"الاتجاه: {direction}\n"
                f"<b>السبب:</b> {error_message}\n"
                f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            self.notifier.send_message(message, f'trade_error_{symbol}')
            
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال إشعار الخطأ: {e}")

    def check_and_close_trades(self):
        """فحص وإغلاق الصفقات بناءً على الشروط"""
        try:
            current_time = datetime.now(damascus_tz)
            symbols_to_close = []
            
            for symbol, trade in list(self.active_trades.items()):
                try:
                    current_price = self.get_current_price(symbol)
                    if current_price is None:
                        continue
                    
                    close_reason = None
                    pnl_percent = 0
                    pnl_usd = 0
                    
                    # حساب الربح/الخسارة
                    if trade['side'] == 'LONG':
                        pnl_percent = (current_price - trade['entry_price']) / trade['entry_price'] * 100 * trade['leverage']
                    else:
                        pnl_percent = (trade['entry_price'] - current_price) / trade['entry_price'] * 100 * trade['leverage']
                    
                    pnl_usd = (pnl_percent / 100) * trade['margin_usd']
                    
                    # التحقق من وقف الخسارة وجني الأرباح
                    if trade['side'] == 'LONG':
                        if current_price <= trade['stop_loss']:
                            close_reason = "وقف الخسارة"
                        elif current_price >= trade['take_profit']:
                            close_reason = "جني الأرباح"
                    else:
                        if current_price >= trade['stop_loss']:
                            close_reason = "وقف الخسارة"
                        elif current_price <= trade['take_profit']:
                            close_reason = "جني الأرباح"
                    
                    # التحقق من انتهاء الوقت
                    trade_age = current_time - trade['timestamp']
                    if trade_age.total_seconds() / 3600 >= self.TRADING_SETTINGS['trade_timeout_hours']:
                        close_reason = "انتهاء الوقت"
                    
                    # التحقق من الحد الأدنى للوقت
                    if current_time < trade['min_duration_end'] and close_reason not in ["وقف الخسارة", "جني الأرباح"]:
                        continue
                    
                    if close_reason:
                        symbols_to_close.append((symbol, trade, close_reason, pnl_percent, pnl_usd))
                        
                except Exception as e:
                    logger.error(f"❌ خطأ في فحص صفقة {symbol}: {e}")
            
            # إغلاق الصفقات
            for symbol, trade, reason, pnl_percent, pnl_usd in symbols_to_close:
                if self.close_trade(symbol, trade, reason, pnl_percent, pnl_usd):
                    logger.info(f"✅ تم إغلاق صفقة {symbol} بسبب: {reason}")
                    
        except Exception as e:
            logger.error(f"❌ خطأ عام في فحص الصفقات: {e}")

    def close_trade(self, symbol, trade, reason, pnl_percent, pnl_usd):
        """إغلاق صفقة محددة"""
        try:
            side = Client.SIDE_SELL if trade['side'] == 'LONG' else Client.SIDE_BUY
            
            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type=Client.ORDER_TYPE_MARKET,
                quantity=trade['quantity']
            )
            
            if order['status'] == 'FILLED':
                # تحديث الرصيد
                self.symbol_balances[symbol] += trade['margin_usd'] + pnl_usd
                
                # إزالة من الصفقات النشطة
                del self.active_trades[symbol]
                
                # تسجيل في تقارير الأداء
                self.performance_reporter.record_trade_close(symbol, float(order['avgPrice']), pnl_percent, pnl_usd, reason)
                
                # إرسال إشعار الإغلاق
                if self.notifier:
                    message = (
                        f"🔒 <b>تم إغلاق الصفقة</b>\n"
                        f"العملة: {symbol}\n"
                        f"الاتجاه: {trade['side']}\n"
                        f"السبب: {reason}\n"
                        f"الربح/الخسارة: {pnl_percent:+.2f}% (${pnl_usd:+.2f})\n"
                        f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    self.notifier.send_message(message, f'trade_close_{symbol}')
                
                logger.info(f"✅ تم إغلاق صفقة {symbol} - السبب: {reason}")
                return True
            else:
                raise Exception(f"فشل إغلاق الأمر: {order}")
                
        except Exception as e:
            logger.error(f"❌ خطأ في إغلاق صفقة {symbol}: {e}")
            if self.notifier:
                error_message = (
                    f"❌ <b>فشل إغلاق صفقة</b>\n"
                    f"العملة: {symbol}\n"
                    f"الاتجاه: {trade['side']}\n"
                    f"<b>السبب:</b> {str(e)}\n"
                    f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(error_message, f'close_error_{symbol}')
            return False

    def scan_and_trade(self):
        """المسح الضوئي وتنفيذ الصفقات"""
        try:
            logger.info("🔍 بدء المسح الضوئي للفرص...")
            
            for symbol in self.symbols:
                try:
                    has_signal, analysis, direction = self.analyze_symbol(symbol)
                    
                    if has_signal:
                        logger.info(f"🎯 إشارة {direction} لـ {symbol} - قوة: {analysis['signal_strength']:.1f}%")
                        
                        # إرسال إشعار الإشارة
                        can_trade, reasons = self.can_open_trade(symbol)
                        self.send_trade_signal_notification(symbol, direction, analysis, can_trade, reasons)
                        
                        # تنفيذ الصفقة إذا كان ذلك ممكناً
                        if can_trade:
                            success = self.execute_futures_trade(symbol, direction, analysis['signal_strength'], analysis)
                            if success:
                                logger.info(f"✅ تم تنفيذ صفقة {symbol} بنجاح")
                            else:
                                logger.error(f"❌ فشل تنفيذ صفقة {symbol}")
                        else:
                            logger.info(f"⏸️ إشارة لـ {symbol} ولكن لا يمكن التنفيذ: {reasons}")
                    
                    time.sleep(1)
                    
                except Exception as e:
                    logger.error(f"❌ خطأ في معالجة {symbol}: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"❌ خطأ عام في المسح الضوئي: {e}")

    def run(self):
        """الدالة الرئيسية لتشغيل البوت"""
        logger.info("🚀 بدء تشغيل بوت العقود الآجلة...")
        
        # تشغيل خادم Flask في thread منفصل
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        # جدولة المهام
        schedule.every(5).minutes.do(self.check_and_close_trades)
        schedule.every(15).minutes.do(self.scan_and_trade)
        schedule.every(1).hours.do(self.send_status_update)
        
        logger.info("✅ تم بدء جميع المهام المجدولة")
        
        # بدء المسح الفوري
        self.scan_and_trade()
        
        # الحلقة الرئيسية
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("⏹️ إيقاف البوت بواسطة المستخدم")
                break
            except Exception as e:
                logger.error(f"❌ خطأ في الحلقة الرئيسية: {e}")
                time.sleep(60)

    def send_status_update(self):
        """إرسال تحديث حالة دوري"""
        if not self.notifier:
            return
            
        try:
            current_time = datetime.now(damascus_tz)
            total_balance = sum(self.symbol_balances.values())
            
            message = (
                f"📊 <b>تحديث الحالة الدوري</b>\n"
                f"الوقت: {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"إجمالي الرصيد: ${total_balance:.2f}\n"
                f"الصفقات النشطة: {len(self.active_trades)}\n"
                f"الرموز المتابعة: {len(self.symbols)}\n"
                f"حالة البوت: 🟢 نشط"
            )
            
            self.notifier.send_message(message, 'status_update')
            
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال تحديث الحالة: {e}")

def main():
    try:
        bot = FuturesTradingBot()
        bot.run()
    except Exception as e:
        logger.error(f"❌ فشل تشغيل البوت: {e}")
        time.sleep(10)

if __name__ == "__main__":
    main()
