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
    return {'status': 'healthy', 'service': 'bnb-signal-bot', 'timestamp': datetime.now().isoformat()}

@app.route('/status')
def status():
    return {'status': 'running', 'bot': 'BNB Signal Bot', 'time': datetime.now().isoformat()}

@app.route('/recent_signals')
def recent_signals():
    try:
        bot = BNB_Signal_Bot()
        report = bot.generate_12h_signal_report()
        return report
    except Exception as e:
        return {'error': str(e)}

@app.route('/signal_analysis')
def signal_analysis():
    try:
        bot = BNB_Signal_Bot()
        analysis = bot.get_current_signal_analysis()
        return analysis
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
        logging.FileHandler('signal_activity.log', encoding='utf-8'),
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

class SignalAnalyzer:
    def __init__(self):
        self.signal_history = []
        self.signal_start_time = datetime.now()
        
    def add_signal(self, signal_data):
        self.signal_history.append(signal_data)
        
    def calculate_signal_performance(self):
        total_signals = len(self.signal_history)
        buy_signals = len([s for s in self.signal_history if s.get('type') == 'buy'])
        sell_signals = len([s for s in self.signal_history if s.get('type') == 'sell'])
        
        strong_signals = len([s for s in self.signal_history if abs(s.get('strength', 0)) >= 80])
        medium_signals = len([s for s in self.signal_history if 50 <= abs(s.get('strength', 0)) < 80])
        weak_signals = len([s for s in self.signal_history if abs(s.get('strength', 0)) < 50])
        
        avg_strength = np.mean([abs(s.get('strength', 0)) for s in self.signal_history]) if self.signal_history else 0
        
        return {
            'total_signals': total_signals,
            'buy_signals': buy_signals,
            'sell_signals': sell_signals,
            'strong_signals': strong_signals,
            'medium_signals': medium_signals,
            'weak_signals': weak_signals,
            'avg_signal_strength': round(avg_strength, 1)
        }

class BNB_Signal_Bot:
    def __init__(self, api_key=None, api_secret=None, telegram_token=None, telegram_chat_id=None):
        self.notifier = None
        self.signal_history = []
        self.signal_analyzer = SignalAnalyzer()
        self.load_signal_history()
        
        # إعدادات العتبات للإشارات
        self.BASELINE_BUY_THRESHOLD = 45
        self.STRICT_BUY_THRESHOLD = 55
        self.SELL_THRESHOLD = 40

        self.last_buy_contributions = {}
        self.last_sell_contributions = {}
        
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
            logger.info("✅ تم الاتصال بمنصة Binance لجلب البيانات")
            self.test_connection()
                
        except Exception as e:
            error_msg = f"❌ فشل الاتصال بـ Binance: {e}"
            logger.error(error_msg)
            raise ConnectionError(error_msg)
            
        self.symbol = "BNBUSDT"
        
        if telegram_token and telegram_chat_id:
            self.notifier = TelegramNotifier(telegram_token, telegram_chat_id)
            logger.info("تم تهيئة إشعارات Telegram")
        else:
            logger.warning("مفاتيح Telegram غير موجودة، سيتم تعطيل الإشعارات")
        
        try:
            success_msg = f"✅ تم تهيئة بوت الإشارات بنجاح"
            logger.info(success_msg)
            if self.notifier:
                self.notifier.send_message(
                    f"📊 <b>بدء تشغيل بوت إشارات BNB</b>\n\n"
                    f"{success_msg}\n"
                    f"عتبة الشراء الأساسية: {self.BASELINE_BUY_THRESHOLD}%\n"
                    f"عتبة الشراء المشددة: {self.STRICT_BUY_THRESHOLD}%\n"
                    f"عتبة البيع: {self.SELL_THRESHOLD}%\n"
                    f"وضع التشغيل: إرسال الإشارات فقط"
                )
        except Exception as e:
            logger.error(f"خطأ في تهيئة البوت: {e}")

    def load_signal_history(self):
        """تحميل تاريخ الإشارات من ملف"""
        try:
            if os.path.exists('signal_history.json'):
                with open('signal_history.json', 'r', encoding='utf-8') as f:
                    self.signal_history = json.load(f)
        except Exception as e:
            logger.error(f"خطأ في تحميل تاريخ الإشارات: {e}")
            self.signal_history = []

    def save_signal_history(self):
        """حفظ تاريخ الإشارات إلى ملف"""
        try:
            with open('signal_history.json', 'w', encoding='utf-8') as f:
                json.dump(self.signal_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"خطأ في حفظ تاريخ الإشارات: {e}")

    def add_signal_record(self, signal_type, strength, price, analysis):
        """إضافة سجل إشارة جديدة"""
        signal_record = {
            'timestamp': datetime.now().isoformat(),
            'type': signal_type,
            'strength': strength,
            'price': price,
            'analysis': analysis
        }
        self.signal_history.append(signal_record)
        self.signal_analyzer.add_signal(signal_record)
        self.save_signal_history()

    def generate_12h_signal_report(self):
        """إنشاء تقرير الإشارات لآخر 12 ساعة"""
        try:
            twelve_hours_ago = datetime.now() - timedelta(hours=12)
            recent_signals = [
                signal for signal in self.signal_history 
                if datetime.fromisoformat(signal['timestamp']) >= twelve_hours_ago
            ]
            
            if not recent_signals:
                return {"message": "لا توجد إشارات في آخر 12 ساعة"}
            
            # حساب الإحصائيات
            buy_signals = [s for s in recent_signals if s['type'] == 'buy']
            sell_signals = [s for s in recent_signals if s['type'] == 'sell']
            
            avg_buy_strength = np.mean([s['strength'] for s in buy_signals]) if buy_signals else 0
            avg_sell_strength = np.mean([s['strength'] for s in sell_signals]) if sell_signals else 0
            
            strong_signals = [s for s in recent_signals if abs(s['strength']) >= 80]
            medium_signals = [s for s in recent_signals if 50 <= abs(s['strength']) < 80]
            weak_signals = [s for s in recent_signals if abs(s['strength']) < 50]
            
            report = {
                "period": "آخر 12 ساعة",
                "total_signals": len(recent_signals),
                "buy_signals": len(buy_signals),
                "sell_signals": len(sell_signals),
                "strong_signals": len(strong_signals),
                "medium_signals": len(medium_signals),
                "weak_signals": len(weak_signals),
                "avg_buy_signal_strength": round(avg_buy_strength, 1),
                "avg_sell_signal_strength": round(avg_sell_strength, 1),
                "recent_signals": recent_signals[-10:]
            }
            
            return report
        except Exception as e:
            logger.error(f"خطأ في إنشاء تقرير الإشارات: {e}")
            return {"error": str(e)}

    def get_current_signal_analysis(self):
        """الحصول على التحليل الحالي للإشارات"""
        try:
            data = self.get_historical_data()
            if data is None:
                return {"error": "لا يمكن جلب البيانات الحالية"}
            
            buy_strength = self.calculate_signal_strength(data, 'buy')
            sell_strength = self.calculate_signal_strength(data, 'sell')
            latest = data.iloc[-1]
            
            analysis = {
                "timestamp": datetime.now().isoformat(),
                "current_price": latest['close'],
                "buy_signal_strength": buy_strength,
                "sell_signal_strength": sell_strength,
                "signal_decision": self.get_signal_decision(buy_strength, sell_strength),
                "technical_indicators": {
                    "rsi": round(latest['rsi'], 1),
                    "macd": round(latest['macd'], 6),
                    "ema_34": round(latest['ema34'], 4),
                    "ema_200": round(latest['ema200'], 4),
                    "bollinger_position": round(((latest['close'] - latest['bb_lower']) / (latest['bb_upper'] - latest['bb_lower']) * 100), 1),
                    "volume_ratio": round(latest['vol_ratio'], 1)
                }
            }
            
            return analysis
        except Exception as e:
            logger.error(f"خطأ في الحصول على التحليل الحالي: {e}")
            return {"error": str(e)}

    def get_signal_decision(self, buy_strength, sell_strength):
        """اتخاذ قرار الإشارة"""
        if buy_strength > 0 and buy_strength > sell_strength:
            if buy_strength >= self.BASELINE_BUY_THRESHOLD:
                return "BUY_SIGNAL"
            else:
                return "WEAK_BUY_SIGNAL"
        elif sell_strength > 0 and sell_strength > buy_strength:
            if sell_strength >= self.SELL_THRESHOLD:
                return "SELL_SIGNAL"
            else:
                return "WEAK_SELL_SIGNAL"
        else:
            return "HOLD_SIGNAL"

    def test_connection(self):
        try:
            server_time = self.client.get_server_time()
            logger.info(f"✅ الاتصال ناجح - وقت الخادم: {server_time['serverTime']}")
            
            public_ip = self.get_public_ip()
            logger.info(f"🌐 IP الخادم: {public_ip}")
            
            print("="*50)
            print("✅ اختبار الاتصال ناجح!")
            print("وضع التشغيل: إرسال الإشارات فقط")
            print(f"IP الخادم: {public_ip}")
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
    
    def send_notification(self, message):
        logger.info(message)
        if self.notifier:
            success = self.notifier.send_message(message)
            if not success:
                logger.error("فشل إرسال الإشعار إلى Telegram")
            return success
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
            if price_vs_ema > 5.0:
                return 20.0
            elif price_vs_ema > 2.0:
                return 15.0
            elif price_vs_ema > 0.5:
                return 10.0
            elif price_vs_ema > -1.0:
                return 5.0
            elif price_vs_ema > -3.0:
                return -5.0
            else:
                return -15.0
    
        else:  # sell
            if price_vs_ema < -5.0:
                return 20.0
            elif price_vs_ema < -2.0:
                return 15.0
            elif price_vs_ema < -0.5:
                return 10.0
            elif price_vs_ema < 1.0:
                return 5.0
            elif price_vs_ema < 3.0:
                return -5.0
            else:
                return -15.0

    def calculate_macd_score(self, data, signal_type):
        """حساب درجة MACD بتدرج منطقي"""
        latest = data.iloc[-1]
    
        macd_diff = latest['macd'] - latest['macd_sig']
        macd_strength = abs(latest['macd'])
        combined_score = (macd_diff * 0.7) + (macd_strength * 0.3)
    
        if signal_type == 'buy':
            if combined_score > 0.4:
                return 20.0
            elif combined_score > 0.2:
                return 16.0
            elif combined_score > 0.1:
                return 12.0
            elif combined_score > 0.05:
                return 8.0
            elif combined_score > -0.05:
                return 0.0
            elif combined_score > -0.1:
                return -6.0
            elif combined_score > -0.2:
                return -12.0
            else:
                return -18.0
    
        else:  # sell
            if combined_score < -0.4:
                return 20.0
            elif combined_score < -0.2:
                return 16.0
            elif combined_score < -0.1:
                return 12.0
            elif combined_score < -0.05:
                return 8.0
            elif combined_score < 0.05:
                return 0.0
            elif combined_score < 0.1:
                return -6.0
            elif combined_score < 0.2:
                return -12.0
            else:
                return -18.0

    def calculate_rsi_score(self, data, signal_type):
        """حساب درجة RSI بتدرج منطقي"""
        latest = data.iloc[-1]
        rsi = latest['rsi']
    
        if signal_type == 'buy':
            if rsi < 25:
                return 15.0
            elif rsi < 30:
                return 12.0
            elif rsi < 35:
                return 8.0
            elif rsi < 45:
                return 4.0
            elif rsi < 55:
                return 0.0
            elif rsi < 65:
                return -4.0
            elif rsi < 70:
                return -8.0
            else:
                return -15.0
    
        else:  # sell
            if rsi > 75:
                return 15.0
            elif rsi > 70:
                return 12.0
            elif rsi > 65:
                return 8.0
            elif rsi > 55:
                return 4.0
            elif rsi > 45:
                return 0.0
            elif rsi > 35:
                return -4.0
            elif rsi > 30:
                return -8.0
            else:
                return -15.0

    def calculate_bollinger_bands_score(self, data, signal_type):
        """حساب درجة بولينجر باند بتدرج منطقي"""
        latest = data.iloc[-1]
    
        bb_position = (latest['close'] - latest['bb_lower']) / (latest['bb_upper'] - latest['bb_lower'])
    
        if signal_type == 'buy':
            if bb_position < 0.05:
                return 20.0
            elif bb_position < 0.15:
                return 16.0
            elif bb_position < 0.25:
                return 12.0
            elif bb_position < 0.4:
                return 8.0
            elif bb_position < 0.6:
                return 4.0
            elif bb_position < 0.75:
                return -4.0
            elif bb_position < 0.85:
                return -8.0
            elif bb_position < 0.95:
                return -12.0
            else:
                return -16.0
    
        else:  # sell
            if bb_position > 0.95:
                return 20.0
            elif bb_position > 0.85:
                return 16.0
            elif bb_position > 0.75:
                return 12.0
            elif bb_position > 0.6:
                return 8.0
            elif bb_position > 0.4:
                return 4.0
            elif bb_position > 0.25:
                return -4.0
            elif bb_position > 0.15:
                return -8.0
            elif bb_position > 0.05:
                return -12.0
            else:
                return -16.0

    def calculate_volume_score(self, data, signal_type):
        """حساب درجة الحجم بتدرج دقيق"""
        latest = data.iloc[-1]
        volume_ratio = latest['vol_ratio']
    
        price_move = latest['close'] - latest['open']
        price_direction = 1 if price_move > 0 else -1 if price_move < 0 else 0
    
        direction_match = (price_direction == 1 and signal_type == 'buy') or \
                         (price_direction == -1 and signal_type == 'sell')
    
        if volume_ratio > 3.5:
            score = 18.0 + (2.0 if direction_match else -4.0)
        elif volume_ratio > 2.5:
            score = 15.0 + (2.0 if direction_match else -3.0)
        elif volume_ratio > 2.0:
            score = 12.0 + (2.0 if direction_match else -2.0)
        elif volume_ratio > 1.5:
            score = 9.0 + (1.0 if direction_match else -1.0)
        elif volume_ratio > 1.2:
            score = 6.0 + (1.0 if direction_match else -1.0)
        elif volume_ratio > 0.9:
            score = 3.0
        elif volume_ratio > 0.7:
            score = 0.0
        elif volume_ratio > 0.5:
            score = -4.0
        elif volume_ratio > 0.3:
            score = -8.0
        else:
            score = -12.0
    
        return max(min(score, 20.0), -20.0)

    def calculate_market_trend_score(self, data, signal_type):
        """حساب درجة اتجاه السوق بتدرج منطقي"""
        latest = data.iloc[-1]
    
        price_vs_ema200 = ((latest['close'] - latest['ema200']) / latest['ema200']) * 100
        price_vs_ema34 = ((latest['close'] - latest['ema34']) / latest['ema34']) * 100
        trend_strength = (price_vs_ema200 * 0.4) + (price_vs_ema34 * 0.6)
    
        if signal_type == 'buy':
            if trend_strength > 8.0:
                return 25.0
            elif trend_strength > 4.0:
                return 20.0
            elif trend_strength > 1.5:
                return 15.0
            elif trend_strength > -1.0:
                return 8.0
            elif trend_strength > -3.0:
                return 2.0
            elif trend_strength > -6.0:
                return -10.0
            else:
                return -20.0
    
        else:  # sell
            if trend_strength < -8.0:
                return 25.0
            elif trend_strength < -4.0:
                return 20.0
            elif trend_strength < -1.5:
                return 15.0
            elif trend_strength < 1.0:
                return 8.0
            elif trend_strength < 3.0:
                return 2.0
            elif trend_strength < 6.0:
                return -10.0
            else:
                return -20.0
    
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
            data['ema200'] = data['close'].ewm(span=200, adjust=False).mean()
            data['ema50'] = data['close'].ewm(span=50, adjust=False).mean()
            data['ema21'] = data['close'].ewm(span=21, adjust=False).mean()
            data['ema9'] = data['close'].ewm(span=9, adjust=False).mean()
        
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

    def get_strength_level(self, strength):
        """الحصول على اسم مستوى القوة"""
        abs_strength = abs(strength)
        if abs_strength >= 80: return "4 🟢 (قوي جداً)"
        elif abs_strength >= 50: return "3 🟡 (قوي)"
        elif abs_strength >= 25: return "2 🔵 (متوسط)"
        else: return "1 ⚪ (ضعيف)"
    
    def bnb_signal_strategy(self, data):
        """استراتيجية توليد الإشارات فقط"""
        if data is None or len(data) < 100:
            return 'hold', 0
        
        latest = data.iloc[-1]
        current_price = latest['close']
        
        buy_strength = self.calculate_signal_strength(data, 'buy')
        sell_strength = self.calculate_signal_strength(data, 'sell')
        
        # إرسال إشعارات لجميع الإشارات (حتى الضعيفة)
        if buy_strength > 10 or sell_strength > 10:
            analysis_msg = self.generate_signal_analysis(data, buy_strength, sell_strength)
            self.send_notification(analysis_msg)
        
        # تحديد الإشارة الرئيسية
        if buy_strength > 0 and buy_strength > sell_strength:
            if buy_strength >= self.BASELINE_BUY_THRESHOLD:
                return 'buy', buy_strength
            else:
                logger.info(f"📊 إشارة شراء قوتها {buy_strength}% (أقل من العتبة: {self.BASELINE_BUY_THRESHOLD}%)")
                return 'weak_buy', buy_strength
                    
        elif sell_strength > 0 and sell_strength > buy_strength:
            if sell_strength >= self.SELL_THRESHOLD:
                return 'sell', sell_strength
            else:
                logger.info(f"📊 إشارة بيع قوتها {sell_strength}% (أقل من العتبة: {self.SELL_THRESHOLD}%)")
                return 'weak_sell', sell_strength
        
        else:
            return 'hold', 0
    
    def generate_signal_analysis(self, data, buy_strength, sell_strength):
        """إنشاء تحليل مفصل للإشارة"""
        latest = data.iloc[-1]
        current_price = latest['close']

        analysis = f"📊 <b>تحليل إشارات BNB</b>\n\n"
        analysis += f"💰 السعر الحالي: ${current_price:.4f}\n"
        analysis += f"📈 قوة إشارة الشراء: {buy_strength}%\n"
        analysis += f"📉 قوة إشارة البيع: {sell_strength}%\n\n"

        # تحديد الإشارة الأقوى
        if abs(buy_strength) > abs(sell_strength):
            stronger_signal = "شراء" if buy_strength > 0 else "بيع"
            stronger_strength = abs(buy_strength)
        else:
            stronger_signal = "شراء" if sell_strength > 0 else "بيع"
            stronger_strength = abs(sell_strength)

        analysis += f"🎯 الإشارة الأقوى: {stronger_signal} ({stronger_strength}%)\n\n"

        analysis += f"📈 <b>التفاصيل الفنية:</b>\n"
        analysis += f"• RSI: {latest['rsi']:.1f}\n"
        analysis += f"• MACD: {latest['macd']:.6f}\n"
        analysis += f"• EMA 34: ${latest['ema34']:.4f}\n"
        analysis += f"• EMA 200: ${latest['ema200']:.4f}\n"
        analysis += f"• موقع بولينجر: {((latest['close'] - latest['bb_lower']) / (latest['bb_upper'] - latest['bb_lower']) * 100):.1f}%\n"
        analysis += f"• نسبة الحجم: {latest['vol_ratio']:.1f}x\n\n"

        analysis += f"⚙️ <b>عتبات الإشارات:</b>\n"
        analysis += f"• شراء: {self.BASELINE_BUY_THRESHOLD}%\n"
        analysis += f"• بيع: {self.SELL_THRESHOLD}%\n\n"

        # توصية
        if buy_strength >= self.BASELINE_BUY_THRESHOLD:
            analysis += f"✅ <b>توصية: إشارة شراء قوية</b>"
        elif sell_strength >= self.SELL_THRESHOLD:
            analysis += f"✅ <b>توصية: إشارة بيع قوية</b>"
        elif buy_strength > 25:
            analysis += f"⚠️ <b>توصية: إشارة شراء متوسطة</b>"
        elif sell_strength > 25:
            analysis += f"⚠️ <b>توصية: إشارة بيع متوسطة</b>"
        else:
            analysis += f"⏸️ <b>توصية: الانتظار</b>"

        return analysis
    
    def send_signal_report(self):
        """إرسال تقرير بالإشارات الحالية"""
        try:
            data = self.get_historical_data()
            if data is None:
                return
            
            buy_strength = self.calculate_signal_strength(data, 'buy')
            sell_strength = self.calculate_signal_strength(data, 'sell')
            latest = data.iloc[-1]
            
            performance = self.signal_analyzer.calculate_signal_performance()
            
            message = f"📊 <b>تقرير إشارات BNB</b>\n\n"
            message += f"💰 السعر الحالي: ${latest['close']:.4f}\n"
            message += f"📈 إشارة الشراء: {buy_strength}%\n"
            message += f"📉 إشارة البيع: {sell_strength}%\n\n"
            
            message += f"📈 <b>إحصائيات الإشارات:</b>\n"
            message += f"• إجمالي الإشارات: {performance['total_signals']}\n"
            message += f"• إشارات الشراء: {performance['buy_signals']}\n"
            message += f"• إشارات البيع: {performance['sell_signals']}\n"
            message += f"• قوة الإشارة المتوسطة: {performance['avg_signal_strength']}%\n\n"
            
            message += f"📊 <b>جودة الإشارات:</b>\n"
            message += f"• إشارات قوية: {performance['strong_signals']}\n"
            message += f"• إشارات متوسطة: {performance['medium_signals']}\n"
            message += f"• إشارات ضعيفة: {performance['weak_signals']}\n\n"
            
            # توصية حالية
            if buy_strength >= self.BASELINE_BUY_THRESHOLD:
                message += f"✅ <b>التوصية الحالية: شراء</b>"
            elif sell_strength >= self.SELL_THRESHOLD:
                message += f"✅ <b>التوصية الحالية: بيع</b>"
            else:
                message += f"⏸️ <b>التوصية الحالية: انتظار</b>"
            
            self.send_notification(message)
            
        except Exception as e:
            error_msg = f"❌ خطأ في إرسال تقرير الإشارات: {e}"
            logger.error(error_msg)
    
    def check_and_send_signals(self):
        """التحقق من الإشارات وإرسالها"""
        try:
            data = self.get_historical_data()
            if data is None:
                return False
            
            signal_type, signal_strength = self.bnb_signal_strategy(data)
            latest = data.iloc[-1]
            current_price = latest['close']
            
            if signal_type in ['buy', 'sell', 'weak_buy', 'weak_sell']:
                # إضافة سجل الإشارة
                analysis_msg = self.generate_signal_analysis(data, 
                    buy_strength if signal_type in ['buy', 'weak_buy'] else 0,
                    sell_strength if signal_type in ['sell', 'weak_sell'] else 0
                )
                
                self.add_signal_record(signal_type, signal_strength, current_price, analysis_msg)
                
                # إرسال إشعار بالإشارة القوية فقط
                if signal_type in ['buy', 'sell']:
                    level = self.get_strength_level(signal_strength)
                    msg = f"🎯 <b>إشارة {signal_type} بمستوى {level}</b>\n\n"
                    msg += f"قوة الإشارة: {signal_strength}%\n"
                    msg += f"السعر: ${current_price:.4f}\n"
                    msg += f"التوقيت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    
                    self.send_notification(msg)
                
                return True
            
            return False
        
        except Exception as e:
            error_msg = f"❌ خطأ في التحقق من الإشارات: {e}"
            logger.error(error_msg)
            return False
    
    def run(self):
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        interval_minutes = 15
        self.send_notification(
            f"📊 بدء تشغيل بوت إشارات BNB\n\n"
            f"سيعمل البوت على فحص السوق كل {interval_minutes} دقيقة\n"
            f"عتبة الشراء الأساسية: {self.BASELINE_BUY_THRESHOLD}%\n"
            f"عتبة البيع: {self.SELL_THRESHOLD}%\n"
            f"وضع التشغيل: إرسال الإشارات فقط"
        )
        
        self.send_signal_report()
        
        report_counter = 0
        
        while True:
            try:
                signal_sent = self.check_and_send_signals()
                
                report_counter += 1
                if signal_sent or report_counter >= 4:
                    self.send_signal_report()
                    report_counter = 0
                
                time.sleep(interval_minutes * 60)
                
            except Exception as e:
                error_msg = f"❌ خطأ غير متوقع في التشغيل: {e}"
                self.send_notification(error_msg)
                logger.error(error_msg)
                time.sleep(300)

if __name__ == "__main__":
    try:
        print("📊 بدء تشغيل بوت إشارات BNB...")
        print("=" * 60)
        
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        print("🌐 خادم الويب يعمل على المنفذ 10000")
        
        bot = BNB_Signal_Bot()
        
        if bot.test_connection():
            print("✅ اختبار الاتصال ناجح!")
            print("🎯 بدء إرسال الإشارات...")
            bot.run()
        
    except Exception as e:
        logger.error(f"فشل تشغيل البوت: {e}")
        print(f"❌ فشل تشغيل البوت: {e}")
