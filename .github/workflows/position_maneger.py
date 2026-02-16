"""
position_manager.py - 仮想トレード・ポジション管理モジュール

主な機能:
1. ポジションの追加・更新・削除
2. 分割決済（TP1: 50%, TP2: 100%）
3. 損益計算
4. positions.json および trade_results.csv への永続化
"""
import json
import csv
import logging
from datetime import datetime
from typing import Dict, Optional, List
import os
from config import POSITION_MANAGEMENT

logger = logging.getLogger(__name__)


class PositionManager:
    """ポジション管理クラス"""
    
    def __init__(self):
        self.positions_file = POSITION_MANAGEMENT['positions_file']
        self.results_file = POSITION_MANAGEMENT['trade_results_file']
        self.positions = self.load_positions()
    
    def load_positions(self) -> Dict:
        """positions.jsonからポジション情報を読み込み"""
        if not os.path.exists(self.positions_file):
            logger.info(f"ポジションファイルが存在しません。新規作成: {self.positions_file}")
            return {}
        
        try:
            with open(self.positions_file, 'r', encoding='utf-8') as f:
                positions = json.load(f)
            logger.info(f"ポジション読み込み完了: {len(positions)}件")
            return positions
        except Exception as e:
            logger.error(f"ポジション読み込みエラー: {str(e)}")
            return {}
    
    def save_positions(self):
        """positions.jsonに保存"""
        try:
            with open(self.positions_file, 'w', encoding='utf-8') as f:
                json.dump(self.positions, f, indent=2, ensure_ascii=False)
            logger.info(f"ポジション保存完了: {len(self.positions)}件")
        except Exception as e:
            logger.error(f"ポジション保存エラー: {str(e)}")
    
    def has_position(self, ticker: str) -> bool:
        """指定銘柄のポジションが存在するか確認"""
        return ticker in self.positions
    
    def add_position(self, ticker: str, side: str, entry_price: float,
                    atr: float, stop_loss: float, tp1: float, tp2: float,
                    params: Dict) -> None:
        """
        新規ポジションを追加
        
        Args:
            ticker: 銘柄コード
            side: 'LONG' または 'SHORT'
            entry_price: エントリー価格
            atr: ATR値（固定）
            stop_loss: 損切価格
            tp1: 第一利確目標
            tp2: 第二利確目標
            params: パラメータ辞書
        """
        position = {
            'ticker': ticker,
            'side': side,
            'entry_price': entry_price,
            'atr': atr,
            'stop_loss': stop_loss,
            'tp1': tp1,
            'tp2': tp2,
            'tp1_hit': False,
            'remaining_ratio': 1.0,
            'entry_time': datetime.now().isoformat(),
            'params': params,
            'divergence_warned': False,  # Version 9.0: 逆行ダイバージェンス警告フラグ
            'trailing_mode': False       # Ver 10.3: トレーリングモードフラグ
        }
        
        self.positions[ticker] = position
        self.save_positions()
        
        logger.info(
            f"ポジション追加: {ticker} {side} "
            f"Entry={entry_price:.1f}, SL={stop_loss:.1f}, "
            f"TP1={tp1:.1f}, TP2={tp2:.1f}"
        )
    
    def update_stop_loss(self, ticker: str, new_stop_loss: float) -> None:
        """損切価格を更新（建値への移動）"""
        if ticker in self.positions:
            old_sl = self.positions[ticker]['stop_loss']
            self.positions[ticker]['stop_loss'] = new_stop_loss
            self.save_positions()
            logger.info(f"{ticker}: 損切を建値へ移動 {old_sl:.1f} → {new_stop_loss:.1f}")
    
    def set_trailing_mode(self, ticker: str, enabled: bool = True) -> None:
        """トレーリングモードを設定（Ver 10.3）"""
        if ticker in self.positions:
            self.positions[ticker]['trailing_mode'] = enabled
            self.save_positions()
            logger.info(f"{ticker}: トレーリングモード開始")

    def execute_tp1(self, ticker: str, tp1_price: float) -> float:
        """
        TP1で50%利確
        
        Returns:
            TP1時の損益率（%）
        """
        if ticker not in self.positions:
            return 0.0
        
        position = self.positions[ticker]
        entry_price = position['entry_price']
        side = position['side']
        
        # 損益計算
        if side == 'LONG':
            profit_pct = ((tp1_price / entry_price) - 1) * 100
        else:  # SHORT
            profit_pct = (1 - (tp1_price / entry_price)) * 100
        
        # TP1フラグを立てる
        position['tp1_hit'] = True
        position['tp1_price'] = tp1_price
        position['tp1_profit'] = profit_pct
        position['remaining_ratio'] = 0.5  # 残り50%
        
        # 建値へ移動
        if POSITION_MANAGEMENT['breakeven_enabled']:
            position['stop_loss'] = entry_price
        
        self.save_positions()
        
        logger.info(f"{ticker}: TP1達成 価格={tp1_price:.1f}, 損益={profit_pct:.2f}%")
        
        return profit_pct
    
    def close_position(self, ticker: str, exit_price: float, reason: str) -> Dict:
        """
        ポジションをクローズ
        
        Args:
            ticker: 銘柄コード
            exit_price: 決済価格
            reason: 決済理由（'TP2', 'SL', '15:00強制決済', 'TRAILING_EXIT'）
        
        Returns:
            取引結果の辞書
        """
        if ticker not in self.positions:
            return {}
        
        position = self.positions[ticker]
        entry_price = position['entry_price']
        side = position['side']
        tp1_hit = position['tp1_hit']
        remaining_ratio = position.get('remaining_ratio', 1.0)
        
        # 最終決済の損益計算
        if side == 'LONG':
            final_profit_pct = ((exit_price / entry_price) - 1) * 100
        else:  # SHORT
            final_profit_pct = (1 - (exit_price / entry_price)) * 100
        
        # 総合損益計算
        if tp1_hit:
            # TP1で既に50%利確済み
            tp1_profit = position['tp1_profit']
            # 残りの割合(0.5)で計算
            total_profit = (tp1_profit * 0.5) + (final_profit_pct * remaining_ratio)
        else:
            # 全量決済（もしくはTP2到達時のADX<30での全決済）
            total_profit = final_profit_pct
        
        # 取引結果を記録
        result = {
            'ticker': ticker,
            'side': side,
            'entry_price': entry_price,
            'entry_time': position['entry_time'],
            'exit_price': exit_price,
            'exit_time': datetime.now().isoformat(),
            'exit_reason': reason,
            'tp1_hit': tp1_hit,
            'tp1_profit': position.get('tp1_profit', 0.0) if tp1_hit else 0.0,
            'final_profit': final_profit_pct,
            'total_profit': total_profit
        }
        
        # CSVに保存
        self.save_trade_result(result)
        
        # ポジション削除
        del self.positions[ticker]
        self.save_positions()
        
        logger.info(
            f"{ticker}: ポジションクローズ {reason} "
            f"総合損益={total_profit:.2f}% "
            f"(TP1:{tp1_hit}, 最終:{final_profit_pct:.2f}%)"
        )
        
        return result
    
    def save_trade_result(self, result: Dict) -> None:
        """取引結果をCSVに保存"""
        file_exists = os.path.exists(self.results_file)
        
        try:
            with open(self.results_file, 'a', newline='', encoding='utf-8') as f:
                fieldnames = [
                    'ticker', 'side', 'entry_price', 'entry_time',
                    'exit_price', 'exit_time', 'exit_reason',
                    'tp1_hit', 'tp1_profit', 'final_profit', 'total_profit'
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                
                if not file_exists:
                    writer.writeheader()
                
                writer.writerow(result)
            
            logger.info(f"取引結果保存: {result['ticker']} {result['total_profit']:.2f}%")
        
        except Exception as e:
            logger.error(f"取引結果保存エラー: {str(e)}")
    
    def get_position(self, ticker: str) -> Optional[Dict]:
        """指定銘柄のポジション情報を取得"""
        return self.positions.get(ticker)
    
    def get_all_positions(self) -> Dict:
        """全ポジションを取得"""
        return self.positions
    
    def force_close_all(self, current_prices: Dict[str, float]) -> List[Dict]:
        """
        全ポジションを強制決済（15:00用）
        
        Args:
            current_prices: {ticker: 現在価格} の辞書
        
        Returns:
            決済結果のリスト
        """
        results = []
        tickers_to_close = list(self.positions.keys())
        
        for ticker in tickers_to_close:
            if ticker in current_prices:
                exit_price = current_prices[ticker]
                result = self.close_position(ticker, exit_price, '15:00強制決済')
                if result:
                    results.append(result)
        
        logger.info(f"全ポジション強制決済完了: {len(results)}件")
        return results
