import hashlib
import hmac
import logging
import time
from json.decoder import JSONDecodeError
from typing import Dict, List, Optional, Tuple, Union, cast
from urllib.parse import urlencode

from rotkehlchen import typing
from rotkehlchen.errors import RemoteError
from rotkehlchen.exchange import Exchange
from rotkehlchen.fval import FVal
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.order_formatting import Trade
from rotkehlchen.utils import (
    cache_response_timewise,
    createTimeStamp,
    get_pair_position,
    rlk_jsonloads,
)

logger = logging.getLogger(__name__)

BITTREX_MARKET_METHODS = {
    'getopenorders',
    'cancel',
    'sellmarket',
    'selllimit',
    'buymarket',
    'buylimit'
}
BITTREX_ACCOUNT_METHODS = {
    'getbalances',
    'getbalance',
    'getdepositaddress',
    'withdraw',
    'getorderhistory'
}


def bittrex_pair_to_world(pair: str) -> str:
    return pair.replace('-', '_')


def world_pair_to_bittrex(pair: str) -> str:
    return pair.replace('_', '-')


def trade_from_bittrex(bittrex_trade: Dict) -> Trade:
    """Turn a bittrex trade returned from bittrex trade history to our common trade
    history format"""
    amount = FVal(bittrex_trade['Quantity']) - FVal(bittrex_trade['QuantityRemaining'])
    rate = FVal(bittrex_trade['PricePerUnit'])
    order_type = bittrex_trade['OrderType']
    bittrex_price = FVal(bittrex_trade['Price'])
    bittrex_commission = FVal(bittrex_trade['Commission'])
    pair = bittrex_pair_to_world(bittrex_trade['Exchange'])
    base_currency = get_pair_position(pair, 'first')
    if order_type == 'LIMIT_BUY':
        order_type = 'buy'
        cost = bittrex_price + bittrex_commission
        fee = bittrex_commission
    elif order_type == 'LIMIT_SEL':
        order_type = 'sell'
        cost = bittrex_price - bittrex_commission
        fee = bittrex_commission
    else:
        raise ValueError('Got unexpected order type "{}" for bittrex trade'.format(order_type))

    return Trade(
        timestamp=bittrex_trade['TimeStamp'],
        pair=pair,
        type=order_type,
        rate=rate,
        cost=cost,
        cost_currency=base_currency,
        fee=fee,
        fee_currency=base_currency,
        amount=amount,
        location='bittrex'
    )


class Bittrex(Exchange):
    def __init__(
            self,
            api_key: typing.ApiKey,
            secret: typing.ApiSecret,
            inquirer: Inquirer,
            data_dir: typing.FilePath
    ):
        super(Bittrex, self).__init__('bittrex', api_key, secret, data_dir)
        self.apiversion = 'v1.1'
        self.uri = 'https://bittrex.com/api/{}/'.format(self.apiversion)
        self.inquirer = inquirer

    def first_connection(self):
        self.first_connection_made = True

    def validate_api_key(self) -> Tuple[bool, str]:
        try:
            self.api_query('getbalance', {'currency': 'BTC'})
        except ValueError as e:
            error = str(e)
            if error == 'APIKEY_INVALID':
                return False, 'Provided API Key is invalid'
            elif error == 'INVALID_SIGNATURE':
                return False, 'Provided API Secret is invalid'
            else:
                raise
        return True, ''

    def api_query(
            self,
            method: str,
            options: Optional[Dict] = None,
    ) -> Union[List, Dict]:
        """
        Queries Bittrex with given method and options
        """
        if not options:
            options = {}
        nonce = str(int(time.time() * 1000))
        method_type = 'public'

        if method in BITTREX_MARKET_METHODS:
            method_type = 'market'
        elif method in BITTREX_ACCOUNT_METHODS:
            method_type = 'account'

        request_url = self.uri + method_type + '/' + method + '?'

        if method_type != 'public':
            request_url += 'apikey=' + self.api_key.decode() + "&nonce=" + nonce + '&'

        request_url += urlencode(options)
        signature = hmac.new(
            self.secret,
            request_url.encode(),
            hashlib.sha512
        ).hexdigest()
        self.session.headers.update({'apisign': signature})
        response = self.session.get(request_url)
        try:
            json_ret = rlk_jsonloads(response.text)
        except JSONDecodeError:
            raise RemoteError('Bittrex returned invalid JSON response')

        if json_ret['success'] is not True:
            raise RemoteError(json_ret['message'])
        return json_ret['result']

    def get_btc_price(self, asset: typing.BlockchainAsset) -> Optional[FVal]:
        if asset == 'BTC':
            return None
        btc_price = None
        btc_pair = 'BTC-' + asset
        for market in self.markets:
            if market['MarketName'] == btc_pair:
                btc_price = FVal(market['Last'])
                break

        return btc_price

    @cache_response_timewise()
    def query_balances(self) -> Tuple[Optional[dict], str]:
        try:
            self.markets = self.api_query('getmarketsummaries')
            resp = self.api_query('getbalances')
        except RemoteError as e:
            msg = (
                'Bittrex API request failed. Could not reach bittrex due '
                'to {}'.format(e)
            )
            logger.error(msg)
            return None, msg

        returned_balances = dict()
        for entry in resp:
            currency = entry['Currency']
            asset_btc_price = self.get_btc_price(currency)
            usd_price = self.inquirer.find_usd_price(
                asset=currency,
                asset_btc_price=asset_btc_price
            )

            balance = dict()
            balance['amount'] = FVal(entry['Balance'])
            balance['usd_value'] = FVal(balance['amount']) * usd_price
            returned_balances[currency] = balance

        return returned_balances, ''

    def query_trade_history(
            self,
            start_ts: typing.Timestamp,
            end_ts: typing.Timestamp,
            end_at_least_ts: typing.Timestamp,
            market: Optional[str] = None,
            count: Optional[int] = None,
    ) -> List:

        options: Dict[str, Union[str, int]] = dict()
        cache = self.check_trades_cache(start_ts, end_at_least_ts)
        cache = cast(List, cache)
        if market is not None:
            options['market'] = world_pair_to_bittrex(market)
        elif cache is not None:
            return cache

        if count is not None:
            options['count'] = count
        order_history = self.api_query('getorderhistory', options)

        returned_history = list()
        for order in order_history:
            order_timestamp = createTimeStamp(order['TimeStamp'], formatstr="%Y-%m-%dT%H:%M:%S.%f")
            if start_ts is not None and order_timestamp < start_ts:
                continue
            if end_ts is not None and order_timestamp > end_ts:
                break
            order['TimeStamp'] = order_timestamp
            returned_history.append(order)

        self.update_trades_cache(returned_history, start_ts, end_ts)
        return returned_history
