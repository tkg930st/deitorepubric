"""
position_manager.py - Version 15.15 仮想トレード・ポジション管理
変更点:
- AM/PM 連携用フラグ (pm_active) の日付ベース管理
- TP2 トレーリングストップ (動的SL引き上げ) 実装
- タイムスタンプの JST (Asia/Tokyo) 統一
- 取引結果出力カラムの厳密な固定
"""
import json
import csv
import logging
from datetime import datetime
from typing import Dict, Optional, List, Any
import os
import pytz
from config import POSITION_MANAGEMENT, SLIPPAGE

logger = logging.getLogger(__name__)
tz = pytz.timezone('Asia/Tokyo')

class PositionManager:
    def __init__(self):
        self.positions_file = POSITION_MANAGEMENT['positions_file']
        self.results_file = POSITION_MANAGEMENT['trade_results_file']
        self.pm_active_date = None # True/False ではなく日付文字列を保持
        self.positions = {}
        self.load_from_file()
    
    def load_from_file(self) -> None:
        if not os.path.exists(self.positions_file):
            self.positions = {}
            self.pm_active_date = None
            return
        try:
            with open(self.positions_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if 'positions' in data:
                    self.positions = data['positions']
                    # pm_active キーから日付、または True (互換用) を取得
                    val = data.get('pm_active')
                    self.pm_active_date = str(val) if val else None
                else:
                    self.positions = data
                    self.pm_active_date = None
        except Exception:
            self.positions = {}
            self.pm_active_date = None
    
    def save_positions(self):
        try:
            data = {
                'positions': self.positions,
                'pm_active': self.pm_active_date,
                'last_update': datetime.now(tz).isoformat()
            }
            with open(self.positions_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e: logger.error(f"Save positions error: {str(e)}")

    def set_pm_active(self, active_val: Any = None):
        """日付文字列または None を設定"""
        self.pm_active_date = active_val
        self.save_positions()

    def get_pm_active_date(self) -> Optional[str]:
        self.load_from_file()
        return self.pm_active_date
    
    def get_position(self, ticker: str) -> Optional[Dict]:
        """
        指定された銘柄のポジション情報を取得
        """
        return self.positions.get(ticker)
    
    def has_position(self, ticker: str) -> bool:
        return ticker in self.positions
    
    def add_position(self, ticker: str, side: str, entry_price: float, detail: Dict, sl: Optional[float] = None, tp1: Optional[float] = None) -> None:
        atr = detail.get('atr', entry_price * 0.02)
        logic_side = side.lower()
        params = detail['params'][logic_side]
        
        # 外部から指定があればそれを使用、なければ従来通り計算
        if sl is None:
            sl_dist = atr * params['sl_mul']
            sl = entry_price - sl_dist if side == 'LONG' else entry_price + sl_dist
        
        if tp1 is None:
            tp1_dist = atr * POSITION_MANAGEMENT.get('tp1_multiplier', 1.5)
            tp1 = entry_price + tp1_dist if side == 'LONG' else entry_price - tp1_dist
        
        position = {
            'ticker': ticker,
            'side': side,
            'entry_price': entry_price,
            'entry_atr': atr,
            'fixed_sl': sl,
            'tp1': tp1,
            'tp1_hit': False,
            'tp1_profit': 0.0,
            'highest_price': entry_price,
            'lowest_price': entry_price,
            'trailing_sl': sl,
            'entry_time': datetime.now(tz).isoformat(),
            'logic_type': detail.get('logic_type', 'Unknown'),
            'params': params
        }
        self.positions[ticker] = position
        self.save_positions()
        logger.info(f"Position Added: {ticker} ({side}) SL={sl:.1f}, TP1={tp1:.1f}")

    def update_price(self, ticker: str, current_price: float) -> Optional[str]:
        """
        価格更新とイベント判定
        戻り値: 'TP1_HIT', 'STOP_LOSS', または None
        """
        if ticker not in self.positions: return None
        pos = self.positions[ticker]
        side = pos['side']
        atr = pos['entry_atr']
        trailing_mul = POSITION_MANAGEMENT.get('trailing_atr_multiplier', 1.0)
        
        event = None
        
        # 1. 最高値/最安値の更新
        if side == 'LONG':
            if current_price > pos['highest_price']:
                pos['highest_price'] = current_price
                # TP1ヒット後は高値に合わせてSLを引き上げる (トレーリング)
                if pos['tp1_hit']:
                    new_sl = current_price - (atr * trailing_mul)
                    pos['trailing_sl'] = max(pos['trailing_sl'], new_sl)
        else: # SHORT
            if current_price < pos['lowest_price']:
                pos['lowest_price'] = current_price
                # TP1ヒット後は安値に合わせてSLを引き下げる (トレーリング)
                if pos['tp1_hit']:
                    new_sl = current_price + (atr * trailing_mul)
                    pos['trailing_sl'] = min(pos['trailing_sl'], new_sl)

        # 2. TP1達成チェック
        if not pos['tp1_hit']:
            if (side == 'LONG' and current_price >= pos['tp1']) or \
               (side == 'SHORT' and current_price <= pos['tp1']):
                pos['tp1_hit'] = True
                # TP1到達時の利益を記録 (スリッページ適用)
                if side == 'LONG':
                    pos['tp1_profit'] = ((current_price / pos['entry_price']) - 1 - SLIPPAGE) * 100
                else:
                    pos['tp1_profit'] = (1 - (current_price / pos['entry_price']) - SLIPPAGE) * 100
                # リスク低減 (建値付近へ)
                if side == 'LONG':
                    pos['trailing_sl'] = max(pos['trailing_sl'], pos['entry_price'] - (atr * 0.2))
                else:
                    pos['trailing_sl'] = min(pos['trailing_sl'], pos['entry_price'] + (atr * 0.2))
                event = 'TP1_HIT'

        # 3. 決済判定 (STOP_LOSS または トレーリング決済)
        if side == 'LONG':
            if current_price <= pos['trailing_sl']: event = 'STOP_LOSS'
        else: # SHORT
            if current_price >= pos['trailing_sl']: event = 'STOP_LOSS'
            
        self.save_positions()
        return event

    def close_position(self, ticker: str, exit_price: float, reason: str) -> Dict:
        if ticker not in self.positions: return {}
        pos = self.positions[ticker]
        entry_price = pos['entry_price']
        side = pos['side']
        # スリッページ適用 (往復0.3% = SLIPPAGE) - バックテストと同期
        remaining_profit_pct = ((exit_price / entry_price - 1 - SLIPPAGE) * 100) if side == 'LONG' else ((1 - exit_price / entry_price - SLIPPAGE) * 100)

        # TP1到達済みの場合: 50%×TP1損益 + 50%×残ポジション損益 でブレンド計算
        tp1_exit_ratio = POSITION_MANAGEMENT.get('tp1_exit_ratio', 0.5)
        tp1_hit = pos.get('tp1_hit', False)
        tp1_profit = pos.get('tp1_profit', 0.0)
        if tp1_hit:
            total_profit = (tp1_exit_ratio * tp1_profit) + ((1 - tp1_exit_ratio) * remaining_profit_pct)
        else:
            total_profit = remaining_profit_pct

        # 通知および解析に必要なデータをすべて保持
        result = {
            'ticker': ticker,
            'side': side,
            'entry_price': entry_price,
            'entry_time': pos['entry_time'],
            'exit_price': exit_price,
            'exit_time': datetime.now(tz).isoformat(),
            'exit_reason': reason,
            'tp1_hit': tp1_hit,
            'tp1_profit': tp1_profit,
            'final_profit': remaining_profit_pct,
            'total_profit': total_profit,
            'profit_pct': total_profit, # monitor.py の通知用 (互換性)
            'logic_type': pos.get('logic_type', 'Unknown') # monitor.py の通知用
        }
        
        self.save_trade_result(result)
        del self.positions[ticker]
        self.save_positions()
        return result

    def save_trade_result(self, result: Dict) -> None:
        # 保存するカラムを厳密に定義
        fieldnames = [
            'ticker', 'side', 'entry_price', 'entry_time', 'exit_price', 
            'exit_time', 'exit_reason', 'tp1_hit', 'tp1_profit', 'final_profit', 
            'total_profit', 'logic_type'
        ]
        file_exists = os.path.exists(self.results_file)
        try:
            with open(self.results_file, 'a', newline='', encoding='utf-8') as f:
                # extrasaction='ignore' により定義外のキーを無視
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                if not file_exists: writer.writeheader()
                writer.writerow(result)
        except Exception as e:
            logger.error(f"Save result error: {e}")

    def force_close_all(self, current_prices: Dict[str, float], reason: str = 'FORCE_CLOSE') -> List[Dict]:
        results = []
        for ticker in list(self.positions.keys()):
            if ticker in current_prices:
                results.append(self.close_position(ticker, current_prices[ticker], reason))
        return results
