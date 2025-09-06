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
    return {'status': 'healthy', 'service': 'multi-crypto-trading-bot', 'timestamp': datetime.now().isoformat()}

@app.route('/status')
def status():
    return {'status': 'running', 'bot': 'Multi Crypto Trading Bot', 'time': datetime.now().isoformat()}

@app.route('/recent_trades/<symbol>')
def recent_trades(symbol):
    try:
        bot = Multi_Crypto_Trading_Bot()
        report = bot.generate_12h_trading_report(symbol)
        return report
    except Exception as e:
        return {'error': str(e)}

@app.route('/daily_report/<symbol>')
def daily_report(symbol):
    try:
        bot = Multi_Crypto_Trading_Bot()
        report = bot.generate_daily_performance_report(symbol)
        return report
    except Exception as e:
        return {'error': str(e)}

@app.route('/performance')
def performance():
    try:
        bot = Multi_Crypto_Trading_Bot()
        report = bot.get_overall_performance()
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
        logging.FileHandler('multi_bot_activity.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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
    def __init__(self, symbol):
        self.symbol = symbol
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
            'symbol': self.symbol,
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

class CryptoBot:
    def __init__(self, symbol, notifier, client, trade_settings):
        self.symbol = symbol
        self.notifier = notifier
        self.client = client
        self.trade_history = []
        self.performance_analyzer = PerformanceAnalyzer(symbol)
        
        # إعدادات خاصة بكل عملة
        self.BASELINE_BUY_THRESHOLD = trade_settings['baseline_buy_threshold']
        self.STRICT_BUY_THRESHOLD = trade_settings['strict_buy_threshold']
        self.SELL_THRESHOLD = trade_settings['sell_threshold']
        self.MIN_TRADE_SIZE = trade_settings['min_trade_size']
        self.MAX_TRADE_SIZE = trade_settings['max_trade_size']
        
        self.last_buy_contributions = {}
        self.last_sell_contributions = {}
        
        self.load_trade_history()
        
    def load_trade_history(self):
        """تحميل تاريخ الصفقات من ملف"""
        try:
            if os.path.exists(f'trade_history_{self.symbol}.json'):
                with open(f'trade_history_{self.symbol}.json', 'r', encoding='utf-8') as f:
                    self.trade_history = json.load(f)
        except Exception as e:
            logger.error(f"خطأ في تحميل تاريخ الصفقات لـ {self.symbol}: {e}")
            self.trade_history = []

    def save_trade_history(self):
        """حفظ تاريخ الصفقات إلى ملف"""
        try:
            with open(f'trade_history_{self.symbol}.json', 'w', encoding='utf-8') as f:
                json.dump(self.trade_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"خطأ في حفظ تاريخ الصفقات لـ {self.symbol}: {e}")

    def add_trade_record(self, trade_type, quantity, price, trade_size, signal_strength, order_id=None, status="executed", profit_loss=0):
        """إضافة سجل صفقة جديدة"""
        trade_record = {
            'symbol': self.symbol,
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
                return {"symbol": self.symbol, "message": "لا توجد صفقات في آخر 12 ساعة"}
            
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
                "symbol": self.symbol,
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
            logger.error(f"خطأ في إنشاء تقرير التداول لـ {self.symbol}: {e}")
            return {"symbol": self.symbol, "error": str(e)}

    def generate_daily_performance_report(self):
        """إنشاء تقرير أداء يومي شامل"""
        try:
            current_balance = self.get_symbol_balance()
            performance = self.performance_analyzer.calculate_daily_performance(current_balance)
            
            # تحليل جودة الإشارات
            strong_signals = [t for t in self.performance_analyzer.daily_trades if abs(t['signal_strength']) >= 80]
            medium_signals = [t for t in self.performance_analyzer.daily_trades if 50 <= abs(t['signal_strength']) < 80]
            weak_signals = [t for t in self.performance_analyzer.daily_trades if abs(t['signal_strength']) < 50]
            
            strong_win_rate = (len([t for t in strong_signals if t.get('profit_loss', 0) > 0]) / len(strong_signals) * 100) if strong_signals else 0
            medium_win_rate = (len([t for t in medium_signals if t.get('profit_loss', 0) > 0]) / len(medium_signals) * 100) if medium_signals else 0
            weak_win_rate = (len([t for t in weak_signals if t.get('profit_loss', 0) > 0]) / len(weak_signals) * 100) if weak_signals else 0
            
            report = {
                "symbol": self.symbol,
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
            logger.error(f"خطأ في إنشاء التقرير اليومي لـ {self.symbol}: {e}")
            return {"symbol": self.symbol, "error": str(e)}

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

    def get_symbol_balance(self):
        """الحصول على رصيد العملة الخاصة"""
        try:
            account = self.client.get_account()
            balances = {asset['asset']: float(asset['free']) + float(asset['locked']) for asset in account['balances']}
            
            asset = self.symbol.replace('USDT', '')
            return balances.get(asset, 0)
        except Exception as e:
            logger.error(f"خطأ في جلب رصيد {self.symbol}: {e}")
            return 0

    def get_algo_orders_count(self):
        """الحصول على عدد جميع الأوامر المعلقة"""
        try:
            open_orders = self.client.get_open_orders(symbol=self.symbol)
            return len(open_orders)
        except Exception as e:
            logger.error(f"خطأ في جلب عدد الأوامر النشطة لـ {self.symbol}: {e}")
            return 0
    
    def get_order_space_status(self):
        """الحصول على حالة مساحة الأوامر"""
        try:
            current_orders = self.get_algo_orders_count()
            
            if current_orders >= 10:  # MAX_ALGO_ORDERS
                return "FULL"
            elif current_orders >= 8:  # MAX_ALGO_ORDERS - 2
                return "NEAR_FULL"
            else:
                return "AVAILABLE"
                
        except Exception as e:
            logger.error(f"خطأ في التحقق من حالة الأوامر لـ {self.symbol}: {e}")
            return "FULL"
    
    def manage_order_space(self):
        """إدارة مساحة الأوامر"""
        try:
            order_status = self.get_order_space_status()
            
            if order_status == "FULL":
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"خطأ في إدارة المساحة لـ {self.symbol}: {e}")
            return False
    
    def calculate_signal_strength(self, data, signal_type='buy'):
        """تقييم قوة الإشارة من -100 إلى +100% مع حسابات منفصلة للشراء والبيع"""
        latest = data.iloc[-1]
        score = 0

        # تسجيل مساهمة كل مؤشر بشكل منفصل
        indicator_contributions = {}

        # 1. اتجاه السوق (25%)
        market_trend_score = self.calculate_market_trend_score(data, signal_type)
        score += market_trend_score
        indicator_contributions['market_trend'] = market_trend_score

        # 2. المتوسطات المتحركة (20%)
        ema_score = self.calculate_ema_score(data, signal_type)
        score += ema_score
        indicator_contributions['moving_averages'] = ema_score

        # 3. MACD (20%)
        macd_score = self.calculate_macd_score(data, signal_type)
        score += macd_score
        indicator_contributions['macd'] = macd_score

        # 4. RSI (15%)
        rsi_score = self.calculate_rsi_score(data, signal_type)
        score += rsi_score
        indicator_contributions['rsi'] = rsi_score

        # 5. بولينجر باند (20%)
        bb_score = self.calculate_bollinger_bands_score(data, signal_type)
        score += bb_score
        indicator_contributions['bollinger_bands'] = bb_score

        # 6. الحجم (20%)
        volume_score = self.calculate_volume_score(data, signal_type)
        score += volume_score
        indicator_contributions['volume'] = volume_score

        # تخزين مساهمات المؤشرات حسب نوع الإشارة
        if signal_type == 'buy':
            self.last_buy_contributions = indicator_contributions
        else:
            self.last_sell_contributions = indicator_contributions

        return max(min(score, 100), -100)

    def calculate_ema_score(self, data, signal_type):
        """حساب درجة المتوسطات المتحركة بتدرج منطقي"""
        latest = data.iloc[-1]
    
        # حساب قوة الاتجاه بالنسبة لـ EMA 34
        price_vs_ema = ((latest['close'] - latest['ema34']) / latest['ema34']) * 100
    
        if signal_type == 'buy':
            if price_vs_ema > 5.0:  # فوق EMA 34 بأكثر من 5%
                return 20.0
            elif price_vs_ema > 2.0:  # فوق بـ 2-5%
                return 15.0
            elif price_vs_ema > 0.5:  # فوق بـ 0.5-2%
                return 10.0
            elif price_vs_ema > -1.0:  # قريب (-1% إلى +0.5%)
                return 5.0
            elif price_vs_ema > -3.0:  # تحت بـ 1-3%
                return -5.0
            else:  # تحت بأكثر من 3%
                return -15.0
    
        else:  # sell
            if price_vs_ema < -5.0:  # تحت EMA 34 بأكثر من 5%
                return 20.0
            elif price_vs_ema < -2.0:  # تحت بـ 2-5%
                return 15.0
            elif price_vs_ema < -0.5:  # تحت بـ 0.5-2%
                return 10.0
            elif price_vs_ema < 1.0:   # قريب (-0.5% إلى +1%)
                return 5.0
            elif price_vs_ema < 3.0:   # فوق بـ 1-3%
                return -5.0
            else:  # فوق بأكثر من 3%
                return -15.0

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
                return 20.0
            elif combined_score > 0.2:  # إشارة شراء قوية
                return 16.0
            elif combined_score > 0.1:  # إشارة شراء متوسطة
                return 12.0
            elif combined_score > 0.05: # إشارة شراء خفيفة
                return 8.0
            elif combined_score > -0.05: # محايد
                return 0.0
            elif combined_score > -0.1: # إشارة بيع خفيفة
                return -6.0
            elif combined_score > -0.2: # إشارة بيع متوسطة
                return -12.0
            else:            # إشارة بيع قوية
                return -18.0
    
        else:  # sell
            if combined_score < -0.4:   # إشارة بيع قوية جداً
                return 20.0
            elif combined_score < -0.2: # إشارة بيع قوية
                return 16.0
            elif combined_score < -0.1: # إشارة بيع متوسطة
                return 12.0
            elif combined_score < -0.05: # إشارة بيع خفيفة
                return 8.0
            elif combined_score < 0.05:  # محايد
                return 0.0
            elif combined_score < 0.1:   # إشارة شراء خفيفة
                return -6.0
            elif combined_score < 0.2:   # إشارة شراء متوسطة
                return -12.0
            else:            # إشارة شراء قوية
                return -18.0

    def calculate_rsi_score(self, data, signal_type):
        """حساب درجة RSI بتدرج منطقي"""
        latest = data.iloc[-1]
        rsi = latest['rsi']
    
        if signal_type == 'buy':
            if rsi < 25:    # ذروة بيع شديدة
                return 15.0
            elif rsi < 30:   # ذروة بيع
                return 12.0
            elif rsi < 35:   # منطقة بيع
                return 8.0
            elif rsi < 45:   # محايد مائل للبيع
                return 4.0
            elif rsi < 55:   # محايد تماماً
                return 0.0
            elif rsi < 65:   # محايد mائل للشراء
                return -4.0
            elif rsi < 70:   # منطقة شراء
                return -8.0
            else:            # ذروة شراء
                return -15.0
    
        else:  # sell
            if rsi > 75:    # ذروة شراء شديدة
                return 15.0
            elif rsi > 70:   # ذروة شراء
                return 12.0
            elif rsi > 65:   # منطقة شراء
                return 8.0
            elif rsi > 55:   # محايد مائل للشراء
                return 4.0
            elif rsi > 45:   # محايد تماماً
                return 0.0
            elif rsi > 35:   # محايد مائل للبيع
                return -4.0
            elif rsi > 30:   # منطقة بيع
                return -8.0
            else:            # ذروة بيع
                return -15.0

    def calculate_bollinger_bands_score(self, data, signal_type):
        """حساب درجة بولينجر باند بتدرج منطقي"""
        latest = data.iloc[-1]
    
        # حساب الموقع النسبي بين النطاقات
        bb_position = (latest['close'] - latest['bb_lower']) / (latest['bb_upper'] - latest['bb_lower'])
    
        if signal_type == 'buy':
            if bb_position < 0.05:      # قرب النطاق السفلي جداً
                return 20.0
            elif bb_position < 0.15:    # قرب النطاق السفلي
                return 16.0
            elif bb_position < 0.25:    # في الثلث السفلي
                return 12.0
            elif bb_position < 0.4:     # في النصف السفلي
                return 8.0
            elif bb_position < 0.6:     # في المنتصف
                return 4.0
            elif bb_position < 0.75:    # في النصف العلوي
                return -4.0
            elif bb_position < 0.85:    # في الثلث العلوي
                return -8.0
            elif bb_position < 0.95:    # قرب النطاق العلوي
                return -12.0
            else:            # عند أو فوق النطاق العلوي
                return -16.0
    
        else:  # sell
            if bb_position > 0.95:      # قرب النطاق العلوي جداً
                return 20.0
            elif bb_position > 0.85:    # قرب النطاق العلوي
                return 16.0
            elif bb_position > 0.75:    # في الثلث العلوي
                return 12.0
            elif bb_position > 0.6:     # في النصف العلوي
                return 8.0
            elif bb_position > 0.4:     # في المنتصف
                return 4.0
            elif bb_position > 0.25:    # في النصف السفلي
                return -4.0
            elif bb_position > 0.15:    # في الثلث السفلي
                return -8.0
            elif bb_position > 0.05:    # قرب النطاق السفلي
                return -12.0
            else:            # عند أو تحت النطاق السفلي
                return -16.0

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

    def calculate_market_trend_score(self, data, signal_type):
        """حساب درجة اتجاه السوق بتدرج منطقي"""
        latest = data.iloc[-1]
    
        # اتجاه طويل الأجل (EMA 200) + اتجاه متوسط (EMA 34)
        price_vs_ema200 = ((latest['close'] - latest['ema200']) / latest['ema200']) * 100
        price_vs_ema34 = ((latest['close'] - latest['ema34']) / latest['ema34']) * 100
    
        trend_strength = (price_vs_ema200 * 0.4) + (price_vs_ema34 * 0.6)
    
        if signal_type == 'buy':
            if trend_strength > 8.0:    # اتجاه صعودي قوي جداً
                return 25.0
            elif trend_strength > 4.0:  # اتجاه صعودي قوي
                return 20.0
            elif trend_strength > 1.5:  # اتجاه صعودي معتدل
                return 15.0
            elif trend_strength > -1.0: # اتجاه محايد
                return 8.0
            elif trend_strength > -3.0: # اتجاه هبوطي طفيف
                return 2.0
            elif trend_strength > -6.0: # اتجاه هبوطي
                return -10.0
            else:            # اتجاه هبوطي قوي
                return -20.0
    
        else:  # sell
            if trend_strength < -8.0:   # اتجاه هبوطي قوي جداً
                return 25.0
            elif trend_strength < -4.0: # اتجاه هبوطي قوي
                return 20.0
            elif trend_strength < -1.5: # اتجاه هبوطي معتدل
                return 15.0
            elif trend_strength < 1.0:  # اتجاه محايد
                return 8.0
            elif trend_strength < 3.0:  # اتجاه صعودي طفيف
                return 2.0
            elif trend_strength < 6.0:  # اتجاه صعودي
                return -10.0
            else:            # اتجاه صعودي قوي
                return -20.0
    
    def calculate_dollar_size(self, signal_strength, signal_type='buy'):
        """حساب حجم الصفقة بالدولار حسب قوة الإشارة"""
        abs_strength = abs(signal_strength)
        
        if signal_type == 'buy' and signal_strength > 0:
            if abs_strength >= 80:    # إشارة شراء قوية جداً
                base_size = 30
                bonus = (abs_strength - 80) * 1.0
                return min(base_size + bonus, self.MAX_TRADE_SIZE)
            
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
                return min(base_size + bonus, self.MAX_TRADE_SIZE)
            
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
        atr = tr.rolling(period).mean()
        return atr
    
    def calculate_macd(self, data, fast=12, slow=26, signal=9):
        ema_fast = data.ewm(span=fast, adjust=False).mean()
        ema_slow = data.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        macd_signal = macd.ewm(span=signal, adjust=False).mean()
        return macd, macd_signal
    
    def calculate_volume_ratio(self, df, period=20):
        current_volume = df["volume"]
        avg_volume = df["volume"].rolling(window=period).mean()
        volume_ratio = current_volume / avg_volume
        return volume_ratio.fillna(1)
    
    def fetch_historical_data(self, symbol, interval='15m', limit=200):
        """جلب البيانات التاريخية"""
        try:
            klines = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'number_of_trades',
                'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
            ])
            
            # تحويل الأنواع
            numeric_cols = ['open', 'high', 'low', 'close', 'volume']
            df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors='coerce')
            
            # حساب المؤشرات
            df['ema34'] = self.calculate_ma(df['close'], 34)
            df['ema200'] = self.calculate_ma(df['close'], 200)
            df['rsi'] = self.calculate_rsi(df['close'], 14)
            df['macd'], df['macd_sig'] = self.calculate_macd(df['close'])
            df['bb_upper'], df['bb_middle'], df['bb_lower'] = self.calculate_bollinger_bands(df['close'])
            df['vol_ratio'] = self.calculate_volume_ratio(df)
            df['atr'] = self.calculate_atr(df)
            
            return df.dropna()
            
        except Exception as e:
            logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
            return None
    
    def place_buy_order(self, quantity, price, signal_strength):
        """وضع أمر شراء"""
        try:
            # التحقق من مساحة الأوامر
            if not self.manage_order_space():
                logger.warning(f"لا توجد مساحة كافية للأوامر لـ {self.symbol}")
                return None
            
            # وضع أمر الشراء
            order = self.client.order_limit_buy(
                symbol=self.symbol,
                quantity=quantity,
                price=price
            )
            
            # إضافة سجل الصفقة
            self.add_trade_record('buy', quantity, price, quantity * price, signal_strength, order['orderId'])
            
            # إرسال إشعار
            message = f"🟢 شراء {self.symbol}\n"
            message += f"الكمية: {quantity:.4f}\n"
            message += f"السعر: ${price:.4f}\n"
            message += f"القيمة: ${quantity * price:.2f}\n"
            message += f"قوة الإشارة: {signal_strength:.1f}% ({self.get_strength_level(signal_strength)})\n"
            message += f"الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            
            self.notifier.send_message(message)
            
            return order
            
        except Exception as e:
            logger.error(f"خطأ في وضع أمر الشراء لـ {self.symbol}: {e}")
            return None
    
    def place_sell_order(self, quantity, price, signal_strength):
        """وضع أمر بيع"""
        try:
            # التحقق من مساحة الأوامر
            if not self.manage_order_space():
                logger.warning(f"لا توجد مساحة كافية للأوامر لـ {self.symbol}")
                return None
            
            # وضع أمر البيع
            order = self.client.order_limit_sell(
                symbol=self.symbol,
                quantity=quantity,
                price=price
            )
            
            # إضافة سجل الصفقة
            self.add_trade_record('sell', quantity, price, quantity * price, signal_strength, order['orderId'])
            
            # إرسال إشعار
            message = f"🔴 بيع {self.symbol}\n"
            message += f"الكمية: {quantity:.4f}\n"
            message += f"السعر: ${price:.4f}\n"
            message += f"القيمة: ${quantity * price:.2f}\n"
            message += f"قوة الإشارة: {signal_strength:.1f}% ({self.get_strength_level(signal_strength)})\n"
            message += f"الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            
            self.notifier.send_message(message)
            
            return order
            
        except Exception as e:
            logger.error(f"خطأ في وضع أمر البيع لـ {self.symbol}: {e}")
            return None
    
    def cancel_all_orders(self):
        """إلغاء جميع الأوامر المعلقة"""
        try:
            orders = self.client.get_open_orders(symbol=self.symbol)
            for order in orders:
                self.client.cancel_order(symbol=self.symbol, orderId=order['orderId'])
                logger.info(f"تم إلغاء الأمر {order['orderId']} لـ {self.symbol}")
            
            return len(orders)
            
        except Exception as e:
            logger.error(f"خطأ في إلغاء الأوامر لـ {self.symbol}: {e}")
            return 0
    
    def execute_trading_strategy(self):
        """تنفيذ استراتيجية التداول"""
        try:
            # جلب البيانات
            data = self.fetch_historical_data(self.symbol)
            if data is None or len(data) < 50:
                logger.warning(f"بيانات غير كافية لـ {self.symbol}")
                return
            
            latest = data.iloc[-1]
            current_price = latest['close']
            
            # حساب قوة الإشارة للشراء والبيع
            buy_signal_strength = self.calculate_signal_strength(data, 'buy')
            sell_signal_strength = self.calculate_signal_strength(data, 'sell')
            
            logger.info(f"{self.symbol} - قوة إشارة الشراء: {buy_signal_strength:.1f}%, البيع: {sell_signal_strength:.1f}%")
            
            # اتخاذ قرار الشراء
            if buy_signal_strength > self.BASELINE_BUY_THRESHOLD:
                # حساب حجم الصفقة
                dollar_size = self.calculate_dollar_size(buy_signal_strength, 'buy')
                
                if dollar_size >= self.MIN_TRADE_SIZE:
                    # حساب الكمية
                    quantity = dollar_size / current_price
                    
                    # وضع أمر الشراء
                    self.place_buy_order(quantity, current_price, buy_signal_strength)
            
            # اتخاذ قرار البيع
            elif sell_signal_strength > self.SELL_THRESHOLD:
                # حساب حجم الصفقة
                dollar_size = self.calculate_dollar_size(sell_signal_strength, 'sell')
                
                if dollar_size >= self.MIN_TRADE_SIZE:
                    # حساب الكمية
                    quantity = dollar_size / current_price
                    
                    # وضع أمر البيع
                    self.place_sell_order(quantity, current_price, sell_signal_strength)
            
        except Exception as e:
            logger.error(f"خطأ في تنفيذ الاستراتيجية لـ {self.symbol}: {e}")

class Multi_Crypto_Trading_Bot:
    def __init__(self):
        # إعدادات API
        self.api_key = os.environ.get('BINANCE_API_KEY')
        self.api_secret = os.environ.get('BINANCE_API_SECRET')
        
        # إعدادات Telegram
        self.telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')
        
        # إنشاء العميل
        self.client = Client(self.api_key, self.api_secret, testnet=False)
        
        # إنشاء Notifier
        self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
        
        # إعدادات كل عملة
        self.bot_settings = {
            'BNBUSDT': {
                'baseline_buy_threshold': 25,
                'strict_buy_threshold': 50,
                'sell_threshold': 25,
                'min_trade_size': 5,
                'max_trade_size': 50
            },
            'ETHUSDT': {
                'baseline_buy_threshold': 25,
                'strict_buy_threshold': 50,
                'sell_threshold': 25,
                'min_trade_size': 5,
                'max_trade_size': 50
            }
        }
        
        # إنشاء البوتات
        self.bots = {}
        for symbol in self.bot_settings.keys():
            self.bots[symbol] = CryptoBot(
                symbol=symbol,
                notifier=self.notifier,
                client=self.client,
                trade_settings=self.bot_settings[symbol]
            )
        
        logger.info("تم تهيئة بوت التداول متعدد العملات بنجاح")
    
    def run_trading_cycle(self):
        """تشغيل دورة تداول واحدة لجميع العملات"""
        try:
            logger.info("بدء دورة التداول لجميع العملات...")
            
            for symbol, bot in self.bots.items():
                logger.info(f"معالجة {symbol}...")
                bot.execute_trading_strategy()
                time.sleep(2)  # فاصل بين العملات
            
            logger.info("اكتملت دورة التداول لجميع العملات")
            
        except Exception as e:
            logger.error(f"خطأ في دورة التداول: {e}")
    
    def generate_12h_trading_report(self, symbol=None):
        """إنشاء تقرير التداول لآخر 12 ساعة"""
        if symbol:
            return self.bots[symbol].generate_12h_trading_report()
        else:
            reports = {}
            for sym, bot in self.bots.items():
                reports[sym] = bot.generate_12h_trading_report()
            return reports
    
    def generate_daily_performance_report(self, symbol=None):
        """إنشاء تقرير أداء يومي"""
        if symbol:
            return self.bots[symbol].generate_daily_performance_report()
        else:
            reports = {}
            for sym, bot in self.bots.items():
                reports[sym] = bot.generate_daily_performance_report()
            return reports
    
    def get_overall_performance(self):
        """الحصول على أداء شامل لجميع العملات"""
        try:
            total_balance = 0
            total_pnl = 0
            total_trades = 0
            
            performance_data = {}
            
            for symbol, bot in self.bots.items():
                balance = bot.get_symbol_balance()
                performance = bot.performance_analyzer.calculate_daily_performance(balance)
                
                performance_data[symbol] = performance
                total_balance += balance
                total_pnl += performance['daily_pnl']
                total_trades += performance['total_trades']
            
            return {
                'overall': {
                    'total_balance': total_balance,
                    'total_pnl': total_pnl,
                    'total_trades': total_trades,
                    'timestamp': datetime.now().isoformat()
                },
                'symbols': performance_data
            }
            
        except Exception as e:
            logger.error(f"خطأ في حساب الأداء الشامل: {e}")
            return {"error": str(e)}

def main():
    """الدالة الرئيسية"""
    try:
        # تشغيل خادم Flask في خيط منفصل
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        # إنشاء البوت
        bot = Multi_Crypto_Trading_Bot()
        
        # إرسال رسالة بدء التشغيل
        bot.notifier.send_message("🚀 بدء تشغيل بوت التداول متعدد العملات (BNB + ETH)")
        
        # الحلقة الرئيسية
        while True:
            try:
                bot.run_trading_cycle()
                
                # انتظار 15 دقيقة بين الدورات
                time.sleep(900)
                
            except Exception as e:
                logger.error(f"خطأ في الحلقة الرئيسية: {e}")
                time.sleep(60)  # انتظار دقيقة قبل إعادة المحاولة
                
    except KeyboardInterrupt:
        logger.info("إيقاف البوت بواسطة المستخدم")
        bot.notifier.send_message("⛔ إيقاف بوت التداول متعدد العملات")
    except Exception as e:
        logger.error(f"خطأ غير متوقع: {e}")
        bot.notifier.send_message(f"💥 خطأ غير متوقع في البوت: {e}")

if __name__ == "__main__":
    main()