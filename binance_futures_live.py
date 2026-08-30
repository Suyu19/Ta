#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance USDⓈ-M Futures live execution layer for Strategy v2.0.

Design goals
------------
- No API keys in source code.
- HMAC signed REST requests.
- Hedge Mode required (FLEX uses a SHORT hedge while LONG inventory may exist).
- Idempotent clientOrderId handling.
- Quantity rounded with exchangeInfo filters.
- Before any risk-reducing MARKET order, re-read the real position and cap
  quantity so a close cannot flip the exchange position.
- Account/margin checks before NEW risk.
- 418/429 backoff.
- A first live bootstrap refuses to adopt pre-existing BTC/ETH positions/orders.

Environment
-----------
BINANCE_API_KEY
BINANCE_API_SECRET
BINANCE_FUTURES_BASE_URL=https://fapi.binance.com
BINANCE_RECV_WINDOW=5000
LIVE_LEVERAGE=10
LIVE_MAX_INITIAL_MARGIN_UTIL=0.50
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlencode

import requests


class BinanceLiveError(RuntimeError):
    pass


class BinanceRateLimitError(BinanceLiveError):
    pass


class BinanceReconciliationError(BinanceLiveError):
    pass


@dataclass(frozen=True)
class SymbolFilter:
    symbol: str
    qty_step: float
    min_qty: float
    max_qty: float
    min_notional: float
    price_tick: float = 0.0
    limit_qty_step: float = 0.0
    limit_min_qty: float = 0.0
    limit_max_qty: float = 0.0


@dataclass
class FillResult:
    symbol: str
    side: str
    position_side: str
    requested_qty: float
    executed_qty: float
    avg_price: float
    quote_qty: float
    commission: float
    realized_pnl: float
    order_id: int
    client_order_id: str
    raw: dict


class BinanceFuturesLive:
    CLIENT_ID_RE = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")

    def __init__(self):
        self.base_url = os.getenv(
            "BINANCE_FUTURES_BASE_URL",
            "https://fapi.binance.com",
        ).rstrip("/")
        self.api_key = os.getenv("BINANCE_API_KEY", "").strip()
        self.api_secret = os.getenv("BINANCE_API_SECRET", "").strip()

        if not self.api_key or not self.api_secret:
            raise BinanceLiveError(
                "LIVE mode requires BINANCE_API_KEY and BINANCE_API_SECRET."
            )

        self.recv_window = max(
            1000,
            min(60000, int(os.getenv("BINANCE_RECV_WINDOW", "5000"))),
        )
        self.leverage = max(1, min(125, int(os.getenv("LIVE_LEVERAGE", "10"))))
        self.max_initial_margin_util = float(
            os.getenv("LIVE_MAX_INITIAL_MARGIN_UTIL", "0.50")
        )
        self.max_initial_margin_util = max(
            0.10, min(0.90, self.max_initial_margin_util)
        )

        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": self.api_key})

        self._time_offset_ms = 0
        self._last_time_sync_mono = 0.0
        self._filters: Dict[str, SymbolFilter] = {}
        self._last_exchange_info_mono = 0.0

    # --------------------------------------------------------
    # HTTP / signing
    # --------------------------------------------------------

    @staticmethod
    def _retry_after(resp: requests.Response, attempt: int) -> float:
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                return max(1.0, float(raw))
            except Exception:
                pass
        return min(300.0, 10.0 * (2 ** attempt))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        signed: bool = False,
        timeout: float = 15.0,
        max_retries: int = 4,
    ):
        params = dict(params or {})

        for attempt in range(max_retries):
            if signed:
                self.sync_time_if_needed()
                params["timestamp"] = self.timestamp_ms()
                params["recvWindow"] = self.recv_window
                query = urlencode(params, doseq=True)
                signature = hmac.new(
                    self.api_secret.encode("utf-8"),
                    query.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                send_params = dict(params)
                send_params["signature"] = signature
            else:
                send_params = params

            try:
                resp = self.session.request(
                    method.upper(),
                    self.base_url + path,
                    params=send_params,
                    timeout=timeout,
                )
            except requests.RequestException:
                if attempt + 1 >= max_retries:
                    raise
                time.sleep(min(10.0, 1.5 * (2 ** attempt)))
                continue

            if resp.status_code in (418, 429):
                wait = self._retry_after(resp, attempt)
                if attempt + 1 >= max_retries:
                    raise BinanceRateLimitError(
                        f"{resp.status_code} rate limited for {path}; "
                        f"Retry-After={wait}s"
                    )
                time.sleep(wait)
                continue

            # Timestamp drift. Resync once and retry.
            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                except Exception:
                    payload = {}
                code = payload.get("code")
                if code == -1021 and attempt + 1 < max_retries:
                    self.sync_time(force=True)
                    continue

                msg = payload.get("msg") or resp.text[:500]
                raise BinanceLiveError(
                    f"Binance {method.upper()} {path} failed: "
                    f"HTTP {resp.status_code}, code={code}, msg={msg}"
                )

            try:
                return resp.json()
            except Exception:
                return {}

        raise BinanceLiveError(f"Request retry loop exhausted: {path}")

    def public_get(self, path: str, params: Optional[dict] = None):
        return self._request("GET", path, params=params, signed=False)

    def signed_get(self, path: str, params: Optional[dict] = None):
        return self._request("GET", path, params=params, signed=True)

    def signed_post(self, path: str, params: Optional[dict] = None):
        return self._request("POST", path, params=params, signed=True)

    def signed_delete(self, path: str, params: Optional[dict] = None):
        return self._request("DELETE", path, params=params, signed=True)

    def sync_time(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_time_sync_mono < 1800:
            return
        data = self.public_get("/fapi/v1/time")
        server = int(data["serverTime"])
        local = int(time.time() * 1000)
        self._time_offset_ms = server - local
        self._last_time_sync_mono = now

    def sync_time_if_needed(self):
        self.sync_time(False)

    def timestamp_ms(self) -> int:
        return int(time.time() * 1000) + int(self._time_offset_ms)

    # --------------------------------------------------------
    # Account / position configuration
    # --------------------------------------------------------

    def account(self) -> dict:
        """Current USDⓈ-M account snapshot (V3)."""
        return self.signed_get("/fapi/v3/account")

    def account_v2(self) -> dict:
        """Account permission/config snapshot; V2 includes canTrade."""
        return self.signed_get("/fapi/v2/account")

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1","true","yes","on"}
        return bool(value)

    def position_mode(self) -> bool:
        data = self.signed_get("/fapi/v1/positionSide/dual")
        return self._as_bool(data.get("dualSidePosition"))

    def multi_assets_mode(self) -> bool:
        data = self.signed_get("/fapi/v1/multiAssetsMargin")
        return self._as_bool(data.get("multiAssetsMargin"))

    def positions(self, symbol: Optional[str] = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        return self.signed_get("/fapi/v3/positionRisk", params)

    def open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        return self.signed_get("/fapi/v1/openOrders", params)

    def change_leverage(self, symbol: str, leverage: Optional[int] = None):
        leverage = leverage or self.leverage
        return self.signed_post(
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": int(leverage)},
        )

    def ensure_cross_margin(self, symbol: str):
        try:
            return self.signed_post(
                "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": "CROSSED"},
            )
        except BinanceLiveError as exc:
            # -4046 = No need to change margin type.
            if "code=-4046" in str(exc):
                return {"code": -4046, "msg": "No need to change margin type."}
            raise

    def validate_environment(
        self,
        symbols: Iterable[str],
        *,
        require_clean: bool,
    ) -> dict:
        if not self.position_mode():
            raise BinanceLiveError(
                "Strategy v2.0 requires Binance Futures Hedge Mode. "
                "Enable Hedge Mode while the account has no conflicting "
                "positions/open orders, then restart the bot."
            )
        if self.multi_assets_mode():
            raise BinanceLiveError(
                "Strategy v2.0 was validated in Single-Asset Mode. "
                "Disable Multi-Assets Mode before LIVE trading."
            )

        acct_v2 = self.account_v2()
        if not self._as_bool(acct_v2.get("canTrade")):
            raise BinanceLiveError("Binance Futures account reports canTrade=false.")

        acct = self.account()
        syms = set(symbols)
        if require_clean:
            dirty_positions = []
            for p in self.positions():
                if p.get("symbol") not in syms:
                    continue
                if abs(float(p.get("positionAmt", 0.0))) > 0:
                    dirty_positions.append(
                        f"{p.get('symbol')}:{p.get('positionSide')}="
                        f"{p.get('positionAmt')}"
                    )
            dirty_orders = []
            for s in syms:
                for o in self.open_orders(s):
                    dirty_orders.append(
                        f"{s}:{o.get('clientOrderId') or o.get('orderId')}"
                    )

            if dirty_positions or dirty_orders:
                raise BinanceLiveError(
                    "First LIVE bootstrap requires BTC/ETH to be clean. "
                    f"Existing positions={dirty_positions or 'none'}, "
                    f"open_orders={dirty_orders or 'none'}. "
                    "This prevents the bot from accidentally adopting or "
                    "closing manual positions."
                )

        for symbol in syms:
            self.change_leverage(symbol, self.leverage)
            self.ensure_cross_margin(symbol)

        return acct

    # --------------------------------------------------------
    # Exchange filters / rounding
    # --------------------------------------------------------

    def _load_exchange_info(self):
        now = time.monotonic()
        if self._filters and now - self._last_exchange_info_mono < 3600:
            return

        info = self.public_get("/fapi/v1/exchangeInfo")
        filt: Dict[str, SymbolFilter] = {}

        for item in info.get("symbols", []):
            symbol = item.get("symbol")
            fmap = {x.get("filterType"): x for x in item.get("filters", [])}
            market_lot = fmap.get("MARKET_LOT_SIZE") or fmap.get("LOT_SIZE") or {}
            limit_lot = fmap.get("LOT_SIZE") or market_lot
            price_filter = fmap.get("PRICE_FILTER") or {}
            notional = fmap.get("MIN_NOTIONAL") or {}
            try:
                step = float(market_lot.get("stepSize", "0"))
                min_qty = float(market_lot.get("minQty", "0"))
                max_qty = float(market_lot.get("maxQty", "0"))
                limit_step = float(limit_lot.get("stepSize", step or 0))
                limit_min = float(limit_lot.get("minQty", min_qty or 0))
                limit_max = float(limit_lot.get("maxQty", max_qty or 0))
                price_tick = float(price_filter.get("tickSize", "0"))
                min_notional = float(
                    notional.get("notional")
                    or notional.get("minNotional")
                    or 0
                )
            except Exception:
                continue
            if step > 0:
                filt[symbol] = SymbolFilter(
                    symbol=symbol,
                    qty_step=step,
                    min_qty=min_qty,
                    max_qty=max_qty,
                    min_notional=min_notional,
                    price_tick=price_tick,
                    limit_qty_step=limit_step,
                    limit_min_qty=limit_min,
                    limit_max_qty=limit_max,
                )

        self._filters = filt
        self._last_exchange_info_mono = now

    def symbol_filter(self, symbol: str) -> SymbolFilter:
        self._load_exchange_info()
        if symbol not in self._filters:
            raise BinanceLiveError(f"No exchange filter found for {symbol}")
        return self._filters[symbol]

    @staticmethod
    def _floor_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        dval = Decimal(str(value))
        dstep = Decimal(str(step))
        units = (dval / dstep).to_integral_value(rounding=ROUND_DOWN)
        return float(units * dstep)

    def round_qty(self, symbol: str, qty: float) -> float:
        f = self.symbol_filter(symbol)
        q = self._floor_step(abs(float(qty)), f.qty_step)
        if q < f.min_qty:
            return 0.0
        if f.max_qty > 0:
            q = min(q, f.max_qty)
            q = self._floor_step(q, f.qty_step)
        return q

    def round_limit_qty(self, symbol: str, qty: float) -> float:
        f = self.symbol_filter(symbol)
        step = f.limit_qty_step or f.qty_step
        min_qty = f.limit_min_qty or f.min_qty
        max_qty = f.limit_max_qty or f.max_qty
        q = self._floor_step(abs(float(qty)), step)
        if q < min_qty:
            return 0.0
        if max_qty > 0:
            q = min(q, max_qty)
            q = self._floor_step(q, step)
        return q

    def round_price(self, symbol: str, price: float, *, side: str) -> float:
        f = self.symbol_filter(symbol)
        tick = float(f.price_tick or 0.0)
        if tick <= 0:
            return float(price)
        dval = Decimal(str(float(price)))
        dtick = Decimal(str(tick))
        rounding = ROUND_DOWN if side.upper() == "BUY" else ROUND_UP
        units = (dval / dtick).to_integral_value(rounding=rounding)
        return float(units * dtick)

    def qty_for_notional(self, symbol: str, notional: float, price: float) -> float:
        if notional <= 0 or price <= 0:
            return 0.0
        qty = self.round_qty(symbol, notional / price)
        f = self.symbol_filter(symbol)
        if qty > 0 and f.min_notional > 0 and qty * price < f.min_notional:
            return 0.0
        return qty

    # --------------------------------------------------------
    # Real position helpers
    # --------------------------------------------------------

    def position_map(self) -> dict[tuple[str, str], dict]:
        out = {}
        for p in self.positions():
            out[(p["symbol"], p["positionSide"])] = p
        return out

    def real_position_qty(self, symbol: str, position_side: str) -> float:
        for p in self.positions(symbol):
            if p.get("positionSide") == position_side:
                return abs(float(p.get("positionAmt", 0.0)))
        return 0.0

    def margin_snapshot(self) -> dict:
        a = self.account()
        margin_balance = float(a.get("totalMarginBalance", 0.0))
        initial_margin = float(a.get("totalInitialMargin", 0.0))
        available = float(a.get("availableBalance", 0.0))
        maint = float(a.get("totalMaintMargin", 0.0))
        util = initial_margin / margin_balance if margin_balance > 0 else math.inf
        return {
            "margin_balance": margin_balance,
            "wallet_balance": float(a.get("totalWalletBalance", 0.0)),
            "unrealized": float(a.get("totalUnrealizedProfit", 0.0)),
            "initial_margin": initial_margin,
            "maint_margin": maint,
            "available_balance": available,
            "initial_margin_util": util,
        }

    def check_new_risk(self, extra_notional: float):
        snap = self.margin_snapshot()
        if snap["margin_balance"] <= 0:
            raise BinanceLiveError("Margin balance <= 0; refusing new risk.")
        projected_im = snap["initial_margin"] + max(0.0, extra_notional) / self.leverage
        projected_util = projected_im / snap["margin_balance"]
        if projected_util > self.max_initial_margin_util:
            raise BinanceLiveError(
                "New-risk margin guard blocked order: "
                f"projected initial-margin utilization={projected_util:.2%}, "
                f"limit={self.max_initial_margin_util:.2%}."
            )
        if snap["available_balance"] <= 0:
            raise BinanceLiveError("availableBalance <= 0; refusing new risk.")
        return snap

    # --------------------------------------------------------
    # Order execution / idempotency
    # --------------------------------------------------------

    def query_order(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        p = {"symbol": symbol}
        if order_id is not None:
            p["orderId"] = int(order_id)
        elif client_order_id:
            p["origClientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id required")
        return self.signed_get("/fapi/v1/order", p)

    def user_trades_for_order(self, symbol: str, order_id: int) -> list[dict]:
        return self.signed_get(
            "/fapi/v1/userTrades",
            {"symbol": symbol, "orderId": int(order_id), "limit": 100},
        )

    def _normalize_fill(
        self,
        raw: dict,
        symbol: str,
        side: str,
        position_side: str,
        requested_qty: float,
        client_order_id: str,
    ) -> FillResult:
        order_id = int(raw.get("orderId", 0))
        trades = []
        if order_id:
            # The RESULT response can still omit commission detail.
            for _ in range(4):
                try:
                    trades = self.user_trades_for_order(symbol, order_id)
                except Exception:
                    trades = []
                if trades:
                    break
                time.sleep(0.25)

        if trades:
            qty = sum(abs(float(x.get("qty", 0.0))) for x in trades)
            quote = sum(abs(float(x.get("quoteQty", 0.0))) for x in trades)
            commission = sum(
                float(x.get("commission", 0.0))
                for x in trades
                if x.get("commissionAsset") in ("USDT", None)
            )
            realized = sum(float(x.get("realizedPnl", 0.0)) for x in trades)
            avg = quote / qty if qty > 0 else 0.0
        else:
            qty = abs(float(raw.get("executedQty", 0.0)))
            quote = abs(float(raw.get("cumQuote", 0.0) or 0.0))
            avg = float(raw.get("avgPrice", 0.0) or 0.0)
            if avg <= 0 and qty > 0 and quote > 0:
                avg = quote / qty
            commission = 0.0
            realized = 0.0

        return FillResult(
            symbol=symbol,
            side=side,
            position_side=position_side,
            requested_qty=requested_qty,
            executed_qty=qty,
            avg_price=avg,
            quote_qty=quote,
            commission=commission,
            realized_pnl=realized,
            order_id=order_id,
            client_order_id=raw.get("clientOrderId") or client_order_id,
            raw=raw,
        )

    def cancel_order(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        p = {"symbol": symbol}
        if order_id is not None:
            p["orderId"] = int(order_id)
        elif client_order_id:
            p["origClientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id required")
        try:
            return self.signed_delete("/fapi/v1/order", p)
        except BinanceLiveError as exc:
            # If an order filled/cancelled during the DELETE race, the caller
            # can query the final state by its id.  Do not hide unrelated errors.
            if any(code in str(exc) for code in ("code=-2011", "code=-2013")):
                try:
                    return self.query_order(
                        symbol, order_id=order_id, client_order_id=client_order_id
                    )
                except Exception:
                    pass
            raise

    def limit_order(
        self,
        *,
        symbol: str,
        side: str,
        position_side: str,
        qty: float,
        price: float,
        client_order_id: str,
        reducing: bool,
        post_only: bool = True,
    ) -> dict:
        """Place a persistent LIMIT order.

        Range Alpha uses GTX (post-only) so an entry/TP that would cross the
        book is rejected instead of silently paying taker fees.  In Hedge Mode
        Binance does not accept reduceOnly; risk-reducing quantities are capped
        against the live position before submission.
        """
        side = side.upper()
        position_side = position_side.upper()
        if not self.CLIENT_ID_RE.match(client_order_id):
            raise BinanceLiveError(
                f"Invalid clientOrderId format: {client_order_id!r}"
            )

        qty = self.round_limit_qty(symbol, qty)
        price = self.round_price(symbol, price, side=side)
        if qty <= 0 or price <= 0:
            raise BinanceLiveError(
                f"{symbol} LIMIT qty/price rounds below exchange minimum."
            )
        f = self.symbol_filter(symbol)
        if f.min_notional > 0 and qty * price < f.min_notional:
            raise BinanceLiveError(
                f"{symbol} LIMIT notional {qty*price:.8g} is below "
                f"minNotional {f.min_notional:.8g}."
            )

        if reducing:
            live_qty = self.real_position_qty(symbol, position_side)
            qty = self.round_limit_qty(symbol, min(qty, live_qty))
            if qty <= 0:
                raise BinanceLiveError(
                    f"No {symbol} {position_side} live quantity remains to reduce."
                )
        else:
            self.check_new_risk(qty * price)

        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "LIMIT",
            "timeInForce": "GTX" if post_only else "GTC",
            "quantity": self._format_decimal(qty),
            "price": self._format_decimal(price),
            "newClientOrderId": client_order_id,
            # ACK is intentional for persistent GTX orders. Binance documents
            # that RESULT with special LIMIT timeInForce can wait for a final
            # order state; Range Alpha needs an immediate order id and then
            # manages the resting order explicitly via query/cancel.
            "newOrderRespType": "ACK",
        }
        try:
            return self.signed_post("/fapi/v1/order", params)
        except (requests.RequestException, BinanceLiveError) as exc:
            # Same idempotency rule as MARKET: a network failure after POST
            # must be resolved by querying the exact client id before retrying.
            try:
                return self.query_order(symbol, client_order_id=client_order_id)
            except Exception:
                raise exc

    def ticker_price(self, symbol: str) -> float:
        raw = self.public_get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(raw.get("price", 0.0) or 0.0)

    def market_order(
        self,
        *,
        symbol: str,
        side: str,
        position_side: str,
        qty: float,
        client_order_id: str,
        reducing: bool,
        reference_price: Optional[float] = None,
    ) -> FillResult:
        side = side.upper()
        position_side = position_side.upper()

        if not self.CLIENT_ID_RE.match(client_order_id):
            raise BinanceLiveError(
                f"Invalid clientOrderId format: {client_order_id!r}"
            )

        qty = self.round_qty(symbol, qty)
        if qty <= 0:
            raise BinanceLiveError(
                f"{symbol} order qty rounds below exchange minimum."
            )

        if reducing:
            # In Hedge Mode Binance does not accept reduceOnly. Instead, cap
            # the opposite-side MARKET quantity to the live position so this
            # order cannot flip the position.
            live_qty = self.real_position_qty(symbol, position_side)
            qty = self.round_qty(symbol, min(qty, live_qty))
            if qty <= 0:
                raise BinanceLiveError(
                    f"No {symbol} {position_side} live quantity remains to reduce."
                )
        else:
            if reference_price is None or reference_price <= 0:
                raise BinanceLiveError(
                    "reference_price required for new-risk margin check"
                )
            self.check_new_risk(qty * reference_price)

        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "MARKET",
            "quantity": self._format_decimal(qty),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }

        try:
            raw = self.signed_post("/fapi/v1/order", params)
        except (requests.RequestException, BinanceLiveError) as exc:
            # Network ambiguity after POST: query the same client ID before
            # ever attempting another order.
            try:
                raw = self.query_order(
                    symbol,
                    client_order_id=client_order_id,
                )
            except Exception:
                raise exc

        fill = self._normalize_fill(
            raw,
            symbol,
            side,
            position_side,
            qty,
            client_order_id,
        )
        if fill.executed_qty <= 0:
            # Query once more in case RESULT/ACK propagation lagged.
            qraw = self.query_order(symbol, order_id=fill.order_id)
            fill = self._normalize_fill(
                qraw,
                symbol,
                side,
                position_side,
                qty,
                client_order_id,
            )

        if fill.executed_qty <= 0:
            raise BinanceLiveError(
                f"Order {client_order_id} has no confirmed executed quantity."
            )
        return fill

    @staticmethod
    def _format_decimal(value: float) -> str:
        # Decimal(str(...)) avoids binary-float tails such as
        # 79999.899999999994 after tick rounding. Binance filter validation is
        # strict, so send a clean fixed-point decimal string.
        d = Decimal(str(value))
        s = format(d.normalize(), "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"
