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
                    f"عتبة البيع: {self.SELL_THRESHOLD}%"
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
        """تقييم قوة الإشارة من -100 إلى +100%"""
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
        """حساب درجة الحجم"""
        latest = data.iloc[-1]
        volume = latest['volume']
        volume_ma = latest['volume_ma']
    
        # نسبة الحجم إلى المتوسط
        volume_ratio = volume / volume_ma if volume_ma > 0 else 1
    
        if signal_type == 'buy':
            if volume_ratio > 3.0:    # حجم كبير جداً (300%+)
                return 20.0  # 100%
            elif volume_ratio > 2.0:  # حجم كبير (200%+)
                return 16.0  # 80%
            elif volume_ratio > 1.5:  # حجم فوق المتوسط (150%+)
                return 12.0  # 60%
            elif volume_ratio > 1.2:  # حجم جيد (120%+)
                return 8.0   # 40%
            elif volume_ratio > 0.8:  # حجم طبيعي (80-120%)
                return 4.0   # 20%
            elif volume_ratio > 0.5:  # حجم منخفض (50-80%)
                return 0.0   # 0%
            else:            # حجم ضعيف جداً (<50%)
                return -8.0  # عقوبة -40%
    
        else:  # sell
            if volume_ratio > 3.0:    # حجم كبير جداً مع بيع
                return 20.0  # 100%
            elif volume_ratio > 2.0:  # حجم كبير مع بيع
                return 16.0  # 80%
            elif volume_ratio > 1.5:  # حجم فوق المتوسط مع بيع
                return 12.0  # 60%
            elif volume_ratio > 1.2:  # حجم جيد مع بيع
                return 8.0   # 40%
            elif volume_ratio > 0.8:  # حجم طبيعي
                return 4.0   # 20%
            elif volume_ratio > 0.5:  # حجم منخفض
                return 0.0   # 0%
            else:            # حجم ضعيف جداً
                return -8.0  # عقوبة -40%

    def calculate_market_trend_score(self, data, signal_type):
        """حساب درجة اتجاه السوق العام"""
        # استخدام آخر 50 شمعة لتحديد الاتجاه
        recent_data = data.tail(50)
    
        # اتجاه بسيط (سعر الإغلاق الحالي مقابل بداية الفترة)
        price_change = ((recent_data['close'].iloc[-1] / recent_data['close'].iloc[0]) - 1) * 100
    
        # قوة الاتجاه (معدل التغير)
        trend_strength = abs(price_change)
    
        if signal_type == 'buy':
            if price_change > 5.0:      # صعود قوي (>5%)
                return 25.0  # 100%
            elif price_change > 2.0:    # صعود جيد (2-5%)
                return 20.0  # 80%
            elif price_change > 0.5:    # صعود خفيف (0.5-2%)
                return 15.0  # 60%
            elif price_change > -1.0:   # ثبات نسبي (-1% إلى +0.5%)
                return 10.0  # 40%
            elif price_change > -3.0:   # هبوط خفيف (-3% إلى -1%)
                return 5.0   # 20%
            elif price_change > -6.0:   # هبوط متوسط (-6% إلى -3%)
                return 0.0   # 0%
            elif price_change > -10.0:  # هبوط قوي (-10% إلى -6%)
                return -10.0 # عقوبة -40%
            else:            # هبوط شديد (<-10%)
                return -20.0 # عقوبة -80%
    
        else:  # sell
            if price_change < -5.0:     # هبوط قوي (<-5%)
                return 25.0  # 100%
            elif price_change < -2.0:   # هبوط جيد (-5% إلى -2%)
                return 20.0  # 80%
            elif price_change < -0.5:   # هبوط خفيف (-2% إلى -0.5%)
                return 15.0  # 60%
            elif price_change < 1.0:    # ثبات نسبي (-0.5% إلى +1%)
                return 10.0  # 40%
            elif price_change < 3.0:    # صعود خفيف (1% إلى 3%)
                return 5.0   # 20%
            elif price_change < 6.0:    # صعود متوسط (3% إلى 6%)
                return 0.0   # 0%
            elif price_change < 10.0:   # صعود قوي (6% إلى 10%)
                return -10.0 # عقوبة -40%
            else:            # صعود شديد (>10%)
                return -20.0 # عقوبة -80%

    def get_historical_data(self, symbol, interval, limit=100):
        """جلب البيانات التاريخية من Binance"""
        try:
            klines = self.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )
            
            data = []
            for k in klines:
                data.append({
                    'timestamp': k[0],
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5]),
                    'close_time': k[6],
                    'quote_volume': float(k[7]),
                    'trades': k[8]
                })
            
            df = pd.DataFrame(data)
            
            # حساب المؤشرات الفنية
            df = self.calculate_technical_indicators(df)
            
            return df
            
        except Exception as e:
            error_msg = f"❌ خطأ في جلب البيانات التاريخية: {e}"
            logger.error(error_msg)
            if self.notifier:
                self.notifier.send_message(error_msg)
            return None

    def calculate_technical_indicators(self, df):
        """حساب المؤشرات الفنية"""
        try:
            # المتوسطات المتحركة
            df['ema34'] = df['close'].ewm(span=34).mean()
            
            # RSI
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
            
            # MACD
            exp12 = df['close'].ewm(span=12).mean()
            exp26 = df['close'].ewm(span=26).mean()
            df['macd'] = exp12 - exp26
            df['macd_sig'] = df['macd'].ewm(span=9).mean()
            
            # بولينجر باند
            df['bb_middle'] = df['close'].rolling(window=20).mean()
            bb_std = df['close'].rolling(window=20).std()
            df['bb_upper'] = df['bb_middle'] + (bb_std * 2)
            df['bb_lower'] = df['bb_middle'] - (bb_std * 2)
            
            # متوسط الحجم
            df['volume_ma'] = df['volume'].rolling(window=20).mean()
            
            return df
            
        except Exception as e:
            logger.error(f"خطأ في حساب المؤشرات الفنية: {e}")
            return df

    def calculate_trade_size(self, signal_strength, current_price):
        """حساب حجم الصفقة بناء على قوة الإشارة"""
        try:
            # تحويل قوة الإشارة إلى نسبة (0-100%)
            strength_percentage = abs(signal_strength) / 100.0
            
            # حساب حجم الصفقة بشكل تدريجي
            base_size = self.MIN_TRADE_SIZE
            adjustable_range = self.MAX_TRADE_SIZE - self.MIN_TRADE_SIZE
            
            trade_size = base_size + (adjustable_range * strength_percentage)
            
            # تقريب إلى منزلتين عشريتين
            trade_size = round(trade_size, 2)
            
            # التأكد من أن الحجم ضمن الحدود
            trade_size = max(self.MIN_TRADE_SIZE, min(trade_size, self.MAX_TRADE_SIZE))
            
            logger.info(f"📊 قوة الإشارة: {signal_strength:.1f}% -> حجم الصفقة: ${trade_size:.2f}")
            
            return trade_size
            
        except Exception as e:
            logger.error(f"خطأ في حساب حجم الصفقة: {e}")
            return self.MIN_TRADE_SIZE

    def execute_market_order(self, side, trade_size, symbol, signal_strength):
        """تنفيذ أمر سوقي مع إدارة المخاطر"""
        try:
            # التحقق من مساحة الأوامر أولاً
            if not self.manage_order_space(symbol):
                return None
            
            # الحصول على السعر الحالي
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            
            # حساب الكمية بناء على حجم الصفقة بالسعر الحالي
            quantity = trade_size / current_price
            
            # الحصول على معلومات الرمز للتقريب الصحيح
            symbol_info = self.client.get_symbol_info(symbol)
            step_size = next((filter['stepSize'] for filter in symbol_info['filters'] 
                            if filter['filterType'] == 'LOT_SIZE'), '0.000001')
            
            # تقريب الكمية إلى المنزلة الصحيحة
            precision = len(step_size.rstrip('0').split('.')[-1])
            quantity = round(quantity, precision)
            
            if quantity <= 0:
                logger.error("❌ الكمية غير صحيحة")
                return None
            
            # تنفيذ الأمر السوقي
            order = self.client.create_order(
                symbol=symbol,
                side=side,
                type=ORDER_TYPE_MARKET,
                quantity=quantity
            )
            
            # حساب السعر الفعلي للتنفيذ (متوسط سعر التنفيذ)
            fills = order.get('fills', [])
            if fills:
                executed_qty = sum(float(fill['qty']) for fill in fills)
                executed_price = sum(float(fill['price']) * float(fill['qty']) for fill in fills) / executed_qty
            else:
                executed_price = current_price
            
            # تسجيل الصفقة
            trade_type = "buy" if side == SIDE_BUY else "sell"
            self.add_trade_record(
                trade_type=trade_type,
                quantity=quantity,
                price=executed_price,
                trade_size=trade_size,
                signal_strength=signal_strength,
                order_id=order['orderId'],
                status="executed"
            )
            
            # إرسال إشعار
            order_type_emoji = "🟢" if side == SIDE_BUY else "🔴"
            message = (
                f"{order_type_emoji} <b>{'شراء' if side == SIDE_BUY else 'بيع'} BNB</b>\n\n"
                f"💰 الحجم: ${trade_size:.2f}\n"
                f"📊 الكمية: {quantity:.4f} BNB\n"
                f"🏷️ السعر: ${executed_price:.4f}\n"
                f"⚡ قوة الإشارة: {signal_strength:.1f}%\n"
                f"🆔 رقم الأمر: {order['orderId']}"
            )
            
            self.send_notification(message)
            
            logger.info(f"✅ تم تنفيذ أمر {side}: {quantity:.4f} BNB بسعر ${executed_price:.4f}")
            
            return order
            
        except Exception as e:
            error_msg = f"❌ خطأ في تنفيذ الأمر السوقي: {e}"
            logger.error(error_msg)
            self.send_notification(f"❌ فشل تنفيذ الأمر: {str(e)}")
            return None

    def execute_limit_order(self, side, trade_size, symbol, signal_strength, price_offset_percent=0.1):
        """تنفيذ أمر محدود مع إدارة المخاطر"""
        try:
            # التحقق من مساحة الأوامر أولاً
            if not self.manage_order_space(symbol):
                return None
            
            # الحصول على السعر الحالي
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            
            # حساب سعر الأمر المحدود مع إزاحة
            if side == SIDE_BUY:
                limit_price = current_price * (1 - price_offset_percent / 100)
            else:
                limit_price = current_price * (1 + price_offset_percent / 100)
            
            # تقريب السعر حسب متطلبات Binance
            limit_price = self.format_price(limit_price, symbol)
            
            # حساب الكمية
            quantity = trade_size / limit_price
            
            # الحصول على معلومات الرمز للتقريب الصحيح
            symbol_info = self.client.get_symbol_info(symbol)
            step_size = next((filter['stepSize'] for filter in symbol_info['filters'] 
                            if filter['filterType'] == 'LOT_SIZE'), '0.000001')
            
            # تقريب الكمية إلى المنزلة الصحيحة
            precision = len(step_size.rstrip('0').split('.')[-1])
            quantity = round(quantity, precision)
            
            if quantity <= 0:
                logger.error("❌ الكمية غير صحيحة")
                return None
            
            # تنفيذ الأمر المحدود
            order = self.client.create_order(
                symbol=symbol,
                side=side,
                type=ORDER_TYPE_LIMIT,
                timeInForce=TIME_IN_FORCE_GTC,
                quantity=quantity,
                price=format(limit_price, '.8f')
            )
            
            # تسجيل الصفقة
            trade_type = "buy" if side == SIDE_BUY else "sell"
            self.add_trade_record(
                trade_type=trade_type,
                quantity=quantity,
                price=limit_price,
                trade_size=trade_size,
                signal_strength=signal_strength,
                order_id=order['orderId'],
                status="pending"
            )
            
            # إرسال إشعار
            order_type_emoji = "🟢" if side == SIDE_BUY else "🔴"
            message = (
                f"{order_type_emoji} <b>{'شراء' if side == SIDE_BUY else 'بيع'} محدود لـ BNB</b>\n\n"
                f"💰 الحجم: ${trade_size:.2f}\n"
                f"📊 الكمية: {quantity:.4f} BNB\n"
                f"🏷️ السعر المستهدف: ${limit_price:.4f}\n"
                f"📈 السعر الحالي: ${current_price:.4f}\n"
                f"⚡ قوة الإشارة: {signal_strength:.1f}%\n"
                f"🆔 رقم الأمر: {order['orderId']}"
            )
            
            self.send_notification(message)
            
            logger.info(f"✅ تم وضع أمر {side} محدود: {quantity:.4f} BNB بسعر ${limit_price:.4f}")
            
            return order
            
        except Exception as e:
            error_msg = f"❌ خطأ في تنفيذ الأمر المحدود: {e}"
            logger.error(error_msg)
            self.send_notification(f"❌ فشل وضع الأمر المحدود: {str(e)}")
            return None

    def monitor_and_manage_orders(self):
        """مراقبة وإدارة الأوامر المعلقة"""
        try:
            open_orders = self.client.get_open_orders(symbol=self.symbol)
            
            for order in open_orders:
                order_id = order['orderId']
                order_time = datetime.fromtimestamp(order['time'] / 1000)
                time_diff = (datetime.now() - order_time).total_seconds() / 60  # الفرق بالدقائق
                
                # إذا مر أكثر من 30 دقيقة على الأمر ولم ينفذ
                if time_diff > 30:
                    try:
                        self.client.cancel_order(
                            symbol=self.symbol,
                            orderId=order_id
                        )
                        
                        # تحديث سجل الصفقة
                        for trade in self.trade_history:
                            if trade.get('order_id') == order_id and trade.get('status') == 'pending':
                                trade['status'] = 'cancelled'
                                break
                        
                        logger.info(f"✅ تم إلغاء الأمر {order_id} لانتهاء الوقت")
                        
                    except Exception as e:
                        logger.error(f"❌ خطأ في إلغاء الأمر {order_id}: {e}")
            
            # حفظ التغييرات في تاريخ الصفقات
            self.save_trade_history()
            
        except Exception as e:
            logger.error(f"❌ خطأ في مراقبة الأوامر: {e}")

    def run_trading_cycle(self):
        """تشغيل دورة تداول واحدة"""
        try:
            logger.info("🔄 بدء دورة التداول...")
            
            # جلب البيانات وتحليلها
            data = self.get_historical_data(self.symbol, Client.KLINE_INTERVAL_15MINUTE, 100)
            if data is None or data.empty:
                logger.error("❌ فشل في جلب البيانات")
                return
            
            # حساب قوة الإشارة للشراء والبيع
            buy_strength = self.calculate_signal_strength(data, 'buy')
            sell_strength = self.calculate_signal_strength(data, 'sell')
            
            logger.info(f"📊 قوة إشارة الشراء: {buy_strength:.1f}%")
            logger.info(f"📊 قوة إشارة البيع: {sell_strength:.1f}%")
            
            # الحصول على السعر الحالي
            ticker = self.client.get_symbol_ticker(symbol=self.symbol)
            current_price = float(ticker['price'])
            
            # اتخاذ قرار التداول
            if buy_strength >= self.BASELINE_BUY_THRESHOLD:
                # حساب حجم الصفقة بناء على قوة الإشارة
                trade_size = self.calculate_trade_size(buy_strength, current_price)
                
                # استخدام أمر السوق للإشارات القوية جداً، والمحدود للإشارات المتوسطة
                if buy_strength >= self.STRICT_BUY_THRESHOLD:
                    self.execute_market_order(SIDE_BUY, trade_size, self.symbol, buy_strength)
                else:
                    self.execute_limit_order(SIDE_BUY, trade_size, self.symbol, buy_strength)
                    
            elif sell_strength >= self.SELL_THRESHOLD:
                # حساب حجم الصفقة بناء على قوة الإشارة
                trade_size = self.calculate_trade_size(sell_strength, current_price)
                self.execute_market_order(SIDE_SELL, trade_size, self.symbol, sell_strength)
            
            # مراقبة وإدارة الأوامر المعلقة
            self.monitor_and_manage_orders()
            
            logger.info("✅ اكتملت دورة التداول بنجاح")
            
        except Exception as e:
            error_msg = f"❌ خطأ في دورة التداول: {e}"
            logger.error(error_msg)
            if self.notifier:
                self.notifier.send_message(error_msg)

    def run_continuous(self, cycle_minutes=15):
        """تشغيل البوت بشكل مستمر"""
        logger.info(f"🚀 بدء التشغيل المستمر - دورة كل {cycle_minutes} دقائق")
        
        if self.notifier:
            self.notifier.send_message(
                f"🚀 <b>بدء التشغيل المستمر لبوت BNB</b>\n\n"
                f"⏰ مدة الدورة: {cycle_minutes} دقائق\n"
                f"💰 نطاق حجم الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}\n"
                f"📈 عتبة الشراء: {self.BASELINE_BUY_THRESHOLD}%\n"
                f"🎯 عتبة الشراء المشددة: {self.STRICT_BUY_THRESHOLD}%\n"
                f"📉 عتبة البيع: {self.SELL_THRESHOLD}%\n"
                f"🌐 IP الخادم: {self.get_public_ip()}"
            )
        
        while True:
            try:
                self.run_trading_cycle()
                
                # انتظار حتى الدورة القادمة
                logger.info(f"⏳ انتظار {cycle_minutes} دقائق للدورة القادمة...")
                time.sleep(cycle_minutes * 60)
                
            except KeyboardInterrupt:
                logger.info("⏹️ تم إيقاف البوت بواسطة المستخدم")
                if self.notifier:
                    self.notifier.send_message("⏹️ <b>تم إيقاف بوت BNB يدوياً</b>")
                break
            except Exception as e:
                error_msg = f"❌ خطأ غير متوقع في التشغيل المستمر: {e}"
                logger.error(error_msg)
                if self.notifier:
                    self.notifier.send_message(error_msg)
                time.sleep(60)  # انتظار دقيقة قبل المحاولة مرة أخرى

def main():
    """الدالة الرئيسية"""
    try:
        # بدء خادم Flask في thread منفصل
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        logger.info("🌐 تم بدء خادم Flask للرصد الصحي")
        
        # تهيئة وتشغيل بوت التداول
        bot = BNB_Trading_Bot()
        bot.run_continuous(cycle_minutes=15)
        
    except Exception as e:
        logger.error(f"❌ خطأ في الدالة الرئيسية: {e}")
        if 'bot' in locals() and bot.notifier:
            bot.notifier.send_message(f"❌ <b>فشل تشغيل البوت:</b> {str(e)}")

if __name__ == "__main__":
    main()
