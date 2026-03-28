//+------------------------------------------------------------------+
//| Blackjack Trade Copier EA                                        |
//| Confidential — Fund Intellectual Property                        |
//+------------------------------------------------------------------+
#property copyright "Blackjack Fund"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

CTrade trade;

// ── Settings ──────────────────────────────────────────────────────
input int    MagicNumber  = 20250101;
input int    PollMs       = 50;        // How often to check for signals (ms)
input int    Deviation    = 20;        // Max slippage in points
input bool   EnableLogs   = true;

// ── File paths (relative to MT5 Files folder) ─────────────────────
string SIGNAL_FILE   = "bj_signal.txt";
string RESPONSE_FILE = "bj_response.txt";
string HEARTBEAT_FILE = "bj_heartbeat.txt";

datetime lastHeartbeat = 0;

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(Deviation);
   trade.SetTypeFilling(ORDER_FILLING_IOC);

   Log("Blackjack EA initialized — polling for signals");
   WriteHeartbeat();
   EventSetMillisecondTimer(PollMs);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   Log("Blackjack EA stopped");
}

//+------------------------------------------------------------------+
//| Timer — check for signal file                                    |
//+------------------------------------------------------------------+
void OnTimer()
{
   // Write heartbeat every 2 seconds so backend knows EA is alive
   if (TimeCurrent() - lastHeartbeat >= 2)
   {
      WriteHeartbeat();
      lastHeartbeat = TimeCurrent();
   }

   // Check for signal file
   if (!FileIsExist(SIGNAL_FILE, FILE_COMMON))
      return;

   // Read signal
   int handle = FileOpen(SIGNAL_FILE, FILE_READ | FILE_TXT | FILE_COMMON | FILE_ANSI);
   if (handle == INVALID_HANDLE)
      return;

   string content = "";
   while (!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);

   // Delete signal file immediately to avoid re-processing
   FileDelete(SIGNAL_FILE, FILE_COMMON);

   Log("Signal received: " + content);
   ProcessSignal(content);
}

//+------------------------------------------------------------------+
//| Parse and execute signal                                         |
//+------------------------------------------------------------------+
void ProcessSignal(string raw)
{
   // Signal format: ACTION|SYMBOL|SIDE|SIZE|SL|TP|COMMENT
   // Actions: OPEN, CLOSE_ALL, FLATTEN
   string parts[];
   int count = StringSplit(raw, '|', parts);

   if (count < 1)
   {
      WriteResponse("ERROR|Invalid signal format: " + raw);
      return;
   }

   string action = parts[0];

   // ── OPEN order ────────────────────────────
   // Signal format: OPEN|SYMBOL|SIDE|SIZE|SL|TP|COMMENT|ORDERTYPE|PRICE
   // SIDE: BUY or SELL
   // ORDERTYPE: MARKET (default) or LIMIT
   // PRICE: limit price (only required for LIMIT orders)
   if (action == "OPEN" && count >= 7)
   {
      string symbol    = parts[1];
      string side      = parts[2];
      double size      = StringToDouble(parts[3]);
      double sl        = StringToDouble(parts[4]);
      double tp        = StringToDouble(parts[5]);
      string comment   = parts[6];
      string ord_type  = (count >= 8) ? parts[7] : "MARKET";
      double ord_price = (count >= 9) ? StringToDouble(parts[8]) : 0.0;

      bool ok = false;

      if (ord_type == "LIMIT")
      {
         // Place a pending limit order
         ENUM_ORDER_TYPE lim_type = (side == "BUY") ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_SELL_LIMIT;
         ok = trade.OrderOpen(symbol, lim_type, size, 0, ord_price, sl, tp, ORDER_TIME_GTC, 0, comment);
      }
      else if (side == "BUY")
      {
         double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
         ok = trade.Buy(size, symbol, ask, sl, tp, comment);
      }
      else if (side == "SELL")
      {
         double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
         ok = trade.Sell(size, symbol, bid, sl, tp, comment);
      }
      else
      {
         WriteResponse("ERROR|Unknown side: " + side);
         return;
      }

      if (ok)
      {
         ulong ticket = trade.ResultOrder();
         double fill  = trade.ResultPrice();

         // If SL was passed as 0, calculate from fill price
         double actual_sl = sl;
         if (sl == 0.0 && fill > 0.0)
         {
            double sl_dist = 17.60;  // XLTRADE fixed SL distance
            actual_sl = (side == "BUY") ? NormalizeDouble(fill - sl_dist, 2)
                                        : NormalizeDouble(fill + sl_dist, 2);
            // Modify the position to set the calculated SL
            if (ord_type != "LIMIT" && ticket > 0)
            {
               MqlTradeRequest mod_req = {};
               MqlTradeResult  mod_res = {};
               mod_req.action   = TRADE_ACTION_SLTP;
               mod_req.position = ticket;
               mod_req.sl       = actual_sl;
               mod_req.tp       = tp;
               OrderSend(mod_req, mod_res);
            }
         }

         WriteResponse("OK|OPEN|" + symbol + "|" + side + "|" +
                       DoubleToString(size, 2) + "|" +
                       DoubleToString(fill, 2) + "|" +
                       DoubleToString(actual_sl, 2) + "|" +
                       DoubleToString(tp, 2)   + "|" +
                       IntegerToString((int)ticket));
         Log("Order opened — ticket:" + IntegerToString((int)ticket) +
             " " + ord_type + " " + side + " " + DoubleToString(size,2) + " " + symbol +
             " @ " + DoubleToString(fill,2) + " SL:" + DoubleToString(actual_sl,2));
      }
      else
      {
         int    code = trade.ResultRetcode();
         string msg  = trade.ResultRetcodeDescription();
         WriteResponse("ERROR|Order failed: " + IntegerToString(code) + " " + msg);
         Log("Order failed: " + IntegerToString(code) + " " + msg);
      }
   }

   // ── FLATTEN: close all positions + cancel all orders ──
   else if (action == "FLATTEN")
   {
      string symbol  = (count >= 2) ? parts[1] : "";
      int closed     = 0;
      int cancelled  = 0;
      int errors     = 0;

      // Close all positions
      for (int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if (ticket == 0) continue;
         if (!PositionSelectByTicket(ticket)) continue;
         if ((int)PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
         if (symbol != "" && PositionGetString(POSITION_SYMBOL) != symbol) continue;

         if (trade.PositionClose(ticket))
            closed++;
         else
            errors++;
      }

      // Cancel all pending orders
      for (int i = OrdersTotal() - 1; i >= 0; i--)
      {
         ulong ticket = OrderGetTicket(i);
         if (ticket == 0) continue;
         if (!OrderSelect(ticket)) continue;
         if ((int)OrderGetInteger(ORDER_MAGIC) != MagicNumber) continue;
         if (symbol != "" && OrderGetString(ORDER_SYMBOL) != symbol) continue;

         if (trade.OrderDelete(ticket))
            cancelled++;
         else
            errors++;
      }

      WriteResponse("OK|FLATTEN|closed:" + IntegerToString(closed) +
                    "|cancelled:" + IntegerToString(cancelled) +
                    "|errors:" + IntegerToString(errors));
      Log("Flatten complete — closed:" + IntegerToString(closed) +
          " cancelled:" + IntegerToString(cancelled) +
          " errors:" + IntegerToString(errors));
   }

   // ── POSITIONS: report open positions ──────
   else if (action == "POSITIONS")
   {
      string result = "OK|POSITIONS";
      int count2 = 0;

      for (int i = 0; i < PositionsTotal(); i++)
      {
         ulong ticket = PositionGetTicket(i);
         if (ticket == 0) continue;
         if (!PositionSelectByTicket(ticket)) continue;
         if ((int)PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

         string sym   = PositionGetString(POSITION_SYMBOL);
         string pside = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "Long" : "Short";
         double vol   = PositionGetDouble(POSITION_VOLUME);
         double open  = PositionGetDouble(POSITION_PRICE_OPEN);
         double psl   = PositionGetDouble(POSITION_SL);
         double ptp   = PositionGetDouble(POSITION_TP);
         double profit= PositionGetDouble(POSITION_PROFIT);
         double cur   = SymbolInfoDouble(sym,
                        pside == "Long" ? SYMBOL_BID : SYMBOL_ASK);

         result += "|" + sym + "," + pside + "," +
                   DoubleToString(vol, 2) + "," +
                   DoubleToString(open, 2) + "," +
                   DoubleToString(cur, 2) + "," +
                   DoubleToString(psl, 2) + "," +
                   DoubleToString(ptp, 2) + "," +
                   DoubleToString(profit, 2) + "," +
                   IntegerToString((int)ticket);
         count2++;
      }

      if (count2 == 0)
         result += "|NONE";

      WriteResponse(result);
   }

   // ── ORDERS: report pending/open orders ────
   else if (action == "ORDERS")
   {
      string result = "OK|ORDERS";
      int count2 = 0;

      for (int i = 0; i < OrdersTotal(); i++)
      {
         ulong ticket = OrderGetTicket(i);
         if (ticket == 0) continue;
         if (!OrderSelect(ticket)) continue;
         if ((int)OrderGetInteger(ORDER_MAGIC) != MagicNumber) continue;

         string sym    = OrderGetString(ORDER_SYMBOL);
         long   otype  = OrderGetInteger(ORDER_TYPE);
         string side   = (otype == ORDER_TYPE_BUY || otype == ORDER_TYPE_BUY_LIMIT || otype == ORDER_TYPE_BUY_STOP)
                         ? "Buy" : "Sell";
         string otype_str;
         switch((int)otype)
         {
            case ORDER_TYPE_BUY:        otype_str = "Market";     break;
            case ORDER_TYPE_SELL:       otype_str = "Market";     break;
            case ORDER_TYPE_BUY_LIMIT:  otype_str = "BuyLimit";   break;
            case ORDER_TYPE_SELL_LIMIT: otype_str = "SellLimit";  break;
            case ORDER_TYPE_BUY_STOP:   otype_str = "BuyStop";    break;
            case ORDER_TYPE_SELL_STOP:  otype_str = "SellStop";   break;
            default:                    otype_str = "Unknown";     break;
         }
         double vol    = OrderGetDouble(ORDER_VOLUME_CURRENT);
         double price  = OrderGetDouble(ORDER_PRICE_OPEN);
         double osl    = OrderGetDouble(ORDER_SL);
         double otp    = OrderGetDouble(ORDER_TP);

         result += "|" + sym + "," + side + "," + otype_str + "," +
                   DoubleToString(vol, 2) + "," +
                   DoubleToString(price, 2) + "," +
                   DoubleToString(osl, 2) + "," +
                   DoubleToString(otp, 2) + "," +
                   IntegerToString((int)ticket);
         count2++;
      }

      if (count2 == 0)
         result += "|NONE";

      WriteResponse(result);
   }

   // ── PING: heartbeat check ─────────────────
   else if (action == "PING")
   {
      WriteResponse("OK|PONG|" + IntegerToString((int)TimeCurrent()));
   }

   // ── ACCOUNT: return balance/equity/margin ──
   else if (action == "ACCOUNT")
   {
      double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
      double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
      double margin     = AccountInfoDouble(ACCOUNT_MARGIN);
      double freeMargin = AccountInfoDouble(ACCOUNT_FREEMARGIN);
      double profit     = AccountInfoDouble(ACCOUNT_PROFIT);
      string currency   = AccountInfoString(ACCOUNT_CURRENCY);
      string name       = AccountInfoString(ACCOUNT_NAME);
      long   login      = AccountInfoInteger(ACCOUNT_LOGIN);
      double leverage   = (double)AccountInfoInteger(ACCOUNT_LEVERAGE);

      // Calculate today's closed PnL from deal history
      double closedPnl = 0.0;
      datetime dayStart = StringToTime(TimeToString(TimeCurrent(), TIME_DATE));
      if (HistorySelect(dayStart, TimeCurrent()))
      {
         int deals = HistoryDealsTotal();
         for (int i = 0; i < deals; i++)
         {
            ulong ticket = HistoryDealGetTicket(i);
            if (ticket == 0) continue;
            long dealType = HistoryDealGetInteger(ticket, DEAL_TYPE);
            // Only count position close deals (entry=0 is buy, entry=1 is sell, entry=2 is out)
            long dealEntry = HistoryDealGetInteger(ticket, DEAL_ENTRY);
            if (dealEntry == DEAL_ENTRY_OUT || dealEntry == DEAL_ENTRY_INOUT)
               closedPnl += HistoryDealGetDouble(ticket, DEAL_PROFIT);
         }
      }

      WriteResponse(
         "OK|ACCOUNT|" +
         "balance="    + DoubleToString(balance, 2)    + "," +
         "equity="     + DoubleToString(equity, 2)     + "," +
         "margin="     + DoubleToString(margin, 2)     + "," +
         "free="       + DoubleToString(freeMargin, 2) + "," +
         "profit="     + DoubleToString(profit, 2)     + "," +
         "closedpnl="  + DoubleToString(closedPnl, 2)  + "," +
         "currency="   + currency                       + "," +
         "leverage="   + DoubleToString(leverage, 0)   + "," +
         "login="      + IntegerToString(login)         + "," +
         "name="       + name
      );
   }

   else
   {
      WriteResponse("ERROR|Unknown action: " + action);
   }
}

//+------------------------------------------------------------------+
//| Write response file for backend to read                          |
//+------------------------------------------------------------------+
void WriteResponse(string msg)
{
   int handle = FileOpen(RESPONSE_FILE, FILE_WRITE | FILE_TXT | FILE_COMMON | FILE_ANSI);
   if (handle != INVALID_HANDLE)
   {
      FileWriteString(handle, msg);
      FileClose(handle);
   }
}

//+------------------------------------------------------------------+
//| Write heartbeat so backend knows EA is alive                     |
//+------------------------------------------------------------------+
void WriteHeartbeat()
{
   int handle = FileOpen(HEARTBEAT_FILE, FILE_WRITE | FILE_TXT | FILE_COMMON | FILE_ANSI);
   if (handle != INVALID_HANDLE)
   {
      FileWriteString(handle, IntegerToString((int)TimeCurrent()));
      FileClose(handle);
   }
}

//+------------------------------------------------------------------+
//| Logging helper                                                   |
//+------------------------------------------------------------------+
void Log(string msg)
{
   if (EnableLogs)
      Print("[BJ-EA] " + msg);
}
//+------------------------------------------------------------------+
