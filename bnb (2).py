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
from flask import Flask
import threading
import json

# تحميل متغيرات البيئة
load_dotenv()

# إنشاء تطبيق Flask
app = Flask(__name__)

@app.route('/')
def health_check():
    return {'status': 'healthy', 'service': 'bnb-trading-bot', 'timestamp': datetime.now().isoformat()}

@app.route('/status')
def status():
    return {'status': 'running', 'bot': 'BNB Trading Bot', 'time': datetime.now().isoformat()}

@app.route('/recent_trades')
def recent_trades():
    try:
        bot = BNB_Trading_Bot()
        report = bot.generate_12h_trading_report()
        return report
    except Exception as e:
        return {'error': str(e)}

@app.route('/daily_report')
def daily_report():
    try:
        bot = BNB_Trading_Bot()
        report = bot.generate_daily_performance_report()
        return report
    except Exception as e:
        return {'error': str(e)}

@app.route('/time_schedule')
def time_schedule():
    try:
        bot = BNB_Trading_Bot()
        schedule = bot.get_daily_optimal_schedule()
        return {
            'today': datetime.now().strftime('%Y-%m-%d'),
            'optimal_windows': schedule,
            'total_windows': len(schedule)
        }
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
        logging.FileHandler('bot_activity.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TimeWeightManager:
    def __init__(self, weights_file='bnb_time_weights_advanced.csv'):
        # تحميل أوزان التوقيت من الملف
        self.weights_df = pd.read_csv(weights_file, index_col=0, encoding='utf-8-sig')
        self.weekdays_arabic = ['الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد']
        self.weekdays_english = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        
        # تحويل الأعمدة الزمنية إلى تنسيق مناسب
        self.time_slots = self.weights_df.columns.tolist()
        
        # إنشاء قاموس للتحويل بين الأيام العربية والإنجليزية
        self.weekday_map = {eng: arb for eng, arb in zip(self.weekdays_english, self.weekdays_arabic)}
        
        # قائمة نوافذ التداول المثلى (سيتم ملؤها تلقائياً)
        self.optimal_windows = self._load_optimal_windows()
        
        # تتبع النوافذ التي تم إرسال إنذار لها
        self.notified_windows = set()
    
    def _load_optimal_windows(self, min_strength=7):
        """تحميل نوافذ التداول المثلى من ملف الأوزان"""
        optimal_windows = []
        
        for weekday_arabic in self.weekdays_arabic:
            for time_slot in self.time_slots:
                weight = self.weights_df.loc[weekday_arabic, time_slot]
                if abs(weight) >= min_strength:
                    hour, minute = map(int, time_slot.split(':'))
                    optimal_windows.append({
                        'weekday_arabic': weekday_arabic,
                        'weekday_english': self._get_english_weekday(weekday_arabic),
                        'time': time_slot,
                        'hour': hour,
                        'minute': minute,
                        'weight': weight,
                        'signal': 'BUY' if weight > 0 else 'SELL',
                        'strength': abs(weight)
                    })
        
        return optimal_windows
    
    def _get_english_weekday(self, arabic_weekday):
        """تحويل اليوم من العربية إلى الإنجليزية"""
        for eng, arb in self.weekday_map.items():
            if arb == arabic_weekday:
                return eng
        return arabic_weekday
    
    def get_current_time_weight(self):
        """الحصول على وزن الوقت الحالي"""
        now = datetime.now()
        current_weekday_english = now.strftime('%A')
        current_weekday_arabic = self.weekday_map.get(current_weekday_english, current_weekday_english)
        
        current_time = now.strftime('%H:%M')
        current_hour = now.hour
        current_minute = now.minute
        
        # البحث عن الوزن المناسب للوقت الحالي
        try:
            weight = self.weights_df.loc[current_weekday_arabic, current_time]
            return {
                'weight': weight,
                'signal': 'BUY' if weight > 0 else 'SELL',
                'strength': abs(weight),
                'time': f"{current_weekday_arabic} {current_time}",
                'hour': current_hour,
                'minute': current_minute
            }
        except KeyError:
            return None
    
    def get_upcoming_optimal_windows(self, lookahead_minutes=30):
        """الحصول على النوافذ المثلى القادمة في الدقائق التالية"""
        now = datetime.now()
        current_weekday_english = now.strftime('%A')
        current_time = now.strftime('%H:%M')
        
        upcoming_windows = []
        
        for window in self.optimal_windows:
            if window['weekday_english'] != current_weekday_english:
                continue
            
            # حساب الوقت المتبقي للنافذة
            window_time = datetime.now().replace(hour=window['hour'], minute=window['minute'], second=0, microsecond=0)
            time_diff = (window_time - now).total_seconds() / 60  # الفرق بالدقائق
            
            if 0 <= time_diff <= lookahead_minutes:
                upcoming_windows.append({
                    **window,
                    'minutes_away': time_diff,
                    'window_time': window_time
                })
        
        return sorted(upcoming_windows, key=lambda x: x['minutes_away'])
    
    def check_alert_windows(self, alert_minutes=5):
        """التحقق من النوافذ التي تحتاج إلى إنذار مسبق"""
        now = datetime.now()
        upcoming_windows = self.get_upcoming_optimal_windows(alert_minutes + 2)
        
        alerts = []
        
        for window in upcoming_windows:
            window_id = f"{window['weekday_english']}_{window['time']}"
            
            # التحقق إذا لم يتم إرسال إنذار لهذه النافذة بعد
            if window_id not in self.notified_windows and window['minutes_away'] <= alert_minutes:
                alerts.append(window)
                self.notified_windows.add(window_id)
        
        return alerts

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
    
    def send_message(self, message):
        try:
            logger.info(f"محاولة إرسال رسالة إلى Telegram: {message}")
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code != 200:
                error_msg = f"فشل إرسال رسالة Telegram: {response.text}"
                logger.error(error_msg)
                return False
            else:
                logger.info("تم إرسال الرسالة إلى Telegram بنجاح")
                return True
        except Exception as e:
            error_msg = f"خطأ في إرسال رسالة Telegram: {e}"
            logger.error(error_msg)
            return False

class PerformanceAnalyzer:
    def __init__(self):
        self.daily_trades = []
        self.daily_start_balance = 0
        self.daily_start_time = datetime.now()
        
    def add_trade(self, trade_data):
        self.daily_trades.append(trade_data)
        
    def calculate_daily_performance(self, current_balance):
        total_trades = len(self.daily_trades)
        winning_trades = len([t for t in self.daily_trades if t.get('profit_loss', 0) > 0])
        losing_trades = total_trades - winning_trades
        
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        
        total_profit = sum(t.get('profit_loss', 0) for t in self.daily_trades if t.get('profit_loss', 0) > 0)
        total_loss = abs(sum(t.get('profit_loss', 0) for t in self.daily_trades if t.get('profit_loss', 0) < 0))
        
        profit_factor = (total_profit / total_loss) if total_loss > 0 else float('inf')
        
        daily_pnl = current_balance - self.daily_start_balance
        daily_return = (daily_pnl / self.daily_start_balance * 100) if self.daily_start_balance > 0 else 0
        
        return {
            'daily_start_balance': self.daily_start_balance,
            'daily_end_balance': current_balance,
            'daily_pnl': daily_pnl,
            'daily_return': daily_return,
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': win_rate,
            'total_profit': total_profit,
            'total_loss': total_loss,
            'profit_factor': profit_factor,
            'avg_profit_per_trade': (total_profit / winning_trades) if winning_trades > 0 else 0,
            'avg_loss_per_trade': (total_loss / losing_trades) if losing_trades > 0 else 0
        }
    
    def reset_daily_stats(self, new_start_balance):
        self.daily_trades = []
        self.daily_start_balance = new_start_balance
        self.daily_start_time = datetime.now()

class BNB_Trading_Bot:
    def __init__(self, api_key=None, api_secret=None, telegram_token=None, telegram_chat_id=None):
        self.notifier = None
        self.trade_history = []
        self.performance_analyzer = PerformanceAnalyzer()
        self.load_trade_history()
        
        # إعدادات العتبات الجديدة
        self.BASELINE_BUY_THRESHOLD = 45  # رفع من 25 إلى 35
        self.STRICT_BUY_THRESHOLD = 55    # رفع من 20 إلى 45 (للأوامر الممتلئة)
        self.SELL_THRESHOLD = 40        # عتبة البيع تبقى كما هي
        
        self.api_key = api_key or os.environ.get('BINANCE_API_KEY')
        self.api_secret = api_secret or os.environ.get('BINANCE_API_SECRET')
        telegram_token = telegram_token or os.environ.get('TELEGRAM_BOT_TOKEN')
        telegram_chat_id = telegram_chat_id or os.environ.get('TELEGRAM_CHAT_ID')
        
        if not self.api_key or not self.api_secret:
            error_msg = "❌ مفاتيح Binance غير موجودة"
            logger.error(error_msg)
            raise ValueError(error_msg)
            
        try:
            self.client = Client(self.api_key, self.api_secret)
            logger.info("✅ تم الاتصال بمنصة Binance الفعلية")
            self.test_connection()
                
        except Exception as e:
            error_msg = f"❌ فشل الاتصال بـ Binance: {e}"
            logger.error(error_msg)
            raise ConnectionError(error_msg)
            
        # إضافة مدير الأوزان الزمنية
        self.time_weight_manager = TimeWeightManager('bnb_time_weights_advanced.csv')
        
        # معامل تضخيم الوزن الزمني (0-1)
        self.TIME_WEIGHT_FACTOR = 0.20  # 20% تأثير للوزن الزمني
        
        self.fee_rate = 0.0005
        self.slippage = 0.00015
        self.trades = []
        self.symbol = "BNBUSDT"
        
        # إعدادات إدارة الأوامر
        self.MAX_ALGO_ORDERS = 10
        self.ORDERS_TO_CANCEL = 2
        
        # إعدادات حجم الصفقة بالدولار حسب قوة الإشارة
        self.MIN_TRADE_SIZE = 5
        self.MAX_TRADE_SIZE = 50
        
        if telegram_token and telegram_chat_id:
            self.notifier = TelegramNotifier(telegram_token, telegram_chat_id)
            logger.info("تم تهيئة إشعارات Telegram")
        else:
            logger.warning("مفاتيح Telegram غير موجودة، سيتم تعطيل الإشعارات")
        
        try:
            self.initial_balance = self.get_real_balance()
            self.performance_analyzer.daily_start_balance = self.initial_balance
            success_msg = f"✅ تم تهيئة البوت بنجاح - الرصيد الابتدائي: ${self.initial_balance:.2f}"
            logger.info(success_msg)
            if self.notifier:
                self.notifier.send_message(
                    f"🤖 <b>بدء تشغيل بوت تداول BNB المحسن</b>\n\n"
                    f"{success_msg}\n"
                    f"نطاق حجم الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}\n"
                    f"الحد الأقصى للأوامر: {self.MAX_ALGO_ORDERS}\n"
                    f"عتبة الشراء الأساسية: {self.BASELINE_BUY_THRESHOLD}%\n"
                    f"عتبة الشراء المشددة: {self.STRICT_BUY_THRESHOLD}%\n"
                    f"عتبة البيع: {self.SELL_THRESHOLD}%\n"
                    f"تأثير الوزن الزمني: {self.TIME_WEIGHT_FACTOR * 100}%"
                )
        except Exception as e:
            logger.error(f"خطأ في جلب الرصيد الابتدائي: {e}")
            self.initial_balance = 0

    def load_trade_history(self):
        """تحميل تاريخ الصفقات من ملف"""
        try:
            if os.path.exists('trade_history.json'):
                with open('trade_history.json', 'r', encoding='utf-8') as f:
                    self.trade_history = json.load(f)
        except Exception as e:
            logger.error(f"خطأ في تحميل تاريخ الصفقات: {e}")
            self.trade_history = []

    def save_trade_history(self):
        """حفظ تاريخ الصفقات إلى ملف"""
        try:
            with open('trade_history.json', 'w', encoding='utf-8') as f:
                json.dump(self.trade_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"خطأ في حفظ تاريخ الصفقات: {e}")

    def add_trade_record(self, trade_type, quantity, price, trade_size, signal_strength, order_id=None, status="executed", profit_loss=0):
        """إضافة سجل صفقة جديدة"""
        trade_record = {
            'timestamp': datetime.now().isoformat(),
            'type': trade_type,
            'quantity': quantity,
            'price': price,
            'trade_size': trade_size,
            'signal_strength': signal_strength,
            'order_id': order_id,
            'status': status,
            'profit_loss': profit_loss
        }
        self.trade_history.append(trade_record)
        self.performance_analyzer.add_trade(trade_record)
        self.save_trade_history()

    def generate_12h_trading_report(self):
        """إنشاء تقرير التداول لآخر 12 ساعة"""
        try:
            twelve_hours_ago = datetime.now() - timedelta(hours=12)
            recent_trades = [
                trade for trade in self.trade_history 
                if datetime.fromisoformat(trade['timestamp']) >= twelve_hours_ago
            ]
            
            if not recent_trades:
                return {"message": "لا توجد صفقات في آخر 12 ساعة"}
            
            # حساب الإحصائيات
            buy_trades = [t for t in recent_trades if t['type'] == 'buy']
            sell_trades = [t for t in recent_trades if t['type'] == 'sell']
            
            total_buy_size = sum(t['trade_size'] for t in buy_trades)
            total_sell_size = sum(t['trade_size'] for t in sell_trades)
            avg_buy_strength = np.mean([t['signal_strength'] for t in buy_trades]) if buy_trades else 0
            avg_sell_strength = np.mean([t['signal_strength'] for t in sell_trades]) if sell_trades else 0
            
            profitable_trades = [t for t in recent_trades if t.get('profit_loss', 0) > 0]
            losing_trades = [t for t in recent_trades if t.get('profit_loss', 0) < 0]
            
            report = {
                "period": "آخر 12 ساعة",
                "total_trades": len(recent_trades),
                "buy_trades": len(buy_trades),
                "sell_trades": len(sell_trades),
                "profitable_trades": len(profitable_trades),
                "losing_trades": len(losing_trades),
                "win_rate": (len(profitable_trades) / len(recent_trades) * 100) if recent_trades else 0,
                "total_buy_size": round(total_buy_size, 2),
                "total_sell_size": round(total_sell_size, 2),
                "total_profit": sum(t.get('profit_loss', 0) for t in profitable_trades),
                "total_loss": abs(sum(t.get('profit_loss', 0) for t in losing_trades)),
                "avg_buy_signal_strength": round(avg_buy_strength, 1),
                "avg_sell_signal_strength": round(avg_sell_strength, 1),
                "recent_trades": recent_trades[-10:]
            }
            
            return report
        except Exception as e:
            logger.error(f"خطأ في إنشاء تقرير التداول: {e}")
            return {"error": str(e)}

    def generate_daily_performance_report(self):
        """إنشاء تقرير أداء يومي شامل"""
        try:
            current_balance = self.get_real_balance()
            performance = self.performance_analyzer.calculate_daily_performance(current_balance)
            
            # تحليل جودة الإشارات
            strong_signals = [t for t in self.performance_analyzer.daily_trades if abs(t['signal_strength']) >= 80]
            medium_signals = [t for t in self.performance_analyzer.daily_trades if 50 <= abs(t['signal_strength']) < 80]
            weak_signals = [t for t in self.performance_analyzer.daily_trades if abs(t['signal_strength']) < 50]
            
            strong_win_rate = (len([t for t in strong_signals if t.get('profit_loss', 0) > 0]) / len(strong_signals) * 100) if strong_signals else 0
            medium_win_rate = (len([t for t in medium_signals if t.get('profit_loss', 0) > 0]) / len(medium_signals) * 100) if medium_signals else 0
            weak_win_rate = (len([t for t in weak_signals if t.get('profit_loss', 0) > 0]) / len(weak_signals) * 100) if weak_signals else 0
            
            report = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "performance": performance,
                "signal_analysis": {
                    "strong_signals": len(strong_signals),
                    "strong_win_rate": round(strong_win_rate, 1),
                    "medium_signals": len(medium_signals),
                    "medium_win_rate": round(medium_win_rate, 1),
                    "weak_signals": len(weak_signals),
                    "weak_win_rate": round(weak_win_rate, 1)
                },
                "recommendations": self.generate_recommendations(performance)
            }
            
            return report
        except Exception as e:
            logger.error(f"خطأ في إنشاء التقرير اليومي: {e}")
            return {"error": str(e)}

    def generate_recommendations(self, performance):
        """توليد توصيات بناء على الأداء"""
        recommendations = []
        
        if performance['win_rate'] < 50:
            recommendations.append("⚡ فكر في تعديل استراتيجية الشراء/البيع")
        
        if performance['profit_factor'] < 1.5:
            recommendations.append("📉 ضعيف - تحتاج إلى تحسين نسبة الربح/الخسارة")
        elif performance['profit_factor'] < 2.0:
            recommendations.append("📊 متوسط - أداء مقبول ولكن يمكن التحسين")
        else:
            recommendations.append("📈 ممتاز - استمر في الاستراتيجية الحالية")
        
        if performance['total_trades'] > 15:
            recommendations.append("⚠️ عدد الصفقات مرتفع - فكر في تقليل التردد")
        elif performance['total_trades'] < 5:
            recommendations.append("ℹ️ عدد الصفقات منخفض - قد تحتاج إلى زيادة حساسية الإشارات")
        
        return recommendations

    def test_connection(self):
        try:
            server_time = self.client.get_server_time()
            logger.info(f"✅ الاتصال ناجح - وقت الخادم: {server_time['serverTime']}")
            
            account_info = self.client.get_account()
            logger.info("✅ جلب معلومات الحساب ناجح")
            
            public_ip = self.get_public_ip()
            logger.info(f"🌐 IP الخادم: {public_ip}")
            
            print("="*50)
            print("✅ اختبار الاتصال ناجح!")
            print("وضع التشغيل: فعلي")
            print(f"IP الخادم: {public_ip}")
            print(f"حجم الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}")
            print(f"الحد الأقصى للأوامر: {self.MAX_ALGO_ORDERS}")
            print(f"عتبة الشراء الأساسية: {self.BASELINE_BUY_THRESHOLD}%")
            print(f"عتبة الشراء المشددة: {self.STRICT_BUY_THRESHOLD}%")
            print(f"عتبة البيع: {self.SELL_THRESHOLD}%")
            print(f"تأثير الوزن الزمني: {self.TIME_WEIGHT_FACTOR * 100}%")
            print("="*50)
            
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال: {e}")
            return False

    def get_public_ip(self):
        try:
            response = requests.get('https://api.ipify.org?format=json', timeout=10)
            return response.json()['ip']
        except:
            return "غير معروف"
    
    def get_real_balance(self):
        try:
            account = self.client.get_account()
            balances = {asset['asset']: float(asset['free']) + float(asset['locked']) for asset in account['balances']}
            
            prices = self.client.get_all_tickers()
            price_dict = {item['symbol']: float(item['price']) for item in prices}
            
            total_balance = 0
            for asset, balance in balances.items():
                if balance > 0:
                    if asset == 'USDT':
                        total_balance += balance
                    else:
                        symbol = asset + 'USDT'
                        if symbol in price_dict:
                            total_balance += balance * price_dict[symbol]
            
            return total_balance
        except Exception as e:
            error_msg = f"❌ خطأ في جلب الرصيد من المنصة: {e}"
            logger.error(error_msg)
            if self.notifier:
                self.notifier.send_message(error_msg)
            raise
    
    def get_account_balance_details(self):
        try:
            account = self.client.get_account()
            balances = {asset['asset']: {
                'free': float(asset['free']),
                'locked': float(asset['locked']),
                'total': float(asset['free']) + float(asset['locked'])
            } for asset in account['balances'] if float(asset['free']) > 0 or float(asset['locked']) > 0}
            
            ticker = self.client.get_symbol_ticker(symbol=self.symbol)
            bnb_price = float(ticker['price'])
            
            total_balance = self.get_real_balance()
            
            return total_balance, balances, bnb_price
        except Exception as e:
            error_msg = f"❌ خطأ في الحصول على رصيد الحساب: {e}"
            logger.error(error_msg)
            return None, None, None
    
    def send_notification(self, message):
        logger.info(message)
        if self.notifier:
            success = self.notifier.send_message(message)
            if not success:
                logger.error("فشل إرسال الإشعار إلى Telegram")
            return success
        return False
    
    def format_price(self, price, symbol):
        """تقريب السعر حسب متطلبات Binance"""
        try:
            info = self.client.get_symbol_info(symbol)
            price_filter = [f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER'][0]
            tick_size = float(price_filter['tickSize'])
            
            formatted_price = round(price / tick_size) * tick_size
            return round(formatted_price, 8)
        except Exception as e:
            logger.error(f"خطأ في تقريب السعر: {e}")
            return round(price, 4)

    def get_algo_orders_count(self, symbol):
        """الحصول على عدد جميع الأوامر المعلقة"""
        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
            return len(open_orders)
        except Exception as e:
            logger.error(f"خطأ في جلب عدد الأوامر النشطة: {e}")
            return 0
    
    def get_order_space_status(self, symbol):
        """الحصول على حالة مساحة الأوامر (الجديدة)"""
        try:
            current_orders = self.get_algo_orders_count(symbol)
            
            if current_orders >= self.MAX_ALGO_ORDERS:
                return "FULL"  # الأوامر ممتلئة
            elif current_orders >= (self.MAX_ALGO_ORDERS - 2):
                return "NEAR_FULL"  # الأوامر قريبة من الامتلاء
            else:
                return "AVAILABLE"  # المساحة متاحة
                
        except Exception as e:
            logger.error(f"خطأ في التحقق من حالة الأوامر: {e}")
            return "FULL"  # في حالة الخطأ، نفترض أن الأوامر ممتلئة للسلامة
    
    def cancel_oldest_orders(self, symbol, num_to_cancel=2):
        """إلغاء أقدم الأوامر"""
        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
        
            all_orders = []
            for order in open_orders:
                order_time = datetime.fromtimestamp(order['time'] / 1000)
                all_orders.append({
                    'orderId': order['orderId'],
                    'time': order_time,
                    'type': order['type'],
                    'side': order['side'],
                    'price': order.get('price', 'N/A'),
                    'quantity': order.get('origQty', 'N/A')
                })
        
            all_orders.sort(key=lambda x: x['time'])
        
            cancelled_count = 0
            cancelled_info = []
        
            for i in range(min(num_to_cancel, len(all_orders))):
                try:
                    self.client.cancel_order(
                        symbol=symbol,
                        orderId=all_orders[i]['orderId']
                    )
                    cancelled_count += 1
                    cancelled_info.append(f"{all_orders[i]['type']} - {all_orders[i]['side']} - {all_orders[i]['price']}")
                    logger.info(f"تم إلغاء الأمر القديم: {all_orders[i]['orderId']}")
                
                    self.add_trade_record(
                        trade_type="cancel",
                        quantity=float(all_orders[i]['quantity']),
                        price=float(all_orders[i]['price']),
                        trade_size=0,
                        signal_strength=0,
                        order_id=all_orders[i]['orderId'],
                        status="cancelled"
                    )
                
                except Exception as e:
                    logger.error(f"خطأ في إلغاء الأمر {all_orders[i]['orderId']}: {e}")
        
            return cancelled_count, cancelled_info
        
        except Exception as e:
            logger.error(f"خطأ في إلغاء الأوامر القديمة: {e}")
            return 0, []
    
    def manage_order_space(self, symbol):
        """إدارة مساحة الأوامر (محدثة)"""
        try:
            order_status = self.get_order_space_status(symbol)
            
            if order_status == "FULL":
                self.send_notification("⛔ الأوامر ممتلئة - تم إلغاء الصفقة الجديدة لحماية الصفقات الحالية")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"خطأ في إدارة المساحة: {e}")
            return False
    
    def calculate_signal_strength(self, data, signal_type='buy'):
        """تقييم قوة الإشارة من -100 إلى +100% مع الأوزان الجديدة"""
        latest = data.iloc[-1]
        score = 0

        # تسجيل مساهمة كل مؤشر
        indicator_contributions = {}

        # 1. اتجاه السوق (25%) - المؤشر الرئيسي
        market_trend_score = self.calculate_market_trend_score(data, signal_type)
        score += market_trend_score
        indicator_contributions['market_trend'] = market_trend_score

        # 2. المتوسطات المتحركة (20%) - EMA 34 بدلاً من المتعددة
        ema_score = self.calculate_ema_score(data, signal_type)
        score += ema_score
        indicator_contributions['moving_averages'] = ema_score

        # 3. MACD (20%) - زيادة الوزن
        macd_score = self.calculate_macd_score(data, signal_type)
        score += macd_score
        indicator_contributions['macd'] = macd_score

        # 4. RSI (15%) - تقليل الوزن قليلاً
        rsi_score = self.calculate_rsi_score(data, signal_type)
        score += rsi_score
        indicator_contributions['rsi'] = rsi_score

        # 5. بولينجر باند (20%) - زيادة الوزن
        bb_score = self.calculate_bollinger_bands_score(data, signal_type)
        score += bb_score
        indicator_contributions['bollinger_bands'] = bb_score

        # 6. الحجم (20%) - إضافة مؤشر الحجم كعنصر رئيسي
        volume_score = self.calculate_volume_score(data, signal_type)
        score += volume_score
        indicator_contributions['volume'] = volume_score

        # تخزين مساهمات المؤشرات للاستخدام لاحقاً
        self.last_indicator_contributions = indicator_contributions

        return max(min(score, 100), -100)

    def calculate_ema_score(self, data, signal_type):
        """حساب درجة المتوسطات المتحركة بتدرج منطقي"""
        latest = data.iloc[-1]
    
        # حساب قوة الاتجاه بالنسبة لـ EMA 34
        price_vs_ema = ((latest['close'] - latest['ema34']) / latest['ema34']) * 100
    
        if signal_type == 'buy':
            if price_vs_ema > 5.0:  # فوق EMA 34 بأكثر من 5%
                return 20.0  # 100%
            elif price_vs_ema > 2.0:  # فوق بـ 2-5%
                return 15.0  # 75%
            elif price_vs_ema > 0.5:  # فوق بـ 0.5-2%
                return 10.0  # 50%
            elif price_vs_ema > -1.0:  # قريب (-1% إلى +0.5%)
                return 5.0   # 25%
            elif price_vs_ema > -3.0:  # تحت بـ 1-3%
                return -5.0  # عقوبة -25%
            else:  # تحت بأكثر من 3%
                return -15.0 # عقوبة قوية -75%
    
        else:  # sell
            if price_vs_ema < -5.0:  # تحت EMA 34 بأكثر من 5%
                return 20.0  # 100%
            elif price_vs_ema < -2.0:  # تحت بـ 2-5%
                return 15.0  # 75%
            elif price_vs_ema < -0.5:  # تحت بـ 0.5-2%
                return 10.0  # 50%
            elif price_vs_ema < 1.0:   # قريب (-0.5% إلى +1%)
                return 5.0   # 25%
            elif price_vs_ema < 3.0:   # فوق بـ 1-3%
                return -5.0  # عقوبة -25%
            else:  # فوق بأكثر من 3%
                return -15.0 # عقوبة قوية -75%

    def calculate_macd_score(self, data, signal_type):
        """حساب درجة MACD بتدرج منطقي"""
        latest = data.iloc[-1]
    
        # قوة الإشارة (الفرق بين MACD وخط الإشارة)
        macd_diff = latest['macd'] - latest['macd_sig']
    
        # قوة الاتجاه (قيمة MACD المطلقة)
        macd_strength = abs(latest['macd'])
    
        # مزيج من قوة الإشارة وقوة الاتجاه
        combined_score = (macd_diff * 0.7) + (macd_strength * 0.3)
    
        if signal_type == 'buy':
            if combined_score > 0.4:    # إشارة شراء قوية جداً
                return 20.0  # 100%
            elif combined_score > 0.2:  # إشارة شراء قوية
                return 16.0  # 80%
            elif combined_score > 0.1:  # إشارة شراء متوسطة
                return 12.0  # 60%
            elif combined_score > 0.05: # إشارة شراء خفيفة
                return 8.0   # 40%
            elif combined_score > -0.05: # محايد
                return 0.0   # 0%
            elif combined_score > -0.1: # إشارة بيع خفيفة
                return -6.0  # عقوبة -30%
            elif combined_score > -0.2: # إشارة بيع متوسطة
                return -12.0 # عقوبة -60%
            else:            # إشارة بيع قوية
                return -18.0 # عقوبة -90%
    
        else:  # sell
            if combined_score < -0.4:   # إشارة بيع قوية جداً
                return 20.0  # 100%
            elif combined_score < -0.2: # إشارة بيع قوية
                return 16.0  # 80%
            elif combined_score < -0.1: # إشارة بيع متوسطة
                return 12.0  # 60%
            elif combined_score < -0.05: # إشارة بيع خفيفة
                return 8.0   # 40%
            elif combined_score < 0.05:  # محايد
                return 0.0   # 0%
            elif combined_score < 0.1:   # إشارة شراء خفيفة
                return -6.0  # عقوبة -30%
            elif combined_score < 0.2:   # إشارة شراء متوسطة
                return -12.0 # عقوبة -60%
            else:            # إشارة شراء قوية
                return -18.0 # عقوبة -90%

    def calculate_rsi_score(self, data, signal_type):
        """حساب درجة RSI بتدرج منطقي"""
        latest = data.iloc[-1]
        rsi = latest['rsi']
    
        if signal_type == 'buy':
            if rsi < 25:    # ذروة بيع شديدة
                return 15.0  # 100%
            elif rsi < 30:   # ذروة بيع
                return 12.0  # 80%
            elif rsi < 35:   # منطقة بيع
                return 8.0   # 53%
            elif rsi < 45:   # محايد مائل للبيع
                return 4.0   # 27%
            elif rsi < 55:   # محايد تماماً
                return 0.0   # 0%
            elif rsi < 65:   # محايد مائل للشراء
                return -4.0  # عقوبة -27%
            elif rsi < 70:   # منطقة شراء
                return -8.0  # عقوبة -53%
            else:            # ذروة شراء
                return -15.0 # عقوبة -100%
    
        else:  # sell
            if rsi > 75:    # ذروة شراء شديدة
                return 15.0  # 100%
            elif rsi > 70:   # ذروة شراء
                return 12.0  # 80%
            elif rsi > 65:   # منطقة شراء
                return 8.0   # 53%
            elif rsi > 55:   # محايد مائل للشراء
                return 4.0   # 27%
            elif rsi > 45:   # محايد تماماً
                return 0.0   # 0%
            elif rsi > 35:   # محايد مائل للبيع
                return -4.0  # عقوبة -27%
            elif rsi > 30:   # منطقة بيع
                return -8.0  # عقوبة -53%
            else:            # ذروة بيع
                return -15.0 # عقوبة -100%

    def calculate_bollinger_bands_score(self, data, signal_type):
        """حساب درجة بولينجر باند بتدرج منطقي"""
        latest = data.iloc[-1]
    
        # حساب الموقع النسبي بين النطاقات
        bb_position = (latest['close'] - latest['bb_lower']) / (latest['bb_upper'] - latest['bb_lower'])
    
        # عرض النطاق (مؤشر للتقلب)
        bb_width = (latest['bb_upper'] - latest['bb_lower']) / latest['bb_middle']
    
        if signal_type == 'buy':
            if bb_position < 0.05:      # قرب النطاق السفلي جداً
                return 20.0  # 100%
            elif bb_position < 0.15:    # قرب النطاق السفلي
                return 16.0  # 80%
            elif bb_position < 0.25:    # في الثلث السفلي
                return 12.0  # 60%
            elif bb_position < 0.4:     # في النصف السفلي
                return 8.0   # 40%
            elif bb_position < 0.6:     # في المنتصف
                return 4.0   # 20%
            elif bb_position < 0.75:    # في النصف العلوي
                return -4.0  # عقوبة -20%
            elif bb_position < 0.85:    # في الثلث العلوي
                return -8.0  # عقوبة -40%
            elif bb_position < 0.95:    # قرب النطاق العلوي
                return -12.0 # عقوبة -60%
            else:            # عند أو فوق النطاق العلوي
                return -16.0 # عقوبة -80%
    
        else:  # sell
            if bb_position > 0.95:      # قرب النطاق العلوي جداً
                return 20.0  # 100%
            elif bb_position > 0.85:    # قرب النطاق العلوي
                return 16.0  # 80%
            elif bb_position > 0.75:    # في الثلث العلوي
                return 12.0  # 60%
            elif bb_position > 0.6:     # في النصف العلوي
                return 8.0   # 40%
            elif bb_position > 0.4:     # في المنتصف
                return 4.0   # 20%
            elif bb_position > 0.25:    # في النصف السفلي
                return -4.0  # عقوبة -20%
            elif bb_position > 0.15:    # في الثلث السفلي
                return -8.0  # عقوبة -40%
            elif bb_position > 0.05:    # قرب النطاق السفلي
                return -12.0 # عقوبة -60%
            else:            # عند أو تحت النطاق السفلي
                return -16.0 # عقوبة -80%

    def calculate_volume_score(self, data, signal_type):
        """حساب درجة الحجم بتدرج دقيق"""
        latest = data.iloc[-1]
        volume_ratio = latest['vol_ratio']
    
        # اتجاه الحركة السعرية
        price_move = latest['close'] - latest['open']
        price_direction = 1 if price_move > 0 else -1 if price_move < 0 else 0
    
        # التوافق بين الحجم والاتجاه
        direction_match = (price_direction == 1 and signal_type == 'buy') or \
                         (price_direction == -1 and signal_type == 'sell')
    
        if volume_ratio > 3.5:          # حجم استثنائي
            score = 18.0 + (2.0 if direction_match else -4.0)
        elif volume_ratio > 2.5:        # حجم عالي جداً
            score = 15.0 + (2.0 if direction_match else -3.0)
        elif volume_ratio > 2.0:        # حجم عالي
            score = 12.0 + (2.0 if direction_match else -2.0)
        elif volume_ratio > 1.5:        # حجم فوق المتوسط
            score = 9.0 + (1.0 if direction_match else -1.0)
        elif volume_ratio > 1.2:        # حجم جيد
            score = 6.0 + (1.0 if direction_match else -1.0)
        elif volume_ratio > 0.9:        # حجم طبيعي
            score = 3.0
        elif volume_ratio > 0.7:        # حجم منخفض
            score = 0.0
        elif volume_ratio > 0.5:        # حجم منخفض جداً
            score = -4.0
        elif volume_ratio > 0.3:        # حجم ضعيف
            score = -8.0
        else:                           # حجم شبه معدوم
            score = -12.0
    
        return max(min(score, 20.0), -20.0)

    def calculate_time_weight_score(self, signal_type):
        """حساب درجة الوزن الزمني"""
        time_signal = self.time_weight_manager.get_current_time_weight()
        
        if not time_signal:
            return 0
        
        # الوزن الزمني الأساسي (من -10 إلى +10)
        base_time_weight = time_signal['weight']
        
        # تحويل إلى نطاق -20 إلى +20 (20% من 100)
        time_score = base_time_weight * 2.0
        
        # إذا كانت الإشارة المطلوبة لا تتطابق مع الإشارة الزمنية
        if (signal_type == 'buy' and time_signal['signal'] == 'SELL') or \
           (signal_type == 'sell' and time_signal['signal'] == 'BUY'):
            time_score = -abs(time_score)  # عقوبة أكبر لتعارض الإشارات
        
        return time_score

    def calculate_market_trend_score(self, data, signal_type):
        """حساب درجة اتجاه السوق بتدرج منطقي"""
        latest = data.iloc[-1]
    
        # اتجاه طويل الأجل (EMA 200) + اتجاه متوسط (EMA 34)
        price_vs_ema200 = ((latest['close'] - latest['ema200']) / latest['ema200']) * 100
        price_vs_ema34 = ((latest['close'] - latest['ema34']) / latest['ema34']) * 100
    
        trend_strength = (price_vs_ema200 * 0.4) + (price_vs_ema34 * 0.6)  # وزن أكبر لـ EMA 34
    
        if signal_type == 'buy':
            if trend_strength > 8.0:    # اتجاه صعودي قوي جداً
                return 25.0  # 100%
            elif trend_strength > 4.0:  # اتجاه صعودي قوي
                return 20.0  # 80%
            elif trend_strength > 1.5:  # اتجاه صعودي معتدل
                return 15.0  # 60%
            elif trend_strength > -1.0: # اتجاه محايد
                return 8.0   # 32%
            elif trend_strength > -3.0: # اتجاه هبوطي طفيف
                return 2.0   # 8%
            elif trend_strength > -6.0: # اتجاه هبوطي
                return -10.0 # عقوبة -40%
            else:            # اتجاه هبوطي قوي
                return -20.0 # عقوبة -80%
    
        else:  # sell
            if trend_strength < -8.0:   # اتجاه هبوطي قوي جداً
                return 25.0  # 100%
            elif trend_strength < -4.0: # اتجاه هبوطي قوي
                return 20.0  # 80%
            elif trend_strength < -1.5: # اتجاه هبوطي معتدل
                return 15.0  # 60%
            elif trend_strength < 1.0:  # اتجاه محايد
                return 8.0   # 32%
            elif trend_strength < 3.0:  # اتجاه صعودي طفيف
                return 2.0   # 8%
            elif trend_strength < 6.0:  # اتجاه صعودي
                return -10.0 # عقوبة -40%
            else:            # اتجاه صعودي قوي
                return -20.0 # عقوبة -80%
    
    def calculate_cci_momentum(self, data, signal_type, period=20):
        """حساب مؤشر CCI للزخم (الجديد)"""
        try:
            # حساب CCI بشكل صحيح
            typical_price = (data['high'] + data['low'] + data['close']) / 3
            sma = typical_price.rolling(window=period).mean()
            mean_deviation = typical_price.rolling(window=period).apply(
                lambda x: np.mean(np.abs(x - np.mean(x)))
            )
            cci = (typical_price - sma) / (0.015 * mean_deviation)
            
            current_cci = cci.iloc[-1]
            
            if signal_type == 'buy':
                if current_cci < -100:  # ذروة بيع
                    return 8
                elif current_cci > 100:  # ذروة شراء
                    return -8
            else:
                if current_cci > 100:  # ذروة شراء
                    return 8
                elif current_cci < -100:  # ذروة بيع
                    return -8
                    
        except Exception as e:
            logger.error(f"خطأ في حساب CCI: {e}")
        
        return 0
    
    def calculate_dollar_size(self, signal_strength, signal_type='buy'):
        """حساب حجم الصفقة بالدولار حسب قوة الإشارة"""
        abs_strength = abs(signal_strength)
        
        if signal_type == 'buy' and signal_strength > 0:
            if abs_strength >= 80:    # إشارة شراء قوية جداً
                base_size = 30
                bonus = (abs_strength - 80) * 1.0
                return min(base_size + bonus, 50)
            
            elif abs_strength >= 50:  # إشارة شراء جيدة
                base_size = 15
                bonus = (abs_strength - 50) * 0.5
                return min(base_size + bonus, 25)
            
            elif abs_strength >= 25:  # إشارة شراء خفيفة
                base_size = 5
                bonus = (abs_strength - 25) * 0.3
                return min(base_size + bonus, 10)
            
            else:
                return 0
                
        elif signal_type == 'sell' and signal_strength > 0:
            if abs_strength >= 80:    # إشارة بيع قوية جداً
                base_size = 30
                bonus = (abs_strength - 80) * 1.0
                return min(base_size + bonus, 50)
            
            elif abs_strength >= 50:  # إشارة بيع جيدة
                base_size = 15
                bonus = (abs_strength - 50) * 0.5
                return min(base_size + bonus, 25)
            
            elif abs_strength >= 25:  # إشارة بيع خفيفة
                base_size = 5
                bonus = (abs_strength - 25) * 0.3
                return min(base_size + bonus, 10)
            
            else:
                return 0
        else:
            return 0
    
    def get_strength_level(self, strength):
        """الحصول على اسم مستوى القوة"""
        abs_strength = abs(strength)
        if abs_strength >= 80: return "4 🟢 (قوي جداً)"
        elif abs_strength >= 50: return "3 🟡 (قوي)"
        elif abs_strength >= 25: return "2 🔵 (متوسط)"
        else: return "1 ⚪ (ضعيف)"
    
    def calculate_rsi(self, data, period=14):
        delta = data.diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        
        avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50)
    
    def calculate_ma(self, data, period):
        return data.rolling(window=period).mean()
    
    def calculate_bollinger_bands(self, data, period=20, std_dev=2):
        sma = data.rolling(window=period).mean()
        std = data.rolling(window=period).std()
        upper_band = sma + (std * std_dev)
        lower_band = sma - (std * std_dev)
        return upper_band, sma, lower_band
    
    def calculate_atr(self, df, period=14):
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr1 = (high - low).abs()
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    def calculate_macd(self, series, fast=12, slow=26, signal=9):
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        sig = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - sig
        return macd_line, sig, hist
    
    def get_historical_data(self, interval=Client.KLINE_INTERVAL_15MINUTE, lookback='2000 hour ago UTC'):
        try:
            klines = self.client.get_historical_klines(self.symbol, interval, lookback)
            if not klines:
                error_msg = f"⚠️ لا توجد بيانات لـ {self.symbol}"
                self.send_notification(error_msg)
                return None
            
            data = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 
                                            'close_time', 'quote_asset_volume', 'number_of_trades', 
                                            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
            data['timestamp'] = pd.to_datetime(data['timestamp'], unit='ms')
            for col in ['open', 'high', 'low', 'close', 'volume']:
                data[col] = pd.to_numeric(data[col], errors='coerce')
        
            data = data.dropna()
        
            if len(data) < 100:
                error_msg = f"⚠️ بيانات غير كافية لـ {self.symbol}: {len(data)} صفوف فقط"
                self.send_notification(error_msg)
                return None
        
            # حساب جميع المؤشرات المطلوبة
            data['rsi'] = self.calculate_rsi(data['close'])
            data['atr'] = self.calculate_atr(data)
            data = data.dropna() 

            data['ema34'] = data['close'].ewm(span=34, adjust=False).mean()
        
            # المتوسطات المتحركة الأسية
            data['ema200'] = data['close'].ewm(span=200, adjust=False).mean()
            data['ema50'] = data['close'].ewm(span=50, adjust=False).mean()
            data['ema21'] = data['close'].ewm(span=21, adjust=False).mean()
            data['ema9'] = data['close'].ewm(span=9, adjust=False).mean()
        
            # حساب Bollinger Bands
            data['bb_upper'], data['bb_middle'], data['bb_lower'] = self.calculate_bollinger_bands(data['close'])
        
            data['vol_ma20'] = data['volume'].rolling(20).mean()
            data['vol_ratio'] = data['volume'] / data['vol_ma20']
         
            macd_line, macd_sig, macd_hist = self.calculate_macd(data['close'])
            data['macd'] = macd_line
            data['macd_sig'] = macd_sig
            data['macd_hist'] = macd_hist
        
            return data
        except Exception as e:
            error_msg = f"❌ خطأ في جلب البيانات: {e}"
            self.send_notification(error_msg)
            return None

    def calculate_dynamic_stop_loss_take_profit(self, entry_price, signal_strength, atr_value):
        """حساب وقف الخسارة وجني الأرباح بشكل ديناميكي - مع تحسين RR"""
        abs_strength = abs(signal_strength)
    
        # تحسين معاملات ATR لنسبة مخاطرة/عائد أفضل
        if abs_strength >= 80:    # إشارة قوية → مجال أوسع
            stop_multiplier = 3.5    # زيادة الحماية (من 4.0)
            profit_multiplier = 7.0  # أهداف أكبر (من 6.0) - RR ≈ 2:1
        elif abs_strength >= 50:  # إشارة متوسطة → مجال متوسط
            stop_multiplier = 3.0    # (من 3.5)
            profit_multiplier = 6.0  # (من 5.0) - RR ≈ 2:1
        else:                     # إشارة ضعيفة → مجال أقرب
            stop_multiplier = 2.5    # (من 3.0)
            profit_multiplier = 5.0  # (من 4.0) - RR ≈ 2:1
    
        if signal_strength > 0:  # إشارة شراء
            stop_loss = entry_price - (stop_multiplier * atr_value)
            take_profit = entry_price + (profit_multiplier * atr_value)
        else:  # إشارة بيع
            stop_loss = entry_price + (stop_multiplier * atr_value)
            take_profit = entry_price - (profit_multiplier * atr_value)
    
        return stop_loss, take_profit

    def get_dynamic_threshold(self, signal_type):
        """عتبات ديناميكية حسب قوة الاتجاه العام"""
        data = self.get_historical_data()
        if data is None:
            return self.BASELINE_BUY_THRESHOLD if signal_type == 'buy' else self.SELL_THRESHOLD
    
        latest = data.iloc[-1]
        trend_strength = abs((latest['close'] - latest['ema34']) / latest['ema34'] * 100)
    
        if signal_type == 'buy':
            if trend_strength > 5:  # اتجاه قوي
                return self.BASELINE_BUY_THRESHOLD - 5  # تخفيف العتبة في الاتجاه القوي
            elif trend_strength < 2:  # اتجاه ضعيف
                return self.BASELINE_BUY_THRESHOLD + 5  # تشديد العتبة في الاتجاه الضعيف
            else:
                return self.BASELINE_BUY_THRESHOLD
        else:
            # نفس المنطق للبيع ولكن بشكل معكوس
            return self.SELL_THRESHOLD
    
    def execute_real_trade(self, signal_type, signal_strength, current_price, stop_loss, take_profit):
        if signal_type == 'buy':
            if not self.manage_order_space(self.symbol):
                self.send_notification("❌ تم إلغاء الصفقة - لا يمكن وضع أوامر الوقف")
                return False
        
        try:
            trade_size = self.calculate_dollar_size(signal_strength, signal_type)
        
            if trade_size <= 0:
                return False
        
            logger.info(f"بدء تنفيذ صفقة {signal_type} بقوة {signal_strength}% بحجم {trade_size}$")
        
            if signal_type == 'buy':
                can_trade, usdt_balance = self.check_balance_before_trade(trade_size)
                        
                if not can_trade:
                    available_balance = usdt_balance * 0.95
                    if available_balance >= 5:
                        trade_size = available_balance
                        self.send_notification(f"⚠️ تعديل حجم الصفقة. أصبح: ${trade_size:.2f} (الرصيد المتاح: ${usdt_balance:.2f})")
                    else:
                        self.send_notification(f"❌ رصيد غير كافي حتى لأصغر صفقة. المطلوب: $5، المتاح: ${usdt_balance:.2f}")
                        return False
            
                quantity = trade_size / current_price
            
                info = self.client.get_symbol_info(self.symbol)
                step_size = float([f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0])
                precision = len(str(step_size).split('.')[1].rstrip('0'))
                quantity = round(quantity - (quantity % step_size), precision)
            
                if quantity <= 0:
                    self.send_notification("⚠️ الكمية غير صالحة للشراء")
                    return False
            
                order = self.client.order_market_buy(
                    symbol=self.symbol,
                    quantity=quantity
                )
            
                self.add_trade_record(
                    trade_type="buy",
                    quantity=quantity,
                    price=current_price,
                    trade_size=trade_size,
                    signal_strength=signal_strength,
                    order_id=order.get('orderId', 'N/A')
                )
            
                # التحقق من مساحة الأوامر مرة أخرى قبل وضع أوامر الوقف
                order_status = self.get_order_space_status(self.symbol)
                if order_status == "FULL":
                    self.send_notification("⚠️ لا يمكن وضع أوامر الوقف - المساحة ممتلئة")
                    return True
            
                try:
                    formatted_stop_loss = self.format_price(stop_loss, self.symbol)
                    formatted_take_profit = self.format_price(take_profit, self.symbol)
                
                    oco_order = self.client.order_oco_sell(
                        symbol=self.symbol,
                        quantity=quantity,
                        stopPrice=formatted_stop_loss,
                        stopLimitPrice=formatted_stop_loss,
                        price=formatted_take_profit,
                        stopLimitTimeInForce='GTC'
                    )
                    logger.info(f"✅ تم وضع أوامر الوقف: SL={formatted_stop_loss}, TP={formatted_take_profit}")
                
                except Exception as e:
                    error_msg = f"⚠️ فشل وضع أوامر الوقف: {e}"
                    self.send_notification(error_msg)
                    logger.error(error_msg)
            
                return True
            
            elif signal_type == 'sell':
                total_balance, balances, _ = self.get_account_balance_details()
                bnb_balance = balances.get('BNB', {}).get('free', 0)
            
                if bnb_balance <= 0.001:
                    self.send_notification("⚠️ رصيد BNB غير كافي للبيع")
                    return False
            
                quantity_by_trade_size = trade_size / current_price
            
                if quantity_by_trade_size > bnb_balance:
                    available_balance = bnb_balance * 0.95
                    quantity_to_sell = available_balance
                    actual_trade_size = quantity_to_sell * current_price
                
                    if actual_trade_size >= 5:
                        trade_size = actual_trade_size
                        self.send_notification(f"⚠️ تعديل حجم صفقة البيع. أصبح: ${trade_size:.2f} (الرصيد المتاح: {bnb_balance:.6f} BNB)")
                    else:
                        self.send_notification(f"❌ رصيد BNB غير كافي حتى لأصغر صفقة بيع. المطلوب: $5، المتاح: ${bnb_balance * current_price:.2f}")
                        return False
                else:
                    quantity_to_sell = quantity_by_trade_size
            
                info = self.client.get_symbol_info(self.symbol)
                step_size = float([f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0])
                precision = len(str(step_size).split('.')[1].rstrip('0'))
                quantity = round(quantity_to_sell - (quantity_to_sell % step_size), precision)
            
                if quantity <= 0:
                    self.send_notification("⚠️ الكمية غير صالحة للبيع")
                    return False
            
                order = self.client.order_market_sell(
                    symbol=self.symbol,
                    quantity=quantity
                )
            
                self.add_trade_record(
                    trade_type="sell",
                    quantity=quantity,
                    price=current_price,
                    trade_size=quantity * current_price,
                    signal_strength=signal_strength,
                    order_id=order.get('orderId', 'N/A')
                 )
            
                return True
            
        except Exception as e:
            error_msg = f"❌ خطأ في تنفيذ الصفقة: {e}"
            self.send_notification(error_msg)
            logger.error(error_msg)
            return False
    
    def bnb_strategy(self, data):
        """استراتيجية التداول - الإصدار المحسن"""
        if data is None or len(data) < 100:
            return 'hold', 0, 0, 0
        
        latest = data.iloc[-1]
        current_price = latest['close']
        atr_value = latest['atr']
        
        buy_strength = self.calculate_signal_strength(data, 'buy')
        sell_strength = self.calculate_signal_strength(data, 'sell')
        
        # الحصول على حالة مساحة الأوامر
        order_space_status = self.get_order_space_status(self.symbol)
        
        # تطبيق العتبات المختلفة حسب حالة الأوامر
        if buy_strength > 0 and buy_strength > sell_strength:
            if order_space_status == "FULL":
                # الأوامر ممتلئة - تطبيق العتبة المشددة
                if buy_strength >= self.STRICT_BUY_THRESHOLD:
                    stop_loss, take_profit = self.calculate_dynamic_stop_loss_take_profit(
                        current_price, buy_strength, atr_value
                    )
                    return 'buy', buy_strength, stop_loss, take_profit
                else:
                    logger.info(f"📊 إشارة شراء قوتها {buy_strength}% تم تجاهلها (العتبة المشددة: {self.STRICT_BUY_THRESHOLD}%)")
                    return 'hold', 0, 0, 0
            else:
                # الأوامر متاحة - تطبيق العتبة الأساسية
                current_threshold = self.get_dynamic_threshold('buy') 
                if buy_strength >= current_threshold:
                    stop_loss, take_profit = self.calculate_dynamic_stop_loss_take_profit(
                        current_price, buy_strength, atr_value
                    )
                    return 'buy', buy_strength, stop_loss, take_profit
                else:
                    logger.info(f"📊 إشارة شراء قوتها {buy_strength}% تم تجاهلها (العتبة الأساسية: {self.BASELINE_BUY_THRESHOLD}%)")
                    return 'hold', 0, 0, 0
                    
        elif sell_strength > 0 and sell_strength > buy_strength:
            # البيع لا يتأثر بمساحة الأوامر (عتبة ثابتة)
            if sell_strength >= self.SELL_THRESHOLD:
                stop_loss, take_profit = self.calculate_dynamic_stop_loss_take_profit(
                    current_price, -sell_strength, atr_value
                )
                return 'sell', sell_strength, stop_loss, take_profit
            else:
                logger.info(f"📊 إشارة بيع قوتها {sell_strength}% تم تجاهلها (عتبة البيع: {self.SELL_THRESHOLD}%)")
                return 'hold', 0, 0, 0
        
        else:
            return 'hold', 0, 0, 0
    
    def check_balance_before_trade(self, required_usdt):
        """التحقق من الرصيد قبل التنفيذ"""
        try:
            total_balance, balances, _ = self.get_account_balance_details()
            usdt_balance = balances.get('USDT', {}).get('free', 0)
            
            if usdt_balance >= required_usdt:
                return True, usdt_balance
            else:
                return False, usdt_balance
        except Exception as e:
            logger.error(f"خطأ في التحقق من الرصيد: {e}")
            return False, 0
    
    def execute_trade(self):
        data = self.get_historical_data()
        if data is None:
            return False
            
        signal_type, signal_strength, stop_loss, take_profit = self.bnb_strategy(data)
        latest = data.iloc[-1]
        current_price = latest['close']
        
        if signal_type in ['buy', 'sell']:
            # إرسال إشعار تحليلي قبل التنفيذ
            order_status = self.get_order_space_status(self.symbol)
            analysis_msg = self.generate_signal_analysis(data, signal_type, signal_strength, order_status)
            self.send_notification(analysis_msg)
            
            success = self.execute_real_trade(signal_type, signal_strength, current_price, stop_loss, take_profit)
            if success:
                level = self.get_strength_level(signal_strength)
                msg = f"🎯 <b>{'شراء' if signal_type == 'buy' else 'بيع'} بمستوى {level}</b>\n\n"
                msg += f"قوة الإشارة: {signal_strength}%\n"
                msg += f"حجم الصفقة: ${self.calculate_dollar_size(signal_strength, signal_type):.2f}\n"
                msg += f"السعر: ${current_price:.4f}\n"
                
                if signal_type == 'buy':
                    msg += f"وقف الخسارة: ${stop_loss:.4f}\n"
                    msg += f"جني الأرباح: ${take_profit:.4f}\n"
                    msg += f"نسبة المخاطرة/العائد: 1:{(take_profit - current_price) / (current_price - stop_loss):.2f}\n"
                
                msg += f"حالة الأوامر: {order_status}"
                self.send_notification(msg)
            return success
        
        return False

    def generate_signal_analysis(self, data, signal_type, signal_strength, order_status):
        """إنشاء تحليل مفصل للإشارة مع نسبة مساهمة كل مؤشر"""
        latest = data.iloc[-1]

        analysis = f"📊 <b>تحليل الإشارة ({signal_type.upper()}) - النظام الجديد</b>\n\n"
        analysis += f"قوة الإشارة: {signal_strength}%\n"
        analysis += f"السعر الحالي: ${latest['close']:.4f}\n"
        analysis += f"الاتجاه العام (EMA 34): {'صاعد' if latest['close'] > latest['ema34'] else 'هبوطي'}\n\n"

        # إضافة مساهمة كل مؤشر مع الأوزان الجديدة
        analysis += "📈 <b>مساهمة المؤشرات (الأوزان الجديدة):</b>\n"

        if hasattr(self, 'last_indicator_contributions'):
            contributions = self.last_indicator_contributions
    
            # تحويل أسماء المؤشرات للعربية مع الأوزان
            indicator_names = {
                'market_trend': 'اتجاه السوق (25%)',
                'moving_averages': 'المتوسطات المتحركة (20%)',
                'macd': 'مؤشر MACD (20%)',
                'rsi': 'مؤشر RSI (15%)',
                'bollinger_bands': 'بولينجر باند (20%)',
                'volume': 'الحجم (20%)'
            }
    
            for indicator, value in contributions.items():
                arabic_name = indicator_names.get(indicator, indicator)
                emoji = "🟢" if value > 0 else "🔴" if value < 0 else "⚪"
                analysis += f"{emoji} {arabic_name}: {value:+.1f}\n"

    
        analysis += f"\n📊 <b>التفاصيل الفنية:</b>\n"
        analysis += f"EMA 34: ${latest['ema34']:.4f}\n"
        analysis += f"السعر/EMA 34: {((latest['close'] - latest['ema34']) / latest['ema34'] * 100):+.2f}%\n"
        analysis += f"RSI: {latest['rsi']:.1f}\n"
        analysis += f"MACD: {latest['macd']:.6f}\n"
        analysis += f"الحجم: {latest['vol_ratio']:.1f}x المتوسط\n"
        analysis += f"بولينجر: {((latest['close'] - latest['bb_lower']) / (latest['bb_upper'] - latest['bb_lower']) * 100):.1f}%\n"
        analysis += f"حالة الأوامر: {order_status}\n"
    
        # تعريف المتغير مسبقاً لتجنب الخطأ
        required_threshold = 0
    
        if signal_type == 'buy':
            required_threshold = self.STRICT_BUY_THRESHOLD if order_status == "FULL" else self.BASELINE_BUY_THRESHOLD
            analysis += f"العتبة المطلوبة: {required_threshold}%\n"
        else:
            required_threshold = self.SELL_THRESHOLD
            analysis += f"العتبة المطلوبة: {required_threshold}%\n"
    
        analysis += f"القرار: {'✅ مقبولة' if abs(signal_strength) >= required_threshold else '❌ مرفوضة'}"
    
        return analysis
    
    def check_time_alerts(self):
        """التحقق من الإنذارات الزمنية وإرسالها"""
        try:
            alert_windows = self.time_weight_manager.check_alert_windows(alert_minutes=5)
            
            for window in alert_windows:
                alert_message = self.generate_time_alert_message(window)
                self.send_notification(alert_message)
                
        except Exception as e:
            logger.error(f"خطأ في التحقق من الإنذارات الزمنية: {e}")

    def generate_time_alert_message(self, window):
        """إنشاء رسالة إنذار زمني"""
        emoji = "🟢" if window['signal'] == 'BUY' else "🔴"
        action = "شراء" if window['signal'] == 'BUY' else "بيع"
        
        message = f"⏰ <b>إنذار مسبق - نافذة تداول قريبة</b>\n\n"
        message += f"{emoji} إشارة {action} قوية قادمة خلال {window['minutes_away']:.1f} دقائق\n"
        message += f"📅 اليوم: {window['weekday_arabic']}\n"
        message += f"🕒 الوقت: {window['time']}\n"
        message += f"💪 قوة الإشارة: {window['strength']:.1f}/10\n"
        message += f"📊 الوزن: {window['weight']:.2f}\n\n"
        message += f"⚡ استعد لفرصة تداول محتملة"
        
        return message

    def get_daily_optimal_schedule(self):
        """الحصول على جدول النوافذ المثلى لليوم"""
        today_english = datetime.now().strftime('%A')
        today_arabic = self.time_weight_manager.weekday_map.get(today_english, today_english)
        
        today_windows = [
            window for window in self.time_weight_manager.optimal_windows 
            if window['weekday_arabic'] == today_arabic
        ]
        
        # ترتيب النوافذ حسب الوقت
        today_windows.sort(key=lambda x: (x['hour'], x['minute']))
        
        return today_windows

    def send_performance_report(self):
        try:
            total_balance, balances, bnb_price = self.get_account_balance_details()
            
            if total_balance is None:
                return
            
            profit_loss = total_balance - self.initial_balance
            profit_loss_percent = (profit_loss / self.initial_balance) * 100 if self.initial_balance > 0 else 0
            
            current_orders = self.get_algo_orders_count(self.symbol)
            order_status = self.get_order_space_status(self.symbol)
            
            balance_details = ""
            for asset, balance_info in balances.items():
                if balance_info['total'] > 0.0001:
                    if asset == 'USDT':
                        balance_details += f"{asset}: {balance_info['total']:.2f}\n"
                    else:
                        balance_details += f"{asset}: {balance_info['total']:.6f}\n"
            
            message = f"📊 <b>تقرير أداء البوت المحسن</b>\n\n"
            message += f"الرصيد الابتدائي: ${self.initial_balance:.2f}\n"
            message += f"الرصيد الحالي: ${total_balance:.2f}\n"
            message += f"الأرباح/الخسائر: ${profit_loss:.2f} ({profit_loss_percent:+.2f}%)\n"
            message += f"الأوامر النشطة: {current_orders}/{self.MAX_ALGO_ORDERS}\n"
            message += f"حالة الأوامر: {order_status}\n"
            message += f"عتبة الشراء: {self.BASELINE_BUY_THRESHOLD}% (عادي) / {self.STRICT_BUY_THRESHOLD}% (مشدد)\n\n"
            message += f"<b>تفاصيل الرصيد:</b>\n{balance_details}"
            
            if bnb_price:
                message += f"\nسعر BNB الحالي: ${bnb_price:.4f}"
            
            report_12h = self.generate_12h_trading_report()
            if 'total_trades' in report_12h and report_12h['total_trades'] > 0:
                message += f"\n\n📈 <b>آخر 12 ساعة:</b>"
                message += f"\nإجمالي الصفقات: {report_12h['total_trades']}"
                message += f"\nصفقات شراء: {report_12h['buy_trades']} (${report_12h['total_buy_size']})"
                message += f"\nصفقات بيع: {report_12h['sell_trades']} (${report_12h['total_sell_size']})"
                message += f"\nنسبة النجاح: {report_12h['win_rate']:.1f}%"
            
            self.send_notification(message)
            
        except Exception as e:
            error_msg = f"❌ خطأ في إرسال تقرير الأداء: {e}"
            logger.error(error_msg)
    
    def send_daily_report(self):
        """إرسال تقرير يومي شامل"""
        try:
            daily_report = self.generate_daily_performance_report()
            
            if 'error' in daily_report:
                return
            
            performance = daily_report['performance']
            signal_analysis = daily_report['signal_analysis']
            recommendations = daily_report['recommendations']
            
            message = f"📅 <b>تقرير أداء يومي - {daily_report['date']}</b>\n\n"
            message += f"💰 الرصيد الابتدائي: ${performance['daily_start_balance']:.2f}\n"
            message += f"💰 الرصيد النهائي: ${performance['daily_end_balance']:.2f}\n"
            message += f"📈 صافي الربح/الخسارة: ${performance['daily_pnl']:.2f} ({performance['daily_return']:+.2f}%)\n\n"
            
            message += f"📊 <b>أداء التداول:</b>\n"
            message += f"• إجمالي الصفقات: {performance['total_trades']}\n"
            message += f"• الصفقات الرابحة: {performance['winning_trades']}\n"
            message += f"• الصفقات الخاسرة: {performance['losing_trades']}\n"
            message += f"• نسبة النجاح: {performance['win_rate']:.1f}%\n"
            message += f"• عامل الربحية: {performance['profit_factor']:.2f}\n\n"
            
            message += f"📈 <b>تحليل الإشارات:</b>\n"
            message += f"• إشارات قوية: {signal_analysis['strong_signals']} ({signal_analysis['strong_win_rate']:.1f}% نجاح)\n"
            message += f"• إشارات متوسطة: {signal_analysis['medium_signals']} ({signal_analysis['medium_win_rate']:.1f}% نجاح)\n"
            message += f"• إشارات ضعيفة: {signal_analysis['weak_signals']} ({signal_analysis['weak_win_rate']:.1f}% نجاح)\n\n"
            
            message += f"⚙️ <b>إعدادات العتبات:</b>\n"
            message += f"• شراء أساسي: {self.BASELINE_BUY_THRESHOLD}%\n"
            message += f"• شراء مشدد: {self.STRICT_BUY_THRESHOLD}%\n"
            message += f"• بيع: {self.SELL_THRESHOLD}%\n\n"
            
            message += f"💡 <b>توصيات:</b>\n"
            for rec in recommendations:
                message += f"• {rec}\n"
            
            self.send_notification(message)
            
            # إعادة تعيين إحصائيات اليوم
            self.performance_analyzer.reset_daily_stats(performance['daily_end_balance'])
            
            
        except Exception as e:
            error_msg = f"❌ خطأ في إرسال التقرير اليومي: {e}"
            logger.error(error_msg)
    
    def run(self):
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        interval_minutes = 15
        self.send_notification(
            f"🚀 بدء تشغيل بوت تداول BNB المحسن\n\n"
            f"سيعمل البوت على فحص السوق كل {interval_minutes} دقيقة\n"
            f"نطاق حجم الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}\n"
            f"عتبة الشراء الأساسية: {self.BASELINE_BUY_THRESHOLD}%\n"
            f"عتبة الشراء المشددة: {self.STRICT_BUY_THRESHOLD}%\n"
            f"عتبة البيع: {self.SELL_THRESHOLD}%\n"
            f"الحد الأقصى للأوامر: {self.MAX_ALGO_ORDERS}"
        )
        
        self.send_performance_report()
        
        report_counter = 0
        last_daily_report = datetime.now()
        
        while True:
            try:
                trade_executed = self.execute_trade()
                
                report_counter += 1
                if trade_executed or report_counter >= 4:
                    self.send_performance_report()
                    report_counter = 0
                
                # إرسال تقرير يومي في الساعة 23:59
                current_time = datetime.now()
                if current_time.hour == 23 and current_time.minute >= 59:
                    if (current_time - last_daily_report).total_seconds() > 3600:
                        self.send_daily_report()
                        last_daily_report = current_time
                
                time.sleep(interval_minutes * 60)
                
            except Exception as e:
                error_msg = f"❌ خطأ غير متوقع في التشغيل: {e}"
                self.send_notification(error_msg)
                logger.error(error_msg)
                time.sleep(300)

if __name__ == "__main__":
    try:
        print("🚀 بدء تشغيل بوت تداول BNB المحسن...")
        print("=" * 60)
        
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        print("🌐 خادم الويب يعمل على المنفذ 10000")
        
        bot = BNB_Trading_Bot()
        
        if bot.test_connection():
            print("✅ اختبار الاتصال ناجح!")
            print("🎯 بدء التشغيل الفعلي للبوت...")
            bot.run()
        
    except Exception as e:
        logger.error(f"فشل تشغيل البوت: {e}")
        print(f"❌ فشل تشغيل البوت: {e}")
