\"\"\"
position_manager.py - Version 15.0 仮想トレード・ポジション管理
主な機能:
1. 買値から-5%の固定損切り
2. 最高値更新から-5%のトレーリングストップ
3. 長期・短期ロジックの判別保存
\"\"\"
import json
import csv
import logging
from datetime import datetime
from typing import Dict, Optional, List
import os
from config import POSITION_MANAGEMENT

logger = logging.getLogger(__name__)

class PositionManager:
    def __init__(self):
        self.positions_file = POSITION_MANAGEMENT['positions_file']
        self.results_file = POSITION_MANAGEMENT['trade_results_file']
        self.positions = self.load_positions()
    
    def load_positions(self) -> Dict:
        if not os.path.exists(self.positions_file): return {}
        try:
            with open(self.positions_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception: return {}
    
    def save_positions(self):
        try:
            with open(self.positions_file, 'w', encoding='utf-8') as f:
                json.dump(self.positions, f, indent=2, ensure_ascii=False)
        except Exception as e: logger.error(f\"Save positions error: {str(e)}\")
    
    def has_position(self, ticker: str) -> bool:
        return ticker in self.positions
    
    def add_position(self, ticker: str, side: str, entry_price: float, params: Dict) -> None:
        # 固定損切り: -5%
        fixed_sl = entry_price * 0.95 if side == 'LONG' else entry_price * 1.05
        
        position = {
            'ticker': ticker,
            'side': side,
            'entry_price': entry_price,
            'fixed_sl': fixed_sl,
            'highest_price': entry_price if side == 'LONG' else entry_price,
            'lowest_price': entry_price if side == 'SHORT' else entry_price,
            'trailing_sl': fixed_sl,
            'entry_time': datetime.now().isoformat(),
            'logic_type': params.get('logic_type', 'Unknown'),
            'params': params
        }
        self.positions[ticker] = position
        self.save_positions()
        logger.info(f\"Position Added: {ticker} ({position['logic_type']}) @ {entry_price}\")

    def update_price(self, ticker: str, current_price: float) -> Optional[str]:
        \"\"\"価格更新と損切り判定\"\"\"
        if ticker not in self.positions: return None
        pos = self.positions[ticker]
        side = pos['side']
        
        # 最高値/最安値の更新
        if side == 'LONG':
            if current_price > pos['highest_price']:
                pos['highest_price'] = current_price
                # トレーリングストップ引き上げ: 最高値から-5%
                new_tsl = current_price * 0.95
                if new_tsl > pos['trailing_sl']:
                    pos['trailing_sl'] = new_tsl
            
            # 損切り判定
            if current_price <= pos['trailing_sl']: return 'TRAILING_SL'
            if current_price <= pos['fixed_sl']: return 'FIXED_SL'
            
        else: # SHORT
            if current_price < pos['lowest_price']:
                pos['lowest_price'] = current_price
                # トレーリングストップ引き下げ: 最安値から+5%
                new_tsl = current_price * 1.05
                if new_tsl < pos['trailing_sl']:
                    pos['trailing_sl'] = new_tsl
            
            # 損切り判定
            if current_price >= pos['trailing_sl']: return 'TRAILING_SL'
            if current_price >= pos['fixed_sl']: return 'FIXED_SL'
            
        self.save_positions()
        return None

    def close_position(self, ticker: str, exit_price: float, reason: str) -> Dict:
        if ticker not in self.positions: return {}
        pos = self.positions[ticker]
        entry_price = pos['entry_price']
        side = pos['side']
        
        profit_pct = ((exit_price / entry_price - 1) * 100) if side == 'LONG' else ((1 - exit_price / entry_price) * 100)
        
        result = {
            'ticker': ticker,
            'side': side,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'exit_time': datetime.now().isoformat(),
            'exit_reason': reason,
            'profit_pct': profit_pct,
            'logic_type': pos['logic_type']
        }
        
        # CSV保存
        self.save_trade_result(result)
        del self.positions[ticker]
        self.save_positions()
        return result

    def save_trade_result(self, result: Dict) -> None:
        file_exists = os.path.exists(self.results_file)
        try:
            with open(self.results_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=result.keys())
                if not file_exists: writer.writeheader()
                writer.writerow(result)
        except Exception: pass

    def get_position(self, ticker: str) -> Optional[Dict]:
        return self.positions.get(ticker)
    
    def get_all_positions(self) -> Dict:
        return self.positions

    def force_close_all(self, current_prices: Dict[str, float], reason: str = 'FORCE_CLOSE') -> List[Dict]:
        results = []
        for ticker in list(self.positions.keys()):
            if ticker in current_prices:
                results.append(self.close_position(ticker, current_prices[ticker], reason))
        return results
