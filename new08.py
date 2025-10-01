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
        'min_signal_conditions': 5,
        'atr_stop_loss_multiplier': 1.5,
        'atr_take_profit_multiplier': 3.0,
        'min_trade_duration_minutes': 30,
        'min_notional_value': 10.0,  # الحد الأدنى للقيمة الاسمية
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
            server_time_sec = server_time['serverTime'] / 1000
            local_time = time.time()
            time_diff = abs(server_time_sec - local_time)
            if time_diff > 5:  # تحذير إذا كان الفارق أكبر من 5 ثواني
                logger.warning(f"⚠️ فارق توقيت مع Binance: {time_diff:.2f} ثانية")
            else:
                logger.info(f"✅ التوقيت متزامن مع Binance (فارق: {time_diff:.2f} ثانية)")
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
            rs = avg_gain / (avg_loss + 1e-6)
            df['rsi'] = 100 - (100 / (1 + rs))
            
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            tr = np.maximum(np.maximum(high_low, high_close), low_close)
            df['atr'] = tr.rolling(14).mean()
            
            df['momentum'] = df['close'] / df['close'].shift(5) - 1
            df['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
            
            exp12 = df['close'].ewm(span=12, adjust=False).mean()
            exp26 = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = exp12 - exp26
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
            
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
                (50 <= latest['rsi'] <= 70),
                (latest['momentum'] > 0.005),
                (latest['volume_ratio'] > 1.0),
                (latest['macd'] > latest['macd_signal'])
            ]
            
            sell_conditions = [
                (latest['sma10'] < latest['sma50']),
                (latest['sma10'] < latest['sma20']),
                (30 <= latest['rsi'] <= 50),
                (latest['momentum'] < -0.005),
                (latest['volume_ratio'] > 1.0),
                (latest['macd'] < latest['macd_signal'])
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
        """الحصول على معلومات الدقة للرمز مع معالجة محسنة للأخطاء"""
        try:
            info = self.client.futures_exchange_info()
            symbol_info = next((s for s in info['symbols'] if s['symbol'] == symbol), None)
            
            if symbol_info:
                lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
                price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
                min_notional_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'MIN_NOTIONAL'), None)
                
                min_notional = float(min_notional_filter['notional']) if min_notional_filter else self.TRADING_SETTINGS['min_notional_value']
                step_size = float(lot_size['stepSize']) if lot_size else 0.001
                
                # حساب الدقة بناءً على step_size
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
                    f"شروط الدخول: {analysis['buy_conditions_met'] if direction == 'LONG' else analysis['sell_conditions_met']}/6\n"
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
                    f"شروط الدخول: {analysis['buy_conditions_met'] if direction == 'LONG' else analysis['sell_conditions_met']}/6\n"
                    f"قوة الإشارة: {analysis['signal_strength']:.1f}%\n"
                    f"<b>أسباب عدم التنفيذ:</b>\n"
                )
                for reason in reasons:
                    message += f"• {reason}\n"
                message += f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                message += f"<b>تفاصيل المؤشرات:</b>\n"
                message += f"• SMA10: {analysis['sma10']:.4f}\n"
                message += f"• SMA20: {analysis['sma20']:.4f}\n"
                message += f"• SMA50: {analysis['sma50']:.4f}\n"
                message += f"• RSI: {analysis['rsi']:.2f}\n"
                message += f"• Momentum: {analysis['momentum']:.4f}\n"
                message += f"• Volume Ratio: {analysis['volume_ratio']:.2f}"
            
            self.notifier.send_message(message, 'trade_signal')
            
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال إشعار الإشارة: {e}")

    def calculate_position_size(self, symbol, direction, analysis, available_balance):
        """حجم المركز مع تحسينات الأمان"""
        try:
            current_price = self.get_current_price(symbol)
            if not current_price:
                logger.error(f"❌ لا يمكن الحصول على السعر الحالي لـ {symbol}")
                return None, None, None

            # الحصول على معلومات الدقة
            precision_info = self.get_futures_precision(symbol)
            step_size = precision_info['step_size']
            min_notional = precision_info['min_notional']
            
            # حساب حجم المركز بناءً على الرصيد المتاح والرافعة
            leverage = self.TRADING_SETTINGS['max_leverage']
            position_value = min(available_balance * leverage, self.TRADING_SETTINGS['base_trade_size'])
            
            # التأكد من أن القيمة الاسمية فوق الحد الأدنى
            if position_value < min_notional:
                position_value = min_notional * 1.1
                logger.info(f"⚖️ تعديل حجم المركز إلى الحد الأدنى: ${position_value:.2f}")
            
            # حساب الكمية بناءً على السعر الحالي
            quantity = position_value / current_price
            
            # تقريب الكمية بناءً على step_size
            if step_size > 0:
                quantity = round(quantity / step_size) * step_size
            
            # التحقق من الحد الأدنى للكمية
            if quantity < precision_info['min_qty']:
                quantity = precision_info['min_qty']
                # إعادة حساب القيمة الاسمية بناءً على الكمية المعدلة
                position_value = quantity * current_price
            
            # التحقق مرة أخرى من القيمة الاسمية
            if position_value < min_notional:
                logger.warning(f"⚠️ القيمة الاسمية ${position_value:.2f} أقل من الحد الأدنى ${min_notional:.2f}")
                return None, None, None
            
            # حساب وقف الخسارة وجني الأرباح باستخدام ATR
            atr = analysis.get('atr', current_price * 0.02)
            stop_loss_pct = (self.TRADING_SETTINGS['atr_stop_loss_multiplier'] * atr / current_price)
            take_profit_pct = (self.TRADING_SETTINGS['atr_take_profit_multiplier'] * atr / current_price)
            
            if direction == 'LONG':
                stop_loss_price = current_price * (1 - stop_loss_pct)
                take_profit_price = current_price * (1 + take_profit_pct)
            else:  # SHORT
                stop_loss_price = current_price * (1 + stop_loss_pct)
                take_profit_price = current_price * (1 - take_profit_pct)
            
            logger.info(f"📏 حجم المركز لـ {symbol}: {quantity:.6f} (قيمة: ${position_value:.2f})")
            
            return quantity, stop_loss_price, take_profit_price
            
        except Exception as e:
            logger.error(f"❌ خطأ في حساب حجم المركز لـ {symbol}: {e}")
            return None, None, None

    def execute_trade(self, symbol, direction, quantity, stop_loss_price, take_profit_price, analysis):
        """تنفيذ الصفقة مع معالجة محسنة للأخطاء"""
        try:
            # التحقق من الكمية والقيمة الاسمية مرة أخرى
            current_price = self.get_current_price(symbol)
            if not current_price:
                raise Exception("لا يمكن الحصول على السعر الحالي")
                
            notional_value = quantity * current_price
            min_notional = self.get_futures_precision(symbol)['min_notional']
            
            if notional_value < min_notional:
                raise Exception(f"القيمة الاسمية ${notional_value:.2f} أقل من الحد الأدنى ${min_notional:.2f}")
            
            # ضبط الرافعة ونوع الهامش
            if not self.set_leverage(symbol, self.TRADING_SETTINGS['max_leverage']):
                raise Exception("فشل ضبط الرافعة")
                
            if not self.set_margin_type(symbol, self.TRADING_SETTINGS['margin_type']):
                raise Exception("فشل ضبط نوع الهامش")
            
            # تنفيذ أمر السوق
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
            for i in range(10):  # 10 محاولات خلال 5 ثواني
                time.sleep(0.5)
                order_status = self.client.futures_get_order(symbol=symbol, orderId=order['orderId'])
                executed_qty = float(order_status.get('executedQty', 0))
                if executed_qty > 0:
                    break
            
            if executed_qty == 0:
                # محاولة الإلغاء وإعادة المحاولة
                try:
                    self.client.futures_cancel_order(symbol=symbol, orderId=order['orderId'])
                except:
                    pass
                raise Exception("الأمر لم ينفذ بعد 5 ثواني")
            
            # الحصول على سعر الدخول الفعلي
            avg_price = float(order_status.get('avgPrice', 0))
            if avg_price == 0:
                avg_price = current_price
            
            # تحديث الرصيد المتاح
            trade_value = notional_value / self.TRADING_SETTINGS['max_leverage']
            self.symbol_balances[symbol] = max(0, self.symbol_balances[symbol] - trade_value)
            
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
            
            self.active_trades[symbol] = trade_data
            
            # تسجيل في تقارير الأداء
            self.performance_reporter.record_trade_open(symbol, direction, avg_price, notional_value)
            
            # إرسال إشعار النجاح
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
                    f"القيمة الاسمية: ${notional_value:.2f}\n"
                    f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_open')
            
            logger.info(f"✅ تم فتح صفقة {direction} لـ {symbol}: {quantity:.6f} @ ${avg_price:.4f}")
            return True
            
        except Exception as e:
            logger.error(f"❌ فشل تنفيذ صفقة {symbol}: {e}")
            
            # إرسال إشعار الفشل
            if self.notifier:
                message = (
                    f"❌ <b>فشل تنفيذ صفقة</b>\n"
                    f"العملة: {symbol}\n"
                    f"الاتجاه: {direction}\n"
                    f"السبب: {str(e)}\n"
                    f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_failed')
            
            return False

    def check_trade_timeout(self):
        """فحص انتهاء وقت الصفقات"""
        current_time = datetime.now(damascus_tz)
        symbols_to_close = []
        
        for symbol, trade in self.active_trades.items():
            trade_age = current_time - trade['timestamp']
            hours_open = trade_age.total_seconds() / 3600
            
            if hours_open >= self.TRADING_SETTINGS['trade_timeout_hours']:
                symbols_to_close.append(symbol)
                logger.info(f"⏰ انتهاء وقت الصفقة لـ {symbol} (مفتوحة منذ {hours_open:.1f} ساعة)")
        
        for symbol in symbols_to_close:
            self.close_trade(symbol, 'timeout')

    def close_trade(self, symbol, reason='manual'):
        """إغلاق الصفقة مع معالجة محسنة"""
        try:
            if symbol not in self.active_trades:
                logger.warning(f"⚠️ لا توجد صفقة نشطة لـ {symbol}")
                return False

            trade = self.active_trades[symbol]
            current_price = self.get_current_price(symbol)
            
            if not current_price:
                logger.error(f"❌ لا يمكن الحصول على السعر الحالي لـ {symbol}")
                return False

            # حساب الربح/الخسارة
            if trade['side'] == 'LONG':
                pnl_percent = (current_price - trade['entry_price']) / trade['entry_price'] * 100
            else:  # SHORT
                pnl_percent = (trade['entry_price'] - current_price) / trade['entry_price'] * 100
            
            pnl_usd = (pnl_percent / 100) * (trade['quantity'] * trade['entry_price'])
            
            # تنفيذ أمر الإغلاق
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
            
            # تحديث الرصيد المتاح
            trade_value = (trade['quantity'] * trade['entry_price']) / trade['leverage']
            self.symbol_balances[symbol] += trade_value
            
            # إزالة الصفقة من النشطة
            del self.active_trades[symbol]
            
            # تسجيل في تقارير الأداء
            self.performance_reporter.record_trade_close(symbol, current_price, pnl_percent, pnl_usd, reason)
            
            # إرسال إشعار الإغلاق
            if self.notifier:
                pnl_emoji = "🟢" if pnl_percent > 0 else "🔴"
                message = (
                    f"{pnl_emoji} <b>تم إغلاق الصفقة</b>\n"
                    f"العملة: {symbol}\n"
                    f"الاتجاه: {trade['side']}\n"
                    f"السبب: {reason}\n"
                    f"سعر الدخول: ${trade['entry_price']:.4f}\n"
                    f"سعر الخروج: ${current_price:.4f}\n"
                    f"الربح/الخسارة: {pnl_percent:+.2f}% (${pnl_usd:+.2f})\n"
                    f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_close')
            
            logger.info(f"✅ تم إغلاق صفقة {symbol}: {pnl_percent:+.2f}% (${pnl_usd:+.2f})")
            return True
            
        except Exception as e:
            logger.error(f"❌ فشل إغلاق صفقة {symbol}: {e}")
            return False

    def scan_and_trade(self):
        """المسح الضوئي وتنفيذ الصفقات"""
        try:
            logger.info("🔍 بدء المسح الضوئي للفرص...")
            
            # فحص انتهاء وقت الصفقات أولاً
            self.check_trade_timeout()
            
            for symbol in self.symbols:
                try:
                    # تخطي الرموز التي بها صفقات نشطة حديثة
                    if symbol in self.active_trades:
                        trade_age = datetime.now(damascus_tz) - self.active_trades[symbol]['timestamp']
                        age_minutes = trade_age.total_seconds() / 60
                        if age_minutes < self.TRADING_SETTINGS['min_trade_duration_minutes']:
                            continue
                    
                    # تحليل الرمز
                    has_signal, analysis, direction = self.analyze_symbol(symbol)
                    
                    if has_signal and direction:
                        # التحقق من إمكانية فتح الصفقة
                        can_trade, reasons = self.can_open_trade(symbol)
                        
                        # إرسال إشعار الإشارة
                        self.send_trade_signal_notification(symbol, direction, analysis, can_trade, reasons)
                        
                        if can_trade:
                            # حساب حجم المركز
                            available_balance = self.symbol_balances.get(symbol, 0)
                            quantity, stop_loss, take_profit = self.calculate_position_size(
                                symbol, direction, analysis, available_balance
                            )
                            
                            if quantity and quantity > 0:
                                # تنفيذ الصفقة
                                success = self.execute_trade(symbol, direction, quantity, stop_loss, take_profit, analysis)
                                
                                if success:
                                    logger.info(f"✅ تم تنفيذ صفقة {direction} لـ {symbol}")
                                else:
                                    logger.error(f"❌ فشل تنفيذ صفقة {direction} لـ {symbol}")
                    
                    # تأجيل قصير بين الرموز
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
        
        # بدء خادم Flask في خيط منفصل
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        try:
            while True:
                try:
                    # تشغيل المهام المجدولة
                    schedule.run_pending()
                    
                    # المسح الضوئي وتنفيذ الصفقات
                    self.scan_and_trade()
                    
                    # انتظار حتى المسح التالي
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
