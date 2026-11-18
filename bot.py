"""
Live / Forward Testing Trading Bot for XAUUSD (M5 Timeframe).
Strictly replicates the logic, parameters, risk management, and state machine architecture
of event_driven_backtester.py using the MetaTrader5 Python library.

Cycle: Spike -> Retracement & Boundary Establishment -> Trading Range (State 0 -> 1 -> 2)
Execution: Touch Limit (Market order on boundary touch)
Exit: Range Midline Take-Profit & Range Breakout Invalidation
"""

import argparse
import collections
import logging
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import shutil
import numpy as np
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[ERROR] MetaTrader5 package is not installed. Run: pip install MetaTrader5")
    sys.exit(1)


# =====================================================================
# CONFIGURATION & HYPERPARAMETERS (EXACT MATCH TO BACKTESTER)
# =====================================================================

TIMEFRAME_NAME = "M5"
TIMEFRAME_SECONDS = 300
ATR_PERIOD = 14
ROLLING_WINDOW_SIZE = 300

# State 0 (Spike) Parameters
MIN_SPIKE_BARS = 3
MAX_SPIKE_BARS = 8
MIN_SPIKE_ATR = 2.5
MIN_EFFICIENCY = 0.55
MIN_DIRECTIONAL_CONSISTENCY = 0.60

# State 1 (Retracement / Boundary Establishment) Parameters
PIVOT_LEFT = 2
PIVOT_RIGHT = 2
MIN_RETRACE_RATIO = 0.30       # Minimum 30% pullback of the spike move
MAX_RETRACEMENT_BARS = 60      # Max bars to wait for valid pullback & pivot
PIVOT_BOUNCE_ATR = 0.15        # Minimum bounce in ATR to confirm structural pivot

# State 2 (Active Range Trading) Parameters
EXPANSION_BUFFER_ATR = 0.20    # Max expansion buffer for false breakout wicks rejecting inside
BREAKOUT_BUFFER_ATR = 0.20     # Buffer beyond boundaries required for candle CLOSE breakout
MAX_HOLD_BARS = 72             # Maximum bars to hold position (6 hours on M5)
MAX_RANGE_LIFETIME_BARS = 288  # Maximum lifetime of a trading range (24 hours on M5)
DEFAULT_SL_MODE = "range_width"# Stop distance equals the full Trading Range width
SL_ATR_MULTIPLIER = 0.20       # Configurable ATR multiplier for Stop Loss in 'atr' mode
SL_WIDTH_MULTIPLIER = 0.20      # Configurable range width multiplier for Stop Loss in 'range_width' mode
RR_RATIO = 1.50                # Risk:Reward fallback ratio
TP_ADJUSTMENT_PERCENT = 3.0    # Move TP 3% of entry-to-midline distance toward entry
DEFAULT_ENTRY_STYLE = "touch"  # Flexible ATR-based Trading Range touch zone
DEFAULT_EXIT_STYLE = "midline" # Close 100% of position at range midline
DEFAULT_MIN_RANGE_WIDTH = 1.2   # Minimum range width in ATR to trade STATE 2
DEFAULT_CHECKIN_INTERVAL = 30.0 # Operational status check-in / heartbeat interval in seconds

# Portfolio & Risk Configuration
DEFAULT_SYMBOL = "XAUUSD"
DEFAULT_MAGIC = 10101
DEFAULT_RISK_MODE = "equity_pct"
DEFAULT_RISK_PCT = 2.0         # Default 2.0% equity risk matching backtester
RISK_PER_TRADE_USD = 2.0       # Fallback fixed USD risk per trade
LOTS_PER_100_BALANCE = 0.02    # Dynamic volume scale: 0.02 lots per $100 of account balance
MIN_VOLUME = 0.01              # Minimum position volume safeguard
MAX_VOLUME = 100.0             # Maximum position volume safeguard
COMMISSION_PER_TRADE = 0.05
SLIPPAGE = 0.10
CONTRACT_SIZE = 100.0          # Standard 100 oz contract for XAUUSD CFD
LEVERAGE = 100.0
VOLUME_STEP = 0.01


# Embedded MQL5 Chart Overlay Indicator Source
MQL5_OVERLAY_INDICATOR_SOURCE = r"""//+------------------------------------------------------------------+
//|                                              ApexLiveOverlay.mq5 |
//|           Real-Time Spike & Trading Range Overlay from Python Bot|
//+------------------------------------------------------------------+
#property copyright "APEX-XAU Quantitative Bot"
#property version   "1.00"
#property indicator_chart_window
#property indicator_plots 0

input string InpFileName = "apex_live_overlay.txt"; // Overlay Sync File (in MQL5/Files)
input int    InpRefreshSeconds = 1;                 // Refresh Interval (Seconds)

string PREFIX = "APEX_LIVE_";

//+------------------------------------------------------------------+
//| Custom indicator initialization function                         |
//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(InpRefreshSeconds);
   UpdateChartObjects();
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Custom indicator deinitialization function                       |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   ObjectsDeleteAll(0, PREFIX);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| Timer function                                                   |
//+------------------------------------------------------------------+
void OnTimer()
  {
   UpdateChartObjects();
  }

//+------------------------------------------------------------------+
//| Custom indicator iteration function                              |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {
   return(rates_total);
  }

//+------------------------------------------------------------------+
//| Reads sync file from MQL5/Files and draws Spike & Trading Range  |
//+------------------------------------------------------------------+
void UpdateChartObjects()
  {
   if(!FileIsExist(InpFileName))
      return;

   int handle = FileOpen(InpFileName, FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
      return;

   while(!FileIsEnding(handle))
     {
      string line = FileReadString(handle);
      if(StringLen(line) == 0)
         continue;

      string parts[];
      int count = StringSplit(line, '|', parts);
      if(count < 2)
         continue;

      string cmd = parts[0];

      // 1. SPIKE|event_id|direction|start_ts|end_ts|peak|trough|start_price|end_price
      if(cmd == "SPIKE" && count >= 9)
        {
         string evt = parts[1];
         string dir = parts[2];
         datetime t1 = (datetime)StringToInteger(parts[3]);
         datetime t2 = (datetime)StringToInteger(parts[4]);
         double peak = StringToDouble(parts[5]);
         double trough = StringToDouble(parts[6]);
         double p1 = StringToDouble(parts[7]);
         double p2 = StringToDouble(parts[8]);

         color spk_col = (dir == "BULLISH") ? clrAqua : clrGold;
         string box_name = PREFIX + evt + "_SPIKE_BOX";
         string line_name = PREFIX + evt + "_SPIKE_LINE";
         string txt_name = PREFIX + evt + "_SPIKE_TXT";

         if(ObjectFind(0, box_name) < 0)
            ObjectCreate(0, box_name, OBJ_RECTANGLE, 0, t1, peak, t2, trough);
         ObjectMove(0, box_name, 0, t1, peak);
         ObjectMove(0, box_name, 1, t2, trough);
         ObjectSetInteger(0, box_name, OBJPROP_COLOR, spk_col);
         ObjectSetInteger(0, box_name, OBJPROP_STYLE, STYLE_DOT);
         ObjectSetInteger(0, box_name, OBJPROP_WIDTH, 1);
         ObjectSetInteger(0, box_name, OBJPROP_FILL, false);
         ObjectSetInteger(0, box_name, OBJPROP_BACK, true);

         if(ObjectFind(0, line_name) < 0)
            ObjectCreate(0, line_name, OBJ_TREND, 0, t1, p1, t2, p2);
         ObjectMove(0, line_name, 0, t1, p1);
         ObjectMove(0, line_name, 1, t2, p2);
         ObjectSetInteger(0, line_name, OBJPROP_COLOR, spk_col);
         ObjectSetInteger(0, line_name, OBJPROP_WIDTH, 2);
         ObjectSetInteger(0, line_name, OBJPROP_RAY_RIGHT, false);

         if(ObjectFind(0, txt_name) < 0)
            ObjectCreate(0, txt_name, OBJ_TEXT, 0, t1, (dir == "BULLISH") ? peak : trough);
         ObjectMove(0, txt_name, 0, t1, (dir == "BULLISH") ? peak : trough);
         ObjectSetString(0, txt_name, OBJPROP_TEXT, "  Spike " + dir + " (" + evt + ")");
         ObjectSetInteger(0, txt_name, OBJPROP_COLOR, spk_col);
         ObjectSetInteger(0, txt_name, OBJPROP_FONTSIZE, 8);
        }

      // 2. RANGE|event_id|det_ts|end_ts|upper|lower|midline|tol
      else if(cmd == "RANGE" && count >= 8)
        {
         string evt = parts[1];
         datetime t_det = (datetime)StringToInteger(parts[2]);
         datetime t_end = (datetime)StringToInteger(parts[3]);
         double upper = StringToDouble(parts[4]);
         double lower = StringToDouble(parts[5]);
         double mid   = StringToDouble(parts[6]);
         double tol   = StringToDouble(parts[7]);

         string up_name  = PREFIX + evt + "_UPPER";
         string dn_name  = PREFIX + evt + "_LOWER";
         string mid_name = PREFIX + evt + "_MID";
         string sz_name  = PREFIX + evt + "_SELL_ZONE";
         string bz_name  = PREFIX + evt + "_BUY_ZONE";

         // Upper Line
         if(ObjectFind(0, up_name) < 0)
            ObjectCreate(0, up_name, OBJ_TREND, 0, t_det, upper, t_end, upper);
         ObjectMove(0, up_name, 0, t_det, upper);
         ObjectMove(0, up_name, 1, t_end, upper);
         ObjectSetInteger(0, up_name, OBJPROP_COLOR, clrCrimson);
         ObjectSetInteger(0, up_name, OBJPROP_STYLE, STYLE_DASH);
         ObjectSetInteger(0, up_name, OBJPROP_WIDTH, 2);
         ObjectSetInteger(0, up_name, OBJPROP_RAY_RIGHT, false);

         // Lower Line
         if(ObjectFind(0, dn_name) < 0)
            ObjectCreate(0, dn_name, OBJ_TREND, 0, t_det, lower, t_end, lower);
         ObjectMove(0, dn_name, 0, t_det, lower);
         ObjectMove(0, dn_name, 1, t_end, lower);
         ObjectSetInteger(0, dn_name, OBJPROP_COLOR, clrSpringGreen);
         ObjectSetInteger(0, dn_name, OBJPROP_STYLE, STYLE_DASH);
         ObjectSetInteger(0, dn_name, OBJPROP_WIDTH, 2);
         ObjectSetInteger(0, dn_name, OBJPROP_RAY_RIGHT, false);

         // Midline
         if(ObjectFind(0, mid_name) < 0)
            ObjectCreate(0, mid_name, OBJ_TREND, 0, t_det, mid, t_end, mid);
         ObjectMove(0, mid_name, 0, t_det, mid);
         ObjectMove(0, mid_name, 1, t_end, mid);
         ObjectSetInteger(0, mid_name, OBJPROP_COLOR, clrDeepSkyBlue);
         ObjectSetInteger(0, mid_name, OBJPROP_STYLE, STYLE_DOT);
         ObjectSetInteger(0, mid_name, OBJPROP_WIDTH, 1);
         ObjectSetInteger(0, mid_name, OBJPROP_RAY_RIGHT, false);

         // Touch Zones (Shaded Rectangles)
         if(tol > 0)
           {
            if(ObjectFind(0, sz_name) < 0)
               ObjectCreate(0, sz_name, OBJ_RECTANGLE, 0, t_det, upper, t_end, upper - tol);
            ObjectMove(0, sz_name, 0, t_det, upper);
            ObjectMove(0, sz_name, 1, t_end, upper - tol);
            ObjectSetInteger(0, sz_name, OBJPROP_COLOR, C'90,20,30');
            ObjectSetInteger(0, sz_name, OBJPROP_FILL, true);
            ObjectSetInteger(0, sz_name, OBJPROP_BACK, true);

            if(ObjectFind(0, bz_name) < 0)
               ObjectCreate(0, bz_name, OBJ_RECTANGLE, 0, t_det, lower + tol, t_end, lower);
            ObjectMove(0, bz_name, 0, t_det, lower + tol);
            ObjectMove(0, bz_name, 1, t_end, lower);
            ObjectSetInteger(0, bz_name, OBJPROP_COLOR, C'15,80,45');
            ObjectSetInteger(0, bz_name, OBJPROP_FILL, true);
            ObjectSetInteger(0, bz_name, OBJPROP_BACK, true);
           }
        }
     }

   FileClose(handle);
   ChartRedraw(0);
  }
//+------------------------------------------------------------------+
"""

# MT5 Return Code Descriptions
RETCODE_DESCRIPTIONS = {
    10004: "Requote",
    10006: "Request rejected",
    10007: "Request canceled by trader",
    10008: "Order placed",
    10009: "Request completed (TRADE_RETCODE_DONE)",
    10010: "Only part of request was completed",
    10011: "Request processing error",
    10012: "Request canceled by timeout",
    10013: "Invalid request",
    10014: "Invalid volume in request",
    10015: "Invalid price in request",
    10016: "Invalid stops in request",
    10017: "Trade is disabled",
    10018: "Market is closed",
    10019: "There is not enough money to complete request",
    10020: "Prices changed",
    10021: "There are no quotes to process request",
    10022: "Invalid order expiration date in request",
    10023: "Order state changed",
    10024: "Too frequent requests",
    10025: "No changes in request",
    10026: "Autotrading disabled by server",
    10027: "Autotrading disabled by client terminal",
    10028: "Request locked for processing",
    10029: "Order or position frozen",
    10030: "Invalid order filling type",
    10031: "No connection with trade server",
    10032: "Operation allowed only for live accounts",
    10033: "Number of pending orders reached limit",
    10034: "Volume of orders and positions reached limit",
}


# =====================================================================
# LOGGING SYSTEM SETUP
# =====================================================================

def setup_bot_logger(name: str = "LiveEventDrivenBot", log_dir: Optional[Path] = None) -> logging.Logger:
    """
    Sets up a timestamped logging system replicating the backtester output format.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_dir is None:
        log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_dir / "live_bot.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

logger = setup_bot_logger()


# =====================================================================
# DATA STRUCTURES
# =====================================================================

@dataclass
class Candle:
    time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread: float
    index: int


@dataclass
class LivePosition:
    ticket: int
    trade_id: int
    symbol: str
    side: str  # "LONG" or "SHORT"
    entry_time: pd.Timestamp
    entry_price: float
    sl: float
    tp: float
    size: float
    risk_points: float
    entry_idx: int
    event_id: str
    midline: float = 0.0
    opposite_boundary: float = 0.0
    upper_boundary: float = 0.0
    lower_boundary: float = 0.0
    range_detection_time: Optional[pd.Timestamp] = None
    equity_at_entry: float = 0.0
    risk_usd: float = 0.0
    margin_required: float = 0.0


# =====================================================================
# PERFORMANT ROLLING WINDOW
# =====================================================================

class RollingWindow:
    """
    Maintains a fixed-size bounded buffer of recent M5 candles.
    Provides fast vector calculations for ATR, pivots, and linear regression.
    """

    def __init__(self, maxlen: int = ROLLING_WINDOW_SIZE):
        self.maxlen = maxlen
        self.candles: Deque[Candle] = collections.deque(maxlen=maxlen)

    def append(self, candle: Candle):
        self.candles.append(candle)

    def __len__(self):
        return len(self.candles)

    def get_arrays(self, length: Optional[int] = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Returns (opens, highs, lows, closes) as contiguous numpy arrays.
        """
        window = list(self.candles)[-length:] if length else list(self.candles)
        opens = np.fromiter((c.open for c in window), dtype=float, count=len(window))
        highs = np.fromiter((c.high for c in window), dtype=float, count=len(window))
        lows = np.fromiter((c.low for c in window), dtype=float, count=len(window))
        closes = np.fromiter((c.close for c in window), dtype=float, count=len(window))
        return opens, highs, lows, closes

    def calculate_atr(self, period: int = ATR_PERIOD) -> float:
        """
        Computes current ATR using True Range rolling mean.
        """
        if len(self.candles) < period + 1:
            return float("nan")

        _, highs, lows, closes = self.get_arrays(period + 1)
        h = highs[1:]
        l = lows[1:]
        prev_c = closes[:-1]

        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        return float(np.mean(tr[-period:]))


# =====================================================================
# STATE MACHINE STRATEGY (EXACT REPLICATION)
# =====================================================================

class MarketCycleStateMachine:
    """
    Early Range Detection State Machine following Al Brooks Price Action mechanics:
    State 0: Searching for Momentum Spike
    State 1: Retracement & Boundary Establishment (Wait for counter-trend pullback > 30% forming structural Pivot)
    State 2: Early Range Trading (Mean Reversion at Boundary Upper and Lower with dynamic boundary expansion)
    """

    STATE_SEARCHING_SPIKE = 0
    STATE_ESTABLISHING_BOUNDARIES = 1
    STATE_TRADING_RANGE = 2

    def __init__(
        self,
        symbol: str = DEFAULT_SYMBOL,
        pa_confirm: bool = False,
        min_range_width: float = DEFAULT_MIN_RANGE_WIDTH,
        entry_style: str = DEFAULT_ENTRY_STYLE,
        min_retrace_ratio: float = MIN_RETRACE_RATIO,
        breakout_buffer_atr: float = BREAKOUT_BUFFER_ATR,
        expansion_buffer_atr: float = EXPANSION_BUFFER_ATR,
    ):
        self.symbol = symbol
        self.pa_confirm = pa_confirm
        self.min_range_width = min_range_width
        self.entry_style = entry_style
        self.min_retrace_ratio = min_retrace_ratio
        self.breakout_buffer_atr = breakout_buffer_atr
        self.expansion_buffer_atr = expansion_buffer_atr
        self.state = self.STATE_SEARCHING_SPIKE

        # State memory
        self.spike_info: Optional[Dict] = None
        self.range_info: Optional[Dict] = None

        self.state_entry_bar: int = 0
        self.event_counter: int = 0

    def reset_to_spike_search(self, reason: str = ""):
        if reason:
            logger.debug(f"[StateMachine] Resetting to STATE 0: {reason}")
        self.state = self.STATE_SEARCHING_SPIKE
        self.spike_info = None
        self.range_info = None

    def on_candle(self, candle: Candle, window: RollingWindow, current_atr: float, in_position: bool) -> Optional[str]:
        """
        Evaluates the market cycle stage on each newly closed candle.
        Returns: "BREAKOUT" or None.
        (Entries in live mode are triggered on real-time price touch of boundaries).
        """
        if np.isnan(current_atr) or current_atr <= 0:
            return None

        # -------------------------------------------------------------
        # STATE 0: SEARCHING FOR MOMENTUM SPIKE
        # -------------------------------------------------------------
        if self.state == self.STATE_SEARCHING_SPIKE:
            self._search_for_spike(candle, window, current_atr)

        # -------------------------------------------------------------
        # STATE 1: RETRACEMENT & BOUNDARY ESTABLISHMENT
        # -------------------------------------------------------------
        elif self.state == self.STATE_ESTABLISHING_BOUNDARIES:
            self._search_for_retracement_boundaries(candle, window, current_atr)

        # -------------------------------------------------------------
        # STATE 2: ACTIVE TRADING RANGE
        # -------------------------------------------------------------
        elif self.state == self.STATE_TRADING_RANGE:
            return self._evaluate_range_trading(candle, window, current_atr, in_position)

        return None

    def _search_for_spike(self, candle: Candle, window: RollingWindow, current_atr: float):
        """
        Evaluates recent bars for a momentum spike.
        """
        if len(window) < MAX_SPIKE_BARS + 2:
            return

        window_candles = list(window.candles)

        for w in range(MIN_SPIKE_BARS, MAX_SPIKE_BARS + 1):
            segment = window_candles[-w:]
            first_candle = segment[0]
            last_candle = segment[-1]

            displacement = last_candle.close - first_candle.open
            abs_move = abs(displacement)

            if abs_move / current_atr < MIN_SPIKE_ATR:
                continue

            # Efficiency = displacement / total_path
            path = sum(c.high - c.low for c in segment)
            for i in range(1, len(segment)):
                path += abs(segment[i].open - segment[i - 1].close)

            if path <= 0:
                continue

            efficiency = abs_move / path
            if efficiency < MIN_EFFICIENCY or efficiency > 1.05:
                continue

            # Directional consistency
            direction = "BULLISH" if displacement > 0 else "BEARISH"
            if direction == "BULLISH":
                aligned = sum(1 for c in segment if c.close > c.open)
                spike_base = min(c.low for c in segment)
                spike_peak = max(c.high for c in segment)
                spike_trough = spike_base
                peak_bar = max(segment, key=lambda c: c.high).index
                trough_bar = first_candle.index
            else:
                aligned = sum(1 for c in segment if c.close < c.open)
                spike_base = max(c.high for c in segment)
                spike_trough = min(c.low for c in segment)
                spike_peak = spike_base
                peak_bar = first_candle.index
                trough_bar = min(segment, key=lambda c: c.low).index

            consistency = aligned / w
            if consistency < MIN_DIRECTIONAL_CONSISTENCY:
                continue

            # Spike confirmed!
            self.event_counter += 1
            self.spike_info = {
                "event_id": f"EVT_{self.event_counter:05d}",
                "direction": direction,
                "start_idx": first_candle.index,
                "end_idx": last_candle.index,
                "start_time": first_candle.time,
                "end_time": last_candle.time,
                "start_price": first_candle.open,
                "end_price": last_candle.close,
                "spike_base": spike_base,
                "spike_peak": spike_peak,
                "spike_trough": spike_trough,
                "peak_bar": peak_bar,
                "trough_bar": trough_bar,
                "move": abs(spike_peak - spike_trough),
                "atr": current_atr,
            }
            self.state = self.STATE_ESTABLISHING_BOUNDARIES
            self.state_entry_bar = candle.index
            logger.info(
                f"[STATE 0 -> 1] Spike Confirmed: {self.spike_info['direction']} "
                f"({self.spike_info['event_id']}) at {candle.time} | Move: {abs_move:.2f} ({abs_move/current_atr:.2f} ATR) | "
                f"Peak: {spike_peak:.2f}, Trough: {spike_trough:.2f}"
            )
            return

    def _search_for_retracement_boundaries(self, candle: Candle, window: RollingWindow, current_atr: float):
        """
        STATE 1 (Retracement / Boundary Establishment):
        Immediately follows a momentum Spike.
        Waits for the first counter-trend pullback (> 30% of spike move) forming a structural Pivot.
        """
        elapsed = candle.index - self.spike_info["end_idx"]
        if elapsed > MAX_RETRACEMENT_BARS:
            self.reset_to_spike_search(reason=f"Retracement timeout ({elapsed} bars)")
            return

        direction = self.spike_info["direction"]
        spike_base = self.spike_info["spike_base"]

        window_candles = list(window.candles)
        candles_since_spike = [c for c in window_candles if c.index >= self.spike_info["start_idx"]]
        if not candles_since_spike:
            return

        prev_candle = window_candles[-2] if len(window_candles) >= 2 else None

        if direction == "BULLISH":
            spike_peak = self.spike_info["spike_peak"]

            # Check if bull spike is still extending before pullback has developed
            candles_after_peak = [c for c in candles_since_spike if c.index >= self.spike_info["peak_bar"]]
            lowest_since_peak = min(c.low for c in candles_after_peak) if candles_after_peak else candle.low
            interim_retrace = (spike_peak - lowest_since_peak) / max(1e-6, spike_peak - spike_base)

            if candle.high > spike_peak and interim_retrace < 0.20:
                self.spike_info["spike_peak"] = candle.high
                self.spike_info["peak_bar"] = candle.index
                self.spike_info["move"] = candle.high - spike_base
                spike_peak = candle.high

            spike_move = spike_peak - spike_base
            if spike_move <= 0:
                return

            # Check if pullback completely wiped out the spike
            if candle.close < spike_base:
                self.reset_to_spike_search(
                    reason=f"Bullish spike completely erased (Close {candle.close:.2f} < Base {spike_base:.2f})"
                )
                return

            pullback_bars = [c for c in candles_since_spike if c.index > self.spike_info["peak_bar"]]
            if not pullback_bars:
                return

            trough_candle = min(pullback_bars, key=lambda c: c.low)
            pullback_depth = spike_peak - trough_candle.low
            retrace_ratio = pullback_depth / spike_move

            if retrace_ratio < self.min_retrace_ratio:
                return

            # Structural Pivot Low Confirmation
            is_pivot_formed = False
            if trough_candle.index < candle.index:
                bounced = (
                    (candle.close > candle.open) or
                    (prev_candle is not None and candle.close > prev_candle.close) or
                    (candle.close >= trough_candle.low + PIVOT_BOUNCE_ATR * current_atr)
                )
                if bounced and (candle.low > trough_candle.low):
                    is_pivot_formed = True

            if not is_pivot_formed and trough_candle.index == candle.index:
                trough_range = trough_candle.high - trough_candle.low
                lower_wick = min(trough_candle.open, trough_candle.close) - trough_candle.low
                if trough_range > 0 and (lower_wick / trough_range >= 0.35) and (trough_candle.close > trough_candle.open):
                    is_pivot_formed = True

            if not is_pivot_formed:
                return

            upper_boundary = float(spike_peak)
            lower_boundary = float(trough_candle.low)

        else:  # BEARISH SPIKE
            spike_trough = self.spike_info["spike_trough"]

            candles_after_trough = [c for c in candles_since_spike if c.index >= self.spike_info["trough_bar"]]
            highest_since_trough = max(c.high for c in candles_after_trough) if candles_after_trough else candle.high
            interim_retrace = (highest_since_trough - spike_trough) / max(1e-6, spike_base - spike_trough)

            if candle.low < spike_trough and interim_retrace < 0.20:
                self.spike_info["spike_trough"] = candle.low
                self.spike_info["trough_bar"] = candle.index
                self.spike_info["move"] = spike_base - candle.low
                spike_trough = candle.low

            spike_move = spike_base - spike_trough
            if spike_move <= 0:
                return

            if candle.close > spike_base:
                self.reset_to_spike_search(
                    reason=f"Bearish spike completely erased (Close {candle.close:.2f} > Base {spike_base:.2f})"
                )
                return

            pullback_bars = [c for c in candles_since_spike if c.index > self.spike_info["trough_bar"]]
            if not pullback_bars:
                return

            peak_candle = max(pullback_bars, key=lambda c: c.high)
            pullback_depth = peak_candle.high - spike_trough
            retrace_ratio = pullback_depth / spike_move

            if retrace_ratio < self.min_retrace_ratio:
                return

            # Structural Pivot High Confirmation
            is_pivot_formed = False
            if peak_candle.index < candle.index:
                reversed_down = (
                    (candle.close < candle.open) or
                    (prev_candle is not None and candle.close < prev_candle.close) or
                    (candle.close <= peak_candle.high - PIVOT_BOUNCE_ATR * current_atr)
                )
                if reversed_down and (candle.high < peak_candle.high):
                    is_pivot_formed = True

            if not is_pivot_formed and peak_candle.index == candle.index:
                peak_range = peak_candle.high - peak_candle.low
                upper_wick = peak_candle.high - max(peak_candle.open, peak_candle.close)
                if peak_range > 0 and (upper_wick / peak_range >= 0.35) and (peak_candle.close < peak_candle.open):
                    is_pivot_formed = True

            if not is_pivot_formed:
                return

            upper_boundary = float(peak_candle.high)
            lower_boundary = float(spike_trough)

        if upper_boundary <= lower_boundary:
            return

        range_width = upper_boundary - lower_boundary
        range_width_atr = range_width / current_atr

        if range_width_atr < self.min_range_width:
            logger.debug(
                f"[STATE 1] Range width {range_width_atr:.2f} ATR < min_range_width {self.min_range_width:.2f} ATR at {candle.time}. Waiting for wider structure."
            )
            return

        # Transition to STATE 2 (Trading Range)
        entry_tol = min(current_atr * 0.50, range_width * 0.10)
        self.range_info = {
            "upper_boundary": upper_boundary,
            "lower_boundary": lower_boundary,
            "original_upper": upper_boundary,
            "original_lower": lower_boundary,
            "width_atr": range_width_atr,
            "start_idx": self.spike_info["start_idx"],
            "detection_idx": candle.index,
            "start_time": self.spike_info["start_time"],
            "detection_time": candle.time,
            "atr": current_atr,
            "entry_tolerance": entry_tol,
            "buy_zone": (lower_boundary, lower_boundary + entry_tol),
            "sell_zone": (upper_boundary - entry_tol, upper_boundary),
            "event_id": self.spike_info["event_id"],
            "spike_direction": direction,
            "retrace_ratio": retrace_ratio,
            "spike_snapshot": dict(self.spike_info),
            "last_entry_side": None,
            "last_entry_bar": -999,
        }

        self.state = self.STATE_TRADING_RANGE
        self.state_entry_bar = candle.index

        logger.info(
            f"[STATE 1 -> 2] Early Range Established at {candle.time} | Spike: {direction} | "
            f"Upper: {upper_boundary:.2f}, Lower: {lower_boundary:.2f}, Width: {range_width_atr:.2f} ATR "
            f"(Retracement: {retrace_ratio*100:.1f}%) | Entry Tol: {entry_tol:.2f} | "
            f"BUY Zone: [{lower_boundary:.2f}, {lower_boundary + entry_tol:.2f}] | "
            f"SELL Zone: [{upper_boundary - entry_tol:.2f}, {upper_boundary:.2f}]"
        )

    def _evaluate_range_trading(
        self, candle: Candle, window: RollingWindow, current_atr: float, in_position: bool
    ) -> Optional[str]:
        """
        Monitors active Trading Range on closed candles:
        1. Checks range lifetime expiration (MAX_RANGE_LIFETIME_BARS).
        2. Dynamic Boundary Adjustment: expands boundary on wick rejection.
        3. Structural Breakout check: triggers when candle closes strictly outside boundary + breakout_buffer_atr.
        """
        upper = self.range_info["upper_boundary"]
        lower = self.range_info["lower_boundary"]
        bars_since_detection = candle.index - self.range_info["detection_idx"]

        # 1. Range expiration check
        if bars_since_detection >= MAX_RANGE_LIFETIME_BARS:
            self.reset_to_spike_search(reason="Range maximum lifetime reached")
            return None

        # 2. Dynamic Boundary Adjustment on wick rejection
        expansion_buffer = self.expansion_buffer_atr * current_atr

        # Upper boundary expansion on rejection wick
        if candle.high > upper and candle.close <= upper:
            wick_pierce = candle.high - upper
            expansion = min(wick_pierce, expansion_buffer)
            new_upper = upper + expansion
            if new_upper > upper:
                logger.info(
                    f"[BOUNDARY EXPANSION] Upper Boundary expanded: {upper:.2f} -> {new_upper:.2f} "
                    f"(+{new_upper - upper:.2f} / +{(new_upper - upper)/current_atr:.2f} ATR) on wick rejection at {candle.time}"
                )
                self.range_info["upper_boundary"] = new_upper
                upper = new_upper

        # Lower boundary expansion on rejection wick
        if candle.low < lower and candle.close >= lower:
            wick_pierce = lower - candle.low
            expansion = min(wick_pierce, expansion_buffer)
            new_lower = lower - expansion
            if new_lower < lower:
                logger.info(
                    f"[BOUNDARY EXPANSION] Lower Boundary expanded: {lower:.2f} -> {new_lower:.2f} "
                    f"(-{lower - new_lower:.2f} / -{(lower - new_lower)/current_atr:.2f} ATR) on wick rejection at {candle.time}"
                )
                self.range_info["lower_boundary"] = new_lower
                lower = new_lower

        self.range_info["width_atr"] = (upper - lower) / current_atr
        range_w = upper - lower
        if range_w > 0 and current_atr > 0 and not np.isnan(current_atr):
            exp_tol = min(current_atr * 0.50, range_w * 0.10)
            self.range_info["entry_tolerance"] = exp_tol
            self.range_info["buy_zone"] = (lower, lower + exp_tol)
            self.range_info["sell_zone"] = (upper - exp_tol, upper)

        # 3. Structural Range Breakout check (Candle CLOSE outside boundary + buffer)
        breakout_buffer = self.breakout_buffer_atr * current_atr
        if candle.close > (upper + breakout_buffer):
            self.reset_to_spike_search(
                reason=f"Structural range breakout above upper boundary (Close: {candle.close:.2f} > {upper + breakout_buffer:.2f})"
            )
            return "BREAKOUT"

        if candle.close < (lower - breakout_buffer):
            self.reset_to_spike_search(
                reason=f"Structural range breakout below lower boundary (Close: {candle.close:.2f} < {lower - breakout_buffer:.2f})"
            )
            return "BREAKOUT"

        if in_position:
            return None

        # Filter: Do not enter on the exact establishment bar (allow starting on next bar)
        if bars_since_detection < 1:
            return None

        # Initialize tracking state if not present
        if "sweep_history" not in self.range_info:
            self.range_info["sweep_history"] = collections.deque(maxlen=4)
            if "last_entry_side" not in self.range_info:
                self.range_info["last_entry_side"] = None
            if "last_entry_bar" not in self.range_info:
                self.range_info["last_entry_bar"] = -999

        self.range_info["sweep_history"].append({
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "open": candle.open,
            "bar": candle.index,
        })

        # In 'touch' mode, entries are handled in real-time by check_touch_entry()
        if self.entry_style == "touch":
            return None

        # Cooldown: Minimum 2 bars between consecutive entries
        if candle.index - self.range_info.get("last_entry_bar", -999) < 2:
            return None

        # -------------------------------------------------------------
        # ENTRY STYLE: PA_CLOSE (Candle close + PA / Rejection confirmation)
        # -------------------------------------------------------------
        candle_range = candle.high - candle.low + 1e-6
        recent_history = list(self.range_info["sweep_history"])
        prev_candle = window.candles[-2] if len(window.candles) >= 2 else None

        real_body = abs(candle.close - candle.open)
        upper_wick = candle.high - max(candle.open, candle.close)
        lower_wick = min(candle.open, candle.close) - candle.low

        is_bearish_pinbar = (candle.close < candle.open) and (upper_wick >= 2.0 * real_body)
        is_bearish_engulfing = False
        if prev_candle is not None and (candle.close < candle.open):
            prev_top = max(prev_candle.open, prev_candle.close)
            prev_bottom = min(prev_candle.open, prev_candle.close)
            if (candle.open >= prev_top - 0.05) and (candle.close <= prev_bottom + 0.05) and (real_body > abs(prev_candle.close - prev_candle.open)):
                is_bearish_engulfing = True

        is_bullish_pinbar = (candle.close > candle.open) and (lower_wick >= 2.0 * real_body)
        is_bullish_engulfing = False
        if prev_candle is not None and (candle.close > candle.open):
            prev_top = max(prev_candle.open, prev_candle.close)
            prev_bottom = min(prev_candle.open, prev_candle.close)
            if (candle.open <= prev_bottom + 0.05) and (candle.close >= prev_top - 0.05) and (real_body > abs(prev_candle.close - prev_candle.open)):
                is_bullish_engulfing = True

        single_bar_bear_sweep = (
            candle.high > upper and
            candle.close < upper and
            candle.close < candle.open and
            (candle.high - max(candle.open, candle.close)) >= 0.20 * candle_range
        )
        prior_tested_upper = any(h["high"] >= upper for h in recent_history[:-1])
        pullback_bear_reentry = (
            prior_tested_upper and
            candle.close < upper and
            candle.close < candle.open
        )
        bear_tested = (candle.high > upper or prior_tested_upper) and (candle.close < upper)

        bear_trigger = False
        if self.pa_confirm:
            if bear_tested and (candle.close < candle.open) and (is_bearish_pinbar or is_bearish_engulfing):
                bear_trigger = True
        else:
            bear_trigger = (single_bar_bear_sweep or pullback_bear_reentry)

        if bear_trigger:
            pa_detail = ""
            if self.pa_confirm:
                pa_name = "Bearish Pinbar" if is_bearish_pinbar else "Bearish Engulfing"
                pa_detail = f" [PA Confirmed: {pa_name}]"
            trigger_type = "Sweep" if single_bar_bear_sweep else "Pullback"
            logger.info(
                f"[TRIGGER] Bearish {trigger_type}{pa_detail} at Upper Bound ({upper:.2f}) on {candle.time} | Close: {candle.close:.2f}"
            )
            return "SHORT"

        single_bar_bull_sweep = (
            candle.low < lower and
            candle.close > lower and
            candle.close > candle.open and
            (min(candle.open, candle.close) - candle.low) >= 0.20 * candle_range
        )
        prior_tested_lower = any(h["low"] <= lower for h in recent_history[:-1])
        pullback_bull_reentry = (
            prior_tested_lower and
            candle.close > lower and
            candle.close > candle.open
        )
        bull_tested = (candle.low < lower or prior_tested_lower) and (candle.close > lower)

        bull_trigger = False
        if self.pa_confirm:
            if bull_tested and (candle.close > candle.open) and (is_bullish_pinbar or is_bullish_engulfing):
                bull_trigger = True
        else:
            bull_trigger = (single_bar_bull_sweep or pullback_bull_reentry)

        if bull_trigger:
            pa_detail = ""
            if self.pa_confirm:
                pa_name = "Bullish Pinbar" if is_bullish_pinbar else "Bullish Engulfing"
                pa_detail = f" [PA Confirmed: {pa_name}]"
            trigger_type = "Sweep" if single_bar_bull_sweep else "Pullback"
            logger.info(
                f"[TRIGGER] Bullish {trigger_type}{pa_detail} at Lower Bound ({lower:.2f}) on {candle.time} | Close: {candle.close:.2f}"
            )
            return "LONG"

        return None


# =====================================================================
# LIVE RISK & POSITION SIZING MANAGER
# =====================================================================

class LiveRiskManager:
    """
    Manages dynamic risk, lot sizing, and stop loss calculation according to backtester specifications:
    - --risk-mode equity_pct (or fixed_usd)
    - --risk-pct (e.g. 10.0%)
    - --max-lot-scaling (0.02 lots per $100 balance)
    - --sl-mode range_width (Stop distance = range_width * sl_width_mult)
    - Strict broker contract_size, volume_step, and min/max volume limits.
    """

    def __init__(
        self,
        risk_mode: str = DEFAULT_RISK_MODE,
        risk_pct: float = DEFAULT_RISK_PCT,
        risk_per_trade: float = RISK_PER_TRADE_USD,
        lots_per_100: float = LOTS_PER_100_BALANCE,
        sl_mode: str = DEFAULT_SL_MODE,
        sl_atr_multiplier: float = SL_ATR_MULTIPLIER,
        sl_width_multiplier: float = SL_WIDTH_MULTIPLIER,
        risk_reward_ratio: float = RR_RATIO,
        commission_per_trade: float = COMMISSION_PER_TRADE,
    ):
        self.risk_mode = risk_mode
        self.risk_pct = float(risk_pct)
        self.risk_per_trade = float(risk_per_trade)
        self.lots_per_100 = float(lots_per_100)
        self.sl_mode = sl_mode
        self.sl_atr_multiplier = float(sl_atr_multiplier)
        self.sl_width_multiplier = float(sl_width_multiplier)
        self.risk_reward_ratio = float(risk_reward_ratio)
        self.commission_per_trade = float(commission_per_trade)

    def calculate_order_parameters(
        self,
        side: str,
        current_bid: float,
        current_ask: float,
        range_info: Dict,
        current_atr: float,
        balance: float,
        contract_size: float,
        volume_step: float,
        min_volume: float,
        max_volume: float,
        leverage: float,
        digits: int,
    ) -> Optional[Dict]:
        """
        Computes entry price, SL, TP (midline), and lot size.
        """
        upper = float(range_info["upper_boundary"])
        lower = float(range_info["lower_boundary"])
        midline = (upper + lower) / 2.0
        range_width = upper - lower

        # 1. Stop Loss Distance Calculation
        if self.sl_mode == "range_width":
            sl_dist = range_width * self.sl_width_multiplier
        else:  # "atr"
            sl_dist = current_atr * self.sl_atr_multiplier

        if not np.isfinite(sl_dist) or sl_dist <= 0:
            return None

        # 2. Entry Price, SL, TP by side
        if side == "SHORT":
            # For SHORT, entry is at upper boundary touch; execution price is Bid
            entry_price = current_bid
            # Reject SHORT entries at or below the Midline (direction to Midline must be downward)
            if entry_price <= midline:
                logger.warning(
                    f"[ENTRY REJECTED] SHORT entry price ({entry_price:.2f}) is at or below "
                    f"Midline ({midline:.2f}). Direction toward Midline is invalid."
                )
                return None

            sl = round(upper + sl_dist, digits)
            risk_points = abs(sl - entry_price)
            if risk_points <= 0:
                risk_points = sl_dist

            opposite_boundary = lower
            # Move SHORT TP upward toward entry by a percentage of the
            # original entry-to-midline target distance.
            tp_adjustment = abs(entry_price - midline) * (TP_ADJUSTMENT_PERCENT / 100.0)
            tp = round(midline + tp_adjustment, digits)
            logger.info(
                f"[TP ADJUSTMENT] SHORT | Base TP: {midline:.{digits}f} | "
                f"Adjustment: +{tp_adjustment:.{digits}f} ({TP_ADJUSTMENT_PERCENT:.2f}%) | "
                f"Adjusted TP: {tp:.{digits}f}"
            )
            if tp >= entry_price:
                logger.warning(
                    f"[ENTRY REJECTED] SHORT TP ({tp:.2f}) >= entry price ({entry_price:.2f}). "
                    f"Direction toward Midline is invalid."
                )
                return None

        else:  # LONG
            # For LONG, entry is at lower boundary touch; execution price is Ask
            entry_price = current_ask
            # Reject LONG entries at or above the Midline (direction to Midline must be upward)
            if entry_price >= midline:
                logger.warning(
                    f"[ENTRY REJECTED] LONG entry price ({entry_price:.2f}) is at or above "
                    f"Midline ({midline:.2f}). Direction toward Midline is invalid."
                )
                return None

            sl = round(lower - sl_dist, digits)
            risk_points = abs(entry_price - sl)
            if risk_points <= 0:
                risk_points = sl_dist

            opposite_boundary = upper
            # Move LONG TP downward toward entry by a percentage of the
            # original entry-to-midline target distance.
            tp_adjustment = abs(midline - entry_price) * (TP_ADJUSTMENT_PERCENT / 100.0)
            tp = round(midline - tp_adjustment, digits)
            logger.info(
                f"[TP ADJUSTMENT] LONG | Base TP: {midline:.{digits}f} | "
                f"Adjustment: -{tp_adjustment:.{digits}f} ({TP_ADJUSTMENT_PERCENT:.2f}%) | "
                f"Adjusted TP: {tp:.{digits}f}"
            )
            if tp <= entry_price:
                logger.warning(
                    f"[ENTRY REJECTED] LONG TP ({tp:.2f}) <= entry price ({entry_price:.2f}). "
                    f"Direction toward Midline is invalid."
                )
                return None

        # 3. Dynamic Max Lot Size (Scaling Rule)
        # Rule: 0.02 lots per every $100 of account balance
        # Formula: max_allowed_lots = (balance / 100) * lots_per_100
        balance = max(0.01, balance)
        max_allowed_lots = (balance / 100.0) * self.lots_per_100

        # 4. Strict Dollar Risk & Position Sizing
        if self.risk_mode == "equity_pct":
            trade_risk_usd = balance * (self.risk_pct / 100.0)
        else:
            trade_risk_usd = self.risk_per_trade

        raw_size = trade_risk_usd / (risk_points * contract_size)
        final_size = min(raw_size, max_allowed_lots)
        final_size = max(final_size, min_volume)  # broker min limit (default 0.01)
        final_size = min(final_size, max_volume)
        final_size = round(final_size, 2)         # exact match to backtester rounding

        if final_size < min_volume:
            logger.warning(f"[ORDER REJECTED] Lot size {final_size} is below broker minimum {min_volume}.")
            return None

        actual_risk = (risk_points * final_size * contract_size) + self.commission_per_trade
        margin_required = (final_size * entry_price * contract_size / leverage) if leverage > 0 else 0.0

        return {
            "side": side,
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "size": final_size,
            "raw_size": raw_size,
            "max_allowed_lots": max_allowed_lots,
            "risk_points": risk_points,
            "sl_dist": sl_dist,
            "midline": midline,
            "opposite_boundary": opposite_boundary,
            "upper_boundary": upper,
            "lower_boundary": lower,
            "actual_risk": actual_risk,
            "margin_required": margin_required,
        }


# =====================================================================
# MT5 INTERFACE & ORDER EXECUTION
# =====================================================================

class MT5OrderExecution:
    """
    Handles live MT5 connectivity, price polling, order placement, position closing,
    and SL/TP modifications with fallback filling modes.
    """

    def __init__(self, symbol: str = DEFAULT_SYMBOL, magic: int = DEFAULT_MAGIC):
        self.symbol = symbol
        self.magic = magic
        self.symbol_info = None
        self.digits = 2
        self.point = 0.01
        self.contract_size = CONTRACT_SIZE
        self.volume_step = VOLUME_STEP
        self.min_volume = MIN_VOLUME
        self.max_volume = MAX_VOLUME
        self.leverage = LEVERAGE
        self.filling_type = mt5.ORDER_FILLING_FOK

    def initialize(self) -> bool:
        """
        Initializes MT5 connection and configures symbol parameters.
        """
        if not mt5.initialize():
            logger.error(f"[MT5] Initialization failed: {mt5.last_error()}")
            return False

        terminal = mt5.terminal_info()
        account = mt5.account_info()

        if terminal is None or account is None:
            logger.error("[MT5] Unable to retrieve terminal or account information.")
            mt5.shutdown()
            return False

        if not terminal.connected:
            logger.error("[MT5] Terminal is not connected to broker.")
            mt5.shutdown()
            return False

        logger.info(f"[MT5] Connected successfully to {account.server} | Account #{account.login} ({account.company})")
        logger.info(f"[MT5] Account Balance: ${account.balance:.2f} {account.currency} | Equity: ${account.equity:.2f} | Leverage: 1:{account.leverage}")

        if not terminal.trade_allowed:
            logger.warning("[MT5 WARNING] 'Algo Trading' button is disabled in MT5 toolbar. Please enable it for automated order execution.")

        # Ensure symbol is available
        self.symbol_info = mt5.symbol_info(self.symbol)
        if self.symbol_info is None:
            available = mt5.symbols_get()
            matches = [item.name for item in (available or []) if "XAUUSD" in item.name.upper() or "GOLD" in item.name.upper()]
            logger.error(f"[MT5] Symbol {self.symbol} not found. Possible matching symbols: {matches[:10]}")
            mt5.shutdown()
            return False

        if not self.symbol_info.visible:
            if not mt5.symbol_select(self.symbol, True):
                logger.error(f"[MT5] Unable to select symbol {self.symbol}: {mt5.last_error()}")
                mt5.shutdown()
                return False

        # Read symbol specification
        self.digits = self.symbol_info.digits
        self.point = self.symbol_info.point
        self.contract_size = float(self.symbol_info.trade_contract_size or CONTRACT_SIZE)
        self.volume_step = float(self.symbol_info.volume_step or VOLUME_STEP)
        self.min_volume = float(self.symbol_info.volume_min or MIN_VOLUME)
        self.max_volume = float(self.symbol_info.volume_max or MAX_VOLUME)
        self.leverage = float(account.leverage or LEVERAGE)

        # Detect filling type
        filling_mode = getattr(self.symbol_info, "filling_mode", 0)
        if filling_mode & 1:  # SYMBOL_FILLING_FOK
            self.filling_type = mt5.ORDER_FILLING_FOK
        elif filling_mode & 2:  # SYMBOL_FILLING_IOC
            self.filling_type = mt5.ORDER_FILLING_IOC
        else:
            self.filling_type = mt5.ORDER_FILLING_RETURN

        logger.info(
            f"[MT5] Symbol ready: {self.symbol} | Digits: {self.digits} | Point: {self.point} | "
            f"Contract Size: {self.contract_size} | VolStep: {self.volume_step} | MinVol: {self.min_volume} | "
            f"FillingType: {self.filling_type}"
        )
        return True

    def get_latest_tick(self):
        """Fetches the latest real-time tick for the symbol."""
        return mt5.symbol_info_tick(self.symbol)

    def get_account_balance(self) -> float:
        """Returns current account balance."""
        acc = mt5.account_info()
        return float(acc.balance) if acc else 0.0

    def get_account_equity(self) -> float:
        """Returns current account equity."""
        acc = mt5.account_info()
        return float(acc.equity) if acc else 0.0

    def _send_with_fallback(self, request: Dict):
        """
        Sends order request to MT5 with automatic fallback on filling modes if error 10030 occurs.
        """
        result = mt5.order_send(request)
        if result and result.retcode == 10030:  # Invalid fill mode
            # Try alternate filling types
            for alt_fill in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
                if alt_fill == request["type_filling"]:
                    continue
                req_copy = request.copy()
                req_copy["type_filling"] = alt_fill
                res_alt = mt5.order_send(req_copy)
                if res_alt and res_alt.retcode == mt5.TRADE_RETCODE_DONE:
                    self.filling_type = alt_fill
                    return res_alt
        return result

    def execute_market_order(self, side: str, volume: float, sl: float, tp: float, comment: str = ""):
        """
        Executes a live market BUY or SELL order.
        """
        tick = self.get_latest_tick()
        if tick is None:
            logger.error("[ORDER ERROR] Cannot execute order: no tick available.")
            return False, None

        order_type = mt5.ORDER_TYPE_BUY if side == "LONG" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "LONG" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(price),
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": int(self.magic),
            "comment": comment[:29],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.filling_type,
        }

        result = self._send_with_fallback(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            retcode = result.retcode if result else "None"
            desc = RETCODE_DESCRIPTIONS.get(retcode, "Unknown return code")
            error_comment = result.comment if result else str(mt5.last_error())
            logger.error(f"[ORDER FAILED] {side} {volume:.2f} lots | Retcode: {retcode} ({desc}) | Comment: {error_comment}")
            return False, result

        return True, result

    def close_position_market(self, position: LivePosition, reason: str):
        """
        Closes an open position using an offsetting market order.
        """
        tick = self.get_latest_tick()
        if tick is None:
            logger.error(f"[CLOSE ERROR] Cannot close position #{position.ticket}: no tick available.")
            return False, None

        close_type = mt5.ORDER_TYPE_SELL if position.side == "LONG" else mt5.ORDER_TYPE_BUY
        price = tick.bid if position.side == "LONG" else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(position.size),
            "type": close_type,
            "position": int(position.ticket),
            "price": float(price),
            "deviation": 20,
            "magic": int(self.magic),
            "comment": f"Close #{position.ticket} {reason}"[:29],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.filling_type,
        }

        result = self._send_with_fallback(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            retcode = result.retcode if result else "None"
            desc = RETCODE_DESCRIPTIONS.get(retcode, "Unknown return code")
            error_comment = result.comment if result else str(mt5.last_error())
            logger.error(f"[CLOSE FAILED] #{position.ticket} | Retcode: {retcode} ({desc}) | Comment: {error_comment}")
            return False, result

        return True, result

    def modify_position_sltp(self, ticket: int, sl: float, tp: float):
        """
        Modifies SL and TP of an open position (e.g. when boundaries expand).
        """
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": int(ticket),
            "sl": float(sl),
            "tp": float(tp),
        }
        res = mt5.order_send(request)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            logger.debug(f"[MT5] Position #{ticket} SL/TP modified -> SL: {sl:.2f}, TP: {tp:.2f}")
            return True
        return False

    def get_open_position_by_ticket(self, ticket: int):
        """Checks if a position is currently open."""
        positions = mt5.positions_get(ticket=ticket)
        if positions and len(positions) > 0:
            return positions[0]
        return None

    def get_open_positions_by_magic(self):
        """Returns all open positions matching symbol and magic."""
        positions = mt5.positions_get(symbol=self.symbol)
        if positions is None:
            return []
        return [p for p in positions if p.magic == self.magic]

    def get_position_deals(self, ticket: int):
        """Fetches executed historical deals for a given position ticket."""
        now = datetime.now(timezone.utc)
        from_time = datetime.fromtimestamp(now.timestamp() - 86400 * 7, tz=timezone.utc)
        deals = mt5.history_deals_get(from_time, now, position=ticket)
        return deals or []

    def shutdown(self):
        """Graceful MT5 shutdown."""
        mt5.shutdown()
        logger.info("[MT5] Disconnected cleanly.")


# =====================================================================
# LIVE EVENT-DRIVEN TRADING BOT ENGINE
# =====================================================================

class LiveEventDrivenBot:
    """
    Production-ready live forward-testing trading bot.
    Strictly integrates:
    1. Historical warmup of 300 M5 bars to initialize the State Machine.
    2. Continuous closed-candle polling to update ATR, detect spikes, boundaries, and breakouts.
    3. High-frequency tick polling to execute Touch Limit market entries and Midline exits.
    4. Exact risk management and logging format matching the backtester.
    """

    def __init__(
        self,
        symbol: str = DEFAULT_SYMBOL,
        magic: int = DEFAULT_MAGIC,
        risk_mode: str = DEFAULT_RISK_MODE,
        risk_pct: float = DEFAULT_RISK_PCT,
        risk_per_trade: float = RISK_PER_TRADE_USD,
        lots_per_100: float = LOTS_PER_100_BALANCE,
        sl_mode: str = DEFAULT_SL_MODE,
        sl_atr_multiplier: float = SL_ATR_MULTIPLIER,
        sl_width_multiplier: float = SL_WIDTH_MULTIPLIER,
        risk_reward_ratio: float = RR_RATIO,
        entry_style: str = DEFAULT_ENTRY_STYLE,
        exit_style: str = DEFAULT_EXIT_STYLE,
        pa_confirm: bool = False,
        min_range_width: float = DEFAULT_MIN_RANGE_WIDTH,
        min_retrace_ratio: float = MIN_RETRACE_RATIO,
        breakout_buffer_atr: float = BREAKOUT_BUFFER_ATR,
        expansion_buffer_atr: float = EXPANSION_BUFFER_ATR,
        poll_interval: float = 1.0,
        checkin_interval: float = DEFAULT_CHECKIN_INTERVAL,
        warmup_bars: int = ROLLING_WINDOW_SIZE,
    ):
        self.symbol = symbol
        self.magic = magic
        self.poll_interval = poll_interval
        self.checkin_interval = checkin_interval
        self.last_checkin_time = 0.0
        self.warmup_bars = warmup_bars

        self.mt5_exec = MT5OrderExecution(symbol=symbol, magic=magic)
        self.window = RollingWindow(maxlen=ROLLING_WINDOW_SIZE)
        self.strategy = MarketCycleStateMachine(
            symbol=symbol,
            pa_confirm=pa_confirm,
            min_range_width=min_range_width,
            entry_style=entry_style,
            min_retrace_ratio=min_retrace_ratio,
            breakout_buffer_atr=breakout_buffer_atr,
            expansion_buffer_atr=expansion_buffer_atr,
        )
        self.risk_manager = LiveRiskManager(
            risk_mode=risk_mode,
            risk_pct=risk_pct,
            risk_per_trade=risk_per_trade,
            lots_per_100=lots_per_100,
            sl_mode=sl_mode,
            sl_atr_multiplier=sl_atr_multiplier,
            sl_width_multiplier=sl_width_multiplier,
            risk_reward_ratio=risk_reward_ratio,
        )

        self.active_position: Optional[LivePosition] = None
        self.trade_counter: int = 0
        self.current_candle_index: int = 0
        self.last_closed_bar_time: int = 0
        self.is_running: bool = False
        self._close_attempt_count: int = 0
        self._close_max_retries: int = 5
        self._close_cooldown_until: float = 0.0
        self._order_in_flight: bool = False

    def _install_mt5_overlay_indicator(self):
        """
        Automatically copies ApexLiveOverlay.mq5 into the active MT5 terminal's MQL5/Indicators folder
        so the user can attach it directly to the XAUUSD M5 chart in MetaTrader 5.
        """
        try:
            term_info = mt5.terminal_info()
            if term_info is None or not getattr(term_info, "data_path", None):
                return
            data_path = Path(term_info.data_path)
            ind_dir = data_path / "MQL5" / "Indicators"
            files_dir = data_path / "MQL5" / "Files"
            ind_dir.mkdir(parents=True, exist_ok=True)
            files_dir.mkdir(parents=True, exist_ok=True)
            self._mt5_overlay_file = files_dir / "apex_live_overlay.txt"

            target_mq5 = ind_dir / "ApexLiveOverlay.mq5"
            target_mq5.write_text(MQL5_OVERLAY_INDICATOR_SOURCE, encoding="utf-8")
            logger.info(
                f"[MT5 OVERLAY] Auto-installed MQL5 chart indicator to: {target_mq5}\n"
                f"              -> In MT5 Navigator, right-click 'Indicators' -> 'Refresh', "
                f"compile 'ApexLiveOverlay' (or press F4 -> Compile), and drag it onto your {self.symbol} M5 chart!"
            )
        except Exception as exc:
            logger.debug(f"[MT5 OVERLAY] Could not auto-copy indicator: {exc}")

    def sync_mt5_chart_overlay(self, stage: str = "UPDATE"):
        """
        Writes active Spike & Trading Range coordinates directly into MT5's MQL5/Files/apex_live_overlay.txt.
        The companion MQL5 indicator (ApexLiveOverlay.mq5) reads this file every second and draws
        the Spike box, Upper/Lower Range boundaries, Midline TP, and Shaded Touch Zones directly on the MT5 chart.
        """
        try:
            if not hasattr(self, "_mt5_overlay_file") or self._mt5_overlay_file is None:
                term_info = mt5.terminal_info()
                if term_info and getattr(term_info, "data_path", None):
                    files_dir = Path(term_info.data_path) / "MQL5" / "Files"
                    files_dir.mkdir(parents=True, exist_ok=True)
                    self._mt5_overlay_file = files_dir / "apex_live_overlay.txt"
                else:
                    self._mt5_overlay_file = Path("apex_live_overlay.txt")

            if not hasattr(self, "_overlay_history"):
                self._overlay_history = collections.OrderedDict()

            now_ts = int(self.window.candles[-1].time.timestamp()) if len(self.window) > 0 else int(time.time())
            # Project current range lines 10 bars (50 minutes) ahead of current bar for clean visual display on MT5
            end_proj_ts = now_ts + (10 * TIMEFRAME_SECONDS)

            # 1. Record Spike
            spike = self.strategy.spike_info
            if spike is None and self.strategy.range_info:
                spike = self.strategy.range_info.get("spike_snapshot")

            if spike:
                evt_id = spike["event_id"]
                s_dir = spike["direction"]
                t1 = int(pd.Timestamp(spike["start_time"]).timestamp())
                t2 = int(pd.Timestamp(spike["end_time"]).timestamp())
                peak = float(spike["spike_peak"])
                trough = float(spike["spike_trough"])
                p1 = float(spike["start_price"])
                p2 = float(spike["end_price"])
                self._overlay_history[f"{evt_id}_SPIKE"] = (
                    f"SPIKE|{evt_id}|{s_dir}|{t1}|{t2}|{peak:.2f}|{trough:.2f}|{p1:.2f}|{p2:.2f}"
                )

            # 2. Record Trading Range
            if self.strategy.state == self.strategy.STATE_TRADING_RANGE and self.strategy.range_info:
                ri = self.strategy.range_info
                evt_id = ri["event_id"]
                t_det = int(pd.Timestamp(ri["detection_time"]).timestamp())
                upper = float(ri["upper_boundary"])
                lower = float(ri["lower_boundary"])
                mid = (upper + lower) / 2.0
                tol = float(ri.get("entry_tolerance", 0.0))
                self._overlay_history[f"{evt_id}_RANGE"] = (
                    f"RANGE|{evt_id}|{t_det}|{end_proj_ts}|{upper:.2f}|{lower:.2f}|{mid:.2f}|{tol:.2f}"
                )

            # Keep last 40 overlay objects so the chart stays fast and clean
            while len(self._overlay_history) > 40:
                self._overlay_history.popitem(last=False)

            content = "\n".join(self._overlay_history.values()) + "\n"
            self._mt5_overlay_file.write_text(content, encoding="utf-8")
            logger.info(f"[MT5 CHART OVERLAY] Synced [{stage}] objects to MT5 chart ({self._mt5_overlay_file.name})")
        except Exception as exc:
            logger.warning(f"[MT5 CHART OVERLAY WARNING] Failed to sync MT5 chart objects: {exc}")

    # Backward-compatible alias
    plot_live_structure = sync_mt5_chart_overlay

    def warmup(self) -> bool:
        """
        Warms up the state machine by fetching recent closed M5 candles from MT5.
        """
        logger.info(f"[WARMUP] Fetching last {self.warmup_bars} closed M5 bars from MT5 for {self.symbol}...")
        rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M5, 1, self.warmup_bars)
        if rates is None or len(rates) == 0:
            logger.error("[WARMUP ERROR] Failed to fetch historical M5 candles from MT5.")
            return False

        logger.info(f"[WARMUP] Replaying {len(rates)} historical bars through state machine...")
        for i, r in enumerate(rates):
            candle_time = pd.to_datetime(r["time"], unit="s", utc=True)
            candle = Candle(
                time=candle_time,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["tick_volume"]),
                spread=float(r["spread"]),
                index=i,
            )
            self.window.append(candle)
            self.current_candle_index = i
            current_atr = self.window.calculate_atr()

            # Warmup state machine without triggering real trade executions
            self.strategy.on_candle(candle, self.window, current_atr, in_position=False)

        self.last_closed_bar_time = int(rates[-1]["time"])
        current_atr = self.window.calculate_atr()
        state_names = {0: "STATE 0 (Searching Spike)", 1: "STATE 1 (Retracement / Boundaries)", 2: "STATE 2 (Trading Range)"}
        logger.info(f"[WARMUP COMPLETE] Current Market State: {state_names.get(self.strategy.state, 'Unknown')} | Current ATR: {current_atr:.2f}")

        if self.strategy.state == self.strategy.STATE_TRADING_RANGE and self.strategy.range_info:
            ri = self.strategy.range_info
            range_w = float(ri["upper_boundary"]) - float(ri["lower_boundary"])
            tol = self.calculate_entry_tolerance(current_atr, range_w)
            buy_z = (float(ri["lower_boundary"]), float(ri["lower_boundary"]) + tol)
            sell_z = (float(ri["upper_boundary"]) - tol, float(ri["upper_boundary"]))
            logger.info(
                f"[WARMUP] Active Range: Upper={ri['upper_boundary']:.2f}, Lower={ri['lower_boundary']:.2f}, "
                f"Midline={(ri['upper_boundary']+ri['lower_boundary'])/2:.2f}, Width={ri['width_atr']:.2f} ATR | "
                f"Tol: {tol:.2f} | BUY Zone: [{buy_z[0]:.2f}, {buy_z[1]:.2f}] | "
                f"SELL Zone: [{sell_z[0]:.2f}, {sell_z[1]:.2f}]"
            )
            self.plot_live_structure(stage="WARMUP_TRADING_RANGE")
        elif self.strategy.state == self.strategy.STATE_ESTABLISHING_BOUNDARIES and self.strategy.spike_info:
            self.plot_live_structure(stage="WARMUP_SPIKE")

        # Reconnect to any existing open position managed by this bot
        existing = self.mt5_exec.get_open_positions_by_magic()
        if existing:
            pos = existing[0]
            side = "LONG" if pos.type == mt5.ORDER_TYPE_BUY else "SHORT"
            self.trade_counter += 1
            self.active_position = LivePosition(
                ticket=pos.ticket,
                trade_id=self.trade_counter,
                symbol=self.symbol,
                side=side,
                entry_time=pd.to_datetime(pos.time, unit="s", utc=True),
                entry_price=float(pos.price_open),
                sl=float(pos.sl),
                tp=float(pos.tp),
                size=float(pos.volume),
                risk_points=abs(float(pos.price_open) - float(pos.sl)),
                entry_idx=self.current_candle_index,
                event_id=self.strategy.range_info.get("event_id", "RECONNECTED") if self.strategy.range_info else "RECONNECTED",
                midline=float(pos.tp),
            )
            logger.info(f"[RECONNECTED] Open {side} position #{pos.ticket} attached (Volume: {pos.volume:.2f}, SL: {pos.sl:.2f}, TP: {pos.tp:.2f})")

        return True

    def on_closed_candle(self, r):
        """
        Processes a newly closed M5 candle:
        - Updates rolling window and ATR.
        - Evaluates holding period timeout (MAX_HOLD_BARS = 72).
        - Evaluates strategy transitions (spikes, boundaries, dynamic expansion, breakout).
        """
        self.current_candle_index += 1
        candle_time = pd.to_datetime(r["time"], unit="s", utc=True)
        candle = Candle(
            time=candle_time,
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["tick_volume"]),
            spread=float(r["spread"]),
            index=self.current_candle_index,
        )

        self.window.append(candle)
        current_atr = self.window.calculate_atr()

        # 1. Holding Period Timeout Exit (72 bars on M5 = 6 hours)
        if self.active_position is not None:
            bars_held = self.current_candle_index - self.active_position.entry_idx
            if bars_held >= MAX_HOLD_BARS:
                logger.info(f"[EXIT TRIGGER] Max hold bars ({MAX_HOLD_BARS}) reached. Closing position #{self.active_position.ticket} (TIME_EXIT).")
                self.close_active_position(reason="TIME_EXIT")

        # 2. Strategy evaluation on closed candle (track state & boundaries for live chart plotting)
        prev_state = self.strategy.state
        prev_upper = float(self.strategy.range_info["upper_boundary"]) if self.strategy.range_info else None
        prev_lower = float(self.strategy.range_info["lower_boundary"]) if self.strategy.range_info else None

        in_pos = self.active_position is not None
        signal = self.strategy.on_candle(candle, self.window, current_atr, in_pos)

        # 2.1 Plot Spike or Trading Range if newly detected or expanded on this candle
        if prev_state != self.strategy.STATE_ESTABLISHING_BOUNDARIES and self.strategy.state == self.strategy.STATE_ESTABLISHING_BOUNDARIES:
            self.plot_live_structure(stage="SPIKE_DETECTED")
        elif prev_state != self.strategy.STATE_TRADING_RANGE and self.strategy.state == self.strategy.STATE_TRADING_RANGE:
            self.plot_live_structure(stage="RANGE_ESTABLISHED")
        elif self.strategy.state == self.strategy.STATE_TRADING_RANGE and self.strategy.range_info:
            curr_upper = float(self.strategy.range_info["upper_boundary"])
            curr_lower = float(self.strategy.range_info["lower_boundary"])
            if (prev_upper is not None and curr_upper != prev_upper) or (prev_lower is not None and curr_lower != prev_lower):
                self.plot_live_structure(stage="RANGE_EXPANDED")

        # 2.2 Keep MT5 chart range lines extended with each new closed bar
        if self.strategy.state in (self.strategy.STATE_ESTABLISHING_BOUNDARIES, self.strategy.STATE_TRADING_RANGE):
            self.sync_mt5_chart_overlay(stage="BAR_UPDATE")

        # 3. Synchronize dynamically expanded boundaries to active position (Midline & TP remain fixed at entry like backtester)
        if self.active_position is not None and self.strategy.range_info:
            self.active_position.upper_boundary = float(self.strategy.range_info["upper_boundary"])
            self.active_position.lower_boundary = float(self.strategy.range_info["lower_boundary"])

        # 4. Range Breakout Exit or PA_CLOSE Entry
        if signal == "BREAKOUT":
            if self.active_position is not None:
                logger.info(f"[BREAKOUT EXIT] Structural range breakout detected. Closing position #{self.active_position.ticket} (RANGE_BREAKOUT).")
                self.close_active_position(reason="RANGE_BREAKOUT")
        elif signal in ("LONG", "SHORT") and self.active_position is None:
            self._execute_signal_entry(signal, candle, current_atr)

    def check_position_status(self):
        """
        Actively monitors open position for:
        1. Broker-side execution (SL or TP hit directly on server).
        2. Real-time Midline Take-Profit touch.
        3. Real-time Stop Loss touch.
        Respects close cooldown to prevent repeated close spam.
        """
        if self.active_position is None:
            return

        pos = self.active_position

        # Check if position is still open in MT5 terminal
        mt5_pos = self.mt5_exec.get_open_position_by_ticket(pos.ticket)
        if mt5_pos is None:
            # Position was closed externally (SL or TP executed by broker)
            self._close_attempt_count = 0
            self._close_cooldown_until = 0.0
            self._handle_external_close(pos)
            return

        # If we already exhausted retries or are in cooldown, skip close attempts
        if self._close_attempt_count >= self._close_max_retries:
            return
        if time.time() < self._close_cooldown_until:
            return

        # Match backtester: Do not evaluate exits on the exact entry candle
        forming_candle_idx = self.current_candle_index + 1
        if pos.entry_idx >= forming_candle_idx:
            return

        tick = self.mt5_exec.get_latest_tick()
        if tick is None:
            return

        # Real-time Midline Take-Profit Exit:
        # LONG closes at Bid >= midline; SHORT closes at Ask <= midline
        if pos.side == "LONG":
            if tick.bid >= pos.midline:
                logger.info(f"[MIDLINE TOUCH] Bid ({tick.bid:.2f}) reached Midline ({pos.midline:.2f}). Closing LONG position #{pos.ticket}.")
                self.close_active_position(reason="TAKE_PROFIT_MIDLINE")
                return
            elif tick.bid <= pos.sl:
                logger.info(f"[STOP LOSS TOUCH] Bid ({tick.bid:.2f}) touched SL ({pos.sl:.2f}). Closing LONG position #{pos.ticket}.")
                self.close_active_position(reason="STOP_LOSS")
                return

        elif pos.side == "SHORT":
            if tick.ask <= pos.midline:
                logger.info(f"[MIDLINE TOUCH] Ask ({tick.ask:.2f}) reached Midline ({pos.midline:.2f}). Closing SHORT position #{pos.ticket}.")
                self.close_active_position(reason="TAKE_PROFIT_MIDLINE")
                return
            elif tick.ask >= pos.sl:
                logger.info(f"[STOP LOSS TOUCH] Ask ({tick.ask:.2f}) touched SL ({pos.sl:.2f}). Closing SHORT position #{pos.ticket}.")
                self.close_active_position(reason="STOP_LOSS")
                return

    def _handle_external_close(self, pos: LivePosition):
        """
        Handles trade closure when MT5 broker executed SL or TP.
        """
        deals = self.mt5_exec.get_position_deals(pos.ticket)
        exit_price = pos.midline
        pnl_usd = 0.0

        if deals and len(deals) > 1:
            out_deal = deals[-1]
            exit_price = float(out_deal.price)
            pnl_usd = float(out_deal.profit + out_deal.commission + out_deal.swap)

        new_balance = self.mt5_exec.get_account_balance()
        reason = "BROKER_TAKE_PROFIT" if pnl_usd > 0 else "BROKER_STOP_LOSS"

        logger.info(
            f"[POSITION CLOSED] #{pos.trade_id} {pos.side} exited {pos.size:.2f} lots at {exit_price:.2f} "
            f"({reason}) | PnL: ${pnl_usd:.2f} | Balance: ${new_balance:.2f}"
        )
        self.active_position = None

    def _execute_signal_entry(self, side: str, candle: Candle, current_atr: float):
        """
        Executes market order on closed candle signal (used when entry_style == 'pa_close').
        Strictly mirrors backtester open_position behavior.
        """
        if self.active_position is not None or getattr(self, "_order_in_flight", False):
            return
        if not self.strategy.range_info:
            return

        ri = self.strategy.range_info
        tick = self.mt5_exec.get_latest_tick()
        if tick is None:
            return

        balance = self.mt5_exec.get_account_balance()
        now_ts = pd.to_datetime(tick.time, unit="s", utc=True)

        params = self.risk_manager.calculate_order_parameters(
            side=side,
            current_bid=tick.bid,
            current_ask=tick.ask,
            range_info=ri,
            current_atr=current_atr,
            balance=balance,
            contract_size=self.mt5_exec.contract_size,
            volume_step=self.mt5_exec.volume_step,
            min_volume=self.mt5_exec.min_volume,
            max_volume=self.mt5_exec.max_volume,
            leverage=self.mt5_exec.leverage,
            digits=self.mt5_exec.digits,
        )
        if params is None:
            return

        self._order_in_flight = True
        try:
            success, res = self.mt5_exec.execute_market_order(
                side=side,
                volume=params["size"],
                sl=params["sl"],
                tp=params["tp"],
                comment=f"TR_{ri.get('event_id', '')}",
            )
        finally:
            self._order_in_flight = False

        if success:
            self.trade_counter += 1
            ticket = res.order if res else 0
            ri["last_entry_side"] = side
            ri["last_entry_bar"] = candle.index
            self._close_attempt_count = 0
            self._close_cooldown_until = 0.0

            self.active_position = LivePosition(
                ticket=ticket,
                trade_id=self.trade_counter,
                symbol=self.symbol,
                side=side,
                entry_time=now_ts,
                entry_price=res.price,
                sl=params["sl"],
                tp=params["tp"],
                size=params["size"],
                risk_points=params["risk_points"],
                entry_idx=candle.index,
                event_id=ri.get("event_id", f"EVT_{self.trade_counter}"),
                midline=params["midline"],
                opposite_boundary=params["opposite_boundary"],
                upper_boundary=params["upper_boundary"],
                lower_boundary=params["lower_boundary"],
                range_detection_time=ri.get("detection_time"),
                equity_at_entry=round(balance, 2),
                risk_usd=round(params["actual_risk"], 4),
                margin_required=round(params["margin_required"], 2),
            )

            capped_notice = (
                f" [Capped from {params['raw_size']:.4f} to {params['size']:.2f} lots (Cap: {params['max_allowed_lots']:.4f})]"
                if params["raw_size"] > params["max_allowed_lots"]
                else ""
            )
            sl_mode_info = (
                f"RangeWidthx{self.risk_manager.sl_width_multiplier:.2f} (Dist: {params['sl_dist']:.2f})"
                if self.risk_manager.sl_mode == "range_width"
                else f"ATRx{self.risk_manager.sl_atr_multiplier:.2f} (Dist: {params['sl_dist']:.2f})"
            )

            logger.info(
                f"[ORDER EXEC] {side} #{self.trade_counter} Filled at {res.price:.2f} | "
                f"Balance: ${balance:.2f} | Size: {params['size']:.2f} lots{capped_notice} | "
                f"SL: {params['sl']:.2f} (RiskPts: {params['risk_points']:.2f}, Mode: {sl_mode_info}) | "
                f"TP: {params['tp']:.2f} | Risk: ${params['actual_risk']:.2f}"
            )

    def close_active_position(self, reason: str):
        """
        Closes current active position via MT5 market order and logs the exact closing metrics.
        Never marks position as closed unless MT5 confirms it is no longer open.
        """
        if self.active_position is None:
            return

        pos = self.active_position

        # Check actual position status in MT5 before attempting close
        mt5_pos = self.mt5_exec.get_open_position_by_ticket(pos.ticket)
        if mt5_pos is None:
            self._close_attempt_count = 0
            self._close_cooldown_until = 0.0
            self._handle_external_close(pos)
            return

        self._close_attempt_count += 1

        success, result = self.mt5_exec.close_position_market(pos, reason=reason)
        if not success:
            # Set cooldown so we don't spam MT5 every poll cycle
            self._close_cooldown_until = time.time() + 5.0
            logger.error(
                f"[CLOSE ERROR] Failed to close position #{pos.ticket} "
                f"(attempt {self._close_attempt_count}/{self._close_max_retries}). "
                f"Retrying in 5s."
            )
            return

        # Verification: ensure MT5 actually closed the position
        time.sleep(0.5)
        remaining = self.mt5_exec.get_open_position_by_ticket(pos.ticket)
        if remaining is not None:
            self._close_cooldown_until = time.time() + 5.0
            logger.error(
                f"[CLOSE UNVERIFIED] Close returned success, but position #{pos.ticket} "
                f"is still open in MT5. Not marking closed. "
                f"(attempt {self._close_attempt_count}/{self._close_max_retries})"
            )
            return

        exit_price = result.price if (result and result.price > 0) else pos.midline

        # Fetch actual realized profit from deal history
        deals = self.mt5_exec.get_position_deals(pos.ticket)
        pnl_usd = 0.0
        if deals and len(deals) > 1:
            out_deal = deals[-1]
            pnl_usd = float(out_deal.profit + out_deal.commission + out_deal.swap)
        else:
            # Fallback estimation if deal history not updated yet
            if pos.side == "LONG":
                points = exit_price - pos.entry_price
            else:
                points = pos.entry_price - exit_price
            pnl_usd = (points * pos.size * self.mt5_exec.contract_size) - COMMISSION_PER_TRADE

        new_balance = self.mt5_exec.get_account_balance()
        logger.info(
            f"[POSITION CLOSED] #{pos.trade_id} {pos.side} exited {pos.size:.2f} lots at {exit_price:.2f} "
            f"({reason}) | PnL: ${pnl_usd:.2f} | Balance: ${new_balance:.2f}"
        )
        self.active_position = None
        self._close_attempt_count = 0
        self._close_cooldown_until = 0.0

    @staticmethod
    def calculate_entry_tolerance(atr: float, upper_or_width: float, lower: Optional[float] = None) -> float:
        """
        Calculates dynamic entry tolerance:
        entry_tolerance = min(ATR * 0.50, range_width * 0.10)
        Supports both calculate_entry_tolerance(atr, upper, lower) and calculate_entry_tolerance(atr, range_width).
        """
        if lower is not None:
            range_width = upper_or_width - lower
        else:
            range_width = upper_or_width

        if range_width <= 0 or np.isnan(atr) or atr <= 0:
            return 0.0
        return min(atr * 0.50, range_width * 0.10)

    def get_touch_zones(
        self, current_atr: Optional[float] = None
    ) -> Optional[Tuple[float, Tuple[float, float], Tuple[float, float]]]:
        """
        Calculates and returns (entry_tolerance, buy_zone, sell_zone) where:
        - entry_tolerance = min(ATR * 0.50, range_width * 0.10)
        - buy_zone = (range_low, range_low + entry_tolerance)
        - sell_zone = (range_high - entry_tolerance, range_high)
        Returns None if not in active trading range or invalid data.
        """
        if self.strategy.state != self.strategy.STATE_TRADING_RANGE or not self.strategy.range_info:
            return None

        ri = self.strategy.range_info
        upper = float(ri["upper_boundary"])
        lower = float(ri["lower_boundary"])
        if upper <= lower:
            return None

        if current_atr is None:
            current_atr = self.window.calculate_atr()

        if np.isnan(current_atr) or current_atr <= 0:
            return None

        entry_tolerance = self.calculate_entry_tolerance(current_atr, upper, lower)
        buy_zone = (lower, lower + entry_tolerance)
        sell_zone = (upper - entry_tolerance, upper)
        return entry_tolerance, buy_zone, sell_zone

    def check_touch_entry(self, current_candle: Optional[Candle] = None):
        """
        Evaluates real-time price touch against the dynamic ATR-based touch zones around
        Trading Range boundaries:
        - entry_tolerance = min(ATR * 0.50, range_width * 0.10)
        - BUY entry zone: [range_low, range_low + entry_tolerance]
        - SELL entry zone: [range_high - entry_tolerance, range_high]
        Executes a market order when price enters the respective touch zone in STATE 2.
        """
        if self.active_position is not None:
            return

        if getattr(self, "_order_in_flight", False):
            return

        if self.strategy.entry_style != "touch":
            return

        if self.strategy.state != self.strategy.STATE_TRADING_RANGE or not self.strategy.range_info:
            return

        ri = self.strategy.range_info
        # In live trading, ticks after current_candle_index belong to forming candle (current_candle_index + 1)
        forming_candle_idx = self.current_candle_index + 1
        bars_since_detection = forming_candle_idx - ri["detection_idx"]

        # Filter: Do not enter on the exact establishment bar (allow starting on next bar, matching backtester)
        if bars_since_detection < 1:
            return

        # Cooldown: Minimum 2 bars between consecutive entries (matching backtester)
        if forming_candle_idx - ri.get("last_entry_bar", -999) < 2:
            return

        tick = self.mt5_exec.get_latest_tick()
        if tick is None:
            return

        upper = float(ri["upper_boundary"])
        lower = float(ri["lower_boundary"])
        midline = (upper + lower) / 2.0
        current_atr = self.window.calculate_atr()
        balance = self.mt5_exec.get_account_balance()
        now_ts = pd.to_datetime(tick.time, unit="s", utc=True)

        # Retrieve appropriate candle High/Low data
        candle_high = float(current_candle.high) if current_candle is not None else None
        candle_low = float(current_candle.low) if current_candle is not None else None
        if candle_high is None or candle_low is None:
            try:
                rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M5, 0, 1)
                if rates is not None and len(rates) > 0:
                    candle_high = float(rates[-1]["high"])
                    candle_low = float(rates[-1]["low"])
            except Exception:
                pass
        if candle_high is None:
            candle_high = max(tick.bid, tick.ask)
        if candle_low is None:
            candle_low = min(tick.bid, tick.ask)

        # Check for inverted/distorted structure safeguards
        range_width = upper - lower
        if range_width <= 0 or np.isnan(current_atr) or current_atr <= 0:
            if tick.bid <= midline and ri.get("last_entry_side") != "SHORT":
                logger.warning(
                    f"[ENTRY REJECTED] SHORT trigger Bid ({tick.bid:.2f}) is at or below "
                    f"Midline ({midline:.2f}). Only entries toward Midline allowed."
                )
            if tick.ask >= midline and ri.get("last_entry_side") != "LONG":
                logger.warning(
                    f"[ENTRY REJECTED] LONG trigger Ask ({tick.ask:.2f}) is at or above "
                    f"Midline ({midline:.2f}). Only entries toward Midline allowed."
                )
            return

        # Dynamic Entry Tolerance: min(ATR * 0.35, range_width * 0.10)
        entry_tolerance = self.calculate_entry_tolerance(current_atr, upper, lower)
        buy_zone = (lower, lower + entry_tolerance)
        sell_zone = (upper - entry_tolerance, upper)

        # Store latest recalculated zones in range memory
        ri["entry_tolerance"] = entry_tolerance
        ri["buy_zone"] = buy_zone
        ri["sell_zone"] = sell_zone

        # Distance to BUY zone (live buying execution price is Ask)
        if tick.ask > buy_zone[1]:
            dist_to_buy_zone = tick.ask - buy_zone[1]
        elif tick.ask < buy_zone[0]:
            dist_to_buy_zone = buy_zone[0] - tick.ask
        else:
            dist_to_buy_zone = 0.0

        # Distance to SELL zone (live selling execution price is Bid)
        if tick.bid < sell_zone[0]:
            dist_to_sell_zone = sell_zone[0] - tick.bid
        elif tick.bid > sell_zone[1]:
            dist_to_sell_zone = tick.bid - sell_zone[1]
        else:
            dist_to_sell_zone = 0.0

        eps = 1e-6

        # -------------------------------------------------------------
        # TOUCH ZONE TRIGGER: SHORT (At Upper Boundary Touch Zone)
        # Matches backtester: candle.high >= upper_touch_zone (open-ended above upper_touch_zone)
        # -------------------------------------------------------------
        in_sell_zone = (tick.bid >= sell_zone[0] - eps)
        candle_touched_sell = (candle_high >= sell_zone[0] - eps)

        if in_sell_zone:
            if tick.bid <= midline:
                logger.warning(
                    f"[ENTRY REJECTED] SHORT trigger Bid ({tick.bid:.2f}) is at or below "
                    f"Midline ({midline:.2f}). Only entries toward Midline allowed."
                )
                return

            params = self.risk_manager.calculate_order_parameters(
                side="SHORT",
                current_bid=tick.bid,
                current_ask=tick.ask,
                range_info=ri,
                current_atr=current_atr,
                balance=balance,
                contract_size=self.mt5_exec.contract_size,
                volume_step=self.mt5_exec.volume_step,
                min_volume=self.mt5_exec.min_volume,
                max_volume=self.mt5_exec.max_volume,
                leverage=self.mt5_exec.leverage,
                digits=self.mt5_exec.digits,
            )
            if params is None:
                return

            logger.info(
                f"[TRIGGER] Dynamic Touch Zone Entry (SHORT) on {now_ts} | "
                f"Reason: Bid ({tick.bid:.2f}) entered SELL zone [{sell_zone[0]:.2f}, {sell_zone[1]:.2f}] | "
                f"TR High: {upper:.2f}, TR Low: {lower:.2f} | ATR: {current_atr:.2f} | "
                f"Entry Tol: {entry_tolerance:.2f} | SELL Zone: [{sell_zone[0]:.2f}, {sell_zone[1]:.2f}] (Dist: 0.00) | "
                f"BUY Zone: [{buy_zone[0]:.2f}, {buy_zone[1]:.2f}] (Dist: {dist_to_buy_zone:.2f}) | "
                f"Bid: {tick.bid:.2f}, Ask: {tick.ask:.2f} -> SHORT Filled at {tick.bid:.2f}"
            )

            self._order_in_flight = True
            try:
                success, res = self.mt5_exec.execute_market_order(
                    side="SHORT",
                    volume=params["size"],
                    sl=params["sl"],
                    tp=params["tp"],
                    comment=f"TR_{ri.get('event_id', '')}",
                )
            finally:
                self._order_in_flight = False

            if success:
                self.trade_counter += 1
                ticket = res.order if res else 0
                ri["last_entry_side"] = "SHORT"
                ri["last_entry_bar"] = forming_candle_idx
                self._close_attempt_count = 0
                self._close_cooldown_until = 0.0

                self.active_position = LivePosition(
                    ticket=ticket,
                    trade_id=self.trade_counter,
                    symbol=self.symbol,
                    side="SHORT",
                    entry_time=now_ts,
                    entry_price=res.price,
                    sl=params["sl"],
                    tp=params["tp"],
                    size=params["size"],
                    risk_points=params["risk_points"],
                    entry_idx=forming_candle_idx,
                    event_id=ri.get("event_id", f"EVT_{self.trade_counter}"),
                    midline=params["midline"],
                    opposite_boundary=params["opposite_boundary"],
                    upper_boundary=params["upper_boundary"],
                    lower_boundary=params["lower_boundary"],
                    range_detection_time=ri.get("detection_time"),
                    equity_at_entry=round(balance, 2),
                    risk_usd=round(params["actual_risk"], 4),
                    margin_required=round(params["margin_required"], 2),
                )

                capped_notice = (
                    f" [Capped from {params['raw_size']:.4f} to {params['size']:.2f} lots (Cap: {params['max_allowed_lots']:.4f})]"
                    if params["raw_size"] > params["max_allowed_lots"]
                    else ""
                )
                sl_mode_info = (
                    f"RangeWidthx{self.risk_manager.sl_width_multiplier:.2f} (Dist: {params['sl_dist']:.2f})"
                    if self.risk_manager.sl_mode == "range_width"
                    else f"ATRx{self.risk_manager.sl_atr_multiplier:.2f} (Dist: {params['sl_dist']:.2f})"
                )

                logger.info(
                    f"[ORDER EXEC] SHORT #{self.trade_counter} Filled at {res.price:.2f} | "
                    f"Balance: ${balance:.2f} | Size: {params['size']:.2f} lots{capped_notice} | "
                    f"SL: {params['sl']:.2f} (RiskPts: {params['risk_points']:.2f}, Mode: {sl_mode_info}) | "
                    f"TP: {params['tp']:.2f} | Risk: ${params['actual_risk']:.2f}"
                )
                self.plot_live_structure(stage=f"TRADE_{self.trade_counter:02d}_SHORT_ENTRY")
            return

        # -------------------------------------------------------------
        # TOUCH ZONE TRIGGER: LONG (At Lower Boundary Touch Zone)
        # Matches backtester: candle.low <= lower_touch_zone (open-ended below lower_touch_zone)
        # -------------------------------------------------------------
        in_buy_zone = (tick.ask <= buy_zone[1] + eps)
        candle_touched_buy = (candle_low <= buy_zone[1] + eps)

        if in_buy_zone:
            if tick.ask >= midline:
                logger.warning(
                    f"[ENTRY REJECTED] LONG trigger Ask ({tick.ask:.2f}) is at or above "
                    f"Midline ({midline:.2f}). Only entries toward Midline allowed."
                )
                return

            params = self.risk_manager.calculate_order_parameters(
                side="LONG",
                current_bid=tick.bid,
                current_ask=tick.ask,
                range_info=ri,
                current_atr=current_atr,
                balance=balance,
                contract_size=self.mt5_exec.contract_size,
                volume_step=self.mt5_exec.volume_step,
                min_volume=self.mt5_exec.min_volume,
                max_volume=self.mt5_exec.max_volume,
                leverage=self.mt5_exec.leverage,
                digits=self.mt5_exec.digits,
            )
            if params is None:
                return

            logger.info(
                f"[TRIGGER] Dynamic Touch Zone Entry (LONG) on {now_ts} | "
                f"Reason: Ask ({tick.ask:.2f}) entered BUY zone [{buy_zone[0]:.2f}, {buy_zone[1]:.2f}] | "
                f"TR High: {upper:.2f}, TR Low: {lower:.2f} | ATR: {current_atr:.2f} | "
                f"Entry Tol: {entry_tolerance:.2f} | BUY Zone: [{buy_zone[0]:.2f}, {buy_zone[1]:.2f}] (Dist: 0.00) | "
                f"SELL Zone: [{sell_zone[0]:.2f}, {sell_zone[1]:.2f}] (Dist: {dist_to_sell_zone:.2f}) | "
                f"Bid: {tick.bid:.2f}, Ask: {tick.ask:.2f} -> LONG Filled at {tick.ask:.2f}"
            )

            self._order_in_flight = True
            try:
                success, res = self.mt5_exec.execute_market_order(
                    side="LONG",
                    volume=params["size"],
                    sl=params["sl"],
                    tp=params["tp"],
                    comment=f"TR_{ri.get('event_id', '')}",
                )
            finally:
                self._order_in_flight = False

            if success:
                self.trade_counter += 1
                ticket = res.order if res else 0
                ri["last_entry_side"] = "LONG"
                ri["last_entry_bar"] = forming_candle_idx
                self._close_attempt_count = 0
                self._close_cooldown_until = 0.0

                self.active_position = LivePosition(
                    ticket=ticket,
                    trade_id=self.trade_counter,
                    symbol=self.symbol,
                    side="LONG",
                    entry_time=now_ts,
                    entry_price=res.price,
                    sl=params["sl"],
                    tp=params["tp"],
                    size=params["size"],
                    risk_points=params["risk_points"],
                    entry_idx=forming_candle_idx,
                    event_id=ri.get("event_id", f"EVT_{self.trade_counter}"),
                    midline=params["midline"],
                    opposite_boundary=params["opposite_boundary"],
                    upper_boundary=params["upper_boundary"],
                    lower_boundary=params["lower_boundary"],
                    range_detection_time=ri.get("detection_time"),
                    equity_at_entry=round(balance, 2),
                    risk_usd=round(params["actual_risk"], 4),
                    margin_required=round(params["margin_required"], 2),
                )

                capped_notice = (
                    f" [Capped from {params['raw_size']:.4f} to {params['size']:.2f} lots (Cap: {params['max_allowed_lots']:.4f})]"
                    if params["raw_size"] > params["max_allowed_lots"]
                    else ""
                )
                sl_mode_info = (
                    f"RangeWidthx{self.risk_manager.sl_width_multiplier:.2f} (Dist: {params['sl_dist']:.2f})"
                    if self.risk_manager.sl_mode == "range_width"
                    else f"ATRx{self.risk_manager.sl_atr_multiplier:.2f} (Dist: {params['sl_dist']:.2f})"
                )

                logger.info(
                    f"[ORDER EXEC] LONG #{self.trade_counter} Filled at {res.price:.2f} | "
                    f"Balance: ${balance:.2f} | Size: {params['size']:.2f} lots{capped_notice} | "
                    f"SL: {params['sl']:.2f} (RiskPts: {params['risk_points']:.2f}, Mode: {sl_mode_info}) | "
                    f"TP: {params['tp']:.2f} | Risk: ${params['actual_risk']:.2f}"
                )
                self.plot_live_structure(stage=f"TRADE_{self.trade_counter:02d}_LONG_ENTRY")
            return

    def perform_checkin(self):
        """
        Periodic operational check-in / heartbeat (default: every 30 seconds):
        - Logs market state, real-time bid/ask, ATR, active boundaries, and distance to triggers.
        - Verifies MT5 connection health and account equity.
        - Reports open position status, unrealized PnL, and distance to Midline TP / SL.
        """
        acc = mt5.account_info()
        balance = float(acc.balance) if acc else 0.0
        equity = float(acc.equity) if acc else 0.0
        tick = self.mt5_exec.get_latest_tick()
        if tick is None:
            logger.warning(f"[CHECK-IN {int(self.checkin_interval)}s] No real-time tick available from MT5.")
            return

        current_atr = self.window.calculate_atr()
        now_utc = datetime.now(timezone.utc)
        time_str = now_utc.strftime("%H:%M:%S")

        sec_in_bar = int(now_utc.timestamp()) % TIMEFRAME_SECONDS
        sec_remaining = TIMEFRAME_SECONDS - sec_in_bar

        state_names = {
            0: "STATE 0 (Searching Spike)",
            1: "STATE 1 (Retracement / Boundaries)",
            2: "STATE 2 (Trading Range)",
        }
        st_name = state_names.get(self.strategy.state, "Unknown")

        # Open Position or Flat summary
        if self.active_position is not None:
            pos = self.active_position
            if pos.side == "LONG":
                unrealized = (tick.bid - pos.entry_price) * pos.size * self.mt5_exec.contract_size
                dist_midline = pos.midline - tick.bid
                dist_sl = tick.bid - pos.sl
            else:
                unrealized = (pos.entry_price - tick.ask) * pos.size * self.mt5_exec.contract_size
                dist_midline = tick.ask - pos.midline
                dist_sl = pos.sl - tick.ask
            pos_info = (
                f"IN POS: #{pos.ticket} {pos.side} {pos.size:.2f} lots @ {pos.entry_price:.2f} | "
                f"PnL: ${unrealized:+.2f} | Dist to Midline: {dist_midline:.2f} | Dist to SL: {dist_sl:.2f}"
            )
        else:
            pos_info = "Flat"

        # Market structure summary
        if self.strategy.state == self.strategy.STATE_TRADING_RANGE and self.strategy.range_info:
            ri = self.strategy.range_info
            upper = float(ri["upper_boundary"])
            lower = float(ri["lower_boundary"])
            midline = (upper + lower) / 2.0
            range_width = upper - lower
            entry_tolerance = self.calculate_entry_tolerance(current_atr, range_width)
            buy_zone = (lower, lower + entry_tolerance)
            sell_zone = (upper - entry_tolerance, upper)

            # Distance to BUY zone (evaluated using Ask)
            if tick.ask > buy_zone[1]:
                dist_to_buy_zone = tick.ask - buy_zone[1]
            elif tick.ask < buy_zone[0]:
                dist_to_buy_zone = buy_zone[0] - tick.ask
            else:
                dist_to_buy_zone = 0.0

            # Distance to SELL zone (evaluated using Bid)
            if tick.bid < sell_zone[0]:
                dist_to_sell_zone = sell_zone[0] - tick.bid
            elif tick.bid > sell_zone[1]:
                dist_to_sell_zone = tick.bid - sell_zone[1]
            else:
                dist_to_sell_zone = 0.0

            structure_str = (
                f"TR: [Low={lower:.2f}, High={upper:.2f}] Mid={midline:.2f} | "
                f"Tol: {entry_tolerance:.2f} | "
                f"BUY Zone: [{buy_zone[0]:.2f}, {buy_zone[1]:.2f}] (Dist: {dist_to_buy_zone:.2f}) | "
                f"SELL Zone: [{sell_zone[0]:.2f}, {sell_zone[1]:.2f}] (Dist: {dist_to_sell_zone:.2f})"
            )
        elif self.strategy.state == self.strategy.STATE_ESTABLISHING_BOUNDARIES and self.strategy.spike_info:
            si = self.strategy.spike_info
            structure_str = f"Spike: {si['direction']} ({si['event_id']}) Move: {si['move']:.2f}"
        else:
            structure_str = f"Scanning for M5 Spike (>= {MIN_SPIKE_ATR}x ATR)"

        logger.info(
            f"[CHECK-IN {int(self.checkin_interval)}s] {time_str} UTC | {st_name} | "
            f"Bid: {tick.bid:.2f} / Ask: {tick.ask:.2f} | ATR: {current_atr:.2f} | "
            f"Bar Close in: {sec_remaining}s | {structure_str} | {pos_info} | "
            f"Bal: ${balance:.2f} (Eq: ${equity:.2f})"
        )

    def run(self):
        """
        Main execution loop for live trading / forward testing.
        """
        logger.info("=" * 65)
        logger.info("APEX-XAU LIVE / FORWARD TESTING BOT STARTING")
        logger.info(f"Symbol:           {self.symbol} ({TIMEFRAME_NAME})")
        logger.info(f"Magic Number:     {self.magic}")
        logger.info(f"Check-In Cadence: Every {self.checkin_interval:.0f} seconds")
        logger.info(f"Risk Mode:        {self.risk_manager.risk_mode}")
        if self.risk_manager.risk_mode == "equity_pct":
            logger.info(f"Dynamic Risk:     {self.risk_manager.risk_pct:.2f}% of current equity")
        else:
            logger.info(f"Fixed Risk:       ${self.risk_manager.risk_per_trade:.2f} per trade")
        logger.info(f"SL Mode:          {self.risk_manager.sl_mode}")
        if self.risk_manager.sl_mode == "range_width":
            logger.info(f"SL Width Mult:    {self.risk_manager.sl_width_multiplier:.2f}x Range Width")
        else:
            logger.info(f"SL ATR Mult:      {self.risk_manager.sl_atr_multiplier:.2f}x ATR")
        logger.info(f"Max Lot Scaling:  {self.risk_manager.lots_per_100:.2f} lots per $100 balance")
        logger.info(f"Entry Style:      {self.strategy.entry_style} (Dynamic ATR-based touch zone)")
        logger.info(f"Exit Style:       midline (100% position close)")
        logger.info(f"Min Range Width:  {self.strategy.min_range_width:.2f} ATR")
        logger.info(f"Min Retrace:      {self.strategy.min_retrace_ratio*100:.1f}%")
        logger.info(f"Breakout Buffer:  {self.strategy.breakout_buffer_atr:.2f} ATR")
        logger.info(f"Expansion Buffer: {self.strategy.expansion_buffer_atr:.2f} ATR")
        logger.info("=" * 65)

        if not self.mt5_exec.initialize():
            logger.error("[ERROR] Could not initialize MT5. Terminating.")
            return

        self._install_mt5_overlay_indicator()

        if not self.warmup():
            logger.error("[ERROR] Warmup failed. Terminating.")
            self.mt5_exec.shutdown()
            return

        self.is_running = True
        self.last_checkin_time = time.time()
        logger.info("[BOT LIVE] Event-driven streaming loop running. Press Ctrl+C to stop gracefully.")

        # Initial check-in immediately upon starting
        self.perform_checkin()

        try:
            while self.is_running:
                now_time = time.time()

                # 1. Periodic 30-second check-in heartbeat
                if now_time - self.last_checkin_time >= self.checkin_interval:
                    self.perform_checkin()
                    self.last_checkin_time = now_time

                # 2. Connection check
                terminal = mt5.terminal_info()
                if terminal is None or not terminal.connected:
                    logger.warning("[MT5] Terminal disconnected from broker. Waiting for reconnect...")
                    time.sleep(3.0)
                    continue

                # 3. Check for newly closed M5 candle
                # copy_rates_from_pos(..., 0, 5) -> rates[-1] is currently forming bar, rates[-2] is last closed bar
                rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M5, 0, 5)
                if rates is not None and len(rates) >= 2:
                    closed_bar_time = int(rates[-2]["time"])
                    if closed_bar_time > self.last_closed_bar_time:
                        self.on_closed_candle(rates[-2])
                        self.last_closed_bar_time = closed_bar_time

                # 4. Active position monitoring (Midline TP / SL / Broker sync)
                self.check_position_status()

                # 5. Touch limit entry check on real-time price
                self.check_touch_entry()

                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            logger.info("\n[SHUTDOWN] KeyboardInterrupt received. Gracefully stopping...")
        except Exception as exc:
            logger.exception(f"[UNEXPECTED ERROR] {exc}")
        finally:
            self.shutdown()

    def shutdown(self):
        """
        Graceful shutdown protocol.
        """
        self.is_running = False
        balance = self.mt5_exec.get_account_balance()
        equity = self.mt5_exec.get_account_equity()

        logger.info("=" * 65)
        logger.info("APEX-XAU LIVE BOT SHUTDOWN SUMMARY")
        logger.info(f"Final Balance: ${balance:.2f}")
        logger.info(f"Final Equity:  ${equity:.2f}")
        if self.active_position is not None:
            logger.warning(
                f"Position #{self.active_position.ticket} ({self.active_position.side} {self.active_position.size} lots) "
                f"remains active in MT5 terminal with SL: {self.active_position.sl:.2f} and TP: {self.active_position.tp:.2f}."
            )
        logger.info("=" * 65)
        self.mt5_exec.shutdown()


# =====================================================================
# CLI ARGUMENT PARSER
# =====================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Live / Forward Testing Trading Bot for XAUUSD M5 Spike -> Trading Range Market Cycle"
    )
    parser.add_argument("--symbol", type=str, default=DEFAULT_SYMBOL, help=f"Symbol to trade (default: {DEFAULT_SYMBOL})")
    parser.add_argument("--magic", type=int, default=DEFAULT_MAGIC, help=f"MT5 Magic Number (default: {DEFAULT_MAGIC})")
    parser.add_argument(
        "--risk-mode",
        choices=["equity_pct", "fixed_usd"],
        default=DEFAULT_RISK_MODE,
        help="Risk mode: 'equity_pct' for dynamic sizing or 'fixed_usd' for fixed dollar risk",
    )
    parser.add_argument(
        "--risk-pct",
        type=float,
        default=DEFAULT_RISK_PCT,
        help=f"Percentage of current equity to risk per trade (default: {DEFAULT_RISK_PCT}%%)",
    )
    parser.add_argument(
        "--risk-per-trade",
        type=float,
        default=RISK_PER_TRADE_USD,
        help=f"Fixed USD risk per trade when risk-mode is fixed_usd (default: {RISK_PER_TRADE_USD})",
    )
    parser.add_argument(
        "--lots-per-100",
        "--max-lot-scaling",
        type=float,
        default=LOTS_PER_100_BALANCE,
        dest="lots_per_100",
        help=f"Max lot scaling: allowed lots per $100 balance (default: {LOTS_PER_100_BALANCE})",
    )
    parser.add_argument(
        "--sl-mode",
        choices=["range_width", "atr"],
        default=DEFAULT_SL_MODE,
        help="Stop loss mode: range_width (full range width beyond boundary) or atr",
    )
    parser.add_argument(
        "--sl-width-mult",
        type=float,
        default=SL_WIDTH_MULTIPLIER,
        help=f"Range width multiplier for SL when in 'range_width' mode (default: {SL_WIDTH_MULTIPLIER})",
    )
    parser.add_argument(
        "--sl-atr-mult",
        type=float,
        default=SL_ATR_MULTIPLIER,
        help=f"ATR multiplier for SL when in 'atr' mode (default: {SL_ATR_MULTIPLIER})",
    )
    parser.add_argument(
        "--entry-style",
        type=str,
        choices=["pa_close", "touch"],
        default=DEFAULT_ENTRY_STYLE,
        help="Entry execution style: touch (limit entry at Trading Range boundary) or pa_close",
    )
    parser.add_argument(
        "--pa-confirm",
        action="store_true",
        help="Require strict Price Action confirmation (pinbar or engulfing) on trigger candle in pa_close mode",
    )
    parser.add_argument(
        "--rr-ratio",
        type=float,
        default=RR_RATIO,
        help=f"Risk:Reward ratio fallback for Take Profit (default: {RR_RATIO})",
    )
    parser.add_argument(
        "--exit-style",
        type=str,
        choices=["midline"],
        default=DEFAULT_EXIT_STYLE,
        help="Exit style: 'midline' closes 100%% of position at range midline",
    )
    parser.add_argument(
        "--min-range-width",
        type=float,
        default=DEFAULT_MIN_RANGE_WIDTH,
        help=f"Minimum trading range width in ATR to trade STATE 2 (default: {DEFAULT_MIN_RANGE_WIDTH})",
    )
    parser.add_argument(
        "--min-retrace-ratio",
        type=float,
        default=MIN_RETRACE_RATIO,
        help=f"Minimum retracement of spike to establish Boundary B (default: {MIN_RETRACE_RATIO})",
    )
    parser.add_argument(
        "--breakout-buffer-atr",
        type=float,
        default=BREAKOUT_BUFFER_ATR,
        help=f"ATR buffer beyond boundary for candle close breakout (default: {BREAKOUT_BUFFER_ATR})",
    )
    parser.add_argument(
        "--expansion-buffer-atr",
        type=float,
        default=EXPANSION_BUFFER_ATR,
        help=f"ATR buffer for dynamic boundary expansion on rejection wicks (default: {EXPANSION_BUFFER_ATR})",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Tick polling interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--checkin-interval",
        type=float,
        default=DEFAULT_CHECKIN_INTERVAL,
        help=f"Periodic operational check-in / heartbeat interval in seconds (default: {DEFAULT_CHECKIN_INTERVAL}s)",
    )
    parser.add_argument(
        "--warmup-bars",
        type=int,
        default=ROLLING_WINDOW_SIZE,
        help=f"Historical M5 bars to fetch for state machine warmup (default: {ROLLING_WINDOW_SIZE})",
    )
    return parser.parse_args()


# =====================================================================
# MAIN ENTRY POINT
# =====================================================================

def main():
    args = parse_args()

    bot = LiveEventDrivenBot(
        symbol=args.symbol,
        magic=args.magic,
        risk_mode=args.risk_mode,
        risk_pct=args.risk_pct,
        risk_per_trade=args.risk_per_trade,
        lots_per_100=args.lots_per_100,
        sl_mode=args.sl_mode,
        sl_atr_multiplier=args.sl_atr_mult,
        sl_width_multiplier=args.sl_width_mult,
        risk_reward_ratio=args.rr_ratio,
        entry_style=args.entry_style,
        exit_style=args.exit_style,
        pa_confirm=args.pa_confirm,
        min_range_width=args.min_range_width,
        min_retrace_ratio=args.min_retrace_ratio,
        breakout_buffer_atr=args.breakout_buffer_atr,
        expansion_buffer_atr=args.expansion_buffer_atr,
        poll_interval=args.poll_interval,
        checkin_interval=args.checkin_interval,
        warmup_bars=args.warmup_bars,
    )

    bot.run()


if __name__ == "__main__":
    main()
